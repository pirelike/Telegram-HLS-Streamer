"""HLS playlist manager with multi-audio and multi-subtitle support.

Generates master M3U8 playlists that reference separate video, audio,
and subtitle variant streams per the HLS specification (RFC 8216).

Master playlist structure:
  #EXT-X-MEDIA:TYPE=AUDIO for each audio track
  #EXT-X-MEDIA:TYPE=SUBTITLES for each subtitle track
  #EXT-X-STREAM-INF referencing video + default audio group

Each media playlist (.m3u8) has its segment URIs rewritten to point
to the server's proxy endpoint which fetches from Telegram on-the-fly.
"""

import json
import logging
import os
import re

from config import Config

logger = logging.getLogger(__name__)

# In-memory store of all uploaded jobs
# job_id -> JobManifest
_jobs = {}


class JobManifest:
    """Stores all metadata needed to serve HLS for a job."""

    def __init__(self, job_id, analysis_summary, processing_result, upload_result):
        self.job_id = job_id
        self.analysis = analysis_summary
        self.video_playlist_path = processing_result.video_playlist
        self.audio_tracks = processing_result.audio_playlists
        self.subtitle_tracks = processing_result.subtitle_files
        # segment_key -> {file_id, bot_index}
        self.segment_map = {}
        for key, seg in upload_result.segments.items():
            self.segment_map[key] = {
                "file_id": seg.file_id,
                "bot_index": seg.bot_index,
            }

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "analysis": self.analysis,
            "audio_tracks": [
                {"index": i, "language": lang, "title": title, "channels": ch}
                for i, (_, _, lang, title, ch) in enumerate(self.audio_tracks)
            ],
            "subtitle_tracks": [
                {"index": i, "language": lang, "title": title}
                for i, (_, _, lang, title) in enumerate(self.subtitle_tracks)
            ],
            "segment_count": len(self.segment_map),
        }


def register_job(job_id, analysis, processing_result, upload_result):
    """Register a completed job so it can be served."""
    manifest = JobManifest(job_id, analysis.summary(), processing_result, upload_result)
    _jobs[job_id] = manifest
    # Persist manifest to disk
    _save_manifest(manifest)
    logger.info("Registered job %s with %d segments", job_id, len(manifest.segment_map))
    return manifest


def get_job(job_id) -> JobManifest | None:
    if job_id in _jobs:
        return _jobs[job_id]
    return _load_manifest(job_id)


def list_jobs():
    _load_all_manifests()
    return {jid: j.to_dict() for jid, j in _jobs.items()}


def _manifest_path(job_id):
    return os.path.join(Config.PROCESSING_DIR, f"{job_id}_manifest.json")


def _save_manifest(manifest):
    data = {
        "job_id": manifest.job_id,
        "analysis": manifest.analysis,
        "audio_tracks": [
            {"index": i, "language": lang, "title": title, "channels": ch}
            for i, (_, _, lang, title, ch) in enumerate(manifest.audio_tracks)
        ],
        "subtitle_tracks": [
            {"index": i, "language": lang, "title": title}
            for i, (_, _, lang, title) in enumerate(manifest.subtitle_tracks)
        ],
        "segment_map": manifest.segment_map,
    }
    path = _manifest_path(manifest.job_id)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _load_manifest(job_id):
    path = _manifest_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        manifest = JobManifest.__new__(JobManifest)
        manifest.job_id = data["job_id"]
        manifest.analysis = data["analysis"]
        manifest.audio_tracks = [
            (None, None, t["language"], t["title"], t["channels"])
            for t in data["audio_tracks"]
        ]
        manifest.subtitle_tracks = [
            (None, None, t["language"], t["title"])
            for t in data["subtitle_tracks"]
        ]
        manifest.segment_map = data["segment_map"]
        _jobs[job_id] = manifest
        return manifest
    except Exception as e:
        logger.error("Failed to load manifest for %s: %s", job_id, e)
        return None


def _load_all_manifests():
    if not os.path.exists(Config.PROCESSING_DIR):
        return
    for fname in os.listdir(Config.PROCESSING_DIR):
        if fname.endswith("_manifest.json"):
            job_id = fname.replace("_manifest.json", "")
            if job_id not in _jobs:
                _load_manifest(job_id)


