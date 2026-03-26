import subprocess
import os
import shutil


def ensure_browser_playable_mp4(video_path: str, quiet: bool = False) -> None:
    """
    Re-encode video to H.264 with an IDR at frame 0 and faststart, for Safari/iOS
    (stream-copy cuts can start on a P-frame and show black on VideoToolbox).
    
    Args:
        video_path: Path to the video file to optimize
        quiet: If True, suppress output messages
    
    Raises:
        Exception: If ffmpeg is not found or conversion fails
    """
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
    
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-force_key_frames", "expr:eq(n,0)",
            "-movflags", "+faststart",
            temp_output,
        ]
        
        if quiet:
            # Suppress ffmpeg output
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
        else:
            result = subprocess.run(cmd, check=True)
        
        # Replace original file with optimized version
        if os.path.exists(temp_output):
            os.replace(temp_output, video_path)
            if not quiet:
                print(f"✅ Video optimized for browser playback: {os.path.basename(video_path)}")
    
    except subprocess.CalledProcessError as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise Exception(f"ffmpeg conversion failed: {e}")
    
    except Exception as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise


