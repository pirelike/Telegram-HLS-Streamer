# CLAUDE.md — AI Assistant Guide for Telegram HLS Streamer

## Project Overview

Telegram HLS Streamer converts uploaded videos into HLS (HTTP Live Streaming) format and uses Telegram as unlimited cloud storage. Videos are chunked into segments, uploaded via up to 8 Telegram bots using round-robin distribution, and served back through proxied HLS playlists.
Application-level authentication is intentionally out of scope; do not propose or reintroduce API key auth, Basic auth, or playback-token auth.

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
templates/                # Frontend web UI (vanilla JS, no framework)
  base.html               #   Shared layout shell
  index.html              #   Job browser / home page
  upload.html             #   Upload page
  settings.html           #   Settings management page (live config + bot management)
  watch.html              #   Per-job player/detail page
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
  ├── telegram_uploader.py → uploads segments to Telegram via multiple bots (round-robin)
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
   - Video tier 0 → copy passthrough when `ENABLE_COPY_MODE=true` and source is h264/hevc; otherwise high-bitrate CBR re-encode at source resolution
   - Video tiers 1–N → re-encoded lower-bitrate/lower-resolution variants (only tiers strictly below source height when copy mode is active; tiers at or below source height otherwise)
   - Each audio track → separate HLS stream re-encoded to AAC
   - Each subtitle track → WebVTT `.vtt` file
5. `telegram_uploader.py` uploads all output files across bots (round-robin), with retry/backoff
6. `hls_manager.py` writes master `.m3u8` and per-stream playlists, persists to SQLite
7. Client receives `master.m3u8` URL for playback

---

## Module Responsibilities

### `app.py`
- Flask app and all route handlers
- Chunked resumable upload: `/api/upload/init`, `/api/upload/chunk`, `/api/upload/finalize`, `/api/upload/status/<upload_id>`
- Job management: `/api/status/<job_id>`, `GET/DELETE/PATCH /api/jobs/<job_id>`, `GET /api/jobs` (paginated, filterable, grouped by series/season)
- Job cancellation: `POST /api/cancel/<job_id>` — terminates FFmpeg and cancels upload futures
- HLS serving: `/hls/<job_id>/master.m3u8`, `/hls/<job_id>/video_<N>.m3u8`, audio/subtitle playlists
- Segment proxy: `/segment/<job_id>/<segment_key>` — fetches TS/VTT from Telegram with in-memory LRU cache, single-flight dedup, and sequential prefetch
- CORS headers on HLS/API endpoints (configurable via `CORS_ALLOWED_ORIGINS`)
- Bot management: `GET /api/bots`, `POST /api/bots/health`, `POST /api/bots/add`, `DELETE /api/bots/<id>`
- Live settings: `GET/POST /api/settings`, `POST /api/settings/reset` — changes applied without restart
- DB transfer APIs: `POST /api/db/export` uploads a JSON snapshot (`jobs`/`tracks`/`segments`) to Telegram; `POST /api/db/import` downloads + merges with bot-index remapping
- Automatic DB merge worker: controlled by config, periodically imports a configured Telegram export file (`DB_AUTO_MERGE_*`)
- Watch-folder auto-ingest: polls `WATCH_ROOT` for stable video files, moves processed files to `WATCH_DONE_DIR`; `GET/POST /api/watch-settings`
- Thumbnail proxy: `GET /thumbnail/<job_id>` — fetches `thumbnail/thumbnail.jpg` from Telegram, cached in the shared segment LRU cache, served as `image/jpeg`
- Health and metrics: `GET /health`, `GET /api/metrics` (queue depth, cache stats, Telegram counters)
- Series/episode metadata: `PATCH /api/jobs/<job_id>` sets `media_type`, `series_name`, season/episode/part numbers
- Optional Cloudflared tunnel (`CLOUDFLARED_ENABLED`) with auto-restart and DNS readiness check
- Persistent async loop for Telegram reads plus a bounded worker queue for processing jobs (`MAX_CONCURRENT_JOBS`)
- Reliability guards: shared aiohttp session recreation is serialized via a thread lock and always created on the persistent async loop; upload finalization removes pending-tracking only after successful queueing; watcher `os.stat()` races are treated as non-fatal skips; and cancel flow now drains in-flight upload futures briefly when `future.cancel()` cannot preempt immediately.

