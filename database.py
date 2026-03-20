"""SQLite database for persistent storage of jobs, tracks, and segment mappings.

This is the single source of truth for retrieving files from Telegram.
Without this database, there is no way to know which Telegram file_id
corresponds to which HLS segment.

Schema:
  jobs       - One row per uploaded video (metadata, duration, etc.)
  tracks     - One row per audio/subtitle track in a job
  segments   - One row per uploaded segment (maps segment_key -> Telegram file_id)
"""

import atexit
import logging
import os
import sqlite3
import threading
import time

from config import Config

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "streamer.db")

# Thread-local connections for SQLite (which doesn't allow sharing across threads)
_local = threading.local()
# Track all opened connections so they can be closed on shutdown
_all_connections = []
_all_connections_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        with _all_connections_lock:
            _all_connections.append(_local.conn)
    return _local.conn


def close_conn():
    """Explicitly close the current thread's database connection.

    Call this when a worker thread is about to terminate to ensure the
    SQLite connection is properly released.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
        with _all_connections_lock:
            try:
                _all_connections.remove(conn)
            except ValueError:
                pass


def _close_all_connections():
    """Close all tracked database connections (called at interpreter shutdown)."""
    with _all_connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()


atexit.register(_close_all_connections)


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
            track_type   TEXT NOT NULL,  -- 'video', 'audio', or 'subtitle'
            track_index  INTEGER NOT NULL,
            codec        TEXT,
            language     TEXT DEFAULT 'und',
            title        TEXT DEFAULT '',
            channels     INTEGER DEFAULT 2,
            width        INTEGER DEFAULT 0,
            height       INTEGER DEFAULT 0,
            bitrate      TEXT DEFAULT '',
            original_stream_index INTEGER DEFAULT -1,
            UNIQUE(job_id, track_type, track_index)
        );

        CREATE TABLE IF NOT EXISTS segments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            segment_key  TEXT NOT NULL,   -- e.g. "video/video_0001.ts", "audio_0/audio_0003.ts"
            file_id      TEXT NOT NULL,   -- Telegram file_id
            bot_index    INTEGER NOT NULL, -- which bot uploaded it
            file_size    INTEGER DEFAULT 0,
            duration     REAL DEFAULT 0,  -- actual segment duration from FFmpeg
            UNIQUE(job_id, segment_key)
        );

        CREATE INDEX IF NOT EXISTS idx_segments_job ON segments(job_id);
        CREATE INDEX IF NOT EXISTS idx_tracks_job ON tracks(job_id);
    """)
    conn.commit()

    # Migrate: add width/height/bitrate columns to tracks if missing
    cursor = conn.execute("PRAGMA table_info(tracks)")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    for col, default_val in [("width", 0), ("height", 0), ("bitrate", ""), ("original_stream_index", -1)]:
        if col not in existing_cols:
            col_type = "INTEGER" if isinstance(default_val, int) else "TEXT"
            # DDL statements don't support parameter binding in SQLite,
            # so we format the default value directly (safe: values are hardcoded above).
            default_literal = str(default_val) if isinstance(default_val, int) else f"'{default_val}'"
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type} DEFAULT {default_literal}")
    conn.commit()

    # Migrate: add duration column to segments if missing
    cursor = conn.execute("PRAGMA table_info(segments)")
    seg_cols = {row["name"] for row in cursor.fetchall()}
    if "duration" not in seg_cols:
        conn.execute("ALTER TABLE segments ADD COLUMN duration REAL DEFAULT 0")
        conn.commit()

    logger.info("Database initialized at %s", DB_PATH)


# ─── Jobs ───

