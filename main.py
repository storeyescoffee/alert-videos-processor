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
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import paho.mqtt.client as mqtt

from src.core.api_client import APIClient
from src.core.clip_extractor import ClipExtractor
from src.core.s3_uploader import S3Uploader
from src.core.email_sender import EmailSender
from src.utils.logger_config import setup_logging, get_logger, run_log_dir, PerformanceLogger

from src.utils.device_utils import get_device_id, is_raspberry_pi
from src.utils.status_manager import publish_status
from src.utils.aws_utils import setup_aws_credentials, check_aws_credentials
from src.utils.config_manager import load_config, parse_config
from src.utils.progress_utils import LoggingTqdm
from src.utils.cleanup_utils import cleanup_recordings, clear_recordings_dir
from src.utils.side_video_utils import sync_side_videos
from src.core.alert_processor import process_alert
from src.tests.test_connectivity import run_connectivity_tests


MQTT_HOST = os.environ.get("MQTT_HOST", "18.100.207.236")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "storeyes")
MQTT_PASS = os.environ.get("MQTT_PASS", "12345")
REQUEST_TOPIC_TEMPLATE = "storeyes/{device_id}/alert-processing"
RESPONSE_TOPIC = "storeyes/alert-processing/response"
# Final responses a server sends once a job ends; anything else is a progress update.
FINAL_RESPONSES = ("finished", "failed")


def setup_resume_logger(log_dir: str) -> logging.Logger:
    """Setup resume log file for progress bar updates"""
    resume_log_file = run_log_dir(log_dir) / "alert_processor_resume.log"
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


