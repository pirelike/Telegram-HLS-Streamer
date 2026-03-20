"""Multi-bot Telegram uploader with round-robin distribution.

Uploads HLS segments and subtitle files to Telegram channels,
storing file_ids for later retrieval during streaming.
"""

import asyncio
import logging
import os
import re
import time

from telegram import Bot
from telegram.request import HTTPXRequest
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TimedOut,
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


class UploadIntegrityError(RuntimeError):
    """Raised when Telegram reports a file size that does not match the local file."""


class TelegramUploader:
    def __init__(self):
        self.bots = []
        for i, bot_config in enumerate(Config.BOTS):
            self.bots.append({
                "bot": Bot(
                    token=bot_config["token"],
                    request=HTTPXRequest(connection_pool_size=16),
                    get_updates_request=HTTPXRequest(connection_pool_size=4),
                ),
                "channel_id": bot_config["channel_id"],
                "index": i,
            })
        self._bot_counter = 0
        # Locks are created lazily inside the event loop to support Python 3.8/3.9,
        # where asyncio.Lock() cannot be created outside a running event loop.
        self._bot_locks: list | None = None

    def _next_bot(self):
        """Round-robin bot selection (counter is atomic enough for asyncio single-thread,
        but we use modular arithmetic to stay safe even with concurrent coroutines)."""
        if not self.bots:
            raise RuntimeError("No Telegram bots configured")
        # Grab-and-increment; safe under asyncio (single-threaded event loop)
        idx = self._bot_counter
        self._bot_counter = (idx + 1) % len(self.bots)
        return self.bots[idx % len(self.bots)]

    async def _upload_file(self, file_path, bot_entry, retries=3):
        """Upload a single file to Telegram with retry logic."""
        bot = bot_entry["bot"]
        channel_id = bot_entry["channel_id"]
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size > Config.TELEGRAM_MAX_FILE_SIZE:
            raise RuntimeError(
                f"Segment {file_name} is {file_size} bytes, exceeds Telegram limit of {Config.TELEGRAM_MAX_FILE_SIZE}"
            )

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
                if message.document.file_size != file_size:
                    raise UploadIntegrityError(
                        f"Upload corrupted: size mismatch {message.document.file_size} != {file_size}"
                    )

                logger.debug(
                    "Uploaded %s via bot %d -> file_id=%s",
                    file_name, bot_entry["index"], file_id[:20],
                )
                return UploadedSegment(file_id, bot_entry["index"], file_name, file_size)

            except BadRequest as e:
                logger.error("Bad request for %s (not retrying): %s", file_name, e)
                raise RuntimeError(f"Telegram rejected upload for {file_name}: {e}") from e
            except Forbidden as e:
                logger.error(
                    "Forbidden while uploading %s via bot %d",
                    file_name, bot_entry["index"],
                )
                raise RuntimeError(
                    f"Bot {bot_entry['index']} is forbidden from channel {channel_id}"
                ) from e
            except UploadIntegrityError:
                logger.error(
                    "Size mismatch after uploading %s — not retrying (corrupted transfer)",
                    file_name,
                )
                raise
            except TimedOut:
                wait = 2 ** attempt
                logger.warning("Upload timeout for %s, retry in %ds", file_name, wait)
                await asyncio.sleep(wait)
            except NetworkError as e:
                wait = 2 ** attempt
                logger.warning("Network error for %s: %s, retry in %ds", file_name, e, wait)
                await asyncio.sleep(wait)
            except RetryAfter as e:
                retry_after = getattr(e, "retry_after", 1)
                logger.warning("Rate limited, waiting %d seconds", retry_after)
                await asyncio.sleep(retry_after)
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
        if self._bot_locks is None:
            self._bot_locks = [asyncio.Lock() for _ in self.bots]
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

        # Create per-bot locks inside the running event loop (required for Python 3.8/3.9).
        if self._bot_locks is None:
            self._bot_locks = [asyncio.Lock() for _ in self.bots]

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

        # Collect all files to upload first to avoid double listdir()
        # and to get accurate total count
        all_upload_tasks = []

        # 1. Video files
        for i, (_, tier_dir, _, _, _) in enumerate(processing_result.video_playlists):
            video_files = [
                (f"video_{i}/{filename}", os.path.join(tier_dir, filename))
                for filename in sorted(os.listdir(tier_dir))
                if filename.endswith(".ts")
            ]
            all_upload_tasks.append(("video", video_files))
            total_files += len(video_files)

        # 2. Audio files
        for i, (_, audio_dir, _, _, _) in enumerate(processing_result.audio_playlists):
            audio_files = [
                (f"audio_{i}/{filename}", os.path.join(audio_dir, filename))
                for filename in sorted(os.listdir(audio_dir))
                if filename.endswith(".ts")
            ]
            all_upload_tasks.append(("audio", audio_files))
            total_files += len(audio_files)

        # 3. Subtitle files
        subtitle_files = [
            (f"sub_{enum_idx}/subtitles.vtt", vtt_path)
            for vtt_path, _, _, _, enum_idx, _ in processing_result.subtitle_files
        ]
        all_upload_tasks.append(("sub", subtitle_files))
        total_files += len(subtitle_files)

        def on_segment(current, total, name):
            nonlocal uploaded_files
            uploaded_files += 1
            if progress_callback:
                progress_callback(uploaded_files, total_files, name)

        # Execute uploads using the pre-collected lists
        for category, file_list in all_upload_tasks:
            if not file_list:
                continue
            category_segments = await self.upload_files(file_list, on_segment)
            result.segments.update(category_segments)

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
        # Telegram file_id validation (no whitespace/newlines)
        if not file_id or not re.match(r"^[a-zA-Z0-9_-]{50,255}$", str(file_id)):
            raise ValueError("Invalid or malformed Telegram file_id")

        if bot_index < 0 or bot_index >= len(self.bots):
            raise RuntimeError(
                f"Bot index {bot_index} out of range (only {len(self.bots)} bots configured). "
                f"The segment was uploaded by a bot that is no longer available."
            )
        bot = self.bots[bot_index]["bot"]
        file = await bot.get_file(file_id)
        return file.file_path

    async def get_file_bytes(self, file_id, bot_index, retries=3):
        """Download bytes for a file using the same bot that uploaded it."""
        # Telegram file_id validation (no whitespace/newlines)
        if not file_id or not re.match(r"^[a-zA-Z0-9_-]{50,255}$", str(file_id)):
            raise ValueError("Invalid or malformed Telegram file_id")

        if bot_index < 0 or bot_index >= len(self.bots):
            raise RuntimeError(
                f"Bot index {bot_index} out of range (only {len(self.bots)} bots configured). "
                f"The segment was uploaded by a bot that is no longer available."
            )
        bot = self.bots[bot_index]["bot"]
        for attempt in range(retries):
            try:
                file = await bot.get_file(file_id)
                return await file.download_as_bytearray()
            except (TimedOut, NetworkError) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning("Download retry %d for file_id=%s: %s", attempt + 1, file_id[:20], e)
                    await asyncio.sleep(wait)
                else:
                    logger.error("Download failed after %d attempts for file_id=%s", retries, file_id[:20])
                    raise
