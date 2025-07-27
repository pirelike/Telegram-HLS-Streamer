import argparse
import asyncio
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from logger_config import logger
from database import DatabaseManager
from video_processor import split_video_to_hls
from stream_server import StreamServer
from utils import get_local_ip, is_valid_domain

# Load environment variables from .env file
load_dotenv()

def setup_directories():
    """Create necessary directories if they don't exist."""
    directories = [
        os.getenv('TEMP_UPLOAD_DIR', 'temp_uploads'),
        os.getenv('SEGMENTS_DIR', 'segments'),
        os.getenv('PLAYLISTS_DIR', 'playlists'),
        f"{os.getenv('PLAYLISTS_DIR', 'playlists')}/local",
        f"{os.getenv('PLAYLISTS_DIR', 'playlists')}/public"
    ]

    # Create cache directory only if using disk cache
    cache_type = os.getenv('CACHE_TYPE', 'memory').lower()
    if cache_type == 'disk':
        directories.append(os.getenv('CACHE_DIR', 'cache'))

    # Create log directory if log file is specified
    log_file = os.getenv('LOG_FILE')
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            directories.append(log_dir)

    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        logger.debug(f"Ensured directory exists: {directory}")

def setup_logging():
    """Setup enhanced logging based on environment configuration."""
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    log_file = os.getenv('LOG_FILE')

    # Set the logging level
    numeric_level = getattr(logging, log_level, logging.INFO)
    logger.setLevel(numeric_level)

    # Clear existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(numeric_level)
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
            logger.info(f"Logging to file: {log_file}")
        except Exception as e:
            logger.warning(f"Could not setup file logging: {e}")

