import aiosqlite
import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timezone
from logger_config import logger

@dataclass
class SegmentInfo:
    """Holds information about each video segment."""
    filename: str
    duration: float
    file_id: str
    file_size: int
    segment_order: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

@dataclass
class VideoInfo:
    """Holds information about each video."""
    video_id: str
    original_filename: str
    total_duration: float
    total_segments: int
    file_size: int
    created_at: str
    updated_at: str
    status: str = 'active'  # active, processing, error, deleted

class DatabaseManager:
    """Manages SQLite database operations for the video streaming system."""

    def __init__(self, db_path: str = "video_streaming.db"):
        """
        Initializes the DatabaseManager.

        Args:
            db_path (str): The path to the SQLite database file.
        """
        self.db_path = db_path
        logger.info(f"DatabaseManager initialized with db_path: {self.db_path}")

    async def initialize_database(self):
        """
        Initializes the database by creating the necessary tables and indexes if they don't exist.
        This method should be called once when the application starts.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS videos (
                        video_id TEXT PRIMARY KEY,
                        original_filename TEXT NOT NULL,
                        total_duration REAL NOT NULL DEFAULT 0.0,
                        total_segments INTEGER NOT NULL DEFAULT 0,
                        file_size INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS segments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        duration REAL NOT NULL,
                        file_id TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        segment_order INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE,
                        UNIQUE(video_id, filename),
                        UNIQUE(video_id, segment_order)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cache_metadata (
                        segment_filename TEXT PRIMARY KEY,
                        video_id TEXT NOT NULL,
                        cached_at TEXT NOT NULL,
                        access_count INTEGER NOT NULL DEFAULT 1,
                        last_accessed TEXT NOT NULL,
                        cache_size INTEGER NOT NULL,
                        FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
                    )
                """)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_video_id ON segments (video_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_order ON segments (video_id, segment_order)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON cache_metadata (last_accessed)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (status)")
                await db.commit()
                logger.info("✅ Database initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize database: {e}", exc_info=True)

    async def add_video(self, video_info: VideoInfo) -> bool:
        """
        Adds or replaces a video record in the database.

        Args:
            video_info (VideoInfo): The VideoInfo object containing the video's metadata.

        Returns:
            bool: True if the video was added successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO videos
                    (video_id, original_filename, total_duration, total_segments, file_size, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_info.video_id, video_info.original_filename, video_info.total_duration,
                    video_info.total_segments, video_info.file_size, video_info.status,
                    video_info.created_at, video_info.updated_at
                ))
                await db.commit()
                logger.info(f"✅ Added/Updated video {video_info.video_id} in the database.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add video {video_info.video_id}: {e}", exc_info=True)
            return False

    async def add_segment(self, video_id: str, segment_info: SegmentInfo) -> bool:
        """
        Adds a video segment record to the database.

        Args:
            video_id (str): The ID of the video to which the segment belongs.
            segment_info (SegmentInfo): The SegmentInfo object with the segment's metadata.

        Returns:
            bool: True if the segment was added successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = datetime.now(timezone.utc).isoformat()
                await db.execute("""
                    INSERT OR REPLACE INTO segments
                    (video_id, filename, duration, file_id, file_size, segment_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_id, segment_info.filename, segment_info.duration, segment_info.file_id,
                    segment_info.file_size, segment_info.segment_order, current_time, current_time
                ))
                await db.commit()
                logger.debug(f"✅ Added segment {segment_info.filename} for video {video_id}.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add segment {segment_info.filename}: {e}", exc_info=True)
            return False

    async def get_video_info(self, video_id: str) -> Optional[VideoInfo]:
        """
        Retrieves video information from the database.

        Args:
            video_id (str): The ID of the video to retrieve.

        Returns:
            Optional[VideoInfo]: A VideoInfo object if the video is found, otherwise None.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)) as cursor:
                    row = await cursor.fetchone()
                    return VideoInfo(*row) if row else None
        except Exception as e:
            logger.error(f"❌ Failed to get video info for {video_id}: {e}", exc_info=True)
            return None

    async def get_video_segments(self, video_id: str) -> Dict[str, SegmentInfo]:
        """
        Retrieves all segments for a given video, ordered by their sequence.

        Args:
            video_id (str): The ID of the video.

        Returns:
            Dict[str, SegmentInfo]: A dictionary of SegmentInfo objects, keyed by filename.
        """
        segments = {}
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT filename, duration, file_id, file_size, segment_order, created_at, updated_at
                    FROM segments WHERE video_id = ? ORDER BY segment_order
                """, (video_id,)) as cursor:
                    async for row in cursor:
                        segment = SegmentInfo(*row)
                        segments[segment.filename] = segment
        except Exception as e:
            logger.error(f"❌ Failed to get segments for video {video_id}: {e}", exc_info=True)
        return segments

    async def get_all_videos(self) -> List[VideoInfo]:
        """
        Retrieves a list of all active videos from the database.

        Returns:
            List[VideoInfo]: A list of VideoInfo objects.
        """
        videos = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT * FROM videos WHERE status = 'active' ORDER BY created_at DESC") as cursor:
                    async for row in cursor:
                        videos.append(VideoInfo(*row))
        except Exception as e:
            logger.error(f"❌ Failed to get all videos: {e}", exc_info=True)
        return videos

    async def delete_video(self, video_id: str) -> bool:
        """
        Deletes a video and all its associated segments from the database.

        Args:
            video_id (str): The ID of the video to delete.

        Returns:
            bool: True if the video was deleted successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
                await db.commit()
                logger.info(f"✅ Deleted video {video_id} from the database.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to delete video {video_id}: {e}", exc_info=True)
            return False
