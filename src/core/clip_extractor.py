"""
Clip Extractor for Local Video Chunks
Extracts video clips from continuous local recordings named YYYYMMDD_<random>.mp4.

These files carry no start time in their name: the date part is only a date, so each
file's time origin comes from a "<file>.json" sidecar (if present) or its filesystem
birthdate, and its end from the probed container duration. The files are non-overlapping,
so they tile the timeline and a clip window can be cut and concatenated across them.
"""
import datetime
import json
import subprocess
import os
import logging
import re
import math
import shutil
from typing import Optional, List, Dict, Tuple
from src.utils.video_utils import (
    ensure_browser_playable_mp4,
    ffmpeg_global_thread_args,
    should_run_browser_reencode,
)

# YYYYMMDD_<random [a-z0-9]>.mp4, e.g. 20260712_vlhst7a6.mp4
CONTINUOUS_FILENAME_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})_[a-z0-9]+\.mp4$")


class ClipExtractor:
    """Extracts video clips from continuous recordings, using each file's birthdate/sidecar as its time origin."""

    def __init__(self, before_seconds: int, after_seconds: int, output_dir: str,
                 local_source_dir: str):
        """
        Initialize clip extractor

        Args:
            before_seconds: Seconds before alert time to include
            after_seconds: Seconds after alert time to include
            output_dir: Directory to save temporary clip files
            local_source_dir: Local directory containing continuous video recordings (required)
        """
        if not local_source_dir:
            raise ValueError("local_source_dir is required")

        self.before_seconds = before_seconds
        self.after_seconds = after_seconds
        self.output_dir = output_dir
        self.local_source_dir = local_source_dir
        self.filename_re = CONTINUOUS_FILENAME_PATTERN

        # path -> (stat signature, start, end); avoids re-probing every file on every alert
        self._span_cache: Dict[str, Tuple[Tuple[int, int], datetime.datetime, datetime.datetime]] = {}

        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)

        if not os.path.isdir(local_source_dir):
            raise FileNotFoundError(f"Local source directory does not exist: {local_source_dir}")

        matching = [f for f in os.listdir(local_source_dir) if self.filename_re.match(f)]
        if not matching:
            raise FileNotFoundError(
                f"No continuous videos matching YYYYMMDD_<random>.mp4 found in {local_source_dir}"
            )

        logging.info(f"Using local source directory: {self.local_source_dir} ({len(matching)} video(s))")

    def _get_birthdate(self, video_path: str) -> datetime.datetime:
        """
        Creation time (birthdate) of a video file — the t=0 of its timeline.

        Tries os.stat().st_birthtime first (macOS, Python 3.12+ on Linux with ext4/btrfs),
        then falls back to `stat -c %W`, which returns 0 when the filesystem does not record
        birth time (treated as an error, since a wrong origin means a wrong clip).
        """
        stat = os.stat(video_path)

        if hasattr(stat, "st_birthtime"):
            ts = stat.st_birthtime
            if ts > 0:
                return datetime.datetime.fromtimestamp(ts)

        try:
            result = subprocess.run(
                ["stat", "-c", "%W", video_path],
                capture_output=True, text=True, timeout=10, check=True
            )
            ts = int(result.stdout.strip())
            if ts > 0:
                return datetime.datetime.fromtimestamp(ts)
            raise RuntimeError(
                f"stat -c %W returned 0 for {video_path} — filesystem does not record birth time. "
                "Use a filesystem that does (ext4, btrfs)."
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
            raise RuntimeError(f"Could not determine birthdate of {video_path}: {e}") from e

    def _get_sidecar_start(self, video_path: str) -> Optional[datetime.datetime]:
        """
        Timeline origin from "<video_path>.json" sidecar, e.g. {"start": "2026-08-03T07:00:08.166979"}.

        Returns None if the sidecar doesn't exist, or logs an error and returns None if it
        exists but is malformed, so the caller can fall back to the filesystem birthdate.
        """
        sidecar_path = video_path + ".json"
        if not os.path.isfile(sidecar_path):
            return None

        try:
            with open(sidecar_path, "r") as f:
                data = json.load(f)
            start = datetime.datetime.fromisoformat(data["start"])
        except Exception as e:
            logging.error(f"Malformed sidecar {sidecar_path}: {e}")
            return None

        if start.tzinfo is not None:
            start = start.astimezone().replace(tzinfo=None)

        logging.info(f"Using sidecar start {start} for {os.path.basename(video_path)}")
        return start

    def _get_span(self, video_path: str) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
        """Time range (start, end) covered by a video, or None if it can't be determined."""
        st = os.stat(video_path)
        signature = (st.st_size, st.st_mtime_ns)

        cached = self._span_cache.get(video_path)
        if cached and cached[0] == signature:
            return cached[1], cached[2]

        start = self._get_sidecar_start(video_path)
        if start is None:
            try:
                start = self._get_birthdate(video_path)
            except RuntimeError as e:
                logging.error(str(e))
                return None

        duration = self._ffprobe_duration_seconds(video_path)
        if duration is None:
            logging.error(f"Could not probe duration of {video_path}; skipping it")
            return None

        end = start + datetime.timedelta(seconds=duration)
        self._span_cache[video_path] = (signature, start, end)
        return start, end

    def _list_chunks(self, window_start: Optional[datetime.datetime] = None,
                      window_end: Optional[datetime.datetime] = None) -> List[Dict]:
        """
        List continuous videos as time-ranged chunks.

        Each file spans birthdate → birthdate + probed duration. Files that we cannot place on
        the timeline (no birth time, unprobeable) are skipped rather than silently misplaced.

        When window_start/window_end are given, files whose filename date falls a full day or
        more outside the window are skipped before probing. Probing (ffprobe, and for the
        currently-recording file, one that can never succeed until the file is finalized) is
        the expensive part of listing, and a backfill run for an old alert has no reason to
        pay it for today's in-progress recording. The one-day margin covers a file that started
        just before midnight but whose window-relevant content lands on the next day.

        Returns:
            List of chunk dictionaries with keys: path, name, S (start time), E (end time)
        """
        if not os.path.exists(self.local_source_dir):
            logging.error(f"Local source directory does not exist: {self.local_source_dir}")
            return []

        date_lo = date_hi = None
        if window_start is not None and window_end is not None:
            date_lo = (window_start - datetime.timedelta(days=1)).date()
            date_hi = (window_end + datetime.timedelta(days=1)).date()

        chunks = []

        try:
            for filename in sorted(os.listdir(self.local_source_dir)):
                match = self.filename_re.match(filename)
                if not match:
                    continue

                if date_lo is not None:
                    y, mo, d = map(int, match.groups())
                    file_date = datetime.date(y, mo, d)
                    if file_date < date_lo or file_date > date_hi:
                        continue

                filepath = os.path.join(self.local_source_dir, filename)
                span = self._get_span(filepath)
                if span is None:
                    continue

                start_time, end_time = span
                chunks.append({
                    "path": filepath,
                    "name": filename,
                    "S": start_time,
                    "E": end_time,
                })
        except OSError as e:
            logging.error(f"Failed to list continuous videos: {e}")
            return []

        chunks.sort(key=lambda c: c["S"])

        # The recordings are expected to tile the timeline; an overlap means a birthdate or a
        # duration is off, and the concatenated clip would repeat footage.
        for earlier, later in zip(chunks, chunks[1:]):
            if later["S"] < earlier["E"]:
                logging.warning(
                    f"Continuous videos overlap: {earlier['name']} ends {earlier['E']} but "
                    f"{later['name']} starts {later['S']}"
                )

        logging.debug(f"Found {len(chunks)} continuous video(s)")
        return chunks

    def _chunk_intersects_window(self, chunk: Dict, window_start: datetime.datetime,
                                  window_end: datetime.datetime) -> bool:
        """
        Check if a chunk intersects with the time window

        Args:
            chunk: Chunk dictionary with S and E keys
            window_start: Start of time window
            window_end: End of time window

        Returns:
            True if chunk intersects window, False otherwise
        """
        return not (chunk["E"] <= window_start or chunk["S"] >= window_end)

    def _cleanup_temp_files(self, temp_files: List[str]):
        """
        Clean up temporary files

        Args:
            temp_files: List of file paths to clean up
        """
        for file_path in temp_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logging.debug(f"Cleaned up temporary file: {file_path}")
            except Exception as e:
                logging.warning(f"Failed to remove temporary file {file_path}: {e}")

    def _thumbnail_seek_seconds_for_alert(
        self,
        selected_chunks: List[Dict],
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        alert_time: datetime.datetime,
    ) -> float:
        """
        Position in the concatenated output (seconds from t=0) that corresponds to alert_time.
        """
        segments: List[Tuple[datetime.datetime, datetime.datetime, float]] = []
        for chunk in selected_chunks:
            chunk_start = max(chunk["S"], window_start)
            chunk_end = min(chunk["E"], window_end)
            dur = (chunk_end - chunk_start).total_seconds()
            if dur <= 0:
                continue
            segments.append((chunk_start, chunk_end, dur))
        total = sum(s[2] for s in segments)
        if total <= 0:
            return 0.0

        first_start, last_end = segments[0][0], segments[-1][1]
        if alert_time < first_start:
            logging.warning("Alert is before the first segment in the clip; thumbnail at start")
            return 0.0
        if alert_time > last_end:
            logging.warning("Alert is after the last segment in the clip; thumbnail near end")
            return max(0.0, total - 0.05)

        accumulated = 0.0
        for chunk_start, chunk_end, dur in segments:
            if chunk_start <= alert_time <= chunk_end:
                offset = accumulated + (alert_time - chunk_start).total_seconds()
                return max(0.0, min(offset, total - 0.05))
            accumulated += dur

        logging.warning(
            "Alert time falls in a gap between segments; using midpoint of clip for thumbnail"
        )
        return max(0.0, min(total / 2.0, total - 0.05))

    def _ffprobe_duration_seconds(self, video_path: str) -> Optional[float]:
        """Actual container duration in seconds (may differ slightly from wall-clock segment sum)."""
        if not shutil.which("ffprobe"):
            return None
        try:
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            d = float(r.stdout.strip())
            if d > 0 and not math.isnan(d):
                return d
        except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired, OSError):
            pass
        return None

    def _generate_thumbnail(
        self,
        video_file: str,
        alert_time: datetime.datetime,
        seek_seconds: float,
    ) -> Optional[str]:
        """
        Generate a thumbnail JPEG from one frame at seek_seconds (alert moment in the clip).

        Args:
            video_file: Path to the video file
            alert_time: Alert datetime for output filename
            seek_seconds: Time offset in the clip (seconds) for the frame to capture

        Returns:
            Path to the thumbnail image, or None if generation failed
        """
        timestamp = alert_time.strftime('%Y%m%d_%H%M%S')
        thumbnail_file = os.path.join(self.output_dir, f"thumb_{timestamp}.jpg")

        seek = max(0.0, float(seek_seconds))
        probed = self._ffprobe_duration_seconds(video_file)
        if probed is not None:
            # Avoid seeking past EOF (stream-copy concat length vs. clock can differ slightly)
            seek = min(seek, max(0.0, probed - 0.1))
        logging.info(
            f"Generating thumbnail at t={seek:.2f}s in clip"
            + (f" (probed duration {probed:.2f}s)" if probed is not None else "")
        )

        try:
            # -ss BEFORE -i: input seek (keyframe-aligned, fast, avoids decoding the whole clip).
            # -ss after -i was slow on long clips and often caused timeouts or bad frames on Pi / MP4.
            ss = f"{seek:.3f}"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    *ffmpeg_global_thread_args(),
                    "-ss", ss,
                    "-i", video_file,
                    "-an",
                    "-sn",
                    "-map", "0:v:0",
                    "-frames:v", "1",
                    "-vf",
                    "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                    "-q:v", "2",
                    thumbnail_file,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if os.path.exists(thumbnail_file) and os.path.getsize(thumbnail_file) > 0:
                logging.info(f"Thumbnail generated: {thumbnail_file}")
                return thumbnail_file
            else:
                logging.warning("Thumbnail file was not created or is empty")
                return None

        except subprocess.CalledProcessError as e:
            logging.error(f"FFmpeg thumbnail generation failed: {e.stderr}")
            logging.error(f"FFmpeg stdout: {e.stdout}")
            return None
        except subprocess.TimeoutExpired:
            logging.error("FFmpeg timeout during thumbnail generation")
            return None
        except Exception as e:
            logging.error(f"Unexpected error generating thumbnail: {e}")
            return None

    def extract_clip(self, alert_time_iso: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract a video clip for the given alert time from S3 chunks

        Args:
            alert_time_iso: Alert datetime in ISO format

        Returns:
            Tuple of (video_file_path, thumbnail_file_path), or (None, None) if extraction failed
        """
        logging.info(f"Starting clip extraction for alert time: {alert_time_iso}")

        # Parse alert time (strip timezone info if present)
        try:
            alert_time = datetime.datetime.fromisoformat(alert_time_iso.replace('Z', ''))
            # Remove timezone info if present
            if alert_time.tzinfo is not None:
                alert_time = alert_time.replace(tzinfo=None)
        except ValueError:
            # Try parsing without timezone
            alert_time = datetime.datetime.fromisoformat(alert_time_iso)
            if alert_time.tzinfo is not None:
                alert_time = alert_time.replace(tzinfo=None)

        logging.debug(f"Parsed alert time: {alert_time}")

        # Calculate time window
        window_start = alert_time - datetime.timedelta(seconds=self.before_seconds)
        window_end = alert_time + datetime.timedelta(seconds=self.after_seconds)

        logging.info(f"Clip time window: {window_start} to {window_end} (before: {self.before_seconds}s, after: {self.after_seconds}s)")

        # List all chunks from local directory
        all_chunks = self._list_chunks(window_start, window_end)
        if not all_chunks:
            logging.error("No chunks found in local directory or failed to list chunks")
            return None, None

        # Find chunks that intersect with the time window
        selected_chunks = [c for c in all_chunks if self._chunk_intersects_window(c, window_start, window_end)]

        if not selected_chunks:
            logging.warning(f"No chunks intersect with time window {window_start} → {window_end}")
            return None, None

        logging.info(f"Found {len(selected_chunks)} chunk(s) intersecting time window")

        # Process each selected chunk
        part_files = []
        temp_files_to_cleanup = []

        try:
            for idx, chunk in enumerate(selected_chunks):
                logging.info(f"Processing chunk {idx + 1}/{len(selected_chunks)}: {chunk['name']}")

                part_mp4 = os.path.join(self.output_dir, f"part_{idx}.mp4")
                temp_files_to_cleanup.append(part_mp4)

                # Use local file directly
                local_mp4 = chunk["path"]
                logging.debug(f"Using local file: {local_mp4}")

                # Calculate intersection of chunk time range with window
                chunk_start = max(chunk["S"], window_start)
                chunk_end = min(chunk["E"], window_end)

                # Calculate offset and duration within the chunk
                offset_seconds = (chunk_start - chunk["S"]).total_seconds()
                duration_seconds = (chunk_end - chunk_start).total_seconds()

                logging.debug(f"Extracting segment: offset={offset_seconds}s, duration={duration_seconds}s")

                # Extract the relevant segment from the chunk
                try:
                    subprocess.run([
                        "ffmpeg", "-y",
                        *ffmpeg_global_thread_args(),
                        "-ss", str(offset_seconds),
                        "-i", local_mp4,
                        "-t", str(duration_seconds),
                        "-c", "copy",
                        part_mp4
                    ], check=True, capture_output=True, text=True, timeout=60)
                except subprocess.CalledProcessError as e:
                    logging.error(f"FFmpeg segment extraction failed: {e.stderr}")
                    logging.error(f"FFmpeg stdout: {e.stdout}")
                    self._cleanup_temp_files(temp_files_to_cleanup)
                    return None, None
                except subprocess.TimeoutExpired:
                    logging.error("FFmpeg timeout during segment extraction")
                    self._cleanup_temp_files(temp_files_to_cleanup)
                    return None, None

                part_files.append(part_mp4)

            # Concatenate all parts into final video
            if not part_files:
                logging.error("No parts to concatenate")
                self._cleanup_temp_files(temp_files_to_cleanup)
                return None, None

            # Create concat file for ffmpeg
            timestamp = alert_time.strftime('%Y%m%d_%H%M%S')
            output_file = os.path.join(self.output_dir, f"alert_clip_{timestamp}.mp4")
            concat_file = os.path.join(self.output_dir, f"concat_{timestamp}.txt")
            temp_files_to_cleanup.append(concat_file)

            # Write concat file
            with open(concat_file, 'w', encoding='utf-8') as f:
                for part_file in part_files:
                    # Use absolute path and escape single quotes for ffmpeg
                    abs_path = os.path.abspath(part_file).replace('\\', '/')
                    f.write(f"file '{abs_path}'\n")

            logging.info(f"Concatenating {len(part_files)} part(s) into final video...")

            # First concatenate parts (using copy for speed)
            temp_concat_file = output_file.replace('.mp4', '_temp.mp4')
            temp_files_to_cleanup.append(temp_concat_file)

            try:
                subprocess.run([
                    "ffmpeg", "-y",
                    *ffmpeg_global_thread_args(),
                    "-f", "concat",
                    "-safe", "0",
                    "-i", concat_file,
                    "-c", "copy",  # Copy streams without re-encoding for speed
                    temp_concat_file
                ], check=True, capture_output=True, text=True, timeout=300)
            except subprocess.CalledProcessError as e:
                logging.error(f"FFmpeg concatenation failed: {e.stderr}")
                logging.error(f"FFmpeg stdout: {e.stdout}")
                self._cleanup_temp_files(temp_files_to_cleanup)
                return None, None
            except subprocess.TimeoutExpired:
                logging.error("FFmpeg timeout during concatenation")
                self._cleanup_temp_files(temp_files_to_cleanup)
                return None, None

            # Verify concatenated file was created
            if not os.path.exists(temp_concat_file) or os.path.getsize(temp_concat_file) == 0:
                logging.error("Concatenated file is empty or doesn't exist")
                self._cleanup_temp_files(temp_files_to_cleanup)
                return None, None

            # Heavy libx264 + faststart is off by default; opt in with ALERT_VIDEOS_BROWSER_REENCODE=1
            if not should_run_browser_reencode():
                logging.info(
                    "Skipping browser re-encode (default); set ALERT_VIDEOS_BROWSER_REENCODE=1 to enable"
                )
                os.replace(temp_concat_file, output_file)
            else:
                logging.info("Optimizing video for browser playback (H.264 + faststart)...")
                try:
                    ensure_browser_playable_mp4(temp_concat_file, quiet=True)
                    os.replace(temp_concat_file, output_file)
                    logging.info("Video optimized successfully for browser playback")
                except Exception as e:
                    logging.error(f"Video optimization failed: {e}")
                    logging.exception("Full traceback:")
                    logging.warning("Using non-optimized concatenated file")
                    if os.path.exists(temp_concat_file):
                        os.replace(temp_concat_file, output_file)

            # Verify final output file was created
            if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
                logging.error("Final output file is empty or doesn't exist")
                self._cleanup_temp_files(temp_files_to_cleanup)
                return None, None

            output_size = os.path.getsize(output_file)
            logging.info(f"MP4 file created: {output_size / 1024 / 1024:.2f} MB")

            # Thumbnail at the alert instant in the clip timeline (not the start of the file)
            seek_thumb = self._thumbnail_seek_seconds_for_alert(
                selected_chunks, window_start, window_end, alert_time
            )
            thumbnail_file = self._generate_thumbnail(output_file, alert_time, seek_thumb)

            # Clean up temporary files (but keep the final output and thumbnail)
            if output_file in temp_files_to_cleanup:
                temp_files_to_cleanup.remove(output_file)
            if thumbnail_file and thumbnail_file in temp_files_to_cleanup:
                temp_files_to_cleanup.remove(thumbnail_file)
            self._cleanup_temp_files(temp_files_to_cleanup)

            return output_file, thumbnail_file

        except Exception as e:
            logging.error(f"Unexpected error during clip extraction: {e}")
            logging.exception("Full traceback:")
            self._cleanup_temp_files(temp_files_to_cleanup)
            return None, None
