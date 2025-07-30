import os
import asyncio
import aiofiles
import aiosqlite
import time
from typing import Optional, Dict, List, Set
from datetime import datetime, timezone
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from src.storage.database import DatabaseManager
from src.utils.logging import logger
from src.utils.networking import calculate_bytes_hash

@dataclass
class ViewingSession:
    """Tracks an individual viewing session for predictive caching."""
    session_id: str
    video_id: str
    current_segment: int = 0
    segments_requested: Set[int] = field(default_factory=set)
    last_request_time: float = field(default_factory=time.time)
    playback_speed: float = 1.0
    user_agent: str = ""
    client_ip: str = ""

    def update_position(self, segment_index: int):
        """Update current position and estimate playback speed."""
        if segment_index > self.current_segment:
            time_diff = time.time() - self.last_request_time
            segment_diff = segment_index - self.current_segment

            if time_diff > 0:
                # Estimate playback speed based on segment progression
                estimated_speed = segment_diff / max(time_diff, 1.0)
                # Smooth the estimate
                self.playback_speed = (self.playback_speed * 0.7) + (estimated_speed * 0.3)

        self.current_segment = segment_index
        self.segments_requested.add(segment_index)
        self.last_request_time = time.time()

class MemoryCacheManager:
    """
    Manages an in-memory LRU cache for video segments.
    Faster than disk cache but data is lost on restart.
    """

    def __init__(self, db_manager: DatabaseManager, max_cache_size: int = 500 * 1024 * 1024):
        self.db = db_manager
        self.max_cache_size = max_cache_size
        self.current_cache_size = 0
        self.cache_lock = asyncio.Lock()

        # In-memory cache storage using OrderedDict for LRU behavior
        self.cache_data: OrderedDict[str, bytes] = OrderedDict()
        self.cache_metadata: Dict[str, Dict] = {}

        logger.info(f"MemoryCacheManager initialized with {max_cache_size / (1024*1024):.1f}MB limit")

    async def initialize(self):
        """Initialize the memory cache."""
        async with self.cache_lock:
            self.cache_data.clear()
            self.cache_metadata.clear()
            self.current_cache_size = 0
            logger.info("Memory cache initialized and cleared")

    async def get_cached_segment(self, video_id: str, segment_filename: str) -> Optional[bytes]:
        """
        Retrieve a cached segment if it exists and update access time.

        Args:
            video_id (str): The video ID
            segment_filename (str): The segment filename

        Returns:
            Optional[bytes]: The cached segment data, or None if not cached
        """
        cache_key = f"{video_id}_{segment_filename}"

        async with self.cache_lock:
            if cache_key in self.cache_data:
                # Move to end (most recently used)
                data = self.cache_data.pop(cache_key)
                self.cache_data[cache_key] = data

                # Update metadata
                if cache_key in self.cache_metadata:
                    self.cache_metadata[cache_key]['access_count'] += 1
                    self.cache_metadata[cache_key]['last_accessed'] = datetime.now(timezone.utc).isoformat()

                logger.debug(f"Memory cache HIT: {cache_key}")
                return data

        return None

    async def cache_segment(self, video_id: str, segment_filename: str, data: bytes) -> bool:
        """
        Cache a segment with LRU eviction if needed.

        Args:
            video_id (str): The video ID
            segment_filename (str): The segment filename
            data (bytes): The segment data to cache

        Returns:
            bool: True if successfully cached, False otherwise
        """
        if len(data) > self.max_cache_size:
            logger.warning(f"Segment {segment_filename} too large to cache ({len(data)} bytes)")
            return False

        cache_key = f"{video_id}_{segment_filename}"

        async with self.cache_lock:
            # Make space if needed
            await self._ensure_cache_space(len(data))

            # Add to cache
            self.cache_data[cache_key] = data
            self.current_cache_size += len(data)

            # Update metadata
            current_time = datetime.now(timezone.utc).isoformat()
            self.cache_metadata[cache_key] = {
                'video_id': video_id,
                'cached_at': current_time,
                'access_count': 1,
                'last_accessed': current_time,
                'cache_size': len(data)
            }

            logger.debug(f"Memory cache STORE: {cache_key} ({len(data)} bytes)")
            return True

    async def _ensure_cache_space(self, needed_space: int):
        """Ensure there's enough cache space by evicting LRU items."""
        while self.current_cache_size + needed_space > self.max_cache_size:
            if not self.cache_data:
                break  # No more items to evict

            # Remove least recently used item (first item in OrderedDict)
            cache_key, data = self.cache_data.popitem(last=False)
            data_size = len(data)
            self.current_cache_size -= data_size

            # Remove metadata
            if cache_key in self.cache_metadata:
                del self.cache_metadata[cache_key]

            logger.debug(f"Memory cache EVICT: {cache_key} ({data_size} bytes)")

    async def get_cache_stats(self) -> Dict:
        """Get current cache statistics."""
        async with self.cache_lock:
            total_items = len(self.cache_data)
            total_accesses = sum(meta.get('access_count', 0) for meta in self.cache_metadata.values())

            return {
                'total_items': total_items,
                'total_size': self.current_cache_size,
                'total_accesses': total_accesses,
                'cache_utilization': (self.current_cache_size / self.max_cache_size * 100) if self.max_cache_size > 0 else 0,
                'max_cache_size': self.max_cache_size,
                'cache_type': 'memory'
            }

    async def clear_cache(self, video_id: str = None):
        """Clear cache for a specific video or all cache."""
        async with self.cache_lock:
            if video_id:
                # Clear cache for specific video
                keys_to_remove = [
                    key for key, meta in self.cache_metadata.items()
                    if meta.get('video_id') == video_id
                ]

                for cache_key in keys_to_remove:
                    if cache_key in self.cache_data:
                        data_size = len(self.cache_data[cache_key])
                        del self.cache_data[cache_key]
                        self.current_cache_size -= data_size

                    if cache_key in self.cache_metadata:
                        del self.cache_metadata[cache_key]

                logger.info(f"Cleared memory cache for video {video_id} ({len(keys_to_remove)} segments)")
            else:
                # Clear all cache
                self.cache_data.clear()
                self.cache_metadata.clear()
                self.current_cache_size = 0
                logger.info("Cleared all memory cache")

    async def cleanup(self):
        """Cleanup memory cache resources."""
        async with self.cache_lock:
            self.cache_data.clear()
            self.cache_metadata.clear()
            self.current_cache_size = 0
            logger.info("MemoryCacheManager cleanup completed")


