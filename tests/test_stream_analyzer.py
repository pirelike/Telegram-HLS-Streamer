import json
import unittest
from unittest.mock import Mock, patch

import stream_analyzer as sa


class TestStreamAnalyzerModels(unittest.TestCase):
    def test_stream_info_repr_defaults(self):
        info = sa.StreamInfo(index=1, codec_type="audio", codec_name="aac")
        self.assertEqual(info.language, "und")
        self.assertEqual(info.title, "")
        self.assertIn("audio:1", repr(info))

    def test_audio_video_subtitle_flags(self):
        audio = sa.AudioStream(index=0, codec_name="aac")
        self.assertTrue(audio.is_copy_compatible)
        self.assertFalse(sa.AudioStream(index=1, codec_name="opus").is_copy_compatible)

        video = sa.VideoStream(index=0, codec_name="h265")
        self.assertEqual(video.codec_name, "hevc")
        self.assertTrue(video.is_copy_compatible)
        self.assertFalse(sa.VideoStream(index=1, codec_name="vp9").is_copy_compatible)

        subtitle = sa.SubtitleStream(index=2, codec_name="srt")
        self.assertTrue(subtitle.is_text_based)
        self.assertFalse(sa.SubtitleStream(index=3, codec_name="hdmv_pgs_subtitle").is_text_based)

    def test_media_analysis_properties_and_summary(self):
        analysis = sa.MediaAnalysis("/tmp/file.mp4", 10.5, 1024)
        self.assertFalse(analysis.has_video)
        self.assertFalse(analysis.has_audio)
        self.assertFalse(analysis.has_subtitles)
        self.assertFalse(analysis.can_copy_video)

        analysis.video_streams.append(sa.VideoStream(index=0, codec_name="h264"))
        analysis.audio_streams.append(sa.AudioStream(index=1, codec_name="aac"))
        analysis.subtitle_streams.append(sa.SubtitleStream(index=2, codec_name="srt"))

        self.assertTrue(analysis.has_video)
        self.assertTrue(analysis.has_audio)
        self.assertTrue(analysis.has_subtitles)
        self.assertTrue(analysis.can_copy_video)

        summary = analysis.summary()
        self.assertEqual(summary["video_tracks"], 1)
        self.assertEqual(summary["audio_tracks"], 1)
        self.assertEqual(summary["subtitle_tracks"], 1)


class TestAnalyze(unittest.TestCase):
    @patch("stream_analyzer.subprocess.run")
    def test_analyze_success(self, mock_run):
        payload = {
            "format": {"duration": "8.0", "size": "256"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "disposition": {"attached_pic": 0},
                    "tags": {"language": "eng", "title": "Main Video"},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 6,
                    "sample_rate": "48000",
                    "tags": {"language": "eng", "title": "Main Audio"},
                },
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "srt",
                    "tags": {"language": "eng", "title": "Subs"},
                },
                {
                    "index": 3,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                },
            ],
        }
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        result = sa.analyze("movie.mp4")

        self.assertEqual(result.duration, 8.0)
        self.assertEqual(result.file_size, 256)
        self.assertEqual(len(result.video_streams), 1)
        self.assertEqual(len(result.audio_streams), 1)
        self.assertEqual(len(result.subtitle_streams), 1)

    @patch("stream_analyzer.subprocess.run", side_effect=FileNotFoundError)
    def test_analyze_ffprobe_missing(self, _):
        with self.assertRaisesRegex(RuntimeError, "ffprobe not found"):
            sa.analyze("movie.mp4")

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_invalid_json(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="not json", stderr="")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            sa.analyze("movie.mp4")

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_ffprobe_error(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="boom")
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            sa.analyze("movie.mp4")


if __name__ == "__main__":
    unittest.main()
