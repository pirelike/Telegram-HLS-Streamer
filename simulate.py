"""Simulate 1 round of upload and play.

This script exercises the full Telegram HLS Streamer pipeline without
requiring real FFmpeg/ffprobe or Telegram bots:

  Phase 1 — Upload     : assembles a fake video file from chunks
  Phase 2 — Analysis   : builds a MediaAnalysis object (skips ffprobe)
  Phase 3 — Processing : generates fake .ts / .vtt segments (skips FFmpeg)
  Phase 4 — Upload     : stores segments in memory, assigns fake file_ids
  Phase 5 — Register   : persists the job to the real SQLite database
  Phase 6 — Play       : generates real HLS playlists, fetches all segments

Run:
    python simulate.py
"""

import logging
import os
import shutil
import sys
import uuid

# ── module path setup ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stream_analyzer import MediaAnalysis, VideoStream, AudioStream, SubtitleStream
from video_processor import ProcessingResult
from hls_manager import register_job, generate_master_playlist, generate_media_playlist
import database as db
from config import Config


# ── Inline stubs for telegram_uploader data classes ────────────────────────
# These replicate the interface of UploadedSegment / UploadResult exactly so
# the simulation can call register_job() (which calls db.save_job()) without
# needing a working python-telegram-bot installation.

class UploadedSegment:
    """Segment successfully uploaded to Telegram (or simulated storage)."""
    def __init__(self, file_id: str, bot_index: int, file_name: str, file_size: int):
        self.file_id = file_id
        self.bot_index = bot_index
        self.file_name = file_name
        self.file_size = file_size


class UploadResult:
    """Aggregated result of uploading all segments for one job."""
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.segments: dict[str, UploadedSegment] = {}
        self.total_bytes = 0
        self.total_files = 0

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,          # suppress library noise
    format="%(levelname)s [%(name)s]: %(message)s",
)
logger = logging.getLogger("simulate")
logger.setLevel(logging.DEBUG)

SEP = "─" * 60
SEGMENT_DURATION = Config.HLS_SEGMENT_DURATION   # seconds per segment
SIM_DURATION = 20.0                               # simulated video length (s)
SIM_FILENAME = "sample_video.mp4"


# ══════════════════════════════════════════════════════════════════════════
# Phase 1 – Upload
# ══════════════════════════════════════════════════════════════════════════

def phase_upload(job_id: str) -> tuple[str, int]:
    """Simulate chunked client upload by writing a fake file to uploads/."""
    print(f"\n{SEP}")
    print("Phase 1 — Upload")
    print(SEP)

    upload_path = os.path.join(Config.UPLOAD_DIR, f"{job_id}_{SIM_FILENAME}")
    chunk_size = Config.UPLOAD_CHUNK_SIZE        # 10 MB per chunk
    total_chunks = 3
    total_size = chunk_size * total_chunks

    print(f"  file     : {SIM_FILENAME}")
    print(f"  chunks   : {total_chunks} × {chunk_size // (1024**2)} MB")
    print(f"  total    : {total_size // (1024**2)} MB")

    with open(upload_path, "wb") as fh:
        for i in range(total_chunks):
            # Each chunk is a repeating pattern so we can verify alignment
            chunk = bytes([i & 0xFF]) * chunk_size
            fh.write(chunk)
            print(f"  chunk {i}: {len(chunk):,} bytes written  ✓")

    actual = os.path.getsize(upload_path)
    assert actual == total_size, f"size mismatch: {actual} != {total_size}"
    print(f"  → assembled {actual:,} bytes at {upload_path}")
    return upload_path, actual


# ══════════════════════════════════════════════════════════════════════════
# Phase 2 – Stream analysis (ffprobe simulation)
# ══════════════════════════════════════════════════════════════════════════

