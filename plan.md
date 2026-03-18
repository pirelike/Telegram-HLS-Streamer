# Plan: Populate todo.md with Tiered Issues

## Goal
Audit the entire codebase and populate `todo.md` with concrete, actionable TODO items organized by the existing priority tiers (P0–P7).

## Approach
After thorough code review of all 7 Python modules + the HTML template, populate each tier with specific issues found. Each item references the relevant file and describes the problem concisely.

## Changes
Single file edit: `todo.md` — add the following items under each tier heading.

### P0 — Breaks Core Functionality
- `app.py`: Job cancellation check misidentifies timeout errors as cancellation (`_is_job_cancelled` checks for "timed out" string)
- `app.py`: Chunk upload can corrupt files — writes at offset without verifying file size, creating sparse file gaps
- `hls_manager.py`: Media playlist generation missing index validation for audio streams, can return None for valid tracks

### P1 — Security / Data Integrity
- `app.py`: Filename sanitization only uses `os.path.basename()` — insufficient against path traversal
- `app.py`: Source video deleted before verifying all Telegram uploads succeeded — unrecoverable on partial failure
- `telegram_uploader.py`: No integrity verification (checksum) of uploaded segments
- `database.py`: Partial segment saves possible if commit crashes mid-transaction — orphaned segments

### P2 — Reliability / Error Handling
- `app.py`: Event loop leaked on exception — `asyncio.new_event_loop()` not cleaned up if `run_until_complete()` fails (repeated in 3 places)
- `app.py`: Race condition in job timeout checker — modifies `_active_jobs` dict concurrently with processing thread
- `video_processor.py`: FFmpeg timeout kills process abruptly, truncates stderr to 500 chars — loses error context
- `video_processor.py`: Subtitle extraction failure silently skipped — user sees no indication subtitle was lost
- `database.py`: `get_segment()` returns None silently — no logging for segment retrieval failures

### P3 — Performance
- `telegram_uploader.py`: Directory listing done twice per segment type (count phase + upload phase)
- `hls_manager.py`: `list_jobs` pagination queries entire result set before applying LIMIT/OFFSET
- `app.py`: O(n) linear scan for duplicate filename on each upload init — bottleneck at scale

### P4 — Security Hardening
- `config.py`: Bot token format not validated beyond checking for `"your_"` prefix
- `app.py`: CORS_ALLOWED_ORIGINS can be empty string, producing malformed origin list
- `telegram_uploader.py`: No validation of file_id format before sending to Telegram API

### P5 — Maintainability
- `app.py`: Event loop creation pattern duplicated 3 times — extract to helper function
- `hls_manager.py`: Bitrate parsing logic duplicated — should cache computed bandwidths

### P6 — UX Improvements
- `app.py`: Timeout error message doesn't indicate which step timed out (analyzing, encoding, uploading)
- `templates/index.html`: No feedback when job list is empty vs. still loading — show "No uploads yet"
- `templates/index.html`: Resume UI shows chunk number but not how long upload has been pending

### P7 — New Features
- Add explicit job cancellation API endpoint (currently only timeout stops a job)
- Add per-segment upload progress indication (currently only total progress shown)
- Add retention policy / cleanup for old completed jobs (DB grows unbounded)
- Add batch/queue support for multiple video uploads
