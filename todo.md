# TODO — Season 2

## P0 — Critical Bugs

- [x] `config.py:31` / `video_processor.py`: **Rewrite encoding pipeline — CBR + size-based segmentation**
  - **Problem:** `TELEGRAM_MAX_FILE_SIZE` (20MB) is never enforced; VBR segments can exceed 20MB and kill uploads; copy mode can't guarantee segment sizes
  - **Plan:**
    1. **Ditch copy mode and VBR entirely** — all video tiers are re-encoded at constant bitrate (CBR) for predictable, consistent segment sizes
    2. **Tier 0 ("near-lossless")** is no longer a stream copy — it's a high-quality CBR re-encode at the source resolution:
       - 4K → 60 Mbps CBR (~2.4s per 18MB segment)
       - 1080p → 30 Mbps CBR (~4.8s per 18MB segment)
       - 720p → 15 Mbps CBR (~9.6s per 18MB segment)
       - 480p → 5 Mbps CBR (~28.8s per 18MB segment)
    3. **Same-resolution lower-bitrate tiers** — for each source resolution, include both "Original (1080p)" at high CBR *and* a separate "1080p" at lower CBR, giving users explicit bitrate control (e.g. a 1080p source produces: Original 1080p @ 30M, 1080p @ 10M, 720p @ 5M, 480p @ 2M, 360p @ 1.2M)
    4. **Lower ABR tiers encode from tier 0's output**, not from the original source — this means FFmpeg only decodes the original once; lower tiers re-encode from the already-decoded high-quality tier 0
    5. **Size-based segmentation** — use `-hls_segment_size 18874368` (18MB) with `-force_key_frames "expr:gte(t,n_forced*1)"` (keyframe every 1s) on all tiers
    6. **CBR makes segment sizes predictable** — with constant bitrate, each segment at a given tier will have roughly the same duration and size, eliminating VBR variance
  - **Config changes:**
    - Remove `ENABLE_COPY_MODE` — always re-encode
    - Update `ABR_TIERS` to include same-resolution lower-bitrate tiers: `[{1080: 10M}, {720: 5M}, {480: 2M}, {360: 1.2M}]` — these are the re-encode tiers *below* tier 0; tier 0 bitrate set by resolution auto-detect (4K→60M, 1080p→30M, 720p→15M, 480p→5M)
    - Change `_get_abr_tiers` filter from `<` to `<=` source height so same-resolution tiers are included
    - Add `SEGMENT_MAX_SIZE = 18874368` (18MB) to replace `HLS_SEGMENT_DURATION` as the primary segmentation control
  - **FFmpeg flags per tier:**
    - `-c:v libx264 -b:v {bitrate} -minrate {bitrate} -maxrate {bitrate} -bufsize {bitrate}` (true CBR)
    - `-hls_segment_size 18874368`
    - `-force_key_frames "expr:gte(t,n_forced*1)"`
  - **HLS naming:** tier 0 labeled "Original (1080p)" / "Original (4K)"; same-resolution lower tier labeled "1080p" / "4K"; lower-resolution tiers labeled "720p", "480p", etc. — `_video_tier_name` in `hls_manager.py` already handles this since `is_original` is only true for tier 0
  - **Encoding chain:** source → tier 0 (high CBR) → tier 1, 2, 3… (lower CBR, encoded from tier 0 output)

## P1 — Performance (High Impact)

- [x] `app.py:901-937`: **Segment proxy has no server-side caching** — every segment request fetches from Telegram (~200-500ms round trip); HLS players re-request segments during seek/quality switch; under concurrent viewers, Telegram rate limits will be hit
  - Fix: `_SegmentCache` class — bounded `OrderedDict`-backed LRU cache, configurable via `SEGMENT_CACHE_SIZE_MB` (default 200MB), thread-safe with `Lock`
- [x] `app.py:147-150`: **New asyncio event loop created per request** — `_run_async()` creates and tears down an event loop for every segment proxy call; for a video with 500 segments, that's 500 event loop lifecycles
  - Fix: single persistent daemon thread loop (`_async_loop`), dispatched via `asyncio.run_coroutine_threadsafe()` with 30s timeout; clean shutdown via `atexit`