def phase_analysis(file_path: str, file_size: int) -> MediaAnalysis:
    """Build a MediaAnalysis object representing a 1080p H.264 file with 2 audio tracks."""
    print(f"\n{SEP}")
    print("Phase 2 — Stream Analysis  (ffprobe simulated)")
    print(SEP)

    analysis = MediaAnalysis(file_path, SIM_DURATION, file_size)

    video = VideoStream(index=0, codec_name="h264", width=1920, height=1080, bit_rate="4000000")
    analysis.video_streams.append(video)

    audio_en = AudioStream(
        index=1, codec_name="aac", channels=2,
        sample_rate=48000, bit_rate="128000",
        language="eng", title="English",
    )
    audio_es = AudioStream(
        index=2, codec_name="aac", channels=2,
        sample_rate=48000, bit_rate="128000",
        language="spa", title="Spanish",
    )
    analysis.audio_streams.extend([audio_en, audio_es])

    sub = SubtitleStream(index=3, codec_name="subrip", language="eng", title="English Subtitles")
    analysis.subtitle_streams.append(sub)

    print(f"  video    : {len(analysis.video_streams)} stream(s)")
    for vs in analysis.video_streams:
        print(f"             stream {vs.index}: {vs.codec_name} {vs.width}×{vs.height}")
    print(f"  audio    : {len(analysis.audio_streams)} track(s)")
    for aus in analysis.audio_streams:
        print(f"             stream {aus.index}: {aus.codec_name} [{aus.language}] \"{aus.title}\"")
    print(f"  subtitles: {len(analysis.subtitle_streams)} track(s)")
    for ss in analysis.subtitle_streams:
        print(f"             stream {ss.index}: {ss.codec_name} [{ss.language}] \"{ss.title}\"")
    print(f"  duration : {SIM_DURATION}s")
    return analysis


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 – Processing (FFmpeg simulation)
# ══════════════════════════════════════════════════════════════════════════

def phase_processing(analysis: MediaAnalysis, job_id: str) -> tuple[ProcessingResult, str]:
    """Generate fake .ts segments and .vtt subtitle files without FFmpeg."""
    print(f"\n{SEP}")
    print("Phase 3 — Processing  (FFmpeg simulated)")
    print(SEP)

    output_dir = os.path.join(Config.PROCESSING_DIR, job_id)
    os.makedirs(output_dir, exist_ok=True)

    result = ProcessingResult(job_id, output_dir)
    n_segs = max(1, int(SIM_DURATION / SEGMENT_DURATION))

    def _fake_ts_packet(tier: int, seg_idx: int) -> bytes:
        """Minimal 188-byte MPEG-TS packet (sync byte + payload)."""
        return b"\x47" + bytes([(tier << 4) | (seg_idx & 0x0F)] + [seg_idx & 0xFF] * 186)

    # ── video tiers ──────────────────────────────────────────────────────
    # Tier 0: original 1080p  (always created)
    video_dir = os.path.join(output_dir, "video_0")
    os.makedirs(video_dir, exist_ok=True)
    for i in range(n_segs):
        seg = os.path.join(video_dir, f"video_{i:04d}.ts")
        with open(seg, "wb") as f:
            f.write(_fake_ts_packet(0, i) * 10)      # ~1.88 KB per segment
    playlist = os.path.join(video_dir, "video.m3u8")
    result.video_playlists.append((playlist, video_dir, 1920, 1080, "4M"))
    print(f"  video tier 0  (1920×1080, 4M)  → {n_segs} segments")

    # Tier 1: 720p  (ABR)
    video_dir_720 = os.path.join(output_dir, "video_1")
    os.makedirs(video_dir_720, exist_ok=True)
    for i in range(n_segs):
        seg = os.path.join(video_dir_720, f"video_{i:04d}.ts")
        with open(seg, "wb") as f:
            f.write(_fake_ts_packet(1, i) * 10)
    playlist_720 = os.path.join(video_dir_720, "video.m3u8")
    result.video_playlists.append((playlist_720, video_dir_720, 1280, 720, "5M"))
    print(f"  video tier 1  (1280×720,  5M)  → {n_segs} segments")

    # ── audio tracks ─────────────────────────────────────────────────────
    for i, audio in enumerate(analysis.audio_streams):
        audio_dir = os.path.join(output_dir, f"audio_{i}")
        os.makedirs(audio_dir, exist_ok=True)
        for j in range(n_segs):
            seg = os.path.join(audio_dir, f"audio_{j:04d}.ts")
            with open(seg, "wb") as f:
                f.write(bytes([0x47, i & 0xFF, j & 0xFF] * 62 + [0x47]))  # 187 bytes
        playlist = os.path.join(audio_dir, "audio.m3u8")
        result.audio_playlists.append((
            playlist, audio_dir, audio.language, audio.title, audio.channels,
        ))
        print(f"  audio track {i}  [{audio.language}] \"{audio.title}\"  → {n_segs} segments")

    # ── subtitle tracks ───────────────────────────────────────────────────
    for i, sub in enumerate(analysis.subtitle_streams):
        if not sub.is_text_based:
            continue
        sub_dir = os.path.join(output_dir, f"sub_{i}")
        os.makedirs(sub_dir, exist_ok=True)
        vtt_path = os.path.join(sub_dir, "subtitles.vtt")
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            t = 0.0
            while t < SIM_DURATION:
                end = min(t + SEGMENT_DURATION, SIM_DURATION)
                f.write(
                    f"{_fmt_vtt_time(t)} --> {_fmt_vtt_time(end)}\n"
                    f"[Simulated subtitle at {t:.0f}s]\n\n"
                )
                t = end
        result.subtitle_files.append((vtt_path, sub_dir, sub.language, sub.title, i, sub.index))
        print(f"  subtitle  {i}  [{sub.language}] \"{sub.title}\"  → subtitles.vtt")

    total_segs = sum(
        len([f for f in os.listdir(d) if f.endswith(".ts")])
        for (_, d, *_) in result.video_playlists + result.audio_playlists
    ) + len(result.subtitle_files)
    print(f"  → {total_segs} files created under {output_dir}")
    return result, output_dir


