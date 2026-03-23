"""FFmpeg-based video processor that splits media into separate HLS streams.

For each input file, produces:
  - Video-only HLS segments
  - One HLS audio stream per audio track
  - One WebVTT file per subtitle track
"""

import concurrent.futures
import glob as _glob
import logging
import os
import shutil
import subprocess
import time
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
    """Detect available hardware encoders for h264 and hevc. Result is cached after first probe.

    Returns a dict {"h264": (enc_name, enc_flags)|None, "hevc": (enc_name, enc_flags)|None}
    or None when hardware acceleration is disabled or no encoders are available.
    """
    global _hw_encoder_cache, _hw_encoder_probed

    if _hw_encoder_probed:
        return _hw_encoder_cache

    _hw_encoder_probed = True

    if not Config.ENABLE_HW_ACCEL:
        _hw_encoder_cache = None
        return None

    preferred = Config.PREFERRED_ENCODER
    if preferred == "vaapi":
        vaapi_device = Config.VAAPI_DEVICE or _detect_vaapi_device()
        enc_flags = ["-vaapi_device", vaapi_device]
        h264_name, hevc_name = "h264_vaapi", "hevc_vaapi"
    elif preferred == "nvenc":
        enc_flags = []
        h264_name, hevc_name = "h264_nvenc", "hevc_nvenc"
    elif preferred == "qsv":
        enc_flags = []
        h264_name, hevc_name = "h264_qsv", "hevc_qsv"
    else:
        _hw_encoder_cache = None
        return None

    result = {"h264": None, "hevc": None}

    if _encoder_list_contains(h264_name) and _probe_hw_encoder(h264_name, enc_flags):
        logger.info("Using hardware h264 encoder: %s", h264_name)
        result["h264"] = (h264_name, enc_flags)

    if _encoder_list_contains(hevc_name) and _probe_hw_encoder(hevc_name, enc_flags):
        logger.info("Using hardware hevc encoder: %s", hevc_name)
        result["hevc"] = (hevc_name, enc_flags)

    if result["h264"] or result["hevc"]:
        _hw_encoder_cache = result
    else:
        _hw_encoder_cache = None

    return _hw_encoder_cache


def _encoder_list_contains(enc_name):
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Failed to list FFmpeg encoders while probing %s: %s", enc_name, exc)
        return False
    return result.returncode == 0 and enc_name in result.stdout


