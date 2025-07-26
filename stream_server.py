import asyncio
import os
import aiofiles
import uuid
from pathlib import Path
from aiohttp import web
import aiohttp_jinja2
import jinja2
from logger_config import logger
from database import DatabaseManager
from telegram_handler import TelegramHandler
from video_processor import split_video_to_hls

# A simple in-memory store for task statuses
task_status = {}

class StreamServer:
    """
    An HTTP server for streaming video content stored on Telegram.
    It serves an HTML frontend, handles file uploads, and streams HLS content.
    """
    def __init__(self, host: str, port: int, db_manager: DatabaseManager, bot_token: str, chat_id: str):
        self.host = host
        self.port = port
        self.db = db_manager
        self.bot_token = bot_token
        self.chat_id = chat_id
        logger.info(f"StreamServer initialized to run on {host}:{port}")

    async def process_video_task(self, task_id: str, video_path: str):
        """A background task to process the video, using server-configured credentials."""
        def log_status(message, level='INFO'):
            status = f"[{level}] {message}"
            if task_id not in task_status:
                task_status[task_id] = []
            task_status[task_id].append(status)
            logger.info(status)

        try:
            log_status("Starting video processing...")
            video_p = Path(video_path)
            # Use a UUID for the video_id to avoid conflicts with filenames
            video_id = str(uuid.uuid4())
            segments_dir = f"segments/{video_id}"

            log_status(f"Splitting video '{video_p.name}' into HLS segments...")
            split_video_to_hls(str(video_p), segments_dir)
            log_status("Video splitting complete.", "SUCCESS")

            telegram_handler = TelegramHandler(self.bot_token, self.chat_id, self.db)
            log_status("Uploading segments to Telegram...")
            success = await telegram_handler.upload_segments_to_telegram(segments_dir, video_id, video_p.name)

            if success:
                playlist_output_path = f"playlists/{video_id}.m3u8"
                await self._create_streaming_playlist(video_id, playlist_output_path)

                playlist_url = f"http://{self.host}:{self.port}/playlist/{video_id}.m3u8"
                log_status(f"Upload complete! <a href='{playlist_url}' target='_blank'>Click here for streaming playlist</a>", "RESULT")
            else:
                log_status("Upload failed.", "ERROR")

        except Exception as e:
            log_status(f"An error occurred: {e}", "ERROR")
            logger.error("Error during video processing task", exc_info=True)
        finally:
            log_status("---STREAM_END---")
            if os.path.exists(video_path):
                try:
                    os.remove(video_path)
                except OSError as e:
                    logger.error(f"Error removing temp file {video_path}: {e}")

    async def handle_process_request(self, request: web.Request):
        """Handles the video processing form submission."""
        data = await request.post()
        video_file_field = data.get('video_file')

        if not video_file_field:
            return web.json_response({'success': False, 'error': 'Video file is required.'}, status=400)

        video_bytes = video_file_field.file.read()
        filename = f"{uuid.uuid4()}-{video_file_field.filename}"
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        video_path = os.path.join(temp_dir, filename)
        with open(video_path, 'wb') as f:
            f.write(video_bytes)

        task_id = str(uuid.uuid4())
        asyncio.create_task(self.process_video_task(task_id, video_path))

        return web.json_response({'success': True, 'task_id': task_id})

    async def handle_status_request(self, request: web.Request):
        """Handles requests for task status via Server-Sent Events."""
        task_id = request.match_info['task_id']
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive'}
        )
        await response.prepare(request)

        last_index = 0
        try:
            while True:
                await asyncio.sleep(1)
                if task_id in task_status and task_status.get(task_id):
                    for i in range(last_index, len(task_status[task_id])):
                        status = task_status[task_id][i]
                        await response.write(f"data: {status}\n\n".encode('utf-8'))
                        if "---STREAM_END---" in status:
                            await response.write(b"data: ---CLOSE---\n\n")
                            return response
                    last_index = len(task_status[task_id])
        except asyncio.CancelledError:
            logger.info(f"Status stream for task {task_id} closed by client.")
        finally:
            if task_id in task_status:
                del task_status[task_id]
        return response

    @aiohttp_jinja2.template('index.html')
    async def index(self, request: web.Request):
        """Serves the main index page."""
        return {"proxy_token_configured": bool(self.bot_token and self.chat_id)}

    async def _create_streaming_playlist(self, video_id: str, output_path: str):
        """Creates the .m3u8 playlist with network-accessible URLs."""
        segments = await self.db.get_video_segments(video_id)
        if not segments:
            raise ValueError(f"No segments found for video {video_id}")

        base_url = f"http://{self.host}:{self.port}"
        target_duration = max((s.duration for s in segments.values()), default=10)

        content = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(target_duration) + 1}"]
        sorted_segments = sorted(segments.values(), key=lambda s: s.segment_order)

        for segment in sorted_segments:
            content.append(f"#EXTINF:{segment.duration:.6f},")
            content.append(f"{base_url}/segment/{video_id}/{segment.filename}")
        content.append("#EXT-X-ENDLIST")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        async with aiofiles.open(output_path, 'w') as f:
            await f.write('\n'.join(content))
        logger.info(f"Created streaming playlist at {output_path}")

    async def serve_playlist(self, request: web.Request):
        """Handles requests for HLS playlists."""
        video_id = request.match_info['video_id']
        playlist_path = f"playlists/{video_id}.m3u8"
        if os.path.exists(playlist_path):
            return web.FileResponse(playlist_path, headers={'Content-Type': 'application/vnd.apple.mpegurl'})
        return web.Response(status=404, text="Playlist not found")

    async def serve_segment(self, request: web.Request):
        """Handles requests for individual video segments."""
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']

        segments = await self.db.get_video_segments(video_id)
        segment_info = segments.get(segment_name)

        if not segment_info:
            return web.Response(status=404, text="Segment not found")

        handler = TelegramHandler(self.bot_token, self.chat_id, self.db)
        segment_bytes = await handler.download_segment_from_telegram(segment_info.file_id)

        if segment_bytes:
            return web.Response(body=segment_bytes, content_type='video/mp2t')

        return web.Response(status=500, text="Failed to retrieve segment")

    async def start(self):
        """Starts the aiohttp web server."""
        # Increase the maximum upload size to 50 GB
        app = web.Application(client_max_size=50 * 1024**3)
        aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))

        app.router.add_get('/', self.index)
        app.router.add_post('/process', self.handle_process_request)
        app.router.add_get('/status/{task_id}', self.handle_status_request)
        app.router.add_get('/playlist/{video_id}.m3u8', self.serve_playlist)
        app.router.add_get('/segment/{video_id}/{segment_name}', self.serve_segment)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info(f"🚀 Streaming server with web UI started at http://{self.host}:{self.port}")
