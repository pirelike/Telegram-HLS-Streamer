# TODO — P6 New Features

## Open Items

- [ ] **Thumbnail UI polish** (`templates/watch.html`, `static/watch.js`, `static/app.css`)
  Thumbnail extraction, upload, DB persistence (`has_thumbnail`), and proxy endpoint (`/thumbnail/<job_id>`) are all done. What's missing: thumbnail is not displayed on the watch/detail page (`/watch/<job_id>`), and there is no fallback placeholder shown there either.

- [ ] **Job re-processing** (`app.py`, `video_processor.py`)
  No way to regenerate a completed job with new tiers or settings without re-uploading the source. The source file is deleted after a successful job (`app.py:1530–1534`), so re-processing would require either source retention or re-encoding from existing Telegram-backed TS segments (lossy). No `POST /api/jobs/<job_id>/reprocess` endpoint exists.

- [ ] **Webhook notifications** (`app.py`, `config.py`)
  No completion callback for external automation. Clients must poll `/api/jobs/<job_id>`. There is no `WEBHOOK_URL` config, no HTTP POST on job completion, and no event stream (SSE/WebSocket). Job completion is only tracked in the in-memory `_active_jobs` dict and in the DB.

- [ ] **Configurable per-job ABR tiers** (`app.py`, `video_processor.py`, `config.py`)
  ABR settings are global config only (`Config.ABR_TIERS`, `Config.TIER0_BITRATES`). `video_processor._get_abr_tiers()` reads only from Config. `POST /api/upload/finalize` accepts metadata fields but no per-job tier overrides. No per-job tier data is stored in the DB at request time.

- [ ] **Download original** (`app.py`, `database.py`)
  The system cannot reconstruct or serve the original uploaded file. The source is deleted after processing. What exists: individual segment proxy at `/segment/<job_id>/<segment_key>` and `db.get_segments_for_prefix()` to enumerate all segments for a stream. A `/download/<job_id>` endpoint could concatenate `video_0/*.ts` segments into a raw MPEG-TS stream (playable in VLC/mpv), though it would not be the original upload format.

## Already Completed

- [x] **Thumbnail generation** — FFmpeg extraction, Telegram upload, DB persistence (`has_thumbnail`), and proxy endpoint (`/thumbnail/<job_id>`) are all implemented.
