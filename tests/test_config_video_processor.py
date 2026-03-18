import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import video_processor as vp


class TestIntEnv(unittest.TestCase):
    def test_missing_env_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            val = config._int_env("NONEXISTENT_VAR", 42)
        self.assertEqual(val, 42)

    def test_valid_integer_env(self):
        with patch.dict(os.environ, {"MY_INT": "99"}):
            val = config._int_env("MY_INT", 0)
        self.assertEqual(val, 99)

    def test_invalid_integer_env_uses_default(self):
        with patch.dict(os.environ, {"MY_INT": "notanint"}):
            val = config._int_env("MY_INT", 7)
        self.assertEqual(val, 7)


class TestConfigLoadBots(unittest.TestCase):
    def test_load_bots_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            bots = config.Config.load_bots()
        self.assertEqual(bots, [])

    def test_load_bots_valid_single(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_1": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                "TELEGRAM_CHANNEL_ID_1": "-1001",
            },
            clear=True,
        ):
            bots = config.Config.load_bots()
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["channel_id"], -1001)

    def test_load_bots_skips_placeholder_token(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_1": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                "TELEGRAM_CHANNEL_ID_1": "-1001",
                "TELEGRAM_BOT_TOKEN_2": "your_token",
                "TELEGRAM_CHANNEL_ID_2": "-1002",
            },
            clear=True,
        ):
            bots = config.Config.load_bots()
        self.assertEqual(len(bots), 1)

    def test_load_bots_invalid_token_format(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_1": "badtoken", "TELEGRAM_CHANNEL_ID_1": "-1001"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                config.Config.load_bots()

    def test_load_bots_invalid_channel_type(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_1": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
             "TELEGRAM_CHANNEL_ID_1": "abc"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "expected integer"):
                config.Config.load_bots()

    def test_load_bots_requires_negative_channel_id(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_1": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
             "TELEGRAM_CHANNEL_ID_1": "100"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "expected negative integer"):
                config.Config.load_bots()


class TestVideoProcessorHelpers(unittest.TestCase):
    def setUp(self):
        vp._hw_encoder_probed = False
        vp._hw_encoder_cache = None
        self.analysis = SimpleNamespace(
            file_path="/tmp/in.mp4",
            has_video=True,
            audio_streams=[],
            subtitle_streams=[],
            video_streams=[SimpleNamespace(index=0, codec_name="h264", is_copy_compatible=True,
                                           width=1280, height=720)],
        )

    def tearDown(self):
        # Reset cache so other tests aren't affected
        vp._hw_encoder_probed = False
        vp._hw_encoder_cache = None

    # ─── _detect_hw_encoder ───

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_disabled(self, mock_run):
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", False):
            result = vp._detect_hw_encoder()
        self.assertIsNone(result)
        mock_run.assert_not_called()

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_success_vaapi(self, mock_run):
        mock_run.return_value = Mock(stdout="h264_vaapi other stuff", returncode=0)
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), \
             patch.object(vp.Config, "PREFERRED_ENCODER", "vaapi"):
            enc = vp._detect_hw_encoder()
        self.assertEqual(enc[0], "h264_vaapi")
        self.assertIn("-vaapi_device", enc[1])

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_not_found_in_output(self, mock_run):
        mock_run.return_value = Mock(stdout="", returncode=0)
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), \
             patch.object(vp.Config, "PREFERRED_ENCODER", "vaapi"):
            result = vp._detect_hw_encoder()
        self.assertIsNone(result)

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_result_is_cached(self, mock_run):
        mock_run.return_value = Mock(stdout="h264_vaapi", returncode=0)
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), \
             patch.object(vp.Config, "PREFERRED_ENCODER", "vaapi"):
            r1 = vp._detect_hw_encoder()
            r2 = vp._detect_hw_encoder()  # second call uses cache
        self.assertIs(r1, r2)
        self.assertEqual(mock_run.call_count, 1)

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_unknown_preferred_returns_none(self, mock_run):
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), \
             patch.object(vp.Config, "PREFERRED_ENCODER", "unknown_encoder"):
            result = vp._detect_hw_encoder()
        self.assertIsNone(result)
        mock_run.assert_not_called()

    # ─── _get_abr_tiers ───

    def test_get_abr_tiers_disabled(self):
        with patch.object(vp.Config, "ABR_ENABLED", False):
            tiers = vp._get_abr_tiers(1080)
        self.assertEqual(tiers, [])

    def test_get_abr_tiers_zero_height(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(0)
        self.assertEqual(tiers, [])

    def test_get_abr_tiers_excludes_source_and_higher(self):
        # Source is 720p — only tiers strictly below 720 should be included
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(720)
        heights = [t["height"] for t in tiers]
        self.assertNotIn(720, heights)
        self.assertNotIn(1080, heights)
        for h in heights:
            self.assertLess(h, 720)

    def test_get_abr_tiers_4k_includes_all_lower(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(2160)
        heights = [t["height"] for t in tiers]
        # All four standard tiers should be included
        for expected_h in [1080, 720, 480, 360]:
            self.assertIn(expected_h, heights)

    def test_get_abr_tiers_360p_no_lower_tiers(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(360)
        self.assertEqual(tiers, [])

    # ─── _build_video_cmd ───

    def test_build_video_cmd_copy_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                cmd, playlist = vp._build_video_cmd(self.analysis, tmpdir, None)
        self.assertIn("copy", cmd)
        self.assertTrue(playlist.endswith("video.m3u8"))

    def test_build_video_cmd_software_encode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", False), \
                 patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, None)
        self.assertIn("libx264", cmd)

    def test_build_video_cmd_hardware_encode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", False), \
                 patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, ("h264_vaapi", ["-foo"]))
        self.assertIn("h264_vaapi", cmd)

    def test_build_video_cmd_abr_tier_adds_scale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", False), \
                 patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                cmd, _ = vp._build_video_cmd(
                    self.analysis, tmpdir, None,
                    tier_index=1, target_height=480, target_bitrate="2M",
                )
        self.assertIn("scale=-2:480", " ".join(cmd))

    def test_build_video_cmd_copy_mode_disabled_for_abr_tier(self):
        # Even with ENABLE_COPY_MODE=True, a tier with target_height must re-encode
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True), \
                 patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                cmd, _ = vp._build_video_cmd(
                    self.analysis, tmpdir, None,
                    tier_index=1, target_height=480, target_bitrate="2M",
                )
        self.assertNotIn("copy", cmd)

    def test_build_video_cmd_creates_tier_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                vp._build_video_cmd(self.analysis, tmpdir, None, tier_index=2)
            tier_dir = os.path.join(tmpdir, "video_2")
            self.assertTrue(os.path.isdir(tier_dir))

    # ─── _build_audio_cmd ───

    def test_build_audio_cmd_copy_mode(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng",
                                codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                cmd, playlist, audio_dir = vp._build_audio_cmd(self.analysis, audio, 0, tmpdir)
            self.assertIn("copy", cmd)
            self.assertTrue(os.path.isdir(audio_dir))

    def test_build_audio_cmd_aac_encode(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=False, language="eng",
                                codec_name="opus", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                cmd, _, _ = vp._build_audio_cmd(self.analysis, audio, 0, tmpdir)
            self.assertIn("aac", cmd)

    def test_build_audio_cmd_copy_mode_disabled_forces_encode(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng",
                                codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", False):
                cmd, _, _ = vp._build_audio_cmd(self.analysis, audio, 1, tmpdir)
            self.assertIn("aac", cmd)

    def test_build_audio_cmd_creates_audio_dir(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng",
                                codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                _, _, audio_dir = vp._build_audio_cmd(self.analysis, audio, 3, tmpdir)
            self.assertTrue(os.path.isdir(audio_dir))
            self.assertTrue(audio_dir.endswith("audio_3"))

    # ─── _extract_subtitle ───

    def test_extract_subtitle_returns_webvtt_cmd(self):
        sub = SimpleNamespace(index=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, vtt_file, sub_dir = vp._extract_subtitle(self.analysis, sub, 0, tmpdir)
            self.assertIn("webvtt", cmd)
            self.assertTrue(vtt_file.endswith("subtitles.vtt"))
            self.assertTrue(os.path.isdir(sub_dir))

    def test_extract_subtitle_index_in_dir_name(self):
        sub = SimpleNamespace(index=5)
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, sub_dir = vp._extract_subtitle(self.analysis, sub, 5, tmpdir)
            self.assertTrue(sub_dir.endswith("sub_5"))

    # ─── _run_ffmpeg ───

    @patch("video_processor.subprocess.run")
    def test_run_ffmpeg_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stderr="")
        proc = vp._run_ffmpeg(["ffmpeg", "-version"], "test")
        self.assertEqual(proc.returncode, 0)

    @patch("video_processor.subprocess.run")
    def test_run_ffmpeg_failure_raises(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stderr="error text")
        with self.assertRaisesRegex(RuntimeError, "FFmpeg failed"):
            vp._run_ffmpeg(["ffmpeg"], "desc")

    @patch("video_processor.subprocess.run")
    def test_run_ffmpeg_timeout_raises(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=1)
        with self.assertRaisesRegex(RuntimeError, "FFmpeg timed out"):
            vp._run_ffmpeg(["ffmpeg"], "desc")

    # ─── _run_ffmpeg_with_progress ───

    @patch("video_processor._run_ffmpeg")
    def test_run_ffmpeg_with_progress_no_callback_delegates(self, mock_run_ffmpeg):
        mock_run_ffmpeg.return_value = Mock(returncode=0)
        vp._run_ffmpeg_with_progress(["ffmpeg"], "desc", duration_seconds=10, step_progress_cb=None)
        mock_run_ffmpeg.assert_called_once()

    @patch("video_processor._run_ffmpeg")
    def test_run_ffmpeg_with_progress_zero_duration_delegates(self, mock_run_ffmpeg):
        mock_run_ffmpeg.return_value = Mock(returncode=0)
        callback = Mock()
        vp._run_ffmpeg_with_progress(["ffmpeg"], "desc", duration_seconds=0, step_progress_cb=callback)
        mock_run_ffmpeg.assert_called_once()

    # ─── ProcessingResult ───

    def test_processing_result_video_playlist_property_empty(self):
        result = vp.ProcessingResult("id", "/tmp/out")
        self.assertIsNone(result.video_playlist)

    def test_processing_result_video_playlist_property_with_entry(self):
        result = vp.ProcessingResult("id", "/tmp/out")
        result.video_playlists = [("/tmp/out/video_0/video.m3u8", "/tmp/out/video_0", 1280, 720, "2500k")]
        self.assertEqual(result.video_playlist, "/tmp/out/video_0/video.m3u8")

    def test_processing_result_all_segment_dirs(self):
        result = vp.ProcessingResult("id", "/tmp/out")
        result.video_playlists = [("/tmp/out/video_0/video.m3u8", "/tmp/out/video_0", 1280, 720, "2500k")]
        result.audio_playlists = [("a", "/tmp/out/audio_0", "eng", "", 2)]
        result.subtitle_files = [("s", "/tmp/out/sub_0", "eng", "")]
        dirs = result.all_segment_dirs()
        self.assertEqual(dirs, ["/tmp/out/video_0", "/tmp/out/audio_0", "/tmp/out/sub_0"])

    def test_processing_result_all_segment_dirs_empty(self):
        result = vp.ProcessingResult("id", "/tmp/out")
        self.assertEqual(result.all_segment_dirs(), [])

    # ─── process() ───

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_basic_pipeline(self, _detect, _run, _run_with_progress):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=12.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=1280, height=720)],
                    audio_streams=[SimpleNamespace(index=1, is_copy_compatible=True,
                                                   language="eng", title="", codec_name="aac", channels=2)],
                    subtitle_streams=[
                        SimpleNamespace(index=2, is_text_based=True, language="eng", title=""),
                        SimpleNamespace(index=3, is_text_based=False, language="eng", title="",
                                        codec_name="dvd_subtitle"),
                    ],
                )
                progress = []
                result = vp.process(analysis, "jobx", lambda c, t, n: progress.append((c, t, n)))

                self.assertGreaterEqual(len(result.video_playlists), 1)
                self.assertEqual(len(result.audio_playlists), 1)
                self.assertEqual(len(result.subtitle_files), 1)
                self.assertGreater(len(progress), 0)

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_no_video_no_subtitle(self, _detect, _run, _run_with_progress):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/audio_only.mp4",
                    has_video=False,
                    duration=5.0,
                    video_streams=[],
                    audio_streams=[SimpleNamespace(index=0, is_copy_compatible=True,
                                                   language="eng", title="", codec_name="aac", channels=2)],
                    subtitle_streams=[],
                )
                result = vp.process(analysis, "jobaudio")

                self.assertEqual(len(result.video_playlists), 0)
                self.assertEqual(len(result.audio_playlists), 1)
                self.assertEqual(len(result.subtitle_files), 0)

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_progress_callback_none(self, _detect, _run, _run_with_progress):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=10.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=640, height=480)],
                    audio_streams=[],
                    subtitle_streams=[],
                )
                # Should not raise even with no progress callback
                result = vp.process(analysis, "jobnoprog", progress_callback=None)
                self.assertGreaterEqual(len(result.video_playlists), 1)

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg", side_effect=RuntimeError("sub extract fail"))
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_subtitle_extraction_failure_is_skipped(self, _detect, _run, _run_with_progress):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=10.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=1280, height=720)],
                    audio_streams=[],
                    subtitle_streams=[SimpleNamespace(index=2, is_text_based=True,
                                                      language="eng", title="", codec_name="srt")],
                )
                # _run_ffmpeg raises for subtitles; process should skip and not propagate
                result = vp.process(analysis, "jobskipsub")
                self.assertEqual(len(result.subtitle_files), 0)

    # ─── cleanup() ───

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_cleanup_removes_processing_dir(self, _detect, _run, _run_with_progress):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=12.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=1280, height=720)],
                    audio_streams=[],
                    subtitle_streams=[],
                )
                vp.process(analysis, "jobclean")
                job_dir = os.path.join(proc_dir, "jobclean")
                self.assertTrue(os.path.exists(job_dir))

                vp.cleanup("jobclean")
                self.assertFalse(os.path.exists(job_dir))

    def test_cleanup_nonexistent_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                vp.cleanup("no_such_job")  # should not raise


if __name__ == "__main__":
    unittest.main()