class DiskCacheManager:
    """
    Disk-based cache manager for persistent caching.
    """

    def __init__(self, db_manager: DatabaseManager, cache_dir: str = "cache",
                 max_cache_size: int = 500 * 1024 * 1024):
        self.db = db_manager
        self.cache_dir = cache_dir
        self.max_cache_size = max_cache_size
        self.current_cache_size = 0
        self.cache_lock = asyncio.Lock()

        # Ensure cache directory exists
        os.makedirs(cache_dir, exist_ok=True)
        logger.info(f"DiskCacheManager initialized with {max_cache_size / (1024*1024):.1f}MB limit")

    async def initialize(self):
        """Initialize the cache by calculating current size and cleaning up orphaned files."""
        async with self.cache_lock:
            await self._calculate_current_cache_size()
            await self._cleanup_orphaned_files()
            logger.info(f"Disk cache initialized: {self.current_cache_size / (1024*1024):.1f}MB used")

    async def get_cached_segment(self, video_id: str, segment_filename: str) -> Optional[bytes]:
        """Retrieve a cached segment if it exists and update access time."""
        cache_key = f"{video_id}_{segment_filename}"
        cache_path = os.path.join(self.cache_dir, cache_key)

        if not os.path.exists(cache_path):
            return None

        try:
            async with aiofiles.open(cache_path, 'rb') as f:
                data = await f.read()

            # Update access time in database
            await self._update_cache_access(cache_key, video_id)
            logger.debug(f"Disk cache HIT: {cache_key}")
            return data

        except Exception as e:
            logger.error(f"Error reading cached segment {cache_key}: {e}")
            try:
                os.remove(cache_path)
            except:
                pass
            return None

    async def cache_segment(self, video_id: str, segment_filename: str, data: bytes) -> bool:
        """Cache a segment with LRU eviction if needed."""
        if len(data) > self.max_cache_size:
            logger.warning(f"Segment {segment_filename} too large to cache ({len(data)} bytes)")
            return False

        cache_key = f"{video_id}_{segment_filename}"
        cache_path = os.path.join(self.cache_dir, cache_key)

        async with self.cache_lock:
            # Make space if needed
            await self._ensure_cache_space(len(data))

            try:
                # Write to cache
                async with aiofiles.open(cache_path, 'wb') as f:
                    await f.write(data)

                # Update database
                current_time = datetime.now(timezone.utc).isoformat()
                async with aiosqlite.connect(self.db.db_path) as db:
                    await db.execute("""
                        INSERT OR REPLACE INTO cache_metadata
                        (segment_filename, video_id, cached_at, access_count, last_accessed, cache_size)
                        VALUES (?, ?, ?, 1, ?, ?)
                    """, (cache_key, video_id, current_time, current_time, len(data)))
                    await db.commit()

                self.current_cache_size += len(data)
                logger.debug(f"Disk cache STORE: {cache_key} ({len(data)} bytes)")
                return True

            except Exception as e:
                logger.error(f"Error caching segment {cache_key}: {e}")
                try:
                    if os.path.exists(cache_path):
                        os.remove(cache_path)
                except:
                    pass
                return False

    async def _ensure_cache_space(self, needed_space: int):
        """Ensure there's enough cache space by evicting LRU items."""
        while self.current_cache_size + needed_space > self.max_cache_size:
            evicted = await self._evict_lru_item()
            if not evicted:
                break

    async def _evict_lru_item(self) -> bool:
        """Evict the least recently used cache item."""
        try:
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute("""
                    SELECT segment_filename, cache_size
                    FROM cache_metadata
                    ORDER BY last_accessed ASC
                    LIMIT 1
                """) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    return False

                cache_key, cache_size = row
                cache_path = os.path.join(self.cache_dir, cache_key)

                # Remove file
                if os.path.exists(cache_path):
                    os.remove(cache_path)

                # Remove from database
                await db.execute("DELETE FROM cache_metadata WHERE segment_filename = ?", (cache_key,))
                await db.commit()

                self.current_cache_size -= cache_size
                logger.debug(f"Disk cache EVICT: {cache_key} ({cache_size} bytes)")
                return True

        except Exception as e:
            logger.error(f"Error evicting cache item: {e}")
            return False

    async def _update_cache_access(self, cache_key: str, video_id: str):
        """Update the last accessed time for a cached item."""
        try:
            current_time = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(self.db.db_path) as db:
                await db.execute("""
                    UPDATE cache_metadata
                    SET last_accessed = ?, access_count = access_count + 1
                    WHERE segment_filename = ?
                """, (current_time, cache_key))
                await db.commit()
        except Exception as e:
            logger.error(f"Error updating cache access for {cache_key}: {e}")

    async def _calculate_current_cache_size(self):
        """Calculate the current cache size from existing files."""
        self.current_cache_size = 0
        try:
            for filename in os.listdir(self.cache_dir):
                file_path = os.path.join(self.cache_dir, filename)
                if os.path.isfile(file_path):
                    self.current_cache_size += os.path.getsize(file_path)
        except Exception as e:
            logger.error(f"Error calculating cache size: {e}")

    async def _cleanup_orphaned_files(self):
        """Remove cached files that don't have database entries."""
        try:
            # Get all cache keys from database
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute("SELECT segment_filename FROM cache_metadata") as cursor:
                    db_cache_keys = set(row[0] for row in await cursor.fetchall())

            # Check filesystem cache files
            fs_cache_keys = set()
            for filename in os.listdir(self.cache_dir):
                file_path = os.path.join(self.cache_dir, filename)
                if os.path.isfile(file_path):
                    fs_cache_keys.add(filename)

            # Remove orphaned files
            orphaned = fs_cache_keys - db_cache_keys
            for cache_key in orphaned:
                file_path = os.path.join(self.cache_dir, cache_key)
                try:
                    os.remove(file_path)
                    logger.debug(f"Removed orphaned cache file: {cache_key}")
                except Exception as e:
                    logger.error(f"Error removing orphaned file {cache_key}: {e}")

            # Remove database entries without files
            db_only = db_cache_keys - fs_cache_keys
            if db_only:
                async with aiosqlite.connect(self.db.db_path) as db:
                    for cache_key in db_only:
                        await db.execute("DELETE FROM cache_metadata WHERE segment_filename = ?", (cache_key,))
                    await db.commit()
                    logger.debug(f"Removed {len(db_only)} orphaned database entries")

        except Exception as e:
            logger.error(f"Error during cache cleanup: {e}")

    async def get_cache_stats(self) -> Dict:
        """Get current cache statistics."""
        try:
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute("""
                    SELECT COUNT(*), SUM(cache_size), SUM(access_count)
                    FROM cache_metadata
                """) as cursor:
                    row = await cursor.fetchone()

                if row and row[0]:
                    total_items, total_size, total_accesses = row
                    return {
                        'total_items': total_items,
                        'total_size': total_size or 0,
                        'total_accesses': total_accesses or 0,
                        'cache_utilization': (total_size or 0) / self.max_cache_size * 100,
                        'max_cache_size': self.max_cache_size,
                        'cache_type': 'disk'
                    }
                else:
                    return {
                        'total_items': 0,
                        'total_size': 0,
                        'total_accesses': 0,
                        'cache_utilization': 0,
                        'max_cache_size': self.max_cache_size,
                        'cache_type': 'disk'
                    }
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {
                'total_items': 0,
                'total_size': 0,
                'total_accesses': 0,
                'cache_utilization': 0,
                'max_cache_size': self.max_cache_size,
                'cache_type': 'disk',
                'error': str(e)
            }

    async def clear_cache(self, video_id: str = None):
        """Clear cache for a specific video or all cache."""
        async with self.cache_lock:
            try:
                if video_id:
                    # Clear cache for specific video
                    async with aiosqlite.connect(self.db.db_path) as db:
                        async with db.execute("""
                            SELECT segment_filename FROM cache_metadata WHERE video_id = ?
                        """, (video_id,)) as cursor:
                            cache_keys = [row[0] for row in await cursor.fetchall()]

                    for cache_key in cache_keys:
                        cache_path = os.path.join(self.cache_dir, cache_key)
                        if os.path.exists(cache_path):
                            os.remove(cache_path)

                    async with aiosqlite.connect(self.db.db_path) as db:
                        await db.execute("DELETE FROM cache_metadata WHERE video_id = ?", (video_id,))
                        await db.commit()

                    logger.info(f"Cleared disk cache for video {video_id}")
                else:
                    # Clear all cache
                    for filename in os.listdir(self.cache_dir):
                        file_path = os.path.join(self.cache_dir, filename)
                        if os.path.isfile(file_path):
                            os.remove(file_path)

                    async with aiosqlite.connect(self.db.db_path) as db:
                        await db.execute("DELETE FROM cache_metadata")
                        await db.commit()

                    logger.info("Cleared all disk cache")

                await self._calculate_current_cache_size()

            except Exception as e:
                logger.error(f"Error clearing cache: {e}")


