"""Telegram HLS Streamer - Main Application.

Web server that handles:
  - File upload with progress tracking
  - Video processing (split into video/audio/subtitle streams)
  - Telegram upload via multi-bot round-robin
  - HLS playlist serving (master + media playlists)
  - Segment proxying from Telegram
"""

import asyncio
import logging
import os
import re
import uuid
from threading import Thread

import aiohttp
from flask import (
    Flask, jsonify, render_template, request, Response, send_from_directory,
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
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_UPLOAD_SIZE

# Track active jobs: job_id -> {status, progress, ...}
_active_jobs = {}


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


# ─── Upload & Processing ───

@app.route("/api/upload", methods=["POST"])
def upload():
    """Handle video file upload and kick off processing pipeline."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No filename"}), 400

    job_id = uuid.uuid4().hex[:12]
    upload_path = os.path.join(Config.UPLOAD_DIR, f"{job_id}_{file.filename}")

    _active_jobs[job_id] = {
        "status": "uploading",
        "filename": file.filename,
        "progress": 0,
        "step": "Saving upload...",
    }

    file.save(upload_path)
    _active_jobs[job_id]["status"] = "queued"

    # Process in background thread
    thread = Thread(target=_process_job, args=(job_id, upload_path), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


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
            _active_jobs[job_id]["progress"] = int(current / total * 50)
            _active_jobs[job_id]["step"] = step_name

        result = process(analysis, job_id, progress_callback=on_process_progress)

        # Step 3: Upload to Telegram
        _active_jobs[job_id]["status"] = "uploading_telegram"

        def on_upload_progress(current, total, name):
            _active_jobs[job_id]["progress"] = 50 + int(current / total * 50)
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
        # Cleanup on error
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
    """Proxy a segment from Telegram.

    Downloads the file from Telegram's servers and streams it to the client.
    """
    info = get_segment_info(job_id, segment_key)
    if not info:
        return jsonify({"error": "Segment not found"}), 404

    file_id = info["file_id"]
    bot_index = info["bot_index"]

    # Get download URL from Telegram
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

    # Determine content type
    if segment_key.endswith(".vtt"):
        content_type = "text/vtt"
    elif segment_key.endswith(".ts"):
        content_type = "video/mp2t"
    else:
        content_type = "application/octet-stream"

    # Stream from Telegram
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
    if request.path.startswith(("/hls/", "/segment/")):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


if __name__ == "__main__":
    logger.info("Starting Telegram HLS Streamer on %s:%d", Config.HOST, Config.PORT)
    logger.info("Configured bots: %d", len(Config.BOTS))
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
