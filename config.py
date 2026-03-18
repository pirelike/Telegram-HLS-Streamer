import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Server
    HOST = os.getenv("LOCAL_HOST", "0.0.0.0")
    PORT = int(os.getenv("LOCAL_PORT", "5050"))
    FORCE_HTTPS = os.getenv("FORCE_HTTPS", "false").lower() == "true"
    BEHIND_PROXY = os.getenv("BEHIND_PROXY", "true").lower() == "true"

    # File handling
    TELEGRAM_MAX_FILE_SIZE = int(os.getenv("TELEGRAM_MAX_FILE_SIZE", "20971520"))
    MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", "107374182400"))  # 100GB
    UPLOAD_CHUNK_SIZE = int(os.getenv("UPLOAD_CHUNK_SIZE", "10485760"))  # 10MB per chunk
    ENABLE_COPY_MODE = os.getenv("ENABLE_COPY_MODE", "true").lower() == "true"

    # Hardware acceleration
    ENABLE_HW_ACCEL = os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() == "true"
    PREFERRED_ENCODER = os.getenv("PREFERRED_ENCODER", "vaapi")
    VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4M")

    # HLS
    HLS_SEGMENT_DURATION = int(os.getenv("HLS_SEGMENT_DURATION", "4"))

    # Adaptive Bitrate Streaming
    ABR_ENABLED = os.getenv("ABR_ENABLED", "true").lower() == "true"
    ABR_TIERS = [
        {"height": 1080, "bitrate": "5M"},
        {"height": 720, "bitrate": "2.5M"},
        {"height": 480, "bitrate": "1M"},
        {"height": 360, "bitrate": "600k"},
    ]

    # Directories
    UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
    PROCESSING_DIR = os.path.join(os.path.dirname(__file__), "processing")

    # Reliability / cleanup
    JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))  # 2h
    PENDING_UPLOAD_TTL_SECONDS = int(os.getenv("PENDING_UPLOAD_TTL_SECONDS", "86400"))  # 24h
    PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS = int(
        os.getenv("PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", "300")
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
    UPLOAD_PARALLELISM = int(os.getenv("UPLOAD_PARALLELISM", "8"))

    @classmethod
    def load_bots(cls):
        cls.BOTS = []
        for i in range(1, 9):
            token = os.getenv(f"TELEGRAM_BOT_TOKEN_{i}")
            channel = os.getenv(f"TELEGRAM_CHANNEL_ID_{i}")
            if token and channel and not token.startswith("your_"):
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
