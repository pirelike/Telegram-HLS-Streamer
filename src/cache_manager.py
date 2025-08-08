"""
Smart caching system for HLS segments.
Provides predictive caching, memory/disk storage, and intelligent preloading.
"""

import asyncio
import logging
import hashlib
import pickle
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass
from collections import OrderedDict, defaultdict
import weakref


@dataclass
class CacheEntry:
    """Represents a cached segment."""
    segment_name: str
    data: bytes
    size: int
    timestamp: float
    access_count: int = 0
    last_access: float = 0.0
    hit_count: int = 0
    preloaded: bool = False


@dataclass
class AccessPattern:
    """Tracks access patterns for predictive caching."""
    segment_name: str
    access_times: List[float]
    next_segments: Dict[str, int]  # segment -> count
    avg_interval: float = 0.0
    
    
class LRUCache:
    """Least Recently Used cache implementation."""
    
    def __init__(self, max_size: int):
        self.max_size = max_size
        self.current_size = 0
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        
    def get(self, key: str) -> Optional[CacheEntry]:
        """Get item from cache, updating access order."""
        if key in self.cache:
            entry = self.cache[key]
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            entry.last_access = time.time()
            entry.access_count += 1
            entry.hit_count += 1
            return entry
        return None
        
    def put(self, key: str, entry: CacheEntry) -> List[str]:
        """Put item in cache, evicting if necessary. Returns evicted keys."""
        evicted_keys = []
        
        # Remove existing entry if present
        if key in self.cache:
            old_entry = self.cache[key]
            self.current_size -= old_entry.size
            del self.cache[key]
            
        # Evict items if necessary
        while self.current_size + entry.size > self.max_size and self.cache:
            evicted_key, evicted_entry = self.cache.popitem(last=False)  # Remove LRU
            self.current_size -= evicted_entry.size
            evicted_keys.append(evicted_key)
            
        # Add new entry
        self.cache[key] = entry
        self.current_size += entry.size
        
        return evicted_keys
        
    def remove(self, key: str) -> bool:
        """Remove item from cache."""
        if key in self.cache:
            entry = self.cache[key]
            self.current_size -= entry.size
            del self.cache[key]
            return True
        return False
        
    def clear(self):
        """Clear all cached items."""
        self.cache.clear()
        self.current_size = 0
        
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_hits = sum(entry.hit_count for entry in self.cache.values())
        total_accesses = sum(entry.access_count for entry in self.cache.values())
        
        return {
            'entries': len(self.cache),
            'size_bytes': self.current_size,
            'size_mb': self.current_size / (1024 * 1024),
            'max_size_mb': self.max_size / (1024 * 1024),
            'utilization': (self.current_size / self.max_size) if self.max_size > 0 else 0,
            'total_hits': total_hits,
            'total_accesses': total_accesses
        }


