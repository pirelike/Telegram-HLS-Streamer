import json
import unittest
from unittest.mock import Mock, patch

import stream_analyzer as sa


class TestStreamInfoRepr(unittest.TestCase):
    def test_repr_defaults(self):
        info = sa.StreamInfo(index=1, codec_type="audio", codec_name="aac")
        self.assertEqual(info.language, "und")
        self.assertEqual(info.title, "")
        self.assertIn("audio:1", repr(info))

    def test_explicit_language_and_title(self):
        info = sa.StreamInfo(index=0, codec_type="video", codec_name="h264",
                             language="fra", title="French Track")
        self.assertEqual(info.language, "fra")
        self.assertEqual(info.title, "French Track")

    def test_none_language_defaults_to_und(self):
        info = sa.StreamInfo(index=0, codec_type="audio", codec_name="aac", language=None)
        self.assertEqual(info.language, "und")

    def test_none_title_defaults_to_empty(self):
        info = sa.StreamInfo(index=0, codec_type="audio", codec_name="aac", title=None)
        self.assertEqual(info.title, "")


class TestAudioStream(unittest.TestCase):
    def test_aac_is_copy_compatible(self):
        self.assertTrue(sa.AudioStream(index=0, codec_name="aac").is_copy_compatible)

    def test_mp3_is_copy_compatible(self):
        self.assertTrue(sa.AudioStream(index=0, codec_name="mp3").is_copy_compatible)

    def test_opus_is_not_copy_compatible(self):
        self.assertFalse(sa.AudioStream(index=1, codec_name="opus").is_copy_compatible)

    def test_flac_is_not_copy_compatible(self):
        self.assertFalse(sa.AudioStream(index=1, codec_name="flac").is_copy_compatible)

    def test_default_channels_and_sample_rate(self):
        a = sa.AudioStream(index=0, codec_name="aac")
        self.assertEqual(a.channels, 2)
        self.assertEqual(a.sample_rate, 48000)

    def test_custom_channels_and_sample_rate(self):
        a = sa.AudioStream(index=0, codec_name="aac", channels=6, sample_rate=44100)
        self.assertEqual(a.channels, 6)
        self.assertEqual(a.sample_rate, 44100)


class TestVideoStream(unittest.TestCase):
    def test_h264_is_copy_compatible(self):
        self.assertTrue(sa.VideoStream(index=0, codec_name="h264").is_copy_compatible)

    def test_hevc_is_copy_compatible(self):
        self.assertTrue(sa.VideoStream(index=0, codec_name="hevc").is_copy_compatible)

    def test_h265_normalised_to_hevc(self):
        v = sa.VideoStream(index=0, codec_name="h265")
        self.assertEqual(v.codec_name, "hevc")
        self.assertTrue(v.is_copy_compatible)

    def test_vp9_not_copy_compatible(self):
        self.assertFalse(sa.VideoStream(index=0, codec_name="vp9").is_copy_compatible)

    def test_av1_not_copy_compatible(self):
        self.assertFalse(sa.VideoStream(index=0, codec_name="av1").is_copy_compatible)

    def test_default_dimensions_are_zero(self):
        v = sa.VideoStream(index=0, codec_name="h264")
        self.assertEqual(v.width, 0)
        self.assertEqual(v.height, 0)


class TestSubtitleStream(unittest.TestCase):
    def test_srt_is_text_based(self):
        self.assertTrue(sa.SubtitleStream(index=0, codec_name="srt").is_text_based)

    def test_subrip_is_text_based(self):
        self.assertTrue(sa.SubtitleStream(index=0, codec_name="subrip").is_text_based)

    def test_ass_is_text_based(self):
        self.assertTrue(sa.SubtitleStream(index=0, codec_name="ass").is_text_based)

    def test_ssa_is_text_based(self):
        self.assertTrue(sa.SubtitleStream(index=0, codec_name="ssa").is_text_based)

    def test_webvtt_is_text_based(self):
        self.assertTrue(sa.SubtitleStream(index=0, codec_name="webvtt").is_text_based)

    def test_pgs_not_text_based(self):
        self.assertFalse(sa.SubtitleStream(index=0, codec_name="hdmv_pgs_subtitle").is_text_based)

    def test_dvd_sub_not_text_based(self):
        self.assertFalse(sa.SubtitleStream(index=0, codec_name="dvd_subtitle").is_text_based)