def _fmt_vtt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ══════════════════════════════════════════════════════════════════════════
# Phase 4 – Telegram upload (simulated)
# ══════════════════════════════════════════════════════════════════════════

def phase_telegram_upload(
    processing_result: ProcessingResult,
    job_id: str,
) -> tuple[UploadResult, dict[str, bytes]]:
    """
    Mock Telegram upload: read each segment into memory, assign a
    fake file_id, and return an UploadResult plus an in-memory store.
    """
    print(f"\n{SEP}")
    print("Phase 4 — Telegram Upload  (simulated)")
    print(SEP)

    def _fake_file_id() -> str:
        """Return an 80-char alphanumeric string (passes DB storage; not validated at read)."""
        return (uuid.uuid4().hex * 3)[:80]

    segment_store: dict[str, bytes] = {}   # fake_file_id → raw bytes
    upload_result = UploadResult(job_id)
    bot_index = 0    # single simulated bot

    def _store(key: str, file_path: str):
        fid = _fake_file_id()
        with open(file_path, "rb") as fh:
            data = fh.read()
        segment_store[fid] = data
        upload_result.segments[key] = UploadedSegment(
            file_id=fid,
            bot_index=bot_index,
            file_name=os.path.basename(file_path),
            file_size=len(data),
        )

    # video
    for i, (_, tier_dir, *_) in enumerate(processing_result.video_playlists):
        for fn in sorted(f for f in os.listdir(tier_dir) if f.endswith(".ts")):
            _store(f"video_{i}/{fn}", os.path.join(tier_dir, fn))

    # audio
    for i, (_, audio_dir, *_) in enumerate(processing_result.audio_playlists):
        for fn in sorted(f for f in os.listdir(audio_dir) if f.endswith(".ts")):
            _store(f"audio_{i}/{fn}", os.path.join(audio_dir, fn))

    # subtitles
    for i, (vtt_path, *_) in enumerate(processing_result.subtitle_files):
        _store(f"sub_{i}/subtitles.vtt", vtt_path)

    upload_result.total_files = len(upload_result.segments)
    upload_result.total_bytes = sum(s.file_size for s in upload_result.segments.values())

    # Print summary by category
    v = sum(1 for k in upload_result.segments if k.startswith("video_"))
    a = sum(1 for k in upload_result.segments if k.startswith("audio_"))
    s = sum(1 for k in upload_result.segments if k.startswith("sub_"))
    print(f"  video segments : {v}")
    print(f"  audio segments : {a}")
    print(f"  subtitle files : {s}")
    print(f"  total uploaded : {upload_result.total_files} files, "
          f"{upload_result.total_bytes:,} bytes  ✓")
    return upload_result, segment_store


# ══════════════════════════════════════════════════════════════════════════
# Phase 5 – Register in database
# ══════════════════════════════════════════════════════════════════════════

def phase_register(
    job_id: str,
    analysis: MediaAnalysis,
    processing_result: ProcessingResult,
    upload_result: UploadResult,
) -> None:
    print(f"\n{SEP}")
    print("Phase 5 — Register Job in Database")
    print(SEP)

    register_job(job_id, analysis, processing_result, upload_result)

    job = db.get_job(job_id)
    tracks = db.get_job_tracks(job_id)
    segs = db.get_segments_for_prefix(job_id, "video_0")
    print(f"  job_id   : {job['job_id']}")
    print(f"  filename : {job['filename']}")
    print(f"  duration : {job['duration']}s")
    print(f"  codec    : {job['video_codec']}  {job['video_width']}×{job['video_height']}")
    print(f"  tracks   : {len(tracks)}  ({sum(1 for t in tracks if t['track_type']=='video')} video, "
          f"{sum(1 for t in tracks if t['track_type']=='audio')} audio, "
          f"{sum(1 for t in tracks if t['track_type']=='subtitle')} subtitle)")
    print(f"  segments : {len(upload_result.segments)} total  "
          f"(video_0 prefix: {len(segs)})")
    print("  → job persisted to SQLite  ✓")