class PredictiveCacheManager:
    """
    Enhanced cache manager with predictive loading capabilities.
    Supports both memory and disk caching with intelligent preloading.
    """

    def __init__(self, db_manager: DatabaseManager, telegram_handler,
                 cache_type: str = "memory", cache_dir: str = "cache",
                 max_cache_size: int = 500 * 1024 * 1024,
                 preload_segments: int = 8, max_concurrent_preloads: int = 5):
        self.db = db_manager
        self.telegram_handler = telegram_handler
        self.cache_type = cache_type.lower()
        self.cache_dir = cache_dir
        self.max_cache_size = max_cache_size
        self.preload_segments = preload_segments
        self.max_concurrent_preloads = max_concurrent_preloads

        # Initialize base cache manager
        if self.cache_type == "memory":
            self.base_cache = MemoryCacheManager(db_manager, max_cache_size)
        else:
            self.base_cache = DiskCacheManager(db_manager, cache_dir, max_cache_size)

        # Predictive caching state
        self.active_sessions: Dict[str, ViewingSession] = {}
        self.preload_tasks: Dict[str, asyncio.Task] = {}
        self.preload_semaphore = asyncio.Semaphore(max_concurrent_preloads)

        # Statistics
        self.stats = {
            'cache_hits': 0,
            'cache_misses': 0,
            'preload_requests': 0,
            'successful_preloads': 0,
            'session_cleanups': 0
        }

        logger.info(f"PredictiveCacheManager initialized: {cache_type.upper()}, "
                    f"{max_cache_size / (1024*1024):.1f}MB, "
                    f"{preload_segments} segments ahead, "
                    f"{max_concurrent_preloads} concurrent preloads")

    async def initialize(self):
        """Initialize the cache manager."""
        await self.base_cache.initialize()
        logger.info("Predictive cache manager initialized")

    async def handle_segment_request(self, video_id: str, segment_filename: str, session_id: str) -> Optional[bytes]:
        """
        Handle a segment request with predictive caching logic.

        Args:
            video_id: The video ID
            segment_filename: The segment filename
            session_id: Unique session identifier

        Returns:
            Segment data if available, None otherwise
        """
        # Extract segment index from filename
        segment_index = self._extract_segment_index(segment_filename)

        # Update or create viewing session
        await self._update_viewing_session(session_id, video_id, segment_index)

        # Try to get from cache first
        segment_data = await self.base_cache.get_cached_segment(video_id, segment_filename)

        if segment_data:
            self.stats['cache_hits'] += 1
            logger.debug(f"Cache HIT: {video_id}/{segment_filename} for session {session_id}")

            # Trigger predictive preloading in background
            asyncio.create_task(self._predictive_preload(session_id, video_id, segment_index))

            return segment_data
        else:
            self.stats['cache_misses'] += 1
            logger.debug(f"Cache MISS: {video_id}/{segment_filename} for session {session_id}")

            # Download from Telegram
            segment_data = await self._download_segment(video_id, segment_filename)

            if segment_data:
                # Cache the segment
                await self.base_cache.cache_segment(video_id, segment_filename, segment_data)

                # Trigger predictive preloading
                asyncio.create_task(self._predictive_preload(session_id, video_id, segment_index))

                return segment_data

        return None

    async def _update_viewing_session(self, session_id: str, video_id: str, segment_index: int):
        """Update or create a viewing session."""
        if session_id not in self.active_sessions:
            self.active_sessions[session_id] = ViewingSession(
                session_id=session_id,
                video_id=video_id,
                current_segment=segment_index
            )
            logger.debug(f"Created new viewing session: {session_id} for video {video_id}")
        else:
            session = self.active_sessions[session_id]
            session.update_position(segment_index)

            # Switch video if needed
            if session.video_id != video_id:
                logger.debug(f"Session {session_id} switched from {session.video_id} to {video_id}")
                session.video_id = video_id
                session.current_segment = segment_index
                session.segments_requested.clear()

    async def _predictive_preload(self, session_id: str, video_id: str, current_segment: int):
        """
        Predictively preload segments based on viewing patterns.
        """
        if session_id not in self.active_sessions:
            return

        session = self.active_sessions[session_id]

        # Calculate segments to preload based on playback speed and configured ahead count
        segments_to_preload = min(
            self.preload_segments,
            max(3, int(self.preload_segments * session.playback_speed))
        )

        # Get video segments from database
        video_segments = await self.db.get_video_segments(video_id)
        if not video_segments:
            return

        total_segments = len(video_segments)
        segment_list = sorted(video_segments.values(), key=lambda s: s.segment_order)

        # Preload upcoming segments
        preload_tasks = []
        for i in range(1, segments_to_preload + 1):
            next_segment_index = current_segment + i

            if next_segment_index >= total_segments:
                break  # End of video

            if next_segment_index < len(segment_list):
                segment_info = segment_list[next_segment_index]

                # Check if already cached
                cache_key = f"{video_id}_{segment_info.filename}"
                if not await self._is_cached(cache_key):
                    # Create preload task
                    task_key = f"{video_id}_{segment_info.filename}_{session_id}"

                    if task_key not in self.preload_tasks or self.preload_tasks[task_key].done():
                        task = asyncio.create_task(
                            self._preload_segment(video_id, segment_info.filename, session_id)
                        )
                        self.preload_tasks[task_key] = task
                        preload_tasks.append(task)

        if preload_tasks:
            logger.debug(f"Started {len(preload_tasks)} preload tasks for session {session_id}")
            self.stats['preload_requests'] += len(preload_tasks)

    async def _preload_segment(self, video_id: str, segment_filename: str, session_id: str):
        """
        Preload a specific segment with semaphore control.
        """
        async with self.preload_semaphore:
            try:
                # Check if still needed (session might be inactive)
                if session_id not in self.active_sessions:
                    return

                # Check if already cached
                cache_key = f"{video_id}_{segment_filename}"
                if await self._is_cached(cache_key):
                    return

                # Download and cache
                segment_data = await self._download_segment(video_id, segment_filename)
                if segment_data:
                    await self.base_cache.cache_segment(video_id, segment_filename, segment_data)
                    self.stats['successful_preloads'] += 1
                    logger.debug(f"Preloaded segment {video_id}/{segment_filename} for session {session_id}")

            except Exception as e:
                logger.warning(f"Preload failed for {video_id}/{segment_filename}: {e}")

    async def _download_segment(self, video_id: str, segment_filename: str) -> Optional[bytes]:
        """Download a segment from Telegram using the configured handler."""
        try:
            # Get segment info from database
            segments = await self.db.get_video_segments(video_id)
            segment_info = segments.get(segment_filename)

            if not segment_info:
                logger.warning(f"Segment not found in database: {video_id}/{segment_filename}")
                return None

            # Download using telegram handler with bot_id for direct access
            return await self.telegram_handler.download_segment_from_telegram(
                segment_info.file_id, segment_info.bot_id
            )

        except Exception as e:
            logger.error(f"Error downloading segment {video_id}/{segment_filename}: {e}")
            return None

    async def _is_cached(self, cache_key: str) -> bool:
        """Check if a segment is already cached."""
        video_id, segment_filename = cache_key.split('_', 1)
        cached_data = await self.base_cache.get_cached_segment(video_id, segment_filename)
        return cached_data is not None

    def _extract_segment_index(self, segment_filename: str) -> int:
        """Extract segment index from filename (e.g., segment_0042.ts -> 42)."""
        try:
            # Handle different naming patterns
            if '_' in segment_filename:
                index_part = segment_filename.split('_')[-1].split('.')[0]
                return int(index_part)
            return 0
        except (ValueError, IndexError):
            return 0

    async def force_preload_video(self, video_id: str, start_segment: int = 0, segment_count: int = 10) -> bool:
        """
        Force preload segments for a video (useful for popular content).

        Args:
            video_id: Video to preload
            start_segment: Starting segment index
            segment_count: Number of segments to preload

        Returns:
            True if preloading started successfully
        """
        try:
            video_segments = await self.db.get_video_segments(video_id)
            if not video_segments:
                logger.warning(f"No segments found for video {video_id}")
                return False

            segment_list = sorted(video_segments.values(), key=lambda s: s.segment_order)

            preload_tasks = []
            for i in range(start_segment, min(start_segment + segment_count, len(segment_list))):
                if i < len(segment_list):
                    segment_info = segment_list[i]
                    cache_key = f"{video_id}_{segment_info.filename}"

                    if not await self._is_cached(cache_key):
                        task_key = f"force_{video_id}_{segment_info.filename}"
                        task = asyncio.create_task(
                            self._preload_segment(video_id, segment_info.filename, f"force_preload_{video_id}")
                        )
                        self.preload_tasks[task_key] = task
                        preload_tasks.append(task)

            if preload_tasks:
                logger.info(f"Force preloading {len(preload_tasks)} segments for video {video_id}")
                return True
            else:
                logger.info(f"All requested segments already cached for video {video_id}")
                return True

        except Exception as e:
            logger.error(f"Error in force preload for video {video_id}: {e}")
            return False

    async def cleanup_inactive_sessions(self, max_idle_time: int = 600):
        """
        Clean up inactive viewing sessions (default: 10 minutes idle).
        """
        current_time = time.time()
        sessions_to_remove = []

        for session_id, session in self.active_sessions.items():
            if current_time - session.last_request_time > max_idle_time:
                sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            del self.active_sessions[session_id]

            # Cancel related preload tasks
            tasks_to_cancel = [
                task_key for task_key in self.preload_tasks.keys()
                if session_id in task_key
            ]

            for task_key in tasks_to_cancel:
                task = self.preload_tasks.pop(task_key, None)
                if task and not task.done():
                    task.cancel()

        if sessions_to_remove:
            logger.info(f"Cleaned up {len(sessions_to_remove)} inactive sessions")
            self.stats['session_cleanups'] += len(sessions_to_remove)

    async def get_popular_videos(self, limit: int = 10) -> List[Dict]:
        """Get most popular videos based on active sessions."""
        video_popularity = defaultdict(int)

        for session in self.active_sessions.values():
            video_popularity[session.video_id] += 1

        # Sort by popularity
        popular_videos = sorted(
            video_popularity.items(),
            key=lambda x: x[1],
            reverse=True
        )[:limit]

        result = []
        for video_id, session_count in popular_videos:
            try:
                video_info = await self.db.get_video_info(video_id)
                if video_info:
                    result.append({
                        'video_id': video_id,
                        'filename': video_info.original_filename,
                        'active_sessions': session_count,
                        'duration': video_info.total_duration,
                        'segments': video_info.total_segments
                    })
            except Exception as e:
                logger.warning(f"Error getting info for popular video {video_id}: {e}")

        return result

    # Standard cache interface methods (compatible with existing code)

    async def get_cached_segment(self, video_id: str, segment_filename: str) -> Optional[bytes]:
        """Get cached segment (standard interface)."""
        return await self.base_cache.get_cached_segment(video_id, segment_filename)

    async def cache_segment(self, video_id: str, segment_filename: str, data: bytes) -> bool:
        """Cache a segment (standard interface)."""
        return await self.base_cache.cache_segment(video_id, segment_filename, data)

    async def get_cache_stats(self) -> Dict:
        """Get comprehensive cache statistics including predictive caching info."""
        base_stats = await self.base_cache.get_cache_stats()

        # Add predictive caching statistics
        active_preload_tasks = sum(1 for task in self.preload_tasks.values() if not task.done())

        videos_being_watched = len(set(session.video_id for session in self.active_sessions.values()))

        base_stats.update({
            'predictive_stats': {
                'active_sessions': len(self.active_sessions),
                'videos_being_watched': videos_being_watched,
                'active_preload_tasks': active_preload_tasks,
                'preload_segments_ahead': self.preload_segments,
                'max_concurrent_preloads': self.max_concurrent_preloads,
                'cache_hits': self.stats['cache_hits'],
                'cache_misses': self.stats['cache_misses'],
                'preload_requests': self.stats['preload_requests'],
                'successful_preloads': self.stats['successful_preloads'],
                'session_cleanups': self.stats['session_cleanups']
            }
        })

        return base_stats

    async def clear_cache(self, video_id: str = None):
        """Clear cache for a specific video or all cache."""
        await self.base_cache.clear_cache(video_id)

        # Clear related sessions and preload tasks
        if video_id:
            sessions_to_remove = [
                sid for sid, session in self.active_sessions.items()
                if session.video_id == video_id
            ]
            for sid in sessions_to_remove:
                del self.active_sessions[sid]

            tasks_to_cancel = [
                key for key in self.preload_tasks.keys()
                if video_id in key
            ]
            for key in tasks_to_cancel:
                task = self.preload_tasks.pop(key, None)
                if task and not task.done():
                    task.cancel()
        else:
            # Clear all sessions and tasks
            self.active_sessions.clear()
            for task in self.preload_tasks.values():
                if not task.done():
                    task.cancel()
            self.preload_tasks.clear()

    async def cleanup(self):
        """Cleanup method for graceful shutdown."""
        # Cancel all preload tasks
        for task in self.preload_tasks.values():
            if not task.done():
                task.cancel()
        self.preload_tasks.clear()
        
        # Clear sessions
        self.active_sessions.clear()
        
        logger.info("PredictiveCacheManager cleanup completed")