def validate_configuration():
    """Validate the configuration loaded from environment variables."""
    errors = []
    warnings = []

    # Check for multi-bot configuration first
    multi_bot_configs = detect_multi_bot_config()

    if len(multi_bot_configs) > 1:
        logger.info(f"🤖 Detected {len(multi_bot_configs)} bots for round-robin uploads:")
        for i, config in enumerate(multi_bot_configs, 1):
            logger.info(f"  Bot {i}: {config['chat_id']}")

        # Validate each bot config
        for i, config in enumerate(multi_bot_configs, 1):
            if not config['token']:
                errors.append(f"Bot {i} token is missing or empty")
            elif len(config['token']) < 40:
                warnings.append(f"Bot {i} token appears invalid (too short)")

            if not config['chat_id']:
                errors.append(f"Bot {i} chat_id is missing or empty")
            elif not config['chat_id'].startswith('@') and not config['chat_id'].lstrip('-').isdigit():
                warnings.append(f"Bot {i} chat_id should start with @ for channels or be numeric")
    else:
        # Single bot validation (existing logic)
        bot_token = os.getenv('BOT_TOKEN')
        chat_id = os.getenv('CHAT_ID')

        if not bot_token:
            errors.append("BOT_TOKEN is required")
        elif len(bot_token) < 40:
            warnings.append("BOT_TOKEN appears to be invalid (too short)")

        if not chat_id:
            errors.append("CHAT_ID is required")
        elif not chat_id.startswith('@') and not chat_id.lstrip('-').isdigit():
            warnings.append("CHAT_ID should start with @ for channels or be a numeric ID")

    # Network configuration validation
    local_host = os.getenv('LOCAL_HOST', '0.0.0.0')
    local_port = os.getenv('LOCAL_PORT', '8080')
    public_domain = os.getenv('PUBLIC_DOMAIN')

    try:
        port_num = int(local_port)
        if not (1 <= port_num <= 65535):
            errors.append(f"LOCAL_PORT must be between 1-65535, got: {port_num}")
    except ValueError:
        errors.append(f"LOCAL_PORT must be a number, got: {local_port}")

    if public_domain and not is_valid_domain(public_domain):
        warnings.append(f"PUBLIC_DOMAIN appears invalid: {public_domain}")

    # SSL/HTTPS configuration validation
    force_https = os.getenv('FORCE_HTTPS', 'false').lower() == 'true'
    ssl_cert_path = os.getenv('SSL_CERT_PATH')
    ssl_key_path = os.getenv('SSL_KEY_PATH')

    if force_https and public_domain:
        logger.info("FORCE_HTTPS enabled - will generate HTTPS URLs for reverse proxy setup")
    elif ssl_cert_path and ssl_key_path:
        if not os.path.exists(ssl_cert_path):
            warnings.append(f"SSL_CERT_PATH file not found: {ssl_cert_path}")
        if not os.path.exists(ssl_key_path):
            warnings.append(f"SSL_KEY_PATH file not found: {ssl_key_path}")

    # Cache configuration validation
    cache_type = os.getenv('CACHE_TYPE', 'memory').lower()
    if cache_type not in ['memory', 'disk']:
        warnings.append(f"CACHE_TYPE should be 'memory' or 'disk', got: {cache_type}")

    try:
        cache_size = int(os.getenv('CACHE_SIZE', '524288000'))
        cache_size_mb = cache_size / (1024 * 1024)
        if cache_size_mb < 10:
            warnings.append(f"CACHE_SIZE is very small ({cache_size_mb:.1f}MB)")
        elif cache_size_mb > 2048:
            warnings.append(f"CACHE_SIZE is very large ({cache_size_mb:.1f}MB) - consider reducing for memory cache")
    except ValueError:
        errors.append("CACHE_SIZE must be a number")

    # Size and duration validation
    try:
        max_upload_size = int(os.getenv('MAX_UPLOAD_SIZE', '53687091200'))
        if max_upload_size < 1024 * 1024:  # Less than 1MB
            warnings.append("MAX_UPLOAD_SIZE is very small (< 1MB)")
    except ValueError:
        errors.append("MAX_UPLOAD_SIZE must be a number")

    # Segment duration validation
    try:
        min_segment_duration = int(os.getenv('MIN_SEGMENT_DURATION', '2'))
        max_segment_duration = int(os.getenv('MAX_SEGMENT_DURATION', '30'))

        if not (1 <= min_segment_duration <= 10):
            warnings.append("MIN_SEGMENT_DURATION should be between 1-10 seconds for optimal performance")
        if not (10 <= max_segment_duration <= 120):
            warnings.append("MAX_SEGMENT_DURATION should be between 10-120 seconds for optimal performance")
        if min_segment_duration >= max_segment_duration:
            errors.append("MIN_SEGMENT_DURATION must be less than MAX_SEGMENT_DURATION")

    except ValueError:
        errors.append("MIN_SEGMENT_DURATION and MAX_SEGMENT_DURATION must be numbers")

    try:
        max_chunk_size = int(os.getenv('MAX_CHUNK_SIZE', '20971520'))
        if max_chunk_size > 20 * 1024 * 1024:  # Telegram's 20MB bot download limit
            errors.append("MAX_CHUNK_SIZE cannot exceed 20MB (Telegram bot download limit)")
        elif max_chunk_size < 5 * 1024 * 1024:  # Less than 5MB
            warnings.append("MAX_CHUNK_SIZE is very small (< 5MB), may cause too many segments")
    except ValueError:
        errors.append("MAX_CHUNK_SIZE must be a number")

    # Print validation results
    if errors:
        logger.error("Configuration errors found:")
        for error in errors:
            logger.error(f"  ❌ {error}")
        return False

    if warnings:
        logger.warning("Configuration warnings:")
        for warning in warnings:
            logger.warning(f"  ⚠️  {warning}")

    logger.info("✅ Configuration validation passed")
    return True

