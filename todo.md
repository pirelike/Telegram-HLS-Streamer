# TODO

## P0 — Breaks Core Functionality

- [ ] `video_processor.py`: VAAPI hardware encoding with ABR scaling uses wrong filter chain — `scale` instead of `scale_vaapi` with required `hwupload`/`hwdownload` filters, causing FFmpeg failure when VAAPI + resolution scaling are both active
- [ ] `app.py`: Upload finalize allows 1% file size tolerance (`actual_size < expected_size * 0.99`) — for a 100GB file, up to 1GB of data could be missing and still pass validation; should verify exact size or check `received_chunks == total_chunks`

## P1 — Security / Data Integrity

- [ ] `templates/index.html`: XSS vulnerability in `renderJobItem` — server filename is interpolated directly into `innerHTML` (`${j.filename || id}`) without HTML escaping; `secure_filename` strips most characters but defense-in-depth requires escaping at render time
- [ ] `app.py`: `total_size` and `total_chunks` in upload init are not safely validated — `int(data["total_size"])` raises unhandled `ValueError` on non-numeric input, returning a raw 500 to the client instead of a 400
- [ ] `app.py`: `register_job` failure after Telegram upload completes leaves segments orphaned — segments are on Telegram with no DB record and the source file is deleted in the `finally` block, making recovery impossible

## P2 — Reliability / Error Handling

- [ ] `app.py`: `_active_jobs` dict grows unboundedly — completed and errored jobs are never removed from the in-memory dict, causing slow memory leak over long-running server instances
- [ ] `app.py`: `_pending_uploads` iterated without a lock in `_cleanup_expired_pending_uploads` — concurrent upload chunk requests can modify the dict during iteration, risking `RuntimeError: dictionary changed size during iteration`
- [ ] `app.py`: New `TelegramUploader` instance created on every segment proxy request (line 744) — each instantiation creates fresh `Bot` objects, wasting resources and connections; should use a shared singleton
- [ ] `database.py`: Thread-local database connections are never explicitly closed — when worker threads terminate, SQLite connections may not be properly cleaned up

## P3 — Performance

- [ ] `video_processor.py`: `_detect_hw_encoder()` spawns `ffmpeg -encoders` subprocess on every job — hardware capabilities don't change at runtime; result should be cached after first probe
- [ ] `hls_manager.py`: `list_jobs` makes 2N+1 database queries — calls `db.get_job_tracks()` twice per job (audio + subtitle tracks) even though `db.list_jobs()` already returns `audio_count` and `subtitle_count` via efficient subqueries; should use the counts directly or batch-fetch tracks

## P4 — Security Hardening

- [ ] `app.py`: CORS `Access-Control-Allow-Methods` and `Access-Control-Allow-Headers` are sent even when the origin is not allowed — these headers should only be present alongside `Access-Control-Allow-Origin`
- [ ] `app.py`: No rate limiting on upload init or chunk endpoints — a malicious client can exhaust disk space by creating unlimited pending uploads

## P5 — Maintainability

- [ ] `telegram_uploader.py`: `TelegramUploader` is instantiated in two separate places (`_process_job` for upload and `serve_segment` for download) with no shared state — extracting a module-level singleton would simplify connection management and reduce Bot instantiation overhead

## P6 — UX Improvements

- [ ] `templates/index.html`: Job list doesn't show duration or file size — `renderJobItem` only displays audio/subtitle/segment counts despite the API returning duration and file_size metadata
- [ ] `templates/index.html`: No visual feedback when delete request is in-flight — user can click delete multiple times; button should show loading state

## P7 — New Features
