#!/usr/bin/env python3
"""
Telegram HLS Streamer - Main Application Entry Point
Turn Telegram into your unlimited personal Netflix!
"""

import asyncio
import sys
import argparse
import logging
import os
import signal
from pathlib import Path
from typing import Optional

from src.config import Config
from src.database import DatabaseManager
from src.telegram_handler import TelegramManager
from src.video_processor import VideoProcessor
from src.web_server import WebServer
from src.cache_manager import CacheManager


class TelegramHLSStreamer:
    """Main application orchestrator for the Telegram HLS Streaming server."""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config = Config(config_path)
        self.db_manager = None
        self.telegram_manager = None
        self.video_processor = None
        self.web_server = None
        self.cache_manager = None
        self.running = False
        
        self.setup_logging()
        
    def setup_logging(self):
        """Configure application logging."""
        log_level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('telegram-hls.log'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("Telegram HLS Streamer starting up...")
        
    async def initialize_components(self):
        """Initialize all application components."""
        try:
            self.logger.info("Initializing database...")
            self.db_manager = DatabaseManager(self.config.database_path)
            await self.db_manager.initialize()
            
            self.logger.info("Initializing cache manager...")
            self.cache_manager = CacheManager(self.config)
            
            self.logger.info("Initializing Telegram manager...")
            self.telegram_manager = TelegramManager(self.config, self.db_manager)
            await self.telegram_manager.initialize()
            
            self.logger.info("Initializing video processor...")
            self.video_processor = VideoProcessor(
                self.config,
                self.db_manager,
                self.telegram_manager,
                self.cache_manager
            )
            
            self.logger.info("Initializing web server...")
            self.web_server = WebServer(
                self.config,
                self.db_manager,
                self.telegram_manager,
                self.video_processor,
                self.cache_manager
            )
            
            self.logger.info("All components initialized successfully!")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize components: {e}")
            raise
            
    async def test_telegram_bots(self):
        """Test connectivity and permissions for all configured Telegram bots."""
        self.logger.info("Testing Telegram bot configuration...")
        
        if not self.telegram_manager:
            await self.initialize_components()
            
        results = await self.telegram_manager.test_all_bots()
        
        total_bots = len(results)
        working_bots = sum(1 for r in results if r['status'] == 'success')
        
        print(f"\n🤖 Bot Test Results: {working_bots}/{total_bots} bots working")
        print("=" * 60)
        
        for result in results:
            status_emoji = "✅" if result['status'] == 'success' else "❌"
            print(f"{status_emoji} Bot {result['bot_index']}: {result['message']}")
            if result['status'] == 'error':
                print(f"   Error: {result['error']}")
                
        print("=" * 60)
        
        if working_bots == 0:
            print("❌ No bots are working! Please check your configuration.")
            return False
        elif working_bots < total_bots:
            print(f"⚠️  Only {working_bots}/{total_bots} bots are working.")
            print("   The system will work but with reduced capacity.")
        else:
            print("✅ All bots are working perfectly!")
            
        return working_bots > 0
        
    async def show_config(self):
        """Display current configuration."""
        print("🔧 Current Configuration")
        print("=" * 50)
        print(f"Server: {self.config.local_host}:{self.config.local_port}")
        print(f"Public Domain: {self.config.public_domain or 'Not set'}")
        print(f"HTTPS: {'Enabled' if self.config.force_https else 'Disabled'}")
        print(f"Database: {self.config.database_path}")
        print(f"Upload Directory: {self.config.upload_dir}")
        print(f"Cache Type: {self.config.cache_type}")
        print(f"Cache Size: {self.config.cache_size // (1024*1024)} MB")
        print(f"FFmpeg Hardware Accel: {self.config.ffmpeg_hardware_accel}")
        print(f"FFmpeg Threads: {self.config.ffmpeg_threads}")
        print(f"Max Upload Size: {self.config.max_upload_size // (1024*1024*1024)} GB")
        
        bot_configs = self.config.get_bot_configs()
        print(f"\n🤖 Configured Bots: {len(bot_configs)}")
        for i, bot_config in enumerate(bot_configs):
            print(f"  Bot {i}: {bot_config['token'][:8]}... -> Channel {bot_config['chat_id']}")
            
    async def serve(self):
        """Start the web server and begin serving requests."""
        self.logger.info("Starting Telegram HLS Streamer server...")
        
        await self.initialize_components()
        
        bot_test_success = await self.test_telegram_bots()
        if not bot_test_success:
            self.logger.error("Bot configuration test failed. Please fix your bot setup.")
            return False
            
        self.running = True
        
        def signal_handler(sig, frame):
            self.logger.info(f"Received signal {sig}, shutting down gracefully...")
            self.running = False
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            server_task = asyncio.create_task(self.web_server.start())
            
            self.logger.info(f"🚀 Server started successfully!")
            self.logger.info(f"🌐 Web interface: http://{self.config.local_host}:{self.config.local_port}")
            if self.config.public_domain:
                protocol = "https" if self.config.force_https else "http"
                self.logger.info(f"🌍 Public access: {protocol}://{self.config.public_domain}")
                
            while self.running:
                await asyncio.sleep(1)
                
            self.logger.info("Shutting down server...")
            await self.web_server.stop()
            await server_task
            
        except Exception as e:
            self.logger.error(f"Server error: {e}")
            return False
            
        finally:
            await self.cleanup()
            
        return True
        
    async def cleanup(self):
        """Clean up resources and close connections."""
        self.logger.info("Cleaning up resources...")
        
        if self.cache_manager:
            await self.cache_manager.cleanup()
            
        if self.telegram_manager:
            await self.telegram_manager.cleanup()
            
        if self.db_manager:
            await self.db_manager.close()
            
        self.logger.info("Cleanup completed.")


def create_directories():
    """Create necessary directories if they don't exist."""
    dirs = [
        "temp_uploads",
        "segments",
        "cache",
        "logs",
        "database"
    ]
    
    for dir_name in dirs:
        Path(dir_name).mkdir(exist_ok=True)


async def main():
    """Main application entry point."""
    parser = argparse.ArgumentParser(
        description="Telegram HLS Streamer - Turn Telegram into your personal Netflix!",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s serve                    Start the streaming server
  %(prog)s test-bots               Test Telegram bot configuration
  %(prog)s config                  Show current configuration
  %(prog)s --config custom.env     Use custom configuration file
        """
    )
    
    parser.add_argument(
        'command',
        choices=['serve', 'test-bots', 'config'],
        help='Command to execute'
    )
    
    parser.add_argument(
        '--config',
        type=str,
        help='Path to configuration file (default: .env)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    create_directories()
    
    try:
        app = TelegramHLSStreamer(config_path=args.config)
        
        if args.verbose:
            logging.getLogger().setLevel(logging.DEBUG)
            
        if args.command == 'serve':
            success = await app.serve()
            sys.exit(0 if success else 1)
            
        elif args.command == 'test-bots':
            success = await app.test_telegram_bots()
            sys.exit(0 if success else 1)
            
        elif args.command == 'config':
            await app.show_config()
            sys.exit(0)
            
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())