- [x] `app.py:973-984`: **Segments fully buffered in memory** — `get_file_bytes()` downloads entire segment into a `bytearray` then sends to client; for 20MB segments, each concurrent viewer uses 20MB RAM per segment fetch
  - Fix: `serve_segment` now fetches via `get_file_url()` + `aiohttp` with `iter_chunked(65536)`; result is cached after first fetch so repeated requests skip Telegram entirely

## P2 — Reliability

- [x] `app.py:731-739`: **Non-reentrant `threading.Lock` is fragile** — inline comments warn against calling `_is_job_cancelled()` while holding `_job_status_lock`; any future refactor that accidentally nests calls will deadlock silently
  - Fix: switch to `threading.RLock()` (zero downside for this use case)
- [x] `hls_manager.py:237`: **HLS segment durations are hardcoded** — every `#EXTINF` uses `Config.HLS_SEGMENT_DURATION` (4s), but actual FFmpeg segments vary (3.8s-4.2s) depending on keyframe placement; causes seek inaccuracies and timeline drift on long videos
  - Fix: parse FFmpeg-generated `.m3u8` to extract actual per-segment durations; store in DB alongside `segment_key`
- [x] No disk space checks before upload assembly or FFmpeg processing — processing a 50GB video can require 2-3x disk space for ABR tiers; running out mid-process leaves corrupted partial files
  - Fix: check available disk space before starting upload assembly and before each FFmpeg invocation; fail fast with clear error
- [x] `app.py:937`: **Cloudflared tunnel thread blocks forever** — `proc.wait()` blocks the tunnel thread; if cloudflared exits (network issues), there's no restart logic
  - Fix: add a restart loop with exponential backoff

## P3 — Data Model

- [x] `database.py:192`: **Original stream indices lost for subtitles** — when non-text subtitles are skipped, the `track_index` in DB uses processing-result enumerate index, not the original FFprobe stream index; makes debugging difficult
  - Fix: store original stream index as a separate column in `tracks` table
- [x] No per-segment duration stored in DB — prevents accurate HLS playlist generation and makes it impossible to generate correct `#EXTINF` values after the FFmpeg output is cleaned up
  - Fix: add `duration` column to `segments` table; populate from FFmpeg output `.m3u8`

## P4 — Security Hardening

- [x] HLS/segment endpoints have **no authentication** — upload can be protected with API key/Basic auth, but anyone with a `job_id` can stream content
  - Fix: add optional token-based auth for playback endpoints (signed URLs or session tokens)
- [x] `templates/index.html:559`: **Fragile XSS pattern** in `onclick="deleteJob('${safeId}')"` — `escapeHtml()` prevents HTML injection but a `job_id` containing `'` could break the JS string; safe today (UUIDs are alphanumeric) but brittle pattern
  - Fix: use `addEventListener` instead of inline `onclick` with interpolated values

## P5 — Operational

- [x] No `/health` endpoint — load balancers, monitoring systems, and container orchestrators need a health check endpoint
  - Fix: add `GET /health` returning `{"status": "ok", "bots": N, "db": true}` with checks for DB connectivity and bot availability
- [ ] No metrics/observability — no request timing, error rates, Telegram API call counting, or queue depth reporting
  - Fix: add optional Prometheus metrics endpoint or structured JSON logging for key events
- [ ] No database backup mechanism — `streamer.db` is the sole source of truth for mapping segments to Telegram `file_id`s; losing it means losing access to all uploaded content
  - Fix: add periodic SQLite backup (`.backup` API) and/or document backup procedures

## P6 — New Features

- [ ] **Scalable bot pool beyond 8** — `config.py` hardcodes a max of 8 bots; target is at least 256, but actual Telegram API limits (rate limits per bot, max bots per account, channel member limits) are unknown and need investigation before implementation
  - Investigation needed: Telegram BotFather limits, per-bot `send_document` rate limits, whether a single channel can receive from 256+ bots, and whether `HTTPXRequest` connection pool sizing needs adjustment at scale
  - Fix: remove the hardcoded `8` cap in `config.py` bot loading loop; make it dynamic (scan all `TELEGRAM_BOT_TOKEN_N` / `TELEGRAM_CHANNEL_ID_N` env vars up to a configurable `MAX_BOTS` limit); update `UPLOAD_PARALLELISM` default to scale with bot count
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
