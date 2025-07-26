# 📺 Telegram Video Streaming System - Complete Documentation

## 🎯 **Project Overview**

This is a sophisticated Python application that transforms Telegram into a **distributed video storage and streaming platform**. It solves the problem of storing and streaming large video files by leveraging Telegram's generous file storage limits and creating an on-demand HLS (HTTP Live Streaming) server.

## 🏗️ **System Architecture**

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Video File    │───►│  FFmpeg Splitter │───►│ Telegram Upload │
│   (Any Format)  │    │    (HLS/m3u8)    │    │   (20MB chunks) │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
                                                         ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Media Players   │◄───│ HTTP Streaming   │◄───│   SQLite DB     │
│ (Jellyfin/VLC)  │    │    Server        │    │  (Metadata)     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         ▲
                                                         │
                                               ┌─────────────────┐
                                               │ Telegram Storage│
                                               │   (File IDs)    │
                                               └─────────────────┘
```

## 🔧 **Core Components**

### **1. Video Processing Engine**
- **FFmpeg Integration**: Automatically splits videos into HLS-compatible `.ts` segments
- **Intelligent Sizing**: Calculates optimal segment duration to stay under 20MB limits
- **Format Optimization**: Converts to H.264/AAC for maximum compatibility

### **2. Telegram Storage Layer**
- **Distributed Upload**: Each video segment uploaded as separate Telegram document
- **Metadata Tracking**: Stores file IDs, durations, and sizes in SQLite database
- **Error Recovery**: Retry logic and partial upload handling

### **3. On-Demand Streaming Server**
- **HLS Playlist Generation**: Creates `.m3u8` playlists with network-accessible URLs
- **Smart Caching**: Pre-fetches upcoming segments for smooth playback
- **CORS Support**: Compatible with web-based media players

### **4. SQLite Database Backend**
- **Production-Grade Storage**: Robust metadata management with ACID compliance
- **Three-Table Schema**: videos, segments, cache_metadata with proper relationships
- **Performance Optimization**: Indexed queries and async operations
- **Data Integrity**: Foreign key constraints and transaction safety

## 📋 **Key Features**

### **✨ Advanced Capabilities**
- **Network Streaming**: Serves videos to any device on your network
- **Jellyfin Integration**: Direct compatibility with Jellyfin media server
- **Bandwidth Optimization**: Only downloads segments as needed
- **Multi-Device Support**: Works with VLC, web browsers, mobile apps
- **Persistent Storage**: Maintains video library across restarts with SQLite

### **🛡️ Reliability Features**
- **Error Handling**: Comprehensive exception handling with retry logic
- **Host Validation**: Prevents common network configuration issues
- **Resource Limits**: Respects Telegram's API limits and file size constraints
- **Debug Endpoints**: Built-in troubleshooting and monitoring tools

### **🗄️ Database Management**
- **Video Status Tracking**: Processing, active, error states
- **Cache Analytics**: Usage statistics and performance metrics  
- **API Endpoints**: RESTful interface for video management
- **CLI Commands**: Database stats, cleanup, and maintenance tools

## 🚀 **Installation & Setup**

### **Dependencies**
```bash
pip install python-telegram-bot aiohttp aiofiles aiosqlite
# Also requires FFmpeg installed on system
```

### **Telegram Bot Setup**
1. Message `@BotFather` on Telegram
2. Create new bot: `/newbot`
3. Get bot token and chat ID
4. Add bot to your channel/group

## 💻 **Usage Guide**

### **Find Your Network IP**
```bash
python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print('Your IP:', s.getsockname()[0]); s.close()"
```

### **Upload Video**
```bash
python telegram_streamer.py upload \
  --video movie.mp4 \
  --bot-token YOUR_BOT_TOKEN \
  --chat-id YOUR_CHAT_ID \
  --host 192.168.1.100
```

### **Start Streaming Server**
```bash
python telegram_streamer.py serve \
  --bot-token YOUR_BOT_TOKEN \
  --chat-id YOUR_CHAT_ID \
  --host 0.0.0.0 \
  --port 8080
```

### **Database Management**
```bash
# Show database statistics
python telegram_streamer.py db-stats --bot-token TOKEN --chat-id CHAT

# List all videos
python telegram_streamer.py list --bot-token TOKEN --chat-id CHAT

# Delete a video
python telegram_streamer.py delete --video-id movie --bot-token TOKEN --chat-id CHAT