def detect_multi_bot_config():
    """
    Detect and return all available bot configurations.

    Returns:
        List of dict: List of bot configurations with 'token' and 'chat_id' keys
    """
    import json

    # Method 1: Try JSON configuration first
    multi_bot_config = os.getenv('MULTI_BOT_CONFIG')
    if multi_bot_config:
        try:
            bot_configs = json.loads(multi_bot_config)
            if isinstance(bot_configs, list) and len(bot_configs) > 0:
                logger.info(f"Found {len(bot_configs)} bots in MULTI_BOT_CONFIG")
                return bot_configs
        except json.JSONDecodeError as e:
            logger.error(f"Invalid MULTI_BOT_CONFIG JSON: {e}")

    # Method 2: Individual environment variables
    bot_configs = []

    # Primary bot
    bot_token = os.getenv('BOT_TOKEN')
    chat_id = os.getenv('CHAT_ID')
    if bot_token and chat_id:
        bot_configs.append({'token': bot_token, 'chat_id': chat_id})

    # Additional bots (BOT_TOKEN_2, BOT_TOKEN_3, etc.)
    for i in range(2, 11):  # Support up to 10 bots total
        token_key = f'BOT_TOKEN_{i}'
        chat_key = f'CHAT_ID_{i}'
        token = os.getenv(token_key)
        chat = os.getenv(chat_key)

        if token and chat:
            bot_configs.append({'token': token, 'chat_id': chat})
        elif token or chat:  # One is set but not the other
            logger.warning(f"Incomplete bot config {i}: {token_key}={'SET' if token else 'MISSING'}, {chat_key}={'SET' if chat else 'MISSING'}")

    return bot_configs

def create_telegram_handler(db_manager: DatabaseManager):
    """
    Create appropriate Telegram handler based on configuration.

    Returns:
        TelegramHandler: Either RoundRobinTelegramHandler or regular TelegramHandler
    """
    multi_bot_configs = detect_multi_bot_config()

    if len(multi_bot_configs) > 1:
        # Use round-robin multi-bot handler
        logger.info(f"🚀 Using Round-Robin Multi-Bot Handler with {len(multi_bot_configs)} bots")

        # Import and create round-robin handler
        try:
            from telegram_handler import RoundRobinTelegramHandler
            return RoundRobinTelegramHandler(multi_bot_configs, db_manager)
        except ImportError:
            logger.error("RoundRobinTelegramHandler not available, falling back to single bot")
            # Fall through to single bot handler

    # Single bot handler (fallback or only one bot configured)
    if len(multi_bot_configs) == 1:
        config = multi_bot_configs[0]
        logger.info("📱 Using Single Bot Handler")

        try:
            from telegram_handler import TelegramHandler
            return TelegramHandler(config['token'], config['chat_id'], db_manager)
        except ImportError:
            logger.error("TelegramHandler not available")
            raise
    else:
        raise ValueError("No valid bot configurations found in environment variables")

