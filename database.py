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
LATEST_SCHEMA_REVISION = 7

# Thread-local connections for SQLite (which doesn't allow sharing across threads)
_local = threading.local()
# Track all opened connections so they can be closed on shutdown
_all_connections = []
_all_connections_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        _local.conn = conn
        with _all_connections_lock:
            _all_connections.append(_local.conn)
    return _local.conn


def _reset_conn():
    """Close and discard the current thread's connection without raising."""
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

    job_cols = _list_table_columns(conn, "jobs")
    has_v4_jobs = "media_type" in job_cols
    has_v5_jobs = "is_series" in job_cols

    if has_v5_jobs:
        return 5
    if has_v4_jobs:
        return 4
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
        4: "add_media_metadata",
        5: "add_series_episode_metadata",
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


def _migration_004_add_media_metadata(conn: sqlite3.Connection):
    conn.executescript("""
        ALTER TABLE jobs ADD COLUMN media_type TEXT DEFAULT 'Film';
        ALTER TABLE jobs ADD COLUMN series_name TEXT DEFAULT '';
        ALTER TABLE jobs ADD COLUMN has_thumbnail INTEGER DEFAULT 0;
    """)


def _migration_005_add_series_episode_metadata(conn: sqlite3.Connection):
    conn.executescript("""
        ALTER TABLE jobs ADD COLUMN is_series INTEGER DEFAULT 0;
        ALTER TABLE jobs ADD COLUMN season_number INTEGER DEFAULT NULL;
        ALTER TABLE jobs ADD COLUMN episode_number INTEGER DEFAULT NULL;
        ALTER TABLE jobs ADD COLUMN part_number INTEGER DEFAULT NULL;
    """)


def _migration_006_create_settings_and_bots_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL,
            label      TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)


