"""FFmpeg-based video processor that splits media into separate HLS streams.

For each input file, produces:
  - Video-only HLS segments
  - One HLS audio stream per audio track
  - One WebVTT file per subtitle track
"""

import logging
import os
import shutil
import subprocess

from config import Config
from stream_analyzer import MediaAnalysis

logger = logging.getLogger(__name__)


def _detect_hw_encoder():
    """Detect available hardware encoder."""
    if not Config.ENABLE_HW_ACCEL:
        return None

    encoders = {
        "vaapi": ("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128"]),
        "nvenc": ("h264_nvenc", []),
        "qsv": ("h264_qsv", []),
    }

    preferred = Config.PREFERRED_ENCODER
    if preferred in encoders:
        enc_name, _ = encoders[preferred]
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            if enc_name in result.stdout:
                logger.info("Using hardware encoder: %s", enc_name)
                return encoders[preferred]
        except Exception:
            pass

    return None


def _build_video_cmd(analysis: MediaAnalysis, output_dir: str, hw_encoder):
    """Build FFmpeg command for video-only HLS extraction."""
    video = analysis.video_streams[0]
    segment_pattern = os.path.join(output_dir, "video_%04d.ts")
    playlist = os.path.join(output_dir, "video.m3u8")

    cmd = ["ffmpeg", "-y", "-i", analysis.file_path]

    # Map only the first video stream, no audio, no subtitles
    cmd += ["-map", f"0:{video.index}", "-an", "-sn"]

    # Encoding
    use_copy = Config.ENABLE_COPY_MODE and video.is_copy_compatible
    if use_copy:
        cmd += ["-c:v", "copy"]
        logger.info("Video: using copy mode (codec=%s)", video.codec_name)
    elif hw_encoder:
        enc_name, enc_flags = hw_encoder
        cmd += enc_flags + ["-c:v", enc_name, "-b:v", "4M"]
        logger.info("Video: using hardware encoder %s", enc_name)
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22"]
        logger.info("Video: using software encoder libx264")

    # HLS output
    cmd += [
        "-f", "hls",
        "-hls_time", str(Config.HLS_SEGMENT_DURATION),
        "-hls_list_size", "0",
        "-hls_segment_filename", segment_pattern,
        "-hls_segment_type", "mpegts",
        playlist,
    ]
    return cmd, playlist


def _build_audio_cmd(analysis: MediaAnalysis, audio_stream, audio_index: int, output_dir: str):
    """Build FFmpeg command for a single audio track HLS extraction."""
    audio_dir = os.path.join(output_dir, f"audio_{audio_index}")
    os.makedirs(audio_dir, exist_ok=True)

    segment_pattern = os.path.join(audio_dir, "audio_%04d.ts")
    playlist = os.path.join(audio_dir, "audio.m3u8")

    cmd = ["ffmpeg", "-y", "-i", analysis.file_path]
    cmd += ["-map", f"0:{audio_stream.index}", "-vn", "-sn"]

    # Audio encoding
    use_copy = Config.ENABLE_COPY_MODE and audio_stream.is_copy_compatible
    if use_copy:
        cmd += ["-c:a", "copy"]
        logger.info(
            "Audio track %d (%s): copy mode (codec=%s)",
            audio_index, audio_stream.language, audio_stream.codec_name,
        )
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", str(audio_stream.channels)]
        logger.info(
            "Audio track %d (%s): encoding to AAC",
            audio_index, audio_stream.language,
        )

    cmd += [
        "-f", "hls",
        "-hls_time", str(Config.HLS_SEGMENT_DURATION),
        "-hls_list_size", "0",
        "-hls_segment_filename", segment_pattern,
        "-hls_segment_type", "mpegts",
        playlist,
    ]
    return cmd, playlist, audio_dir


def _extract_subtitle(analysis: MediaAnalysis, sub_stream, sub_index: int, output_dir: str):
    """Extract a single subtitle track to WebVTT."""
    sub_dir = os.path.join(output_dir, f"sub_{sub_index}")
    os.makedirs(sub_dir, exist_ok=True)
    output_file = os.path.join(sub_dir, "subtitles.vtt")

    cmd = [
        "ffmpeg", "-y", "-i", analysis.file_path,
        "-map", f"0:{sub_stream.index}",
        "-c:s", "webvtt",
        output_file,
    ]
    return cmd, output_file, sub_dir


