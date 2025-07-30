# 🎬 Telegram Video Streaming System

A sophisticated Python application that transforms Telegram into a **distributed video storage and streaming platform**. This system intelligently processes videos, stores them across Telegram channels, and provides on-demand HLS streaming with advanced features like multi-bot uploads, subtitle support, and smart caching.

## 🌟 What This System Does

Think of this as your personal Netflix, but using Telegram's infrastructure:

1. **📤 Upload**: You give it a video file
2. **✂️ Process**: It intelligently splits the video into small chunks
3. **📡 Store**: It uploads chunks to your Telegram channels using multiple bots
4. **🎥 Stream**: It creates streaming URLs you can use anywhere
5. **🚀 Serve**: You can watch from any device that supports HLS (VLC, browsers, Jellyfin, etc.)

### 🎯 Key Benefits
- **🆓 Free Storage**: Use Telegram's generous file limits
- **⚡ Multi-Bot Speed**: Up to 10x faster uploads with multiple bots
- **🌐 Network Streaming**: Stream to any device on your network
- **📱 Universal Compatibility**: Works with VLC, web browsers, media servers
- **📄 Subtitle Support**: Automatically extracts and serves subtitles
- **🧠 Smart Processing**: Minimizes re-encoding for faster processing

---

## 📋 Table of Contents

