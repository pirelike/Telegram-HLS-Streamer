import argparse
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from logger_config import logger
from database import DatabaseManager
from video_processor import split_video_to_hls
from telegram_handler import TelegramHandler
from stream_server import StreamServer

# Load environment variables from .env file
load_dotenv()

async def main():
    """
    The main function of the application.
    It parses command-line arguments and executes the requested command.
    """
    parser = argparse.ArgumentParser(description="Telegram Video Streaming App")
    subparsers = parser.add_subparsers(dest='command', required=True)

    # --- Serve Command ---
    # This is now the primary command for running the web UI
    serve_parser = subparsers.add_parser('serve', help='Start the streaming server with web UI')
    serve_parser.add_argument('--host', default='0.0.0.0', help='Host to bind the server to')
    serve_parser.add_argument('--port', type=int, default=8080, help='Port for the streaming server')
    serve_parser.add_argument('--db-path', default='video_streaming.db', help='SQLite database file path')

    # --- CLI-only Commands (for advanced use) ---
    # These retain the token/chat_id arguments for scripting or manual use
    # Common parser for commands that need Telegram credentials
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--bot-token', help='Telegram bot token (overrides .env)')
    parent_parser.add_argument('--chat-id', help='Telegram chat ID (overrides .env)')
    parent_parser.add_argument('--db-path', default='video_streaming.db', help='SQLite database file path')

    # Upload command
    upload_parser = subparsers.add_parser('upload', help='CLI to upload a video', parents=[parent_parser])
    upload_parser.add_argument('--video', required=True, help='Path to the video file to upload')
    upload_parser.add_argument('--host', required=True, help='Public IP or hostname for playlist URLs')
    upload_parser.add_argument('--port', type=int, default=8080, help='Port for the streaming server')

    # List command
    list_parser = subparsers.add_parser('list', help='CLI to list all videos', parents=[parent_parser])

    # Delete command
    delete_parser = subparsers.add_parser('delete', help='CLI to delete a video', parents=[parent_parser])
    delete_parser.add_argument('--video-id', required=True, help='The ID of the video to delete')

    args = parser.parse_args()

    # Use environment variables as the default for bot token and chat id
    bot_token = getattr(args, 'bot_token', None) or os.getenv('BOT_TOKEN')
    chat_id = getattr(args, 'chat_id', None) or os.getenv('CHAT_ID')

    if not bot_token or not chat_id:
        logger.error("BOT_TOKEN and CHAT_ID must be set in the .env file or provided as arguments.")
        return

    db_manager = DatabaseManager(args.db_path)
    await db_manager.initialize_database()

    if args.command == 'serve':
        # Pass the config to the server
        server = StreamServer(args.host, args.port, db_manager, bot_token, chat_id)
        await server.start()
        await asyncio.Event().wait()  # Keep server running

    else:
        # Handle other CLI commands
        telegram_handler = TelegramHandler(bot_token, chat_id, db_manager)

        if args.command == 'upload':
            video_path = Path(args.video)
            video_id = video_path.stem
            segments_dir = f"segments/{video_id}"

            logger.info(f"Starting CLI upload for {video_path.name}...")
            split_video_to_hls(str(video_path), segments_dir)

            if await telegram_handler.upload_segments_to_telegram(segments_dir, video_id, video_path.name):
                playlist_url = f"http://{args.host}:{args.port}/playlist/{video_id}.m3u8"
                logger.info(f"✅ Upload complete! Your streaming URL is: {playlist_url}")
            else:
                logger.error("Upload failed.")

        elif args.command == 'list':
            videos = await db_manager.get_all_videos()
            if not videos:
                print("No videos found in the database.")
            else:
                print("Available videos:")
                for video in videos:
                    print(f"- {video.video_id} ({video.original_filename})")

        elif args.command == 'delete':
            if await db_manager.delete_video(args.video_id):
                playlist_file = f"playlists/{args.video_id}.m3u8"
                if os.path.exists(playlist_file):
                    os.remove(playlist_file)
                print(f"Video '{args.video_id}' deleted successfully.")
            else:
                print(f"Failed to delete video '{args.video_id}'.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
    except Exception as e:
        logger.critical(f"An unhandled exception occurred: {e}", exc_info=True)
