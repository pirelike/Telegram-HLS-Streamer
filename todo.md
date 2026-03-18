# TODO

## P0 — Breaks Core Functionality

- [ ] `app.py`: Job cancellation check misidentifies timeout errors as cancellation — `_is_job_cancelled` matches "timed out" string instead of using an explicit cancellation flag
- [ ] `app.py`: Chunk upload can corrupt files — writes at calculated offset without verifying file size, creating sparse file gaps on out-of-order chunks
- [ ] `hls_manager.py`: Media playlist generation missing stream_index validation for audio streams — can return None for valid audio tracks

## P1 — Security / Data Integrity

- [ ] `app.py`: Filename sanitization only uses `os.path.basename()` — insufficient against path traversal attacks
- [ ] `app.py`: Source video file deleted before verifying all Telegram segment uploads succeeded — unrecoverable on partial failure
- [ ] `telegram_uploader.py`: No integrity verification (checksum) of uploaded segments — corruption during transmission goes undetected
- [ ] `database.py`: Partial segment saves possible if commit crashes mid-transaction — can leave orphaned segments with inconsistent state

## P2 — Reliability / Error Handling

- [ ] `app.py`: Event loop leaked on exception — `asyncio.new_event_loop()` not cleaned up if `run_until_complete()` fails (repeated in 3 places: upload + 2 segment proxies)
- [ ] `app.py`: Race condition in job timeout checker — modifies `_active_jobs` dict concurrently with processing thread
- [ ] `video_processor.py`: FFmpeg timeout kills process abruptly and truncates stderr to 500 chars — loses error context for debugging
- [ ] `video_processor.py`: Subtitle extraction failure silently skipped — user sees no indication a subtitle track was lost
- [ ] `database.py`: `get_segment()` returns None silently — no logging for segment retrieval failures, makes debugging proxy 404s difficult

## P3 — Performance

- [ ] `telegram_uploader.py`: Directory listing done twice per segment type — once for counting, once for uploading
- [ ] `hls_manager.py`: `list_jobs` pagination queries entire result set before applying LIMIT/OFFSET
- [ ] `app.py`: O(n) linear scan for duplicate filename on each upload init — bottleneck with many concurrent uploads

## P4 — Security Hardening

- [ ] `config.py`: Bot token format not validated beyond checking for `"your_"` prefix — malformed tokens only fail at runtime
- [ ] `app.py`: CORS_ALLOWED_ORIGINS splitting on empty string produces malformed origin list
- [ ] `telegram_uploader.py`: No validation of file_id format before sending to Telegram API — corrupted DB entries cause unexpected errors

## P5 — Maintainability

- [ ] `app.py`: Event loop creation pattern duplicated 3 times — extract to helper function
- [ ] `hls_manager.py`: Bitrate parsing logic duplicated — should cache computed bandwidths

## P6 — UX Improvements

- [ ] `app.py`: Timeout error message doesn't indicate which pipeline step timed out (analyzing, encoding, or uploading)
- [ ] `templates/index.html`: No feedback when job list is empty vs. still loading — should show "No uploads yet"
- [ ] `templates/index.html`: Resume UI shows chunk number but not how long the upload has been pending

## P7 — New Features

- [ ] Add explicit job cancellation API endpoint — currently only timeout stops a running job
- [ ] Add per-segment upload progress indication — currently only total progress is shown
- [ ] Add retention policy / cleanup for old completed jobs — database grows unbounded
- [ ] Add batch/queue support for multiple concurrent video uploads