def _probe_hw_encoder(enc_name, enc_flags):
    probe_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=128x128:d=0.1",
        "-frames:v",
        "1",
    ]
    probe_cmd += enc_flags
    if enc_name in ("h264_vaapi", "hevc_vaapi"):
        probe_cmd += ["-vf", "format=nv12,hwupload"]
    probe_cmd += ["-an", "-c:v", enc_name, "-f", "null", "-"]

    try:
        result = subprocess.run(
            probe_cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        logger.warning("Hardware encoder probe failed for %s: %s", enc_name, exc)
        return False

    if result.returncode != 0:
        logger.warning(
            "Hardware encoder %s failed probe encode and will be disabled: %s",
            enc_name,
            (result.stderr or "").strip()[-400:],
        )
        return False
    return True


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


def _double_bitrate(bitrate_str):
    """Return a bitrate string doubled in value (e.g. '30M' -> '60M', '128k' -> '256k').

    Used to set -bufsize to 2x the target bitrate so the rate controller has
    enough headroom to smooth out instantaneous spikes without allowing the encoder
    to run arbitrarily over the CBR target.
    """
    import re as _re
    m = _re.match(r'^([\d\.]+)([kKmMgG]?)$', str(bitrate_str).strip())
    if not m:
        return bitrate_str
    val = float(m.group(1)) * 2
    suffix = m.group(2)
    # Keep integer representation when possible
    if val == int(val):
        return f"{int(val)}{suffix}"
    return f"{val}{suffix}"


def _get_safe_segment_size(bitrate_str):
    """Calculate a safe `-hls_segment_size` from the target and hard limit.

    FFmpeg's `-hls_segment_size` writes until the limit, then splits at the next
    keyframe. Since we force keyframes every 1 second, the overshoot can be up to
    1 second of video plus container overhead. With CBR encoding and bufsize=2*bitrate
    the instantaneous rate can spike to ~2x the target; we use a 3x multiplier to
    account for bitrate variance, I-frame size spikes, and muxing overhead.
    """
    bytes_per_sec = _parse_bitrate_to_bytes_per_sec(bitrate_str)
    # 3x margin: 1 sec keyframe interval * peak rate (~2x nominal) + overhead
    max_overshoot = int(bytes_per_sec * 3.0)

    # Leave margin under the hard Telegram upload ceiling.
    safe_ceiling = Config.TELEGRAM_MAX_FILE_SIZE - max_overshoot

    # Apply reasonable bounds and clamp the preferred target if needed.
    safe_ceiling = max(1024 * 1024, safe_ceiling)
    return min(Config.SEGMENT_TARGET_SIZE, safe_ceiling)


def _build_video_cmd(analysis: MediaAnalysis, output_dir: str, hw_encoder,
                     tier_index=0, target_height=None, target_bitrate=None,
                     input_override=None, allow_copy=False):
    """Build FFmpeg command for video-only HLS.

    When allow_copy=True and the source is copy-compatible (h264/hevc), passes
    through the video bitstream unchanged (-c:v copy). Oversized segments must
    be handled by the caller via _reencode_oversized_segment.

    Otherwise encodes at constant bitrate (CBR) with forced 1-second keyframes
    for predictable segment sizes. hw_encoder is a dict {"h264": ..., "hevc": ...}
    or None; the h264 entry is used for re-encode paths.

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

    if allow_copy and analysis.can_copy_video:
        # Passthrough: no re-encode, no keyframe injection, no scaling
        cmd += ["-c:v", "copy"]
        source_br = getattr(video, "bit_rate", None)
        bitrate_for_size = str(source_br) if source_br else Config.VIDEO_BITRATE
        safe_segment_size = _get_safe_segment_size(bitrate_for_size)
        logger.info("Video tier %d: copy mode (passthrough)", tier_index)
    else:
        bitrate = target_bitrate or Config.VIDEO_BITRATE

        h264_hw = hw_encoder.get("h264") if isinstance(hw_encoder, dict) else None
        if h264_hw:
            enc_name, enc_flags = h264_hw
            cmd += enc_flags + ["-c:v", enc_name,
                                "-b:v", bitrate, "-minrate", bitrate,
                                "-maxrate", bitrate, "-bufsize", _double_bitrate(bitrate)]
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
                    "-maxrate", bitrate, "-bufsize", _double_bitrate(bitrate)]
            if target_height:
                cmd += ["-vf", f"scale=-2:{target_height}"]
            logger.info(
                "Video tier %d: libx264 CBR at %s (%s)",
                tier_index, bitrate,
                f"{target_height}p" if target_height else "original",
            )

        # Forced keyframes every 1 second for reliable segment splitting
        cmd += ["-force_key_frames", "expr:gte(t,n_forced*1)"]
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

    if audio_stream.is_copy_compatible:
        cmd += ["-c:a", "copy"]
        source_br = getattr(audio_stream, "bit_rate", None)
        bitrate_for_size = str(source_br) if source_br else Config.AUDIO_BITRATE
        logger.info("Audio track %d (%s): copy mode (passthrough)", audio_index, audio_stream.language)
    else:
        audio_bitrate = Config.AUDIO_BITRATE
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
        bitrate_for_size = audio_bitrate
        logger.info(
            "Audio track %d (%s): AAC encode at %s",
            audio_index, audio_stream.language, audio_bitrate,
        )

    safe_segment_size = _get_safe_segment_size(bitrate_for_size)

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


def _stop_ffmpeg_process(proc, description):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    raise RuntimeError(f"FFmpeg cancelled: {description}")


def _run_ffmpeg(cmd, description="", cancel_event=None, on_process_start=None, on_process_end=None):
    """Run an FFmpeg command with logging."""
    logger.info("Running FFmpeg: %s", description)
    logger.debug("Command: %s", " ".join(cmd))

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if on_process_start:
            on_process_start(proc)
        deadline = time.time() + 7200
        while True:
            if cancel_event and cancel_event.is_set():
                _stop_ffmpeg_process(proc, description)
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError(f"FFmpeg timed out: {description}")
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired as e:
        logger.error("FFmpeg timed out for %s:\n%s", description, e.stderr[-2000:] if e.stderr else "")
        raise RuntimeError(f"FFmpeg timed out: {description}") from e
    finally:
        if proc is not None and on_process_end:
            on_process_end(proc)

    if proc.returncode != 0:
        logger.error("FFmpeg failed for %s:\n%s", description, stderr[-2000:])
        raise RuntimeError(f"FFmpeg failed: {description}\n{stderr[-2000:]}")
    return proc


def _run_ffmpeg_with_progress(
    cmd,
    description="",
    duration_seconds=0,
    step_progress_cb=None,
    cancel_event=None,
    on_process_start=None,
    on_process_end=None,
):
    """Run an FFmpeg command, reporting within-step progress via step_progress_cb(pct).

    Falls back to _run_ffmpeg when no callback or duration is provided.
    Uses FFmpeg's -progress pipe:1 to emit progress key=value pairs to stdout.
    """
    if not step_progress_cb or duration_seconds <= 0:
        return _run_ffmpeg(
            cmd,
            description,
            cancel_event=cancel_event,
            on_process_start=on_process_start,
            on_process_end=on_process_end,
        )

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
    if on_process_start:
        on_process_start(proc)

    # Cap stderr to last 200 lines to prevent unbounded memory growth
    _STDERR_MAX_LINES = 200
    stderr_chunks = []
    stdout_exception = []

    def _read_stderr():
        for line in proc.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > _STDERR_MAX_LINES:
                stderr_chunks.pop(0)

    def _read_stdout():
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
        except Exception as exc:
            stdout_exception.append(exc)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
    stderr_thread.start()
    stdout_thread.start()

    try:
        deadline = time.time() + 7200
        while True:
            if cancel_event and cancel_event.is_set():
                _stop_ffmpeg_process(proc, description)
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                proc.kill()
                stderr_output = "".join(stderr_chunks)
                logger.error("FFmpeg with progress timed out for %s:\n%s", description, stderr_output[-2000:])
                raise RuntimeError(f"FFmpeg timed out: {description}")
            time.sleep(0.1)
    finally:
        stderr_thread.join(timeout=5)
        stdout_thread.join(timeout=5)
        if on_process_end:
            on_process_end(proc)

    if stdout_exception:
        logger.warning("Error reading FFmpeg progress: %s", stdout_exception[0])

    if proc.returncode != 0:
        stderr_output = "".join(stderr_chunks)
        logger.error("FFmpeg failed for %s:\n%s", description, stderr_output[-2000:])
        raise RuntimeError(f"FFmpeg failed: {description}\n{stderr_output[-2000:]}")

    return proc


def _probe_segment_duration(filepath):
    """Run ffprobe on a single .ts file and return its duration, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        logger.warning("Failed to probe duration for %s: %s", filepath, e)
    return None


def _parse_segment_durations(playlist_path):
    """Get actual durations for HLS segments in the same directory as a playlist.

    FFmpeg's m3u8 output is unreliable with -hls_segment_size (writes default
    -hls_time instead of actual duration), so we probe each .ts file directly.
    Probing is done in parallel for speed.

    Returns dict mapping filename -> duration (float).
    """
    segment_dir = os.path.dirname(playlist_path)
    durations = {}
    try:
        ts_files = sorted(f for f in os.listdir(segment_dir) if f.endswith(".ts"))
    except OSError as e:
        logger.warning("Failed to list segments in %s: %s", segment_dir, e)
        return durations

    if not ts_files:
        return durations

    max_workers = min(len(ts_files), 8)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(_probe_segment_duration, os.path.join(segment_dir, f)): f
            for f in ts_files
        }
        for future in concurrent.futures.as_completed(future_to_name):
            filename = future_to_name[future]
            try:
                dur = future.result()
                if dur is not None:
                    durations[filename] = dur
            except Exception as e:
                logger.warning("Failed to probe duration for %s: %s", filename, e)

    return durations


