"""
Syncs side videos reported by the device-gw API into the local recordings directory.

Downloaded files are named to match ClipExtractor's YYYYMMDD_<random>.mp4
pattern so they're picked up as ordinary continuous chunks, with a "<file>.json" sidecar
recording the API-reported birthTime as the clip time origin (instead of the file's
download-time filesystem birthdate, which would be wrong).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import requests

from recovery_mdat import recover_mdat
from src.utils.logger_config import get_logger, PerformanceLogger

# Same shape as ClipExtractor.CONTINUOUS_FILENAME_PATTERN: YYYYMMDD_<random>.mp4
SIDE_VIDEO_FILENAME_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})_[a-z0-9]+\.mp4$")


def _target_filename(video: Dict) -> str:
    date_nodash = video["date"].replace("-", "")
    return f"{date_nodash}_{video['id']}.mp4"


def _drop_other_date_videos(recordings_dir: Path, date: str, logger) -> None:
    """Remove existing continuous-pattern recordings whose filename date differs from `date`."""
    date_nodash = date.replace("-", "")
    for path in recordings_dir.glob("*.mp4"):
        match = SIDE_VIDEO_FILENAME_PATTERN.match(path.name)
        if not match or "".join(match.groups()) == date_nodash:
            continue

        try:
            path.unlink()
            logger.info(f"Dropped recording from a different date: {path.name}")
        except OSError as e:
            logger.warning(f"Failed to drop stale recording {path}: {e}")
            continue

        sidecar_path = Path(str(path) + ".json")
        if sidecar_path.exists():
            try:
                sidecar_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to drop stale sidecar {sidecar_path}: {e}")


def _download_video(video: Dict, dest_path: Path, logger) -> bool:
    url = video.get("videoUrl")
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        tmp_path.replace(dest_path)
        logger.info(f"Downloaded side video {video.get('name')} -> {dest_path.name}")
        return True
    except (requests.RequestException, OSError) as e:
        logger.error(f"Failed to download side video {video.get('name')}: {e}", exc_info=True)
        tmp_path.unlink(missing_ok=True)
        return False


def _can_probe(path: Path, logger) -> bool:
    """Whether ffprobe can read `path` (a missing moov atom is the common failure)."""
    if not shutil.which("ffprobe"):
        logger.warning("ffprobe not found on PATH; skipping probe check")
        return True
    try:
        subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _recover_unprobeable(paths: List[Path], logger) -> None:
    """Recover, in place, any of `paths` that ffprobe can't read."""
    broken = [p for p in paths if not _can_probe(p, logger)]
    if not broken:
        return

    logger.info(f"{len(broken)} side video(s) failed ffprobe; attempting mdat recovery")
    for path in broken:
        try:
            recover_mdat(str(path))
            logger.info(f"Recovered {path.name}")
        except Exception as e:
            logger.error(f"Failed to recover {path.name}: {e}", exc_info=True)


def sync_side_videos(api_client, date: str, recordings_dir: str, logger=None) -> None:
    """
    Fetch side videos for `date` from the device-gw API and download the missing ones
    into `recordings_dir`, writing a birthTime sidecar for each. Existing continuous-pattern
    recordings for a different date are dropped first.
    """
    logger = logger or get_logger(__name__)
    recordings_path = Path(recordings_dir)
    recordings_path.mkdir(parents=True, exist_ok=True)

    _drop_other_date_videos(recordings_path, date, logger)

    try:
        with PerformanceLogger(logger, "get_side_videos", date=date):
            side_videos = api_client.get_side_videos(date)
    except Exception as e:
        logger.error(f"Failed to fetch side videos for {date}: {e}", exc_info=True)
        return

    if not side_videos:
        logger.info(f"No side videos found for date {date}")
        return

    video_paths = []
    for video in side_videos:
        filename = _target_filename(video)
        dest_path = recordings_path / filename

        if dest_path.exists():
            logger.debug(f"Side video already present, skipping download: {filename}")
            video_paths.append(dest_path)
            continue

        if not _download_video(video, dest_path, logger):
            continue

        video_paths.append(dest_path)

        sidecar_path = Path(str(dest_path) + ".json")
        try:
            with open(sidecar_path, "w") as f:
                json.dump({"start": f"{video['date']}T{video['birthTime']}"}, f)
        except OSError as e:
            logger.warning(f"Failed to write sidecar for {filename}: {e}")

    # Scan everything for this date (freshly downloaded or already present) and recover
    # any file ffprobe can't read before clip extraction gets a chance to skip it.
    _recover_unprobeable(video_paths, logger)
