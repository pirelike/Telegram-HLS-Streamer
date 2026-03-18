# CLAUDE.md — Telegram HLS Streamer

## Project Overview

Telegram HLS Streamer is a Python async web application that uses Telegram bots as unlimited cloud storage for video files. It accepts video uploads, converts them to HLS (HTTP Live Streaming) format via FFmpeg, splits them into segments, stores each segment in Telegram channels, and serves the segments through a web interface for browser-based streaming.

**Current state of the repository**: The source code was deleted from the main branch (commit history shows progressive deletion). Only `README.md` remains. Any reimplementation should follow the architecture described here, derived from the git history.

---

## Architecture

The application is composed of six main components that collaborate via dependency injection:

```
main.py / main_refactored.py
        │
        ▼
TelegramHLSApp (src/core/app.py)
        │
        ├── Config              (src/core/config.py)       — env/settings loading + validation
        ├── DatabaseManager     (src/storage/database.py)  — SQLite, async wrappers
        ├── TelegramHandler     (src/telegram/handler.py)  — multi-bot upload/download, round-robin
        ├── VideoProcessor      (src/processing/video_processor.py) — FFmpeg HLS conversion
        ├── CacheManager        (src/processing/cache_manager.py)   — LRU + predictive preload
        └── StreamServer / WebServer  (src/web/)           — aiohttp REST + HLS endpoints
```

The two structural versions in git history:

| Version | Entry point | Source layout |
|---|---|---|
| v1 (flat) | `main.py` | `src/*.py` (flat files) + `web/` frontend |
| v2 (refactored) | `main_refactored.py` | `src/{core,processing,storage,telegram,web,utils}/` + `templates/` |

**The refactored v2 layout is the canonical target architecture.**

---

## Directory Structure (target / v2)

```
telegram-hls-streamer/
├── main_refactored.py            # CLI entry point (serve / test-bots / config / status)
├── requirements.txt              # Python dependencies
├── .env                          # Runtime secrets (never commit)
├── src/
│   ├── core/
│   │   ├── app.py               # TelegramHLSApp — orchestrates all components
│   │   ├── config.py            # Config dataclass + get_config() + setup_*
│   │   └── exceptions.py        # TelegramHLSError, ConfigurationError, etc.
│   ├── processing/
│   │   ├── video_processor.py   # FFmpeg analysis + HLS conversion
│   │   ├── hardware_accel.py    # VAAPI / NVENC / QSV detection + args
│   │   ├── cache_manager.py     # LRU eviction + predictive preloading
│   │   ├── batch_processor.py   # Batch/queue management for uploads
│   │   └── segment_optimizer.py # Segment duration tuning
│   ├── storage/
│   │   └── database.py          # DatabaseManager — async SQLite helpers
│   ├── telegram/
│   │   └── handler.py           # TelegramHandler / round-robin multi-bot
│   ├── web/
│   │   ├── server.py            # StreamServer — aiohttp app setup + lifecycle
│   │   ├── routes.py            # Route table
│   │   └── handlers.py          # Request handler methods
│   └── utils/
│       ├── networking.py        # HTTP helpers, proxy detection
│       ├── logging.py           # Logging setup
│       └── file_utils.py        # Path/file helpers
├── templates/
│   └── index_enhanced.html      # Jinja2 template for the web UI
└── web/                         # (v1) Static frontend assets
    ├── index.html
    ├── css/style.css
    └── js/{app,library,streaming,system,upload}.js
```

---

## Key Conventions

### Language & Runtime
- **Python 3.8+**, fully async via `asyncio`
- Async I/O everywhere: `aiohttp`, `aiofiles`, `asyncio.get_event_loop().run_in_executor` for sync SQLite calls
- Dataclasses (`@dataclass`) used for value objects: `BotConfig`, `VideoStream`, `VideoMetadata`, `ProcessingJob`, `CacheEntry`, `UploadResult`

