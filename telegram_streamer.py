import os
import json
import asyncio
import aiofiles
from pathlib import Path
from typing import Dict, List, Optional
import subprocess
import math
from dataclasses import dataclass, asdict
from telegram import Bot
from telegram.error import TelegramError
import logging
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class SegmentInfo:
    """Holds information about each video segment."""
    filename: str
    duration: float
    file_id: str
    file_size: int

class TelegramVideoStreamer:
    """
    Manages splitting, uploading, and streaming video segments using Telegram as storage.
    """
    def __init__(self, bot_token: str, chat_id: str):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.db_path = "segments_db.json"
        self.segments_db = self._load_db()

        # Enhanced caching system
        self.segment_cache: Dict[str, bytes] = {}
        self.cache_timestamps: Dict[str, float] = {}
        self.prefetch_count = 3  # Increased for smoother playback
        self.cache_max_size = 100 * 1024 * 1024  # 100MB cache limit
        self.cache_ttl = 300  # 5 minutes TTL for cached segments

    def _load_db(self) -> Dict[str, Dict[str, SegmentInfo]]:
        """Loads the segment database from a JSON file."""
        if not os.path.exists(self.db_path):
            return {}
        try:
            with open(self.db_path, 'r') as f:
                data = json.load(f)
                # Convert loaded dictionaries back into SegmentInfo objects
                return {
                    video_id: {
                        name: SegmentInfo(**info) for name, info in segments.items()
                    } for video_id, segments in data.items()
                }
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Could not load segment database: {e}")
            return {}

    def _save_db(self):
        """Saves the current segment database to a JSON file."""
        try:
            # Convert SegmentInfo objects to dictionaries for JSON serialization
            serializable_db = {
                video_id: {
                    name: asdict(info) for name, info in segments.items()
                } for video_id, segments in self.segments_db.items()
            }
            with open(self.db_path, 'w') as f:
                json.dump(serializable_db, f, indent=4)
            logger.debug("Segment database saved successfully")
        except IOError as e:
            logger.error(f"Could not save segment database: {e}")

    def _validate_host_accessibility(self, host: str, port: int) -> bool:
        """Validate that the specified host:port combination is accessible."""
        import socket

        if host in ['localhost', '127.0.0.1']:
            logger.warning("⚠️  Using localhost - this will only work on the same machine!")
            logger.warning("   For Jellyfin/network access, use your network IP (e.g., 192.168.x.x)")
            return True

        try:
            # Try to bind to the specified host to check if it's valid
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, 0))  # Use port 0 to get any available port
                actual_ip = s.getsockname()[0]
                logger.info(f"✅ Host {host} is accessible (resolved to {actual_ip})")
                return True
        except socket.error as e:
            logger.error(f"❌ Cannot bind to host {host}: {e}")
            logger.error("   Make sure this is a valid IP address for your machine")
            return False

    def _cleanup_cache(self):
        """Remove expired and excess cache entries to maintain performance."""
        current_time = time.time()

        # Remove expired entries
        expired_keys = [
            key for key, timestamp in self.cache_timestamps.items()
            if current_time - timestamp > self.cache_ttl
        ]

        for key in expired_keys:
            self.segment_cache.pop(key, None)
            self.cache_timestamps.pop(key, None)
            logger.debug(f"Removed expired cache entry: {key}")

        # Remove oldest entries if cache is too large
        current_size = sum(len(data) for data in self.segment_cache.values())
        if current_size > self.cache_max_size:
            # Sort by timestamp, remove oldest first
            sorted_keys = sorted(
                self.cache_timestamps.items(),
                key=lambda x: x[1]
            )

            for key, _ in sorted_keys:
                if current_size <= self.cache_max_size * 0.8:  # Leave some headroom
                    break

                data = self.segment_cache.pop(key, b'')
                self.cache_timestamps.pop(key, None)
                current_size -= len(data)
                logger.debug(f"Removed cache entry due to size limit: {key}")

    async def split_video_to_hls(self, video_path: str, output_dir: str, max_chunk_size: int = 20 * 1024 * 1024) -> str:
        """Splits video into HLS segments using FFmpeg with enhanced error handling."""
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        os.makedirs(output_dir, exist_ok=True)

        # Probe video information
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', video_path
        ]

        try:
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            video_info = json.loads(result.stdout)
            duration = float(video_info['format'].get('duration', 0))
            bitrate = int(video_info['format'].get('bit_rate', 0))

            if bitrate > 0:
                # Calculate optimal segment duration
                max_duration = (max_chunk_size * 8) / bitrate
                segment_duration = min(max_duration * 0.8, 50)  # 80% of max, cap at 50s
                segment_duration = max(10, segment_duration)  # Minimum 10 seconds
            else:
                segment_duration = 30

            logger.info(f"Video duration: {duration:.2f}s, bitrate: {bitrate}, segment duration: {segment_duration:.2f}s")

        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to probe video details: {e}. Using default settings.")
            segment_duration = 30

        playlist_path = os.path.join(output_dir, 'playlist.m3u8')
        segment_pattern = os.path.join(output_dir, 'segment_%04d.ts')

        # Updated FFmpeg command to copy streams instead of re-encoding
        ffmpeg_cmd = [
            'ffmpeg', '-i', video_path,
            '-c:v', 'copy',     # Copy the video stream without re-encoding
            '-c:a', 'copy',     # Copy the audio stream without re-encoding
            '-hls_time', str(segment_duration),
            '-hls_list_size', '0',
            '-hls_flags', 'independent_segments',
            '-hls_segment_filename', segment_pattern,
            '-f', 'hls',
            '-y',
            playlist_path
        ]

        try:
            logger.info("Starting video segmentation with FFmpeg...")
            result = subprocess.run(
                ffmpeg_cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour timeout
            )
            logger.info(f"Video successfully split into HLS segments in {output_dir}")
            return playlist_path
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg process timed out")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg failed with error: {e.stderr}")
            raise

    async def upload_segments_to_telegram(self, segments_dir: str, video_id: str) -> Dict[str, SegmentInfo]:
        """Uploads all .ts segments to Telegram with progress tracking."""
        segment_info_map = {}
        ts_files = sorted([f for f in os.listdir(segments_dir) if f.endswith('.ts')])

        if not ts_files:
            raise ValueError(f"No .ts files found in {segments_dir}")

        logger.info(f"Found {len(ts_files)} segments to upload")

        for i, ts_file in enumerate(ts_files, 1):
            file_path = os.path.join(segments_dir, ts_file)
            file_size = os.path.getsize(file_path)

            # Check Telegram file size limits
            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Segment {ts_file} exceeds Telegram's 50MB limit ({file_size} bytes). Skipping.")
                continue

            if file_size == 0:
                logger.warning(f"Segment {ts_file} is empty. Skipping.")
                continue

            try:
                logger.info(f"Uploading segment {i}/{len(ts_files)}: {ts_file} ({file_size:,} bytes)")

                with open(file_path, 'rb') as f:
                    message = await self.bot.send_document(
                        chat_id=self.chat_id,
                        document=f,
                        filename=ts_file,
                        caption=f"Video: {video_id} | Segment {i}/{len(ts_files)}",
                        read_timeout=60,
                        write_timeout=60,
                        connect_timeout=30
                    )

                duration = self.extract_segment_duration(segments_dir, ts_file)
                segment_info_map[ts_file] = SegmentInfo(
                    filename=ts_file,
                    duration=duration,
                    file_id=message.document.file_id,
                    file_size=file_size
                )

                logger.info(f"✅ Uploaded {ts_file} - File ID: {message.document.file_id}")

                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)

            except TelegramError as e:
                logger.error(f"Failed to upload {ts_file}: {e}")
                # Continue with other segments instead of failing entirely
                continue

        if not segment_info_map:
            raise RuntimeError("No segments were successfully uploaded")

        logger.info(f"Successfully uploaded {len(segment_info_map)}/{len(ts_files)} segments")
        return segment_info_map

    def extract_segment_duration(self, segments_dir: str, segment_filename: str) -> float:
        """Extracts a segment's duration from the original FFmpeg-generated playlist."""
        playlist_path = os.path.join(segments_dir, 'playlist.m3u8')

        if not os.path.exists(playlist_path):
            logger.warning(f"Playlist not found at {playlist_path}")
            return 10.0

        try:
            with open(playlist_path, 'r') as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if segment_filename in line and i > 0:
                    prev_line = lines[i-1].strip()
                    if prev_line.startswith('#EXTINF:'):
                        duration_str = prev_line.split(':')[1].split(',')[0]
                        return float(duration_str)

        except (IOError, IndexError, ValueError) as e:
            logger.warning(f"Could not extract duration for {segment_filename}: {e}")

        return 10.0  # Fallback duration

    async def create_streaming_playlist(self, video_id: str, segment_info: Dict[str, SegmentInfo],
                                      output_path: str, host: str, port: int):
        """Creates the .m3u8 playlist with network-accessible URLs."""
        if not segment_info:
            raise ValueError("No segment information provided")

        # Simple host warning for localhost
        if host in ['localhost', '127.0.0.1']:
            logger.warning("⚠️  Using localhost - this will only work on the same machine!")
            logger.warning("   For Jellyfin/network access, use your network IP (e.g., 192.168.x.x)")
        else:
            logger.info(f"✅ Creating playlist for network host: {host}")

        self.segments_db[video_id] = segment_info
        self._save_db()

        base_url = f"http://{host}:{port}"
        target_duration = math.ceil(max(s.duration for s in segment_info.values()))
        total_duration = sum(s.duration for s in segment_info.values())

        content = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-ALLOW-CACHE:YES"
        ]

        for name in sorted(segment_info.keys()):
            segment = segment_info[name]
            content.append(f"#EXTINF:{segment.duration:.6f},")
            content.append(f"{base_url}/segment/{video_id}/{name}")

        content.append("#EXT-X-ENDLIST")

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        async with aiofiles.open(output_path, 'w') as f:
            await f.write('\n'.join(content))

        logger.info(f"Created streaming playlist at {output_path}")
        logger.info(f"Total video duration: {total_duration:.2f}s, {len(segment_info)} segments")
        logger.info(f"Playlist URL: {base_url}/playlist/{video_id}.m3u8")

        # Show a sample of the playlist for verification
        sample_lines = content[:10] + (["..."] if len(content) > 10 else [])
        logger.debug("Playlist preview:")
        for line in sample_lines:
            logger.debug(f"  {line}")

    async def download_segment_from_telegram(self, file_id: str) -> Optional[bytes]:
        """Downloads a segment from Telegram directly into memory with retry logic."""
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                file = await self.bot.get_file(file_id)
                content = await file.download_as_bytearray()
                return bytes(content)
            except TelegramError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Download attempt {attempt + 1} failed for {file_id}: {e}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"Failed to download segment {file_id} after {max_retries} attempts: {e}")

        return None

    async def _prefetch_segments(self, video_id: str, current_segment_name: str):
        """Asynchronously pre-fetches the next segments into the cache for smooth playback."""
        if video_id not in self.segments_db:
            return

        all_segments = sorted(self.segments_db[video_id].keys())

        try:
            current_index = all_segments.index(current_segment_name)
        except ValueError:
            logger.warning(f"Current segment {current_segment_name} not found in segment list")
            return

        # Clean up cache before prefetching
        self._cleanup_cache()

        prefetch_tasks = []
        for i in range(1, self.prefetch_count + 1):
            next_index = current_index + i
            if next_index >= len(all_segments):
                break

            next_segment_name = all_segments[next_index]
            if next_segment_name not in self.segment_cache:
                logger.debug(f"🚀 Scheduling prefetch for segment: {next_segment_name}")
                prefetch_tasks.append(self._prefetch_single_segment(video_id, next_segment_name))

        if prefetch_tasks:
            # Run prefetch tasks concurrently
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)

    async def _prefetch_single_segment(self, video_id: str, segment_name: str):
        """Prefetch a single segment into cache."""
        try:
            segment_info = self.segments_db[video_id][segment_name]
            downloaded_bytes = await self.download_segment_from_telegram(segment_info.file_id)

            if downloaded_bytes:
                self.segment_cache[segment_name] = downloaded_bytes
                self.cache_timestamps[segment_name] = time.time()
                logger.debug(f"✅ Prefetched segment: {segment_name} ({len(downloaded_bytes):,} bytes)")
            else:
                logger.warning(f"❌ Failed to prefetch segment: {segment_name}")

        except Exception as e:
            logger.error(f"Error prefetching segment {segment_name}: {e}")

    async def start_streaming_server(self, host: str, port: int):
        """Starts the HTTP streaming server with enhanced caching and CORS support."""
        from aiohttp import web
        from aiohttp.web import middleware

        @middleware
        async def cors_handler(request, handler):
            """Add CORS headers and handle OPTIONS requests for Jellyfin compatibility."""
            if request.method == 'OPTIONS':
                return web.Response(
                    headers={
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS, HEAD',
                        'Access-Control-Allow-Headers': 'Range, Content-Type, Authorization',
                        'Access-Control-Max-Age': '3600'
                    }
                )

            response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, HEAD'
            response.headers['Access-Control-Allow-Headers'] = 'Range, Content-Type, Authorization'
            response.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
            return response

        async def serve_segment(request: web.Request):
            video_id = request.match_info['video_id']
            segment_name = request.match_info['segment_name']

            if video_id not in self.segments_db:
                logger.warning(f"Video {video_id} not found")
                return web.Response(status=404, text="Video not found")

            segment_info = self.segments_db[video_id].get(segment_name)
            if not segment_info:
                logger.warning(f"Segment {segment_name} not found for video {video_id}")
                return web.Response(status=404, text="Segment not found")

            # Start prefetching in background
            asyncio.create_task(self._prefetch_segments(video_id, segment_name))

            # Check cache first
            if segment_name in self.segment_cache:
                logger.info(f"✅ Cache HIT for segment: {segment_name}")
                segment_bytes = self.segment_cache[segment_name]
                # Update timestamp for LRU
                self.cache_timestamps[segment_name] = time.time()
            else:
                logger.warning(f"⚠️ Cache MISS for segment: {segment_name}. Downloading on-demand.")
                segment_bytes = await self.download_segment_from_telegram(segment_info.file_id)

            if not segment_bytes:
                return web.Response(status=500, text="Failed to retrieve segment")

            return web.Response(
                body=segment_bytes,
                content_type='video/mp2t',
                headers={
                    'Cache-Control': 'public, max-age=3600',
                    'Content-Length': str(len(segment_bytes)),
                    'Accept-Ranges': 'bytes',
                    'Connection': 'keep-alive'
                }
            )

        async def serve_playlist(request: web.Request):
            video_id = request.match_info['video_id']
            playlist_path = f"playlists/{video_id}.m3u8"

            if not os.path.exists(playlist_path):
                logger.warning(f"Playlist not found: {playlist_path}")
                return web.Response(status=404, text="Playlist not found")

            # Read and serve playlist content with proper headers for Jellyfin
            async with aiofiles.open(playlist_path, 'r') as f:
                content = await f.read()

            return web.Response(
                text=content,
                content_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache',
                    'Expires': '0',
                    'Accept-Ranges': 'bytes'
                }
            )

        async def debug_info(request: web.Request):
            """Debug endpoint to help troubleshoot Jellyfin issues."""
            video_id = request.match_info.get('video_id', 'all')

            debug_data = {
                'server_info': {
                    'host': host,
                    'port': port,
                    'server_url': f"http://{host}:{port}"
                },
                'cache_stats': {
                    'cached_segments': len(self.segment_cache),
                    'cache_size_mb': sum(len(data) for data in self.segment_cache.values()) / (1024*1024)
                }
            }

            if video_id == 'all':
                debug_data['videos'] = {}
                for vid, segments in self.segments_db.items():
                    debug_data['videos'][vid] = {
                        'segment_count': len(segments),
                        'total_duration': sum(s.duration for s in segments.values()),
                        'playlist_url': f"http://{host}:{port}/playlist/{vid}.m3u8",
                        'first_segment_url': f"http://{host}:{port}/segment/{vid}/{sorted(segments.keys())[0]}" if segments else None
                    }
            else:
                if video_id in self.segments_db:
                    segments = self.segments_db[video_id]
                    debug_data['video_info'] = {
                        'video_id': video_id,
                        'segment_count': len(segments),
                        'segments': [
                            {
                                'name': name,
                                'duration': info.duration,
                                'size': info.file_size,
                                'url': f"http://{host}:{port}/segment/{video_id}/{name}"
                            } for name, info in sorted(segments.items())
                        ]
                    }
                else:
                    debug_data['error'] = f"Video {video_id} not found"

            return web.json_response(debug_data)

        async def test_jellyfin_compatibility(request: web.Request):
            """Test endpoint specifically for Jellyfin compatibility."""
            return web.Response(
                text="""# Jellyfin HLS Test Playlist
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-ALLOW-CACHE:YES
#EXTINF:10.0,
http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4
#EXT-X-ENDLIST
""",
                content_type='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Access-Control-Allow-Origin': '*'
                }
            )

        # Create application with middleware
        app = web.Application(middlewares=[cors_handler])

        # Add routes
        app.router.add_get('/segment/{video_id}/{segment_name}', serve_segment)
        app.router.add_get('/playlist/{video_id}.m3u8', serve_playlist)
        app.router.add_get('/debug/{video_id}', debug_info)
        app.router.add_get('/debug', debug_info)
        app.router.add_get('/test-jellyfin.m3u8', test_jellyfin_compatibility)
        app.router.add_get('/', lambda r: web.Response(
            text=f"""Telegram Video Streaming Server
Available endpoints:
• /debug - Server debug info
• /debug/{{video_id}} - Video-specific debug info
• /playlist/{{video_id}}.m3u8 - HLS playlist
• /segment/{{video_id}}/{{segment}} - Video segments
• /test-jellyfin.m3u8 - Jellyfin compatibility test

Videos in database: {len(self.segments_db)}
""", content_type='text/plain'))

        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"🚀 Streaming server started on http://{host}:{port}")
        logger.info(f"📊 Available endpoints:")
        logger.info(f"   • Server info: http://{host}:{port}")
        logger.info(f"   • Debug info: http://{host}:{port}/debug")
        logger.info(f"   • Videos debug: http://{host}:{port}/debug/{{video_id}}")
        logger.info(f"   • Playlists: http://{host}:{port}/playlist/{{video_id}}.m3u8")
        logger.info(f"   • Jellyfin test: http://{host}:{port}/test-jellyfin.m3u8")

        return runner

