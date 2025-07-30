"""
Route definitions for the Telegram HLS Streamer web server.
"""

from aiohttp import web
from .handlers import RequestHandlers


def setup_routes(app: web.Application, handlers: RequestHandlers):
    """Setup all routes for the web application."""
    
    # Main interface
    app.router.add_get('/', handlers.index)
    
    # Video processing routes
    app.router.add_post('/process', handlers.handle_process_request)
    app.router.add_get('/status/{task_id}', handlers.handle_status_request)
    
    # Batch processing routes
    app.router.add_post('/batch-process', handlers.handle_batch_process_request)
    app.router.add_get('/batch-status/{task_id}', handlers.handle_batch_status_request)
    
    # Playlist routes (with access type)
    app.router.add_get('/playlist/{access_type}/{video_id}.m3u8', handlers.serve_playlist)
    # Backwards compatibility route (defaults to local)
    app.router.add_get('/playlist/{video_id}.m3u8', handlers.serve_playlist)
    
    # Enhanced segment route with predictive caching
    app.router.add_get('/segment/{video_id}/{segment_name}', handlers.serve_segment)
    
    # Subtitle routes
    app.router.add_get('/subtitle/{video_id}/{language}.{ext}', handlers.serve_subtitle)
    app.router.add_get('/subtitle/{video_id}/{language}', handlers.serve_subtitle)
    app.router.add_get('/subtitle/{video_id}', handlers.serve_subtitle)  # Default language
    app.router.add_get('/subtitles/{video_id}', handlers.list_subtitles)  # List available subtitles
    
    # Basic cache management routes (always available)
    app.router.add_get('/cache/stats', handlers.serve_cache_stats)
    app.router.add_post('/cache/clear', handlers.clear_cache_endpoint)
    
    # Enhanced API endpoints for the web interface
    app.router.add_get('/api/system-stats', handlers.api_system_stats)
    app.router.add_get('/api/database-stats', handlers.api_database_stats)
    app.router.add_get('/api/settings', handlers.api_get_settings)
    app.router.add_post('/api/settings', handlers.api_save_settings)
    app.router.add_get('/api/videos', handlers.api_get_all_videos)
    
    # Telegram Configuration API endpoints
    app.router.add_get('/api/telegram-config', handlers.api_get_telegram_config)
    app.router.add_post('/api/telegram-config', handlers.api_save_telegram_config)
    app.router.add_post('/api/telegram-test', handlers.api_test_telegram_bot)


def setup_predictive_routes(app: web.Application, handlers: RequestHandlers):
    """Setup routes that require predictive cache manager."""
    # Predictive caching routes (only if predictive cache manager is available)
    app.router.add_post('/cache/preload', handlers.force_preload_endpoint)
    app.router.add_get('/sessions', handlers.list_active_sessions)
    app.router.add_get('/analytics/{video_id}', handlers.get_video_analytics)
    app.router.add_get('/analytics', handlers.get_video_analytics)  # All videos