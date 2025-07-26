import argparse
import asyncio
import os
from pathlib import Path
from logger_config import logger
from database import DatabaseManager
from video_processor import split_video_to_hls
from telegram_handler import TelegramHandler
from stream_server import StreamServer

async def main():
    """
    The main function of the application.
    It parses command-line arguments and executes the requested command.
    """
    parser = argparse.ArgumentParser(description="Telegram Video Streaming App")
    subparsers = parser.add_subparsers(dest='command', required=True)

    # Common arguments
    for p_name in ['upload', 'serve', 'list', 'delete']:
        p = subparsers.add_parser(p_name, help=f'{p_name} command')
        p.add_argument('--bot-token', required=True, help='Telegram bot token')
        p.add_argument('--chat-id', required=True, help='Telegram chat ID')
        p.add_argument('--db-path', default='video_streaming.db', help='SQLite database file path')
    
    # Upload command arguments
    upload_parser = subparsers.choices['upload']
    upload_parser.add_argument('--video', required=True, help='Path to the video file to upload')
    upload_parser.add_argument('--host', required=True, help='Public IP or hostname for playlist URLs')
    upload_parser.add_argument('--port', type=int, default=8080, help='Port for the streaming server')

    # Serve command arguments
    serve_parser = subparsers.choices['serve']
    serve_parser.add_argument('--host', default='0.0.0.0', help='Host to bind the server to')
    serve_parser.add_argument('--port', type=int, default=8080, help='Port for the streaming server')

    # Delete command arguments
    delete_parser = subparsers.choices['delete']
    delete_parser.add_argument('--video-id', required=True, help='The ID of the video to delete')

    args = parser.parse_args()
    
    db_manager = DatabaseManager(args.db_path)
    await db_manager.initialize_database()

    telegram_handler = TelegramHandler(args.bot_token, args.chat_id, db_manager)

    if args.command == 'upload':
        video_path = Path(args.video)
        video_id = video_path.stem
        segments_dir = f"segments/{video_id}"
        
        logger.info(f"Starting upload process for {video_path.name}...")
        playlist_path = split_video_to_hls(str(video_path), segments_dir)
        
        if await telegram_handler.upload_segments_to_telegram(segments_dir, video_id, video_path.name):
            playlist_url = f"http://{args.host}:{args.port}/playlist/{video_id}.m3u8"
            logger.info(f"✅ Upload complete! Your streaming URL is: {playlist_url}")
        else:
            logger.error("Upload failed.")

    elif args.command == 'serve':
        server = StreamServer(args.host, args.port, db_manager, telegram_handler)
        await server.start()
        await asyncio.Event().wait()  # Keep server running

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
