"""
Continuous Clip Extractor
Extracts video clips from continuous recordings named YYYYMMDD_<random>.mp4.

Unlike the chunk mode, these files carry no start time in their name: the date part
is only a date, so each file's time origin comes from its filesystem birthdate and its
end from the probed container duration. The files are non-overlapping, so they tile the
timeline and a clip window can be cut and concatenated across them exactly like chunks.
"""
import datetime
import json
import subprocess
import os
import logging
from typing import Optional, List, Dict, Tuple

from src.core.clip_extractor import ClipExtractor

# YYYYMMDD_<random [a-z0-9]>.mp4, e.g. 20260712_vlhst7a6.mp4
CONTINUOUS_FILENAME_PATTERN = r"^(\d{4})(\d{2})(\d{2})_[a-z0-9]+\.mp4$"


class ContinuousClipExtractor(ClipExtractor):
    """Extracts clips from continuous recordings, using each file's birthdate as its time origin."""

    def __init__(self, before_seconds: int, after_seconds: int, output_dir: str,
                 local_source_dir: str):
        super().__init__(
            before_seconds=before_seconds,
            after_seconds=after_seconds,
            output_dir=output_dir,
            chunk_filename_pattern=CONTINUOUS_FILENAME_PATTERN,
            local_source_dir=local_source_dir,
        )

        # path -> (stat signature, start, end); avoids re-probing every file on every alert
        self._span_cache: Dict[str, Tuple[Tuple[int, int], datetime.datetime, datetime.datetime]] = {}

        if not os.path.isdir(local_source_dir):
            raise FileNotFoundError(f"Local source directory does not exist: {local_source_dir}")

        matching = [f for f in os.listdir(local_source_dir) if self.filename_re.match(f)]
        if not matching:
            raise FileNotFoundError(
                f"No continuous videos matching YYYYMMDD_<random>.mp4 found in {local_source_dir}"
            )

        logging.info(f"Continuous mode: {len(matching)} video(s) in {local_source_dir}")

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

    def _list_local_chunks(self) -> List[Dict]:
        """
        List continuous videos as time-ranged chunks.

        Each file spans birthdate → birthdate + probed duration. Files that we cannot place on
        the timeline (no birth time, unprobeable) are skipped rather than silently misplaced.
        """
        if not os.path.exists(self.local_source_dir):
            logging.error(f"Local source directory does not exist: {self.local_source_dir}")
            return []

        chunks = []

        try:
            for filename in sorted(os.listdir(self.local_source_dir)):
                if not self.filename_re.match(filename):
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