async def main():
    """
    The main function of the application with multi-bot support.
    It parses command-line arguments and executes the requested command.
    """
    # Setup enhanced logging first
    setup_logging()

    parser = argparse.ArgumentParser(description="Telegram Video Streaming App with Multi-Bot Support")
    subparsers = parser.add_subparsers(dest='command', required=True)

    # --- Serve Command ---
    serve_parser = subparsers.add_parser('serve', help='Start the streaming server with web UI')
    serve_parser.add_argument('--host', help='Host to bind the server to (overrides LOCAL_HOST)')
    serve_parser.add_argument('--port', type=int, help='Port for the streaming server (overrides LOCAL_PORT)')
    serve_parser.add_argument('--db-path', help='SQLite database file path (overrides DB_PATH)')
    serve_parser.add_argument('--cache-type', choices=['memory', 'disk'], help='Cache type (overrides CACHE_TYPE)')
    serve_parser.add_argument('--force-https', action='store_true', help='Force HTTPS URLs (overrides FORCE_HTTPS)')

    # --- CLI-only Commands ---
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--db-path', help='SQLite database file path (overrides .env)')

    # Upload command - now supports multi-bot
    upload_parser = subparsers.add_parser('upload', help='CLI to upload a video (supports multi-bot)', parents=[parent_parser])
    upload_parser.add_argument('--video', required=True, help='Path to the video file to upload')
    upload_parser.add_argument('--host', help='Public IP or hostname for playlist URLs')
    upload_parser.add_argument('--port', type=int, help='Port for the streaming server')

    # List command
    list_parser = subparsers.add_parser('list', help='CLI to list all videos', parents=[parent_parser])

    # Delete command
    delete_parser = subparsers.add_parser('delete', help='CLI to delete a video', parents=[parent_parser])
    delete_parser.add_argument('--video-id', required=True, help='The ID of the video to delete')

    # Config command - now shows multi-bot info
    config_parser = subparsers.add_parser('config', help='Show current configuration including multi-bot setup')

    # New command: Test bots
    test_parser = subparsers.add_parser('test-bots', help='Test all configured bots')

    args = parser.parse_args()

    # Setup directories
    setup_directories()

    # Handle config command early
    if args.command == 'config':
        show_configuration()
        return

    # Handle test-bots command
    if args.command == 'test-bots':
        await test_bots()
        return

    # Validate configuration
    if not validate_configuration():
        logger.error("Please fix configuration errors before continuing.")
        return

    # Load database
    db_path = getattr(args, 'db_path', None) or os.getenv('DB_PATH', 'video_streaming.db')
    db_manager = DatabaseManager(db_path)
    await db_manager.initialize_database()

    if args.command == 'serve':
        # Get bot configuration for server (use first bot for web UI operations)
        multi_bot_configs = detect_multi_bot_config()
        if not multi_bot_configs:
            logger.error("No bot configuration found for server")
            return

        primary_bot = multi_bot_configs[0]

        # Load server configuration with command line overrides
        host = args.host or os.getenv('LOCAL_HOST', '0.0.0.0')
        port = args.port or int(os.getenv('LOCAL_PORT', '8080'))
        public_domain = os.getenv('PUBLIC_DOMAIN')
        playlists_dir = os.getenv('PLAYLISTS_DIR', 'playlists')
        cache_type = args.cache_type or os.getenv('CACHE_TYPE', 'memory').lower()
        force_https = args.force_https or os.getenv('FORCE_HTTPS', 'false').lower() == 'true'

        # Pass the enhanced config to the server
        server = StreamServer(
            host=host,
            port=port,
            db_manager=db_manager,
            bot_token=primary_bot['token'],
            chat_id=primary_bot['chat_id'],
            public_domain=public_domain,
            playlists_dir=playlists_dir,
            ssl_cert_path=os.getenv('SSL_CERT_PATH'),
            ssl_key_path=os.getenv('SSL_KEY_PATH'),
            cache_size=int(os.getenv('CACHE_SIZE', 500 * 1024 * 1024)),
            force_https=force_https,
            cache_type=cache_type
        )

        # Create and set the appropriate telegram handler in the server
        server.telegram_handler = create_telegram_handler(db_manager)

        await server.start()

        # Keep server running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Server stopped by user.")

    else:
        # Handle other CLI commands with multi-bot support
        telegram_handler = create_telegram_handler(db_manager)

        if args.command == 'upload':
            video_path = Path(args.video)
            video_id = video_path.stem
            segments_dir = f"{os.getenv('SEGMENTS_DIR', 'segments')}/{video_id}"

            logger.info(f"Starting CLI upload for {video_path.name}...")
            split_video_to_hls(str(video_path), segments_dir)

            if await telegram_handler.upload_segments_to_telegram(segments_dir, video_id, video_path.name):
                host = args.host or get_local_ip() or 'localhost'
                port = args.port or int(os.getenv('LOCAL_PORT', '8080'))

                # Determine protocol for CLI uploads
                force_https = os.getenv('FORCE_HTTPS', 'false').lower() == 'true'
                has_ssl_certs = (os.getenv('SSL_CERT_PATH') and os.getenv('SSL_KEY_PATH') and
                               os.path.exists(os.getenv('SSL_CERT_PATH')) and
                               os.path.exists(os.getenv('SSL_KEY_PATH')))
                protocol = "https" if (force_https or has_ssl_certs) else "http"

                local_url = f"{protocol}://{host}:{port}/playlist/local/{video_id}.m3u8"
                logger.info(f"✅ Upload complete! Local streaming URL: {local_url}")

                public_domain = os.getenv('PUBLIC_DOMAIN')
                if public_domain:
                    public_url = f"{protocol}://{public_domain}/playlist/public/{video_id}.m3u8"
                    logger.info(f"🌐 Public streaming URL: {public_url}")
            else:
                logger.error("Upload failed.")

        elif args.command == 'list':
            videos = await db_manager.get_all_videos()
            if not videos:
                print("No videos found in the database.")
            else:
                print("Available videos:")
                for video in videos:
                    print(f"- {video.video_id} ({video.original_filename}) - {video.status}")

        elif args.command == 'delete':
            if await db_manager.delete_video(args.video_id):
                # Clean up playlist files
                playlists_dir = os.getenv('PLAYLISTS_DIR', 'playlists')
                for access_type in ['local', 'public']:
                    playlist_file = f"{playlists_dir}/{access_type}/{args.video_id}.m3u8"
                    if os.path.exists(playlist_file):
                        os.remove(playlist_file)
                        logger.info(f"Removed playlist file: {playlist_file}")
                print(f"Video '{args.video_id}' deleted successfully.")
            else:
                print(f"Failed to delete video '{args.video_id}'.")

