"""
Main web server for the Telegram HLS Streamer.
"""

import asyncio
import os
import ssl
from pathlib import Path
from aiohttp import web
import aiohttp_jinja2
import jinja2

from .handlers import RequestHandlers
from .routes import setup_routes, setup_predictive_routes
from ..core.config import get_config
from ..core.exceptions import StreamingError
from ..storage.database import DatabaseManager
from ..processing.cache_manager import create_cache_manager, create_predictive_cache_manager
from ..utils.networking import get_local_ip, is_valid_domain
from ..utils.logging import logger


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

        # Telegram handler will be set by main app
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
        
        # Server components
        self.app = None
        self.runner = None
        self.site = None
        self.handlers = None

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
    
    def set_telegram_handler(self, handler):
        """Set the telegram handler and initialize predictive cache manager."""
        self.telegram_handler = handler
        
        # Initialize predictive cache manager now that we have telegram handler
        if self.preload_segments > 0:
            self.predictive_cache_manager = create_predictive_cache_manager(
                self.db,
                handler,
                cache_manager=self.cache_manager,
                preload_segments=self.preload_segments,
                max_concurrent_preloads=self.max_concurrent_preloads
            )
            logger.info("Predictive cache manager initialized")
        
        # Set handler on request handlers if they exist
        if self.handlers:
            self.handlers.set_telegram_handler(handler)
    
    @web.middleware
    async def _cors_middleware(self, request, handler):
        """CORS middleware to handle cross-origin requests for streaming."""
        # Handle OPTIONS preflight requests
        if request.method == 'OPTIONS':
            response = web.Response()
        else:
            try:
                response = await handler(request)
            except Exception as e:
                # For streaming endpoints, we still want CORS headers on errors
                response = web.Response(status=500, text=str(e))
        
        # Add CORS headers for all responses
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Range'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Range, Accept-Ranges, Content-Length'
        
        # Special headers for video streaming
        if request.path.startswith('/playlist/') or request.path.startswith('/segment/'):
            response.headers['Access-Control-Allow-Credentials'] = 'false'
            response.headers['Cross-Origin-Resource-Policy'] = 'cross-origin'
            
        return response
    
    async def start_server(self, max_upload_size: int = 50 * 1024**3):
        """Start the web server."""
        try:
            logger.info("🌐 Starting web server...")
            
            # Create aiohttp application
            self.app = web.Application(client_max_size=max_upload_size)
            
            # Add CORS middleware for public domain access
            self.app.middlewares.append(self._cors_middleware)
            
            # Setup Jinja2 templates
            aiohttp_jinja2.setup(self.app, loader=jinja2.FileSystemLoader('templates'))
            
            # Create request handlers
            self.handlers = RequestHandlers(self)
            if self.telegram_handler:
                self.handlers.set_telegram_handler(self.telegram_handler)
            
            # Setup routes
            setup_routes(self.app, self.handlers)
            
            # Setup predictive routes if available
            if self.predictive_cache_manager:
                setup_predictive_routes(self.app, self.handlers)
                logger.info("Predictive caching routes enabled")
            
            # Create runner
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            
            # Setup SSL context if certificates are provided
            ssl_context = None
            direct_ssl = False
            
            if (not self.force_https and self.ssl_cert_path and self.ssl_key_path and
                os.path.exists(self.ssl_cert_path) and os.path.exists(self.ssl_key_path)):
                try:
                    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    ssl_context.load_cert_chain(self.ssl_cert_path, self.ssl_key_path)
                    self.protocol = "https"
                    direct_ssl = True
                    logger.info(f"✅ SSL enabled with certificate: {self.ssl_cert_path}")
                except Exception as e:
                    logger.error(f"❌ SSL setup failed: {e}")
                    logger.info("🔄 Falling back to HTTP")
                    ssl_context = None
            elif self.force_https:
                self.protocol = "https"
                logger.info("✅ HTTPS mode enabled (reverse proxy)")
            
            # Create and start site
            logger.info(f"🔧 Creating TCPSite with host='{self.host}', port={self.port}")
            self.site = web.TCPSite(self.runner, self.host, self.port, ssl_context=ssl_context)
            logger.info("🔧 Starting TCPSite...")
            await self.site.start()
            logger.info("✅ TCPSite started successfully")
            
            # Start cleanup task
            self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
            
            # Log server information
            if self.public_domain and is_valid_domain(self.public_domain):
                # For forced HTTPS (reverse proxy), don't show port since proxy handles standard HTTPS port 443
                if self.force_https and self.public_domain:
                    port_suffix = ""  # Reverse proxy handles HTTPS on standard port 443
                else:
                    port_suffix = "" if (self.protocol == "https" and self.port == 443) or (self.protocol == "http" and self.port == 80) else f":{self.port}"
                public_url = f"{self.protocol}://{self.public_domain}{port_suffix}"
                logger.info(f"🌍 Public URL: {public_url}")
            
            local_url = f"http://{self.local_ip}:{self.port}"
            logger.info(f"🏠 Local URL: {local_url}")
            
            if direct_ssl:
                logger.info("🔒 Direct SSL encryption enabled")
            elif self.force_https:
                logger.info("🔄 HTTPS via reverse proxy expected")
            
            logger.info("✅ Web server started successfully!")
            
        except Exception as e:
            logger.error(f"❌ Failed to start web server: {e}")
            await self.stop_server()
            raise StreamingError(f"Server startup failed: {e}")
    
    async def stop_server(self):
        """Stop the web server and cleanup resources."""
        try:
            logger.info("🛑 Stopping web server...")
            
            # Stop cleanup task
            if self.cleanup_task:
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass
            
            # Stop site
            if self.site:
                await self.site.stop()
                self.site = None
            
            # Cleanup runner
            if self.runner:
                await self.runner.cleanup()
                self.runner = None
            
            # Cleanup cache managers
            if self.predictive_cache_manager:
                await self.predictive_cache_manager.cleanup()
                self.predictive_cache_manager = None
            
            if self.cache_manager:
                await self.cache_manager.cleanup()
                self.cache_manager = None
            
            self.app = None
            self.handlers = None
            
            logger.info("✅ Web server stopped successfully")
            
        except Exception as e:
            logger.error(f"❌ Error stopping server: {e}")
    
    def is_running(self) -> bool:
        """Check if the server is running."""
        return self.site is not None and not self.site._server.is_closed() if self.site else False
    
    async def _periodic_cleanup(self):
        """Periodic cleanup task for expired sessions and cache."""
        try:
            while True:
                await asyncio.sleep(300)  # Run every 5 minutes
                
                try:
                    # Cleanup expired cache entries
                    if self.cache_manager:
                        await self.cache_manager.cleanup_expired()
                    
                    # Cleanup expired sessions
                    if self.predictive_cache_manager:
                        await self.predictive_cache_manager.cleanup_expired_sessions()
                    
                    logger.debug("🧹 Periodic cleanup completed")
                    
                except Exception as e:
                    logger.warning(f"⚠️ Cleanup task error: {e}")
                    
        except asyncio.CancelledError:
            logger.debug("🛑 Cleanup task cancelled")
        except Exception as e:
            logger.error(f"❌ Cleanup task failed: {e}")
    
    def get_server_info(self) -> dict:
        """Get server information."""
        config = get_config()
        
        return {
            'host': self.host,
            'port': self.port,
            'local_ip': self.local_ip,
            'public_domain': self.public_domain,
            'protocol': self.protocol,
            'force_https': self.force_https,
            'ssl_enabled': bool(self.ssl_cert_path and self.ssl_key_path),
            'cache_type': self.cache_type,
            'cache_size_mb': self.cache_size // (1024 * 1024),
            'predictive_cache_enabled': self.predictive_cache_manager is not None,
            'preload_segments': self.preload_segments,
            'running': self.is_running(),
            'multi_bot_count': len(config.multi_bot_tokens) if config else 0
        }
    
    async def get_status(self) -> dict:
        """Get detailed server status."""
        status = {
            'server_info': self.get_server_info(),
            'is_running': self.is_running(),
            'telegram_handler_available': self.telegram_handler is not None,
            'cache_manager_available': self.cache_manager is not None,
            'predictive_cache_available': self.predictive_cache_manager is not None
        }
        
        # Add cache statistics if available
        if self.cache_manager:
            try:
                status['cache_stats'] = await self.cache_manager.get_cache_stats()
            except Exception as e:
                status['cache_stats'] = {'error': str(e)}
        
        # Add predictive cache statistics if available
        if self.predictive_cache_manager:
            try:
                status['predictive_cache_stats'] = await self.predictive_cache_manager.get_cache_stats()
            except Exception as e:
                status['predictive_cache_stats'] = {'error': str(e)}
        
        return status
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop_server()