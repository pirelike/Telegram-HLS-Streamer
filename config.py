import logging
import os
import re
from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)


def _int_env(name, default):
    """Read an integer from an environment variable with a safe fallback."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default


class Config:
    # Server
    HOST = os.getenv("LOCAL_HOST", "0.0.0.0")
    PORT = _int_env("LOCAL_PORT", 5050)
    FORCE_HTTPS = os.getenv("FORCE_HTTPS", "false").lower() == "true"
    BEHIND_PROXY = os.getenv("BEHIND_PROXY", "true").lower() == "true"

    # File handling
    TELEGRAM_MAX_FILE_SIZE = _int_env("TELEGRAM_MAX_FILE_SIZE", 20971520)
    MAX_UPLOAD_SIZE = _int_env("MAX_UPLOAD_SIZE", 107374182400)  # 100GB
    UPLOAD_CHUNK_SIZE = _int_env("UPLOAD_CHUNK_SIZE", 10485760)  # 10MB per chunk
    ENABLE_COPY_MODE = os.getenv("ENABLE_COPY_MODE", "true").lower() == "true"

    # Hardware acceleration
    ENABLE_HW_ACCEL = os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() == "true"
    PREFERRED_ENCODER = os.getenv("PREFERRED_ENCODER", "vaapi")
    VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4M")
    AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")

    # HLS
    HLS_SEGMENT_DURATION = _int_env("HLS_SEGMENT_DURATION", 4)

    # Adaptive Bitrate Streaming
    ABR_ENABLED = os.getenv("ABR_ENABLED", "true").lower() == "true"
    ABR_TIERS = [
        {"height": 1080, "bitrate": "10M"},
        {"height": 720, "bitrate": "5M"},
        {"height": 480, "bitrate": "2M"},
        {"height": 360, "bitrate": "1200k"},
    ]

    # Directories
    UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
    PROCESSING_DIR = os.path.join(os.path.dirname(__file__), "processing")

    # Reliability / cleanup
    JOB_TIMEOUT_SECONDS = _int_env("JOB_TIMEOUT_SECONDS", 7200)  # 2h
    PENDING_UPLOAD_TTL_SECONDS = _int_env("PENDING_UPLOAD_TTL_SECONDS", 86400)  # 24h
    PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS = _int_env(
        "PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", 300
    )
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]

    # Optional auth for upload endpoints
    UPLOAD_API_KEY = os.getenv("UPLOAD_API_KEY", "").strip()
    UPLOAD_BASIC_USER = os.getenv("UPLOAD_BASIC_USER", "").strip()
    UPLOAD_BASIC_PASSWORD = os.getenv("UPLOAD_BASIC_PASSWORD", "").strip()

    # Telegram bots
    BOTS = []
    UPLOAD_PARALLELISM = _int_env("UPLOAD_PARALLELISM", 8)

    @classmethod
    def load_bots(cls):
        cls.BOTS = []
        token_pattern = re.compile(r"^[0-9]{8,12}:[a-zA-Z0-9_-]{35,45}$")
        for i in range(1, 9):
            token = os.getenv(f"TELEGRAM_BOT_TOKEN_{i}")
            channel = os.getenv(f"TELEGRAM_CHANNEL_ID_{i}")
            if token and channel and not token.startswith("your_"):
                if not token_pattern.match(token):
                    raise ValueError(f"Invalid TELEGRAM_BOT_TOKEN_{i}: malformed token format")
                try:
                    channel_id = int(channel)
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid TELEGRAM_CHANNEL_ID_{i}: expected integer, got {channel!r}"
                    ) from exc
                if channel_id >= 0:
                    raise ValueError(
                        f"Invalid TELEGRAM_CHANNEL_ID_{i}: expected negative integer, got {channel_id}"
                    )
                cls.BOTS.append({"token": token, "channel_id": channel_id})
        return cls.BOTS


Config.load_bots()
os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
os.makedirs(Config.PROCESSING_DIR, exist_ok=True)
