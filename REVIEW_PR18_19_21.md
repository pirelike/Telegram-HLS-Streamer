# Review: PRs #18, #19, #21 — Follow-up Fixes to P0 Review

PRs #19 and #21 (from branch `codex/fix-errors-in-pr-review`) address issues raised in the PR #18 review. PR #18 was the review document itself.

**Files changed:** `app.py`, `hls_manager.py`, `video_processor.py`, `telegram_uploader.py`, `tests/test_app_p0_todos.py`, `tests/test_config_video_processor.py`, `tests/test_database_hls_manager.py`, `tests/test_telegram_uploader.py`

---

## 1. Review Items Addressed

### 1a. Dead `cancelled` flag removed — FIXED
`_is_job_cancelled` now only checks `job.get("timed_out")` (app.py:173). The unused `cancelled` branch is gone. Clean fix.

### 1b. Per-upload locking added — FIXED
A `_upload_locks` dict maps `upload_id → threading.Lock()`. The critical section (retry detection, gap/overlap check, file write, counter update) is wrapped in `with upload_lock:` (app.py:308-331). Locks are created at upload init and cleaned up at finalize and expiry. Correct implementation.

### 1c. Video track validation in HLS — FIXED
`generate_media_playlist` now calls `_get_track(job_id, "video", stream_index)` for video requests (hls_manager.py:211-213), consistent with audio/subtitle branches. Tests added for invalid video indexes.

### 1d. Test coverage expanded — FIXED
New tests in `test_app_p0_todos.py`:
- `test_upload_chunk_rejects_negative_chunk_index` — validates 400 on `-1`
- `test_upload_chunk_rejects_overlap_non_retry` — validates 409 on overlap
- `test_upload_chunk_rejects_boundary_violations` — validates offset and size bounds
- `test_upload_chunk_rejects_non_final_partial_chunk` — validates chunk size for non-final

All four gaps from the prior review are now covered.

### 1e. Test fixture mismatch — FIXED
`test_database_hls_manager.py` fixtures updated with `video_playlists` tuple. Track count assertion updated from 2 to 3 (now includes the video track). Master playlist test updated to expect `/hls/job4/video_0.m3u8` instead of `/hls/job4/video.m3u8`.

---

## 2. Additional Changes (Beyond Review Follow-ups)

### 2a. `video_processor.py` — `_build_video_cmd` return value change

`_build_video_cmd` was changed from returning `(cmd, playlist, tier_dir)` to `(cmd, playlist)`. The caller now derives `tier_dir` via `os.path.dirname(playlist)`.

**BUG: Test `test_build_video_cmd_variants` still unpacks 3 values.** The test at `tests/test_config_video_processor.py:80` does `cmd, playlist, _ = vp._build_video_cmd(...)` but the function only returns 2 values. **This test fails on `main` with `ValueError: not enough values to unpack (expected 3, got 2)`.**

Verified by running the full test suite — 32 pass, 1 fails:
```
ERROR: test_build_video_cmd_variants
ValueError: not enough values to unpack (expected 3, got 2)
```

### 2b. `video_processor.py` — Audio encoding change

Audio processing changed from unconditional lossless copy to conditional:
- If `Config.ENABLE_COPY_MODE` and `audio_stream.is_copy_compatible` → copy
- Otherwise → re-encode to AAC at `Config.AUDIO_BITRATE` (default 128k)

This is a **behavioral change** that contradicts CLAUDE.md which states "Audio is always copied losslessly (never re-encoded)". Uses `getattr` with fallbacks suggesting `AUDIO_BITRATE` may not exist in Config yet.

### 2c. `video_processor.py` — Defensive `getattr` additions

- `source_height = getattr(analysis.video_streams[0], "height", 0) or 0`
- `source_width = getattr(analysis.video_streams[0], "width", 0) or 0`
- `media_duration = getattr(analysis, "duration", 0) or 0`

These guard against missing attributes in test fixtures (SimpleNamespace). Reasonable but masks real data issues — if `height` is genuinely None/missing in production, tiers will be computed from 0, producing no ABR tiers (only tier 0). This is actually safe behavior (fall back to original-only).

### 2d. `video_processor.py` — `ProcessingResult` backward compatibility

Added `video_playlist` setter and `_legacy_video_playlist` attribute for backward compatibility:
```python
@video_playlist.setter
def video_playlist(self, playlist_path):
    self._legacy_video_playlist = playlist_path
```

And `all_segment_dirs()` falls back to `self.output_dir` if `video_playlists` is empty but legacy `video_playlist` exists. This is a backward-compat shim that CLAUDE.md advises against ("Avoid backwards-compatibility hacks").

### 2e. `telegram_uploader.py` — Exception handler reordering

The `except` clauses in `_upload_file` were reordered: `BadRequest` and `Unauthorized` (non-retryable) now come before `TimedOut`, `NetworkError`, and `RetryAfter` (retryable). This is correct — non-retryable exceptions should be caught first to avoid accidentally falling into retry logic. Previously, `RetryAfter` was caught first, which is fine since Python matches except clauses top-down, but the new order is clearer.

Also: `RetryAfter` now uses `getattr(e, "retry_after", 1)` instead of `e.retry_after` — defensive against API version changes.

### 2f. `telegram_uploader.py` — Lazy bot lock initialization

`_upload_file_with_bot_lock` now lazily creates `_bot_locks` if None:
```python
if self._bot_locks is None:
    self._bot_locks = [asyncio.Lock() for _ in self.bots]
```
This handles the case where `_bot_locks` initialization was skipped (e.g., in tests with mocked constructors).

### 2g. `telegram_uploader.py` — ABR-aware upload

Video segment upload loop changed from using `processing_result.video_playlist` (single) and `output_dir` to iterating `processing_result.video_playlists` (multiple tiers), each with its own `tier_dir`. Segment keys changed from `video/filename` to `video_{i}/filename`. This correctly aligns upload paths with the ABR tier directory structure.

---

## 3. Issues Found

| # | Severity | Description |
|---|----------|-------------|
| 1 | **HIGH** | `test_build_video_cmd_variants` fails on main — unpacks 3 values from 2-value return |
| 2 | **MEDIUM** | Audio encoding behavioral change contradicts CLAUDE.md documentation |
| 3 | **LOW** | `ProcessingResult.video_playlist` setter is a backward-compat shim (CLAUDE.md advises against) |
| 4 | **LOW** | `AUDIO_BITRATE` config accessed via `getattr` fallback — not defined in `Config` class |

---

## 4. Recommendations

1. **Fix the broken test immediately** — change `cmd, playlist, _ = vp._build_video_cmd(...)` to `cmd, playlist = vp._build_video_cmd(...)` in `test_build_video_cmd_variants`
2. **Update CLAUDE.md** to reflect the new audio encoding behavior (conditional copy vs always-copy)
3. **Add `AUDIO_BITRATE` to `config.py`** with a default value instead of using `getattr` fallback
4. **Consider removing the `video_playlist` setter** — if all callers now use `video_playlists`, the backward-compat shim is unnecessary
