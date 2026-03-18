# Review: PRs #15, #16, #17 — P0 Upload/Cancellation Fixes & Playlist Validation

## Overview

Three merged PRs (all with commit message "Fix P0 upload/cancellation issues and validate playlist stream indexes") contain identical changes across `app.py`, `hls_manager.py`, and new tests.

**Files changed:** `app.py`, `hls_manager.py`, `tests/test_app_p0_todos.py`, `tests/test_database_hls_manager.py`, `todo.md`

---

## 1. Cancellation Detection (`app.py:158-174`)

**Change:** Replaced substring matching (`"timed out" in error`) with explicit flags (`job.get("cancelled") or job.get("timed_out")`).

**Positive:** Eliminates misclassification — error messages containing "timed out" no longer falsely trigger cancellation.

**Finding — `cancelled` flag is dead code:** `_is_job_cancelled` checks `job.get("cancelled")` but nothing in the codebase ever sets `job["cancelled"] = True`. Only `job["timed_out"]` is set by the timeout watcher. The `cancelled` branch is unreachable. Either add a cancel mechanism that sets this flag, or remove the check until that feature exists.

---

## 2. Chunked Upload Hardening (`app.py:284-338`)

**Changes:**
- `X-Chunk-Index` validated as non-negative integer
- Offset/size boundary checks against `total_size`
- Gap detection: rejects chunks that would leave holes (`offset > current_size`)
- Overlap detection: rejects non-retry chunks where `offset < current_size`
- Retry tracking via `received_chunk_indices` set (avoids double-counting bytes)
- Non-final chunk size validation

**Positive:** Prevents sparse file gaps from out-of-order writes and eliminates corrupt uploads from partial/oversized chunks.

**Findings:**

- **TOCTOU race on gap/overlap detection:** `os.path.getsize()` is read, then the file is opened and written in a separate step. Under concurrent requests for the same upload (e.g., client retries while original is in-flight), two requests could pass the check simultaneously. Safe with Flask dev server (single-threaded) but vulnerable in production with threaded workers (gunicorn). Fix: add a `threading.Lock` per upload.

- **Out-of-order uploads intentionally broken:** The old code allowed arbitrary-order chunk writes. The new gap detection (`offset > current_size → 409`) enforces strictly sequential uploads. This is a valid hardening choice but represents a behavioral change. The client already sends chunks sequentially, so this is compatible, but parallel chunk uploading is now impossible.

- **`received_chunk_indices` not persisted:** The set lives in memory. Server restart mid-upload loses retry tracking. Not a regression (old code had no persistence either), but worth noting.

---

## 3. HLS Playlist Stream Index Validation (`hls_manager.py:192-272`)

**Changes:**
- `stream_index` coerced to `int` with `TypeError`/`ValueError` handling
- Negative index check
- Track existence verified via new `_get_track` helper for audio and subtitle
- `_get_track` queries `db.get_job_tracks()` and matches by `track_index`

**Positive:** Prevents returning playlists for nonexistent tracks. Previously, requesting `/hls/<job_id>/audio_999.m3u8` would query segments with prefix `audio_999`, potentially returning empty/malformed playlists.

**Finding — Video tracks not validated:** Audio and subtitle branches call `_get_track` to verify the track exists before proceeding. The video branch does not. A request for `/hls/<job_id>/video_999.m3u8` bypasses track validation and falls through to `get_segments_for_prefix` which returns empty → `None`. Functionally safe but inconsistent: audio/sub fail early ("track not found"), video fails late ("no segments"). Adding `_get_track(job_id, "video", stream_index)` would make behavior uniform.

---

## 4. Tests

### `tests/test_app_p0_todos.py` (new, 114 lines)

- **Telegram stub:** Clever `sys.modules.setdefault` approach to mock `telegram` and `telegram.error` so `app.py` imports cleanly in test environments without the real package.
- **`test_is_job_cancelled_uses_explicit_flag_not_error_text`:** Verifies flag-based detection — error text alone no longer triggers cancellation.
- **`test_upload_chunk_rejects_out_of_order_gap`:** Verifies 409 on gap, then success on in-order sequence.
- **`test_upload_chunk_retry_does_not_double_count`:** Verifies `received_chunks` stays at 1 after retry, `is_retry` flag correct.

**Missing coverage:**
- No test for the overlap rejection path (`offset < current_size and not is_retry`)
- No test for chunk boundary violations (`offset >= total_size`, `offset + chunk_len > total_size`)
- No test for non-final chunk size validation
- No test for negative chunk index

### `tests/test_database_hls_manager.py` (2 lines added)

- Added assertions for `"not-an-int"` and `-1` stream indexes returning `None`.

### Known issue

Combined test run (`test_app_p0_todos` + `test_database_hls_manager`) fails due to fixture mismatch in the latter — `processing_result.video_playlists` expected by `database.save_job` is missing from existing test fixtures. Not addressed in these PRs.

---

## 5. Process Note

Three PRs (#15, #16, #17) with identical diffs were merged. This appears to be repeated submission attempts. A single PR would have been cleaner for git history.

---

## Recommendations

1. **Remove or implement `cancelled` flag** — currently dead code in `_is_job_cancelled`
2. **Add per-upload locking** for thread safety in production deployments
3. **Add `_get_track` validation for video** in `generate_media_playlist` for consistency
4. **Expand test coverage** — overlap rejection, boundary violations, negative index
5. **Fix test fixture mismatch** in `test_database_hls_manager` so tests can run together
6. **Squash or clean up** the triple-PR pattern in future submissions
