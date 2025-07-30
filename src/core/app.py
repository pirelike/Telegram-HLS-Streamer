"""
Main application orchestrator for Telegram HLS Streamer.
"""

import asyncio
import logging
from typing import Optional
from .config import Config, get_config
from .exceptions import ConfigurationError, TelegramHLSError
from ..storage.database import DatabaseManager
from ..telegram.handler import TelegramHandler
from ..web.server import StreamServer
from ..processing.cache_manager import create_cache_manager, create_predictive_cache_manager

logger = logging.getLogger(__name__)


class TelegramHLSApp:
    """Main application class that orchestrates all components."""
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.db_manager: Optional[DatabaseManager] = None
        self.telegram_handler: Optional[TelegramHandler] = None
        self.stream_server: Optional[StreamServer] = None
        self.cache_manager = None
        self.predictive_cache_manager = None
        
    async def initialize(self):
        """Initialize all application components."""
        try:
            # Setup directories and logging
            self.config.setup_directories()
            self.config.setup_logging()
            
            logger.info("🚀 Initializing Telegram HLS Streamer...")
            
            # Initialize database
            self.db_manager = DatabaseManager(
                db_path=self.config.database_path
            )
            await self.db_manager.initialize_database()
            logger.info("✅ Database initialized")
            
            # Initialize cache manager
            self.cache_manager = create_cache_manager(
                self.db_manager,
                cache_type=self.config.cache_type,
                cache_dir=self.config.cache_dir,
                max_cache_size=self.config.cache_size
            )
            logger.info(f"✅ Cache manager initialized ({self.config.cache_type})")
            
            # Initialize Telegram handler
            from ..telegram.handler import create_round_robin_handler
            self.telegram_handler = create_round_robin_handler(self.db_manager)
            logger.info("✅ Telegram handler initialized")
            
            # Initialize predictive cache manager if enabled
            if self.config.preload_segments > 0:
                self.predictive_cache_manager = create_predictive_cache_manager(
                    self.db_manager,
                    self.telegram_handler,
                    cache_type=self.config.cache_type,
                    cache_dir=self.config.cache_dir,
                    max_cache_size=self.config.cache_size,
                    preload_segments=self.config.preload_segments,
                    max_concurrent_preloads=self.config.max_concurrent_preloads
                )
                logger.info("✅ Predictive cache manager initialized")
            
            # Initialize stream server
            self.stream_server = StreamServer(
                host=self.config.local_host,
                port=self.config.local_port,
                db_manager=self.db_manager,
                bot_token=self.config.bot_token or '',
                chat_id=self.config.chat_id or '',
                public_domain=self.config.public_domain,
                playlists_dir=self.config.playlists_dir,
                ssl_cert_path=self.config.ssl_cert_path,
                ssl_key_path=self.config.ssl_key_path,
                cache_size=self.config.cache_size,
                force_https=self.config.force_https,
                cache_type=self.config.cache_type,
                preload_segments=self.config.preload_segments,
                max_concurrent_preloads=self.config.max_concurrent_preloads
            )
            
            # Set telegram handler on stream server
            self.stream_server.telegram_handler = self.telegram_handler
            
            # Set predictive cache manager if available
            if self.predictive_cache_manager:
                self.stream_server.predictive_cache_manager = self.predictive_cache_manager
            
            logger.info("✅ Stream server initialized")
            logger.info("🎉 All components initialized successfully!")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize application: {e}")
            raise TelegramHLSError(f"Application initialization failed: {e}")
    
    async def start_server(self):
        """Start the web server."""
        if not self.stream_server:
            raise TelegramHLSError("Application not initialized. Call initialize() first.")
        
        try:
            logger.info("🌐 Starting web server...")
            await self.stream_server.start_server(
                max_upload_size=self.config.max_upload_size
            )
        except Exception as e:
            logger.error(f"❌ Failed to start server: {e}")
            raise TelegramHLSError(f"Server startup failed: {e}")
    
    async def stop_server(self):
        """Stop the web server."""
        if self.stream_server:
            try:
                logger.info("🛑 Stopping web server...")
                await self.stream_server.stop_server()
                logger.info("✅ Web server stopped")
            except Exception as e:
                logger.error(f"❌ Error stopping server: {e}")
    
    async def cleanup(self):
        """Cleanup all resources."""
        try:
            logger.info("🧹 Cleaning up resources...")
            
            # Stop server
            await self.stop_server()
            
            # Cleanup database
            if self.db_manager:
                await self.db_manager.cleanup()
                logger.info("✅ Database cleaned up")
            
            # Cleanup cache managers
            if self.predictive_cache_manager:
                await self.predictive_cache_manager.cleanup()
                logger.info("✅ Predictive cache manager cleaned up")
            
            if self.cache_manager:
                await self.cache_manager.cleanup()
                logger.info("✅ Cache manager cleaned up")
            
            logger.info("🎉 Cleanup completed successfully!")
            
        except Exception as e:
            logger.error(f"❌ Error during cleanup: {e}")
    
    def get_status(self) -> dict:
        """Get application status."""
        return {
            'initialized': all([
                self.db_manager is not None,
                self.telegram_handler is not None,
                self.stream_server is not None,
                self.cache_manager is not None
            ]),
            'server_running': self.stream_server.is_running() if self.stream_server else False,
            'database_connected': self.db_manager.is_connected() if self.db_manager else False,
            'cache_type': self.config.cache_type,
            'predictive_cache_enabled': self.predictive_cache_manager is not None,
            'multi_bot_count': len(self.config.multi_bot_tokens),
            'hardware_acceleration': self.config.ffmpeg_hardware_accel
        }
    
    async def test_bots(self):
        """Test all configured bots."""
        if not self.telegram_handler:
            raise TelegramHLSError("Application not initialized. Call initialize() first.")
        
        return await self.telegram_handler.test_all_bots()
    
    async def __aenter__(self):
        """Async context manager entry."""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.cleanup()