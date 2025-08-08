"""
Configuration management for Telegram HLS Streamer.
Handles loading and validation of all application settings.
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field


@dataclass
class BotConfig:
    """Configuration for a single Telegram bot."""
    token: str
    chat_id: str
    index: int = 0


class Config:
    """Main configuration manager for the application."""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or ".env"
        self.logger = logging.getLogger(__name__)
        
        # Load configuration from environment/file
        self._load_config()
        self._validate_config()
        
    def _load_config(self):
        """Load configuration from .env file and environment variables."""
        # Load from .env file if it exists
        if os.path.exists(self.config_path):
            self._load_env_file()
            
        # Core server settings
        self.local_host = os.getenv("LOCAL_HOST", "0.0.0.0")
        self.local_port = int(os.getenv("LOCAL_PORT", "8080"))
        self.public_domain = os.getenv("PUBLIC_DOMAIN")
        self.force_https = os.getenv("FORCE_HTTPS", "false").lower() == "true"
        
        # SSL settings (only needed if app serves HTTPS directly - not recommended)
        self.ssl_cert_path = os.getenv("SSL_CERT_PATH")
        self.ssl_key_path = os.getenv("SSL_KEY_PATH")
        self.behind_proxy = os.getenv("BEHIND_PROXY", "true").lower() == "true"
        
        # File and directory settings
        self.upload_dir = Path(os.getenv("UPLOAD_DIR", "temp_uploads"))
        self.segments_dir = Path(os.getenv("SEGMENTS_DIR", "segments"))
        self.cache_dir = Path(os.getenv("CACHE_DIR", "cache"))
        self.database_path = os.getenv("DATABASE_PATH", "database/telegram_hls.db")
        
        # Processing settings
        self.ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")
        self.ffmpeg_hardware_accel = os.getenv("FFMPEG_HARDWARE_ACCEL", "auto")
        self.ffmpeg_threads = int(os.getenv("FFMPEG_THREADS", "4"))
        self.enable_two_pass_encoding = os.getenv("ENABLE_TWO_PASS_ENCODING", "false").lower() == "true"
        
        # Copy mode settings for lossless uploads
        self.enable_copy_mode = os.getenv("ENABLE_COPY_MODE", "true").lower() == "true"
        self.copy_mode_threshold = int(os.getenv("COPY_MODE_THRESHOLD", str(20 * 1024 * 1024)))  # 20MB default
        
        # Segment settings
        self.min_segment_duration = int(os.getenv("MIN_SEGMENT_DURATION", "2"))
        self.max_segment_duration = int(os.getenv("MAX_SEGMENT_DURATION", "30"))
        self.target_segment_duration = int(os.getenv("TARGET_SEGMENT_DURATION", "10"))
        
        # Cache settings
        self.cache_type = os.getenv("CACHE_TYPE", "memory")  # memory or disk
        self.cache_size = int(os.getenv("CACHE_SIZE", str(1024 * 1024 * 1024)))  # 1GB default
        self.preload_segments = int(os.getenv("PRELOAD_SEGMENTS", "8"))
        self.max_concurrent_preloads = int(os.getenv("MAX_CONCURRENT_PRELOADS", "5"))
        
        # File limits
        self.max_upload_size = int(os.getenv("MAX_UPLOAD_SIZE", str(50 * 1024 * 1024 * 1024)))  # 50GB
        self.max_concurrent_uploads = int(os.getenv("MAX_CONCURRENT_UPLOADS", "3"))
        
        # Telegram-specific limits
        self.telegram_max_file_size = int(os.getenv("TELEGRAM_MAX_FILE_SIZE", str(20 * 1024 * 1024)))  # 20MB default
        self.telegram_file_size_buffer = int(os.getenv("TELEGRAM_FILE_SIZE_BUFFER", str(1024 * 1024)))  # 1MB buffer
        
        # Logging
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.log_file = os.getenv("LOG_FILE", "telegram-hls.log")
        
        # Security settings
        self.api_key = os.getenv("API_KEY")  # Optional API key for authentication
        self.allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
        
        # Load bot configurations
        self._load_bot_configs()
        
    def _load_env_file(self):
        """Load environment variables from .env file."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    
                    # Skip empty lines and comments
                    if not line or line.startswith('#'):
                        continue
                        
                    # Parse KEY=VALUE format
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        # Remove inline comments
                        if '#' in value:
                            value = value.split('#')[0].strip()
                        
                        # Remove quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                            
                        # Only set if not already in environment
                        if key not in os.environ:
                            os.environ[key] = value
                    else:
                        self.logger.warning(f"Invalid line format in {self.config_path}:{line_num}: {line}")
                        
        except Exception as e:
            self.logger.error(f"Error loading config file {self.config_path}: {e}")
            
    def _load_bot_configs(self):
        """Load Telegram bot configurations."""
        self.bot_configs: List[BotConfig] = []
        
        # Load primary bot
        primary_token = os.getenv("BOT_TOKEN")
        primary_chat_id = os.getenv("CHAT_ID")
        
        if primary_token and primary_chat_id:
            self.bot_configs.append(BotConfig(
                token=primary_token,
                chat_id=primary_chat_id,
                index=0
            ))
            
        # Load additional bots (BOT_TOKEN_1, BOT_TOKEN_2, etc.)
        for i in range(1, 11):  # Support up to 10 additional bots
            token_key = f"BOT_TOKEN_{i}"
            chat_key = f"CHAT_ID_{i}"
            
            token = os.getenv(token_key)
            chat_id = os.getenv(chat_key)
            
            if token and chat_id:
                self.bot_configs.append(BotConfig(
                    token=token,
                    chat_id=chat_id,
                    index=i
                ))
                
        if not self.bot_configs:
            raise ValueError("No Telegram bot configuration found. Please set BOT_TOKEN and CHAT_ID in your .env file.")
            
        self.logger.info(f"Loaded {len(self.bot_configs)} bot configuration(s)")
        
    def _validate_config(self):
        """Validate configuration values."""
        errors = []
        
        # Validate port
        if not (1 <= self.local_port <= 65535):
            errors.append(f"Invalid port number: {self.local_port}")
            
        # Validate cache size
        if self.cache_size < 1024 * 1024:  # Minimum 1MB
            errors.append(f"Cache size too small: {self.cache_size} bytes (minimum 1MB)")
            
        # Validate segment duration
        if self.min_segment_duration > self.max_segment_duration:
            errors.append("MIN_SEGMENT_DURATION cannot be greater than MAX_SEGMENT_DURATION")
            
        # Validate SSL configuration (only if not behind proxy)
        if self.force_https and not self.behind_proxy:
            if not self.ssl_cert_path or not self.ssl_key_path:
                errors.append("SSL certificate and key paths required when FORCE_HTTPS=true and BEHIND_PROXY=false")
            elif not os.path.exists(self.ssl_cert_path) or not os.path.exists(self.ssl_key_path):
                errors.append("SSL certificate or key file not found")
                
        # Validate bot tokens format
        for bot_config in self.bot_configs:
            if not self._validate_bot_token(bot_config.token):
                errors.append(f"Invalid bot token format for bot {bot_config.index}")
                
        # Create directories
        self._create_directories()
        
        if errors:
            raise ValueError(f"Configuration errors:\n" + "\n".join(f"  - {error}" for error in errors))
            
    def _validate_bot_token(self, token: str) -> bool:
        """Validate Telegram bot token format."""
        import re
        # Telegram bot tokens follow the pattern: XXXXXXXXX:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
        pattern = r'^\d{8,10}:[a-zA-Z0-9_-]{35}$'
        return bool(re.match(pattern, token))
        
    def _create_directories(self):
        """Create necessary directories."""
        directories = [
            self.upload_dir,
            self.segments_dir,
            self.cache_dir,
            Path(self.database_path).parent
        ]
        
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.logger.debug(f"Created directory: {directory}")
            except Exception as e:
                self.logger.error(f"Failed to create directory {directory}: {e}")
                raise
                
    def get_bot_configs(self) -> List[Dict[str, Any]]:
        """Get all bot configurations as dictionaries."""
        return [
            {
                "token": bot.token,
                "chat_id": bot.chat_id,
                "index": bot.index
            }
            for bot in self.bot_configs
        ]
        
    def get_bot_config(self, index: int) -> Optional[BotConfig]:
        """Get bot configuration by index."""
        for bot in self.bot_configs:
            if bot.index == index:
                return bot
        return None
        
    def get_ffmpeg_hardware_accel_args(self) -> List[str]:
        """Get FFmpeg hardware acceleration arguments based on configuration."""
        if self.ffmpeg_hardware_accel == "none":
            return []
            
        accel_configs = {
            "nvenc": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
            "vaapi": ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"],
            "qsv": ["-hwaccel", "qsv"],
            "videotoolbox": ["-hwaccel", "videotoolbox"]
        }
        
        if self.ffmpeg_hardware_accel == "auto":
            # Try to detect available hardware acceleration
            import subprocess
            try:
                # Check for NVIDIA GPU
                subprocess.run(["nvidia-smi"], check=True, capture_output=True)
                return accel_configs["nvenc"]
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
                
            try:
                # Check for Intel VAAPI
                if os.path.exists("/dev/dri/renderD128"):
                    return accel_configs["vaapi"]
            except Exception:
                pass
                
            # Default to software encoding
            return []
            
        return accel_configs.get(self.ffmpeg_hardware_accel, [])
        
    def get_base_url(self) -> str:
        """Get the base URL for the application."""
        if self.public_domain:
            protocol = "https" if self.force_https else "http"
            return f"{protocol}://{self.public_domain}"
        else:
            return f"http://{self.local_host}:{self.local_port}"
            
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary for display/serialization."""
        return {
            "server": {
                "host": self.local_host,
                "port": self.local_port,
                "public_domain": self.public_domain,
                "force_https": self.force_https,
                "base_url": self.get_base_url()
            },
            "directories": {
                "upload_dir": str(self.upload_dir),
                "segments_dir": str(self.segments_dir),
                "cache_dir": str(self.cache_dir),
                "database_path": self.database_path
            },
            "processing": {
                "ffmpeg_path": self.ffmpeg_path,
                "hardware_accel": self.ffmpeg_hardware_accel,
                "threads": self.ffmpeg_threads,
                "two_pass_encoding": self.enable_two_pass_encoding,
                "copy_mode": self.enable_copy_mode,
                "copy_mode_threshold_mb": self.copy_mode_threshold // (1024 * 1024)
            },
            "segments": {
                "min_duration": self.min_segment_duration,
                "max_duration": self.max_segment_duration,
                "target_duration": self.target_segment_duration
            },
            "cache": {
                "type": self.cache_type,
                "size_mb": self.cache_size // (1024 * 1024),
                "preload_segments": self.preload_segments,
                "max_concurrent_preloads": self.max_concurrent_preloads
            },
            "limits": {
                "max_upload_size_gb": self.max_upload_size // (1024 * 1024 * 1024),
                "max_concurrent_uploads": self.max_concurrent_uploads,
                "telegram_max_file_size_mb": self.telegram_max_file_size // (1024 * 1024),
                "telegram_file_size_buffer_kb": self.telegram_file_size_buffer // 1024
            },
            "bots": {
                "count": len(self.bot_configs),
                "configs": [
                    {
                        "index": bot.index,
                        "token_preview": f"{bot.token[:8]}...",
                        "chat_id": bot.chat_id
                    }
                    for bot in self.bot_configs
                ]
            }
        }