class TestMediaAnalysis(unittest.TestCase):
    def test_empty_analysis_properties(self):
        analysis = sa.MediaAnalysis("/tmp/file.mp4", 10.5, 1024)
        self.assertFalse(analysis.has_video)
        self.assertFalse(analysis.has_audio)
        self.assertFalse(analysis.has_subtitles)
        self.assertFalse(analysis.can_copy_video)

    def test_populated_analysis_properties(self):
        analysis = sa.MediaAnalysis("/tmp/file.mp4", 10.5, 1024)
        analysis.video_streams.append(sa.VideoStream(index=0, codec_name="h264"))
        analysis.audio_streams.append(sa.AudioStream(index=1, codec_name="aac"))
        analysis.subtitle_streams.append(sa.SubtitleStream(index=2, codec_name="srt"))

        self.assertTrue(analysis.has_video)
        self.assertTrue(analysis.has_audio)
        self.assertTrue(analysis.has_subtitles)
        self.assertTrue(analysis.can_copy_video)

    def test_can_copy_video_false_for_incompatible_codec(self):
        analysis = sa.MediaAnalysis("/tmp/file.mp4", 5.0, 512)
        analysis.video_streams.append(sa.VideoStream(index=0, codec_name="vp9"))
        self.assertFalse(analysis.can_copy_video)

    def test_summary_counts(self):
        analysis = sa.MediaAnalysis("/tmp/file.mp4", 12.0, 2048)
        analysis.video_streams.append(sa.VideoStream(index=0, codec_name="h264"))
        analysis.audio_streams.append(sa.AudioStream(index=1, codec_name="aac"))
        analysis.audio_streams.append(sa.AudioStream(index=2, codec_name="mp3"))
        analysis.subtitle_streams.append(sa.SubtitleStream(index=3, codec_name="srt"))

        summary = analysis.summary()
        self.assertEqual(summary["video_tracks"], 1)
        self.assertEqual(summary["audio_tracks"], 2)
        self.assertEqual(summary["subtitle_tracks"], 1)
        self.assertEqual(summary["duration"], 12.0)
        self.assertEqual(summary["file_size"], 2048)

    def test_summary_file_path(self):
        analysis = sa.MediaAnalysis("/some/path.mkv", 0, 0)
        self.assertEqual(analysis.summary()["file"], "/some/path.mkv")


class TestAnalyze(unittest.TestCase):
    @patch("stream_analyzer.subprocess.run")
    def test_analyze_full_success(self, mock_run):
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
        self.assertEqual(len(result.video_streams), 1)  # mjpeg album art filtered out
        self.assertEqual(len(result.audio_streams), 1)
        self.assertEqual(len(result.subtitle_streams), 1)
        self.assertEqual(result.video_streams[0].width, 1920)
        self.assertEqual(result.audio_streams[0].channels, 6)
        self.assertEqual(result.subtitle_streams[0].codec_name, "srt")

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_multiple_audio_streams(self, mock_run):
        payload = {
            "format": {"duration": "60.0", "size": "10000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264",
                 "width": 1280, "height": 720, "disposition": {"attached_pic": 0}, "tags": {}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac",
                 "channels": 2, "sample_rate": "48000", "tags": {"language": "eng"}},
                {"index": 2, "codec_type": "audio", "codec_name": "aac",
                 "channels": 6, "sample_rate": "48000", "tags": {"language": "fra"}},
            ],
        }
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        result = sa.analyze("multi_audio.mkv")

        self.assertEqual(len(result.audio_streams), 2)
        self.assertEqual(result.audio_streams[0].language, "eng")
        self.assertEqual(result.audio_streams[1].language, "fra")
        self.assertEqual(result.audio_streams[1].channels, 6)

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_missing_tags_defaults(self, mock_run):
        payload = {
            "format": {"duration": "5.0", "size": "500"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "hevc",
                 "width": 1280, "height": 720, "disposition": {}, "tags": {}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac",
                 "channels": 2, "sample_rate": "48000", "tags": {}},
            ],
        }
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        result = sa.analyze("notags.mp4")

        self.assertEqual(result.video_streams[0].language, "und")
        self.assertEqual(result.video_streams[0].title, "")
        self.assertEqual(result.audio_streams[0].language, "und")

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_no_streams(self, mock_run):
        payload = {"format": {"duration": "0", "size": "0"}, "streams": []}
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        result = sa.analyze("empty.mp4")

        self.assertFalse(result.has_video)
        self.assertFalse(result.has_audio)
        self.assertFalse(result.has_subtitles)

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_unknown_stream_types_ignored(self, mock_run):
        payload = {
            "format": {"duration": "10.0", "size": "1000"},
            "streams": [
                {"index": 0, "codec_type": "data", "codec_name": "bin_data", "tags": {}},
                {"index": 1, "codec_type": "attachment", "codec_name": "ttf", "tags": {}},
            ],
        }
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        result = sa.analyze("data.mp4")

        self.assertFalse(result.has_video)
        self.assertFalse(result.has_audio)
        self.assertFalse(result.has_subtitles)

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

    @patch("stream_analyzer.subprocess.run")
    def test_analyze_zero_duration_and_size(self, mock_run):
        payload = {"format": {}, "streams": []}
        mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        result = sa.analyze("bare.mp4")
        self.assertEqual(result.duration, 0.0)
        self.assertEqual(result.file_size, 0)


if __name__ == "__main__":
    unittest.main()
