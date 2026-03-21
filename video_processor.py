"""FFmpeg-based video processor that splits media into separate HLS streams.

For each input file, produces:
  - Video-only HLS segments
  - One HLS audio stream per audio track
  - One WebVTT file per subtitle track
"""

import glob as _glob
import logging
import os
import shutil
import subprocess
import threading

from config import Config
from stream_analyzer import MediaAnalysis

logger = logging.getLogger(__name__)


_hw_encoder_cache = None
_hw_encoder_probed = False


def _detect_vaapi_device():
    """Pick the best available VAAPI render device.

    Scans /dev/dri/renderD* and returns the highest-numbered one, which is
    typically the discrete GPU on systems with both an iGPU and a dGPU.
    Falls back to /dev/dri/renderD128 if nothing is found.
    """
    devices = sorted(_glob.glob("/dev/dri/renderD*"))
    if devices:
        device = devices[-1]  # highest number = dGPU on most multi-GPU systems
        logger.info("Auto-detected VAAPI device: %s", device)
        return device
    logger.warning("No /dev/dri/renderD* devices found, falling back to renderD128")
    return "/dev/dri/renderD128"


def _detect_hw_encoder():
    """Detect available hardware encoder. Result is cached after first probe."""
    global _hw_encoder_cache, _hw_encoder_probed

    if _hw_encoder_probed:
        return _hw_encoder_cache

    _hw_encoder_probed = True

    if not Config.ENABLE_HW_ACCEL:
        _hw_encoder_cache = None
        return None

    encoders = {
        "nvenc": ("h264_nvenc", []),
        "qsv": ("h264_qsv", []),
    }

    preferred = Config.PREFERRED_ENCODER
    if preferred == "vaapi":
        vaapi_device = Config.VAAPI_DEVICE or _detect_vaapi_device()
        encoders["vaapi"] = ("h264_vaapi", ["-vaapi_device", vaapi_device])

    if preferred in encoders:
        enc_name, _ = encoders[preferred]
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            if enc_name in result.stdout:
                logger.info("Using hardware encoder: %s", enc_name)
                _hw_encoder_cache = encoders[preferred]
                return _hw_encoder_cache
        except Exception:
            pass

    _hw_encoder_cache = None
    return None


def _get_tier0_bitrate(source_height):
    """Return the CBR bitrate for tier 0 based on source resolution.

    Picks the highest configured threshold that doesn't exceed the source height.
    Falls back to TIER0_BITRATE_DEFAULT for unlisted resolutions.
    """
    best = None
    for threshold in sorted(Config.TIER0_BITRATES.keys()):
        if threshold <= source_height:
            best = Config.TIER0_BITRATES[threshold]
    return best or Config.TIER0_BITRATE_DEFAULT


def _get_abr_tiers(source_height):
    """Return applicable ABR tiers for the given source resolution.

    Includes tiers whose height is less than or equal to the source height,
    so same-resolution lower-bitrate tiers are produced alongside tier 0.
    """
    if not Config.ABR_ENABLED or source_height <= 0:
        return []

    tiers = []
    for tier in Config.ABR_TIERS:
        if tier["height"] <= source_height:
            tiers.append(tier)
    return tiers


def _parse_bitrate_to_bytes_per_sec(bitrate_str):
    """Convert a bitrate string like '5M' or '1200k' to bytes per second."""
    import re
    bitrate_str = str(bitrate_str).upper()
    match = re.search(r'([\d\.]+)([KMG]?)', bitrate_str)
    if not match:
        return 0
    val = float(match.group(1))
    unit = match.group(2)
    if unit == 'K':
        val *= 1000
    elif unit == 'M':
        val *= 1000000
    elif unit == 'G':
        val *= 1000000000
    return int(val / 8)


def _get_safe_segment_size(bitrate_str):
    """Calculate a safe `-hls_segment_size` from the target and hard limit.

    FFmpeg's `-hls_segment_size` writes until the limit, then splits at the next
    keyframe. Since we force keyframes every 1 second, the overshoot can be up to
    1 second of video plus container overhead. We subtract 1.5 seconds of bitrate
    from the hard Telegram limit, then clamp the configured segment target under it.
    """
    bytes_per_sec = _parse_bitrate_to_bytes_per_sec(bitrate_str)
    # Estimate max overshoot as 1.5 seconds of data (1 sec keyframe interval + 50% overhead margin)
    max_overshoot = int(bytes_per_sec * 1.5)

    # Leave margin under the hard Telegram upload ceiling.
    safe_ceiling = Config.TELEGRAM_MAX_FILE_SIZE - max_overshoot

    # Apply reasonable bounds and clamp the preferred target if needed.
    safe_ceiling = max(1024 * 1024, safe_ceiling)
    return min(Config.SEGMENT_TARGET_SIZE, safe_ceiling)