def extract_device_id_from_topic(topic: str) -> Optional[str]:
    """Pull the <device-id> segment out of a "storeyes/<device-id>/alert-processing" topic."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "storeyes" and parts[2] == "alert-processing":
        return parts[1]
    return None


def run_server_forever(
    device_id: Optional[str],
    date_cursor: Optional[int],
    config_obj,
    config: Dict,
    api_client: APIClient,
    resume_logger: logging.Logger,
    logger,
) -> None:
    """
    Listen forever on "storeyes/+/alert-processing" and process each "start" message
    as it arrives, never returning. The device ID used for processing each message
    (API headers, S3 prefix, status board ID) is taken from that message's own topic,
    not from this device's .device.id file. On a Raspberry Pi, incoming messages are
    still filtered to this device's own "storeyes/<device-id>/alert-processing" topic
    (using the hardware serial, not the file); off a Pi (dev/test), a "start" on any
    device's topic is processed.

    Global settings (clip timing, S3/AWS, email, MQTT broker) are per-device on the
    API side, so before processing each message the worker re-fetches them for that
    message's device ID rather than reusing the settings resolved at startup.

    Runs two threads:
      - The listener: paho-mqtt's own background network thread (started by loop_start()),
        which stays connected and calls on_message for every incoming publish.
      - The processor: a single worker thread that pulls jobs off a queue and runs
        sync_side_videos + process_alerts_for_date for each one, one at a time.

    Every "start" is accepted and queued. If the worker is idle it is answered with
    "processing"; otherwise it is answered with how many jobs are ahead of it in the
    queue (e.g. "queued: there are 2 processes ahead"), on
    "storeyes/alert-processing/response". When the job ends, a final "finished" or
    "failed" response carries the run summary (download and per-alert processing
    metrics). Every response echoes the start message's "request_id" and the device ID
    so a waiting client can pick out its own.

    A start message may carry "date" (YYYY-MM-DD) and "date_cursor" (int); the latter
    only decides PROCESSING vs MF_PROCESSING status and falls back to --date-cursor.
    """
    subscribe_topic = REQUEST_TOPIC_TEMPLATE.format(device_id="+")
    response_topic = RESPONSE_TOPIC
    # On a Pi, only react to this device's own topic (identified by its hardware serial).
    # Off a Pi (dev/test), react to a "start" on any device's topic since there's no real
    # device to be strict about.
    restrict_to_own_topic = is_raspberry_pi()

    # (device_id, date, date_cursor, request_id)
    work_queue: "queue.Queue[tuple[str, Optional[str], Optional[int], Optional[str]]]" = queue.Queue()
    # Jobs queued or currently being processed, i.e. not yet finished. Guarded by
    # state_lock since on_message (paho's network thread) and the worker thread both
    # touch it. Used to tell a newly queued job how many runs are ahead of it.
    state_lock = threading.Lock()
    jobs_outstanding = 0

    def publish_response(client, message: str, **fields) -> None:
        try:
            client.publish(response_topic, json.dumps({"response": message, **fields}), qos=1)
        except Exception as e:
            logger.error(f"Failed to publish response on {response_topic}: {e}", exc_info=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
            client.subscribe(subscribe_topic, qos=1)
            logger.info(f"Subscribed to topic: {subscribe_topic}")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code {reason_code}")

    def on_message(client, userdata, msg):
        nonlocal jobs_outstanding
        topic_device_id = extract_device_id_from_topic(msg.topic)
        if topic_device_id is None:
            logger.warning(f"Ignoring message on unexpected topic: {msg.topic}")
            return

        if restrict_to_own_topic and topic_device_id != device_id:
            return

        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON message: {e}")
            return

        action = payload.get("action")
        date = payload.get("date")
        date_provided = date is not None and date != ""
        request_id = payload.get("request_id")
        msg_date_cursor = payload.get("date_cursor")
        if msg_date_cursor is not None and not isinstance(msg_date_cursor, int):
            logger.warning(f"Ignoring non-integer date_cursor in message: {msg_date_cursor!r}")
            msg_date_cursor = None

        if action not in ("start", "abort"):
            logger.warning(f"Invalid action '{action}' in message. Expected 'start' or 'abort'")
            return

        logger.info(f"Received message on topic {msg.topic}: action={action}, date={date}")

        if action == "abort":
            logger.info("Received 'abort' action from broker; continuing to listen")
            return

        # action == "start". Accept every start and queue it. `ahead` is how many jobs
        # (running + already queued) will be handled before this one.
        with state_lock:
            ahead = jobs_outstanding
            jobs_outstanding += 1
        work_queue.put((topic_device_id, date if date_provided else None, msg_date_cursor, request_id))

        ids = {"request_id": request_id, "device_id": topic_device_id}
        if ahead == 0:
            publish_response(client, "processing", **ids)
        elif ahead == 1:
            logger.info("Queued 'start'; 1 process ahead")
            publish_response(client, "queued: there is 1 process ahead", **ids)
        else:
            logger.info(f"Queued 'start'; {ahead} processes ahead")
            publish_response(client, f"queued: there are {ahead} processes ahead", **ids)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
        if reason_code != 0:
            logger.warning(f"Unexpected MQTT disconnection (reason_code={reason_code}, flags={disconnect_flags})")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    logger.info(f"Connecting to MQTT broker at {MQTT_HOST}:{MQTT_PORT}...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    def worker():
        nonlocal jobs_outstanding
        while True:
            topic_device_id, broker_date, msg_date_cursor, request_id = work_queue.get()
            job_cursor = msg_date_cursor if msg_date_cursor is not None else date_cursor
            summary = {"date": None, "success": False, "error": None, "download": None, "processing": None}
            try:
                fetch_date = broker_date or get_fetch_date(job_cursor)
                summary["date"] = fetch_date
                if broker_date:
                    logger.info(f"Using date from broker message: {fetch_date}")
                else:
                    logger.info(f"No date in broker message, using default: {fetch_date}")

                # A single worker thread processes one job at a time, so mutating the
                # shared api_client here is safe.
                api_client.device_id = topic_device_id

                # Global settings are per-device, so reload them for this message's
                # device ID before processing rather than reusing the startup config.
                cycle_config = parse_config(config_obj, api_client)
                setup_aws_credentials(config_obj)
                if not check_aws_credentials():
                    logger.error(
                        f"AWS credentials unavailable after reloading settings for device {topic_device_id}; "
                        "skipping this message"
                    )
                    summary["error"] = "AWS credentials unavailable"
                    continue

                summary["download"] = sync_side_videos(
                    api_client, fetch_date, cycle_config["local_source_dir"], logger
                )

                cycle_correlation_id = str(uuid.uuid4())
                cycle_logger = get_logger(
                    __name__, {"correlation_id": cycle_correlation_id, "device_id": topic_device_id}
                )
                processing = process_alerts_for_date(
                    fetch_date, job_cursor, cycle_config, topic_device_id, api_client,
                    cycle_correlation_id, resume_logger, cycle_logger
                )
                summary["success"] = processing.pop("success")
                summary["error"] = processing.pop("error")
                summary["processing"] = processing
            except Exception as e:
                logger.error(f"Unexpected error while processing broker message: {e}", exc_info=True)
                summary["error"] = f"Unexpected error: {e}"
            finally:
                # Clear the recordings folder after each processing session so continuous
                # chunks and sidecars don't carry over to the next message.
                clear_recordings_dir(config["local_source_dir"])
                with state_lock:
                    jobs_outstanding -= 1
                publish_response(
                    client, "finished" if summary["success"] else "failed",
                    request_id=request_id, device_id=topic_device_id, **summary
                )
                work_queue.task_done()

    processing_thread = threading.Thread(target=worker, name="alert-processor", daemon=True)
    processing_thread.start()

    logger.info(
        f"Listening forever on {subscribe_topic} "
        f"({'restricted to device ' + str(device_id) if restrict_to_own_topic else 'any device topic'}); "
        f"responses published on {response_topic}"
    )
    processing_thread.join()


def _fmt_seconds(seconds: Optional[float]) -> str:
    return "-" if seconds is None else f"{seconds:.1f}s"


def print_run_summary(summary: Dict) -> None:
    """Print the final "finished"/"failed" response a server sent for a client run."""
    print(f"\n=== Alert processing summary: device {summary.get('device_id')}, date {summary.get('date')} ===")

    download = summary.get("download")
    if download:
        megabytes = download["bytes"] / (1024 * 1024)
        rate = f", {megabytes / download['seconds']:.2f} MB/s" if download["seconds"] > 0 and download["bytes"] else ""
        print(
            f"Download:   {_fmt_seconds(download['seconds'])} | {download['videos']} side video(s): "
            f"{download['downloaded']} downloaded, {download['already_present']} already present, "
            f"{download['failed']} failed | {megabytes:.1f} MB{rate}"
        )
        if download.get("error"):
            print(f"            {download['error']}")
    else:
        print("Download:   not run")

    processing = summary.get("processing")
    if processing:
        print(
            f"Processing: {processing['alerts']} alert(s): "
            f"✓ {processing['successful']} | ✗ {processing['failed']}"
        )
        print(
            f"            total {_fmt_seconds(processing['total_seconds'])} | "
            f"min {_fmt_seconds(processing['min_seconds'])} | "
            f"max {_fmt_seconds(processing['max_seconds'])} | "
            f"avg {_fmt_seconds(processing['avg_seconds'])} per alert"
        )
    else:
        print("Processing: not run")

    result = "SUCCESS" if summary.get("success") else "FAILED"
    if summary.get("error"):
        result += f" ({summary['error']})"
    print(f"Result:     {result}")


def run_client(device_id: str, fetch_date: str, date_cursor: Optional[int], timeout: float, logger) -> bool:
    """
    Ask a server (main.py --server) to process `fetch_date` for this device and wait for
    the result.

    Publishes a "start" on "storeyes/<device-id>/alert-processing" tagged with a fresh
    request_id, then follows that request's responses on
    "storeyes/alert-processing/response": progress updates ("processing", "queued: ...")
    are printed as they arrive, and the final "finished"/"failed" response is printed as
    a summary. Returns True only if the server reported success; False on failure or if
    no final response arrived within `timeout` seconds (0 waits forever).
    """
    request_id = str(uuid.uuid4())
    request_topic = REQUEST_TOPIC_TEMPLATE.format(device_id=device_id)
    subscribed = threading.Event()
    done = threading.Event()
    final: Dict = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
            # Subscribe (again, after a reconnect) before anything is published so the
            # server's responses can't slip past us.
            client.subscribe(RESPONSE_TOPIC, qos=1)
        else:
            logger.error(f"Failed to connect to MQTT broker, return code {reason_code}")

    def on_subscribe(client, userdata, mid, reason_code_list, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            return

        response = payload.get("response")
        if response in FINAL_RESPONSES:
            final.update(payload)
            done.set()
        else:
            logger.info(f"Server response: {response}")
            print(f"Server: {response}")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    logger.info(f"Connecting to MQTT broker at {MQTT_HOST}:{MQTT_PORT}...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    try:
        if not subscribed.wait(timeout=30):
            logger.error(f"Timed out subscribing to {RESPONSE_TOPIC}")
            return False

        request = {"action": "start", "date": fetch_date, "request_id": request_id}
        if date_cursor is not None:
            request["date_cursor"] = date_cursor
        result = client.publish(request_topic, json.dumps(request), qos=1)
        result.wait_for_publish(timeout=30)
        if not result.is_published():
            logger.error(f"Failed to publish processing request on {request_topic}")
            return False
        logger.info(f"Requested processing of {fetch_date} on {request_topic} (request_id={request_id})")
        print(f"Requested processing of {fetch_date} for device {device_id}; waiting for the server...")

        if not done.wait(timeout=timeout or None):
            logger.error(f"No result from the server within {timeout:g}s (request_id={request_id})")
            print(f"\n✗ No result from the server within {timeout:g}s")
            return False
    finally:
        client.loop_stop()
        client.disconnect()

    print_run_summary(final)
    return bool(final.get("success"))


def process_alerts_for_date(
    fetch_date: str,
    date_cursor: Optional[int],
    config: Dict,
    device_id: str,
    api_client: APIClient,
    correlation_id: str,
    resume_logger: logging.Logger,
    logger,
) -> Dict:
    """
    Fetch, extract, upload, and email alerts for a single date.

    Returns a result dict: "success" is True if there were no alerts or all of them
    succeeded, False if the alert fetch failed, the local recordings couldn't be loaded,
    or any alert failed; "error" describes an early failure; "alerts", "successful",
    "failed" count alerts; "total_seconds", "min_seconds", "max_seconds", "avg_seconds"
    time each alert's process_alert call (None when no alert was processed).
    """
    result = {
        "success": False, "error": None, "alerts": 0, "successful": 0, "failed": 0,
        "total_seconds": 0.0, "min_seconds": None, "max_seconds": None, "avg_seconds": None,
    }
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
        result["error"] = f"Cannot load recordings: {e}"
        return result

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
        result["error"] = f"Failed to fetch alerts: {e}"
        return result

    if not alerts:
        logger.info(f"No alerts found for date {fetch_date}")
        publish_status("EMPTY", board_id=device_id)
        result["success"] = True
        return result

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
    alert_durations = []

    with LoggingTqdm(total=len(alerts), desc="Processing alerts", unit="alert",
                     bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]',
                     resume_logger=resume_logger) as pbar:
        for alert in alerts:
            alert_id = alert.get("id")
            alert_logger = get_logger(__name__, {"correlation_id": correlation_id, "alert_id": alert_id})

            pbar.set_description(f"Processing alert {alert_id}")

            alert_started = time.monotonic()
            with PerformanceLogger(alert_logger, f"process_alert_{alert_id}", alert_id=alert_id):
                success, video_url, thumbnail_url = process_alert(
                    alert, clip_extractor, s3_uploader, api_client,
                    max_retries=config["max_retries"], retry_delay_seconds=config["retry_delay_seconds"]
                )
            alert_durations.append(time.monotonic() - alert_started)

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

    total_seconds = sum(alert_durations)
    result.update(
        alerts=total_alerts, successful=successful, failed=failed,
        total_seconds=round(total_seconds, 3),
        min_seconds=round(min(alert_durations), 3),
        max_seconds=round(max(alert_durations), 3),
        avg_seconds=round(total_seconds / len(alert_durations), 3),
        success=failed == 0,
    )

    if failed > 0:
        logger.warning(f"{failed} alert(s) failed")
    return result


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
        "--server",
        action="store_true",
        help="Server mode: run forever, listening on topic 'storeyes/<device-id>/alert-processing' and processing each 'start' message as it arrives"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Process alerts in this process instead of asking a server (the old default behavior)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=7200,
        help="Client mode: seconds to wait for the server's result before giving up; 0 waits forever (default: 7200)"
    )
    args = parser.parse_args()
    
    # If --fallback is specified and date-cursor is not provided, set it to -1
    if args.fallback and args.date_cursor is None:
        args.date_cursor = -1
    
    # Setup logging
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    log_dir = os.environ.get("LOG_DIR", "logs")
    json_logging = os.environ.get("JSON_LOGGING", "false").lower() == "true"
    keep_runs = int(os.environ.get("LOG_KEEP_RUNS", "30"))
    
    # Each run writes into its own logs/<timestamp>/ directory
    setup_logging(
        log_level=log_level,
        log_dir=log_dir,
        log_file="alert_processor.log",
        json_logging=json_logging,
        verbose=args.verbose,
        keep_runs=keep_runs
    )
    
    # Setup resume logger
    resume_logger = setup_resume_logger(log_dir)
    
    # Get logger with correlation ID
    correlation_id = str(uuid.uuid4())
    logger = get_logger(__name__, {"correlation_id": correlation_id})
    
    # Get device ID early (needed for fetching global settings and creating APIClient).
    # In --server mode, the real device ID for each run is taken from the incoming MQTT
    # topic instead, so the .device.id file is not required here — a missing file just
    # means no device ID is known yet until the first message arrives.
    device_id = get_device_id(required=not args.server)
    if device_id:
        logger.info(f"Device ID: {device_id}", extra={"device_id": device_id})
    else:
        logger.info("No device ID resolved at startup; will use the device ID from each incoming alert-processing topic")

    # Default (client) mode: the server does all the work, so no config, API or AWS setup
    # is needed here — just send the request and print the summary it sends back.
    if not (args.server or args.local or args.test):
        if args.fallback:
            logger.info("Fallback mode enabled: using yesterday's date")
        fetch_date = get_fetch_date(args.date_cursor)
        success = run_client(device_id, fetch_date, args.date_cursor, args.timeout, logger)
        sys.exit(0 if success else 1)

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

    # Create APIClient early (needed for fetching global settings in parse_config).
    # device_id may still be unknown here in --server mode (no .device.id file yet); the
    # worker sets api_client.device_id from each message's own topic before using it.
    api_client = APIClient(
        base_url=api_base_url,
        alerts_endpoint=alerts_endpoint,
        secondary_video_endpoint=secondary_video_endpoint,
        side_videos_endpoint=side_videos_endpoint,
        device_id=device_id or ""
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
    
    # Run connectivity tests if --test flag is set (independent of --server)
    if args.test:
        fetch_date = get_fetch_date(args.date_cursor)
        test_date = fetch_date if args.date_cursor is not None else None
        if test_date:
            logger.info(f"Testing alerts API with date: {test_date}")
        s3_upload_prefix = config["s3_upload_prefix_template"].replace("{device-id}", device_id or "").replace("{date}", fetch_date)
        s3_uploader = S3Uploader(config["aws_region"], config["s3_bucket"], s3_upload_prefix)
        success = run_connectivity_tests(api_client, s3_uploader, test_date=test_date)
        sys.exit(0 if success else 1)

    if args.fallback:
        logger.info("Fallback mode enabled: using yesterday's date")

    if args.server:
        run_server_forever(device_id, args.date_cursor, config_obj, config, api_client, resume_logger, logger)
    else:
        fetch_date = get_fetch_date(args.date_cursor)
        result = process_alerts_for_date(
            fetch_date, args.date_cursor, config, device_id, api_client,
            correlation_id, resume_logger, logger
        )
        if not result["success"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
