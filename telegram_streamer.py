import os
import json
import asyncio
import aiofiles
import sqlite3
import aiosqlite
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import subprocess
import math
from dataclasses import dataclass, asdict
from telegram import Bot
from telegram.error import TelegramError
import logging
import time
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
        self.db_path = db_path
        self.connection_pool = {}

    async def initialize_database(self):
        """Initialize the database with required tables."""
        async with aiosqlite.connect(self.db_path) as db:
            # Enable foreign key constraints
            await db.execute("PRAGMA foreign_keys = ON")

            # Create videos table
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

            # Create segments table
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

            # Create cache metadata table for tracking cached segments
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

            # Create indexes for better performance
            await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_video_id ON segments (video_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_order ON segments (video_id, segment_order)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON cache_metadata (last_accessed)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos (status)")

            await db.commit()
            logger.info("✅ Database initialized successfully")

    async def add_video(self, video_info: VideoInfo) -> bool:
        """Add a new video to the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO videos
                    (video_id, original_filename, total_duration, total_segments, file_size, status, created_at, updated_at)
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
                logger.info(f"✅ Added video {video_info.video_id} to database")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add video {video_info.video_id}: {e}")
            return False

    async def add_segment(self, video_id: str, segment_info: SegmentInfo) -> bool:
        """Add a segment to the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = datetime.now(timezone.utc).isoformat()
                await db.execute("""
                    INSERT OR REPLACE INTO segments
                    (video_id, filename, duration, file_id, file_size, segment_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_id,
                    segment_info.filename,
                    segment_info.duration,
                    segment_info.file_id,
                    segment_info.file_size,
                    segment_info.segment_order,
                    current_time,
                    current_time
                ))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"❌ Failed to add segment {segment_info.filename}: {e}")
            return False

    async def get_video_info(self, video_id: str) -> Optional[VideoInfo]:
        """Get video information by video_id."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT video_id, original_filename, total_duration, total_segments,
                           file_size, status, created_at, updated_at
                    FROM videos WHERE video_id = ?
                """, (video_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return VideoInfo(*row)
                    return None
        except Exception as e:
            logger.error(f"❌ Failed to get video info for {video_id}: {e}")
            return None

    async def get_video_segments(self, video_id: str) -> Dict[str, SegmentInfo]:
        """Get all segments for a video, ordered by segment_order."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT filename, duration, file_id, file_size, segment_order, created_at, updated_at
                    FROM segments
                    WHERE video_id = ?
                    ORDER BY segment_order
                """, (video_id,)) as cursor:
                    segments = {}
                    async for row in cursor:
                        filename, duration, file_id, file_size, segment_order, created_at, updated_at = row
                        segments[filename] = SegmentInfo(
                            filename=filename,
                            duration=duration,
                            file_id=file_id,
                            file_size=file_size,
                            segment_order=segment_order,
                            created_at=created_at,
                            updated_at=updated_at
                        )
                    return segments
        except Exception as e:
            logger.error(f"❌ Failed to get segments for video {video_id}: {e}")
            return {}

    async def get_all_videos(self) -> List[VideoInfo]:
        """Get all videos from the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT video_id, original_filename, total_duration, total_segments,
                           file_size, status, created_at, updated_at
                    FROM videos
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                """) as cursor:
                    videos = []
                    async for row in cursor:
                        videos.append(VideoInfo(*row))
                    return videos
        except Exception as e:
            logger.error(f"❌ Failed to get all videos: {e}")
            return []

    async def update_video_status(self, video_id: str, status: str) -> bool:
        """Update video status."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = datetime.now(timezone.utc).isoformat()
                await db.execute("""
                    UPDATE videos
                    SET status = ?, updated_at = ?
                    WHERE video_id = ?
                """, (status, current_time, video_id))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"❌ Failed to update video status for {video_id}: {e}")
            return False

    async def delete_video(self, video_id: str) -> bool:
        """Delete a video and all its segments from the database."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Delete segments first due to foreign key constraint
                await db.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
                await db.execute("DELETE FROM cache_metadata WHERE video_id = ?", (video_id,))
                await db.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
                await db.commit()
                logger.info(f"✅ Deleted video {video_id} from database")
                return True
        except Exception as e:
            logger.error(f"❌ Failed to delete video {video_id}: {e}")
            return False

    async def update_cache_metadata(self, segment_filename: str, video_id: str, cache_size: int):
        """Update cache metadata for a segment."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                current_time = datetime.now(timezone.utc).isoformat()
                await db.execute("""
                    INSERT OR REPLACE INTO cache_metadata
                    (segment_filename, video_id, cached_at, access_count, last_accessed, cache_size)
                    VALUES (?, ?, ?,
                        COALESCE((SELECT access_count + 1 FROM cache_metadata WHERE segment_filename = ?), 1),
                        ?, ?)
                """, (segment_filename, video_id, current_time, segment_filename, current_time, cache_size))
                await db.commit()
        except Exception as e:
            logger.error(f"❌ Failed to update cache metadata for {segment_filename}: {e}")

    async def get_cache_statistics(self) -> Dict:
        """Get cache statistics."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT
                        COUNT(*) as total_cached,
                        SUM(cache_size) as total_size,
                        AVG(access_count) as avg_access_count,
                        MAX(last_accessed) as last_cache_access
                    FROM cache_metadata
                """) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {
                            'total_cached_segments': row[0] or 0,
                            'total_cache_size_bytes': row[1] or 0,
                            'average_access_count': round(row[2] or 0, 2),
                            'last_cache_access': row[3]
                        }
                    return {}
        except Exception as e:
            logger.error(f"❌ Failed to get cache statistics: {e}")
            return {}

    async def cleanup_old_cache_entries(self, hours_old: int = 24) -> int:
        """Remove cache metadata entries older than specified hours."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cutoff_time = datetime.now(timezone.utc).replace(
                    hour=datetime.now().hour - hours_old
                ).isoformat()

                cursor = await db.execute("""
                    DELETE FROM cache_metadata
                    WHERE last_accessed < ?
                """, (cutoff_time,))
                deleted_count = cursor.rowcount
                await db.commit()

                if deleted_count > 0:
                    logger.info(f"🧹 Cleaned up {deleted_count} old cache entries")
                return deleted_count
        except Exception as e:
            logger.error(f"❌ Failed to cleanup old cache entries: {e}")
            return 0

    async def get_database_stats(self) -> Dict:
        """Get comprehensive database statistics."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                stats = {}

                # Video statistics
                async with db.execute("""
                    SELECT
                        COUNT(*) as total_videos,
                        SUM(total_segments) as total_segments,
                        SUM(file_size) as total_file_size,
                        SUM(total_duration) as total_duration
                    FROM videos WHERE status = 'active'
                """) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        stats['videos'] = {
                            'total_videos': row[0] or 0,
                            'total_segments': row[1] or 0,
                            'total_file_size_bytes': row[2] or 0,
                            'total_duration_seconds': row[3] or 0
                        }

                # Get cache stats
                cache_stats = await self.get_cache_statistics()
                stats['cache'] = cache_stats

                # Database file size
                if os.path.exists(self.db_path):
                    stats['database_size_bytes'] = os.path.getsize(self.db_path)

                return stats
        except Exception as e:
            logger.error(f"❌ Failed to get database stats: {e}")
            return {}