def _parse_segment_names(playlist_path):
    """Parse segment filenames from a .m3u8 playlist file."""
    segments = []
    with open(playlist_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                segments.append(os.path.basename(line))
    return segments


def generate_master_playlist(job_id, base_url):
    """Generate a master M3U8 playlist with multi-audio and subtitle variants.

    The master playlist uses:
      - #EXT-X-MEDIA:TYPE=AUDIO for each audio track
      - #EXT-X-MEDIA:TYPE=SUBTITLES for each subtitle track
      - #EXT-X-STREAM-INF for the video stream, referencing audio/sub groups
    """
    manifest = get_job(job_id)
    if not manifest:
        return None

    lines = ["#EXTM3U", "#EXT-X-VERSION:4"]
    audio_group = "audio"
    sub_group = "subs"

    # Audio tracks
    for i, (_, _, lang, title, channels) in enumerate(manifest.audio_tracks):
        is_default = "YES" if i == 0 else "NO"
        name = title if title else f"Audio {i + 1} ({lang})"
        uri = f"{base_url}/hls/{job_id}/audio_{i}.m3u8"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group}",'
            f'NAME="{name}",LANGUAGE="{lang}",DEFAULT={is_default},'
            f'AUTOSELECT={is_default},CHANNELS="{channels}",URI="{uri}"'
        )

    # Subtitle tracks
    for i, (_, _, lang, title) in enumerate(manifest.subtitle_tracks):
        is_default = "YES" if i == 0 else "NO"
        name = title if title else f"Subtitle {i + 1} ({lang})"
        uri = f"{base_url}/hls/{job_id}/sub_{i}.m3u8"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{sub_group}",'
            f'NAME="{name}",LANGUAGE="{lang}",DEFAULT={is_default},'
            f'AUTOSELECT={is_default},URI="{uri}"'
        )

    # Video stream-inf
    bandwidth = manifest.analysis.get("file_size", 0) * 8
    duration = manifest.analysis.get("duration", 1)
    if duration > 0:
        bandwidth = int(bandwidth / duration)

    stream_inf = f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}"
    if manifest.audio_tracks:
        stream_inf += f',AUDIO="{audio_group}"'
    if manifest.subtitle_tracks:
        stream_inf += f',SUBTITLES="{sub_group}"'

    lines.append(stream_inf)
    lines.append(f"{base_url}/hls/{job_id}/video.m3u8")

    return "\n".join(lines) + "\n"


def generate_media_playlist(job_id, stream_type, stream_index=None):
    """Generate a media-level M3U8 playlist for a specific stream.

    stream_type: "video", "audio", or "sub"
    stream_index: index of audio/subtitle track (ignored for video)
    """
    manifest = get_job(job_id)
    if not manifest:
        return None

    if stream_type == "video":
        prefix = "video"
    elif stream_type == "audio" and stream_index is not None:
        prefix = f"audio_{stream_index}"
    elif stream_type == "sub" and stream_index is not None:
        return _generate_subtitle_playlist(job_id, manifest, stream_index)
    else:
        return None

    # Find all segments for this prefix
    segment_keys = sorted([
        k for k in manifest.segment_map.keys()
        if k.startswith(f"{prefix}/")
    ])

    if not segment_keys:
        return None

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{Config.HLS_SEGMENT_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    for key in segment_keys:
        lines.append(f"#EXTINF:{Config.HLS_SEGMENT_DURATION},")
        lines.append(f"/segment/{job_id}/{key}")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _generate_subtitle_playlist(job_id, manifest, sub_index):
    """Generate a playlist for a subtitle track (single VTT file)."""
    duration = manifest.analysis.get("duration", 0)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{int(duration) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    key = f"sub_{sub_index}/subtitles.vtt"
    if key in manifest.segment_map:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"/segment/{job_id}/{key}")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def get_segment_info(job_id, segment_key):
    """Get Telegram file_id and bot_index for a segment."""
    manifest = get_job(job_id)
    if not manifest:
        return None
    return manifest.segment_map.get(segment_key)
