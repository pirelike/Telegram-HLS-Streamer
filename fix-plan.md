# Fix Plan — Issues Introduced by Commit d30bf07

Commit: `d30bf07` — "Fix P1 security and data integrity errors"
Branch: `fix/p1-security-errors` (merged via PR #24)

This plan covers the three regressions introduced by the commit, ordered by severity.

---

## Issue 1 — Source file never deleted on job failure (HIGH)

### What happened

The commit removed this block from the `except` branch of `_process_job` in `app.py`:

```python
# REMOVED:
if os.path.exists(file_path):
    os.remove(file_path)
```

The intent was correct — the file should not be deleted before confirming uploads succeeded.
But no replacement cleanup was added for the failure path.
`_cleanup_expired_pending_uploads` cannot help here because the file has already been
removed from `_pending_uploads` during `upload_finalize` (line 360), before `_process_job`
even starts. This means every failed job permanently leaks its full source video file
(up to 100 GB) until someone manually clears `uploads/`.

### Fix

Move cleanup into a `finally` block that always runs, but only removes the file after
the pipeline has either completed successfully or given up. This guarantees cleanup
on both success and failure paths without resurrecting the original bug.

**File:** `app.py`, function `_process_job`

```python
def _process_job(job_id, file_path):
    """Full pipeline: analyze -> process -> upload -> register."""
    try:
        # ... existing pipeline steps unchanged ...

        # Cleanup temp files (success path)
        cleanup(job_id)

        _active_jobs[job_id]["status"] = "complete"
        _active_jobs[job_id]["progress"] = 100
        _active_jobs[job_id]["step"] = "Done"
        logger.info("Job %s complete", job_id)

    except Exception as e:
        if _is_job_cancelled(job_id):
            return
        logger.exception("Job %s failed", job_id)
        _active_jobs[job_id]["status"] = "error"
        _active_jobs[job_id]["error"] = str(e)

    finally:
        # Always remove the source upload file — it is no longer needed
        # regardless of whether the job succeeded or failed.
        # By this point all Telegram uploads have either completed or been
        # abandoned, so deleting the source cannot cause data loss.
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logger.warning("Could not remove upload file: %s", file_path)
```

**Why `finally` is safe here (addresses the original concern):**
The original deletion happened _inside_ the `except` block, which ran when any step failed —
including early failures before Telegram uploads had even started. With `finally`, the deletion
still runs after a failure, but:
- If FFmpeg fails, no segments exist on Telegram yet — safe to delete the source.
- If Telegram upload fails partway, the DB transaction rolled back — no orphaned references.
- The source file is never needed again after `_process_job` exits.

---

## Issue 2 — Size mismatch retried unnecessarily (MEDIUM)

### What happened

The commit added an integrity check in `telegram_uploader.py`:

```python
file_id = message.document.file_id
if message.document.file_size != file_size:
    raise RuntimeError(f"Upload corrupted: size mismatch ...")
```

This `RuntimeError` falls through to the generic `except Exception` handler at the bottom
of the retry loop, which sleeps and retries up to 3 times. A Telegram-side size mismatch
will produce the same result on every attempt, so the retries accomplish nothing and waste
time and API quota.

### Fix

Raise a distinct exception type for integrity failures so the retry loop can treat them
as non-retriable, the same way `BadRequest` and `Unauthorized` are already handled.

**File:** `telegram_uploader.py`, function `_upload_file`

```python
# Add at module level (alongside existing imports):
class UploadIntegrityError(RuntimeError):
    """Raised when Telegram reports a file size that does not match the local file."""

# In _upload_file, replace the size check:
file_id = message.document.file_id
if message.document.file_size != file_size:
    raise UploadIntegrityError(
        f"Upload corrupted: size mismatch "
        f"{message.document.file_size} != {file_size} for {file_name}"
    )

# Add a new except clause before the generic `except Exception` handler:
except UploadIntegrityError:
    logger.error(
        "Size mismatch after uploading %s — not retrying (corrupted transfer)",
        file_name,
    )
    raise
```

This means the `except Exception` catch-all is never reached for integrity failures,
so no retries occur. The error propagates immediately up to `upload_job`, which lets
`_process_job` mark the job as failed.

---

## Issue 3 — Dead code after `secure_filename` fallback (LOW)

### What happened

The commit changed filename sanitization in `app.py` to:

```python
filename = secure_filename(data["filename"]) or "unnamed_upload"
if not filename:
    return jsonify({"error": "Invalid filename"}), 400
```

The `or "unnamed_upload"` guarantees `filename` is always a non-empty string,
so the `if not filename:` guard immediately below it can never be `True`.
The error response on that branch is dead and will never be returned.

### Fix

Remove the unreachable guard. The `or "unnamed_upload"` already handles the
empty-result case from `secure_filename`.

**File:** `app.py`, function `upload_init`

```python
# Before:
filename = secure_filename(data["filename"]) or "unnamed_upload"
if not filename:
    return jsonify({"error": "Invalid filename"}), 400

# After:
filename = secure_filename(data["filename"]) or "unnamed_upload"
```

---

## Out of Scope (Pre-existing Issues)

The following issues were identified in the review but are **not** introduced by this commit.
They should be tracked separately and are listed here for completeness only.

| Issue | File | Description |
|-------|------|-------------|
| `serve_segment` TelegramUploader per request | `app.py:578` | New Bot pool created on every segment fetch |
| Dict iteration race in timeout watcher | `app.py:156` | No lock around `_active_jobs.items()` |
| `delete_job` transaction style | `database.py:252` | Uses `conn.commit()` instead of `with conn:` |
| F-string in `ALTER TABLE` migration | `database.py:88` | Non-exploitable but risky pattern |
| `RetryAfter` not consuming retry count | `telegram_uploader.py:116` | Can hang indefinitely under sustained rate limiting |
| ABR `<=` instead of `<` in tier filter | `video_processor.py:60` | Produces redundant same-resolution encode tier |
| N+1 queries in `list_jobs` | `hls_manager.py:36` | 2 extra DB queries per job in pagination |

---

## Implementation Order

1. **Issue 1** (`finally` cleanup) — highest impact, prevents disk exhaustion
2. **Issue 2** (`UploadIntegrityError`) — medium impact, prevents wasted retries
3. **Issue 3** (dead code removal) — cosmetic, trivial one-liner deletion
