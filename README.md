## 📺 Telegram Video Streaming System - Complete Documentation

### 🎯 **Project Overview**

This is a sophisticated Python application that transforms Telegram into a **distributed video storage and streaming platform**. It solves the problem of storing and streaming large video files by leveraging Telegram's generous file storage limits. The system works by splitting a large video file into smaller, manageable segments, uploading them to a designated Telegram channel, and then serving them on demand through a built-in HLS (HTTP Live Streaming) server.

The application features a modern web interface for easy drag-and-drop video uploads and a real-time progress log. It is also configurable through a simple `.env` file, making it secure and easy to set up for both local network streaming and public access via a custom domain.

### 🏗️ **System Architecture**

The application is built with a modular, asynchronous design to handle concurrent processing and streaming efficiently.

```
┌──────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ Web UI or CLI    ├─►  │ FFmpeg Splitter │───►│ Telegram Upload │
│ (User Input)     │    │   (HLS Segments)  │    │  (Bot API)      │
└──────────────────┘    └─────────────────┘    └─────────────────┘
                                                        │
                                                        ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Media Players   │◄───│ HTTP Streamer    │◄───│   SQLite DB     │
│ (Jellyfin/VLC)  │    │ (aiohttp Server) │    │  (Metadata)     │
└──────────────────┘    └──────────────────┘    └─────────────────┘
      ▲                                                  ▲
      │                                                  │
┌─────┴──────────┐                           ┌─────────────────┐
│ .env Config    │                           │ Telegram Storage│
│ (Tokens/Domain)│                           │   (File IDs)    │
└────────────────┘                           └─────────────────┘
```

#### **File Structure**

The project is organized into logical modules for maintainability and clarity.

```
your-project-folder/
├── main.py              # Main entry point, handles CLI and starts the server
├── stream_server.py     # The aiohttp web server, web UI, and API endpoints
├── video_processor.py   # Handles video splitting with FFmpeg
├── telegram_handler.py  # Manages all communication with the Telegram API
├── database.py          # Manages the SQLite database for metadata
├── utils.py             # Utility functions, like IP detection
├── logger_config.py     # Centralized logging configuration
├── templates/
│   └── index.html       # The HTML, CSS, and JS for the web frontend
└── .env                 # Configuration file for secrets and settings
```

-----

### 🔧 **Core Components & Code Explanation**

  * **`main.py`**: This is the application's entry point. It uses `argparse` to handle command-line arguments and `dotenv` to load configurations from the `.env` file. Its primary role is to initialize all other components and start the `StreamServer`.

  * **`stream_server.py`**: The heart of the application. It uses `aiohttp` to create a powerful asynchronous web server. It serves the `index.html` frontend, provides API endpoints (`/process`, `/status/{task_id}`) to handle uploads and report progress, and serves the final HLS playlists (`/playlist/...`) and video segments (`/segment/...`). It's responsible for managing the entire web-based workflow.

  * **`video_processor.py`**: A dedicated module that interfaces with **FFmpeg**. Its `split_video_to_hls` function takes a video file and intelligently splits it into HLS-compatible `.ts` segments, preparing them for upload.

  * **`telegram_handler.py`**: This module encapsulates all logic for interacting with the Telegram Bot API using the `python-telegram-bot` library. It handles the reliable upload of video segments and the on-demand downloading of those segments for streaming.

  * **`database.py`**: Manages all interactions with the **SQLite database** using `aiosqlite`. It creates the necessary tables (`videos`, `segments`) and provides asynchronous methods to add, retrieve, and manage the metadata associated with each video and its corresponding segments.

  * **`utils.py`**: Contains helper functions, most notably `get_local_ip()`, which automatically detects the machine's local IP address to simplify setup for local network streaming.

  * **`templates/index.html`**: A single-file, modern web interface. It contains the HTML structure, CSS for styling, and JavaScript to handle file uploads, form submissions, and real-time log updates using Server-Sent Events (SSE). It provides a user-friendly alternative to the command line.

-----

### 🚀 **Installation & Setup**

#### **1. Prerequisites**

  * **Python 3.8+**
  * **FFmpeg**: Must be installed on your system and accessible in your system's PATH.

#### **2. Install Python Dependencies**

Create a virtual environment and install the required packages.

```bash
# Create and activate a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows, use `venv\Scripts\activate`

# Install dependencies
pip install python-telegram-bot aiohttp aiofiles aiosqlite aiohttp-jinja2 python-dotenv
```

#### **3. Configure Your `.env` File**

Create a file named `.env` in your project root.

```
# Your Telegram Bot Token from @BotFather
BOT_TOKEN="12345:ABCDEF..."

# The username of your public Telegram channel (e.g., @my_stream_channel)
CHAT_ID="@your_channel_id"

# Optional: For public-facing links, uncomment and set your domain
# PUBLIC_DOMAIN="telegram.yourdomain.com"
```

-----

### 💻 **Usage Guide**

The primary way to use the application is through the web interface.

#### **Start the Streaming Server**

Run the following command in your terminal. It requires no arguments as all configuration is loaded from the `.env` file.

```bash
python main.py serve
```

By default, the server starts on port `8080`. You can change this with the `--port` flag:

```bash
python main.py serve --port 5050
```

#### **Use the Web Interface**

1.  Open your browser and navigate to `http://127.0.0.1:8080` (or your PC's local IP address, e.g., `http://192.168.0.199:8080`).
2.  Drag and drop a video file onto the page or click to select a file.
3.  Click "Upload and Process."
4.  Watch the real-time log for progress.
5.  Once complete, a link to the HLS playlist will appear. This is your streaming URL.

#### **Jellyfin / Plex Integration**

1.  In your Jellyfin/Plex media library, create a new file with the `.strm` extension (e.g., `My Movie.strm`).
2.  Open the file and paste the streaming URL generated by the application into it (e.g., `http://192.168.0.199:8080/playlist/your_video_id.m3u8`).
3.  Refresh your media library. The video will now appear and stream directly from your Telegram-backed server.

-----

### 🛠️ **Technical Implementation Details**

  * **Asynchronous Processing**: The entire workflow is asynchronous. When a video is uploaded via the web UI, the server immediately responds and starts a background task. This prevents the UI from freezing and allows for real-time progress updates using Server-Sent Events (SSE).
  * **HLS Streaming**: The application uses **HTTP Live Streaming (HLS)**, an adaptive bitrate streaming protocol. It generates a `.m3u8` playlist that media players use to request video segments sequentially, enabling smooth playback and seeking even for very large files.
  * **Database Schema**: The SQLite database uses two main tables:
      * `videos`: Stores high-level information about each uploaded video, like its original filename and total duration.
      * `segments`: Stores metadata for every individual video chunk, including its duration, order, and the crucial Telegram `file_id` needed for downloading. A `FOREIGN KEY` links each segment back to its parent video, ensuring data integrity.
