"""
Cleanup utilities for removing temporary files
"""
import os
import shutil
from datetime import datetime
from pathlib import Path
from src.utils.logger_config import get_logger


def clear_recordings_dir(recordings_dir=None):
    """Delete every file in the recordings directory, leaving the directory itself.

    Called at the end of a processing session (server mode) so continuous chunks
    and their sidecars don't accumulate between messages.
    """
    try:
        recordings_path = Path(
            recordings_dir if recordings_dir else Path(os.path.expanduser("~")) / "recordings"
        )

        if not recordings_path.exists():
            return

        deleted_count = 0
        logger = get_logger(__name__)
        for entry in recordings_path.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                deleted_count += 1
                logger.debug(f"Removed from recordings dir: {entry}")
            except Exception as e:
                logger.warning(f"Failed to remove {entry}: {e}", exc_info=True)

        if deleted_count > 0:
            logger.info(f"Cleared {deleted_count} entr{'y' if deleted_count == 1 else 'ies'} from recordings dir: {recordings_path}")
    except Exception as e:
        try:
            logger = get_logger(__name__)
            logger.error(f"Failed to clear recordings dir: {e}", exc_info=True)
        except Exception:
            print(f"Error: Failed to clear recordings dir: {e}")


def cleanup_recordings(fetch_date):
    """Delete recordings matching pattern DDMMYYYY_*.mp4 (e.g., 08022026_221814.mp4)"""
    try:
        # Extract date from fetch_date (format: 2026-02-08 or 2026-02-08T00:00:00Z)
        date_part = fetch_date.split('T')[0] if 'T' in fetch_date else fetch_date  # Get YYYY-MM-DD part
        
        # Parse date and convert to DDMMYYYY format
        try:
            date_obj = datetime.strptime(date_part, '%Y-%m-%d')
            date_pattern = date_obj.strftime('%d%m%Y')  # Convert to DDMMYYYY (e.g., 08022026)
        except Exception as e:
            try:
                logger = get_logger(__name__)
                logger.error(f"Failed to parse date {date_part}: {e}", exc_info=True)
            except:
                pass
            return
        
        # Get recordings directory path
        recordings_dir = Path(os.path.expanduser("~")) / "recordings"
        
        if not recordings_dir.exists():
            try:
                logger = get_logger(__name__)
                logger.debug(f"Recordings directory does not exist: {recordings_dir}")
            except:
                pass
            return
        
        # Find all files matching pattern DDMMYYYY_*.mp4
        pattern = f"{date_pattern}_*.mp4"
        matching_files = list(recordings_dir.glob(pattern))
        
        if not matching_files:
            try:
                logger = get_logger(__name__)
                logger.debug(f"No recordings found matching pattern: {pattern}")
            except:
                pass
            return
        
        # Delete matching files
        deleted_count = 0
        logger = get_logger(__name__)
        for file_path in matching_files:
            try:
                file_path.unlink()
                deleted_count += 1
                logger.debug(f"Deleted recording file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete recording file {file_path}: {e}", exc_info=True)
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} recording file(s) matching pattern: {pattern}")
    except Exception as e:
        try:
            logger = get_logger(__name__)
            logger.error(f"Failed to cleanup recordings: {e}", exc_info=True)
        except:
            print(f"Error: Failed to cleanup recordings: {e}")