def _check_segment_sizes(segment_dir):
    """Warn about any .ts segments that exceed the Telegram upload limit.

    Returns a list of (filename, size) tuples for oversized files.
    Does not raise — the uploader will catch hard violations; this is for early
    visibility in the logs.
    """
    oversized = []
    try:
        for filename in sorted(os.listdir(segment_dir)):
            if not filename.endswith(".ts"):
                continue
            filepath = os.path.join(segment_dir, filename)
            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue
            if size > Config.TELEGRAM_MAX_FILE_SIZE:
                oversized.append((filename, size))
                logger.warning(
                    "Segment %s is %d bytes — exceeds Telegram limit of %d bytes",
                    filepath, size, Config.TELEGRAM_MAX_FILE_SIZE,
                )
    except OSError as e:
        logger.warning("Could not scan segment dir %s: %s", segment_dir, e)
    return oversized


def _reencode_oversized_segment(segment_path, duration, hw_encoders, source_codec):
    """Re-encode a single oversized .ts segment in-place to fit within TELEGRAM_MAX_FILE_SIZE.

    source_codec: "h264" or "hevc" — selects which hw encoder to prefer.
    hw_encoders: dict from _detect_hw_encoder, e.g. {"h264": (...), "hevc": (...)} or None.
    """
    if duration is None or duration <= 0:
        logger.warning("Cannot re-encode segment with unknown duration: %s", segment_path)
        return

    # Target bitrate with 0.85 safety margin so the re-encoded file fits
    target_bps = int((Config.TELEGRAM_MAX_FILE_SIZE * 8 * 0.85) / duration)

    codec_hw = hw_encoders.get(source_codec) if hw_encoders else None
    if codec_hw:
        enc_name, enc_flags = codec_hw
    elif source_codec == "hevc":
        enc_name, enc_flags = "libx265", []
    else:
        enc_name, enc_flags = "libx264", []

    target_str = str(target_bps)
    bufsize_str = str(target_bps * 2)

    temp_path = segment_path + ".tmp.ts"
    cmd = ["ffmpeg", "-y", "-i", segment_path]
    cmd += enc_flags
    cmd += ["-c:v", enc_name,
            "-b:v", target_str, "-maxrate", target_str, "-bufsize", bufsize_str]
    if enc_name.endswith("_vaapi"):
        cmd += ["-vf", "format=nv12,hwupload"]
    cmd += ["-c:a", "copy", "-f", "mpegts", temp_path]

    logger.info(
        "Re-encoding oversized segment %s (%.1f MB) — target bitrate %d kbps",
        os.path.basename(segment_path),
        os.path.getsize(segment_path) / 1024 / 1024,
        target_bps // 1000,
    )
    try:
        _run_ffmpeg(cmd, f"re-encode oversized segment {os.path.basename(segment_path)}")
    except RuntimeError:
        logger.error("Failed to re-encode oversized segment: %s", segment_path)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return

    os.replace(temp_path, segment_path)

    final_size = os.path.getsize(segment_path)
    if final_size > Config.TELEGRAM_MAX_FILE_SIZE:
        logger.warning(
            "Segment still oversized after re-encode: %s (%d bytes)",
            os.path.basename(segment_path), final_size,
        )


