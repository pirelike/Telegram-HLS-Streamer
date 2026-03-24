# P5 — Operational Backlog

Extracted from `todo.md` and supplemented by a codebase audit of `app.py`, `config.py`,
`database.py`, `telegram_uploader.py`, `video_processor.py`, `hls_manager.py`, and `stream_analyzer.py`.

---

## Items from `todo.md`

- [x] `app.py` + `telegram_uploader.py`: metrics surface added — `/api/metrics` exposes queue depth,
  cache hit/miss/eviction counts, prefetch pending, and Telegram upload/download counters.
- [x] `config.py:load_bots`: bot discovery is no longer hardcoded to `TELEGRAM_BOT_TOKEN_1`–`_8`;
  the loader now iterates env vars without an upper limit, so larger pools are pure configuration.

---

## Additional Items Found by Codebase Audit

- [ ] `database.py:23` — `streamer.db` is the sole mapping of `segment_key → file_id`; losing it
  makes ALL uploaded Telegram content permanently inaccessible. There is no periodic backup,
  export endpoint, or restore workflow. Fix: implement a `/api/db/backup` endpoint that streams a
  live SQLite backup (using the `sqlite3.Connection.backup()` API) and schedule an automatic
  on-disk copy to a configurable `DB_BACKUP_PATH` on a user-defined interval. Document the
  restore procedure in `README.md`. (Flagged as Roadmap #1 in `CLAUDE.md`.)

- [ ] `config.py:159` / `app.py:599–619` — `JOB_RETENTION_DAYS` defaults to `0` (keep forever).
  With no retention policy set, `streamer.db` and the associated Telegram channel grow without
  bound; there is also no warning at startup when the DB exceeds a configurable size threshold.
  Fix: log a startup warning when `JOB_RETENTION_DAYS == 0` and `jobs` row count exceeds a
  threshold (e.g. 1 000); optionally expose a `GET /api/metrics` field for total DB file size so
  operators can monitor growth.

- [ ] `app.py:326` — watch-folder settings load failure is silently swallowed:
  `logger.warning("Could not load persisted watch settings: %s", exc)`. If the `settings` table
  row is corrupted or missing, the watch folder falls back to defaults with no operator
  notification beyond a log line. Fix: surface the failure in `GET /api/watch-settings` response
  (e.g. an `"error"` field) so the UI can display a banner prompting the operator to reconfigure.

- [ ] `app.py:1206` — disk-space pre-check failure is silently ignored:
  `logger.warning("Could not check disk space: %s", e)`. When `shutil.disk_usage()` raises
  (e.g. on unusual mount points), the job proceeds without knowing whether there is enough space,
  risking mid-encode failures that waste Telegram quota on partial uploads. Fix: treat a failed
  disk-usage call the same as insufficient space (reject the job with a clear error) unless
  `SEGMENT_PREFETCH_MIN_FREE_BYTES == 0`, in which case log and proceed.

- [ ] `app.py:2528–2537` — Cloudflared tunnel restart warnings are log-only; there is no
  structured signal in `/health` or `/api/metrics` when the tunnel has exited or is restarting.
  Operators running the app behind Cloudflared have no machine-readable way to detect a broken
  tunnel without tailing logs. Fix: expose a `"cloudflared": {"status": "running"|"restarting"|"disabled"}`
  field in the `/health` response so monitoring tools can alert on tunnel outages.

- [ ] `app.py` (logging) — the application logs to stdout/stderr via the root logger with no
  rotation, size limit, or structured format. Long-running deployments produce unbounded log
  output that must be managed entirely by the host OS or container runtime. Fix: add an optional
  `LOG_FILE` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` env-var–driven `RotatingFileHandler` in
  `app.py` startup, defaulting to stdout-only (no behaviour change unless configured).