async def test_bots():
    """Test all configured bots to ensure they work."""
    logger.info("🧪 Testing bot configurations...")

    multi_bot_configs = detect_multi_bot_config()
    if not multi_bot_configs:
        logger.error("❌ No bot configurations found!")
        return

    from telegram import Bot
    from telegram.error import TelegramError

    successful_bots = 0

    for i, config in enumerate(multi_bot_configs, 1):
        logger.info(f"Testing Bot {i}: {config['chat_id']}...")

        try:
            bot = Bot(token=config['token'])

            # Test bot info
            bot_info = await bot.get_me()
            logger.info(f"  ✅ Bot {i} (@{bot_info.username}) - {bot_info.first_name}")

            # Test chat access (optional - comment out if you don't want test messages)
            try:
                test_message = await bot.send_message(
                    chat_id=config['chat_id'],
                    text=f"🧪 Bot test from Telegram Video Streamer\n\nBot {i} (@{bot_info.username}) is working correctly!",
                    parse_mode='HTML'
                )
                logger.info(f"  ✅ Bot {i} can send messages to {config['chat_id']}")

                # Clean up test message after 2 seconds
                await asyncio.sleep(2)
                try:
                    await bot.delete_message(chat_id=config['chat_id'], message_id=test_message.message_id)
                    logger.info(f"  🗑️ Cleaned up test message for Bot {i}")
                except:
                    pass  # Ignore cleanup errors

            except TelegramError as e:
                logger.warning(f"  ⚠️ Bot {i} cannot send to {config['chat_id']}: {e}")

            successful_bots += 1

        except TelegramError as e:
            logger.error(f"  ❌ Bot {i} failed: {e}")
        except Exception as e:
            logger.error(f"  💥 Bot {i} unexpected error: {e}")

        # Brief pause between tests
        if i < len(multi_bot_configs):
            await asyncio.sleep(1)

    logger.info(f"🏁 Bot testing complete: {successful_bots}/{len(multi_bot_configs)} bots working")

    if successful_bots == len(multi_bot_configs):
        logger.info("🎉 All bots are ready for round-robin uploads!")
    elif successful_bots > 1:
        logger.info(f"⚡ {successful_bots} bots ready - round-robin will still provide benefits")
    else:
        logger.warning("⚠️ Only one or no bots working - round-robin benefits limited")

