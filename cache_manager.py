import os
import asyncio
import aiofiles
import aiosqlite
import time
from typing import Optional, Dict, List
from datetime import datetime, timezone
from collections import OrderedDict
from database import DatabaseManager
from logger_config import logger
from utils import calculate_bytes_hash

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


class DiskCacheManager:
    """
    Original disk-based cache manager for backward compatibility.
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


def create_cache_manager(db_manager: DatabaseManager, cache_type: str = "memory",
                        cache_dir: str = "cache", max_cache_size: int = 500 * 1024 * 1024):
    """
    Factory function to create the appropriate cache manager based on configuration.

    Args:
        db_manager: Database manager instance
        cache_type: "memory" or "disk"
        cache_dir: Directory for disk cache (ignored for memory cache)
        max_cache_size: Maximum cache size in bytes

    Returns:
        Cache manager instance
    """
    if cache_type.lower() == "memory":
        return MemoryCacheManager(db_manager, max_cache_size)
    else:
        return DiskCacheManager(db_manager, cache_dir, max_cache_size)
