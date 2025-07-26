#!/usr/bin/env python3
"""
Telegram Video Streaming System - Main Entry Point

This is the primary entry point for the Telegram Video Streaming application.
It provides both a web interface and CLI commands for managing video uploads
and streaming through Telegram's infrastructure.

Author: Your Name
Version: 2.0.0
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import NoReturn

from config import load_config, AppConfig
from database import DatabaseManager
from logger_config import setup_logging, get_logger
from stream_server import StreamServer
from telegram_handler import TelegramHandler
from video_processor import split_video_to_hls


def setup_argument_parser() -> argparse.ArgumentParser:
    """
    Configure and return the command-line argument parser.

    Returns:
        Configured ArgumentParser instance
    """
    parser = argparse.ArgumentParser(
        description="Telegram Video Streaming System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s serve                           # Start web server with default settings
  %(prog)s serve --port 9000               # Start on custom port
  %(prog)s upload video.mp4                # Upload video via CLI
  %(prog)s list                            # List all uploaded videos
  %(prog)s delete video_id                 # Delete a specific video
        """
    )

    # Global arguments
    parser.add_argument(
        '--config',
        default='.env',
        help='Path to configuration file (default: .env)'
    )
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Override logging level from config'
    )

    # Subcommands
    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        help='Available commands'
    )

    # Serve command (primary web interface)
    serve_parser = subparsers.add_parser(
        'serve',
        help='Start the streaming server with web UI'
    )
    serve_parser.add_argument(
        '--host',
        help='Override host from config'
    )
    serve_parser.add_argument(
        '--port',
        type=int,
        help='Override port from config'
    )

    # Upload command (CLI)
    upload_parser = subparsers.add_parser(
        'upload',
        help='Upload a video file via CLI'
    )
    upload_parser.add_argument(
        'video_path',
        type=Path,
        help='Path to the video file to upload'
    )
    upload_parser.add_argument(
        '--public',
        action='store_true',
        help='Generate public playlist URL instead of local'
    )

    # List command
    list_parser = subparsers.add_parser(
        'list',
        help='List all uploaded videos'
    )
    list_parser.add_argument(
        '--detailed',
        action='store_true',
        help='Show detailed information for each video'
    )

    # Delete command
    delete_parser = subparsers.add_parser(
        'delete',
        help='Delete a video and its segments'
    )
    delete_parser.add_argument(
        'video_id',
        help='ID of the video to delete'
    )
    delete_parser.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt'
    )

    # Status command
    status_parser = subparsers.add_parser(
        'status',
        help='Show system status and configuration'
    )

    return parser