1. [🏗️ System Architecture](#️-system-architecture)
2. [✨ Features Overview](#-features-overview)
3. [📦 Installation & Setup](#-installation--setup)
4. [🔧 Configuration](#-configuration)
5. [🚀 Usage Guide](#-usage-guide)
6. [🤖 Multi-Bot Setup](#-multi-bot-setup)
7. [🎬 How Video Processing Works](#-how-video-processing-works)
8. [🌐 Streaming & Access](#-streaming--access)
9. [📄 Subtitle System](#-subtitle-system)
10. [💾 Database & Cache](#-database--cache)
11. [🔍 Monitoring & Debugging](#-monitoring--debugging)
12. [🛠️ Advanced Configuration](#️-advanced-configuration)
13. [❓ Troubleshooting](#-troubleshooting)
14. [🏆 Best Practices](#-best-practices)

---

## 🏗️ System Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Video File    │───►│  Smart Processor │───►│   Multi-Bot     │
│   (Any Format)  │    │   (FFmpeg HLS)   │    │   Uploader      │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                ▲                        │
                        ┌───────┴────────┐               ▼
                        │ Finds Optimal  │     ┌─────────────────┐
                        │ Segment Size   │     │ Telegram Bots   │
                        │ Minimizes      │     │ (1-10 bots for  │
                        │ Re-encoding    │     │  parallel upload)│
                        └────────────────┘     └─────────────────┘
                                                         │
┌─────────────────┐    ┌──────────────────┐             ▼
│ Media Players   │◄───│ HLS Streaming    │    ┌─────────────────┐
│ • VLC           │    │ Server           │◄───│   SQLite DB     │
│ • Browsers      │    │ • Caching        │    │ • Video metadata│
│ • Jellyfin      │    │ • Subtitles      │    │ • Segment info  │
│ • Mobile Apps   │    │ • Multi-format   │    │ • Subtitles     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

### 🧩 Core Components

| Component | Purpose | What It Does |
|-----------|---------|--------------|
| **`main.py`** | Entry Point | Command-line interface, server startup, configuration validation |
| **`video_processor.py`** | Smart Processing | Intelligently splits videos, minimizes re-encoding, extracts subtitles |
| **`telegram_handler.py`** | Multi-Bot Upload | Round-robin uploads across multiple bots for speed |
| **`stream_server.py`** | HTTP Server | Serves HLS streams, subtitles, web UI with SSL support |
| **`database.py`** | Data Management | SQLite database for video metadata, segments, subtitles |
| **`cache_manager.py`** | Performance | Memory/disk caching for fast segment delivery |
| **`utils.py`** | Utilities | Helper functions for IP detection, file hashing, validation |

---

## ✨ Features Overview

### 🚀 **Smart Video Processing**
- **Intelligent Segmentation**: Automatically finds the optimal segment duration to minimize file sizes
- **Hybrid Encoding**: Only re-encodes segments that exceed size limits, preserving quality
- **Format Support**: Handles MP4, MKV, WebM, and most video formats
- **Subtitle Extraction**: Automatically extracts and serves subtitle tracks

### 🤖 **Multi-Bot Upload System**
- **Round-Robin Distribution**: Spreads segments across multiple Telegram bots
- **Parallel Processing**: Upload segments simultaneously for massive speed improvements
- **Rate Limit Isolation**: Each bot has separate rate limits for consistent performance
- **Automatic Failover**: Continues working even if some bots are rate-limited

### 🌐 **Advanced Streaming**
- **HLS Compatibility**: Works with all major media players and browsers
- **Dual Access**: Local network and public internet streaming
- **SSL/HTTPS Support**: Secure streaming with certificate or reverse proxy support
- **Subtitle Integration**: HLS-compliant subtitle serving

### 💾 **Intelligent Caching**
- **Memory Cache**: Ultra-fast in-memory caching for active segments
- **Disk Cache**: Persistent caching that survives restarts
- **Predictive Caching**: Smart preloading based on viewing patterns
- **Cache Warming**: Automatically pre-cache popular content
- **Session Tracking**: Optimized caching per user session
- **LRU Eviction**: Automatically manages cache space efficiently
- **Cache Statistics**: Monitor cache hit rates and performance

### 🖥️ **Modern Web Interface**
- **Dark/Light Theme**: Beautiful, responsive web UI
- **Complete Settings Panel**: Configure all .env variables through UI
- **Real-time Logs**: Watch processing progress live
- **System Monitoring**: CPU, memory, and cache statistics
- **Bot Management**: Visual configuration and testing of all bots
- **Drag & Drop**: Easy file uploading
- **Live Configuration**: Changes saved instantly to .env file

---

## 📦 Installation & Setup

### 📋 **Prerequisites**

```bash
# System Requirements
- Python 3.8 or higher
- FFmpeg (for video processing)
- 2GB+ RAM (for memory caching)
- Stable internet connection

# Operating System Support
- ✅ Windows 10/11
- ✅ macOS 10.15+
- ✅ Linux (Ubuntu 18.04+, Debian 10+, CentOS 8+)
```

### 🔧 **Step 1: Install FFmpeg**

<details>
<summary><b>🪟 Windows Installation</b></summary>

1. Download FFmpeg from https://ffmpeg.org/download.html
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to your PATH environment variable
4. Verify: Open Command Prompt and run `ffmpeg -version`

</details>

<details>
<summary><b>🍎 macOS Installation</b></summary>

```bash
# Using Homebrew (recommended)
brew install ffmpeg

# Verify installation
ffmpeg -version
```

</details>

<details>
<summary><b>🐧 Linux Installation</b></summary>

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install ffmpeg

# CentOS/RHEL
sudo yum install epel-release
sudo yum install ffmpeg

# Verify installation
ffmpeg -version
```

</details>

### 🐍 **Step 2: Python Setup**

```bash
# Clone the repository
git clone https://github.com/yourusername/telegram-video-streaming.git
cd telegram-video-streaming

# Create virtual environment (recommended)
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 📦 **Dependencies Explained**

| Package | Purpose | Why We Need It |
|---------|---------|----------------|
| `python-telegram-bot` | Telegram API | Upload/download files to/from Telegram |
| `aiohttp` | HTTP Server | Serve streaming content and web UI |
| `aiofiles` | Async File I/O | Handle large files efficiently |
| `aiosqlite` | Async Database | Store video metadata and segments |
| `aiohttp-jinja2` | Web Templates | Render the beautiful web interface |
| `httpx` | HTTP Client | Make API requests |
| `psutil` | System Info | Monitor CPU, memory, disk usage |

---

## 🔧 Configuration

### 🤖 **Step 1: Create Telegram Bots**

You'll need at least one Telegram bot, but multiple bots give you much faster uploads!

1. **Message @BotFather on Telegram**
2. **Create your first bot**:
   ```
   /newbot
   Choose a name: My Streaming Bot
   Choose a username: mystreaming_bot
   ```
3. **Save the bot token** (looks like `123456789:ABCdef...`)
4. **Create a public channel** for file storage
5. **Add your bot as admin** to the channel
6. **Get channel username** (like `@mychannelname`)

<details>
<summary><b>🚀 Create Multiple Bots for Speed (Recommended)</b></summary>

For 3x faster uploads, create 3 bots:

1. **Create Bot 1**: `/newbot` → `streaming_bot_1` → Save token
2. **Create Bot 2**: `/newbot` → `streaming_bot_2` → Save token  
3. **Create Bot 3**: `/newbot` → `streaming_bot_3` → Save token

Create separate channels or use the same channel for all bots.

</details>

### ⚙️ **Step 2: Configuration**

#### **Method 1: Web Interface (Recommended)**
1. **Start with basic config**: Create a minimal `.env` file:
   ```env
   BOT_TOKEN="your_first_bot_token"
   CHAT_ID="@yourchannel"
   LOCAL_HOST="0.0.0.0"
   LOCAL_PORT="8080"
   ```
2. **Start the server**: `python main.py serve`
3. **Open web interface**: `http://localhost:8080`
4. **Configure via web**:
   - 📡 **Telegram Configuration tab**: Add all your bots visually
   - ⚙️ **Settings tab**: Configure system settings
   - 🧪 **Test everything**: Use built-in testing tools

#### **Method 2: Manual .env File**
Create a `.env` file in the project root:

```bash
# Copy the example configuration
cp .env.example .env

# Edit with your favorite editor
nano .env
```

### 📝 **Step 3: Basic Configuration**

Here's a **minimum working configuration**:

```env
# ===== BASIC SETUP =====
# Your primary bot (REQUIRED)
BOT_TOKEN="123456789:ABCdef_your_bot_token_here"
CHAT_ID="@yourchannelname"

# Server settings
LOCAL_HOST="0.0.0.0"  # Allows network access
LOCAL_PORT="8080"
```

### 🚀 **Step 4: Multi-Bot Configuration (Recommended)**

For **faster uploads**, add multiple bots:

```env
# ===== MULTI-BOT SETUP =====
# Primary bot
BOT_TOKEN="123456789:ABCdef_first_bot_token"
CHAT_ID="@yourchannel1"

# Additional bots for speed
BOT_TOKEN_2="987654321:XYZabc_second_bot_token"
CHAT_ID_2="@yourchannel2"

BOT_TOKEN_3="456789123:DEFghi_third_bot_token"
CHAT_ID_3="@yourchannel3"
```

### 🌐 **Step 5: Network Configuration**

```env
# ===== NETWORK SETUP =====
# Your router IP (find with ipconfig/ifconfig)
LOCAL_HOST="192.168.1.100"  # Your actual IP
LOCAL_PORT="8080"

# For internet access (optional)
PUBLIC_DOMAIN="yourdomain.com"  # If you have a domain
FORCE_HTTPS="true"  # If using reverse proxy
```

---

## 🚀 Usage Guide

> **🎆 New: Integrated Web Dashboard**  
> All functionality is now available through a modern tabbed web interface! No need for separate configuration pages or command-line tools for basic operations.

### 🖥️ **Method 1: Web Interface (Easiest)**

1. **Start the server**:
   ```bash
   python main.py serve
   ```

2. **Open your browser**:
   ```
   http://localhost:8080  # Local access
   http://192.168.1.100:8080  # Network access
   ```

3. **Upload a video**:
   - Drag and drop or click to select
   - Watch real-time processing logs
   - Get streaming URLs when complete

4. **Stream your video**:
   - Copy the provided URL
   - Open in VLC, browser, or media server

### 💻 **Method 2: Command Line**

```bash
# Upload a video
python main.py upload --video movie.mp4

# List all videos
python main.py list

# Delete a video
python main.py delete --video-id movie

# Show configuration
python main.py config

# Test your bots
python main.py test-bots
```

### 📱 **Method 3: Integration Examples**

<details>
<summary><b>🎬 Jellyfin Media Server Integration</b></summary>

1. Create a `.strm` file in your Jellyfin library:
   ```bash
   echo "http://192.168.1.100:8080/playlist/local/movie.m3u8" > movie.strm
   ```

2. Refresh your Jellyfin library

3. Stream directly through Jellyfin!

</details>

<details>
<summary><b>📺 VLC Network Stream</b></summary>

1. Open VLC
2. Media → Open Network Stream
3. Enter: `http://192.168.1.100:8080/playlist/local/movie.m3u8`
4. Click Play!

</details>

---

## 🤖 Multi-Bot Setup

### 🎯 **Why Use Multiple Bots?**

| Bots | Upload Speed | Rate Limits | Reliability |
|------|-------------|-------------|-------------|
| 1 Bot | 1x (baseline) | Shared limits | Single point of failure |
| 3 Bots | ~3x faster | Isolated limits | High reliability |
| 5 Bots | ~5x faster | Very isolated | Very high reliability |

### 🔄 **How Round-Robin Works**

```
Video with 12 segments + 3 bots:

Bot 1 uploads: segments 0, 3, 6, 9    (4 segments)
Bot 2 uploads: segments 1, 4, 7, 10   (4 segments)  
Bot 3 uploads: segments 2, 5, 8, 11   (4 segments)

All uploads happen simultaneously = 3x faster!
```

### ⚙️ **Configuration Methods**

<details>
<summary><b>Method 1: Environment Variables (Simple)</b></summary>

```env
# Primary bot
BOT_TOKEN="token1"
CHAT_ID="@channel1"

# Additional bots
BOT_TOKEN_2="token2"
CHAT_ID_2="@channel2"

BOT_TOKEN_3="token3"
CHAT_ID_3="@channel3"
```

</details>

<details>
<summary><b>Method 2: JSON Configuration (Advanced)</b></summary>

```env
MULTI_BOT_CONFIG='[
  {"token": "token1", "chat_id": "@channel1"},
  {"token": "token2", "chat_id": "@channel2"},
  {"token": "token3", "chat_id": "@channel3"}
]'
```

</details>

### 🧪 **Testing Your Bots**

#### **Command Line Testing**
```bash
# Test all configured bots
python main.py test-bots

# Expected output:
# 🧪 Testing bot configurations...
# ✅ Bot 1 (@bot1) - My Streaming Bot 1
# ✅ Bot 2 (@bot2) - My Streaming Bot 2  
# ✅ Bot 3 (@bot3) - My Streaming Bot 3
# 🎉 All bots are ready for round-robin uploads!
```

#### **Web Interface Testing (Recommended)**
1. **Open your browser**: `http://localhost:8080`
2. **Go to Telegram Configuration tab**: Click "📡 Telegram Configuration"
3. **Test individual bots**: Click "🧪 Test Bot" on each configured bot
4. **View results**: Real-time success/failure messages with detailed error information
5. **Fix issues**: Edit bot tokens or chat IDs directly and test again

**Web interface advantages**:
- ✅ Test individual bots separately
- ✅ Real-time results with detailed error messages
- ✅ Edit and re-test without restarting
- ✅ Visual status indicators for each bot
- ✅ Automatic .env file synchronization

---

## 🎬 How Video Processing Works

### 🧠 **Smart Segmentation Algorithm**

The system uses a sophisticated 3-phase approach:

#### **Phase 1: Find Optimal Duration** 🔍
```
Test durations: 30s → 25s → 20s → 15s → 10s → 8s → 6s → 5s → 3s → 2s

For each duration:
1. Split video using copy mode (no re-encoding)
2. Count how many segments exceed 15MB
3. Find duration with minimum oversized segments

Result: "20 seconds gives only 2 oversized segments - optimal!"
```

#### **Phase 2: Create Final Segments** ✂️
```
Use optimal duration (20s) to create final segments:
- segment_0000.ts (18.2MB) ✅
- segment_0001.ts (22.1MB) ❌ Too large
- segment_0002.ts (19.7MB) ✅  
- segment_0003.ts (21.8MB) ❌ Too large
- segment_0004.ts (16.4MB) ✅
```

#### **Phase 3: Smart Re-encoding** 🎬
```
Only re-encode oversized segments:

segment_0001.ts (22.1MB):
- Calculate target bitrate: 15MB ÷ 20s = 6 Mbps
- Re-encode with quality optimization
- Result: 14.8MB ✅

segment_0003.ts (21.8MB):
- Calculate target bitrate: 15MB ÷ 20s = 6 Mbps  
- Re-encode with quality optimization
- Result: 14.5MB ✅
```

### 📊 **Processing Statistics**

After processing, you'll see detailed statistics:

```
📊 Smart segmentation results:
  🎯 Optimal duration: 20s
  📦 Total segments: 85
  ✅ Copy mode segments: 78 (91.8%)
  🎬 Re-encoded segments: 7 (8.2%)
  💾 Total size: 1.2 GB
  📊 Average segment: 14.1 MB
  📏 Largest segment: 14.9 MB
```

### 🎭 **Subtitle Processing**

```
Subtitle extraction process:
1. Detect subtitle tracks in video
2. Extract to separate files (.srt, .vtt, .ass)
3. Upload subtitle files to Telegram
4. Create HLS-compliant subtitle references
5. Serve subtitles through streaming URLs
```

---

## 🌐 Streaming & Access

### 🏠 **Local Network Streaming**

Perfect for home use:

```
URL Format: http://192.168.1.100:8080/playlist/local/{video_id}.m3u8

Examples:
- VLC: Media → Open Network Stream
- Browser: Direct playback in video element
- Jellyfin: Add as .strm file
- Mobile: Any HLS-compatible app
```

### 🌍 **Public Internet Streaming**

For remote access:

1. **Set up port forwarding** on your router (port 8080)
2. **Configure public domain**:
   ```env
   PUBLIC_DOMAIN="yourdomain.duckdns.org"
   FORCE_HTTPS="true"  # Uses standard HTTPS port (443) in URLs
   ```
3. **Access globally**:
   ```
   https://yourdomain.duckdns.org/playlist/public/{video_id}.m3u8
   ```

**Note**: When `FORCE_HTTPS=true`, the system generates clean HTTPS URLs without port numbers, assuming you're using a reverse proxy (nginx, Cloudflare) that handles SSL termination on port 443.

### 🔒 **HTTPS/SSL Configuration**

<details>
<summary><b>Option 1: Reverse Proxy (Recommended)</b></summary>

Use nginx, Cloudflare, or similar:

```env
FORCE_HTTPS="true"  # Generates HTTPS URLs
# Server runs HTTP internally, proxy handles SSL
```

</details>

<details>
<summary><b>Option 2: Direct SSL Certificates</b></summary>

```env
SSL_CERT_PATH="/path/to/certificate.crt"
SSL_KEY_PATH="/path/to/private.key"
# Server handles SSL directly
```

</details>

### 📱 **Compatible Players**

| Player | Local Network | Internet | Subtitles | Notes |
|--------|---------------|----------|-----------|-------|
| **VLC** | ✅ | ✅ | ✅ | Perfect compatibility |
| **Browsers** | ✅ | ✅ | ✅ | Chrome, Firefox, Safari |
| **Jellyfin** | ✅ | ✅ | ✅ | Use .strm files |
| **Plex** | ✅ | ✅ | ⚠️ | May need transcoding |
| **MPV** | ✅ | ✅ | ✅ | Lightweight player |
| **Mobile Apps** | ✅ | ✅ | ✅ | VLC Mobile, others |

---

## 📄 Subtitle System

### 🎭 **Automatic Subtitle Detection**

The system automatically handles:

```
Supported subtitle formats:
- SRT (SubRip)
- VTT (WebVTT)
- ASS/SSA (Advanced SubStation)
- PGS (Presentation Graphics)
- DVD Subtitles

Languages detected:
- Language codes (eng, spa, fre, etc.)
- Forced subtitles
- Hearing impaired (SDH)
- Default track selection
```

### 🌐 **HLS-Compliant Subtitle Serving**

Subtitles are integrated into HLS playlists:

```m3u8
#EXTM3U
#EXT-X-VERSION:3

# Subtitle tracks
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="English",LANGUAGE="eng",DEFAULT=YES,URI="http://server/subtitle/video_id/eng.srt"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="Spanish",LANGUAGE="spa",DEFAULT=NO,URI="http://server/subtitle/video_id/spa.srt"

# Video segments
#EXTINF:20.000000,
http://server/segment/video_id/segment_0000.ts
```

### 📥 **Subtitle Access**

```bash
# Direct subtitle access
http://localhost:8080/subtitle/{video_id}/{language}.srt

# List available subtitles
http://localhost:8080/subtitles/{video_id}

# Examples
http://localhost:8080/subtitle/movie/eng.srt     # English subtitles
http://localhost:8080/subtitle/movie/spa.srt     # Spanish subtitles
http://localhost:8080/subtitles/movie            # JSON list of all
```

---

## 💾 Database & Cache

### 🗄️ **SQLite Database Schema**

The system uses a robust database structure:

```sql
videos table:
- video_id (primary key)
- original_filename
- total_duration, total_segments
- format_name, video_codec, audio_codec
- resolution, bitrate
- subtitle_count
- status (active/processing/error)
- created_at, updated_at

segments table:
- video_id (foreign key)
- filename, duration, file_id
- file_size, segment_order
- created_at, updated_at

subtitles table:
- video_id, track_index
- language, title, codec
- is_default, is_forced, is_hearing_impaired
- file_path

subtitle_files table:
- video_id, track_index
- filename, file_id, file_size
- language, file_type
```

### 💾 **Caching System**

<details>
<summary><b>Memory Cache (Default - Fastest)</b></summary>

```env
CACHE_TYPE="memory"
CACHE_SIZE="1073741824"  # 1GB

Advantages:
✅ Instant access (no disk I/O)
✅ Perfect for active streaming
✅ No disk space used

Disadvantages:
❌ Lost on restart
❌ Uses system RAM
```

</details>

<details>
<summary><b>Disk Cache (Persistent)</b></summary>

```env
CACHE_TYPE="disk"
CACHE_SIZE="2147483648"  # 2GB
CACHE_DIR="cache"

Advantages:
✅ Survives restarts
✅ Doesn't use RAM
✅ Larger cache possible

Disadvantages:
❌ Slower than memory
❌ Uses disk space
```

</details>

<details>
<summary><b>Advanced Cache Settings</b></summary>

```env
# Predictive caching
PRELOAD_SEGMENTS="10"              # Segments to preload ahead
MAX_CONCURRENT_PRELOADS="7"       # Parallel preload operations
ENABLE_CACHE_WARMING="true"        # Auto-cache popular content
CACHE_WARMING_SEGMENTS="12"        # Segments to warm per video

# Session management
SESSION_CLEANUP_INTERVAL="300"     # Cleanup every 5 minutes
SESSION_IDLE_TIMEOUT="600"         # 10-minute session timeout

# Performance
STREAMING_THRESHOLD_GB="3"         # Use streaming for files > 3GB
```

</details>

### 📊 **Cache Management**

```bash
# View cache statistics
curl http://localhost:8080/cache/stats

# Clear all cache
curl -X POST http://localhost:8080/cache/clear

# Clear cache for specific video
curl -X POST "http://localhost:8080/cache/clear?video_id=movie"
```

---

## 🔍 Monitoring & Debugging

### 🌐 **Integrated Web Dashboard**

The modern tabbed interface provides comprehensive management:

#### **📊 Dashboard Tab**
```
System Status:
- CPU, Memory, Disk usage with live graphs
- Python version, Platform info
- Cache statistics and utilization

Network Configuration:
- Local IP, Public domain status
- SSL/HTTPS status with protocol detection
- Telegram connectivity status

Database & Cache:
- Video count, Total segments
- Subtitle tracks, Storage used
- Cache hit rate, Real-time utilization
```

#### **📡 Telegram Configuration Tab**
```
Bot Management:
- Visual cards for all 10 bot slots
- Automatic .env file import
- Individual bot testing with live results
- Status indicators: ✅ Configured, ⚠️ Incomplete, ⭕ Empty
- Real-time configuration saving
```

#### **⚙️ Settings Tab**
```
System Configuration:
- Network settings (host, port, domain, SSL)
- Video processing parameters (threads, hardware accel)
- Cache configuration (type, size, preloading)
- Directory configuration (uploads, segments, playlists)
- Advanced cache settings (warming, session management)
- Performance settings (streaming thresholds)
- All .env variables accessible through modern UI
- All changes saved to .env automatically
```

#### **📋 Logs Tab**
```
Real-time Monitoring:
- Live processing logs with timestamps
- Color-coded log levels
- Auto-scrolling and filtering
- Download logs functionality
```

### 📋 **Available Endpoints**

| Endpoint | Purpose | Example |
|----------|---------|---------|
| `/` | Web dashboard | Main interface |
| `/playlist/local/{id}.m3u8` | Local streaming | For network access |
| `/playlist/public/{id}.m3u8` | Public streaming | For internet access |
| `/segment/{id}/{name}` | Video segments | HLS segment delivery |
| `/subtitle/{id}/{lang}` | Subtitle files | Direct subtitle access |
| `/cache/stats` | Cache statistics | Performance monitoring |
| `/debug` | Debug information | Troubleshooting |

### 🛠️ **Command Line Tools**

```bash
# Database statistics
python main.py db-stats

# List all videos with details
python main.py list

# Test bot connectivity
python main.py test-bots

# Show full configuration
python main.py config

# Clean up old cache entries
python main.py cleanup --hours 24
```

### 📝 **Logging Configuration**

```env
# Set logging level
LOG_LEVEL="INFO"  # DEBUG, INFO, WARNING, ERROR

# Optional log file
LOG_FILE="logs/streaming.log"

# Example debug mode
LOG_LEVEL="DEBUG"
```

**Log levels explained**:
- `DEBUG`: Very detailed information for troubleshooting
- `INFO`: Normal operation information (recommended)
- `WARNING`: Important issues that don't stop operation
- `ERROR`: Serious problems that may cause failures

---

## 🛠️ Advanced Configuration

### ⚡ **Hardware Acceleration**

Enable GPU encoding for faster processing:

```env
# NVIDIA GPUs (requires NVENC)
FFMPEG_HARDWARE_ACCEL="nvidia"

# Intel GPUs (requires QuickSync)
FFMPEG_HARDWARE_ACCEL="intel"

# Software encoding (default, works everywhere)
FFMPEG_HARDWARE_ACCEL=""
```

### 🎚️ **Segmentation Fine-Tuning**

```env
# Smart segmentation parameters
MIN_SEGMENT_DURATION="2"   # Minimum segment length (seconds)
MAX_SEGMENT_DURATION="30"  # Maximum to test (seconds)
MAX_CHUNK_SIZE="15728640"  # 15MB (safe for Telegram bots)

# Processing limits
MAX_UPLOAD_SIZE="53687091200"  # 50GB max file size
```

### 🌐 **Network Optimization**

```env
# Server configuration
LOCAL_HOST="0.0.0.0"     # Bind to all interfaces
LOCAL_PORT="8080"        # Choose your port

# Performance tuning
CACHE_SIZE="1073741824"  # 1GB cache for busy servers
CACHE_TYPE="memory"      # Fastest option
```

### 🔐 **Security Configuration**

```env
# HTTPS enforcement
FORCE_HTTPS="true"

# SSL certificates (if not using reverse proxy)
SSL_CERT_PATH="/etc/ssl/certs/your-cert.pem"
SSL_KEY_PATH="/etc/ssl/private/your-key.pem"

# Optional: Restrict access
# (Configure firewall rules separately)
```

---

## ❓ Troubleshooting

### 🚨 **Common Issues & Solutions**

<details>
<summary><b>❌ "Telegram Bot Error: Unauthorized"</b></summary>

**Problem**: Bot token is invalid or bot isn't added to channel

**Solutions**:
1. Double-check your `BOT_TOKEN` in `.env`
2. Ensure bot is added as admin to your channel
3. Test with: `python main.py test-bots`

</details>

<details>
<summary><b>❌ "Segments too large for Telegram"</b></summary>

**Problem**: Video bitrate too high for size limits

**Solutions**:
1. Reduce `MAX_CHUNK_SIZE` to `10485760` (10MB)
2. Lower `MIN_SEGMENT_DURATION` to `1`
3. The system will automatically re-encode more segments

```env
MAX_CHUNK_SIZE="10485760"
MIN_SEGMENT_DURATION="1"
```

</details>

<details>
<summary><b>❌ "FFmpeg not found"</b></summary>

**Problem**: FFmpeg not installed or not in PATH

**Solutions**:
1. **Windows**: Download from https://ffmpeg.org and add to PATH
2. **macOS**: `brew install ffmpeg`
3. **Linux**: `sudo apt install ffmpeg`
4. Test with: `ffmpeg -version`

</details>

<details>
<summary><b>❌ "Can't access from other devices"</b></summary>

**Problem**: Server only listening on localhost

**Solutions**:
1. Set `LOCAL_HOST="0.0.0.0"` in `.env`
2. Check firewall allows port 8080
3. Use your actual IP: `http://192.168.1.100:8080`

</details>

<details>
<summary><b>❌ "Rate limited / Upload too slow"</b></summary>

**Problem**: Single bot hitting rate limits

**Solutions**:
1. Add more bots for round-robin uploads
2. Check your internet upload speed
3. Verify bots are in different channels (optional)

</details>

<details>
<summary><b>❌ "Video won't play in browser"</b></summary>

**Problem**: HTTPS required for browser playback

**Solutions**:
1. Set up HTTPS with reverse proxy
2. Use VLC for testing (works with HTTP)
3. Enable `FORCE_HTTPS="true"` if using proxy

</details>

### 🔧 **Diagnostic Commands**

```bash
# Test system configuration
python main.py config

# Test all bots
python main.py test-bots

# Check database
python main.py db-stats

# View detailed logs
LOG_LEVEL="DEBUG" python main.py serve

# Test video processing
python main.py upload --video small_test.mp4
```

### 📊 **Performance Troubleshooting**

| Issue | Symptoms | Solution |
|-------|----------|----------|
| **Slow uploads** | Single-threaded upload | Add more bots |
| **High memory usage** | System RAM at 90%+ | Use disk cache |
| **Slow streaming** | Buffering, delays | Increase cache size |
| **Large segments** | Upload failures | Lower max chunk size |

---

## 🏆 Best Practices

### 🚀 **Optimal Setup Recommendations**

```env
# Recommended configuration for best performance
CACHE_TYPE="memory"
CACHE_SIZE="1073741824"  # 1GB
MIN_SEGMENT_DURATION="3"
MAX_SEGMENT_DURATION="20"
MAX_CHUNK_SIZE="15728640"  # 15MB (safe)

# Multi-bot setup (3-5 bots ideal)
BOT_TOKEN="your_primary_bot"
BOT_TOKEN_2="your_second_bot"
BOT_TOKEN_3="your_third_bot"
```

### 📁 **File Organization**

```
your-project/
├── .env                 # Your configuration
├── video_streaming.db   # Database file
├── temp_uploads/        # Temporary files
├── segments/           # Processing workspace
├── playlists/          # Generated playlists
│   ├── local/          # Local network access
│   └── public/         # Internet access
├── cache/              # Disk cache (if enabled)
└── logs/               # Log files (if enabled)
```

### 🎯 **Usage Recommendations**

1. **Start with single bot**, verify everything works
2. **Add more bots** for speed once stable
3. **Use memory cache** for active streaming
4. **Monitor disk space** if using disk cache
5. **Set up HTTPS** for browser compatibility
6. **Regular cleanup** of old segments

### 🔄 **Maintenance Tasks**

```bash
# Weekly: Check database statistics
python main.py db-stats

# Monthly: Clean up old cache
python main.py cleanup --hours 720  # 30 days

# As needed: Clear all cache
curl -X POST http://localhost:8080/cache/clear

# As needed: Test bots
python main.py test-bots
```

### 📈 **Scaling Guidelines**

| Usage Level | Bots | Cache | Hardware |
|-------------|------|-------|----------|
| **Personal** | 1-2 | 500MB memory | 2GB RAM |
| **Family** | 2-3 | 1GB memory | 4GB RAM |
| **Small Group** | 3-5 | 2GB disk | 8GB RAM |
| **Heavy Usage** | 5-10 | 5GB disk | 16GB RAM |

---

## 📚 Additional Resources

### 🔗 **Useful Links**
- [Telegram Bot API Documentation](https://core.telegram.org/bots/api)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)
- [HLS Specification](https://tools.ietf.org/html/rfc8216)
- [Jellyfin Documentation](https://jellyfin.org/docs/)

### 🆘 **Getting Help**
- **GitHub Issues**: Report bugs and request features
- **Discussions**: Community support and questions
- **Wiki**: Extended documentation and examples

### 🤝 **Contributing**
We welcome contributions! Please see:
- `CONTRIBUTING.md` for development guidelines
- `CODE_OF_CONDUCT.md` for community standards
- Open issues for known bugs and requested features

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## ⭐ Star History

If this project helped you, please consider giving it a star! ⭐

**Built with ❤️ for the community** - Transform your Telegram into a powerful streaming platform!
