"""SQLite database for persistent storage of jobs, tracks, and segment mappings.

This is the single source of truth for retrieving files from Telegram.
Without this database, there is no way to know which Telegram file_id
corresponds to which HLS segment.

Schema:
  jobs       - One row per uploaded video (metadata, duration, etc.)
  tracks     - One row per audio/subtitle track in a job
  segments   - One row per uploaded segment (maps segment_key -> Telegram file_id)
"""

import json
import logging
import os
import sqlite3
import threading

from config import Config

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "streamer.db")

# Thread-local connections for SQLite (which doesn't allow sharing across threads)
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id       TEXT PRIMARY KEY,
            filename     TEXT,
            duration     REAL DEFAULT 0,
            file_size    INTEGER DEFAULT 0,
            video_codec  TEXT,
            video_width  INTEGER DEFAULT 0,
            video_height INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'complete',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            track_type   TEXT NOT NULL,  -- 'audio' or 'subtitle'
            track_index  INTEGER NOT NULL,
            codec        TEXT,
            language     TEXT DEFAULT 'und',
            title        TEXT DEFAULT '',
            channels     INTEGER DEFAULT 2,
            UNIQUE(job_id, track_type, track_index)
        );

        CREATE TABLE IF NOT EXISTS segments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            segment_key  TEXT NOT NULL,   -- e.g. "video/video_0001.ts", "audio_0/audio_0003.ts"
            file_id      TEXT NOT NULL,   -- Telegram file_id
            bot_index    INTEGER NOT NULL, -- which bot uploaded it
            file_size    INTEGER DEFAULT 0,
            UNIQUE(job_id, segment_key)
        );

        CREATE INDEX IF NOT EXISTS idx_segments_job ON segments(job_id);
        CREATE INDEX IF NOT EXISTS idx_tracks_job ON tracks(job_id);
    """)
    conn.commit()
    logger.info("Database initialized at %s", DB_PATH)


# ─── Jobs ───

def save_job(job_id, analysis, processing_result, upload_result):
    """Persist a completed job with all its tracks and segments."""
    conn = _get_conn()

    video = analysis.video_streams[0] if analysis.has_video else None

    conn.execute(
        """INSERT OR REPLACE INTO jobs
           (job_id, filename, duration, file_size, video_codec, video_width, video_height)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id,
            os.path.basename(analysis.file_path),
            analysis.duration,
            analysis.file_size,
            video.codec_name if video else None,
            video.width if video else 0,
            video.height if video else 0,
        ),
    )

    # Save audio tracks
    for i, (_, _, lang, title, channels) in enumerate(processing_result.audio_playlists):
        audio = analysis.audio_streams[i] if i < len(analysis.audio_streams) else None
        conn.execute(
            """INSERT OR REPLACE INTO tracks
               (job_id, track_type, track_index, codec, language, title, channels)
               VALUES (?, 'audio', ?, ?, ?, ?, ?)""",
            (job_id, i, audio.codec_name if audio else "aac", lang, title, channels),
        )

    # Save subtitle tracks
    for i, (_, _, lang, title) in enumerate(processing_result.subtitle_files):
        sub = analysis.subtitle_streams[i] if i < len(analysis.subtitle_streams) else None
        conn.execute(
            """INSERT OR REPLACE INTO tracks
               (job_id, track_type, track_index, codec, language, title, channels)
               VALUES (?, 'subtitle', ?, ?, ?, ?, 0)""",
            (job_id, i, sub.codec_name if sub else "webvtt", lang, title),
        )

    # Save all segment mappings (the critical Telegram file_id references)
    for key, seg in upload_result.segments.items():
        conn.execute(
            """INSERT OR REPLACE INTO segments
               (job_id, segment_key, file_id, bot_index, file_size)
               VALUES (?, ?, ?, ?, ?)""",
            (job_id, key, seg.file_id, seg.bot_index, seg.file_size),
        )

    conn.commit()
    logger.info(
        "Saved job %s to database: %d segments, %d tracks",
        job_id, len(upload_result.segments),
        len(processing_result.audio_playlists) + len(processing_result.subtitle_files),
    )


def get_job(job_id):
    """Load job metadata from database. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def get_job_tracks(job_id, track_type=None):
    """Get all tracks for a job, optionally filtered by type."""
    conn = _get_conn()
    if track_type:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE job_id = ? AND track_type = ? ORDER BY track_index",
            (job_id, track_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE job_id = ? ORDER BY track_type, track_index",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_segment(job_id, segment_key):
    """Look up a single segment's Telegram file_id and bot_index."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT file_id, bot_index FROM segments WHERE job_id = ? AND segment_key = ?",
        (job_id, segment_key),
    ).fetchone()
    if not row:
        return None
    return {"file_id": row["file_id"], "bot_index": row["bot_index"]}


def get_segments_for_prefix(job_id, prefix):
    """Get all segment keys matching a prefix, sorted."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT segment_key FROM segments WHERE job_id = ? AND segment_key LIKE ? ORDER BY segment_key",
        (job_id, f"{prefix}/%"),
    ).fetchall()
    return [r["segment_key"] for r in rows]


def list_jobs():
    """List all jobs with their track counts."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT j.*,
               COUNT(DISTINCT CASE WHEN t.track_type = 'audio' THEN t.id END) as audio_count,
               COUNT(DISTINCT CASE WHEN t.track_type = 'subtitle' THEN t.id END) as subtitle_count,
               COUNT(DISTINCT s.id) as segment_count
        FROM jobs j
        LEFT JOIN tracks t ON t.job_id = j.job_id
        LEFT JOIN segments s ON s.job_id = j.job_id
        GROUP BY j.job_id
        ORDER BY j.created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id):
    """Delete a job and all its tracks/segments (cascading)."""
    conn = _get_conn()
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.commit()
    logger.info("Deleted job %s from database", job_id)


# Initialize on import
init_db()
