import aiosqlite
import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timezone
from src.utils.logging import logger

@dataclass
class SegmentInfo:
    """Holds information about each video segment with bot tracking."""
    filename: str
    duration: float
    file_id: str
    file_size: int
    segment_order: int = 0
    bot_id: str = ""  # NEW: Track which bot uploaded this segment
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

@dataclass
class SubtitleInfo:
    """Holds information about subtitle tracks."""
    video_id: str
    track_index: int
    language: str
    title: str
    codec: str
    is_default: bool = False
    is_forced: bool = False
    is_hearing_impaired: bool = False
    file_path: Optional[str] = None
    created_at: Optional[str] = None

@dataclass
class SubtitleFileInfo:
    """Holds information about extracted subtitle files."""
    video_id: str
    track_index: int
    filename: str
    file_id: str
    file_size: int
    language: str
    file_type: str  # 'srt', 'ass', 'vtt', etc.
    created_at: str
    bot_id: str = ""  # NEW: Track which bot uploaded this subtitle

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
    # New fields for enhanced video info
    format_name: str = 'unknown'
    video_codec: str = 'unknown'
    audio_codec: str = 'unknown'
    resolution: str = 'unknown'
    bitrate: int = 0
    subtitle_count: int = 0

class DatabaseManager:
    """Manages SQLite database operations for the video streaming system with subtitle support and bot tracking."""

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

                # Enhanced videos table with additional metadata
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS videos (
                        video_id TEXT PRIMARY KEY,
                        original_filename TEXT NOT NULL,
                        total_duration REAL NOT NULL DEFAULT 0.0,
                        total_segments INTEGER NOT NULL DEFAULT 0,
                        file_size INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        format_name TEXT DEFAULT 'unknown',
                        video_codec TEXT DEFAULT 'unknown',
                        audio_codec TEXT DEFAULT 'unknown',
                        resolution TEXT DEFAULT 'unknown',
                        bitrate INTEGER DEFAULT 0,
                        subtitle_count INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)

                # Add new columns to existing videos table if they don't exist
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN format_name TEXT DEFAULT 'unknown'")
                except:
                    pass  # Column already exists
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN video_codec TEXT DEFAULT 'unknown'")
                except:
                    pass
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN audio_codec TEXT DEFAULT 'unknown'")
                except:
                    pass
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN resolution TEXT DEFAULT 'unknown'")
                except:
                    pass
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN bitrate INTEGER DEFAULT 0")
                except:
                    pass
                try:
                    await db.execute("ALTER TABLE videos ADD COLUMN subtitle_count INTEGER DEFAULT 0")
                except:
                    pass

                # Enhanced segments table with bot tracking
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS segments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        duration REAL NOT NULL,
                        file_id TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        segment_order INTEGER NOT NULL,
                        bot_id TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE,
                        UNIQUE(video_id, filename),
                        UNIQUE(video_id, segment_order)
                    )
                """)

                # Add bot_id column to existing segments table if it doesn't exist
                try:
                    await db.execute("ALTER TABLE segments ADD COLUMN bot_id TEXT DEFAULT ''")
                except:
                    pass  # Column already exists

                # Subtitles table
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS subtitles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id TEXT NOT NULL,
                        track_index INTEGER NOT NULL,
                        language TEXT NOT NULL DEFAULT 'und',
                        title TEXT DEFAULT '',
                        codec TEXT NOT NULL,
                        is_default BOOLEAN DEFAULT FALSE,
                        is_forced BOOLEAN DEFAULT FALSE,
                        is_hearing_impaired BOOLEAN DEFAULT FALSE,
                        file_path TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE,
                        UNIQUE(video_id, track_index)
                    )
                """)

                # Enhanced subtitle files table with bot tracking
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS subtitle_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id TEXT NOT NULL,
                        track_index INTEGER NOT NULL,
                        filename TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        language TEXT NOT NULL DEFAULT 'und',
                        file_type TEXT NOT NULL DEFAULT 'srt',
                        bot_id TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE,
                        FOREIGN KEY (video_id, track_index) REFERENCES subtitles (video_id, track_index) ON DELETE CASCADE,
                        UNIQUE(video_id, track_index)
                    )
                """)

                # Add bot_id column to existing subtitle_files table if it doesn't exist
                try:
                    await db.execute("ALTER TABLE subtitle_files ADD COLUMN bot_id TEXT DEFAULT ''")
                except:
                    pass  # Column already exists

                # Cache metadata table (unchanged)
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

                # Create indexes
                await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_video_id ON segments (video_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_order ON segments (video_id, segment_order)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_bot_id ON segments (bot_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON cache_metadata (last_accessed)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (status)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_subtitles_video_id ON subtitles (video_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_subtitles_language ON subtitles (language)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_subtitle_files_video_id ON subtitle_files (video_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_subtitle_files_language ON subtitle_files (language)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_subtitle_files_bot_id ON subtitle_files (bot_id)")

                await db.commit()
                logger.info("✅ Database initialized successfully with bot tracking support")
        except Exception as e:
            logger.error(f"❌ Failed to initialize database: {e}", exc_info=True)

    async def add_subtitle_file(self, subtitle_file_info: SubtitleFileInfo) -> bool:
        """
        Adds a subtitle file record to the database.

        Args:
            subtitle_file_info (SubtitleFileInfo): The SubtitleFileInfo object with the file's metadata.

        Returns:
            bool: True if the subtitle file was added successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO subtitle_files
                    (video_id, track_index, filename, file_id, file_size, language, file_type, bot_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    subtitle_file_info.video_id, subtitle_file_info.track_index,
                    subtitle_file_info.filename, subtitle_file_info.file_id,
                    subtitle_file_info.file_size, subtitle_file_info.language,
                    subtitle_file_info.file_type, subtitle_file_info.bot_id, subtitle_file_info.created_at
                ))
                await db.commit()
                logger.debug(f"✅ Added subtitle file {subtitle_file_info.filename} for video {subtitle_file_info.video_id}.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add subtitle file: {e}", exc_info=True)
            return False

    async def get_subtitle_files(self, video_id: str) -> List[SubtitleFileInfo]:
        """
        Retrieves all subtitle files for a given video.

        Args:
            video_id (str): The ID of the video.

        Returns:
            List[SubtitleFileInfo]: A list of SubtitleFileInfo objects.
        """
        subtitle_files = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT video_id, track_index, filename, file_id, file_size, language, file_type, created_at, bot_id
                    FROM subtitle_files WHERE video_id = ? ORDER BY track_index
                """, (video_id,)) as cursor:
                    async for row in cursor:
                        # Handle both old and new schema by checking row length
                        if len(row) == 8:  # Old schema without bot_id
                            subtitle_file = SubtitleFileInfo(
                                video_id=row[0], track_index=row[1], filename=row[2],
                                file_id=row[3], file_size=row[4], language=row[5],
                                file_type=row[6], created_at=row[7], bot_id=""
                            )
                        else:  # New schema with bot_id
                            subtitle_file = SubtitleFileInfo(
                                video_id=row[0], track_index=row[1], filename=row[2],
                                file_id=row[3], file_size=row[4], language=row[5],
                                file_type=row[6], created_at=row[7], bot_id=row[8]
                            )
                        subtitle_files.append(subtitle_file)
        except Exception as e:
            logger.error(f"❌ Failed to get subtitle files for video {video_id}: {e}", exc_info=True)
        return subtitle_files

    async def get_subtitle_file_by_language(self, video_id: str, language: str) -> Optional[SubtitleFileInfo]:
        """
        Get a subtitle file by video ID and language.

        Args:
            video_id (str): The video ID
            language (str): The language code

        Returns:
            Optional[SubtitleFileInfo]: The subtitle file info, or None if not found
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT video_id, track_index, filename, file_id, file_size, language, file_type, created_at, bot_id
                    FROM subtitle_files WHERE video_id = ? AND language = ? LIMIT 1
                """, (video_id, language)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        # Handle both old and new schema
                        if len(row) == 8:  # Old schema without bot_id
                            return SubtitleFileInfo(
                                video_id=row[0], track_index=row[1], filename=row[2],
                                file_id=row[3], file_size=row[4], language=row[5],
                                file_type=row[6], created_at=row[7], bot_id=""
                            )
                        else:  # New schema with bot_id
                            return SubtitleFileInfo(
                                video_id=row[0], track_index=row[1], filename=row[2],
                                file_id=row[3], file_size=row[4], language=row[5],
                                file_type=row[6], created_at=row[7], bot_id=row[8]
                            )
        except Exception as e:
            logger.error(f"❌ Failed to get subtitle file for {video_id}/{language}: {e}")
        return None

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
                    (video_id, original_filename, total_duration, total_segments, file_size, status,
                     format_name, video_codec, audio_codec, resolution, bitrate, subtitle_count,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_info.video_id, video_info.original_filename, video_info.total_duration,
                    video_info.total_segments, video_info.file_size, video_info.status,
                    video_info.format_name, video_info.video_codec, video_info.audio_codec,
                    video_info.resolution, video_info.bitrate, video_info.subtitle_count,
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
        Adds a video segment record to the database with bot tracking.

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
                    (video_id, filename, duration, file_id, file_size, segment_order, bot_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_id, segment_info.filename, segment_info.duration, segment_info.file_id,
                    segment_info.file_size, segment_info.segment_order, segment_info.bot_id,
                    current_time, current_time
                ))
                await db.commit()
                logger.debug(f"✅ Added segment {segment_info.filename} for video {video_id} (bot: {segment_info.bot_id}).")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add segment {segment_info.filename}: {e}", exc_info=True)
            return False

    async def add_subtitle(self, subtitle_info: SubtitleInfo) -> bool:
        """
        Adds a subtitle track record to the database.

        Args:
            subtitle_info (SubtitleInfo): The SubtitleInfo object with the subtitle's metadata.

        Returns:
            bool: True if the subtitle was added successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = datetime.now(timezone.utc).isoformat()
                await db.execute("""
                    INSERT OR REPLACE INTO subtitles
                    (video_id, track_index, language, title, codec, is_default, is_forced,
                     is_hearing_impaired, file_path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    subtitle_info.video_id, subtitle_info.track_index, subtitle_info.language,
                    subtitle_info.title, subtitle_info.codec, subtitle_info.is_default,
                    subtitle_info.is_forced, subtitle_info.is_hearing_impaired,
                    subtitle_info.file_path, current_time
                ))
                await db.commit()
                logger.debug(f"✅ Added subtitle track {subtitle_info.track_index} ({subtitle_info.language}) for video {subtitle_info.video_id}.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add subtitle track: {e}", exc_info=True)
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
                    if row:
                        # Handle both old and new database schemas
                        if len(row) == 8:  # Old schema
                            return VideoInfo(
                                video_id=row[0], original_filename=row[1], total_duration=row[2],
                                total_segments=row[3], file_size=row[4], status=row[5],
                                created_at=row[6], updated_at=row[7]
                            )
                        else:  # New schema with additional fields
                            return VideoInfo(
                                video_id=row[0], original_filename=row[1], total_duration=row[2],
                                total_segments=row[3], file_size=row[4], status=row[5],
                                format_name=row[6] or 'unknown', video_codec=row[7] or 'unknown',
                                audio_codec=row[8] or 'unknown', resolution=row[9] or 'unknown',
                                bitrate=row[10] or 0, subtitle_count=row[11] or 0,
                                created_at=row[12], updated_at=row[13]
                            )
                    return None
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
                    SELECT filename, duration, file_id, file_size, segment_order, bot_id, created_at, updated_at
                    FROM segments WHERE video_id = ? ORDER BY segment_order
                """, (video_id,)) as cursor:
                    async for row in cursor:
                        # Handle both old and new schema
                        if len(row) == 7:  # Old schema without bot_id
                            segment = SegmentInfo(
                                filename=row[0], duration=row[1], file_id=row[2],
                                file_size=row[3], segment_order=row[4],
                                created_at=row[5], updated_at=row[6], bot_id=""
                            )
                        else:  # New schema with bot_id
                            segment = SegmentInfo(
                                filename=row[0], duration=row[1], file_id=row[2],
                                file_size=row[3], segment_order=row[4], bot_id=row[5],
                                created_at=row[6], updated_at=row[7]
                            )
                        segments[segment.filename] = segment
        except Exception as e:
            logger.error(f"❌ Failed to get segments for video {video_id}: {e}", exc_info=True)
        return segments

    async def get_video_subtitles(self, video_id: str) -> List[SubtitleInfo]:
        """
        Retrieves all subtitle tracks for a given video.

        Args:
            video_id (str): The ID of the video.

        Returns:
            List[SubtitleInfo]: A list of SubtitleInfo objects.
        """
        subtitles = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT video_id, track_index, language, title, codec, is_default,
                           is_forced, is_hearing_impaired, file_path, created_at
                    FROM subtitles WHERE video_id = ? ORDER BY track_index
                """, (video_id,)) as cursor:
                    async for row in cursor:
                        subtitle = SubtitleInfo(*row)
                        subtitles.append(subtitle)
        except Exception as e:
            logger.error(f"❌ Failed to get subtitles for video {video_id}: {e}", exc_info=True)
        return subtitles

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
                        # Handle both old and new database schemas
                        if len(row) == 8:  # Old schema
                            video = VideoInfo(
                                video_id=row[0], original_filename=row[1], total_duration=row[2],
                                total_segments=row[3], file_size=row[4], status=row[5],
                                created_at=row[6], updated_at=row[7]
                            )
                        else:  # New schema with additional fields
                            video = VideoInfo(
                                video_id=row[0], original_filename=row[1], total_duration=row[2],
                                total_segments=row[3], file_size=row[4], status=row[5],
                                format_name=row[6] or 'unknown', video_codec=row[7] or 'unknown',
                                audio_codec=row[8] or 'unknown', resolution=row[9] or 'unknown',
                                bitrate=row[10] or 0, subtitle_count=row[11] or 0,
                                created_at=row[12], updated_at=row[13]
                            )
                        videos.append(video)
        except Exception as e:
            logger.error(f"❌ Failed to get all videos: {e}", exc_info=True)
        return videos

    async def delete_video(self, video_id: str) -> bool:
        """
        Deletes a video and all its associated segments, subtitles, and subtitle files from the database.

        Args:
            video_id (str): The ID of the video to delete.

        Returns:
            bool: True if the video was deleted successfully, False otherwise.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Delete video (cascading will handle segments, subtitles, and subtitle files)
                await db.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
                await db.commit()
                logger.info(f"✅ Deleted video {video_id} from the database.")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to delete video {video_id}: {e}", exc_info=True)
            return False

    async def get_videos_with_subtitles(self) -> List[Dict]:
        """
        Retrieves all videos with their subtitle information.

        Returns:
            List[Dict]: List of dictionaries containing video and subtitle information.
        """
        videos_with_subs = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT v.*, COUNT(s.id) as sub_count, COUNT(sf.id) as sub_file_count
                    FROM videos v
                    LEFT JOIN subtitles s ON v.video_id = s.video_id
                    LEFT JOIN subtitle_files sf ON v.video_id = sf.video_id
                    WHERE v.status = 'active'
                    GROUP BY v.video_id
                    ORDER BY v.created_at DESC
                """) as cursor:
                    async for row in cursor:
                        # Handle both old and new schemas
                        if len(row) == 10:  # Old schema + subtitle count + file count
                            video_data = {
                                'video_id': row[0],
                                'original_filename': row[1],
                                'total_duration': row[2],
                                'total_segments': row[3],
                                'file_size': row[4],
                                'status': row[5],
                                'created_at': row[6],
                                'updated_at': row[7],
                                'subtitle_count_actual': row[8],
                                'subtitle_file_count': row[9],
                                'format_name': 'unknown',
                                'video_codec': 'unknown',
                                'audio_codec': 'unknown',
                                'resolution': 'unknown',
                                'bitrate': 0
                            }
                        else:  # New schema + subtitle count + file count
                            video_data = {
                                'video_id': row[0],
                                'original_filename': row[1],
                                'total_duration': row[2],
                                'total_segments': row[3],
                                'file_size': row[4],
                                'status': row[5],
                                'format_name': row[6] or 'unknown',
                                'video_codec': row[7] or 'unknown',
                                'audio_codec': row[8] or 'unknown',
                                'resolution': row[9] or 'unknown',
                                'bitrate': row[10] or 0,
                                'subtitle_count': row[11] or 0,
                                'created_at': row[12],
                                'updated_at': row[13],
                                'subtitle_count_actual': row[14],
                                'subtitle_file_count': row[15]
                            }
                        videos_with_subs.append(video_data)
        except Exception as e:
            logger.error(f"❌ Failed to get videos with subtitles: {e}", exc_info=True)
        return videos_with_subs

    async def search_subtitles(self, language: str = None, video_id: str = None) -> List[SubtitleInfo]:
        """
        Search for subtitle tracks by language or video ID.

        Args:
            language (str, optional): Language code to search for
            video_id (str, optional): Video ID to search within

        Returns:
            List[SubtitleInfo]: List of matching subtitle tracks
        """
        subtitles = []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                query = "SELECT * FROM subtitles WHERE 1=1"
                params = []

                if language:
                    query += " AND language = ?"
                    params.append(language)

                if video_id:
                    query += " AND video_id = ?"
                    params.append(video_id)

                query += " ORDER BY video_id, track_index"

                async with db.execute(query, params) as cursor:
                    async for row in cursor:
                        subtitle = SubtitleInfo(
                            video_id=row[1], track_index=row[2], language=row[3],
                            title=row[4], codec=row[5], is_default=bool(row[6]),
                            is_forced=bool(row[7]), is_hearing_impaired=bool(row[8]),
                            file_path=row[9], created_at=row[10]
                        )
                        subtitles.append(subtitle)
        except Exception as e:
            logger.error(f"❌ Failed to search subtitles: {e}", exc_info=True)
        return subtitles

    async def get_database_stats(self) -> Dict:
        """
        Get statistics about the database contents.

        Returns:
            Dict: Database statistics
        """
        stats = {
            'total_videos': 0,
            'total_segments': 0,
            'total_subtitles': 0,
            'total_subtitle_files': 0,
            'total_size_mb': 0,
            'total_duration_hours': 0,
            'languages': [],
            'codecs': {'video': [], 'audio': [], 'subtitle': []},
            'bot_distribution': {}
        }

        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Video stats
                async with db.execute("SELECT COUNT(*), SUM(file_size), SUM(total_duration) FROM videos WHERE status = 'active'") as cursor:
                    row = await cursor.fetchone()
                    if row:
                        stats['total_videos'] = row[0] or 0
                        stats['total_size_mb'] = (row[1] or 0) / (1024 * 1024)
                        stats['total_duration_hours'] = (row[2] or 0) / 3600

                # Segment stats
                async with db.execute("SELECT COUNT(*) FROM segments") as cursor:
                    row = await cursor.fetchone()
                    stats['total_segments'] = row[0] if row else 0

                # Subtitle stats
                async with db.execute("SELECT COUNT(*) FROM subtitles") as cursor:
                    row = await cursor.fetchone()
                    stats['total_subtitles'] = row[0] if row else 0

                # Subtitle file stats
                async with db.execute("SELECT COUNT(*) FROM subtitle_files") as cursor:
                    row = await cursor.fetchone()
                    stats['total_subtitle_files'] = row[0] if row else 0

                # Bot distribution for segments
                async with db.execute("""
                    SELECT bot_id, COUNT(*) as segment_count
                    FROM segments
                    WHERE bot_id != ''
                    GROUP BY bot_id
                    ORDER BY segment_count DESC
                """) as cursor:
                    async for row in cursor:
                        stats['bot_distribution'][row[0]] = row[1]

                # Languages from both subtitles and subtitle files
                async with db.execute("""
                    SELECT DISTINCT language FROM (
                        SELECT language FROM subtitles
                        UNION
                        SELECT language FROM subtitle_files
                    ) ORDER BY language
                """) as cursor:
                    async for row in cursor:
                        if row[0]:
                            stats['languages'].append(row[0])

                # Video codecs
                async with db.execute("SELECT DISTINCT video_codec FROM videos WHERE video_codec IS NOT NULL AND video_codec != 'unknown' ORDER BY video_codec") as cursor:
                    async for row in cursor:
                        stats['codecs']['video'].append(row[0])

                # Audio codecs
                async with db.execute("SELECT DISTINCT audio_codec FROM videos WHERE audio_codec IS NOT NULL AND audio_codec != 'unknown' ORDER BY audio_codec") as cursor:
                    async for row in cursor:
                        stats['codecs']['audio'].append(row[0])

                # Subtitle codecs
                async with db.execute("SELECT DISTINCT codec FROM subtitles ORDER BY codec") as cursor:
                    async for row in cursor:
                        stats['codecs']['subtitle'].append(row[0])

        except Exception as e:
            logger.error(f"❌ Failed to get database stats: {e}", exc_info=True)

        return stats

    async def get_statistics(self) -> Dict:
        """
        Alias for get_database_stats for backward compatibility.
        
        Returns:
            Dict: Database statistics
        """
        return await self.get_database_stats()

    async def cleanup(self):
        """Cleanup method for graceful shutdown."""
        # No specific cleanup needed for database manager
        logger.info("DatabaseManager cleanup completed")
