"""HLS playlist manager with multi-audio and multi-subtitle support.

Generates master M3U8 playlists that reference separate video, audio,
and subtitle variant streams per the HLS specification (RFC 8216).

All job/segment data is stored in SQLite via the database module.
"""

import logging

import database as db
from config import Config

logger = logging.getLogger(__name__)


def register_job(job_id, analysis, processing_result, upload_result):
    """Persist a completed job to the database."""
    db.save_job(job_id, analysis, processing_result, upload_result)
    logger.info("Registered job %s", job_id)


def get_job(job_id):
    """Get job metadata from database."""
    return db.get_job(job_id)


def list_jobs():
    """List all jobs with summary info."""
    jobs = db.list_jobs()
    result = {}
    for j in jobs:
        job_id = j["job_id"]
        audio_tracks = db.get_job_tracks(job_id, "audio")
        subtitle_tracks = db.get_job_tracks(job_id, "subtitle")
        result[job_id] = {
            "job_id": job_id,
            "filename": j["filename"],
            "duration": j["duration"],
            "audio_tracks": [
                {"index": t["track_index"], "language": t["language"],
                 "title": t["title"], "channels": t["channels"]}
                for t in audio_tracks
            ],
            "subtitle_tracks": [
                {"index": t["track_index"], "language": t["language"],
                 "title": t["title"]}
                for t in subtitle_tracks
            ],
            "segment_count": j["segment_count"],
        }
    return result


def get_segment_info(job_id, segment_key):
    """Get Telegram file_id and bot_index for a segment from database."""
    return db.get_segment(job_id, segment_key)


def generate_master_playlist(job_id, base_url):
    """Generate a master M3U8 playlist with multi-audio and subtitle variants."""
    job = db.get_job(job_id)
    if not job:
        return None

    audio_tracks = db.get_job_tracks(job_id, "audio")
    subtitle_tracks = db.get_job_tracks(job_id, "subtitle")

    lines = ["#EXTM3U", "#EXT-X-VERSION:4"]
    audio_group = "audio"
    sub_group = "subs"

    # Audio tracks
    for t in audio_tracks:
        i = t["track_index"]
        is_default = "YES" if i == 0 else "NO"
        name = t["title"] if t["title"] else f"Audio {i + 1} ({t['language']})"
        uri = f"{base_url}/hls/{job_id}/audio_{i}.m3u8"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group}",'
            f'NAME="{name}",LANGUAGE="{t["language"]}",DEFAULT={is_default},'
            f'AUTOSELECT={is_default},CHANNELS="{t["channels"]}",URI="{uri}"'
        )

    # Subtitle tracks
    for t in subtitle_tracks:
        i = t["track_index"]
        is_default = "YES" if i == 0 else "NO"
        name = t["title"] if t["title"] else f"Subtitle {i + 1} ({t['language']})"
        uri = f"{base_url}/hls/{job_id}/sub_{i}.m3u8"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{sub_group}",'
            f'NAME="{name}",LANGUAGE="{t["language"]}",DEFAULT={is_default},'
            f'AUTOSELECT={is_default},URI="{uri}"'
        )

    # Video stream-inf with estimated bandwidth
    bandwidth = job["file_size"] * 8
    duration = job["duration"] or 1
    if duration > 0:
        bandwidth = int(bandwidth / duration)

    stream_inf = f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}"
    if job["video_width"] and job["video_height"]:
        stream_inf += f',RESOLUTION={job["video_width"]}x{job["video_height"]}'
    if audio_tracks:
        stream_inf += f',AUDIO="{audio_group}"'
    if subtitle_tracks:
        stream_inf += f',SUBTITLES="{sub_group}"'

    lines.append(stream_inf)
    lines.append(f"{base_url}/hls/{job_id}/video.m3u8")

    return "\n".join(lines) + "\n"


def generate_media_playlist(job_id, stream_type, stream_index=None):
    """Generate a media-level M3U8 playlist for a specific stream.

    stream_type: "video", "audio", or "sub"
    stream_index: index of audio/subtitle track (ignored for video)
    """
    job = db.get_job(job_id)
    if not job:
        return None

    if stream_type == "video":
        prefix = "video"
    elif stream_type == "audio" and stream_index is not None:
        prefix = f"audio_{stream_index}"
    elif stream_type == "sub" and stream_index is not None:
        return _generate_subtitle_playlist(job_id, job, stream_index)
    else:
        return None

    # Query segments from database
    segment_keys = db.get_segments_for_prefix(job_id, prefix)
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


def _generate_subtitle_playlist(job_id, job, sub_index):
    """Generate a playlist for a subtitle track (single VTT file)."""
    duration = job["duration"] or 0

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{int(duration) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    key = f"sub_{sub_index}/subtitles.vtt"
    info = db.get_segment(job_id, key)
    if info:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"/segment/{job_id}/{key}")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
