import logging
import os
import re
from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)

_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmMgG]$")


def _parse_tiers(raw, as_dict=False):
    """Parse a comma-separated height:bitrate string into ABR tier structures.

    Returns list[dict] when as_dict=False, dict[int, str] when as_dict=True.
    Returns None on empty/missing input so callers can fall back to defaults.
    Raises ValueError on malformed input.
    """
    if not raw or not raw.strip():
        return None
    result_list = []
    result_dict = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid tier entry {entry!r}: expected height:bitrate")
        height_str, bitrate = parts[0].strip(), parts[1].strip()
        try:
            height = int(height_str)
        except ValueError:
            raise ValueError(f"Invalid tier height {height_str!r}: expected positive integer")
        if height <= 0:
            raise ValueError(f"Invalid tier height {height}: must be positive")
        if not _BITRATE_RE.match(bitrate):
            raise ValueError(f"Invalid bitrate {bitrate!r}: expected format like 10M, 5M, 1200k")
        result_list.append({"height": height, "bitrate": bitrate})
        result_dict[height] = bitrate
    if not result_list:
        return None
    return result_dict if as_dict else result_list


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

    # Cloudflare tunnel
    CLOUDFLARED_ENABLED = os.getenv("CLOUDFLARED_ENABLED", "true").lower() == "true"

    # File handling
    TELEGRAM_MAX_FILE_SIZE = _int_env("TELEGRAM_MAX_FILE_SIZE", 20971520)
    MAX_UPLOAD_SIZE = _int_env("MAX_UPLOAD_SIZE", 107374182400)  # 100GB
    UPLOAD_CHUNK_SIZE = _int_env("UPLOAD_CHUNK_SIZE", 10485760)  # 10MB per chunk
    SEGMENT_MAX_SIZE = _int_env("SEGMENT_MAX_SIZE", 15728640)  # 15MB — extra headroom; oversized segments are re-split
    SEGMENT_CACHE_SIZE_MB = _int_env("SEGMENT_CACHE_SIZE_MB", 200)
    SEGMENT_PREFETCH_COUNT = _int_env("SEGMENT_PREFETCH_COUNT", 2)
    SEGMENT_PREFETCH_MIN_FREE_BYTES = _int_env("SEGMENT_PREFETCH_MIN_FREE_BYTES", 33554432)

    # Hardware acceleration
    ENABLE_HW_ACCEL = os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() == "true"
    PREFERRED_ENCODER = os.getenv("PREFERRED_ENCODER", "vaapi")
    # VAAPI device path. Leave empty to auto-detect (picks highest renderD* device,
    # which is typically the discrete GPU on multi-GPU systems).
    VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "").strip()
    VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4M")
    AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")

    # HLS
    HLS_SEGMENT_DURATION = _int_env("HLS_SEGMENT_DURATION", 4)

    # Adaptive Bitrate Streaming
    ABR_ENABLED = os.getenv("ABR_ENABLED", "true").lower() == "true"
    ABR_TIERS = _parse_tiers(os.getenv("ABR_TIERS")) or [
        {"height": 1080, "bitrate": "10M"},
        {"height": 720, "bitrate": "5M"},
        {"height": 480, "bitrate": "2M"},
        {"height": 360, "bitrate": "1200k"},
    ]

    # Tier 0 CBR bitrates by source resolution (near-lossless quality)
    TIER0_BITRATES = _parse_tiers(os.getenv("TIER0_BITRATES"), as_dict=True) or {
        2160: "60M",   # 4K
        1080: "30M",   # 1080p
        720: "15M",    # 720p
        480: "5M",     # 480p
    }
    TIER0_BITRATE_DEFAULT = os.getenv("TIER0_BITRATE_DEFAULT", "15M").strip()

    # Directories
    UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
    PROCESSING_DIR = os.path.join(os.path.dirname(__file__), "processing")

    # Reliability / cleanup
    JOB_TIMEOUT_SECONDS = _int_env("JOB_TIMEOUT_SECONDS", 7200)  # 2h
    PENDING_UPLOAD_TTL_SECONDS = _int_env("PENDING_UPLOAD_TTL_SECONDS", 86400)  # 24h
    PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS = _int_env(
        "PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", 300
    )
    # Retention: automatically delete completed jobs older than N days (0 = disabled)
    JOB_RETENTION_DAYS = _int_env("JOB_RETENTION_DAYS", 0)

    # Queue: max number of jobs processed concurrently
    MAX_CONCURRENT_JOBS = _int_env("MAX_CONCURRENT_JOBS", 1)
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]

    # Rate limiting for upload endpoints (per IP)
    UPLOAD_RATE_LIMIT_WINDOW = _int_env("UPLOAD_RATE_LIMIT_WINDOW", 60)  # seconds
    UPLOAD_RATE_LIMIT_MAX_REQUESTS = _int_env("UPLOAD_RATE_LIMIT_MAX_REQUESTS", 100)
    # Max concurrent pending uploads per IP (0 = unlimited)
    MAX_PENDING_UPLOADS_PER_IP = _int_env("MAX_PENDING_UPLOADS_PER_IP", 5)

    # Optional auth for upload endpoints
    UPLOAD_API_KEY = os.getenv("UPLOAD_API_KEY", "").strip()
    UPLOAD_BASIC_USER = os.getenv("UPLOAD_BASIC_USER", "").strip()
    UPLOAD_BASIC_PASSWORD = os.getenv("UPLOAD_BASIC_PASSWORD", "").strip()

    # Optional auth for HLS playback endpoints (HMAC-signed per-job tokens)
    PLAYBACK_SECRET = os.getenv("PLAYBACK_SECRET", "").strip()

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