# --- Main execution block ---
async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram Video Streaming App - Upload and stream videos using Telegram as storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Find your network IP first:
    python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print('Your IP:', s.getsockname()[0]); s.close()"

  Upload a video (use your actual network IP):
    python %(prog)s upload --video movie.mp4 --bot-token YOUR_TOKEN --chat-id @your_channel --host 192.168.1.100

  Start streaming server:
    python %(prog)s serve --bot-token YOUR_TOKEN --chat-id @your_channel --host 0.0.0.0
        """
    )

    subparsers = parser.add_subparsers(dest='command', required=True, help='Available commands')

    # Upload command
    upload_parser = subparsers.add_parser('upload', help='Split, upload a video, and create a streaming playlist')
    upload_parser.add_argument('--video', required=True, help='Path to the video file to upload')
    upload_parser.add_argument('--video-id', help='Custom video ID (defaults to filename without extension)')
    upload_parser.add_argument('--host', required=True,
                             help='Hostname/IP to embed in playlist URLs (e.g., 192.168.1.100 for network access) - REQUIRED for Jellyfin compatibility')

    # Serve command
    serve_parser = subparsers.add_parser('serve', help='Start the streaming server')
    serve_parser.add_argument('--host', default='0.0.0.0',
                            help='Host to bind server to (0.0.0.0 for network access, localhost for local only)')

    # Common arguments
    for p in [upload_parser, serve_parser]:
        p.add_argument('--bot-token', required=True, help='Telegram bot token')
        p.add_argument('--chat-id', required=True, help='Telegram chat ID (e.g., @channel or numeric ID)')
        p.add_argument('--port', type=int, default=5050, help='Port for the streaming server (default: 5050)')
        p.add_argument('--max-chunk-size', type=int, default=20,
                      help='Maximum chunk size in MB (default: 20)')

    args = parser.parse_args()

    # Validate arguments
    if args.command == 'upload' and not os.path.exists(args.video):
        print(f"Error: Video file '{args.video}' not found")
        return 1

    try:
        streamer = TelegramVideoStreamer(args.bot_token, args.chat_id)

        if args.command == 'upload':
            video_id = args.video_id or Path(args.video).stem
            segments_dir = os.path.join('segments', video_id)
            max_chunk_bytes = args.max_chunk_size * 1024 * 1024

            print(f"🎬 Processing video: {args.video}")
            print(f"📁 Video ID: {video_id}")
            print(f"📊 Max chunk size: {args.max_chunk_size}MB")
            print()

            print("1️⃣ Splitting video into HLS segments...")
            await streamer.split_video_to_hls(args.video, segments_dir, max_chunk_bytes)

            print("\n2️⃣ Uploading segments to Telegram...")
            segment_info = await streamer.upload_segments_to_telegram(segments_dir, video_id)

            print(f"\n3️⃣ Creating streaming playlist for host '{args.host}'...")
            os.makedirs('playlists', exist_ok=True)
            playlist_path = f"playlists/{video_id}.m3u8"
            await streamer.create_streaming_playlist(video_id, segment_info, playlist_path, args.host, args.port)

            print(f"\n✅ Upload completed successfully!")
            print(f"📋 Playlist created: {playlist_path}")
            print(f"🎯 Streaming URL: http://{args.host}:{args.port}/playlist/{video_id}.m3u8")
            print(f"\n💡 For Jellyfin:")
            print(f"   1. Create a file named '{video_id}.strm' in your Jellyfin media folder")
            print(f"   2. Put this URL inside the .strm file:")
            print(f"      http://{args.host}:{args.port}/playlist/{video_id}.m3u8")
            print(f"\n🚀 To start streaming, run:")
            print(f"   python {Path(__file__).name} serve --bot-token YOUR_TOKEN --chat-id YOUR_CHAT_ID --host {args.host}")
            print(f"\n🔧 To test the stream works:")
            print(f"   • Browser: http://{args.host}:{args.port}/debug/{video_id}")
            print(f"   • VLC: Open Network Stream → http://{args.host}:{args.port}/playlist/{video_id}.m3u8")

        elif args.command == 'serve':
            if not streamer.segments_db:
                print("⚠️ No videos found in database. Upload some videos first.")
                print(f"📊 Available videos: {len(streamer.segments_db)}")
            else:
                print(f"📊 Found {len(streamer.segments_db)} video(s) in database")

            runner = await streamer.start_streaming_server(args.host, args.port)

            print("\n⌨️ Press Ctrl+C to stop the server")
            try:
                await asyncio.Event().wait()  # Keep server running indefinitely
            except KeyboardInterrupt:
                print("\n🛑 Stopping server...")
            finally:
                await runner.cleanup()
                print("✅ Server stopped")

    except KeyboardInterrupt:
        print("\n🛑 Operation cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"Application error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
