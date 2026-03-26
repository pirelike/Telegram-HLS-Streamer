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
telegram_request_mod = types.ModuleType("telegram.request")


class StubRetryAfter(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class StubNetworkError(Exception):
    pass


class StubTimedOut(Exception):
    pass


class StubBadRequest(Exception):
    pass


class StubForbidden(Exception):
    pass


telegram_mod.Bot = Mock
telegram_request_mod.HTTPXRequest = Mock
telegram_error_mod.RetryAfter = StubRetryAfter
telegram_error_mod.NetworkError = StubNetworkError
telegram_error_mod.TimedOut = StubTimedOut
telegram_error_mod.BadRequest = StubBadRequest
telegram_error_mod.Forbidden = StubForbidden
sys.modules.setdefault("telegram", telegram_mod)
sys.modules.setdefault("telegram.error", telegram_error_mod)
sys.modules.setdefault("telegram.request", telegram_request_mod)

import telegram_uploader as tu


class FakeRetryAfter(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class TestUploadedSegmentAndUploadResult(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_segment_fields(self):
        seg = tu.UploadedSegment("fileXYZ", 2, "a.ts", 1024)
        self.assertEqual(seg.file_id, "fileXYZ")
        self.assertEqual(seg.bot_index, 2)
        self.assertEqual(seg.file_size, 1024)

    async def test_upload_result_defaults(self):
        res = tu.UploadResult("jobABC")
        self.assertEqual(res.job_id, "jobABC")
        self.assertEqual(res.total_files, 0)
        self.assertEqual(res.total_bytes, 0)
        self.assertEqual(res.segments, {})


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

    # ─── _next_bot ───

    async def test_next_bot_round_robin(self):
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

    async def test_next_bot_single_bot_always_returns_same(self):
        self.uploader.bots = self.uploader.bots[:1]
        self.assertEqual(self.uploader._next_bot()["index"], 0)
        self.assertEqual(self.uploader._next_bot()["index"], 0)

    async def test_probe_health_all_bots_healthy(self):
        self.bot_instances[0].get_chat = AsyncMock(return_value=Mock())
        self.bot_instances[1].get_chat = AsyncMock(return_value=Mock())

        result = await self.uploader.probe_health()

        self.assertEqual([bot["ok"] for bot in result], [True, True])
        self.assertEqual([bot["index"] for bot in result], [0, 1])

    async def test_probe_health_mixed_results(self):
        self.bot_instances[0].get_chat = AsyncMock(return_value=Mock())
        self.bot_instances[1].get_chat = AsyncMock(side_effect=tu.Forbidden("blocked"))

        result = await self.uploader.probe_health()

        self.assertTrue(result[0]["ok"])
        self.assertFalse(result[1]["ok"])
        self.assertEqual(result[1]["error"], "forbidden")

    async def test_probe_health_generic_error_uses_stable_string(self):
        self.bot_instances[0].get_chat = AsyncMock(side_effect=RuntimeError("bad token"))
        self.bot_instances[1].get_chat = AsyncMock(return_value=Mock())

        result = await self.uploader.probe_health()

        self.assertFalse(result[0]["ok"])
        self.assertIn("runtimeerror", result[0]["error"])

    # ─── _upload_file success ───

    async def test_upload_file_success(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name

        try:
            message = Mock(document=Mock(file_id="file123", file_size=3))
            self.bot_instances[0].send_document = AsyncMock(return_value=message)
            result = await self.uploader._upload_file(path, self.uploader.bots[0])
            self.assertEqual(result.file_id, "file123")
            self.assertEqual(result.bot_index, 0)
            self.assertEqual(result.file_size, 3)
        finally:
            os.unlink(path)

    # ─── _upload_file error cases ───

    async def test_upload_file_bad_request_raises_runtime_error(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.BadRequest("bad"))
            with self.assertRaises(RuntimeError):
                await self.uploader._upload_file(path, self.uploader.bots[0])
        finally:
            os.unlink(path)

    async def test_upload_file_unauthorized_raises_runtime_error(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.Forbidden("nope"))
            with self.assertRaises(RuntimeError):
                await self.uploader._upload_file(path, self.uploader.bots[0])
        finally:
            os.unlink(path)

    async def test_upload_file_timed_out_exhausts_retries(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.TimedOut("timeout"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)
        finally:
            os.unlink(path)

    async def test_upload_file_network_error_exhausts_retries(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.bot_instances[0].send_document = AsyncMock(side_effect=tu.NetworkError("net"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)
        finally:
            os.unlink(path)

    async def test_upload_file_generic_exception_exhausts_retries(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            self.bot_instances[0].send_document = AsyncMock(side_effect=Exception("boom"))
            with patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(Exception):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)
        finally:
            os.unlink(path)

    async def test_upload_file_retry_after_exhausts_retries(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            with patch("telegram_uploader.RetryAfter", FakeRetryAfter), \
                 patch("telegram_uploader.asyncio.sleep", new=AsyncMock()):
                self.bot_instances[0].send_document = AsyncMock(side_effect=FakeRetryAfter(0))
                with self.assertRaises(RuntimeError):
                    await self.uploader._upload_file(path, self.uploader.bots[0], retries=1)
        finally:
            os.unlink(path)

    async def test_upload_file_succeeds_on_retry_via_mock(self):
        """Retry logic: patching _upload_file_with_bot_lock to succeed on 2nd call."""
        call_count = 0

        async def upload_side_effect(path, bot_entry, cancel_event=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return tu.UploadedSegment("ok", 0, "f.ts", 5)

        with patch.object(self.uploader, "_upload_file_with_bot_lock",
                           side_effect=upload_side_effect):
            with self.assertRaises(RuntimeError):
                # upload_files does not retry — each file is tried once
                result = await self.uploader.upload_files([("k/f.ts", "/tmp/f.ts")])

        # Reset and test that success on first call works
        call_count = 0

        async def success_side_effect(path, bot_entry, cancel_event=None):
            return tu.UploadedSegment("ok", 0, "f.ts", 5)

        with patch.object(self.uploader, "_upload_file_with_bot_lock",
                           side_effect=success_side_effect):
            result = await self.uploader.upload_files([("k/f.ts", "/tmp/f.ts")])
        self.assertEqual(result["k/f.ts"].file_id, "ok")

    # ─── _upload_file_with_bot_lock ───

    async def test_upload_file_with_bot_lock_delegates(self):
        mock_seg = tu.UploadedSegment("f", 0, "x", 1)
        with patch.object(self.uploader, "_upload_file", new=AsyncMock(return_value=mock_seg)) as mocked:
            result = await self.uploader._upload_file_with_bot_lock("/tmp/f", self.uploader.bots[0])
        self.assertEqual(result.file_id, "f")
        mocked.assert_awaited_once()

    # ─── upload_files ───

    async def test_upload_files_empty_returns_empty_dict(self):
        self.assertEqual(await self.uploader.upload_files([]), {})

    async def test_upload_files_calls_progress_callback(self):
        async def fake_upload(path, bot_entry, cancel_event=None):
            return tu.UploadedSegment(f"fid-{os.path.basename(path)}", bot_entry["index"],
                                      os.path.basename(path), 5)

        with patch.object(self.uploader, "_upload_file_with_bot_lock", side_effect=fake_upload):
            progress = []
            out = await self.uploader.upload_files(
                [("video/a.ts", "/tmp/a.ts"), ("video/b.ts", "/tmp/b.ts")],
                lambda c, t, k: progress.append((c, t, k)),
            )
        self.assertEqual(len(out), 2)
        self.assertEqual(progress[-1][0], 2)  # current == total at end

    async def test_upload_files_maps_keys_to_segments(self):
        async def fake_upload(path, bot_entry, cancel_event=None):
            return tu.UploadedSegment(f"id-{os.path.basename(path)}", 0,
                                      os.path.basename(path), 10)

        with patch.object(self.uploader, "_upload_file_with_bot_lock", side_effect=fake_upload):
            out = await self.uploader.upload_files([
                ("video/seg1.ts", "/tmp/seg1.ts"),
                ("audio_0/seg2.ts", "/tmp/seg2.ts"),
            ])
        self.assertIn("video/seg1.ts", out)
        self.assertIn("audio_0/seg2.ts", out)

    async def test_upload_files_cancels_others_on_error(self):
        """Verify that when one task fails, others are cancelled and awaited."""
        first_fail = True
        others_cancelled = False

        async def mock_upload(path, bot_entry, cancel_event=None):
            nonlocal first_fail, others_cancelled
            if "fail.ts" in path:
                raise RuntimeError("forced failure")

            try:
                # Wait longer than the failure take to trigger
                await asyncio.sleep(1)
                return tu.UploadedSegment("ok", 0, "ok.ts", 5)
            except asyncio.CancelledError:
                others_cancelled = True
                raise

        with patch.object(self.uploader, "_upload_file_with_bot_lock", side_effect=mock_upload):
            files = [("k1", "/tmp/fail.ts"), ("k2", "/tmp/ok.ts")]
            with self.assertRaises(RuntimeError):
                await self.uploader.upload_files(files)

        self.assertTrue(others_cancelled, "Other tasks should have been cancelled")

    async def test_upload_file_check_existence(self):
        """_upload_file should raise FileNotFoundError if file is missing."""
        path = "/tmp/non_existent_segment_xyz.ts"
        with self.assertRaises(FileNotFoundError):
            await self.uploader._upload_file(path, self.uploader.bots[0])


    async def test_upload_file_cancel_event_aborts_before_attempt(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            path = f.name
        try:
            cancel_event = asyncio.Event()
            cancel_event.set()
            with self.assertRaises(asyncio.CancelledError):
                await self.uploader._upload_file(path, self.uploader.bots[0], cancel_event=cancel_event)
        finally:
            os.unlink(path)

    async def test_upload_files_cancelled_cleans_up_tasks(self):
        async def fake_upload(path, bot_entry, cancel_event=None):
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

        with patch.object(self.uploader, "_upload_file_with_bot_lock", side_effect=fake_upload):
            with self.assertRaises(asyncio.CancelledError):
                await self.uploader.upload_files([("video/a.ts", "/tmp/a.ts")], cancel_event=asyncio.Event())

    # ─── upload_job ───

    async def test_upload_job_collects_all_files(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "out")
            video_dir = os.path.join(out_dir, "video_0")
            audio_dir = os.path.join(root, "audio_0")
            os.makedirs(video_dir, exist_ok=True)
            os.makedirs(audio_dir, exist_ok=True)

            for path in [os.path.join(video_dir, "video_0001.ts"),
                         os.path.join(audio_dir, "audio_0001.ts")]:
                with open(path, "wb") as fh:
                    fh.write(b"abc")

            vtt = os.path.join(root, "subtitles.vtt")
            with open(vtt, "wb") as fh:
                fh.write(b"WEBVTT")

            proc = Mock(
                job_id="job",
                output_dir=out_dir,
                video_playlists=[(os.path.join(video_dir, "video.m3u8"), video_dir, 1280, 720, "2500k")],
                audio_playlists=[("a.m3u8", audio_dir, "eng", "English", 2)],
                subtitle_files=[(vtt, os.path.dirname(vtt), "eng", "English", 0, 3)],
            )

            async def fake_upload_files(files, cb=None, cancel_event=None):
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

    async def test_upload_job_no_callback(self):
        with tempfile.TemporaryDirectory() as root:
            video_dir = os.path.join(root, "video_0")
            os.makedirs(video_dir, exist_ok=True)
            ts = os.path.join(video_dir, "video_0001.ts")
            with open(ts, "wb") as fh:
                fh.write(b"x")

            proc = Mock(
                job_id="jobnocb",
                output_dir=root,
                video_playlists=[(os.path.join(video_dir, "video.m3u8"), video_dir, 1280, 720, "2M")],
                audio_playlists=[],
                subtitle_files=[],
            )

            async def fake_upload_files(files, cb=None, cancel_event=None):
                return {k: tu.UploadedSegment("id", 0, "f", 1) for k, _ in files}

            with patch.object(self.uploader, "upload_files", side_effect=fake_upload_files):
                result = await self.uploader.upload_job(proc)

            self.assertEqual(result.total_files, 1)

    # ─── upload_document ───

    async def test_upload_document_uses_first_bot(self):
        segment = tu.UploadedSegment("doc123", 0, "db_export.json", 4)
        with patch.object(self.uploader, "_upload_file_with_bot_lock", new=AsyncMock(return_value=segment)) as upload_mock:
            result = await self.uploader.upload_document(b"test", "db_export.json")

        self.assertEqual(result["file_id"], "doc123")
        self.assertEqual(result["bot_index"], 0)
        self.assertEqual(result["file_size"], 4)
        self.assertEqual(upload_mock.await_args.args[1]["index"], 0)

    # ─── get_file_url / get_file_bytes ───

    async def test_get_file_url(self):
        fake_file = Mock(file_path="http://file")
        self.bot_instances[0].get_file = AsyncMock(return_value=fake_file)
        url = await self.uploader.get_file_url("A" * 50, 0)
        self.assertEqual(url, "http://file")

    async def test_get_file_bytes(self):
        fake_file = Mock(file_path="http://file")
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"x"))
        self.bot_instances[0].get_file = AsyncMock(return_value=fake_file)
        data = await self.uploader.get_file_bytes("A" * 50, 0)
        self.assertEqual(bytes(data), b"x")

    async def test_get_file_url_out_of_range_raises(self):
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            await self.uploader.get_file_url("A" * 50, 99)

    async def test_get_file_bytes_negative_index_raises(self):
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            await self.uploader.get_file_bytes("A" * 50, -1)

    async def test_get_file_bytes_second_bot(self):
        fake_file = Mock(file_path="http://f2")
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"y"))
        self.bot_instances[1].get_file = AsyncMock(return_value=fake_file)
        data = await self.uploader.get_file_bytes("B" * 50, 1)
        self.assertEqual(bytes(data), b"y")


class TestTelegramUploaderMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN_1": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "TELEGRAM_CHANNEL_ID_1": "-1001",
        }, clear=True):
            from config import Config
            Config.load_bots()
        self.uploader = tu.TelegramUploader()

    def test_get_metrics_returns_all_keys(self):
        m = self.uploader.get_metrics()
        for key in ("upload_count", "upload_errors", "upload_total_seconds",
                    "download_count", "download_errors", "download_total_seconds"):
            self.assertIn(key, m, msg=f"metrics missing key: {key}")

    def test_get_metrics_initial_values_are_zero(self):
        m = self.uploader.get_metrics()
        for key, val in m.items():
            self.assertEqual(val, 0 if isinstance(val, int) else 0.0)

    def test_record_metric_success_increments_count_and_time(self):
        self.uploader._record_metric("upload", 1.5)
        m = self.uploader.get_metrics()
        self.assertEqual(m["upload_count"], 1)
        self.assertAlmostEqual(m["upload_total_seconds"], 1.5)
        self.assertEqual(m["upload_errors"], 0)

    def test_record_metric_error_increments_errors_not_count(self):
        self.uploader._record_metric("download", 0.5, error=True)
        m = self.uploader.get_metrics()
        self.assertEqual(m["download_errors"], 1)
        self.assertEqual(m["download_count"], 0)
        self.assertEqual(m["download_total_seconds"], 0.0)

    def test_get_metrics_returns_copy(self):
        m1 = self.uploader.get_metrics()
        m1["upload_count"] = 999
        m2 = self.uploader.get_metrics()
        self.assertEqual(m2["upload_count"], 0)


class TestReloadBots(unittest.TestCase):
    def test_reload_bots_updates_bot_list(self):
        with patch.object(tu.Config, "BOTS", [{"token": "t1", "channel_id": -1001}]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()
                self.assertEqual(len(uploader.bots), 1)

        # Change Config.BOTS to two entries and reload
        with patch.object(tu.Config, "BOTS", [
            {"token": "t1", "channel_id": -1001},
            {"token": "t2", "channel_id": -1002},
        ]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader.reload_bots()
                self.assertEqual(len(uploader.bots), 2)
                self.assertEqual(uploader.bots[0]["channel_id"], -1001)
                self.assertEqual(uploader.bots[1]["channel_id"], -1002)

    def test_reload_bots_resets_counter(self):
        with patch.object(tu.Config, "BOTS", [
            {"token": "t1", "channel_id": -1001},
            {"token": "t2", "channel_id": -1002},
        ]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()
                uploader._bot_counter = 5
                uploader.reload_bots()
                self.assertEqual(uploader._bot_counter, 0)

    def test_reload_bots_resets_locks(self):
        with patch.object(tu.Config, "BOTS", [{"token": "t1", "channel_id": -1001}]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()
                uploader._bot_locks = ["fake_lock"]
                uploader.reload_bots()
                self.assertIsNone(uploader._bot_locks)

    def test_reload_bots_preserves_old_bot_index_locking_capacity(self):
        with patch.object(tu.Config, "BOTS", [
            {"token": "t1", "channel_id": -1001},
            {"token": "t2", "channel_id": -1002},
        ]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()

        # Simulate an in-flight upload that already chose bot index 1.
        old_bot_entry = {"bot": Mock(), "channel_id": -1002, "index": 1}

        with patch.object(tu.Config, "BOTS", [{"token": "t1", "channel_id": -1001}]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader.reload_bots()

        locks = uploader._get_or_create_bot_locks(required_index=old_bot_entry["index"])
        self.assertGreaterEqual(len(locks), 2)

    def test_get_or_create_bot_locks_initializes_once_and_reuses_list(self):
        with patch.object(tu.Config, "BOTS", [{"token": "t1", "channel_id": -1001}]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()
                locks_first = uploader._get_or_create_bot_locks()
                locks_second = uploader._get_or_create_bot_locks()
                self.assertIs(locks_first, locks_second)
                self.assertEqual(len(locks_first), 1)

    def test_reload_bots_empty_config(self):
        with patch.object(tu.Config, "BOTS", [{"token": "t1", "channel_id": -1001}]):
            with patch("telegram_uploader.Bot", Mock()), patch("telegram_uploader.HTTPXRequest", Mock()):
                uploader = tu.TelegramUploader()

        with patch.object(tu.Config, "BOTS", []):
            uploader.reload_bots()
            self.assertEqual(uploader.bots, [])


if __name__ == "__main__":
    unittest.main()
