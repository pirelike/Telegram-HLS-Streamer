# 🎬 Telegram HLS Streamer

Transform Telegram into your unlimited personal Netflix storage! This sophisticated video streaming server uses multiple Telegram bots as cloud storage, automatically converts videos to HLS format, and provides a modern web interface for seamless streaming.

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/Status-Development-orange.svg)

## ✨ Features

### 🚀 Core Functionality
- **HLS Video Streaming**: Automatic conversion to HTTP Live Streaming format with hardware acceleration
- **Multi-Bot Distribution**: Uses 8 Telegram bots with intelligent round-robin distribution and bot isolation
- **Unlimited Storage**: Leverage Telegram's infrastructure as your personal cloud storage
- **Streaming Uploads**: Memory-efficient upload handling for large files (multi-GB support)
- **Real-time Progress**: Live upload and processing progress with speed and ETA calculations

### 🧠 Intelligence Features  
- **Copy Mode**: Lossless processing for HLS-compatible files ≥20MB (H.264/HEVC + AAC/MP3)
- **Smart Caching**: LRU eviction with predictive preloading for optimal streaming performance
- **Configurable Limits**: Telegram 20MB segment limit (future-proof and configurable)
- **Hardware Acceleration**: VAAPI, NVENC, QSV support for blazing-fast encoding

## 🛠️ Quick Start

### ⚙️ Configuration

### Telegram Bot Setup

You need 8 Telegram bots for optimal performance. For each bot:

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Create a private channel for the bot
3. Add the bot as an administrator to the channel
4. Add tokens and channel IDs to `.env`

### Environment Variables

```bash
# Server Configuration
LOCAL_HOST=0.0.0.0
LOCAL_PORT=5050
FORCE_HTTPS=false
BEHIND_PROXY=true

# File Handling
TELEGRAM_MAX_FILE_SIZE=20971520  # 20MB (configurable)
MAX_UPLOAD_SIZE=21474836480      # 20GB
ENABLE_COPY_MODE=true

# Hardware Acceleration (auto-detected)
ENABLE_HARDWARE_ACCELERATION=true
PREFERRED_ENCODER=vaapi  # vaapi, nvenc, qsv
VIDEO_BITRATE=4M         # target video bitrate when encoding

# Bot Tokens
TELEGRAM_BOT_TOKEN_1=your_bot_1_token
TELEGRAM_BOT_TOKEN_2=your_bot_2_token
# ... up to 8 bots

# Channel IDs  
TELEGRAM_CHANNEL_ID_1=-100xxxxxxxxxx
TELEGRAM_CHANNEL_ID_2=-100xxxxxxxxxx
# ... corresponding channels

# Upload performance
UPLOAD_PARALLELISM=8     # concurrent uploads cap across bots
```

### Copy Mode Logic

The system intelligently determines when to use lossless copy mode:

- **File size**: Must be ≥20MB 
- **Video codec**: H.264 or HEVC
- **Audio codec**: AAC or MP3
- **Container**: Compatible with HLS

When copy mode is used, files are processed without re-encoding, dramatically reducing processing time and preserving quality.
