"""
Telegram Bot API Handler for Video Streaming System.

This module manages all interactions with the Telegram Bot API, including:
- Uploading video segments to Telegram channels
- Downloading segments on-demand for streaming
- Retry logic and error handling for reliable operation
- File size validation and optimization

The handler uses the python-telegram-bot library with proper async/await
patterns for optimal performance.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple
import hashlib

from telegram import Bot
from telegram.error import TelegramError, NetworkError, RetryAfter, TimedOut

from config import AppConfig
from database import DatabaseManager, SegmentInfo, VideoInfo
from logger_config import get_logger

logger = get_logger(__name__)


class TelegramUploadError(Exception):
    """Custom exception for Telegram upload failures."""
    pass


class TelegramDownloadError(Exception):
    """Custom exception for Telegram download failures."""
    pass


class TelegramHandler:
    """
    Handles all Telegram Bot API operations for video streaming.

    This class provides reliable upload and download functionality with
    proper error handling, retry logic, and progress tracking.
    """

    # Telegram's file size limits
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

    # Retry configuration
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # Exponential backoff

    def __init__(self, config: AppConfig, db_manager: DatabaseManager):
        """
        Initialize the Telegram handler.

        Args:
            config: Application configuration containing bot token and chat ID
            db_manager: Database manager for storing segment metadata

        Raises:
            ValueError: If bot token or chat ID is invalid
        """
        if not config.bot_token or not config.bot_token.strip():
            raise ValueError("Bot token is required")

        if not config.chat_id or not config.chat_id.startswith('@'):
            raise ValueError("Chat ID must start with '@'")

        self.config = config
        self.bot = Bot(token=config.bot_token)
        self.chat_id = config.chat_id
        self.db = db_manager

        logger.info(f"TelegramHandler initialized for channel: {config.chat_id}")

    async def verify_bot_configuration(self) -> bool:
        """
        Verify that the bot is properly configured and can access the channel.

        Returns:
            True if configuration is valid, False otherwise
        """
        try:
            # Test bot credentials
            bot_info = await self.bot.get_me()
            logger.info(f"Bot verified: @{bot_info.username} ({bot_info.full_name})")

            # Test channel access by sending a test message
            test_message = await self.bot.send_message(
                chat_id=self.chat_id,
                text="🤖 Bot configuration test - System ready!"
            )

            # Delete the test message
            await self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=test_message.message_id
            )

            logger.info(f"✅ Channel access verified: {self.chat_id}")
            return True

        except TelegramError as e:
            logger.error(f"❌ Bot configuration verification failed: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error during verification: {e}", exc_info=True)
            return False

    async def upload_segments_to_telegram(
        self,
        segments_dir: str,
        video_id: str,
        original_filename: str
    ) -> bool:
        """
        Upload all video segments from a directory to Telegram.

        This method handles the complete upload process including:
        - Validating segment files
        - Creating video record in database
        - Uploading segments with progress tracking
        - Updating database with segment metadata

        Args:
            segments_dir: Directory containing .ts segment files
            video_id: Unique identifier for the video
            original_filename: Original name of the uploaded video

        Returns:
            True if all segments were uploaded successfully, False otherwise

        Raises:
            TelegramUploadError: If upload process fails
        """
        segments_path = Path(segments_dir)

        if not segments_path.exists() or not segments_path.is_dir():
            raise TelegramUploadError(f"Segments directory not found: {segments_dir}")

        # Find all .ts files and sort them
        ts_files = sorted([
            f for f in segments_path.iterdir()
            if f.suffix == '.ts' and f.is_file()
        ])

        if not ts_files:
            raise TelegramUploadError(f"No .ts files found in {segments_dir}")

        logger.info(f"Found {len(ts_files)} segments to upload for video {video_id}")

        # Create video record in database
        try:
            video_info = VideoInfo(
                video_id=video_id,
                original_filename=original_filename,
                total_duration=0.0,  # Will be updated after processing segments
                total_segments=len(ts_files),
                file_size=0,  # Will be updated after processing segments
                status='processing',
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat()
            )

            success = await self.db.add_video(video_info)
            if not success:
                raise TelegramUploadError("Failed to create video record in database")

        except Exception as e:
            raise TelegramUploadError(f"Database error during video creation: {e}") from e

        # Upload segments
        total_duration = 0.0
        total_size = 0
        uploaded_segments = []

        try:
            for i, ts_file in enumerate(ts_files, 1):
                segment_result = await self._upload_single_segment(
                    ts_file,
                    video_id,
                    i,
                    len(ts_files),
                    segments_dir
                )

                if segment_result:
                    segment_info, file_size = segment_result
                    uploaded_segments.append(segment_info)
                    total_duration += segment_info.duration
                    total_size += file_size

                    # Add segment to database
                    await self.db.add_segment(video_id, segment_info)

                    logger.info(
                        f"✅ Uploaded segment {i}/{len(ts_files)}: "
                        f"{segment_info.filename} ({file_size / 1024:.1f} KB)"
                    )
                else:
                    # Upload failed for this segment
                    raise TelegramUploadError(f"Failed to upload segment {ts_file.name}")

                # Rate limiting: small delay between uploads
                if i < len(ts_files):  # Don't delay after the last upload
                    await asyncio.sleep(0.5)

            # Update video record with final statistics
            video_info.total_duration = total_duration
            video_info.file_size = total_size
            video_info.status = 'active'
            video_info.updated_at = datetime.now(timezone.utc).isoformat()

            await self.db.add_video(video_info)

            logger.info(
                f"✅ Upload completed for video {video_id}: "
                f"{len(uploaded_segments)} segments, "
                f"{total_duration:.1f}s duration, "
                f"{total_size / (1024**2):.1f} MB total"
            )

            return True

        except Exception as e:
            # Update video status to error
            try:
                video_info.status = 'error'
                video_info.updated_at = datetime.now(timezone.utc).isoformat()
                await self.db.add_video(video_info)
            except Exception as db_error:
                logger.error(f"Failed to update video status to error: {db_error}")

            logger.error(f"Upload failed for video {video_id}: {e}", exc_info=True)
            raise TelegramUploadError(f"Upload process failed: {e}") from e

    async def _upload_single_segment(
        self,
        segment_file: Path,
        video_id: str,
        segment_number: int,
        total_segments: int,
        segments_dir: str
    ) -> Optional[Tuple[SegmentInfo, int]]:
        """
        Upload a single video segment to Telegram with retry logic.

        Args:
            segment_file: Path to the segment file
            video_id: Video identifier
            segment_number: Current segment number (1-based)
            total_segments: Total number of segments
            segments_dir: Directory containing segments (for duration extraction)

        Returns:
            Tuple of (SegmentInfo, file_size) if successful, None if failed
        """
        file_size = segment_file.stat().st_size

        # Validate file size
        if file_size > self.MAX_FILE_SIZE:
            logger.error(
                f"Segment {segment_file.name} exceeds Telegram's 50MB limit "
                f"({file_size / (1024**2):.1f} MB)"
            )
            return None

        if file_size == 0:
            logger.error(f"Segment {segment_file.name} is empty")
            return None

        # Extract segment duration from playlist
        duration = self._extract_segment_duration(
            Path(segments_dir) / 'playlist.m3u8',
            segment_file.name
        )

        # Calculate file hash for integrity verification
        file_hash = await self._calculate_file_hash(segment_file)

        # Upload with retry logic
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                logger.debug(
                    f"Uploading {segment_file.name} (attempt {attempt}/{self.MAX_RETRIES})"
                )

                # Create caption with metadata
                caption = (
                    f"📹 Video: {video_id}\n"
                    f"🎬 Segment: {segment_number}/{total_segments}\n"
                    f"⏱️ Duration: {duration:.2f}s\n"
                    f"📊 Size: {file_size / 1024:.1f} KB\n"
                    f"🔍 Hash: {file_hash[:16]}..."
                )

                # Upload file
                async with segment_file.open('rb') as file_obj:
                    message = await self.bot.send_document(
                        chat_id=self.chat_id,
                        document=file_obj,
                        filename=segment_file.name,
                        caption=caption,
                        read_timeout=300,  # 5 minutes
                        write_timeout=300,
                        connect_timeout=60
                    )

                # Verify upload
                if not message.document:
                    raise TelegramUploadError("No document in response message")

                # Create segment info
                segment_info = SegmentInfo(
                    filename=segment_file.name,
                    duration=duration,
                    file_id=message.document.file_id,
                    file_size=file_size,
                    segment_order=segment_number - 1,  # 0-based for database
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=datetime.now(timezone.utc).isoformat()
                )

                logger.debug(f"✅ Upload successful: {segment_file.name} -> {message.document.file_id}")
                return segment_info, file_size

            except RetryAfter as e:
                # Telegram rate limiting
                wait_time = e.retry_after + 1
                logger.warning(
                    f"Rate limited for {segment_file.name}, waiting {wait_time}s "
                    f"(attempt {attempt}/{self.MAX_RETRIES})"
                )
                await asyncio.sleep(wait_time)

            except (NetworkError, TimedOut) as e:
                # Network issues - retry with exponential backoff
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)]
                    logger.warning(
                        f"Network error for {segment_file.name}: {e}. "
                        f"Retrying in {delay}s (attempt {attempt}/{self.MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Network error persists for {segment_file.name}: {e}")

            except TelegramError as e:
                # Other Telegram API errors
                logger.error(
                    f"Telegram API error for {segment_file.name}: {e} "
                    f"(attempt {attempt}/{self.MAX_RETRIES})"
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)])

            except Exception as e:
                # Unexpected errors
                logger.error(
                    f"Unexpected error uploading {segment_file.name}: {e} "
                    f"(attempt {attempt}/{self.MAX_RETRIES})",
                    exc_info=True
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(2)

        logger.error(f"❌ Failed to upload {segment_file.name} after {self.MAX_RETRIES} attempts")
        return None

    async def download_segment_from_telegram(
        self,
        file_id: str,
        max_retries: int = 3
    ) -> Optional[bytes]:
        """
        Download a video segment from Telegram by file_id.

        This method implements retry logic and proper error handling
        for reliable segment retrieval during streaming.

        Args:
            file_id: Telegram file_id of the segment to download
            max_retries: Maximum number of retry attempts

        Returns:
            Segment content as bytes, or None if download fails

        Raises:
            TelegramDownloadError: If download fails after all retries
        """
        if not file_id or not file_id.strip():
            raise TelegramDownloadError("Invalid file_id provided")

        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(f"Downloading segment {file_id[:16]}... (attempt {attempt}/{max_retries})")

                # Get file info
                file_obj = await self.bot.get_file(
                    file_id,
                    read_timeout=60,
                    write_timeout=60
                )

                if not file_obj:
                    raise TelegramDownloadError("Failed to get file object from Telegram")

                # Download file content
                content = await file_obj.download_as_bytearray()

                if not content:
                    raise TelegramDownloadError("Downloaded content is empty")

                segment_bytes = bytes(content)

                logger.debug(
                    f"✅ Downloaded segment {file_id[:16]}: {len(segment_bytes)} bytes"
                )

                return segment_bytes

            except RetryAfter as e:
                # Rate limiting
                wait_time = e.retry_after + 1
                logger.warning(
                    f"Rate limited downloading {file_id[:16]}, waiting {wait_time}s "
                    f"(attempt {attempt}/{max_retries})"
                )
                await asyncio.sleep(wait_time)

            except (NetworkError, TimedOut) as e:
                # Network issues
                if attempt < max_retries:
                    delay = self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)]
                    logger.warning(
                        f"Network error downloading {file_id[:16]}: {e}. "
                        f"Retrying in {delay}s (attempt {attempt}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Network error persists for {file_id[:16]}: {e}")

            except TelegramError as e:
                # Telegram API errors
                logger.error(
                    f"Telegram API error downloading {file_id[:16]}: {e} "
                    f"(attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    await asyncio.sleep(self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)])

            except Exception as e:
                # Unexpected errors
                logger.error(
                    f"Unexpected error downloading {file_id[:16]}: {e} "
                    f"(attempt {attempt}/{max_retries})",
                    exc_info=True
                )
                if attempt < max_retries:
                    await asyncio.sleep(2)

        error_msg = f"Failed to download segment {file_id[:16]} after {max_retries} attempts"
        logger.error(error_msg)
        raise TelegramDownloadError(error_msg)

    def _extract_segment_duration(
        self,
        playlist_path: Path,
        segment_filename: str
    ) -> float:
        """
        Extract segment duration from HLS playlist file.

        Args:
            playlist_path: Path to the .m3u8 playlist file
            segment_filename: Name of the segment to find duration for

        Returns:
            Duration in seconds, or default value if not found
        """
        default_duration = 10.0

        try:
            if not playlist_path.exists():
                logger.warning(f"Playlist file not found: {playlist_path}")
                return default_duration

            with playlist_path.open('r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines()]

            # Look for EXTINF line followed by segment filename
            for i, line in enumerate(lines):
                if segment_filename in line and i > 0:
                    prev_line = lines[i - 1]
                    if prev_line.startswith('#EXTINF:'):
                        try:
                            # Extract duration from #EXTINF:duration,
                            duration_str = prev_line.split(':')[1].split(',')[0]
                            duration = float(duration_str)

                            if 0 < duration <= 3600:  # Sanity check: 0-1 hour
                                return duration
                            else:
                                logger.warning(
                                    f"Invalid duration {duration} for {segment_filename}, "
                                    f"using default {default_duration}"
                                )

                        except (IndexError, ValueError) as e:
                            logger.warning(
                                f"Failed to parse duration from '{prev_line}': {e}"
                            )

            logger.debug(f"Duration not found for {segment_filename}, using default {default_duration}")

        except Exception as e:
            logger.warning(
                f"Error reading playlist {playlist_path} for segment {segment_filename}: {e}",
                exc_info=True
            )

        return default_duration

    async def _calculate_file_hash(self, file_path: Path) -> str:
        """
        Calculate SHA-256 hash of a file for integrity verification.

        Args:
            file_path: Path to the file to hash

        Returns:
            Hexadecimal hash string
        """
        hasher = hashlib.sha256()

        try:
            async with file_path.open('rb') as f:
                while chunk := await f.read(8192):  # Read in 8KB chunks
                    hasher.update(chunk)

            return hasher.hexdigest()

        except Exception as e:
            logger.warning(f"Failed to calculate hash for {file_path}: {e}")
            return "unknown"

    async def get_upload_statistics(self) -> dict:
        """
        Get statistics about uploaded content in the Telegram channel.

        Returns:
            Dictionary with upload statistics
        """
        try:
            videos = await self.db.get_all_videos()

            stats = {
                'total_videos': len(videos),
                'total_segments': sum(v.total_segments for v in videos),
                'total_size_bytes': sum(v.file_size for v in videos),
                'total_duration_seconds': sum(v.total_duration for v in videos),
                'status_breakdown': {},
                'average_video_size_mb': 0,
                'average_video_duration_minutes': 0
            }

            # Status breakdown
            for video in videos:
                status = video.status
                stats['status_breakdown'][status] = stats['status_breakdown'].get(status, 0) + 1

            # Averages
            if videos:
                stats['average_video_size_mb'] = stats['total_size_bytes'] / len(videos) / (1024**2)
                stats['average_video_duration_minutes'] = stats['total_duration_seconds'] / len(videos) / 60

            return stats

        except Exception as e:
            logger.error(f"Failed to get upload statistics: {e}", exc_info=True)
            return {'error': str(e)}

    async def cleanup_failed_uploads(self) -> int:
        """
        Clean up database records for videos with 'error' or 'processing' status.

        This method can be used to clean up incomplete uploads that may have
        failed due to network issues or other problems.

        Returns:
            Number of cleaned up video records
        """
        try:
            # Get all videos with error or processing status
            all_videos = await self.db.get_all_videos()
            failed_videos = [
                v for v in all_videos
                if v.status in ('error', 'processing')
            ]

            cleanup_count = 0

            for video in failed_videos:
                # Check if video was created more than 1 hour ago
                try:
                    created_time = datetime.fromisoformat(video.created_at.replace('Z', '+00:00'))
                    time_diff = datetime.now(timezone.utc) - created_time

                    if time_diff.total_seconds() > 3600:  # 1 hour
                        success = await self.db.delete_video(video.video_id)
                        if success:
                            cleanup_count += 1
                            logger.info(f"Cleaned up failed video: {video.video_id}")

                except Exception as e:
                    logger.warning(f"Error cleaning up video {video.video_id}: {e}")

            if cleanup_count > 0:
                logger.info(f"Cleaned up {cleanup_count} failed video uploads")

            return cleanup_count

        except Exception as e:
            logger.error(f"Failed to cleanup failed uploads: {e}", exc_info=True)
            return 0
