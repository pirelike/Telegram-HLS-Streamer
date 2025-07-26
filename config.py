"""
Configuration management for the Telegram Video Streaming System.

This module handles loading and validating configuration from environment variables
and .env files, providing a centralized configuration object with proper defaults
and validation.
"""

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AppConfig:
    """
    Application configuration with validation and type safety.

    This class holds all configuration values needed by the application,
    with proper defaults and validation logic.
    """
    # Telegram Configuration
    bot_token: str
    chat_id: str

    # Network Configuration
    local_host: str
    local_port: int
    public_domain: Optional[str]

    # Database Configuration
    db_path: str

    # Server Configuration
    max_upload_size: int  # in bytes
    segment_duration: int  # in seconds
    max_chunk_size: int   # in bytes

    # Logging Configuration
    log_level: str
    log_file: Optional[str]

    # Directories
    temp_upload_dir: str
    segments_dir: str
    playlists_dir: str

    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate()

    def _validate(self) -> None:
        """
        Validate all configuration values.

        Raises:
            ValueError: If any configuration value is invalid
        """
        # Validate Telegram configuration
        if not self.bot_token or not self.bot_token.strip():
            raise ValueError("BOT_TOKEN is required and cannot be empty")

        if not self.chat_id or not self.chat_id.strip():
            raise ValueError("CHAT_ID is required and cannot be empty")

        if not self.chat_id.startswith('@'):
            raise ValueError("CHAT_ID must start with '@' (e.g., @mychannel)")

        # Validate network configuration
        if not (1 <= self.local_port <= 65535):
            raise ValueError(f"LOCAL_PORT must be between 1-65535, got: {self.local_port}")

        if self.public_domain:
            # Basic domain validation
            if self.public_domain.startswith(('http://', 'https://')):
                raise ValueError("PUBLIC_DOMAIN should not include protocol (http/https)")

            if not self._is_valid_domain(self.public_domain):
                raise ValueError(f"Invalid PUBLIC_DOMAIN format: {self.public_domain}")

        # Validate file sizes
        if self.max_upload_size <= 0:
            raise ValueError("MAX_UPLOAD_SIZE must be positive")

        if self.max_chunk_size <= 0:
            raise ValueError("MAX_CHUNK_SIZE must be positive")

        # Validate segment duration
        if not (5 <= self.segment_duration <= 300):
            raise ValueError("SEGMENT_DURATION must be between 5-300 seconds")

        # Validate log level
        valid_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
        if self.log_level.upper() not in valid_levels:
            raise ValueError(f"LOG_LEVEL must be one of: {valid_levels}")

        logger.info("Configuration validation completed successfully")

    @staticmethod
    def _is_valid_domain(domain: str) -> bool:
        """
        Validate domain format.

        Args:
            domain: Domain to validate

        Returns:
            True if domain format is valid
        """
        try:
            # Simple domain validation
            parts = domain.split('.')
            return (
                len(parts) >= 2 and
                all(part and part.replace('-', '').isalnum() for part in parts) and
                len(domain) <= 253
            )
        except Exception:
            return False

    def get_local_url(self) -> str:
        """Get the local server URL."""
        return f"http://{self.local_host}:{self.local_port}"

    def get_public_url(self) -> str:
        """Get the public server URL for external access."""
        if self.public_domain:
            return f"http://{self.public_domain}"
        return self.get_local_url()

    def get_playlist_url(self, video_id: str, public: bool = False) -> str:
        """
        Generate playlist URL for a video.

        Args:
            video_id: The video identifier
            public: If True, use public domain; otherwise use local

        Returns:
            Complete playlist URL
        """
        base_url = self.get_public_url() if public else self.get_local_url()
        return f"{base_url}/playlist/{video_id}.m3u8"


def get_local_ip() -> str:
    """
    Automatically detect the local IP address.

    Returns:
        Local IP address as string, or '127.0.0.1' if detection fails
    """
    try:
        # Connect to a dummy address to determine the local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
        logger.info(f"Detected local IP: {local_ip}")
        return local_ip
    except Exception as e:
        logger.warning(f"Failed to detect local IP, using localhost: {e}")
        return '127.0.0.1'


def load_config(env_file: str = '.env') -> AppConfig:
    """
    Load configuration from environment variables and .env file.

    Args:
        env_file: Path to the .env file

    Returns:
        Validated AppConfig instance

    Raises:
        ValueError: If required configuration is missing or invalid
        FileNotFoundError: If .env file is specified but doesn't exist
    """
    # Load .env file if it exists
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path)
        logger.info(f"Loaded configuration from: {env_path.absolute()}")
    else:
        logger.warning(f".env file not found: {env_path.absolute()}")

    # Helper function to get environment variables with type conversion
    def get_env(key: str, default=None, var_type=str, required=True):
        value = os.getenv(key, default)
        if required and value is None:
            raise ValueError(f"Required environment variable {key} is not set")

        if value is None:
            return None

        try:
            if var_type == bool:
                return value.lower() in ('true', '1', 'yes', 'on')
            return var_type(value)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid value for {key}: {value} (expected {var_type.__name__})") from e

    # Auto-detect local IP if not specified
    local_host = get_env('LOCAL_HOST', get_local_ip(), str, False)

    try:
        config = AppConfig(
            # Telegram Configuration
            bot_token=get_env('BOT_TOKEN'),
            chat_id=get_env('CHAT_ID'),

            # Network Configuration
            local_host=local_host,
            local_port=get_env('LOCAL_PORT', 8080, int, False),
            public_domain=get_env('PUBLIC_DOMAIN', None, str, False),

            # Database Configuration
            db_path=get_env('DB_PATH', 'video_streaming.db', str, False),

            # Server Configuration
            max_upload_size=get_env('MAX_UPLOAD_SIZE', 50 * 1024**3, int, False),  # 50GB
            segment_duration=get_env('SEGMENT_DURATION', 30, int, False),
            max_chunk_size=get_env('MAX_CHUNK_SIZE', 20 * 1024**2, int, False),    # 20MB

            # Logging Configuration
            log_level=get_env('LOG_LEVEL', 'INFO', str, False),
            log_file=get_env('LOG_FILE', None, str, False),

            # Directories
            temp_upload_dir=get_env('TEMP_UPLOAD_DIR', 'temp_uploads', str, False),
            segments_dir=get_env('SEGMENTS_DIR', 'segments', str, False),
            playlists_dir=get_env('PLAYLISTS_DIR', 'playlists', str, False),
        )

        logger.info("Configuration loaded successfully")
        logger.info(f"Local server will run on: {config.get_local_url()}")
        if config.public_domain:
            logger.info(f"Public access available at: {config.get_public_url()}")

        return config

    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        raise


# Global configuration instance
# This will be imported by other modules
config = load_config()
