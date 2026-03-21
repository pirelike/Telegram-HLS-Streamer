# TODO — Active Backlog

Audit basis: `app.py`, `config.py`, `database.py`, `hls_manager.py`, `stream_analyzer.py`, `telegram_uploader.py`, `video_processor.py`, `templates/index.html`, `README.md`, `CLAUDE.md`, and the `tests/` suite.
Policy: application-level authentication is intentionally out of scope and should not be planned (no API key auth, no Basic auth, no playback-token auth).

## P0 — Critical Bugs

- [x] `telegram_uploader.py:_next_bot` + `app.py:_process_job`: the server accepts uploads even when no Telegram bots are configured and only fails after analysis/FFmpeg complete, wasting time and disk before surfacing the misconfiguration.
- [x] `hls_manager.py:generate_master_playlist`: track titles/languages are interpolated into HLS attribute strings without escaping quotes, commas, or newlines, so crafted media metadata can break playlists or inject invalid tags.

## P1 — Performance (High Impact)

- [x] `app.py:cancel_job` + `app.py:_process_job`: cancellation/timeouts are soft flags only; FFmpeg work and in-flight Telegram uploads keep running until their current phase ends, so cancelled jobs still burn CPU/network and may upload orphaned segments.
- [x] `app.py:_schedule_segment_prefetch` + `config.py:SEGMENT_PREFETCH_MIN_FREE_BYTES`: the low-free-memory prefetch guard is documented and configurable but never enforced, so cache-pressure behavior does not match config or docs.
- [x] `video_processor.py:_detect_hw_encoder`: hardware detection only checks whether an encoder name appears in `ffmpeg -encoders`; it never verifies device access or a real encode path, so partially configured VAAPI/QSV/NVENC hosts fail jobs instead of falling back cleanly.

## P2 — Reliability

- [x] `app.py:/api/upload/init`: pending-upload deduplication is still keyed only by sanitized basename, so different files with the same name can collide and incorrectly share one resumable session.
- [x] `app.py:_finalize_source_file`: upload-mode source files are deleted even after failed or cancelled jobs, which removes the only local copy and makes retry/debug impossible without re-uploading.
- [x] `stream_analyzer.py:SubtitleStream.is_text_based`: the subtitle codec whitelist excludes common text codecs such as `mov_text`, so valid subtitles from MP4 sources are silently skipped.

## P3 — Data Model

## P4 — Security Hardening

- [ ] `config.py:BEHIND_PROXY`: this defaults to `true`, so direct deployments trust spoofed `X-Forwarded-For` / `X-Forwarded-Proto` headers and weaken rate limiting, per-IP pending-upload caps, and generated base URLs unless the app is actually behind a trusted proxy.
- [ ] `config.py:CLOUDFLARED_ENABLED` + `app.py:__main__`: public quick tunnels are enabled by default and start automatically whenever `cloudflared` is installed, which can expose the service unexpectedly.

## P5 — Operational

- [ ] `app.py` + `telegram_uploader.py`: there is still no metrics surface for queue depth, Telegram API latency/error rates, cache hit rate, or active job counts.
- [ ] `config.py:load_bots`: bot discovery is still hardcoded to `TELEGRAM_BOT_TOKEN_1` through `_8`; larger pools require code changes instead of pure configuration.

## P6 — New Features

- [ ] Thumbnail generation: there is still no thumbnail extraction, persistence, or proxying for the job list UI.
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
