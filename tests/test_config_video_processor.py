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


class TestParseTiers(unittest.TestCase):
    def test_valid_list_format(self):
        result = config._parse_tiers("1080:10M,720:5M,480:2M")
        self.assertEqual(result, [
            {"height": 1080, "bitrate": "10M"},
            {"height": 720, "bitrate": "5M"},
            {"height": 480, "bitrate": "2M"},
        ])

    def test_valid_dict_format(self):
        result = config._parse_tiers("2160:60M,1080:30M", as_dict=True)
        self.assertEqual(result, {2160: "60M", 1080: "30M"})

    def test_none_returns_none(self):
        self.assertIsNone(config._parse_tiers(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(config._parse_tiers(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(config._parse_tiers("   "))

    def test_trailing_commas_ignored(self):
        result = config._parse_tiers("720:5M,")
        self.assertEqual(result, [{"height": 720, "bitrate": "5M"}])

    def test_whitespace_tolerance(self):
        result = config._parse_tiers(" 1080 : 10M , 720 : 5M ")
        self.assertEqual(result, [
            {"height": 1080, "bitrate": "10M"},
            {"height": 720, "bitrate": "5M"},
        ])

    def test_single_entry(self):
        result = config._parse_tiers("360:1200k")
        self.assertEqual(result, [{"height": 360, "bitrate": "1200k"}])

    def test_bad_height_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("abc:10M")

    def test_zero_height_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("0:10M")

    def test_negative_height_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("-720:5M")

    def test_bad_bitrate_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("720:fast")

    def test_bad_format_no_colon_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("720x5M")

    def test_too_many_colons_raises(self):
        with self.assertRaises(ValueError):
            config._parse_tiers("720:5M:extra")

    def test_lowercase_bitrate_suffix(self):
        result = config._parse_tiers("480:2m")
        self.assertEqual(result, [{"height": 480, "bitrate": "2m"}])

    def test_decimal_bitrate(self):
        result = config._parse_tiers("720:5.5M")
        self.assertEqual(result, [{"height": 720, "bitrate": "5.5M"}])


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


class TestSegmentTargetConfig(unittest.TestCase):
    def test_segment_target_size_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            val = config._int_env("SEGMENT_TARGET_SIZE", 15728640)
        self.assertEqual(val, 15728640)

    def test_segment_target_size_reads_env(self):
        with patch.dict(os.environ, {"SEGMENT_TARGET_SIZE": "8388608"}, clear=True):
            val = config._int_env("SEGMENT_TARGET_SIZE", 15728640)
        self.assertEqual(val, 8388608)


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

    # ─── _get_tier0_bitrate ───

    def test_get_tier0_bitrate_1080p(self):
        result = vp._get_tier0_bitrate(1080)
        self.assertEqual(result, "30M")

    def test_get_tier0_bitrate_4k(self):
        result = vp._get_tier0_bitrate(2160)
        self.assertEqual(result, "60M")

    def test_get_tier0_bitrate_720p(self):
        result = vp._get_tier0_bitrate(720)
        self.assertEqual(result, "15M")

    def test_get_tier0_bitrate_480p(self):
        result = vp._get_tier0_bitrate(480)
        self.assertEqual(result, "5M")

    def test_get_tier0_bitrate_unlisted_uses_closest_lower(self):
        # 900p is between 720 and 1080 — should pick 720's bitrate (15M)
        result = vp._get_tier0_bitrate(900)
        self.assertEqual(result, "15M")

    def test_get_tier0_bitrate_below_all_uses_default(self):
        result = vp._get_tier0_bitrate(240)
        self.assertEqual(result, vp.Config.TIER0_BITRATE_DEFAULT)

    # ─── _get_abr_tiers ───

    def test_get_abr_tiers_disabled(self):
        with patch.object(vp.Config, "ABR_ENABLED", False):
            tiers = vp._get_abr_tiers(1080)
        self.assertEqual(tiers, [])

    def test_get_abr_tiers_zero_height(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(0)
        self.assertEqual(tiers, [])

    def test_get_abr_tiers_includes_same_resolution(self):
        # Source is 1080p — should include the 1080p tier (same-res lower bitrate)
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(1080)
        heights = [t["height"] for t in tiers]
        self.assertIn(1080, heights)
        self.assertNotIn(2160, heights)

    def test_get_abr_tiers_720p_includes_720_and_lower(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(720)
        heights = [t["height"] for t in tiers]
        self.assertIn(720, heights)
        self.assertIn(480, heights)
        self.assertIn(360, heights)
        self.assertNotIn(1080, heights)

    def test_get_abr_tiers_4k_includes_all(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(2160)
        heights = [t["height"] for t in tiers]
        for expected_h in [1080, 720, 480, 360]:
            self.assertIn(expected_h, heights)

    def test_get_abr_tiers_360p_includes_360(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(360)
        heights = [t["height"] for t in tiers]
        self.assertIn(360, heights)

    def test_get_abr_tiers_below_all_returns_empty(self):
        with patch.object(vp.Config, "ABR_ENABLED", True):
            tiers = vp._get_abr_tiers(240)
        self.assertEqual(tiers, [])

    # ─── _get_safe_segment_size ───

    def test_get_safe_segment_size_uses_segment_target_when_safe(self):
        with patch.object(vp.Config, "SEGMENT_TARGET_SIZE", 8 * 1024 * 1024), \
             patch.object(vp.Config, "TELEGRAM_MAX_FILE_SIZE", 20 * 1024 * 1024):
            size = vp._get_safe_segment_size("2M")
        self.assertEqual(size, 8 * 1024 * 1024)

    def test_get_safe_segment_size_clamps_to_safe_ceiling(self):
        with patch.object(vp.Config, "SEGMENT_TARGET_SIZE", 19 * 1024 * 1024), \
             patch.object(vp.Config, "TELEGRAM_MAX_FILE_SIZE", 20 * 1024 * 1024):
            size = vp._get_safe_segment_size("8M")
        expected = (
            20 * 1024 * 1024
            - int(vp._parse_bitrate_to_bytes_per_sec("8M") * 2.0)
            - int(20 * 1024 * 1024 * 0.05)
        )
        self.assertEqual(size, expected)

    def test_get_safe_segment_size_has_one_mb_floor(self):
        with patch.object(vp.Config, "SEGMENT_TARGET_SIZE", 2 * 1024 * 1024), \
             patch.object(vp.Config, "TELEGRAM_MAX_FILE_SIZE", 512 * 1024):
            size = vp._get_safe_segment_size("10M")
        self.assertEqual(size, 1024 * 1024)

    # ─── _build_video_cmd ───

    def test_build_video_cmd_always_encodes_cbr(self):
        # Tier 0 should always re-encode with CBR, never copy
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, playlist = vp._build_video_cmd(self.analysis, tmpdir, None,
                                                 target_bitrate="15M")
        cmd_str = " ".join(cmd)
        self.assertNotIn("copy", cmd_str)
        self.assertIn("libx264", cmd_str)
        self.assertIn("-minrate", cmd_str)
        self.assertIn("-maxrate", cmd_str)
        self.assertIn("-bufsize", cmd_str)
        self.assertTrue(playlist.endswith("video.m3u8"))

    def test_build_video_cmd_software_encode_cbr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, None,
                                          target_bitrate="2M")
        cmd_str = " ".join(cmd)
        self.assertIn("libx264", cmd_str)
        # Verify true CBR: -b:v, -minrate, -maxrate, -bufsize all set to same value
        self.assertIn("-b:v 2M", cmd_str)
        self.assertIn("-minrate 2M", cmd_str)
        self.assertIn("-maxrate 2M", cmd_str)
        self.assertIn("-bufsize 2M", cmd_str)

    def test_build_video_cmd_hardware_encode_cbr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, ("h264_vaapi", ["-foo"]),
                                          target_bitrate="2M")
        cmd_str = " ".join(cmd)
        self.assertIn("h264_vaapi", cmd_str)
        self.assertIn("-minrate 2M", cmd_str)

    def test_build_video_cmd_abr_tier_adds_scale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(
                self.analysis, tmpdir, None,
                tier_index=1, target_height=480, target_bitrate="2M",
            )
        self.assertIn("scale=-2:480", " ".join(cmd))

    def test_build_video_cmd_uses_size_based_segmentation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, None,
                                          target_bitrate="5M")
        cmd_str = " ".join(cmd)
        self.assertIn("-hls_segment_size", cmd_str)
        self.assertNotIn("-hls_time", cmd_str)

    def test_build_video_cmd_has_forced_keyframes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, None,
                                          target_bitrate="5M")
        cmd_str = " ".join(cmd)
        self.assertIn("-force_key_frames", cmd_str)

    def test_build_video_cmd_input_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _ = vp._build_video_cmd(
                self.analysis, tmpdir, None,
                tier_index=1, target_height=480, target_bitrate="2M",
                input_override="/tmp/tier0/video.m3u8",
            )
        # Input should be the override, not analysis.file_path
        idx = cmd.index("-i")
        self.assertEqual(cmd[idx + 1], "/tmp/tier0/video.m3u8")

    def test_build_video_cmd_creates_tier_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vp._build_video_cmd(self.analysis, tmpdir, None, tier_index=2,
                                target_bitrate="5M")
            tier_dir = os.path.join(tmpdir, "video_2")
            self.assertTrue(os.path.isdir(tier_dir))

    # ─── _build_audio_cmd ───

    def test_build_audio_cmd_always_encodes_aac(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng",
                                codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, playlist, audio_dir = vp._build_audio_cmd(self.analysis, audio, 0, tmpdir)
            self.assertIn("aac", cmd)
            self.assertNotIn("copy", cmd)
            self.assertTrue(os.path.isdir(audio_dir))

    def test_build_audio_cmd_non_compatible_codec(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=False, language="eng",
                                codec_name="opus", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, _, _ = vp._build_audio_cmd(self.analysis, audio, 0, tmpdir)
            self.assertIn("aac", cmd)

    def test_build_audio_cmd_creates_audio_dir(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng",
                                codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
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
        result.subtitle_files = [("s", "/tmp/out/sub_0", "eng", "", 0, 3)]
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
    def test_process_tier0_uses_cbr_bitrate(self, _detect, _run, _run_with_progress):
        """Verify tier 0 uses resolution-based CBR bitrate, not default VIDEO_BITRATE."""
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=10.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=1920, height=1080)],
                    audio_streams=[],
                    subtitle_streams=[],
                )
                result = vp.process(analysis, "jobt0cbr")
                # Tier 0 should use 30M for 1080p source
                self.assertEqual(result.video_playlists[0][4], "30M")

    @patch("video_processor._run_ffmpeg_with_progress")
    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_lower_tiers_use_tier0_as_input(self, _detect, _run, _run_with_progress):
        """Verify ABR tiers encode from tier 0's playlist output."""
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    duration=10.0,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264",
                                                   is_copy_compatible=True, width=1920, height=1080)],
                    audio_streams=[],
                    subtitle_streams=[],
                )
                vp.process(analysis, "jobt0input")
                # Check that lower tier FFmpeg calls used tier 0 playlist as input
                calls = _run_with_progress.call_args_list
                # First call is tier 0, uses original file
                tier0_cmd = calls[0][0][0]
                self.assertIn("/tmp/in.mp4", tier0_cmd)
                # Subsequent calls should use tier 0 playlist
                if len(calls) > 1:
                    tier1_cmd = calls[1][0][0]
                    # Input should be the tier 0 playlist, not the original
                    input_idx = tier1_cmd.index("-i")
                    self.assertTrue(tier1_cmd[input_idx + 1].endswith("video.m3u8"))

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


class TestUploadSizeGate(unittest.TestCase):
    def test_upload_file_raises_on_oversized(self):
        import asyncio
        from telegram_uploader import TelegramUploader

        uploader = TelegramUploader.__new__(TelegramUploader)
        uploader.bots = [{"bot": Mock(), "channel_id": -1001, "index": 0}]

        with tempfile.TemporaryDirectory() as tmpdir:
            big_file = os.path.join(tmpdir, "segment.ts")
            with open(big_file, "wb") as f:
                f.write(b"x" * 101)

            bot_entry = uploader.bots[0]
            with patch.object(config.Config, "TELEGRAM_MAX_FILE_SIZE", 100):
                with self.assertRaisesRegex(RuntimeError, "exceeds Telegram limit"):
                    asyncio.run(uploader._upload_file(big_file, bot_entry))

    def test_upload_file_does_not_raise_on_exact_limit(self):
        import asyncio
        from telegram_uploader import TelegramUploader

        uploader = TelegramUploader.__new__(TelegramUploader)

        with tempfile.TemporaryDirectory() as tmpdir:
            exact_file = os.path.join(tmpdir, "segment.ts")
            with open(exact_file, "wb") as f:
                f.write(b"x" * 100)

            mock_bot = Mock()
            mock_message = Mock()
            mock_message.document.file_id = "abc123xyz" * 10
            mock_message.document.file_size = 100

            async def mock_send_document(**kwargs):
                return mock_message

            mock_bot.send_document = mock_send_document
            bot_entry = {"bot": mock_bot, "channel_id": -1001, "index": 0}
            uploader.bots = [bot_entry]

            with patch.object(config.Config, "TELEGRAM_MAX_FILE_SIZE", 100):
                # Should not raise — file is exactly at limit
                result = asyncio.run(uploader._upload_file(exact_file, bot_entry))
            self.assertEqual(result.file_size, 100)


if __name__ == "__main__":
    unittest.main()
