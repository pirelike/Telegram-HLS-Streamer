import os
import asyncio
from telegram import Bot
from telegram.error import TelegramError
from database import DatabaseManager, SegmentInfo, VideoInfo
from logger_config import logger
from datetime import datetime, timezone

class TelegramHandler:
    """
    Handles interactions with the Telegram Bot API, specifically for uploading
    and downloading video segments.
    """
    def __init__(self, bot_token: str, chat_id: str, db_manager: DatabaseManager):
        """
        Initializes the TelegramHandler.

        Args:
            bot_token (str): The Telegram bot token.
            chat_id (str): The ID of the Telegram chat to upload to.
            db_manager (DatabaseManager): An instance of the DatabaseManager.
        """
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.db = db_manager
        logger.info("TelegramHandler initialized.")

    async def upload_segments_to_telegram(self, segments_dir: str, video_id: str, original_filename: str) -> bool:
        """
        Uploads all .ts segments from a directory to Telegram.

        Args:
            segments_dir (str): The directory containing the .ts segment files.
            video_id (str): The unique ID for the video.
            original_filename (str): The original name of the video file.

        Returns:
            bool: True if all segments were uploaded successfully, False otherwise.
        """
        ts_files = sorted([f for f in os.listdir(segments_dir) if f.endswith('.ts')])
        if not ts_files:
            logger.error(f"No .ts files found in {segments_dir}")
            return False

        logger.info(f"Found {len(ts_files)} segments to upload for video {video_id}.")
        
        video_info = VideoInfo(
            video_id=video_id, original_filename=original_filename, total_duration=0,
            total_segments=len(ts_files), file_size=0, status='processing',
            created_at=datetime.now(timezone.utc).isoformat(), updated_at=datetime.now(timezone.utc).isoformat()
        )
        await self.db.add_video(video_info)

        total_duration = 0
        total_size = 0
        
        for i, ts_file in enumerate(ts_files, 1):
            file_path = os.path.join(segments_dir, ts_file)
            file_size = os.path.getsize(file_path)

            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Segment {ts_file} exceeds 50MB limit. Skipping.")
                continue
            
            try:
                logger.info(f"Uploading segment {i}/{len(ts_files)}: {ts_file}")
                with open(file_path, 'rb') as f:
                    message = await self.bot.send_document(
                        chat_id=self.chat_id, document=f, filename=ts_file,
                        caption=f"Video: {video_id} | Segment: {i}/{len(ts_files)}"
                    )
                
                duration = self._extract_segment_duration(os.path.join(segments_dir, 'playlist.m3u8'), ts_file)
                total_duration += duration
                total_size += file_size

                segment_info = SegmentInfo(
                    filename=ts_file, duration=duration, file_id=message.document.file_id,
                    file_size=file_size, segment_order=i-1
                )
                await self.db.add_segment(video_id, segment_info)
                logger.info(f"✅ Uploaded {ts_file} with File ID: {message.document.file_id}")
                await asyncio.sleep(0.5)

            except TelegramError as e:
                logger.error(f"Failed to upload {ts_file}: {e}", exc_info=True)
                await self.db.update_video_status(video_id, 'error')
                return False

        video_info.total_duration = total_duration
        video_info.file_size = total_size
        video_info.status = 'active'
        video_info.updated_at = datetime.now(timezone.utc).isoformat()
        await self.db.add_video(video_info)
        
        logger.info(f"✅ All segments for video {video_id} uploaded successfully.")
        return True

    async def download_segment_from_telegram(self, file_id: str) -> Optional[bytes]:
        """
        Downloads a segment from Telegram into memory with retry logic.

        Args:
            file_id (str): The file_id of the segment to download.

        Returns:
            Optional[bytes]: The content of the segment as bytes, or None if download fails.
        """
        for attempt in range(3):
            try:
                file = await self.bot.get_file(file_id)
                content = await file.download_as_bytearray()
                logger.info(f"Downloaded segment with file_id: {file_id}")
                return bytes(content)
            except TelegramError as e:
                logger.warning(f"Download attempt {attempt + 1} failed for {file_id}: {e}. Retrying...")
                await asyncio.sleep(2 ** attempt)
        logger.error(f"Failed to download segment {file_id} after multiple retries.")
        return None

    def _extract_segment_duration(self, playlist_path: str, segment_filename: str) -> float:
        """
        Extracts a segment's duration from the HLS playlist.

        Args:
            playlist_path (str): The path to the `.m3u8` playlist file.
            segment_filename (str): The filename of the segment to find the duration for.

        Returns:
            float: The duration of the segment in seconds. Defaults to 10.0 if not found.
        """
        try:
            with open(playlist_path, 'r') as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                if segment_filename in line and i > 0 and lines[i-1].startswith('#EXTINF:'):
                    return float(lines[i-1].split(':')[1].split(',')[0])
        except (IOError, IndexError, ValueError) as e:
            logger.warning(f"Could not extract duration for {segment_filename}: {e}", exc_info=True)
        return 10.0