class DiskCache:
    """Disk-based cache for persistent storage."""
    
    def __init__(self, cache_dir: Path, max_size: int):
        self.cache_dir = cache_dir
        self.max_size = max_size
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Index file for tracking cached items
        self.index_file = self.cache_dir / "cache_index.pkl"
        self.cache_index: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.current_size = 0
        
        self._load_index()
        
    def _load_index(self):
        """Load cache index from disk."""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'rb') as f:
                    self.cache_index = pickle.load(f)
                    
                # Calculate current size and cleanup missing files
                valid_entries = OrderedDict()
                total_size = 0
                
                for key, info in self.cache_index.items():
                    file_path = self.cache_dir / info['filename']
                    if file_path.exists():
                        valid_entries[key] = info
                        total_size += info['size']
                    
                self.cache_index = valid_entries
                self.current_size = total_size
                
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to load cache index: {e}")
                self.cache_index = OrderedDict()
                self.current_size = 0
                
    def _save_index(self):
        """Save cache index to disk."""
        try:
            with open(self.index_file, 'wb') as f:
                pickle.dump(self.cache_index, f)
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to save cache index: {e}")
            
    def _get_filename(self, key: str) -> str:
        """Generate filename for cache entry."""
        hash_obj = hashlib.md5(key.encode())
        return f"cache_{hash_obj.hexdigest()}.bin"
        
    def get(self, key: str) -> Optional[bytes]:
        """Get item from disk cache."""
        if key in self.cache_index:
            info = self.cache_index[key]
            file_path = self.cache_dir / info['filename']
            
            if file_path.exists():
                try:
                    with open(file_path, 'rb') as f:
                        data = f.read()
                    
                    # Update access info
                    info['last_access'] = time.time()
                    info['access_count'] = info.get('access_count', 0) + 1
                    info['hit_count'] = info.get('hit_count', 0) + 1
                    
                    # Move to end (most recently used)
                    self.cache_index.move_to_end(key)
                    self._save_index()
                    
                    return data
                    
                except Exception as e:
                    logging.getLogger(__name__).error(f"Failed to read cache file {file_path}: {e}")
                    # Remove corrupted entry
                    self.remove(key)
                    
        return None
        
    def put(self, key: str, data: bytes) -> List[str]:
        """Put item in disk cache."""
        evicted_keys = []
        filename = self._get_filename(key)
        file_path = self.cache_dir / filename
        data_size = len(data)
        
        # Remove existing entry if present
        if key in self.cache_index:
            self.remove(key)
            
        # Evict items if necessary
        while self.current_size + data_size > self.max_size and self.cache_index:
            evicted_key, evicted_info = self.cache_index.popitem(last=False)
            evicted_file = self.cache_dir / evicted_info['filename']
            
            if evicted_file.exists():
                try:
                    evicted_file.unlink()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to delete cache file {evicted_file}: {e}")
                    
            self.current_size -= evicted_info['size']
            evicted_keys.append(evicted_key)
            
        # Write new file
        try:
            with open(file_path, 'wb') as f:
                f.write(data)
                
            # Add to index
            self.cache_index[key] = {
                'filename': filename,
                'size': data_size,
                'timestamp': time.time(),
                'access_count': 0,
                'hit_count': 0,
                'last_access': time.time()
            }
            
            self.current_size += data_size
            self._save_index()
            
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to write cache file {file_path}: {e}")
            
        return evicted_keys
        
    def remove(self, key: str) -> bool:
        """Remove item from disk cache."""
        if key in self.cache_index:
            info = self.cache_index[key]
            file_path = self.cache_dir / info['filename']
            
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Failed to delete cache file {file_path}: {e}")
                    
            self.current_size -= info['size']
            del self.cache_index[key]
            self._save_index()
            return True
            
        return False
        
    def clear(self):
        """Clear all cached items."""
        for info in self.cache_index.values():
            file_path = self.cache_dir / info['filename']
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
                    
        self.cache_index.clear()
        self.current_size = 0
        self._save_index()
        
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_hits = sum(info.get('hit_count', 0) for info in self.cache_index.values())
        total_accesses = sum(info.get('access_count', 0) for info in self.cache_index.values())
        
        return {
            'entries': len(self.cache_index),
            'size_bytes': self.current_size,
            'size_mb': self.current_size / (1024 * 1024),
            'max_size_mb': self.max_size / (1024 * 1024),
            'utilization': (self.current_size / self.max_size) if self.max_size > 0 else 0,
            'total_hits': total_hits,
            'total_accesses': total_accesses
        }


