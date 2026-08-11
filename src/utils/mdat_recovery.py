"""
Recovery for continuous-recording MP4s that have no moov atom (e.g. power loss or a
killed process mid-write), which makes them unprobeable by ffprobe/ffmpeg.

Walks the mdat payload as AVCC-style 4-byte-length-prefixed NAL units and writes an
Annex-B H.264 elementary stream, resyncing byte-by-byte on garbage, then remuxes that
stream into a fresh MP4 so the normal ffprobe/ffmpeg pipeline can use it.

Used as a fallback by ContinuousClipExtractor when a chunk's duration can't be probed.
"""
import logging
import mmap
import os
import shutil
import struct
import subprocess
from typing import Optional

from src.utils.video_utils import ffmpeg_global_thread_args

MAX_NAL = 8 * 1024 * 1024
START = b"\x00\x00\x00\x01"

# Raw H.264 (Annex B) carries no timing info, so the remux needs an assumed frame rate.
DEFAULT_RECOVERY_FPS = float(os.environ.get("ALERT_VIDEOS_RECOVERY_FPS", "25") or "25")


def _find_mdat_payload(mm: mmap.mmap, size: int) -> int:
    pos = 0
    while pos + 8 <= size:
        box = struct.unpack(">I", mm[pos:pos + 4])[0]
        btype = mm[pos + 4:pos + 8]
        if btype == b"mdat":
            return pos + 16 if box == 1 else pos + 8
        if box < 8:
            break
        pos += box
    idx = mm.find(b"mdat", 0, min(size, 1 << 20))
    if idx < 0:
        raise ValueError("no mdat box found in first 1 MiB")
    return idx + 4


def _plausible_nal_header(b: int) -> bool:
    if b & 0x80:
        return False
    t = b & 0x1F
    return 1 <= t <= 12


def extract_h264_from_mdat(src_path: str, dst_path: str) -> bool:
    """
    Walk src_path's mdat payload and write an Annex-B H.264 stream to dst_path.

    Returns True if at least one NAL unit was recovered, False otherwise (no mdat box,
    or nothing in the payload looks like H.264). Leaves no file behind on failure.
    """
    size = os.path.getsize(src_path)
    if size == 0:
        return False

    kept = resync = 0
    with open(src_path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            try:
                pos = _find_mdat_payload(mm, size)
            except ValueError as e:
                logging.error(f"Recovery scan failed for {src_path}: {e}")
                return False

            logging.debug(
                f"{src_path}: mdat payload at 0x{pos:x}, "
                f"{(size - pos) / 1e6:.1f} MB to scan"
            )

            with open(dst_path, "wb") as out:
                while pos + 5 <= size:
                    ln = struct.unpack(">I", mm[pos:pos + 4])[0]
                    avail = size - (pos + 4)

                    if ln == 0 or ln > MAX_NAL or not _plausible_nal_header(mm[pos + 4]):
                        pos += 1
                        resync += 1
                        continue

                    if ln > avail:
                        out.write(START)
                        out.write(mm[pos + 4:size])
                        kept += 1
                        break

                    out.write(START)
                    out.write(mm[pos + 4:pos + 4 + ln])
                    kept += 1
                    pos += 4 + ln
        finally:
            mm.close()

    logging.info(f"{src_path}: recovered {kept} NAL unit(s), skipped {resync} resync byte(s)")
    if kept == 0 and os.path.exists(dst_path):
        os.remove(dst_path)
    return kept > 0


def _remux_h264_to_mp4(h264_path: str, mp4_path: str, framerate: float) -> bool:
    if not shutil.which("ffmpeg"):
        logging.error("ffmpeg not found in PATH; cannot remux recovered H.264 stream")
        return False
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                *ffmpeg_global_thread_args(),
                "-r", str(framerate),
                "-f", "h264",
                "-i", h264_path,
                "-c", "copy",
                mp4_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to remux recovered stream {h264_path} -> {mp4_path}: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout remuxing recovered stream {h264_path} -> {mp4_path}")
        return False
    return os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0


def recover_broken_mp4(
    src_path: str,
    output_dir: str,
    framerate: float = DEFAULT_RECOVERY_FPS,
) -> Optional[str]:
    """
    Best-effort recovery for an MP4 that ffprobe can't read (typically: recording was
    interrupted before the moov atom was written). Pulls the raw H.264 out of the mdat
    payload and remuxes it into a new, probeable MP4 in output_dir.

    The recovered file has no reliable per-frame timestamps (Annex-B carries none), so
    its duration reflects `framerate` rather than the wall clock -- good enough to place
    it on the timeline and cut a clip, not frame-accurate.

    Returns the path to the recovered MP4, or None if recovery wasn't possible. The
    recovered MP4's filename is deterministic (derived from src_path's basename), so
    callers can treat it as a cache: skip calling this again while the recovered file's
    mtime is >= src_path's mtime.
    """
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    h264_path = os.path.join(output_dir, f"{base}.recovered.h264")
    mp4_path = os.path.join(output_dir, f"{base}.recovered.mp4")

    logging.warning(f"Attempting mdat recovery for unprobeable file: {src_path}")

    try:
        recovered_any = extract_h264_from_mdat(src_path, h264_path)
    except OSError as e:
        logging.error(f"Failed to read {src_path} for recovery: {e}")
        recovered_any = False

    if not recovered_any:
        logging.error(f"No recoverable H.264 data found in {src_path}")
        return None

    ok = _remux_h264_to_mp4(h264_path, mp4_path, framerate)
    if os.path.exists(h264_path):
        os.remove(h264_path)

    if not ok:
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
        return None

    logging.info(f"Recovered {src_path} -> {mp4_path}")
    return mp4_path


def recovered_mp4_path(src_path: str, output_dir: str) -> str:
    """Deterministic path recover_broken_mp4() would write to for src_path, without running recovery."""
    base = os.path.splitext(os.path.basename(src_path))[0]
    return os.path.join(output_dir, f"{base}.recovered.mp4")
