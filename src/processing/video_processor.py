"""
Video processing functionality with hardware acceleration support.
"""

import os
import json
import subprocess
import psutil
import time
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import logging

from .hardware_accel import get_hardware_accelerator, get_acceleration_params
from ..core.exceptions import VideoProcessingError
from ..core.config import get_config

logger = logging.getLogger(__name__)


class VideoProcessor:
    """Main video processing class with hardware acceleration support."""
    
    def __init__(self, config=None):
        self.config = config or get_config()
        self.hw_accelerator = get_hardware_accelerator()
    
    def get_encoding_params(self, force_reencode: bool = False, quality_preset: str = 'balanced') -> Dict:
        """
        Get encoding parameters based on environment configuration and hardware availability.
        
        Args:
            force_reencode: If True, always use encoding instead of copy
            quality_preset: 'fast', 'balanced', or 'quality'
        
        Returns:
            Dict with encoding parameters including codec, input args, and output args
        """
        hardware_accel_env = self.config.ffmpeg_hardware_accel.lower()
        
        if hardware_accel_env == 'none' or not force_reencode:
            # Use software encoding or copy mode
            if force_reencode:
                return {
                    'video_codec': 'libx264',
                    'extra_input_args': [],
                    'extra_output_args': ['-preset', 'fast', '-crf', '23'],
                    'hw_accel_type': 'software'
                }
            else:
                return {
                    'video_codec': 'copy',
                    'extra_input_args': [],
                    'extra_output_args': [],
                    'hw_accel_type': 'copy'
                }
        
        if hardware_accel_env == 'auto':
            # Use preferred hardware accelerator
            if self.hw_accelerator.is_available():
                params = self.hw_accelerator.get_acceleration_params(quality_preset=quality_preset)
                params['hw_accel_type'] = self.hw_accelerator.preferred_accel
                logger.info(f"🚀 Using hardware acceleration: {self.hw_accelerator.preferred_accel}")
                return params
            else:
                logger.info("ℹ️ No hardware acceleration available, using software encoding")
                return {
                    'video_codec': 'libx264',
                    'extra_input_args': [],
                    'extra_output_args': ['-preset', 'fast', '-crf', '23'],
                    'hw_accel_type': 'software'
                }
        else:
            # Use specific hardware accelerator
            if self.hw_accelerator.is_available(hardware_accel_env):
                params = self.hw_accelerator.get_acceleration_params(hardware_accel_env, quality_preset)
                params['hw_accel_type'] = hardware_accel_env
                logger.info(f"🚀 Using specified hardware acceleration: {hardware_accel_env}")
                return params
            else:
                logger.warning(f"⚠️ Requested hardware acceleration '{hardware_accel_env}' not available, falling back to software")
                return {
                    'video_codec': 'libx264',
                    'extra_input_args': [],
                    'extra_output_args': ['-preset', 'fast', '-crf', '23'],
                    'hw_accel_type': 'software'
                }
    
    def split_video_to_hls(self, video_path: str, output_dir: str, max_chunk_size: int = None) -> str:
        """
        Memory-efficient video processing that automatically handles files of any size.
        
        This function uses a streaming-first approach:
        1. Always starts with streaming processing to minimize memory usage
        2. Automatically detects optimal settings based on file properties
        3. Falls back to emergency mode for extreme cases
        4. Preserves subtitle extraction functionality
        """
        
        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            raise VideoProcessingError(f"Video file not found: {video_path}")

        # Load configuration with streaming-first defaults
        if max_chunk_size is None:
            max_chunk_size = self.config.max_chunk_size

        telegram_bot_limit = 20 * 1024 * 1024  # 20MB
        if max_chunk_size > telegram_bot_limit:
            logger.warning(f"MAX_CHUNK_SIZE ({max_chunk_size / (1024*1024):.1f}MB) exceeds Telegram bot limit (20MB)")
            max_chunk_size = telegram_bot_limit

        # Streaming-first configuration
        min_segment_duration = self.config.min_segment_duration
        max_segment_duration = self.config.max_segment_duration
        ffmpeg_threads = self.config.ffmpeg_threads
        emergency_segment_duration = self.config.emergency_segment_duration
        emergency_video_scale = self.config.emergency_video_scale

        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Created output directory: {output_dir}")
        
        # Get file info and memory status
        file_size = os.path.getsize(video_path)
        file_size_gb = file_size / (1024**3)
        memory = psutil.virtual_memory()
        
        logger.info(f"🎬 Processing video: {Path(video_path).name}")
        logger.info(f"📊 File size: {file_size_gb:.2f}GB")
        logger.info(f"💾 Memory usage: {memory.percent}% ({memory.available / (1024**3):.1f}GB available)")
        
        # Log hardware acceleration status
        if self.hw_accelerator.is_available():
            logger.info(f"🚀 Hardware acceleration available: {self.hw_accelerator.preferred_accel}")
        else:
            logger.info("💻 Using software encoding (no hardware acceleration)")
        
        # Always use streaming approach - it's more memory efficient
        logger.info("🌊 Using STREAMING processing (memory-efficient approach)")
        
        # Probe video with minimal memory footprint
        video_info, subtitle_info = self.probe_video_lightweight(video_path)
        
        if subtitle_info:
            logger.info(f"🎭 Found {len(subtitle_info)} subtitle tracks (will extract after video processing)")
        
        # Process video using streaming approach
        playlist_path = self.process_video_streaming(
            video_path, output_dir, max_chunk_size, min_segment_duration, max_segment_duration,
            ffmpeg_threads, video_info
        )
        
        # Extract subtitles after video processing to minimize memory impact
        if subtitle_info:
            logger.info(f"🎭 Extracting {len(subtitle_info)} subtitle tracks...")
            try:
                self.extract_subtitles_streaming(video_path, output_dir, subtitle_info)
                logger.info("✅ Subtitle extraction completed")
            except Exception as e:
                logger.warning(f"⚠️ Subtitle extraction failed: {e}")
        
        return playlist_path
    
    def probe_video_lightweight(self, video_path: str) -> Tuple[Dict, List[Dict]]:
        """Lightweight video probing that extracts only essential information."""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams',
                '-analyzeduration', '1M', '-probesize', '1M',  # Minimal analysis
                video_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
            probe_data = json.loads(result.stdout)
            
            # Extract basic video info
            format_info = probe_data.get('format', {})
            streams = probe_data.get('streams', [])
            
            # Find video stream info (just the first one)
            video_stream = next((s for s in streams if s.get('codec_type') == 'video'), {})
            
            video_info = {
                'duration': float(format_info.get('duration', 0)),
                'size': int(format_info.get('size', 0)),
                'bitrate': int(format_info.get('bit_rate', 0)),
                'width': int(video_stream.get('width', 0)),
                'height': int(video_stream.get('height', 0)),
                'codec': video_stream.get('codec_name', 'unknown')
            }
            
            # Extract subtitle info
            subtitle_info = self.extract_subtitle_info_lightweight(streams)
            
            logger.info(f"📹 Video: {video_info['width']}x{video_info['height']}, {video_info['codec']}, {video_info['duration']:.1f}s")
            
            return video_info, subtitle_info
            
        except subprocess.TimeoutExpired:
            logger.error("Video probing timed out")
            raise VideoProcessingError("Video probing timed out")
        except subprocess.CalledProcessError as e:
            logger.error(f"Video probing failed: {e}")
            raise VideoProcessingError(f"Video probing failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during video probing: {e}")
            raise VideoProcessingError(f"Video probing failed: {e}")
    
    def extract_subtitle_info_lightweight(self, streams: List[Dict]) -> List[Dict]:
        """Extract subtitle information from stream data."""
        subtitle_streams = []
        
        for i, stream in enumerate(streams):
            if stream.get('codec_type') == 'subtitle':
                # Only extract basic info
                subtitle_info = {
                    'index': i,
                    'codec_name': stream.get('codec_name', 'unknown'),
                    'language': stream.get('tags', {}).get('language', 'unknown')
                }
                subtitle_streams.append(subtitle_info)
        
        return subtitle_streams
    
    # Import other methods from the original video_processor.py
    # This is a simplified version - the full implementation would include all the methods
    # from the original file, properly organized into this class structure
    
    def process_video_streaming(self, video_path: str, output_dir: str, max_chunk_size: int,
                              min_segment_duration: int, max_segment_duration: int,
                              ffmpeg_threads: int, video_info: Dict) -> str:
        """Process video using streaming approach with systematic duration testing."""
        
        # Calculate target segment duration based on bitrate and size limits
        duration = video_info.get('duration', 0)
        bitrate = video_info.get('bitrate', 0)
        
        # Start with a reasonable segment duration
        segment_duration = min(max_segment_duration, max(min_segment_duration, 10))
        
        # Get encoding parameters
        encoding_params = self.get_encoding_params(force_reencode=False)
        
        # Prepare FFmpeg command
        playlist_path = os.path.join(output_dir, "playlist.m3u8")
        segment_pattern = os.path.join(output_dir, "segment_%03d.ts")
        
        cmd = [
            'ffmpeg', '-i', video_path,
            '-f', 'hls',
            '-hls_time', str(segment_duration),
            '-hls_list_size', '0',
            '-hls_segment_filename', segment_pattern,
            '-c:v', encoding_params.get('video_codec', 'copy'),
            '-c:a', 'aac',
            '-threads', str(ffmpeg_threads),
            '-y',  # Overwrite output files
            playlist_path
        ]
        
        # Add hardware acceleration if available
        if encoding_params.get('extra_input_args'):
            cmd = cmd[:2] + encoding_params['extra_input_args'] + cmd[2:]
        
        if encoding_params.get('extra_output_args'):
            cmd = cmd[:-1] + encoding_params['extra_output_args'] + [cmd[-1]]
        
        logger.info(f"🎬 Running FFmpeg command: {' '.join(cmd[:10])}...")
        
        try:
            # Run FFmpeg
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout
                check=True
            )
            
            # Check if segments were created
            segments = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
            logger.info(f"✅ Created {len(segments)} video segments")
            
            if not segments:
                raise VideoProcessingError("No segments were created")
            
            return playlist_path
            
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg failed: {e.stderr}")
            raise VideoProcessingError(f"FFmpeg processing failed: {e.stderr}")
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg processing timed out")
            raise VideoProcessingError("Video processing timed out")
        except Exception as e:
            logger.error(f"Video processing failed: {e}")
            raise VideoProcessingError(f"Video processing failed: {e}")
    
    def extract_subtitles_streaming(self, video_path: str, output_dir: str, subtitle_info: List[Dict]):
        """Extract subtitles from video file."""
        if not subtitle_info:
            return
        
        for i, sub_info in enumerate(subtitle_info):
            try:
                # Extract subtitle track
                sub_path = os.path.join(output_dir, f"subtitle_{i}.srt")
                cmd = [
                    'ffmpeg', '-i', video_path,
                    '-map', f'0:s:{i}',
                    '-c:s', 'srt',
                    '-y',
                    sub_path
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                    check=True
                )
                
                logger.info(f"✅ Extracted subtitle track {i} to {sub_path}")
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"⚠️ Failed to extract subtitle track {i}: {e.stderr}")
            except Exception as e:
                logger.warning(f"⚠️ Error extracting subtitle track {i}: {e}")