### `config.py`
- All settings loaded from environment variables (via `python-dotenv`)
- Server: `HOST` (0.0.0.0), `PORT` (5050), `FORCE_HTTPS` (false), `BEHIND_PROXY` (false), `CLOUDFLARED_ENABLED` (false), `CORS_ALLOWED_ORIGINS` (empty)
- File handling: `MAX_UPLOAD_SIZE` (100 GB), `UPLOAD_CHUNK_SIZE` (10 MB), `SEGMENT_TARGET_SIZE` (15 MB), `TELEGRAM_MAX_FILE_SIZE` (20 MB)
- Playback cache: `SEGMENT_CACHE_SIZE_MB` (200), `SEGMENT_PREFETCH_COUNT` (3), `SEGMENT_PREFETCH_MIN_FREE_BYTES` (0 = no check)
- HLS/encoding: `HLS_SEGMENT_DURATION` (4 s), `VIDEO_BITRATE` (4M), `AUDIO_BITRATE` (128k)
- Hardware acceleration: `ENABLE_HARDWARE_ACCELERATION` (true), `PREFERRED_ENCODER` (vaapi), `VAAPI_DEVICE` (empty = auto-detect highest /dev/dri/renderD*), `MAX_PARALLEL_ENCODES` (2)
- ABR: `ABR_ENABLED` (true), `ENABLE_COPY_MODE` (true — passthrough tier 0 if source is h264/hevc; ABR tiers only at strictly lower resolutions), `ABR_TIERS` (1080p/10M, 720p/5M, 480p/2M, 360p/1200k), `TIER0_BITRATES`, `TIER0_BITRATE_DEFAULT`
- Reliability/cleanup: `JOB_TIMEOUT_SECONDS` (7200), `PENDING_UPLOAD_TTL_SECONDS` (86400), `PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS` (300), `JOB_RETENTION_DAYS` (0), `MAX_CONCURRENT_JOBS` (1)
- Rate limiting (per IP): `UPLOAD_RATE_LIMIT_WINDOW` (60 s), `UPLOAD_RATE_LIMIT_MAX_REQUESTS` (100), `MAX_PENDING_UPLOADS_PER_IP` (5)
- Watch folder: `WATCH_ENABLED` (false), `WATCH_ROOT`, `WATCH_DONE_DIR`, `WATCH_POLL_SECONDS` (5), `WATCH_STABLE_SECONDS` (30), `WATCH_VIDEO_EXTENSIONS`, `WATCH_IGNORE_SUFFIXES`
- Telegram: `UPLOAD_PARALLELISM` (8), `DB_AUTO_MERGE_INTERVAL_MINUTES` (0 = disabled), `DB_AUTO_MERGE_FILE_ID`, `DB_AUTO_MERGE_BOT_INDEX`, and `BOTS` dynamically loaded from `TELEGRAM_BOT_TOKEN_1`…`_N` + `TELEGRAM_CHANNEL_ID_1`…`_N` (no hardcoded upper limit; duplicate tokens are skipped with a warning so each token appears once)
- Runtime: `Config.load_from_db()` applies DB-persisted overrides; `POST /api/settings` now applies changed values in-place (no full `.env` re-read) and only triggers bot reloads for bot-related setting changes; `Config.reload()` remains the full re-read path used by bot add/remove and reset flows; `Config.to_dict()` returns all configurable settings for the settings API
- Creates `uploads/` and `processing/` directories on import

