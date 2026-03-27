import subprocess
import os
import shutil
import platform
from typing import List


def ffmpeg_global_thread_args() -> List[str]:
    """
    Global ffmpeg options to cap thread usage (decode + filters + encoders that honor -threads).
    On Pi Zero / Pi 1–3 (armv6l / armv7l), defaults to a single thread when unset to reduce RAM use.

    ALERT_VIDEOS_FFMPEG_THREADS:
      unset  → 1 thread on armv6l/armv7l; otherwise omit (ffmpeg default / auto)
      0      → omit (ffmpeg default)
      N      → -threads N
    """
    raw = os.environ.get("ALERT_VIDEOS_FFMPEG_THREADS")
    if raw is None:
        if platform.machine().lower() in ("armv6l", "armv7l"):
            return ["-threads", "1"]
        return []
    raw = raw.strip()
    if raw == "" or raw == "0":
        return []
    if raw.isdigit() and int(raw) > 0:
        return ["-threads", raw]
    return ["-threads", "1"]


def should_run_browser_reencode() -> bool:
    """
    Heavy libx264 re-encode + faststart is opt-in (off by default).

    Set ALERT_VIDEOS_BROWSER_REENCODE=1 (or true/yes/on) to enable Safari/iOS-friendly output.
    """
    v = os.environ.get("ALERT_VIDEOS_BROWSER_REENCODE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _libx264_preset() -> str:
    """Default veryfast uses less encoder RAM than 'fast'. Override: ALERT_VIDEOS_FFMPEG_PRESET."""
    return os.environ.get("ALERT_VIDEOS_FFMPEG_PRESET", "veryfast").strip() or "veryfast"


def ensure_browser_playable_mp4(video_path: str, quiet: bool = False) -> None:
    """
    Re-encode video to H.264 with an IDR at frame 0 and faststart, for Safari/iOS
    (stream-copy cuts can start on a P-frame and show black on VideoToolbox).

    Only call this when should_run_browser_reencode() is true (see ALERT_VIDEOS_BROWSER_REENCODE).

    Env:
      ALERT_VIDEOS_FFMPEG_THREADS — see ffmpeg_global_thread_args()
      ALERT_VIDEOS_FFMPEG_PRESET — libx264 preset (default veryfast)
      ALERT_VIDEOS_FFMPEG_CRF — default 23

    Args:
        video_path: Path to the video file to optimize
        quiet: If True, suppress output messages

    Raises:
        Exception: If ffmpeg is not found or conversion fails
    """
    # Resolve $HOME, %USERPROFILE%, ~, etc. (config may pass unexpanded paths)
    video_path = os.path.normpath(os.path.expanduser(os.path.expandvars(video_path)))
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Check if ffmpeg is available
    if not shutil.which("ffmpeg"):
        if not quiet:
            print("⚠️  ffmpeg not found in PATH. Skipping browser optimization.")
            print("   Install ffmpeg: https://ffmpeg.org/download.html")
        return

    # Create temporary output file
    temp_output = video_path + ".temp.mp4"

    crf = os.environ.get("ALERT_VIDEOS_FFMPEG_CRF", "23").strip() or "23"

    try:
        cmd = [
            "ffmpeg",
            "-y",
            *ffmpeg_global_thread_args(),
            "-i", video_path,
            "-c:v", "libx264",
            "-preset", _libx264_preset(),
            "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-force_key_frames", "expr:eq(n,0)",
            "-movflags", "+faststart",
            temp_output,
        ]

        if quiet:
            # Suppress ffmpeg output
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
        else:
            subprocess.run(cmd, check=True)

        # Replace original file with optimized version
        if os.path.exists(temp_output):
            os.replace(temp_output, video_path)
            if not quiet:
                print(f"✅ Video optimized for browser playback: {os.path.basename(video_path)}")

    except subprocess.CalledProcessError as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise Exception(f"ffmpeg conversion failed: {e}") from e

    except Exception as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise
