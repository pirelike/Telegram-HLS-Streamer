import os
import json
import asyncio
import time
from typing import Optional, List, Dict, Tuple
from telegram import Bot
from telegram.error import TelegramError, RetryAfter, TimedOut
from database import DatabaseManager, SegmentInfo, VideoInfo, SubtitleInfo, SubtitleFileInfo
from logger_config import logger
from datetime import datetime, timezone
from utils import calculate_file_hash, format_file_size

class RoundRobinTelegramHandler:
    """
    Simple round-robin multi-bot handler: segment 0 → bot1, segment 1 → bot2, segment 2 → bot3, segment 3 → bot1, etc.
    """
    def __init__(self, bot_configs: List[Dict], db_manager: DatabaseManager):
        """
        Initialize with multiple bot configurations for round-robin uploads.

        Args:
            bot_configs: List of dicts with 'token' and 'chat_id' keys
            db_manager: Database manager instance

        Example bot_configs:
        [
            {'token': 'bot1_token', 'chat_id': '@channel1'},
            {'token': 'bot2_token', 'chat_id': '@channel2'},
            {'token': 'bot3_token', 'chat_id': '@channel3'}
        ]
        """
        self.db = db_manager
        self.bot_configs = bot_configs
        self.bots = []

        # Initialize bots in order
        for i, config in enumerate(bot_configs):
            bot_info = {
                'id': f"bot_{i+1}",
                'bot': Bot(token=config['token']),
                'chat_id': config['chat_id'],
                'token': config['token']
            }
            self.bots.append(bot_info)

        # Limits and timing
        self.upload_limit = 20 * 1024 * 1024
        self.download_limit = 20 * 1024 * 1024
        self.base_delay = 0.8  # Reduced since load is distributed
        self.max_retries = 3

        logger.info(f"RoundRobinTelegramHandler initialized with {len(self.bots)} bots")
        logger.info("🔄 Round-robin distribution:")
        for i, bot in enumerate(self.bots):
            logger.info(f"  Slot {i}: {bot['id']} → {bot['chat_id']}")

    async def upload_segments_to_telegram(self, segments_dir: str, video_id: str, original_filename: str) -> bool:
        """
        Upload segments using round-robin distribution across bots.
        """
        ts_files = sorted([f for f in os.listdir(segments_dir) if f.endswith('.ts')])
        if not ts_files:
            logger.error(f"No .ts files found in {segments_dir}")
            return False

        logger.info(f"Found {len(ts_files)} segments for round-robin upload across {len(self.bots)} bots")

        # Show distribution preview
        self._preview_distribution(ts_files)

        # Pre-validate segments
        oversized_segments = []
        for ts_file in ts_files:
            file_path = os.path.join(segments_dir, ts_file)
            file_size = os.path.getsize(file_path)
            if file_size > self.download_limit:
                oversized_segments.append((ts_file, file_size))

        if oversized_segments:
            logger.error(f"❌ {len(oversized_segments)} segments exceed 20MB limit")
            return False

        # Setup video info
        subtitle_info = await self._load_subtitle_metadata(segments_dir)
        format_info = await self._extract_format_info(segments_dir)

        video_info = VideoInfo(
            video_id=video_id,
            original_filename=original_filename,
            total_duration=0,
            total_segments=len(ts_files),
            file_size=0,
            status='processing',
            format_name=format_info.get('format_name', 'unknown'),
            video_codec=format_info.get('video_codec', 'unknown'),
            audio_codec=format_info.get('audio_codec', 'unknown'),
            resolution=format_info.get('resolution', 'unknown'),
            bitrate=format_info.get('bitrate', 0),
            subtitle_count=len(subtitle_info),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat()
        )
        await self.db.add_video(video_info)

        # Store subtitle information
        if subtitle_info:
            for subtitle in subtitle_info:
                subtitle_obj = SubtitleInfo(
                    video_id=video_id,
                    track_index=subtitle['index'],
                    language=subtitle['language'],
                    title=subtitle['title'],
                    codec=subtitle['codec'],
                    is_default=subtitle['default'],
                    is_forced=subtitle['forced'],
                    is_hearing_impaired=subtitle['hearing_impaired'],
                    file_path=subtitle.get('extracted_file')
                )
                await self.db.add_subtitle(subtitle_obj)

        # **ROUND-ROBIN PARALLEL UPLOAD**
        start_time = time.time()

        # Group segments by bot using round-robin
        bot_segments = self._distribute_round_robin(ts_files)

        # Create parallel upload tasks
        upload_tasks = []
        for bot_index, segments in bot_segments.items():
            if segments:  # Only create task if bot has segments
                task = asyncio.create_task(
                    self._upload_bot_segments(
                        bot_index, segments, segments_dir, video_id,
                        original_filename, subtitle_info, format_info
                    )
                )
                upload_tasks.append(task)

        # Execute all uploads in parallel
        logger.info(f"🚀 Starting round-robin parallel uploads...")
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)

        # Analyze results
        total_uploaded = 0
        successful_bots = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"❌ {self.bots[i]['id']} task failed: {result}")
            else:
                uploaded_count, success = result
                total_uploaded += uploaded_count
                if success:
                    successful_bots += 1
                logger.info(f"📊 {self.bots[i]['id']}: {uploaded_count} segments uploaded")

        upload_time = time.time() - start_time

        if total_uploaded == len(ts_files):
            logger.info(f"✅ SUCCESS! All {len(ts_files)} segments uploaded in {upload_time/60:.1f} minutes")
            logger.info(f"📈 Performance: {len(ts_files)/upload_time*60:.1f} segments/minute")
            logger.info(f"⚡ Speedup: ~{len(self.bots)}x faster than single bot")

            # Upload subtitles using first bot
            if subtitle_info:
                await self._upload_subtitles(video_id, subtitle_info, original_filename)

            # Update final video info
            total_duration = sum(self._extract_segment_duration(os.path.join(segments_dir, 'playlist.m3u8'), f) for f in ts_files)
            total_size = sum(os.path.getsize(os.path.join(segments_dir, f)) for f in ts_files)

            video_info.total_duration = total_duration
            video_info.file_size = total_size
            video_info.status = 'active'
            video_info.updated_at = datetime.now(timezone.utc).isoformat()
            await self.db.add_video(video_info)

            return True
        else:
            logger.error(f"❌ FAILED: Only {total_uploaded}/{len(ts_files)} segments uploaded")
            return False

    def _preview_distribution(self, ts_files: List[str]):
        """Show how segments will be distributed across bots."""
        logger.info("📋 Round-robin distribution preview:")

        # Show first 10 and last 5 assignments
        preview_count = min(10, len(ts_files))
        for i in range(preview_count):
            bot_index = i % len(self.bots)
            bot_id = self.bots[bot_index]['id']
            chat_id = self.bots[bot_index]['chat_id']
            logger.info(f"  {ts_files[i]} → {bot_id} ({chat_id})")

        if len(ts_files) > 10:
            logger.info(f"  ... ({len(ts_files) - 10} more segments)")

            # Show last few
            for i in range(max(len(ts_files) - 3, preview_count), len(ts_files)):
                bot_index = i % len(self.bots)
                bot_id = self.bots[bot_index]['id']
                chat_id = self.bots[bot_index]['chat_id']
                logger.info(f"  {ts_files[i]} → {bot_id} ({chat_id})")

    def _distribute_round_robin(self, ts_files: List[str]) -> Dict[int, List[Tuple[int, str]]]:
        """
        Distribute segments using simple round-robin: 0→bot0, 1→bot1, 2→bot2, 3→bot0, etc.

        Returns:
            Dict mapping bot_index to list of (segment_index, filename) tuples
        """
        bot_segments = {i: [] for i in range(len(self.bots))}

        for segment_index, filename in enumerate(ts_files):
            bot_index = segment_index % len(self.bots)
            bot_segments[bot_index].append((segment_index, filename))

        # Log distribution summary
        logger.info("📊 Round-robin distribution summary:")
        for bot_index, segments in bot_segments.items():
            bot_id = self.bots[bot_index]['id']
            chat_id = self.bots[bot_index]['chat_id']
            logger.info(f"  {bot_id} ({chat_id}): {len(segments)} segments")

        return bot_segments

    async def _upload_bot_segments(self, bot_index: int, segments: List[Tuple[int, str]],
                                  segments_dir: str, video_id: str, original_filename: str,
                                  subtitle_info: List, format_info: dict) -> Tuple[int, bool]:
        """
        Upload assigned segments using a specific bot.

        Args:
            bot_index: Index of the bot to use
            segments: List of (segment_index, filename) tuples

        Returns:
            Tuple of (uploaded_count, success)
        """
        bot = self.bots[bot_index]
        uploaded_count = 0

        logger.info(f"🤖 {bot['id']} starting upload of {len(segments)} segments to {bot['chat_id']}")

        for i, (segment_index, filename) in enumerate(segments, 1):
            file_path = os.path.join(segments_dir, filename)

            # Staggered start to avoid initial burst
            if i == 1 and bot_index > 0:
                initial_delay = bot_index * 0.3  # 0.3s stagger per bot
                await asyncio.sleep(initial_delay)
            elif i > 1:
                await asyncio.sleep(self.base_delay)

            success = await self._upload_single_segment(
                bot_index, file_path, filename, segment_index, video_id,
                original_filename, subtitle_info, format_info, len(segments), i
            )

            if success:
                uploaded_count += 1
                progress = (uploaded_count / len(segments)) * 100
                logger.info(f"✅ {bot['id']}: {filename} uploaded ({uploaded_count}/{len(segments)} - {progress:.1f}%)")
            else:
                logger.error(f"❌ {bot['id']}: Failed to upload {filename}")
                # Continue with other segments even if one fails

        success_rate = uploaded_count / len(segments) if segments else 0
        logger.info(f"📊 {bot['id']} completed: {uploaded_count}/{len(segments)} segments ({success_rate*100:.1f}% success)")

        return uploaded_count, (uploaded_count == len(segments))

    async def _upload_single_segment(self, bot_index: int, file_path: str, filename: str,
                                    segment_index: int, video_id: str, original_filename: str,
                                    subtitle_info: List, format_info: dict,
                                    total_bot_segments: int, current_bot_segment: int) -> bool:
        """
        Upload a single segment using the specified bot with enhanced error handling.
        """
        bot = self.bots[bot_index]
        file_size = os.path.getsize(file_path)

        for attempt in range(self.max_retries):
            try:
                upload_start = time.time()

                # Calculate file hash for integrity
                file_hash = calculate_file_hash(file_path, algorithm='md5')
                if not file_hash:
                    file_hash = "N/A"
                else:
                    file_hash = file_hash[:12] + "..."

                # Extract duration
                duration = self._extract_segment_duration(
                    os.path.join(os.path.dirname(file_path), 'playlist.m3u8'), filename
                )

                # Create round-robin specific caption
                caption = self._create_round_robin_caption(
                    video_id=video_id,
                    filename=filename,
                    segment_index=segment_index,
                    file_size=file_size,
                    duration=duration,
                    file_hash=file_hash,
                    original_filename=original_filename,
                    subtitle_count=len(subtitle_info),
                    format_info=format_info,
                    bot_info=bot,
                    bot_progress=(current_bot_segment, total_bot_segments)
                )

                # Perform upload
                with open(file_path, 'rb') as f:
                    message = await bot['bot'].send_document(
                        chat_id=bot['chat_id'],
                        document=f,
                        filename=filename,
                        caption=caption,
                        parse_mode='HTML',
                        read_timeout=90,
                        write_timeout=90,
                        connect_timeout=30
                    )

                upload_time = time.time() - upload_start

                # Store segment info in database
                segment_order = int(filename.split('_')[-1].split('.')[0]) if '_' in filename else segment_index
                segment_info = SegmentInfo(
                    filename=filename,
                    duration=duration,
                    file_id=message.document.file_id,
                    file_size=file_size,
                    segment_order=segment_order
                )
                await self.db.add_segment(video_id, segment_info)

                logger.debug(f"🎯 {bot['id']}: {filename} uploaded in {upload_time:.1f}s (File ID: {message.document.file_id})")
                return True

            except RetryAfter as e:
                wait_time = e.retry_after + 2
                logger.warning(f"⏱️ {bot['id']} rate limited! Waiting {wait_time}s for {filename} (attempt {attempt+1})")
                await asyncio.sleep(wait_time)

            except TimedOut as e:
                wait_time = (attempt + 1) * 10
                logger.warning(f"⏰ {bot['id']} timed out uploading {filename}. Retrying in {wait_time}s (attempt {attempt+1})")
                await asyncio.sleep(wait_time)

            except TelegramError as e:
                if "file is too big" in str(e).lower():
                    logger.error(f"❌ {bot['id']}: {filename} too large for Telegram: {e}")
                    return False
                elif "too many requests" in str(e).lower():
                    wait_time = (attempt + 1) * 30
                    logger.warning(f"🚫 {bot['id']} hit rate limit for {filename}. Waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    wait_time = (attempt + 1) * 5
                    logger.warning(f"🔄 {bot['id']} Telegram error for {filename}: {e}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)

            except Exception as e:
                wait_time = (attempt + 1) * 8
                logger.error(f"💥 {bot['id']} unexpected error uploading {filename}: {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

        logger.error(f"❌ {bot['id']}: Failed to upload {filename} after {self.max_retries} attempts")
        return False

    def _create_round_robin_caption(self, video_id: str, filename: str, segment_index: int,
                                   file_size: int, duration: float, file_hash: str,
                                   original_filename: str, subtitle_count: int,
                                   format_info: dict, bot_info: dict,
                                   bot_progress: Tuple[int, int]) -> str:
        """Create caption for round-robin uploads with detailed info."""
        formatted_size = format_file_size(file_size)
        current_segment, total_bot_segments = bot_progress

        caption = f"""<b>🔄 Round-Robin Upload</b>

📹 <b>Video:</b> <code>{video_id}</code>
📁 <b>Original:</b> {original_filename}
📄 <b>Segment:</b> {filename} (#{segment_index:04d})
🤖 <b>Bot:</b> {bot_info['id']} → {bot_info['chat_id']}
📊 <b>Bot Progress:</b> {current_segment}/{total_bot_segments}
⏱️ <b>Duration:</b> {duration:.2f}s
📊 <b>Size:</b> {formatted_size}
🔍 <b>Hash:</b> <code>{file_hash}</code>"""

        if subtitle_count > 0:
            caption += f"\n🌐 <b>Subtitles:</b> {subtitle_count} tracks"

        if format_info.get('resolution') != 'unknown':
            caption += f"\n📺 <b>Resolution:</b> {format_info['resolution']}"

        # Size compliance indicator
        if file_size <= self.download_limit:
            caption += f"\n✅ <b>Status:</b> Bot-Compatible (≤20MB)"
        else:
            caption += f"\n❌ <b>Status:</b> TOO LARGE"

        caption += f"\n\n<i>#{video_id.replace('-', '_')} #round_robin #{bot_info['id']}</i>"
        return caption

    async def _upload_subtitles(self, video_id: str, subtitle_info: List[dict], original_filename: str):
        """Upload subtitles using the first bot."""
        if not subtitle_info:
            return

        logger.info(f"📄 Uploading subtitles using {self.bots[0]['id']}...")

        # Extract subtitle files
        extracted_files = []
        for subtitle in subtitle_info:
            if subtitle.get('extracted_file') and os.path.exists(subtitle['extracted_file']):
                extracted_files.append(subtitle['extracted_file'])

        if extracted_files:
            # Upload subtitle files using first bot
            bot = self.bots[0]
            for subtitle_file in extracted_files:
                try:
                    filename = os.path.basename(subtitle_file)
                    file_size = os.path.getsize(subtitle_file)

                    caption = f"""<b>📄 Subtitle File</b>

📹 <b>Video:</b> <code>{video_id}</code>
📁 <b>Original:</b> {original_filename}
📄 <b>Subtitle:</b> {filename}
📊 <b>Size:</b> {format_file_size(file_size)}
🤖 <b>Bot:</b> {bot['id']} → {bot['chat_id']}

<i>#{video_id.replace('-', '_')} #subtitle</i>"""

                    with open(subtitle_file, 'rb') as f:
                        message = await bot['bot'].send_document(
                            chat_id=bot['chat_id'],
                            document=f,
                            filename=filename,
                            caption=caption,
                            parse_mode='HTML'
                        )

                    logger.info(f"✅ Uploaded subtitle: {filename}")
                    await asyncio.sleep(1)  # Brief pause between subtitles

                except Exception as e:
                    logger.error(f"❌ Failed to upload subtitle {filename}: {e}")

    # Download method - can use any bot for redundancy
    async def download_segment_from_telegram(self, file_id: str) -> Optional[bytes]:
        """Download segment using the first available bot."""
        for bot in self.bots:
            try:
                file = await bot['bot'].get_file(file_id)

                if file.file_size and file.file_size > self.download_limit:
                    logger.error(f"File {file_id} exceeds 20MB limit")
                    return None

                content = await file.download_as_bytearray()
                logger.info(f"Downloaded segment with {bot['id']}: {file_id} ({len(content) / (1024*1024):.1f}MB)")
                return bytes(content)

            except Exception as e:
                logger.warning(f"Download failed with {bot['id']}: {e}")
                continue

        logger.error(f"Failed to download {file_id} with all bots")
        return None

    # Helper methods
    async def _load_subtitle_metadata(self, segments_dir: str) -> List[dict]:
        """Load subtitle metadata from segments directory."""
        subtitle_file = os.path.join(segments_dir, 'subtitles.json')
        if os.path.exists(subtitle_file):
            try:
                with open(subtitle_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load subtitle metadata: {e}")
        return []

    async def _extract_format_info(self, segments_dir: str) -> dict:
        """Extract format information."""
        return {
            'format_name': 'hls',
            'video_codec': 'unknown',
            'audio_codec': 'unknown',
            'resolution': 'unknown',
            'bitrate': 0
        }

    def _extract_segment_duration(self, playlist_path: str, segment_filename: str) -> float:
        """Extract segment duration from playlist."""
        try:
            with open(playlist_path, 'r') as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                if segment_filename in line and i > 0 and lines[i-1].startswith('#EXTINF:'):
                    return float(lines[i-1].split(':')[1].split(',')[0])
        except Exception as e:
            logger.warning(f"Could not extract duration for {segment_filename}: {e}")
        return 10.0


# Factory function to create the handler from environment variables
def create_round_robin_handler(db_manager: DatabaseManager) -> RoundRobinTelegramHandler:
    """
    Create a RoundRobinTelegramHandler from environment variables.

    Supports multiple configuration methods:
    1. MULTI_BOT_CONFIG JSON string
    2. Individual BOT_TOKEN_X and CHAT_ID_X variables
    3. Single bot fallback
    """
    import json

    # Method 1: JSON configuration
    multi_bot_config = os.getenv('MULTI_BOT_CONFIG')
    if multi_bot_config:
        try:
            bot_configs = json.loads(multi_bot_config)
            logger.info(f"Loaded {len(bot_configs)} bots from MULTI_BOT_CONFIG")
            return RoundRobinTelegramHandler(bot_configs, db_manager)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid MULTI_BOT_CONFIG JSON: {e}")

    # Method 2: Individual environment variables
    bot_configs = []

    # Primary bot
    bot_token = os.getenv('BOT_TOKEN')
    chat_id = os.getenv('CHAT_ID')
    if bot_token and chat_id:
        bot_configs.append({'token': bot_token, 'chat_id': chat_id})

    # Additional bots
    for i in range(2, 10):  # Support up to 9 additional bots
        token_key = f'BOT_TOKEN_{i}'
        chat_key = f'CHAT_ID_{i}'
        token = os.getenv(token_key)
        chat = os.getenv(chat_key)

        if token and chat:
            bot_configs.append({'token': token, 'chat_id': chat})
        else:
            break  # Stop at first missing pair

    if len(bot_configs) > 1:
        logger.info(f"Loaded {len(bot_configs)} bots from individual environment variables")
        return RoundRobinTelegramHandler(bot_configs, db_manager)
    elif len(bot_configs) == 1:
        logger.warning("Only one bot configured - round-robin benefits won't apply")
        return RoundRobinTelegramHandler(bot_configs, db_manager)
    else:
        raise ValueError("No valid bot configurations found in environment variables")