### `database.py`
- SQLite via standard `sqlite3`, thread-safe with per-thread connection pooling and one-time self-healing reconnect on stale-handle `OperationalError`
- Tables: `jobs`, `tracks`, `segments`, `settings`, `bots`, `schema_migrations`
- `jobs` stores job metadata including `media_type`, `series_name`, `has_thumbnail`, `is_series`, `season_number`, `episode_number`, `part_number`
- `tracks` stores video tiers (with width, height, bitrate), audio tracks, and subtitle tracks
- `segments` stores `(job_id, segment_key, file_id, bot_index)` — maps HLS keys to Telegram file_ids
- `settings` stores key-value config overrides applied at runtime (persisted across restarts)
- `bots` stores dynamically registered bots (beyond .env-defined bots)
- Indexed for fast lookup; cascade delete on job removal
- Schema migration framework with `schema_migrations` tracking (revisions 1-8 implemented, including strict CHECK/NOT NULL constraints and listing indexes on `jobs(media_type)` + `jobs(created_at DESC)`)

### `stream_analyzer.py`
- Runs `ffprobe -v quiet -print_format json -show_streams`
- Returns `MediaAnalysis` with `.video`, `.audio[]`, `.subtitles[]`
- `MediaAnalysis.is_copy_compatible` → True if video/audio already suitable for HLS (skip re-encode)
- Filters out album art streams (codec_name == "mjpeg", disposition attached_pic)
- Tolerates missing ffprobe stream `index` fields by falling back to the stream's enumerate position (prevents analysis crashes on malformed container metadata)

### `video_processor.py`
- `process(analysis, job_id, progress_callback)` → `ProcessingResult`
- `_detect_hw_encoder()` performs test encodes for VAAPI (auto-detecting `/dev/dri/renderD*`), NVENC, QSV in that order; falls back to libx264/libx265
- Copy mode + ABR interaction (four scenarios):
  - `ENABLE_COPY_MODE=true` + `ABR_ENABLED=true` (default): tier 0 uses `-c:v copy` passthrough; ABR tiers only at resolutions **strictly below** the source are re-encoded. If source codec is not h264/hevc, falls back to full encoding.
  - `ENABLE_COPY_MODE=false` + `ABR_ENABLED=true`: tier 0 is CBR re-encoded at source resolution; all ABR tiers at or below source height are re-encoded (including same-resolution lower-bitrate tier).
  - `ENABLE_COPY_MODE=true` + `ABR_ENABLED=false`: tier 0 copy passthrough only — fastest mode, no encoding at all. Falls back to tier 0 encode if codec incompatible.
  - `ENABLE_COPY_MODE=false` + `ABR_ENABLED=false`: tier 0 CBR re-encode only.
- Tier 0 bitrate selected from `TIER0_BITRATES` by source height; ABR tiers from `ABR_TIERS`
- Video tiers encoded in parallel via `ThreadPoolExecutor` limited by `MAX_PARALLEL_ENCODES`
- If any parallel ABR tier fails, `process()` waits for the executor to exit, then calls `cleanup(job_id)` before re-raising to avoid leaving partial tier output behind
- Audio always re-encoded to AAC at `AUDIO_BITRATE` (128k default); only text-based subtitle formats extracted to WebVTT
- Oversized segment handling: scans `.ts` files exceeding `TELEGRAM_MAX_FILE_SIZE`; re-encodes in-place at computed target bitrate
- Thumbnail extraction: frame at 10% of duration (min 2 s), 640 px wide; non-fatal if it fails; stored as `thumbnail.jpg` and uploaded to Telegram
- `_run_ffmpeg_with_progress()` reports within-step FFmpeg progress via `-progress pipe:1`
- `ProcessingResult` includes `.video_playlists`, `.audio_playlists`, `.subtitle_files`, `.segment_durations`, `.thumbnail_path`
- `cleanup(job_id)` removes the `processing/<job_id>/` directory and logs (without raising) if deletion fails, so callers can safely invoke it from `finally` blocks