class ProcessingResult:
    """Result of processing a single media file."""

    def __init__(self, job_id, output_dir):
        self.job_id = job_id
        self.output_dir = output_dir
        self.video_playlists = []   # list of (playlist_path, tier_dir, width, height, bitrate)
        self.audio_playlists = []   # list of (playlist_path, audio_dir, language, title, channels)
        self.subtitle_files = []    # list of (vtt_path, sub_dir, language, title, enum_idx, orig_stream_idx)
        self.segment_durations = {} # maps "video_0/video_0001.ts" -> duration (float)
        self.thumbnail_path = None  # path to thumbnail.jpg or None

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


def extract_thumbnail(file_path, output_dir):
    """Extract a thumbnail image from a video file.

    Seeks to 10% of the video duration (minimum 2 seconds) and captures
    one frame as a JPEG. Returns the path to the thumbnail or None on failure.
    """
    thumb_dir = os.path.join(output_dir, "thumbnail")
    os.makedirs(thumb_dir, exist_ok=True)
    output_path = os.path.join(thumb_dir, "thumbnail.jpg")

    # Probe duration first
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            seek_time = 2.0
        else:
            duration = float(probe.stdout.strip())
            seek_time = max(2.0, duration * 0.1)
    except Exception:
        seek_time = 2.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_time),
        "-i", file_path,
        "-vframes", "1",
        "-q:v", "5",
        "-vf", "scale='min(640,iw)':-2",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and os.path.exists(output_path):
            logger.info("Thumbnail extracted: %s", output_path)
            return output_path
        logger.warning("Thumbnail extraction failed (non-fatal): %s", result.stderr[-500:])
    except Exception as exc:
        logger.warning("Thumbnail extraction failed (non-fatal): %s", exc)
    return None


