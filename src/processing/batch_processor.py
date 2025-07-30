import os
import asyncio
import glob
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from src.utils.logging import logger
from src.processing.video_processor import VideoProcessor
from src.storage.database import DatabaseManager
from ..telegram.handler import RoundRobinTelegramHandler, TelegramHandler
import time


class EnhancedVideoProcessor:
    """
    Enhanced video processor with batch processing capabilities for folder uploads.
    """
    
    def __init__(self, telegram_handler, db_manager: DatabaseManager):
        """
        Initialize the enhanced video processor.
        
        Args:
            telegram_handler: Either RoundRobinTelegramHandler or TelegramHandler
            db_manager: Database manager instance
        """
        self.telegram_handler = telegram_handler
        self.db_manager = db_manager
        self.processing_queue = asyncio.Queue()
        
        # Video file extensions to process
        self.video_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', 
            '.m4v', '.3gp', '.mpg', '.mpeg', '.ts', '.mts', '.m2ts'
        }
        
        # Configuration
        self.max_concurrent_workers = getattr(telegram_handler, 'bots', None)
        if self.max_concurrent_workers:
            self.max_concurrent_workers = len(self.max_concurrent_workers)
        else:
            self.max_concurrent_workers = 1
            
        logger.info(f"EnhancedVideoProcessor initialized with {self.max_concurrent_workers} worker(s)")

    def scan_video_files(self, folder_path: str, recursive: bool = True) -> List[Dict[str, str]]:
        """
        Scan folder for video files and return metadata.
        
        Args:
            folder_path: Path to folder containing videos
            recursive: Whether to scan subdirectories recursively
            
        Returns:
            List of video file information dictionaries
        """
        video_files = []
        folder_path = Path(folder_path)
        
        if not folder_path.exists():
            logger.error(f"Folder does not exist: {folder_path}")
            return []
        
        if not folder_path.is_dir():
            logger.error(f"Path is not a directory: {folder_path}")
            return []
        
        # Search pattern
        pattern = "**/*" if recursive else "*"
        
        logger.info(f"🔍 Scanning {'recursively' if recursive else 'non-recursively'}: {folder_path}")
        
        for file_path in folder_path.glob(pattern):
            if file_path.is_file() and file_path.suffix.lower() in self.video_extensions:
                try:
                    file_size = file_path.stat().st_size
                    video_info = {
                        'path': str(file_path),
                        'filename': file_path.name,
                        'stem': file_path.stem,
                        'size': file_size,
                        'size_gb': file_size / (1024**3),
                        'extension': file_path.suffix.lower(),
                        'relative_path': str(file_path.relative_to(folder_path))
                    }
                    video_files.append(video_info)
                    
                except Exception as e:
                    logger.warning(f"⚠️ Could not process {file_path}: {e}")
        
        # Sort by size (smallest first for faster initial processing)
        video_files.sort(key=lambda x: x['size'])
        
        logger.info(f"📁 Found {len(video_files)} video files:")
        total_size_gb = sum(v['size_gb'] for v in video_files)
        logger.info(f"📊 Total size: {total_size_gb:.2f} GB")
        
        # Show file list (first 10 and last 5)
        for i, video in enumerate(video_files[:10]):
            logger.info(f"  {i+1:2d}. {video['filename']} ({video['size_gb']:.2f} GB)")
        
        if len(video_files) > 10:
            logger.info(f"  ... {len(video_files) - 10} more files ...")
            for i, video in enumerate(video_files[-3:], len(video_files) - 2):
                logger.info(f"  {i:2d}. {video['filename']} ({video['size_gb']:.2f} GB)")
        
        return video_files

    async def process_season_folder(self, folder_path: str, recursive: bool = True) -> Dict[str, any]:
        """
        Process all video files in a folder (season/batch processing).
        
        Args:
            folder_path: Path to folder containing video files
            recursive: Whether to scan subdirectories recursively
            
        Returns:
            Processing results dictionary
        """
        start_time = time.time()
        
        logger.info(f"🎬 Starting batch processing of folder: {folder_path}")
        
        # Scan for video files
        video_files = self.scan_video_files(folder_path, recursive)
        
        if not video_files:
            logger.warning("No video files found to process")
            return {
                'success': False,
                'processed': 0,
                'failed': 0,
                'total': 0,
                'duration': 0,
                'errors': ['No video files found']
            }
        
        # Queue all videos for processing
        logger.info(f"📋 Queuing {len(video_files)} videos for processing...")
        for video_file in video_files:
            await self.processing_queue.put(video_file)
        
        # Process with multi-bot system
        logger.info(f"🚀 Starting parallel processing with {self.max_concurrent_workers} workers...")
        
        # Create worker tasks
        worker_tasks = []
        for worker_id in range(self.max_concurrent_workers):
            task = asyncio.create_task(
                self.process_video_worker(worker_id, len(video_files))
            )
            worker_tasks.append(task)
        
        # Wait for all workers to complete
        results = await asyncio.gather(*worker_tasks, return_exceptions=True)
        
        # Analyze results
        total_processed = 0
        total_failed = 0
        errors = []
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"❌ Worker {i} failed: {result}")
                errors.append(f"Worker {i}: {result}")
            else:
                processed, failed, worker_errors = result
                total_processed += processed
                total_failed += failed
                errors.extend(worker_errors)
        
        duration = time.time() - start_time
        
        # Final summary
        logger.info("=" * 60)
        logger.info(f"🏁 Batch processing completed!")
        logger.info(f"📊 Results: {total_processed} successful, {total_failed} failed")
        logger.info(f"⏱️ Total time: {duration/60:.1f} minutes")
        logger.info(f"📈 Average: {len(video_files)/duration*60:.1f} videos/hour")
        
        if total_processed > 0:
            logger.info(f"⚡ Multi-bot speedup: ~{self.max_concurrent_workers}x faster")
        
        if errors:
            logger.warning(f"⚠️ {len(errors)} errors encountered")
        
        return {
            'success': total_processed > 0,
            'processed': total_processed,
            'failed': total_failed,
            'total': len(video_files),
            'duration': duration,
            'errors': errors,
            'speedup': self.max_concurrent_workers
        }

    async def process_video_worker(self, worker_id: int, total_videos: int) -> Tuple[int, int, List[str]]:
        """
        Worker process to handle video processing queue.
        
        Args:
            worker_id: Unique identifier for this worker
            total_videos: Total number of videos being processed
            
        Returns:
            Tuple of (processed_count, failed_count, errors)
        """
        processed = 0
        failed = 0
        errors = []
        
        logger.info(f"🔧 Worker {worker_id} started")
        
        while True:
            try:
                # Get next video from queue (with timeout to prevent hanging)
                video_file = await asyncio.wait_for(
                    self.processing_queue.get(), timeout=5.0
                )
                
                logger.info(f"🎬 Worker {worker_id}: Processing {video_file['filename']} ({video_file['size_gb']:.2f} GB)")
                
                # Process the video
                success = await self.process_single_video(
                    video_file, worker_id, processed + failed + 1, total_videos
                )
                
                if success:
                    processed += 1
                    logger.info(f"✅ Worker {worker_id}: Successfully processed {video_file['filename']}")
                else:
                    failed += 1
                    error_msg = f"Failed to process {video_file['filename']}"
                    logger.error(f"❌ Worker {worker_id}: {error_msg}")
                    errors.append(error_msg)
                
                # Mark task as done
                self.processing_queue.task_done()
                
            except asyncio.TimeoutError:
                # No more videos in queue
                logger.info(f"🏁 Worker {worker_id} finished: {processed} processed, {failed} failed")
                break
                
            except Exception as e:
                failed += 1
                error_msg = f"Worker {worker_id} error: {e}"
                logger.error(f"💥 {error_msg}")
                errors.append(error_msg)
                
                # Mark task as done even on error
                if not self.processing_queue.empty():
                    self.processing_queue.task_done()
        
        return processed, failed, errors

    async def process_single_video(self, video_file: Dict[str, str], worker_id: int, 
                                 current_index: int, total_count: int) -> bool:
        """
        Process a single video file.
        
        Args:
            video_file: Video file information dictionary
            worker_id: ID of the worker processing this video
            current_index: Current position in processing order
            total_count: Total number of videos being processed
            
        Returns:
            True if processing succeeded, False otherwise
        """
        try:
            video_path = video_file['path']
            video_id = video_file['stem']
            original_filename = video_file['filename']
            
            # Create segments directory
            segments_dir = f"{os.getenv('SEGMENTS_DIR', 'segments')}/{video_id}"
            
            logger.info(f"🔧 Worker {worker_id}: [{current_index}/{total_count}] Starting HLS conversion for {original_filename}")
            
            # Convert to HLS segments
            try:
                playlist_path = split_video_to_hls(video_path, segments_dir)
                if not playlist_path or not os.path.exists(playlist_path):
                    logger.error(f"❌ Worker {worker_id}: HLS conversion failed for {original_filename}")
                    return False
                    
                logger.info(f"✅ Worker {worker_id}: HLS conversion completed for {original_filename}")
                
            except Exception as e:
                logger.error(f"❌ Worker {worker_id}: HLS conversion error for {original_filename}: {e}")
                return False
            
            # Upload segments to Telegram
            logger.info(f"📤 Worker {worker_id}: Starting Telegram upload for {original_filename}")
            
            try:
                upload_success = await self.telegram_handler.upload_segments_to_telegram(
                    segments_dir, video_id, original_filename
                )
                
                if upload_success:
                    logger.info(f"✅ Worker {worker_id}: Upload completed for {original_filename}")
                    return True
                else:
                    logger.error(f"❌ Worker {worker_id}: Upload failed for {original_filename}")
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Worker {worker_id}: Upload error for {original_filename}: {e}")
                return False
                
        except Exception as e:
            logger.error(f"💥 Worker {worker_id}: Unexpected error processing {video_file.get('filename', 'unknown')}: {e}")
            return False

    async def get_processing_status(self) -> Dict[str, any]:
        """
        Get current processing status.
        
        Returns:
            Status information dictionary
        """
        return {
            'queue_size': self.processing_queue.qsize(),
            'max_workers': self.max_concurrent_workers,
            'telegram_handler_type': type(self.telegram_handler).__name__
        }

    def estimate_processing_time(self, video_files: List[Dict[str, str]]) -> Dict[str, float]:
        """
        Estimate processing time for batch operation.
        
        Args:
            video_files: List of video file information
            
        Returns:
            Time estimates in different units
        """
        total_size_gb = sum(v['size_gb'] for v in video_files)
        
        # Conservative estimates (GB per hour of processing)
        single_bot_rate = 10.0  # GB/hour
        multi_bot_rate = single_bot_rate * self.max_concurrent_workers * 0.8  # 80% efficiency
        
        estimated_hours_single = total_size_gb / single_bot_rate
        estimated_hours_multi = total_size_gb / multi_bot_rate
        
        return {
            'total_size_gb': total_size_gb,
            'estimated_hours_single_bot': estimated_hours_single,
            'estimated_hours_multi_bot': estimated_hours_multi,
            'estimated_speedup': estimated_hours_single / estimated_hours_multi if estimated_hours_multi > 0 else 1,
            'videos_count': len(video_files)
        }


