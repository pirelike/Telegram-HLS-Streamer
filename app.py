"""Telegram HLS Streamer - Main Application.

Web server that handles:
  - Chunked resumable file uploads (supports 50GB+ files)
  - Video processing (split into video/audio/subtitle streams)
  - Telegram upload via multi-bot round-robin
  - HLS playlist serving (master + media playlists)
  - Segment proxying from Telegram
"""

import asyncio
import base64
import logging
import os
import time
import uuid
from threading import Lock, Thread
from werkzeug.utils import secure_filename

from flask import (
    Flask, jsonify, render_template, request, Response,
)

from config import Config
from stream_analyzer import analyze
from video_processor import process, cleanup
from telegram_uploader import TelegramUploader
from hls_manager import (
    register_job, generate_master_playlist, generate_media_playlist,
    get_segment_info, list_jobs, get_job, count_jobs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Per-chunk limit only — the full file is assembled from many chunks
app.config["MAX_CONTENT_LENGTH"] = Config.UPLOAD_CHUNK_SIZE * 2

# Track active jobs: job_id -> {status, progress, ...}
_active_jobs = {}

# Track in-progress chunked uploads: upload_id -> {path, filename, received, total, ...}
_pending_uploads = {}
_pending_filenames = {}  # filename -> upload_id (for O(1) duplicate check)
_upload_locks = {}
_last_pending_cleanup = 0.0
_watcher_started = False

_TERMINAL_JOB_STATES = {"complete", "error"}
# Protects status transitions so cancel_job cannot overwrite a just-completed job.
_job_status_lock = Lock()


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


def _run_async(coro):
    """Run an async coroutine synchronously from a Flask thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _is_upload_authorized():
    """Validate optional API key / basic auth for upload APIs."""
    if not (Config.UPLOAD_API_KEY or Config.UPLOAD_BASIC_USER):
        return True

    if Config.UPLOAD_API_KEY:
        api_key = request.headers.get("X-API-Key", "").strip()
        if api_key and api_key == Config.UPLOAD_API_KEY:
            return True

    if Config.UPLOAD_BASIC_USER:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            b64 = auth_header.split(" ", 1)[1]
            try:
                decoded = base64.b64decode(b64).decode("utf-8")
                username, password = decoded.split(":", 1)
            except Exception:
                return False
            if (
                username == Config.UPLOAD_BASIC_USER
                and password == Config.UPLOAD_BASIC_PASSWORD
            ):
                return True

    return False


def _require_upload_auth():
    """Return auth response tuple if unauthorized, otherwise None."""
    if request.method == "OPTIONS":
        return None
    if _is_upload_authorized():
        return None
    return jsonify({"error": "Unauthorized"}), 401


def _cleanup_expired_pending_uploads(force=False):
    """Delete stale pending uploads from memory + disk."""
    global _last_pending_cleanup
    now = time.time()
    if not force and (now - _last_pending_cleanup) < Config.PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS:
        return

    ttl = Config.PENDING_UPLOAD_TTL_SECONDS
    expired_ids = []
    for upload_id, info in _pending_uploads.items():
        last_activity = info.get("last_activity_ts", info.get("created_ts", now))
        if now - last_activity > ttl:
            expired_ids.append(upload_id)

    for upload_id in expired_ids:
        info = _pending_uploads.pop(upload_id, None)
        if not info:
            continue
        _pending_filenames.pop(info.get("filename"), None)
        _upload_locks.pop(upload_id, None)
        path = info.get("path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not remove expired pending upload file: %s", path)
        logger.info("Cleaned expired pending upload: %s (%s)", upload_id, info.get("filename"))

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
                    logger.error("Job %s timed out at %s", job_id, step)

    Thread(target=watch, daemon=True).start()


def _is_job_cancelled(job_id):
    job = _active_jobs.get(job_id)
    if not job:
        return True
    return bool(job.get("timed_out") or job.get("cancelled"))


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Cancel a running job."""
    unauthorized = _require_upload_auth()
    if unauthorized:
        return unauthorized

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

    logger.info("Job %s cancelled by user", job_id)
    return jsonify({"message": "Job cancellation requested"})


_start_timeout_watcher()


# ─── Web UI ───

@app.route("/")
def index():
    return render_template("index.html")


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

@app.route("/api/upload/init", methods=["POST"])
def upload_init():
    """Initialize a chunked upload session."""
    unauthorized = _require_upload_auth()
    if unauthorized:
        return unauthorized
    _cleanup_expired_pending_uploads()
    data = request.get_json()
    if not data or "filename" not in data or "total_size" not in data:
        return jsonify({"error": "filename and total_size required"}), 400

    filename = secure_filename(data["filename"]) or "unnamed_upload"
    total_size = int(data["total_size"])
    total_chunks = int(data.get("total_chunks", 0))

    if total_size > Config.MAX_UPLOAD_SIZE:
        return jsonify({
            "error": f"File too large. Max {Config.MAX_UPLOAD_SIZE // (1024**3)}GB"
        }), 413

    # Reject if there's already a pending upload for this filename
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
    }
    _pending_filenames[filename] = upload_id
    _upload_locks[upload_id] = Lock()

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
    unauthorized = _require_upload_auth()
    if unauthorized:
        return unauthorized
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

        current_size = os.path.getsize(upload["path"])
        if offset > current_size:
            return jsonify({
                "error": "Out-of-order chunk would create a file gap",
                "expected_offset": current_size,
            }), 409
        if offset < current_size and not is_retry:
            return jsonify({"error": "Chunk overlaps existing data"}), 409

        # Write chunk at validated offset (supports in-order + retry)
        with open(upload["path"], "r+b") as f:
            f.seek(offset)
            f.write(chunk_data)

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