def save_job(job_id, analysis, processing_result, upload_result):
    """Persist a completed job with all its tracks and segments.

    Uses an explicit transaction so partial failures roll back cleanly.
    """
    conn = _get_conn()

    try:
        with conn:
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

            # Save video tracks (ABR tiers)
            orig_video_idx = video.index if video else -1
            for i, (_, _, width, height, bitrate) in enumerate(processing_result.video_playlists):
                conn.execute(
                    """INSERT OR REPLACE INTO tracks
                       (job_id, track_type, track_index, codec, language, title, channels, width, height, bitrate, original_stream_index)
                       VALUES (?, 'video', ?, ?, 'und', ?, 0, ?, ?, ?, ?)""",
                    (job_id, i, video.codec_name if video else "h264",
                     f"{width}x{height}", width, height, bitrate, orig_video_idx),
                )

            # Save audio tracks
            for i, (_, _, lang, title, channels) in enumerate(processing_result.audio_playlists):
                audio = analysis.audio_streams[i] if i < len(analysis.audio_streams) else None
                orig_audio_idx = audio.index if audio else -1
                conn.execute(
                    """INSERT OR REPLACE INTO tracks
                       (job_id, track_type, track_index, codec, language, title, channels, original_stream_index)
                       VALUES (?, 'audio', ?, ?, ?, ?, ?, ?)""",
                    (job_id, i, audio.codec_name if audio else "aac", lang, title, channels, orig_audio_idx),
                )

            # Save subtitle tracks
            # Each tuple is (vtt_path, sub_dir, lang, title, enum_idx, orig_stream_idx).
            # enum_idx is the enumerate index over ALL subtitle streams (including skipped
            # bitmap ones), so it matches the sub_N directory name used by video_processor.
            for _, _, lang, title, enum_idx, orig_idx in processing_result.subtitle_files:
                conn.execute(
                    """INSERT OR REPLACE INTO tracks
                       (job_id, track_type, track_index, codec, language, title, channels, original_stream_index)
                       VALUES (?, 'subtitle', ?, 'webvtt', ?, ?, 0, ?)""",
                    (job_id, enum_idx, lang, title, orig_idx),
                )

            # Save all segment mappings (the critical Telegram file_id references)
            segment_durations = getattr(processing_result, "segment_durations", {})
            for key, seg in upload_result.segments.items():
                dur = segment_durations.get(key, 0)
                conn.execute(
                    """INSERT OR REPLACE INTO segments
                       (job_id, segment_key, file_id, bot_index, file_size, duration)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job_id, key, seg.file_id, seg.bot_index, seg.file_size, dur),
                )

    except Exception:
        logger.exception("Failed to save job %s, rolled back transaction", job_id)
        raise

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
        logger.warning("Segment not found: job_id=%s, segment_key=%s", job_id, segment_key)
        return None
    return {"file_id": row["file_id"], "bot_index": row["bot_index"]}


def get_segments_for_prefix(job_id, prefix):
    """Get all segments matching a prefix, sorted.

    Returns list of dicts with 'segment_key' and 'duration'.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT segment_key, duration FROM segments WHERE job_id = ? AND segment_key LIKE ? ORDER BY segment_key",
        (job_id, f"{prefix}/%"),
    ).fetchall()
    return [{"segment_key": r["segment_key"], "duration": r["duration"] or 0} for r in rows]


def list_jobs(limit=50, offset=0):
    """List jobs with their track counts, newest first.

    Args:
        limit: Maximum number of jobs to return.
        offset: Number of jobs to skip (for pagination).
    """
    conn = _get_conn()
    # Use subqueries instead of JOIN + GROUP BY to avoid Cartesian product
    # before limiting, which is much faster with many segments per job.
    rows = conn.execute("""
        SELECT j.*,
               (SELECT COUNT(*) FROM tracks t WHERE t.job_id = j.job_id AND t.track_type = 'audio') as audio_count,
               (SELECT COUNT(*) FROM tracks t WHERE t.job_id = j.job_id AND t.track_type = 'subtitle') as subtitle_count,
               (SELECT COUNT(*) FROM segments s WHERE s.job_id = j.job_id) as segment_count
        FROM jobs j
        ORDER BY j.created_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_jobs():
    """Return the total number of completed jobs."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM jobs").fetchone()
    return row["cnt"]


def delete_job(job_id):
    """Delete a job and all its tracks/segments (cascading)."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    logger.info("Deleted job %s from database", job_id)


def delete_old_jobs(older_than_days):
    """Delete completed jobs older than the specified number of days.

    Returns the number of jobs deleted.
    """
    if older_than_days <= 0:
        return 0
    cutoff_ts = time.time() - older_than_days * 86400
    # SQLite stores CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS' UTC.
    # We compare using unixepoch() which is available in SQLite 3.38+; fall back
    # to a string comparison against an ISO-8601 representation for older SQLite.
    conn = _get_conn()
    with conn:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE strftime('%s', created_at) < ?",
            (str(int(cutoff_ts)),),
        )
    count = cursor.rowcount
    if count:
        logger.info("Retention cleanup: deleted %d jobs older than %d days", count, older_than_days)
    return count


# Initialize on import
init_db()
