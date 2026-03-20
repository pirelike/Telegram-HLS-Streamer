import os
import tempfile
import threading
import time
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


class TestDatabaseBase(unittest.TestCase):
    def setUp(self):
        self.harness = DatabaseHarness()
        self.db_patch = patch.object(database, "DB_PATH", self.harness.db_path)
        self.db_patch.start()
        database._close_all_connections()
        database._local = threading.local()
        database.init_db()

    def tearDown(self):
        database._close_all_connections()
        self.db_patch.stop()
        self.harness.close()

    def _sample_payload(self, job_id="job1"):
        analysis = SimpleNamespace(
            file_path=f"/tmp/{job_id}.mp4",
            duration=12.0,
            file_size=1200,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=1280, height=720, index=0)],
            audio_streams=[SimpleNamespace(codec_name="aac", index=1)],
            subtitle_streams=[SimpleNamespace(codec_name="srt", index=3)],
        )
        processing = SimpleNamespace(
            video_playlists=[("video.m3u8", "/tmp/video", 1280, 720, "2500k")],
            audio_playlists=[("a.m3u8", "/tmp/a", "eng", "English", 2)],
            subtitle_files=[("s.vtt", "/tmp/s", "eng", "English", 0, 3)],
            segment_durations={},
        )
        upload = SimpleNamespace(
            segments={
                "video/video_0001.ts": SimpleNamespace(file_id="f1", bot_index=0, file_size=100),
                "audio_0/audio_0001.ts": SimpleNamespace(file_id="f2", bot_index=1, file_size=50),
                "sub_0/subtitles.vtt": SimpleNamespace(file_id="f3", bot_index=0, file_size=10),
            }
        )
        return analysis, processing, upload


class TestDatabaseConnections(TestDatabaseBase):
    def test_get_conn_reuses_thread_connection(self):
        c1 = database._get_conn()
        c2 = database._get_conn()
        self.assertIs(c1, c2)

    def test_get_conn_different_threads_different_connections(self):
        conns = []

        def grab():
            conns.append(database._get_conn())

        t = threading.Thread(target=grab)
        t.start()
        t.join()

        main_conn = database._get_conn()
        self.assertIsNotNone(conns[0])
        self.assertIsNot(main_conn, conns[0])

    def test_close_conn_clears_local(self):
        c1 = database._get_conn()
        database.close_conn()
        c2 = database._get_conn()
        # After close, a fresh connection is created
        self.assertIsNotNone(c2)
        # They may or may not be the same object depending on sqlite internals,
        # but the old one should have been closed. Simply verify no exception.

    def test_close_conn_idempotent_when_no_connection(self):
        database.close_conn()
        database.close_conn()  # second call should not raise

    def test_open_connection_count_tracks_live_connections(self):
        self.assertEqual(database.open_connection_count(), 1)  # init_db opened main-thread conn
        database.close_conn()
        self.assertEqual(database.open_connection_count(), 0)

        database._get_conn()
        self.assertEqual(database.open_connection_count(), 1)


