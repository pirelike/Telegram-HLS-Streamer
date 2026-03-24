# TODO — Active Backlog

Audit basis: `app.py`, `config.py`, `database.py`, `hls_manager.py`, `stream_analyzer.py`, `telegram_uploader.py`, `video_processor.py`, `templates/index.html`, `README.md`, `CLAUDE.md`, and the `tests/` suite.
Policy: application-level authentication is intentionally out of scope and should not be planned (no API key auth, no Basic auth, no playback-token auth).

## P0 — Critical Bugs

- [x] `video_processor.py`: copy mode + ABR interaction fixed — when `ENABLE_COPY_MODE=true`, ABR tiers are now filtered to strictly lower resolutions than the source (same-resolution tiers are excluded since tier 0 already covers it via passthrough).
- [ ] `hls_manager.py`: segment_key values are embedded in M3U8 playlist lines without escaping — a key containing `\n` or `#` corrupts the playlist and breaks playback for the affected job.
- [ ] `hls_manager.py`: subtitle playlists emit `#EXTINF:0.000,` when job duration is NULL or 0, which is invalid per the HLS spec and causes players to reject or skip the subtitle track.
- [ ] `hls_manager.py`: `BANDWIDTH` attribute in the master playlist is set to `file_size * 8` (un-divided) when duration is 0 or NULL, producing an astronomically large value that causes player quality-selection failures.
- [ ] `app.py`: the global `_aiohttp_session` is recreated without a lock — multiple concurrent coroutines can each create a new `ClientSession`, leaking the earlier sessions as open sockets until the OS reclaims them, eventually exhausting the connection pool.
- [ ] `app.py`: temp files created by `tempfile.mkstemp()` inside the segment download path are not reliably cleaned up when the download task is cancelled or times out, leading to gradual disk exhaustion.
- [ ] `stream_analyzer.py`: `stream["index"]` uses bare dict access; if ffprobe omits the `index` field for any stream object (seen with some containers), an unhandled `KeyError` crashes the analysis stage and permanently fails the job.

## P1 — Performance (High Impact)

## P2 — Reliability

## P3 — Data Model

## P4 — Security Hardening

## P5 — Operational

- [x] `app.py` + `telegram_uploader.py`: metrics surface added — `/api/metrics` exposes queue depth, cache hit/miss/eviction counts, prefetch pending, and Telegram upload/download counters.
- [x] `config.py:load_bots`: bot discovery is still hardcoded to `TELEGRAM_BOT_TOKEN_1` through `_8`; larger pools require code changes instead of pure configuration.

## P6 — New Features

- [x] Thumbnail generation: FFmpeg extraction, Telegram upload, DB persistence (`has_thumbnail`), and proxy endpoint (`/thumbnail/<job_id>`) are all implemented.
- [ ] Thumbnail UI polish: dedicated per-series/per-episode thumbnail display and fallback placeholder in the job browser could be improved.
- [ ] Job re-processing: there is still no way to regenerate a completed job with new tiers/settings without re-uploading the source.
- [ ] Webhook notifications: there is still no completion callback for external automation.
- [ ] Configurable per-job ABR tiers: ABR settings are still global config only.
- [ ] Download original: the system still cannot reconstruct and serve the original uploaded file from Telegram-backed artifacts.

## P7 — Code Quality

- [ ] `templates/index.html` + `config.py:UPLOAD_CHUNK_SIZE`: the bundled frontend hardcodes a 10 MB chunk size instead of reading it from the server, so changing upload chunk config can desynchronize the UI and backend.
- [ ] `tests/`: `python -m unittest` runs zero tests in this repo; the suite only executes under explicit discovery (`python -m unittest discover -s tests -p 'test_*.py'`), which makes the default stdlib test command misleading for local runs and CI.
- [ ] `tests/test_app_p0_todos.py`: the "minimal environment" test module stubs Telegram/aiohttp/dotenv but still hard-imports Flask via `app.py`, so the suite is not actually runnable in the reduced-dependency environment the code otherwise tries to support.
- [ ] Type coverage: most of the Flask app, processing pipeline, and database helpers still rely on untyped dicts/tuples instead of explicit types or typed models.
- [ ] Test coverage: there are strong unit tests around many regressions, but no runnable end-to-end pipeline/integration path in the current repo setup.
- [ ] Architecture: the codebase still mixes sync Flask request handling with async Telegram I/O and background worker state, which keeps concurrency and lifecycle logic spread across modules.
- [x] `README.md` / `CLAUDE.md` drift: code and docs are no longer aligned, which increases maintenance cost and makes future regressions harder to review correctly.
