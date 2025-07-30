"""
Custom exceptions for the Telegram HLS Streamer.
"""


class TelegramHLSError(Exception):
    """Base exception for all Telegram HLS Streamer errors."""
    pass


class ConfigurationError(TelegramHLSError):
    """Raised when there's a configuration error."""
    pass


class ProcessingError(TelegramHLSError):
    """Base exception for processing errors."""
    pass


class VideoProcessingError(ProcessingError):
    """Raised when video processing fails."""
    pass


class TelegramError(TelegramHLSError):
    """Raised when Telegram API operations fail."""
    pass


class DatabaseError(TelegramHLSError):
    """Raised when database operations fail."""
    pass


class CacheError(TelegramHLSError):
    """Raised when cache operations fail."""
    pass


class HardwareAccelerationError(TelegramHLSError):
    """Raised when hardware acceleration setup fails."""
    pass


class StreamingError(TelegramHLSError):
    """Raised when streaming operations fail."""
    pass