def _migration_007_add_listing_performance_indexes(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tracks_job_type ON tracks(job_id, track_type);
        CREATE INDEX IF NOT EXISTS idx_jobs_series ON jobs(series_name);
    """)


MIGRATIONS = [
    (1, "create_base_schema", _migration_001_create_base_schema),
    (2, "add_track_dimensions_and_stream_index", _migration_002_add_track_dimensions_and_stream_index),
    (3, "add_segment_duration", _migration_003_add_segment_duration),
    (4, "add_media_metadata", _migration_004_add_media_metadata),
    (5, "add_series_episode_metadata", _migration_005_add_series_episode_metadata),
    (6, "create_settings_and_bots_tables", _migration_006_create_settings_and_bots_tables),
    (7, "add_listing_performance_indexes", _migration_007_add_listing_performance_indexes),
]


def _handle_corrupt_db() -> None:
    """Rename a corrupt DB file and reset the thread-local connection so a fresh
    database can be created on the next _get_conn() call.

    The corrupt file is kept (renamed) so the user has a chance to attempt
    manual recovery via ``sqlite3 streamer.db.corrupted.N ".recover"`` or similar.
    """
    _reset_conn()
    if os.path.exists(DB_PATH):
        # Find a unique backup name
        backup_path = DB_PATH + ".corrupted"
        counter = 1
        while os.path.exists(backup_path):
            backup_path = f"{DB_PATH}.corrupted.{counter}"
            counter += 1
        try:
            os.rename(DB_PATH, backup_path)
            logger.error(
                "SQLite database at %s is corrupted. "
                "The file has been renamed to %s. "
                "A fresh database will be created. "
                "Job history and segment mappings from the corrupted database are lost "
                "unless you can recover the file manually.",
                DB_PATH,
                backup_path,
            )
        except OSError as exc:
            logger.error(
                "SQLite database at %s is corrupted and could not be renamed: %s. "
                "Attempting to delete it so a fresh database can be created.",
                DB_PATH,
                exc,
            )
            try:
                os.remove(DB_PATH)
            except OSError:
                pass


def _integrity_check(conn: sqlite3.Connection) -> None:
    """Run a quick SQLite integrity check; raise DatabaseError if the DB is corrupt."""
    result = conn.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise sqlite3.DatabaseError(
            f"PRAGMA quick_check returned: {result[0] if result else 'no result'}"
        )


def init_db():
    """Initialize the database and migrate it to the latest supported schema."""
    try:
        conn = _get_conn()
    except sqlite3.DatabaseError as exc:
        logger.error("Failed to open database: %s — attempting corruption recovery.", exc)
        _handle_corrupt_db()
        conn = _get_conn()
    try:
        _integrity_check(conn)
    except sqlite3.DatabaseError as exc:
        logger.error("Database integrity check failed: %s — attempting corruption recovery.", exc)
        _handle_corrupt_db()
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

def save_job(job_id, analysis, processing_result, upload_result,
             media_type=None, series_name=None,
             is_series=None, season_number=None, episode_number=None, part_number=None):
    """Persist a completed job with all its tracks and segments.

    Uses an explicit transaction so partial failures roll back cleanly.
    """
    conn = _get_conn()

    try:
        with conn:
            video = analysis.video_streams[0] if analysis.has_video else None

            conn.execute(
                """INSERT OR REPLACE INTO jobs
                   (job_id, filename, duration, file_size, video_codec, video_width, video_height,
                    media_type, series_name, is_series, season_number, episode_number, part_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    os.path.basename(analysis.file_path),
                    analysis.duration,
                    analysis.file_size,
                    video.codec_name if video else None,
                    video.width if video else 0,
                    video.height if video else 0,
                    media_type or "Film",
                    series_name or "",
                    1 if is_series else 0,
                    season_number,
                    episode_number,
                    part_number,
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


def update_job_thumbnail(job_id):
    """Mark a job as having a thumbnail (sets has_thumbnail = 1)."""
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE jobs SET has_thumbnail = 1 WHERE job_id = ?", (job_id,))


def update_job_metadata(job_id, media_type=None, series_name=None,
                        is_series=None, season_number=None, episode_number=None, part_number=None):
    """Update metadata for an existing job."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """UPDATE jobs
               SET media_type = ?, series_name = ?, is_series = ?,
                   season_number = ?, episode_number = ?, part_number = ?
               WHERE job_id = ?""",
            (media_type, series_name, is_series,
             season_number, episode_number, part_number, job_id)
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


def list_jobs(limit=50, offset=0, search=None, category=None, group_by=None, series_name=None, season_number=None):
    """List jobs or groups of jobs (series/seasons) with pagination.

    Args:
        limit: Maximum number of items to return.
        offset: Number of items to skip.
        search: Optional search filter.
        category: Optional category filter.
        group_by: 'series' or 'season' to group results.
        series_name: Filter by specific series when grouping by season or listing episodes.
        season_number: Filter by specific season when listing episodes.
    """
    conn = _get_conn()
    
    where_clauses = []
    params = []
    
    if search:
        search_val = f"%{search}%"
        where_clauses.append("(j.filename LIKE ? OR j.series_name LIKE ?)")
        params.extend([search_val, search_val])
    
    if category and category != "all":
        if category == "Anime Film":
            where_clauses.append("j.media_type IN ('Anime Film', 'Anime')")
        elif category == "Anime TV":
            where_clauses.append("j.media_type IN ('Anime TV', 'Anime')")
        else:
            where_clauses.append("j.media_type = ?")
            params.append(category)

    if series_name:
        where_clauses.append("j.series_name = ?")
        params.append(series_name)

    if season_number is not None:
        where_clauses.append("j.season_number = ?")
        params.append(season_number)
            
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    if group_by == 'series':
        # Group by series name and select the latest job as representative.
        query = f"""
            WITH filtered_jobs AS (
                SELECT j.*
                FROM jobs j
                {where_sql}
                {"AND" if where_sql else "WHERE"} j.series_name IS NOT NULL AND j.series_name != ''
            ),
            ranked_jobs AS (
                SELECT
                    fj.series_name,
                    fj.job_id,
                    fj.has_thumbnail,
                    fj.created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY fj.series_name
                        ORDER BY fj.created_at DESC
                    ) AS row_num
                FROM filtered_jobs fj
            ),
            grouped AS (
                SELECT
                    fj.series_name,
                    COUNT(*) AS episode_count,
                    MAX(fj.created_at) AS last_updated
                FROM filtered_jobs fj
                GROUP BY fj.series_name
            )
            SELECT
                g.series_name,
                g.episode_count,
                g.last_updated,
                rj.job_id,
                rj.has_thumbnail,
                'Series' AS media_type
            FROM grouped g
            JOIN ranked_jobs rj
              ON rj.series_name = g.series_name
             AND rj.row_num = 1
            ORDER BY g.last_updated DESC
            LIMIT ? OFFSET ?
        """
    elif group_by == 'season':
        # Group by season for a specific series.
        query = f"""
            WITH filtered_jobs AS (
                SELECT j.*
                FROM jobs j
                {where_sql}
            ),
            ranked_jobs AS (
                SELECT
                    fj.series_name,
                    fj.season_number,
                    fj.job_id,
                    fj.has_thumbnail,
                    ROW_NUMBER() OVER (
                        PARTITION BY fj.series_name, fj.season_number
                        ORDER BY fj.episode_number ASC
                    ) AS row_num
                FROM filtered_jobs fj
            ),
            grouped AS (
                SELECT
                    fj.series_name,
                    fj.season_number,
                    COUNT(*) AS episode_count,
                    MAX(fj.created_at) AS last_updated
                FROM filtered_jobs fj
                GROUP BY fj.series_name, fj.season_number
            )
            SELECT
                g.series_name,
                g.season_number,
                g.episode_count,
                g.last_updated,
                rj.job_id,
                rj.has_thumbnail,
                'Series' AS media_type
            FROM grouped g
            JOIN ranked_jobs rj
              ON rj.series_name = g.series_name
             AND (
                    (rj.season_number = g.season_number)
                    OR (rj.season_number IS NULL AND g.season_number IS NULL)
                 )
             AND rj.row_num = 1
            ORDER BY g.season_number ASC
            LIMIT ? OFFSET ?
        """
    else:
        # Standard episode list.
        query = f"""
            WITH filtered_jobs AS (
                SELECT j.*
                FROM jobs j
                {where_sql}
            ),
            track_counts AS (
                SELECT
                    t.job_id,
                    SUM(CASE WHEN t.track_type = 'audio' THEN 1 ELSE 0 END) AS audio_count,
                    SUM(CASE WHEN t.track_type = 'subtitle' THEN 1 ELSE 0 END) AS subtitle_count
                FROM tracks t
                GROUP BY t.job_id
            ),
            segment_counts AS (
                SELECT s.job_id, COUNT(*) AS segment_count
                FROM segments s
                GROUP BY s.job_id
            ),
            series_last_updated AS (
                SELECT
                    fj.series_name,
                    MAX(fj.created_at) AS series_last_updated
                FROM filtered_jobs fj
                WHERE fj.series_name IS NOT NULL AND fj.series_name != ''
                GROUP BY fj.series_name
            )
            SELECT
                fj.*,
                COALESCE(tc.audio_count, 0) AS audio_count,
                COALESCE(tc.subtitle_count, 0) AS subtitle_count,
                COALESCE(sc.segment_count, 0) AS segment_count
            FROM filtered_jobs fj
            LEFT JOIN track_counts tc ON tc.job_id = fj.job_id
            LEFT JOIN segment_counts sc ON sc.job_id = fj.job_id
            LEFT JOIN series_last_updated slu ON slu.series_name = fj.series_name
            ORDER BY 
                CASE WHEN fj.series_name IS NOT NULL AND fj.series_name != ''
                     THEN slu.series_last_updated
                     ELSE fj.created_at
                END DESC,
                fj.series_name ASC,
                fj.season_number ASC,
                fj.episode_number ASC,
                fj.part_number ASC,
                fj.created_at DESC
            LIMIT ? OFFSET ?
        """
        
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def count_jobs(search=None, category=None, group_by=None, series_name=None, season_number=None):
    """Return the total number of items (jobs or groups) matching filters."""
    conn = _get_conn()
    
    where_clauses = []
    params = []
    
    if search:
        search_val = f"%{search}%"
        where_clauses.append("(filename LIKE ? OR series_name LIKE ?)")
        params.extend([search_val, search_val])
    
    if category and category != "all":
        if category == "Anime Film":
            where_clauses.append("media_type IN ('Anime Film', 'Anime')")
        elif category == "Anime TV":
            where_clauses.append("media_type IN ('Anime TV', 'Anime')")
        else:
            where_clauses.append("media_type = ?")
            params.append(category)

    if series_name:
        where_clauses.append("series_name = ?")
        params.append(series_name)
            
    if season_number is not None:
        where_clauses.append("season_number = ?")
        params.append(season_number)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    if group_by == 'series':
        series_filter_prefix = "AND" if where_sql else "WHERE"
        query = (
            f"SELECT COUNT(DISTINCT series_name) AS cnt FROM jobs {where_sql} "
            f"{series_filter_prefix} series_name IS NOT NULL AND series_name != ''"
        )
    elif group_by == 'season':
        query = f"SELECT COUNT(*) FROM (SELECT 1 FROM jobs {where_sql} GROUP BY series_name, season_number) as t"
    else:
        query = f"SELECT COUNT(*) AS cnt FROM jobs {where_sql}"
    
    row = conn.execute(query, params).fetchone()
    if group_by == 'season':
        return row[0] if row else 0
    return row["cnt"] if row else 0



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


# ─── Settings CRUD ────────────────────────────────────────────────────────────

def get_all_settings() -> dict:
    """Return all rows from the settings table as {key: value}."""
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str):
    """Insert or replace a single setting."""
    conn = _get_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )


def set_settings(mapping: dict):
    """Bulk upsert multiple settings in a single transaction."""
    conn = _get_conn()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            [(k, v) for k, v in mapping.items()],
        )


def delete_setting(key: str):
    """Remove a setting, reverting to .env/default on next Config.reload()."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# ─── Bots CRUD ────────────────────────────────────────────────────────────────

def get_all_bots() -> list:
    """Return all rows from the bots table as a list of dicts."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, token, channel_id, label FROM bots ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def add_bot(token: str, channel_id: int, label: str = "") -> int:
    """Insert a new bot and return its id."""
    conn = _get_conn()
    with conn:
        cursor = conn.execute(
            "INSERT INTO bots (token, channel_id, label) VALUES (?, ?, ?)",
            (token, channel_id, label),
        )
    return cursor.lastrowid


def delete_bot(bot_id: int):
    """Delete a bot by its primary key."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))


def bot_exists(token: str) -> bool:
    """Return True if a bot with the given token already exists."""
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM bots WHERE token = ?", (token,)).fetchone()
    return row is not None


# Initialize on import
init_db()
