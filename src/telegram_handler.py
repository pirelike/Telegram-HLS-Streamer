"""
Telegram bot handler for file upload/download and management.
Handles multiple bots with round-robin distribution and bot isolation.
"""

import asyncio
import logging
import hashlib
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, BinaryIO
from dataclasses import dataclass
from io import BytesIO

import aiohttp
import aiofiles


@dataclass
class TelegramFile:
    """Represents a file stored in Telegram."""
    file_id: str
    file_unique_id: str
    file_size: int
    file_path: Optional[str] = None


@dataclass
class UploadResult:
    """Result of uploading a file to Telegram."""
    success: bool
    file_id: Optional[str] = None
    bot_index: Optional[int] = None
    error: Optional[str] = None


class TelegramBot:
    """Individual Telegram bot wrapper."""
    
    def __init__(self, token: str, chat_id: str, index: int, config=None):
        self.token = token
        self.chat_id = chat_id
        self.index = index
        self.config = config
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.session: Optional[aiohttp.ClientSession] = None
        self.logger = logging.getLogger(f"{__name__}.Bot{index}")
        
    async def initialize(self):
        """Initialize the bot session."""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, connect=30)
        )
        
    async def cleanup(self):
        """Clean up bot resources."""
        if self.session:
            await self.session.close()
            
    async def test_connection(self) -> Dict[str, Any]:
        """Test bot connection and permissions."""
        try:
            # Test bot token
            me_response = await self._api_request("getMe")
            if not me_response.get("ok"):
                return {
                    "status": "error",
                    "error": f"Invalid bot token: {me_response.get('description', 'Unknown error')}"
                }
                
            bot_info = me_response["result"]
            
            # Test chat access by getting chat info
            chat_response = await self._api_request("getChat", {"chat_id": self.chat_id})
            if not chat_response.get("ok"):
                return {
                    "status": "error", 
                    "error": f"Cannot access chat {self.chat_id}: {chat_response.get('description', 'Unknown error')}"
                }
                
            chat_info = chat_response["result"]
            
            # Test if bot can send messages
            test_response = await self._api_request("sendMessage", {
                "chat_id": self.chat_id,
                "text": f"🤖 Bot test successful - {bot_info['first_name']} is ready!"
            })
            
            if not test_response.get("ok"):
                return {
                    "status": "error",
                    "error": f"Cannot send messages: {test_response.get('description', 'Unknown error')}"
                }
                
            return {
                "status": "success",
                "bot_info": bot_info,
                "chat_info": chat_info,
                "message": f"Bot '{bot_info['first_name']}' connected to '{chat_info.get('title', 'Private Chat')}'"
            }
            
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
            
    async def upload_file(self, file_path: Path, filename: Optional[str] = None) -> UploadResult:
        """Upload a file to Telegram."""
        try:
            if not file_path.exists():
                return UploadResult(success=False, error=f"File not found: {file_path}")
                
            file_size = file_path.stat().st_size
            
            # Get Telegram file size limit from config
            if self.config and hasattr(self.config, 'telegram_max_file_size'):
                telegram_limit = self.config.telegram_max_file_size
            else:
                telegram_limit = 20 * 1024 * 1024  # 20MB default
                
            if file_size > telegram_limit:
                limit_mb = telegram_limit // (1024 * 1024)
                return UploadResult(success=False, error=f"File too large: {file_size} bytes (max {limit_mb}MB)")
                
            filename = filename or file_path.name
            mime_type, _ = mimetypes.guess_type(str(file_path))
            
            # Create multipart form data
            data = aiohttp.FormData()
            data.add_field("chat_id", self.chat_id)
            
            async with aiofiles.open(file_path, "rb") as f:
                file_content = await f.read()
                data.add_field("document", file_content, filename=filename, content_type=mime_type)
                
            response = await self._api_request_form("sendDocument", data)
            
            if response.get("ok"):
                document = response["result"]["document"]
                self.logger.debug(f"Successfully uploaded {filename} (file_id: {document['file_id']})")
                
                return UploadResult(
                    success=True,
                    file_id=document["file_id"],
                    bot_index=self.index
                )
            else:
                error_msg = response.get("description", "Unknown error")
                self.logger.error(f"Failed to upload {filename}: {error_msg}")
                return UploadResult(success=False, error=error_msg)
                
        except Exception as e:
            self.logger.error(f"Exception during upload of {file_path}: {e}")
            return UploadResult(success=False, error=str(e))
            
    async def download_file(self, file_id: str) -> Tuple[bool, Optional[bytes], Optional[str]]:
        """Download a file from Telegram."""
        try:
            # Get file info
            file_response = await self._api_request("getFile", {"file_id": file_id})
            
            if not file_response.get("ok"):
                error_msg = file_response.get("description", "Unknown error")
                self.logger.error(f"Failed to get file info for {file_id}: {error_msg}")
                return False, None, error_msg
                
            file_info = file_response["result"]
            file_path = file_info.get("file_path")
            
            if not file_path:
                return False, None, "No file path in response"
                
            # Download file content
            download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            
            async with self.session.get(download_url) as response:
                if response.status == 200:
                    content = await response.read()
                    self.logger.debug(f"Successfully downloaded file {file_id} ({len(content)} bytes)")
                    return True, content, None
                else:
                    error_msg = f"HTTP {response.status}: {await response.text()}"
                    self.logger.error(f"Failed to download file {file_id}: {error_msg}")
                    return False, None, error_msg
                    
        except Exception as e:
            self.logger.error(f"Exception during download of {file_id}: {e}")
            return False, None, str(e)
            
    async def _api_request(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make a Telegram API request."""
        url = f"{self.api_url}/{method}"
        
        async with self.session.post(url, json=params or {}) as response:
            return await response.json()
            
    async def _api_request_form(self, method: str, data: aiohttp.FormData) -> Dict[str, Any]:
        """Make a Telegram API request with form data."""
        url = f"{self.api_url}/{method}"
        
        async with self.session.post(url, data=data) as response:
            return await response.json()


class TelegramManager:
    """Manages multiple Telegram bots with round-robin distribution."""
    
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.bots: List[TelegramBot] = []
        self.current_bot_index = 0
        self.logger = logging.getLogger(__name__)
        
    async def initialize(self):
        """Initialize all configured bots."""
        for bot_config in self.config.bot_configs:
            bot = TelegramBot(
                token=bot_config.token,
                chat_id=bot_config.chat_id,
                index=bot_config.index,
                config=self.config
            )
            await bot.initialize()
            self.bots.append(bot)
            
        self.logger.info(f"Initialized {len(self.bots)} Telegram bots")
        
    async def cleanup(self):
        """Clean up all bot resources."""
        for bot in self.bots:
            await bot.cleanup()
            
    async def test_all_bots(self) -> List[Dict[str, Any]]:
        """Test connection and permissions for all bots."""
        results = []
        
        for bot in self.bots:
            test_result = await bot.test_connection()
            results.append({
                "bot_index": bot.index,
                "status": test_result["status"],
                "message": test_result.get("message", ""),
                "error": test_result.get("error", "")
            })
            
        return results
        
    def _get_bot_for_segment(self, segment_name: str) -> TelegramBot:
        """Get the bot that should handle a specific segment using consistent hashing."""
        # Use hash of segment name to determine bot (ensures same segment always goes to same bot)
        segment_hash = hashlib.md5(segment_name.encode()).hexdigest()
        bot_index = int(segment_hash, 16) % len(self.bots)
        return self.bots[bot_index]
        
    def _get_next_bot(self) -> TelegramBot:
        """Get the next bot in round-robin fashion."""
        bot = self.bots[self.current_bot_index]
        self.current_bot_index = (self.current_bot_index + 1) % len(self.bots)
        return bot
        
    async def upload_segment(self, segment_path: Path, segment_name: str) -> UploadResult:
        """Upload a video segment to Telegram using consistent hashing."""
        # Always use the same bot for the same segment name (bot isolation)
        bot = self._get_bot_for_segment(segment_name)
        
        self.logger.debug(f"Uploading {segment_name} to bot {bot.index}")
        
        result = await bot.upload_file(segment_path, segment_name)
        
        if result.success:
            # Store in database with bot isolation info
            await self.db_manager.store_segment_metadata(
                segment_name=segment_name,
                file_id=result.file_id,
                bot_index=bot.index,
                file_size=segment_path.stat().st_size
            )
            
        return result
        
    async def download_segment(self, segment_name: str) -> Tuple[bool, Optional[bytes], Optional[str]]:
        """Download a video segment from Telegram with bot isolation enforcement."""
        # Get segment metadata from database
        segment_info = await self.db_manager.get_segment_metadata(segment_name)
        
        if not segment_info:
            return False, None, f"Segment {segment_name} not found in database"
            
        # Enforce bot isolation - only the bot that uploaded can download
        required_bot_index = segment_info["bot_index"]
        bot = None
        
        for b in self.bots:
            if b.index == required_bot_index:
                bot = b
                break
                
        if not bot:
            return False, None, f"Bot {required_bot_index} not available for segment {segment_name}"
            
        self.logger.debug(f"Bot isolation enforced: downloading {segment_name} from bot {bot.index}")
        
        success, content, error = await bot.download_file(segment_info["file_id"])
        
        if success:
            self.logger.debug(f"Successfully downloaded {segment_name} ({len(content)} bytes)")
        else:
            self.logger.error(f"Failed to download {segment_name}: {error}")
            
        return success, content, error
        
    async def upload_file_distributed(self, file_path: Path, custom_filename: Optional[str] = None) -> UploadResult:
        """Upload a regular file using round-robin distribution."""
        bot = self._get_next_bot()
        filename = custom_filename or file_path.name
        
        self.logger.debug(f"Uploading {filename} to bot {bot.index}")
        
        return await bot.upload_file(file_path, filename)
        
    async def get_bot_stats(self) -> Dict[str, Any]:
        """Get statistics about bot usage."""
        stats = {
            "total_bots": len(self.bots),
            "bots": []
        }
        
        for bot in self.bots:
            bot_segments = await self.db_manager.get_segments_by_bot(bot.index)
            total_size = sum(segment["file_size"] for segment in bot_segments)
            
            stats["bots"].append({
                "index": bot.index,
                "chat_id": bot.chat_id,
                "segment_count": len(bot_segments),
                "total_size_mb": total_size / (1024 * 1024),
                "status": "active"  # Could be enhanced with health checks
            })
            
        return stats
        
    def get_available_bots(self) -> List[int]:
        """Get list of available bot indices."""
        return [bot.index for bot in self.bots]
        
    async def health_check(self) -> Dict[str, Any]:
        """Perform health check on all bots."""
        health_status = {
            "healthy_bots": 0,
            "total_bots": len(self.bots),
            "bot_status": []
        }
        
        for bot in self.bots:
            try:
                # Simple health check - get bot info
                response = await bot._api_request("getMe")
                is_healthy = response.get("ok", False)
                
                if is_healthy:
                    health_status["healthy_bots"] += 1
                    
                health_status["bot_status"].append({
                    "bot_index": bot.index,
                    "healthy": is_healthy,
                    "last_check": "now"  # Could be enhanced with timestamps
                })
                
            except Exception as e:
                health_status["bot_status"].append({
                    "bot_index": bot.index,
                    "healthy": False,
                    "error": str(e),
                    "last_check": "now"
                })
                
        health_status["overall_healthy"] = health_status["healthy_bots"] > 0
        
        return health_status