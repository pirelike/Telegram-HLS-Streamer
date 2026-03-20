# CLAUDE.md — AI Assistant Guide for Telegram HLS Streamer

## Project Overview

Telegram HLS Streamer converts uploaded videos into HLS (HTTP Live Streaming) format and uses Telegram as unlimited cloud storage. Videos are chunked into segments, uploaded via up to 8 Telegram bots using round-robin distribution, and served back through proxied HLS playlists.

---

## Active Files (Watch These Only)

The following files are the **only active source files** in this repository. Several older files (see [Deleted/Obsolete Files](#deletedобsolете-files)) were removed during refactoring and must not be referenced.

```
app.py                    # Flask web server — main entry point
config.py                 # Environment-based configuration
database.py               # SQLite data layer (jobs, tracks, segments)
hls_manager.py            # HLS M3U8 playlist generation
stream_analyzer.py        # FFprobe stream analysis
telegram_uploader.py      # Multi-bot Telegram upload handler
video_processor.py        # FFmpeg HLS conversion pipeline
templates/index.html      # Frontend web UI (vanilla JS, no framework)
requirements.txt          # Python dependencies
.env.example              # Environment variable template
todo.md                   # Prioritized backlog of issues and features
tests/                    # pytest test suite (~2,300 lines)
.gitignore
README.md
```

## Deleted/Obsolete Files

The following files were **deleted** during the project's evolution. Do **not** recreate them, reference them, or follow patterns from them:

- `src/` directory — entire old source tree deleted
- `web/` directory — old frontend deleted
- `main.py` — original monolithic entry point, replaced by `app.py`
- `main_refactored.py` — transitional refactor, fully replaced
- `REFACTORING_GUIDE.md` — historical guide, no longer applicable
- JSON manifest files — replaced by SQLite database (`database.py`)

Old files were bad. The current flat module structure is intentional and correct.

---

## Architecture & Data Flow

```
Browser → Flask (app.py)
  ├── Chunked upload endpoint → temp file assembly
  ├── stream_analyzer.py (FFprobe) → detects video/audio/subtitle streams
  ├── video_processor.py (FFmpeg) → produces HLS segments + WebVTT subtitles
  ├── telegram_uploader.py → uploads segments to Telegram via 8 bots (round-robin)
  ├── hls_manager.py → generates M3U8 playlists, registers in SQLite
  └── database.py → stores job metadata, track info, segment file_ids
         ↓
HLS playback: /hls/<job_id>/master.m3u8
  └── Segment proxy: /segment/<job_id>/<segment_key> → fetches from Telegram
```

### Processing Pipeline (end to end)

1. Client uploads video in 10 MB chunks to `/api/upload/chunk`
2. All chunks assembled and verified on server
3. `stream_analyzer.py` runs FFprobe to enumerate all streams
4. `video_processor.py` runs FFmpeg:
   - Video tier 0 → high-bitrate CBR re-encode at source resolution
   - Video tiers 1–N → re-encoded lower-bitrate/lower-resolution variants (1080p, 720p, 480p, 360p as applicable)
   - Each audio track → separate HLS stream re-encoded to AAC
   - Each subtitle track → WebVTT `.vtt` file
5. `telegram_uploader.py` uploads all output files across bots (round-robin), with retry/backoff
6. `hls_manager.py` writes master `.m3u8` and per-stream playlists, persists to SQLite
7. Client receives `master.m3u8` URL for playback

---

## Module Responsibilities

### `app.py`
- Flask app and all route handlers
- Chunked resumable upload: `/api/upload/init`, `/api/upload/chunk`, `/api/upload/finalize`
- Job status polling: `/api/status/<job_id>`
- HLS serving: `/hls/<job_id>/master.m3u8`, `/hls/<job_id>/video_<N>.m3u8`, audio/subtitle playlists
- Segment proxy: `/segment/<job_id>/<segment_key>` — fetches TS/VTT from Telegram
- CORS headers on HLS endpoints (required for cross-origin HLS playback)
- Persistent async loop for Telegram reads plus a bounded worker queue for processing jobs

### `config.py`
- All settings loaded from environment variables (via `python-dotenv`)
- Key settings: `MAX_UPLOAD_SIZE` (100 GB), `UPLOAD_CHUNK_SIZE` (10 MB), `SEGMENT_TARGET_SIZE` (15 MB), `TELEGRAM_MAX_FILE_SIZE` (20 MB default in code), `HLS_SEGMENT_DURATION` (4 s)
- ABR settings: `ABR_ENABLED` (default true), `ABR_TIERS` defines re-encoded quality tiers (1080p/10M, 720p/5M, 480p/2M, 360p/1.2M)
- Playback cache settings: `SEGMENT_CACHE_SIZE_MB`, `SEGMENT_PREFETCH_COUNT`, `SEGMENT_PREFETCH_MIN_FREE_BYTES`
- Dynamically reads up to 8 bot tokens (`TELEGRAM_BOT_TOKEN_1`…`TELEGRAM_BOT_TOKEN_8`) and channel IDs (`TELEGRAM_CHANNEL_ID_1`…`TELEGRAM_CHANNEL_ID_8`)
- Creates `uploads/` and `processing/` directories on import

### `database.py`
- SQLite via standard `sqlite3`, thread-safe with connection pooling
- Tables: `jobs`, `tracks`, `segments`
- `tracks` stores video tiers (with width, height, bitrate), audio tracks, and subtitle tracks
- `segments` stores `(job_id, segment_key, file_id, bot_index)` — maps HLS keys to Telegram file_ids
- Indexed for fast lookup; cascade delete on job removal
- Auto-migrates schema on startup (adds new columns if missing)

### `stream_analyzer.py`
- Runs `ffprobe -v quiet -print_format json -show_streams`
- Returns `MediaAnalysis` with `.video`, `.audio[]`, `.subtitles[]`
- `MediaAnalysis.is_copy_compatible` → True if video/audio already suitable for HLS (skip re-encode)
- Filters out album art streams (codec_name == "mjpeg", disposition attached_pic)

### `video_processor.py`
- `process(analysis, job_id, progress_callback)` → `ProcessingResult`
- `_detect_hw_encoder()` probes for VAAPI, NVENC, QSV in that order; falls back to libx264
- Adaptive bitrate: tier 0 is always re-encoded at a high CBR chosen from source resolution; additional tiers re-encode at lower resolutions (1080p, 720p, 480p, 360p) — only tiers ≤ source height are produced
- Audio is always re-encoded to AAC at `AUDIO_BITRATE` (default 128k) for HLS compatibility
- `_run_ffmpeg_with_progress()` reports within-step FFmpeg progress via `-progress pipe:1`
- Separate FFmpeg invocations for each video tier, each audio track, each subtitle track
- `cleanup(job_id)` removes the `processing/<job_id>/` directory

### `telegram_uploader.py`
- `TelegramUploader` wraps up to 8 `python-telegram-bot` Bot instances
- `upload_files()` / `upload_job()` distribute uploads across bots with per-bot serialization
- `UploadedSegment(file_id, bot_index)` — bot_index stored so segments can be retrieved from correct bot
- Retry: exponential backoff on `RetryAfter` (rate limit) and `TimedOut` errors
- Async throughout (asyncio + aiohttp)

### `hls_manager.py`
- `generate_master_playlist(job_id, base_url)` → master M3U8 string
- Multi-variant ABR: `#EXT-X-MEDIA:TYPE=VIDEO` entries with named tiers — tier 0 labeled "Original (1080p)" / "Original (4K)", lower tiers labeled "1080p", "720p", etc.
- Audio and subtitle groups referenced by name via `#EXT-X-MEDIA`
- `generate_media_playlist(job_id, stream_type, stream_index)` → per-stream M3U8
- Legacy support for single-stream jobs without video tracks in DB

### `templates/index.html`
- Single-page UI, no JavaScript framework
- Chunked uploader: splits file client-side, sends chunks sequentially with per-chunk retry (5 retries, exponential backoff)
- Displays upload speed, ETA, detected tracks, resulting M3U8 URL
- Lists previous jobs with metadata (audio/subtitle counts, segment counts)

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and populate:

```env
# Server
LOCAL_HOST=0.0.0.0
LOCAL_PORT=5050
FORCE_HTTPS=false
BEHIND_PROXY=true

# Telegram bots (1 required, up to 8 supported)
TELEGRAM_BOT_TOKEN_1=...
TELEGRAM_CHANNEL_ID_1=-100...
TELEGRAM_BOT_TOKEN_2=...       # optional
TELEGRAM_CHANNEL_ID_2=-100...

# Limits (defaults shown)
MAX_UPLOAD_SIZE=107374182400   # 100 GB
UPLOAD_CHUNK_SIZE=10485760     # 10 MB
SEGMENT_TARGET_SIZE=15728640   # 15 MB preferred segment target
TELEGRAM_MAX_FILE_SIZE=20971520 # 20 MB hard upload ceiling
SEGMENT_CACHE_SIZE_MB=512
SEGMENT_PREFETCH_COUNT=2
SEGMENT_PREFETCH_MIN_FREE_BYTES=134217728

# Encoding
HLS_SEGMENT_DURATION=4
ENABLE_HARDWARE_ACCELERATION=true
PREFERRED_ENCODER=vaapi        # vaapi | nvenc | qsv
VIDEO_BITRATE=4M
AUDIO_BITRATE=128k

# Adaptive Bitrate
ABR_ENABLED=true               # source-res tier 0 + re-encoded lower tiers
# ABR_TIERS=1080:10M,720:5M,480:2M,360:1200k
# TIER0_BITRATES=2160:60M,1080:30M,720:15M,480:5M
# TIER0_BITRATE_DEFAULT=15M

# Upload / playback auth
UPLOAD_API_KEY=
UPLOAD_BASIC_USER=
UPLOAD_BASIC_PASSWORD=
PLAYBACK_SECRET=
```

---

## Development Workflow

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in bot tokens and channel IDs
```

**External dependencies** (must be installed on the host):
- `ffmpeg` and `ffprobe` — must be on `$PATH`
- Python 3.8+

### Running

```bash
python app.py
```

Starts Flask on `LOCAL_HOST:LOCAL_PORT` (default `0.0.0.0:5050`). Access UI at `http://localhost:5050`.

### Testing

```bash
pip install pytest
pytest
```

Test files in `tests/`:
- `test_app_p0_todos.py` — upload flow, finalization, job lifecycle
- `test_database_hls_manager.py` — SQLite persistence, playlist generation
- `test_telegram_uploader.py` — multi-bot upload, retry/backoff
- `test_stream_analyzer.py` — FFprobe parsing, stream detection
- `test_config_video_processor.py` — configuration, FFmpeg command building

For end-to-end validation, upload a sample video through the web UI and verify HLS playback.

---

## Key Conventions

### Python Style
- Standard library preferred; dependencies kept minimal
- Async code uses `asyncio` and `aiohttp`; sync Flask routes bridge into a persistent background event loop
- Dataclasses used for structured results (`StreamInfo`, `ProcessingResult`, etc.)
- No type annotations required but docstrings appreciated on public functions

### Database
- Do not bypass `database.py` — always use its provided functions
- Never manually construct SQL outside of `database.py`
- `segment_key` format: `{stream_prefix}/{filename}` such as `video_0/video_0001.ts` or `audio_0/audio_0003.ts`

### HLS
- Segment URLs in playlists point to `/segment/<job_id>/<segment_key>` (proxied, never direct Telegram URLs)
- Master playlist defines `#EXT-X-MEDIA` groups for video (named quality tiers), audio, and subtitles before `#EXT-X-STREAM-INF` entries
- Video tiers use `#EXT-X-MEDIA:TYPE=VIDEO` with `NAME` attributes (e.g. "Original (4K)", "1080p") so players can distinguish lossless from re-encoded
- Per-tier video playlists served at `/hls/<job_id>/video_<index>.m3u8`
- WebVTT subtitles use single-segment playlists with duration-based `#EXT-X-TARGETDURATION`

### Telegram Bots
- Always respect rate limits — the uploader has built-in backoff, do not remove it
- `bot_index` in the segments table is essential for retrieval — always store it
- Channel IDs must be negative integers (Telegram channel format)

### File Handling
- `uploads/` — temporary chunk assembly; cleaned up after processing
- `processing/<job_id>/` — FFmpeg output; cleaned up after Telegram upload via `video_processor.cleanup()`
- Never store permanent data on disk; Telegram is the storage layer

---

## Common Tasks

### Adding a New API Endpoint
Add route to `app.py`. Query `database.py` functions for data. No new files needed.

### Supporting a New Stream Type
1. Add detection in `stream_analyzer.py` (new stream class + update `MediaAnalysis`)
2. Add FFmpeg command in `video_processor.py`
3. Update `telegram_uploader.py` if a new file extension needs uploading
4. Update `hls_manager.py` to include the new stream in playlists
5. Update `database.py` if new metadata columns are needed

### Changing Segment Size or HLS Duration
Update `.env` values `SEGMENT_TARGET_SIZE`, `TELEGRAM_MAX_FILE_SIZE`, `HLS_SEGMENT_DURATION`, and optionally the segment cache settings. No code changes needed.

### Adding a New Bot
Add `TELEGRAM_BOT_TOKEN_N` and `TELEGRAM_CHANNEL_ID_N` to `.env` (N = 1–8). `config.py` loads them automatically.

---

## Known Architectural Issues

These are documented in `todo.md` with priorities. Key points for contributors:

### Sync/Async Tension
Flask is synchronous while Telegram operations remain async. `app.py` now bridges that with a persistent background event loop, which removes the earlier per-request event loop churn. The longer-term architectural tradeoff is still that async Telegram I/O and sync Flask request handling live in the same process.

### Process-Local Segment Caching
The segment proxy (`/segment/`) now uses an in-memory LRU cache plus sequential prefetch for Telegram-backed reads. Cache misses stream through a temp-file backed single-flight download path, so one request fetches from Telegram while same-key followers wait and then reuse the completed artifact. This is the intended setup for a single-process home deployment. If the app is ever scaled to multiple workers or nodes, each process will maintain its own cache and a shared backend such as Redis would be the follow-up path.

### Segment Size Depends on Encoder Planning
Segment sizing is driven by FFmpeg `-hls_segment_size` planning plus forced 1-second keyframes. `SEGMENT_TARGET_SIZE` is the preferred size, while `TELEGRAM_MAX_FILE_SIZE` remains the hard upload limit if generated output still overshoots.

### Database Is Critical Infrastructure
`streamer.db` maps `segment_key → file_id`. Losing it means losing access to ALL uploaded content on Telegram. There is currently no backup mechanism.

---

## Roadmap

See `todo.md` for the full prioritized backlog. Key items for next season:
1. Add metrics for cache hit rate, Telegram latency, and active jobs (P5)
2. Add backup/export workflow for `streamer.db` (P5)
3. Thumbnail generation (P6)
4. Optional shared cache backend if multi-worker deployment becomes necessary