class TelegramVideoStreamer:
    """
    Manages splitting, uploading, and streaming video segments using Telegram as storage.
    Now with robust SQLite backend for metadata management.
    """
    def __init__(self, bot_token: str, chat_id: str, db_path: str = "video_streaming.db"):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.db = DatabaseManager(db_path)

        # Enhanced caching system
        self.segment_cache: Dict[str, bytes] = {}
        self.cache_timestamps: Dict[str, float] = {}
        self.prefetch_count = 3  # Increased for smoother playback
        self.cache_max_size = 100 * 1024 * 1024  # 100MB cache limit
        self.cache_ttl = 300  # 5 minutes TTL for cached segments

    async def initialize(self):
        """Initialize the database and perform startup tasks."""
        await self.db.initialize_database()

        # Clean up old cache metadata entries
        await self.db.cleanup_old_cache_entries(24)

        # Log startup statistics
        stats = await self.db.get_database_stats()
        if stats:
            logger.info(f"📊 Database loaded: {stats.get('videos', {}).get('total_videos', 0)} videos, "
                       f"{stats.get('videos', {}).get('total_segments', 0)} segments")

    def _validate_host_accessibility(self, host: str, port: int) -> bool:
        """Validate that the specified host:port combination is accessible."""
        import socket

        if host in ['localhost', '127.0.0.1']:
            logger.warning("⚠️  Using localhost - this will only work on the same machine!")
            logger.warning("   For Jellyfin/network access, use your network IP (e.g., 192.168.x.x)")
            return True

        try:
            # Try to bind to the specified host to check if it's valid
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, 0))  # Use port 0 to get any available port
                actual_ip = s.getsockname()[0]
                logger.info(f"✅ Host {host} is accessible (resolved to {actual_ip})")
                return True
        except socket.error as e:
            logger.error(f"❌ Cannot bind to host {host}: {e}")
            logger.error("   Make sure this is a valid IP address for your machine")
            return False

    def _cleanup_cache(self):
        """Remove expired and excess cache entries to maintain performance."""
        current_time = time.time()

        # Remove expired entries
        expired_keys = [
            key for key, timestamp in self.cache_timestamps.items()
            if current_time - timestamp > self.cache_ttl
        ]

        for key in expired_keys:
            self.segment_cache.pop(key, None)
            self.cache_timestamps.pop(key, None)
            logger.debug(f"Removed expired cache entry: {key}")

        # Remove oldest entries if cache is too large
        current_size = sum(len(data) for data in self.segment_cache.values())
        if current_size > self.cache_max_size:
            # Sort by timestamp, remove oldest first
            sorted_keys = sorted(
                self.cache_timestamps.items(),
                key=lambda x: x[1]
            )

            for key, _ in sorted_keys:
                if current_size <= self.cache_max_size * 0.8:  # Leave some headroom
                    break

                data = self.segment_cache.pop(key, b'')
                self.cache_timestamps.pop(key, None)
                current_size -= len(data)
                logger.debug(f"Removed cache entry due to size limit: {key}")

    async def split_video_to_hls(self, video_path: str, output_dir: str, max_chunk_size: int = 20 * 1024 * 1024) -> str:
        """Splits video into HLS segments using FFmpeg with enhanced error handling."""
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        os.makedirs(output_dir, exist_ok=True)

        # Probe video information
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]

        try:
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            video_info = json.loads(result.stdout)
            duration = float(video_info['format'].get('duration', 0))
            bitrate = int(video_info['format'].get('bit_rate', 0))

            if bitrate > 0:
                # Calculate optimal segment duration
                max_duration = (max_chunk_size * 8) / bitrate
                segment_duration = min(max_duration * 0.8, 50)  # 80% of max, cap at 50s
                segment_duration = max(10, segment_duration)  # Minimum 10 seconds
            else:
                segment_duration = 30

            logger.info(f"Video duration: {duration:.2f}s, bitrate: {bitrate}, segment duration: {segment_duration:.2f}s")

        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to probe video details: {e}. Using default settings.")
            segment_duration = 30
            duration = 0

        playlist_path = os.path.join(output_dir, 'playlist.m3u8')
        segment_pattern = os.path.join(output_dir, 'segment_%04d.ts')

        # Updated FFmpeg command to copy streams instead of re-encoding
        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path,
            '-c:v', 'copy',     # Copy the video stream without re-encoding
            '-c:a', 'copy',     # Copy the audio stream without re-encoding
            '-hls_time', str(segment_duration),
            '-hls_list_size', '0',
            '-hls_flags', 'independent_segments',
            '-hls_segment_filename', segment_pattern,
            '-f', 'hls',
            '-y',
            playlist_path
        ]

        try:
            logger.info("Starting video segmentation with FFmpeg...")
            result = subprocess.run(
                ffmpeg_cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour timeout
            )
            logger.info(f"Video successfully split into HLS segments in {output_dir}")
            return playlist_path
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg process timed out")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg failed with error: {e.stderr}")
            raise

    async def upload_segments_to_telegram(self, segments_dir: str, video_id: str, original_filename: str) -> Dict[str, SegmentInfo]:
        """Uploads all .ts segments to Telegram with progress tracking and database storage."""
        segment_info_map = {}
        ts_files = sorted([f for f in os.listdir(segments_dir) if f.endswith('.ts')])

        if not ts_files:
            raise ValueError(f"No .ts files found in {segments_dir}")

        logger.info(f"Found {len(ts_files)} segments to upload")

        # Create video record in database
        current_time = datetime.now(timezone.utc).isoformat()
        video_info = VideoInfo(
            video_id=video_id,
            original_filename=original_filename,
            total_duration=0.0,  # Will be updated after processing segments
            total_segments=len(ts_files),
            file_size=sum(os.path.getsize(os.path.join(segments_dir, f)) for f in ts_files),
            created_at=current_time,
            updated_at=current_time,
            status='processing'
        )

        await self.db.add_video(video_info)

        total_duration = 0
        successful_uploads = 0

        for i, ts_file in enumerate(ts_files, 1):
            file_path = os.path.join(segments_dir, ts_file)
            file_size = os.path.getsize(file_path)

            # Check Telegram file size limits
            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Segment {ts_file} exceeds Telegram's 50MB limit ({file_size} bytes). Skipping.")
                continue

            if file_size == 0:
                logger.warning(f"Segment {ts_file} is empty. Skipping.")
                continue

            try:
                logger.info(f"Uploading segment {i}/{len(ts_files)}: {ts_file} ({file_size:,} bytes)")

                with open(file_path, 'rb') as f:
                    message = await self.bot.send_document(
                        chat_id=self.chat_id,
                        document=f,
                        filename=ts_file,
                        caption=f"Video: {video_id} | Segment {i}/{len(ts_files)}",
                        read_timeout=60,
                        write_timeout=60,
                        connect_timeout=30
                    )

                duration = self.extract_segment_duration(segments_dir, ts_file)
                total_duration += duration

                segment_info = SegmentInfo(
                    filename=ts_file,
                    duration=duration,
                    file_id=message.document.file_id,
                    file_size=file_size,
                    segment_order=i-1  # 0-based ordering
                )

                # Store in database
                await self.db.add_segment(video_id, segment_info)
                segment_info_map[ts_file] = segment_info
                successful_uploads += 1

                logger.info(f"✅ Uploaded {ts_file} - File ID: {message.document.file_id}")

                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)

            except TelegramError as e:
                logger.error(f"Failed to upload {ts_file}: {e}")
                # Continue with other segments instead of failing entirely
                continue

        if not segment_info_map:
            # Update video status to error
            await self.db.update_video_status(video_id, 'error')
            raise RuntimeError("No segments were successfully uploaded")

        # Update video with final information
        video_info.total_duration = total_duration
        video_info.total_segments = successful_uploads
        video_info.status = 'active'
        video_info.updated_at = datetime.now(timezone.utc).isoformat()
        await self.db.add_video(video_info)

        logger.info(f"Successfully uploaded {successful_uploads}/{len(ts_files)} segments")
        logger.info(f"Total video duration: {total_duration:.2f} seconds")

        return segment_info_map

    def extract_segment_duration(self, segments_dir: str, segment_filename: str) -> float:
        """Extracts a segment's duration from the original FFmpeg-generated playlist."""
        playlist_path = os.path.join(segments_dir, 'playlist.m3u8')

        if not os.path.exists(playlist_path):
            logger.warning(f"Playlist not found at {playlist_path}")
            return 10.0

        try:
            with open(playlist_path, 'r') as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if segment_filename in line and i > 0:
                    prev_line = lines[i-1].strip()
                    if prev_line.startswith('#EXTINF:'):
                        duration_str = prev_line.split(':')[1].split(',')[0]
                        return float(duration_str)

        except (IOError, IndexError, ValueError) as e:
            logger.warning(f"Could not extract duration for {segment_filename}: {e}")

        return 10.0  # Fallback duration

    async def create_streaming_playlist(self, video_id: str, output_path: str, host: str, port: int):
        """Creates the .m3u8 playlist with network-accessible URLs using database data."""
        # Get segments from database
        segment_info = await self.db.get_video_segments(video_id)

        if not segment_info:
            raise ValueError(f"No segments found for video {video_id}")

        # Simple host warning for localhost
        if host in ['localhost', '127.0.0.1']:
            logger.warning("⚠️  Using localhost - this will only work on the same machine!")
            logger.warning("   For Jellyfin/network access, use your network IP (e.g., 192.168.x.x)")
        else:
            logger.info(f"✅ Creating playlist for network host: {host}")

        base_url = f"http://{host}:{port}"
        target_duration = math.ceil(max(s.duration for s in segment_info.values()))
        total_duration = sum(s.duration for s in segment_info.values())

        content = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-ALLOW-CACHE:YES"
        ]

        # Sort segments by segment_order
        sorted_segments = sorted(segment_info.items(), key=lambda x: x[1].segment_order)

        for name, segment in sorted_segments:
            content.append(f"#EXTINF:{segment.duration:.6f},")
            content.append(f"{base_url}/segment/{video_id}/{name}")

        content.append("#EXT-X-ENDLIST")

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        async with aiofiles.open(output_path, 'w') as f:
            await f.write('\n'.join(content))

        logger.info(f"Created streaming playlist at {output_path}")
        logger.info(f"Total video duration: {total_duration:.2f}s, {len(segment_info)} segments")
        logger.info(f"Playlist URL: {base_url}/playlist/{video_id}.m3u8")

        # Show a sample of the playlist for verification
        sample_lines = content[:10] + (["..."] if len(content) > 10 else [])
        logger.debug("Playlist preview:")
        for line in sample_lines:
            logger.debug(f"  {line}")

    async def download_segment_from_telegram(self, file_id: str) -> Optional[bytes]:
        """Downloads a segment from Telegram directly into memory with retry logic."""
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                file = await self.bot.get_file(file_id)
                content = await file.download_as_bytearray()
                return bytes(content)
            except TelegramError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Download attempt {attempt + 1} failed for {file_id}: {e}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"Failed to download segment {file_id} after {max_retries} attempts: {e}")

        return None

    async def _prefetch_segments(self, video_id: str, current_segment_name: str):
        """Asynchronously pre-fetches the next segments into the cache for smooth playback."""
        segments = await self.db.get_video_segments(video_id)
        if not segments:
            return

        # Sort segments by order
        sorted_segments = sorted(segments.items(), key=lambda x: x[1].segment_order)
        segment_names = [name for name, _ in sorted_segments]

        try:
            current_index = segment_names.index(current_segment_name)
        except ValueError:
            logger.warning(f"Current segment {current_segment_name} not found in segment list")
            return

        # Clean up cache before prefetching
        self._cleanup_cache()

        prefetch_tasks = []
        for i in range(1, self.prefetch_count + 1):
            next_index = current_index + i
            if next_index >= len(segment_names):
                break

            next_segment_name = segment_names[next_index]
            if next_segment_name not in self.segment_cache:
                logger.debug(f"🚀 Scheduling prefetch for segment: {next_segment_name}")
                prefetch_tasks.append(self._prefetch_single_segment(video_id, next_segment_name, segments[next_segment_name]))

        if prefetch_tasks:
            # Run prefetch tasks concurrently
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)

    async def _prefetch_single_segment(self, video_id: str, segment_name: str, segment_info: SegmentInfo):
        """Prefetch a single segment into cache."""
        try:
            downloaded_bytes = await self.download_segment_from_telegram(segment_info.file_id)

            if downloaded_bytes:
                self.segment_cache[segment_name] = downloaded_bytes
                self.cache_timestamps[segment_name] = time.time()

                # Update cache metadata in database
                await self.db.update_cache_metadata(segment_name, video_id, len(downloaded_bytes))

                logger.debug(f"✅ Prefetched segment: {segment_name} ({len(downloaded_bytes):,} bytes)")
            else:
                logger.warning(f"❌ Failed to prefetch segment: {segment_name}")

        except Exception as e:
            logger.error(f"Error prefetching segment {segment_name}: {e}")

    async def start_streaming_server(self, host: str, port: int):
        """Starts the HTTP streaming server with enhanced caching and CORS support."""
        from aiohttp import web
        from aiohttp.web import middleware

        @middleware
        async def cors_handler(request, handler):
            """Add CORS headers and handle OPTIONS requests for Jellyfin compatibility."""
            if request.method == 'OPTIONS':
                return web.Response(
                    headers={
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS, HEAD',
                        'Access-Control-Allow-Headers': 'Range, Content-Type, Authorization',
                        'Access-Control-Max-Age': '3600'
                    }
                )

            response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, HEAD'
            response.headers['Access-Control-Allow-Headers'] = 'Range, Content-Type, Authorization'
            response.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
            return response

        async def serve_segment(request: web.Request):
            video_id = request.match_info['video_id']
            segment_name = request.match_info['segment_name']

            # Get segment info from database
            segments = await self.db.get_video_segments(video_id)
            if not segments:
                logger.warning(f"Video {video_id} not found")
                return web.Response(status=404, text="Video not found")

            segment_info = segments.get(segment_name)
            if not segment_info:
                logger.warning(f"Segment {segment_name} not found for video {video_id}")
                return web.Response(status=404, text="Segment not found")

            # Start prefetching in background
            asyncio.create_task(self._prefetch_segments(video_id, segment_name))

            # Check cache first
            if segment_name in self.segment_cache:
                logger.info(f"✅ Cache HIT for segment: {segment_name}")
                segment_bytes = self.segment_cache[segment_name]
                # Update timestamp for LRU
                self.cache_timestamps[segment_name] = time.time()
                # Update cache metadata
                await self.db.update_cache_metadata(segment_name, video_id, len(segment_bytes))
            else:
                logger.warning(f"⚠️ Cache MISS for segment: {segment_name}. Downloading on-demand.")
                segment_bytes = await self.download_segment_from_telegram(segment_info.file_id)

            if not segment_bytes:
                return web.Response(status=500, text="Failed to retrieve segment")

            return web.Response(
                body=segment_bytes,
                content_type='video/mp2t',
                headers={
                    'Cache-Control': 'public, max-age=3600',
                    'Content-Length': str(len(segment_bytes)),
                    'Accept-Ranges': 'bytes',
                    'Connection': 'keep-alive'
                }
            )

        async def serve_playlist(request: web.Request):
            video_id = request.match_info['video_id']
            playlist_path = f"playlists/{video_id}.m3u8"

            if not os.path.exists(playlist_path):
                logger.warning(f"Playlist not found: {playlist_path}")
                return web.Response(status=404, text="Playlist not found")

            # Read and serve playlist content with proper headers for Jellyfin
            async with aiofiles.open(playlist_path, 'r') as f:
                content = await f.read()

            return web.Response(
                text=content,
                content_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache',
                    'Expires': '0',
                    'Accept-Ranges': 'bytes'
                }
            )

        async def debug_info(request: web.Request):
            """Enhanced debug endpoint with database statistics."""
            video_id = request.match_info.get('video_id', 'all')

            debug_data = {
                'server_info': {
                    'host': host,
                    'port': port,
                    'server_url': f"http://{host}:{port}"
                },
                'cache_stats': {
                    'cached_segments': len(self.segment_cache),
                    'cache_size_mb': sum(len(data) for data in self.segment_cache.values()) / (1024*1024)
                }
            }

            # Add database statistics
            db_stats = await self.db.get_database_stats()
            debug_data['database_stats'] = db_stats

            if video_id == 'all':
                debug_data['videos'] = {}
                videos = await self.db.get_all_videos()

                for video in videos:
                    segments = await self.db.get_video_segments(video.video_id)
                    debug_data['videos'][video.video_id] = {
                        'original_filename': video.original_filename,
                        'segment_count': len(segments),
                        'total_duration': video.total_duration,
                        'file_size': video.file_size,
                        'status': video.status,
                        'created_at': video.created_at,
                        'playlist_url': f"http://{host}:{port}/playlist/{video.video_id}.m3u8",
                        'first_segment_url': f"http://{host}:{port}/segment/{video.video_id}/{sorted(segments.keys(), key=lambda x: segments[x].segment_order)[0]}" if segments else None
                    }
            else:
                video_info = await self.db.get_video_info(video_id)
                if video_info:
                    segments = await self.db.get_video_segments(video_id)
                    debug_data['video_info'] = {
                        'video_id': video_id,
                        'original_filename': video_info.original_filename,
                        'segment_count': len(segments),
                        'total_duration': video_info.total_duration,
                        'file_size': video_info.file_size,
                        'status': video_info.status,
                        'created_at': video_info.created_at,
                        'segments': [
                            {
                                'name': info.filename,
                                'duration': info.duration,
                                'size': info.file_size,
                                'order': info.segment_order,
                                'url': f"http://{host}:{port}/segment/{video_id}/{info.filename}"
                            } for info in sorted(segments.values(), key=lambda x: x.segment_order)
                        ]
                    }
                else:
                    debug_data['error'] = f"Video {video_id} not found"

            return web.json_response(debug_data)

        async def list_videos(request: web.Request):
            """Endpoint to list all available videos."""
            videos = await self.db.get_all_videos()

            video_list = []
            for video in videos:
                segments = await self.db.get_video_segments(video.video_id)
                video_list.append({
                    'video_id': video.video_id,
                    'original_filename': video.original_filename,
                    'total_duration': video.total_duration,
                    'total_segments': len(segments),
                    'file_size': video.file_size,
                    'status': video.status,
                    'created_at': video.created_at,
                    'playlist_url': f"http://{host}:{port}/playlist/{video.video_id}.m3u8"
                })

            return web.json_response({
                'total_videos': len(video_list),
                'videos': video_list
            })

        async def delete_video_endpoint(request: web.Request):
            """Endpoint to delete a video and its segments."""
            video_id = request.match_info['video_id']

            # Check if video exists
            video_info = await self.db.get_video_info(video_id)
            if not video_info:
                return web.json_response({'error': f'Video {video_id} not found'}, status=404)

            # Delete from database
            success = await self.db.delete_video(video_id)

            if success:
                # Clean up playlist file
                playlist_path = f"playlists/{video_id}.m3u8"
                if os.path.exists(playlist_path):
                    os.remove(playlist_path)

                # Clean up cache entries for this video
                cache_keys_to_remove = [key for key in self.segment_cache.keys() if key.startswith(f"{video_id}_")]
                for key in cache_keys_to_remove:
                    self.segment_cache.pop(key, None)
                    self.cache_timestamps.pop(key, None)

                return web.json_response({'message': f'Video {video_id} deleted successfully'})
            else:
                return web.json_response({'error': f'Failed to delete video {video_id}'}, status=500)

        async def test_jellyfin_compatibility(request: web.Request):
            """Test endpoint specifically for Jellyfin compatibility."""
            return web.Response(
                text="""# Jellyfin HLS Test Playlist
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-ALLOW-CACHE:YES
#EXTINF:10.0,
http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4
#EXT-X-ENDLIST
""",
                content_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Access-Control-Allow-Origin': '*'
                }
            )

        # Create application with middleware
        app = web.Application(middlewares=[cors_handler])

        # Add routes
        app.router.add_get('/segment/{video_id}/{segment_name}', serve_segment)
        app.router.add_get('/playlist/{video_id}.m3u8', serve_playlist)
        app.router.add_get('/debug/{video_id}', debug_info)
        app.router.add_get('/debug', debug_info)
        app.router.add_get('/videos', list_videos)
        app.router.add_delete('/videos/{video_id}', delete_video_endpoint)
        app.router.add_get('/test-jellyfin.m3u8', test_jellyfin_compatibility)

        # Enhanced root endpoint
        async def root_handler(request):
            stats = await self.db.get_database_stats()
            videos_count = stats.get('videos', {}).get('total_videos', 0)
            total_segments = stats.get('videos', {}).get('total_segments', 0)
            cache_stats = stats.get('cache', {})

            return web.Response(
                text=f"""🎬 Telegram Video Streaming Server

📊 Server Statistics:
• Total Videos: {videos_count}
• Total Segments: {total_segments}
• Cached Segments: {cache_stats.get('total_cached_segments', 0)}
• Cache Size: {cache_stats.get('total_cache_size_bytes', 0) / (1024*1024):.1f} MB
• Database Size: {stats.get('database_size_bytes', 0) / (1024*1024):.1f} MB

🔗 Available Endpoints:
• GET  /                           - This page
• GET  /videos                     - List all videos (JSON)
• GET  /debug                      - Server debug info (JSON)
• GET  /debug/{{video_id}}           - Video-specific debug info (JSON)
• GET  /playlist/{{video_id}}.m3u8   - HLS playlist for video
• GET  /segment/{{video_id}}/{{name}}  - Individual video segment
• DEL  /videos/{{video_id}}          - Delete video and segments
• GET  /test-jellyfin.m3u8         - Jellyfin compatibility test

🎯 Usage Examples:
• Stream in VLC: http://{host}:{port}/playlist/{{video_id}}.m3u8
• Jellyfin .strm file content: http://{host}:{port}/playlist/{{video_id}}.m3u8
""", content_type='text/plain')

        app.router.add_get('/', root_handler)

        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"🚀 Streaming server started on http://{host}:{port}")
        logger.info(f"📊 Available endpoints:")
        logger.info(f"   • Server info: http://{host}:{port}")
        logger.info(f"   • Videos list: http://{host}:{port}/videos")
        logger.info(f"   • Debug info: http://{host}:{port}/debug")
        logger.info(f"   • Videos debug: http://{host}:{port}/debug/{{video_id}}")
        logger.info(f"   • Playlists: http://{host}:{port}/playlist/{{video_id}}.m3u8")
        logger.info(f"   • Jellyfin test: http://{host}:{port}/test-jellyfin.m3u8")

        return runner

