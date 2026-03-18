import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


# Provide a lightweight telegram stub so tests don't depend on exact library versions.
telegram_mod = types.ModuleType("telegram")
telegram_error_mod = types.ModuleType("telegram.error")


class StubRetryAfter(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class StubNetworkError(Exception):
    pass


class StubTimedOut(Exception):
    pass


class StubBadRequest(Exception):
    pass


class StubUnauthorized(Exception):
    pass


telegram_mod.Bot = Mock
telegram_error_mod.RetryAfter = StubRetryAfter
telegram_error_mod.NetworkError = StubNetworkError
telegram_error_mod.TimedOut = StubTimedOut
telegram_error_mod.BadRequest = StubBadRequest
telegram_error_mod.Unauthorized = StubUnauthorized
sys.modules.setdefault("telegram", telegram_mod)
sys.modules.setdefault("telegram.error", telegram_error_mod)

import telegram_uploader as tu


class FakeRetryAfter(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class TestTelegramUploader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_cfg_patch = patch.object(tu.Config, "BOTS", [
            {"token": "t1", "channel_id": -1001},
            {"token": "t2", "channel_id": -1002},
        ])
        self.bot_cfg_patch.start()

        bot1 = Mock()
        bot2 = Mock()
        self.bot_instances = [bot1, bot2]
        self.bot_patch = patch("telegram_uploader.Bot", side_effect=self.bot_instances)
        self.bot_patch.start()

        self.uploader = tu.TelegramUploader()

    def tearDown(self):
        self.bot_patch.stop()
        self.bot_cfg_patch.stop()

    async def test_constructors_and_next_bot(self):
        seg = tu.UploadedSegment("f", 0, "a.ts", 1)
        self.assertEqual(seg.file_id, "f")

        res = tu.UploadResult("job")
        self.assertEqual(res.job_id, "job")
        self.assertEqual(res.total_files, 0)

        b1 = self.uploader._next_bot()
        b2 = self.uploader._next_bot()
        b3 = self.uploader._next_bot()
        self.assertEqual(b1["index"], 0)
        self.assertEqual(b2["index"], 1)
        self.assertEqual(b3["index"], 0)

    async def test_next_bot_without_bots_raises(self):
        self.uploader.bots = []
        with self.assertRaisesRegex(RuntimeError, "No Telegram bots"):
            self.uploader._next_bot()

    async def test_upload_file_success_and_errors(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name

        try:
            message = Mock(document=Mock(file_id="file123"))
            self.bot_instances[0].send_document = AsyncMock(return_value=message)
            result = await self.uploader._upload_file(path, self.uploader.bots[0])
            self.assertEqual(result.file_id, "file123")

            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.BadRequest("bad"))
            with self.assertRaises(RuntimeError):
                await self.uploader._upload_file(path, self.uploader.bots[0])

            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.Unauthorized("nope"))
            with self.assertRaises(RuntimeError):
                await self.uploader._upload_file(path, self.uploader.bots[0])

            self.bot_instances[0].send_document = AsyncMock(side_effect=Exception("boom"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(Exception):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)

            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.TimedOut("timeout"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)

            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.NetworkError("net"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)

            with patch("telegram_uploader.RetryAfter", FakeRetryAfter), patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                self.bot_instances[0].send_document = AsyncMock(side_effect=FakeRetryAfter(0))
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)
        finally:
            os.unlink(path)

    async def test_upload_file_with_bot_lock_delegates(self):
        mock_seg = tu.UploadedSegment("f", 0, "x", 1)
        with patch.object(self.uploader, "_upload_file", new=AsyncMock(return_value=mock_seg)) as mocked:
            result = await self.uploader._upload_file_with_bot_lock("/tmp/f", self.uploader.bots[0])
        self.assertEqual(result.file_id, "f")
        mocked.assert_awaited_once()

    async def test_upload_files_and_upload_job(self):
        async def fake_upload(path, bot_entry):
            return tu.UploadedSegment(f"fid-{os.path.basename(path)}", bot_entry["index"], os.path.basename(path), 5)

        with patch.object(self.uploader, "_upload_file_with_bot_lock", side_effect=fake_upload):
            progress = []
            out = await self.uploader.upload_files(
                [("video/a.ts", "/tmp/a.ts"), ("video/b.ts", "/tmp/b.ts")],
                lambda c, t, k: progress.append((c, t, k)),
            )
            self.assertEqual(len(out), 2)
            self.assertEqual(progress[-1][0], 2)

        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "out")
            audio_dir = os.path.join(root, "audio_0")
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(audio_dir, exist_ok=True)

            for path in [os.path.join(out_dir, "video_0001.ts"), os.path.join(audio_dir, "audio_0001.ts")]:
                with open(path, "wb") as f:
                    f.write(b"abc")

            vtt = os.path.join(root, "subtitles.vtt")
            with open(vtt, "wb") as f:
                f.write(b"WEBVTT")

            proc = Mock(
                job_id="job",
                output_dir=out_dir,
                video_playlist=os.path.join(out_dir, "video.m3u8"),
                audio_playlists=[("a.m3u8", audio_dir, "eng", "English", 2)],
                subtitle_files=[(vtt, os.path.dirname(vtt), "eng", "English")],
            )

            async def fake_upload_files(files, cb=None):
                result = {}
                for idx, (k, p) in enumerate(files, start=1):
                    if cb:
                        cb(idx, len(files), k)
                    result[k] = tu.UploadedSegment(f"id-{k}", idx % 2, os.path.basename(p), 10)
                return result

            with patch.object(self.uploader, "upload_files", side_effect=fake_upload_files):
                updates = []
                result = await self.uploader.upload_job(proc, lambda c, t, n: updates.append((c, t, n)))

            self.assertEqual(result.total_files, 3)
            self.assertEqual(result.total_bytes, 30)
            self.assertEqual(len(updates), 3)

    async def test_upload_files_empty(self):
        self.assertEqual(await self.uploader.upload_files([]), {})

    async def test_get_file_url_and_bytes(self):
        fake_file = Mock(file_path="http://file")
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"x"))
        self.bot_instances[0].get_file = AsyncMock(return_value=fake_file)

        url = await self.uploader.get_file_url("id", 0)
        self.assertEqual(url, "http://file")

        data = await self.uploader.get_file_bytes("id", 0)
        self.assertEqual(bytes(data), b"x")

        with self.assertRaisesRegex(RuntimeError, "out of range"):
            await self.uploader.get_file_url("id", 99)

        with self.assertRaisesRegex(RuntimeError, "out of range"):
            await self.uploader.get_file_bytes("id", -1)


if __name__ == "__main__":
    unittest.main()
