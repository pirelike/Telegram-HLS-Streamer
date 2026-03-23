# Telegram HLS Streamer

Telegram HLS Streamer is a Flask-based video pipeline that accepts large resumable uploads, converts media into HLS-compatible streams, uploads segments to Telegram channels through multiple bots, and serves HLS playlists/segments through HTTP.

The project is designed for self-hosted personal media delivery with:
- chunked uploads (multi-GB files)
- FFmpeg/ffprobe stream analysis and processing
- adaptive bitrate (ABR) tier generation
- multi-audio and multi-subtitle HLS playlists
- persistent SQLite mapping of HLS segment keys to Telegram `file_id`s

---

## Table of Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the server](#running-the-server)
- [Web UI workflow](#web-ui-workflow)
- [HTTP API reference](#http-api-reference)
- [HLS output format](#hls-output-format)
- [Storage, cleanup, and lifecycle](#storage-cleanup-and-lifecycle)
- [Security and deployment notes](#security-and-deployment-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Limitations](#limitations)

---

## How it works

1. A client uploads a video in chunks using `/api/upload/init` + `/api/upload/chunk`, or the optional folder watcher notices a completed local download under `WATCH_ROOT`.
2. The server finalizes the upload or watcher claim and starts a background job.
3. The job pipeline performs:
   - media analysis via `ffprobe`
   - video/audio/subtitle extraction and HLS packaging via `ffmpeg`
   - parallel upload of generated files to Telegram using multiple bots
   - persistence of metadata and segment mappings to SQLite
4. The server exposes HLS playlists (`/hls/...`) and segment proxy endpoints (`/segment/...`) so players can stream content.

---

## Architecture

### Core modules

- `app.py` — Flask app, upload endpoints, job lifecycle, HLS and segment routes.
- `stream_analyzer.py` — wraps `ffprobe`; detects video/audio/subtitle streams, codec metadata.
- `video_processor.py` — wraps `ffmpeg`; builds HLS video/audio playlists and VTT subtitles.
- `telegram_uploader.py` — async uploader with multi-bot round-robin and retry/backoff.
- `hls_manager.py` — generates master/media playlists and resolves segment metadata.
- `database.py` — SQLite schema and persistence for jobs, tracks, and segments.
- `config.py` — environment-driven runtime configuration.

### Data model (SQLite)

The database (`streamer.db`) is the source of truth for playback.

- `jobs`: one row per uploaded media job (includes media_type, series/episode metadata, has_thumbnail).
- `tracks`: one row per track variant (video tier, audio track, subtitle track).
- `segments`: maps `segment_key` (e.g. `video_0/video_0001.ts`) to Telegram `file_id` + `bot_index`.
- `settings`: key-value store for live configuration overrides persisted across restarts.
- `bots`: dynamically registered bots added via UI (beyond .env-defined bots).
- `schema_migrations`: ordered schema revision history applied on startup.

If `segments` data is lost, the server cannot resolve files back from Telegram for streaming.
On startup, the app upgrades older schemas in place (currently 6 revisions) and refuses to run against a newer unknown schema revision.

---

## Requirements

### System dependencies

- Python 3.8+
- FFmpeg (must include `ffmpeg` and `ffprobe` in PATH)
- Linux/macOS/WSL recommended for FFmpeg + optional hardware acceleration

### Python dependencies

Defined in `requirements.txt`:
- `flask`
- `python-dotenv`
- `aiohttp`
- `aiofiles`
- `python-telegram-bot`

---

## Installation

```bash
git clone https://github.com/pirelike/Telegram-HLS-Streamer
cd Telegram-HLS-Streamer
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the repository root (see full template below).

---

## Configuration

All runtime config is environment-variable based (`config.py`).

### Required Telegram settings

Bots are configured via `_N` suffixes — any number is supported. Two ways to add bots:

1. In `.env`: `TELEGRAM_BOT_TOKEN_N` / `TELEGRAM_CHANNEL_ID_N` for any positive N (applied on next restart).
2. Via the Settings UI (`/settings`) or `POST /api/bots/add` — validated live and persisted to the DB without restart.

```bash
TELEGRAM_BOT_TOKEN_1=123456:ABCDEF...
TELEGRAM_CHANNEL_ID_1=-1001234567890

TELEGRAM_BOT_TOKEN_2=...
TELEGRAM_CHANNEL_ID_2=-100...
# add TELEGRAM_BOT_TOKEN_3, _4, ... as needed
```

Notes:
- Channel IDs must be negative integers (Telegram private channel format).
- Placeholder values like `your_...` are ignored.
- Each configured bot should be admin in its corresponding channel.

### Full `.env` template

```bash
# Server
LOCAL_HOST=0.0.0.0
LOCAL_PORT=5050
FORCE_HTTPS=false
BEHIND_PROXY=false
CORS_ALLOWED_ORIGINS=

# Cloudflare tunnel
CLOUDFLARED_ENABLED=false

# File handling
TELEGRAM_MAX_FILE_SIZE=20971520
SEGMENT_TARGET_SIZE=15728640
MAX_UPLOAD_SIZE=107374182400
UPLOAD_CHUNK_SIZE=10485760
SEGMENT_CACHE_SIZE_MB=200
SEGMENT_PREFETCH_COUNT=3
SEGMENT_PREFETCH_MIN_FREE_BYTES=0

# Processing
HLS_SEGMENT_DURATION=4
VIDEO_BITRATE=4M
AUDIO_BITRATE=128k

# Hardware acceleration
ENABLE_HARDWARE_ACCELERATION=true
PREFERRED_ENCODER=vaapi
VAAPI_DEVICE=

# Adaptive bitrate
ABR_ENABLED=true
# ABR_TIERS=1080:10M,720:5M,480:2M,360:1200k
# TIER0_BITRATES=2160:60M,1080:30M,720:15M,480:5M
# TIER0_BITRATE_DEFAULT=15M

# Reliability / cleanup
JOB_TIMEOUT_SECONDS=7200
PENDING_UPLOAD_TTL_SECONDS=86400
PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS=300
JOB_RETENTION_DAYS=0
MAX_CONCURRENT_JOBS=1

# Optional watch-folder auto-ingest
WATCH_ENABLED=false
WATCH_ROOT=
WATCH_DONE_DIR=
WATCH_POLL_SECONDS=5
WATCH_STABLE_SECONDS=30
WATCH_VIDEO_EXTENSIONS=mp4,mkv,avi,mov,webm,ts,m4v,flv
WATCH_IGNORE_SUFFIXES=.part,.crdownload,.tmp,.partial

# Upload rate limiting
UPLOAD_RATE_LIMIT_WINDOW=60
UPLOAD_RATE_LIMIT_MAX_REQUESTS=100
MAX_PENDING_UPLOADS_PER_IP=5

# App-level authentication is intentionally unsupported.

# Telegram upload behavior
UPLOAD_PARALLELISM=8

# Telegram bots/channels
TELEGRAM_BOT_TOKEN_1=
TELEGRAM_CHANNEL_ID_1=
TELEGRAM_BOT_TOKEN_2=
TELEGRAM_CHANNEL_ID_2=
TELEGRAM_BOT_TOKEN_3=
TELEGRAM_CHANNEL_ID_3=
TELEGRAM_BOT_TOKEN_4=
TELEGRAM_CHANNEL_ID_4=
TELEGRAM_BOT_TOKEN_5=
TELEGRAM_CHANNEL_ID_5=
TELEGRAM_BOT_TOKEN_6=
TELEGRAM_CHANNEL_ID_6=
TELEGRAM_BOT_TOKEN_7=
TELEGRAM_CHANNEL_ID_7=
TELEGRAM_BOT_TOKEN_8=
TELEGRAM_CHANNEL_ID_8=
```

### Important behavior notes

- `SEGMENT_TARGET_SIZE` is the preferred FFmpeg segment size target. Lower values produce smaller segments.
- `TELEGRAM_MAX_FILE_SIZE` is the hard upload ceiling. Segment planning clamps under it, and uploads still fail fast if a file exceeds it.
- `SEGMENT_CACHE_SIZE_MB` is a shared cache budget for the full app process, not per viewer.
- `SEGMENT_PREFETCH_COUNT` controls how many upcoming segments are warmed per active playback flow.
- `SEGMENT_PREFETCH_MIN_FREE_BYTES` stops prefetch when the cache is close to full, reducing churn on smaller home servers.
- `UPLOAD_CHUNK_SIZE` must match frontend expectation if using bundled UI (currently 10MB).
- When `WATCH_ENABLED=true`, the watcher scans `WATCH_ROOT` recursively, ignores the `done/` subtree plus partial-download suffixes, and only queues files whose size/mtime have stayed unchanged for `WATCH_STABLE_SECONDS`.
- Successful watcher-ingested files are moved into `WATCH_DONE_DIR` after the full pipeline completes; failed files stay in place and will only be retried after they change.

---

## Running the server

```bash
python app.py
```

By default, the app starts on `0.0.0.0:5050`.

If `WATCH_ENABLED=true`, the same process also polls `WATCH_ROOT` for completed downloads and auto-enqueues supported video files.

Open:
- UI: `http://localhost:5050/`
- Jobs API: `http://localhost:5050/api/jobs`

The UI also lets you set the watched folder and finished-folder paths; those values are saved in `watch_settings.json` so they survive restarts.

---

## Web UI workflow

The included UI (`templates/index.html`) supports:
- drag/drop file select
- resumable chunked upload using localStorage
- upload + processing progress display
- stream analysis badges
- listing previous jobs
- copyable master playlist URL

The UI uses this upload flow:
1. `POST /api/upload/init`
2. `POST /api/upload/chunk` repeatedly
3. `POST /api/upload/finalize`
4. Poll `GET /api/status/<job_id>` until complete

---

## HTTP API reference

### Upload APIs

#### `POST /api/upload/init`
Start an upload session.

**Request JSON**
```json
{
  "filename": "movie.mkv",
  "total_size": 734003200,
  "total_chunks": 70
}
```

**Response JSON**
```json
{
  "upload_id": "abcd1234ef567890",
  "chunk_size": 10485760
}
```

#### `POST /api/upload/chunk`
Upload one binary chunk.

**Headers**
- `X-Upload-Id`: upload session id
- `X-Chunk-Index`: zero-based chunk index

**Body**
- raw bytes

The server validates chunk ordering, overlap, and size consistency.

#### `POST /api/upload/finalize`
Finalize upload and enqueue processing.

**Request JSON**
```json
{
  "upload_id": "abcd1234ef567890"
}
```

**Response JSON**
```json
{
  "job_id": "f0e1d2c3b4a5",
  "status": "queued"
}
```

#### `GET /api/upload/status/<upload_id>`
Returns current chunked upload progress.

### Job APIs

#### `GET /api/status/<job_id>`
Returns live/in-memory status for active jobs, or persisted metadata for completed jobs.

#### `GET /api/jobs?page=1&limit=20`
Returns paginated completed jobs (`limit` max 50).

#### `GET /api/jobs/<job_id>`
Returns full metadata for a single job.

#### `DELETE /api/jobs/<job_id>`
Deletes a completed job and its metadata from SQLite.

#### `PATCH /api/jobs/<job_id>`
Updates job metadata fields: `media_type`, `series_name`, `season_number`, `episode_number`, `part_number`.

#### `POST /api/cancel/<job_id>`
Marks an active job as cancelled.

### Playlist APIs

- `GET /hls/<job_id>/master.m3u8`
- `GET /hls/<job_id>/video.m3u8` (legacy/single-tier compatibility)
- `GET /hls/<job_id>/video_<index>.m3u8`
- `GET /hls/<job_id>/audio_<index>.m3u8`
- `GET /hls/<job_id>/sub_<index>.m3u8`

### Segment API

- `GET /segment/<job_id>/<segment_key>`

This endpoint proxies the segment from Telegram with the original bot. Cache hits return immediately from the in-memory segment cache; cache misses stream to the player while the server writes a temp-file spill copy and warms the cache when the completed segment fits.

### Settings & Bot APIs

#### `GET /api/settings`
Returns all configurable settings with current values, defaults, and descriptions organized by category.

#### `POST /api/settings`
Saves one or more settings and applies them live without restart. Values are persisted to the `settings` table in SQLite.

#### `POST /api/settings/reset`
Resets a specific setting to its default by removing its DB override (reverts to `.env` value).

#### `GET /api/bots`
Lists all configured bots (tokens masked).

#### `POST /api/bots/health`
Probes all bot connections and returns per-bot reachability status.

#### `POST /api/bots/add`
Validates a new bot token/channel pair (live `get_chat` check) and registers it.

#### `DELETE /api/bots/<id>`
Removes a dynamically registered bot.

#### `GET /api/watch-settings`
Returns current watch-folder configuration.

#### `POST /api/watch-settings`
Updates watch-folder settings (persisted to `watch_settings.json`).

### Thumbnail API

#### `GET /thumbnail/<job_id>`
Proxies the job's thumbnail JPEG from Telegram. Returns `image/jpeg` or 404 if no thumbnail was extracted.

### Metrics & Health

#### `GET /health`
Checks SQLite access plus Telegram bot/channel reachability via `get_chat`.

#### `GET /api/metrics`
Returns operational metrics: job queue depth, cache hit/miss/eviction counts, prefetch pending count, and Telegram upload/download counters (counts, errors, cumulative duration).

---

## HLS output format

### Master playlist

Master playlists include:
- `EXT-X-MEDIA` entries for audio tracks
- `EXT-X-MEDIA` entries for subtitle tracks
- video quality tiers and `EXT-X-STREAM-INF` references

### Video tiers (ABR)

When enabled, the processor creates:
- original-resolution tier (index 0)
- additional tiers according to configured ABR heights (`Config.ABR_TIERS`) up to source height

### Audio and subtitles

- each audio track is emitted as an independent HLS audio rendition
- subtitles are extracted to WebVTT and exposed as HLS subtitle playlists

---

## Storage, cleanup, and lifecycle

### Directories

- `uploads/`: incoming upload files before processing completes
- `processing/<job_id>/`: temporary FFmpeg outputs before Telegram upload finalizes
- `WATCH_DONE_DIR` (default `WATCH_ROOT/done`): successful watch-folder source files, preserving relative subdirectories
- `streamer.db`: persistent metadata/segment mapping database

### Cleanup behavior

- completed jobs remove temporary files from `uploads/` and `processing/`
- watch-folder jobs move successful source files into `WATCH_DONE_DIR` instead of deleting them
- pending/incomplete uploads are cleaned by TTL (`PENDING_UPLOAD_TTL_SECONDS`)
- long jobs can be force-marked as timed out via watcher (`JOB_TIMEOUT_SECONDS`)

---

## Security and deployment notes

### CORS

CORS is applied to `/api`, `/hls`, and `/segment` routes. Set `CORS_ALLOWED_ORIGINS=*` to allow all origins, or provide an explicit comma-separated allowlist.

### Reverse proxy

If running behind Nginx/Caddy/Traefik:
- set `BEHIND_PROXY=true`
- use `FORCE_HTTPS=true` if TLS is terminated at proxy and you need HTTPS playlist URLs in responses

### Playback cache behavior

The `/segment/...` proxy uses an in-memory LRU cache inside the app process. Misses are de-duplicated per segment key, streamed to the first client, and spilled to a temp file so the whole Telegram response is not buffered in RAM before serving. Sequential prefetch can warm upcoming segments into RAM for faster playback on the next requests. For the intended home deployment, run a single app process and treat that process-local cache as the normal operating mode.

If you run multiple workers or multiple app instances, they will not share cached segments. Playback will still work, but hot segments may be re-downloaded from Telegram by each worker or node.

---

## Troubleshooting

### `ffprobe not found` / `ffmpeg not found`
Install FFmpeg and verify:

```bash
ffprobe -version
ffmpeg -version
```

### Upload starts but never completes

- confirm `MAX_UPLOAD_SIZE` is greater than your file
- confirm frontend and backend chunk size alignment (`UPLOAD_CHUNK_SIZE`)
- check disk space for `uploads/` and `processing/`

### Telegram upload failures

- verify bot token correctness
- verify bot has permission in target channel
- confirm channel id format is negative integer (`-100...`)
- lower `UPLOAD_PARALLELISM` if you hit frequent network/rate-limit issues

### Segment playback times out after the manifest loads

- increase `SEGMENT_CACHE_SIZE_MB` on machines with available RAM
- keep `SEGMENT_PREFETCH_COUNT` modest (`1` or `2` is usually enough for a home server)
- use `SEGMENT_PREFETCH_MIN_FREE_BYTES` to prevent cache churn when many streams are active
- if you run multiple workers, remember each worker has its own cache and will re-fetch hot segments independently

### Playback fails for older jobs after bot changes

Segments are tied to the uploading bot. If you remove or rotate bots, old `bot_index` mappings may no longer resolve correctly.

---

## Development

### Running tests

The project has a test suite (~2,300 lines) covering core modules:

```bash
pip install pytest
pytest
```

Test files:
- `tests/test_app_p0_todos.py` — upload flow, finalization, job lifecycle
- `tests/test_database_hls_manager.py` — SQLite persistence, playlist generation
- `tests/test_telegram_uploader.py` — multi-bot upload, retry/backoff logic
- `tests/test_stream_analyzer.py` — FFprobe parsing, stream detection
- `tests/test_config_video_processor.py` — configuration loading, FFmpeg command building

### Manual verification

For end-to-end validation, upload a sample video through the web UI and verify HLS playback. Automated integration tests for the full pipeline (upload -> process -> Telegram upload -> playback) are not yet implemented.

### Roadmap

See `todo.md` for the prioritized list of known issues, planned improvements, and feature ideas.

---

## Limitations

- Single-process architecture — no distributed queue/worker support; job state is in-memory and not durable across restarts.
- Segment caching is process-local only; multiple workers or nodes will still duplicate hot Telegram reads unless a shared cache is added.
- Basic metrics are available via `/api/metrics` (queue depth, cache hit/miss stats, Telegram counters). Per-request latency histograms are not yet tracked.
- SQLite is the playback metadata store; it includes a schema versioning and migration framework for upgrades.
- ABR tiers are static config; no per-title complexity optimization.
- There is still no backup/export workflow for `streamer.db`.

---

## License

MIT (see repository licensing files/settings).
