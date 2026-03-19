Validate the project environment and dependencies.

1. Check that Python 3.8+ is available
2. Check that `ffmpeg` and `ffprobe` are on PATH and report their versions
3. Check that all Python packages from `requirements.txt` are installed (run `pip list` and compare)
4. Check if `.env` exists and has the required `TELEGRAM_BOT_TOKEN_1` and `TELEGRAM_CHANNEL_ID_1` set (do NOT print actual token values — just confirm presence)
5. Check if `uploads/` and `processing/` directories exist
6. Check if `streamer.db` exists and report its size
7. Report a summary of what's ready and what's missing
