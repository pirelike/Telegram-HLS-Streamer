#!/usr/bin/env python3
"""
Refactored main entry point for Telegram HLS Video Streamer.

This is the new, clean entry point that uses the refactored codebase structure.
"""

import asyncio
import argparse
import sys
import signal
import logging
from pathlib import Path

# Add src directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

from src.core.app import TelegramHLSApp
from src.core.config import get_config, Config
from src.core.exceptions import TelegramHLSError, ConfigurationError

logger = logging.getLogger(__name__)


class GracefulShutdown:
    """Handle graceful shutdown of the application."""
    
    def __init__(self, app: TelegramHLSApp):
        self.app = app
        self.shutdown_event = asyncio.Event()
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.shutdown_event.set()
    
    async def wait_for_shutdown(self):
        """Wait for shutdown signal."""
        await self.shutdown_event.wait()


async def serve_command():
    """Start the streaming server."""
    try:
        config = get_config()
        
        async with TelegramHLSApp(config) as app:
            # Setup graceful shutdown
            shutdown_handler = GracefulShutdown(app)
            signal.signal(signal.SIGINT, shutdown_handler.signal_handler)
            signal.signal(signal.SIGTERM, shutdown_handler.signal_handler)
            
            # Start the server
            await app.start_server()
            
            # Get server info
            protocol = "https" if config.force_https or config.ssl_cert_path else "http"
            host = config.public_domain or config.local_host
            port = "" if (protocol == "https" and config.local_port == 443) or (protocol == "http" and config.local_port == 80) else f":{config.local_port}"
            
            print(f"""
🎉 Telegram HLS Streaming Server Started!

📡 Server Details:
   • URL: {protocol}://{host}{port}
   • Local: http://{config.local_host}:{config.local_port}
   • Hardware Acceleration: {config.ffmpeg_hardware_accel}
   • Multi-bot support: {len(config.multi_bot_tokens)} bots configured

🚀 Ready to process videos!
   
Press Ctrl+C to shutdown gracefully...
            """)
            
            # Wait for shutdown signal
            await shutdown_handler.wait_for_shutdown()
            
    except ConfigurationError as e:
        logger.error(f"❌ Configuration error: {e}")
        sys.exit(1)
    except TelegramHLSError as e:
        logger.error(f"❌ Application error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        sys.exit(1)


async def test_bots_command():
    """Test all configured Telegram bots."""
    try:
        config = get_config()
        
        async with TelegramHLSApp(config) as app:
            print("🤖 Testing Telegram bots...")
            results = await app.test_bots()
            
            print("\n📊 Bot Test Results:")
            print("=" * 40)
            
            success_count = 0
            for bot_id, result in results.items():
                status = "✅ Working" if result['success'] else "❌ Failed"
                print(f"Bot {bot_id}: {status}")
                if result['success']:
                    print(f"  • Name: {result.get('bot_name', 'Unknown')}")
                    print(f"  • Username: @{result.get('username', 'unknown')}")
                    success_count += 1
                else:
                    print(f"  • Error: {result.get('error', 'Unknown error')}")
                print()
            
            print(f"Summary: {success_count}/{len(results)} bots working correctly")
            
            if success_count == 0:
                print("❌ No working bots found! Check your configuration.")
                sys.exit(1)
            elif success_count < len(results):
                print("⚠️  Some bots have issues. Check the errors above.")
                sys.exit(1)
            else:
                print("🎉 All bots are working correctly!")
                
    except ConfigurationError as e:
        logger.error(f"❌ Configuration error: {e}")
        sys.exit(1)
    except TelegramHLSError as e:
        logger.error(f"❌ Application error: {e}")
        sys.exit(1)


def show_configuration():
    """Display current configuration."""
    try:
        config = get_config()
        
        print("⚙️ Current Configuration:")
        print("=" * 50)
        
        # Telegram settings
        print("📡 Telegram Configuration:")
        tokens = config.multi_bot_tokens
        chats = config.multi_bot_chats
        
        if tokens:
            for bot_id in sorted(tokens.keys()):
                token = tokens[bot_id]
                chat = chats.get(bot_id, 'Not configured')
                print(f"  • Bot {bot_id}: {token[:10]}****** -> {chat}")
        else:
            print("  • No bots configured")
        
        print(f"\n🌐 Network Configuration:")
        print(f"  • Host: {config.local_host}")
        print(f"  • Port: {config.local_port}")
        print(f"  • Public Domain: {config.public_domain or 'Not set'}")
        print(f"  • Force HTTPS: {config.force_https}")
        
        print(f"\n🎬 Video Processing:")
        print(f"  • Max upload size: {config.max_upload_size / (1024**3):.1f}GB")
        print(f"  • Max chunk size: {config.max_chunk_size / (1024*1024):.1f}MB")
        print(f"  • Hardware acceleration: {config.ffmpeg_hardware_accel}")
        print(f"  • FFmpeg threads: {config.ffmpeg_threads}")
        
        print(f"\n💾 Cache Configuration:")
        print(f"  • Cache type: {config.cache_type}")
        print(f"  • Cache size: {config.cache_size / (1024*1024):.1f}MB")
        print(f"  • Preload segments: {config.preload_segments}")
        
        print(f"\n📁 Directory Structure:")
        print(f"  • Playlists: {config.playlists_dir}")
        print(f"  • Segments: {config.segments_dir}")
        print(f"  • Cache: {config.cache_dir}")
        print(f"  • Database: {config.database_path}")
        
    except ConfigurationError as e:
        logger.error(f"❌ Configuration error: {e}")
        sys.exit(1)


def show_status():
    """Show application status."""
    print("📊 Application Status:")
    print("=" * 30)
    
    # Check if files exist
    required_files = [
        'src/core/config.py',
        'src/core/app.py',
        'src/storage/database.py',
        'src/telegram/handler.py',
        'src/processing/video_processor.py'
    ]
    
    print("📁 Core Files:")
    for file_path in required_files:
        exists = "✅" if Path(file_path).exists() else "❌"
        print(f"  {exists} {file_path}")
    
    print(f"\n🐍 Python Environment:")
    print(f"  • Python: {sys.version.split()[0]}")
    print(f"  • Working directory: {Path.cwd()}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Telegram HLS Video Streaming Server (Refactored)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s serve                    # Start the streaming server
  %(prog)s test-bots               # Test all configured bots
  %(prog)s config                  # Show current configuration
  %(prog)s status                  # Show application status
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Serve command
    serve_parser = subparsers.add_parser('serve', help='Start the streaming server')
    
    # Test bots command
    test_parser = subparsers.add_parser('test-bots', help='Test all configured Telegram bots')
    
    # Config command
    config_parser = subparsers.add_parser('config', help='Show current configuration')
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show application status')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'serve':
            asyncio.run(serve_command())
        elif args.command == 'test-bots':
            asyncio.run(test_bots_command())
        elif args.command == 'config':
            show_configuration()
        elif args.command == 'status':
            show_status()
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()