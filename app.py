"""Telegram HLS Streamer - Main Application.

Web server that handles:
  - Chunked resumable file uploads (supports 50GB+ files)
  - Video processing (split into video/audio/subtitle streams)
  - Telegram upload via multi-bot round-robin
  - HLS playlist serving (master + media playlists)
  - Segment proxying from Telegram
"""

import asyncio
import atexit
import collections
import concurrent.futures
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from threading import Lock, RLock, Thread

import aiohttp
try:
    from werkzeug.utils import secure_filename
except ImportError:  # pragma: no cover - fallback for minimal test environments
    def secure_filename(filename):
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(filename or "")).strip("._")
        return cleaned[:255]

from flask import (
    Flask, jsonify, render_template, request, Response, stream_with_context,
)

from config import Config
from stream_analyzer import analyze
from video_processor import process, cleanup
from telegram_uploader import TelegramUploader
from hls_manager import (
    register_job, generate_master_playlist, generate_media_playlist,
    get_segment_info, list_jobs, get_job, count_jobs,
)
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Per-chunk limit only — the full file is assembled from many chunks
app.config["MAX_CONTENT_LENGTH"] = Config.UPLOAD_CHUNK_SIZE * 2
_WATCH_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "watch_settings.json")

# Track active jobs: job_id -> {status, progress, ...}
_active_jobs = {}
_job_source_info = {}
_job_runtime = {}

# Track in-progress chunked uploads: upload_id -> {path, filename, received, total, ...}
_pending_uploads = {}
_pending_filenames = {}  # filename -> upload_id (for O(1) duplicate check)
_pending_uploads_lock = Lock()  # protects _pending_uploads, _pending_filenames, _upload_locks
_upload_locks = {}
_last_pending_cleanup = 0.0
_watcher_started = False
_folder_watcher_started = False
_watch_state_lock = Lock()
_watch_candidates = {}
_watch_claimed_paths = set()
_watch_failed_signatures = {}

# ─── Upload Rate Limiting ───
# Per-IP sliding window: maps IP -> deque of request timestamps
_rate_limit_hits = collections.defaultdict(collections.deque)
_rate_limit_lock = Lock()
# Per-IP pending upload count (incremented on init, decremented on finalize/expiry)
_pending_uploads_per_ip = collections.Counter()


class _JobRuntime:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.lock = Lock()
        self.current_process = None
        self.upload_future = None


def _get_job_runtime(job_id):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        runtime = _JobRuntime()
        _job_runtime[job_id] = runtime
    return runtime


def _set_job_process(job_id, proc):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        return
    with runtime.lock:
        runtime.current_process = proc


def _clear_job_process(job_id, proc):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        return
    with runtime.lock:
        if runtime.current_process is proc:
            runtime.current_process = None


def _set_job_upload_future(job_id, future):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        return
    with runtime.lock:
        runtime.upload_future = future


def _clear_job_upload_future(job_id, future):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        return
    with runtime.lock:
        if runtime.upload_future is future:
            runtime.upload_future = None


def _terminate_process(proc, job_id):
    if proc is None or proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=5)
        logger.info("Job %s: terminated FFmpeg process %s", job_id, proc.pid)
        return
    except subprocess.TimeoutExpired:
        logger.warning("Job %s: FFmpeg process %s did not exit after terminate()", job_id, proc.pid)
    except Exception as exc:
        logger.debug("Job %s: terminate() failed for process %s: %s", job_id, getattr(proc, "pid", "?"), exc)

    try:
        proc.kill()
        logger.info("Job %s: killed FFmpeg process %s", job_id, proc.pid)
    except Exception as exc:
        logger.debug("Job %s: kill() failed for process %s: %s", job_id, getattr(proc, "pid", "?"), exc)


def _request_job_stop(job_id):
    runtime = _job_runtime.get(job_id)
    if runtime is None:
        return

    runtime.cancel_event.set()
    with runtime.lock:
        proc = runtime.current_process
        future = runtime.upload_future

    _terminate_process(proc, job_id)
    if future and not future.done():
        future.cancel()


def _get_client_ip():
    """Return the client IP, respecting X-Forwarded-For when behind a proxy."""
    if Config.BEHIND_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _check_rate_limit():
    """Return a 429 response tuple if the client IP exceeds the rate limit, else None."""
    window = Config.UPLOAD_RATE_LIMIT_WINDOW
    max_requests = Config.UPLOAD_RATE_LIMIT_MAX_REQUESTS
    if max_requests <= 0:
        return None  # rate limiting disabled

    ip = _get_client_ip()
    now = time.time()
    cutoff = now - window

    with _rate_limit_lock:
        timestamps = _rate_limit_hits[ip]
        # Evict timestamps outside the window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if len(timestamps) >= max_requests:
            return jsonify({
                "error": "Rate limit exceeded. Try again later.",
            }), 429
        timestamps.append(now)

    return None


_TERMINAL_JOB_STATES = {"complete", "error"}
# How long to keep finished jobs in _active_jobs before eviction (seconds).
_ACTIVE_JOB_RETENTION = 300  # 5 minutes
# Protects status transitions so cancel_job cannot overwrite a just-completed job.
_job_status_lock = RLock()

# ─── Job Queue ───
# Supports multiple concurrent processing jobs via a bounded worker pool.
_job_queue = queue.Queue()
_queue_order = []         # ordered list of job_ids waiting to be processed
_queue_order_lock = Lock()
_queue_workers_started = False


def _get_base_url():
    """Determine base URL for playlist generation."""
    if Config.FORCE_HTTPS:
        scheme = "https"
    elif Config.BEHIND_PROXY:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    else:
        scheme = request.scheme
    return f"{scheme}://{request.host}"


def _is_origin_allowed(origin: str) -> bool:
    """Check whether CORS origin is allowed."""
    if not origin:
        return False
    if "*" in Config.CORS_ALLOWED_ORIGINS:
        return True
    return origin in Config.CORS_ALLOWED_ORIGINS


def _normalize_watch_settings(data=None):
    """Validate and normalize mutable watcher settings."""
    data = data or {}
    watch_enabled = data.get("watch_enabled", Config.WATCH_ENABLED)
    if isinstance(watch_enabled, str):
        watch_enabled = watch_enabled.strip().lower() in {"1", "true", "yes", "on"}
    else:
        watch_enabled = bool(watch_enabled)

    watch_root_raw = str(data.get("watch_root", Config.WATCH_ROOT or "") or "").strip()
    watch_done_raw = str(data.get("watch_done_dir", Config.WATCH_DONE_DIR or "") or "").strip()

    watch_root = (
        os.path.abspath(os.path.expanduser(watch_root_raw))
        if watch_root_raw
        else ""
    )
    if watch_enabled and not watch_root:
        raise ValueError("watch_root is required when watch_enabled is true")

    watch_done_dir = (
        os.path.abspath(os.path.expanduser(watch_done_raw))
        if watch_done_raw
        else (os.path.join(watch_root, "done") if watch_root else "")
    )

    return {
        "watch_enabled": watch_enabled,
        "watch_root": watch_root,
        "watch_done_dir": watch_done_dir,
    }


def _current_watch_settings():
    settings = _normalize_watch_settings()
    settings["watch_running"] = bool(_folder_watcher_started and Config.WATCH_ENABLED)
    return settings


def _persist_watch_settings(settings):
    with open(_WATCH_SETTINGS_PATH, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, sort_keys=True)


def _apply_watch_settings(data=None, *, persist=False):
    """Apply watcher settings to the live process and optionally persist them."""
    settings = _normalize_watch_settings(data)
    previous_root = Config.WATCH_ROOT
    previous_done = Config.WATCH_DONE_DIR

    Config.WATCH_ENABLED = settings["watch_enabled"]
    Config.WATCH_ROOT = settings["watch_root"]
    Config.WATCH_DONE_DIR = settings["watch_done_dir"]

    if Config.WATCH_ROOT:
        os.makedirs(Config.WATCH_ROOT, exist_ok=True)
    if Config.WATCH_DONE_DIR:
        os.makedirs(Config.WATCH_DONE_DIR, exist_ok=True)

    if previous_root != Config.WATCH_ROOT or previous_done != Config.WATCH_DONE_DIR:
        with _watch_state_lock:
            _watch_candidates.clear()
            _watch_claimed_paths.clear()
            _watch_failed_signatures.clear()

    if persist:
        _persist_watch_settings(settings)

    return _current_watch_settings()


