# TODO — Season 2

## P0 — Critical Bugs

- [ ] `config.py:31` / `video_processor.py`: `TELEGRAM_MAX_FILE_SIZE` is declared (20MB) but **never enforced** — FFmpeg `-hls_time 4` controls segment duration, not size; a 4K high-bitrate segment can exceed 20MB and cause Telegram upload failure, killing the entire job
  - Fix: add `-hls_segment_size` to FFmpeg commands as a hard cap, or validate segment sizes before upload and re-segment oversized ones

## P1 — Performance (High Impact)

- [ ] `app.py:864-892`: **Segment proxy has no server-side caching** — every segment request fetches from Telegram (~200-500ms round trip); HLS players re-request segments during seek/quality switch; under concurrent viewers, Telegram rate limits will be hit
  - Fix: add in-memory LRU cache (e.g. bounded dict or `cachetools.LRUCache`) for recently served segments; even 100MB cache would dramatically reduce Telegram API calls
- [ ] `app.py:136-144`: **New asyncio event loop created per request** — `_run_async()` creates and tears down an event loop for every segment proxy call; for a video with 500 segments, that's 500 event loop lifecycles
  - Fix: maintain a single persistent background event loop, dispatch async work via `asyncio.run_coroutine_threadsafe()`; or migrate to Quart (async Flask drop-in)
- [ ] `app.py:874`: **Segments fully buffered in memory** — `get_file_bytes()` downloads entire segment into a `bytearray` then sends to client; for 20MB segments, each concurrent viewer uses 20MB RAM per segment fetch
  - Fix: stream Telegram download directly to HTTP response using chunked transfer encoding

## P2 — Reliability

- [ ] `app.py:731-739`: **Non-reentrant `threading.Lock` is fragile** — inline comments warn against calling `_is_job_cancelled()` while holding `_job_status_lock`; any future refactor that accidentally nests calls will deadlock silently
  - Fix: switch to `threading.RLock()` (zero downside for this use case)
- [ ] `hls_manager.py:237`: **HLS segment durations are hardcoded** — every `#EXTINF` uses `Config.HLS_SEGMENT_DURATION` (4s), but actual FFmpeg segments vary (3.8s-4.2s) depending on keyframe placement; causes seek inaccuracies and timeline drift on long videos
  - Fix: parse FFmpeg-generated `.m3u8` to extract actual per-segment durations; store in DB alongside `segment_key`
- [ ] No disk space checks before upload assembly or FFmpeg processing — processing a 50GB video can require 2-3x disk space for ABR tiers; running out mid-process leaves corrupted partial files
  - Fix: check available disk space before starting upload assembly and before each FFmpeg invocation; fail fast with clear error
- [ ] `app.py:937`: **Cloudflared tunnel thread blocks forever** — `proc.wait()` blocks the tunnel thread; if cloudflared exits (network issues), there's no restart logic
  - Fix: add a restart loop with exponential backoff

## P3 — Data Model

- [ ] `database.py:192`: **Original stream indices lost for subtitles** — when non-text subtitles are skipped, the `track_index` in DB uses processing-result enumerate index, not the original FFprobe stream index; makes debugging difficult
  - Fix: store original stream index as a separate column in `tracks` table
- [ ] No per-segment duration stored in DB — prevents accurate HLS playlist generation and makes it impossible to generate correct `#EXTINF` values after the FFmpeg output is cleaned up
  - Fix: add `duration` column to `segments` table; populate from FFmpeg output `.m3u8`

## P4 — Security Hardening

- [ ] HLS/segment endpoints have **no authentication** — upload can be protected with API key/Basic auth, but anyone with a `job_id` can stream content
  - Fix: add optional token-based auth for playback endpoints (signed URLs or session tokens)
- [ ] `templates/index.html:559`: **Fragile XSS pattern** in `onclick="deleteJob('${safeId}')"` — `escapeHtml()` prevents HTML injection but a `job_id` containing `'` could break the JS string; safe today (UUIDs are alphanumeric) but brittle pattern
  - Fix: use `addEventListener` instead of inline `onclick` with interpolated values

## P5 — Operational

- [ ] No `/health` endpoint — load balancers, monitoring systems, and container orchestrators need a health check endpoint
  - Fix: add `GET /health` returning `{"status": "ok", "bots": N, "db": true}` with checks for DB connectivity and bot availability
- [ ] No metrics/observability — no request timing, error rates, Telegram API call counting, or queue depth reporting
  - Fix: add optional Prometheus metrics endpoint or structured JSON logging for key events
- [ ] No database backup mechanism — `streamer.db` is the sole source of truth for mapping segments to Telegram `file_id`s; losing it means losing access to all uploaded content
  - Fix: add periodic SQLite backup (`.backup` API) and/or document backup procedures

## P6 — New Features

- [ ] **Thumbnail generation** — no preview thumbnails for job list; would improve UX significantly
  - Implementation: extract frame at ~10% duration via `ffmpeg -ss <time> -vframes 1`, upload to Telegram, store `file_id` in `jobs` table, serve via proxy endpoint
- [ ] **Job re-processing** — no way to re-process a job (e.g. to add/change ABR tiers) without re-uploading the original file
- [ ] **Webhook notifications** — notify external services when jobs complete (useful for automation/bots)
  - Implementation: optional `WEBHOOK_URL` env var; POST job metadata on completion
- [ ] **Multi-user support** — all jobs are in a single namespace; no per-user isolation or access control
- [ ] **Configurable ABR tiers via API** — `ABR_TIERS` is hardcoded in `config.py`; allow per-job override via upload init payload
- [ ] **Download original** — allow downloading the original file back from Telegram segments (reverse the HLS segmentation)

## P7 — Code Quality

- [ ] **Test coverage gaps** — existing tests (2,324 lines across 5 files) cover core paths but lack integration tests for the full pipeline, segment proxy, and edge cases (oversized segments, concurrent uploads, rate limiting)
- [ ] **Type annotations** — most functions lack type hints; adding them would improve IDE support and catch bugs earlier
- [ ] **Async/sync boundary cleanup** — the codebase mixes sync Flask with async Telegram calls via `_run_async()`; a clear architectural decision (stay sync, go fully async, or use a background event loop) would reduce complexity
