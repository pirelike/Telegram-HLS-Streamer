# TODO

## P0 — Breaks Core Functionality

- [x] **`stream_analyzer.py`** — Fix `h265` vs `hevc` codec name mismatch → all HEVC videos re-encode unnecessarily or fail copy mode
- [x] **`stream_analyzer.py`** — Fix `mov_text` misclassified as text-based → subtitle extraction silently fails
- [x] **`telegram_uploader.py`** — Fix bot index inconsistency between upload and retrieval → segments served from wrong bot → 404s

## P1 — Security / Data Integrity

- [x] **`app.py`** — Fix path traversal on filename input → can write files outside `uploads/`
- [x] **`app.py`** — Fix silent file truncation on re-init → can destroy an in-progress upload
- [x] **`app.py` / `video_processor.py` / `telegram_uploader.py`** — Fix mismatched `progress_callback` signatures → runtime crashes
- [x] **`database.py`** — Add transaction rollback in `save_job()` → partial writes leave DB inconsistent

## P2 — Reliability / Error Handling

- [x] **`app.py`** — Add error handling to segment stream generator → clients get corrupted partial responses on network failure
- [x] **`telegram_uploader.py`** — Handle `BadRequest`, `NetworkError`, `Unauthorized` in retry logic (currently only `RetryAfter` and `TimedOut`)
- [x] **`app.py`** — Add job timeout → hung processing jobs stay in `"processing"` forever
- [x] **`app.py`** — Clean up orphaned `_pending_uploads` → disk fills over time with no TTL

## P3 — Performance

- [x] **`telegram_uploader.py`** — Parallelize uploads across bots → currently sequential despite 8 available bots (~8x speedup possible)
- [x] **`video_processor.py`** — Make video bitrate configurable → hardcoded `4M` is wrong quality for most videos

## P4 — Security Hardening

- [ ] **`app.py`** — Restrict CORS policy → wildcard `Access-Control-Allow-Origin: *` on segment endpoints
- [ ] **`app.py`** — Add optional API key / basic auth to upload endpoints → currently fully public
- [ ] **`config.py`** — Validate channel IDs are negative integers on startup

## P5 — Maintainability

- [ ] **`requirements.txt`** — Pin dependency upper bounds → unguarded `>=` risks silent breakage on major releases

## P6 — UX Improvements

- [ ] **`index.html`** — Add upload resume on page reload → progress lost if browser closes mid-upload
- [ ] **`index.html`** — Add pause/cancel button during upload
- [ ] **`index.html`** — Add client-side file format validation before upload begins
- [ ] **`video_processor.py`** — Add within-step FFmpeg progress percentage → currently only step-level progress
- [ ] **`index.html`** — Add pagination to job list → fetching all jobs at once won't scale

## P7 — New Features

- [ ] **`hls_manager.py` + `video_processor.py`** — Adaptive bitrate streaming (multiple quality tiers in master playlist)
