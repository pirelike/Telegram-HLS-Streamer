import os
import json
import subprocess
from typing import List, Dict, Optional, Tuple
from logger_config import logger

def split_video_to_hls(video_path: str, output_dir: str, max_chunk_size: int = None) -> str:
    """
    Smart video segmentation that finds the optimal segment duration to minimize re-encoding.

    Strategy:
    1. Start with longer segments (less processing)
    2. Find the duration that minimizes oversized segments
    3. Only re-encode the remaining oversized segments with calculated optimal bitrates
    """
    if not os.path.exists(video_path):
        logger.error(f"Video file not found: {video_path}")
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Load configuration
    if max_chunk_size is None:
        max_chunk_size = int(os.getenv('MAX_CHUNK_SIZE', 15 * 1024 * 1024))

    telegram_bot_limit = 20 * 1024 * 1024  # 20MB
    if max_chunk_size > telegram_bot_limit:
        logger.warning(f"MAX_CHUNK_SIZE ({max_chunk_size / (1024*1024):.1f}MB) exceeds Telegram bot limit (20MB)")
        max_chunk_size = telegram_bot_limit

    # Get minimum segment length from environment
    min_segment_duration = int(os.getenv('MIN_SEGMENT_DURATION', 2))  # Default 2 seconds
    max_segment_duration = int(os.getenv('MAX_SEGMENT_DURATION', 30))  # Default 30 seconds max

    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Created output directory: {output_dir}")
    logger.info(f"Smart segmentation: finding optimal duration (min: {min_segment_duration}s, max: {max_segment_duration}s)")

    # Probe video first
    video_info, subtitle_info = probe_video_info(video_path)
    bitrate = video_info.get('bitrate', 0)

    if subtitle_info:
        logger.info(f"Subtitle tracks found: {len(subtitle_info)}")

    # PHASE 1: Find optimal segment duration
    optimal_duration, best_oversized_count = find_optimal_segment_duration(
        video_path, output_dir, min_segment_duration, max_segment_duration, max_chunk_size
    )

    if optimal_duration is None:
        raise RuntimeError("Unable to find any viable segment duration")

    logger.info(f"🎯 Optimal duration: {optimal_duration}s with {best_oversized_count} oversized segments")

    # PHASE 2: Create segments with optimal duration
    playlist_path = create_segments_with_duration(video_path, output_dir, optimal_duration)

    if not playlist_path:
        raise RuntimeError("Failed to create segments with optimal duration")

    # PHASE 3: Smart re-encoding of oversized segments only
    ts_files = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
    oversized_segments = identify_oversized_segments(output_dir, ts_files, max_chunk_size)

    if oversized_segments:
        logger.info(f"🎬 PHASE 3: Smart re-encoding {len(oversized_segments)} oversized segments")

        if not smart_reencode_oversized_segments(video_path, output_dir, oversized_segments, optimal_duration, max_chunk_size):
            raise RuntimeError("Failed to re-encode oversized segments")
    else:
        logger.info("✅ All segments are within size limits - no re-encoding needed!")

    # Extract subtitles
    extracted_subtitle_files = []
    if subtitle_info:
        logger.info(f"Extracting {len(subtitle_info)} subtitle tracks to files...")
        extracted_subtitle_files = extract_subtitles_to_files(video_path, output_dir, subtitle_info)

        for i, subtitle in enumerate(subtitle_info):
            if i < len(extracted_subtitle_files):
                subtitle['extracted_file'] = extracted_subtitle_files[i]

    # Save subtitle metadata
    if subtitle_info:
        subtitle_metadata_path = os.path.join(output_dir, 'subtitles.json')
        with open(subtitle_metadata_path, 'w', encoding='utf-8') as f:
            json.dump(subtitle_info, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved subtitle metadata to {subtitle_metadata_path}")

    # Final statistics
    ts_files = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
    log_final_statistics(output_dir, ts_files, max_chunk_size, optimal_duration, best_oversized_count)

    return playlist_path

def find_optimal_segment_duration(video_path: str, output_dir: str, min_duration: int, max_duration: int, max_size: int) -> Tuple[Optional[int], int]:
    """
    Find the optimal segment duration that minimizes the number of oversized segments.

    Returns:
        Tuple[Optional[int], int]: (optimal_duration, oversized_count)
    """
    logger.info("🔍 PHASE 1: Finding optimal segment duration to minimize re-encoding")

    # Test different durations from longer to shorter
    test_durations = []

    # Start with longer segments (less total segments = faster processing)
    for duration in range(max_duration, min_duration - 1, -2):  # Step down by 2 seconds
        test_durations.append(duration)

    # Add some specific good values
    additional_durations = [15, 10, 8, 6, 5, 4, 3]
    for d in additional_durations:
        if min_duration <= d <= max_duration and d not in test_durations:
            test_durations.append(d)

    test_durations.sort(reverse=True)  # Test longer durations first
    logger.info(f"Testing durations: {test_durations}")

    best_duration = None
    best_oversized_count = float('inf')
    duration_results = []

    for duration in test_durations:
        logger.info(f"Testing {duration}s segments...")

        # Quick test with copy mode
        temp_playlist = test_segmentation_duration(video_path, output_dir, duration)

        if temp_playlist:
            # Count oversized segments
            ts_files = [f for f in os.listdir(output_dir) if f.endswith('.ts') and f.startswith(f'test_{duration}_')]
            oversized_count = count_oversized_segments(output_dir, ts_files, max_size)
            total_segments = len(ts_files)

            duration_results.append({
                'duration': duration,
                'total_segments': total_segments,
                'oversized_count': oversized_count,
                'oversized_percent': (oversized_count / total_segments * 100) if total_segments > 0 else 100
            })

            logger.info(f"  {duration}s: {total_segments} total segments, {oversized_count} oversized ({oversized_count/total_segments*100:.1f}%)")

            # Update best if this is better
            if oversized_count < best_oversized_count:
                best_oversized_count = oversized_count
                best_duration = duration
                logger.info(f"  🎯 New best: {duration}s with {oversized_count} oversized segments")

            # Clean up test files
            cleanup_test_files(output_dir, f'test_{duration}_')

            # Early exit if we found a perfect solution
            if oversized_count == 0:
                logger.info(f"  ✅ Perfect! No oversized segments with {duration}s")
                break

            # Early exit if we're getting worse (more oversized segments)
            if len(duration_results) >= 3 and oversized_count > best_oversized_count * 1.5:
                logger.info(f"  ⏭️  Stopping early - results getting worse")
                break
        else:
            logger.warning(f"  ❌ {duration}s segmentation failed")

    # Log summary of results
    logger.info("📊 Duration test results:")
    for result in duration_results:
        status = "🎯 OPTIMAL" if result['duration'] == best_duration else ""
        logger.info(f"  {result['duration']:2d}s: {result['total_segments']:3d} segments, {result['oversized_count']:2d} oversized ({result['oversized_percent']:4.1f}%) {status}")

    return best_duration, best_oversized_count

def test_segmentation_duration(video_path: str, output_dir: str, duration: int) -> Optional[str]:
    """Test segmentation with a specific duration using copy mode."""
    playlist_path = os.path.join(output_dir, f'test_{duration}.m3u8')
    segment_pattern = os.path.join(output_dir, f'test_{duration}_%04d.ts')

    ffmpeg_cmd = [
        'ffmpeg', '-i', video_path,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-c:v', 'copy',  # Copy mode for speed
        '-c:a', 'copy',
        '-hls_time', str(duration),
        '-hls_list_size', '0',
        '-hls_segment_filename', segment_pattern,
        '-hls_flags', 'delete_segments+append_list',
        '-f', 'hls',
        '-y',
        playlist_path
    ]

    try:
        process = subprocess.run(
            ffmpeg_cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout for testing
        )
        return playlist_path
    except Exception:
        return None

def count_oversized_segments(output_dir: str, ts_files: List[str], max_size: int) -> int:
    """Count how many segments exceed the size limit."""
    oversized_count = 0
    for ts_file in ts_files:
        file_path = os.path.join(output_dir, ts_file)
        if os.path.exists(file_path) and os.path.getsize(file_path) > max_size:
            oversized_count += 1
    return oversized_count

def cleanup_test_files(output_dir: str, prefix: str):
    """Clean up test files with given prefix."""
    try:
        for file in os.listdir(output_dir):
            if file.startswith(prefix):
                os.remove(os.path.join(output_dir, file))
    except Exception as e:
        logger.debug(f"Error cleaning up test files: {e}")

def create_segments_with_duration(video_path: str, output_dir: str, duration: int) -> Optional[str]:
    """Create final segments with the optimal duration."""
    logger.info(f"🎬 PHASE 2: Creating segments with optimal duration ({duration}s)")

    playlist_path = os.path.join(output_dir, 'playlist.m3u8')
    segment_pattern = os.path.join(output_dir, 'segment_%04d.ts')

    ffmpeg_cmd = [
        'ffmpeg', '-i', video_path,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-c:v', 'copy',  # Copy mode for quality
        '-c:a', 'copy',
        '-hls_time', str(duration),
        '-hls_list_size', '0',
        '-hls_segment_filename', segment_pattern,
        '-hls_flags', 'delete_segments+append_list',
        '-f', 'hls',
        '-y',
        playlist_path
    ]

    try:
        process = subprocess.run(
            ffmpeg_cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout
        )

        ts_files = [f for f in os.listdir(output_dir) if f.endswith('.ts')]
        logger.info(f"✅ Created {len(ts_files)} segments with {duration}s duration")
        return playlist_path

    except Exception as e:
        logger.error(f"❌ Failed to create segments: {e}")
        return None

def identify_oversized_segments(output_dir: str, ts_files: List[str], max_size: int) -> List[Tuple[str, int, int]]:
    """Identify oversized segments and return (filename, size, segment_number)."""
    oversized = []

    for ts_file in ts_files:
        file_path = os.path.join(output_dir, ts_file)
        file_size = os.path.getsize(file_path)

        if file_size > max_size:
            # Extract segment number from filename: segment_0001.ts -> 1
            try:
                segment_num = int(ts_file.split('_')[1].split('.')[0])
                oversized.append((ts_file, file_size, segment_num))
            except:
                oversized.append((ts_file, file_size, 0))

    return sorted(oversized, key=lambda x: x[2])  # Sort by segment number

def smart_reencode_oversized_segments(video_path: str, output_dir: str, oversized_segments: List[Tuple],
                                    segment_duration: int, max_size: int) -> bool:
    """Re-encode oversized segments with calculated optimal bitrates for each segment."""

    # Calculate the target size with safety margin
    target_size = int(max_size * 0.9)  # 90% of max size for safety

    logger.info(f"🧮 Calculating optimal bitrate for each oversized segment (target: {target_size / (1024*1024):.1f}MB)")

    for segment_file, original_size, segment_num in oversized_segments:
        logger.info(f"Re-encoding {segment_file} (was {original_size / (1024*1024):.1f}MB)")

        # Calculate start time for this specific segment
        start_time = segment_num * segment_duration

        # Calculate optimal bitrate for this specific segment
        # Target size in bits / duration in seconds = bits per second
        target_size_bits = target_size * 8  # Convert to bits
        optimal_bitrate = int(target_size_bits / segment_duration)

        # Set reasonable limits
        min_bitrate = 5 * 1000 * 1000   # 5 Mbps minimum
        max_bitrate = 30 * 1000 * 1000  # 30 Mbps maximum
        optimal_bitrate = max(min_bitrate, min(optimal_bitrate, max_bitrate))

        logger.info(f"  Calculated optimal bitrate: {optimal_bitrate / 1000000:.1f} Mbps for {segment_duration}s segment")

        # Re-encode this specific segment
        segment_path = os.path.join(output_dir, segment_file)
        temp_segment_path = os.path.join(output_dir, f"temp_{segment_file}")

        reencode_cmd = [
            'ffmpeg', '-i', video_path,
            '-ss', str(start_time),  # Start time
            '-t', str(segment_duration),  # Duration
            '-map', '0:v:0',
            '-map', '0:a?',

            # Calculated optimal video encoding
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '22',  # Good quality baseline
            '-maxrate', str(optimal_bitrate),  # Calculated optimal bitrate
            '-bufsize', str(optimal_bitrate),  # Buffer = maxrate

            # Audio
            '-c:a', 'aac',
            '-b:a', '192k',

            '-f', 'mpegts',
            '-y',
            temp_segment_path
        ]

        try:
            logger.debug(f"Re-encode command: {' '.join(reencode_cmd)}")

            process = subprocess.run(
                reencode_cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=1800  # 30 minute timeout per segment
            )

            if os.path.exists(temp_segment_path):
                new_size = os.path.getsize(temp_segment_path)
                size_reduction = ((original_size - new_size) / original_size) * 100

                if new_size <= max_size:
                    # Success - replace original
                    os.replace(temp_segment_path, segment_path)
                    logger.info(f"  ✅ {segment_file}: {original_size / (1024*1024):.1f}MB → {new_size / (1024*1024):.1f}MB ({size_reduction:.1f}% reduction)")
                else:
                    logger.error(f"  ❌ Still too large: {new_size / (1024*1024):.1f}MB")
                    if os.path.exists(temp_segment_path):
                        os.remove(temp_segment_path)
                    return False
            else:
                logger.error(f"  ❌ Re-encoding failed to create output")
                return False

        except Exception as e:
            logger.error(f"  ❌ Re-encoding failed: {e}")
            if os.path.exists(temp_segment_path):
                os.remove(temp_segment_path)
            return False

    logger.info("✅ All oversized segments successfully re-encoded with optimal bitrates")
    return True

def log_final_statistics(output_dir: str, ts_files: List[str], max_chunk_size: int,
                        optimal_duration: int, oversized_count: int):
    """Log final segmentation statistics."""
    if not ts_files:
        logger.error("❌ No segments were created")
        return

    total_size = sum(os.path.getsize(os.path.join(output_dir, f)) for f in ts_files)
    avg_segment_size = total_size / len(ts_files)
    max_segment_size = max((os.path.getsize(os.path.join(output_dir, f)) for f in ts_files), default=0)

    # Count re-encoded segments (temp files that were processed)
    copy_segments = len(ts_files) - oversized_count
    reencode_segments = oversized_count

    logger.info(f"📊 Smart segmentation results:")
    logger.info(f"  🎯 Optimal duration: {optimal_duration}s")
    logger.info(f"  📦 Total segments: {len(ts_files)}")
    logger.info(f"  ✅ Copy mode segments: {copy_segments} ({copy_segments/len(ts_files)*100:.1f}%)")
    logger.info(f"  🎬 Re-encoded segments: {reencode_segments} ({reencode_segments/len(ts_files)*100:.1f}%)")
    logger.info(f"  💾 Total size: {total_size / (1024*1024):.1f} MB")
    logger.info(f"  📊 Average segment: {avg_segment_size / (1024*1024):.1f} MB")
    logger.info(f"  📏 Largest segment: {max_segment_size / (1024*1024):.1f} MB")

    # Final validation
    telegram_bot_limit = 20 * 1024 * 1024
    final_oversized = [f for f in ts_files if os.path.getsize(os.path.join(output_dir, f)) > telegram_bot_limit]

    if final_oversized:
        logger.error(f"❌ CRITICAL: {len(final_oversized)} segments still exceed 20MB limit!")
    else:
        logger.info("✅ All segments are within 20MB bot download limit")
        logger.info(f"🎉 Smart processing complete: {copy_segments} segments preserved, {reencode_segments} optimized")

# Include the existing helper functions
def probe_video_info(video_path: str) -> Tuple[Dict, List[Dict]]:
    """Probe video file to extract detailed information."""
    try:
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(result.stdout)

        format_info = probe_data.get('format', {})
        duration = float(format_info.get('duration', 0))
        bitrate = int(format_info.get('bit_rate', 0))

        streams = probe_data.get('streams', [])
        video_streams = [s for s in streams if s.get('codec_type') == 'video']
        audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
        subtitle_streams = [s for s in streams if s.get('codec_type') == 'subtitle']

        video_info = {
            'duration': duration,
            'bitrate': bitrate,
            'video_streams': len(video_streams),
            'audio_streams': len(audio_streams),
            'subtitle_streams': len(subtitle_streams)
        }

        if video_streams:
            video_stream = video_streams[0]
            width = video_stream.get('width', 0)
            height = video_stream.get('height', 0)
            fps = eval(video_stream.get('avg_frame_rate', '0/1')) if video_stream.get('avg_frame_rate') != '0/1' else 0
            codec = video_stream.get('codec_name', 'unknown')

            video_info.update({
                'width': width,
                'height': height,
                'fps': fps,
                'codec': codec,
                'resolution': f"{width}x{height}"
            })

            logger.info(f"Video details: {width}x{height}, {fps:.2f}fps, codec: {codec}")

        logger.info(f"Found {len(video_streams)} video stream(s), {len(audio_streams)} audio stream(s), {len(subtitle_streams)} subtitle stream(s)")
        logger.info(f"Video probed successfully:")
        logger.info(f"  Duration: {duration:.2f}s")
        logger.info(f"  Bitrate: {bitrate}bps ({bitrate/(1024*1024):.2f} Mbps)")

        subtitle_info = extract_subtitle_info(streams)
        return video_info, subtitle_info

    except Exception as e:
        logger.warning(f"Failed to probe video details: {e}")
        return {'duration': 0, 'bitrate': 0}, []

# Include all the existing subtitle extraction functions
def extract_subtitle_info(streams: List[Dict]) -> List[Dict]:
    """Extract subtitle information from video streams."""
    subtitle_info = []
    for stream in streams:
        if stream.get('codec_type') != 'subtitle':
            continue
        if stream.get('codec_name') in ['none', 'attachment']:
            continue

        tags = stream.get('tags', {})
        subtitle_track = {
            'index': stream.get('index', 0),
            'codec': stream.get('codec_name', 'unknown'),
            'language': tags.get('language', 'und'),
            'title': tags.get('title', ''),
            'default': bool(stream.get('disposition', {}).get('default', 0)),
            'forced': bool(stream.get('disposition', {}).get('forced', 0)),
            'hearing_impaired': 'SDH' in tags.get('title', '').upper() or 'hearing impaired' in tags.get('title', '').lower()
        }
        subtitle_info.append(subtitle_track)
    return subtitle_info

def extract_subtitles_to_files(video_path: str, output_dir: str, subtitle_info: List[Dict]) -> List[str]:
    """Extract subtitle tracks to separate files."""
    extracted_files = []
    for i, sub_info in enumerate(subtitle_info):
        lang = sub_info['language']
        title_part = ""

        if sub_info.get('title'):
            title_clean = "".join(c for c in sub_info['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
            if title_clean and len(title_clean) < 30:
                title_part = f".{title_clean.replace(' ', '_')}"

        if sub_info['forced']:
            lang += '.forced'
        if sub_info['hearing_impaired']:
            lang += '.sdh'

        codec = sub_info['codec']
        if codec in ['ass', 'ssa']:
            ext = 'ass'
        elif codec == 'srt':
            ext = 'srt'
        elif codec == 'webvtt':
            ext = 'vtt'
        elif codec == 'hdmv_pgs_subtitle':
            ext = 'sup'
        elif codec == 'dvd_subtitle':
            ext = 'sub'
        else:
            ext = 'srt'

        subtitle_file = os.path.join(output_dir, f'subtitle_{i:02d}_{lang}{title_part}.{ext}')

        try:
            extract_cmd = [
                'ffmpeg', '-i', video_path,
                '-map', f"0:s:{i}",
            ]

            if codec in ['ass', 'ssa', 'webvtt', 'subrip', 'mov_text']:
                extract_cmd.extend(['-c:s', 'srt'])
                if ext != 'srt':
                    subtitle_file = subtitle_file.replace(f'.{ext}', '.srt')
                    ext = 'srt'
            else:
                extract_cmd.extend(['-c:s', 'copy'])

            extract_cmd.extend(['-y', subtitle_file])

            result = subprocess.run(
                extract_cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=300
            )

            if os.path.exists(subtitle_file) and os.path.getsize(subtitle_file) > 0:
                extracted_files.append(subtitle_file)
                file_size = os.path.getsize(subtitle_file)
                logger.info(f"✅ Extracted subtitle: {os.path.basename(subtitle_file)} ({file_size} bytes)")

        except Exception as e:
            logger.warning(f"⚠️  Failed to extract subtitle {i} ({lang}): {e}")

    logger.info(f"Successfully extracted {len(extracted_files)} subtitle files")
    return extracted_files

# Keep other utility functions as before
def get_video_info(video_path: str) -> dict:
    """Get detailed information about a video file using FFprobe."""
    try:
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except Exception as e:
        logger.error(f"Failed to get video info for {video_path}: {e}")
        return {}

def estimate_processing_time(video_path: str) -> float:
    """Estimate processing time based on video properties."""
    try:
        video_info = get_video_info(video_path)
        format_info = video_info.get('format', {})
        duration = float(format_info.get('duration', 0))
        estimate = duration * 0.2  # 20% for smart approach
        return max(estimate, 60)  # Minimum 1 minute
    except Exception:
        return 300  # Default 5 minutes if estimation fails

def is_mkv_file(file_path: str) -> bool:
    """Check if a file is an MKV file."""
    return file_path.lower().endswith(('.mkv', '.webm'))

def get_file_format_info(file_path: str) -> Dict:
    """Get format-specific information about a video file."""
    try:
        video_info = get_video_info(file_path)
        format_info = video_info.get('format', {})
        streams = video_info.get('streams', [])

        video_streams = len([s for s in streams if s.get('codec_type') == 'video'])
        audio_streams = len([s for s in streams if s.get('codec_type') == 'audio'])
        subtitle_streams = len([s for s in streams if s.get('codec_type') == 'subtitle'])
        attachment_streams = len([s for s in streams if s.get('codec_type') == 'attachment'])

        return {
            'format_name': format_info.get('format_name', 'unknown'),
            'duration': float(format_info.get('duration', 0)),
            'bitrate': int(format_info.get('bit_rate', 0)),
            'file_size': int(format_info.get('size', 0)),
            'stream_counts': {
                'video': video_streams,
                'audio': audio_streams,
                'subtitle': subtitle_streams,
                'attachment': attachment_streams
            },
            'is_complex': subtitle_streams > 5 or attachment_streams > 10 or audio_streams > 3
        }
    except Exception as e:
        logger.error(f"Failed to get format info for {file_path}: {e}")
        return {
            'format_name': 'unknown',
            'duration': 0,
            'bitrate': 0,
            'file_size': 0,
            'stream_counts': {'video': 0, 'audio': 0, 'subtitle': 0, 'attachment': 0},
            'is_complex': False
        }
