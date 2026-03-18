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
   - Video → H.264/HEVC HLS `.ts` segments (hardware-accelerated if available)
   - Each audio track → separate HLS stream
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
- HLS serving: `/hls/<job_id>/master.m3u8`, media playlists
- Segment proxy: `/segment/<job_id>/<segment_key>` — fetches TS/VTT from Telegram
- CORS headers on HLS endpoints (required for cross-origin HLS playback)
- Spawns a background thread per job for the full pipeline

### `config.py`
- All settings loaded from environment variables (via `python-dotenv`)
- Key settings: `MAX_UPLOAD_SIZE` (100 GB), `CHUNK_SIZE` (10 MB), `TELEGRAM_SEGMENT_SIZE` (20 MB), `HLS_SEGMENT_DURATION` (4 s)
- Dynamically reads up to 8 bot tokens (`TELEGRAM_BOT_TOKEN_1`…`TELEGRAM_BOT_TOKEN_8`) and channel IDs (`TELEGRAM_CHANNEL_ID_1`…`TELEGRAM_CHANNEL_ID_8`)
- Creates `uploads/` and `processing/` directories on import

### `database.py`
- SQLite via standard `sqlite3`, thread-safe with connection pooling
- Tables: `jobs`, `tracks`, `segments`
- `segments` stores `(job_id, segment_key, file_id, bot_index)` — maps HLS keys to Telegram file_ids
- Indexed for fast lookup; cascade delete on job removal

### `stream_analyzer.py`
- Runs `ffprobe -v quiet -print_format json -show_streams`
- Returns `MediaAnalysis` with `.video`, `.audio[]`, `.subtitles[]`
- `MediaAnalysis.is_copy_compatible` → True if video/audio already suitable for HLS (skip re-encode)
- Filters out album art streams (codec_name == "mjpeg", disposition attached_pic)

### `video_processor.py`
- `process(input_path, job_id, analysis, config)` → `ProcessingResult`
- `_detect_hw_encoder()` probes for VAAPI, NVENC, QSV in that order; falls back to libx264
- Copy mode: if `is_copy_compatible`, uses `-c copy` (lossless, fast)
- Separate FFmpeg invocations for video, each audio track, each subtitle track
- `cleanup(job_id)` removes the `processing/<job_id>/` directory

### `telegram_uploader.py`
- `TelegramUploader` wraps up to 8 `python-telegram-bot` Bot instances
- `upload_file(path)` → round-robin bot selection, `send_document()` call
- `UploadedSegment(file_id, bot_index)` — bot_index stored so segments can be retrieved from correct bot
- Retry: exponential backoff on `RetryAfter` (rate limit) and `TimedOut` errors
- Async throughout (asyncio + aiohttp)

### `hls_manager.py`
- `generate_master_playlist(job_id, result, analysis, db)` → master M3U8 string
- Audio groups and subtitle groups referenced by name in master playlist
- Bandwidth estimated from file size and duration
- `generate_media_playlist(job_id, track_key, db)` → per-stream M3U8

### `templates/index.html`
- Single-page UI, dark theme, no JavaScript framework
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
TELEGRAM_MAX_FILE_SIZE=20971520 # 20 MB per segment

# Encoding
HLS_SEGMENT_DURATION=4
ENABLE_HARDWARE_ACCELERATION=true
PREFERRED_ENCODER=vaapi        # vaapi | nvenc | qsv
VIDEO_BITRATE=4M
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

### No Test Suite

There is currently no automated test suite. When making changes, manually verify the full pipeline by uploading a sample video and confirming HLS playback.

---

## Key Conventions

### Python Style
- Standard library preferred; dependencies kept minimal
- Async code uses `asyncio` and `aiohttp`; sync Flask routes run async code via `asyncio.run()`
- Dataclasses used for structured results (`StreamInfo`, `ProcessingResult`, etc.)
- No type annotations required but docstrings appreciated on public functions

### Database
- Do not bypass `database.py` — always use its provided functions
- Never manually construct SQL outside of `database.py`
- `segment_key` format: `{job_id}/{filename}` — must be consistent between uploader and proxy endpoint

### HLS
- Segment URLs in playlists point to `/segment/<job_id>/<segment_key>` (proxied, never direct Telegram URLs)
- Master playlist must correctly define `#EXT-X-MEDIA` groups for audio and subtitles before `#EXT-X-STREAM-INF` entries
- WebVTT subtitles use `#EXT-X-TARGETDURATION:0` and single-segment playlists

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
Update `.env` values `TELEGRAM_SEGMENT_SIZE` and `HLS_SEGMENT_DURATION`. No code changes needed.

### Adding a New Bot
Add `BOT_TOKEN_N` and `CHANNEL_ID_N` to `.env` (N = 1–8). `config.py` loads them automatically.
