"""
Video processing module with FFmpeg integration.
Handles video analysis, HLS conversion, and multi-track support.
"""

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
import tempfile
import uuid


@dataclass
class VideoStream:
    """Represents a video or audio stream."""
    index: int
    codec_name: str
    codec_type: str  # 'video', 'audio', 'subtitle'
    language: Optional[str] = None
    title: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass
class VideoMetadata:
    """Video file metadata."""
    duration: float
    format_name: str
    size: int
    bitrate: int
    video_streams: List[VideoStream] = field(default_factory=list)
    audio_streams: List[VideoStream] = field(default_factory=list)
    subtitle_streams: List[VideoStream] = field(default_factory=list)
    is_hls_compatible: bool = False
    copy_mode_eligible: bool = False


@dataclass
class ProcessingJob:
    """Represents a video processing job."""
    job_id: str
    input_path: Path
    output_dir: Path
    status: str = "pending"  # pending, processing, completed, error
    progress: float = 0.0
    error_message: Optional[str] = None
    metadata: Optional[VideoMetadata] = None
    created_segments: List[str] = field(default_factory=list)


class VideoProcessor:
    """Handles video processing and HLS conversion."""
    
    def __init__(self, config, db_manager, telegram_manager, cache_manager):
        self.config = config
        self.db_manager = db_manager
        self.telegram_manager = telegram_manager
        self.cache_manager = cache_manager
        self.logger = logging.getLogger(__name__)
        
        # Active processing jobs
        self.active_jobs: Dict[str, ProcessingJob] = {}
        
        # Check FFmpeg availability
        self._check_ffmpeg()
        
    def _check_ffmpeg(self):
        """Check if FFmpeg is available and working."""
        try:
            result = subprocess.run(
                [self.config.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                check=True
            )
            
            self.logger.info("FFmpeg check successful")
            
            # Check for hardware acceleration
            hw_accel_args = self.config.get_ffmpeg_hardware_accel_args()
            if hw_accel_args:
                self.logger.info(f"Hardware acceleration enabled: {hw_accel_args}")
            else:
                self.logger.info("Using software encoding")
                
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(f"FFmpeg not found or not working: {e}")
            
    async def analyze_video(self, video_path: Path) -> VideoMetadata:
        """Analyze video file and extract metadata."""
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")
            
        self.logger.info(f"Analyzing video: {video_path}")
        
        # Use ffprobe to get detailed metadata
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path)
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to analyze video: {e}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse ffprobe output: {e}")
            
        # Parse streams
        video_streams = []
        audio_streams = []
        subtitle_streams = []
        
        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            
            stream_obj = VideoStream(
                index=stream.get("index", 0),
                codec_name=stream.get("codec_name", "unknown"),
                codec_type=codec_type,
                language=stream.get("tags", {}).get("language"),
                title=stream.get("tags", {}).get("title"),
                duration=float(stream.get("duration", 0)) if stream.get("duration") else None,
                bitrate=int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else None
            )
            
            if codec_type == "video":
                stream_obj.width = stream.get("width")
                stream_obj.height = stream.get("height")
                video_streams.append(stream_obj)
                
            elif codec_type == "audio":
                stream_obj.sample_rate = stream.get("sample_rate")
                stream_obj.channels = stream.get("channels")
                audio_streams.append(stream_obj)
                
            elif codec_type == "subtitle":
                subtitle_streams.append(stream_obj)
                
        # Parse format info
        format_info = data.get("format", {})
        
        metadata = VideoMetadata(
            duration=float(format_info.get("duration", 0)),
            format_name=format_info.get("format_name", "unknown"),
            size=int(format_info.get("size", 0)),
            bitrate=int(format_info.get("bit_rate", 0)),
            video_streams=video_streams,
            audio_streams=audio_streams,
            subtitle_streams=subtitle_streams
        )
        
        # Check HLS compatibility and copy mode eligibility
        metadata.is_hls_compatible = self._check_hls_compatibility(metadata)
        metadata.copy_mode_eligible = self._check_copy_mode_eligibility(metadata)
        
        self.logger.info(f"Video analysis complete: {len(video_streams)} video, {len(audio_streams)} audio, {len(subtitle_streams)} subtitle streams")
        self.logger.info(f"HLS compatible: {metadata.is_hls_compatible}, Copy mode eligible: {metadata.copy_mode_eligible}")
        
        return metadata
        
    async def process_video(self, video_path: Path, video_title: Optional[str] = None) -> str:
        """Process a video file into HLS format."""
        job_id = str(uuid.uuid4())
        video_title = video_title or video_path.stem
        
        # Create output directory
        output_dir = self.config.segments_dir / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create processing job
        job = ProcessingJob(
            job_id=job_id,
            input_path=video_path,
            output_dir=output_dir,
            status="processing"
        )
        
        self.active_jobs[job_id] = job
        
        try:
            # Analyze video first
            job.metadata = await self.analyze_video(video_path)
            
            # Store video in database
            await self.db_manager.create_video(
                video_id=job_id,
                title=video_title,
                duration=job.metadata.duration,
                file_size=job.metadata.size,
                status="processing"
            )
            
            # Determine processing mode
            use_copy_mode = self._should_use_copy_mode(job.metadata)
            
            if use_copy_mode:
                self.logger.info(f"Using copy mode for lossless processing: {job_id}")
                # Process using copy mode (no re-encoding)
                await self._process_video_streams_copy(job)
                await self._process_audio_streams_copy(job)
            else:
                self.logger.info(f"Using transcoding mode for processing: {job_id}")
                # Process with re-encoding
                await self._process_video_streams(job)
                await self._process_audio_streams(job)
                
            await self._process_subtitle_streams(job)
            
            # Create master playlist
            await self._create_master_playlist(job)
            
            # Upload segments to Telegram
            await self._upload_segments_to_telegram(job)
            
            # Update job status
            job.status = "completed"
            job.progress = 100.0
            
            # Update database
            await self.db_manager.update_video_status(job_id, "completed")
            
            self.logger.info(f"Video processing completed: {job_id}")
            
        except Exception as e:
            job.status = "error"
            job.error_message = str(e)
            await self.db_manager.update_video_status(job_id, "error")
            self.logger.error(f"Video processing failed for {job_id}: {e}")
            raise
            
        return job_id
        
    async def _process_video_streams(self, job: ProcessingJob):
        """Process video streams into HLS segments."""
        if not job.metadata.video_streams:
            return
            
        video_stream = job.metadata.video_streams[0]  # Use first video stream
        video_dir = job.output_dir / "video"
        video_dir.mkdir(exist_ok=True)
        
        # Determine output resolution and bitrate
        quality_settings = self._get_video_quality_settings(video_stream)
        
        for quality_name, settings in quality_settings.items():
            quality_dir = video_dir / quality_name
            quality_dir.mkdir(exist_ok=True)
            
            playlist_path = quality_dir / "playlist.m3u8"
            
            cmd = self._build_ffmpeg_video_command(
                job.input_path,
                quality_dir,
                video_stream.index,
                settings
            )
            
            self.logger.info(f"Processing video quality {quality_name}: {settings}")
            
            await self._run_ffmpeg_command(cmd, job)
            
            # Store segments info and validate file sizes
            segments = list(quality_dir.glob("*.ts"))
            for segment in segments:
                # Check segment size
                if not await self._validate_segment_size(segment):
                    self.logger.warning(f"Segment {segment.name} exceeds Telegram file size limit")
                    # Could implement segment splitting here if needed
                job.created_segments.append(str(segment.relative_to(job.output_dir)))
                
    async def _process_video_streams_copy(self, job: ProcessingJob):
        """Process video streams using copy mode (no re-encoding)."""
        if not job.metadata.video_streams:
            return
            
        video_stream = job.metadata.video_streams[0]  # Use first video stream
        video_dir = job.output_dir / "video"
        video_dir.mkdir(exist_ok=True)
        
        # Create single quality level for copy mode
        quality_dir = video_dir / "original"
        quality_dir.mkdir(exist_ok=True)
        
        # Calculate segment duration based on estimated bitrate
        estimated_bitrate = job.metadata.bitrate // 1000 if job.metadata.bitrate > 0 else 3000  # kbps
        segment_duration = self._calculate_max_segment_duration(estimated_bitrate)
        
        cmd = [
            self.config.ffmpeg_path,
            "-i", str(job.input_path),
            "-map", f"0:v:{video_stream.index}",
            "-c:v", "copy",  # Copy video without re-encoding
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(quality_dir / "segment_%03d.ts"),
            str(quality_dir / "playlist.m3u8")
        ]
        
        self.logger.info(f"Processing video in copy mode with {segment_duration}s segments")
        
        await self._run_ffmpeg_command(cmd, job)
        
        # Store segments info and validate file sizes
        segments = list(quality_dir.glob("*.ts"))
        for segment in segments:
            # Check segment size
            if not await self._validate_segment_size(segment):
                self.logger.warning(f"Copy mode segment {segment.name} exceeds Telegram file size limit")
            job.created_segments.append(str(segment.relative_to(job.output_dir)))
            
    async def _process_audio_streams_copy(self, job: ProcessingJob):
        """Process audio streams using copy mode (no re-encoding)."""
        audio_dir = job.output_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        
        for i, audio_stream in enumerate(job.metadata.audio_streams):
            # Create language-specific directory
            lang = audio_stream.language or f"track_{i}"
            stream_dir = audio_dir / f"{lang}_{audio_stream.index}"
            stream_dir.mkdir(exist_ok=True)
            
            # Calculate segment duration for audio
            estimated_bitrate = audio_stream.bitrate // 1000 if audio_stream.bitrate else 128  # kbps
            segment_duration = self._calculate_max_segment_duration(estimated_bitrate)
            
            cmd = [
                self.config.ffmpeg_path,
                "-i", str(job.input_path),
                "-map", f"0:a:{audio_stream.index}",
                "-c:a", "copy",  # Copy audio without re-encoding
                "-f", "hls",
                "-hls_time", str(segment_duration),
                "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(stream_dir / "segment_%03d.ts"),
                str(stream_dir / "playlist.m3u8")
            ]
            
            self.logger.info(f"Processing audio stream {audio_stream.index} ({lang}) in copy mode")
            
            await self._run_ffmpeg_command(cmd, job)
            
            # Store segments info and validate file sizes
            segments = list(stream_dir.glob("*.ts"))
            for segment in segments:
                # Check segment size
                if not await self._validate_segment_size(segment):
                    self.logger.warning(f"Copy mode audio segment {segment.name} exceeds Telegram file size limit")
                job.created_segments.append(str(segment.relative_to(job.output_dir)))
                
    async def _process_audio_streams(self, job: ProcessingJob):
        """Process audio streams into HLS segments."""
        audio_dir = job.output_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        
        for i, audio_stream in enumerate(job.metadata.audio_streams):
            # Create language-specific directory
            lang = audio_stream.language or f"track_{i}"
            stream_dir = audio_dir / f"{lang}_{audio_stream.index}"
            stream_dir.mkdir(exist_ok=True)
            
            cmd = self._build_ffmpeg_audio_command(
                job.input_path,
                stream_dir,
                audio_stream.index
            )
            
            self.logger.info(f"Processing audio stream {audio_stream.index} ({lang})")
            
            await self._run_ffmpeg_command(cmd, job)
            
            # Store segments info and validate file sizes
            segments = list(stream_dir.glob("*.ts"))
            for segment in segments:
                # Check segment size
                if not await self._validate_segment_size(segment):
                    self.logger.warning(f"Audio segment {segment.name} exceeds Telegram file size limit")
                job.created_segments.append(str(segment.relative_to(job.output_dir)))
                
    async def _process_subtitle_streams(self, job: ProcessingJob):
        """Process subtitle streams."""
        if not job.metadata.subtitle_streams:
            return
            
        subtitle_dir = job.output_dir / "subtitles"
        subtitle_dir.mkdir(exist_ok=True)
        
        for i, subtitle_stream in enumerate(job.metadata.subtitle_streams):
            lang = subtitle_stream.language or f"track_{i}"
            subtitle_file = subtitle_dir / f"{lang}_{subtitle_stream.index}.vtt"
            
            cmd = [
                self.config.ffmpeg_path,
                "-i", str(job.input_path),
                "-map", f"0:s:{i}",
                "-c:s", "webvtt",
                "-y",
                str(subtitle_file)
            ]
            
            self.logger.info(f"Extracting subtitle stream {subtitle_stream.index} ({lang})")
            
            try:
                await self._run_ffmpeg_command(cmd, job)
                job.created_segments.append(str(subtitle_file.relative_to(job.output_dir)))
            except Exception as e:
                self.logger.warning(f"Failed to extract subtitle {subtitle_stream.index}: {e}")
                
    def _get_video_quality_settings(self, video_stream: VideoStream) -> Dict[str, Dict[str, Any]]:
        """Get video quality settings based on input resolution."""
        width = video_stream.width or 1920
        height = video_stream.height or 1080
        
        settings = {}
        
        # Always include original quality if reasonable size
        if width <= 1920 and height <= 1080:
            settings["720p"] = {
                "width": min(width, 1280),
                "height": min(height, 720),
                "bitrate": "2500k",
                "maxrate": "3000k",
                "bufsize": "5000k"
            }
            
        # Add lower quality for bandwidth saving
        if width > 640 or height > 480:
            settings["480p"] = {
                "width": 854,
                "height": 480,
                "bitrate": "1000k",
                "maxrate": "1200k", 
                "bufsize": "2000k"
            }
            
        # Always provide a low quality option
        settings["360p"] = {
            "width": 640,
            "height": 360,
            "bitrate": "600k",
            "maxrate": "750k",
            "bufsize": "1200k"
        }
        
        return settings
        
    def _build_ffmpeg_video_command(self, input_path: Path, output_dir: Path, 
                                   stream_index: int, quality: Dict[str, Any]) -> List[str]:
        """Build FFmpeg command for video processing."""
        cmd = [self.config.ffmpeg_path]
        
        # Hardware acceleration
        hw_accel_args = self.config.get_ffmpeg_hardware_accel_args()
        cmd.extend(hw_accel_args)
        
        # Input
        cmd.extend(["-i", str(input_path)])
        
        # Map video stream
        cmd.extend(["-map", f"0:v:{stream_index}"])
        
        # Calculate bitrate for segment duration calculation
        bitrate_kbps = int(quality["bitrate"].replace("k", ""))
        segment_duration = self._calculate_max_segment_duration(bitrate_kbps)
        
        # Video encoding settings
        cmd.extend([
            "-c:v", self._get_video_encoder(),
            "-b:v", quality["bitrate"],
            "-maxrate", quality["maxrate"],
            "-bufsize", quality["bufsize"],
            "-vf", f"scale={quality['width']}:{quality['height']}",
            "-threads", str(self.config.ffmpeg_threads),
            "-preset", "fast",
            "-g", "30",  # Keyframe interval
        ])
        
        # HLS settings with calculated segment duration
        cmd.extend([
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(output_dir / "segment_%03d.ts"),
            str(output_dir / "playlist.m3u8")
        ])
        
        return cmd
        
    def _build_ffmpeg_audio_command(self, input_path: Path, output_dir: Path, 
                                   stream_index: int) -> List[str]:
        """Build FFmpeg command for audio processing."""
        # Calculate segment duration for audio (128kbps)
        audio_bitrate_kbps = 128
        segment_duration = self._calculate_max_segment_duration(audio_bitrate_kbps)
        
        cmd = [
            self.config.ffmpeg_path,
            "-i", str(input_path),
            "-map", f"0:a:{stream_index}",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ac", "2",  # Stereo
            "-f", "hls",
            "-hls_time", str(segment_duration),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(output_dir / "segment_%03d.ts"),
            str(output_dir / "playlist.m3u8")
        ]
        
        return cmd
        
    def _get_video_encoder(self) -> str:
        """Get the appropriate video encoder based on hardware acceleration."""
        hw_accel = self.config.ffmpeg_hardware_accel
        
        if hw_accel == "nvenc":
            return "h264_nvenc"
        elif hw_accel in ["vaapi", "qsv"]:
            return "h264_vaapi"
        elif hw_accel == "videotoolbox":
            return "h264_videotoolbox"
        else:
            return "libx264"
            
    def _calculate_max_segment_duration(self, bitrate_kbps: int) -> int:
        """Calculate maximum segment duration to stay under Telegram file size limit."""
        # Get Telegram limits
        max_file_size = self.config.telegram_max_file_size
        buffer_size = getattr(self.config, 'telegram_file_size_buffer', 1024 * 1024)  # 1MB buffer
        
        # Calculate usable size (with buffer for metadata)
        usable_size = max_file_size - buffer_size
        
        # Convert bitrate to bytes per second
        bytes_per_second = (bitrate_kbps * 1000) / 8
        
        # Calculate max duration in seconds
        max_duration = int(usable_size / bytes_per_second)
        
        # Apply constraints from config
        max_duration = max(self.config.min_segment_duration, max_duration)
        max_duration = min(self.config.max_segment_duration, max_duration)
        
        self.logger.debug(f"Calculated max segment duration: {max_duration}s for bitrate {bitrate_kbps}kbps")
        
        return max_duration
        
    def _check_hls_compatibility(self, metadata: VideoMetadata) -> bool:
        """Check if video codecs are HLS-compatible."""
        hls_compatible = True
        
        # Check video streams
        for stream in metadata.video_streams:
            if stream.codec_name not in ["h264", "hevc", "h265"]:
                self.logger.debug(f"Video codec {stream.codec_name} not HLS-compatible")
                hls_compatible = False
                break
                
        # Check audio streams  
        for stream in metadata.audio_streams:
            if stream.codec_name not in ["aac", "mp3", "ac3", "eac3"]:
                self.logger.debug(f"Audio codec {stream.codec_name} not HLS-compatible")
                hls_compatible = False
                break
                
        return hls_compatible
        
    def _check_copy_mode_eligibility(self, metadata: VideoMetadata) -> bool:
        """Check if video is eligible for copy mode processing."""
        if not self.config.enable_copy_mode:
            return False
            
        # Must be HLS compatible
        if not metadata.is_hls_compatible:
            return False
            
        # File size check
        if metadata.size > self.config.copy_mode_threshold:
            self.logger.debug(f"File size {metadata.size} exceeds copy mode threshold {self.config.copy_mode_threshold}")
            return True
            
        return False
        
    def _should_use_copy_mode(self, metadata: VideoMetadata) -> bool:
        """Determine if we should use copy mode for this video."""
        return metadata.copy_mode_eligible and metadata.is_hls_compatible
        
    async def _validate_segment_size(self, segment_path: Path) -> bool:
        """Validate that a segment file doesn't exceed Telegram's file size limit."""
        try:
            file_size = segment_path.stat().st_size
            max_size = self.config.telegram_max_file_size
            
            if file_size > max_size:
                self.logger.error(
                    f"Segment {segment_path.name} is {file_size} bytes "
                    f"(exceeds {max_size} byte limit by {file_size - max_size} bytes)"
                )
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to validate segment size for {segment_path}: {e}")
            return False
            
    async def _run_ffmpeg_command(self, cmd: List[str], job: ProcessingJob):
        """Run FFmpeg command with progress tracking."""
        self.logger.debug(f"Running FFmpeg: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown FFmpeg error"
            self.logger.error(f"FFmpeg failed: {error_msg}")
            raise RuntimeError(f"FFmpeg processing failed: {error_msg}")
            
        job.progress += 10  # Simple progress tracking
        
    async def _create_master_playlist(self, job: ProcessingJob):
        """Create master HLS playlist."""
        master_playlist = job.output_dir / "master.m3u8"
        
        lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
        
        # Add video variants
        video_dir = job.output_dir / "video"
        if video_dir.exists():
            for quality_dir in video_dir.iterdir():
                if quality_dir.is_dir():
                    playlist_path = quality_dir / "playlist.m3u8"
                    if playlist_path.exists():
                        # Extract quality info (simplified)
                        quality_name = quality_dir.name
                        bandwidth = self._estimate_bandwidth(quality_name)
                        
                        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION=1280x720")
                        lines.append(f"video/{quality_name}/playlist.m3u8")
                        
        # Add audio tracks
        audio_dir = job.output_dir / "audio"
        if audio_dir.exists():
            for i, audio_stream_dir in enumerate(audio_dir.iterdir()):
                if audio_stream_dir.is_dir():
                    playlist_path = audio_stream_dir / "playlist.m3u8"
                    if playlist_path.exists():
                        lang = audio_stream_dir.name.split('_')[0]
                        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{lang}",LANGUAGE="{lang}",URI="audio/{audio_stream_dir.name}/playlist.m3u8"')
                        
        with open(master_playlist, 'w') as f:
            f.write('\n'.join(lines))
            
        job.created_segments.append("master.m3u8")
        
    def _estimate_bandwidth(self, quality_name: str) -> int:
        """Estimate bandwidth for quality level."""
        bandwidth_map = {
            "360p": 800000,
            "480p": 1400000,
            "720p": 3000000,
            "1080p": 6000000
        }
        return bandwidth_map.get(quality_name, 2000000)
        
    async def _upload_segments_to_telegram(self, job: ProcessingJob):
        """Upload all generated segments to Telegram."""
        self.logger.info(f"Uploading {len(job.created_segments)} segments to Telegram")
        
        upload_tasks = []
        for segment_path in job.created_segments:
            full_path = job.output_dir / segment_path
            if full_path.exists():
                task = self.telegram_manager.upload_segment(full_path, segment_path)
                upload_tasks.append(task)
                
        # Upload in batches to avoid overwhelming Telegram
        batch_size = 5
        for i in range(0, len(upload_tasks), batch_size):
            batch = upload_tasks[i:i + batch_size]
            results = await asyncio.gather(*batch, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"Upload failed: {result}")
                elif not result.success:
                    self.logger.error(f"Upload failed: {result.error}")
                    
        self.logger.info("Segment upload completed")
        
    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of a processing job."""
        job = self.active_jobs.get(job_id)
        if not job:
            return None
            
        return {
            "job_id": job_id,
            "status": job.status,
            "progress": job.progress,
            "error_message": job.error_message,
            "segments_count": len(job.created_segments),
            "metadata": {
                "duration": job.metadata.duration if job.metadata else None,
                "video_streams": len(job.metadata.video_streams) if job.metadata else 0,
                "audio_streams": len(job.metadata.audio_streams) if job.metadata else 0,
                "subtitle_streams": len(job.metadata.subtitle_streams) if job.metadata else 0,
                "is_hls_compatible": job.metadata.is_hls_compatible if job.metadata else False,
                "copy_mode_eligible": job.metadata.copy_mode_eligible if job.metadata else False,
                "used_copy_mode": self._should_use_copy_mode(job.metadata) if job.metadata else False
            } if job.metadata else None
        }
        
    async def cleanup_job(self, job_id: str):
        """Clean up processing job files."""
        job = self.active_jobs.get(job_id)
        if job and job.output_dir.exists():
            shutil.rmtree(job.output_dir)
            
        if job_id in self.active_jobs:
            del self.active_jobs[job_id]