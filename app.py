"""Telegram HLS Streamer - Main Application.

Web server that handles:
  - Chunked resumable file uploads (supports 50GB+ files)
  - Video processing (split into video/audio/subtitle streams)
  - Telegram upload via multi-bot round-robin
  - HLS playlist serving (master + media playlists)
  - Segment proxying from Telegram
"""

import asyncio
import hashlib
import logging
import os
import uuid
from threading import Thread

from flask import (
    Flask, jsonify, render_template, request, Response,
)

from config import Config
from stream_analyzer import analyze
from video_processor import process, cleanup
from telegram_uploader import TelegramUploader
from hls_manager import (
    register_job, generate_master_playlist, generate_media_playlist,
    get_segment_info, list_jobs, get_job,
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


def _get_base_url():
    """Determine base URL for playlist generation."""
    if Config.FORCE_HTTPS:
        scheme = "https"
    elif Config.BEHIND_PROXY:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    else:
        scheme = request.scheme
    return f"{scheme}://{request.host}"


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
    data = request.get_json()
    if not data or "filename" not in data or "total_size" not in data:
        return jsonify({"error": "filename and total_size required"}), 400

    filename = os.path.basename(data["filename"])
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400
    total_size = int(data["total_size"])
    total_chunks = int(data.get("total_chunks", 0))

    if total_size > Config.MAX_UPLOAD_SIZE:
        return jsonify({
            "error": f"File too large. Max {Config.MAX_UPLOAD_SIZE // (1024**3)}GB"
        }), 413

    # Reject if there's already a pending upload for this filename
    for uid, info in _pending_uploads.items():
        if info["filename"] == filename:
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
    }

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
    upload_id = request.headers.get("X-Upload-Id")
    chunk_index = request.headers.get("X-Chunk-Index")

    if not upload_id or chunk_index is None:
        return jsonify({"error": "X-Upload-Id and X-Chunk-Index headers required"}), 400

    if upload_id not in _pending_uploads:
        return jsonify({"error": "Unknown upload_id. Call /api/upload/init first"}), 404

    upload = _pending_uploads[upload_id]
    chunk_index = int(chunk_index)

    # Read raw chunk from request body — streams directly, no buffering
    chunk_data = request.get_data()
    chunk_len = len(chunk_data)

    if chunk_len == 0:
        return jsonify({"error": "Empty chunk"}), 400

    # Write chunk at correct offset (allows out-of-order/retry)
    offset = chunk_index * Config.UPLOAD_CHUNK_SIZE
    with open(upload["path"], "r+b") as f:
        f.seek(offset)
        f.write(chunk_data)

    upload["received_bytes"] += chunk_len
    upload["received_chunks"] += 1

    return jsonify({
        "chunk_index": chunk_index,
        "received_bytes": upload["received_bytes"],
        "received_chunks": upload["received_chunks"],
    })


@app.route("/api/upload/finalize", methods=["POST"])
def upload_finalize():
    """Finalize a chunked upload and start processing."""
    data = request.get_json()
    upload_id = data.get("upload_id") if data else None

    if not upload_id or upload_id not in _pending_uploads:
        return jsonify({"error": "Unknown upload_id"}), 404

    upload = _pending_uploads.pop(upload_id)
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
    }

    thread = Thread(target=_process_job, args=(job_id, file_path), daemon=True)
    thread.start()

    logger.info("Upload finalized: %s -> job %s (%d bytes)", upload_id, job_id, actual_size)
    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/upload/status/<upload_id>")
def upload_status(upload_id):
    """Check how many chunks have been received for a pending upload."""
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
        # Step 1: Analyze
        _active_jobs[job_id]["status"] = "analyzing"
        _active_jobs[job_id]["step"] = "Analyzing streams..."
        analysis = analyze(file_path)

        _active_jobs[job_id]["analysis"] = analysis.summary()
        logger.info("Job %s analysis: %s", job_id, analysis.summary())

        # Step 2: Process (split into separate streams)
        _active_jobs[job_id]["status"] = "processing"

        def on_process_progress(current, total, step_name):
            _active_jobs[job_id]["progress"] = int(current / total * 50) if total else 0
            _active_jobs[job_id]["step"] = step_name

        result = process(analysis, job_id, progress_callback=on_process_progress)

        # Step 3: Upload to Telegram
        _active_jobs[job_id]["status"] = "uploading_telegram"

        def on_upload_progress(current, total, name):
            _active_jobs[job_id]["progress"] = 50 + int(current / total * 50) if total else 50
            _active_jobs[job_id]["step"] = f"Uploading {name}"

        uploader = TelegramUploader()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            upload_result = loop.run_until_complete(
                uploader.upload_job(result, progress_callback=on_upload_progress)
            )
        finally:
            loop.close()

        # Step 4: Register for serving
        register_job(job_id, analysis, result, upload_result)

        _active_jobs[job_id]["status"] = "complete"
        _active_jobs[job_id]["progress"] = 100
        _active_jobs[job_id]["step"] = "Done"

        # Cleanup temp files
        cleanup(job_id)
        if os.path.exists(file_path):
            os.remove(file_path)

        logger.info("Job %s complete", job_id)

    except Exception as e:
        logger.exception("Job %s failed", job_id)
        _active_jobs[job_id]["status"] = "error"
        _active_jobs[job_id]["error"] = str(e)
        if os.path.exists(file_path):
            os.remove(file_path)


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
    """List all completed jobs."""
    return jsonify(list_jobs())


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
    """Serve video-only media playlist."""
    playlist = generate_media_playlist(job_id, "video")
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
    loop = asyncio.new_event_loop()
    try:
        file_url = loop.run_until_complete(
            uploader.get_file_url(file_id, bot_index)
        )
    finally:
        loop.close()

    if not file_url:
        return jsonify({"error": "Could not get file URL"}), 500

    if segment_key.endswith(".vtt"):
        content_type = "text/vtt"
    elif segment_key.endswith(".ts"):
        content_type = "video/mp2t"
    else:
        content_type = "application/octet-stream"

    def stream_from_telegram():
        import urllib.request
        with urllib.request.urlopen(file_url) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk

    return Response(
        stream_from_telegram(),
        content_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ─── CORS for HLS ───

@app.after_request
def add_cors(response):
    if request.path.startswith(("/hls/", "/segment/", "/api/")):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Upload-Id, X-Chunk-Index"
        )
    return response


if __name__ == "__main__":
    logger.info("Starting Telegram HLS Streamer on %s:%d", Config.HOST, Config.PORT)
    logger.info("Configured bots: %d", len(Config.BOTS))
    logger.info("Max upload size: %d GB", Config.MAX_UPLOAD_SIZE // (1024**3))
    logger.info("Chunk size: %d MB", Config.UPLOAD_CHUNK_SIZE // (1024**2))
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
