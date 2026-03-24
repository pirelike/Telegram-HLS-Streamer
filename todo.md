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

- [ ] `app.py:406–420,2010–2015`: `_aiohttp_session` recreated without a lock and outside the async event loop — two concurrent Flask threads can both see the session as `None`/`closed`, each call `aiohttp.ClientSession()`, and only one assignment survives while the other leaks open sockets; creating a `ClientSession` from a sync Flask thread is also incorrect per aiohttp's event-loop affinity requirement. Fix: schedule session creation via `_run_async()` inside the persistent async loop and protect the recreation check with a `threading.Lock` so only one coroutine ever creates the session.
- [ ] `app.py:1447–1485`: upload finalization leaks the assembled temp file when `_queue_local_file()` raises — `_remove_pending_upload()` is called at line 1468 before `_queue_local_file()`, so if queuing throws, the `upload_id` is gone from tracking but the file in `uploads/` is never deleted and the per-IP counter has already been decremented, leaving orphaned disk usage the cleanup task will not find. Fix: wrap `_queue_local_file()` in a try/except that deletes the temp file and re-raises, or move `_remove_pending_upload()` to after a successful enqueue.
- [ ] `app.py:162–175`: `_request_job_stop()` calls `future.cancel()` on in-flight Telegram upload futures but never handles the case where `cancel()` returns `False` (future already running) — those uploads continue in the background, pushing segments to Telegram that are never registered in the DB and become permanently orphaned in the channel. Fix: after cancellation, await remaining futures with a short timeout inside the async loop so the uploader's cancel-event check can terminate them cleanly before the job is marked finished.
- [ ] `app.py:1275–1343`: watch-folder polling crashes the entire watch thread when a file disappears between directory scan and stability check — `_watch_file_signature()` calls `os.stat()` with no exception handling, so a `FileNotFoundError` or `PermissionError` propagates through `_claim_watch_file_if_stable()` into the top-level poll loop and kills auto-ingest until the app is restarted. Fix: wrap `os.stat()` in a try/except returning `None` on any OS error, and treat a `None` signature in `_claim_watch_file_if_stable()` as "file gone — skip".
- [ ] `telegram_uploader.py:169–199,320–340`: `reload_bots()` replaces `self.bots` and nulls `self._bot_locks` without a lock — a concurrent upload task that already read a `bot_index` against the old list length will index into the newly built (possibly shorter) `_bot_locks`, raising `IndexError`; two tasks that both see `_bot_locks is None` will each allocate a new lock list and the second write discards the first, letting two uploads proceed to the same bot concurrently and defeating per-bot serialisation. Fix: protect `self.bots`, `self._bot_counter`, and `self._bot_locks` under a single `threading.Lock` held across `reload_bots()`, `_next_bot()`, and the lazy `_bot_locks` initialisation.
- [ ] `video_processor.py:907–933`: when one ABR tier encode fails, `cancel_event.set()` is called but already-running `ThreadPoolExecutor` workers cannot be interrupted (`future.cancel()` returns `False`), so those threads continue writing `.ts` files into `processing/<job_id>/` alongside any already-complete tiers; if `cleanup()` then races against a still-writing thread, `shutil.rmtree()` can fail or leave a partial directory that contaminates a subsequent run. Fix: after the `ThreadPoolExecutor` context exits (all workers done or timed out), unconditionally invoke `cleanup(job_id)` before re-raising so partial output is removed in the failure path.
- [ ] `video_processor.py:1022–1027`: `cleanup()` calls `shutil.rmtree()` with no exception handling — if any file in the processing directory is still held open by a lingering FFmpeg subprocess or encode thread, the `rmtree` raises and the exception propagates into the caller's `finally` block, silently replacing the original job-failure exception and making root-cause diagnosis impossible. Fix: wrap `shutil.rmtree()` in a try/except, log a warning on failure, and ensure the function always returns without raising so it is safe to call from `finally` blocks.
- [ ] `database.py:33–65`: thread-local SQLite connections have no staleness detection — if the DB file is moved or a WAL-recovery event invalidates the cached handle, the stale `_local.conn` passes the `if not hasattr(…) or _local.conn is None` guard and all subsequent queries on that thread raise `sqlite3.OperationalError` with no automatic reconnection; `_handle_corrupt_db()` calls `_reset_conn()` only for the calling thread, leaving all other threads' connections in an unknown broken state. Fix: in `_get_conn()`, catch `sqlite3.OperationalError`, call `_reset_conn()`, and retry the connection once before re-raising so threads self-heal after transient DB file disruptions.
- [ ] `app.py:1210–1221`: zero-count entries in `_pending_uploads_per_ip` (a `defaultdict`) are never removed — over a long-running session the dict accumulates one entry per distinct client IP that has ever submitted an upload, growing without bound and inflating memory usage on busy instances. Fix: after decrementing, add `if _pending_uploads_per_ip[ip] == 0: del _pending_uploads_per_ip[ip]` to remove exhausted entries immediately.

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
