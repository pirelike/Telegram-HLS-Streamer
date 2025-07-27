import asyncio
import os
import aiofiles
import uuid
import ssl
from pathlib import Path
from aiohttp import web
import aiohttp_jinja2
import jinja2
from logger_config import logger
from database import DatabaseManager
from video_processor import split_video_to_hls
from utils import get_local_ip, is_valid_domain
from cache_manager import create_cache_manager

# A simple in-memory store for task statuses
task_status = {}

class StreamServer:
    """
    An HTTP/HTTPS server for streaming video content stored on Telegram.
    Enhanced with multi-bot support, configurable caching system, and subtitle serving.
    """
    def __init__(self, host: str, port: int, db_manager: DatabaseManager, bot_token: str,
                 chat_id: str, public_domain: str = None, playlists_dir: str = "playlists",
                 ssl_cert_path: str = None, ssl_key_path: str = None, cache_size: int = 500 * 1024 * 1024,
                 force_https: bool = False, cache_type: str = "memory"):
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

        # Initialize cache manager based on configuration
        cache_dir = os.getenv('CACHE_DIR', 'cache')
        self.cache_manager = create_cache_manager(
            db_manager,
            cache_type=cache_type,
            cache_dir=cache_dir,
            max_cache_size=cache_size
        )

        # Auto-detect local IP if host is 0.0.0.0
        if host == "0.0.0.0":
            detected_ip = get_local_ip()
            self.local_ip = detected_ip if detected_ip else "127.0.0.1"
        else:
            self.local_ip = host

        # Determine protocol based on SSL configuration or force_https
        self.protocol = "http"  # Default to HTTP, will be updated after SSL check

        # Telegram handler will be set by main.py
        self.telegram_handler = None

        logger.info(f"StreamServer initialized to run on {host}:{port}")
        logger.info(f"Local IP detected/configured: {self.local_ip}")
        logger.info(f"Cache type: {cache_type}")
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

    @aiohttp_jinja2.template('index.html')
    async def index(self, request: web.Request):
        """Serves the main index page with enhanced information including multi-bot status."""

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

        # Cache stats
        try:
            cache_stats = await self.cache_manager.get_cache_stats()
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

        # Configuration info
        config_info = {
            'max_upload_size': int(os.getenv('MAX_UPLOAD_SIZE', 50 * 1024**3)),
            'max_chunk_size': int(os.getenv('MAX_CHUNK_SIZE', 15 * 1024 * 1024)),
            'min_segment_duration': int(os.getenv('MIN_SEGMENT_DURATION', 2)),
            'max_segment_duration': int(os.getenv('MAX_SEGMENT_DURATION', 30)),
            'cache_size': int(os.getenv('CACHE_SIZE', 500 * 1024 * 1024)),
            'log_level': os.getenv('LOG_LEVEL', 'INFO'),
            'hardware_accel': os.getenv('FFMPEG_HARDWARE_ACCEL', 'None'),
            'force_https': os.getenv('FORCE_HTTPS', 'false').lower() == 'true'
        }

        # Recent activity (if we have any task status)
        recent_tasks = []
        if hasattr(self, 'recent_tasks'):
            recent_tasks = getattr(self, 'recent_tasks', [])

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
            "recent_tasks": recent_tasks,
            "server_version": "2.1.0",  # Updated version
            "multi_bot_info": multi_bot_info,  # New multi-bot information
            "features": {
                "subtitle_support": True,
                "hybrid_encoding": True,
                "auto_resegmentation": True,
                "smart_caching": True,
                "multi_bot_upload": multi_bot_info['enabled']  # Dynamic based on config
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
            return web.Response(
                text=content,
                content_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache',
                    'Access-Control-Allow-Origin': '*'
                }
            )
        except Exception as e:
            logger.error(f"Error reading playlist {playlist_path}: {e}")
            return web.Response(status=500, text="Error reading playlist")

    async def serve_subtitle(self, request: web.Request):
        """Handles requests for subtitle files."""
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

        # Download subtitle from Telegram using the configured handler
        if not self.telegram_handler:
            logger.error("No telegram handler available for subtitle download")
            return web.Response(status=500, text="Telegram handler not available")

        subtitle_bytes = await self.telegram_handler.download_subtitle_from_telegram(subtitle_file.file_id)

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

            logger.info(f"Serving subtitle: {subtitle_file.filename} for video {video_id}")
            return web.Response(
                body=subtitle_bytes,
                content_type=content_type,
                headers={
                    'Cache-Control': 'public, max-age=86400',  # Cache for 1 day
                    'Access-Control-Allow-Origin': '*',
                    'Content-Disposition': f'inline; filename="{subtitle_file.filename}"'
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
                'url': f"/subtitle/{video_id}/{subtitle_file.language}.{subtitle_file.file_type}"
            })

        return web.json_response({
            'video_id': video_id,
            'subtitles': subtitles_data
        })

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
            f"# Generated for {playlist_type} access",
            f"# Video ID: {video_id}",
            f"# Base URL: {base_url}",
            ""
        ]

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

    async def serve_segment(self, request: web.Request):
        """Handles requests for individual video segments with configurable caching."""
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']

        # Try cache first
        cached_data = await self.cache_manager.get_cached_segment(video_id, segment_name)
        if cached_data:
            return web.Response(
                body=cached_data,
                content_type='video/mp2t',
                headers={
                    'Cache-Control': 'public, max-age=31536000',  # Cache for 1 year
                    'Access-Control-Allow-Origin': '*',
                    'X-Cache': 'HIT',
                    'X-Cache-Type': self.cache_type
                }
            )

        # Get segment info from database
        segments = await self.db.get_video_segments(video_id)
        segment_info = segments.get(segment_name)

        if not segment_info:
            return web.Response(status=404, text="Segment not found")

        # Download from Telegram using the configured handler
        if not self.telegram_handler:
            logger.error("No telegram handler available for segment download")
            return web.Response(status=500, text="Telegram handler not available")

        segment_bytes = await self.telegram_handler.download_segment_from_telegram(segment_info.file_id)

        if segment_bytes:
            # Cache the segment for future requests
            await self.cache_manager.cache_segment(video_id, segment_name, segment_bytes)

            return web.Response(
                body=segment_bytes,
                content_type='video/mp2t',
                headers={
                    'Cache-Control': 'public, max-age=31536000',  # Cache for 1 year
                    'Access-Control-Allow-Origin': '*',
                    'X-Cache': 'MISS',
                    'X-Cache-Type': self.cache_type
                }
            )

        return web.Response(status=500, text="Failed to retrieve segment")

    async def serve_cache_stats(self, request: web.Request):
        """Endpoint to view cache statistics."""
        stats = await self.cache_manager.get_cache_stats()
        return web.json_response(stats)

    async def clear_cache_endpoint(self, request: web.Request):
        """Endpoint to clear cache."""
        video_id = request.query.get('video_id')
        await self.cache_manager.clear_cache(video_id)
        return web.json_response({'success': True, 'message': f'Cache cleared{"for video " + video_id if video_id else " (all)"}'})

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

    async def start(self):
        """Starts the aiohttp web server with HTTPS support and subtitle functionality."""
        # Initialize database and cache manager at server startup
        logger.info("Initializing database and cache manager...")
        await self.db.initialize_database()
        await self.cache_manager.initialize()
        logger.info("Database and cache manager initialized successfully")

        # Determine protocol before creating playlists
        self.protocol = self._determine_protocol()

        # Get max upload size from environment or use default
        max_upload_size = int(os.getenv('MAX_UPLOAD_SIZE', 50 * 1024**3))

        app = web.Application(client_max_size=max_upload_size)
        aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))

        # Routes
        app.router.add_get('/', self.index)
        app.router.add_post('/process', self.handle_process_request)
        app.router.add_get('/status/{task_id}', self.handle_status_request)

        # Playlist routes (with access type)
        app.router.add_get('/playlist/{access_type}/{video_id}.m3u8', self.serve_playlist)
        # Backwards compatibility route (defaults to local)
        app.router.add_get('/playlist/{video_id}.m3u8', self.serve_playlist)

        # Segment route
        app.router.add_get('/segment/{video_id}/{segment_name}', self.serve_segment)

        # Subtitle routes
        app.router.add_get('/subtitle/{video_id}/{language}.{ext}', self.serve_subtitle)
        app.router.add_get('/subtitle/{video_id}/{language}', self.serve_subtitle)
        app.router.add_get('/subtitle/{video_id}', self.serve_subtitle)  # Default language
        app.router.add_get('/subtitles/{video_id}', self.list_subtitles)  # List available subtitles

        # Cache management routes
        app.router.add_get('/cache/stats', self.serve_cache_stats)
        app.router.add_post('/cache/clear', self.clear_cache_endpoint)

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

        # Log server startup information
        logger.info(f"🚀 Streaming server started at {self.protocol}://{self.host}:{self.port}")
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

        # Log cache info
        try:
            cache_stats = await self.cache_manager.get_cache_stats()
            cache_size_mb = cache_stats.get('max_cache_size', 500 * 1024 * 1024) / (1024*1024)
            cache_type = cache_stats.get('cache_type', self.cache_type)
            logger.info(f"💾 Cache: {cache_type.upper()}, {cache_size_mb:.0f}MB limit, {cache_stats.get('total_items', 0)} items cached")
        except Exception as e:
            logger.warning(f"Could not display cache stats: {e}")

        # Log multi-bot information if applicable
        multi_bot_info = self._get_multi_bot_info()
        if multi_bot_info['enabled']:
            logger.info(f"🤖 Multi-bot upload: {multi_bot_info['handler_type']} (~{multi_bot_info['estimated_speedup']}x speedup)")
        else:
            logger.info("📱 Single bot upload mode")

        logger.info("📄 Subtitle support enabled - subtitles will be extracted and served automatically")
