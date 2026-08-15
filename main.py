"""
Main orchestrator script for processing alerts and extracting video clips
"""
import argparse
import logging
import os
import sys
import uuid
import json
import queue
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional

import paho.mqtt.client as mqtt

from src.core.api_client import APIClient
from src.core.clip_extractor import ClipExtractor
from src.core.s3_uploader import S3Uploader
from src.core.email_sender import EmailSender
from src.utils.logger_config import setup_logging, get_logger, PerformanceLogger

from src.utils.device_utils import get_device_id
from src.utils.status_manager import publish_status
from src.utils.aws_utils import setup_aws_credentials, check_aws_credentials
from src.utils.config_manager import load_config, parse_config
from src.utils.progress_utils import LoggingTqdm
from src.utils.cleanup_utils import cleanup_recordings
from src.utils.side_video_utils import sync_side_videos
from src.core.alert_processor import process_alert
from src.tests.test_connectivity import run_connectivity_tests


def setup_resume_logger(log_dir: str) -> logging.Logger:
    """Setup resume log file for progress bar updates"""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    resume_log_file = log_path / "alert_processor_resume.log"
    resume_log_handler = logging.FileHandler(resume_log_file, encoding="utf-8")
    resume_log_handler.setLevel(logging.INFO)
    resume_log_formatter = logging.Formatter(
        fmt="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    resume_log_handler.setFormatter(resume_log_formatter)
    resume_logger = logging.getLogger("resume")
    resume_logger.setLevel(logging.INFO)
    resume_logger.addHandler(resume_log_handler)
    resume_logger.propagate = False
    return resume_logger


def get_fetch_date(date_cursor: Optional[int]) -> str:
    """Calculate fetch date based on date cursor"""
    if date_cursor is not None:
        current_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        target_date = current_date + timedelta(days=date_cursor)
        return target_date.strftime('%Y-%m-%d')
    else:
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d')


def initialize_email_sender(config, logger):
    """Initialize email sender if enabled"""
    email_enabled = config.get("email_enabled", False)
    if not email_enabled:
        return None
    
    try:
        email_sender = EmailSender(
            from_email=config["from_email"],
            to_emails=config["to_emails"],
            use_tls=config["use_tls"]
        )
        return email_sender
    except Exception as e:
        logger.warning(f"Failed to initialize email sender: {e}", exc_info=True)
        logger.warning("Continuing without email notifications...")
        return None


def run_wait_forever(
    device_id: str,
    date_cursor: Optional[int],
    config: Dict,
    api_client: APIClient,
    resume_logger: logging.Logger,
    logger,
) -> None:
    """
    Listen forever on "storeyes/<device-id>/alert-processing" and process each "start"
    message as it arrives, never returning.

    Runs two threads:
      - The listener: paho-mqtt's own background network thread (started by loop_start()),
        which stays connected and calls on_message for every incoming publish.
      - The processor: a single worker thread that pulls jobs off a queue and runs
        sync_side_videos + process_alerts_for_date for each one, one at a time.

    Processing is single-flight: a "start" received while the worker is busy is rejected
    (not queued) and answered with an "already running" response instead of a "processing"
    one, on "storeyes/alert-processing/response".
    """
    mqtt_host = os.environ.get("MQTT_HOST", "18.100.207.236")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    mqtt_user = os.environ.get("MQTT_USER", "storeyes")
    mqtt_pass = os.environ.get("MQTT_PASS", "12345")
    request_topic = f"storeyes/{device_id}/alert-processing"
    response_topic = "storeyes/alert-processing/response"

    work_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    busy = threading.Event()

    def publish_response(client, message: str) -> None:
        try:
            client.publish(response_topic, json.dumps({"response": message}), qos=1)
        except Exception as e:
            logger.error(f"Failed to publish response on {response_topic}: {e}", exc_info=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {mqtt_host}:{mqtt_port}")
            client.subscribe(request_topic, qos=1)
            logger.info(f"Subscribed to topic: {request_topic}")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code {reason_code}")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON message: {e}")
            return

        action = payload.get("action")
        date = payload.get("date")
        date_provided = date is not None and date != ""

        if action not in ("start", "abort"):
            logger.warning(f"Invalid action '{action}' in message. Expected 'start' or 'abort'")
            return

        logger.info(f"Received message on topic {msg.topic}: action={action}, date={date}")

        if action == "abort":
            logger.info("Received 'abort' action from broker; continuing to listen")
            return

        # action == "start". on_message runs in paho's single network thread, so this
        # check-then-set is not racing against another on_message call.
        if busy.is_set():
            logger.warning("Received 'start' while a run is already in progress; rejecting")
            publish_response(client, "processing already running")
            return

        busy.set()
        work_queue.put(date if date_provided else None)
        publish_response(client, "processing")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
        if reason_code != 0:
            logger.warning(f"Unexpected MQTT disconnection (reason_code={reason_code}, flags={disconnect_flags})")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(mqtt_user, mqtt_pass)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    logger.info(f"Connecting to MQTT broker at {mqtt_host}:{mqtt_port}...")
    client.connect(mqtt_host, mqtt_port, keepalive=60)
    client.loop_start()

    def worker():
        while True:
            broker_date = work_queue.get()
            try:
                fetch_date = broker_date or get_fetch_date(date_cursor)
                if broker_date:
                    logger.info(f"Using date from broker message: {fetch_date}")
                else:
                    logger.info(f"No date in broker message, using default: {fetch_date}")

                sync_side_videos(api_client, fetch_date, config["local_source_dir"], logger)

                cycle_correlation_id = str(uuid.uuid4())
                cycle_logger = get_logger(__name__, {"correlation_id": cycle_correlation_id})
                process_alerts_for_date(
                    fetch_date, date_cursor, config, device_id, api_client,
                    cycle_correlation_id, resume_logger, cycle_logger
                )
            except Exception as e:
                logger.error(f"Unexpected error while processing broker message: {e}", exc_info=True)
            finally:
                busy.clear()
                work_queue.task_done()

    processing_thread = threading.Thread(target=worker, name="alert-processor", daemon=True)
    processing_thread.start()

    logger.info(f"Listening forever on {request_topic}; responses published on {response_topic}")
    processing_thread.join()


def process_alerts_for_date(
    fetch_date: str,
    date_cursor: Optional[int],
    config: Dict,
    device_id: str,
    api_client: APIClient,
    correlation_id: str,
    resume_logger: logging.Logger,
    logger,
) -> bool:
    """
    Fetch, extract, upload, and email alerts for a single date.

    Returns True if there were no alerts or all of them succeeded, False if the alert
    fetch failed, the local recordings couldn't be loaded, or any alert failed.
    """
    s3_upload_prefix = config["s3_upload_prefix_template"].replace("{device-id}", device_id).replace("{date}", fetch_date)

    logger.info(f"Source: Loading video chunks from local directory '{config['local_source_dir']}'")
    logger.info(f"Destination: Uploading processed clips to S3 bucket '{config['s3_bucket']}/{s3_upload_prefix}'")

    s3_uploader = S3Uploader(config["aws_region"], config["s3_bucket"], s3_upload_prefix)

    try:
        clip_extractor = ClipExtractor(
            before_seconds=config["before_seconds"],
            after_seconds=config["after_seconds"],
            output_dir=config["output_dir"],
            local_source_dir=config["local_source_dir"],
        )
    except FileNotFoundError as e:
        logger.error(f"Cannot process {fetch_date}: {e}")
        return False

    email_sender = initialize_email_sender(config, logger)

    if date_cursor is not None:
        days_ago = abs(date_cursor) if date_cursor < 0 else 0
        if date_cursor < 0:
            logger.info(f"Using date cursor {date_cursor} ({days_ago} day{'s' if days_ago != 1 else ''} ago): {fetch_date}")
        elif date_cursor > 0:
            logger.info(f"Using date cursor {date_cursor} ({date_cursor} day{'s' if date_cursor != 1 else ''} in future): {fetch_date}")
        else:
            logger.info(f"Using date cursor 0 (today): {fetch_date}")
    else:
        logger.info(f"No date cursor provided, using current date: {fetch_date}")

    # Fetch alerts
    try:
        with PerformanceLogger(logger, "fetch_alerts", fetch_date=fetch_date):
            alerts = api_client.get_alerts(fetch_date)
    except Exception as e:
        logger.error(f"Failed to fetch alerts: {e}", exc_info=True)
        return False

    if not alerts:
        logger.info(f"No alerts found for date {fetch_date}")
        publish_status("EMPTY", board_id=device_id)
        return True

    # Determine status string based on date_cursor
    processing_status = "MF_PROCESSING" if date_cursor is not None else "PROCESSING"

    # Write PROCESSING/MF_PROCESSING status with total alerts count
    total_alerts = len(alerts)
    publish_status(processing_status, total_count=total_alerts, processed_count=0, board_id=device_id)
    logger.info(f"Status file updated: {processing_status} with {total_alerts} total alerts")

    # Process each alert with progress bar
    successful = 0
    failed = 0
    processed_alerts = []

    with LoggingTqdm(total=len(alerts), desc="Processing alerts", unit="alert",
                     bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]',
                     resume_logger=resume_logger) as pbar:
        for alert in alerts:
            alert_id = alert.get("id")
            alert_logger = get_logger(__name__, {"correlation_id": correlation_id, "alert_id": alert_id})

            pbar.set_description(f"Processing alert {alert_id}")

            with PerformanceLogger(alert_logger, f"process_alert_{alert_id}", alert_id=alert_id):
                success, video_url, thumbnail_url = process_alert(
                    alert, clip_extractor, s3_uploader, api_client,
                    max_retries=config["max_retries"], retry_delay_seconds=config["retry_delay_seconds"]
                )

            if success:
                successful += 1
                processed_alerts.append((alert, video_url, thumbnail_url))
                pbar.set_postfix({"✓": successful, "✗": failed})
            else:
                failed += 1
                pbar.set_postfix({"✓": successful, "✗": failed})
                logger.error(f"Alert {alert_id} processing failed", extra={"alert_id": alert_id})

            # Update status file with successful count
            publish_status(processing_status, total_count=total_alerts, processed_count=successful, board_id=device_id)

            pbar.update(1)

    # Send batch email with all processed alerts if email sender is configured
    if email_sender and processed_alerts:
        with LoggingTqdm(desc="Sending email notification", total=1,
                         bar_format='{desc}: {elapsed}', resume_logger=resume_logger) as pbar:
            with PerformanceLogger(logger, "send_batch_email", alert_count=len(processed_alerts)):
                email_sender.send_batch_alert_email(processed_alerts)
            pbar.update(1)

    # Write FINISHED status
    publish_status("FINISHED", total_count=total_alerts, processed_count=successful, board_id=device_id)
    logger.info(f"Status file updated: FINISHED with {total_alerts} total alerts, {successful} successfully processed")

    # Cleanup recordings for the processed date
    cleanup_recordings(fetch_date)

    # Final summary
    print(f"\n✓ Completed: {successful} | ✗ Failed: {failed} | Total: {len(alerts)}")

    if failed > 0:
        logger.warning(f"Exiting with error code due to {failed} failed alerts")
        return False
    return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Process alerts and extract video clips")
    parser.add_argument(
        "--date-cursor",
        type=int,
        default=None,
        help="Days offset from today (negative values for past dates). -1: yesterday, -2: 2 days ago, etc. If not provided, uses current date."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging (DEBUG level)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.conf",
        help="Path to config file (default: config/config.conf)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test API and S3 connectivity and exit"
    )
    parser.add_argument(
        "--fallback",
        action="store_true",
        help="Fallback mode: use yesterday's date (-1)"
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Run forever, listening on topic 'storeyes/<device-id>/alert-processing' and processing each 'start' message as it arrives"
    )
    args = parser.parse_args()
    
    # If --fallback is specified and date-cursor is not provided, set it to -1
    if args.fallback and args.date_cursor is None:
        args.date_cursor = -1
    
    # Setup logging
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    log_dir = os.environ.get("LOG_DIR", "logs")
    json_logging = os.environ.get("JSON_LOGGING", "false").lower() == "true"
    
    setup_logging(
        log_level=log_level,
        log_dir=log_dir,
        log_file="alert_processor.log",
        json_logging=json_logging,
        verbose=args.verbose
    )
    
    # Setup resume logger
    resume_logger = setup_resume_logger(log_dir)
    
    # Get logger with correlation ID
    correlation_id = str(uuid.uuid4())
    logger = get_logger(__name__, {"correlation_id": correlation_id})
    
    # Get device ID early (needed for fetching global settings and creating APIClient)
    device_id = get_device_id()
    logger.info(f"Device ID: {device_id}", extra={"device_id": device_id})
    
    # Load config file first to get BASE_URL for APIClient
    try:
        config_obj = load_config(args.config)
        logger.info("Configuration loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}", exc_info=True)
        sys.exit(1)
    
    # Get API base URL from config (needed to create APIClient)
    api_base_url = config_obj.get("API", "BASE_URL", fallback=None)
    if not api_base_url:
        logger.error("BASE_URL not found in config.conf! Please add BASE_URL to the [API] section")
        sys.exit(1)
    api_base_url = api_base_url.strip()
    alerts_endpoint = config_obj.get("API", "ALERTS_ENDPOINT").strip()
    secondary_video_endpoint = config_obj.get("API", "SECONDARY_VIDEO_ENDPOINT").strip()
    side_videos_endpoint = config_obj.get("API", "SIDE_VIDEOS_ENDPOINT", fallback="/side-videos").strip()

    # Create APIClient early (needed for fetching global settings in parse_config)
    api_client = APIClient(
        base_url=api_base_url,
        alerts_endpoint=alerts_endpoint,
        secondary_video_endpoint=secondary_video_endpoint,
        side_videos_endpoint=side_videos_endpoint,
        device_id=device_id
    )
    
    # Parse configuration (this will fetch global settings using api_client)
    try:
        config = parse_config(config_obj, api_client)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}", exc_info=True)
        sys.exit(1)
    
    # Set up AWS credentials (may have been set from global settings)
    with PerformanceLogger(logger, "setup_aws_credentials"):
        setup_aws_credentials(config_obj)
    
    # Check AWS credentials
    if not check_aws_credentials():
        logger.error("AWS credentials are required for uploading processed clips to S3")
        sys.exit(1)
    
    # Run connectivity tests if --test flag is set (independent of --wait)
    if args.test:
        fetch_date = get_fetch_date(args.date_cursor)
        test_date = fetch_date if args.date_cursor is not None else None
        if test_date:
            logger.info(f"Testing alerts API with date: {test_date}")
        s3_upload_prefix = config["s3_upload_prefix_template"].replace("{device-id}", device_id).replace("{date}", fetch_date)
        s3_uploader = S3Uploader(config["aws_region"], config["s3_bucket"], s3_upload_prefix)
        success = run_connectivity_tests(api_client, s3_uploader, test_date=test_date)
        sys.exit(0 if success else 1)

    if args.fallback:
        logger.info("Fallback mode enabled: using yesterday's date")

    if args.wait:
        run_wait_forever(device_id, args.date_cursor, config, api_client, resume_logger, logger)
    else:
        fetch_date = get_fetch_date(args.date_cursor)
        success = process_alerts_for_date(
            fetch_date, args.date_cursor, config, device_id, api_client,
            correlation_id, resume_logger, logger
        )
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
