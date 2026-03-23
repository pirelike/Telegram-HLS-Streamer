"""Multi-bot Telegram uploader with round-robin distribution.

Uploads HLS segments and subtitle files to Telegram channels,
storing file_ids for later retrieval during streaming.
"""

import asyncio
import logging
import os
import re
import threading
import time

try:
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from telegram.error import (
        BadRequest,
        Forbidden,
        NetworkError,
        RetryAfter,
        TimedOut,
    )
except ImportError:  # pragma: no cover - fallback for minimal test environments
    class Bot:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

    class HTTPXRequest:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

    class BadRequest(Exception):  # type: ignore[no-redef]
        pass

    class Forbidden(Exception):  # type: ignore[no-redef]
        pass

    class NetworkError(Exception):  # type: ignore[no-redef]
        pass

    class RetryAfter(Exception):  # type: ignore[no-redef]
        def __init__(self, retry_after=None):
            super().__init__(retry_after)
            self.retry_after = retry_after

    class TimedOut(Exception):  # type: ignore[no-redef]
        pass

from config import Config

logger = logging.getLogger(__name__)


def _normalize_error_type(exc_type, fallback_name):
    """Replace broad placeholder stubs with distinct local exception classes."""
    if isinstance(exc_type, type) and exc_type not in (Exception, BaseException):
        return exc_type
    return type(fallback_name, (Exception,), {})


