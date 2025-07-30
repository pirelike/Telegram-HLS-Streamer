"""
Web request handlers for the Telegram HLS Streamer.
"""

import asyncio
import os
import aiofiles
import uuid
import time
import hashlib
import json
from pathlib import Path
from aiohttp import web
import aiohttp_jinja2

from ..core.exceptions import StreamingError, VideoProcessingError
from ..storage.database import DatabaseManager
from ..processing.video_processor import VideoProcessor
from ..utils.logging import logger

# A simple in-memory store for task statuses
task_status = {}


class RequestHandlers:
    """Collection of web request handlers for the streaming server."""
    
    def __init__(self, server):
        self.server = server
        self.db = server.db
        self.telegram_handler = None  # Will be set by server
        self.video_processor = VideoProcessor()
    
    def set_telegram_handler(self, handler):
        """Set the telegram handler instance."""
        self.telegram_handler = handler
    
    def _get_multi_bot_info(self):
        """Get multi-bot configuration information."""
        try:
            # Import config here to get current values
            from ..core.config import Config
            config = Config()
            
            # Get multi-bot tokens and chats
            multi_bot_tokens = config.multi_bot_tokens
            multi_bot_chats = config.multi_bot_chats
            
            bots = []
            # Ensure bot 1 (primary) is always included first
            for bot_id in sorted(multi_bot_tokens.keys()):
                token = multi_bot_tokens[bot_id]
                chat_id = multi_bot_chats.get(bot_id, '')
                bots.append({
                    'id': bot_id,
                    'token': token[:20] + '...' if len(token) > 20 else token,  # Mask token for security
                    'chat_id': chat_id,
                    'status': 'configured' if token and chat_id else 'incomplete'
                })
            
            return {
                'bots': bots,
                'total_bots': len([b for b in bots if b['status'] == 'configured']),
                'is_configured': len([b for b in bots if b['status'] == 'configured']) > 0,
                'primary_bot_configured': bool(config.bot_token and config.chat_id)
            }
            
        except Exception as e:
            logger.warning(f"Could not get multi-bot info: {e}")
            return {
                'bots': [],
                'total_bots': 0,
                'is_configured': False,
                'primary_bot_configured': False
            }
    
    # ============== Main Interface ==============
    
    @aiohttp_jinja2.template('index_enhanced.html')
    async def index(self, request: web.Request):
        """Serve the main web interface."""
        try:
            # Get local IP
            from ..utils.networking import get_local_ip
            local_ip = get_local_ip()
            
            # Get multi-bot info
            multi_bot_info = self._get_multi_bot_info()
            
            # Get system stats for dashboard
            try:
                import psutil
                memory_info = psutil.virtual_memory()
                disk_info = psutil.disk_usage('/')
                system_stats = {
                    'cpu_percent': psutil.cpu_percent(interval=1),
                    'memory_percent': memory_info.percent,
                    'memory_used': memory_info.used,
                    'memory_total': memory_info.total,
                    'disk_percent': disk_info.percent,
                    'disk_used': disk_info.used,
                    'disk_total': disk_info.total,
                    'boot_time': psutil.boot_time()
                }
            except ImportError:
                system_stats = {
                    'cpu_percent': 0,
                    'memory_percent': 0,
                    'memory_used': 0,
                    'memory_total': 8 * 1024**3,  # Default 8GB
                    'disk_percent': 0,
                    'disk_used': 0,
                    'disk_total': 100 * 1024**3,  # Default 100GB
                    'boot_time': time.time()
                }
            
            # Get cache stats
            cache_stats = {}
            if hasattr(self.server, 'cache_manager') and self.server.cache_manager:
                try:
                    cache_stats = await self.server.cache_manager.get_cache_stats()
                except Exception as e:
                    logger.warning(f"Could not get cache stats: {e}")
                    cache_stats = {'size': 0, 'max_size': 0, 'hit_rate': 0}
            
            # Get database stats
            db_stats = {}
            try:
                db_stats = await self.db.get_statistics()
            except Exception as e:
                logger.warning(f"Could not get database stats: {e}")
                db_stats = {'total_videos': 0, 'total_segments': 0}
            
            # Get configuration info for the template
            config_info = {
                'max_upload_size': getattr(self.server, 'max_upload_size', 50 * 1024**3),  # Default 50GB
                'max_chunk_size': 15 * 1024**2,  # 15MB chunk size for Telegram compatibility
                'cache_type': getattr(self.server, 'cache_type', 'memory'),
                'cache_size': getattr(self.server, 'cache_size', 500 * 1024**2),  # Default 500MB
                'force_https': getattr(self.server, 'force_https', False),
                'public_domain': getattr(self.server, 'public_domain', ''),
                'preload_segments': getattr(self.server, 'preload_segments', 0),
                'streaming_threshold_gb': 2,  # Default streaming threshold
                'ffmpeg_threads': 2,  # Default FFmpeg threads
                'min_segment_duration': 1,  # Default minimum segment duration
                'max_segment_duration': 5,   # Default maximum segment duration
                'log_level': 'INFO',  # Default log level
                'hardware_accel': getattr(self.server, 'hardware_accel', 'auto')  # Hardware acceleration setting
            }
            
            return {
                'local_ip': local_ip,
                'multi_bot_info': multi_bot_info,
                'system_stats': system_stats,
                'cache_stats': cache_stats,
                'db_stats': db_stats,
                'config_info': config_info,
                'telegram_configured': multi_bot_info['is_configured'],
                'ssl_enabled': getattr(self.server, 'ssl_cert_path', None) or getattr(self.server, 'force_https', False),
                'protocol': 'https' if getattr(self.server, 'ssl_cert_path', None) or getattr(self.server, 'force_https', False) else 'http',
                'public_domain': getattr(self.server, 'public_domain', 'Not configured') or 'Not configured'
            }
            
        except Exception as e:
            logger.error(f"Error serving index page: {e}")
            return web.Response(text=f"Error loading interface: {e}", status=500)
    
    @aiohttp_jinja2.template('telegram_config.html')
    async def telegram_config_page(self, request: web.Request):
        """Serve the Telegram configuration page."""
        try:
            # Get server version
            server_version = getattr(self.server, 'version', '1.0.0')
            
            return {
                'server_version': server_version
            }
            
        except Exception as e:
            logger.error(f"Error serving Telegram config page: {e}")
            return web.Response(text=f"Error loading Telegram configuration: {e}", status=500)
    
    # ============== Settings API ==============
    
    async def api_get_settings(self, request: web.Request):
        """API endpoint to get current settings for the settings panel."""
        try:
            from ..core.config import Config
            config = Config()
            
            # Get all environment variables from the config (now reads everything from .env)
            settings = config.get_settings_dict()
            
            # Add metadata for multi-bot configuration - include all slots for UI
            configured_bots = []
            for i in range(1, 11):
                token_key = f'BOT_TOKEN_{i}'
                chat_key = f'CHAT_ID_{i}'
                token = settings.get(token_key, '')
                chat = settings.get(chat_key, '')
                configured_bots.append({
                    'id': i,
                    'token_key': token_key,
                    'chat_key': chat_key,
                    'has_token': bool(token),
                    'has_chat': bool(chat),
                    'is_configured': bool(token and chat),
                    'is_empty': not bool(token or chat)
                })
            
            # Add metadata to response
            response = {
                'settings': settings,
                'meta': {
                    'configured_bots': configured_bots,
                    'total_bots': len(configured_bots),
                    'max_bots': 10
                }
            }
            
            return web.json_response(response)
            
        except Exception as e:
            logger.error(f"Error getting settings: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def api_save_settings(self, request: web.Request):
        """API endpoint to save settings to .env file."""
        try:
            data = await request.json()
            
            # Write settings to .env file
            env_path = '.env'
            env_content = "# Telegram HLS Streamer Configuration\n# Generated by web interface\n\n"
            
            # Group settings by category and clean up empty values
            telegram_settings = {}
            network_settings = {}
            video_settings = {}  
            cache_settings = {}
            system_settings = {}
            additional_settings = {}
            
            for key, value in data.items():
                # Skip empty values for telegram settings to avoid creating empty entries
                if key.startswith('BOT_TOKEN') or key.startswith('CHAT_ID'):
                    if value and value.strip():  # Only add non-empty values
                        telegram_settings[key] = value.strip()
                elif key in ['LOCAL_HOST', 'LOCAL_PORT', 'PUBLIC_DOMAIN', 'FORCE_HTTPS', 'SSL_CERT_PATH', 'SSL_KEY_PATH']:
                    if value and value.strip():
                        network_settings[key] = value.strip()
                elif key in ['MAX_UPLOAD_SIZE', 'MAX_CHUNK_SIZE', 'MIN_SEGMENT_DURATION', 'MAX_SEGMENT_DURATION', 'FFMPEG_THREADS', 'FFMPEG_HARDWARE_ACCEL']:
                    if value and value.strip():
                        video_settings[key] = value.strip()
                elif key in ['CACHE_TYPE', 'CACHE_SIZE', 'PRELOAD_SEGMENTS']:
                    if value and value.strip():
                        cache_settings[key] = value.strip()
                elif key in ['LOG_LEVEL', 'LOG_FILE']:
                    if value and value.strip():
                        system_settings[key] = value.strip()
                else:
                    if value and value.strip():
                        additional_settings[key] = value.strip()
            
            # Write sections to .env file
            if telegram_settings:
                env_content += "# Telegram Configuration\n"
                for key, value in telegram_settings.items():
                    if value:  # Only write non-empty values
                        env_content += f'{key}="{value}"\n'
                env_content += "\n"
            
            if network_settings:
                env_content += "# Network Configuration\n"
                for key, value in network_settings.items():
                    if value:
                        env_content += f'{key}={value}\n'
                env_content += "\n"
            
            if video_settings:
                env_content += "# Video Processing\n"
                for key, value in video_settings.items():
                    if value:
                        env_content += f'{key}={value}\n'
                env_content += "\n"
            
            if cache_settings:
                env_content += "# Cache Configuration\n"
                for key, value in cache_settings.items():
                    if value:
                        env_content += f'{key}={value}\n'
                env_content += "\n"
            
            if system_settings:
                env_content += "# System Settings\n"
                for key, value in system_settings.items():
                    if value:
                        env_content += f'{key}="{value}"\n'
                env_content += "\n"
            
            if additional_settings:
                env_content += "# Additional Settings\n"
                for key, value in additional_settings.items():
                    if value:
                        env_content += f'{key}={value}\n'
            
            # Write to file
            with open(env_path, 'w') as f:
                f.write(env_content)
            
            logger.info("Settings saved to .env file")
            return web.json_response({'success': True, 'message': 'Settings saved successfully'})
            
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    
    # ============== Video Processing ==============
    
    async def handle_process_request(self, request: web.Request):
        """Handle video processing form submission with streaming file upload."""
        data = await request.post()
        video_file_field = data.get('video_file')

        if not video_file_field:
            return web.json_response({'success': False, 'error': 'Video file is required.'}, status=400)

        # Stream file upload to prevent memory issues
        filename = f"{uuid.uuid4()}-{video_file_field.filename}"
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        video_path = os.path.join(temp_dir, filename)
        
        try:
            chunk_size = 8192  # 8KB chunks
            total_written = 0
            
            # Use aiofiles for async file writing
            async with aiofiles.open(video_path, 'wb') as f:
                while True:
                    chunk = video_file_field.file.read(chunk_size)
                    if not chunk:
                        break
                    await f.write(chunk)
                    total_written += len(chunk)
                    
                    # Progress logging for very large files
                    if total_written % (50 * 1024 * 1024) == 0:  # Every 50MB
                        logger.info(f"📤 Uploaded {total_written / (1024*1024):.1f}MB...")

            logger.info(f"✅ File uploaded successfully: {total_written / (1024*1024):.1f}MB")

        except Exception as e:
            logger.error(f"❌ File upload failed: {e}")
            if os.path.exists(video_path):
                os.remove(video_path)
            return web.json_response({'success': False, 'error': f'File upload failed: {e}'}, status=500)

        # Start video processing task
        task_id = str(uuid.uuid4())
        asyncio.create_task(self._process_video_task(task_id, video_path))

        return web.json_response({'success': True, 'task_id': task_id})
    
    async def _process_video_task(self, task_id: str, video_path: str):
        """Background task to process the video."""
        def log_status(message, level='INFO'):
            status = f"[{level}] {message}"
            if task_id not in task_status:
                task_status[task_id] = []
            task_status[task_id].append(status)
            logger.info(status)

        try:
            log_status("🎬 Starting video processing...")
            
            # Monitor memory usage
            try:
                import psutil
                memory_start = psutil.virtual_memory().percent
                log_status(f"💾 Initial memory usage: {memory_start:.1f}%")
            except ImportError:
                log_status("💾 Memory monitoring unavailable (psutil not installed)")
            
            # Get file info
            file_size = os.path.getsize(video_path)
            file_size_mb = file_size / (1024 * 1024)
            log_status(f"📊 Processing file: {file_size_mb:.1f}MB")
            
            # Create output directory
            video_id = str(uuid.uuid4())
            output_dir = os.path.join("segments", video_id)
            os.makedirs(output_dir, exist_ok=True)
            
            # Process video using video processor
            try:
                playlist_path = self.video_processor.split_video_to_hls(video_path, output_dir)
                log_status("✅ Video processing completed successfully!")
                
                # Store in database if we have telegram handler
                if self.telegram_handler:
                    try:
                        await self._store_processed_video(video_id, video_path, output_dir, log_status)
                    except Exception as e:
                        log_status(f"⚠️ Database storage failed: {e}", 'WARNING')
                
                # Generate access URLs
                protocol = "https" if self.server.force_https or self.server.ssl_cert_path else "http"
                host = self.server.public_domain or self.server.local_ip
                
                # For forced HTTPS (reverse proxy), don't show port since proxy handles standard HTTPS port 443
                # For direct SSL, only show port if not 443 for HTTPS or 80 for HTTP
                if self.server.force_https and self.server.public_domain:
                    port = ""  # Reverse proxy handles HTTPS on standard port 443
                else:
                    port = "" if (protocol == "https" and self.server.port == 443) or (protocol == "http" and self.server.port == 80) else f":{self.server.port}"
                
                public_url = f"{protocol}://{host}{port}/playlist/public/{video_id}.m3u8"
                local_url = f"http://{self.server.local_ip}:{self.server.port}/playlist/local/{video_id}.m3u8"
                
                log_status(f"🌐 Public URL: <a href='{public_url}' target='_blank'>{public_url}</a>", 'RESULT')
                log_status(f"🏠 Local URL: <a href='{local_url}' target='_blank'>{local_url}</a>", 'RESULT')
                
            except VideoProcessingError as e:
                log_status(f"❌ Video processing failed: {e}", 'ERROR')
            except Exception as e:
                log_status(f"💥 Unexpected error during processing: {e}", 'ERROR')
                
        except Exception as e:
            log_status(f"💥 Task execution failed: {e}", 'ERROR')
        finally:
            # Cleanup temp file
            try:
                if os.path.exists(video_path):
                    os.remove(video_path)
                    log_status("🧹 Temporary file cleaned up")
            except Exception as e:
                log_status(f"⚠️ Cleanup warning: {e}", 'WARNING')
            
            # Final memory check
            try:
                import psutil
                memory_end = psutil.virtual_memory().percent
                log_status(f"💾 Final memory usage: {memory_end:.1f}%")
            except ImportError:
                pass
            
            log_status("🏁 Task completed", 'SUCCESS')
    
    async def _store_processed_video(self, video_id: str, video_path: str, output_dir: str, log_status):
        """Store processed video information in database."""
        try:
            # Upload segments to Telegram using the proper round-robin method
            all_files = os.listdir(output_dir) if os.path.exists(output_dir) else []
            segments = [f for f in all_files if f.endswith('.ts')]
            log_status(f"📁 Found {len(all_files)} files in output directory: {all_files[:10]}...")  # Show first 10 files
            log_status(f"📤 Uploading {len(segments)} segments to Telegram using round-robin distribution...")
            
            # Use the sophisticated upload method that handles round-robin properly
            upload_success = await self.telegram_handler.upload_segments_to_telegram(
                segments_dir=output_dir,
                video_id=video_id,
                original_filename=os.path.basename(video_path)
            )
            
            if not upload_success:
                log_status("❌ Failed to upload segments to Telegram", 'ERROR')
                return
            
            log_status(f"✅ Successfully uploaded all {len(segments)} segments with round-robin distribution")
            
            # Database storage is handled by upload_segments_to_telegram method
            log_status(f"✅ Video and segments stored in database with round-robin bot tracking")
            
            # Generate playlists for both local and public access
            try:
                log_status("📄 Generating playlists for local and public access...")
                await self._generate_both_playlists(video_id)
                log_status("✅ Playlists generated and saved successfully")
            except Exception as e:
                log_status(f"⚠️ Warning: Could not generate playlists: {e}", 'WARNING')
            
            # Clean up segments directory after successful upload
            try:
                import shutil
                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir)
                    log_status(f"🧹 Cleaned up segments directory: {output_dir}")
            except Exception as e:
                log_status(f"⚠️ Warning: Could not clean up segments directory: {e}", 'WARNING')
            
        except Exception as e:
            raise StreamingError(f"Failed to store video data: {e}")
    
    async def handle_status_request(self, request: web.Request):
        """Handle requests for task status via Server-Sent Events."""
        task_id = request.match_info['task_id']
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET',
                'Access-Control-Allow-Headers': 'Cache-Control'
            }
        )
        
        await response.prepare(request)
        
        try:
            last_sent_count = 0
            
            while True:
                if task_id in task_status:
                    messages = task_status[task_id]
                    
                    # Send new messages
                    for message in messages[last_sent_count:]:
                        await response.write(f"data: {message}\n\n".encode('utf-8'))
                    
                    last_sent_count = len(messages)
                    
                    # Check if task is complete
                    if messages and any("---STREAM_END---" in msg or "🏁 Task completed" in msg for msg in messages):
                        await response.write(b"data: ---STREAM_END---\n\n")
                        await response.write(b"data: ---CLOSE---\n\n")
                        break
                
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Error in status stream: {e}")
        finally:
            # Cleanup task status after streaming
            if task_id in task_status:
                del task_status[task_id]
        
        return response
    
    # ============== Batch Processing ==============
    
    async def handle_batch_process_request(self, request: web.Request):
        """Handle batch processing of video files."""
        data = await request.post()
        folder_files = data.getall('folder_files', [])
        recursive = data.get('recursive', 'false').lower() == 'true'
        dry_run = data.get('dry_run', 'false').lower() == 'true'

        if not folder_files:
            return web.json_response({'success': False, 'error': 'No files uploaded.'}, status=400)

        # Filter video files
        video_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.mpg', '.mpeg', '.ts', '.mts', '.m2ts'}
        video_files = []
        
        for file_field in folder_files:
            if file_field.filename:
                ext = Path(file_field.filename).suffix.lower()
                if ext in video_extensions:
                    video_files.append(file_field)

        if not video_files:
            return web.json_response({'success': False, 'error': 'No video files found in uploaded folder.'}, status=400)

        # Create temp directory for batch processing
        batch_id = str(uuid.uuid4())
        temp_dir = f"temp_batch_{batch_id}"
        os.makedirs(temp_dir, exist_ok=True)

        # Save uploaded files
        saved_files = []
        for file_field in video_files:
            filename = f"{uuid.uuid4()}-{file_field.filename}"
            file_path = os.path.join(temp_dir, filename)
            
            # Create directory structure if needed (for folder uploads)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # Stream file to disk
            async with aiofiles.open(file_path, 'wb') as f:
                while True:
                    chunk = file_field.file.read(8192)
                    if not chunk:
                        break
                    await f.write(chunk)
            
            saved_files.append({
                'path': file_path,
                'original_name': file_field.filename,
                'relative_path': getattr(file_field, 'name', file_field.filename)
            })

        # Initialize batch status
        task_status[batch_id] = {
            'type': 'batch',
            'total': len(saved_files),
            'processed': 0,
            'success': 0,
            'failed': 0,
            'current_file': None,
            'logs': [],
            'files': saved_files,
            'dry_run': dry_run,
            'temp_dir': temp_dir
        }

        # Start batch processing task
        asyncio.create_task(self._process_batch_task(batch_id))

        return web.json_response({
            'success': True, 
            'task_id': batch_id, 
            'file_count': len(saved_files),
            'dry_run': dry_run
        })
    
    async def _process_batch_task(self, batch_id: str):
        """Process a batch of video files."""
        batch_info = task_status[batch_id]
        
        def log_batch(message, level='INFO'):
            log_entry = {'message': message, 'level': level, 'timestamp': time.time()}
            batch_info['logs'].append(log_entry)
            logger.info(f"[BATCH {batch_id}] {message}")

        try:
            log_batch(f"🚀 Starting batch processing of {batch_info['total']} files...")
            
            if batch_info['dry_run']:
                log_batch("🧪 DRY RUN MODE - Analyzing files without processing")

            for i, file_info in enumerate(batch_info['files']):
                batch_info['current_file'] = file_info['original_name']
                batch_info['processed'] = i
                
                try:
                    log_batch(f"📁 Processing {file_info['original_name']} ({i+1}/{batch_info['total']})")
                    
                    if batch_info['dry_run']:
                        # Simulate processing for dry run
                        await asyncio.sleep(1)
                        log_batch(f"✅ Would process: {file_info['original_name']}")
                        batch_info['success'] += 1
                    else:
                        # Create individual task for this file
                        file_task_id = str(uuid.uuid4())
                        
                        # Process the video using existing logic
                        await self._process_video_task(file_task_id, file_info['path'])
                        
                        # Check if processing was successful
                        if file_task_id in task_status and any('SUCCESS' in status for status in task_status[file_task_id]):
                            batch_info['success'] += 1
                            log_batch(f"✅ Successfully processed: {file_info['original_name']}")
                        else:
                            batch_info['failed'] += 1
                            log_batch(f"❌ Failed to process: {file_info['original_name']}", 'ERROR')
                        
                        # Clean up individual task status
                        if file_task_id in task_status:
                            del task_status[file_task_id]
                
                except Exception as e:
                    batch_info['failed'] += 1
                    log_batch(f"❌ Error processing {file_info['original_name']}: {str(e)}", 'ERROR')
                
                batch_info['processed'] = i + 1

            # Final summary
            log_batch(f"🎉 Batch processing completed! Success: {batch_info['success']}, Failed: {batch_info['failed']}")
            batch_info['current_file'] = None
            
        except Exception as e:
            log_batch(f"💥 Batch processing failed: {str(e)}", 'ERROR')
        finally:
            # Clean up temp directory
            try:
                import shutil
                if os.path.exists(batch_info['temp_dir']):
                    shutil.rmtree(batch_info['temp_dir'])
                log_batch("🧹 Cleaned up temporary files")
            except Exception as e:
                log_batch(f"⚠️ Failed to clean up temp directory: {str(e)}", 'WARNING')
    
    async def handle_batch_status_request(self, request: web.Request):
        """Server-sent events stream for batch processing status."""
        task_id = request.match_info['task_id']
        
        if task_id not in task_status:
            return web.json_response({'error': 'Task not found'}, status=404)

        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        await response.prepare(request)

        batch_info = task_status[task_id]
        last_log_count = 0
        
        try:
            while True:
                # Send progress update
                progress_data = {
                    'type': 'progress',
                    'processed': batch_info['processed'],
                    'total': batch_info['total'],
                    'success': batch_info['success'],
                    'failed': batch_info['failed'],
                    'current_file': batch_info['current_file']
                }
                await response.write(f"data: {web.json_response(progress_data).text}\n\n".encode())

                # Send new log messages
                if len(batch_info['logs']) > last_log_count:
                    for log_entry in batch_info['logs'][last_log_count:]:
                        log_data = {
                            'type': 'log',
                            'message': log_entry['message'],
                            'level': log_entry['level'],
                            'timestamp': log_entry['timestamp']
                        }
                        await response.write(f"data: {web.json_response(log_data).text}\n\n".encode())
                    last_log_count = len(batch_info['logs'])

                # Check if batch is complete
                if batch_info['processed'] >= batch_info['total'] and batch_info['current_file'] is None:
                    complete_data = {'type': 'complete'}
                    await response.write(f"data: {web.json_response(complete_data).text}\n\n".encode())
                    break

                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Error in batch status stream: {e}")
        finally:
            # Clean up task status after completion
            if task_id in task_status:
                del task_status[task_id]

        return response
    
    # ============== Content Serving ==============
    
    async def serve_playlist(self, request: web.Request):
        """Serve HLS playlist files."""
        video_id = request.match_info['video_id']
        access_type = request.match_info.get('access_type', 'local')
        
        try:
            # Get video info from database
            video_info = await self.db.get_video_info(video_id)
            if not video_info:
                raise web.HTTPNotFound(text="Video not found")
            
            playlist_dir = f"{self.server.playlists_dir}/{access_type}"
            playlist_path = os.path.join(playlist_dir, f"{video_id}.m3u8")
            
            if not os.path.exists(playlist_path):
                # Generate playlist if it doesn't exist
                await self._generate_playlist(video_id, access_type, playlist_path)
            
            headers = {
                'Content-Type': 'application/vnd.apple.mpegurl',
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
            return web.FileResponse(playlist_path, headers=headers)
            
        except Exception as e:
            logger.error(f"Error serving playlist {video_id}: {e}")
            raise web.HTTPInternalServerError(text="Failed to serve playlist")
    
    async def serve_segment(self, request: web.Request):
        """Enhanced segment serving with predictive caching and session tracking."""
        video_id = request.match_info['video_id']
        segment_name = request.match_info['segment_name']
        
        try:
            # Get session ID for tracking
            session_id = self._get_session_id(request)
            
            # Try to get from cache first
            if hasattr(self.server, 'cache_manager') and self.server.cache_manager:
                cached_data = await self.server.cache_manager.get_cached_segment(video_id, segment_name)
                if cached_data:
                    logger.debug(f"Cache hit for {video_id}/{segment_name}")
                    return web.Response(body=cached_data, headers={'Content-Type': 'video/mp2t'})
            
            # Get segment info from database
            video_segments = await self.db.get_video_segments(video_id)
            segment_info = video_segments.get(segment_name)
            if not segment_info:
                raise web.HTTPNotFound()
            
            # Download from Telegram
            if self.telegram_handler:
                file_data = await self.telegram_handler.download_segment_from_telegram(
                    segment_info.file_id, segment_info.bot_id
                )
                
                # Cache the segment
                if file_data and hasattr(self.server, 'cache_manager') and self.server.cache_manager:
                    await self.server.cache_manager.cache_segment(video_id, segment_name, file_data)
                
                if file_data:
                    # Trigger predictive caching if available
                    if hasattr(self.server, 'predictive_cache_manager') and self.server.predictive_cache_manager:
                        await self.server.predictive_cache_manager.handle_segment_request(
                            video_id, segment_name, session_id
                        )
                    
                    headers = {
                        'Content-Type': 'video/mp2t',
                        'Accept-Ranges': 'bytes',
                        'Cache-Control': 'public, max-age=31536000'  # Cache segments for 1 year since they don't change
                    }
                    return web.Response(body=file_data, headers=headers)
                else:
                    raise web.HTTPNotFound(text="Segment not available")
            else:
                raise web.HTTPInternalServerError(text="Telegram handler not available")
                
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error serving segment {video_id}/{segment_name}: {e}")
            raise web.HTTPInternalServerError()
    
    async def serve_subtitle(self, request: web.Request):
        """Handle requests for subtitle files with bot-aware downloads."""
        video_id = request.match_info['video_id']
        language = request.match_info.get('language', 'unknown')
        ext = request.match_info.get('ext', 'srt')
        
        try:
            # Get subtitle info from database
            subtitle_info = await self.db.get_subtitle_info(video_id, language)
            if not subtitle_info:
                raise web.HTTPNotFound()
            
            # Download from Telegram
            if self.telegram_handler:
                file_data = await self.telegram_handler.download_file(subtitle_info['file_id'])
                
                # Determine content type
                content_type = 'text/vtt' if ext == 'vtt' else 'text/plain'
                
                return web.Response(
                    body=file_data,
                    headers={
                        'Content-Type': content_type,
                        'Content-Disposition': f'inline; filename="{video_id}_{language}.{ext}"'
                    }
                )
            else:
                raise web.HTTPInternalServerError(text="Telegram handler not available")
                
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error serving subtitle {video_id}/{language}: {e}")
            raise web.HTTPInternalServerError()
    
    async def list_subtitles(self, request: web.Request):
        """List available subtitles for a video."""
        video_id = request.match_info['video_id']
        
        try:
            subtitles = await self.db.get_video_subtitles(video_id)
            return web.json_response({'subtitles': subtitles})
            
        except Exception as e:
            logger.error(f"Error listing subtitles for {video_id}: {e}")
            return web.json_response({'error': 'Failed to list subtitles'}, status=500)
    
    # ============== Cache Management ==============
    
    async def serve_cache_stats(self, request: web.Request):
        """Enhanced cache statistics endpoint with predictive caching info."""
        try:
            stats = {}
            
            # Basic cache stats
            if hasattr(self.server, 'cache_manager') and self.server.cache_manager:
                stats.update(await self.server.cache_manager.get_cache_stats())
            
            # Predictive cache stats
            if hasattr(self.server, 'predictive_cache_manager') and self.server.predictive_cache_manager:
                predictive_stats = await self.server.predictive_cache_manager.get_cache_stats()
                stats['predictive'] = predictive_stats
            
            return web.json_response(stats)
            
        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return web.json_response({'error': 'Failed to get cache stats'}, status=500)
    
    async def clear_cache_endpoint(self, request: web.Request):
        """Enhanced cache clearing endpoint."""
        video_id = request.query.get('video_id')
        
        try:
            cleared_count = 0
            
            # Clear basic cache
            if hasattr(self.server, 'cache_manager') and self.server.cache_manager:
                if video_id:
                    cleared_count += await self.server.cache_manager.clear_video_cache(video_id)
                else:
                    cleared_count += await self.server.cache_manager.clear_all()
            
            # Clear predictive cache
            if hasattr(self.server, 'predictive_cache_manager') and self.server.predictive_cache_manager:
                if video_id:
                    await self.server.predictive_cache_manager.clear_video_cache(video_id)
                else:
                    await self.server.predictive_cache_manager.clear_all()
            
            return web.json_response({
                'success': True,
                'message': f'Cache cleared successfully',
                'cleared_items': cleared_count
            })
            
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    # ============== Predictive Cache Endpoints ==============
    
    async def force_preload_endpoint(self, request: web.Request):
        """Endpoint to manually trigger preloading for a specific video."""
        if not hasattr(self.server, 'predictive_cache_manager') or not self.server.predictive_cache_manager:
            return web.json_response({'error': 'Predictive caching not available'}, status=503)
        
        data = await request.json()
        video_id = data.get('video_id')
        start_segment = data.get('start_segment', 0)
        
        if not video_id:
            return web.json_response({'error': 'video_id is required'}, status=400)
        
        try:
            preloaded_count = await self.server.predictive_cache_manager.force_preload(
                video_id, start_segment
            )
            
            return web.json_response({
                'success': True,
                'message': f'Preloaded {preloaded_count} segments for video {video_id}',
                'preloaded_segments': preloaded_count
            })
            
        except Exception as e:
            logger.error(f"Error in force preload: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def list_active_sessions(self, request: web.Request):
        """Endpoint to list active viewing sessions."""
        if not hasattr(self.server, 'predictive_cache_manager') or not self.server.predictive_cache_manager:
            return web.json_response({'error': 'Predictive caching not available'}, status=503)
        
        try:
            sessions = await self.server.predictive_cache_manager.get_active_sessions()
            return web.json_response({'sessions': sessions})
            
        except Exception as e:
            logger.error(f"Error listing sessions: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def get_video_analytics(self, request: web.Request):
        """Get analytics for video viewing patterns."""
        if not hasattr(self.server, 'predictive_cache_manager') or not self.server.predictive_cache_manager:
            return web.json_response({'error': 'Predictive caching not available'}, status=503)
        
        video_id = request.match_info.get('video_id')
        
        try:
            if video_id:
                analytics = await self.server.predictive_cache_manager.get_video_analytics(video_id)
            else:
                analytics = await self.server.predictive_cache_manager.get_all_analytics()
            
            return web.json_response(analytics)
            
        except Exception as e:
            logger.error(f"Error getting analytics: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    # ============== API Endpoints ==============
    
    async def api_get_all_videos(self, request: web.Request):
        """API endpoint to get all videos with their original filenames for playlist management."""
        try:
            # Get all videos from database
            videos = await self.db.get_all_videos()
            
            # Format for frontend
            video_list = []
            for video in videos:
                # Generate URLs
                protocol = "https" if self.server.force_https or self.server.ssl_cert_path else "http"
                
                # Public URL
                if self.server.public_domain:
                    if self.server.force_https and self.server.public_domain:
                        public_url = f"https://{self.server.public_domain}/playlist/public/{video.video_id}.m3u8"
                    else:
                        port = "" if (protocol == "https" and self.server.port == 443) or (protocol == "http" and self.server.port == 80) else f":{self.server.port}"
                        public_url = f"{protocol}://{self.server.public_domain}{port}/playlist/public/{video.video_id}.m3u8"
                else:
                    public_url = f"http://{self.server.local_ip}:{self.server.port}/playlist/public/{video.video_id}.m3u8"
                
                # Local URL
                local_url = f"http://{self.server.local_ip}:{self.server.port}/playlist/local/{video.video_id}.m3u8"
                
                video_list.append({
                    'video_id': video.video_id,
                    'original_filename': video.original_filename,
                    'total_segments': video.total_segments,
                    'total_duration': video.total_duration,
                    'file_size': video.file_size,
                    'created_at': video.created_at,
                    'status': video.status,
                    'public_url': public_url,
                    'local_url': local_url
                })
            
            return web.json_response({
                'success': True,
                'videos': video_list,
                'total_count': len(video_list)
            })
            
        except Exception as e:
            logger.error(f"Error getting all videos: {e}")
            return web.json_response({
                'success': False,
                'error': f'Failed to get videos: {e}'
            }, status=500)
    
    async def api_system_stats(self, request: web.Request):
        """API endpoint for real-time system statistics."""
        try:
            import psutil
            
            stats = {
                'cpu_percent': psutil.cpu_percent(interval=0.1),
                'memory': {
                    'percent': psutil.virtual_memory().percent,
                    'available_gb': psutil.virtual_memory().available / (1024**3),
                    'total_gb': psutil.virtual_memory().total / (1024**3)
                },
                'disk': {
                    'percent': psutil.disk_usage('/').percent,
                    'free_gb': psutil.disk_usage('/').free / (1024**3),
                    'total_gb': psutil.disk_usage('/').total / (1024**3)
                },
                'uptime_seconds': time.time() - psutil.boot_time(),
                'timestamp': time.time()
            }
            
            return web.json_response(stats)
            
        except ImportError:
            return web.json_response({
                'error': 'System statistics unavailable (psutil not installed)'
            }, status=503)
        except Exception as e:
            return web.json_response({
                'error': f'Failed to get system stats: {e}'
            }, status=500)
    
    async def api_database_stats(self, request: web.Request):
        """API endpoint for real-time database statistics."""
        try:
            stats = await self.db.get_statistics()
            stats['timestamp'] = time.time()
            return web.json_response(stats)
            
        except Exception as e:
            return web.json_response({
                'error': f'Failed to get database stats: {e}'
            }, status=500)
    
    async def api_get_settings(self, request: web.Request):
        """API endpoint to get current environment settings."""
        try:
            from ..core.config import get_config
            config = get_config()
            settings = config.get_settings_dict()
            return web.json_response(settings)
            
        except Exception as e:
            return web.json_response({
                'error': f'Failed to get settings: {e}'
            }, status=500)
    
    async def api_save_settings(self, request: web.Request):
        """API endpoint to save environment settings to .env file."""
        try:
            data = await request.json()
            
            from ..core.config import get_config
            config = get_config()
            config.save_settings(data)
            
            logger.info("Settings saved successfully via API")
            return web.json_response({
                'success': True,
                'message': 'Settings saved successfully! Please restart the server for changes to take effect.'
            })
            
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            return web.json_response({
                'success': False,
                'error': f'Failed to save settings: {e}'
            }, status=500)
    
    # ============== Telegram Configuration API ==============
    
    async def api_get_telegram_config(self, request: web.Request):
        """API endpoint to get Telegram bot configurations from .env file."""
        try:
            from ..core.config import Config
            config = Config()
            
            # Get all bot configurations
            multi_bot_tokens = config.multi_bot_tokens
            multi_bot_chats = config.multi_bot_chats
            
            logger.info(f"Loading bot configs - tokens: {list(multi_bot_tokens.keys())}, chats: {list(multi_bot_chats.keys())}")
            
            bots = {}
            # Always include all 10 bot slots for the UI
            for i in range(1, 11):
                token = multi_bot_tokens.get(i, '')
                chat_id = multi_bot_chats.get(i, '')
                
                status = 'configured' if token and chat_id else ('incomplete' if token or chat_id else 'empty')
                
                bots[i] = {
                    'id': i,
                    'token': token,
                    'chat_id': chat_id,
                    'status': status
                }
                
                if token or chat_id:
                    logger.info(f"Bot {i}: token={'***' if token else 'None'}, chat_id={chat_id or 'None'}, status={status}")
            
            return web.json_response({
                'success': True,
                'bots': bots,
                'total_configured': len([b for b in bots.values() if b['status'] == 'configured'])
            })
            
        except Exception as e:
            logger.error(f"Error getting Telegram config: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            }, status=500)
    
    async def api_save_telegram_config(self, request: web.Request):
        """API endpoint to save Telegram bot configurations to .env file."""
        try:
            data = await request.json()
            
            # Load existing .env content
            env_path = '.env'
            existing_content = {}
            
            if os.path.exists(env_path):
                with open(env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Remove quotes
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            existing_content[key] = value
            
            # Update with new Telegram settings
            for key, value in data.items():
                if key.startswith('BOT_TOKEN_') or key.startswith('CHAT_ID_'):
                    if value and value.strip():
                        existing_content[key] = value.strip()
                    else:
                        # Remove empty values
                        existing_content.pop(key, None)
            
            # Also remove old-style BOT_TOKEN/CHAT_ID without numbers for consistency
            # existing_content.pop('BOT_TOKEN', None)
            # existing_content.pop('CHAT_ID', None)
            
            # Write updated .env file
            env_content = "# Telegram HLS Streamer Configuration\n# Updated via Telegram Configuration Page\n\n"
            
            # Group settings
            telegram_settings = {}
            other_settings = {}
            
            for key, value in existing_content.items():
                if key.startswith('BOT_TOKEN_') or key.startswith('CHAT_ID_'):
                    telegram_settings[key] = value
                else:
                    other_settings[key] = value
            
            # Write Telegram settings first
            if telegram_settings:
                env_content += "# Telegram Bot Configuration\n"
                # Sort telegram settings by bot number
                sorted_telegram = sorted(telegram_settings.items(), key=lambda x: (
                    int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999,
                    'TOKEN' not in x[0]  # Tokens first, then chat IDs
                ))
                
                for key, value in sorted_telegram:
                    if 'TOKEN' in key:
                        env_content += f'{key}="{value}"\n'
                    else:
                        env_content += f'{key}={value}\n'
                env_content += "\n"
            
            # Write other settings
            if other_settings:
                env_content += "# Other Configuration\n"
                for key, value in sorted(other_settings.items()):
                    if key in ['PUBLIC_DOMAIN', 'SSL_CERT_PATH', 'SSL_KEY_PATH']:
                        env_content += f'{key}="{value}"\n'
                    else:
                        env_content += f'{key}={value}\n'
            
            # Write to file
            with open(env_path, 'w') as f:
                f.write(env_content)
            
            logger.info("Telegram configuration saved to .env file")
            return web.json_response({
                'success': True,
                'message': 'Telegram configuration saved successfully'
            })
            
        except Exception as e:
            logger.error(f"Error saving Telegram config: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            }, status=500)
    
    async def api_test_telegram_bot(self, request: web.Request):
        """API endpoint to test a Telegram bot configuration."""
        try:
            data = await request.json()
            bot_id = data.get('bot_id')
            token = data.get('token')
            chat_id = data.get('chat_id')
            
            if not token or not chat_id:
                return web.json_response({
                    'success': False,
                    'error': 'Both token and chat_id are required'
                }, status=400)
            
            # Test the bot by trying to get bot info and send a test message
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                # Test 1: Get bot info
                bot_info_url = f"https://api.telegram.org/bot{token}/getMe"
                async with session.get(bot_info_url) as resp:
                    if resp.status != 200:
                        return web.json_response({
                            'success': False,
                            'error': 'Invalid bot token - could not get bot info'
                        })
                    
                    bot_data = await resp.json()
                    if not bot_data.get('ok'):
                        return web.json_response({
                            'success': False,
                            'error': f'Bot API error: {bot_data.get("description", "Unknown error")}'
                        })
                    
                    bot_info = bot_data['result']
                    bot_name = bot_info.get('first_name', 'Unknown')
                    bot_username = bot_info.get('username', 'unknown')
                
                # Test 2: Send a test message
                test_message = f"🧪 Test message from Bot {bot_id} ({bot_username})\n\nBot is working correctly! ✅"
                send_url = f"https://api.telegram.org/bot{token}/sendMessage"
                
                async with session.post(send_url, json={
                    'chat_id': chat_id,
                    'text': test_message,
                    'parse_mode': 'HTML'
                }) as resp:
                    if resp.status != 200:
                        return web.json_response({
                            'success': False,
                            'error': f'Could not send message to chat {chat_id}'
                        })
                    
                    send_data = await resp.json()
                    if not send_data.get('ok'):
                        return web.json_response({
                            'success': False,
                            'error': f'Message send error: {send_data.get("description", "Could not send to chat")}'
                        })
            
            return web.json_response({
                'success': True,
                'message': f'Bot "{bot_name}" (@{bot_username}) is working correctly and can send messages to the specified chat!'
            })
            
        except Exception as e:
            logger.error(f"Error testing Telegram bot: {e}")
            return web.json_response({
                'success': False,
                'error': f'Test failed: {str(e)}'
            }, status=500)
    
    # ============== Helper Methods ==============
    
    def _get_session_id(self, request: web.Request) -> str:
        """Generate or retrieve session ID for a request."""
        # Try to get from cookie first
        session_cookie = request.cookies.get('session_id')
        if session_cookie:
            return session_cookie
        
        # Generate new session ID based on IP and User-Agent
        client_ip = request.remote
        user_agent = request.headers.get('User-Agent', '')
        
        # Create a hash for session identification
        session_data = f"{client_ip}:{user_agent}:{time.time()}"
        session_id = hashlib.md5(session_data.encode()).hexdigest()[:16]
        
        return session_id
    
    async def _generate_both_playlists(self, video_id: str):
        """Generate playlists for both local and public access."""
        # Generate local playlist
        local_dir = f"{self.server.playlists_dir}/local"
        local_path = os.path.join(local_dir, f"{video_id}.m3u8")
        await self._generate_playlist(video_id, 'local', local_path)
        
        # Generate public playlist
        public_dir = f"{self.server.playlists_dir}/public"
        public_path = os.path.join(public_dir, f"{video_id}.m3u8")
        await self._generate_playlist(video_id, 'public', public_path)
    
    async def _generate_playlist(self, video_id: str, access_type: str, playlist_path: str):
        """Generate HLS playlist file for a video."""
        try:
            # Get video segments from database
            segments_dict = await self.db.get_video_segments(video_id)
            if not segments_dict:
                raise StreamingError("No segments found for video")
            
            # Convert dict to sorted list by segment order
            segments_list = sorted(segments_dict.values(), key=lambda s: s.segment_order)
            
            # Create playlist directory
            os.makedirs(os.path.dirname(playlist_path), exist_ok=True)
            
            # Generate M3U8 content
            protocol = "https" if self.server.force_https or self.server.ssl_cert_path else "http"
            if access_type == 'public' and self.server.public_domain:
                base_url = f"{protocol}://{self.server.public_domain}"
                # For forced HTTPS (reverse proxy), don't add port since proxy handles standard HTTPS port 443
                if not (self.server.force_https and self.server.public_domain):
                    if not ((protocol == "https" and self.server.port == 443) or (protocol == "http" and self.server.port == 80)):
                        base_url += f":{self.server.port}"
            else:
                base_url = f"http://{self.server.local_ip}:{self.server.port}"
            
            # Write playlist file
            with open(playlist_path, 'w') as f:
                f.write("#EXTM3U\n")
                f.write("#EXT-X-VERSION:3\n")
                f.write("#EXT-X-TARGETDURATION:30\n")
                f.write("#EXT-X-MEDIA-SEQUENCE:0\n")
                
                for segment in segments_list:
                    f.write(f"#EXTINF:{segment.duration:.6f},\n")
                    f.write(f"{base_url}/segment/{video_id}/{segment.filename}\n")
                
                f.write("#EXT-X-ENDLIST\n")
            
            logger.info(f"Generated playlist: {playlist_path}")
            
        except Exception as e:
            logger.error(f"Error generating playlist: {e}")
            raise StreamingError(f"Failed to generate playlist: {e}")