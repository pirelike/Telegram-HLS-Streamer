"""
Video Processing Module for Telegram Video Streaming System.

This module handles video conversion and segmentation using FFmpeg,
transforming input videos into HLS (HTTP Live Streaming) format
suitable for streaming and upload to Telegram.

Features:
- Automatic video probing and analysis
- Intelligent segment duration calculation
- Quality preservation during conversion
- Error handling and validation
- Progress monitoring support
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import shutil

from logger_config import get_logger

logger = get_logger(__name__)


class VideoProcessingError(Exception):
    """Custom exception for video processing failures."""
    pass


class FFmpegNotFoundError(VideoProcessingError):
    """Exception raised when FFmpeg is not available."""
    pass


class VideoProcessor:
    """
    Handles video processing operations using FFmpeg.

    This class provides methods for video analysis, segmentation,
    and format conversion optimized for streaming applications.
    """

    # FFmpeg timeout settings
    PROBE_TIMEOUT = 60  # seconds
    CONVERSION_TIMEOUT = 3600  # 1 hour

    # Video quality settings
    DEFAULT_SEGMENT_DURATION = 30  # seconds
    MIN_SEGMENT_DURATION = 5
    MAX_SEGMENT_DURATION = 60

    # File size constraints
    TARGET_SEGMENT_SIZE = 20 * 1024 * 1024  # 20MB target
    MAX_SEGMENT_SIZE = 45 * 1024 * 1024     # 45MB max (leave buffer for Telegram's 50MB limit)

    def __init__(self):
        """Initialize the video processor and verify FFmpeg availability."""
        self._verify_ffmpeg()
        logger.info("VideoProcessor initialized successfully")

    def _verify_ffmpeg(self) -> None:
        """
        Verify that FFmpeg is available and working.

        Raises:
            FFmpegNotFoundError: If FFmpeg is not found or not working
        """
        try:
            # Test FFmpeg availability
            result = subprocess.run(
                ['ffmpeg', '-version'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise FFmpegNotFoundError("FFmpeg command failed")

            # Test FFprobe availability
            result = subprocess.run(
                ['ffprobe', '-version'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise FFmpegNotFoundError("FFprobe command failed")

            logger.debug("FFmpeg and FFprobe verified successfully")

        except FileNotFoundError:
            raise FFmpegNotFoundError(
                "FFmpeg not found. Please install FFmpeg and ensure it's in your PATH."
            )
        except subprocess.TimeoutExpired:
            raise FFmpegNotFoundError("FFmpeg verification timed out")
        except Exception as e:
            raise FFmpegNotFoundError(f"FFmpeg verification failed: {e}") from e

    def analyze_video(self, video_path: str) -> Dict[str, Any]:
        """
        Analyze a video file and extract detailed information.

        Args:
            video_path: Path to the video file to analyze

        Returns:
            Dictionary containing video information including:
            - duration: Video duration in seconds
            - bitrate: Average bitrate in bits per second
            - size: File size in bytes
            - video_streams: List of video stream information
            - audio_streams: List of audio stream information
            - format_info: Container format information

        Raises:
            VideoProcessingError: If video analysis fails
        """
        video_file = Path(video_path)

        if not video_file.exists():
            raise VideoProcessingError(f"Video file not found: {video_path}")

        if not video_file.is_file():
            raise VideoProcessingError(f"Path is not a file: {video_path}")

        try:
            logger.info(f"Analyzing video: {video_file.name}")

            # Run ffprobe to get detailed video information
            probe_cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                str(video_file)
            ]

            result = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.PROBE_TIMEOUT
            )

            probe_data = json.loads(result.stdout)

            # Extract format information
            format_info = probe_data.get('format', {})
            streams = probe_data.get('streams', [])

            # Separate video and audio streams
            video_streams = [s for s in streams if s.get('codec_type') == 'video']
            audio_streams = [s for s in streams if s.get('codec_type') == 'audio']

            # Calculate basic metrics
            duration = float(format_info.get('duration', 0))
            bitrate = int(format_info.get('bit_rate', 0))
            file_size = int(format_info.get('size', video_file.stat().st_size))

            analysis_result = {
                'filename': video_file.name,
                'file_path': str(video_file),
                'file_size': file_size,
                'duration': duration,
                'bitrate': bitrate,
                'format_name': format_info.get('format_name', 'unknown'),
                'format_long_name': format_info.get('format_long_name', 'unknown'),
                'video_streams': video_streams,
                'audio_streams': audio_streams,
                'total_streams': len(streams),
                'format_info': format_info
            }

            # Log analysis summary
            logger.info(f"Video analysis complete:")
            logger.info(f"  Duration: {duration:.2f} seconds ({duration/60:.1f} minutes)")
            logger.info(f"  Size: {file_size / (1024**2):.1f} MB")
            logger.info(f"  Bitrate: {bitrate / 1000:.0f} kbps")
            logger.info(f"  Video streams: {len(video_streams)}")
            logger.info(f"  Audio streams: {len(audio_streams)}")

            return analysis_result

        except subprocess.CalledProcessError as e:
            error_msg = f"FFprobe failed: {e.stderr or 'Unknown error'}"
            logger.error(error_msg)
            raise VideoProcessingError(error_msg) from e
        except subprocess.TimeoutExpired:
            error_msg = f"Video analysis timed out after {self.PROBE_TIMEOUT} seconds"
            logger.error(error_msg)
            raise VideoProcessingError(error_msg)
        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse FFprobe output: {e}"
            logger.error(error_msg)
            raise VideoProcessingError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error during video analysis: {e}"
            logger.error(error_msg, exc_info=True)
            raise VideoProcessingError(error_msg) from e

    def calculate_optimal_segment_duration(
        self,
        video_info: Dict[str, Any],
        max_segment_size: int = TARGET_SEGMENT_SIZE
    ) -> float:
        """
        Calculate optimal segment duration based on video characteristics.

        This method aims to keep segments under the specified size limit
        while maintaining reasonable playback segments.

        Args:
            video_info: Video information from analyze_video()
            max_segment_size: Maximum desired segment size in bytes

        Returns:
            Optimal segment duration in seconds
        """
        try:
            bitrate = video_info.get('bitrate', 0)
            duration = video_info.get('duration', 0)

            if bitrate <= 0 or duration <= 0:
                logger.warning("Invalid video bitrate or duration, using default segment duration")
                return self.DEFAULT_SEGMENT_DURATION

            # Calculate segment duration to achieve target size
            # Formula: segment_size = bitrate * segment_duration / 8
            target_duration = (max_segment_size * 8) / bitrate

            # Apply safety factor to account for bitrate variations
            target_duration *= 0.8

            # Clamp to reasonable range
            optimal_duration = max(
                self.MIN_SEGMENT_DURATION,
                min(target_duration, self.MAX_SEGMENT_DURATION)
            )

            logger.info(
                f"Calculated optimal segment duration: {optimal_duration:.1f}s "
                f"(target size: {max_segment_size / (1024**2):.1f} MB)"
            )

            return optimal_duration

        except Exception as e:
            logger.warning(f"Error calculating optimal segment duration: {e}")
            return self.DEFAULT_SEGMENT_DURATION

    def split_video_to_hls(
        self,
        video_path: str,
        output_dir: str,
        max_chunk_size: int = TARGET_SEGMENT_SIZE,
        segment_duration: Optional[float] = None
    ) -> str:
        """
        Split a video file into HLS segments using FFmpeg.

        This method converts a video into HTTP Live Streaming format,
        creating .ts segment files and a .m3u8 playlist.

        Args:
            video_path: Path to the source video file
            output_dir: Directory where segments and playlist will be saved
            max_chunk_size: Maximum size for each segment in bytes
            segment_duration: Override segment duration (calculated if None)

        Returns:
            Path to the generated .m3u8 playlist file

        Raises:
            VideoProcessingError: If video processing fails
        """
        video_file = Path(video_path)
        output_path = Path(output_dir)

        # Validate inputs
        if not video_file.exists():
            raise VideoProcessingError(f"Video file not found: {video_path}")

        # Create output directory
        output_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created output directory: {output_path}")

        try:
            # Analyze video to determine optimal settings
            logger.info("Analyzing video for optimal processing settings...")
            video_info = self.analyze_video(video_path)

            # Calculate segment duration if not provided
            if segment_duration is None:
                segment_duration = self.calculate_optimal_segment_duration(
                    video_info, max_chunk_size
                )

            # Prepare file paths
            playlist_path = output_path / 'playlist.m3u8'
            segment_pattern = output_path / 'segment_%04d.ts'

            # Build FFmpeg command
            ffmpeg_cmd = self._build_ffmpeg_command(
                video_path=str(video_file),
                playlist_path=str(playlist_path),
                segment_pattern=str(segment_pattern),
                segment_duration=segment_duration,
                video_info=video_info
            )

            logger.info("Starting video segmentation...")
            logger.info(f"Segment duration: {segment_duration:.1f}s")
            logger.info(f"Expected segments: ~{int(video_info['duration'] / segment_duration) + 1}")

            # Execute FFmpeg
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.CONVERSION_TIMEOUT
            )

            # Verify output
            if not playlist_path.exists():
                raise VideoProcessingError("Playlist file was not created")

            # Count generated segments
            segment_files = list(output_path.glob('segment_*.ts'))
            total_size = sum(f.stat().st_size for f in segment_files)

            logger.info(f"✅ Video segmentation completed successfully!")
            logger.info(f"Generated {len(segment_files)} segments")
            logger.info(f"Total size: {total_size / (1024**2):.1f} MB")
            logger.info(f"Playlist: {playlist_path}")

            # Validate segment sizes
            oversized_segments = [
                f for f in segment_files
                if f.stat().st_size > self.MAX_SEGMENT_SIZE
            ]

            if oversized_segments:
                logger.warning(
                    f"⚠️ {len(oversized_segments)} segments exceed recommended size "
                    f"({self.MAX_SEGMENT_SIZE / (1024**2):.1f} MB)"
                )
                for seg in oversized_segments[:3]:  # Log first 3
                    size_mb = seg.stat().st_size / (1024**2)
                    logger.warning(f"  {seg.name}: {size_mb:.1f} MB")

            return str(playlist_path)

        except subprocess.CalledProcessError as e:
            error_msg = f"FFmpeg segmentation failed: {e.stderr or 'Unknown error'}"
            logger.error(error_msg)

            # Try to provide more specific error information
            if "No space left on device" in str(e.stderr):
                error_msg += " (Insufficient disk space)"
            elif "Permission denied" in str(e.stderr):
                error_msg += " (Permission denied - check file/directory permissions)"
            elif "Invalid data found" in str(e.stderr):
                error_msg += " (Invalid or corrupted video file)"

            raise VideoProcessingError(error_msg) from e

        except subprocess.TimeoutExpired:
            error_msg = f"Video processing timed out after {self.CONVERSION_TIMEOUT} seconds"
            logger.error(error_msg)
            raise VideoProcessingError(error_msg)

        except Exception as e:
            error_msg = f"Unexpected error during video processing: {e}"
            logger.error(error_msg, exc_info=True)
            raise VideoProcessingError(error_msg) from e

    def _build_ffmpeg_command(
        self,
        video_path: str,
        playlist_path: str,
        segment_pattern: str,
        segment_duration: float,
        video_info: Dict[str, Any]
    ) -> List[str]:
        """
        Build optimized FFmpeg command for HLS segmentation.

        Args:
            video_path: Input video file path
            playlist_path: Output playlist file path
            segment_pattern: Pattern for segment filenames
            segment_duration: Duration of each segment
            video_info: Video analysis information

        Returns:
            List of command arguments for subprocess
        """
        cmd = ['ffmpeg', '-i', video_path]

        # --- MODIFICATION START ---
        # Force stream copy for both video and audio to avoid re-encoding.
        # This preserves original quality and is much faster.
        cmd.extend(['-c:v', 'copy'])
        cmd.extend(['-c:a', 'copy'])
        logger.debug("Forcing video and audio stream copy to prevent re-encoding.")
        # --- MODIFICATION END ---

        # HLS-specific options
        cmd.extend([
            '-f', 'hls',
            '-hls_time', str(segment_duration),
            '-hls_list_size', '0',  # Keep all segments in playlist
            '-hls_segment_filename', segment_pattern,
            '-hls_flags', 'independent_segments',  # Make segments independently decodable
            '-y',  # Overwrite output files
            playlist_path
        ])

        logger.debug(f"FFmpeg command: {' '.join(cmd)}")
        return cmd

    def validate_segments(self, segments_dir: str) -> Tuple[bool, List[str]]:
        """
        Validate generated video segments for common issues.

        Args:
            segments_dir: Directory containing the segments

        Returns:
            Tuple of (is_valid, list_of_issues)
        """
        segments_path = Path(segments_dir)
        issues = []

        if not segments_path.exists():
            return False, ["Segments directory does not exist"]

        # Check for playlist file
        playlist_file = segments_path / 'playlist.m3u8'
        if not playlist_file.exists():
            issues.append("Playlist file (playlist.m3u8) not found")

        # Find all segment files
        segment_files = sorted(segments_path.glob('segment_*.ts'))
        if not segment_files:
            issues.append("No segment files found")
            return False, issues

        # Validate each segment
        total_size = 0
        oversized_count = 0
        empty_count = 0

        for segment_file in segment_files:
            size = segment_file.stat().st_size
            total_size += size

            if size == 0:
                empty_count += 1
                issues.append(f"Empty segment: {segment_file.name}")
            elif size > self.MAX_SEGMENT_SIZE:
                oversized_count += 1

        # Summary issues
        if empty_count > 0:
            issues.append(f"{empty_count} empty segments found")

        if oversized_count > 0:
            issues.append(
                f"{oversized_count} segments exceed {self.MAX_SEGMENT_SIZE / (1024**2):.1f} MB limit"
            )

        # Check playlist content
        if playlist_file.exists():
            try:
                playlist_content = playlist_file.read_text()
                segment_references = [
                    line for line in playlist_content.split('\n')
                    if line.strip() and not line.startswith('#')
                ]

                if len(segment_references) != len(segment_files):
                    issues.append(
                        f"Playlist references {len(segment_references)} segments, "
                        f"but {len(segment_files)} files exist"
                    )

                # Validate playlist format
                if not playlist_content.startswith('#EXTM3U'):
                    issues.append("Invalid playlist format (missing #EXTM3U header)")

                if '#EXT-X-ENDLIST' not in playlist_content:
                    issues.append("Playlist missing end marker (#EXT-X-ENDLIST)")

            except Exception as e:
                issues.append(f"Error reading playlist file: {e}")

        # Log validation results
        if issues:
            logger.warning(f"Segment validation found {len(issues)} issues:")
            for issue in issues[:5]:  # Log first 5 issues
                logger.warning(f"  - {issue}")
            if len(issues) > 5:
                logger.warning(f"  ... and {len(issues) - 5} more issues")
        else:
            logger.info(f"✅ Segment validation passed: {len(segment_files)} segments, {total_size / (1024**2):.1f} MB total")

        return len(issues) == 0, issues

    def get_segment_info(self, segments_dir: str) -> Dict[str, Any]:
        """
        Get detailed information about generated segments.

        Args:
            segments_dir: Directory containing the segments

        Returns:
            Dictionary with segment information
        """
        segments_path = Path(segments_dir)

        if not segments_path.exists():
            return {'error': 'Segments directory does not exist'}

        try:
            segment_files = sorted(segments_path.glob('segment_*.ts'))
            playlist_file = segments_path / 'playlist.m3u8'

            segment_info = {
                'segments_dir': str(segments_path),
                'total_segments': len(segment_files),
                'playlist_exists': playlist_file.exists(),
                'segments': [],
                'total_size': 0,
                'total_duration': 0.0,
                'average_segment_size': 0,
                'largest_segment_size': 0,
                'smallest_segment_size': float('inf')
            }

            # Analyze each segment
            for i, segment_file in enumerate(segment_files):
                size = segment_file.stat().st_size
                segment_info['total_size'] += size

                # Update size statistics
                segment_info['largest_segment_size'] = max(segment_info['largest_segment_size'], size)
                segment_info['smallest_segment_size'] = min(segment_info['smallest_segment_size'], size)

                segment_data = {
                    'filename': segment_file.name,
                    'size': size,
                    'size_mb': size / (1024**2),
                    'index': i,
                    'duration': 0.0  # Will be filled from playlist if available
                }

                segment_info['segments'].append(segment_data)

            # Calculate averages
            if segment_files:
                segment_info['average_segment_size'] = segment_info['total_size'] / len(segment_files)
            else:
                segment_info['smallest_segment_size'] = 0

            # Extract duration information from playlist
            if playlist_file.exists():
                try:
                    playlist_content = playlist_file.read_text()
                    lines = playlist_content.split('\n')

                    segment_index = 0
                    for i, line in enumerate(lines):
                        if line.startswith('#EXTINF:'):
                            try:
                                duration_str = line.split(':')[1].split(',')[0]
                                duration = float(duration_str)

                                if segment_index < len(segment_info['segments']):
                                    segment_info['segments'][segment_index]['duration'] = duration
                                    segment_info['total_duration'] += duration
                                    segment_index += 1

                            except (IndexError, ValueError):
                                pass

                except Exception as e:
                    logger.warning(f"Error parsing playlist for duration info: {e}")

            segment_info['total_size_mb'] = segment_info['total_size'] / (1024**2)
            segment_info['total_duration_minutes'] = segment_info['total_duration'] / 60
            segment_info['average_segment_size_mb'] = segment_info['average_segment_size'] / (1024**2)

            return segment_info

        except Exception as e:
            logger.error(f"Error getting segment info: {e}", exc_info=True)
            return {'error': str(e)}

    def cleanup_segments(self, segments_dir: str) -> bool:
        """
        Clean up segment files and directory.

        Args:
            segments_dir: Directory to clean up

        Returns:
            True if cleanup successful, False otherwise
        """
        try:
            segments_path = Path(segments_dir)

            if not segments_path.exists():
                logger.debug(f"Segments directory already cleaned up: {segments_dir}")
                return True

            # Remove all files in the directory
            for file_path in segments_path.iterdir():
                if file_path.is_file():
                    file_path.unlink()
                    logger.debug(f"Removed file: {file_path}")

            # Remove the directory if empty
            try:
                segments_path.rmdir()
                logger.info(f"Cleaned up segments directory: {segments_dir}")
            except OSError:
                # Directory not empty, leave it
                logger.debug(f"Segments directory not empty, leaving: {segments_dir}")

            return True

        except Exception as e:
            logger.error(f"Error cleaning up segments: {e}", exc_info=True)
            return False


# Module-level convenience functions for backward compatibility

def split_video_to_hls(
    video_path: str,
    output_dir: str,
    max_chunk_size: int = VideoProcessor.TARGET_SEGMENT_SIZE
) -> str:
    """
    Convenience function for video splitting.

    Args:
        video_path: Path to the source video file
        output_dir: Directory where segments will be saved
        max_chunk_size: Maximum size for each segment in bytes

    Returns:
        Path to the generated .m3u8 playlist file

    Raises:
        VideoProcessingError: If video processing fails
    """
    processor = VideoProcessor()
    return processor.split_video_to_hls(video_path, output_dir, max_chunk_size)


def analyze_video(video_path: str) -> Dict[str, Any]:
    """
    Convenience function for video analysis.

    Args:
        video_path: Path to the video file to analyze

    Returns:
        Dictionary containing video information

    Raises:
        VideoProcessingError: If video analysis fails
    """
    processor = VideoProcessor()
    return processor.analyze_video(video_path)


def validate_segments(segments_dir: str) -> Tuple[bool, List[str]]:
    """
    Convenience function for segment validation.

    Args:
        segments_dir: Directory containing the segments

    Returns:
        Tuple of (is_valid, list_of_issues)
    """
    processor = VideoProcessor()
    return processor.validate_segments(segments_dir)
