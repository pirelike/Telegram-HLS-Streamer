import os
import subprocess
import time
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from src.utils.logging import logger


class SegmentOptimizer:
    """
    Utility for optimizing and re-encoding existing .ts segments.
    """
    
    def __init__(self, max_chunk_size: int = None):
        """
        Initialize the segment optimizer.
        
        Args:
            max_chunk_size: Maximum size for segments in bytes
        """
        self.max_chunk_size = max_chunk_size or int(os.getenv('MAX_CHUNK_SIZE', 15 * 1024 * 1024))
        self.telegram_limit = 20 * 1024 * 1024  # 20MB hard limit
        
        # Ensure we don't exceed Telegram limits
        if self.max_chunk_size > self.telegram_limit:
            logger.warning(f"MAX_CHUNK_SIZE ({self.max_chunk_size / (1024*1024):.1f}MB) exceeds Telegram limit")
            self.max_chunk_size = self.telegram_limit
        
        logger.info(f"SegmentOptimizer initialized with {self.max_chunk_size / (1024*1024):.1f}MB limit")

    def analyze_segments(self, segments_dir: str) -> Dict[str, any]:
        """
        Analyze segments in a directory for optimization opportunities.
        
        Args:
            segments_dir: Directory containing .ts segments
            
        Returns:
            Analysis results dictionary
        """
        segments_path = Path(segments_dir)
        if not segments_path.exists():
            logger.error(f"Segments directory does not exist: {segments_dir}")
            return {'error': 'Directory not found'}
        
        ts_files = sorted([f for f in segments_path.iterdir() if f.suffix == '.ts'])
        if not ts_files:
            logger.warning(f"No .ts files found in {segments_dir}")
            return {'error': 'No segments found'}
        
        logger.info(f"🔍 Analyzing {len(ts_files)} segments in {segments_dir}")
        
        # Analyze each segment
        segments_info = []
        total_size = 0
        oversized_count = 0
        critically_oversized_count = 0
        
        for ts_file in ts_files:
            file_size = ts_file.stat().st_size
            total_size += file_size
            
            is_oversized = file_size > self.max_chunk_size
            is_critical = file_size > self.telegram_limit
            
            if is_oversized:
                oversized_count += 1
            if is_critical:
                critically_oversized_count += 1
            
            segment_info = {
                'filename': ts_file.name,
                'path': str(ts_file),
                'size': file_size,
                'size_mb': file_size / (1024 * 1024),
                'is_oversized': is_oversized,
                'is_critical': is_critical,
                'compression_needed': is_oversized
            }
            segments_info.append(segment_info)
        
        analysis = {
            'segments_dir': segments_dir,
            'total_segments': len(ts_files),
            'total_size': total_size,
            'total_size_mb': total_size / (1024 * 1024),
            'average_size_mb': (total_size / len(ts_files)) / (1024 * 1024),
            'oversized_count': oversized_count,
            'critically_oversized_count': critically_oversized_count,
            'oversized_percent': (oversized_count / len(ts_files)) * 100,
            'segments': segments_info,
            'optimization_needed': oversized_count > 0
        }
        
        # Log analysis results
        logger.info(f"📊 Segment Analysis Results:")
        logger.info(f"  📦 Total segments: {analysis['total_segments']}")
        logger.info(f"  💾 Total size: {analysis['total_size_mb']:.1f} MB")
        logger.info(f"  📊 Average size: {analysis['average_size_mb']:.1f} MB")
        logger.info(f"  ⚠️ Oversized: {oversized_count} ({analysis['oversized_percent']:.1f}%)")
        logger.info(f"  🚨 Critical: {critically_oversized_count}")
        
        return analysis

    def optimize_segments(self, segments_dir: str, quality_mode: str = 'balanced') -> Dict[str, any]:
        """
        Optimize all oversized segments in a directory.
        
        Args:
            segments_dir: Directory containing .ts segments
            quality_mode: 'fast', 'balanced', or 'quality'
            
        Returns:
            Optimization results dictionary
        """
        logger.info(f"🔧 Starting segment optimization in {quality_mode} mode")
        
        # Analyze segments first
        analysis = self.analyze_segments(segments_dir)
        if 'error' in analysis:
            return analysis
        
        if not analysis['optimization_needed']:
            logger.info("✅ No optimization needed - all segments are within size limits")
            return {
                'success': True,
                'optimized_count': 0,
                'total_segments': analysis['total_segments'],
                'size_reduction': 0,
                'message': 'No optimization needed'
            }
        
        # Get optimization settings based on quality mode
        settings = self._get_optimization_settings(quality_mode)
        
        # Optimize oversized segments
        oversized_segments = [s for s in analysis['segments'] if s['is_oversized']]
        logger.info(f"🔧 Optimizing {len(oversized_segments)} oversized segments...")
        
        optimization_results = []
        total_size_before = 0
        total_size_after = 0
        successful_optimizations = 0
        
        for i, segment in enumerate(oversized_segments, 1):
            logger.info(f"📝 [{i}/{len(oversized_segments)}] Optimizing {segment['filename']} ({segment['size_mb']:.1f}MB)")
            
            result = self._optimize_single_segment(
                segment['path'], 
                settings,
                segment['size']
            )
            
            if result['success']:
                successful_optimizations += 1
                total_size_before += result['size_before']
                total_size_after += result['size_after']
                
                reduction_percent = ((result['size_before'] - result['size_after']) / result['size_before']) * 100
                logger.info(f"✅ {segment['filename']}: {result['size_before']/(1024*1024):.1f}MB → {result['size_after']/(1024*1024):.1f}MB (-{reduction_percent:.1f}%)")
            else:
                logger.error(f"❌ Failed to optimize {segment['filename']}: {result.get('error', 'Unknown error')}")
            
            optimization_results.append(result)
        
        # Calculate overall results
        total_reduction = total_size_before - total_size_after
        overall_reduction_percent = (total_reduction / total_size_before * 100) if total_size_before > 0 else 0
        
        results = {
            'success': successful_optimizations > 0,
            'optimized_count': successful_optimizations,
            'failed_count': len(oversized_segments) - successful_optimizations,
            'total_segments': analysis['total_segments'],
            'size_before_mb': total_size_before / (1024 * 1024),
            'size_after_mb': total_size_after / (1024 * 1024),
            'size_reduction_mb': total_reduction / (1024 * 1024),
            'reduction_percent': overall_reduction_percent,
            'quality_mode': quality_mode,
            'segment_results': optimization_results
        }
        
        # Log final results
        logger.info("=" * 60)
        logger.info(f"🏁 Segment Optimization Complete!")
        logger.info(f"✅ Successfully optimized: {successful_optimizations}/{len(oversized_segments)}")
        logger.info(f"💾 Size reduction: {results['size_reduction_mb']:.1f}MB ({results['reduction_percent']:.1f}%)")
        logger.info(f"🎯 Quality mode: {quality_mode}")
        
        return results

    def _get_optimization_settings(self, quality_mode: str) -> Dict[str, any]:
        """Get FFmpeg settings based on quality mode."""
        base_settings = {
            'codec': 'libx264',
            'audio_codec': 'aac',
            'audio_bitrate': '128k'
        }
        
        if quality_mode == 'fast':
            return {
                **base_settings,
                'preset': 'veryfast',
                'crf': '28',
                'target_reduction': 0.7  # Target 70% of original size
            }
        elif quality_mode == 'quality':
            return {
                **base_settings,
                'preset': 'medium',
                'crf': '21',
                'target_reduction': 0.85  # Target 85% of original size
            }
        else:  # balanced
            return {
                **base_settings,
                'preset': 'fast',
                'crf': '23',
                'target_reduction': 0.80  # Target 80% of original size
            }

    def _optimize_single_segment(self, segment_path: str, settings: Dict[str, any], original_size: int) -> Dict[str, any]:
        """
        Optimize a single segment file.
        
        Args:
            segment_path: Path to the segment file
            settings: Optimization settings
            original_size: Original file size in bytes
            
        Returns:
            Optimization result dictionary
        """
        try:
            # Calculate target size and bitrate
            target_size = int(self.max_chunk_size * settings['target_reduction'])
            
            # Estimate segment duration (assume ~10 seconds as default)
            estimated_duration = 10.0
            target_bitrate = max(1500000, min((target_size * 8) // estimated_duration, 12000000))
            
            # Create temporary output file
            temp_path = segment_path + '.tmp'
            
            # Build FFmpeg command
            ffmpeg_cmd = [
                'ffmpeg', '-i', segment_path,
                
                # Input settings
                '-avoid_negative_ts', 'make_zero',
                
                # Video encoding
                '-c:v', settings['codec'],
                '-preset', settings['preset'],
                '-crf', settings['crf'],
                '-maxrate', str(target_bitrate),
                '-bufsize', str(int(target_bitrate * 1.5)),
                
                # Audio encoding
                '-c:a', settings['audio_codec'],
                '-b:a', settings['audio_bitrate'],
                
                # Output format
                '-f', 'mpegts',
                '-y', temp_path
            ]
            
            # Execute optimization
            start_time = time.time()
            
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=1800  # 30 minute timeout
            )
            
            processing_time = time.time() - start_time
            
            if result.returncode != 0:
                return {
                    'success': False,
                    'error': f'FFmpeg failed: {result.stderr}',
                    'size_before': original_size,
                    'processing_time': processing_time
                }
            
            # Check if optimization was successful
            if not os.path.exists(temp_path):
                return {
                    'success': False,
                    'error': 'Output file not created',
                    'size_before': original_size,
                    'processing_time': processing_time
                }
            
            new_size = os.path.getsize(temp_path)
            
            # Verify the new file is smaller and within limits
            if new_size >= original_size:
                os.remove(temp_path)
                return {
                    'success': False,
                    'error': 'No size reduction achieved',
                    'size_before': original_size,
                    'size_after': new_size,
                    'processing_time': processing_time
                }
            
            # Check if still too large
            if new_size > self.max_chunk_size:
                logger.warning(f"Optimized segment still oversized: {new_size / (1024*1024):.1f}MB")
                # Keep it anyway if it's smaller than before and under Telegram limit
                if new_size > self.telegram_limit:
                    os.remove(temp_path)
                    return {
                        'success': False,
                        'error': 'Still exceeds Telegram limit after optimization',
                        'size_before': original_size,
                        'size_after': new_size,
                        'processing_time': processing_time
                    }
            
            # Replace original with optimized version
            os.replace(temp_path, segment_path)
            
            return {
                'success': True,
                'size_before': original_size,
                'size_after': new_size,
                'processing_time': processing_time,
                'reduction_bytes': original_size - new_size,
                'reduction_percent': ((original_size - new_size) / original_size) * 100
            }
            
        except subprocess.TimeoutExpired:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return {
                'success': False,
                'error': 'FFmpeg timeout',
                'size_before': original_size,
                'processing_time': 1800
            }
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return {
                'success': False,
                'error': str(e),
                'size_before': original_size,
                'processing_time': 0
            }

    def batch_optimize_multiple_dirs(self, base_dir: str, quality_mode: str = 'balanced') -> Dict[str, any]:
        """
        Optimize segments across multiple video directories.
        
        Args:
            base_dir: Base directory containing video segment directories
            quality_mode: Quality mode for optimization
            
        Returns:
            Batch optimization results
        """
        base_path = Path(base_dir)
        if not base_path.exists():
            logger.error(f"Base directory does not exist: {base_dir}")
            return {'error': 'Base directory not found'}
        
        # Find all segment directories
        segment_dirs = [d for d in base_path.iterdir() if d.is_dir()]
        
        if not segment_dirs:
            logger.warning(f"No segment directories found in {base_dir}")
            return {'error': 'No segment directories found'}
        
        logger.info(f"🔧 Starting batch optimization of {len(segment_dirs)} directories")
        
        batch_results = {
            'directories_processed': 0,
            'total_segments_optimized': 0,
            'total_size_reduction_mb': 0,
            'total_processing_time': 0,
            'results_per_dir': []
        }
        
        start_time = time.time()
        
        for i, segment_dir in enumerate(segment_dirs, 1):
            logger.info(f"📁 [{i}/{len(segment_dirs)}] Processing {segment_dir.name}")
            
            dir_result = self.optimize_segments(str(segment_dir), quality_mode)
            
            if dir_result.get('success'):
                batch_results['directories_processed'] += 1
                batch_results['total_segments_optimized'] += dir_result.get('optimized_count', 0)
                batch_results['total_size_reduction_mb'] += dir_result.get('size_reduction_mb', 0)
            
            batch_results['results_per_dir'].append({
                'directory': segment_dir.name,
                'result': dir_result
            })
        
        batch_results['total_processing_time'] = time.time() - start_time
        
        # Log batch results
        logger.info("=" * 80)
        logger.info(f"🏁 Batch Optimization Complete!")
        logger.info(f"📁 Directories processed: {batch_results['directories_processed']}/{len(segment_dirs)}")
        logger.info(f"📦 Total segments optimized: {batch_results['total_segments_optimized']}")
        logger.info(f"💾 Total size reduction: {batch_results['total_size_reduction_mb']:.1f}MB")
        logger.info(f"⏱️ Total time: {batch_results['total_processing_time']/60:.1f} minutes")
        
        return batch_results


# CLI interface for segment optimization
def optimize_segments_cli(segments_dir: str, quality_mode: str = 'balanced') -> bool:
    """
    CLI interface for segment optimization.
    
    Args:
        segments_dir: Directory containing segments to optimize
        quality_mode: Quality mode ('fast', 'balanced', 'quality')
        
    Returns:
        True if optimization succeeded, False otherwise
    """
    try:
        optimizer = SegmentOptimizer()
        results = optimizer.optimize_segments(segments_dir, quality_mode)
        return results.get('success', False)
    except Exception as e:
        logger.error(f"Segment optimization CLI failed: {e}")
        return False


def batch_optimize_cli(base_dir: str, quality_mode: str = 'balanced') -> bool:
    """
    CLI interface for batch segment optimization.
    
    Args:
        base_dir: Base directory containing multiple segment directories
        quality_mode: Quality mode for optimization
        
    Returns:
        True if batch optimization succeeded, False otherwise
    """
    try:
        optimizer = SegmentOptimizer()
        results = optimizer.batch_optimize_multiple_dirs(base_dir, quality_mode)
        return not ('error' in results)
    except Exception as e:
        logger.error(f"Batch segment optimization CLI failed: {e}")
        return False