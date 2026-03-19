Report the current state of the SQLite database.

1. Check if `streamer.db` exists in the project root
2. If it exists, run these queries and report results:
   - `SELECT COUNT(*) FROM jobs` — total jobs
   - `SELECT COUNT(*) FROM tracks` — total tracks
   - `SELECT COUNT(*) FROM segments` — total segments
   - `SELECT job_id, filename, duration, file_size, created_at FROM jobs ORDER BY created_at DESC LIMIT 10` — recent jobs
   - `SELECT SUM(file_size) FROM segments` — total bytes stored on Telegram
3. Report the database file size on disk
4. If the database doesn't exist, report that and suggest running `python app.py` to initialize it