def _raise_if_cancelled(cancel_event, description):
    if cancel_event and cancel_event.is_set():
        raise RuntimeError(description)


def process(
    analysis: MediaAnalysis,
    job_id: str,
    progress_callback=None,
    cancel_event=None,
    on_process_start=None,
    on_process_end=None,
    on_stream_encoded=None,
) -> ProcessingResult:
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

    # Determine ABR tiers and copy mode
    abr_tiers = []
    use_copy_mode = False
    if analysis.has_video:
        source_height = getattr(analysis.video_streams[0], "height", 0) or 0
        source_width = getattr(analysis.video_streams[0], "width", 0) or 0
        use_copy_mode = analysis.can_copy_video and Config.ENABLE_COPY_MODE
        abr_tiers = _get_abr_tiers(source_height)
    media_duration = getattr(analysis, "duration", 0) or 0

    # Total steps: 1 tier 0 + ABR tiers + audio + subtitles
    if analysis.has_video:
        num_video_steps = 1 + len(abr_tiers)
    else:
        num_video_steps = 0
    total_steps = (
        num_video_steps
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

    # 1. Video streams
    if analysis.has_video:
        _raise_if_cancelled(cancel_event, f"Processing cancelled: {job_id}")

        tier0_bitrate = _get_tier0_bitrate(source_height)

        # Tier 0 + all applicable ABR tiers (copy mode only affects tier 0 encoding)
        tier_descriptors = [(0, None, source_width, source_height, tier0_bitrate, "original")]
        for ti, tier in enumerate(abr_tiers, start=1):
            target_h = tier["height"]
            target_w = int(source_width * target_h / source_height)
            target_w = target_w + (target_w % 2)  # ensure even
            tier_descriptors.append((ti, target_h, target_w, target_h, tier["bitrate"], f"{target_h}p"))

        num_video_tiers = len(tier_descriptors)

        # Aggregate per-tier progress to produce a single smooth value.
        # Each tier independently reports 0-100; we average all tiers and map
        # that aggregate percentage onto the [0, num_video_tiers] step range so
        # the overall bar advances monotonically even when tiers run in parallel.
        _tier_progress_lock = threading.Lock()
        _tier_progress = {td[0]: 0 for td in tier_descriptors}  # tier_index -> 0-100

        def _encode_tier(tier_index, target_height, width, height, bitrate, label, allow_copy=False):
            """Encode a single video tier; returns collected data for assembly."""
            _raise_if_cancelled(cancel_event, f"Processing cancelled: {job_id}")
            cmd, playlist = _build_video_cmd(
                analysis, output_dir, hw_encoder,
                tier_index=tier_index, target_height=target_height,
                target_bitrate=bitrate, allow_copy=allow_copy,
            )
            def step_cb(pct):
                if not progress_callback or total_steps == 0:
                    return
                with _tier_progress_lock:
                    _tier_progress[tier_index] = pct
                    aggregate_pct = sum(_tier_progress.values()) / num_video_tiers
                fractional = aggregate_pct / 100.0 * num_video_tiers
                progress_callback(fractional, total_steps, f"Encoding video ({int(aggregate_pct)}%)")

            _run_ffmpeg_with_progress(
                cmd, f"video tier {tier_index} ({label}) for {job_id}",
                duration_seconds=media_duration,
                step_progress_cb=step_cb,
                cancel_event=cancel_event,
                on_process_start=on_process_start,
                on_process_end=on_process_end,
            )
            tier_dir = os.path.dirname(playlist)
            _check_segment_sizes(tier_dir)
            seg_durations = _parse_segment_durations(playlist)
            return (tier_index, playlist, tier_dir, width, height, bitrate, seg_durations, label)

        tier_results = {}  # tier_index -> result tuple

        if use_copy_mode:
            # Run tier 0 with copy passthrough
            tier_0_result = _encode_tier(
                0, None, source_width, source_height, tier0_bitrate, "original",
                allow_copy=True,
            )
            tier_results[0] = tier_0_result

            # Re-encode any segments that still exceed Telegram's file size limit
            _, _, tier_dir, _, _, _, seg_durations, _ = tier_0_result
            oversized = _check_segment_sizes(tier_dir)
            if oversized:
                source_codec = analysis.video_streams[0].codec_name
                for seg_file, _seg_size in oversized:
                    seg_path = os.path.join(tier_dir, seg_file)
                    seg_duration = seg_durations.get(seg_file)
                    _reencode_oversized_segment(seg_path, seg_duration, hw_encoder, source_codec)

            if on_stream_encoded:
                ts_files = [
                    (f"video_0/{fn}", os.path.join(tier_dir, fn))
                    for fn in sorted(os.listdir(tier_dir))
                    if fn.endswith(".ts")
                ]
                on_stream_encoded("video", 0, ts_files)

        abr_tier_descriptors = [td for td in tier_descriptors if td[0] > 0]
        if not use_copy_mode:
            # Normal mode: encode tier 0 + all ABR tiers in parallel
            abr_tier_descriptors = tier_descriptors

        if abr_tier_descriptors:
            failed_exc = None
            max_workers = min(len(abr_tier_descriptors), Config.MAX_PARALLEL_ENCODES)

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
                    executor.submit(_encode_tier, *td): td[0]
                    for td in abr_tier_descriptors
                }
                for future in concurrent.futures.as_completed(future_to_idx):
                    if failed_exc is not None:
                        future.cancel()
                        continue
                    try:
                        tier_result = future.result()
                        tier_results[tier_result[0]] = tier_result
                        if on_stream_encoded:
                            ti, _, tier_dir, _, _, _, _, _ = tier_result
                            ts_files = [
                                (f"video_{ti}/{fn}", os.path.join(tier_dir, fn))
                                for fn in sorted(os.listdir(tier_dir))
                                if fn.endswith(".ts")
                            ]
                            on_stream_encoded("video", ti, ts_files)
                    except Exception as exc:
                        failed_exc = exc
                        if cancel_event:
                            cancel_event.set()

            if failed_exc is not None:
                raise failed_exc

        # Assemble results in tier order to keep video_playlists sorted by quality
        for ti in range(num_video_tiers):
            _, playlist, tier_dir, width, height, bitrate, seg_durations, label = tier_results[ti]
            result.video_playlists.append((playlist, tier_dir, width, height, bitrate))
            for filename, dur in seg_durations.items():
                result.segment_durations[f"video_{ti}/{filename}"] = dur

        # Advance current_step past all video tiers for subsequent audio/subtitle steps.
        # Fire a single callback at the exact boundary so audio steps index correctly.
        current_step = num_video_tiers
        if progress_callback and total_steps > 0:
            progress_callback(current_step, total_steps, "Video encoding complete")

    # 2. Audio streams - each track gets its own HLS stream
    for i, audio in enumerate(analysis.audio_streams):
        _raise_if_cancelled(cancel_event, f"Processing cancelled: {job_id}")
        cmd, playlist, audio_dir = _build_audio_cmd(analysis, audio, i, output_dir)
        _run_ffmpeg_with_progress(
            cmd, f"audio track {i} ({audio.language}) for {job_id}",
            duration_seconds=media_duration,
            step_progress_cb=make_step_progress_cb(f"Encoding audio {i}"),
            cancel_event=cancel_event,
            on_process_start=on_process_start,
            on_process_end=on_process_end,
        )
        _check_segment_sizes(audio_dir)
        result.audio_playlists.append((
            playlist, audio_dir, audio.language, audio.title, audio.channels,
        ))
        for filename, dur in _parse_segment_durations(playlist).items():
            result.segment_durations[f"audio_{i}/{filename}"] = dur
        if on_stream_encoded:
            ts_files = [
                (f"audio_{i}/{fn}", os.path.join(audio_dir, fn))
                for fn in sorted(os.listdir(audio_dir))
                if fn.endswith(".ts")
            ]
            on_stream_encoded("audio", i, ts_files)
        report(f"Audio track {i} ({audio.language}) extracted")

    # 3. Subtitle streams - extract text-based subtitles to WebVTT
    #    Skip bitmap formats (dvd_subtitle, hdmv_pgs_subtitle, etc.)
    #    which cannot be converted to WebVTT
    for i, sub in enumerate(analysis.subtitle_streams):
        _raise_if_cancelled(cancel_event, f"Processing cancelled: {job_id}")
        if not sub.is_text_based:
            logger.info(
                "Skipping subtitle track %d (%s): codec %s is not text-based",
                i, sub.language, sub.codec_name,
            )
            report(f"Subtitle track {i} skipped (non-text)")
            continue
        cmd, vtt_file, sub_dir = _extract_subtitle(analysis, sub, i, output_dir)
        try:
            _run_ffmpeg(
                cmd,
                f"subtitle track {i} ({sub.language}) for {job_id}",
                cancel_event=cancel_event,
                on_process_start=on_process_start,
                on_process_end=on_process_end,
            )
            result.subtitle_files.append((vtt_file, sub_dir, sub.language, sub.title, i, sub.index))
            if on_stream_encoded:
                on_stream_encoded("subtitle", i, [(f"sub_{i}/subtitles.vtt", vtt_file)])
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

    # Extract thumbnail (non-fatal — failure does not abort the job)
    if analysis.has_video:
        result.thumbnail_path = extract_thumbnail(analysis.file_path, output_dir)
        if result.thumbnail_path and on_stream_encoded:
            on_stream_encoded("thumbnail", 0, [("thumbnail/thumbnail.jpg", result.thumbnail_path)])

    return result


def cleanup(job_id: str):
    """Remove processing artifacts for a job."""
    output_dir = os.path.join(Config.PROCESSING_DIR, job_id)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
        logger.info("Cleaned up processing dir for %s", job_id)