# Factory functions for easy creation

def create_cache_manager(db_manager: DatabaseManager, cache_type: str = "memory",
                        cache_dir: str = "cache", max_cache_size: int = 500 * 1024 * 1024):
    """
    Factory function to create a basic cache manager.

    Args:
        db_manager: Database manager instance
        cache_type: "memory" or "disk"
        cache_dir: Directory for disk cache (ignored for memory cache)
        max_cache_size: Maximum cache size in bytes

    Returns:
        Basic cache manager instance
    """
    if cache_type.lower() == "memory":
        return MemoryCacheManager(db_manager, max_cache_size)
    else:
        return DiskCacheManager(db_manager, cache_dir, max_cache_size)


def create_predictive_cache_manager(db_manager: DatabaseManager, telegram_handler,
                                   cache_type: str = "memory", cache_dir: str = "cache",
                                   max_cache_size: int = 500 * 1024 * 1024,
                                   preload_segments: int = 8, max_concurrent_preloads: int = 5):
    """
    Factory function to create a predictive cache manager with enhanced features.

    Args:
        db_manager: Database manager instance
        telegram_handler: Telegram handler for downloading segments
        cache_type: "memory" or "disk"
        cache_dir: Directory for disk cache (ignored for memory cache)
        max_cache_size: Maximum cache size in bytes
        preload_segments: Number of segments to preload ahead
        max_concurrent_preloads: Maximum concurrent preload operations

    Returns:
        Predictive cache manager instance
    """
    return PredictiveCacheManager(
        db_manager=db_manager,
        telegram_handler=telegram_handler,
        cache_type=cache_type,
        cache_dir=cache_dir,
        max_cache_size=max_cache_size,
        preload_segments=preload_segments,
        max_concurrent_preloads=max_concurrent_preloads
    )
