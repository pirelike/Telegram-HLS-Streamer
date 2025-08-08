"""
Database management for Telegram HLS Streamer.
Handles SQLite database operations for videos, segments, and metadata.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timezone
import json


class DatabaseManager:
    """Manages SQLite database operations."""
    
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)
        
        # Create database connection pool
        self._connection = None
        
    async def initialize(self):
        """Initialize database schema."""
        self._connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        
        await self._create_tables()
        self.logger.info("Database initialized successfully")
        
    async def close(self):
        """Close database connection."""
        if self._connection:
            self._connection.close()
            
    async def _execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a database query."""
        def _execute_sync():
            cursor = self._connection.cursor()
            cursor.execute(query, params)
            self._connection.commit()
            return cursor
            
        return await asyncio.get_event_loop().run_in_executor(None, _execute_sync)
        
    async def _fetchall(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Execute query and fetch all results."""
        def _fetch_sync():
            cursor = self._connection.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
            
        return await asyncio.get_event_loop().run_in_executor(None, _fetch_sync)
        
    async def _fetchone(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Execute query and fetch one result."""
        def _fetch_sync():
            cursor = self._connection.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()
            
        return await asyncio.get_event_loop().run_in_executor(None, _fetch_sync)
        
    async def _create_tables(self):
        """Create database tables."""
        
        # Videos table - stores main video information
        await self._execute("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                original_filename TEXT,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration REAL,
                file_size INTEGER,
                format_name TEXT,
                status TEXT DEFAULT 'processing',
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Video streams table - stores stream information
        await self._execute("""
            CREATE TABLE IF NOT EXISTS video_streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                stream_index INTEGER NOT NULL,
                stream_type TEXT NOT NULL,
                codec_name TEXT,
                language TEXT,
                title TEXT,
                width INTEGER,
                height INTEGER,
                duration REAL,
                bitrate INTEGER,
                sample_rate INTEGER,
                channels INTEGER,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
            )
        """)
        
        # Segments table - stores HLS segment information with bot isolation
        await self._execute("""
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                segment_name TEXT NOT NULL UNIQUE,
                segment_path TEXT NOT NULL,
                segment_type TEXT NOT NULL,
                quality TEXT,
                language TEXT,
                file_id TEXT NOT NULL,
                bot_index INTEGER NOT NULL,
                file_size INTEGER,
                duration REAL,
                sequence_number INTEGER,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
            )
        """)
        
        # Playlists table - stores HLS playlist information
        await self._execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                playlist_type TEXT NOT NULL,
                quality TEXT,
                language TEXT,
                playlist_content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
            )
        """)
        
        # Cache statistics table
        await self._execute("""
            CREATE TABLE IF NOT EXISTS cache_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_name TEXT NOT NULL,
                hit_count INTEGER DEFAULT 0,
                miss_count INTEGER DEFAULT 0,
                last_hit TIMESTAMP,
                last_miss TIMESTAMP,
                total_bytes_served INTEGER DEFAULT 0,
                avg_response_time REAL DEFAULT 0.0
            )
        """)
        
        # Processing jobs table
        await self._execute("""
            CREATE TABLE IF NOT EXISTS processing_jobs (
                job_id TEXT PRIMARY KEY,
                video_id TEXT,
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0.0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
            )
        """)
        
        # Create indexes for better performance
        await self._execute("CREATE INDEX IF NOT EXISTS idx_segments_video_id ON segments(video_id)")
        await self._execute("CREATE INDEX IF NOT EXISTS idx_segments_bot_index ON segments(bot_index)")
        await self._execute("CREATE INDEX IF NOT EXISTS idx_segments_segment_name ON segments(segment_name)")
        await self._execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
        await self._execute("CREATE INDEX IF NOT EXISTS idx_cache_stats_segment ON cache_stats(segment_name)")
        
    # Video operations
    async def create_video(self, video_id: str, title: str, duration: float = 0, 
                          file_size: int = 0, original_filename: str = None,
                          format_name: str = None, status: str = "processing") -> bool:
        """Create a new video record."""
        try:
            await self._execute("""
                INSERT INTO videos (video_id, title, original_filename, duration, 
                                  file_size, format_name, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (video_id, title, original_filename, duration, file_size, format_name, status))
            
            self.logger.info(f"Created video record: {video_id}")
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to create video {video_id}: {e}")
            return False
            
    async def get_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Get video information by ID."""
        row = await self._fetchone("""
            SELECT * FROM videos WHERE video_id = ?
        """, (video_id,))
        
        if row:
            return dict(row)
        return None
        
    async def get_all_videos(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all videos, optionally filtered by status."""
        if status:
            rows = await self._fetchall("""
                SELECT * FROM videos WHERE status = ? ORDER BY upload_date DESC
            """, (status,))
        else:
            rows = await self._fetchall("""
                SELECT * FROM videos ORDER BY upload_date DESC
            """)
            
        return [dict(row) for row in rows]
        
    async def update_video_status(self, video_id: str, status: str, 
                                 error_message: str = None) -> bool:
        """Update video processing status."""
        try:
            await self._execute("""
                UPDATE videos 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ?
            """, (status, video_id))
            
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to update video status {video_id}: {e}")
            return False
            
    async def delete_video(self, video_id: str) -> bool:
        """Delete a video and all its related data."""
        try:
            await self._execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            self.logger.info(f"Deleted video: {video_id}")
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to delete video {video_id}: {e}")
            return False
            
    # Stream operations
    async def store_video_streams(self, video_id: str, streams: List[Dict[str, Any]]) -> bool:
        """Store video stream metadata."""
        try:
            for stream in streams:
                await self._execute("""
                    INSERT INTO video_streams 
                    (video_id, stream_index, stream_type, codec_name, language, title,
                     width, height, duration, bitrate, sample_rate, channels)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    video_id,
                    stream.get('index', 0),
                    stream.get('codec_type', ''),
                    stream.get('codec_name', ''),
                    stream.get('language'),
                    stream.get('title'),
                    stream.get('width'),
                    stream.get('height'),
                    stream.get('duration'),
                    stream.get('bitrate'),
                    stream.get('sample_rate'),
                    stream.get('channels')
                ))
                
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to store streams for {video_id}: {e}")
            return False
            
    async def get_video_streams(self, video_id: str) -> List[Dict[str, Any]]:
        """Get all streams for a video."""
        rows = await self._fetchall("""
            SELECT * FROM video_streams WHERE video_id = ? ORDER BY stream_index
        """, (video_id,))
        
        return [dict(row) for row in rows]
        
    # Segment operations
    async def store_segment_metadata(self, segment_name: str, file_id: str, 
                                   bot_index: int, file_size: int, video_id: str = None,
                                   segment_type: str = "video", quality: str = None,
                                   language: str = None, duration: float = None,
                                   sequence_number: int = None) -> bool:
        """Store segment metadata with bot isolation info."""
        try:
            await self._execute("""
                INSERT OR REPLACE INTO segments 
                (video_id, segment_name, segment_path, segment_type, quality, language,
                 file_id, bot_index, file_size, duration, sequence_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id, segment_name, segment_name, segment_type, quality, language,
                file_id, bot_index, file_size, duration, sequence_number
            ))
            
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to store segment {segment_name}: {e}")
            return False
            
    async def get_segment_metadata(self, segment_name: str) -> Optional[Dict[str, Any]]:
        """Get segment metadata including bot isolation info."""
        row = await self._fetchone("""
            SELECT * FROM segments WHERE segment_name = ?
        """, (segment_name,))
        
        if row:
            # Update access statistics
            await self._execute("""
                UPDATE segments 
                SET last_accessed = CURRENT_TIMESTAMP, access_count = access_count + 1
                WHERE segment_name = ?
            """, (segment_name,))
            
            return dict(row)
        return None
        
    async def get_segments_by_video(self, video_id: str) -> List[Dict[str, Any]]:
        """Get all segments for a video."""
        rows = await self._fetchall("""
            SELECT * FROM segments WHERE video_id = ? ORDER BY sequence_number
        """, (video_id,))
        
        return [dict(row) for row in rows]
        
    async def get_segments_by_bot(self, bot_index: int) -> List[Dict[str, Any]]:
        """Get all segments handled by a specific bot."""
        rows = await self._fetchall("""
            SELECT * FROM segments WHERE bot_index = ?
        """, (bot_index,))
        
        return [dict(row) for row in rows]
        
    # Playlist operations
    async def store_playlist(self, video_id: str, playlist_type: str, 
                           playlist_content: str, quality: str = None,
                           language: str = None) -> bool:
        """Store HLS playlist content."""
        try:
            await self._execute("""
                INSERT OR REPLACE INTO playlists 
                (video_id, playlist_type, quality, language, playlist_content)
                VALUES (?, ?, ?, ?, ?)
            """, (video_id, playlist_type, quality, language, playlist_content))
            
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to store playlist for {video_id}: {e}")
            return False
            
    async def get_playlist(self, video_id: str, playlist_type: str = "master",
                          quality: str = None, language: str = None) -> Optional[str]:
        """Get playlist content."""
        if playlist_type == "master":
            row = await self._fetchone("""
                SELECT playlist_content FROM playlists 
                WHERE video_id = ? AND playlist_type = 'master'
            """, (video_id,))
        else:
            row = await self._fetchone("""
                SELECT playlist_content FROM playlists 
                WHERE video_id = ? AND playlist_type = ? AND quality = ? AND language = ?
            """, (video_id, playlist_type, quality, language))
            
        return row['playlist_content'] if row else None
        
    # Cache statistics
    async def record_cache_hit(self, segment_name: str, response_time: float = 0.0,
                              bytes_served: int = 0):
        """Record a cache hit."""
        await self._execute("""
            INSERT OR REPLACE INTO cache_stats 
            (segment_name, hit_count, miss_count, last_hit, total_bytes_served, avg_response_time)
            VALUES (
                ?, 
                COALESCE((SELECT hit_count FROM cache_stats WHERE segment_name = ?), 0) + 1,
                COALESCE((SELECT miss_count FROM cache_stats WHERE segment_name = ?), 0),
                CURRENT_TIMESTAMP,
                COALESCE((SELECT total_bytes_served FROM cache_stats WHERE segment_name = ?), 0) + ?,
                ?
            )
        """, (segment_name, segment_name, segment_name, segment_name, bytes_served, response_time))
        
    async def record_cache_miss(self, segment_name: str):
        """Record a cache miss."""
        await self._execute("""
            INSERT OR REPLACE INTO cache_stats 
            (segment_name, hit_count, miss_count, last_miss)
            VALUES (
                ?, 
                COALESCE((SELECT hit_count FROM cache_stats WHERE segment_name = ?), 0),
                COALESCE((SELECT miss_count FROM cache_stats WHERE segment_name = ?), 0) + 1,
                CURRENT_TIMESTAMP
            )
        """, (segment_name, segment_name, segment_name))
        
    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache performance statistics."""
        rows = await self._fetchall("""
            SELECT 
                COUNT(*) as total_segments,
                SUM(hit_count) as total_hits,
                SUM(miss_count) as total_misses,
                SUM(total_bytes_served) as total_bytes_served,
                AVG(avg_response_time) as avg_response_time
            FROM cache_stats
        """)
        
        if rows and rows[0]['total_segments']:
            stats = dict(rows[0])
            stats['hit_ratio'] = stats['total_hits'] / (stats['total_hits'] + stats['total_misses']) if (stats['total_hits'] + stats['total_misses']) > 0 else 0
            return stats
        else:
            return {
                'total_segments': 0,
                'total_hits': 0,
                'total_misses': 0,
                'total_bytes_served': 0,
                'avg_response_time': 0.0,
                'hit_ratio': 0.0
            }
            
    # Processing jobs
    async def create_processing_job(self, job_id: str, video_id: str = None) -> bool:
        """Create a processing job record."""
        try:
            await self._execute("""
                INSERT INTO processing_jobs (job_id, video_id, status, started_at)
                VALUES (?, ?, 'processing', CURRENT_TIMESTAMP)
            """, (job_id, video_id))
            
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to create processing job {job_id}: {e}")
            return False
            
    async def update_job_progress(self, job_id: str, progress: float, 
                                 status: str = None) -> bool:
        """Update processing job progress."""
        try:
            if status:
                await self._execute("""
                    UPDATE processing_jobs 
                    SET progress = ?, status = ?, 
                        completed_at = CASE WHEN ? IN ('completed', 'error') THEN CURRENT_TIMESTAMP ELSE completed_at END
                    WHERE job_id = ?
                """, (progress, status, status, job_id))
            else:
                await self._execute("""
                    UPDATE processing_jobs SET progress = ? WHERE job_id = ?
                """, (progress, job_id))
                
            return True
            
        except sqlite3.Error as e:
            self.logger.error(f"Failed to update job progress {job_id}: {e}")
            return False
            
    async def get_processing_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get processing job status."""
        row = await self._fetchone("""
            SELECT * FROM processing_jobs WHERE job_id = ?
        """, (job_id,))
        
        return dict(row) if row else None
        
    # Database maintenance
    async def cleanup_old_cache_stats(self, days_old: int = 30):
        """Clean up old cache statistics."""
        await self._execute("""
            DELETE FROM cache_stats 
            WHERE last_hit < datetime('now', '-{} days') 
              AND last_miss < datetime('now', '-{} days')
        """.format(days_old, days_old))
        
    async def get_database_stats(self) -> Dict[str, Any]:
        """Get general database statistics."""
        stats = {}
        
        # Video counts
        video_rows = await self._fetchall("""
            SELECT status, COUNT(*) as count FROM videos GROUP BY status
        """)
        stats['videos'] = {row['status']: row['count'] for row in video_rows}
        
        # Segment counts
        segment_row = await self._fetchone("SELECT COUNT(*) as count FROM segments")
        stats['total_segments'] = segment_row['count'] if segment_row else 0
        
        # Bot distribution
        bot_rows = await self._fetchall("""
            SELECT bot_index, COUNT(*) as count, SUM(file_size) as total_size 
            FROM segments GROUP BY bot_index
        """)
        stats['bot_distribution'] = [
            {
                'bot_index': row['bot_index'], 
                'segment_count': row['count'],
                'total_size_mb': (row['total_size'] or 0) / (1024 * 1024)
            }
            for row in bot_rows
        ]
        
        return stats