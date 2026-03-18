"""Multi-bot Telegram uploader with round-robin distribution.

Uploads HLS segments and subtitle files to Telegram channels,
storing file_ids for later retrieval during streaming.
"""

import asyncio
import logging
import os
import time

from telegram import Bot
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
    Unauthorized,
)

from config import Config

logger = logging.getLogger(__name__)


class UploadedSegment:
    """Represents a segment uploaded to Telegram."""

    def __init__(self, file_id, bot_index, file_name, file_size):
        self.file_id = file_id
        self.bot_index = bot_index
        self.file_name = file_name
        self.file_size = file_size


class UploadResult:
    """Result of uploading all segments for a job."""

    def __init__(self, job_id):
        self.job_id = job_id
        # Maps: "video/segment_0001.ts" -> UploadedSegment
        self.segments = {}
        self.total_bytes = 0
        self.total_files = 0


class TelegramUploader:
    def __init__(self):
        self.bots = []
        for i, bot_config in enumerate(Config.BOTS):
            self.bots.append({
                "bot": Bot(token=bot_config["token"]),
                "channel_id": bot_config["channel_id"],
                "index": i,
            })
        self._bot_counter = 0
        self._bot_locks = [asyncio.Lock() for _ in self.bots]

    def _next_bot(self):
        """Round-robin bot selection."""
        if not self.bots:
            raise RuntimeError("No Telegram bots configured")
        bot = self.bots[self._bot_counter % len(self.bots)]
        self._bot_counter += 1
        return bot

    async def _upload_file(self, file_path, bot_entry, retries=3):
        """Upload a single file to Telegram with retry logic."""
        bot = bot_entry["bot"]
        channel_id = bot_entry["channel_id"]
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        for attempt in range(retries):
            try:
                with open(file_path, "rb") as f:
                    message = await bot.send_document(
                        chat_id=channel_id,
                        document=f,
                        filename=file_name,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
                file_id = message.document.file_id
                logger.debug(
                    "Uploaded %s via bot %d -> file_id=%s",
                    file_name, bot_entry["index"], file_id[:20],
                )
                return UploadedSegment(file_id, bot_entry["index"], file_name, file_size)

            except RetryAfter as e:
                logger.warning("Rate limited, waiting %d seconds", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                wait = 2 ** attempt
                logger.warning("Upload timeout for %s, retry in %ds", file_name, wait)
                await asyncio.sleep(wait)
            except NetworkError as e:
                wait = 2 ** attempt
                logger.warning("Network error for %s: %s, retry in %ds", file_name, e, wait)
                await asyncio.sleep(wait)
            except BadRequest as e:
                logger.error("Bad request for %s (not retrying): %s", file_name, e)
                raise RuntimeError(f"Telegram rejected upload for {file_name}: {e}") from e
            except Unauthorized as e:
                logger.error(
                    "Unauthorized while uploading %s via bot %d",
                    file_name, bot_entry["index"],
                )
                raise RuntimeError(
                    f"Bot {bot_entry['index']} is unauthorized for channel {channel_id}"
                ) from e
            except Exception as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning("Upload error for %s: %s, retry in %ds", file_name, e, wait)
                    await asyncio.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"Failed to upload {file_path} after {retries} attempts")

    async def _upload_file_with_bot_lock(self, file_path, bot_entry):
        """Upload file while ensuring one in-flight upload per bot."""
        bot_index = bot_entry["index"]
        async with self._bot_locks[bot_index]:
            return await self._upload_file(file_path, bot_entry)

    async def upload_files(self, files, progress_callback=None):
        """Upload provided files in parallel with per-bot serialization.

        Args:
            files: list of (key, file_path) tuples
            progress_callback: callback(current, total, key)
        """
        if not files:
            return {}

        max_parallelism = min(
            len(self.bots),
            max(1, Config.UPLOAD_PARALLELISM),
            len(files),
        )
        semaphore = asyncio.Semaphore(max_parallelism)
        uploaded = {}
        total = len(files)
        completed = 0

        async def worker(key, file_path):
            nonlocal completed
            bot_entry = self._next_bot()
            async with semaphore:
                segment = await self._upload_file_with_bot_lock(file_path, bot_entry)
            completed += 1
            if progress_callback:
                progress_callback(completed, total, key)
            return key, segment

        tasks = [asyncio.create_task(worker(key, path)) for key, path in files]
        for task in asyncio.as_completed(tasks):
            key, segment = await task
            uploaded[key] = segment

        return uploaded

    async def upload_job(self, processing_result, progress_callback=None):
        """Upload all segments from a processing result.

        Handles video segments, audio track segments, and subtitle files.
        """
        result = UploadResult(processing_result.job_id)
        total_files = 0
        uploaded_files = 0

        # Count total files
        if processing_result.video_playlist:
            total_files += len([
                f for f in os.listdir(processing_result.output_dir)
                if f.endswith(".ts")
            ])
        for _, audio_dir, _, _, _ in processing_result.audio_playlists:
            total_files += len([
                f for f in os.listdir(audio_dir)
                if f.endswith(".ts")
            ])
        for vtt_path, _, _, _ in processing_result.subtitle_files:
            total_files += 1

        def on_segment(current, total, name):
            nonlocal uploaded_files
            uploaded_files += 1
            if progress_callback:
                progress_callback(uploaded_files, total_files, name)

        # Upload video segments
        if processing_result.video_playlist:
            video_files = [
                (f"video/{filename}", os.path.join(processing_result.output_dir, filename))
                for filename in sorted(os.listdir(processing_result.output_dir))
                if filename.endswith(".ts")
            ]
            video_segments = await self.upload_files(video_files, on_segment)
            result.segments.update(video_segments)

        # Upload audio track segments
        for i, (_, audio_dir, lang, title, _) in enumerate(processing_result.audio_playlists):
            audio_files = [
                (f"audio_{i}/{filename}", os.path.join(audio_dir, filename))
                for filename in sorted(os.listdir(audio_dir))
                if filename.endswith(".ts")
            ]
            audio_segments = await self.upload_files(audio_files, on_segment)
            result.segments.update(audio_segments)

        # Upload subtitle files
        subtitle_files = [
            (f"sub_{i}/subtitles.vtt", vtt_path)
            for i, (vtt_path, _, _, _) in enumerate(processing_result.subtitle_files)
        ]
        subtitle_segments = await self.upload_files(subtitle_files, on_segment)
        result.segments.update(subtitle_segments)

        result.total_files = len(result.segments)
        result.total_bytes = sum(s.file_size for s in result.segments.values())

        logger.info(
            "Upload complete for %s: %d files, %d bytes",
            result.job_id, result.total_files, result.total_bytes,
        )
        return result

    async def get_file_url(self, file_id, bot_index):
        """Get a temporary download URL for a file from Telegram.

        The bot_index must match the bot that originally uploaded the file,
        since Telegram file_ids are only valid for the bot that created them.
        """
        if bot_index < 0 or bot_index >= len(self.bots):
            raise RuntimeError(
                f"Bot index {bot_index} out of range (only {len(self.bots)} bots configured). "
                f"The segment was uploaded by a bot that is no longer available."
            )
        bot = self.bots[bot_index]["bot"]
        file = await bot.get_file(file_id)
        return file.file_path
