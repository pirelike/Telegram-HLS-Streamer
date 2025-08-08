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

### 🎨 User Experience
- **Netflix-style Interface**: Modern, responsive web interface with dark/light themes
- **Drag & Drop Upload**: Intuitive file upload with progress tracking
- **Library Management**: Organize and browse your video collection
- **System Monitoring**: Real-time bot status, cache statistics, and system health

## 🛠️ Quick Start

### Prerequisites
- Python 3.8+
- FFmpeg with hardware acceleration support
- 8 Telegram bot tokens (for optimal distribution)
- Telegram channels for each bot

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd telegram-hls
   ```

2. **Set up virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Telegram bot tokens and settings
   ```

5. **Run the application**
   ```bash
   python main.py serve
   ```

6. **Access the web interface**
   ```
   http://localhost:5050
   ```

## ⚙️ Configuration

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

# Bot Tokens
TELEGRAM_BOT_TOKEN_1=your_bot_1_token
TELEGRAM_BOT_TOKEN_2=your_bot_2_token
# ... up to 8 bots

# Channel IDs  
TELEGRAM_CHANNEL_ID_1=-100xxxxxxxxxx
TELEGRAM_CHANNEL_ID_2=-100xxxxxxxxxx
# ... corresponding channels
```

### Copy Mode Logic

The system intelligently determines when to use lossless copy mode:

- **File size**: Must be ≥20MB 
- **Video codec**: H.264 or HEVC
- **Audio codec**: AAC or MP3
- **Container**: Compatible with HLS

When copy mode is used, files are processed without re-encoding, dramatically reducing processing time and preserving quality.

## 🏗️ Architecture

```
telegram-hls/
├── main.py                 # Application entry point
├── src/
│   ├── config.py          # Configuration management
│   ├── telegram_handler.py # Multi-bot management
│   ├── video_processor.py  # FFmpeg integration
│   ├── database.py        # SQLite data layer
│   ├── cache_manager.py   # Smart caching system
│   └── web_server.py      # REST API & streaming
├── web/                   # Frontend interface
│   ├── index.html
│   ├── css/style.css
│   └── js/                # Modular JavaScript
├── requirements.txt
├── .env.example
├── CLAUDE.md             # Development notes
└── README.md
```

### Component Overview

- **Video Processor**: FFmpeg integration with hardware acceleration and copy mode detection
- **Telegram Manager**: Handles 8-bot distribution with hash-based segment assignment
- **Cache Manager**: LRU caching with predictive preloading
- **Web Server**: aiohttp-based REST API with streaming upload support
- **Database**: SQLite for metadata storage and video library management

## 📊 API Reference

### Upload & Progress
- `POST /api/upload` - Stream upload video files with progress tracking
- `GET /api/upload/{upload_id}/progress` - Real-time upload progress and ETA

### Video Management
- `GET /api/videos` - List all processed videos
- `GET /api/videos/{video_id}` - Get video details and metadata
- `DELETE /api/videos/{video_id}` - Remove video and cleanup segments
- `GET /api/videos/{video_id}/status` - Processing status and progress

### HLS Streaming
- `GET /hls/{video_id}/master.m3u8` - Master playlist for video
- `GET /hls/{video_id}/{track}/playlist.m3u8` - Track-specific playlist
- `GET /hls/{video_id}/{track}/{segment}` - Video segment download

### System Management
- `GET /api/system/status` - System health and statistics
- `GET /api/system/bots/status` - Bot distribution and health
- `POST /api/system/bots/test` - Test all Telegram bot connections
- `GET /api/system/cache/stats` - Cache hit rates and storage info
- `POST /api/system/cache/clear` - Clear system cache

## 🔧 CLI Commands

```bash
# Start the server
python main.py serve

# Test all Telegram bots
python main.py test-bots

# Show current configuration
python main.py config
```

## 🚀 Performance Features

### Streaming Uploads
- **Memory Efficient**: 64KB chunks prevent memory exhaustion
- **Progress Tracking**: Real-time progress with speed and ETA
- **Large File Support**: Handle multi-GB files without issues
- **Concurrent Uploads**: Support multiple simultaneous uploads

### Hardware Acceleration
Auto-detects and configures:
- **VAAPI** (Intel/AMD GPUs on Linux)
- **NVENC** (NVIDIA GPUs)
- **QSV** (Intel Quick Sync Video)
- **Software fallback** when hardware unavailable

### Bot Isolation & Distribution
- **Deterministic Assignment**: Segments consistently assigned to same bot
- **Even Distribution**: Hash-based load balancing across 8 bots  
- **Fault Tolerance**: Continues operation even if some bots fail
- **Security**: Segments tied to specific bots for isolation

## 🛡️ Security Features

- **Bot Isolation**: Segments can only be retrieved by their assigned bot
- **No Secret Logging**: Sensitive data never written to logs
- **Proper Cleanup**: Failed uploads and processing attempts cleaned up
- **CORS Support**: Configurable cross-origin resource sharing

## 📈 Monitoring & Debugging

### System Status
The web interface provides real-time monitoring:
- Bot connection status and message counts
- Cache hit/miss ratios and storage usage
- Active uploads and processing jobs
- System resource utilization

### Logging
Comprehensive logging for troubleshooting:
- Upload progress and errors
- Video processing steps and performance
- Bot communication status
- Cache operations and performance

## 🤝 Development

This project is in active development. See `CLAUDE.md` for detailed development notes.

### Development Setup
```bash
# Clone and setup
git clone <repo>
cd telegram-hls
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure for development
cp .env.example .env
# Edit .env with your settings

# Run in development mode
python main.py serve
```

### Key Development Notes
- **No Backwards Compatibility**: Breaking changes allowed during development
- **Test Coverage**: Use provided video files for end-to-end testing
- **Bot Health**: Verify all 8 bots working before major changes
- **Performance**: Monitor upload/processing performance with test files

## 📝 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

- **Telegram** for providing the bot API and generous file limits
- **FFmpeg** for powerful video processing capabilities
- **aiohttp** for async web framework
- Modern web standards for HLS streaming support

## ⚠️ Disclaimer

This tool is for personal use only. Ensure you comply with Telegram's Terms of Service and any applicable laws regarding content storage and distribution. The developers are not responsible for misuse of this software.

---

**Turn your Telegram into unlimited Netflix storage! 🍿**
