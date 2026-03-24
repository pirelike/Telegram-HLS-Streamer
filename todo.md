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

- [ ] `database.py:631-649`: N+1 subqueries in `list_jobs()` standard episode path — 3 correlated `COUNT(*)` subqueries (audio tracks, subtitle tracks, segment count) execute per row, plus a `MAX(created_at)` subquery per series-grouped job. A 50-job page issues ≈200 SQL statements. Fix: rewrite as a single query using `LEFT JOIN … GROUP BY` or a `WITH` CTE that aggregates counts before joining to jobs.
- [ ] `database.py:600-628`: Series and season grouping queries fire two identical correlated subqueries per group to retrieve the representative `job_id` and `has_thumbnail`. Browsing a series list with many seasons becomes O(groups²) in SQL work. Fix: replace correlated subqueries with a `ROW_NUMBER()` window function or a pre-computed sub-select joined once.
- [ ] `database.py:241`: Missing composite index on `tracks(job_id, track_type)` — the N+1 COUNT subqueries in the episode list must scan all rows for a given `job_id` to filter by `track_type`; only `idx_tracks_job(job_id)` exists today. Fix: `CREATE INDEX IF NOT EXISTS idx_tracks_job_type ON tracks(job_id, track_type)` (new schema migration).
- [ ] `database.py`: Missing index on `jobs(series_name)` — correlated subqueries in the series/season grouping paths do `WHERE j2.series_name = j.series_name` with no covering index, causing full `jobs` table scans for every group row. Fix: `CREATE INDEX IF NOT EXISTS idx_jobs_series ON jobs(series_name)` (new schema migration).
- [ ] `app.py:874` + `config.py:273-322`: `Config.reload()` is called on every `POST /api/settings`, unconditionally re-reading the `.env` file, re-parsing all 31 settings, rescanning all `TELEGRAM_BOT_TOKEN_*` env vars, re-querying the full `settings` and `bots` tables, and rebuilding all `python-telegram-bot` Bot instances. Fix: diff the incoming changes against current config; only reload bot clients when a bot-related key actually changed.
- [ ] `app.py:1898-1907`: `_SegmentCache.put()` holds `self._lock` for the entire eviction loop — every `popitem()` in the while loop runs under the lock, blocking concurrent `get()` calls on all other threads for the duration of the eviction pass. Fix: collect eviction candidates with the lock held (snapshot `_current_bytes` and identify keys to drop), release the lock, then re-acquire briefly to apply deletions.

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
