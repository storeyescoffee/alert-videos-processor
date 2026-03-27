"""
Clip Extractor for Local Video Chunks
Extracts video clips from local MP4 chunks for a given alert time
"""
import datetime
import subprocess
import os
import logging
import re
from typing import Optional, List, Dict, Tuple
from pathlib import Path
from src.utils.video_utils import (
    ensure_browser_playable_mp4,
    ffmpeg_global_thread_args,
    should_run_browser_reencode,
)


class ClipExtractor:
    """Extracts video clips from local video chunks"""
    
    def __init__(self, before_minutes: int, after_minutes: int, output_dir: str,
                 chunk_duration_seconds: int = 300, chunk_filename_pattern: str = None,
                 local_source_dir: str = None):
        """
        Initialize clip extractor
        
        Args:
            before_minutes: Minutes before alert time to include
            after_minutes: Minutes after alert time to include
            output_dir: Directory to save temporary clip files
            chunk_duration_seconds: Duration of each chunk in seconds (default: 300 = 5 minutes)
            chunk_filename_pattern: Regex pattern for chunk filenames (default: gcam_DDMMYYYY_HHMMSS.mp4)
            local_source_dir: Local directory containing video chunks (required)
        """
        if not local_source_dir:
            raise ValueError("local_source_dir is required")
        
        self.before_minutes = before_minutes
        self.after_minutes = after_minutes
        self.output_dir = output_dir
        self.chunk_duration_seconds = chunk_duration_seconds
        self.local_source_dir = local_source_dir
        
        # Default filename pattern: gcam_DDMMYYYY_HHMMSS.mp4
        if chunk_filename_pattern is None:
            self.filename_re = re.compile(r"gcam_(\d{2})(\d{2})(\d{4})_(\d{2})(\d{2})(\d{2})\.mp4")
        else:
            self.filename_re = re.compile(chunk_filename_pattern)
        
        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)
        
        logging.info(f"Using local source directory: {self.local_source_dir}")
    
    
    def _parse_chunk_start_time(self, filename: str) -> Optional[datetime.datetime]:
        """
        Parse chunk start time from filename
        
        Args:
            filename: Chunk filename (e.g., gcam_22122025_075030.mp4)
            
        Returns:
            Datetime object representing chunk start time, or None if parsing fails
        """
        match = self.filename_re.match(filename)
        if not match:
            return None
        
        # Extract date components: DD, MM, YYYY, HH, MM, SS
        d, mo, y, h, mi, s = map(int, match.groups())
        return datetime.datetime(y, mo, d, h, mi, s)
    
    def _list_chunks(self) -> List[Dict]:
        """
        List all video chunks from local directory
        
        Returns:
            List of chunk dictionaries with keys: path, name, S (start time), E (end time)
        """
        return self._list_local_chunks()
    
    def _list_local_chunks(self) -> List[Dict]:
        """
        List all video chunks from local directory
        
        Returns:
            List of chunk dictionaries with keys: path, name, S (start time), E (end time)
        """
        chunks = []
        
        if not os.path.exists(self.local_source_dir):
            logging.error(f"Local source directory does not exist: {self.local_source_dir}")
            return []
        
        try:
            for filename in os.listdir(self.local_source_dir):
                if not filename.endswith('.mp4'):
                    continue
                
                # Parse start time from filename
                start_time = self._parse_chunk_start_time(filename)
                if not start_time:
                    continue
                
                # Calculate end time (chunk duration after start time)
                end_time = start_time + datetime.timedelta(seconds=self.chunk_duration_seconds)
                
                filepath = os.path.join(self.local_source_dir, filename)
                
                chunks.append({
                    "path": filepath,
                    "name": filename,
                    "S": start_time,
                    "E": end_time
                })
            
            # Sort chunks by start time
            chunks.sort(key=lambda x: x["S"])
            logging.debug(f"Found {len(chunks)} video chunks in local directory")
            return chunks
            
        except Exception as e:
            logging.error(f"Failed to list chunks from local directory: {e}")
            logging.exception("Full traceback:")
            return []
    
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

        logging.info(f"Generating thumbnail at t={seek_seconds:.2f}s (alert time in clip)...")

        try:
            # -ss after -i decodes to the requested time (frame-accurate for the thumbnail)
            ss = f"{max(0.0, seek_seconds):.3f}"
            subprocess.run([
                "ffmpeg", "-y",
                *ffmpeg_global_thread_args(),
                "-i", video_file,
                "-ss", ss,
                "-vframes", "1",
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                "-q:v", "2",
                thumbnail_file
            ], check=True, capture_output=True, text=True, timeout=60)
            
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
        before_seconds = self.before_minutes * 60
        after_seconds = self.after_minutes * 60
        window_start = alert_time - datetime.timedelta(seconds=before_seconds)
        window_end = alert_time + datetime.timedelta(seconds=after_seconds)
        
        logging.info(f"Clip time window: {window_start} to {window_end} (before: {self.before_minutes}min, after: {self.after_minutes}min)")
        
        # List all chunks from local directory
        all_chunks = self._list_chunks()
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