def show_configuration():
    """Display the current configuration with multi-bot information."""
    logger.info("🔧 Current Configuration:")
    logger.info("=" * 60)

    # Multi-bot configuration
    multi_bot_configs = detect_multi_bot_config()
    logger.info(f"🤖 Bot Configuration: {len(multi_bot_configs)} bot(s) detected")

    if len(multi_bot_configs) > 1:
        logger.info("   Round-robin multi-bot mode enabled 🔄")
        for i, config in enumerate(multi_bot_configs, 1):
            token_display = f"{'*' * (len(config['token']) - 8)}{config['token'][-8:]}" if len(config['token']) > 8 else "***"
            logger.info(f"   Bot {i}: {config['chat_id']} (Token: {token_display})")
    elif len(multi_bot_configs) == 1:
        config = multi_bot_configs[0]
        token_display = f"{'*' * (len(config['token']) - 8)}{config['token'][-8:]}" if len(config['token']) > 8 else "***"
        logger.info(f"   Single bot mode: {config['chat_id']} (Token: {token_display})")
    else:
        logger.info("   ❌ No valid bot configurations found!")

    # Regular configuration items
    config_items = [
        ('LOCAL_HOST', os.getenv('LOCAL_HOST', '0.0.0.0')),
        ('LOCAL_PORT', os.getenv('LOCAL_PORT', '8080')),
        ('PUBLIC_DOMAIN', os.getenv('PUBLIC_DOMAIN', 'Not configured')),
        ('FORCE_HTTPS', os.getenv('FORCE_HTTPS', 'false')),
        ('SSL_CERT_PATH', os.getenv('SSL_CERT_PATH', 'Not configured')),
        ('SSL_KEY_PATH', os.getenv('SSL_KEY_PATH', 'Not configured')),
        ('CACHE_TYPE', os.getenv('CACHE_TYPE', 'memory')),
        ('CACHE_SIZE', f"{int(os.getenv('CACHE_SIZE', '524288000')) // (1024**2)} MB"),
        ('DB_PATH', os.getenv('DB_PATH', 'video_streaming.db')),
        ('MAX_UPLOAD_SIZE', f"{int(os.getenv('MAX_UPLOAD_SIZE', '53687091200')) // (1024**3)} GB"),
        ('MIN_SEGMENT_DURATION', f"{os.getenv('MIN_SEGMENT_DURATION', '2')}s"),
        ('MAX_SEGMENT_DURATION', f"{os.getenv('MAX_SEGMENT_DURATION', '30')}s"),
        ('MAX_CHUNK_SIZE', f"{int(os.getenv('MAX_CHUNK_SIZE', '20971520')) // (1024**2)} MB"),
        ('LOG_LEVEL', os.getenv('LOG_LEVEL', 'INFO')),
        ('LOG_FILE', os.getenv('LOG_FILE', 'Console only')),
    ]

    for key, value in config_items:
        logger.info(f"{key:<20}: {value}")

    logger.info("=" * 60)

    # Show auto-detected values
    detected_ip = get_local_ip()
    if detected_ip:
        logger.info(f"{'Auto-detected IP':<20}: {detected_ip}")

    # Show HTTPS status
    force_https = os.getenv('FORCE_HTTPS', 'false').lower() == 'true'
    has_ssl_certs = (os.getenv('SSL_CERT_PATH') and os.getenv('SSL_KEY_PATH') and
                    os.path.exists(os.getenv('SSL_CERT_PATH', '')) and
                    os.path.exists(os.getenv('SSL_KEY_PATH', '')))

    if force_https:
        logger.info(f"{'HTTPS Mode':<20}: Enabled (Reverse Proxy)")
    elif has_ssl_certs:
        logger.info(f"{'HTTPS Mode':<20}: Enabled (Direct SSL)")
    else:
        logger.info(f"{'HTTPS Mode':<20}: Disabled (HTTP only)")

    # Performance estimation
    if len(multi_bot_configs) > 1:
        logger.info(f"{'Expected Speedup':<20}: ~{len(multi_bot_configs)}x faster uploads")
        logger.info(f"{'Rate Limit Isolation':<20}: Each bot has separate limits")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
    except Exception as e:
        logger.critical(f"An unhandled exception occurred: {e}", exc_info=True)