def _run_ffmpeg(cmd, description=""):
    """Run an FFmpeg command with logging."""
    logger.info("Running FFmpeg: %s", description)
    logger.debug("Command: %s", " ".join(cmd))

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=7200,
    )
    if proc.returncode != 0:
        logger.error("FFmpeg failed for %s:\n%s", description, proc.stderr[-2000:])
        raise RuntimeError(f"FFmpeg failed: {description}\n{proc.stderr[-500:]}")
    return proc


class ProcessingResult:
    """Result of processing a single media file."""

    def __init__(self, job_id, output_dir):
        self.job_id = job_id
        self.output_dir = output_dir
        self.video_playlist = None
        self.audio_playlists = []   # list of (playlist_path, audio_dir, language, title, channels)
        self.subtitle_files = []    # list of (vtt_path, sub_dir, language, title)

    def all_segment_dirs(self):
        """Return all directories containing segments to upload."""
        dirs = []
        if self.video_playlist:
            dirs.append(self.output_dir)
        for _, audio_dir, _, _, _ in self.audio_playlists:
            dirs.append(audio_dir)
        for _, sub_dir, _, _ in self.subtitle_files:
            dirs.append(sub_dir)
        return dirs


def process(analysis: MediaAnalysis, job_id: str, progress_callback=None) -> ProcessingResult:
    """Process a media file into separate HLS streams.

    Splits the input into:
      1. Video-only HLS stream
      2. Separate HLS stream per audio track
      3. Separate WebVTT file per subtitle track

    Every audio track is treated independently for multi-audio support.
    """
    output_dir = os.path.join(Config.PROCESSING_DIR, job_id)
    os.makedirs(output_dir, exist_ok=True)

    result = ProcessingResult(job_id, output_dir)
    hw_encoder = _detect_hw_encoder()

    total_steps = (
        (1 if analysis.has_video else 0)
        + len(analysis.audio_streams)
        + len(analysis.subtitle_streams)
    )
    current_step = 0

    def report(step_name):
        nonlocal current_step
        current_step += 1
        if progress_callback:
            progress_callback(current_step, total_steps, step_name)

    # 1. Video stream
    if analysis.has_video:
        cmd, playlist = _build_video_cmd(analysis, output_dir, hw_encoder)
        _run_ffmpeg(cmd, f"video extraction for {job_id}")
        result.video_playlist = playlist
        report("Video extracted")

    # 2. Audio streams - each track gets its own HLS stream
    for i, audio in enumerate(analysis.audio_streams):
        cmd, playlist, audio_dir = _build_audio_cmd(analysis, audio, i, output_dir)
        _run_ffmpeg(cmd, f"audio track {i} ({audio.language}) for {job_id}")
        result.audio_playlists.append((
            playlist, audio_dir, audio.language, audio.title, audio.channels,
        ))
        report(f"Audio track {i} ({audio.language}) extracted")

    # 3. Subtitle streams - extract to WebVTT
    for i, sub in enumerate(analysis.subtitle_streams):
        cmd, vtt_file, sub_dir = _extract_subtitle(analysis, sub, i, output_dir)
        try:
            _run_ffmpeg(cmd, f"subtitle track {i} ({sub.language}) for {job_id}")
            result.subtitle_files.append((vtt_file, sub_dir, sub.language, sub.title))
            report(f"Subtitle track {i} ({sub.language}) extracted")
        except RuntimeError:
            logger.warning("Failed to extract subtitle track %d, skipping", i)
            report(f"Subtitle track {i} skipped")

    logger.info(
        "Processing complete for %s: video=%s, audio=%d tracks, subs=%d tracks",
        job_id,
        "yes" if result.video_playlist else "no",
        len(result.audio_playlists),
        len(result.subtitle_files),
    )
    return result


def cleanup(job_id: str):
    """Remove processing artifacts for a job."""
    output_dir = os.path.join(Config.PROCESSING_DIR, job_id)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
        logger.info("Cleaned up processing dir for %s", job_id)