# Cleanup old cache entries
python telegram_streamer.py cleanup --hours 24 --bot-token TOKEN --chat-id CHAT
```

### **Jellyfin Integration**
1. Create `movie.strm` file in Jellyfin media folder
2. Add single line: `http://192.168.1.100:8080/playlist/movie.m3u8`
3. Refresh Jellyfin library

## 🔍 **Debug & Monitoring**

### **Available Endpoints**
- `http://localhost:8080/debug` - Server status and statistics
- `http://localhost:8080/debug/video_id` - Specific video information
- `http://localhost:8080/videos` - List all videos (JSON API)
- `http://localhost:8080/playlist/video_id.m3u8` - HLS playlist
- `http://localhost:8080/test-jellyfin.m3u8` - Compatibility test
- `DELETE /videos/video_id` - Remove video via API

## ⚡ **Performance Optimizations**

### **Caching Strategy**
- **Prefetch Count**: Downloads 3 segments ahead of current playback
- **Cache Size**: 100MB memory limit with automatic cleanup
- **TTL Management**: 5-minute expiration for cached segments

### **Database Efficiency**
- **Indexed Queries**: Optimized database access patterns
- **Async Operations**: Non-blocking database operations
- **Connection Pooling**: Efficient database connection management
- **VACUUM Operations**: Automatic database optimization

### **Network Efficiency**
- **Concurrent Downloads**: Multiple segments fetched simultaneously
- **Smart Sizing**: Segments optimized for 20MB target size
- **HTTP Headers**: Proper caching and CORS headers for compatibility

## 🎯 **Use Cases**

### **Home Media Server**
- Store large movie collection using Telegram's free storage
- Stream to multiple devices without local storage requirements
- Integrate with existing Jellyfin/Plex setups

### **Content Distribution**
- Share videos across multiple locations
- Backup and redundancy through Telegram's infrastructure
- Mobile-friendly streaming for remote access

### **Educational/Business**
- Distribute training videos without bandwidth costs
- Archive video content with unlimited retention
- Cross-platform compatibility for diverse user bases

## 🛠️ **Technical Implementation Details**

### **Database Structure**
```sql
-- Videos table
CREATE TABLE videos (
    video_id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    total_duration REAL NOT NULL,
    total_segments INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Segments table  
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    duration REAL NOT NULL,
    file_id TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    segment_order INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
);

-- Cache metadata table
CREATE TABLE cache_metadata (
    segment_filename TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 1,
    last_accessed TEXT NOT NULL,
    cache_size INTEGER NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
);
```

### **HLS Playlist Format**
```m3u8
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:55
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-ALLOW-CACHE:YES
#EXTINF:53.080000,
http://192.168.1.100:8080/segment/video_id/segment_0000.ts
#EXT-X-ENDLIST
```

## 🔐 **Security Considerations**
- Bot token security (use environment variables in production)
- Network access controls (firewall rules for streaming port)
- Content access validation (authentication for sensitive content)
- Database file permissions and backup strategies

## 🚧 **Future Enhancements**
- **User Authentication**: Login system for private content
- **Quality Selection**: Multiple bitrate streams for adaptive streaming
- **Web Interface**: Browser-based management dashboard
- **Load Balancing**: Multiple server instances for high availability
- **Auto-Transcoding**: Automatic video format optimization
- **Bulk Operations**: Mass upload and management features

## 📊 **Current Status**
- ✅ **SQLite Backend**: Production-ready database with full ACID compliance
- ✅ **Enhanced CLI**: Complete command-line interface with all management operations
- ✅ **RESTful API**: HTTP endpoints for programmatic access
- ✅ **Cache Management**: Intelligent caching with statistics and cleanup
- ✅ **Error Handling**: Comprehensive error recovery and logging
- ✅ **Performance Monitoring**: Built-in statistics and debugging tools

---

This system represents an innovative approach to video storage and streaming, utilizing Telegram's infrastructure as a free, reliable content delivery network while maintaining full control over access and playback through a custom streaming server with enterprise-grade SQLite backend for metadata management.
## 🚧 **Future Enhancements**
- **User Authentication**: Login system for private content
- **Quality Selection**: Multiple bitrate streams for adaptive streaming
- **Web Interface**: Browser-based management dashboard
- **Load Balancing**: Multiple server instances for high availability

---

This system represents a innovative approach to video storage and streaming, utilizing Telegram's infrastructure as a free, reliable content delivery network while maintaining full control over access and playback through a custom streaming server.
