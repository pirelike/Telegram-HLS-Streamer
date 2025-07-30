"""
Configuration management for Telegram HLS Streamer.
"""

import os
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
from .exceptions import ConfigurationError


class Config:
    """Centralized configuration management."""
    
    def __init__(self, env_file: str = '.env'):
        self.env_file = env_file
        self._config = {}
        self._load_environment()
        self._validate_required_settings()
    
    def _load_environment(self):
        """Load configuration from environment variables and .env file."""
        # Load from .env file if it exists
        if os.path.exists(self.env_file):
            try:
                with open(self.env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Remove quotes from value if present
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            os.environ.setdefault(key, value)
            except Exception as e:
                raise ConfigurationError(f"Failed to load .env file: {e}")
    
    def _validate_required_settings(self):
        """Validate that required settings are present."""
        # Skip validation if we have placeholder tokens (for development)
        bot_token = self.get('BOT_TOKEN')
        if bot_token and bot_token == 'your_bot_token_here':
            return  # Skip validation for placeholder tokens
            
        # At least one bot token is required for production
        if not (self.get('BOT_TOKEN') or self._has_multi_bot_config()):
            import warnings
            warnings.warn(
                "No bot token configured. Web interface will load but functionality will be limited.",
                UserWarning
            )
            return
        
        # Chat ID is required if we have a bot token
        if self.get('BOT_TOKEN') and not self.get('CHAT_ID'):
            import warnings
            warnings.warn("CHAT_ID is required when BOT_TOKEN is set", UserWarning)
    
    def _has_multi_bot_config(self) -> bool:
        """Check if multi-bot configuration is present."""
        for i in range(1, 11):
            if self.get(f'BOT_TOKEN_{i}'):
                return True
        return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value."""
        return os.getenv(key, default)
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value."""
        try:
            return int(self.get(key, default))
        except (ValueError, TypeError):
            return default
    
    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get float configuration value."""
        try:
            return float(self.get(key, default))
        except (ValueError, TypeError):
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value."""
        value = self.get(key, '').lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        elif value in ('false', '0', 'no', 'off'):
            return False
        return default
    
    def get_list(self, key: str, separator: str = ',', default: List[str] = None) -> List[str]:
        """Get list configuration value."""
        if default is None:
            default = []
        value = self.get(key, '')
        if not value:
            return default
        return [item.strip() for item in value.split(separator) if item.strip()]
    
    # Telegram Configuration
    @property
    def bot_token(self) -> Optional[str]:
        """Primary bot token."""
        return self.get('BOT_TOKEN')
    
    @property
    def chat_id(self) -> Optional[str]:
        """Primary chat ID."""
        return self.get('CHAT_ID')
    
    @property
    def multi_bot_tokens(self) -> Dict[int, str]:
        """Get all bot tokens (multi-bot support)."""
        tokens = {}
        
        # Add primary bot as bot 1 if it exists
        if self.bot_token:
            tokens[1] = self.bot_token
        
        # Add numbered bots (2-10) and potentially override bot 1 if BOT_TOKEN_1 exists
        for i in range(1, 11):
            token = self.get(f'BOT_TOKEN_{i}')
            if token:
                tokens[i] = token
        
        return tokens
    
    @property
    def multi_bot_chats(self) -> Dict[int, str]:
        """Get all chat IDs (multi-bot support)."""
        chats = {}
        
        # Add primary chat as chat 1 if it exists
        if self.chat_id:
            chats[1] = self.chat_id
        
        # Add numbered chats (2-10) and potentially override chat 1 if CHAT_ID_1 exists
        for i in range(1, 11):
            chat = self.get(f'CHAT_ID_{i}')
            if chat:
                chats[i] = chat
        
        return chats
    
    # Network Configuration
    @property
    def local_host(self) -> str:
        """Local host address."""
        return self.get('LOCAL_HOST', '0.0.0.0')
    
    @property
    def local_port(self) -> int:
        """Local port number."""
        return self.get_int('LOCAL_PORT', 8080)
    
    @property
    def public_domain(self) -> Optional[str]:
        """Public domain for external access."""
        domain = self.get('PUBLIC_DOMAIN')
        if domain:
            # Remove quotes if present
            domain = domain.strip('"\'')
        return domain if domain else None
    
    @property
    def force_https(self) -> bool:
        """Force HTTPS usage."""
        return self.get_bool('FORCE_HTTPS', False)
    
    # SSL Configuration
    @property
    def ssl_cert_path(self) -> Optional[str]:
        """SSL certificate path."""
        return self.get('SSL_CERT_PATH')
    
    @property
    def ssl_key_path(self) -> Optional[str]:
        """SSL private key path."""
        return self.get('SSL_KEY_PATH')
    
    # Video Processing Configuration
    @property
    def max_upload_size(self) -> int:
        """Maximum upload size in bytes."""
        return self.get_int('MAX_UPLOAD_SIZE', 50 * 1024**3)  # 50GB default
    
    @property
    def max_chunk_size(self) -> int:
        """Maximum chunk size in bytes."""
        return self.get_int('MAX_CHUNK_SIZE', 15 * 1024 * 1024)  # 15MB default
    
    @property
    def min_segment_duration(self) -> int:
        """Minimum segment duration in seconds."""
        return self.get_int('MIN_SEGMENT_DURATION', 2)
    
    @property
    def max_segment_duration(self) -> int:
        """Maximum segment duration in seconds."""
        return self.get_int('MAX_SEGMENT_DURATION', 30)
    
    @property
    def ffmpeg_threads(self) -> int:
        """Number of FFmpeg threads."""
        return self.get_int('FFMPEG_THREADS', 2)
    
    @property
    def ffmpeg_hardware_accel(self) -> str:
        """FFmpeg hardware acceleration setting."""
        return self.get('FFMPEG_HARDWARE_ACCEL', 'auto')
    
    @property
    def emergency_segment_duration(self) -> float:
        """Emergency segment duration for problematic videos."""
        return self.get_float('EMERGENCY_SEGMENT_DURATION', 0.5)
    
    @property
    def emergency_video_scale(self) -> int:
        """Emergency video scale resolution."""
        return self.get_int('EMERGENCY_VIDEO_SCALE', 1280)
    
    # Cache Configuration  
    @property
    def cache_type(self) -> str:
        """Cache type (memory/disk)."""
        return self.get('CACHE_TYPE', 'memory')
    
    @property
    def cache_size(self) -> int:
        """Cache size in bytes."""
        return self.get_int('CACHE_SIZE', 500 * 1024 * 1024)  # 500MB default
    
    @property
    def cache_dir(self) -> str:
        """Cache directory path."""
        return self.get('CACHE_DIR', 'cache')
    
    @property
    def preload_segments(self) -> int:
        """Number of segments to preload."""
        return self.get_int('PRELOAD_SEGMENTS', 8)
    
    @property
    def max_concurrent_preloads(self) -> int:
        """Maximum concurrent preload operations."""
        return self.get_int('MAX_CONCURRENT_PRELOADS', 5)
    
    # Directory Configuration
    @property
    def playlists_dir(self) -> str:
        """Playlists directory."""
        return self.get('PLAYLISTS_DIR', 'playlists')
    
    @property
    def segments_dir(self) -> str:
        """Segments directory."""
        return self.get('SEGMENTS_DIR', 'segments')
    
    @property
    def temp_uploads_dir(self) -> str:
        """Temporary uploads directory."""
        return self.get('TEMP_UPLOADS_DIR', 'temp_uploads')
    
    @property
    def database_path(self) -> str:
        """Database file path."""
        return self.get('DATABASE_PATH', 'video_streaming.db')
    
    # Logging Configuration
    @property
    def log_level(self) -> str:
        """Logging level."""
        return self.get('LOG_LEVEL', 'INFO')
    
    @property
    def log_file(self) -> Optional[str]:
        """Log file path."""
        return self.get('LOG_FILE')
    
    def setup_directories(self):
        """Create necessary directories."""
        directories = [
            self.playlists_dir,
            os.path.join(self.playlists_dir, 'local'),
            os.path.join(self.playlists_dir, 'public'),
            self.segments_dir,
            self.temp_uploads_dir,
            self.cache_dir
        ]
        
        for directory in directories:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise ConfigurationError(f"Failed to create directory {directory}: {e}")
    
    def setup_logging(self):
        """Setup logging configuration."""
        level = getattr(logging, self.log_level.upper(), logging.INFO)
        
        # Configure root logger
        logging.basicConfig(
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                *([] if not self.log_file else [logging.FileHandler(self.log_file)])
            ]
        )
    
    def get_settings_dict(self) -> Dict[str, Any]:
        """Get all settings as dictionary for API responses."""
        # Read all variables from .env file
        all_env_vars = {}
        if os.path.exists(self.env_file):
            try:
                with open(self.env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Remove quotes from value if present
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            all_env_vars[key] = value
            except Exception as e:
                # Fallback to minimal settings if .env read fails
                pass
        
        # Mask sensitive bot tokens for security
        for key in all_env_vars:
            if key.startswith('BOT_TOKEN') and all_env_vars[key]:
                all_env_vars[key] = '***SET***'
        
        return all_env_vars
    
    def save_settings(self, settings: Dict[str, Any]):
        """Save settings to .env file."""
        # Read current .env file
        current_env = {}
        if os.path.exists(self.env_file):
            try:
                with open(self.env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            current_env[key.strip()] = value.strip()
            except Exception as e:
                raise ConfigurationError(f"Failed to read .env file: {e}")
        
        # Update with new settings
        for key, value in settings.items():
            if not value:  # Skip empty values
                continue
                
            # Handle special conversions
            if key == 'MAX_UPLOAD_SIZE':
                value = str(int(float(value) * 1024**3))
            elif key == 'MAX_CHUNK_SIZE':
                value = str(int(float(value) * 1024 * 1024))
            elif key == 'CACHE_SIZE':
                value = str(int(float(value) * 1024 * 1024))
            elif key.startswith('BOT_TOKEN') and value == '***SET***':
                continue  # Don't update if placeholder
            
            current_env[key] = value
        
        # Write updated .env file
        try:
            with open(self.env_file, 'w') as f:
                f.write("# Telegram HLS Streamer Configuration\n")
                f.write("# Generated by web interface\n\n")
                
                # Group settings logically
                telegram_settings = ['BOT_TOKEN', 'CHAT_ID']
                for i in range(2, 11):
                    telegram_settings.extend([f'BOT_TOKEN_{i}', f'CHAT_ID_{i}'])
                
                network_settings = ['LOCAL_HOST', 'LOCAL_PORT', 'PUBLIC_DOMAIN', 'FORCE_HTTPS', 'SSL_CERT_PATH', 'SSL_KEY_PATH']
                video_settings = ['MAX_UPLOAD_SIZE', 'MAX_CHUNK_SIZE', 'MIN_SEGMENT_DURATION', 'MAX_SEGMENT_DURATION', 'FFMPEG_THREADS', 'FFMPEG_HARDWARE_ACCEL']
                cache_settings = ['CACHE_TYPE', 'CACHE_SIZE', 'PRELOAD_SEGMENTS']
                system_settings = ['LOG_LEVEL', 'LOG_FILE']
                
                def write_section(title, keys):
                    f.write(f"# {title}\n")
                    for key in keys:
                        if key in current_env:
                            f.write(f"{key}={current_env[key]}\n")
                    f.write("\n")
                
                write_section("Telegram Configuration", telegram_settings)
                write_section("Network Configuration", network_settings)
                write_section("Video Processing", video_settings)
                write_section("Cache Configuration", cache_settings)
                write_section("System Settings", system_settings)
                
                # Write any remaining settings
                all_known_keys = set(telegram_settings + network_settings + video_settings + cache_settings + system_settings)
                remaining_keys = set(current_env.keys()) - all_known_keys
                if remaining_keys:
                    write_section("Additional Settings", remaining_keys)
        
        except Exception as e:
            raise ConfigurationError(f"Failed to save .env file: {e}")


# Global configuration instance
_config = None

def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config