# --- Main execution block ---
async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram Video Streaming App with SQLite Backend - Upload and stream videos using Telegram as storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Find your network IP first:
    python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print('Your IP:', s.getsockname()[0]); s.close()"

  Upload a video (use your actual network IP):
    python %(prog)s upload --video movie.mp4 --bot-token YOUR_TOKEN --chat-id @your_channel --host 192.168.1.100

  Start streaming server:
    python %(prog)s serve --bot-token YOUR_TOKEN --chat-id @your_channel --host 0.0.0.0

  Database management:
    python %(prog)s db-stats --bot-token YOUR_TOKEN --chat-id @your_channel
    python %(prog)s cleanup --bot-token YOUR_TOKEN --chat-id @your_channel
        """
    )

    subparsers = parser.add_subparsers(dest='command', required=True, help='Available commands')

    # Upload command
    upload_parser = subparsers.add_parser('upload', help='Split, upload a video, and create a streaming playlist')
    upload_parser.add_argument('--video', required=True, help='Path to the video file to upload')
    upload_parser.add_argument('--video-id', help='Custom video ID (defaults to filename without extension)')
    upload_parser.add_argument('--host', required=True,
                             help='Hostname/IP to embed in playlist URLs (e.g., 192.168.1.100 for network access) - REQUIRED for Jellyfin compatibility')

    # Serve command
    serve_parser = subparsers.add_parser('serve', help='Start the streaming server')
    serve_parser.add_argument('--host', default='0.0.0.0',
                            help='Host to bind server to (0.0.0.0 for network access, localhost for local only)')

    # Database stats command
    stats_parser = subparsers.add_parser('db-stats', help='Show database statistics')

    # Cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up old cache entries and optimize database')
    cleanup_parser.add_argument('--hours', type=int, default=24, help='Remove cache entries older than N hours (default: 24)')

    # List videos command
    list_parser = subparsers.add_parser('list', help='List all videos in database')

    # Delete video command
    delete_parser = subparsers.add_parser('delete', help='Delete a video and its segments')
    delete_parser.add_argument('--video-id', required=True, help='Video ID to delete')

    # Common arguments
    for p in [upload_parser, serve_parser, stats_parser, cleanup_parser, list_parser, delete_parser]:
        p.add_argument('--bot-token', required=True, help='Telegram bot token')
        p.add_argument('--chat-id', required=True, help='Telegram chat ID (e.g., @channel or numeric ID)')
        p.add_argument('--db-path', default='video_streaming.db', help='SQLite database file path (default: video_streaming.db)')

    # Additional arguments for specific commands
    for p in [upload_parser, serve_parser]:
        p.add_argument('--port', type=int, default=5050, help='Port for the streaming server (default: 5050)')

    upload_parser.add_argument('--max-chunk-size', type=int, default=20,
                      help='Maximum chunk size in MB (default: 20)')

    args = parser.parse_args()

    # Validate arguments
    if args.command == 'upload' and not os.path.exists(args.video):
        print(f"Error: Video file '{args.video}' not found")
        return 1

    try:
        streamer = TelegramVideoStreamer(args.bot_token, args.chat_id, args.db_path)
        await streamer.initialize()

        if args.command == 'upload':
            video_id = args.video_id or Path(args.video).stem
            segments_dir = os.path.join('segments', video_id)
            max_chunk_bytes = args.max_chunk_size * 1024 * 1024

            print(f"🎬 Processing video: {args.video}")
            print(f"📁 Video ID: {video_id}")
            print(f"📊 Max chunk size: {args.max_chunk_size}MB")
            print(f"🗄️ Database: {args.db_path}")
            print()

            print("1️⃣ Splitting video into HLS segments...")
            await streamer.split_video_to_hls(args.video, segments_dir, max_chunk_bytes)

            print("\n2️⃣ Uploading segments to Telegram and storing in database...")
            segment_info = await streamer.upload_segments_to_telegram(segments_dir, video_id, args.video)

            print(f"\n3️⃣ Creating streaming playlist for host '{args.host}'...")
            os.makedirs('playlists', exist_ok=True)
            playlist_path = f"playlists/{video_id}.m3u8"
            await streamer.create_streaming_playlist(video_id, playlist_path, args.host, args.port)

            print(f"\n✅ Upload completed successfully!")
            print(f"📋 Playlist created: {playlist_path}")
            print(f"🎯 Streaming URL: http://{args.host}:{args.port}/playlist/{video_id}.m3u8")
            print(f"🗄️ Video stored in database: {args.db_path}")
            print(f"\n💡 For Jellyfin:")
            print(f"   1. Create a file named '{video_id}.strm' in your Jellyfin media folder")
            print(f"   2. Put this URL inside the .strm file:")
            print(f"      http://{args.host}:{args.port}/playlist/{video_id}.m3u8")
            print(f"\n🚀 To start streaming, run:")
            print(f"   python {Path(__file__).name} serve --bot-token YOUR_TOKEN --chat-id YOUR_CHAT_ID --host {args.host}")

        elif args.command == 'serve':
            stats = await streamer.db.get_database_stats()
            videos_count = stats.get('videos', {}).get('total_videos', 0)

            if videos_count == 0:
                print("⚠️ No videos found in database. Upload some videos first.")
            else:
                print(f"📊 Found {videos_count} video(s) in database")

            runner = await streamer.start_streaming_server(args.host, args.port)

            print("\n⌨️ Press Ctrl+C to stop the server")
            try:
                await asyncio.Event().wait()  # Keep server running indefinitely
            except KeyboardInterrupt:
                print("\n🛑 Stopping server...")
            finally:
                await runner.cleanup()
                print("✅ Server stopped")

        elif args.command == 'db-stats':
            stats = await streamer.db.get_database_stats()

            print("🗄️ Database Statistics:")
            print(f"   Database file: {args.db_path}")
            if os.path.exists(args.db_path):
                print(f"   Database size: {os.path.getsize(args.db_path) / (1024*1024):.2f} MB")

            video_stats = stats.get('videos', {})
            print(f"\n📹 Videos:")
            print(f"   Total videos: {video_stats.get('total_videos', 0)}")
            print(f"   Total segments: {video_stats.get('total_segments', 0)}")
            print(f"   Total file size: {video_stats.get('total_file_size_bytes', 0) / (1024*1024*1024):.2f} GB")
            print(f"   Total duration: {video_stats.get('total_duration_seconds', 0) / 3600:.2f} hours")

            cache_stats = stats.get('cache', {})
            print(f"\n💾 Cache:")
            print(f"   Cached segments: {cache_stats.get('total_cached_segments', 0)}")
            print(f"   Cache size: {cache_stats.get('total_cache_size_bytes', 0) / (1024*1024):.2f} MB")
            print(f"   Average access count: {cache_stats.get('average_access_count', 0)}")

        elif args.command == 'cleanup':
            print(f"🧹 Cleaning up cache entries older than {args.hours} hours...")
            deleted_count = await streamer.db.cleanup_old_cache_entries(args.hours)
            print(f"✅ Cleaned up {deleted_count} old cache entries")

        elif args.command == 'list':
            videos = await streamer.db.get_all_videos()

            if not videos:
                print("📭 No videos found in database")
            else:
                print(f"📹 Found {len(videos)} video(s):\n")
                for video in videos:
                    segments = await streamer.db.get_video_segments(video.video_id)
                    print(f"🎬 {video.video_id}")
                    print(f"   Original: {video.original_filename}")
                    print(f"   Duration: {video.total_duration:.2f}s")
                    print(f"   Segments: {len(segments)}")
                    print(f"   Size: {video.file_size / (1024*1024):.2f} MB")
                    print(f"   Status: {video.status}")
                    print(f"   Created: {video.created_at}")
                    print()

        elif args.command == 'delete':
            video_id = args.video_id

            # Check if video exists
            video_info = await streamer.db.get_video_info(video_id)
            if not video_info:
                print(f"❌ Video '{video_id}' not found in database")
                return 1

            print(f"🗑️ Deleting video: {video_id}")
            print(f"   Original file: {video_info.original_filename}")
            print(f"   Segments: {video_info.total_segments}")

            confirm = input("Are you sure you want to delete this video? (yes/no): ")
            if confirm.lower() not in ['yes', 'y']:
                print("❌ Deletion cancelled")
                return 0

            success = await streamer.db.delete_video(video_id)

            if success:
                # Clean up playlist file
                playlist_path = f"playlists/{video_id}.m3u8"
                if os.path.exists(playlist_path):
                    os.remove(playlist_path)
                    print(f"🗑️ Removed playlist file: {playlist_path}")

                print(f"✅ Video '{video_id}' deleted successfully")
            else:
                print(f"❌ Failed to delete video '{video_id}'")
                return 1

    except KeyboardInterrupt:
        print("\n🛑 Operation cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"Application error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
