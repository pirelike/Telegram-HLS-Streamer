import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import database
import hls_manager


class DatabaseHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.db")

    def close(self):
        self.temp.cleanup()


class TestDatabaseAndHLS(unittest.TestCase):
    def setUp(self):
        self.harness = DatabaseHarness()
        self.db_patch = patch.object(database, "DB_PATH", self.harness.db_path)
        self.db_patch.start()
        database._local = threading.local()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.harness.close()

    def _sample_payload(self):
        analysis = SimpleNamespace(
            file_path="/tmp/sample.mp4",
            duration=12.0,
            file_size=1200,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=1280, height=720)],
            audio_streams=[SimpleNamespace(codec_name="aac")],
            subtitle_streams=[SimpleNamespace(codec_name="srt")],
        )
        processing = SimpleNamespace(
            audio_playlists=[("a.m3u8", "/tmp/a", "eng", "English", 2)],
            subtitle_files=[("s.vtt", "/tmp/s", "eng", "English")],
        )
        upload = SimpleNamespace(
            segments={
                "video/video_0001.ts": SimpleNamespace(file_id="f1", bot_index=0, file_size=100),
                "audio_0/audio_0001.ts": SimpleNamespace(file_id="f2", bot_index=1, file_size=50),
                "sub_0/subtitles.vtt": SimpleNamespace(file_id="f3", bot_index=0, file_size=10),
            }
        )
        return analysis, processing, upload

    def test_get_conn_reuses_thread_connection(self):
        c1 = database._get_conn()
        c2 = database._get_conn()
        self.assertIs(c1, c2)

    def test_save_and_query_job(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        job = database.get_job("job1")
        self.assertIsNotNone(job)
        self.assertEqual(job["filename"], "sample.mp4")

        tracks = database.get_job_tracks("job1")
        self.assertEqual(len(tracks), 2)
        self.assertEqual(len(database.get_job_tracks("job1", "audio")), 1)

        seg = database.get_segment("job1", "video/video_0001.ts")
        self.assertEqual(seg["file_id"], "f1")

        keys = database.get_segments_for_prefix("job1", "audio_0")
        self.assertEqual(keys, ["audio_0/audio_0001.ts"])

        jobs = database.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["segment_count"], 3)

    def test_delete_job(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job2", analysis, processing, upload)
        database.delete_job("job2")
        self.assertIsNone(database.get_job("job2"))

    def test_save_job_rolls_back_on_error(self):
        analysis, processing, upload = self._sample_payload()
        bad_upload = SimpleNamespace(segments={"video/video_0001.ts": SimpleNamespace(file_id=None, bot_index=0, file_size=1)})
        with self.assertRaises(Exception):
            database.save_job("bad", analysis, processing, bad_upload)
        self.assertIsNone(database.get_job("bad"))

    def test_hls_manager_register_get_list_and_segment(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)

        self.assertEqual(hls_manager.get_job("job3")["job_id"], "job3")
        listing = hls_manager.list_jobs()
        self.assertIn("job3", listing)
        self.assertEqual(listing["job3"]["audio_tracks"][0]["language"], "eng")

        seg = hls_manager.get_segment_info("job3", "video/video_0001.ts")
        self.assertEqual(seg["bot_index"], 0)

    def test_generate_master_playlist_variants(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job4", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("job4", "https://cdn.example")
        self.assertIn("#EXTM3U", playlist)
        self.assertIn('TYPE=AUDIO', playlist)
        self.assertIn('TYPE=SUBTITLES', playlist)
        self.assertIn('/hls/job4/video.m3u8', playlist)

        self.assertIsNone(hls_manager.generate_master_playlist("missing", "https://cdn.example"))

    def test_generate_media_playlist_and_subtitle_playlist(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)

        video_playlist = hls_manager.generate_media_playlist("job5", "video")
        self.assertIn("/segment/job5/video/video_0001.ts", video_playlist)

        audio_playlist = hls_manager.generate_media_playlist("job5", "audio", 0)
        self.assertIn("audio_0/audio_0001.ts", audio_playlist)

        sub_playlist = hls_manager.generate_media_playlist("job5", "sub", 0)
        self.assertIn("sub_0/subtitles.vtt", sub_playlist)

        self.assertIsNone(hls_manager.generate_media_playlist("job5", "audio", 99))
        self.assertIsNone(hls_manager.generate_media_playlist("missing", "video"))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "badtype"))

    def test_generate_subtitle_playlist_without_file(self):
        analysis, processing, upload = self._sample_payload()
        upload.segments.pop("sub_0/subtitles.vtt")
        hls_manager.register_job("job6", analysis, processing, upload)

        job = database.get_job("job6")
        result = hls_manager._generate_subtitle_playlist("job6", job, 0)
        self.assertIn("#EXT-X-ENDLIST", result)
        self.assertNotIn("#EXTINF", result)


if __name__ == "__main__":
    unittest.main()
