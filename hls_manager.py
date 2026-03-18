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


def list_jobs(limit=50, offset=0):
    """List jobs with summary info, newest first.

    Returns a dict keyed by job_id.
    """
    jobs = db.list_jobs(limit=limit, offset=offset)
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


def count_jobs():
    """Return the total number of completed jobs."""
    return db.count_jobs()


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

    # Video variant streams (one per quality tier)
    video_tracks = db.get_job_tracks(job_id, "video")
    video_group = "video"
    duration = job["duration"] or 1

    if video_tracks:
        # EXT-X-MEDIA:TYPE=VIDEO entries with named quality tiers
        for t in video_tracks:
            i = t["track_index"]
            is_default = "YES" if i == 0 else "NO"
            name = _video_tier_name(t["height"], is_original=(i == 0))
            uri = f"{base_url}/hls/{job_id}/video_{i}.m3u8"
            lines.append(
                f'#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="{video_group}",'
                f'NAME="{name}",DEFAULT={is_default},'
                f'AUTOSELECT={is_default},URI="{uri}"'
            )

        # STREAM-INF entries referencing the video group
        for t in video_tracks:
            bw = _parse_bitrate(t["bitrate"]) if t["bitrate"] else 0
            if bw == 0:
                bw = int(job["file_size"] * 8 / duration) if duration > 0 else 0

            stream_inf = f"#EXT-X-STREAM-INF:BANDWIDTH={bw}"
            if t["width"] and t["height"]:
                stream_inf += f',RESOLUTION={t["width"]}x{t["height"]}'
            stream_inf += f',VIDEO="{video_group}"'
            if audio_tracks:
                stream_inf += f',AUDIO="{audio_group}"'
            if subtitle_tracks:
                stream_inf += f',SUBTITLES="{sub_group}"'

            lines.append(stream_inf)
            lines.append(f"{base_url}/hls/{job_id}/video_{t['track_index']}.m3u8")
    else:
        # Legacy: single video stream (no video tracks in DB)
        bandwidth = job["file_size"] * 8
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


def _video_tier_name(height, is_original=False):
    """Generate a human-readable name for a video quality tier."""
    label = _height_to_label(height)
    if is_original:
        return f"Original ({label})"
    return label


def _height_to_label(height):
    """Convert a pixel height to a display label (e.g. 2160 -> '4K')."""
    labels = {2160: "4K", 4320: "8K"}
    if height in labels:
        return labels[height]
    return f"{height}p"


def _parse_bitrate(bitrate_str):
    """Parse a bitrate string like '5M' or '600k' into bits per second."""
    if not bitrate_str:
        return 0
    s = bitrate_str.strip().upper()
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        elif s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        else:
            return int(s)
    except (ValueError, IndexError):
        return 0


def generate_media_playlist(job_id, stream_type, stream_index=None):
    """Generate a media-level M3U8 playlist for a specific stream.

    stream_type: "video", "audio", or "sub"
    stream_index: index of audio/subtitle track (ignored for video)
    """
    job = db.get_job(job_id)
    if not job:
        return None

    if stream_index is not None:
        try:
            stream_index = int(stream_index)
        except (TypeError, ValueError):
            return None
        if stream_index < 0:
            return None

    if stream_type == "video":
        prefix = f"video_{stream_index}" if stream_index is not None else "video"
    elif stream_type == "audio" and stream_index is not None:
        track = _get_track(job_id, "audio", stream_index)
        if not track:
            return None
        prefix = f"audio_{stream_index}"
    elif stream_type == "sub" and stream_index is not None:
        track = _get_track(job_id, "subtitle", stream_index)
        if not track:
            return None
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


def _get_track(job_id, track_type, track_index):
    """Return the track dict for a specific job/type/index, or None."""
    tracks = db.get_job_tracks(job_id, track_type)
    for track in tracks:
        if track["track_index"] == track_index:
            return track
    return None
