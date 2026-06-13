"""
Continuous Clip Extractor
Extracts video clips from a single continuous video.mp4 file using file birthdate as t=0.
"""
import datetime
import subprocess
import os
import logging
import math
import shutil
from typing import Optional, Tuple

from src.utils.video_utils import (
    ensure_browser_playable_mp4,
    ffmpeg_global_thread_args,
    should_run_browser_reencode,
)

CONTINUOUS_FILENAME = "video.mp4"


class ContinuousClipExtractor:
    """Extracts clips from a single continuous video file using its birthdate as the time origin."""

    def __init__(self, before_minutes: int, after_minutes: int, output_dir: str,
                 local_source_dir: str):
        if not local_source_dir:
            raise ValueError("local_source_dir is required")

        self.before_minutes = before_minutes
        self.after_minutes = after_minutes
        self.output_dir = output_dir
        self.video_path = os.path.join(local_source_dir, CONTINUOUS_FILENAME)

        if not os.path.exists(self.video_path):
            raise FileNotFoundError(f"Continuous video not found: {self.video_path}")

        os.makedirs(self.output_dir, exist_ok=True)
        logging.info(f"Continuous mode: using {self.video_path}")

    def _get_birthdate(self) -> datetime.datetime:
        """
        Return the file creation time (birthdate) of the video.

        Tries os.stat().st_birthtime first (macOS, Python 3.12+ Linux with ext4/btrfs).
        Falls back to `stat -c %W` on Linux which returns the birth time as a Unix timestamp
        (returns 0 when the filesystem does not support it, which we treat as an error).
        """
        stat = os.stat(self.video_path)

        if hasattr(stat, "st_birthtime"):
            ts = stat.st_birthtime
            if ts > 0:
                return datetime.datetime.fromtimestamp(ts)

        # Fallback: call stat(1) for birth time
        try:
            result = subprocess.run(
                ["stat", "-c", "%W", self.video_path],
                capture_output=True, text=True, timeout=10, check=True
            )
            ts = int(result.stdout.strip())
            if ts > 0:
                return datetime.datetime.fromtimestamp(ts)
            raise RuntimeError(
                f"stat -c %W returned 0 for {self.video_path} — "
                "filesystem does not support birth time. "
                "Use a filesystem that records birth time (ext4, btrfs) or pass --video-start-time."
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
            raise RuntimeError(f"Could not determine birthdate of {self.video_path}: {e}") from e

    def _ffprobe_duration_seconds(self) -> Optional[float]:
        if not shutil.which("ffprobe"):
            return None
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 self.video_path],
                capture_output=True, text=True, timeout=45, check=True,
            )
            d = float(r.stdout.strip())
            if d > 0 and not math.isnan(d):
                return d
        except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired, OSError):
            pass
        return None

    def _generate_thumbnail(self, video_file: str, alert_time: datetime.datetime,
                             seek_seconds: float) -> Optional[str]:
        timestamp = alert_time.strftime('%Y%m%d_%H%M%S')
        thumbnail_file = os.path.join(self.output_dir, f"thumb_{timestamp}.jpg")
        seek = max(0.0, float(seek_seconds))

        try:
            subprocess.run(
                ["ffmpeg", "-y",
                 *ffmpeg_global_thread_args(),
                 "-ss", f"{seek:.3f}",
                 "-i", video_file,
                 "-an", "-sn",
                 "-map", "0:v:0",
                 "-frames:v", "1",
                 "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                 "-q:v", "2",
                 thumbnail_file],
                check=True, capture_output=True, text=True, timeout=120,
            )
            if os.path.exists(thumbnail_file) and os.path.getsize(thumbnail_file) > 0:
                logging.info(f"Thumbnail generated: {thumbnail_file}")
                return thumbnail_file
            logging.warning("Thumbnail file was not created or is empty")
            return None
        except subprocess.CalledProcessError as e:
            logging.error(f"FFmpeg thumbnail generation failed: {e.stderr}")
            return None
        except subprocess.TimeoutExpired:
            logging.error("FFmpeg timeout during thumbnail generation")
            return None

    def extract_clip(self, alert_time_iso: str) -> Tuple[Optional[str], Optional[str]]:
        logging.info(f"[continuous] Extracting clip for alert: {alert_time_iso}")

        try:
            alert_time = datetime.datetime.fromisoformat(alert_time_iso.replace('Z', ''))
            if alert_time.tzinfo is not None:
                alert_time = alert_time.replace(tzinfo=None)
        except ValueError as e:
            logging.error(f"Failed to parse alert time '{alert_time_iso}': {e}")
            return None, None

        try:
            birthdate = self._get_birthdate()
        except RuntimeError as e:
            logging.error(str(e))
            return None, None

        logging.info(f"Video birthdate: {birthdate}")

        before_seconds = self.before_minutes * 60
        after_seconds = self.after_minutes * 60
        total_duration = before_seconds + after_seconds

        # Offset from t=0 (birthdate) to the start of the extraction window
        seek = (alert_time - birthdate).total_seconds() - before_seconds
        seek = max(0.0, seek)  # clamp: don't seek before file start

        video_duration = self._ffprobe_duration_seconds()
        if video_duration is not None and seek >= video_duration:
            logging.error(
                f"Alert time {alert_time} is past the end of the video "
                f"(seek={seek:.1f}s, video duration={video_duration:.1f}s)"
            )
            return None, None

        # Clamp total_duration so we don't read past EOF
        if video_duration is not None:
            total_duration = min(total_duration, video_duration - seek)

        logging.info(
            f"Cutting: seek={seek:.1f}s, duration={total_duration:.1f}s "
            f"(before={self.before_minutes}min, after={self.after_minutes}min)"
        )

        timestamp = alert_time.strftime('%Y%m%d_%H%M%S')
        output_file = os.path.join(self.output_dir, f"alert_clip_{timestamp}.mp4")
        temp_file = os.path.join(self.output_dir, f"alert_clip_{timestamp}_temp.mp4")

        try:
            subprocess.run(
                ["ffmpeg", "-y",
                 *ffmpeg_global_thread_args(),
                 "-ss", f"{seek:.3f}",
                 "-i", self.video_path,
                 "-t", f"{total_duration:.3f}",
                 "-c", "copy",
                 temp_file],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"FFmpeg clip extraction failed: {e.stderr}")
            return None, None
        except subprocess.TimeoutExpired:
            logging.error("FFmpeg timeout during clip extraction")
            return None, None

        if not os.path.exists(temp_file) or os.path.getsize(temp_file) == 0:
            logging.error("Extracted clip is empty or missing")
            return None, None

        if not should_run_browser_reencode():
            os.replace(temp_file, output_file)
        else:
            try:
                ensure_browser_playable_mp4(temp_file, quiet=True)
                os.replace(temp_file, output_file)
            except Exception as e:
                logging.error(f"Browser re-encode failed: {e}")
                if os.path.exists(temp_file):
                    os.replace(temp_file, output_file)

        output_size = os.path.getsize(output_file)
        logging.info(f"Clip created: {output_size / 1024 / 1024:.2f} MB → {output_file}")

        # Thumbnail at the alert moment within the clip
        # alert_time lands at before_seconds into the clip (unless we clamped seek)
        seek_in_clip = (alert_time - birthdate).total_seconds() - seek
        seek_in_clip = max(0.0, seek_in_clip)
        thumbnail_file = self._generate_thumbnail(output_file, alert_time, seek_in_clip)

        return output_file, thumbnail_file
