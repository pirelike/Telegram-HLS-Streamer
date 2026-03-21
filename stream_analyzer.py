"""Analyze video files using ffprobe to detect all streams (video, audio, subtitle)."""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def _safe_int(value, default=0):
    """Parse an integer-ish value from ffprobe, tolerating blanks and N/A."""
    try:
        if value in (None, "", "N/A"):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    """Parse a float-ish value from ffprobe, tolerating blanks and N/A."""
    try:
        if value in (None, "", "N/A"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class StreamInfo:
    def __init__(self, index, codec_type, codec_name, language=None, title=None, **extra):
        self.index = index
        self.codec_type = codec_type
        self.codec_name = codec_name
        self.language = language or "und"
        self.title = title or ""
        self.extra = extra

    def __repr__(self):
        return f"<StreamInfo {self.codec_type}:{self.index} codec={self.codec_name} lang={self.language}>"


class AudioStream(StreamInfo):
    def __init__(self, index, codec_name, channels=2, sample_rate=48000, bit_rate=None, **kwargs):
        super().__init__(index, "audio", codec_name, **kwargs)
        self.channels = channels
        self.sample_rate = sample_rate
        self.bit_rate = bit_rate

    @property
    def is_copy_compatible(self):
        return self.codec_name in ("aac", "mp3")


class VideoStream(StreamInfo):
    def __init__(self, index, codec_name, width=0, height=0, bit_rate=None, **kwargs):
        # Normalize h265 → hevc (ffprobe uses "hevc" but some sources report "h265")
        if codec_name == "h265":
            codec_name = "hevc"
        super().__init__(index, "video", codec_name, **kwargs)
        self.width = width
        self.height = height
        self.bit_rate = bit_rate

    @property
    def is_copy_compatible(self):
        return self.codec_name in ("h264", "hevc")


class SubtitleStream(StreamInfo):
    def __init__(self, index, codec_name, **kwargs):
        super().__init__(index, "subtitle", codec_name, **kwargs)

    @property
    def is_text_based(self):
        return self.codec_name in ("subrip", "srt", "ass", "ssa", "webvtt")


class MediaAnalysis:
    """Complete analysis of a media file's streams."""

    def __init__(self, file_path, duration, file_size):
        self.file_path = file_path
        self.duration = duration
        self.file_size = file_size
        self.video_streams: list[VideoStream] = []
        self.audio_streams: list[AudioStream] = []
        self.subtitle_streams: list[SubtitleStream] = []

    @property
    def has_video(self):
        return len(self.video_streams) > 0

    @property
    def has_audio(self):
        return len(self.audio_streams) > 0

    @property
    def has_subtitles(self):
        return len(self.subtitle_streams) > 0

    @property
    def can_copy_video(self):
        return self.has_video and self.video_streams[0].is_copy_compatible

    def summary(self):
        return {
            "file": self.file_path,
            "duration": self.duration,
            "file_size": self.file_size,
            "video_tracks": len(self.video_streams),
            "audio_tracks": len(self.audio_streams),
            "subtitle_tracks": len(self.subtitle_streams),
        }


def analyze(file_path: str) -> MediaAnalysis:
    """Probe a media file and return structured stream information."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        file_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")
        data = json.loads(result.stdout)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found. Install FFmpeg.")
    except json.JSONDecodeError:
        raise RuntimeError(f"ffprobe returned invalid JSON for {file_path}")

    fmt = data.get("format", {})
    duration = _safe_float(fmt.get("duration", 0))
    file_size = _safe_int(fmt.get("size", 0))

    analysis = MediaAnalysis(file_path, duration, file_size)

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        tags = stream.get("tags", {})
        common = {
            "index": stream["index"],
            "codec_name": stream.get("codec_name", "unknown"),
            "language": tags.get("language"),
            "title": tags.get("title"),
        }

        if codec_type == "video":
            # Skip attached pictures (album art)
            if stream.get("disposition", {}).get("attached_pic", 0):
                continue
            vs = VideoStream(
                width=stream.get("width", 0),
                height=stream.get("height", 0),
                bit_rate=stream.get("bit_rate"),
                **common,
            )
            analysis.video_streams.append(vs)

        elif codec_type == "audio":
            aus = AudioStream(
                channels=_safe_int(stream.get("channels", 2), 2),
                sample_rate=_safe_int(stream.get("sample_rate", 48000), 48000),
                bit_rate=stream.get("bit_rate"),
                **common,
            )
            analysis.audio_streams.append(aus)

        elif codec_type == "subtitle":
            ss = SubtitleStream(**common)
            analysis.subtitle_streams.append(ss)

    logger.info(
        "Analyzed %s: %d video, %d audio, %d subtitle streams",
        file_path,
        len(analysis.video_streams),
        len(analysis.audio_streams),
        len(analysis.subtitle_streams),
    )
    return analysis
