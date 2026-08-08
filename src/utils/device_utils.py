"""
Device utilities for retrieving device information
"""
import os
import subprocess
from src.utils.logger_config import get_logger

DEVICE_ID_FILE = ".device.id"


def get_device_id() -> str:
    """
    Get device ID.

    On Raspberry Pi devices, reads the serial number from /proc/cpuinfo.
    On non-RPI devices (no /proc/cpuinfo), falls back to reading the
    device ID from the .device.id file in the project root.

    Returns:
        Device ID (serial number) as string

    Raises:
        RuntimeError: If device ID cannot be retrieved
    """
    logger = get_logger(__name__)

    if os.path.exists("/proc/cpuinfo"):
        try:
            result = subprocess.run(
                ["awk", "/Serial/ {print $3}", "/proc/cpuinfo"],
                capture_output=True,
                text=True,
                check=True
            )
            device_id = result.stdout.strip()
            if device_id:
                return device_id
            logger.warning("Device ID not found in /proc/cpuinfo, falling back to .device.id")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"Failed to get device ID from /proc/cpuinfo, falling back to .device.id: {e}")

    try:
        with open(DEVICE_ID_FILE, "r") as f:
            device_id = f.read().strip()
        if not device_id:
            raise RuntimeError(f"Device ID not found in {DEVICE_ID_FILE}")
        return device_id
    except (FileNotFoundError, RuntimeError) as e:
        logger.error(f"Failed to get device ID: {e}", exc_info=True)
        raise RuntimeError(f"Failed to get device ID from {DEVICE_ID_FILE}: {e}")