BadRequest = _normalize_error_type(BadRequest, "BadRequest")
Forbidden = _normalize_error_type(Forbidden, "Forbidden")
NetworkError = _normalize_error_type(NetworkError, "NetworkError")
RetryAfter = _normalize_error_type(RetryAfter, "RetryAfter")
TimedOut = _normalize_error_type(TimedOut, "TimedOut")


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
        self.metrics = {
            "upload_count": 0,
            "upload_errors": 0,
            "upload_total_seconds": 0.0,
            "download_count": 0,
            "download_errors": 0,
            "download_total_seconds": 0.0,
        }
        self._metrics_lock = threading.Lock()

    @staticmethod
    def _format_health_error(exc):
        """Collapse Telegram probe failures into short, stable monitoring strings."""
        if isinstance(exc, Forbidden):
            return "forbidden"
        if isinstance(exc, RetryAfter):
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                return f"rate_limited:{retry_after}"
            return "rate_limited"
        if isinstance(exc, TimedOut):
            return "timeout"
        if isinstance(exc, NetworkError):
            return "network_error"
        message = str(exc).strip()
        if not message:
            return exc.__class__.__name__.lower()
        return f"{exc.__class__.__name__.lower()}: {message[:120]}"

    async def probe_health(self):
        """Verify that every configured bot can access its configured channel."""
        async def probe_bot(bot_entry):
            try:
                await bot_entry["bot"].get_chat(bot_entry["channel_id"])
                return {
                    "index": bot_entry["index"],
                    "channel_id": bot_entry["channel_id"],
                    "ok": True,
                    "error": None,
                }
            except Exception as exc:
                logger.warning(
                    "Health probe failed for bot %d channel %s: %s",
                    bot_entry["index"], bot_entry["channel_id"], exc,
                )
                return {
                    "index": bot_entry["index"],
                    "channel_id": bot_entry["channel_id"],
                    "ok": False,
                    "error": self._format_health_error(exc),
                }

        if not self.bots:
            return []

        results = await asyncio.gather(*(probe_bot(bot_entry) for bot_entry in self.bots))
        return sorted(results, key=lambda result: result["index"])

    def reload_bots(self):
        """Rebuild the bot list from current Config.BOTS without restarting.

        Old Bot objects stay alive until their references are dropped, so
        any in-flight uploads using old bot references complete normally.
        """
        new_bots = []
        for i, bot_config in enumerate(Config.BOTS):
            new_bots.append({
                "bot": Bot(
                    token=bot_config["token"],
                    request=HTTPXRequest(connection_pool_size=16),
                    get_updates_request=HTTPXRequest(connection_pool_size=4),
                ),
                "channel_id": bot_config["channel_id"],
                "index": i,
            })
        self.bots = new_bots
        self._bot_counter = 0
        self._bot_locks = None  # will be recreated lazily on next use
        logger.info("Reloaded %d Telegram bots", len(self.bots))

    def _next_bot(self):
        """Round-robin bot selection (counter is atomic enough for asyncio single-thread,
        but we use modular arithmetic to stay safe even with concurrent coroutines)."""
        if not self.bots:
            raise RuntimeError("No Telegram bots configured")
        # Grab-and-increment; safe under asyncio (single-threaded event loop)
        idx = self._bot_counter
        self._bot_counter = (idx + 1) % len(self.bots)
        return self.bots[idx % len(self.bots)]

    def _record_metric(self, prefix, elapsed, error=False):
        """Increment operation counters under the metrics lock."""
        with self._metrics_lock:
            if error:
                self.metrics[f"{prefix}_errors"] += 1
            else:
                self.metrics[f"{prefix}_count"] += 1
                self.metrics[f"{prefix}_total_seconds"] += elapsed

    def get_metrics(self):
        """Return a snapshot copy of the metrics dict."""
        with self._metrics_lock:
            return dict(self.metrics)

    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError()

    async def _sleep_with_cancel(self, delay, cancel_event):
        if delay <= 0:
            self._raise_if_cancelled(cancel_event)
            return
        end = time.monotonic() + delay
        while True:
            self._raise_if_cancelled(cancel_event)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.25, remaining))

    async def _upload_file(self, file_path, bot_entry, retries=3, cancel_event=None):
        """Upload a single file to Telegram with retry logic."""
        self._raise_if_cancelled(cancel_event)
        
        if not os.path.exists(file_path):
            self._raise_if_cancelled(cancel_event)
            raise FileNotFoundError(f"Segment file {file_path} not found")

        bot = bot_entry["bot"]
        channel_id = bot_entry["channel_id"]
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size > Config.TELEGRAM_MAX_FILE_SIZE:
            raise RuntimeError(
                f"Segment {file_name} is {file_size} bytes, exceeds Telegram limit of {Config.TELEGRAM_MAX_FILE_SIZE}"
            )

        t0 = time.monotonic()
        for attempt in range(retries):
            self._raise_if_cancelled(cancel_event)
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
                self._record_metric("upload", time.monotonic() - t0)
                return UploadedSegment(file_id, bot_entry["index"], file_name, file_size)

            except BadRequest as e:
                logger.error("Bad request for %s (not retrying): %s", file_name, e)
                self._record_metric("upload", time.monotonic() - t0, error=True)
                raise RuntimeError(f"Telegram rejected upload for {file_name}: {e}") from e
            except Forbidden as e:
                logger.error(
                    "Forbidden while uploading %s via bot %d",
                    file_name, bot_entry["index"],
                )
                self._record_metric("upload", time.monotonic() - t0, error=True)
                raise RuntimeError(
                    f"Bot {bot_entry['index']} is forbidden from channel {channel_id}"
                ) from e
            except UploadIntegrityError:
                logger.error(
                    "Size mismatch after uploading %s — not retrying (corrupted transfer)",
                    file_name,
                )
                self._record_metric("upload", time.monotonic() - t0, error=True)
                raise
            except TimedOut:
                wait = 2 ** attempt
                logger.warning("Upload timeout for %s, retry in %ds", file_name, wait)
                await self._sleep_with_cancel(wait, cancel_event)
            except NetworkError as e:
                wait = 2 ** attempt
                logger.warning("Network error for %s: %s, retry in %ds", file_name, e, wait)
                await self._sleep_with_cancel(wait, cancel_event)
            except RetryAfter as e:
                retry_after = getattr(e, "retry_after", 1)
                logger.warning("Rate limited, waiting %d seconds", retry_after)
                await self._sleep_with_cancel(retry_after, cancel_event)
            except Exception as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning("Upload error for %s: %s, retry in %ds", file_name, e, wait)
                    await self._sleep_with_cancel(wait, cancel_event)
                else:
                    self._record_metric("upload", time.monotonic() - t0, error=True)
                    raise

        self._record_metric("upload", time.monotonic() - t0, error=True)
        raise RuntimeError(f"Failed to upload {file_path} after {retries} attempts")

    async def _upload_file_with_bot_lock(self, file_path, bot_entry, cancel_event=None):
        """Upload file while ensuring one in-flight upload per bot."""
        if self._bot_locks is None:
            self._bot_locks = [asyncio.Lock() for _ in self.bots]
        bot_index = bot_entry["index"]
        async with self._bot_locks[bot_index]:
            return await self._upload_file(file_path, bot_entry, cancel_event=cancel_event)

    async def upload_files(self, files, progress_callback=None, cancel_event=None):
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
            self._raise_if_cancelled(cancel_event)
            bot_entry = self._next_bot()
            async with semaphore:
                segment = await self._upload_file_with_bot_lock(
                    file_path,
                    bot_entry,
                    cancel_event=cancel_event,
                )
            completed += 1
            if progress_callback:
                progress_callback(completed, total, key)
            return key, segment

        tasks = [asyncio.create_task(worker(key, path)) for key, path in files]
        try:
            for task in asyncio.as_completed(tasks):
                self._raise_if_cancelled(cancel_event)
                key, segment = await task
                uploaded[key] = segment
        except (asyncio.CancelledError, Exception):
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise

        return uploaded

    async def upload_job(self, processing_result, progress_callback=None, cancel_event=None):
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

        # 4. Thumbnail (optional)
        thumbnail_path = getattr(processing_result, "thumbnail_path", None)
        if thumbnail_path and isinstance(thumbnail_path, str) and os.path.exists(thumbnail_path):
            all_upload_tasks.append(("thumbnail", [("thumbnail/thumbnail.jpg", thumbnail_path)]))
            total_files += 1

        def on_segment(current, total, name):
            nonlocal uploaded_files
            uploaded_files += 1
            if progress_callback:
                progress_callback(uploaded_files, total_files, name)

        # Execute uploads using the pre-collected lists
        for category, file_list in all_upload_tasks:
            self._raise_if_cancelled(cancel_event)
            if not file_list:
                continue
            category_segments = await self.upload_files(
                file_list,
                on_segment,
                cancel_event=cancel_event,
            )
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
        t0 = time.monotonic()
        for attempt in range(retries):
            try:
                file = await bot.get_file(file_id)
                data = await file.download_as_bytearray()
                self._record_metric("download", time.monotonic() - t0)
                return data
            except (TimedOut, NetworkError) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning("Download retry %d for file_id=%s: %s", attempt + 1, file_id[:20], e)
                    await asyncio.sleep(wait)
                else:
                    logger.error("Download failed after %d attempts for file_id=%s", retries, file_id[:20])
                    self._record_metric("download", time.monotonic() - t0, error=True)
                    raise
