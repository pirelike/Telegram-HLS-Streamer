import os
import json
import subprocess
from logger_config import logger

def split_video_to_hls(video_path: str, output_dir: str, max_chunk_size: int = 20 * 1024 * 1024) -> str:
    """
    Splits a video file into HLS segments using FFmpeg.

    This function probes the video to determine its properties and then uses FFmpeg
    to split it into `.ts` segments and generate a `.m3u8` playlist.

    Args:
        video_path (str): The path to the source video file.
        output_dir (str): The directory where the HLS segments and playlist will be saved.
        max_chunk_size (int): The maximum size for each segment in bytes.

    Returns:
        str: The path to the generated `.m3u8` playlist file.

    Raises:
        FileNotFoundError: If the video_path does not exist.
        subprocess.CalledProcessError: If the FFmpeg command fails.
    """
    if not os.path.exists(video_path):
        logger.error(f"Video file not found: {video_path}")
        raise FileNotFoundError(f"Video file not found: {video_path}")

    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Created output directory: {output_dir}")

    try:
        probe_cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', video_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        video_info = json.loads(result.stdout)
        duration = float(video_info['format'].get('duration', 0))
        bitrate = int(video_info['format'].get('bit_rate', 0))

        segment_duration = (max_chunk_size * 8) / bitrate if bitrate > 0 else 30
        segment_duration = min(max(10, segment_duration * 0.8), 50)

        logger.info(f"Video probed successfully: duration={duration}s, bitrate={bitrate}bps, segment_duration={segment_duration}s")

    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to probe video details: {e}. Using default segment duration.", exc_info=True)
        segment_duration = 30

    playlist_path = os.path.join(output_dir, 'playlist.m3u8')
    segment_pattern = os.path.join(output_dir, 'segment_%04d.ts')

    ffmpeg_cmd = [
        'ffmpeg', '-i', video_path, '-c', 'copy', '-hls_time', str(segment_duration),
        '-hls_list_size', '0', '-hls_segment_filename', segment_pattern, '-f', 'hls', '-y', playlist_path
    ]

    try:
        logger.info("Starting video segmentation with FFmpeg...")
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True, timeout=3600)
        logger.info(f"✅ Video successfully split into HLS segments in {output_dir}")
        return playlist_path
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg failed with error: {e.stderr}", exc_info=True)
        raise
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg process timed out after 1 hour.")
        raise