class TestDatabaseCRUD(TestDatabaseBase):
    def test_save_and_get_job(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        job = database.get_job("job1")
        self.assertIsNotNone(job)
        self.assertEqual(job["filename"], "job1.mp4")
        self.assertAlmostEqual(job["duration"], 12.0)
        self.assertEqual(job["file_size"], 1200)
        self.assertEqual(job["video_codec"], "h264")
        self.assertEqual(job["video_width"], 1280)
        self.assertEqual(job["video_height"], 720)

    def test_get_job_missing_returns_none(self):
        self.assertIsNone(database.get_job("nonexistent"))

    def test_get_job_tracks_all_types(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        tracks = database.get_job_tracks("job1")
        self.assertEqual(len(tracks), 3)

    def test_get_job_tracks_filtered_by_type(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        self.assertEqual(len(database.get_job_tracks("job1", "video")), 1)
        self.assertEqual(len(database.get_job_tracks("job1", "audio")), 1)
        self.assertEqual(len(database.get_job_tracks("job1", "subtitle")), 1)

    def test_get_job_tracks_video_has_dimensions(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        video_tracks = database.get_job_tracks("job1", "video")
        self.assertEqual(video_tracks[0]["width"], 1280)
        self.assertEqual(video_tracks[0]["height"], 720)
        self.assertEqual(video_tracks[0]["bitrate"], "2500k")

    def test_get_segment_found(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        seg = database.get_segment("job1", "video/video_0001.ts")
        self.assertEqual(seg["file_id"], "f1")
        self.assertEqual(seg["bot_index"], 0)

    def test_get_segment_missing_returns_none(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        self.assertIsNone(database.get_segment("job1", "nonexistent/key.ts"))

    def test_get_segments_for_prefix(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        segments = database.get_segments_for_prefix("job1", "audio_0")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["segment_key"], "audio_0/audio_0001.ts")
        self.assertEqual(segments[0]["duration"], 0)

    def test_get_segments_for_prefix_empty(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        self.assertEqual(database.get_segments_for_prefix("job1", "noexist"), [])

    def test_list_jobs_includes_counts(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)

        jobs = database.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["segment_count"], 3)
        self.assertEqual(jobs[0]["audio_count"], 1)
        self.assertEqual(jobs[0]["subtitle_count"], 1)

    def test_list_jobs_pagination(self):
        for i in range(5):
            analysis, processing, upload = self._sample_payload(f"job{i}")
            database.save_job(f"job{i}", analysis, processing, upload)

        page1 = database.list_jobs(limit=2, offset=0)
        page2 = database.list_jobs(limit=2, offset=2)
        all_jobs = database.list_jobs(limit=10, offset=0)

        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertEqual(len(all_jobs), 5)
        # No overlap between pages
        ids1 = {j["job_id"] for j in page1}
        ids2 = {j["job_id"] for j in page2}
        self.assertEqual(len(ids1 & ids2), 0)

    def test_count_jobs(self):
        self.assertEqual(database.count_jobs(), 0)

        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        self.assertEqual(database.count_jobs(), 1)

        analysis2, processing2, upload2 = self._sample_payload("job2")
        database.save_job("job2", analysis2, processing2, upload2)
        self.assertEqual(database.count_jobs(), 2)

    def test_delete_job(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        database.delete_job("job1")
        self.assertIsNone(database.get_job("job1"))
        self.assertEqual(database.count_jobs(), 0)

    def test_delete_job_cascades_segments_and_tracks(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        database.delete_job("job1")

        self.assertEqual(database.get_job_tracks("job1"), [])
        self.assertIsNone(database.get_segment("job1", "video/video_0001.ts"))

    def test_delete_nonexistent_job_does_not_raise(self):
        database.delete_job("nonexistent")  # should not raise

    def test_save_job_rolls_back_on_error(self):
        analysis, processing, upload = self._sample_payload()
        bad_upload = SimpleNamespace(
            segments={"video/video_0001.ts": SimpleNamespace(file_id=None, bot_index=0, file_size=1)}
        )
        with self.assertRaises(Exception):
            database.save_job("bad", analysis, processing, bad_upload)
        self.assertIsNone(database.get_job("bad"))

    def test_delete_old_jobs_zero_days_does_nothing(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        count = database.delete_old_jobs(0)
        self.assertEqual(count, 0)
        self.assertIsNotNone(database.get_job("job1"))

    def test_delete_old_jobs_negative_days_does_nothing(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        count = database.delete_old_jobs(-5)
        self.assertEqual(count, 0)

    def test_delete_old_jobs_future_cutoff_deletes_nothing(self):
        # A cutoff of 999 days means jobs must be older than 999 days — nothing should match
        analysis, processing, upload = self._sample_payload()
        database.save_job("job1", analysis, processing, upload)
        count = database.delete_old_jobs(999)
        self.assertEqual(count, 0)
        self.assertIsNotNone(database.get_job("job1"))

    def test_save_job_no_video(self):
        analysis = SimpleNamespace(
            file_path="/tmp/audio_only.mp4",
            duration=5.0,
            file_size=500,
            has_video=False,
            video_streams=[],
            audio_streams=[SimpleNamespace(codec_name="aac", index=1)],
            subtitle_streams=[],
        )
        processing = SimpleNamespace(
            video_playlists=[],
            audio_playlists=[("a.m3u8", "/tmp/a", "eng", "", 2)],
            subtitle_files=[],
            segment_durations={},
        )
        upload = SimpleNamespace(
            segments={"audio_0/audio_0001.ts": SimpleNamespace(file_id="fA", bot_index=0, file_size=50)}
        )
        database.save_job("jobaudio", analysis, processing, upload)
        job = database.get_job("jobaudio")
        self.assertIsNotNone(job)
        self.assertIsNone(job["video_codec"])
        self.assertEqual(job["video_width"], 0)


class TestHLSHelpers(unittest.TestCase):
    def test_height_to_label_4k(self):
        self.assertEqual(hls_manager._height_to_label(2160), "4K")

    def test_height_to_label_8k(self):
        self.assertEqual(hls_manager._height_to_label(4320), "8K")

    def test_height_to_label_standard(self):
        self.assertEqual(hls_manager._height_to_label(1080), "1080p")
        self.assertEqual(hls_manager._height_to_label(720), "720p")
        self.assertEqual(hls_manager._height_to_label(480), "480p")

    def test_video_tier_name_original(self):
        name = hls_manager._video_tier_name(1080, is_original=True)
        self.assertEqual(name, "Original (1080p)")

    def test_video_tier_name_original_4k(self):
        name = hls_manager._video_tier_name(2160, is_original=True)
        self.assertEqual(name, "Original (4K)")

    def test_video_tier_name_non_original(self):
        name = hls_manager._video_tier_name(720, is_original=False)
        self.assertEqual(name, "720p")

    def test_parse_bitrate_megabits(self):
        self.assertEqual(hls_manager._parse_bitrate("5M"), 5_000_000)
        self.assertEqual(hls_manager._parse_bitrate("2.5M"), 2_500_000)

    def test_parse_bitrate_kilobits(self):
        self.assertEqual(hls_manager._parse_bitrate("1200k"), 1_200_000)
        self.assertEqual(hls_manager._parse_bitrate("600K"), 600_000)

    def test_parse_bitrate_bare_number(self):
        self.assertEqual(hls_manager._parse_bitrate("4000000"), 4_000_000)

    def test_parse_bitrate_empty_string(self):
        self.assertEqual(hls_manager._parse_bitrate(""), 0)

    def test_parse_bitrate_invalid(self):
        self.assertEqual(hls_manager._parse_bitrate("abc"), 0)


class TestHLSManagerWithDB(TestDatabaseBase):
    def test_register_and_get_job(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)

        job = hls_manager.get_job("job3")
        self.assertIsNotNone(job)
        self.assertEqual(job["job_id"], "job3")

    def test_list_jobs_structure(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)

        listing = hls_manager.list_jobs()
        self.assertIn("job3", listing)
        info = listing["job3"]
        self.assertEqual(info["audio_count"], 1)
        self.assertEqual(info["subtitle_count"], 1)
        self.assertEqual(info["segment_count"], 3)
        self.assertIn("filename", info)
        self.assertIn("duration", info)

    def test_count_jobs_via_hls_manager(self):
        self.assertEqual(hls_manager.count_jobs(), 0)
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)
        self.assertEqual(hls_manager.count_jobs(), 1)

    def test_get_segment_info(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)

        seg = hls_manager.get_segment_info("job3", "video/video_0001.ts")
        self.assertEqual(seg["bot_index"], 0)
        self.assertEqual(seg["file_id"], "f1")

    def test_get_segment_info_missing(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job3", analysis, processing, upload)
        self.assertIsNone(hls_manager.get_segment_info("job3", "nope/nope.ts"))

    def test_generate_master_playlist(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job4", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("job4", "https://cdn.example")

        self.assertIn("#EXTM3U", playlist)
        self.assertIn('TYPE=AUDIO', playlist)
        self.assertIn('TYPE=SUBTITLES', playlist)
        self.assertIn('/hls/job4/video_0.m3u8', playlist)
        self.assertIn("https://cdn.example", playlist)

    def test_generate_master_playlist_missing_job(self):
        self.assertIsNone(hls_manager.generate_master_playlist("missing", "https://cdn.example"))

    def test_generate_master_playlist_audio_default_flag(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job4", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("job4", "https://cdn.example")
        # First audio track should be DEFAULT=YES
        self.assertIn('DEFAULT=YES', playlist)

    def test_generate_master_playlist_subtitle_group(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job4", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("job4", "https://cdn.example")
        self.assertIn('SUBTITLES="subs"', playlist)

    def test_generate_master_playlist_legacy_no_video_tracks(self):
        # Job saved without video tracks falls back to legacy video.m3u8
        analysis = SimpleNamespace(
            file_path="/tmp/legacy.mp4",
            duration=10.0,
            file_size=1000,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=640, height=480, index=0)],
            audio_streams=[],
            subtitle_streams=[],
        )
        # No video playlists → no video tracks in DB
        processing = SimpleNamespace(video_playlists=[], audio_playlists=[], subtitle_files=[], segment_durations={})
        upload = SimpleNamespace(segments={
            "video/video_0001.ts": SimpleNamespace(file_id="fL", bot_index=0, file_size=10)
        })
        hls_manager.register_job("legacy", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("legacy", "https://cdn.example")
        self.assertIn("/hls/legacy/video.m3u8", playlist)
        self.assertNotIn("video_0.m3u8", playlist)

    def test_generate_master_playlist_no_audio_or_subtitle(self):
        analysis = SimpleNamespace(
            file_path="/tmp/vid_only.mp4",
            duration=5.0,
            file_size=500,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=1280, height=720, index=0)],
            audio_streams=[],
            subtitle_streams=[],
        )
        processing = SimpleNamespace(
            video_playlists=[("v.m3u8", "/tmp/v", 1280, 720, "2M")],
            audio_playlists=[],
            subtitle_files=[],
            segment_durations={},
        )
        upload = SimpleNamespace(segments={
            "video_0/video_0001.ts": SimpleNamespace(file_id="fV", bot_index=0, file_size=50)
        })
        hls_manager.register_job("vidonly", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("vidonly", "https://cdn.example")
        self.assertNotIn("TYPE=AUDIO", playlist)
        self.assertNotIn("TYPE=SUBTITLES", playlist)

    def test_generate_media_playlist_video(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("job5", "video")
        self.assertIn("/segment/job5/video/video_0001.ts", playlist)
        self.assertIn("#EXTM3U", playlist)
        self.assertIn("#EXT-X-ENDLIST", playlist)

    def test_generate_media_playlist_audio(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("job5", "audio", 0)
        self.assertIn("audio_0/audio_0001.ts", playlist)

    def test_generate_media_playlist_subtitle(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("job5", "sub", 0)
        self.assertIn("sub_0/subtitles.vtt", playlist)
        self.assertIn("#EXT-X-ENDLIST", playlist)

    def test_generate_media_playlist_invalid_stream_index(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)

        self.assertIsNone(hls_manager.generate_media_playlist("job5", "audio", 99))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "audio", "not-an-int"))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "audio", -1))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "video", "not-an-int"))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "video", -1))
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "video", 99))

    def test_generate_media_playlist_missing_job(self):
        self.assertIsNone(hls_manager.generate_media_playlist("missing", "video"))

    def test_generate_media_playlist_bad_stream_type(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("job5", analysis, processing, upload)
        self.assertIsNone(hls_manager.generate_media_playlist("job5", "badtype"))

    def test_generate_subtitle_playlist_without_segment(self):
        analysis, processing, upload = self._sample_payload()
        upload.segments.pop("sub_0/subtitles.vtt")
        hls_manager.register_job("job6", analysis, processing, upload)

        job = database.get_job("job6")
        result = hls_manager._generate_subtitle_playlist("job6", job, 0)
        self.assertIn("#EXT-X-ENDLIST", result)
        self.assertNotIn("#EXTINF", result)

    def test_generate_media_playlist_video_tier_indexed(self):
        # Tier 0 uses prefix "video_0" so the segment key must match
        analysis, processing, upload = self._sample_payload()
        # Add a video_0 segment to match the indexed lookup
        upload.segments["video_0/video_0001.ts"] = SimpleNamespace(
            file_id="fv0", bot_index=0, file_size=100
        )
        hls_manager.register_job("job5b", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("job5b", "video", 0)
        self.assertIn("/segment/job5b/video_0/video_0001.ts", playlist)


class TestSubtitleTrackIndexMismatch(TestDatabaseBase):
    """P3: subtitle track_index must match the enumerate index (including skipped bitmaps)."""

    def _payload_with_skipped_bitmap_sub(self, job_id="jobsub"):
        """Simulate a file with a bitmap sub at index 0 (skipped) and text sub at index 1."""
        analysis = SimpleNamespace(
            file_path=f"/tmp/{job_id}.mkv",
            duration=10.0,
            file_size=1000,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=1280, height=720, index=0)],
            audio_streams=[SimpleNamespace(codec_name="aac", index=1)],
            subtitle_streams=[
                SimpleNamespace(codec_name="hdmv_pgs_subtitle", index=2),  # bitmap, skipped
                SimpleNamespace(codec_name="srt", index=3),                # text, extracted
            ],
        )
        # video_processor skips index 0 (bitmap) and only appends index 1 (text).
        # The 6-tuple is (vtt_path, sub_dir, lang, title, enum_idx, orig_stream_idx).
        processing = SimpleNamespace(
            video_playlists=[("v.m3u8", "/tmp/v", 1280, 720, "2M")],
            audio_playlists=[("a.m3u8", "/tmp/a", "eng", "English", 2)],
            subtitle_files=[("s.vtt", "/tmp/sub_1", "eng", "English", 1, 3)],
            segment_durations={},
        )
        upload = SimpleNamespace(
            segments={
                "video_0/video_0001.ts": SimpleNamespace(file_id="fv", bot_index=0, file_size=100),
                "audio_0/audio_0001.ts": SimpleNamespace(file_id="fa", bot_index=0, file_size=50),
                "sub_1/subtitles.vtt": SimpleNamespace(file_id="fs", bot_index=0, file_size=10),
            }
        )
        return analysis, processing, upload

    def test_subtitle_track_index_matches_directory_when_bitmap_skipped(self):
        """track_index stored in DB must be enum_idx (1), not sequential (0)."""
        analysis, processing, upload = self._payload_with_skipped_bitmap_sub()
        database.save_job("jobsub", analysis, processing, upload)

        sub_tracks = database.get_job_tracks("jobsub", "subtitle")
        self.assertEqual(len(sub_tracks), 1)
        self.assertEqual(sub_tracks[0]["track_index"], 1,
                         "track_index must be 1 to match sub_1/ directory")

    def test_subtitle_original_stream_index_stored(self):
        """original_stream_index must store the FFprobe stream index."""
        analysis, processing, upload = self._payload_with_skipped_bitmap_sub()
        database.save_job("jobsub", analysis, processing, upload)

        sub_tracks = database.get_job_tracks("jobsub", "subtitle")
        self.assertEqual(sub_tracks[0]["original_stream_index"], 3)

    def test_subtitle_segment_lookup_succeeds_with_correct_index(self):
        """get_segment for sub_1/subtitles.vtt must succeed."""
        analysis, processing, upload = self._payload_with_skipped_bitmap_sub()
        database.save_job("jobsub", analysis, processing, upload)

        seg = database.get_segment("jobsub", "sub_1/subtitles.vtt")
        self.assertIsNotNone(seg)
        self.assertEqual(seg["file_id"], "fs")

    def test_subtitle_playlist_uses_correct_key(self):
        """generate_media_playlist for sub index 1 must find sub_1/subtitles.vtt."""
        analysis, processing, upload = self._payload_with_skipped_bitmap_sub()
        hls_manager.register_job("jobsub2", analysis, processing, upload)

        # Sub track has track_index=1 → playlist must reference sub_1
        playlist = hls_manager.generate_media_playlist("jobsub2", "sub", 1)
        self.assertIsNotNone(playlist)
        self.assertIn("sub_1/subtitles.vtt", playlist)

    def test_original_stream_index_stored_for_video_and_audio(self):
        analysis, processing, upload = self._sample_payload()
        database.save_job("job_orig_idx", analysis, processing, upload)

        video_tracks = database.get_job_tracks("job_orig_idx", "video")
        audio_tracks = database.get_job_tracks("job_orig_idx", "audio")
        self.assertEqual(video_tracks[0]["original_stream_index"], 0)
        self.assertEqual(audio_tracks[0]["original_stream_index"], 1)


class TestTokenPropagation(TestDatabaseBase):
    """P4a: token is appended to sub-resource URLs in playlists."""

    def test_with_token_appends_query(self):
        self.assertEqual(
            hls_manager._with_token("/hls/job/video.m3u8", "abc123"),
            "/hls/job/video.m3u8?token=abc123",
        )

    def test_with_token_no_token_returns_url_unchanged(self):
        url = "/hls/job/video.m3u8"
        self.assertEqual(hls_manager._with_token(url, None), url)
        self.assertEqual(hls_manager._with_token(url, ""), url)

    def test_with_token_existing_query_uses_ampersand(self):
        self.assertEqual(
            hls_manager._with_token("/segment/x?foo=1", "tok"),
            "/segment/x?foo=1&token=tok",
        )

    def test_generate_master_playlist_with_token_includes_token_in_urls(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("tokjob", analysis, processing, upload)

        playlist = hls_manager.generate_master_playlist("tokjob", "https://cdn.example", token="mytoken")
        self.assertIn("?token=mytoken", playlist)

    def test_generate_master_playlist_without_token_no_query_param(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("notokjob", analysis, processing, upload)

        playlist = hls_manager.generate_master_playlist("notokjob", "https://cdn.example")
        self.assertNotIn("?token=", playlist)

    def test_generate_media_playlist_with_token_in_segment_urls(self):
        analysis, processing, upload = self._sample_payload()
        upload.segments["video_0/video_0001.ts"] = SimpleNamespace(
            file_id="fv0", bot_index=0, file_size=100
        )
        hls_manager.register_job("tokjob2", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("tokjob2", "video", 0, token="segtoken")
        self.assertIn("?token=segtoken", playlist)

    def test_generate_subtitle_playlist_with_token(self):
        analysis, processing, upload = self._sample_payload()
        hls_manager.register_job("tokjob3", analysis, processing, upload)

        job = database.get_job("tokjob3")
        playlist = hls_manager._generate_subtitle_playlist("tokjob3", job, 0, token="subtoken")
        self.assertIn("?token=subtoken", playlist)


if __name__ == "__main__":
    unittest.main()
