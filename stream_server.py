"""
HTTP Streaming Server for Telegram Video Streaming System.

This module provides an aiohttp-based web server that handles:
- Web UI for video uploads
- Background video processing with real-time progress updates
- HLS playlist and segment serving
- RESTful API endpoints for video management

The server supports both local network and public internet access
through configurable domain settings.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any
import json

import aiofiles
import aiohttp_jinja2
import jinja2
from aiohttp import web, WSMsgType
from aiohttp.web_request import Request
from aiohttp.web_response import Response, StreamResponse

from config import AppConfig
from database import DatabaseManager, VideoInfo
from logger_config import get_logger
from telegram_handler import TelegramHandler
from video_processor import split_video_to_hls

logger = get_logger(__name__)

# Type aliases for better code readability
TaskId = str
TaskStatus = Dict[str, Any]

# Global task status storage
# In production, consider using Redis or database storage
_task_status: Dict[TaskId, List[str]] = {}


class TaskStatusManager:
    """
    Manages background task status and logging for real-time updates.

    This class provides thread-safe status tracking for video processing
    tasks, enabling real-time progress updates via Server-Sent Events.
    """

    def __init__(self):
        self._status: Dict[TaskId, List[str]] = {}
        self._lock = asyncio.Lock()

    async def add_status(self, task_id: TaskId, message: str, level: str = 'INFO') -> None:
        """
        Add a status message for a specific task.

        Args:
            task_id: Unique identifier for the task
            message: Status message to add
            level: Log level (INFO, SUCCESS, ERROR, WARNING)
        """
        formatted_message = f"[{level}] {message}"

        async with self._lock:
            if task_id not in self._status:
                self._status[task_id] = []
            self._status[task_id].append(formatted_message)

        # Also log to the main logger
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(f"Task {task_id[:8]}: {message}")

    async def get_status(self, task_id: TaskId, from_index: int = 0) -> List[str]:
        """
        Get status messages for a task starting from a specific index.

        Args:
            task_id: Task identifier
            from_index: Starting index for messages

        Returns:
            List of status messages from the specified index
        """
        async with self._lock:
            messages = self._status.get(task_id, [])
            return messages[from_index:]

    async def mark_completed(self, task_id: TaskId) -> None:
        """Mark a task as completed with a special end marker."""
        await self.add_status(task_id, "---STREAM_END---", "SYSTEM")

    async def cleanup_task(self, task_id: TaskId) -> None:
        """Remove task status from memory after completion."""
        async with self._lock:
            self._status.pop(task_id, None)
        logger.debug(f"Cleaned up task status for {task_id[:8]}")


class StreamServer:
    """
    HTTP server for streaming video content stored on Telegram.

    This server provides a modern web interface for video uploads and
    serves HLS content for streaming to media players like Jellyfin, Plex,
    VLC, and web browsers.
    """

    def __init__(
        self,
        host: str,
        port: int,
        db_manager: DatabaseManager,
        config: AppConfig
    ):
        """
        Initialize the streaming server.

        Args:
            host: Host address to bind the server to
            port: Port number for the server
            db_manager: Database manager instance
            config: Application configuration
        """
        self.host = host
        self.port = port
        self.db = db_manager
        self.config = config
        self.task_manager = TaskStatusManager()

        # Ensure required directories exist
        self._ensure_directories()

        logger.info(f"StreamServer initialized - {host}:{port}")
        logger.info(f"Local URL: {config.get_local_url()}")
        if config.public_domain:
            logger.info(f"Public URL: {config.get_public_url()}")

    def _ensure_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        directories = [
            self.config.temp_upload_dir,
            self.config.segments_dir,
            self.config.playlists_dir,
        ]

        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)

        logger.debug("Ensured all required directories exist")

    async def _process_video_task(self, task_id: TaskId, video_path: Path) -> None:
        """
        Background task for processing uploaded videos.

        This method handles the complete video processing pipeline:
        1. Split video into HLS segments
        2. Upload segments to Telegram
        3. Create streaming playlist
        4. Clean up temporary files

        Args:
            task_id: Unique identifier for this processing task
            video_path: Path to the uploaded video file
        """
        async def log_status(message: str, level: str = 'INFO') -> None:
            await self.task_manager.add_status(task_id, message, level)

        try:
            await log_status("Initializing video processing...")

            # Generate unique video ID
            video_id = str(uuid.uuid4())
            segments_dir = Path(self.config.segments_dir) / video_id

            await log_status(f"Processing video: {video_path.name}")
            await log_status(f"Video ID: {video_id}")

            # Step 1: Split video into HLS segments
            await log_status("Splitting video into HLS segments...")
            try:
                playlist_path = split_video_to_hls(
                    str(video_path),
                    str(segments_dir),
                    self.config.max_chunk_size
                )
                await log_status("✅ Video splitting completed", "SUCCESS")
            except Exception as e:
                await log_status(f"❌ Video splitting failed: {e}", "ERROR")
                return

            # Step 2: Upload segments to Telegram
            await log_status("Uploading segments to Telegram...")
            telegram_handler = TelegramHandler(self.config, self.db)

            try:
                success = await telegram_handler.upload_segments_to_telegram(
                    str(segments_dir),
                    video_id,
                    video_path.name
                )

                if not success:
                    await log_status("❌ Failed to upload segments to Telegram", "ERROR")
                    return

                await log_status("✅ All segments uploaded successfully", "SUCCESS")

            except Exception as e:
                await log_status(f"❌ Upload error: {e}", "ERROR")
                return

            # Step 3: Create streaming playlist
            await log_status("Creating streaming playlist...")
            try:
                playlist_output_path = Path(self.config.playlists_dir) / f"{video_id}.m3u8"
                await self._create_streaming_playlist(video_id, playlist_output_path)

                # Generate URLs
                local_url = self.config.get_playlist_url(video_id, public=False)
                public_url = self.config.get_playlist_url(video_id, public=True)

                await log_status("✅ Streaming playlist created", "SUCCESS")
                await log_status(f"🎬 Local URL: <a href='{local_url}' target='_blank'>{local_url}</a>", "RESULT")

                if self.config.public_domain:
                    await log_status(f"🌐 Public URL: <a href='{public_url}' target='_blank'>{public_url}</a>", "RESULT")

                await log_status("🎉 Video processing completed successfully!", "SUCCESS")

            except Exception as e:
                await log_status(f"❌ Playlist creation failed: {e}", "ERROR")
                return

        except Exception as e:
            await log_status(f"❌ Unexpected error: {e}", "ERROR")
            logger.error(f"Video processing task {task_id} failed", exc_info=True)

        finally:
            # Step 4: Cleanup
            await log_status("Cleaning up temporary files...")
            try:
                if video_path.exists():
                    video_path.unlink()
                    await log_status("✅ Temporary files cleaned up", "SUCCESS")
            except Exception as e:
                await log_status(f"⚠️ Cleanup warning: {e}", "WARNING")

            # Mark task as completed
            await self.task_manager.mark_completed(task_id)

            # Schedule cleanup of task status (after 5 minutes)
            asyncio.create_task(self._delayed_cleanup(task_id, delay=300))

    async def _delayed_cleanup(self, task_id: TaskId, delay: int) -> None:
        """
        Clean up task status after a delay.

        Args:
            task_id: Task to clean up
            delay: Delay in seconds before cleanup
        """
        await asyncio.sleep(delay)
        await self.task_manager.cleanup_task(task_id)

    async def _create_streaming_playlist(self, video_id: str, output_path: Path) -> None:
        """
        Create an HLS playlist file with network-accessible URLs.

        Args:
            video_id: The video identifier
            output_path: Path where the playlist should be saved

        Raises:
            ValueError: If no segments are found for the video
            IOError: If playlist file cannot be written
        """
        logger.debug(f"Creating playlist for video {video_id}")

        segments = await self.db.get_video_segments(video_id)
        if not segments:
            raise ValueError(f"No segments found for video {video_id}")

        # Use public URL for playlist generation if available
        base_url = self.config.get_public_url()

        # Calculate target duration (max segment duration + buffer)
        target_duration = max(
            (segment.duration for segment in segments.values()),
            default=10.0
        )
        target_duration = int(target_duration) + 1

        # Build playlist content
        playlist_lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0"
        ]

        # Sort segments by their order
        sorted_segments = sorted(
            segments.values(),
            key=lambda s: s.segment_order
        )

        # Add segment entries
        for segment in sorted_segments:
            playlist_lines.extend([
                f"#EXTINF:{segment.duration:.6f},",
                f"{base_url}/segment/{video_id}/{segment.filename}"
            ])

        playlist_lines.append("#EXT-X-ENDLIST")

        # Write playlist file
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
                await f.write('\n'.join(playlist_lines))

            logger.info(f"Created streaming playlist: {output_path}")

        except Exception as e:
            logger.error(f"Failed to write playlist file {output_path}: {e}")
            raise

    # ===== HTTP Request Handlers =====

    @aiohttp_jinja2.template('index.html')
    async def handle_index(self, request: Request) -> Dict[str, Any]:
        """
        Serve the main web interface.

        Args:
            request: The HTTP request

        Returns:
            Template context dictionary
        """
        return {
            "app_name": "Telegram Video Streaming",
            "version": "2.0.0",
            "local_url": self.config.get_local_url(),
            "public_url": self.config.get_public_url() if self.config.public_domain else None,
            "max_upload_size": self.config.max_upload_size,
            "telegram_configured": bool(self.config.bot_token and self.config.chat_id)
        }

    async def handle_upload(self, request: Request) -> Response:
        """
        Handle video file upload and start background processing.

        Args:
            request: The HTTP request containing the video file

        Returns:
            JSON response with task_id or error message
        """
        try:
            # Parse multipart form data
            reader = await request.multipart()
            video_field = await reader.next()

            if not video_field or video_field.name != 'video_file':
                return web.json_response(
                    {'success': False, 'error': 'Video file is required'},
                    status=400
                )

            # Validate filename
            filename = video_field.filename
            if not filename:
                return web.json_response(
                    {'success': False, 'error': 'Invalid filename'},
                    status=400
                )

            # Check file size (rough estimate)
            content_length = request.headers.get('Content-Length')
            if content_length and int(content_length) > self.config.max_upload_size:
                return web.json_response(
                    {'success': False, 'error': 'File too large'},
                    status=413
                )

            # Save uploaded file
            task_id = str(uuid.uuid4())
            safe_filename = f"{task_id}-{filename}"
            video_path = Path(self.config.temp_upload_dir) / safe_filename

            # Stream file to disk
            async with aiofiles.open(video_path, 'wb') as f:
                while True:
                    chunk = await video_field.read_chunk()
                    if not chunk:
                        break
                    await f.write(chunk)

            file_size = video_path.stat().st_size
            logger.info(f"Received upload: {filename} ({file_size / (1024**2):.2f} MB)")

            # Start background processing
            asyncio.create_task(self._process_video_task(task_id, video_path))

            return web.json_response({
                'success': True,
                'task_id': task_id,
                'filename': filename,
                'size': file_size
            })

        except Exception as e:
            logger.error(f"Upload handler error: {e}", exc_info=True)
            return web.json_response(
                {'success': False, 'error': 'Upload failed'},
                status=500
            )

    async def handle_task_status(self, request: Request) -> StreamResponse:
        """
        Handle Server-Sent Events for real-time task status updates.

        Args:
            request: The HTTP request containing task_id

        Returns:
            Streaming response with status updates
        """
        task_id = request.match_info['task_id']

        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*'
            }
        )
        await response.prepare(request)

        last_index = 0

        try:
            logger.debug(f"Starting status stream for task {task_id[:8]}")

            while True:
                # Get new status messages
                messages = await self.task_manager.get_status(task_id, last_index)

                # Send new messages
                for message in messages:
                    await response.write(f"data: {message}\n\n".encode('utf-8'))

                    # Check for end marker
                    if "---STREAM_END---" in message:
                        await response.write(b"data: ---CLOSE---\n\n")
                        logger.debug(f"Status stream completed for task {task_id[:8]}")
                        return response

                last_index += len(messages)
                await asyncio.sleep(1)  # Poll interval

        except asyncio.CancelledError:
            logger.debug(f"Status stream cancelled for task {task_id[:8]}")
        except Exception as e:
            logger.error(f"Status stream error for task {task_id[:8]}: {e}")

        return response

    async def handle_playlist(self, request: Request) -> Response:
        """
        Serve HLS playlist files.

        Args:
            request: HTTP request containing video_id

        Returns:
            Playlist file response or 404
        """
        video_id = request.match_info['video_id']
        playlist_path = Path(self.config.playlists_dir) / f"{video_id}.m3u8"

        if not playlist_path.exists():
            logger.warning(f"Playlist not found: {video_id}")
            return web.Response(status=404, text="Playlist not found")

        try:
            return web.FileResponse(
                playlist_path,
                headers={
                    'Content-Type': 'application/vnd.apple.mpegurl',
                    'Cache-Control': 'no-cache'
                }
            )
        except Exception as e:
            logger.error(f"Error serving playlist {video_id}: {e}")
            return web.Response(status=500, text="Internal server error")

    async def handle_segment(self, request: Request) -> Response:
        """
        Serve video segments by downloading from Telegram.

        Args:
            request: HTTP request containing video_id and segment_name

        Returns:
            Video segment response or error
        """
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']

        try:
            # Get segment info from database
            segments = await self.db.get_video_segments(video_id)
            segment_info = segments.get(segment_name)

            if not segment_info:
                logger.warning(f"Segment not found: {video_id}/{segment_name}")
                return web.Response(status=404, text="Segment not found")

            # Download segment from Telegram
            telegram_handler = TelegramHandler(self.config, self.db)
            segment_bytes = await telegram_handler.download_segment_from_telegram(
                segment_info.file_id
            )

            if not segment_bytes:
                logger.error(f"Failed to download segment: {video_id}/{segment_name}")
                return web.Response(status=500, text="Failed to retrieve segment")

            logger.debug(f"Served segment: {video_id}/{segment_name} ({len(segment_bytes)} bytes)")

            return web.Response(
                body=segment_bytes,
                content_type='video/mp2t',
                headers={
                    'Cache-Control': 'public, max-age=3600',
                    'Content-Length': str(len(segment_bytes))
                }
            )

        except Exception as e:
            logger.error(f"Error serving segment {video_id}/{segment_name}: {e}", exc_info=True)
            return web.Response(status=500, text="Internal server error")

    async def handle_api_videos(self, request: Request) -> Response:
        """
        API endpoint to list all videos.

        Args:
            request: HTTP request

        Returns:
            JSON response with video list
        """
        try:
            videos = await self.db.get_all_videos()

            video_list = []
            for video in videos:
                video_data = {
                    'id': video.video_id,
                    'filename': video.original_filename,
                    'duration': video.total_duration,
                    'segments': video.total_segments,
                    'size': video.file_size,
                    'status': video.status,
                    'created_at': video.created_at,
                    'urls': {
                        'local': self.config.get_playlist_url(video.video_id, public=False),
                    }
                }

                if self.config.public_domain:
                    video_data['urls']['public'] = self.config.get_playlist_url(video.video_id, public=True)

                video_list.append(video_data)

            return web.json_response({
                'success': True,
                'videos': video_list,
                'total': len(video_list)
            })

        except Exception as e:
            logger.error(f"API videos error: {e}", exc_info=True)
            return web.json_response(
                {'success': False, 'error': 'Failed to retrieve videos'},
                status=500
            )

    async def start(self) -> None:
        """
        Start the aiohttp web server.

        This method sets up all routes and starts the server on the
        configured host and port.
        """
        try:
            # Create application with custom settings
            app = web.Application(
                client_max_size=self.config.max_upload_size
            )

            # Setup Jinja2 templating
            aiohttp_jinja2.setup(
                app,
                loader=jinja2.FileSystemLoader('templates')
            )

            # Add routes
            app.router.add_get('/', self.handle_index)
            app.router.add_post('/upload', self.handle_upload)
            app.router.add_get('/status/{task_id}', self.handle_task_status)
            app.router.add_get('/playlist/{video_id}.m3u8', self.handle_playlist)
            app.router.add_get('/segment/{video_id}/{segment_name}', self.handle_segment)

            # API routes
            app.router.add_get('/api/videos', self.handle_api_videos)

            # Start server
            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, self.host, self.port)
            await site.start()

            logger.info(f"🚀 Streaming server started successfully")
            logger.info(f"📡 Local access: {self.config.get_local_url()}")

            if self.config.public_domain:
                logger.info(f"🌐 Public access: {self.config.get_public_url()}")
            else:
                logger.info("🏠 Public access: Not configured (local network only)")

            logger.info("💡 Upload videos at the web interface or use CLI commands")

        except Exception as e:
            logger.error(f"Failed to start server: {e}", exc_info=True)
            raise
