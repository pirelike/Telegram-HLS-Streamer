# TODO — Codebase Audit

Audit basis: `app.py`, `config.py`, `database.py`, `hls_manager.py`, `stream_analyzer.py`, `telegram_uploader.py`, `video_processor.py`, `templates/index.html`, `README.md`, `CLAUDE.md`, and the `tests/` suite.

## P0 — Critical Bugs

- [x] `video_processor.py` + `telegram_uploader.py`: segment sizing now uses a configurable FFmpeg target size plus upload-time hard rejection against the Telegram file size limit.
- [x] `app.py:/health`: health status still does not verify actual Telegram bot usability; it only reports configured bot count, so a dead bot pool can still look healthy.

## P1 — Performance (High Impact)

- [x] `app.py`: segment proxy now uses an in-memory LRU cache for Telegram-backed segment reads.
- [x] `app.py`: async Telegram fetches now use a persistent background event loop instead of creating a loop per request.
- [x] `app.py`: sequential segment prefetch is implemented with in-flight de-duplication and cache headroom guards.
- [x] `app.py:/segment`: process-local caching is sufficient for the intended single-process home deployment; shared cache remains deferred unless multi-worker or multi-node deployment becomes a real requirement.

## P2 — Reliability

- [x] `app.py`: job status locking is re-entrant, avoiding the earlier deadlock-prone `Lock` pattern.
- [x] `video_processor.py` + `database.py` + `hls_manager.py`: actual segment durations are persisted and used for playlist generation.
- [x] `app.py` + `video_processor.py`: disk space checks and cloudflared restart handling were added.
- [x] `app.py:/segment`: cache misses now stream through a temp-file backed single-flight download path instead of buffering the full Telegram response in memory per request.

## P3 — Data Model

- [x] `database.py`: subtitle `original_stream_index` is stored separately from HLS-facing `track_index`.
- [x] `database.py`: per-segment duration is stored in `segments.duration`.
- [x] `database.py`: the SQLite playback database now uses explicit schema revisions with ordered startup migrations and fail-closed handling for newer unknown schemas.

## P4 — Security Hardening

- [x] `app.py` + `hls_manager.py`: playback endpoints support HMAC-gated tokenized URLs.
- [x] `templates/index.html`: the fragile inline delete handler was replaced with delegated event handling and `data-job-id`.

## P5 — Operational

- [x] `app.py:/health`: a health endpoint exists and verifies database access.
- [ ] `app.py` + `telegram_uploader.py`: there is still no metrics surface for queue depth, Telegram API latency/error rates, cache hit rate, or active job counts.
- [ ] `database.py`: there is still no backup/export workflow for `streamer.db`.
- [x] `README.md` + `CLAUDE.md` + `.env.example`: docs now reflect the current config surface, queue/cache behavior, and playback/auth endpoints.
- [ ] Test environment: repository tests require undeclared local dependencies in this environment (`aiohttp`, `python-dotenv`, telegram package pieces), so verification is not reproducible from a bare Python install.

## P6 — New Features

- [ ] `config.py:load_bots`: bot discovery is still hardcoded to `TELEGRAM_BOT_TOKEN_1` through `_8`; larger pools require code changes instead of pure configuration.
- [ ] Thumbnail generation: there is still no thumbnail extraction, persistence, or proxying for the job list UI.
- [ ] Job re-processing: there is still no way to regenerate a completed job with new tiers/settings without re-uploading the source.
- [ ] Webhook notifications: there is still no completion callback for external automation.
- [ ] Multi-user support: jobs remain in a single global namespace with no user ownership model.
- [ ] Configurable per-job ABR tiers: ABR settings are still global config only.
- [ ] Download original: the system still cannot reconstruct and serve the original uploaded file from Telegram-backed artifacts.

## P7 — Code Quality

- [ ] Type coverage: most of the Flask app, processing pipeline, and database helpers still rely on untyped dicts/tuples instead of explicit types or typed models.
- [ ] Test coverage: there are strong unit tests around many regressions, but no runnable end-to-end pipeline/integration path in the current repo setup.
- [ ] Architecture: the codebase still mixes sync Flask request handling with async Telegram I/O and background worker state, which keeps concurrency and lifecycle logic spread across modules.
- [ ] `README.md` / `CLAUDE.md` drift: code and docs are no longer aligned, which increases maintenance cost and makes future regressions harder to review correctly.
