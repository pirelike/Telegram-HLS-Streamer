"""
Database Management for Telegram Video Streaming System.

This module provides comprehensive database operations using SQLite with
aiosqlite for async operations. It manages video metadata, segment information,
and provides efficient querying and data integrity features.

Features:
- Async SQLite operations with connection pooling
- Data validation and integrity constraints
- Comprehensive error handling and logging
- Database migration support
- Performance optimizations with proper indexing
"""

import aiosqlite
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, AsyncGenerator
import json

from logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SegmentInfo:
    """
    Immutable data class for video segment information.

    This class holds all metadata associated with a video segment,
    including Telegram file information and timing data.
    """
    filename: str
    duration: float
    file_id: str
    file_size: int
    segment_order: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        """Validate segment data after initialization."""
        if not self.filename or not self.filename.strip():
            raise ValueError("Filename cannot be empty")

        if self.duration < 0:
            raise ValueError("Duration cannot be negative")

        if not self.file_id or not self.file_id.strip():
            raise ValueError("File ID cannot be empty")

        if self.file_size < 0:
            raise ValueError("File size cannot be negative")

        if self.segment_order < 0:
            raise ValueError("Segment order cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SegmentInfo':
        """Create instance from dictionary."""
        return cls(**data)


@dataclass(frozen=True)
class VideoInfo:
    """
    Immutable data class for video information.

    This class holds all metadata associated with an uploaded video,
    including processing status and statistics.
    """
    video_id: str
    original_filename: str
    total_duration: float
    total_segments: int
    file_size: int
    created_at: str
    updated_at: str
    status: str = 'active'  # active, processing, error, deleted

    # Valid status values
    VALID_STATUSES = {'active', 'processing', 'error', 'deleted'}

    def __post_init__(self):
        """Validate video data after initialization."""
        if not self.video_id or not self.video_id.strip():
            raise ValueError("Video ID cannot be empty")

        if not self.original_filename or not self.original_filename.strip():
            raise ValueError("Original filename cannot be empty")

        if self.total_duration < 0:
            raise ValueError("Total duration cannot be negative")

        if self.total_segments < 0:
            raise ValueError("Total segments cannot be negative")

        if self.file_size < 0:
            raise ValueError("File size cannot be negative")

        if self.status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status}. Must be one of {self.VALID_STATUSES}")

        # Validate ISO format timestamps
        try:
            datetime.fromisoformat(self.created_at.replace('Z', '+00:00'))
            datetime.fromisoformat(self.updated_at.replace('Z', '+00:00'))
        except ValueError as e:
            raise ValueError(f"Invalid timestamp format: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VideoInfo':
        """Create instance from dictionary."""
        return cls(**data)

    @property
    def duration_minutes(self) -> float:
        """Get duration in minutes."""
        return self.total_duration / 60.0

    @property
    def size_mb(self) -> float:
        """Get file size in megabytes."""
        return self.file_size / (1024 ** 2)

    @property
    def created_datetime(self) -> datetime:
        """Get created_at as datetime object."""
        return datetime.fromisoformat(self.created_at.replace('Z', '+00:00'))

    @property
    def updated_datetime(self) -> datetime:
        """Get updated_at as datetime object."""
        return datetime.fromisoformat(self.updated_at.replace('Z', '+00:00'))


class DatabaseError(Exception):
    """Base exception for database operations."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Exception for database connection issues."""
    pass


class DatabaseIntegrityError(DatabaseError):
    """Exception for data integrity violations."""
    pass


class DatabaseManager:
    """
    Manages SQLite database operations for the video streaming system.

    This class provides a comprehensive interface for video and segment
    metadata management with proper error handling, connection pooling,
    and data validation.
    """

    # Database schema version for migration support
    SCHEMA_VERSION = 2

    # Connection pool settings
    MAX_CONNECTIONS = 10
    CONNECTION_TIMEOUT = 30.0

    def __init__(self, db_path: str = "video_streaming.db"):
        """
        Initialize the DatabaseManager.

        Args:
            db_path: Path to the SQLite database file

        Raises:
            DatabaseError: If database path is invalid
        """
        if not db_path or not db_path.strip():
            raise DatabaseError("Database path cannot be empty")

        self.db_path = Path(db_path).resolve()
        self._connection_pool: List[aiosqlite.Connection] = []
        self._pool_lock = asyncio.Lock()
        self._initialized = False

        logger.info(f"DatabaseManager initialized: {self.db_path}")

    @asynccontextmanager
    async def get_connection(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """
        Get a database connection from the pool.

        This context manager ensures proper connection handling and
        returns connections to the pool after use.

        Yields:
            Database connection

        Raises:
            DatabaseConnectionError: If connection cannot be established
        """
        connection = None
        try:
            async with self._pool_lock:
                if self._connection_pool:
                    connection = self._connection_pool.pop()
                else:
                    connection = await aiosqlite.connect(
                        self.db_path,
                        timeout=self.CONNECTION_TIMEOUT
                    )
                    await connection.execute("PRAGMA foreign_keys = ON")
                    await connection.execute("PRAGMA journal_mode = WAL")  # Better concurrency
                    await connection.execute("PRAGMA synchronous = NORMAL")  # Performance optimization

            yield connection

        except Exception as e:
            if connection:
                await connection.close()
                connection = None
            raise DatabaseConnectionError(f"Failed to get database connection: {e}") from e

        finally:
            if connection:
                async with self._pool_lock:
                    if len(self._connection_pool) < self.MAX_CONNECTIONS:
                        self._connection_pool.append(connection)
                    else:
                        await connection.close()

    async def initialize_database(self) -> None:
        """
        Initialize the database by creating tables and indexes.

        This method should be called once when the application starts.
        It handles database migrations and ensures proper schema setup.

        Raises:
            DatabaseError: If database initialization fails
        """
        if self._initialized:
            logger.debug("Database already initialized")
            return

        try:
            async with self.get_connection() as db:
                # Create metadata table for schema versioning
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS schema_info (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)

                # Check current schema version
                current_version = await self._get_schema_version(db)

                if current_version < self.SCHEMA_VERSION:
                    logger.info(f"Upgrading database schema from v{current_version} to v{self.SCHEMA_VERSION}")
                    await self._migrate_schema(db, current_version)

                # Create main tables
                await self._create_tables(db)

                # Create indexes for performance
                await self._create_indexes(db)

                await db.commit()

                self._initialized = True
                logger.info("✅ Database initialized successfully")

                # Log database statistics
                await self._log_database_stats(db)

        except Exception as e:
            logger.error(f"❌ Failed to initialize database: {e}", exc_info=True)
            raise DatabaseError(f"Database initialization failed: {e}") from e

    async def _get_schema_version(self, db: aiosqlite.Connection) -> int:
        """Get current database schema version."""
        try:
            async with db.execute(
                "SELECT value FROM schema_info WHERE key = 'version'"
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    async def _migrate_schema(self, db: aiosqlite.Connection, from_version: int) -> None:
        """
        Migrate database schema to current version.

        Args:
            db: Database connection
            from_version: Current schema version
        """
        # Migration logic would go here
        # For now, we'll just update the version
        current_time = datetime.now(timezone.utc).isoformat()

        await db.execute("""
            INSERT OR REPLACE INTO schema_info (key, value, updated_at)
            VALUES ('version', ?, ?)
        """, (str(self.SCHEMA_VERSION), current_time))

        logger.info(f"Schema migrated to version {self.SCHEMA_VERSION}")

    async def _create_tables(self, db: aiosqlite.Connection) -> None:
        """Create database tables if they don't exist."""

        # Videos table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                total_duration REAL NOT NULL DEFAULT 0.0,
                total_segments INTEGER NOT NULL DEFAULT 0,
                file_size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'processing', 'error', 'deleted')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Segments table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                duration REAL NOT NULL DEFAULT 0.0,
                file_id TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                segment_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE,
                UNIQUE(video_id, filename),
                UNIQUE(video_id, segment_order)
            )
        """)

        # Cache metadata table for future use
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache_metadata (
                segment_filename TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                cached_at TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 1,
                last_accessed TEXT NOT NULL,
                cache_size INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
            )
        """)

    async def _create_indexes(self, db: aiosqlite.Connection) -> None:
        """Create database indexes for performance optimization."""
        indexes = [
            ("idx_videos_status", "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (status)"),
            ("idx_videos_created", "CREATE INDEX IF NOT EXISTS idx_videos_created ON videos (created_at DESC)"),
            ("idx_segments_video_id", "CREATE INDEX IF NOT EXISTS idx_segments_video_id ON segments (video_id)"),
            ("idx_segments_order", "CREATE INDEX IF NOT EXISTS idx_segments_order ON segments (video_id, segment_order)"),
            ("idx_segments_filename", "CREATE INDEX IF NOT EXISTS idx_segments_filename ON segments (video_id, filename)"),
            ("idx_cache_last_accessed", "CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON cache_metadata (last_accessed DESC)"),
        ]

        for index_name, sql in indexes:
            try:
                await db.execute(sql)
                logger.debug(f"Created index: {index_name}")
            except Exception as e:
                logger.warning(f"Failed to create index {index_name}: {e}")

    async def _log_database_stats(self, db: aiosqlite.Connection) -> None:
        """Log basic database statistics."""
        try:
            async with db.execute("SELECT COUNT(*) FROM videos") as cursor:
                video_count = (await cursor.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM segments") as cursor:
                segment_count = (await cursor.fetchone())[0]

            logger.info(f"Database stats: {video_count} videos, {segment_count} segments")

        except Exception as e:
            logger.warning(f"Failed to get database stats: {e}")

    async def add_video(self, video_info: VideoInfo) -> bool:
        """
        Add or update a video record in the database.

        Args:
            video_info: VideoInfo object containing video metadata

        Returns:
            True if operation successful, False otherwise

        Raises:
            DatabaseError: If database operation fails
        """
        if not isinstance(video_info, VideoInfo):
            raise DatabaseError("video_info must be a VideoInfo instance")

        try:
            async with self.get_connection() as db:
                await db.execute("""
                    INSERT OR REPLACE INTO videos
                    (video_id, original_filename, total_duration, total_segments,
                     file_size, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_info.video_id,
                    video_info.original_filename,
                    video_info.total_duration,
                    video_info.total_segments,
                    video_info.file_size,
                    video_info.status,
                    video_info.created_at,
                    video_info.updated_at
                ))

                await db.commit()

                logger.debug(f"Added/updated video: {video_info.video_id}")
                return True

        except Exception as e:
            logger.error(f"Failed to add video {video_info.video_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to add video: {e}") from e

    async def add_segment(self, video_id: str, segment_info: SegmentInfo) -> bool:
        """
        Add a video segment record to the database.

        Args:
            video_id: ID of the parent video
            segment_info: SegmentInfo object containing segment metadata

        Returns:
            True if operation successful, False otherwise

        Raises:
            DatabaseError: If database operation fails or video doesn't exist
        """
        if not video_id or not video_id.strip():
            raise DatabaseError("video_id cannot be empty")

        if not isinstance(segment_info, SegmentInfo):
            raise DatabaseError("segment_info must be a SegmentInfo instance")

        try:
            async with self.get_connection() as db:
                # Verify parent video exists
                async with db.execute(
                    "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
                ) as cursor:
                    if not await cursor.fetchone():
                        raise DatabaseIntegrityError(f"Video {video_id} does not exist")

                # Add current timestamps if not provided
                current_time = datetime.now(timezone.utc).isoformat()
                created_at = segment_info.created_at or current_time
                updated_at = segment_info.updated_at or current_time

                await db.execute("""
                    INSERT OR REPLACE INTO segments
                    (video_id, filename, duration, file_id, file_size,
                     segment_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_id,
                    segment_info.filename,
                    segment_info.duration,
                    segment_info.file_id,
                    segment_info.file_size,
                    segment_info.segment_order,
                    created_at,
                    updated_at
                ))

                await db.commit()

                logger.debug(f"Added segment: {segment_info.filename} for video {video_id}")
                return True

        except DatabaseIntegrityError:
            raise
        except Exception as e:
            logger.error(f"Failed to add segment {segment_info.filename}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to add segment: {e}") from e

    async def get_video_info(self, video_id: str) -> Optional[VideoInfo]:
        """
        Retrieve video information from the database.

        Args:
            video_id: ID of the video to retrieve

        Returns:
            VideoInfo object if found, None otherwise

        Raises:
            DatabaseError: If database operation fails
        """
        if not video_id or not video_id.strip():
            raise DatabaseError("video_id cannot be empty")

        try:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT * FROM videos WHERE video_id = ?", (video_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                    if row:
                        return VideoInfo(*row)
                    return None

        except Exception as e:
            logger.error(f"Failed to get video info for {video_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to get video info: {e}") from e

    async def get_video_segments(self, video_id: str) -> Dict[str, SegmentInfo]:
        """
        Retrieve all segments for a video, ordered by segment order.

        Args:
            video_id: ID of the video

        Returns:
            Dictionary mapping filename to SegmentInfo objects

        Raises:
            DatabaseError: If database operation fails
        """
        if not video_id or not video_id.strip():
            raise DatabaseError("video_id cannot be empty")

        segments = {}

        try:
            async with self.get_connection() as db:
                async with db.execute("""
                    SELECT filename, duration, file_id, file_size, segment_order, created_at, updated_at
                    FROM segments
                    WHERE video_id = ?
                    ORDER BY segment_order
                """, (video_id,)) as cursor:
                    async for row in cursor:
                        segment = SegmentInfo(*row)
                        segments[segment.filename] = segment

            logger.debug(f"Retrieved {len(segments)} segments for video {video_id}")
            return segments

        except Exception as e:
            logger.error(f"Failed to get segments for video {video_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to get segments: {e}") from e

    async def get_all_videos(
        self,
        status_filter: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[VideoInfo]:
        """
        Retrieve all videos from the database with optional filtering.

        Args:
            status_filter: Filter by video status (None for all statuses)
            limit: Maximum number of videos to return
            offset: Number of videos to skip (for pagination)

        Returns:
            List of VideoInfo objects

        Raises:
            DatabaseError: If database operation fails
        """
        videos = []

        try:
            async with self.get_connection() as db:
                # Build query based on parameters
                query = "SELECT * FROM videos"
                params = []

                if status_filter:
                    if status_filter not in VideoInfo.VALID_STATUSES:
                        raise DatabaseError(f"Invalid status filter: {status_filter}")
                    query += " WHERE status = ?"
                    params.append(status_filter)

                query += " ORDER BY created_at DESC"

                if limit:
                    query += " LIMIT ?"
                    params.append(limit)

                    if offset > 0:
                        query += " OFFSET ?"
                        params.append(offset)

                async with db.execute(query, params) as cursor:
                    async for row in cursor:
                        videos.append(VideoInfo(*row))

            logger.debug(f"Retrieved {len(videos)} videos (status={status_filter}, limit={limit})")
            return videos

        except Exception as e:
            logger.error(f"Failed to get videos: {e}", exc_info=True)
            raise DatabaseError(f"Failed to get videos: {e}") from e

    async def update_video_status(self, video_id: str, new_status: str) -> bool:
        """
        Update the status of a video.

        Args:
            video_id: ID of the video to update
            new_status: New status value

        Returns:
            True if update successful, False if video not found

        Raises:
            DatabaseError: If database operation fails or status is invalid
        """
        if not video_id or not video_id.strip():
            raise DatabaseError("video_id cannot be empty")

        if new_status not in VideoInfo.VALID_STATUSES:
            raise DatabaseError(f"Invalid status: {new_status}")

        try:
            async with self.get_connection() as db:
                updated_at = datetime.now(timezone.utc).isoformat()

                cursor = await db.execute("""
                    UPDATE videos
                    SET status = ?, updated_at = ?
                    WHERE video_id = ?
                """, (new_status, updated_at, video_id))

                await db.commit()

                success = cursor.rowcount > 0

                if success:
                    logger.info(f"Updated video {video_id} status to {new_status}")
                else:
                    logger.warning(f"Video {video_id} not found for status update")

                return success

        except Exception as e:
            logger.error(f"Failed to update video status: {e}", exc_info=True)
            raise DatabaseError(f"Failed to update video status: {e}") from e

    async def delete_video(self, video_id: str) -> bool:
        """
        Delete a video and all its associated segments.

        Args:
            video_id: ID of the video to delete

        Returns:
            True if deletion successful, False if video not found

        Raises:
            DatabaseError: If database operation fails
        """
        if not video_id or not video_id.strip():
            raise DatabaseError("video_id cannot be empty")

        try:
            async with self.get_connection() as db:
                # Check if video exists and get info for logging
                video_info = await self.get_video_info(video_id)
                if not video_info:
                    logger.warning(f"Video {video_id} not found for deletion")
                    return False

                # Delete video (segments will be deleted automatically due to CASCADE)
                cursor = await db.execute(
                    "DELETE FROM videos WHERE video_id = ?", (video_id,)
                )

                await db.commit()

                success = cursor.rowcount > 0

                if success:
                    logger.info(f"Deleted video {video_id} ({video_info.original_filename})")

                return success

        except Exception as e:
            logger.error(f"Failed to delete video {video_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to delete video: {e}") from e

    async def get_video_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive statistics about stored videos.

        Returns:
            Dictionary containing various statistics

        Raises:
            DatabaseError: If database operation fails
        """
        try:
            async with self.get_connection() as db:
                stats = {}

                # Basic counts
                async with db.execute("SELECT COUNT(*) FROM videos") as cursor:
                    stats['total_videos'] = (await cursor.fetchone())[0]

                async with db.execute("SELECT COUNT(*) FROM segments") as cursor:
                    stats['total_segments'] = (await cursor.fetchone())[0]

                # Status breakdown
                async with db.execute("""
                    SELECT status, COUNT(*)
                    FROM videos
                    GROUP BY status
                """) as cursor:
                    status_counts = {}
                    async for row in cursor:
                        status_counts[row[0]] = row[1]
                    stats['status_breakdown'] = status_counts

                # Size and duration totals
                async with db.execute("""
                    SELECT
                        SUM(file_size) as total_size,
                        SUM(total_duration) as total_duration,
                        AVG(file_size) as avg_size,
                        AVG(total_duration) as avg_duration,
                        MAX(file_size) as max_size,
                        MIN(file_size) as min_size
                    FROM videos
                    WHERE status = 'active'
                """) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        stats.update({
                            'total_size_bytes': row[0] or 0,
                            'total_duration_seconds': row[1] or 0.0,
                            'average_size_bytes': row[2] or 0,
                            'average_duration_seconds': row[3] or 0.0,
                            'largest_video_size': row[4] or 0,
                            'smallest_video_size': row[5] or 0
                        })

                # Convert to more readable units
                stats['total_size_mb'] = stats.get('total_size_bytes', 0) / (1024 ** 2)
                stats['total_size_gb'] = stats.get('total_size_bytes', 0) / (1024 ** 3)
                stats['total_duration_minutes'] = stats.get('total_duration_seconds', 0) / 60
                stats['total_duration_hours'] = stats.get('total_duration_seconds', 0) / 3600

                # Recent activity (last 24 hours)
                cutoff_time = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                cutoff_iso = cutoff_time.isoformat()

                async with db.execute("""
                    SELECT COUNT(*)
                    FROM videos
                    WHERE created_at >= ?
                """, (cutoff_iso,)) as cursor:
                    stats['videos_added_today'] = (await cursor.fetchone())[0]

                logger.debug("Retrieved database statistics")
                return stats

        except Exception as e:
            logger.error(f"Failed to get video statistics: {e}", exc_info=True)
            raise DatabaseError(f"Failed to get statistics: {e}") from e

    async def cleanup_old_processing_videos(self, max_age_hours: int = 24) -> int:
        """
        Clean up videos stuck in 'processing' status for too long.

        Args:
            max_age_hours: Maximum age in hours for processing videos

        Returns:
            Number of videos cleaned up

        Raises:
            DatabaseError: If database operation fails
        """
        if max_age_hours <= 0:
            raise DatabaseError("max_age_hours must be positive")

        try:
            cutoff_time = datetime.now(timezone.utc)
            cutoff_time = cutoff_time.replace(
                hour=cutoff_time.hour - max_age_hours,
                minute=0, second=0, microsecond=0
            )
            cutoff_iso = cutoff_time.isoformat()

            async with self.get_connection() as db:
                # Find old processing videos
                old_videos = []
                async with db.execute("""
                    SELECT video_id, original_filename
                    FROM videos
                    WHERE status = 'processing' AND created_at < ?
                """, (cutoff_iso,)) as cursor:
                    async for row in cursor:
                        old_videos.append((row[0], row[1]))

                if not old_videos:
                    logger.debug("No old processing videos found")
                    return 0

                # Delete old processing videos
                cleanup_count = 0
                for video_id, filename in old_videos:
                    try:
                        success = await self.delete_video(video_id)
                        if success:
                            cleanup_count += 1
                            logger.info(f"Cleaned up old processing video: {filename} ({video_id})")
                    except Exception as e:
                        logger.warning(f"Failed to cleanup video {video_id}: {e}")

                logger.info(f"Cleaned up {cleanup_count} old processing videos")
                return cleanup_count

        except Exception as e:
            logger.error(f"Failed to cleanup old processing videos: {e}", exc_info=True)
            raise DatabaseError(f"Failed to cleanup old videos: {e}") from e

    async def search_videos(
        self,
        search_term: str,
        limit: int = 50
    ) -> List[VideoInfo]:
        """
        Search for videos by filename.

        Args:
            search_term: Term to search for in filenames
            limit: Maximum number of results to return

        Returns:
            List of matching VideoInfo objects

        Raises:
            DatabaseError: If database operation fails
        """
        if not search_term or not search_term.strip():
            return []

        search_pattern = f"%{search_term.strip()}%"

        try:
            async with self.get_connection() as db:
                videos = []
                async with db.execute("""
                    SELECT * FROM videos
                    WHERE original_filename LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (search_pattern, limit)) as cursor:
                    async for row in cursor:
                        videos.append(VideoInfo(*row))

                logger.debug(f"Found {len(videos)} videos matching '{search_term}'")
                return videos

        except Exception as e:
            logger.error(f"Failed to search videos: {e}", exc_info=True)
            raise DatabaseError(f"Failed to search videos: {e}") from e

    async def backup_database(self, backup_path: str) -> bool:
        """
        Create a backup of the database.

        Args:
            backup_path: Path where backup should be saved

        Returns:
            True if backup successful, False otherwise

        Raises:
            DatabaseError: If backup operation fails
        """
        try:
            backup_file = Path(backup_path)
            backup_file.parent.mkdir(parents=True, exist_ok=True)

            async with self.get_connection() as source_db:
                # Create backup connection
                async with aiosqlite.connect(backup_file) as backup_db:
                    # Use SQLite's backup API
                    await source_db.backup(backup_db)

            logger.info(f"Database backed up to: {backup_file}")
            return True

        except Exception as e:
            logger.error(f"Failed to backup database: {e}", exc_info=True)
            raise DatabaseError(f"Failed to backup database: {e}") from e

    async def optimize_database(self) -> None:
        """
        Optimize database performance by running maintenance commands.

        Raises:
            DatabaseError: If optimization fails
        """
        try:
            async with self.get_connection() as db:
                # Analyze tables to update statistics
                await db.execute("ANALYZE")

                # Vacuum to reclaim space (use with caution on large DBs)
                file_size_mb = self.db_path.stat().st_size / (1024 ** 2)
                if file_size_mb < 100:  # Only vacuum smaller databases
                    await db.execute("VACUUM")
                    logger.info("Database vacuumed")
                else:
                    logger.info("Skipped VACUUM (database too large)")

                await db.commit()

            logger.info("Database optimization completed")

        except Exception as e:
            logger.error(f"Failed to optimize database: {e}", exc_info=True)
            raise DatabaseError(f"Failed to optimize database: {e}") from e

    async def close(self) -> None:
        """
        Close all database connections and clean up resources.

        This method should be called when shutting down the application.
        """
        try:
            async with self._pool_lock:
                while self._connection_pool:
                    connection = self._connection_pool.pop()
                    await connection.close()

            self._initialized = False
            logger.info("Database connections closed")

        except Exception as e:
            logger.error(f"Error closing database connections: {e}", exc_info=True)

    async def get_database_info(self) -> Dict[str, Any]:
        """
        Get information about the database file and performance.

        Returns:
            Dictionary with database information
        """
        try:
            info = {
                'database_path': str(self.db_path),
                'database_exists': self.db_path.exists(),
                'schema_version': self.SCHEMA_VERSION,
                'connection_pool_size': len(self._connection_pool),
                'max_connections': self.MAX_CONNECTIONS
            }

            if self.db_path.exists():
                stat = self.db_path.stat()
                info.update({
                    'file_size_bytes': stat.st_size,
                    'file_size_mb': stat.st_size / (1024 ** 2),
                    'last_modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                })

            # Get current schema version from database
            if self._initialized:
                async with self.get_connection() as db:
                    current_version = await self._get_schema_version(db)
                    info['current_schema_version'] = current_version

            return info

        except Exception as e:
            logger.error(f"Failed to get database info: {e}", exc_info=True)
            return {'error': str(e)}


# Utility functions for database operations

async def create_database_manager(db_path: str = "video_streaming.db") -> DatabaseManager:
    """
    Create and initialize a database manager.

    Args:
        db_path: Path to the database file

    Returns:
        Initialized DatabaseManager instance

    Raises:
        DatabaseError: If initialization fails
    """
    manager = DatabaseManager(db_path)
    await manager.initialize_database()
    return manager


async def migrate_database(old_db_path: str, new_db_path: str) -> bool:
    """
    Migrate data from one database to another.

    Args:
        old_db_path: Path to the source database
        new_db_path: Path to the destination database

    Returns:
        True if migration successful, False otherwise
    """
    try:
        # Create new database
        new_manager = await create_database_manager(new_db_path)

        # Open old database
        old_manager = DatabaseManager(old_db_path)
        await old_manager.initialize_database()

        # Migrate videos
        old_videos = await old_manager.get_all_videos()
        for video in old_videos:
            await new_manager.add_video(video)

            # Migrate segments for this video
            segments = await old_manager.get_video_segments(video.video_id)
            for segment in segments.values():
                await new_manager.add_segment(video.video_id, segment)

        await old_manager.close()
        await new_manager.close()

        logger.info(f"Migrated {len(old_videos)} videos from {old_db_path} to {new_db_path}")
        return True

    except Exception as e:
        logger.error(f"Database migration failed: {e}", exc_info=True)
        return False
