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
from typing import Set

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "streamer.db")
LATEST_SCHEMA_REVISION = 3

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


def open_connection_count() -> int:
    """Return the number of currently tracked open SQLite connections."""
    with _all_connections_lock:
        return len(_all_connections)


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _list_table_columns(conn: sqlite3.Connection, table_name: str) -> Set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _create_schema_migrations_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            revision   INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _record_migration(conn: sqlite3.Connection, revision: int, name: str):
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (revision, name) VALUES (?, ?)",
        (revision, name),
    )


def _get_recorded_schema_revision(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT MAX(revision) AS revision FROM schema_migrations").fetchone()
    return int(row["revision"] or 0)


def _detect_legacy_schema_revision(conn: sqlite3.Connection) -> int:
    base_tables = ("jobs", "tracks", "segments")
    table_presence = {table: _table_exists(conn, table) for table in base_tables}
    if not any(table_presence.values()):
        return 0
    if not all(table_presence.values()):
        raise RuntimeError(
            f"Unsupported partial legacy schema in {DB_PATH}: "
            f"expected {base_tables}, found {[name for name, present in table_presence.items() if present]}"
        )

    track_cols = _list_table_columns(conn, "tracks")
    segment_cols = _list_table_columns(conn, "segments")

    v2_track_cols = {"width", "height", "bitrate", "original_stream_index"}
    has_v2_tracks = v2_track_cols.issubset(track_cols)
    has_any_v2_tracks = bool(v2_track_cols & track_cols)
    has_v3_segments = "duration" in segment_cols

    if has_any_v2_tracks and not has_v2_tracks:
        raise RuntimeError(
            f"Unsupported legacy tracks schema in {DB_PATH}: expected all of {sorted(v2_track_cols)}"
        )
    if has_v3_segments and not has_v2_tracks:
        raise RuntimeError(
            f"Unsupported legacy schema in {DB_PATH}: segments.duration exists without full tracks v2 columns"
        )

    if has_v3_segments:
        return 3
    if has_v2_tracks:
        return 2
    return 1


def _bootstrap_legacy_schema_migrations(conn: sqlite3.Connection):
    revision = _detect_legacy_schema_revision(conn)
    _create_schema_migrations_table(conn)
    if revision == 0:
        return
    names = {
        1: "create_base_schema",
        2: "add_track_dimensions_and_stream_index",
        3: "add_segment_duration",
    }
    for current_revision in range(1, revision + 1):
        _record_migration(conn, current_revision, names[current_revision])
    logger.info("Bootstrapped legacy database at schema revision %d", revision)


def _migration_001_create_base_schema(conn: sqlite3.Connection):
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


def _migration_002_add_track_dimensions_and_stream_index(conn: sqlite3.Connection):
    conn.executescript("""
        ALTER TABLE tracks ADD COLUMN width INTEGER DEFAULT 0;
        ALTER TABLE tracks ADD COLUMN height INTEGER DEFAULT 0;
        ALTER TABLE tracks ADD COLUMN bitrate TEXT DEFAULT '';
        ALTER TABLE tracks ADD COLUMN original_stream_index INTEGER DEFAULT -1;
    """)


def _migration_003_add_segment_duration(conn: sqlite3.Connection):
    conn.execute("ALTER TABLE segments ADD COLUMN duration REAL DEFAULT 0")


MIGRATIONS = [
    (1, "create_base_schema", _migration_001_create_base_schema),
    (2, "add_track_dimensions_and_stream_index", _migration_002_add_track_dimensions_and_stream_index),
    (3, "add_segment_duration", _migration_003_add_segment_duration),
]


def init_db():
    """Initialize the database and migrate it to the latest supported schema."""
    conn = _get_conn()
    with conn:
        if not _table_exists(conn, "schema_migrations"):
            _bootstrap_legacy_schema_migrations(conn)

        current_revision = _get_recorded_schema_revision(conn)
        if current_revision > LATEST_SCHEMA_REVISION:
            raise RuntimeError(
                f"Database schema revision {current_revision} is newer than supported "
                f"revision {LATEST_SCHEMA_REVISION} for {DB_PATH}"
            )

        for revision, name, migrate in MIGRATIONS:
            if revision <= current_revision:
                continue
            logger.info("Applying database migration %03d: %s", revision, name)
            migrate(conn)
            _create_schema_migrations_table(conn)
            _record_migration(conn, revision, name)
            current_revision = revision

    logger.info("Database initialized at %s (schema revision %d)", DB_PATH, current_revision)


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
        "SELECT segment_key, duration, file_id, bot_index FROM segments WHERE job_id = ? AND segment_key LIKE ? ORDER BY segment_key",
        (job_id, f"{prefix}/%"),
    ).fetchall()
    return [{"segment_key": r["segment_key"], "duration": r["duration"] or 0,
             "file_id": r["file_id"], "bot_index": r["bot_index"]} for r in rows]


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
