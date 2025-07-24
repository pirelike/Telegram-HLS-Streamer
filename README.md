# Project Overview: Telegram HLS Streamer
This document serves as a complete reference and explanation for the "Telegram HLS Streamer" web application.

# Purpose
The goal of this project is to provide a user-friendly web interface that takes a large video file (like a movie or TV show episode), automatically splits it into smaller, streamable chunks, and uploads those chunks to a Telegram channel. It then generates a standard M3U8 playlist file. When this playlist is opened in a compatible video player (like VLC), it uses a built-in proxy to stream the video seamlessly, using Telegram as a free and unlimited file host.

# Core Architecture
The application is built on four key components working together:

1. FastAPI Web Application (Your Python Code): This is the heart of the project. It's a single Python application that does everything:
   - Serves the HTML web page to your browser.
   - Accepts the video file upload and user credentials (Bot Token, Channel ID).
   - Uses FFmpeg to split the video into .ts chunks.
   - Communicates with your local Telegram Bot API server to upload the chunks.
   - Provides real-time logging to the browser during the process.
   - Acts as a Proxy Server to fetch the video chunks from Telegram when a video player requests them.
2. Local Telegram Bot API Server (Docker): This is a separate application you run using Docker. Its sole purpose is to bypass Telegram's default file size limits. It allows your bot to upload and download files of virtually any size, which is critical for this project.
3. FFmpeg (System Command): A powerful, open-source command-line tool for handling multimedia files. Our Python script calls FFmpeg to perform the heavy lifting of splitting the large video file into smaller HLS (HTTP Live Streaming) segments.
4. Your Web Browser: The browser is the user interface. You use it to upload the video and monitor the process. The video player (like VLC) then acts as the final client, requesting the video stream from your FastAPI application.

## Project Setup & Codebase

Here is the complete file structure and the code for each file.

    telegram_hls_streamer/
    │
    ├── .venv/                      # Python virtual environment
    │
    ├── app/                        # Main application package
    │   ├── __init__.py             # Makes 'app' a Python package
    │   ├── main.py                 # Main FastAPI application logic
    │   └── templates/
    │       └── index.html          # The HTML frontend
    │
    ├── outputs/                    # Generated M3U8 playlists and .ts segments
    │
    ├── temp_uploads/               # Temporary storage for uploaded videos
    │
    ├── .env                        # Your private configuration file
    ├── requirements.txt            # Python dependencies
    └── run.py                      # Script to run the server

## 1. Configuration (.env)

    This file holds your secret credentials. It should be in the project's root directory.# This file is for your secret credentials.
    # Keep it private and do not share it.
    # --- Telegram App Credentials ---
    # These are required by the Local Bot API Server (the Docker container).
    # Get these from my.telegram.org.
    TELEGRAM_API_ID=""
    TELEGRAM_API_HASH=""
    
    # --- Telegram Bot Token ---
    # This token is used by both the Docker container and the Python app's proxy.
    # Replace with your bot token from @BotFather.
    PROXY_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"

- The TELEGRAM_API_ID and TELEGRAM_API_HASH are used by your Docker container to authenticate with Telegram's core systems.
- The PROXY_BOT_TOKEN is used by your Python application to verify that the stream links are valid.

## 2. Dependencies (requirements.txt)
This file lists all the Python libraries the project needs.
   
    fastapi
    uvicorn[standard]
    python-telegram-bot[ext]
    aiohttp
    python-multipart
    aiofiles
    python-dotenv
    jinja2

## 3. Server Runner (run.py)
A simple script to start the web server.import uvicorn

