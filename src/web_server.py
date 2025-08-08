"""
Web server implementation using aiohttp.
Provides REST API and HLS streaming endpoints.
"""

import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
import uuid

from aiohttp import web, web_request, web_response, hdrs
import aiohttp_cors
import aiofiles
import aiofiles.os


class WebServer:
    """Main web server class handling all HTTP endpoints."""
    
    def __init__(self, config, db_manager, telegram_manager, video_processor, cache_manager):
        self.config = config
        self.db_manager = db_manager
        self.telegram_manager = telegram_manager
        self.video_processor = video_processor
        self.cache_manager = cache_manager
        self.logger = logging.getLogger(__name__)
        
        self.app = None
        self.runner = None
        self.site = None
        
        # Upload tracking
        self.active_uploads: Dict[str, Dict[str, Any]] = {}
        
    async def create_app(self) -> web.Application:
        """Create and configure the web application."""
        app = web.Application(
            client_max_size=self.config.max_upload_size,
            middlewares=[self._error_middleware, self._cors_middleware]
        )
        
        # API routes
        app.router.add_post('/api/upload', self._handle_upload)
        app.router.add_get('/api/videos', self._handle_get_videos)
        app.router.add_get('/api/videos/{video_id}', self._handle_get_video)
        app.router.add_delete('/api/videos/{video_id}', self._handle_delete_video)
        app.router.add_get('/api/videos/{video_id}/status', self._handle_video_status)
        # app.router.add_post('/api/videos/{video_id}/process', self._handle_process_video)  # Not implemented yet
        
        # HLS streaming routes
        app.router.add_get('/hls/{video_id}/master.m3u8', self._handle_master_playlist)
        app.router.add_get('/hls/{video_id}/{track_type}/{track_name}/playlist.m3u8', self._handle_track_playlist)
        app.router.add_get('/hls/{video_id}/segments/{segment_name}', self._handle_segment)
        
        # System API routes
        app.router.add_get('/api/system/status', self._handle_system_status)
        app.router.add_get('/api/system/cache/stats', self._handle_cache_stats)
        app.router.add_post('/api/system/cache/clear', self._handle_cache_clear)
        app.router.add_get('/api/system/bots/status', self._handle_bot_status)
        app.router.add_post('/api/system/bots/test', self._handle_bot_test)
        
        # Configuration routes
        app.router.add_get('/api/config', self._handle_get_config)
        
        # Index route (must come before static routing)
        app.router.add_get('/', self._handle_index)
        
        # Static file serving for web interface
        static_dir = Path(__file__).parent.parent / 'web'
        if static_dir.exists():
            app.router.add_static('/static', static_dir, name='static')
        
        self.app = app
        return app
        
    async def start(self):
        """Start the web server."""
        await self.create_app()
        
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        # Setup SSL if configured (only if not behind proxy)
        ssl_context = None
        if (self.config.force_https and not self.config.behind_proxy and 
            self.config.ssl_cert_path and self.config.ssl_key_path):
            import ssl
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self.config.ssl_cert_path, self.config.ssl_key_path)
            
        self.site = web.TCPSite(
            self.runner,
            self.config.local_host,
            self.config.local_port,
            ssl_context=ssl_context
        )
        
        await self.site.start()
        self.logger.info(f"Web server started on {self.config.local_host}:{self.config.local_port}")
        
    async def stop(self):
        """Stop the web server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
            
    # Middleware
    async def _error_middleware(self, app, handler):
        """Error handling middleware."""
        async def middleware_handler(request):
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as e:
                self.logger.error(f"Unhandled error in {request.path}: {e}")
                return web.json_response(
                    {'error': 'Internal server error', 'message': str(e)},
                    status=500
                )
        
        return middleware_handler
            
    async def _cors_middleware(self, app, handler):
        """CORS middleware."""
        async def middleware_handler(request):
            response = await handler(request)
            
            # Add CORS headers
            origin = request.headers.get('Origin')
            if origin and (origin in self.config.allowed_origins or '*' in self.config.allowed_origins):
                response.headers['Access-Control-Allow-Origin'] = origin
            else:
                response.headers['Access-Control-Allow-Origin'] = '*'
                
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
            response.headers['Access-Control-Max-Age'] = '86400'
            
            return response
        
        return middleware_handler
        
    # Static file handlers
    async def _handle_index(self, request: web_request.Request):
        """Serve the main index page."""
        static_dir = Path(__file__).parent.parent / 'web'
        index_file = static_dir / 'index.html'
        
        if index_file.exists():
            async with aiofiles.open(index_file, 'r') as f:
                content = await f.read()
            return web.Response(text=content, content_type='text/html')
        else:
            # Return a simple default page if no web interface is built yet
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Telegram HLS Streamer</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }
                    .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }
                    h1 { color: #333; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }
                    .status { background: #e8f5e8; padding: 15px; border-radius: 5px; margin: 20px 0; }
                    .api-link { background: #f0f8ff; padding: 10px; margin: 5px 0; border-radius: 5px; }
                    code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; }
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>🚀 Telegram HLS Streamer</h1>
                    <div class="status">
                        <strong>✅ Server is running!</strong><br>
                        Your Telegram HLS streaming server is operational.
                    </div>
                    
                    <h2>📡 API Endpoints</h2>
                    <div class="api-link"><strong>System Status:</strong> <code>GET /api/system/status</code></div>
                    <div class="api-link"><strong>Upload Video:</strong> <code>POST /api/upload</code></div>
                    <div class="api-link"><strong>List Videos:</strong> <code>GET /api/videos</code></div>
                    <div class="api-link"><strong>Bot Status:</strong> <code>GET /api/system/bots/status</code></div>
                    <div class="api-link"><strong>Cache Stats:</strong> <code>GET /api/system/cache/stats</code></div>
                    
                    <h2>🎥 HLS Streaming</h2>
                    <p>Once you upload a video, HLS playlists will be available at:</p>
                    <div class="api-link"><code>/hls/{video_id}/master.m3u8</code></div>
                    
                    <h2>📖 Next Steps</h2>
                    <ol>
                        <li>Test your bot configuration: <code>python main.py test-bots</code></li>
                        <li>Upload a video using the API or build the web interface</li>
                        <li>Stream your videos using any HLS-compatible player</li>
                    </ol>
                </div>
            </body>
            </html>
            """
            return web.Response(text=html, content_type='text/html')
            
    # Upload handlers
    async def _handle_upload(self, request: web_request.Request):
        """Handle video file upload."""
        try:
            # Check if multipart form data
            if not request.content_type.startswith('multipart/form-data'):
                return web.json_response(
                    {'error': 'Content-Type must be multipart/form-data'},
                    status=400
                )
                
            reader = await request.multipart()
            
            video_file = None
            video_title = None
            
            # Process form fields
            async for field in reader:
                if field.name == 'file':
                    video_file = field
                elif field.name == 'title':
                    video_title = await field.text()
                    
            if not video_file:
                return web.json_response(
                    {'error': 'No video file provided'},
                    status=400
                )
                
            # Generate upload ID
            upload_id = str(uuid.uuid4())
            filename = video_file.filename or f"upload_{upload_id}"
            video_title = video_title or Path(filename).stem
            
            # Save file to upload directory
            upload_path = self.config.upload_dir / f"{upload_id}_{filename}"
            
            self.active_uploads[upload_id] = {
                'filename': filename,
                'title': video_title,
                'status': 'uploading',
                'progress': 0.0,
                'size': 0,
                'start_time': time.time()
            }
            
            # Stream file to disk
            total_size = 0
            async with aiofiles.open(upload_path, 'wb') as f:
                async for chunk in video_file.iter_chunked(8192):
                    await f.write(chunk)
                    total_size += len(chunk)
                    
                    # Update progress
                    self.active_uploads[upload_id]['size'] = total_size
                    
            # Start processing
            self.active_uploads[upload_id]['status'] = 'processing'
            
            # Process video asynchronously
            asyncio.create_task(self._process_uploaded_video(
                upload_id, upload_path, video_title
            ))
            
            return web.json_response({
                'upload_id': upload_id,
                'filename': filename,
                'title': video_title,
                'size': total_size,
                'status': 'processing'
            })
            
        except Exception as e:
            self.logger.error(f"Upload error: {e}")
            return web.json_response(
                {'error': 'Upload failed', 'message': str(e)},
                status=500
            )
            
    async def _process_uploaded_video(self, upload_id: str, file_path: Path, title: str):
        """Process uploaded video in background."""
        try:
            video_id = await self.video_processor.process_video(file_path, title)
            
            self.active_uploads[upload_id].update({
                'status': 'completed',
                'video_id': video_id,
                'progress': 100.0
            })
            
            # Clean up upload file
            if file_path.exists():
                await aiofiles.os.remove(file_path)
                
        except Exception as e:
            self.logger.error(f"Video processing failed for {upload_id}: {e}")
            self.active_uploads[upload_id].update({
                'status': 'error',
                'error': str(e)
            })
            
    # Video management handlers
    async def _handle_get_videos(self, request: web_request.Request):
        """Get list of all videos."""
        try:
            status_filter = request.query.get('status')
            videos = await self.db_manager.get_all_videos(status_filter)
            return web.json_response({'videos': videos})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_get_video(self, request: web_request.Request):
        """Get specific video information."""
        try:
            video_id = request.match_info['video_id']
            video = await self.db_manager.get_video(video_id)
            
            if not video:
                return web.json_response({'error': 'Video not found'}, status=404)
                
            # Get additional info
            streams = await self.db_manager.get_video_streams(video_id)
            segments = await self.db_manager.get_segments_by_video(video_id)
            
            video['streams'] = streams
            video['segments_count'] = len(segments)
            
            return web.json_response({'video': video})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_delete_video(self, request: web_request.Request):
        """Delete a video and its segments."""
        try:
            video_id = request.match_info['video_id']
            success = await self.db_manager.delete_video(video_id)
            
            if success:
                return web.json_response({'message': 'Video deleted successfully'})
            else:
                return web.json_response({'error': 'Failed to delete video'}, status=500)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_video_status(self, request: web_request.Request):
        """Get video processing status."""
        try:
            video_id = request.match_info['video_id']
            
            # Check if it's an upload ID
            if video_id in self.active_uploads:
                return web.json_response(self.active_uploads[video_id])
                
            # Check processing job status
            job_status = self.video_processor.get_job_status(video_id)
            if job_status:
                return web.json_response(job_status)
                
            # Check database
            video = await self.db_manager.get_video(video_id)
            if video:
                return web.json_response({
                    'status': video['status'],
                    'video_id': video_id
                })
            else:
                return web.json_response({'error': 'Video not found'}, status=404)
                
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    # HLS streaming handlers
    async def _handle_master_playlist(self, request: web_request.Request):
        """Serve master HLS playlist."""
        try:
            video_id = request.match_info['video_id']
            
            # Get master playlist from database
            playlist_content = await self.db_manager.get_playlist(video_id, "master")
            
            if not playlist_content:
                return web.json_response({'error': 'Playlist not found'}, status=404)
                
            return web.Response(
                text=playlist_content,
                content_type='application/x-mpegURL',
                headers={
                    'Cache-Control': 'no-cache',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            
        except Exception as e:
            self.logger.error(f"Master playlist error: {e}")
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_track_playlist(self, request: web_request.Request):
        """Serve track-specific playlist."""
        try:
            video_id = request.match_info['video_id']
            track_type = request.match_info['track_type']  # video/audio
            track_name = request.match_info['track_name']  # quality/language
            
            playlist_content = await self.db_manager.get_playlist(
                video_id, track_type, track_name
            )
            
            if not playlist_content:
                return web.json_response({'error': 'Playlist not found'}, status=404)
                
            return web.Response(
                text=playlist_content,
                content_type='application/x-mpegURL',
                headers={
                    'Cache-Control': 'max-age=30',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_segment(self, request: web_request.Request):
        """Serve HLS segment with caching."""
        try:
            video_id = request.match_info['video_id']
            segment_name = request.match_info['segment_name']
            
            start_time = time.time()
            
            # Try cache first
            cached_data = await self.cache_manager.get(segment_name)
            
            if cached_data:
                # Cache hit
                response_time = time.time() - start_time
                await self.db_manager.record_cache_hit(segment_name, response_time, len(cached_data))
                
                # Predict and preload next segments
                next_segments = await self.cache_manager.predict_next_segments(segment_name, video_id)
                if next_segments:
                    def fetch_func_factory(seg_name):
                        async def fetch():
                            _, data, _ = await self.telegram_manager.download_segment(seg_name)
                            return data
                        return fetch
                        
                    await self.cache_manager.preload_segments(next_segments, fetch_func_factory)
                
                return web.Response(
                    body=cached_data,
                    content_type='video/MP2T',
                    headers={
                        'Cache-Control': 'max-age=86400',  # 24 hours
                        'Access-Control-Allow-Origin': '*',
                        'X-Cache-Status': 'HIT'
                    }
                )
            else:
                # Cache miss - download from Telegram
                await self.db_manager.record_cache_miss(segment_name)
                
                success, data, error = await self.telegram_manager.download_segment(segment_name)
                
                if not success:
                    self.logger.error(f"Failed to download segment {segment_name}: {error}")
                    return web.json_response({'error': f'Segment not found: {error}'}, status=404)
                    
                # Cache the segment
                await self.cache_manager.put(segment_name, data)
                
                response_time = time.time() - start_time
                await self.db_manager.record_cache_hit(segment_name, response_time, len(data))
                
                return web.Response(
                    body=data,
                    content_type='video/MP2T',
                    headers={
                        'Cache-Control': 'max-age=86400',
                        'Access-Control-Allow-Origin': '*',
                        'X-Cache-Status': 'MISS'
                    }
                )
                
        except Exception as e:
            self.logger.error(f"Segment serving error: {e}")
            return web.json_response({'error': str(e)}, status=500)
            
    # System API handlers
    async def _handle_system_status(self, request: web_request.Request):
        """Get system status."""
        try:
            # Get various system stats
            db_stats = await self.db_manager.get_database_stats()
            cache_stats = self.cache_manager.get_cache_stats()
            bot_health = await self.telegram_manager.health_check()
            
            return web.json_response({
                'server_status': 'running',
                'uptime': time.time(),  # Would track actual uptime
                'database': db_stats,
                'cache': cache_stats,
                'bots': bot_health,
                'active_uploads': len(self.active_uploads),
                'config': {
                    'version': '1.0.0',
                    'cache_type': self.config.cache_type,
                    'hardware_accel': self.config.ffmpeg_hardware_accel,
                    'max_upload_size_gb': self.config.max_upload_size // (1024**3)
                }
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_cache_stats(self, request: web_request.Request):
        """Get cache statistics."""
        try:
            stats = self.cache_manager.get_cache_stats()
            return web.json_response({'cache_stats': stats})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_cache_clear(self, request: web_request.Request):
        """Clear cache."""
        try:
            await self.cache_manager.clear_cache()
            return web.json_response({'message': 'Cache cleared successfully'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_bot_status(self, request: web_request.Request):
        """Get bot status and statistics."""
        try:
            bot_stats = await self.telegram_manager.get_bot_stats()
            bot_health = await self.telegram_manager.health_check()
            
            return web.json_response({
                'bot_stats': bot_stats,
                'bot_health': bot_health
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_bot_test(self, request: web_request.Request):
        """Test bot connectivity."""
        try:
            results = await self.telegram_manager.test_all_bots()
            return web.json_response({'test_results': results})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
            
    async def _handle_get_config(self, request: web_request.Request):
        """Get current configuration (sanitized)."""
        try:
            config_dict = self.config.to_dict()
            # Remove sensitive information
            if 'bots' in config_dict:
                for bot in config_dict['bots'].get('configs', []):
                    if 'token_preview' in bot:
                        # Already sanitized in config.to_dict()
                        pass
            return web.json_response({'config': config_dict})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)