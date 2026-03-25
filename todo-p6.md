## P6 — New Features

- [x] Thumbnail generation: FFmpeg extraction, Telegram upload, DB persistence (`has_thumbnail`), and proxy endpoint (`/thumbnail/<job_id>`) are all implemented.
- [ ] Thumbnail UI polish: dedicated per-series/per-episode thumbnail display and fallback placeholder in the job browser could be improved.
- [ ] Job re-processing: there is still no way to regenerate a completed job with new tiers/settings without re-uploading the source.
- [ ] Webhook notifications: there is still no completion callback for external automation.
- [ ] Configurable per-job ABR tiers: ABR settings are still global config only.
- [ ] Download original: the system still cannot reconstruct and serve the original uploaded file from Telegram-backed artifacts.