if __name__ == "__main__":
    
    This is the main entry point to run the FastAPI web application.
    
    It uses uvicorn, a lightning-fast ASGI server, to run the app.
    
    - "app.main:app": Tells uvicorn where to find the FastAPI app instance.
      It means: look in the 'app' package, inside the 'main.py' file, for a variable named 'app'.
    - host="0.0.0.0": Makes the server accessible on your local network, not just on your machine.
    - port=8000: The port the server will listen on. Access it at http://127.0.0.1:8000.
    - reload=True: Enables auto-reload. The server will restart automatically when you save changes
      to the code, which is very convenient for development.
    """
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

## 4. Package Initializer (app/__init__.py)An empty file that tells Python to treat the app directory as a package. 
   This file can be empty.
   Its presence tells Python that the 'app' directory is a package,
   which allows for relative imports within the application.
## 5. Web App Backend (app/main.py)This is the main application file containing all the server-side logic.import os
      
      import os
      import subprocess
      import math
      import asyncio
      import uuid
      import logging
      import sqlite3
      import socket
      from pathlib import Path
      from typing import AsyncGenerator
      from io import BytesIO
      from dotenv import load_dotenv
      
      from fastapi import FastAPI, Request, Form, File, UploadFile, BackgroundTasks, HTTPException
      from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
      from fastapi.templating import Jinja2Templates
      from fastapi.staticfiles import StaticFiles
      from fastapi.exceptions import RequestValidationError
      from fastapi.middleware.cors import CORSMiddleware
      from starlette.exceptions import HTTPException as StarletteHTTPException
      
      import aiofiles
      from telegram.ext import Application
      from aiohttp import ClientSession
      
      # --- Configuration & Setup ---
      
      # Load environment variables from .env file in the project root
      load_dotenv()
      
      # Get the proxy token from the environment. Used for UI warnings.
      PROXY_BOT_TOKEN = os.getenv("PROXY_BOT_TOKEN")
      
      # Public URL for external access - change this to your domain/public IP
      PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
      
      # Define paths relative to this file's location (app/main.py)
      BASE_DIR = Path(__file__).parent
      PROJECT_ROOT = BASE_DIR.parent
      TEMP_UPLOADS_DIR = PROJECT_ROOT / "temp_uploads"
      OUTPUTS_DIR = PROJECT_ROOT / "outputs"
      LOG_FILE = PROJECT_ROOT / "app.log"
      DB_FILE = PROJECT_ROOT / "file_mappings.db"
      
      # --- Enhanced Logging Setup ---
      logging.basicConfig(
          level=logging.INFO,
          format="%(asctime)s - %(levelname)s - %(message)s",
          handlers=[
              logging.FileHandler(LOG_FILE),
              logging.StreamHandler()
          ]
      )
      # --- End of Logging Setup ---
      
      # URL of your local Bot API server (from your Docker setup).
      LOCAL_API_BASE_URL = "http://127.0.0.1:8081"
      # Desired chunk size in Megabytes (MB) for video segments.
      CHUNK_SIZE_MB = 14
      # Set maximum file size (e.g., 10GB)
      MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024  # 10GB in bytes
      
      # --- Database Functions ---
      
      def init_db():
          """Initialize the database for storing file mappings."""
          conn = sqlite3.connect(DB_FILE)
          cursor = conn.cursor()
          cursor.execute('''
              CREATE TABLE IF NOT EXISTS file_mappings (
                  file_unique_id TEXT PRIMARY KEY,
                  file_id TEXT NOT NULL,
                  filename TEXT NOT NULL,
                  bot_token TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
              )
          ''')
          conn.commit()
          conn.close()
      
      def store_file_mapping(file_unique_id: str, file_id: str, filename: str, bot_token: str):
          """Store file mapping in database with better error handling."""
          try:
              conn = sqlite3.connect(DB_FILE)
              cursor = conn.cursor()
              cursor.execute('''
                  INSERT OR REPLACE INTO file_mappings
                  (file_unique_id, file_id, filename, bot_token)
                  VALUES (?, ?, ?, ?)
              ''', (file_unique_id, file_id, filename, bot_token))
              conn.commit()
              conn.close()
              logging.info(f"Stored file mapping: {file_unique_id} -> {file_id}")
          except Exception as e:
              logging.error(f"Failed to store file mapping: {e}")
      
      def get_file_mapping(file_unique_id: str) -> tuple[str, str] | None:
          """Get file_id and bot_token from database."""
          try:
              conn = sqlite3.connect(DB_FILE)
              cursor = conn.cursor()
              cursor.execute('SELECT file_id, bot_token FROM file_mappings WHERE file_unique_id = ?', (file_unique_id,))
              result = cursor.fetchone()
              conn.close()
              if result:
                  logging.info(f"Retrieved file mapping from DB: {file_unique_id} -> {result[0]}")
                  return result
              else:
                  logging.info(f"No file mapping found in DB for: {file_unique_id}")
                  return None
          except Exception as e:
              logging.error(f"Failed to get file mapping: {e}")
              return None
      
      def get_local_ip() -> str:
          """Gets the local network IP of the machine."""
          s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
          try:
              # doesn't even have to be reachable
              s.connect(('10.255.255.255', 1))
              IP = s.getsockname()[0]
          except Exception:
              IP = '127.0.0.1' # Fallback to loopback
          finally:
              s.close()
          return IP
      
      def list_all_file_mappings():
          """Debug function: List all file mappings in database"""
          try:
              conn = sqlite3.connect(DB_FILE)
              cursor = conn.cursor()
              cursor.execute('SELECT file_unique_id, file_id, filename, created_at FROM file_mappings ORDER BY created_at DESC LIMIT 20')
              results = cursor.fetchall()
              conn.close()
      
              logging.info("Recent file mappings in database:")
              for row in results:
                  logging.info(f"  {row[0]} -> {row[1]} ({row[2]}) [{row[3]}]")
      
              return results
          except Exception as e:
              logging.error(f"Failed to list file mappings: {e}")
              return []
      
      # Initialize database on startup
      init_db()
      
      # --- Application Setup ---
      app = FastAPI()
      
      # Add CORS middleware to allow requests from any origin
      app.add_middleware(
          CORSMiddleware,
          allow_origins=["*"],
          allow_credentials=True,
          allow_methods=["*"],
          allow_headers=["*"],
      )
      
      # --- Custom Exception Handlers ---
      
      @app.exception_handler(RequestValidationError)
      async def validation_exception_handler(request: Request, exc: RequestValidationError):
          """
          Handles FastAPI's validation errors. This is triggered when incoming request
          data (body, query params, etc.) doesn't match the expected types.
          The fix is to not include exc.body, which is not JSON serializable.
          """
          error_details = exc.errors()
          logging.error(f"Validation error on {request.url}: {error_details}")
          return JSONResponse(
              status_code=422,
              content={
                  "detail": error_details,
                  "message": "Validation failed - check the field names and types"
              }
          )
      
      @app.exception_handler(StarletteHTTPException)
      async def http_exception_handler(request: Request, exc: StarletteHTTPException):
          """Handles standard HTTP exceptions (like 404 Not Found)."""
          logging.error(f"HTTP error on {request.url}: {exc.detail}")
          return JSONResponse(
              status_code=exc.status_code,
              content={"detail": exc.detail}
          )
      
      templates = Jinja2Templates(directory=BASE_DIR / "templates")
      
      # Ensure necessary directories exist.
      TEMP_UPLOADS_DIR.mkdir(exist_ok=True)
      OUTPUTS_DIR.mkdir(exist_ok=True)
      
      # Mount the outputs directory so generated files can be downloaded.
      app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")
      
      # In-memory storage for task logs to stream to the frontend.
      task_logs = {}
      
      # Store file mappings: file_unique_id -> file_id
      file_mappings = {}
      
      # --- Helper & Core Logic ---
      
      async def get_public_ip() -> str:
          """Get the public IP address of this server."""
          try:
              async with ClientSession() as session:
                  async with session.get('https://api.ipify.org?format=text') as response:
                      if response.status == 200:
                          ip = await response.text()
                          return ip.strip()
          except Exception as e:
              logging.warning(f"Could not get public IP: {e}")
          return "127.0.0.1"  # Fallback to localhost
      
      async def log_message(task_id: str, message: str, level: str = "INFO"):
          """Appends a log message for the frontend and logs it to the persistent file."""
          if task_id not in task_logs:
              task_logs[task_id] = []
      
          log_func = getattr(logging, level.lower(), logging.info)
          log_func(f"TASK {task_id}: {message}")
      
          log_entry_for_frontend = f"[{level}] {message}"
          task_logs[task_id].append(log_entry_for_frontend)
      
      def run_command(command: list[str]) -> str:
          """Executes a shell command, raising a detailed error on failure."""
          process = subprocess.run(command, capture_output=True, text=True, check=False)
          if process.returncode != 0:
              error_message = f"Command failed: {' '.join(command)}\n\n--- FFmpeg/FFprobe Error Output ---\n{process.stderr}"
              raise RuntimeError(error_message)
          return process.stdout
      
      async def upload_file_with_progress(application, chat_id: str, file_path: Path, task_id: str):
          """Upload file with chunked reading and progress tracking - Enhanced version"""
          file_size = file_path.stat().st_size
          await log_message(task_id, f"Starting upload of {file_path.name} ({file_size // (1024*1024)}MB)")
      
          try:
              # For files larger than 50MB, use a different approach
              if file_size > 50 * 1024 * 1024:  # 50MB
                  await log_message(task_id, f"Large file detected ({file_size // (1024*1024)}MB). Using optimized upload...")
      
                  # Use synchronous file reading for large files to avoid memory issues
                  with open(file_path, "rb") as f:
                      message = await application.bot.send_document(
                          chat_id=chat_id,
                          document=f,
                          filename=file_path.name,
                          read_timeout=600,  # 10 minutes for very large files
                          write_timeout=600,
                          connect_timeout=120  # 2 minutes to establish connection
                      )
              else:
                  # For smaller files, use the existing async approach
                  async with aiofiles.open(file_path, "rb") as f:
                      file_content = await f.read()
      
                  file_obj = BytesIO(file_content)
                  file_obj.name = file_path.name
      
                  message = await application.bot.send_document(
                      chat_id=chat_id,
                      document=file_obj,
                      filename=file_path.name,
                      read_timeout=300,
                      write_timeout=300,
                      connect_timeout=60
                  )
      
              # Detailed logging of the uploaded file info
              doc = message.document
              await log_message(task_id, f"Upload successful! File details:")
              await log_message(task_id, f"  - file_id: {doc.file_id}")
              await log_message(task_id, f"  - file_unique_id: {doc.file_unique_id}")
              await log_message(task_id, f"  - file_name: {doc.file_name}")
              await log_message(task_id, f"  - file_size: {doc.file_size}")
      
              return message
      
          except Exception as e:
              await log_message(task_id, f"Upload failed with error: {str(e)}", "ERROR")
              # Log additional details for debugging
              await log_message(task_id, f"Error type: {type(e).__name__}", "ERROR")
              raise
      
      async def get_file_id_from_channel(session: ClientSession, bot_token: str, file_unique_id: str) -> str | None:
          """
          Enhanced function to search for file_id in channel history with better error handling
          """
          logging.info(f"Searching channel for file_unique_id: {file_unique_id}")
      
          # Try multiple approaches to find the file
          search_methods = [
              {"offset": 0, "limit": 100},
              {"offset": -100, "limit": 100},  # Search recent messages
              {"offset": -200, "limit": 100},  # Search a bit further back
          ]
      
          for method in search_methods:
              updates_url = f"{LOCAL_API_BASE_URL}/bot{bot_token}/getUpdates"
              params = {
                  "limit": method["limit"],
                  "offset": method["offset"]
              }
      
              try:
                  # Add query parameters
                  query_string = "&".join([f"{k}={v}" for k, v in params.items()])
                  full_url = f"{updates_url}?{query_string}"
                  logging.info(f"Searching with URL: {full_url}")
      
                  async with session.get(full_url) as response:
                      if response.status != 200:
                          logging.warning(f"getUpdates failed with status {response.status}")
                          continue
      
                      response_data = await response.json()
                      updates = response_data.get("result", [])
                      logging.info(f"Got {len(updates)} updates to search through")
      
                      # Search through all updates
                      for update in updates:
                          # Check different message types
                          message_sources = [
                              update.get("channel_post"),
                              update.get("message"),
                              update.get("edited_channel_post"),
                              update.get("edited_message")
                          ]
      
                          for message in message_sources:
                              if not message:
                                  continue
      
                              # Check for document
                              if "document" in message:
                                  doc = message["document"]
                                  if doc.get("file_unique_id") == file_unique_id:
                                      found_file_id = doc.get("file_id")
                                      logging.info(f"Found matching file_id: {found_file_id}")
                                      return found_file_id
      
                              # Also check for video files (in case the file was sent as video)
                              if "video" in message:
                                  video = message["video"]
                                  if video.get("file_unique_id") == file_unique_id:
                                      found_file_id = video.get("file_id")
                                      logging.info(f"Found matching video file_id: {found_file_id}")
                                      return found_file_id
      
              except Exception as e:
                  logging.error(f"Error searching channel with method {method}: {e}")
                  continue
      
          logging.error(f"File with unique_id {file_unique_id} not found in any search method")
          return None
      
      async def process_video_task(
          task_id: str, input_file_path: Path, bot_token: str, chat_id: str
      ):
          """
          The main video processing logic, running as a background task.
          This version is optimized for LOCAL NETWORK streaming.
          """
          output_dir = OUTPUTS_DIR / input_file_path.stem
          output_dir.mkdir(exist_ok=True)
      
          # --- CHANGE: Use local IP for streaming, no public internet access needed ---
          local_ip = get_local_ip()
          stream_base_url = f"http://{local_ip}:8000"
          await log_message(task_id, f"Using LOCAL network URL for streaming: {stream_base_url}")
          # --- END OF CHANGE ---
      
          try:
              await log_message(task_id, "Initializing Telegram connection...")
              application = (
                  Application.builder()
                  .token(bot_token)
                  .base_url(f"{LOCAL_API_BASE_URL}/bot")
                  .base_file_url(f"{LOCAL_API_BASE_URL}/file/bot")
                  .read_timeout(300)
                  .write_timeout(300)
                  .connect_timeout(60)
                  .build()
              )
              await log_message(task_id, "Telegram connection established.", "SUCCESS")
      
              await log_message(task_id, f"Analyzing video file: {input_file_path.name}")
              bitrate_cmd = [
                  "ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "format=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1",
                  str(input_file_path),
              ]
              bitrate_bps = int(run_command(bitrate_cmd).strip())
              await log_message(task_id, f"Detected bitrate: {bitrate_bps} bps.")
      
              chunk_size_bits = CHUNK_SIZE_MB * 1024 * 1024 * 8
              segment_duration = math.floor(chunk_size_bits / bitrate_bps) if bitrate_bps > 0 else 300
              if segment_duration < 10: segment_duration = 10
              await log_message(task_id, f"Calculated segment duration: {segment_duration} seconds.")
      
              local_m3u8_path = output_dir / "local_playlist.m3u8"
              segment_filename = output_dir / "segment_%04d.ts"
              split_cmd = [
                  "ffmpeg", "-i", str(input_file_path), "-c", "copy", "-map", "0",
                  "-f", "segment", "-segment_time", str(segment_duration),
                  "-segment_list", str(local_m3u8_path),
                  "-segment_format", "mpegts", str(segment_filename),
              ]
              await log_message(task_id, "Starting video splitting... (This may take a while)")
              run_command(split_cmd)
              await log_message(task_id, f"Video split successfully into '{output_dir.name}'", "SUCCESS")
      
              if not chat_id.startswith('@') and not chat_id.lstrip('-').isdigit():
                  chat_id = f"@{chat_id}"
      
              await log_message(task_id, f"Uploading segments to channel: {chat_id}")
      
              try:
                  chat_info = await application.bot.get_chat(chat_id)
                  await log_message(task_id, f"Channel access confirmed: {chat_info.title}")
              except Exception as e:
                  await log_message(task_id, f"Cannot access channel {chat_id}: {e}", "ERROR")
                  raise
      
              segments = sorted(output_dir.glob("*.ts"))
              url_mapping = {}
              for i, segment_path in enumerate(segments):
                  await log_message(task_id, f"Uploading {segment_path.name} ({i+1}/{len(segments)})...")
      
                  max_retries = 3
                  for attempt in range(max_retries):
                      try:
                          await log_message(task_id, f"Upload attempt {attempt + 1}/{max_retries} for {segment_path.name}")
                          message = await upload_file_with_progress(application, chat_id, segment_path, task_id)
      
                          file_mappings[message.document.file_unique_id] = message.document.file_id
                          store_file_mapping(
                              message.document.file_unique_id,
                              message.document.file_id,
                              segment_path.name,
                              bot_token
                          )
                          break
                      except Exception as e:
                          if attempt < max_retries - 1:
                              wait_time = min((2 ** attempt) * 2, 30)
                              await log_message(task_id, f"Upload failed (attempt {attempt + 1}): {e}. Retrying in {wait_time}s...", "WARNING")
                              await asyncio.sleep(wait_time)
                          else:
                              await log_message(task_id, f"Upload failed after {max_retries} attempts: {e}", "ERROR")
                              raise
      
                  # Use the local stream_base_url to build the link
                  proxy_link = f"{stream_base_url}/stream/{bot_token}/{message.document.file_unique_id}/{segment_path.name}"
                  url_mapping[segment_path.name] = proxy_link
      
              await log_message(task_id, "All segments uploaded.", "SUCCESS")
      
              final_m3u8_path = output_dir / "stream_playlist.m3u8"
              await log_message(task_id, "Creating final streamable playlist...")
              async with aiofiles.open(local_m3u8_path, "r") as f_in, aiofiles.open(final_m3u8_path, "w") as f_out:
                  async for line in f_in:
                      line = line.strip()
                      if line.endswith(".ts"):
                          await f_out.write(url_mapping.get(os.path.basename(line), "") + "\n")
                      else:
                          await f_out.write(line + "\n")
      
              final_playlist_url = f"/outputs/{output_dir.name}/stream_playlist.m3u8"
              await log_message(task_id, f"Final playlist created!", "SUCCESS")
              await log_message(task_id, f"----> DOWNLOAD: <a href='{final_playlist_url}'>Click here to download stream_playlist.m3u8</a> <----", "RESULT")
      
          except Exception as e:
              await log_message(task_id, f"A critical error occurred: {e}", "ERROR")
          finally:
              if input_file_path.exists():
                  os.remove(input_file_path)
                  await log_message(task_id, f"Cleaned up temporary file: {input_file_path.name}")
              await log_message(task_id, "---STREAM_END---")
      
      # --- API Endpoints ---
      
      @app.get("/", response_class=HTMLResponse)
      async def read_root(request: Request):
          return templates.TemplateResponse("index.html", {"request": request, "proxy_token_configured": bool(PROXY_BOT_TOKEN)})
      
      @app.post("/process")
      async def process_video_endpoint(
          request: Request,
          background_tasks: BackgroundTasks,
          video_file: UploadFile = File(...),
          # The bot_token is no longer accepted from the form.
          chat_id: str = Form(...),
      ):
          # --- CHANGE #1: Get the bot token from the server's environment variables ---
          # This ensures the app always uses the same token as the local API server.
          bot_token = PROXY_BOT_TOKEN
          if not bot_token:
              raise HTTPException(
                  status_code=500,
                  detail="Server configuration error: PROXY_BOT_TOKEN is not set in the .env file."
              )
      
          # --- The rest of the function remains mostly the same ---
          if not video_file.filename:
              raise HTTPException(status_code=422, detail="No video file provided")
      
          content_length = request.headers.get('content-length')
          if content_length and int(content_length) > MAX_FILE_SIZE:
              raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024*1024)}GB")
      
          task_id = str(uuid.uuid4())
          task_logs[task_id] = []
          input_file_path = TEMP_UPLOADS_DIR / f"{task_id}_{video_file.filename}"
      
          try:
              async with aiofiles.open(input_file_path, "wb") as f:
                  await f.write(await video_file.read())
              logging.info(f"File uploaded successfully: {video_file.filename}")
          except Exception as e:
              raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {str(e)}")
      
          proxy_base_url = str(request.base_url).rstrip('/')
      
          # --- CHANGE #2: The 'bot_token' passed here is now the one from the server, not the form ---
          background_tasks.add_task(process_video_task, task_id, input_file_path, bot_token, chat_id)
      
          return {"success": True, "task_id": task_id}
      
      @app.get("/status/{task_id}")
      async def get_status(task_id: str):
          async def event_generator() -> AsyncGenerator[str, None]:
              last_sent_index = 0
              while True:
                  if task_id in task_logs:
                      logs = task_logs[task_id]
                      if last_sent_index < len(logs):
                          for i in range(last_sent_index, len(logs)):
                              log_entry = logs[i]
                              yield f"data: {log_entry}\n\n"
                              if "---STREAM_END---" in log_entry:
                                  task_logs.pop(task_id, None)
                                  return
                          last_sent_index = len(logs)
                  await asyncio.sleep(0.5)
          return StreamingResponse(event_generator(), media_type="text/event-stream")
      
      @app.get("/stream/{bot_token}/{file_unique_id}/{filename}")
      async def stream_handler(bot_token: str, file_unique_id: str, filename: str):
          """Enhanced stream handler with corrected session management."""
          logging.info(f"Stream request for file_unique_id: {file_unique_id}, filename: {filename}")
      
          file_id = None
          stored_bot_token = bot_token  # Default to URL bot_token
      
          # First, try to get file_id from memory
          if file_unique_id in file_mappings:
              file_id = file_mappings[file_unique_id]
              logging.info(f"Memory lookup result: {file_id}")
      
          # If not in memory, try database
          if not file_id:
              db_result = get_file_mapping(file_unique_id)
              if db_result:
                  file_id, stored_bot_token = db_result
                  file_mappings[file_unique_id] = file_id # Store in memory for faster access
                  logging.info(f"Database lookup result: {file_id}")
      
          actual_bot_token = stored_bot_token
      
          if not file_id:
              logging.error(f"Could not find file_id for unique_id: {file_unique_id}")
              raise HTTPException(status_code=404, detail=f"File not found for unique_id: {file_unique_id}")
      
          # We must use a new session to get the file info, as the main session will be in the generator
          file_path = None
          async with ClientSession() as session:
              get_file_url = f"{LOCAL_API_BASE_URL}/bot{actual_bot_token}/getFile?file_id={file_id}"
              logging.info(f"Requesting file info from: {get_file_url}")
      
              async with session.get(get_file_url) as response:
                  if response.status != 200:
                      error_text = await response.text()
                      logging.error(f"Failed to get file info: {error_text}")
                      raise HTTPException(status_code=502, detail=f"Failed to get file info from Telegram: {error_text}")
      
                  file_info = await response.json()
                  file_path = file_info.get("result", {}).get("file_path")
      
          if not file_path:
              logging.error(f"No file_path in response: {file_info}")
              raise HTTPException(status_code=404, detail="File path not found in Telegram response.")
      
          # Construct download URL
          download_url = f"{LOCAL_API_BASE_URL}/file/bot{actual_bot_token}/{file_path}"
          logging.info(f"Ready to stream from: {download_url}")
      
          # Stream the file
          async def file_streamer():
              # FIX: The ClientSession must be created *inside* the generator
              # so it stays alive for the duration of the download.
              async with ClientSession() as session:
                  try:
                      async with session.get(download_url) as resp:
                          if resp.status != 200:
                              logging.error(f"Error downloading file: Status {resp.status}, URL: {download_url}")
                              error_text = await resp.text()
                              logging.error(f"Download error details: {error_text}")
                              return # Stop the generator
      
                          # Stream the file in chunks
                          async for chunk in resp.content.iter_chunked(1024 * 128):  # 128KB chunks
                              yield chunk
      
                  except Exception as e:
                      # This will catch connection errors etc. during the stream
                      logging.error(f"Exception while streaming file: {str(e)}")
      
          return StreamingResponse(
              file_streamer(),
              media_type="video/MP2T",
              headers={
                  "Accept-Ranges": "bytes",
                  "Cache-Control": "no-cache"
              }
          )


## 6. Web App Frontend (app/templates/index.html)This is the HTML file that creates the user interface in the browser.<!DOCTYPE html>

    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Telegram HLS Streamer</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --primary: #6366f1; --primary-dark: #4f46e5; --primary-light: #8b5cf6;
                --secondary: #10b981; --danger: #ef4444; --warning: #f59e0b;
                --dark: #0f172a; --dark-light: #1e293b; --light: #f8fafc;
                --border: #e2e8f0; --text-dark: #0f172a; --text-light: #64748b;
                --text-lighter: #94a3b8; --shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1);
                --shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -4px rgba(0,0,0,0.1);
                --radius: 12px; --radius-lg: 16px;
            }
            [data-theme="dark"] {
                --light: #0f172a; --border: #334155; --text-dark: #f8fafc;
                --text-light: #cbd5e1; --text-lighter: #94a3b8;
            }
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: var(--text-dark); line-height: 1.6; min-height: 100vh;
                padding: 2rem; display: flex; justify-content: center; align-items: flex-start;
            }
            .container { max-width: 800px; width: 100%; }
            .main-card {
                background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(30px) saturate(180%);
                border-radius: var(--radius-lg); padding: 2rem 3rem; box-shadow: var(--shadow-lg);
            }
            [data-theme="dark"] .main-card { background: rgba(15, 23, 42, 0.95); }
            .header { text-align: center; margin-bottom: 3rem; }
            .header h1 { font-size: 2.25rem; font-weight: 700; color: var(--text-dark); margin-bottom: 0.5rem; }
            .header p { color: var(--text-light); font-size: 1.1rem; }
            #main-fieldset { border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 2.5rem; margin-bottom: 2rem; }
            .form-group { margin-bottom: 2rem; }
            .form-group label { display: flex; align-items: center; font-weight: 600; color: var(--text-dark); margin-bottom: 0.75rem; font-size: 0.95rem; }
            .step-number {
                background: linear-gradient(135deg, var(--primary), var(--primary-light));
                color: white; border-radius: 50%; width: 24px; height: 24px;
                display: inline-flex; align-items: center; justify-content: center;
                font-size: 0.8rem; font-weight: 700; margin-right: 0.75rem; flex-shrink: 0;
            }
            .input-field, .file-input-label {
                width: 100%; padding: 1rem 1.25rem; border: 2px solid var(--border);
                border-radius: var(--radius); font-size: 1rem; transition: all 0.3s ease;
                background: var(--light); color: var(--text-dark); font-family: inherit;
            }
            .input-field:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1); }
            .file-input-label { cursor: pointer; display: block; text-align: center; color: var(--text-light); }
            input[type="file"] { display: none; }
            .helper-text { color: var(--text-light); font-size: 0.9rem; margin-top: 0.75rem; }
            .helper-text code { background: var(--dark-light); color: var(--text-lighter); padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }
            .submit-btn {
                width: 100%; padding: 1.1rem 2rem; background: linear-gradient(135deg, var(--primary), var(--primary-light));
                color: white; border: none; border-radius: var(--radius); font-size: 1.1rem;
                font-weight: 600; cursor: pointer; transition: all 0.3s ease; margin-top: 1rem;
            }
            .submit-btn:disabled { opacity: 0.6; cursor: not-allowed; }
            .submit-btn .btn-content { display: flex; align-items: center; justify-content: center; }
            .loading-spinner {
                width: 20px; height: 20px; border: 2px solid rgba(255, 255, 255, 0.3);
                border-radius: 50%; border-top-color: white; animation: spin 1s linear infinite; margin-right: 0.75rem;
            }
            @keyframes spin { to { transform: rotate(360deg); } }
            .log-section { margin-top: 3rem; padding-top: 2rem; border-top: 1px solid var(--border); }
            .log-section h3 { color: var(--text-dark); margin-bottom: 1rem; font-weight: 600; }
            .log-output {
                background: var(--dark-light); color: #e2e8f0; border-radius: var(--radius);
                padding: 1.5rem; height: 400px; overflow-y: auto; font-family: 'SF Mono', 'Fira Code', 'Menlo', monospace;
                font-size: 0.9rem; line-height: 1.5; white-space: pre-wrap; border: 1px solid var(--border);
            }
            .log-output span.log-INFO { color: #cbd5e1; }
            .log-output span.log-SUCCESS { color: #34d399; font-weight: bold; }
            .log-output span.log-ERROR { color: #f87171; font-weight: bold; }
            .log-output span.log-RESULT a { color: #60a5fa; text-decoration: underline; font-weight: bold; }
            .hidden { display: none !important; }
            .alert { padding: 1rem 1.5rem; margin-bottom: 2rem; border-radius: var(--radius); border: 1px solid transparent; }
            .alert-danger { background-color: rgba(239, 68, 68, 0.1); border-color: rgba(239, 68, 68, 0.3); color: #f87171; }
            @media (max-width: 768px) { .main-card { padding: 1.5rem; } }
        </style>
    </head>
    <body data-theme="dark">
    
        <div class="container">
            <div class="main-card">
                <header class="header">
                    <h1>Telegram HLS Streamer</h1>
                    <p>Split, upload, and stream large videos using Telegram</p>
                </header>
    
                {% if not proxy_token_configured %}
                <div class="alert alert-danger">
                    <strong>Configuration Error:</strong> The <code>PROXY_BOT_TOKEN</code> is not set on the server. The final stream links will not work. Please create a <code>.env</code> file in the project root with your token.
                </div>
                {% endif %}
    
                <form id="process-form">
                    <fieldset id="main-fieldset">
                        <div class="form-group">
                            <label for="bot_token"><span class="step-number">1</span> Bot Token</label>
                            <input type="password" id="bot_token" name="bot_token" class="input-field" placeholder="Enter your Telegram Bot Token" required>
                            <div class="helper-text">This token will be used to upload the video segments.</div>
                        </div>
    
                        <div class="form-group">
                            <label for="chat_id"><span class="step-number">2</span> Public Channel Username</label>
                            <input type="text" id="chat_id" name="chat_id" class="input-field" placeholder="@your_public_channel" required>
                            <div class="helper-text">Your bot must be an administrator in this channel.</div>
                        </div>
    
                        <div class="form-group">
                            <label for="video_file"><span class="step-number">3</span> Select Video File</label>
                            <input type="file" id="video_file" name="video_file" accept="video/mp4,video/x-matroska,video/*" required>
                            <label for="video_file" class="file-input-label" id="file-label-text">Click to choose a video file</label>
                        </div>
                    </fieldset>
    
                    <button type="submit" id="submit-btn" class="submit-btn">
                        <span class="btn-content" id="btn-content">Start Processing</span>
                    </button>
                </form>
    
                <div id="log-container" class="log-section hidden">
                    <h3>📋 Processing Log</h3>
                    <div id="log-output" class="log-output"></div>
                </div>
            </div>
        </div>
    
        <script>
            const form = document.getElementById('process-form');
            const mainFieldset = document.getElementById('main-fieldset');
            const submitBtn = document.getElementById('submit-btn');
            const btnContent = document.getElementById('btn-content');
            const logContainer = document.getElementById('log-container');
            const logOutput = document.getElementById('log-output');
            const fileInput = document.getElementById('video_file');
            const fileLabelText = document.getElementById('file-label-text');
    
            fileInput.addEventListener('change', () => {
                fileLabelText.textContent = fileInput.files.length > 0 ? `Selected: ${fileInput.files[0].name}` : 'Click to choose a video file';
            });
    
            form.addEventListener('submit', async function(event) {
                event.preventDefault();
                mainFieldset.disabled = true;
                submitBtn.disabled = true;
                btnContent.innerHTML = '<span class="loading-spinner"></span><span>Uploading...</span>';
                logOutput.innerHTML = '';
                logContainer.classList.remove('hidden');
                logContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    
                // --- FIXED: Manually build FormData ---
                const formData = new FormData();
                formData.append('bot_token', document.getElementById('bot_token').value);
                formData.append('chat_id', document.getElementById('chat_id').value);
    
                if (fileInput.files.length > 0) {
                    formData.append('video_file', fileInput.files[0]);
                } else {
                    appendLog('No video file selected.', 'ERROR');
                    resetUi();
                    return;
                }
                // --- End of Fix ---
    
                let taskId = null;
    
                try {
                    const response = await fetch('/process', { method: 'POST', body: formData });
    
                    if (!response.ok) {
                        const errorData = await response.json().catch(() => null);
                        let detail = response.statusText;
                        if (errorData && errorData.detail) {
                            if (Array.isArray(errorData.detail)) {
                                detail = errorData.detail.map(e => `${e.loc.join(' -> ')}: ${e.msg}`).join('; ');
                            } else {
                                detail = JSON.stringify(errorData.detail);
                            }
                        }
                        throw new Error(`Server error: ${detail}`);
                    }
    
                    const result = await response.json();
                    if (!result.success) throw new Error('Failed to start processing task.');
    
                    taskId = result.task_id;
                    appendLog('File upload complete. Starting process...', 'SUCCESS');
                    btnContent.innerHTML = '<span class="loading-spinner"></span><span>Processing...</span>';
    
                } catch (error) {
                    appendLog(`Upload failed: ${error.message}`, 'ERROR');
                    resetUi();
                    return;
                }
    
                if (!taskId) return;
    
                const evtSource = new EventSource(`/status/${taskId}`);
                evtSource.onmessage = (event) => {
                    if (event.data.includes("---STREAM_END---")) {
                        appendLog('Process finished!', 'SUCCESS');
                        evtSource.close();
                        resetUi();
                        return;
                    }
                    const match = event.data.match(/^\[(INFO|SUCCESS|ERROR|RESULT|WARNING)\] (.*)$/s);
                    if (match) {
                        const level = match[1];
                        const message = match[2];
                        appendLog(message, level);
                    } else {
                        appendLog(event.data);
                    }
                };
                evtSource.onerror = () => {
                    appendLog('Connection to server lost.', 'ERROR');
                    evtSource.close();
                    resetUi();
                };
            });
    
            function appendLog(message, level = 'INFO') {
                const span = document.createElement('span');
                span.className = `log-${level}`;
    
                const safeMessage = message.replace(/</g, "&lt;").replace(/>/g, "&gt;");
                const linkedMessage = safeMessage.replace(
                    /&lt;a href='(.*?)'&gt;(.*?)&lt;\/a&gt;/,
                    '<a href="$1" target="_blank">$2</a>'
                );
                span.innerHTML = linkedMessage;
    
                logOutput.appendChild(span);
                logOutput.appendChild(document.createTextNode('\n'));
                logOutput.scrollTop = logOutput.scrollHeight;
            }
    
            function resetUi() {
                mainFieldset.disabled = false;
                submitBtn.disabled = false;
                btnContent.textContent = 'Start Processing';
            }
        </script>
    </body>
    </html>