async def cmd_serve(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Handle the 'serve' command - start the web server.

    Args:
        args: Parsed command line arguments
        config: Application configuration
    """
    logger = get_logger(__name__)

    # Override config with command line arguments if provided
    host = args.host or config.local_host
    port = args.port or config.local_port

    try:
        # Initialize database
        db_manager = DatabaseManager(config.db_path)
        await db_manager.initialize_database()

        # Start server
        server = StreamServer(host, port, db_manager, config)
        await server.start()

        logger.info("Server started successfully. Press Ctrl+C to stop.")

        # Keep server running until interrupted
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")

    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        sys.exit(1)


async def cmd_upload(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Handle the 'upload' command - upload video via CLI.

    Args:
        args: Parsed command line arguments
        config: Application configuration
    """
    logger = get_logger(__name__)

    video_path = args.video_path

    # Validate video file
    if not video_path.exists():
        logger.error(f"Video file not found: {video_path}")
        sys.exit(1)

    if not video_path.is_file():
        logger.error(f"Path is not a file: {video_path}")
        sys.exit(1)

    try:
        # Initialize components
        db_manager = DatabaseManager(config.db_path)
        await db_manager.initialize_database()

        telegram_handler = TelegramHandler(config, db_manager)

        # Generate video ID and process
        video_id = video_path.stem
        segments_dir = Path(config.segments_dir) / video_id

        logger.info(f"Starting upload for: {video_path.name}")
        logger.info(f"Video ID: {video_id}")

        # Split video into segments
        logger.info("Splitting video into HLS segments...")
        playlist_path = split_video_to_hls(
            str(video_path),
            str(segments_dir),
            config.max_chunk_size
        )

        # Upload segments to Telegram
        logger.info("Uploading segments to Telegram...")
        success = await telegram_handler.upload_segments_to_telegram(
            str(segments_dir),
            video_id,
            video_path.name
        )

        if success:
            playlist_url = config.get_playlist_url(video_id, public=args.public)
            logger.info(f"✅ Upload complete!")
            logger.info(f"🎬 Streaming URL: {playlist_url}")
        else:
            logger.error("❌ Upload failed")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        sys.exit(1)


async def cmd_list(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Handle the 'list' command - list all videos.

    Args:
        args: Parsed command line arguments
        config: Application configuration
    """
    logger = get_logger(__name__)

    try:
        db_manager = DatabaseManager(config.db_path)
        await db_manager.initialize_database()

        videos = await db_manager.get_all_videos()

        if not videos:
            print("No videos found in the database.")
            return

        print(f"\nFound {len(videos)} video(s):")
        print("-" * 80)

        for video in videos:
            print(f"ID: {video.video_id}")
            print(f"Filename: {video.original_filename}")
            print(f"Status: {video.status}")

            if args.detailed:
                print(f"Duration: {video.total_duration:.2f}s")
                print(f"Segments: {video.total_segments}")
                print(f"Size: {video.file_size / (1024**2):.2f} MB")
                print(f"Created: {video.created_at}")

                # Show URLs
                local_url = config.get_playlist_url(video.video_id, public=False)
                print(f"Local URL: {local_url}")

                if config.public_domain:
                    public_url = config.get_playlist_url(video.video_id, public=True)
                    print(f"Public URL: {public_url}")

            print("-" * 80)

    except Exception as e:
        logger.error(f"Failed to list videos: {e}", exc_info=True)
        sys.exit(1)


async def cmd_delete(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Handle the 'delete' command - delete a video.

    Args:
        args: Parsed command line arguments
        config: Application configuration
    """
    logger = get_logger(__name__)

    video_id = args.video_id

    try:
        db_manager = DatabaseManager(config.db_path)
        await db_manager.initialize_database()

        # Check if video exists
        video_info = await db_manager.get_video_info(video_id)
        if not video_info:
            logger.error(f"Video not found: {video_id}")
            sys.exit(1)

        # Confirmation prompt
        if not args.force:
            response = input(f"Delete video '{video_info.original_filename}' ({video_id})? [y/N]: ")
            if response.lower() not in ('y', 'yes'):
                print("Deletion cancelled.")
                return

        # Delete from database
        success = await db_manager.delete_video(video_id)

        if success:
            # Clean up playlist file
            playlist_file = Path(config.playlists_dir) / f"{video_id}.m3u8"
            if playlist_file.exists():
                playlist_file.unlink()
                logger.info(f"Removed playlist file: {playlist_file}")

            logger.info(f"✅ Video '{video_id}' deleted successfully")
        else:
            logger.error(f"❌ Failed to delete video '{video_id}'")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Delete operation failed: {e}", exc_info=True)
        sys.exit(1)


async def cmd_status(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Handle the 'status' command - show system status.

    Args:
        args: Parsed command line arguments
        config: Application configuration
    """
    print("\n📊 System Status")
    print("=" * 50)

    # Configuration info
    print(f"Local Server: {config.get_local_url()}")
    if config.public_domain:
        print(f"Public Access: {config.get_public_url()}")
    else:
        print("Public Access: Not configured")

    print(f"Database: {config.db_path}")
    print(f"Log Level: {config.log_level}")

    # Database status
    try:
        db_manager = DatabaseManager(config.db_path)
        await db_manager.initialize_database()

        videos = await db_manager.get_all_videos()
        total_size = sum(v.file_size for v in videos)

        print(f"\n📹 Database Statistics")
        print(f"Total Videos: {len(videos)}")
        print(f"Total Storage: {total_size / (1024**3):.2f} GB")

        # Status breakdown
        status_counts = {}
        for video in videos:
            status_counts[video.status] = status_counts.get(video.status, 0) + 1

        for status, count in status_counts.items():
            print(f"  {status.title()}: {count}")

    except Exception as e:
        print(f"Database Error: {e}")

    print()


async def main() -> NoReturn:
    """
    Main application entry point.

    Parses command line arguments, loads configuration, and dispatches
    to the appropriate command handler.
    """
    parser = setup_argument_parser()
    args = parser.parse_args()

    try:
        # Load configuration
        config = load_config(args.config)

        # Override log level if specified
        if args.log_level:
            config = AppConfig(
                **{**config.__dict__, 'log_level': args.log_level}
            )

        # Setup logging with config
        setup_logging(config.log_level, config.log_file)
        logger = get_logger(__name__)

        logger.info(f"Starting Telegram Video Streaming System")
        logger.info(f"Command: {args.command}")

        # Dispatch to command handlers
        if args.command == 'serve':
            await cmd_serve(args, config)
        elif args.command == 'upload':
            await cmd_upload(args, config)
        elif args.command == 'list':
            await cmd_list(args, config)
        elif args.command == 'delete':
            await cmd_delete(args, config)
        elif args.command == 'status':
            await cmd_status(args, config)
        else:
            parser.print_help()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
