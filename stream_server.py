import asyncio
import os
import aiofiles
from aiohttp import web
from logger_config import logger
from database import DatabaseManager
from telegram_handler import TelegramHandler

class StreamServer:
    """
    An HTTP server for streaming video content stored on Telegram.
    It serves HLS playlists and video segments.
    """
    def __init__(self, host: str, port: int, db_manager: DatabaseManager, telegram_handler: TelegramHandler):
        """
        Initializes the StreamServer.

        Args:
            host (str): The host IP address to bind the server to.
            port (int): The port to run the server on.
            db_manager (DatabaseManager): An instance of the DatabaseManager.
            telegram_handler (TelegramHandler): An instance of the TelegramHandler.
        """
        self.host = host
        self.port = port
        self.db = db_manager
        self.telegram_handler = telegram_handler
        self.segment_cache = {}
        logger.info(f"StreamServer initialized to run on {host}:{port}")

    async def _create_streaming_playlist(self, video_id: str, output_path: str):
        """Creates the .m3u8 playlist with network-accessible URLs."""
        segments = await self.db.get_video_segments(video_id)
        if not segments:
            raise ValueError(f"No segments found for video {video_id}")

        base_url = f"http://{self.host}:{self.port}"
        target_duration = max(s.duration for s in segments.values())
        
        content = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(target_duration)}"]
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
        
        try:
            if not os.path.exists(playlist_path):
                await self._create_streaming_playlist(video_id, playlist_path)
            
            async with aiofiles.open(playlist_path, 'r') as f:
                content = await f.read()
            return web.Response(text=content, content_type='application/vnd.apple.mpegurl')
        except (ValueError, FileNotFoundError) as e:
            logger.error(f"Could not serve playlist for {video_id}: {e}", exc_info=True)
            return web.Response(status=404, text="Playlist not found")

    async def serve_segment(self, request: web.Request):
        """Handles requests for individual video segments."""
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']

        if segment_name in self.segment_cache:
            logger.info(f"Cache HIT for segment: {segment_name}")
            return web.Response(body=self.segment_cache[segment_name], content_type='video/mp2t')

        logger.info(f"Cache MISS for segment: {segment_name}. Downloading...")
        segments = await self.db.get_video_segments(video_id)
        segment_info = segments.get(segment_name)

        if not segment_info:
            return web.Response(status=404, text="Segment not found")
        
        segment_bytes = await self.telegram_handler.download_segment_from_telegram(segment_info.file_id)
        if segment_bytes:
            self.segment_cache[segment_name] = segment_bytes
            return web.Response(body=segment_bytes, content_type='video/mp2t')
        
        return web.Response(status=500, text="Failed to retrieve segment")

    async def list_videos(self, request: web.Request):
        """Lists all available videos."""
        videos = await self.db.get_all_videos()
        video_list = [{
            'video_id': v.video_id,
            'original_filename': v.original_filename,
            'playlist_url': f"http://{self.host}:{self.port}/playlist/{v.video_id}.m3u8"
        } for v in videos]
        return web.json_response({'videos': video_list})

    async def start(self):
        """Starts the aiohttp web server."""
        app = web.Application()
        app.router.add_get('/playlist/{video_id}.m3u8', self.serve_playlist)
        app.router.add_get('/segment/{video_id}/{segment_name}', self.serve_segment)
        app.router.add_get('/videos', self.list_videos)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info(f"🚀 Streaming server started at http://{self.host}:{self.port}")
