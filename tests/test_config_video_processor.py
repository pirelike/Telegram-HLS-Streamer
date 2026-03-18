import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import video_processor as vp


class TestConfig(unittest.TestCase):
    def test_load_bots_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            bots = config.Config.load_bots()
        self.assertEqual(bots, [])

    def test_load_bots_valid_and_skips_placeholder(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_1": "token1",
                "TELEGRAM_CHANNEL_ID_1": "-1001",
                "TELEGRAM_BOT_TOKEN_2": "your_token",
                "TELEGRAM_CHANNEL_ID_2": "-1002",
            },
            clear=True,
        ):
            bots = config.Config.load_bots()
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["channel_id"], -1001)

    def test_load_bots_invalid_channel_type(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_1": "token1", "TELEGRAM_CHANNEL_ID_1": "abc"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "expected integer"):
                config.Config.load_bots()

    def test_load_bots_requires_negative_channel_id(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN_1": "token1", "TELEGRAM_CHANNEL_ID_1": "100"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "expected negative integer"):
                config.Config.load_bots()


class TestVideoProcessor(unittest.TestCase):
    def setUp(self):
        self.analysis = SimpleNamespace(
            file_path="/tmp/in.mp4",
            has_video=True,
            audio_streams=[],
            subtitle_streams=[],
            video_streams=[SimpleNamespace(index=0, codec_name="h264", is_copy_compatible=True)],
        )

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_disabled_or_missing(self, mock_run):
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", False):
            self.assertIsNone(vp._detect_hw_encoder())

        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), patch.object(vp.Config, "PREFERRED_ENCODER", "vaapi"):
            mock_run.return_value = Mock(stdout="", returncode=0)
            self.assertIsNone(vp._detect_hw_encoder())

    @patch("video_processor.subprocess.run")
    def test_detect_hw_encoder_success(self, mock_run):
        mock_run.return_value = Mock(stdout="h264_vaapi", returncode=0)
        with patch.object(vp.Config, "ENABLE_HW_ACCEL", True), patch.object(vp.Config, "PREFERRED_ENCODER", "vaapi"):
            enc = vp._detect_hw_encoder()
        self.assertEqual(enc[0], "h264_vaapi")

    def test_build_video_cmd_variants(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                cmd, playlist = vp._build_video_cmd(self.analysis, tmpdir, None)
            self.assertIn("copy", cmd)
            self.assertTrue(playlist.endswith("video.m3u8"))

            with patch.object(vp.Config, "ENABLE_COPY_MODE", False), patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                hw_cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, ("h264_vaapi", ["-foo"]))
            self.assertIn("h264_vaapi", hw_cmd)

            with patch.object(vp.Config, "ENABLE_COPY_MODE", False), patch.object(vp.Config, "VIDEO_BITRATE", "2M"):
                sw_cmd, _ = vp._build_video_cmd(self.analysis, tmpdir, None)
            self.assertIn("libx264", sw_cmd)

    def test_build_audio_cmd_and_extract_subtitle(self):
        audio = SimpleNamespace(index=1, is_copy_compatible=True, language="eng", codec_name="aac", channels=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(vp.Config, "ENABLE_COPY_MODE", True):
                cmd, _, audio_dir = vp._build_audio_cmd(self.analysis, audio, 0, tmpdir)
            self.assertIn("-c:a", cmd)
            self.assertIn("copy", cmd)
            self.assertTrue(os.path.isdir(audio_dir))

            with patch.object(vp.Config, "ENABLE_COPY_MODE", False):
                cmd2, _, _ = vp._build_audio_cmd(self.analysis, audio, 1, tmpdir)
            self.assertIn("aac", cmd2)

            sub = SimpleNamespace(index=2)
            sub_cmd, sub_file, sub_dir = vp._extract_subtitle(self.analysis, sub, 0, tmpdir)
            self.assertIn("webvtt", sub_cmd)
            self.assertTrue(sub_file.endswith("subtitles.vtt"))
            self.assertTrue(os.path.isdir(sub_dir))

    @patch("video_processor.subprocess.run")
    def test_run_ffmpeg_success_and_failure(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stderr="")
        self.assertEqual(vp._run_ffmpeg(["ffmpeg"], "desc").returncode, 0)

        mock_run.return_value = Mock(returncode=1, stderr="failure")
        with self.assertRaisesRegex(RuntimeError, "FFmpeg failed"):
            vp._run_ffmpeg(["ffmpeg"], "desc")

    def test_processing_result_dirs(self):
        result = vp.ProcessingResult("id", "/tmp/out")
        result.video_playlist = "/tmp/out/video.m3u8"
        result.audio_playlists = [("a", "/tmp/out/audio_0", "eng", "", 2)]
        result.subtitle_files = [("s", "/tmp/out/sub_0", "eng", "")]
        self.assertEqual(result.all_segment_dirs(), ["/tmp/out", "/tmp/out/audio_0", "/tmp/out/sub_0"])

    @patch("video_processor._run_ffmpeg")
    @patch("video_processor._detect_hw_encoder", return_value=None)
    def test_process_and_cleanup(self, _detect, _run):
        with tempfile.TemporaryDirectory() as proc_dir:
            with patch.object(vp.Config, "PROCESSING_DIR", proc_dir):
                analysis = SimpleNamespace(
                    file_path="/tmp/in.mp4",
                    has_video=True,
                    video_streams=[SimpleNamespace(index=0, codec_name="h264", is_copy_compatible=True)],
                    audio_streams=[SimpleNamespace(index=1, is_copy_compatible=True, language="eng", title="", codec_name="aac", channels=2)],
                    subtitle_streams=[
                        SimpleNamespace(index=2, is_text_based=True, language="eng", title=""),
                        SimpleNamespace(index=3, is_text_based=False, language="eng", title="", codec_name="dvd_subtitle"),
                    ],
                )

                progress = []
                result = vp.process(analysis, "jobx", lambda c, t, n: progress.append((c, t, n)))
                self.assertTrue(result.video_playlist.endswith("video.m3u8"))
                self.assertEqual(len(result.audio_playlists), 1)
                self.assertEqual(len(result.subtitle_files), 1)
                self.assertGreater(len(progress), 0)

                vp.cleanup("jobx")
                self.assertFalse(os.path.exists(os.path.join(proc_dir, "jobx")))


if __name__ == "__main__":
    unittest.main()