def _build_video_cmd(analysis: MediaAnalysis, output_dir: str, hw_encoder,
                     tier_index=0, target_height=None, target_bitrate=None,
                     input_override=None):
    """Build FFmpeg command for video-only HLS with CBR encoding.

    All tiers are re-encoded at constant bitrate for predictable segment sizes.
    Uses -hls_segment_size for size-based segmentation and forced keyframes
    every 1 second so the muxer has frequent split points.

    When input_override is given, uses that file/playlist as input instead of
    the original source (used for encoding lower tiers from tier 0 output).
    """
    video = analysis.video_streams[0]
    tier_dir = os.path.join(output_dir, f"video_{tier_index}")
    os.makedirs(tier_dir, exist_ok=True)
    segment_pattern = os.path.join(tier_dir, "video_%04d.ts")
    playlist = os.path.join(tier_dir, "video.m3u8")

    input_path = input_override or analysis.file_path
    cmd = ["ffmpeg", "-y", "-i", input_path]

    # Map only the first video stream, no audio, no subtitles
    cmd += ["-map", "0:v:0", "-an", "-sn"]

    bitrate = target_bitrate or Config.VIDEO_BITRATE

    # CBR encoding — always re-encode, never copy
    if hw_encoder:
        enc_name, enc_flags = hw_encoder
        cmd += enc_flags + ["-c:v", enc_name,
                            "-b:v", bitrate, "-minrate", bitrate,
                            "-maxrate", bitrate, "-bufsize", bitrate]
        if enc_name == "h264_vaapi":
            if target_height:
                cmd += ["-vf", f"format=nv12,hwupload,scale_vaapi=-2:{target_height}"]
            else:
                cmd += ["-vf", "format=nv12,hwupload"]
        elif target_height:
            cmd += ["-vf", f"scale=-2:{target_height}"]
        logger.info(
            "Video tier %d: hardware CBR %s at %s (%s)",
            tier_index, enc_name, bitrate,
            f"{target_height}p" if target_height else "original",
        )
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast",
                "-b:v", bitrate, "-minrate", bitrate,
                "-maxrate", bitrate, "-bufsize", bitrate]
        if target_height:
            cmd += ["-vf", f"scale=-2:{target_height}"]
        logger.info(
            "Video tier %d: libx264 CBR at %s (%s)",
            tier_index, bitrate,
            f"{target_height}p" if target_height else "original",
        )

    # Forced keyframes every 1 second for reliable segment splitting
    cmd += ["-force_key_frames", "expr:gte(t,n_forced*1)"]

    # Calculate safe segment size to prevent overshooting TELEGRAM_MAX_FILE_SIZE
    safe_segment_size = _get_safe_segment_size(bitrate)

    # HLS output with size-based segmentation
    cmd += [
        "-f", "hls",
        "-hls_segment_size", str(safe_segment_size),
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

    # Always encode to AAC for consistent HLS compatibility
    audio_bitrate = Config.AUDIO_BITRATE
    cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    logger.info(
        "Audio track %d (%s): AAC encode at %s",
        audio_index, audio_stream.language, audio_bitrate,
    )

    safe_segment_size = _get_safe_segment_size(audio_bitrate)

    cmd += [
        "-f", "hls",
        "-hls_segment_size", str(safe_segment_size),
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

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
        )
    except subprocess.TimeoutExpired as e:
        logger.error("FFmpeg timed out for %s:\n%s", description, e.stderr[-2000:] if e.stderr else "")
        raise RuntimeError(f"FFmpeg timed out: {description}") from e

    if proc.returncode != 0:
        logger.error("FFmpeg failed for %s:\n%s", description, proc.stderr[-2000:])
        raise RuntimeError(f"FFmpeg failed: {description}\n{proc.stderr[-2000:]}")
    return proc


def _run_ffmpeg_with_progress(cmd, description="", duration_seconds=0, step_progress_cb=None):
    """Run an FFmpeg command, reporting within-step progress via step_progress_cb(pct).

    Falls back to _run_ffmpeg when no callback or duration is provided.
    Uses FFmpeg's -progress pipe:1 to emit progress key=value pairs to stdout.
    """
    if not step_progress_cb or duration_seconds <= 0:
        return _run_ffmpeg(cmd, description)

    logger.info("Running FFmpeg with progress: %s", description)
    logger.debug("Command: %s", " ".join(cmd))

    # Inject -progress pipe:1 as a global option (right after the ffmpeg binary)
    cmd_with_progress = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]

    proc = subprocess.Popen(
        cmd_with_progress,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Cap stderr to last 200 lines to prevent unbounded memory growth
    _STDERR_MAX_LINES = 200
    stderr_chunks = []

    def _read_stderr():
        for line in proc.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > _STDERR_MAX_LINES:
                stderr_chunks.pop(0)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    try:
        for line in proc.stdout:
            if line.startswith("out_time="):
                time_str = line.split("=", 1)[1].strip()
                if time_str.startswith("N/A"):
                    continue
                try:
                    parts = time_str.split(":")
                    secs = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                    if secs >= 0:
                        pct = min(99, int(secs / duration_seconds * 100))
                        step_progress_cb(pct)
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        logger.warning("Error reading FFmpeg progress: %s", e)

    try:
        # Popen doesn't have a direct timeout in wait(), but we can use wait(timeout)
        proc.wait(timeout=7200)
    except subprocess.TimeoutExpired:
        proc.kill()
        stderr_output = "".join(stderr_chunks)
        logger.error("FFmpeg with progress timed out for %s:\n%s", description, stderr_output[-2000:])
        raise RuntimeError(f"FFmpeg timed out: {description}")

    stderr_thread.join(timeout=5)

    if proc.returncode != 0:
        stderr_output = "".join(stderr_chunks)
        logger.error("FFmpeg failed for %s:\n%s", description, stderr_output[-2000:])
        raise RuntimeError(f"FFmpeg failed: {description}\n{stderr_output[-2000:]}")

    return proc


def _parse_segment_durations(playlist_path):
    """Get actual durations for HLS segments in the same directory as a playlist.

    FFmpeg's m3u8 output is unreliable with -hls_segment_size (writes default
    -hls_time instead of actual duration), so we probe each .ts file directly.

    Returns dict mapping filename -> duration (float).
    """
    segment_dir = os.path.dirname(playlist_path)
    durations = {}
    try:
        ts_files = sorted(f for f in os.listdir(segment_dir) if f.endswith(".ts"))
    except OSError as e:
        logger.warning("Failed to list segments in %s: %s", segment_dir, e)
        return durations

    for filename in ts_files:
        filepath = os.path.join(segment_dir, filename)
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                durations[filename] = float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            logger.warning("Failed to probe duration for %s: %s", filepath, e)

    return durations


class ProcessingResult:
    """Result of processing a single media file."""

    def __init__(self, job_id, output_dir):
        self.job_id = job_id
        self.output_dir = output_dir
        self.video_playlists = []   # list of (playlist_path, tier_dir, width, height, bitrate)
        self.audio_playlists = []   # list of (playlist_path, audio_dir, language, title, channels)
        self.subtitle_files = []    # list of (vtt_path, sub_dir, language, title, enum_idx, orig_stream_idx)
        self.segment_durations = {} # maps "video_0/video_0001.ts" -> duration (float)

    @property
    def video_playlist(self):
        """Return the first video playlist path or None."""
        if self.video_playlists:
            return self.video_playlists[0][0]
        return None

    def all_segment_dirs(self):
        """Return all directories containing segments to upload."""
        dirs = []
        for _, tier_dir, _, _, _ in self.video_playlists:
            dirs.append(tier_dir)
        for _, audio_dir, _, _, _ in self.audio_playlists:
            dirs.append(audio_dir)
        for _, sub_dir, _, _, _, _ in self.subtitle_files:
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

    # Determine ABR tiers
    abr_tiers = []
    if analysis.has_video:
        source_height = getattr(analysis.video_streams[0], "height", 0) or 0
        source_width = getattr(analysis.video_streams[0], "width", 0) or 0
        abr_tiers = _get_abr_tiers(source_height)
    media_duration = getattr(analysis, "duration", 0) or 0

    # Total steps: original video + ABR tiers + audio + subtitles
    total_steps = (
        (1 + len(abr_tiers) if analysis.has_video else 0)
        + len(analysis.audio_streams)
        + len(analysis.subtitle_streams)
    )
    current_step = 0

    def report(step_name):
        nonlocal current_step
        current_step += 1
        if progress_callback:
            progress_callback(current_step, total_steps, step_name)

    def make_step_progress_cb(step_label):
        """Create a within-step progress callback for FFmpeg.

        Emits fractional progress so the overall bar moves smoothly during
        each FFmpeg invocation rather than jumping at step completion.
        """
        if not progress_callback or total_steps == 0:
            return None

        def cb(pct):
            fractional = current_step + pct / 100.0
            progress_callback(fractional, total_steps, f"{step_label} ({pct}%)")

        return cb

    # 1. Video streams (tier 0 CBR + ABR tiers from tier 0 output)
    if analysis.has_video:
        # Tier 0: high-quality CBR re-encode at source resolution
        tier0_bitrate = _get_tier0_bitrate(source_height)
        cmd, playlist = _build_video_cmd(
            analysis, output_dir, hw_encoder, tier_index=0,
            target_bitrate=tier0_bitrate,
        )
        _run_ffmpeg_with_progress(
            cmd, f"video tier 0 (original CBR {tier0_bitrate}) for {job_id}",
            duration_seconds=media_duration,
            step_progress_cb=make_step_progress_cb("Encoding video (original)"),
        )
        tier0_playlist = playlist
        tier_dir = os.path.dirname(playlist)
        result.video_playlists.append((
            playlist, tier_dir, source_width, source_height, tier0_bitrate,
        ))
        for filename, dur in _parse_segment_durations(playlist).items():
            result.segment_durations[f"video_0/{filename}"] = dur
        report("Video (original) encoded")

        # Additional ABR tiers — encoded from tier 0 output, not original source
        for ti, tier in enumerate(abr_tiers, start=1):
            target_h = tier["height"]
            # Calculate proportional width (even number)
            target_w = int(source_width * target_h / source_height)
            target_w = target_w + (target_w % 2)  # ensure even
            cmd, playlist = _build_video_cmd(
                analysis, output_dir, hw_encoder,
                tier_index=ti, target_height=target_h, target_bitrate=tier["bitrate"],
                input_override=tier0_playlist,
            )
            _run_ffmpeg_with_progress(
                cmd, f"video tier {ti} ({target_h}p) for {job_id}",
                duration_seconds=media_duration,
                step_progress_cb=make_step_progress_cb(f"Encoding video ({target_h}p)"),
            )
            tier_dir = os.path.dirname(playlist)
            result.video_playlists.append((
                playlist, tier_dir, target_w, target_h, tier["bitrate"],
            ))
            for filename, dur in _parse_segment_durations(playlist).items():
                result.segment_durations[f"video_{ti}/{filename}"] = dur
            report(f"Video ({target_h}p) encoded")

    # 2. Audio streams - each track gets its own HLS stream
    for i, audio in enumerate(analysis.audio_streams):
        cmd, playlist, audio_dir = _build_audio_cmd(analysis, audio, i, output_dir)
        _run_ffmpeg_with_progress(
            cmd, f"audio track {i} ({audio.language}) for {job_id}",
            duration_seconds=media_duration,
            step_progress_cb=make_step_progress_cb(f"Encoding audio {i}"),
        )
        result.audio_playlists.append((
            playlist, audio_dir, audio.language, audio.title, audio.channels,
        ))
        for filename, dur in _parse_segment_durations(playlist).items():
            result.segment_durations[f"audio_{i}/{filename}"] = dur
        report(f"Audio track {i} ({audio.language}) extracted")

    # 3. Subtitle streams - extract text-based subtitles to WebVTT
    #    Skip bitmap formats (dvd_subtitle, hdmv_pgs_subtitle, mov_text, etc.)
    #    which cannot be converted to WebVTT
    for i, sub in enumerate(analysis.subtitle_streams):
        if not sub.is_text_based:
            logger.info(
                "Skipping subtitle track %d (%s): codec %s is not text-based",
                i, sub.language, sub.codec_name,
            )
            report(f"Subtitle track {i} skipped (non-text)")
            continue
        cmd, vtt_file, sub_dir = _extract_subtitle(analysis, sub, i, output_dir)
        try:
            _run_ffmpeg(cmd, f"subtitle track {i} ({sub.language}) for {job_id}")
            result.subtitle_files.append((vtt_file, sub_dir, sub.language, sub.title, i, sub.index))
            report(f"Subtitle track {i} ({sub.language}) extracted")
        except RuntimeError as e:
            logger.warning("Failed to extract subtitle track %d, skipping: %s", i, e)
            # Report failure to user so they know why a track is missing
            report(f"Subtitle track {i} FAILED (Skipped)")

    logger.info(
        "Processing complete for %s: video=%d tiers, audio=%d tracks, subs=%d tracks",
        job_id,
        len(result.video_playlists),
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
