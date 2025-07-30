"""
Telegram HLS Video Streaming Server
==================================

A comprehensive video streaming solution that processes videos into HLS format
and streams them through Telegram bots with advanced caching and multi-bot support.
"""

__version__ = "2.0.0"
__author__ = "Telegram HLS Streamer Team"

# Core modules
from src.core.app import TelegramHLSApp
from src.core.config import Config
from src.core.exceptions import *

__all__ = [
    'TelegramHLSApp',
    'Config',
    '__version__',
    '__author__'
]