# ══════════════════════════════════════════════════════════════════════════
# Phase 6 – Playback simulation
# ══════════════════════════════════════════════════════════════════════════

def phase_play(
    job_id: str,
    upload_result: UploadResult,
    segment_store: dict[str, bytes],
    base_url: str = "http://localhost:5050",
) -> None:
    print(f"\n{SEP}")
    print("Phase 6 — HLS Playback Simulation")
    print(SEP)

    # ── master playlist ───────────────────────────────────────────────────
    master = generate_master_playlist(job_id, base_url)
    assert master, "master playlist is empty!"
    print(f"\n  [master.m3u8]  ({len(master)} bytes)\n")
    for line in master.splitlines():
        print(f"    {line}")

    # ── video media playlists ─────────────────────────────────────────────
    video_tracks = db.get_job_tracks(job_id, "video")
    for t in video_tracks:
        idx = t["track_index"]
        pl = generate_media_playlist(job_id, "video", idx)
        assert pl, f"video_{idx}.m3u8 is empty!"
        lines = pl.splitlines()
        seg_lines = [l for l in lines if l.startswith("/segment/")]
        print(f"\n  [video_{idx}.m3u8]  {len(seg_lines)} segment(s)")
        for ln in lines:
            print(f"    {ln}")

    # ── audio playlists ───────────────────────────────────────────────────
    audio_tracks = db.get_job_tracks(job_id, "audio")
    for t in audio_tracks:
        idx = t["track_index"]
        pl = generate_media_playlist(job_id, "audio", idx)
        assert pl, f"audio_{idx}.m3u8 is empty!"
        seg_lines = [l for l in pl.splitlines() if l.startswith("/segment/")]
        print(f"\n  [audio_{idx}.m3u8]  [{t['language']}] \"{t['title']}\"  "
              f"{len(seg_lines)} segment(s)")

    # ── subtitle playlists ────────────────────────────────────────────────
    sub_tracks = db.get_job_tracks(job_id, "subtitle")
    for t in sub_tracks:
        idx = t["track_index"]
        pl = generate_media_playlist(job_id, "sub", idx)
        assert pl, f"sub_{idx}.m3u8 is empty!"
        print(f"\n  [sub_{idx}.m3u8]  [{t['language']}] \"{t['title']}\"")
        for ln in pl.splitlines():
            print(f"    {ln}")

    # ── segment retrieval ─────────────────────────────────────────────────
    print(f"\n  Simulating segment retrieval ({len(upload_result.segments)} segments)…")
    ok = 0
    err = 0
    for key, seg in sorted(upload_result.segments.items()):
        # Lookup via database (same path app.py uses)
        info = db.get_segment(job_id, key)
        if not info:
            print(f"    ✗ DB miss:  {key}")
            err += 1
            continue
        data = segment_store.get(info["file_id"])
        if data is None:
            print(f"    ✗ store miss: {key}")
            err += 1
            continue
        ok += 1

    print(f"\n  ✓ {ok} segment(s) fetched successfully")
    if err:
        print(f"  ✗ {err} segment(s) failed")
    else:
        print(f"  ✗ 0 errors")


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    job_id = uuid.uuid4().hex[:12]
    print(f"\n{'═' * 60}")
    print(f"  Telegram HLS Streamer — Upload & Play Simulation")
    print(f"  job_id: {job_id}")
    print(f"{'═' * 60}")

    tmp_dirs = []

    try:
        # Phase 1: upload
        file_path, file_size = phase_upload(job_id)

        # Phase 2: analysis
        analysis = phase_analysis(file_path, file_size)

        # Phase 3: processing
        processing_result, proc_dir = phase_processing(analysis, job_id)
        tmp_dirs.append(proc_dir)

        # Phase 4: telegram upload (simulated)
        upload_result, segment_store = phase_telegram_upload(processing_result, job_id)

        # Phase 5: register
        phase_register(job_id, analysis, processing_result, upload_result)

        # Phase 6: play
        phase_play(job_id, upload_result, segment_store)

        print(f"\n{'═' * 60}")
        print("  Simulation complete — all phases passed  ✓")
        print(f"{'═' * 60}\n")

    finally:
        # Cleanup temp files
        if os.path.exists(file_path):
            os.remove(file_path)
        for d in tmp_dirs:
            if os.path.exists(d):
                shutil.rmtree(d)
        # Remove simulated job from DB so re-runs stay clean
        db.delete_job(job_id)


if __name__ == "__main__":
    main()
