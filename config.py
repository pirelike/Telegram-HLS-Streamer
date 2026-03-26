import logging
import os
import re

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only in minimal environments
    def load_dotenv():
        return False

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


def _csv_env(name, default):
    """Read a comma-separated env var into a normalized list of strings."""
    raw = os.getenv(name)
    if raw is None:
        raw = default
    values = []
    for entry in raw.split(","):
        item = entry.strip()
        if item:
            values.append(item)
    return values


class Config:
    # Server
    HOST = os.getenv("LOCAL_HOST", "0.0.0.0")
    PORT = _int_env("LOCAL_PORT", 5050)
    FORCE_HTTPS = os.getenv("FORCE_HTTPS", "false").lower() == "true"
    BEHIND_PROXY = os.getenv("BEHIND_PROXY", "false").lower() == "true"

    # Cloudflare tunnel
    CLOUDFLARED_ENABLED = os.getenv("CLOUDFLARED_ENABLED", "false").lower() == "true"

    # File handling
    TELEGRAM_MAX_FILE_SIZE = _int_env("TELEGRAM_MAX_FILE_SIZE", 20971520)
    MAX_UPLOAD_SIZE = _int_env("MAX_UPLOAD_SIZE", 107374182400)  # 100GB
    UPLOAD_CHUNK_SIZE = _int_env("UPLOAD_CHUNK_SIZE", 10485760)  # 10MB per chunk
    SEGMENT_TARGET_SIZE = _int_env("SEGMENT_TARGET_SIZE", 15728640)  # 15MB preferred FFmpeg segment target
    SEGMENT_CACHE_SIZE_MB = _int_env("SEGMENT_CACHE_SIZE_MB", 200)
    SEGMENT_PREFETCH_COUNT = _int_env("SEGMENT_PREFETCH_COUNT", 3)
    SEGMENT_PREFETCH_MIN_FREE_BYTES = _int_env("SEGMENT_PREFETCH_MIN_FREE_BYTES", 0)

    # Hardware acceleration
    ENABLE_HW_ACCEL = os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() == "true"
    PREFERRED_ENCODER = os.getenv("PREFERRED_ENCODER", "vaapi")
    # Max simultaneous FFmpeg video encodes. Default 2 is safe for GPU encoders
    # (NVENC consumer cards cap at ~5 sessions; VAAPI varies by driver).
    # Increase for CPU encoding on multi-core systems.
    MAX_PARALLEL_ENCODES = _int_env("MAX_PARALLEL_ENCODES", 2)
    # VAAPI device path. Leave empty to auto-detect (picks highest renderD* device,
    # which is typically the discrete GPU on multi-GPU systems).
    VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "").strip()
    VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4M")
    AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")

    # HLS
    HLS_SEGMENT_DURATION = _int_env("HLS_SEGMENT_DURATION", 4)

    # Adaptive Bitrate Streaming
    ABR_ENABLED = os.getenv("ABR_ENABLED", "true").lower() == "true"
    ENABLE_COPY_MODE = os.getenv("ENABLE_COPY_MODE", "true").lower() == "true"
    VIRTUAL_ABR_TIERS = os.getenv("VIRTUAL_ABR_TIERS", "false").lower() in ("true", "1", "yes")
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
    WATCH_ENABLED = os.getenv("WATCH_ENABLED", "false").lower() == "true"
    WATCH_ROOT = os.path.abspath(os.path.expanduser(os.getenv("WATCH_ROOT", "").strip()))
    _watch_done_dir = os.getenv("WATCH_DONE_DIR", "").strip()
    WATCH_DONE_DIR = (
        os.path.abspath(os.path.expanduser(_watch_done_dir))
        if _watch_done_dir
        else (os.path.join(WATCH_ROOT, "done") if WATCH_ROOT else "")
    )
    WATCH_POLL_SECONDS = max(1, _int_env("WATCH_POLL_SECONDS", 5))
    WATCH_STABLE_SECONDS = max(1, _int_env("WATCH_STABLE_SECONDS", 30))
    WATCH_VIDEO_EXTENSIONS = tuple(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in _csv_env("WATCH_VIDEO_EXTENSIONS", "mp4,mkv,avi,mov,webm,ts,m4v,flv")
    )
    WATCH_IGNORE_SUFFIXES = tuple(
        suffix.lower() for suffix in _csv_env("WATCH_IGNORE_SUFFIXES", ".part,.crdownload,.tmp,.partial")
    )

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

    # Telegram bots
    BOTS = []
    UPLOAD_PARALLELISM = _int_env("UPLOAD_PARALLELISM", 8)
    DB_AUTO_MERGE_INTERVAL_MINUTES = _int_env("DB_AUTO_MERGE_INTERVAL_MINUTES", 0)
    DB_AUTO_MERGE_FILE_ID = os.getenv("DB_AUTO_MERGE_FILE_ID", "").strip()
    DB_AUTO_MERGE_BOT_INDEX = _int_env("DB_AUTO_MERGE_BOT_INDEX", 0)

    # Registry of all UI-configurable settings.
    # Each entry: (attr_name, env_name, type_hint, category, description, default_value)
    # type_hint: "int" | "bool" | "str" | "tiers"
    CONFIGURABLE_SETTINGS = [
        # Server
        ("HOST", "LOCAL_HOST", "str", "server", "Bind address", "0.0.0.0"),
        ("PORT", "LOCAL_PORT", "int", "server", "Bind port", 5050),
        ("FORCE_HTTPS", "FORCE_HTTPS", "bool", "server", "Redirect HTTP to HTTPS", False),
        ("BEHIND_PROXY", "BEHIND_PROXY", "bool", "server", "Trust X-Forwarded-For header (enable when behind reverse proxy)", False),
        ("CORS_ALLOWED_ORIGINS", "CORS_ALLOWED_ORIGINS", "str", "server", "Comma-separated allowed CORS origins (empty = none)", ""),
        ("CLOUDFLARED_ENABLED", "CLOUDFLARED_ENABLED", "bool", "server", "Enable Cloudflare tunnel integration", False),
        # Files
        ("TELEGRAM_MAX_FILE_SIZE", "TELEGRAM_MAX_FILE_SIZE", "int", "files", "Hard upload ceiling per file sent to Telegram (bytes)", 20971520),
        ("MAX_UPLOAD_SIZE", "MAX_UPLOAD_SIZE", "int", "files", "Maximum video upload size accepted from browser (bytes)", 107374182400),
        ("UPLOAD_CHUNK_SIZE", "UPLOAD_CHUNK_SIZE", "int", "files", "Client chunk size for chunked uploads (bytes)", 10485760),
        ("SEGMENT_TARGET_SIZE", "SEGMENT_TARGET_SIZE", "int", "files", "Preferred HLS segment size target passed to FFmpeg (bytes)", 15728640),
        ("SEGMENT_CACHE_SIZE_MB", "SEGMENT_CACHE_SIZE_MB", "int", "files", "In-memory segment cache size (MB)", 200),
        ("SEGMENT_PREFETCH_COUNT", "SEGMENT_PREFETCH_COUNT", "int", "files", "Number of segments to prefetch ahead during playback", 3),
        ("SEGMENT_PREFETCH_MIN_FREE_BYTES", "SEGMENT_PREFETCH_MIN_FREE_BYTES", "int", "files", "Minimum free memory before prefetch is suspended (bytes, 0 = disabled)", 0),
        # HW Acceleration
        ("ENABLE_HW_ACCEL", "ENABLE_HARDWARE_ACCELERATION", "bool", "hw_accel", "Enable hardware-accelerated encoding (VAAPI/NVENC/QSV)", True),
        ("PREFERRED_ENCODER", "PREFERRED_ENCODER", "str", "hw_accel", "Preferred HW encoder: vaapi | nvenc | qsv", "vaapi"),
        ("VAAPI_DEVICE", "VAAPI_DEVICE", "str", "hw_accel", "VAAPI render device path (empty = auto-detect)", ""),
        ("MAX_PARALLEL_ENCODES", "MAX_PARALLEL_ENCODES", "int", "hw_accel", "Maximum simultaneous FFmpeg video encodes", 2),
        ("VIDEO_BITRATE", "VIDEO_BITRATE", "str", "hw_accel", "Default video bitrate (e.g. 4M)", "4M"),
        ("AUDIO_BITRATE", "AUDIO_BITRATE", "str", "hw_accel", "Audio bitrate for AAC re-encode (e.g. 128k)", "128k"),
        # HLS
        ("HLS_SEGMENT_DURATION", "HLS_SEGMENT_DURATION", "int", "hls", "Target HLS segment duration in seconds", 4),
        # ABR
        ("ABR_ENABLED", "ABR_ENABLED", "bool", "abr", "Enable adaptive bitrate: produce multiple quality tiers", True),
        ("ENABLE_COPY_MODE", "ENABLE_COPY_MODE", "bool", "abr", "Skip re-encode when source stream is already HLS-compatible", True),
        ("VIRTUAL_ABR_TIERS", "VIRTUAL_ABR_TIERS", "bool", "abr", "Transcode lower-quality tiers on demand at playback time (mutually exclusive with ABR_ENABLED)", False),
        ("ABR_TIERS", "ABR_TIERS", "tiers", "abr", "Comma-separated height:bitrate pairs for ABR tiers (e.g. 1080:10M,720:5M)", "1080:10M,720:5M,480:2M,360:1200k"),
        ("TIER0_BITRATES", "TIER0_BITRATES", "tiers", "abr", "Comma-separated height:bitrate pairs for source-quality tier 0 (e.g. 2160:60M,1080:30M)", "2160:60M,1080:30M,720:15M,480:5M"),
        ("TIER0_BITRATE_DEFAULT", "TIER0_BITRATE_DEFAULT", "str", "abr", "Fallback bitrate for tier 0 when source resolution is not listed", "15M"),
        # Reliability
        ("JOB_TIMEOUT_SECONDS", "JOB_TIMEOUT_SECONDS", "int", "reliability", "Maximum seconds a processing job may run before being timed out", 7200),
        ("PENDING_UPLOAD_TTL_SECONDS", "PENDING_UPLOAD_TTL_SECONDS", "int", "reliability", "Time before an incomplete chunked upload session expires (seconds)", 86400),
        ("PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", "PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", "int", "reliability", "How often to sweep and expire stale upload sessions (seconds)", 300),
        ("JOB_RETENTION_DAYS", "JOB_RETENTION_DAYS", "int", "reliability", "Auto-delete completed jobs older than N days (0 = disabled)", 0),
        ("MAX_CONCURRENT_JOBS", "MAX_CONCURRENT_JOBS", "int", "reliability", "Maximum jobs processed simultaneously", 1),
        # Rate limiting
        ("UPLOAD_RATE_LIMIT_WINDOW", "UPLOAD_RATE_LIMIT_WINDOW", "int", "rate_limiting", "Rate-limit window duration per IP (seconds)", 60),
        ("UPLOAD_RATE_LIMIT_MAX_REQUESTS", "UPLOAD_RATE_LIMIT_MAX_REQUESTS", "int", "rate_limiting", "Max upload requests per IP within the window", 100),
        ("MAX_PENDING_UPLOADS_PER_IP", "MAX_PENDING_UPLOADS_PER_IP", "int", "rate_limiting", "Max concurrent pending upload sessions per IP (0 = unlimited)", 5),
        # Watch folder (advanced; WATCH_ENABLED/ROOT/DONE managed via /api/watch-settings)
        ("WATCH_POLL_SECONDS", "WATCH_POLL_SECONDS", "int", "watch", "Seconds between watch folder scans", 5),
        ("WATCH_STABLE_SECONDS", "WATCH_STABLE_SECONDS", "int", "watch", "Seconds a file must be stable (unchanged) before processing", 30),
        ("WATCH_VIDEO_EXTENSIONS", "WATCH_VIDEO_EXTENSIONS", "str", "watch", "Comma-separated video extensions to watch (e.g. mp4,mkv,avi)", "mp4,mkv,avi,mov,webm,ts,m4v,flv"),
        ("WATCH_IGNORE_SUFFIXES", "WATCH_IGNORE_SUFFIXES", "str", "watch", "Comma-separated suffixes to ignore (e.g. .part,.tmp)", ".part,.crdownload,.tmp,.partial"),
        # Telegram
        ("UPLOAD_PARALLELISM", "UPLOAD_PARALLELISM", "int", "telegram", "Max concurrent Telegram upload tasks per bot", 8),
        ("DB_AUTO_MERGE_INTERVAL_MINUTES", "DB_AUTO_MERGE_INTERVAL_MINUTES", "int", "telegram", "How often to run automatic DB import merge (minutes, 0 = disabled)", 0),
        ("DB_AUTO_MERGE_FILE_ID", "DB_AUTO_MERGE_FILE_ID", "str", "telegram", "Telegram file_id used by automatic DB merge import", ""),
        ("DB_AUTO_MERGE_BOT_INDEX", "DB_AUTO_MERGE_BOT_INDEX", "int", "telegram", "Bot index to use when downloading DB_AUTO_MERGE_FILE_ID", 0),
    ]

    # Category display labels
    _CATEGORY_LABELS = {
        "server": "Server",
        "files": "File Handling",
        "hw_accel": "Hardware Acceleration",
        "hls": "HLS",
        "abr": "Adaptive Bitrate",
        "reliability": "Reliability",
        "rate_limiting": "Rate Limiting",
        "watch": "Watch Folder (Advanced)",
        "telegram": "Telegram",
    }


    @classmethod
    def setting_type_map(cls):
        """Return a map of configurable key -> type hint."""
        return {entry[0]: entry[2] for entry in cls.CONFIGURABLE_SETTINGS}

    @classmethod
    def parse_setting_value(cls, key, raw_value):
        """Parse a raw setting payload value into the runtime Python value."""
        type_hint = cls.setting_type_map().get(key)
        if type_hint is None:
            raise KeyError(key)
        if type_hint == "int":
            return int(raw_value)
        if type_hint == "bool":
            if isinstance(raw_value, bool):
                return raw_value
            return str(raw_value).lower() == "true"
        if type_hint == "tiers":
            parsed = _parse_tiers(str(raw_value), as_dict=(key == "TIER0_BITRATES"))
            if parsed is None:
                raise ValueError("tiers value cannot be empty")
            return parsed
        if key in ("WATCH_VIDEO_EXTENSIONS", "WATCH_IGNORE_SUFFIXES"):
            return tuple(s.strip() for s in str(raw_value).split(",") if s.strip())
        if key == "CORS_ALLOWED_ORIGINS":
            return [s.strip() for s in str(raw_value).split(",") if s.strip()]
        return str(raw_value)

    @classmethod
    def stringify_setting_value(cls, key, parsed_value):
        """Normalize a parsed runtime value into DB storage string form."""
        type_hint = cls.setting_type_map().get(key)
        if type_hint is None:
            raise KeyError(key)
        if type_hint == "bool":
            return "true" if bool(parsed_value) else "false"
        if type_hint == "tiers":
            if isinstance(parsed_value, dict):
                return ",".join(f"{h}:{b}" for h, b in parsed_value.items())
            if isinstance(parsed_value, list):
                return ",".join(f"{t['height']}:{t['bitrate']}" for t in parsed_value)
            return str(parsed_value)
        if key in ("WATCH_VIDEO_EXTENSIONS", "WATCH_IGNORE_SUFFIXES") and isinstance(parsed_value, tuple):
            return ",".join(parsed_value)
        if key == "CORS_ALLOWED_ORIGINS" and isinstance(parsed_value, list):
            return ",".join(parsed_value)
        return str(parsed_value)

    @classmethod
    def apply_runtime_settings(cls, updates):
        """Apply already-parsed setting values to Config class attributes."""
        for key, value in updates.items():
            setattr(cls, key, value)

    @classmethod
    def settings_require_bot_reload(cls, changed_keys):
        """Return whether changed setting keys require Telegram bot client rebuild."""
        bot_reload_keys = set()
        return bool(set(changed_keys) & bot_reload_keys)

    @classmethod
    def load_bots(cls):
        cls.BOTS = []
        token_pattern = re.compile(r"^[0-9]{8,12}:[a-zA-Z0-9_-]{35,45}$")
        suffix_re = re.compile(r"^TELEGRAM_BOT_TOKEN_(\d+)$")
        suffixes = sorted(
            (int(m.group(1)) for key in os.environ if (m := suffix_re.match(key))),
        )
        for i in suffixes:
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

    @classmethod
    def load_from_db(cls):
        """Apply DB-persisted setting overrides on top of env-var defaults.

        Also merges DB-stored bots with env-loaded bots (deduplicating by token).
        Lazy-imports database to avoid circular import at module load time.
        """
        try:
            import database as _db  # noqa: PLC0415
        except ImportError:
            return

        db_settings = _db.get_all_settings()

        _type_map = {entry[0]: entry[2] for entry in cls.CONFIGURABLE_SETTINGS}

        for key, raw_value in db_settings.items():
            type_hint = _type_map.get(key)
            if type_hint is None:
                continue
            try:
                if type_hint == "int":
                    setattr(cls, key, int(raw_value))
                elif type_hint == "bool":
                    setattr(cls, key, raw_value.lower() == "true")
                elif type_hint == "tiers":
                    parsed = _parse_tiers(raw_value, as_dict=(key == "TIER0_BITRATES"))
                    if parsed is not None:
                        setattr(cls, key, parsed)
                elif key in ("WATCH_VIDEO_EXTENSIONS", "WATCH_IGNORE_SUFFIXES"):
                    # Stored as CSV string; runtime attr is a tuple
                    setattr(cls, key, tuple(s.strip() for s in raw_value.split(",") if s.strip()))
                elif key == "CORS_ALLOWED_ORIGINS":
                    # Stored as CSV string; runtime attr is a list
                    setattr(cls, key, [s.strip() for s in raw_value.split(",") if s.strip()])
                else:
                    setattr(cls, key, raw_value)
            except (ValueError, TypeError) as exc:
                _logger.warning("DB setting %s=%r is invalid, skipping: %s", key, raw_value, exc)

        if cls.ABR_ENABLED and cls.VIRTUAL_ABR_TIERS:
            _logger.warning(
                "ABR_ENABLED and VIRTUAL_ABR_TIERS cannot both be true; disabling VIRTUAL_ABR_TIERS"
            )
            cls.VIRTUAL_ABR_TIERS = False

        # Merge DB bots (append after env bots, deduplicate by token)
        env_tokens = {b["token"] for b in cls.BOTS}
        try:
            db_bots = _db.get_all_bots()
        except Exception:  # noqa: BLE001
            db_bots = []
        for bot in db_bots:
            if bot["token"] not in env_tokens:
                cls.BOTS.append({"token": bot["token"], "channel_id": bot["channel_id"], "_db_id": bot["id"]})
                env_tokens.add(bot["token"])

    @classmethod
    def reload(cls):
        """Re-read .env and env vars, then apply DB overrides.

        This is the single entry point for "refresh everything at runtime".
        """
        load_dotenv(override=True)
        # Re-read all env vars into class attributes
        cls.HOST = os.getenv("LOCAL_HOST", "0.0.0.0")
        cls.PORT = _int_env("LOCAL_PORT", 5050)
        cls.FORCE_HTTPS = os.getenv("FORCE_HTTPS", "false").lower() == "true"
        cls.BEHIND_PROXY = os.getenv("BEHIND_PROXY", "false").lower() == "true"
        cls.CLOUDFLARED_ENABLED = os.getenv("CLOUDFLARED_ENABLED", "false").lower() == "true"
        cls.TELEGRAM_MAX_FILE_SIZE = _int_env("TELEGRAM_MAX_FILE_SIZE", 20971520)
        cls.MAX_UPLOAD_SIZE = _int_env("MAX_UPLOAD_SIZE", 107374182400)
        cls.UPLOAD_CHUNK_SIZE = _int_env("UPLOAD_CHUNK_SIZE", 10485760)
        cls.SEGMENT_TARGET_SIZE = _int_env("SEGMENT_TARGET_SIZE", 15728640)
        cls.SEGMENT_CACHE_SIZE_MB = _int_env("SEGMENT_CACHE_SIZE_MB", 200)
        cls.SEGMENT_PREFETCH_COUNT = _int_env("SEGMENT_PREFETCH_COUNT", 3)
        cls.SEGMENT_PREFETCH_MIN_FREE_BYTES = _int_env("SEGMENT_PREFETCH_MIN_FREE_BYTES", 0)
        cls.ENABLE_HW_ACCEL = os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() == "true"
        cls.PREFERRED_ENCODER = os.getenv("PREFERRED_ENCODER", "vaapi")
        cls.MAX_PARALLEL_ENCODES = _int_env("MAX_PARALLEL_ENCODES", 2)
        cls.VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "").strip()
        cls.VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4M")
        cls.AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")
        cls.HLS_SEGMENT_DURATION = _int_env("HLS_SEGMENT_DURATION", 4)
        cls.ABR_ENABLED = os.getenv("ABR_ENABLED", "true").lower() == "true"
        cls.ENABLE_COPY_MODE = os.getenv("ENABLE_COPY_MODE", "true").lower() == "true"
        cls.VIRTUAL_ABR_TIERS = os.getenv("VIRTUAL_ABR_TIERS", "false").lower() in ("true", "1", "yes")
        cls.ABR_TIERS = _parse_tiers(os.getenv("ABR_TIERS")) or [
            {"height": 1080, "bitrate": "10M"},
            {"height": 720, "bitrate": "5M"},
            {"height": 480, "bitrate": "2M"},
            {"height": 360, "bitrate": "1200k"},
        ]
        cls.TIER0_BITRATES = _parse_tiers(os.getenv("TIER0_BITRATES"), as_dict=True) or {
            2160: "60M",
            1080: "30M",
            720: "15M",
            480: "5M",
        }
        cls.TIER0_BITRATE_DEFAULT = os.getenv("TIER0_BITRATE_DEFAULT", "15M").strip()
        cls.JOB_TIMEOUT_SECONDS = _int_env("JOB_TIMEOUT_SECONDS", 7200)
        cls.PENDING_UPLOAD_TTL_SECONDS = _int_env("PENDING_UPLOAD_TTL_SECONDS", 86400)
        cls.PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS = _int_env("PENDING_UPLOAD_CLEANUP_INTERVAL_SECONDS", 300)
        cls.JOB_RETENTION_DAYS = _int_env("JOB_RETENTION_DAYS", 0)
        cls.MAX_CONCURRENT_JOBS = _int_env("MAX_CONCURRENT_JOBS", 1)
        cls.CORS_ALLOWED_ORIGINS = [
            origin.strip()
            for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ]
        cls.UPLOAD_RATE_LIMIT_WINDOW = _int_env("UPLOAD_RATE_LIMIT_WINDOW", 60)
        cls.UPLOAD_RATE_LIMIT_MAX_REQUESTS = _int_env("UPLOAD_RATE_LIMIT_MAX_REQUESTS", 100)
        cls.MAX_PENDING_UPLOADS_PER_IP = _int_env("MAX_PENDING_UPLOADS_PER_IP", 5)
        cls.WATCH_POLL_SECONDS = max(1, _int_env("WATCH_POLL_SECONDS", 5))
        cls.WATCH_STABLE_SECONDS = max(1, _int_env("WATCH_STABLE_SECONDS", 30))
        cls.WATCH_VIDEO_EXTENSIONS = tuple(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in _csv_env("WATCH_VIDEO_EXTENSIONS", "mp4,mkv,avi,mov,webm,ts,m4v,flv")
        )
        cls.WATCH_IGNORE_SUFFIXES = tuple(
            suffix.lower() for suffix in _csv_env("WATCH_IGNORE_SUFFIXES", ".part,.crdownload,.tmp,.partial")
        )
        cls.UPLOAD_PARALLELISM = _int_env("UPLOAD_PARALLELISM", 8)
        cls.DB_AUTO_MERGE_INTERVAL_MINUTES = _int_env("DB_AUTO_MERGE_INTERVAL_MINUTES", 0)
        cls.DB_AUTO_MERGE_FILE_ID = os.getenv("DB_AUTO_MERGE_FILE_ID", "").strip()
        cls.DB_AUTO_MERGE_BOT_INDEX = _int_env("DB_AUTO_MERGE_BOT_INDEX", 0)
        cls.load_bots()
        cls.load_from_db()

    @classmethod
    def to_dict(cls) -> dict:
        """Return all configurable settings organized by category.

        Used by GET /api/settings. Never includes bot tokens.
        """
        categories: dict = {}
        for attr, env_name, type_hint, category, description, default in cls.CONFIGURABLE_SETTINGS:
            if category not in categories:
                categories[category] = {
                    "label": cls._CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
                    "settings": [],
                }
            value = getattr(cls, attr)
            # Serialize complex types to strings for the API
            if type_hint == "tiers" and isinstance(value, list):
                value = ",".join(f"{t['height']}:{t['bitrate']}" for t in value)
            elif type_hint == "tiers" and isinstance(value, dict):
                value = ",".join(f"{h}:{b}" for h, b in value.items())
            elif type_hint == "bool":
                value = bool(value)
            elif type_hint == "int":
                value = int(value)
            elif type_hint == "str" and isinstance(value, (list, tuple)):
                # CORS_ALLOWED_ORIGINS is a list; WATCH_VIDEO_EXTENSIONS/WATCH_IGNORE_SUFFIXES are tuples
                value = ",".join(value)
            categories[category]["settings"].append({
                "key": attr,
                "env": env_name,
                "type": type_hint,
                "value": value,
                "default": default,
                "description": description,
            })
        return {"categories": categories}


Config.load_bots()
Config.load_from_db()
os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
os.makedirs(Config.PROCESSING_DIR, exist_ok=True)
if Config.WATCH_ENABLED:
    if not Config.WATCH_ROOT:
        raise ValueError("WATCH_ROOT must be configured when WATCH_ENABLED=true")
    os.makedirs(Config.WATCH_ROOT, exist_ok=True)
    os.makedirs(Config.WATCH_DONE_DIR, exist_ok=True)