def _load_persisted_watch_settings():
    if not os.path.exists(_WATCH_SETTINGS_PATH):
        return
    try:
        with open(_WATCH_SETTINGS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        _apply_watch_settings(payload, persist=False)
        logger.info("Loaded persisted watch settings from %s", _WATCH_SETTINGS_PATH)
    except Exception as exc:
        logger.warning("Could not load persisted watch settings: %s", exc)


_async_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_async_loop_thread = None
_aiohttp_session = None
_loop_ready = threading.Event()
_segment_downloads = {}
_segment_download_lock = Lock()
_scheduled_segment_prefetches = set()
_segment_prefetch_lock = Lock()
_last_player_segment = {}          # (job_id, prefix) -> segment_key
_last_player_segment_lock = Lock()

_STREAM_EOF = object()


class _SegmentStreamError:
    def __init__(self, exc):
        self.exc = exc


class _SegmentDownloadState:
    def __init__(self, cache_key, enable_stream=False):
        self.cache_key = cache_key
        self.enable_stream = enable_stream
        self.stream_queue = queue.Queue(maxsize=4) if enable_stream else None
        self.stream_abandoned = threading.Event()
        self.completed = threading.Event()
        self.lock = Lock()
        self.temp_path = None
        self.error = None
        self.cached = False
        self.pending_followers = 0
        self.file_readers = 0

    def mark_waiting_follower(self):
        with self.lock:
            if self.completed.is_set():
                return False
            self.pending_followers += 1
            return True

    def promote_waiting_follower_to_reader(self):
        with self.lock:
            if self.pending_followers > 0:
                self.pending_followers -= 1
            self.file_readers += 1

    def finish_waiting_follower(self):
        with self.lock:
            if self.pending_followers > 0:
                self.pending_followers -= 1

    def acquire_completed_reader(self):
        with self.lock:
            if self.completed.is_set() and self.error is None and not self.cached and self.temp_path:
                self.file_readers += 1
                return True
            return False

    def release_reader(self):
        with self.lock:
            if self.file_readers > 0:
                self.file_readers -= 1

    def should_cleanup(self):
        with self.lock:
            if not self.completed.is_set():
                return False
            return self.pending_followers == 0 and self.file_readers == 0


def _run_async(coro, timeout=30):
    """Run an async coroutine synchronously from a Flask thread via the persistent loop."""
    future = asyncio.run_coroutine_threadsafe(coro, _async_loop)
    return future.result(timeout=timeout)


def _start_persistent_loop():
    global _async_loop_thread, _aiohttp_session

    async def _init():
        global _aiohttp_session
        _aiohttp_session = aiohttp.ClientSession()

    def run_loop():
        asyncio.set_event_loop(_async_loop)
        _async_loop.call_soon(_loop_ready.set)
        _async_loop.run_forever()

    _async_loop_thread = Thread(target=run_loop, daemon=True, name="async-loop")
    _async_loop_thread.start()
    _loop_ready.wait(timeout=5)
    asyncio.run_coroutine_threadsafe(_init(), _async_loop).result(timeout=10)


def _shutdown_persistent_loop():
    async def _close():
        if _aiohttp_session and not _aiohttp_session.closed:
            await _aiohttp_session.close()
    try:
        asyncio.run_coroutine_threadsafe(_close(), _async_loop).result(timeout=5)
    except Exception:
        pass
    _async_loop.call_soon_threadsafe(_async_loop.stop)


atexit.register(_shutdown_persistent_loop)


def _cleanup_expired_pending_uploads(force=False):
    """Delete stale pending uploads from memory + disk."""
    global _last_pending_cleanup
    now = time.time()
    if not force and (now - _last_pending_cleanup) < Config.PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS:
        return

    ttl = Config.PENDING_UPLOAD_TTL_SECONDS
    expired_ids = []
    paths_to_remove = []

    with _pending_uploads_lock:
        for upload_id, info in list(_pending_uploads.items()):
            last_activity = info.get("last_activity_ts", info.get("created_ts", now))
            if now - last_activity > ttl:
                expired_ids.append(upload_id)

        for upload_id in expired_ids:
            info = _pending_uploads.pop(upload_id, None)
            if not info:
                continue
            _pending_filenames.pop(info.get("filename"), None)
            _upload_locks.pop(upload_id, None)
            ip = info.get("client_ip", "unknown")
            if _pending_uploads_per_ip[ip] > 0:
                _pending_uploads_per_ip[ip] -= 1
            path = info.get("path")
            if path:
                paths_to_remove.append((upload_id, info.get("filename"), path))

    # Remove files outside the lock to avoid holding it during I/O
    for upload_id, filename, path in paths_to_remove:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove expired pending upload file: %s", path)
        logger.info("Cleaned expired pending upload: %s (%s)", upload_id, filename)

    _last_pending_cleanup = now


def _job_timed_out(job):
    started = job.get("started_ts")
    if not started:
        return False
    return (time.time() - started) > Config.JOB_TIMEOUT_SECONDS


def _start_timeout_watcher():
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True

    def watch():
        while True:
            time.sleep(5)
            # Use list() to avoid dictionary changed size during iteration
            for job_id, job in list(_active_jobs.items()):
                status = job.get("status")
                if status in _TERMINAL_JOB_STATES:
                    continue
                if _job_timed_out(job):
                    with _job_status_lock:
                        if job.get("status") in _TERMINAL_JOB_STATES:
                            continue
                        job["status"] = "error"
                        job["timed_out"] = True
                        step = job.get("step", "processing")
                        job["error"] = (
                            f"Job timed out after {Config.JOB_TIMEOUT_SECONDS} seconds "
                            f"at step: {step}"
                        )
                        job["step"] = "Timed out"
                        job["finished_ts"] = time.time()
                    _request_job_stop(job_id)
                    logger.error("Job %s timed out at %s", job_id, step)

            # Evict finished jobs from _active_jobs to prevent unbounded memory growth.
            # Jobs that reached a terminal state more than _ACTIVE_JOB_RETENTION ago
            # are removed; they remain queryable from the database.
            now = time.time()
            evict_ids = [
                jid for jid, j in list(_active_jobs.items())
                if j.get("status") in _TERMINAL_JOB_STATES
                and now - j.get("finished_ts", now) > _ACTIVE_JOB_RETENTION
            ]
            for jid in evict_ids:
                _active_jobs.pop(jid, None)

    Thread(target=watch, daemon=True).start()


def _start_folder_watcher():
    """Start the background polling watcher for local auto-ingest."""
    global _folder_watcher_started
    if _folder_watcher_started or not Config.WATCH_ENABLED:
        return
    _folder_watcher_started = True

    def watch():
        logger.info(
            "Folder watcher enabled: root=%s done=%s poll=%ss stable=%ss",
            Config.WATCH_ROOT,
            Config.WATCH_DONE_DIR,
            Config.WATCH_POLL_SECONDS,
            Config.WATCH_STABLE_SECONDS,
        )
        while True:
            try:
                _watch_scan_once()
            except Exception:
                logger.exception("Watch-folder scan failed")
            time.sleep(Config.WATCH_POLL_SECONDS)

    Thread(target=watch, daemon=True, name="folder-watcher").start()


def _is_job_cancelled(job_id):
    with _job_status_lock:
        job = _active_jobs.get(job_id)
        if not job:
            return True
        return bool(job.get("timed_out") or job.get("cancelled"))


def _start_queue_workers():
    """Start worker threads that pull jobs from the queue and process them."""
    global _queue_workers_started
    if _queue_workers_started:
        return
    _queue_workers_started = True

    def worker():
        while True:
            job_id, file_path = _job_queue.get()
            try:
                with _queue_order_lock:
                    if job_id in _queue_order:
                        _queue_order.remove(job_id)
                _process_job(job_id, file_path)
            finally:
                db.close_conn()
                _job_queue.task_done()

    n_workers = max(1, Config.MAX_CONCURRENT_JOBS)
    for _ in range(n_workers):
        Thread(target=worker, daemon=True).start()
    logger.info("Started %d job queue worker(s)", n_workers)


def _enqueue_job(job_id, file_path):
    """Add a job to the processing queue and record its position."""
    with _queue_order_lock:
        _queue_order.append(job_id)
        position = len(_queue_order)
    with _job_status_lock:
        _active_jobs[job_id]["queue_position"] = position
    _job_queue.put((job_id, file_path))


def _start_retention_cleanup():
    """Background thread that periodically purges old completed jobs from the DB."""
    if Config.JOB_RETENTION_DAYS <= 0:
        return

    def cleanup_loop():
        while True:
            time.sleep(3600)  # check hourly
            try:
                count = db.delete_old_jobs(Config.JOB_RETENTION_DAYS)
                if count:
                    logger.info("Retention: purged %d old job(s)", count)
            except Exception:
                logger.exception("Retention cleanup failed")
            finally:
                db.close_conn()

    Thread(target=cleanup_loop, daemon=True).start()
    logger.info(
        "Retention cleanup enabled: jobs older than %d days will be purged hourly",
        Config.JOB_RETENTION_DAYS,
    )


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Cancel a running job."""
    job = _active_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found or already finished"}), 404

    with _job_status_lock:
        if job.get("status") in _TERMINAL_JOB_STATES:
            return jsonify({"error": "Cannot cancel a finished job"}), 400

        job["status"] = "error"
        job["cancelled"] = True
        job["error"] = "Job cancelled by user"
        job["step"] = "Cancelled"
        job["finished_ts"] = time.time()

    _request_job_stop(job_id)
    logger.info("Job %s cancelled by user", job_id)
    return jsonify({"message": "Job cancellation requested"})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job_endpoint(job_id):
    """Delete a completed job from the database."""
    if job_id in _active_jobs:
        status = _active_jobs[job_id].get("status")
        if status not in _TERMINAL_JOB_STATES:
            return jsonify({"error": "Cannot delete an active job; cancel it first"}), 400

    if not get_job(job_id):
        return jsonify({"error": "Job not found"}), 404

    db.delete_job(job_id)
    logger.info("Job %s deleted by user", job_id)
    return jsonify({"message": "Job deleted"})


# Shared TelegramUploader singleton — avoids creating fresh Bot objects on every
# segment proxy request or processing job.
_load_persisted_watch_settings()
_telegram_uploader = TelegramUploader()
_start_persistent_loop()

_start_timeout_watcher()
_start_queue_workers()
_start_folder_watcher()
_start_retention_cleanup()


# ─── Web UI ───

@app.route("/")
def index():
    return render_template("index.html")


# ─── Health Check ───

@app.route("/health")
def health():
    """Health check endpoint for load balancers and monitoring."""
    db_ok = False
    bot_results = []
    try:
        db.get_job("__healthcheck__")
        db_ok = True
    except Exception:
        pass

    try:
        bot_results = _run_async(_telegram_uploader.probe_health(), timeout=10)
    except concurrent.futures.TimeoutError:
        bot_results = [{
            "index": None,
            "channel_id": None,
            "ok": False,
            "error": "timeout",
        }]
    except Exception as exc:
        bot_results = [{
            "index": None,
            "channel_id": None,
            "ok": False,
            "error": f"probe_error: {exc.__class__.__name__}",
        }]

    bots_healthy = sum(1 for bot in bot_results if bot.get("ok"))
    bots_configured = len(_telegram_uploader.bots)
    bots_ok = bots_configured > 0 and bots_healthy == bots_configured
    status = "ok" if db_ok and bots_ok else "degraded"
    code = 200 if status == "ok" else 503

    return jsonify({
        "status": status,
        "db": db_ok,
        "bots_configured": bots_configured,
        "bots_healthy": bots_healthy,
        "bots": bot_results,
    }), code


@app.route("/api/watch-settings", methods=["GET", "POST"])
def watch_settings():
    """Read or update mutable watch-folder settings for the UI."""
    if request.method == "GET":
        return jsonify(_current_watch_settings())

    data = request.get_json() or {}
    try:
        settings = _apply_watch_settings(data, persist=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        logger.warning("Failed to save watch settings: %s", exc)
        return jsonify({"error": f"Could not save watch settings: {exc}"}), 500

    if settings["watch_enabled"]:
        _start_folder_watcher()
        try:
            _watch_scan_once()
        except Exception:
            logger.exception("Initial watch-folder scan failed after settings update")
    return jsonify(settings)


# ─── Chunked Upload API ───
#
# Flow:
#   1. POST /api/upload/init       → returns upload_id
#   2. POST /api/upload/chunk      → send each 10MB chunk (with retry)
#   3. POST /api/upload/finalize   → triggers processing pipeline
#
# If the connection drops mid-upload, the client resumes from the last
# successful chunk. Only the failed 10MB chunk needs to be re-sent,
# not the entire 50GB+ file.

def _bots_configured():
    """Return True if at least one Telegram bot is configured."""
    return len(_telegram_uploader.bots) > 0

@app.route("/api/upload/init", methods=["POST"])
def upload_init():
    """Initialize a chunked upload session."""
    if not _bots_configured():
        return jsonify({"error": "No Telegram bots configured."}), 503

    rate_limited = _check_rate_limit()
    if rate_limited:
        return rate_limited
    _cleanup_expired_pending_uploads()

    # Enforce per-IP pending upload limit
    if Config.MAX_PENDING_UPLOADS_PER_IP > 0:
        ip = _get_client_ip()
        with _pending_uploads_lock:
            if _pending_uploads_per_ip[ip] >= Config.MAX_PENDING_UPLOADS_PER_IP:
                return jsonify({
                    "error": "Too many pending uploads. Finalize or wait for existing uploads to expire.",
                }), 429

    data = request.get_json()
    if not data or "filename" not in data or "total_size" not in data:
        return jsonify({"error": "filename and total_size required"}), 400

    filename = secure_filename(data["filename"]) or "unnamed_upload"
    try:
        total_size = int(data["total_size"])
        total_chunks = int(data.get("total_chunks", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "total_size and total_chunks must be valid integers"}), 400

    if total_size <= 0:
        return jsonify({"error": "total_size must be a positive integer"}), 400
    if total_chunks < 0:
        return jsonify({"error": "total_chunks must be non-negative"}), 400

    if total_size > Config.MAX_UPLOAD_SIZE:
        return jsonify({
            "error": f"File too large. Max {Config.MAX_UPLOAD_SIZE // (1024**3)}GB"
        }), 413

    # Reject if there's already a pending upload for this filename
    with _pending_uploads_lock:
        if filename in _pending_filenames:
            uid = _pending_filenames[filename]
            return jsonify({
                "error": "An upload for this file is already in progress",
                "upload_id": uid,
            }), 409

    upload_id = uuid.uuid4().hex[:16]
    upload_path = os.path.join(Config.UPLOAD_DIR, f"{upload_id}_{filename}")

    # Create empty file (exclusive creation to avoid overwriting)
    try:
        fd = os.open(upload_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return jsonify({"error": "Upload file already exists"}), 409
    except OSError as e:
        logger.error("Failed to create upload file %s: %s", upload_path, e)
        return jsonify({"error": "Failed to create upload file on server"}), 500

    ip = _get_client_ip()
    with _pending_uploads_lock:
        _pending_uploads[upload_id] = {
            "path": upload_path,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "received_bytes": 0,
            "received_chunks": 0,
            "received_chunk_indices": set(),
            "created_ts": time.time(),
            "last_activity_ts": time.time(),
            "client_ip": ip,
        }
        _pending_filenames[filename] = upload_id
        _upload_locks[upload_id] = Lock()
        _pending_uploads_per_ip[ip] += 1

    logger.info(
        "Upload initialized: %s, file=%s, size=%d bytes (%d chunks)",
        upload_id, filename, total_size, total_chunks,
    )

    return jsonify({
        "upload_id": upload_id,
        "chunk_size": Config.UPLOAD_CHUNK_SIZE,
    })


@app.route("/api/upload/chunk", methods=["POST"])
def upload_chunk():
    """Receive a single chunk of an upload.

    Headers:
      X-Upload-Id: the upload session id
      X-Chunk-Index: 0-based chunk number
    Body: raw binary chunk data
    """
    rate_limited = _check_rate_limit()
    if rate_limited:
        return rate_limited
    _cleanup_expired_pending_uploads()
    upload_id = request.headers.get("X-Upload-Id")
    chunk_index = request.headers.get("X-Chunk-Index")

    if not upload_id or chunk_index is None:
        return jsonify({"error": "X-Upload-Id and X-Chunk-Index headers required"}), 400

    if upload_id not in _pending_uploads:
        return jsonify({"error": "Unknown upload_id. Call /api/upload/init first"}), 404

    upload = _pending_uploads[upload_id]
    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return jsonify({"error": "X-Chunk-Index must be an integer"}), 400
    if chunk_index < 0:
        return jsonify({"error": "X-Chunk-Index must be >= 0"}), 400

    # Read raw chunk from request body — streams directly, no buffering
    chunk_data = request.get_data()
    chunk_len = len(chunk_data)

    if chunk_len == 0:
        return jsonify({"error": "Empty chunk"}), 400

    total_size = upload["total_size"]
    offset = chunk_index * Config.UPLOAD_CHUNK_SIZE
    if offset >= total_size:
        return jsonify({"error": "Chunk index exceeds file size"}), 400
    if offset + chunk_len > total_size:
        return jsonify({"error": "Chunk exceeds declared total size"}), 400

    upload_lock = _upload_locks.setdefault(upload_id, Lock())
    with upload_lock:
        is_retry = chunk_index in upload["received_chunk_indices"]
        if (
            not is_retry
            and chunk_len != Config.UPLOAD_CHUNK_SIZE
            and offset + chunk_len != total_size
        ):
            return jsonify({"error": "Invalid chunk size for non-final chunk"}), 400

        try:
            current_size = os.path.getsize(upload["path"])
        except OSError:
            return jsonify({"error": "Upload file missing or inaccessible"}), 500
        if offset > current_size:
            return jsonify({
                "error": "Out-of-order chunk would create a file gap",
                "expected_offset": current_size,
            }), 409
        if offset < current_size and not is_retry:
            return jsonify({"error": "Chunk overlaps existing data"}), 409

        # Write chunk at validated offset (supports in-order + retry)
        try:
            with open(upload["path"], "r+b") as f:
                f.seek(offset)
                f.write(chunk_data)
        except OSError as e:
            logger.error("Failed to write chunk %d for upload %s: %s", chunk_index, upload_id, e)
            return jsonify({"error": f"Failed to write chunk: {e}"}), 500

        if not is_retry:
            upload["received_chunk_indices"].add(chunk_index)
            upload["received_bytes"] += chunk_len
            upload["received_chunks"] += 1
        upload["last_activity_ts"] = time.time()

    return jsonify({
        "chunk_index": chunk_index,
        "received_bytes": upload["received_bytes"],
        "received_chunks": upload["received_chunks"],
        "is_retry": is_retry,
    })


def _check_disk_space(file_size):
    """Check that at least 2x file_size bytes are free in the processing directory.

    Returns (ok, message). Caller should return HTTP 507 on failure.
    """
    try:
        usage = shutil.disk_usage(Config.PROCESSING_DIR)
        required = 2 * file_size
        if usage.free < required:
            free_gb = usage.free / (1024 ** 3)
            req_gb = required / (1024 ** 3)
            return False, (
                f"Insufficient disk space: {free_gb:.1f} GB free, "
                f"{req_gb:.1f} GB required (2x file size)"
            )
    except OSError as e:
        logger.warning("Could not check disk space: %s", e)
    return True, ""


def _remove_pending_upload(upload_id):
    """Remove a finalized pending upload from memory and per-IP tracking."""
    with _pending_uploads_lock:
        upload = _pending_uploads.pop(upload_id, None)
        if not upload:
            return None
        _pending_filenames.pop(upload.get("filename"), None)
        _upload_locks.pop(upload_id, None)
        ip = upload.get("client_ip", "unknown")
        if _pending_uploads_per_ip[ip] > 0:
            _pending_uploads_per_ip[ip] -= 1
        return upload


def _queue_local_file(file_path, *, filename=None, source_mode="upload", skip_disk_check=False):
    """Queue an existing local file for processing via the shared job pipeline."""
    actual_size = os.path.getsize(file_path)
    if not skip_disk_check:
        ok, msg = _check_disk_space(actual_size)
        if not ok:
            raise RuntimeError(msg)

    job_id = uuid.uuid4().hex[:12]
    _active_jobs[job_id] = {
        "status": "queued",
        "filename": filename or os.path.basename(file_path),
        "file_size": actual_size,
        "progress": 0,
        "step": "Queued for processing...",
        "started_ts": time.time(),
        "queue_position": None,
    }
    _job_source_info[job_id] = {
        "mode": source_mode,
        "path": file_path,
    }
    _get_job_runtime(job_id)
    _enqueue_job(job_id, file_path)
    return job_id, actual_size


def _normalize_watch_path(path):
    return os.path.realpath(os.path.abspath(path))


def _path_is_within(path, root):
    if not path or not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _watch_file_signature(path):
    stat_result = os.stat(path)
    return stat_result.st_size, stat_result.st_mtime_ns


def _is_supported_watch_video(path):
    return os.path.splitext(path)[1].lower() in Config.WATCH_VIDEO_EXTENSIONS


def _is_ignored_watch_path(path):
    name = os.path.basename(path).lower()
    if name.startswith("."):
        return True
    return any(name.endswith(suffix) for suffix in Config.WATCH_IGNORE_SUFFIXES)


def _iter_watch_video_files():
    """Yield supported video files under the watched root, excluding done/ and temp files."""
    root = _normalize_watch_path(Config.WATCH_ROOT)
    done_dir = _normalize_watch_path(Config.WATCH_DONE_DIR)
    for dirpath, dirnames, filenames in os.walk(root):
        norm_dir = _normalize_watch_path(dirpath)
        if _path_is_within(norm_dir, done_dir):
            continue

        kept_dirs = []
        for dirname in dirnames:
            child_path = _normalize_watch_path(os.path.join(dirpath, dirname))
            if _path_is_within(child_path, done_dir):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if _is_ignored_watch_path(path):
                continue
            if not os.path.isfile(path):
                continue
            if not _is_supported_watch_video(path):
                continue
            yield _normalize_watch_path(path)


def _claim_watch_file_if_stable(path):
    """Claim a watched file once it has stopped changing for the quiet period."""
    now = time.time()
    signature = _watch_file_signature(path)
    with _watch_state_lock:
        if path in _watch_claimed_paths:
            return False
        if _watch_failed_signatures.get(path) == signature:
            return False

        entry = _watch_candidates.get(path)
        if not entry or entry["signature"] != signature:
            _watch_candidates[path] = {
                "signature": signature,
                "stable_since": now,
            }
            return False

        if now - entry["stable_since"] < Config.WATCH_STABLE_SECONDS:
            return False

        _watch_claimed_paths.add(path)
        _watch_candidates.pop(path, None)
        _watch_failed_signatures.pop(path, None)
        return True


def _release_watch_file(path, *, success):
    """Release watcher state after a queued file succeeds or fails."""
    norm_path = _normalize_watch_path(path)
    with _watch_state_lock:
        _watch_claimed_paths.discard(norm_path)
        _watch_candidates.pop(norm_path, None)
        if success:
            _watch_failed_signatures.pop(norm_path, None)
            return
        try:
            _watch_failed_signatures[norm_path] = _watch_file_signature(norm_path)
        except OSError:
            _watch_failed_signatures.pop(norm_path, None)


def _build_done_destination(path):
    root = _normalize_watch_path(Config.WATCH_ROOT)
    done_dir = _normalize_watch_path(Config.WATCH_DONE_DIR)
    rel_path = os.path.relpath(path, root)
    if rel_path.startswith(".."):
        rel_path = os.path.basename(path)

    destination = os.path.join(done_dir, rel_path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if not os.path.exists(destination):
        return destination

    base, ext = os.path.splitext(destination)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _move_watched_file_to_done(path):
    destination = _build_done_destination(path)
    shutil.move(path, destination)
    return destination


def _watch_scan_once():
    """Scan the watched root once and queue any stable video files."""
    if not Config.WATCH_ENABLED:
        return []

    queued = []
    for path in _iter_watch_video_files():
        try:
            if not _claim_watch_file_if_stable(path):
                continue
        except OSError:
            continue

        try:
            job_id, _ = _queue_local_file(
                path,
                filename=os.path.basename(path),
                source_mode="watch",
            )
        except Exception as exc:
            logger.warning("Watcher could not queue %s: %s", path, exc)
            _release_watch_file(path, success=False)
            continue

        queued.append(job_id)
        logger.info("Watcher queued %s as job %s", path, job_id)
    return queued


@app.route("/api/upload/finalize", methods=["POST"])
def upload_finalize():
    """Finalize a chunked upload and start processing."""
    if not _bots_configured():
        return jsonify({"error": "No Telegram bots configured."}), 503

    _cleanup_expired_pending_uploads()
    data = request.get_json()
    upload_id = data.get("upload_id") if data else None

    with _pending_uploads_lock:
        if not upload_id or upload_id not in _pending_uploads:
            return jsonify({"error": "Unknown upload_id"}), 404
        upload_lock = _upload_locks.setdefault(upload_id, Lock())
        upload = dict(_pending_uploads[upload_id])

    file_path = upload["path"]
    filename = upload["filename"]
    with upload_lock:
        actual_size = os.path.getsize(file_path)
        expected_size = upload["total_size"]
        if actual_size != expected_size:
            return jsonify({
                "error": f"Incomplete upload: got {actual_size} bytes, expected {expected_size}",
            }), 400

        ok, msg = _check_disk_space(actual_size)
        if not ok:
            logger.warning("Job rejected due to disk space: %s", msg)
            return jsonify({"error": msg}), 507

        removed = _remove_pending_upload(upload_id)
        if removed is None:
            return jsonify({"error": "Unknown upload_id"}), 404
        job_id, actual_size = _queue_local_file(
            file_path,
            filename=filename,
            source_mode="upload",
            skip_disk_check=True,
        )

    logger.info("Upload finalized: %s -> job %s (%d bytes)", upload_id, job_id, actual_size)
    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/upload/status/<upload_id>")
def upload_status(upload_id):
    """Check how many chunks have been received for a pending upload."""
    _cleanup_expired_pending_uploads()
    if upload_id not in _pending_uploads:
        return jsonify({"error": "Unknown upload_id"}), 404
    upload = _pending_uploads[upload_id]
    return jsonify({
        "received_bytes": upload["received_bytes"],
        "received_chunks": upload["received_chunks"],
        "total_chunks": upload["total_chunks"],
        "total_size": upload["total_size"],
    })


# ─── Processing Pipeline ───


def _finalize_source_file(job_id, file_path):
    """Clean up the original source according to how the job entered the system."""
    _job_runtime.pop(job_id, None)
    source = _job_source_info.pop(job_id, {"mode": "upload", "path": file_path})
    source_mode = source.get("mode", "upload")
    source_path = source.get("path", file_path)

    if source_mode == "watch":
        success = False
        if _active_jobs.get(job_id, {}).get("status") == "complete":
            if not os.path.exists(source_path):
                success = True
            else:
                try:
                    moved_to = _move_watched_file_to_done(source_path)
                    logger.info("Moved watched source to done/: %s -> %s", source_path, moved_to)
                    success = True
                except OSError as exc:
                    logger.warning("Could not move watched source to done/: %s", exc)
        _release_watch_file(source_path, success=success)
        return

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            logger.warning("Could not remove upload file: %s", file_path)


def _process_job(job_id, file_path):
    """Full pipeline: analyze -> process -> upload -> register."""
    runtime = _get_job_runtime(job_id)
    try:
        if _is_job_cancelled(job_id):
            return

        if not _bots_configured():
            msg = "No Telegram bots configured"
            logger.warning("Job %s aborted: %s", job_id, msg)
            with _job_status_lock:
                job = _active_jobs.get(job_id)
                if job and not job.get("timed_out") and not job.get("cancelled"):
                    job["status"] = "error"
                    job["error"] = msg
                    job["finished_ts"] = time.time()
            return

        # Disk space check (space may have dropped since finalize if queue was long)
        file_size = _active_jobs.get(job_id, {}).get("file_size", 0)
        ok, msg = _check_disk_space(file_size)
        if not ok:
            logger.warning("Job %s aborted: %s", job_id, msg)
            with _job_status_lock:
                job = _active_jobs.get(job_id)
                if job and not job.get("timed_out") and not job.get("cancelled"):
                    job["status"] = "error"
                    job["error"] = msg
                    job["finished_ts"] = time.time()
            return
        # Step 1: Analyze
        with _job_status_lock:
            _active_jobs[job_id]["status"] = "analyzing"
            _active_jobs[job_id]["step"] = "Analyzing streams..."
        analysis = analyze(file_path)

        with _job_status_lock:
            _active_jobs[job_id]["analysis"] = analysis.summary()
        logger.info("Job %s analysis: %s", job_id, analysis.summary())

        # Step 2: Process (split into separate streams)
        with _job_status_lock:
            _active_jobs[job_id]["status"] = "processing"

        def on_process_progress(current, total, step_name):
            if _is_job_cancelled(job_id):
                return
            with _job_status_lock:
                _active_jobs[job_id]["progress"] = int(current / total * 50) if total else 0
                _active_jobs[job_id]["step"] = step_name

        result = process(
            analysis,
            job_id,
            progress_callback=on_process_progress,
            cancel_event=runtime.cancel_event,
            on_process_start=lambda proc: _set_job_process(job_id, proc),
            on_process_end=lambda proc: _clear_job_process(job_id, proc),
        )
        if _is_job_cancelled(job_id):
            return

        # Step 3: Upload to Telegram
        with _job_status_lock:
            _active_jobs[job_id]["status"] = "uploading_telegram"

        def on_upload_progress(current, total, name):
            if _is_job_cancelled(job_id):
                return
            with _job_status_lock:
                _active_jobs[job_id]["progress"] = 50 + int(current / total * 50) if total else 50
                _active_jobs[job_id]["step"] = f"Uploading {name}"
                _active_jobs[job_id]["upload_current"] = current
                _active_jobs[job_id]["upload_total"] = total

        upload_coro = _telegram_uploader.upload_job(
            result,
            progress_callback=on_upload_progress,
            cancel_event=runtime.cancel_event,
        )
        upload_future = asyncio.run_coroutine_threadsafe(upload_coro, _async_loop)
        _set_job_upload_future(job_id, upload_future)
        try:
            upload_result = upload_future.result(timeout=None)
        finally:
            _clear_job_upload_future(job_id, upload_future)
        if _is_job_cancelled(job_id):
            return

        # Step 4: Register for serving
        try:
            register_job(job_id, analysis, result, upload_result)
        except Exception as reg_err:
            # Segments are already on Telegram — log enough detail for manual recovery.
            logger.error(
                "Job %s: register_job failed AFTER Telegram upload. "
                "Segments are on Telegram but not recorded in the database. "
                "Upload result: %s | Error: %s",
                job_id, upload_result, reg_err,
            )
            raise RuntimeError(
                f"Failed to register job after uploading segments: {reg_err}"
            ) from reg_err

        with _job_status_lock:
            # Only mark complete if the user hasn't cancelled in the meantime.
            job = _active_jobs.get(job_id)
            if job and not job.get("timed_out") and not job.get("cancelled"):
                job["status"] = "complete"
                job["progress"] = 100
                job["step"] = "Done"
                job["finished_ts"] = time.time()
        logger.info("Job %s complete", job_id)

    except Exception as e:
        if _is_job_cancelled(job_id):
            return
        logger.exception("Job %s failed", job_id)
        with _job_status_lock:
            job = _active_jobs.get(job_id)
            if job and not job.get("timed_out") and not job.get("cancelled"):
                job["status"] = "error"
                job["error"] = str(e)
                job["finished_ts"] = time.time()
    except BaseException as e:
        if _is_job_cancelled(job_id):
            return
        logger.exception("Job %s failed with non-standard exception", job_id)
        with _job_status_lock:
            job = _active_jobs.get(job_id)
            if job and not job.get("timed_out") and not job.get("cancelled"):
                job["status"] = "error"
                job["error"] = str(e)
                job["finished_ts"] = time.time()

    finally:
        # Always clean up processing artifacts and the source upload file,
        # regardless of whether the job succeeded, failed, or was cancelled.
        cleanup(job_id)
        _finalize_source_file(job_id, file_path)


# ─── Job Status ───

@app.route("/api/status/<job_id>")
def job_status(job_id):
    if job_id in _active_jobs:
        with _job_status_lock:
            snapshot = dict(_active_jobs[job_id])
        # Compute live queue position so the client sees it decreasing
        if snapshot.get("status") == "queued":
            with _queue_order_lock:
                try:
                    snapshot["queue_position"] = _queue_order.index(job_id) + 1
                    snapshot["queue_length"] = len(_queue_order)
                except ValueError:
                    snapshot["queue_position"] = None
        return jsonify(snapshot)
    job = get_job(job_id)
    if job:
        return jsonify({"status": "complete", "progress": 100, **job})
    return jsonify({"error": "Job not found"}), 404


@app.route("/api/jobs")
def jobs_list():
    """List completed jobs with pagination.

    Query params:
      page  – 1-based page number (default 1)
      limit – jobs per page, 1–50 (default 20)
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(50, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        page, limit = 1, 20

    offset = (page - 1) * limit
    total = count_jobs()
    jobs = list_jobs(limit=limit, offset=offset)
    return jsonify({
        "jobs": jobs,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": offset + limit < total,
    })


# ─── HLS Serving ───

@app.route("/hls/<job_id>/master.m3u8")
def master_playlist(job_id):
    """Serve master M3U8 playlist with multi-audio and subtitle variants."""
    base_url = _get_base_url()
    playlist = generate_master_playlist(job_id, base_url)
    if not playlist:
        return jsonify({"error": "Job not found"}), 404
    return Response(playlist, content_type="application/vnd.apple.mpegurl")


@app.route("/hls/<job_id>/video.m3u8")
def video_playlist(job_id):
    """Serve video-only media playlist (legacy, single-tier)."""
    playlist = generate_media_playlist(job_id, "video")
    if not playlist:
        return jsonify({"error": "Not found"}), 404
    return Response(playlist, content_type="application/vnd.apple.mpegurl")


@app.route("/hls/<job_id>/video_<int:index>.m3u8")
def video_tier_playlist(job_id, index):
    """Serve video media playlist for a specific quality tier."""
    playlist = generate_media_playlist(job_id, "video", index)
    if not playlist:
        return jsonify({"error": "Not found"}), 404
    return Response(playlist, content_type="application/vnd.apple.mpegurl")


@app.route("/hls/<job_id>/audio_<int:index>.m3u8")
def audio_playlist(job_id, index):
    """Serve audio track media playlist."""
    playlist = generate_media_playlist(job_id, "audio", index)
    if not playlist:
        return jsonify({"error": "Not found"}), 404
    return Response(playlist, content_type="application/vnd.apple.mpegurl")


@app.route("/hls/<job_id>/sub_<int:index>.m3u8")
def subtitle_playlist(job_id, index):
    """Serve subtitle track playlist."""
    playlist = generate_media_playlist(job_id, "sub", index)
    if not playlist:
        return jsonify({"error": "Not found"}), 404
    return Response(playlist, content_type="application/vnd.apple.mpegurl")


# ─── Segment Cache ───

class _SegmentCache:
    def __init__(self, max_bytes):
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._data = collections.OrderedDict()
        self._lock = Lock()

    def has(self, key):
        with self._lock:
            return key in self._data

    def get(self, key):
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key, data):
        size = len(data)
        if self._max_bytes == 0 or size > self._max_bytes:
            return
        with self._lock:
            if key in self._data:
                self._current_bytes -= len(self._data[key])
                del self._data[key]
            while self._current_bytes + size > self._max_bytes and self._data:
                _, v = self._data.popitem(last=False)
                self._current_bytes -= len(v)
            self._data[key] = data
            self._current_bytes += size

    def clear(self):
        with self._lock:
            self._data.clear()
            self._current_bytes = 0

    @property
    def current_bytes(self):
        with self._lock:
            return self._current_bytes

    @property
    def max_bytes(self):
        return self._max_bytes

    @property
    def free_bytes(self):
        with self._lock:
            return max(0, self._max_bytes - self._current_bytes)


_segment_cache = _SegmentCache(max_bytes=Config.SEGMENT_CACHE_SIZE_MB * 1024 * 1024)


def _get_segment_prefix(segment_key):
    """Extract the HLS stream prefix from a segment key."""
    if "/" not in segment_key:
        return None
    return segment_key.split("/", 1)[0]


def _claim_segment_download(cache_key, *, enable_stream=False):
    """Return an existing in-flight download or claim ownership for a new one."""
    with _segment_download_lock:
        state = _segment_downloads.get(cache_key)
        if state is not None:
            return state, False
        state = _SegmentDownloadState(cache_key, enable_stream=enable_stream)
        _segment_downloads[cache_key] = state
        return state, True


def _release_segment_download(state):
    """Remove a completed download state and any temp file when no readers remain."""
    if not state.should_cleanup():
        return

    temp_path = None
    with _segment_download_lock:
        if _segment_downloads.get(state.cache_key) is not state:
            return
        if not state.should_cleanup():
            return
        temp_path = state.temp_path
        state.temp_path = None
        del _segment_downloads[state.cache_key]

    if temp_path:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Failed to remove temp segment file %s: %s", temp_path, exc)


def _enqueue_stream_item(stream_queue, stream_abandoned, item):
    """Enqueue a stream item with bounded backpressure and disconnect awareness."""
    while True:
        if stream_abandoned.is_set():
            return False
        try:
            stream_queue.put(item, timeout=0.1)
            return True
        except queue.Full:
            continue


def _cache_segment_from_file(cache_key, temp_path):
    """Populate the in-memory cache from a completed temp file if it fits."""
    if _segment_cache.max_bytes <= 0:
        return False
    size = os.path.getsize(temp_path)
    if size <= 0 or size > _segment_cache.max_bytes:
        return False
    with open(temp_path, "rb") as handle:
        _segment_cache.put(cache_key, handle.read())
    return True


async def _download_segment_to_state(file_id, bot_index, cache_key, state):
    """Download a segment from Telegram to temp storage and optionally stream chunks."""
    global _aiohttp_session

    if _aiohttp_session is None or _aiohttp_session.closed:
        logger.warning("aiohttp session closed, recreating")
        _aiohttp_session = aiohttp.ClientSession()

    url = await _telegram_uploader.get_file_url(file_id, bot_index)
    temp_fd, temp_path = tempfile.mkstemp(prefix="segment-", suffix=".tmp")
    os.close(temp_fd)
    state.temp_path = temp_path
    wrote_any = False

    try:
        async with _aiohttp_session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Telegram HTTP {resp.status}")

            with open(temp_path, "wb") as handle:
                async for chunk in resp.content.iter_chunked(65536):
                    if not chunk:
                        continue
                    wrote_any = True
                    handle.write(chunk)
                    if state.stream_queue is not None:
                        await asyncio.to_thread(
                            _enqueue_stream_item,
                            state.stream_queue,
                            state.stream_abandoned,
                            chunk,
                        )
                    else:
                        # Yield to event loop between chunks so concurrent prefetch
                        # downloads can interleave fairly on the same asyncio loop.
                        await asyncio.sleep(0)

        if not wrote_any:
            raise RuntimeError(f"Empty Telegram response for {cache_key}")

        state.cached = await asyncio.to_thread(_cache_segment_from_file, cache_key, temp_path)
    except Exception as exc:
        state.error = exc
        if state.stream_queue is not None:
            await asyncio.to_thread(
                _enqueue_stream_item,
                state.stream_queue,
                state.stream_abandoned,
                _SegmentStreamError(exc),
            )
    finally:
        if state.stream_queue is not None and state.error is None:
            await asyncio.to_thread(
                _enqueue_stream_item,
                state.stream_queue,
                state.stream_abandoned,
                _STREAM_EOF,
            )
        state.completed.set()
        _release_segment_download(state)


def _start_segment_download(file_id, bot_index, cache_key, state):
    """Run a segment download on the persistent async loop."""
    asyncio.run_coroutine_threadsafe(
        _download_segment_to_state(file_id, bot_index, cache_key, state),
        _async_loop,
    )


def _claim_segment_prefetch(cache_key):
    """Reserve a future segment for one queued prefetch task."""
    with _segment_prefetch_lock:
        if cache_key in _scheduled_segment_prefetches:
            return False
        _scheduled_segment_prefetches.add(cache_key)
        return True


def _release_segment_prefetch(cache_key):
    """Clear a queued/running prefetch reservation."""
    with _segment_prefetch_lock:
        _scheduled_segment_prefetches.discard(cache_key)


def _stream_segment_owner(state, first_item):
    """Yield stream items for the owner request while the async download runs."""
    item = first_item
    try:
        while True:
            if item is _STREAM_EOF:
                return
            if isinstance(item, _SegmentStreamError):
                raise item.exc
            yield item
            item = state.stream_queue.get()
    finally:
        state.stream_abandoned.set()
        _release_segment_download(state)


def _stream_segment_file(state):
    """Yield bytes from a completed temp file for waiting followers."""
    temp_path = state.temp_path
    try:
        with open(temp_path, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        state.release_reader()
        _release_segment_download(state)


async def _prefetch_segment_with_info(job_id, segment_key, file_id, bot_index):
    """Best-effort background fetch for a future segment with pre-resolved Telegram info."""
    cache_key = f"{job_id}/{segment_key}"
    try:
        if _segment_cache.has(cache_key):
            return

        state, is_owner = _claim_segment_download(cache_key)
        if not is_owner:
            return
        logger.debug("Prefetch start: %s (bot %d)", segment_key, bot_index)
        await _download_segment_to_state(file_id, bot_index, cache_key, state)
        logger.debug("Prefetch done:  %s", segment_key)
    except Exception as exc:
        logger.debug("Segment prefetch failed %s: %s", cache_key, exc)
    finally:
        _release_segment_prefetch(cache_key)


async def _batch_prefetch(segments, allow_chain=False):
    """Run multiple segment prefetches concurrently via asyncio.gather."""
    logger.info(
        "Prefetch batch: %d segments — %s",
        len(segments),
        ", ".join(s["segment_key"].rsplit("/", 1)[-1] for s in segments),
    )
    await asyncio.gather(
        *[_prefetch_segment_with_info(s["job_id"], s["segment_key"], s["file_id"], s["bot_index"])
          for s in segments],
        return_exceptions=True,
    )

    if allow_chain:
        seen = set()
        for s in segments:
            job_id = s["job_id"]
            prefix = _get_segment_prefix(s["segment_key"])
            if not prefix or (job_id, prefix) in seen:
                continue
            seen.add((job_id, prefix))
            with _last_player_segment_lock:
                player_seg = _last_player_segment.get((job_id, prefix))
            if player_seg:
                _schedule_segment_prefetch(job_id, player_seg, _from_chain=True)


def _start_batch_prefetch(segments, allow_chain=False):
    """Create a single async task that concurrently fetches all given segments."""
    asyncio.create_task(_batch_prefetch(segments, allow_chain))


def _schedule_segment_prefetch(job_id, segment_key, *, _from_chain=False):
    """Schedule background prefetch for the next sequential segments."""
    prefetch_count = max(0, Config.SEGMENT_PREFETCH_COUNT)
    if prefetch_count <= 0:
        return

    if _segment_cache.max_bytes <= 0:
        return

    if (
        Config.SEGMENT_PREFETCH_MIN_FREE_BYTES > 0
        and _segment_cache.free_bytes < Config.SEGMENT_PREFETCH_MIN_FREE_BYTES
    ):
        return

    prefix = _get_segment_prefix(segment_key)
    if not prefix or not prefix.startswith("video") or not segment_key.endswith(".ts"):
        return

    segments = db.get_segments_for_prefix(job_id, prefix)
    if not segments:
        return

    current_index = None
    for index, segment in enumerate(segments):
        if segment["segment_key"] == segment_key:
            current_index = index
            break

    if current_index is None:
        return

    to_prefetch = []
    buffered_ahead = 0
    n_cached = 0
    n_inflight = 0
    for segment in segments[current_index + 1:]:
        if buffered_ahead >= prefetch_count:
            break
        next_segment_key = segment["segment_key"]
        next_cache_key = f"{job_id}/{next_segment_key}"

        if _segment_cache.has(next_cache_key):
            buffered_ahead += 1
            n_cached += 1
            continue

        if next_cache_key in _segment_downloads or next_cache_key in _scheduled_segment_prefetches:
            buffered_ahead += 1
            n_inflight += 1
            continue

        if not _claim_segment_prefetch(next_cache_key):
            buffered_ahead += 1
            n_inflight += 1
            continue

        buffered_ahead += 1
        to_prefetch.append({
            "job_id": job_id,
            "segment_key": next_segment_key,
            "file_id": segment["file_id"],
            "bot_index": segment["bot_index"],
        })

    logger.info(
        "Prefetch [%s/%s pos=%s]: ahead=%d (cached=%d inflight=%d) new=%d chain=%s",
        job_id[:8], prefix, segment_key.rsplit("/", 1)[-1],
        buffered_ahead, n_cached, n_inflight, len(to_prefetch), _from_chain,
    )

    if to_prefetch:
        _async_loop.call_soon_threadsafe(_start_batch_prefetch, to_prefetch, not _from_chain)


# ─── Segment Proxy ───

@app.route("/segment/<job_id>/<path:segment_key>")
def serve_segment(job_id, segment_key):
    """Proxy a segment from Telegram."""
    info = get_segment_info(job_id, segment_key)
    if not info:
        return jsonify({"error": "Segment not found"}), 404

    file_id = info["file_id"]
    bot_index = info["bot_index"]

    _prefix = _get_segment_prefix(segment_key)
    if _prefix and _prefix.startswith("video") and segment_key.endswith(".ts"):
        with _last_player_segment_lock:
            _last_player_segment[(job_id, _prefix)] = segment_key

    if segment_key.endswith(".vtt"):
        content_type = "text/vtt"
    elif segment_key.endswith(".ts"):
        content_type = "video/mp2t"
    else:
        content_type = "application/octet-stream"

    headers = {"Cache-Control": "public, max-age=86400"}

    # Cache hit — return immediately
    cache_key = f"{job_id}/{segment_key}"
    cached = _segment_cache.get(cache_key)
    if cached is not None:
        logger.debug("Segment cache HIT: %s", cache_key)
        _schedule_segment_prefetch(job_id, segment_key)
        return Response(cached, content_type=content_type, headers=headers)

    logger.debug("Segment cache MISS: %s", cache_key)

    state, is_owner = _claim_segment_download(cache_key, enable_stream=True)

    if is_owner:
        _start_segment_download(file_id, bot_index, cache_key, state)
        _schedule_segment_prefetch(job_id, segment_key)
        first_item = state.stream_queue.get()
        if isinstance(first_item, _SegmentStreamError):
            logger.warning("Segment download failed %s: %s", cache_key, first_item.exc)
            _release_segment_download(state)
            return jsonify({"error": "Could not download segment from Telegram"}), 500
        return Response(
            stream_with_context(_stream_segment_owner(state, first_item)),
            content_type=content_type,
            headers=headers,
        )

    if state.mark_waiting_follower():
        state.completed.wait(timeout=30)

    if not state.completed.is_set():
        state.finish_waiting_follower()
        _release_segment_download(state)
        logger.debug("Segment follower timed out; falling back to direct download for %s", cache_key)
        fallback_key = f"{cache_key}#fallback-{threading.get_ident()}"
        fallback_state = _SegmentDownloadState(fallback_key, enable_stream=True)
        with _segment_download_lock:
            _segment_downloads[fallback_key] = fallback_state
        _start_segment_download(file_id, bot_index, cache_key, fallback_state)
        _schedule_segment_prefetch(job_id, segment_key)
        first_item = fallback_state.stream_queue.get()
        if isinstance(first_item, _SegmentStreamError):
            logger.warning("Segment fallback download failed %s: %s", cache_key, first_item.exc)
            _release_segment_download(fallback_state)
            return jsonify({"error": "Could not download segment from Telegram"}), 500
        return Response(
            stream_with_context(_stream_segment_owner(fallback_state, first_item)),
            content_type=content_type,
            headers=headers,
        )

    cached = _segment_cache.get(cache_key)
    if cached is not None:
        state.finish_waiting_follower()
        _schedule_segment_prefetch(job_id, segment_key)
        _release_segment_download(state)
        return Response(cached, content_type=content_type, headers=headers)

    if state.error is not None:
        state.finish_waiting_follower()
        _release_segment_download(state)
        logger.warning("Segment download failed %s: %s", cache_key, state.error)
        return jsonify({"error": "Could not download segment from Telegram"}), 500

    if not state.temp_path:
        state.finish_waiting_follower()
        _release_segment_download(state)
        logger.warning("Segment download produced no cacheable output for %s", cache_key)
        return jsonify({"error": "Could not download segment from Telegram"}), 500

    state.promote_waiting_follower_to_reader()
    _schedule_segment_prefetch(job_id, segment_key)
    return Response(
        stream_with_context(_stream_segment_file(state)),
        content_type=content_type,
        headers=headers,
    )


# ─── CORS for HLS ───

@app.after_request
def add_cors(response):
    if request.path.startswith(("/hls/", "/segment/", "/api/")):
        origin = request.headers.get("Origin", "")
        if _is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, X-Upload-Id, X-Chunk-Index"
            )
    return response


@app.teardown_request
def close_request_db_conn(_exc):
    """Release per-request SQLite connections so streaming traffic does not accumulate them."""
    db.close_conn()


def _kill_existing_cloudflared(port: int) -> None:
    """Kill any orphaned cloudflared processes tunneling to the given port."""
    target = f"http://localhost:{port}"
    try:
        result = subprocess.run(
            ["pgrep", "-af", "cloudflared"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if target in line:
                pid = int(line.split()[0])
                try:
                    os.kill(pid, signal.SIGTERM)
                    logger.info("Killed orphaned cloudflared process %d", pid)
                except ProcessLookupError:
                    pass
    except Exception:
        pass


_cloudflared_proc = None


def _stop_cloudflared():
    """Terminate the cloudflared subprocess if it's running."""
    global _cloudflared_proc
    if _cloudflared_proc is not None:
        try:
            _cloudflared_proc.terminate()
            _cloudflared_proc.wait(timeout=5)
        except Exception:
            try:
                _cloudflared_proc.kill()
            except Exception:
                pass
        _cloudflared_proc = None
        logger.info("Cloudflared tunnel stopped")


def _cloudflared_dns_ready(hostname: str, timeout: float = 3.0) -> bool:
    """Query 1.1.1.1 directly for hostname, bypassing the system resolver.

    trycloudflare.com is Cloudflare's own zone, so 1.1.1.1 has the record as
    soon as the tunnel registers — even when the local resolver hasn't caught up.
    """
    import struct
    qid = 0xABCD
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    parts = hostname.encode().split(b".")
    question = b"".join(bytes([len(p)]) + p for p in parts) + b"\x00\x00\x01\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(header + question, ("1.1.1.1", 53))
            data, _ = s.recvfrom(512)
        rcode = struct.unpack(">H", data[2:4])[0] & 0xF
        ancount = struct.unpack(">H", data[6:8])[0]
        return rcode == 0 and ancount > 0
    except Exception:
        return False


def _start_cloudflared_tunnel(port: int) -> None:
    """Start a cloudflared quick tunnel and print the public URL.

    Runs cloudflared as a subprocess, parses the assigned *.trycloudflare.com
    URL from its stderr output, and logs it.  If cloudflared is not installed
    the function prints an install hint instead.  Restarts the tunnel with
    exponential backoff if it exits unexpectedly.
    """
    global _cloudflared_proc

    if not shutil.which("cloudflared"):
        print("  [!] cloudflared nincs telepítve - tunnel nem elérhető")
        print("      Telepítés: sudo pacman -S cloudflared")
        return

    atexit.register(_stop_cloudflared)
    url_pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    backoff = 5

    while True:
        _kill_existing_cloudflared(port)
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _cloudflared_proc = proc
            tunnel_url = None
            hostname = None
            for line in proc.stdout:
                match = url_pattern.search(line)
                if match:
                    tunnel_url = match.group()
                    hostname = tunnel_url.removeprefix("https://")
                    backoff = 5  # reset on successful connection
                    break
            # Drain stdout immediately so cloudflared never stalls on a full pipe buffer.
            # This must happen before the DNS poll below, which can take up to 30 s.
            Thread(target=lambda: [_ for _ in proc.stdout], daemon=True).start()
            if tunnel_url:
                # Wait for DNS to propagate before announcing the URL.
                # Query 1.1.1.1 directly — the system resolver often lags on
                # newly created *.trycloudflare.com subdomains.
                for attempt in range(30):
                    if _cloudflared_dns_ready(hostname):
                        break
                    if attempt == 29:
                        logger.warning(
                            "Cloudflared DNS did not resolve after 30s: %s", tunnel_url
                        )
                    time.sleep(1)
                logger.info("Cloudflared tunnel active: %s", tunnel_url)
                print(f"  [✓] Cloudflared tunnel: {tunnel_url}")
            proc.wait()
            logger.warning("Cloudflared tunnel exited unexpectedly")
        except Exception as exc:
            logger.warning("cloudflared tunnel failed: %s", exc)
        logger.info("Restarting cloudflared tunnel in %ds...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


def _shutdown_handler(signum, frame):
    """Handle SIGINT/SIGTERM: stop cloudflared, then exit."""
    _stop_cloudflared()
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    _cleanup_expired_pending_uploads(force=True)
    logger.info("Starting Telegram HLS Streamer on %s:%d", Config.HOST, Config.PORT)
    logger.info("Configured bots: %d", len(Config.BOTS))
    logger.info("Max upload size: %d GB", Config.MAX_UPLOAD_SIZE // (1024**3))
    logger.info("Chunk size: %d MB", Config.UPLOAD_CHUNK_SIZE // (1024**2))
    if Config.WATCH_ENABLED:
        logger.info("Watch root: %s -> done: %s", Config.WATCH_ROOT, Config.WATCH_DONE_DIR)
    if Config.CLOUDFLARED_ENABLED:
        tunnel_thread = Thread(target=_start_cloudflared_tunnel, args=(Config.PORT,), daemon=True)
        tunnel_thread.start()
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
