"""
Status publishing over MQTT for tracking processing state
"""
import os
import json
import time
from datetime import datetime, timezone
from typing import Optional
import paho.mqtt.client as mqtt
from src.utils.logger_config import get_logger


def _publish_mqtt_status(board_id: str, status: str, total_count: Optional[int] = None, 
                         processed_count: Optional[int] = None) -> bool:
    """
    Publish status to MQTT broker
    
    Args:
        board_id: Device/board ID
        status: Status string (EMPTY, PROCESSING, MF_PROCESSING, FINISHED)
        total_count: Total number of alerts (optional)
        processed_count: Number of processed alerts (optional)
        
    Returns:
        True if published successfully, False otherwise
    """
    logger = get_logger(__name__)
    
    # MQTT configuration (same as alert-monitor.sh)
    mqtt_host = os.environ.get("MQTT_HOST", "18.100.207.236")
    mqtt_port = os.environ.get("MQTT_PORT", "1883")
    mqtt_user = os.environ.get("MQTT_USER", "storeyes")
    mqtt_pass = os.environ.get("MQTT_PASS", "12345")
    mqtt_topic = os.environ.get("MQTT_TOPIC", f"storeyes/{board_id}/alert-processor")
    qos = os.environ.get("QOS", "1")
    retain = os.environ.get("RETAIN", "false").lower() == "true"
    timeout = int(os.environ.get("TIMEOUT", "5"))
    retries = int(os.environ.get("RETRIES", "3"))
    
    # Build JSON payload (same format as alert-monitor.sh)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    payload = {
        "board_id": board_id,
        "timestamp": timestamp,
        "alert-processor": {
            "status": status,
            "total": str(total_count) if total_count is not None else "-1",
            "processed": str(processed_count) if processed_count is not None else "-1"
        }
    }
    
    # Convert to compact JSON
    try:
        json_payload = json.dumps(payload, separators=(',', ':'))
    except Exception as e:
        logger.error(f"Failed to serialize MQTT payload: {e}", exc_info=True)
        return False
    
    # Convert QOS to int
    qos_int = int(qos)

    # Callback API v2 callbacks for detailed logging and compatibility with paho-mqtt >= 2.0
    def on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info(f"MQTT connect result: reason_code={reason_code}, flags={flags}")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
        logger.warning(
            f"MQTT disconnect: reason_code={reason_code}, flags={disconnect_flags}"
        )

    def on_publish(client, userdata, mid, reason_code, properties=None):
        logger.debug(
            f"MQTT on_publish: mid={mid}, reason_code={reason_code}, properties={properties}"
        )

    def on_log(client, userdata, level, buf):
        # Mirror the debug logs you see in the Linux runtime
        logger.debug(f"MQTT log [{level}]: {buf}")

    # Retry loop (same as alert-monitor.sh)
    client = None
    for attempt in range(1, retries + 1):
        try:
            logger.info(
                f"MQTT publish config: host={mqtt_host}, port={mqtt_port}, "
                f"user={mqtt_user}, topic={mqtt_topic}, qos={qos_int}, "
                f"timeout={timeout}, retries={retries}"
            )
            logger.debug(
                f"Connecting to MQTT broker {mqtt_host}:{mqtt_port} (attempt {attempt}/{retries})"
            )

            # Create MQTT client (use latest callback API version to avoid deprecation warning)
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set(mqtt_user, mqtt_pass)
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_publish = on_publish
            client.on_log = on_log

            # Connect to broker
            client.connect(mqtt_host, int(mqtt_port), keepalive=60)

            # Start network loop to handle connection
            client.loop_start()

            # Wait a bit for connection to establish
            time.sleep(0.1)

            # Publish message
            result = client.publish(
                mqtt_topic,
                json_payload,
                qos=qos_int,
                retain=retain
            )
            logger.debug(
                f"MQTT publish invoked, mid={result.mid}, rc={result.rc}"
            )

            # Wait for message to be published, then check publish state explicitly.
            result.wait_for_publish(timeout=timeout)

            if result.is_published():
                # Stop network loop
                client.loop_stop()
                client.disconnect()
                logger.debug(
                    f"MQTT status published successfully (attempt {attempt}/{retries})"
                )
                return True
            else:
                logger.warning(
                    f"MQTT publish timeout (attempt {attempt}/{retries}), "
                    f"mid={result.mid}, rc={result.rc}"
                )
                client.loop_stop()
                client.disconnect()

        except Exception as e:
            # Log full stack trace so that with --verbose you can see exactly why publish failed,
            # then re-raise so the caller can see the real exception instead of only a warning.
            logger.warning(
                f"MQTT publish error (attempt {attempt}/{retries}): {e}",
                exc_info=True,
            )
            if client:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
            # Do not swallow the exception – propagate it so the caller sees the real cause.
            raise

        if attempt < retries:
            time.sleep(2)
    
    logger.error(f"Failed to publish MQTT status after {retries} attempts")
    return False


def publish_status(status, total_count=None, processed_count=None, board_id: Optional[str] = None):
    """
    Publish processing status to MQTT

    Args:
        status: Status string (EMPTY, PROCESSING, MF_PROCESSING, FINISHED)
        total_count: Total number of alerts (optional)
        processed_count: Number of processed alerts (optional)
        board_id: Device/board ID for MQTT (optional, will try to get from device_utils if not provided)
    """
    logger = get_logger(__name__)

    if board_id is None:
        # Try to get board_id from device_utils
        try:
            from src.utils.device_utils import get_device_id
            board_id = get_device_id()
        except Exception as e:
            # In this very specific case we *have* to catch the error, because without a
            # board_id we cannot even attempt MQTT. Log it at debug level and return.
            logger.debug(f"Could not get device ID for MQTT: {e}", exc_info=True)
            return  # Skip MQTT if we can't get board_id
    
    # Let any MQTT-related exceptions from _publish_mqtt_status propagate to the caller
    # so they are visible in logs and not hidden behind generic warnings.
    _publish_mqtt_status(board_id, status, total_count, processed_count)