@app.route("/api/upload/finalize", methods=["POST"])
def upload_finalize():
    """Finalize a chunked upload and start processing."""
    unauthorized = _require_upload_auth()
    if unauthorized:
        return unauthorized
    _cleanup_expired_pending_uploads()
    data = request.get_json()
    upload_id = data.get("upload_id") if data else None

    if not upload_id or upload_id not in _pending_uploads:
        return jsonify({"error": "Unknown upload_id"}), 404

    upload = _pending_uploads.pop(upload_id)
    _pending_filenames.pop(upload["filename"], None)
    _upload_locks.pop(upload_id, None)
    file_path = upload["path"]
    filename = upload["filename"]

    # Verify file size
    actual_size = os.path.getsize(file_path)
    expected_size = upload["total_size"]
    if actual_size < expected_size * 0.99:  # allow 1% tolerance for rounding
        return jsonify({
            "error": f"Incomplete upload: got {actual_size} bytes, expected {expected_size}",
        }), 400

    # Create job and start processing
    job_id = uuid.uuid4().hex[:12]
    _active_jobs[job_id] = {
        "status": "queued",
        "filename": filename,
        "file_size": actual_size,
        "progress": 0,
        "step": "Queued for processing...",
        "started_ts": time.time(),
    }

    thread = Thread(target=_process_job, args=(job_id, file_path), daemon=True)
    thread.start()

    logger.info("Upload finalized: %s -> job %s (%d bytes)", upload_id, job_id, actual_size)
    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/upload/status/<upload_id>")
def upload_status(upload_id):
    """Check how many chunks have been received for a pending upload."""
    unauthorized = _require_upload_auth()
    if unauthorized:
        return unauthorized
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

def _process_job(job_id, file_path):
    """Full pipeline: analyze -> process -> upload -> register."""
    try:
        if _is_job_cancelled(job_id):
            return
        # Step 1: Analyze
        _active_jobs[job_id]["status"] = "analyzing"
        _active_jobs[job_id]["step"] = "Analyzing streams..."
        analysis = analyze(file_path)

        _active_jobs[job_id]["analysis"] = analysis.summary()
        logger.info("Job %s analysis: %s", job_id, analysis.summary())

        # Step 2: Process (split into separate streams)
        _active_jobs[job_id]["status"] = "processing"

        def on_process_progress(current, total, step_name):
            if _is_job_cancelled(job_id):
                return
            _active_jobs[job_id]["progress"] = int(current / total * 50) if total else 0
            _active_jobs[job_id]["step"] = step_name

        result = process(analysis, job_id, progress_callback=on_process_progress)
        if _is_job_cancelled(job_id):
            return

        # Step 3: Upload to Telegram
        _active_jobs[job_id]["status"] = "uploading_telegram"

        def on_upload_progress(current, total, name):
            if _is_job_cancelled(job_id):
                return
            _active_jobs[job_id]["progress"] = 50 + int(current / total * 50) if total else 50
            _active_jobs[job_id]["step"] = f"Uploading {name}"

        uploader = TelegramUploader()
        upload_result = _run_async(
            uploader.upload_job(result, progress_callback=on_upload_progress)
        )
        if _is_job_cancelled(job_id):
            return

        # Step 4: Register for serving
        register_job(job_id, analysis, result, upload_result)

        with _job_status_lock:
            # Only mark complete if the user hasn't cancelled in the meantime.
            if not _is_job_cancelled(job_id):
                _active_jobs[job_id]["status"] = "complete"
                _active_jobs[job_id]["progress"] = 100
                _active_jobs[job_id]["step"] = "Done"
        logger.info("Job %s complete", job_id)

    except Exception as e:
        if _is_job_cancelled(job_id):
            return
        logger.exception("Job %s failed", job_id)
        with _job_status_lock:
            if not _is_job_cancelled(job_id):
                _active_jobs[job_id]["status"] = "error"
                _active_jobs[job_id]["error"] = str(e)

    finally:
        # Always clean up processing artifacts and the source upload file,
        # regardless of whether the job succeeded, failed, or was cancelled.
        cleanup(job_id)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logger.warning("Could not remove upload file: %s", file_path)


# ─── Job Status ───

@app.route("/api/status/<job_id>")
def job_status(job_id):
    if job_id in _active_jobs:
        return jsonify(_active_jobs[job_id])
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


# ─── Segment Proxy ───

@app.route("/segment/<job_id>/<path:segment_key>")
def serve_segment(job_id, segment_key):
    """Proxy a segment from Telegram."""
    info = get_segment_info(job_id, segment_key)
    if not info:
        return jsonify({"error": "Segment not found"}), 404

    file_id = info["file_id"]
    bot_index = info["bot_index"]

    uploader = TelegramUploader()
    segment_bytes = _run_async(uploader.get_file_bytes(file_id, bot_index))

    if not segment_bytes:
        return jsonify({"error": "Could not download segment from Telegram"}), 500

    if segment_key.endswith(".vtt"):
        content_type = "text/vtt"
    elif segment_key.endswith(".ts"):
        content_type = "video/mp2t"
    else:
        content_type = "application/octet-stream"

    return Response(
        bytes(segment_bytes),
        content_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
        },
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
            "Content-Type, Authorization, X-API-Key, X-Upload-Id, X-Chunk-Index"
        )
    return response


if __name__ == "__main__":
    _cleanup_expired_pending_uploads(force=True)
    logger.info("Starting Telegram HLS Streamer on %s:%d", Config.HOST, Config.PORT)
    logger.info("Configured bots: %d", len(Config.BOTS))
    logger.info("Max upload size: %d GB", Config.MAX_UPLOAD_SIZE // (1024**3))
    logger.info("Chunk size: %d MB", Config.UPLOAD_CHUNK_SIZE // (1024**2))
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