### `telegram_uploader.py`
- `TelegramUploader` wraps any number of `python-telegram-bot` Bot instances (from `.env` or DB)
- `upload_files()` / `upload_job()` distribute uploads across bots with per-bot serialization and cross-bot parallelism
- `UploadedSegment(file_id, bot_index, file_name, file_size)` — bot_index stored so segments can be retrieved from correct bot
- Retry: exponential backoff on `RetryAfter` (rate limit), `TimedOut`, and `NetworkError`; `BadRequest`/`Forbidden` fail immediately
- Upload integrity check: validates file_size match post-upload
- Includes thumbnail upload as part of `upload_job()`
- `probe_health()` — async verification that all bots can access their channels; returns per-bot status dict
- `reload_bots()` — rebuilds bot list from `Config.BOTS` at runtime without restart (for live bot management)
- Bot state synchronization: `reload_bots()`, `_next_bot()`, and lazy per-bot lock initialization are guarded by one `threading.Lock` to avoid lock-list races and stale-index crashes during live bot reloads
- Metrics tracking: upload/download counts, error counts, and cumulative durations (thread-safe `threading.Lock`)
- Async throughout (asyncio + aiohttp)

### `hls_manager.py`
- `generate_master_playlist(job_id, base_url)` → master M3U8 string
- Standard ABR: one `#EXT-X-STREAM-INF` per video quality tier (BANDWIDTH, RESOLUTION, CODECS, AUDIO, SUBTITLES) — tier 0 labeled "Original (4K)" / "Original (1080p)", lower tiers labeled "1080p", "720p", etc.
- Audio groups via `#EXT-X-MEDIA:TYPE=AUDIO`; subtitle groups via `#EXT-X-MEDIA:TYPE=SUBTITLES`
- `generate_media_playlist(job_id, stream_type, stream_index)` → per-stream M3U8
- Legacy support for single-stream jobs without video tracks in DB

### `templates/index.html`
- Multi-page UI (vanilla JS, no framework); pages: `/` (upload + job list), `/settings` (live config + bot management), `/watch/<job_id>` (job detail / watch folder)
- Chunked uploader: splits file client-side, sends chunks sequentially with per-chunk retry (5 retries, exponential backoff)
- Displays upload speed, ETA, detected tracks, resulting M3U8 URL
- Lists previous jobs with metadata (audio/subtitle counts, segment counts, series grouping)
- Settings page reads `/api/settings` and `/api/bots`, allows live edits and bot add/remove

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and populate:

```env
# Server
LOCAL_HOST=0.0.0.0
LOCAL_PORT=5050
FORCE_HTTPS=false
BEHIND_PROXY=false
CORS_ALLOWED_ORIGINS=          # comma-separated origins, or * for all

# Cloudflare tunnel
CLOUDFLARED_ENABLED=false

# Telegram bots (1 required; add _2…_N for more)
TELEGRAM_BOT_TOKEN_1=...
TELEGRAM_CHANNEL_ID_1=-100...

# File handling (defaults shown)
MAX_UPLOAD_SIZE=107374182400   # 100 GB
UPLOAD_CHUNK_SIZE=10485760     # 10 MB
SEGMENT_TARGET_SIZE=15728640   # 15 MB preferred segment target
TELEGRAM_MAX_FILE_SIZE=20971520 # 20 MB hard upload ceiling
SEGMENT_CACHE_SIZE_MB=200
SEGMENT_PREFETCH_COUNT=3
SEGMENT_PREFETCH_MIN_FREE_BYTES=0

# Encoding
HLS_SEGMENT_DURATION=4
ENABLE_HARDWARE_ACCELERATION=true
PREFERRED_ENCODER=vaapi        # vaapi | nvenc | qsv
VAAPI_DEVICE=                  # empty = auto-detect /dev/dri/renderD*
MAX_PARALLEL_ENCODES=2
VIDEO_BITRATE=4M
AUDIO_BITRATE=128k

# Adaptive Bitrate
ABR_ENABLED=true               # produce re-encoded lower-resolution tiers alongside tier 0
ENABLE_COPY_MODE=true          # tier 0 passthrough for h264/hevc sources (skip re-encode)
                               # with ABR: copy tier 0 + encode strictly lower-res tiers
                               # without ABR: copy-only, fastest mode (no encoding)
                               # incompatible codec: falls back to tier 0 re-encode
# ABR_TIERS=1080:10M,720:5M,480:2M,360:1200k
# TIER0_BITRATES=2160:60M,1080:30M,720:15M,480:5M
# TIER0_BITRATE_DEFAULT=15M

# Reliability / cleanup
JOB_TIMEOUT_SECONDS=7200
PENDING_UPLOAD_TTL_SECONDS=86400
PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS=300
JOB_RETENTION_DAYS=0           # 0 = keep forever
MAX_CONCURRENT_JOBS=1

# Rate limiting (per IP)
UPLOAD_RATE_LIMIT_WINDOW=60
UPLOAD_RATE_LIMIT_MAX_REQUESTS=100
MAX_PENDING_UPLOADS_PER_IP=5

# Watch folder (auto-ingest)
WATCH_ENABLED=false
# WATCH_ROOT=/path/to/watch
# WATCH_DONE_DIR=              # defaults to WATCH_ROOT/done

# Telegram upload
UPLOAD_PARALLELISM=8
DB_AUTO_MERGE_INTERVAL_MINUTES=0   # 0 disables automatic merge
DB_AUTO_MERGE_FILE_ID=             # Telegram file_id to import
DB_AUTO_MERGE_BOT_INDEX=0          # bot index used for download

# App-level authentication is intentionally unsupported.
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
- Master playlist defines `#EXT-X-MEDIA` groups for audio and subtitles, then `#EXT-X-STREAM-INF` entries for each video quality tier
- Video tier names (e.g. "Original (4K)", "1080p") appear in `#EXT-X-STREAM-INF` BANDWIDTH/RESOLUTION attributes so players can present quality selection
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
Two options:
1. **Via `.env`**: Add `TELEGRAM_BOT_TOKEN_N` and `TELEGRAM_CHANNEL_ID_N` (N = any number). `config.py` loads them automatically on next restart, deduplicating duplicate tokens by keeping the lowest suffix and warning for duplicates.
2. **Via UI**: Use the Settings page (`/settings`) → Bot Management → Add Bot. Token/channel are validated live via `POST /api/bots/add` and persisted to the `bots` DB table (no restart needed).

---

## Known Architectural Issues

These are documented in `todo.md` with priorities. Key points for contributors:

### Sync/Async Tension
Flask is synchronous while Telegram operations remain async. `app.py` now bridges that with a persistent background event loop, which removes the earlier per-request event loop churn. The longer-term architectural tradeoff is still that async Telegram I/O and sync Flask request handling live in the same process.

### Process-Local Segment Caching
The segment proxy (`/segment/`) now uses an in-memory LRU cache plus sequential prefetch for Telegram-backed reads. Cache misses stream through a temp-file backed single-flight download path, so one request fetches from Telegram while same-key followers wait and then reuse the completed artifact. `_SegmentCache.put()` also stages eviction planning before the mutation pass to shrink lock hold time during heavy eviction bursts. This is the intended setup for a single-process home deployment. If the app is ever scaled to multiple workers or nodes, each process will maintain its own cache and a shared backend such as Redis would be the follow-up path.

### Segment Size Depends on Encoder Planning
Segment sizing is driven by FFmpeg `-hls_segment_size` planning plus forced 1-second keyframes. `SEGMENT_TARGET_SIZE` is the preferred size, while `TELEGRAM_MAX_FILE_SIZE` remains the hard upload limit if generated output still overshoots.

### Database Is Critical Infrastructure
`streamer.db` maps `segment_key → file_id`. Losing it means losing access to ALL uploaded content on Telegram. There is currently no backup mechanism.

---

## Roadmap

See `todo.md` for the full prioritized backlog. Key items for next season:
1. Add backup/export workflow for `streamer.db` (P5)
2. Thumbnail UI polish — extraction, upload, and proxy are done; dedicated display improvements in the job browser (P6)
3. Job re-processing without re-upload (P6)
4. Optional shared cache backend if multi-worker deployment becomes necessary