### Configuration
- All settings loaded from `.env` file and/or environment variables via `Config._load_config()`
- Configuration is validated at startup; missing required values raise `ValueError` with a list of all errors
- `Config` exposes helper methods: `get_ffmpeg_hardware_accel_args()`, `get_base_url()`, `to_dict()`
- Never hardcode secrets or paths — always read from `os.getenv()`

### Database
- SQLite via synchronous `sqlite3` with `row_factory = sqlite3.Row`
- All DB operations wrapped with `asyncio.get_event_loop().run_in_executor(None, sync_fn)` to avoid blocking the event loop
- Schema: `videos`, `segments`, and metadata tables
- `DatabaseManager` exposes async `_execute()`, `_fetchall()`, `_fetchone()` helpers

### Telegram Integration
- Up to 8 bots configured via `TELEGRAM_BOT_TOKEN_1..8` + `TELEGRAM_CHANNEL_ID_1..8`
- Each bot owns a private Telegram channel used as segment storage
- Round-robin distribution balances upload load across bots
- Bot isolation: a segment uploaded by bot N is always retrieved via bot N
- HTTP calls use `aiohttp.ClientSession` with `timeout=aiohttp.ClientTimeout(total=300, connect=30)`
- Telegram 20 MB per-file limit is the key constraint for segment sizing (configurable via `TELEGRAM_MAX_FILE_SIZE`)

### Video Processing
- FFmpeg is required at runtime; path configurable via `FFMPEG_PATH`
- Hardware acceleration auto-detected in order: NVENC (nvidia-smi) → VAAPI (/dev/dri/renderD128) → software fallback
- **Copy mode**: if a file is ≥20 MB, video codec is H.264/HEVC, and audio codec is AAC/MP3, the file is split without re-encoding (lossless, fast)
- HLS output: `master.m3u8` → per-track `playlist.m3u8` → `.ts` segments
- Segment target duration: 10 s (min 2 s, max 30 s), configurable

### Caching
- `LRUCache` based on `collections.OrderedDict`, evicts least-recently-used segments when `max_size` exceeded
- `CacheEntry` tracks `access_count`, `last_access`, `hit_count`, `preloaded`
- `AccessPattern` records which segments follow which (predictive preloading)
- Preloading: up to `PRELOAD_SEGMENTS=8` next segments loaded concurrently (max `MAX_CONCURRENT_PRELOADS=5`)

### Web Server
- `aiohttp` web application; CORS configured via `aiohttp_cors`
- Static files served from `web/` directory at `/static/`
- Client max size set to `MAX_UPLOAD_SIZE` (default 20 GB)
- Two middleware layers: `_error_middleware` (JSON error responses), `_cors_middleware`

### Logging
- `logging.basicConfig` with both file (`telegram-hls.log`) and stdout handlers
- Log level controlled by `LOG_LEVEL` env var (default INFO)
- Each module instantiates `logging.getLogger(__name__)`; bot handlers use `logging.getLogger(f"{__name__}.Bot{index}")`

---

## API Endpoints

### Video Management
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/upload` | Upload a video file (multipart/form-data) |
| GET | `/api/videos` | List all videos |
| GET | `/api/videos/{video_id}` | Get video metadata |
| DELETE | `/api/videos/{video_id}` | Delete a video |
| GET | `/api/videos/{video_id}/status` | Get processing status |

### HLS Streaming
| Method | Path | Description |
|--------|------|-------------|
| GET | `/hls/{video_id}/master.m3u8` | Master HLS playlist |
| GET | `/hls/{video_id}/{track_type}/{track_name}/playlist.m3u8` | Track playlist |
| GET | `/hls/{video_id}/segments/{segment_name}` | Fetch a segment (fetches from Telegram if not cached) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/system/status` | Server + component health |
| GET | `/api/system/cache/stats` | Cache hit/miss stats |
| POST | `/api/system/cache/clear` | Flush cache |
| GET | `/api/system/bots/status` | Bot connectivity status |
| POST | `/api/system/bots/test` | Run bot connectivity test |
| GET | `/api/config` | Current non-secret configuration |
| GET | `/` | Web UI (serves index.html) |

---

## Environment Variables Reference

