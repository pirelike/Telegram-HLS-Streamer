import asyncio
import os
import aiofiles
import uuid
import ssl
import time
import hashlib
from pathlib import Path
from aiohttp import web
import aiohttp_jinja2
import jinja2
from logger_config import logger
from database import DatabaseManager
from video_processor import split_video_to_hls
from utils import get_local_ip, is_valid_domain
from cache_manager import create_cache_manager, create_predictive_cache_manager

# A simple in-memory store for task statuses
task_status = {}

class StreamServer:
    """
    Enhanced HTTP/HTTPS server for streaming video content stored on Telegram.
    Features predictive caching, multi-bot support, bot-aware downloads, and subtitle serving.
    """
    def __init__(self, host: str, port: int, db_manager: DatabaseManager, bot_token: str,
                 chat_id: str, public_domain: str = None, playlists_dir: str = "playlists",
                 ssl_cert_path: str = None, ssl_key_path: str = None, cache_size: int = 500 * 1024 * 1024,
                 force_https: bool = False, cache_type: str = "memory", preload_segments: int = 8,
                 max_concurrent_preloads: int = 5):
        self.host = host
        self.port = port
        self.db = db_manager
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.public_domain = public_domain
        self.playlists_dir = playlists_dir
        self.ssl_cert_path = ssl_cert_path
        self.ssl_key_path = ssl_key_path
        self.force_https = force_https
        self.cache_type = cache_type
        self.preload_segments = preload_segments
        self.max_concurrent_preloads = max_concurrent_preloads
        self.cache_size = cache_size

        # Telegram handler will be set by main.py
        self.telegram_handler = None

        # Initialize basic cache manager first (predictive will be set up later)
        cache_dir = os.getenv('CACHE_DIR', 'cache')
        self.cache_manager = create_cache_manager(
            db_manager,
            cache_type=cache_type,
            cache_dir=cache_dir,
            max_cache_size=cache_size
        )

        # Predictive cache manager will be initialized when telegram_handler is available
        self.predictive_cache_manager = None

        # Session cleanup task
        self.cleanup_task = None

        # Auto-detect local IP if host is 0.0.0.0
        if host == "0.0.0.0":
            detected_ip = get_local_ip()
            self.local_ip = detected_ip if detected_ip else "127.0.0.1"
        else:
            self.local_ip = host

        # Determine protocol based on SSL configuration or force_https
        self.protocol = "http"  # Default to HTTP, will be updated after SSL check

        logger.info(f"StreamServer initialized to run on {host}:{port}")
        logger.info(f"Local IP detected/configured: {self.local_ip}")
        logger.info(f"Cache type: {cache_type}, Predictive preloading: {preload_segments} segments")
        if public_domain:
            logger.info(f"Public domain configured: {public_domain}")
        if force_https:
            logger.info("Force HTTPS enabled (reverse proxy mode)")

    async def process_video_task(self, task_id: str, video_path: str):
        """A background task to process the video using the configured Telegram handler."""
        def log_status(message, level='INFO'):
            status = f"[{level}] {message}"
            if task_id not in task_status:
                task_status[task_id] = []
            task_status[task_id].append(status)
            logger.info(status)

        try:
            log_status("Starting video processing...")
            video_p = Path(video_path)
            video_id = str(uuid.uuid4())
            segments_dir = f"segments/{video_id}"

            # CRITICAL FIX: Initialize database and cache manager for this task
            log_status("Initializing database for processing task...")
            await self.db.initialize_database()
            if self.cache_manager:
                await self.cache_manager.initialize()
            log_status("Database initialized successfully.", "SUCCESS")

            log_status(f"Splitting video '{video_p.name}' into HLS segments...")
            split_video_to_hls(str(video_p), segments_dir)
            log_status("Video splitting complete.", "SUCCESS")

            # Check if we have a telegram handler (should be set by main.py)
            if not self.telegram_handler:
                log_status("Creating telegram handler for upload...", "INFO")
                # Fallback: create handler if not set
                from main import create_telegram_handler
                self.telegram_handler = create_telegram_handler(self.db)

            # Determine handler type and log accordingly
            handler_type = type(self.telegram_handler).__name__
            if "RoundRobin" in handler_type:
                bot_count = len(self.telegram_handler.bots) if hasattr(self.telegram_handler, 'bots') else 1
                log_status(f"Using {handler_type} with {bot_count} bots for upload...", "INFO")
            else:
                log_status(f"Using {handler_type} for upload...", "INFO")

            log_status("Uploading segments and subtitles to Telegram...")
            success = await self.telegram_handler.upload_segments_to_telegram(segments_dir, video_id, video_p.name)

            if success:
                # Generate both local and public playlists
                await self._create_dual_playlists(video_id)

                # Provide both URLs in the response
                local_url = f"{self.protocol}://{self.local_ip}:{self.port}/playlist/local/{video_id}.m3u8"

                result_message = f"Upload complete!<br>"
                result_message += f"🏠 <a href='{local_url}' target='_blank'>Local Network Playlist</a><br>"

                if self.public_domain:
                    public_url = f"{self.protocol}://{self.public_domain}/playlist/public/{video_id}.m3u8"
                    result_message += f"🌐 <a href='{public_url}' target='_blank'>Public Playlist</a><br>"
                else:
                    result_message += "ℹ️ Configure PUBLIC_DOMAIN in .env for public access<br>"

                # Add subtitle information
                subtitle_files = await self.db.get_subtitle_files(video_id)
                if subtitle_files:
                    result_message += f"<br>📄 <strong>Subtitles:</strong> {len(subtitle_files)} tracks available<br>"
                    for sf in subtitle_files[:5]:  # Show first 5
                        subtitle_url = f"{self.protocol}://{self.local_ip}:{self.port}/subtitle/{video_id}/{sf.language}.{sf.file_type}"
                        result_message += f"  • <a href='{subtitle_url}' target='_blank'>{sf.language.upper()}</a> ({sf.file_type})<br>"
                    if len(subtitle_files) > 5:
                        result_message += f"  • ... and {len(subtitle_files) - 5} more<br>"

                # Add multi-bot performance info if applicable
                if "RoundRobin" in handler_type:
                    result_message += f"<br>⚡ <strong>Multi-Bot Upload:</strong> Used {bot_count} bots for faster processing<br>"

                # Add predictive caching info if available
                if self.predictive_cache_manager:
                    result_message += f"<br>🔮 <strong>Predictive Caching:</strong> Will preload {self.preload_segments} segments ahead for smooth playback<br>"

                    # Trigger initial preloading for the first segments
                    log_status("Pre-warming cache with initial segments...", "INFO")
                    await self.predictive_cache_manager.force_preload_video(video_id, 0, min(10, self.preload_segments))
                    log_status("Initial cache warming complete.", "SUCCESS")

                log_status(result_message, "RESULT")
            else:
                log_status("Upload failed.", "ERROR")

        except Exception as e:
            log_status(f"An error occurred: {e}", "ERROR")
            logger.error("Error during video processing task", exc_info=True)
        finally:
            log_status("---STREAM_END---")
            # Clean up temporary video file
            if os.path.exists(video_path):
                try:
                    os.remove(video_path)
                    log_status(f"Cleaned up temporary file: {video_path}")
                except OSError as e:
                    logger.error(f"Error removing temp file {video_path}: {e}")

            # Clean up segments directory if it exists
            try:
                if 'segments_dir' in locals() and os.path.exists(segments_dir):
                    import shutil
                    shutil.rmtree(segments_dir)
                    log_status(f"Cleaned up segments directory: {segments_dir}")
            except Exception as e:
                logger.warning(f"Error cleaning up segments directory: {e}")

    async def handle_process_request(self, request: web.Request):
        """Handles the video processing form submission."""
        data = await request.post()
        video_file_field = data.get('video_file')

        if not video_file_field:
            return web.json_response({'success': False, 'error': 'Video file is required.'}, status=400)

        video_bytes = video_file_field.file.read()
        filename = f"{uuid.uuid4()}-{video_file_field.filename}"
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        video_path = os.path.join(temp_dir, filename)
        with open(video_path, 'wb') as f:
            f.write(video_bytes)

        task_id = str(uuid.uuid4())
        asyncio.create_task(self.process_video_task(task_id, video_path))

        return web.json_response({'success': True, 'task_id': task_id})

    async def handle_status_request(self, request: web.Request):
        """Handles requests for task status via Server-Sent Events."""
        task_id = request.match_info['task_id']
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive'}
        )
        await response.prepare(request)

        last_index = 0
        try:
            while True:
                await asyncio.sleep(1)
                if task_id in task_status and task_status.get(task_id):
                    for i in range(last_index, len(task_status[task_id])):
                        status = task_status[task_id][i]
                        await response.write(f"data: {status}\n\n".encode('utf-8'))
                        if "---STREAM_END---" in status:
                            await response.write(b"data: ---CLOSE---\n\n")
                            return response
                    last_index = len(task_status[task_id])
        except asyncio.CancelledError:
            logger.info(f"Status stream for task {task_id} closed by client.")
        finally:
            if task_id in task_status:
                del task_status[task_id]
        return response

    def _get_session_id(self, request: web.Request) -> str:
        """
        Get or generate session ID for viewer tracking.
        Uses a combination of IP, User-Agent, and optional custom header.
        """
        # Try to get from custom header first (for clients that support it)
        session_id = request.headers.get('X-Session-ID')

        if not session_id:
            # Generate consistent session ID based on client fingerprint
            user_agent = request.headers.get('User-Agent', 'unknown')
            client_ip = request.remote or 'unknown'
            forwarded_for = request.headers.get('X-Forwarded-For', '')

            # Use forwarded IP if available (for reverse proxy setups)
            if forwarded_for:
                client_ip = forwarded_for.split(',')[0].strip()

            # Create a hash-based session ID
            fingerprint = f"{client_ip}_{user_agent}_{request.headers.get('Accept-Language', '')}"
            session_hash = hashlib.md5(fingerprint.encode()).hexdigest()[:16]
            session_id = f"session_{session_hash}"

        return session_id

    @aiohttp_jinja2.template('index.html')
    async def index(self, request: web.Request):
        """Serves the main index page with enhanced information including multi-bot status and predictive caching."""

        # Get system information
        try:
            import psutil
            import platform
            system_stats_available = True
        except ImportError:
            system_stats_available = False

        # Database stats
        try:
            db_stats = await self.db.get_database_stats()
        except Exception as e:
            logger.warning(f"Could not get database stats: {e}")
            db_stats = {}

        # Enhanced cache stats with predictive caching info
        try:
            # Use predictive cache manager if available, otherwise fall back to basic
            cache_manager_to_use = self.predictive_cache_manager or self.cache_manager
            if cache_manager_to_use:
                cache_stats = await cache_manager_to_use.get_cache_stats()
            else:
                cache_stats = {}
        except Exception as e:
            logger.warning(f"Could not get cache stats: {e}")
            cache_stats = {}

        # System stats
        system_stats = {}
        if system_stats_available:
            try:
                import psutil
                cpu_percent = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')

                system_stats = {
                    'cpu_percent': cpu_percent,
                    'memory_total': memory.total,
                    'memory_used': memory.used,
                    'memory_percent': memory.percent,
                    'disk_total': disk.total,
                    'disk_used': disk.used,
                    'disk_percent': (disk.used / disk.total) * 100,
                    'platform': platform.system(),
                    'platform_release': platform.release(),
                    'python_version': platform.python_version()
                }
            except Exception as e:
                logger.warning(f"Could not get system stats: {e}")

        # Multi-bot information
        multi_bot_info = self._get_multi_bot_info()

        # Enhanced configuration info
        config_info = {
            'max_upload_size': int(os.getenv('MAX_UPLOAD_SIZE', 50 * 1024**3)),
            'max_chunk_size': int(os.getenv('MAX_CHUNK_SIZE', 15 * 1024 * 1024)),
            'min_segment_duration': int(os.getenv('MIN_SEGMENT_DURATION', 2)),
            'max_segment_duration': int(os.getenv('MAX_SEGMENT_DURATION', 30)),
            'cache_size': int(os.getenv('CACHE_SIZE', 500 * 1024 * 1024)),
            'log_level': os.getenv('LOG_LEVEL', 'INFO'),
            'hardware_accel': os.getenv('FFMPEG_HARDWARE_ACCEL', 'None'),
            'force_https': os.getenv('FORCE_HTTPS', 'false').lower() == 'true',
            'preload_segments': self.preload_segments,
            'max_concurrent_preloads': self.max_concurrent_preloads
        }

        # Predictive caching info
        predictive_info = {
            'enabled': self.predictive_cache_manager is not None,
            'preload_segments': self.preload_segments,
            'max_concurrent_preloads': self.max_concurrent_preloads,
            'active_sessions': cache_stats.get('predictive_stats', {}).get('active_sessions', 0),
            'videos_being_watched': cache_stats.get('predictive_stats', {}).get('videos_being_watched', 0)
        }

        return {
            "telegram_configured": bool(self.bot_token and self.chat_id),
            "local_ip": self.local_ip,
            "public_domain": self.public_domain or "Not configured",
            "protocol": self.protocol,
            "ssl_enabled": self.protocol == "https",
            "cache_type": self.cache_type,
            "db_stats": db_stats,
            "cache_stats": cache_stats,
            "system_stats": system_stats,
            "config_info": config_info,
            "server_version": "2.2.0",  # Updated version with predictive caching
            "multi_bot_info": multi_bot_info,
            "predictive_info": predictive_info,  # New predictive caching info
            "features": {
                "subtitle_support": True,
                "hybrid_encoding": True,
                "auto_resegmentation": True,
                "smart_caching": True,
                "predictive_caching": self.predictive_cache_manager is not None,  # Dynamic based on availability
                "multi_bot_upload": multi_bot_info['enabled']
            }
        }

    def _get_multi_bot_info(self):
        """Get information about multi-bot configuration for the web UI."""
        multi_bot_info = {
            'enabled': False,
            'bot_count': 1,
            'handler_type': 'Single Bot',
            'estimated_speedup': 1
        }

        if self.telegram_handler:
            handler_type = type(self.telegram_handler).__name__
            if "RoundRobin" in handler_type and hasattr(self.telegram_handler, 'bots'):
                bot_count = len(self.telegram_handler.bots)
                multi_bot_info.update({
                    'enabled': True,
                    'bot_count': bot_count,
                    'handler_type': f'Round-Robin ({bot_count} bots)',
                    'estimated_speedup': bot_count
                })

        return multi_bot_info

    async def serve_playlist(self, request: web.Request):
        """Serves HLS playlist files."""
        video_id = request.match_info['video_id']
        access_type = request.match_info.get('access_type', 'local')  # Default to local for backwards compatibility

        # Remove .m3u8 extension from video_id if it exists (for backwards compatibility)
        if video_id.endswith('.m3u8'):
            video_id = video_id[:-5]

        # Construct the correct path with .m3u8 extension
        playlist_path = f"{self.playlists_dir}/{access_type}/{video_id}.m3u8"

        if not os.path.exists(playlist_path):
            logger.warning(f"Playlist not found at: {playlist_path}")
            return web.Response(status=404, text="Playlist not found")

        try:
            async with aiofiles.open(playlist_path, 'r') as f:
                content = await f.read()

            logger.info(f"Serving playlist: {playlist_path}")
            headers = {
                'Cache-Control': 'no-cache',
                'Access-Control-Allow-Origin': '*'
            }

            # Add predictive cache header if available
            if self.predictive_cache_manager:
                headers['X-Predictive-Cache'] = 'Enabled'

            return web.Response(
                text=content,
                content_type='application/vnd.apple.mpegurl',
                headers=headers
            )
        except Exception as e:
            logger.error(f"Error reading playlist {playlist_path}: {e}")
            return web.Response(status=500, text="Error reading playlist")

    async def serve_subtitle(self, request: web.Request):
        """Handles requests for subtitle files with bot-aware downloads."""
        video_id = request.match_info['video_id']
        language = request.match_info.get('language', 'eng')  # Default to English

        # Remove file extension if present
        if '.' in language:
            language = language.split('.')[0]

        # Get subtitle file info from database
        subtitle_file = await self.db.get_subtitle_file_by_language(video_id, language)

        if not subtitle_file:
            # Try to find any subtitle file for this video
            subtitle_files = await self.db.get_subtitle_files(video_id)
            if subtitle_files:
                subtitle_file = subtitle_files[0]  # Use first available
            else:
                logger.warning(f"No subtitle found for video {video_id}, language {language}")
                return web.Response(status=404, text="Subtitle not found")

        # Download subtitle from Telegram using the configured handler with bot_id
        if not self.telegram_handler:
            logger.error("No telegram handler available for subtitle download")
            return web.Response(status=500, text="Telegram handler not available")

        subtitle_bytes = await self.telegram_handler.download_subtitle_from_telegram(
            subtitle_file.file_id, subtitle_file.bot_id
        )

        if subtitle_bytes:
            # Determine content type based on file type
            content_type_map = {
                'srt': 'text/plain; charset=utf-8',
                'vtt': 'text/vtt; charset=utf-8',
                'ass': 'text/plain; charset=utf-8',
                'ssa': 'text/plain; charset=utf-8',
                'sup': 'application/octet-stream'
            }

            content_type = content_type_map.get(subtitle_file.file_type.lower(), 'text/plain; charset=utf-8')

            logger.info(f"Serving subtitle: {subtitle_file.filename} for video {video_id} (bot: {subtitle_file.bot_id})")
            return web.Response(
                body=subtitle_bytes,
                content_type=content_type,
                headers={
                    'Cache-Control': 'public, max-age=86400',  # Cache for 1 day
                    'Access-Control-Allow-Origin': '*',
                    'Content-Disposition': f'inline; filename="{subtitle_file.filename}"',
                    'X-Bot-Used': subtitle_file.bot_id  # Debug header
                }
            )

        logger.error(f"Failed to download subtitle {subtitle_file.filename} from Telegram")
        return web.Response(status=500, text="Failed to retrieve subtitle")

    async def list_subtitles(self, request: web.Request):
        """Lists available subtitles for a video."""
        video_id = request.match_info['video_id']

        subtitle_files = await self.db.get_subtitle_files(video_id)
        subtitles_data = []

        for subtitle_file in subtitle_files:
            subtitles_data.append({
                'language': subtitle_file.language,
                'filename': subtitle_file.filename,
                'file_type': subtitle_file.file_type,
                'file_size': subtitle_file.file_size,
                'bot_id': subtitle_file.bot_id,
                'url': f"/subtitle/{video_id}/{subtitle_file.language}.{subtitle_file.file_type}"
            })

        return web.json_response({
            'video_id': video_id,
            'subtitles': subtitles_data
        })

    async def serve_segment(self, request: web.Request):
        """
        Enhanced segment serving with predictive caching and session tracking.
        """
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']

        # Generate or get session ID from request
        session_id = self._get_session_id(request)

        start_time = time.time()

        # Use predictive cache manager for enhanced performance if available
        if self.predictive_cache_manager:
            segment_bytes = await self.predictive_cache_manager.handle_segment_request(
                video_id, segment_name, session_id
            )
        else:
            # Fallback to basic cache manager
            segment_bytes = await self.cache_manager.get_cached_segment(video_id, segment_name)

            if not segment_bytes:
                # Download from Telegram using basic method
                segments = await self.db.get_video_segments(video_id)
                segment_info = segments.get(segment_name)

                if not segment_info:
                    logger.warning(f"Segment not found in database: {video_id}/{segment_name}")
                    return web.Response(status=404, text="Segment not found")

                if not self.telegram_handler:
                    logger.error("No telegram handler available for segment download")
                    return web.Response(status=500, text="Telegram handler not available")

                segment_bytes = await self.telegram_handler.download_segment_from_telegram(
                    segment_info.file_id, segment_info.bot_id
                )

                if segment_bytes:
                    await self.cache_manager.cache_segment(video_id, segment_name, segment_bytes)

        if segment_bytes:
            response_time = time.time() - start_time

            # Determine cache status
            cache_status = "HIT" if response_time < 0.1 else "MISS"  # Fast response indicates cache hit

            logger.debug(f"Served segment {video_id}/{segment_name} to session {session_id} in {response_time:.3f}s ({cache_status})")

            headers = {
                'Cache-Control': 'public, max-age=31536000',  # Cache for 1 year
                'Access-Control-Allow-Origin': '*',
                'X-Cache': cache_status,
                'X-Cache-Type': self.cache_type,
                'X-Session-ID': session_id,
                'X-Response-Time': f"{response_time:.3f}s",
                'X-Segment-Size': str(len(segment_bytes))
            }

            # Add predictive cache header if available
            if self.predictive_cache_manager:
                headers['X-Predictive-Cache'] = 'Enabled'

            return web.Response(
                body=segment_bytes,
                content_type='video/mp2t',
                headers=headers
            )

        logger.error(f"Failed to serve segment {video_id}/{segment_name} to session {session_id}")
        return web.Response(status=500, text="Failed to retrieve segment")

    async def serve_cache_stats(self, request: web.Request):
        """Enhanced cache statistics endpoint with predictive caching info."""
        # Use predictive cache manager if available, otherwise fall back to basic
        cache_manager_to_use = self.predictive_cache_manager or self.cache_manager

        if not cache_manager_to_use:
            return web.json_response({
                'error': 'Cache manager not initialized',
                'cache_type': self.cache_type
            })

        stats = await cache_manager_to_use.get_cache_stats()

        # Add server-specific information
        stats['server_info'] = {
            'preload_segments': self.preload_segments,
            'max_concurrent_preloads': self.max_concurrent_preloads,
            'cache_type': self.cache_type,
            'server_version': '2.2.0',
            'predictive_cache_available': self.predictive_cache_manager is not None
        }

        if self.predictive_cache_manager:
            stats['server_info']['active_preload_tasks'] = len(getattr(self.predictive_cache_manager, 'preload_tasks', {}))

        # Add bot distribution stats from database
        try:
            db_stats = await self.db.get_database_stats()
            if 'bot_distribution' in db_stats:
                stats['bot_distribution'] = db_stats['bot_distribution']
        except Exception as e:
            logger.warning(f"Could not get bot distribution stats: {e}")

        return web.json_response(stats)

    async def clear_cache_endpoint(self, request: web.Request):
        """Enhanced cache clearing endpoint."""
        video_id = request.query.get('video_id')

        # Use predictive cache manager if available, otherwise fall back to basic
        cache_manager_to_use = self.predictive_cache_manager or self.cache_manager

        if not cache_manager_to_use:
            return web.json_response({
                'success': False,
                'error': 'Cache manager not initialized'
            })

        await cache_manager_to_use.clear_cache(video_id)

        clear_type = f"for video {video_id}" if video_id else "(all videos)"
        logger.info(f"Cache cleared {clear_type} via API request")

        response_data = {
            'success': True,
            'message': f'Cache cleared {clear_type}'
        }

        if self.predictive_cache_manager:
            response_data['predictive_cache'] = 'Sessions and preload tasks also cleared'

        return web.json_response(response_data)

    # Predictive caching endpoints (only work if predictive cache manager is available)

    async def force_preload_endpoint(self, request: web.Request):
        """Endpoint to manually trigger preloading for a specific video."""
        if not self.predictive_cache_manager:
            return web.json_response({
                'error': 'Predictive cache manager not available'
            }, status=501)

        try:
            data = await request.json()
        except:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        video_id = data.get('video_id')
        start_segment = data.get('start_segment', 0)
        segment_count = data.get('segment_count', 10)

        if not video_id:
            return web.json_response({'error': 'video_id required'}, status=400)

        # Trigger force preloading
        success = await self.predictive_cache_manager.force_preload_video(
            video_id, start_segment, segment_count
        )

        if success:
            return web.json_response({
                'success': True,
                'message': f'Preloading triggered for {video_id}',
                'start_segment': start_segment,
                'segment_count': segment_count
            })
        else:
            return web.json_response({
                'success': False,
                'error': f'Failed to preload video {video_id}'
            }, status=500)

    async def list_active_sessions(self, request: web.Request):
        """Endpoint to list active viewing sessions."""
        if not self.predictive_cache_manager:
            return web.json_response({
                'error': 'Predictive cache manager not available'
            })

        sessions_info = []

        for session_id, session in getattr(self.predictive_cache_manager, 'active_sessions', {}).items():
            sessions_info.append({
                'session_id': session_id,
                'video_id': session.video_id,
                'current_segment': session.current_segment,
                'playback_speed': session.playback_speed,
                'segments_requested': len(session.segments_requested),
                'idle_time': time.time() - session.last_request_time,
                'user_agent': session.user_agent,
                'client_ip': session.client_ip
            })

        # Get popular videos
        popular_videos = []
        if hasattr(self.predictive_cache_manager, 'get_popular_videos'):
            popular_videos = await self.predictive_cache_manager.get_popular_videos()

        return web.json_response({
            'active_sessions': sessions_info,
            'total_sessions': len(sessions_info),
            'popular_videos': popular_videos
        })

    async def get_video_analytics(self, request: web.Request):
        """Get analytics for video viewing patterns."""
        if not self.predictive_cache_manager:
            return web.json_response({
                'error': 'Analytics not available without predictive cache manager'
            })

        video_id = request.match_info.get('video_id')

        # Get sessions for this video
        video_sessions = []
        if hasattr(self.predictive_cache_manager, 'active_sessions'):
            for session_id, session in self.predictive_cache_manager.active_sessions.items():
                if not video_id or session.video_id == video_id:
                    video_sessions.append({
                        'session_id': session_id,
                        'video_id': session.video_id,
                        'current_segment': session.current_segment,
                        'playback_speed': session.playback_speed,
                        'segments_watched': len(session.segments_requested),
                        'watch_duration': time.time() - session.last_request_time,
                        'last_activity': session.last_request_time
                    })

        analytics = {
            'video_id': video_id,
            'active_sessions': video_sessions,
            'total_viewers': len(video_sessions),
            'average_playback_speed': sum(s['playback_speed'] for s in video_sessions) / len(video_sessions) if video_sessions else 0,
            'most_watched_segment': max((s['current_segment'] for s in video_sessions), default=0) if video_sessions else 0
        }

        return web.json_response(analytics)

    async def _create_dual_playlists(self, video_id: str):
        """Creates both local and public playlists for the video."""
        segments = await self.db.get_video_segments(video_id)
        if not segments:
            raise ValueError(f"No segments found for video {video_id}")

        target_duration = max((s.duration for s in segments.values()), default=10)
        sorted_segments = sorted(segments.values(), key=lambda s: s.segment_order)

        # Create local playlist
        await self._create_playlist(
            video_id=video_id,
            segments=sorted_segments,
            target_duration=target_duration,
            base_url=f"{self.protocol}://{self.local_ip}:{self.port}",
            output_path=f"{self.playlists_dir}/local/{video_id}.m3u8",
            playlist_type="local"
        )

        # Create public playlist if domain is configured
        if self.public_domain:
            await self._create_playlist(
                video_id=video_id,
                segments=sorted_segments,
                target_duration=target_duration,
                base_url=f"{self.protocol}://{self.public_domain}",
                output_path=f"{self.playlists_dir}/public/{video_id}.m3u8",
                playlist_type="public"
            )

    async def _create_playlist(self, video_id: str, segments: list, target_duration: float,
                             base_url: str, output_path: str, playlist_type: str):
        """Creates a single playlist file with the specified configuration and subtitle support."""

        # Get subtitle files for this video
        subtitle_files = await self.db.get_subtitle_files(video_id)

        content = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{int(target_duration) + 1}",
            f"# Generated for {playlist_type} access"
        ]

        # Add predictive cache info if available
        if self.predictive_cache_manager:
            content.append(f"# Predictive Cache: {self.preload_segments} segments ahead")

        content.extend([
            f"# Video ID: {video_id}",
            f"# Base URL: {base_url}",
            ""
        ])

        # Add subtitle media groups if subtitles are available
        if subtitle_files:
            content.append("# Subtitle tracks")

            for subtitle_file in subtitle_files:
                # Create subtitle media declaration
                subtitle_url = f"{base_url}/subtitle/{video_id}/{subtitle_file.language}.{subtitle_file.file_type}"

                # Determine if this is the default subtitle
                is_default = subtitle_file.language.lower() in ['eng', 'en']
                default_attr = "YES" if is_default else "NO"
                autoselect_attr = "YES" if is_default else "NO"

                # Handle forced subtitles
                forced_attr = ""
                if 'forced' in subtitle_file.filename.lower():
                    forced_attr = ',FORCED=YES'

                # Create proper language name
                language_names = {
                    'eng': 'English', 'en': 'English', 'spa': 'Spanish', 'es': 'Spanish',
                    'fre': 'French', 'fr': 'French', 'ger': 'German', 'de': 'German',
                    'rus': 'Russian', 'ru': 'Russian', 'jpn': 'Japanese', 'ja': 'Japanese',
                    'kor': 'Korean', 'ko': 'Korean', 'chi': 'Chinese', 'zh': 'Chinese',
                    'por': 'Portuguese', 'pt': 'Portuguese', 'ita': 'Italian', 'it': 'Italian',
                    'dut': 'Dutch', 'nl': 'Dutch', 'swe': 'Swedish', 'sv': 'Swedish',
                    'nor': 'Norwegian', 'no': 'Norwegian', 'dan': 'Danish', 'da': 'Danish',
                    'fin': 'Finnish', 'fi': 'Finnish', 'und': 'Unknown'
                }

                language_name = language_names.get(subtitle_file.language.lower(), subtitle_file.language.upper())

                # Add EXT-X-MEDIA tag for subtitle
                content.append(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="{language_name}",LANGUAGE="{subtitle_file.language}",DEFAULT={default_attr},AUTOSELECT={autoselect_attr}{forced_attr},URI="{subtitle_url}"')

            content.append("")

        # Add video segments
        for segment in segments:
            content.append(f"#EXTINF:{segment.duration:.6f},")
            content.append(f"{base_url}/segment/{video_id}/{segment.filename}")

        content.append("#EXT-X-ENDLIST")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        async with aiofiles.open(output_path, 'w') as f:
            await f.write('\n'.join(content))

        logger.info(f"Created {playlist_type} playlist at {output_path}")
        if subtitle_files:
            logger.info(f"  └── Included {len(subtitle_files)} subtitle tracks: {', '.join(sf.language for sf in subtitle_files)}")

    def _determine_protocol(self):
        """Determine the protocol to use based on configuration."""
        # If force_https is enabled (reverse proxy mode), use HTTPS
        if self.force_https:
            return "https"

        # If SSL certificates are provided and exist, use HTTPS
        if (self.ssl_cert_path and self.ssl_key_path and
            os.path.exists(self.ssl_cert_path) and os.path.exists(self.ssl_key_path)):
            return "https"

        # Default to HTTP
        return "http"

    async def start_cleanup_task(self):
        """Start the periodic cleanup task for inactive sessions."""
        if not self.predictive_cache_manager:
            return  # No cleanup needed for basic cache

        async def cleanup_loop():
            while True:
                try:
                    cleanup_interval = int(os.getenv('SESSION_CLEANUP_INTERVAL', 300))
                    await asyncio.sleep(cleanup_interval)  # Default 5 minutes

                    if hasattr(self.predictive_cache_manager, 'cleanup_inactive_sessions'):
                        await self.predictive_cache_manager.cleanup_inactive_sessions()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in session cleanup task: {e}")

        self.cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info("Started predictive cache session cleanup task")

    async def start(self):
        """Starts the enhanced aiohttp web server with predictive caching and HTTPS support."""
        # Initialize database at server startup
        logger.info("Initializing database...")
        await self.db.initialize_database()
        logger.info("Database initialized successfully")

        # Initialize basic cache manager first
        logger.info("Initializing basic cache manager...")
        await self.cache_manager.initialize()
        logger.info("Basic cache manager initialized successfully")

        # Initialize predictive cache manager if telegram_handler is available
        if self.telegram_handler:
            logger.info("Initializing predictive cache manager...")
            self.predictive_cache_manager = create_predictive_cache_manager(
                db_manager=self.db,
                telegram_handler=self.telegram_handler,
                cache_type=self.cache_type,
                cache_dir=os.getenv('CACHE_DIR', 'cache'),
                max_cache_size=self.cache_size,
                preload_segments=self.preload_segments,
                max_concurrent_preloads=self.max_concurrent_preloads
            )
            await self.predictive_cache_manager.initialize()
            logger.info("Predictive cache manager initialized successfully")
        else:
            logger.warning("Telegram handler not set - using basic cache manager only")

        # Start cleanup task if predictive cache is available
        await self.start_cleanup_task()

        # Determine protocol before creating playlists
        self.protocol = self._determine_protocol()

        # Get max upload size from environment or use default
        max_upload_size = int(os.getenv('MAX_UPLOAD_SIZE', 50 * 1024**3))

        app = web.Application(client_max_size=max_upload_size)
        aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))

        # Enhanced Routes
        app.router.add_get('/', self.index)
        app.router.add_post('/process', self.handle_process_request)
        app.router.add_get('/status/{task_id}', self.handle_status_request)

        # Playlist routes (with access type)
        app.router.add_get('/playlist/{access_type}/{video_id}.m3u8', self.serve_playlist)
        # Backwards compatibility route (defaults to local)
        app.router.add_get('/playlist/{video_id}.m3u8', self.serve_playlist)

        # Enhanced segment route with predictive caching
        app.router.add_get('/segment/{video_id}/{segment_name}', self.serve_segment)

        # Subtitle routes
        app.router.add_get('/subtitle/{video_id}/{language}.{ext}', self.serve_subtitle)
        app.router.add_get('/subtitle/{video_id}/{language}', self.serve_subtitle)
        app.router.add_get('/subtitle/{video_id}', self.serve_subtitle)  # Default language
        app.router.add_get('/subtitles/{video_id}', self.list_subtitles)  # List available subtitles

        # Basic cache management routes (always available)
        app.router.add_get('/cache/stats', self.serve_cache_stats)
        app.router.add_post('/cache/clear', self.clear_cache_endpoint)

        # Predictive caching routes (only if predictive cache manager is available)
        if self.predictive_cache_manager:
            app.router.add_post('/cache/preload', self.force_preload_endpoint)
            app.router.add_get('/sessions', self.list_active_sessions)
            app.router.add_get('/analytics/{video_id}', self.get_video_analytics)
            app.router.add_get('/analytics', self.get_video_analytics)  # All videos

        runner = web.AppRunner(app)
        await runner.setup()

        # Setup SSL context if certificates are provided (only for direct SSL, not reverse proxy)
        ssl_context = None
        direct_ssl = False
        if (not self.force_https and self.ssl_cert_path and self.ssl_key_path and
            os.path.exists(self.ssl_cert_path) and os.path.exists(self.ssl_key_path)):
            try:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_context.load_cert_chain(self.ssl_cert_path, self.ssl_key_path)
                direct_ssl = True
                logger.info(f"🔒 SSL certificates loaded: {self.ssl_cert_path}")
            except Exception as e:
                logger.error(f"❌ Failed to load SSL certificates: {e}")
                logger.error("   Falling back to HTTP mode")
                self.protocol = "http"

        site = web.TCPSite(runner, self.host, self.port, ssl_context=ssl_context)
        await site.start()

        # Enhanced server startup logging
        logger.info(f"🚀 Enhanced streaming server started at {self.protocol}://{self.host}:{self.port}")
        logger.info(f"📡 Local access: {self.protocol}://{self.local_ip}:{self.port}")

        if self.public_domain:
            logger.info(f"🌐 Public access: {self.protocol}://{self.public_domain}")
        else:
            logger.info("ℹ️  Configure PUBLIC_DOMAIN in .env for public access")

        # SSL/HTTPS status logging
        if self.protocol == "https":
            if direct_ssl:
                logger.info("🔒 Direct HTTPS enabled with SSL certificates")
            elif self.force_https:
                logger.info("🔒 HTTPS URLs enabled (reverse proxy mode)")
                logger.info("   Server listening on HTTP but generating HTTPS URLs")
        else:
            logger.warning("⚠️ HTTP mode - modern browsers may block video playback")
            if self.public_domain:
                logger.warning("   Consider enabling HTTPS for public access")

        # Enhanced cache and predictive caching info
        try:
            cache_manager_to_use = self.predictive_cache_manager or self.cache_manager
            cache_stats = await cache_manager_to_use.get_cache_stats()
            cache_size_mb = cache_stats.get('max_cache_size', 500 * 1024 * 1024) / (1024*1024)
            cache_type = cache_stats.get('cache_type', self.cache_type)
            logger.info(f"💾 Cache: {cache_type.upper()}, {cache_size_mb:.0f}MB limit")

            if self.predictive_cache_manager:
                logger.info(f"🔮 Predictive caching: {self.preload_segments} segments ahead, {self.max_concurrent_preloads} concurrent preloads")
                logger.info(f"📊 Session tracking: Enabled for adaptive preloading")
            else:
                logger.info("📊 Basic caching: Standard LRU cache without predictive features")
        except Exception as e:
            logger.warning(f"Could not display cache stats: {e}")

        # Log multi-bot information if applicable
        multi_bot_info = self._get_multi_bot_info()
        if multi_bot_info['enabled']:
            cache_mode = "with predictive caching" if self.predictive_cache_manager else "with basic caching"
            logger.info(f"🤖 Multi-bot downloads: Bot-aware downloads {cache_mode}")
            logger.info(f"🔄 Upload mode: {multi_bot_info['handler_type']} (~{multi_bot_info['estimated_speedup']}x speedup)")
        else:
            cache_mode = "with predictive caching" if self.predictive_cache_manager else "with basic caching"
            logger.info(f"📱 Single bot mode {cache_mode}")

        logger.info("📄 Enhanced subtitle support with bot-aware downloads")
        logger.info("🎯 Bot-aware segment downloads with session tracking")

        # Log available API endpoints
        logger.info("🔧 Available API endpoints:")
        logger.info("   GET /cache/stats - Cache statistics")
        logger.info("   POST /cache/clear - Clear cache")

        if self.predictive_cache_manager:
            logger.info("   POST /cache/preload - Force preload segments")
            logger.info("   GET /sessions - View active sessions")
            logger.info("   GET /analytics/{video_id} - Video analytics")

    async def stop(self):
        """Stop the server and cleanup tasks."""
        logger.info("Stopping streaming server...")

        # Cancel cleanup task
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        # Cancel all preload tasks if predictive cache is available
        if self.predictive_cache_manager and hasattr(self.predictive_cache_manager, 'preload_tasks'):
            for task in self.predictive_cache_manager.preload_tasks.values():
                task.cancel()

            # Wait for tasks to complete
            for task in self.predictive_cache_manager.preload_tasks.values():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("Streaming server stopped")