class CacheManager:
    """Smart cache manager with predictive caching capabilities."""
    
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize cache backend
        if config.cache_type == "memory":
            self.cache = LRUCache(config.cache_size)
        else:  # disk
            self.cache = DiskCache(config.cache_dir, config.cache_size)
            
        # Access pattern tracking for predictive caching
        self.access_patterns: Dict[str, AccessPattern] = {}
        self.sequence_tracking: Dict[str, List[str]] = defaultdict(list)  # video_id -> segment sequence
        
        # Preloading management
        self.preload_queue = asyncio.Queue()
        self.preload_tasks: Dict[str, asyncio.Task] = {}
        self.max_concurrent_preloads = config.max_concurrent_preloads
        
        # Statistics
        self.stats = {
            'hits': 0,
            'misses': 0,
            'preload_hits': 0,
            'total_bytes_served': 0,
            'avg_response_time': 0.0
        }
        
        # Start preload workers
        self._start_preload_workers()
        
    def _start_preload_workers(self):
        """Start background workers for preloading segments."""
        for i in range(self.max_concurrent_preloads):
            task = asyncio.create_task(self._preload_worker(f"worker_{i}"))
            self.preload_tasks[f"worker_{i}"] = task
            
    async def _preload_worker(self, worker_id: str):
        """Background worker for preloading segments."""
        while True:
            try:
                # Get preload request
                segment_name, fetch_func = await self.preload_queue.get()
                
                # Check if already cached
                if await self.get(segment_name) is not None:
                    self.preload_queue.task_done()
                    continue
                    
                # Fetch and cache the segment
                try:
                    data = await fetch_func()
                    if data:
                        await self.put(segment_name, data, preloaded=True)
                        self.logger.debug(f"Preloaded segment: {segment_name}")
                except Exception as e:
                    self.logger.warning(f"Failed to preload {segment_name}: {e}")
                    
                self.preload_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Preload worker {worker_id} error: {e}")
                await asyncio.sleep(1)
                
    async def get(self, segment_name: str) -> Optional[bytes]:
        """Get segment from cache."""
        start_time = time.time()
        
        # Try cache backends
        data = None
        if isinstance(self.cache, LRUCache):
            entry = self.cache.get(segment_name)
            if entry:
                data = entry.data
                if entry.preloaded:
                    self.stats['preload_hits'] += 1
        else:  # DiskCache
            data = self.cache.get(segment_name)
            
        response_time = time.time() - start_time
        
        if data:
            self.stats['hits'] += 1
            self.stats['total_bytes_served'] += len(data)
            self._update_avg_response_time(response_time)
            self._track_access(segment_name)
            return data
        else:
            self.stats['misses'] += 1
            return None
            
    async def put(self, segment_name: str, data: bytes, preloaded: bool = False) -> bool:
        """Put segment in cache."""
        try:
            if isinstance(self.cache, LRUCache):
                entry = CacheEntry(
                    segment_name=segment_name,
                    data=data,
                    size=len(data),
                    timestamp=time.time(),
                    preloaded=preloaded
                )
                evicted = self.cache.put(segment_name, entry)
            else:  # DiskCache
                evicted = self.cache.put(segment_name, data)
                
            if evicted:
                self.logger.debug(f"Evicted {len(evicted)} segments for {segment_name}")
                
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to cache segment {segment_name}: {e}")
            return False
            
    def _track_access(self, segment_name: str):
        """Track access patterns for predictive caching."""
        current_time = time.time()
        
        # Initialize pattern if new
        if segment_name not in self.access_patterns:
            self.access_patterns[segment_name] = AccessPattern(
                segment_name=segment_name,
                access_times=[],
                next_segments={}
            )
            
        pattern = self.access_patterns[segment_name]
        pattern.access_times.append(current_time)
        
        # Keep only recent access times (last 10)
        if len(pattern.access_times) > 10:
            pattern.access_times = pattern.access_times[-10:]
            
        # Calculate average access interval
        if len(pattern.access_times) > 1:
            intervals = []
            for i in range(1, len(pattern.access_times)):
                intervals.append(pattern.access_times[i] - pattern.access_times[i-1])
            pattern.avg_interval = sum(intervals) / len(intervals)
            
    def _extract_video_id_and_sequence(self, segment_name: str) -> Tuple[Optional[str], Optional[int]]:
        """Extract video ID and sequence number from segment name."""
        try:
            # Expected format: video/{quality}/segment_001.ts or similar
            parts = segment_name.split('/')
            if len(parts) >= 2:
                # Try to extract sequence number from filename
                filename = parts[-1]
                if 'segment_' in filename:
                    seq_part = filename.split('segment_')[1].split('.')[0]
                    sequence = int(seq_part)
                    
                    # Video ID might be embedded or we need to track it separately
                    return None, sequence  # Would need video context to determine video_id
                    
        except (ValueError, IndexError):
            pass
            
        return None, None
        
    async def predict_next_segments(self, current_segment: str, video_id: str = None) -> List[str]:
        """Predict which segments will be accessed next."""
        predictions = []
        
        # Pattern-based prediction
        if current_segment in self.access_patterns:
            pattern = self.access_patterns[current_segment]
            
            # Get most frequently accessed next segments
            sorted_next = sorted(pattern.next_segments.items(), key=lambda x: x[1], reverse=True)
            predictions.extend([seg for seg, _ in sorted_next[:3]])  # Top 3
            
        # Sequence-based prediction (for continuous playback)
        video_id, sequence = self._extract_video_id_and_sequence(current_segment)
        if sequence is not None:
            # Predict next sequential segments
            for i in range(1, self.config.preload_segments + 1):
                next_seq = sequence + i
                # Reconstruct segment name (this would need video context)
                next_segment = current_segment.replace(f"segment_{sequence:03d}", f"segment_{next_seq:03d}")
                if next_segment not in predictions:
                    predictions.append(next_segment)
                    
        return predictions[:self.config.preload_segments]
        
    async def preload_segments(self, segment_names: List[str], fetch_func_factory):
        """Queue segments for preloading."""
        for segment_name in segment_names:
            if segment_name not in [item[0] for item in self.preload_queue._queue]:
                fetch_func = fetch_func_factory(segment_name)
                await self.preload_queue.put((segment_name, fetch_func))
                
    async def invalidate(self, segment_name: str) -> bool:
        """Remove segment from cache."""
        return self.cache.remove(segment_name)
        
    async def clear_cache(self):
        """Clear all cached segments."""
        self.cache.clear()
        self.access_patterns.clear()
        self.sequence_tracking.clear()
        
    def _update_avg_response_time(self, response_time: float):
        """Update average response time using exponential moving average."""
        alpha = 0.1  # Smoothing factor
        if self.stats['avg_response_time'] == 0:
            self.stats['avg_response_time'] = response_time
        else:
            self.stats['avg_response_time'] = (
                alpha * response_time + (1 - alpha) * self.stats['avg_response_time']
            )
            
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics."""
        cache_stats = self.cache.get_stats()
        
        hit_ratio = (
            self.stats['hits'] / (self.stats['hits'] + self.stats['misses'])
            if (self.stats['hits'] + self.stats['misses']) > 0 else 0
        )
        
        preload_ratio = (
            self.stats['preload_hits'] / self.stats['hits']
            if self.stats['hits'] > 0 else 0
        )
        
        return {
            **cache_stats,
            'hit_ratio': hit_ratio,
            'preload_hit_ratio': preload_ratio,
            'total_hits': self.stats['hits'],
            'total_misses': self.stats['misses'],
            'preload_hits': self.stats['preload_hits'],
            'total_bytes_served': self.stats['total_bytes_served'],
            'total_mb_served': self.stats['total_bytes_served'] / (1024 * 1024),
            'avg_response_time_ms': self.stats['avg_response_time'] * 1000,
            'access_patterns_tracked': len(self.access_patterns),
            'preload_queue_size': self.preload_queue.qsize()
        }
        
    async def cleanup(self):
        """Clean up cache resources."""
        # Cancel preload workers
        for task in self.preload_tasks.values():
            task.cancel()
            
        # Wait for workers to finish
        await asyncio.gather(*self.preload_tasks.values(), return_exceptions=True)
        
        # Clear queue
        while not self.preload_queue.empty():
            try:
                self.preload_queue.get_nowait()
                self.preload_queue.task_done()
            except asyncio.QueueEmpty:
                break