# Factory function for creating enhanced processor
def create_enhanced_processor(db_manager: DatabaseManager) -> EnhancedVideoProcessor:
    """
    Create an EnhancedVideoProcessor with appropriate telegram handler.
    
    Args:
        db_manager: Database manager instance
        
    Returns:
        Configured EnhancedVideoProcessor instance
    """
    # Import here to avoid circular imports
    from main import create_telegram_handler
    
    telegram_handler = create_telegram_handler(db_manager)
    return EnhancedVideoProcessor(telegram_handler, db_manager)


# CLI interface for batch processing
async def batch_process_cli(folder_path: str, recursive: bool = True) -> bool:
    """
    CLI interface for batch processing.
    
    Args:
        folder_path: Path to folder containing videos
        recursive: Whether to scan recursively
        
    Returns:
        True if processing succeeded, False otherwise
    """
    try:
        # Setup database
        from src.storage.database import DatabaseManager
        db_path = os.getenv('DB_PATH', 'video_streaming.db')
        db_manager = DatabaseManager(db_path)
        await db_manager.initialize_database()
        
        # Create processor
        processor = create_enhanced_processor(db_manager)
        
        # Get estimates
        video_files = processor.scan_video_files(folder_path, recursive)
        if not video_files:
            logger.warning("No video files found for processing")
            return False
        
        estimates = processor.estimate_processing_time(video_files)
        
        logger.info("📊 Processing estimates:")
        logger.info(f"  📁 Files: {estimates['videos_count']}")
        logger.info(f"  💾 Total size: {estimates['total_size_gb']:.2f} GB")
        logger.info(f"  ⏱️ Single bot: {estimates['estimated_hours_single_bot']:.1f} hours")
        logger.info(f"  ⚡ Multi-bot: {estimates['estimated_hours_multi_bot']:.1f} hours")
        logger.info(f"  🚀 Speedup: {estimates['estimated_speedup']:.1f}x")
        
        # Process folder
        results = await processor.process_season_folder(folder_path, recursive)
        
        return results['success']
        
    except Exception as e:
        logger.error(f"Batch processing CLI failed: {e}")
        return False