```bash
# Server
LOCAL_HOST=0.0.0.0
LOCAL_PORT=5050
PUBLIC_DOMAIN=                    # Set for production (used in HLS URLs)
FORCE_HTTPS=false
BEHIND_PROXY=true                 # Disables direct SSL; use nginx/Caddy in front
SSL_CERT_PATH=
SSL_KEY_PATH=

# Directories
UPLOAD_DIR=temp_uploads
SEGMENTS_DIR=segments
CACHE_DIR=cache
DATABASE_PATH=database/telegram_hls.db

# FFmpeg / Processing
FFMPEG_PATH=ffmpeg
FFMPEG_HARDWARE_ACCEL=auto        # auto | vaapi | nvenc | qsv | none
FFMPEG_THREADS=4
ENABLE_TWO_PASS_ENCODING=false
ENABLE_COPY_MODE=true
COPY_MODE_THRESHOLD=20971520      # 20 MB

# Segments
MIN_SEGMENT_DURATION=2
TARGET_SEGMENT_DURATION=10
MAX_SEGMENT_DURATION=30

# Cache
CACHE_TYPE=memory                 # memory | disk
CACHE_SIZE=1073741824             # 1 GB
PRELOAD_SEGMENTS=8
MAX_CONCURRENT_PRELOADS=5

# Upload limits
MAX_UPLOAD_SIZE=21474836480       # 20 GB
MAX_CONCURRENT_UPLOADS=3
TELEGRAM_MAX_FILE_SIZE=20971520   # 20 MB — Telegram per-file cap

# Telegram bots (1–8)
TELEGRAM_BOT_TOKEN_1=
TELEGRAM_BOT_TOKEN_2=
# ... up to 8
TELEGRAM_CHANNEL_ID_1=
TELEGRAM_CHANNEL_ID_2=
# ... up to 8
```

---

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in secrets
cp .env.example .env  # (create from the variables above)

# Test bot connectivity first
python main_refactored.py test-bots

# Show resolved configuration
python main_refactored.py config

# Start the server
python main_refactored.py serve
```

FFmpeg must be installed and accessible at `FFMPEG_PATH`.

---

## Development Guidelines

1. **Keep components decoupled.** Each component receives its dependencies via constructor injection. Do not import component singletons globally.

2. **Never block the event loop.** All I/O must be async. Use `run_in_executor` when calling synchronous libraries (SQLite, subprocess for FFmpeg analysis).

3. **Telegram segment sizing.** Any code that splits or uploads segments must respect `config.telegram_max_file_size` (default 20 MB). Do not hardcode this value.

4. **Copy mode first.** Before invoking FFmpeg re-encoding, always check `VideoProcessor.is_copy_mode_eligible()`. Unnecessary re-encoding wastes CPU and degrades quality.

5. **Bot index is sticky.** When storing a segment in `database.segments`, always record `bot_index`. When retrieving, use the same `bot_index`. Mixing bots for a single segment will produce download failures.

6. **HLS URL construction.** Always derive public URLs from `config.get_base_url()`. Never hardcode `localhost` or port numbers in playlist files.

7. **Error responses.** The `_error_middleware` wraps all aiohttp errors into `{"error": "...", "status": N}` JSON. Handlers should raise `aiohttp.HTTPException` subclasses rather than returning raw error dicts.

8. **Secrets.** `.env` must never be committed. Validate that bot tokens match the regex `^\d{8,10}:[a-zA-Z0-9_-]{35}$` before startup (already done in `Config._validate_config()`).

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `aiohttp` | Async HTTP server and client (Telegram API calls) |
| `aiofiles` | Async file I/O |
| `aiosqlite` | (imported in some modules) async SQLite |
| `aiohttp-jinja2` | Jinja2 template rendering |
| `python-dotenv` | `.env` file loading |
| `httpx` | Alternative HTTP client |
| `Jinja2` | HTML templating |
| `psutil` | System resource stats for `/api/system/status` |
| `python-telegram-bot` | Optional higher-level Telegram SDK |

FFmpeg is a **system dependency**, not a Python package.
