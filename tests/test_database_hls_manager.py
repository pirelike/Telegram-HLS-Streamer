import os
import sqlite3
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


class TestDatabaseMigrations(TestDatabaseBase):
    def _reset_db_file(self):
        database._close_all_connections()
        database._local = threading.local()
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.harness.db_path}{suffix}"
            if os.path.exists(path):
                os.remove(path)

    def _reinit_db_with_sql(self, statements):
        self._reset_db_file()
        conn = sqlite3.connect(self.harness.db_path)
        try:
            for statement in statements:
                conn.executescript(statement)
            conn.commit()
        finally:
            conn.close()
        database._local = threading.local()
        database.init_db()

    def test_init_db_creates_schema_migrations_table(self):
        conn = database._get_conn()
        rows = conn.execute(
            "SELECT revision, name FROM schema_migrations ORDER BY revision"
        ).fetchall()
        self.assertEqual(
            [(row["revision"], row["name"]) for row in rows],
            [
                (1, "create_base_schema"),
                (2, "add_track_dimensions_and_stream_index"),
                (3, "add_segment_duration"),
                (4, "add_media_metadata"),
                (5, "add_series_episode_metadata"),
                (6, "create_settings_and_bots_tables"),
            ],
        )

    def test_init_db_bootstraps_legacy_revision_one_and_upgrades(self):
        self._reinit_db_with_sql([
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                filename TEXT
            );
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                track_type TEXT NOT NULL,
                track_index INTEGER NOT NULL,
                codec TEXT,
                language TEXT DEFAULT 'und',
                title TEXT DEFAULT '',
                channels INTEGER DEFAULT 2,
                UNIQUE(job_id, track_type, track_index)
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                segment_key TEXT NOT NULL,
                file_id TEXT NOT NULL,
                bot_index INTEGER NOT NULL,
                file_size INTEGER DEFAULT 0,
                UNIQUE(job_id, segment_key)
            );
            INSERT INTO jobs (job_id, filename) VALUES ('legacy1', 'legacy.mp4');
            INSERT INTO tracks (job_id, track_type, track_index, codec, language, title, channels)
            VALUES ('legacy1', 'audio', 0, 'aac', 'eng', 'English', 2);
            INSERT INTO segments (job_id, segment_key, file_id, bot_index, file_size)
            VALUES ('legacy1', 'audio_0/audio_0001.ts', 'f1', 0, 123);
            """,
        ])

        conn = database._get_conn()
        track_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        segment_cols = {row["name"] for row in conn.execute("PRAGMA table_info(segments)").fetchall()}
        revision = conn.execute("SELECT MAX(revision) AS revision FROM schema_migrations").fetchone()["revision"]

        self.assertTrue({"width", "height", "bitrate", "original_stream_index"}.issubset(track_cols))
        self.assertIn("duration", segment_cols)
        self.assertEqual(revision, database.LATEST_SCHEMA_REVISION)
        self.assertEqual(database.get_job("legacy1")["filename"], "legacy.mp4")
        self.assertEqual(
            database.get_segments_for_prefix("legacy1", "audio_0")[0]["segment_key"],
            "audio_0/audio_0001.ts",
        )

    def test_init_db_bootstraps_legacy_revision_two_and_upgrades(self):
        self._reinit_db_with_sql([
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                filename TEXT
            );
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                track_type TEXT NOT NULL,
                track_index INTEGER NOT NULL,
                codec TEXT,
                language TEXT DEFAULT 'und',
                title TEXT DEFAULT '',
                channels INTEGER DEFAULT 2,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                bitrate TEXT DEFAULT '',
                original_stream_index INTEGER DEFAULT -1,
                UNIQUE(job_id, track_type, track_index)
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                segment_key TEXT NOT NULL,
                file_id TEXT NOT NULL,
                bot_index INTEGER NOT NULL,
                file_size INTEGER DEFAULT 0,
                UNIQUE(job_id, segment_key)
            );
            INSERT INTO jobs (job_id, filename) VALUES ('legacy2', 'legacy2.mp4');
            INSERT INTO segments (job_id, segment_key, file_id, bot_index, file_size)
            VALUES ('legacy2', 'video/video_0001.ts', 'f2', 0, 321);
            """,
        ])

        conn = database._get_conn()
        revision = conn.execute("SELECT MAX(revision) AS revision FROM schema_migrations").fetchone()["revision"]
        segment_cols = {row["name"] for row in conn.execute("PRAGMA table_info(segments)").fetchall()}

        self.assertEqual(revision, database.LATEST_SCHEMA_REVISION)
        self.assertIn("duration", segment_cols)
        self.assertEqual(database.get_job("legacy2")["filename"], "legacy2.mp4")

    def test_init_db_is_idempotent(self):
        database.init_db()
        conn = database._get_conn()
        rows = conn.execute("SELECT revision FROM schema_migrations ORDER BY revision").fetchall()
        self.assertEqual([row["revision"] for row in rows], [1, 2, 3, 4, 5, 6])

    def test_init_db_fails_for_newer_schema_revision(self):
        self._reset_db_file()
        conn = sqlite3.connect(self.harness.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    revision INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO schema_migrations (revision, name) VALUES (99, 'future_schema');
                """
            )
            conn.commit()
        finally:
            conn.close()

        database._local = threading.local()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            database.init_db()


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

    def test_sanitize_segment_uri_path_encodes_unsafe_characters(self):
        value = hls_manager._sanitize_segment_uri_path("video 1/seg?x#y.ts")
        self.assertEqual(value, "video%201/seg%3Fx%23y.ts")

    def test_sanitize_segment_uri_path_rejects_leading_hash(self):
        self.assertIsNone(hls_manager._sanitize_segment_uri_path("#bad.ts"))

    def test_compute_subtitle_duration_has_floor(self):
        self.assertEqual(hls_manager._compute_subtitle_duration(None), 4)
        self.assertEqual(hls_manager._compute_subtitle_duration(0), 4)
        self.assertEqual(hls_manager._compute_subtitle_duration(12.5), 12.5)

    def test_compute_bandwidth_uses_fallback_for_missing_duration(self):
        bw = hls_manager._compute_bandwidth(10_000_000, 0)
        self.assertGreater(bw, 0)
        self.assertLess(bw, 50_000_001)


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

    def test_generate_media_playlist_sanitizes_segment_keys(self):
        analysis, processing, upload = self._sample_payload("job_key_sanitize")
        upload.segments = {
            "video/bad\n#EXT-X-INJECTED.ts": SimpleNamespace(file_id="f1", bot_index=0, file_size=100),
            "video/seg\rb.ts": SimpleNamespace(file_id="f2", bot_index=0, file_size=100),
            "video/seg with space.ts": SimpleNamespace(file_id="f3", bot_index=0, file_size=100),
            "video/seg?query.ts": SimpleNamespace(file_id="f4", bot_index=0, file_size=100),
            "video/#leading_hash.ts": SimpleNamespace(file_id="f5", bot_index=0, file_size=100),
        }
        hls_manager.register_job("job_key_sanitize", analysis, processing, upload)

        playlist = hls_manager.generate_media_playlist("job_key_sanitize", "video")
        self.assertIsNotNone(playlist)
        self.assertNotIn("#EXT-X-INJECTED", playlist)
        self.assertIn("/segment/job_key_sanitize/video/bad%23EXT-X-INJECTED.ts", playlist)
        self.assertIn("/segment/job_key_sanitize/video/segb.ts", playlist)
        self.assertIn("/segment/job_key_sanitize/video/seg%20with%20space.ts", playlist)
        self.assertIn("/segment/job_key_sanitize/video/seg%3Fquery.ts", playlist)
        self.assertNotIn("/segment/job_key_sanitize/video/%23leading_hash.ts", playlist)

        extinf_count = playlist.count("#EXTINF:")
        uri_count = sum(1 for line in playlist.splitlines() if line.startswith("/segment/job_key_sanitize/"))
        self.assertEqual(extinf_count, uri_count)

    def test_generate_subtitle_playlist_duration_floor_when_null_or_zero(self):
        analysis, processing, upload = self._sample_payload("job_sub_floor")
        analysis.duration = None
        hls_manager.register_job("job_sub_floor", analysis, processing, upload)
        job = database.get_job("job_sub_floor")

        playlist = hls_manager._generate_subtitle_playlist("job_sub_floor", job, 0)
        self.assertIn("#EXTINF:4.000,", playlist)
        self.assertIn("#EXT-X-TARGETDURATION:4", playlist)

        analysis2, processing2, upload2 = self._sample_payload("job_sub_zero")
        analysis2.duration = 0
        hls_manager.register_job("job_sub_zero", analysis2, processing2, upload2)
        job2 = database.get_job("job_sub_zero")
        playlist2 = hls_manager._generate_subtitle_playlist("job_sub_zero", job2, 0)
        self.assertIn("#EXTINF:4.000,", playlist2)
        self.assertIn("#EXT-X-TARGETDURATION:4", playlist2)

    def test_generate_subtitle_playlist_uses_positive_job_duration(self):
        analysis, processing, upload = self._sample_payload("job_sub_positive")
        analysis.duration = 9.25
        hls_manager.register_job("job_sub_positive", analysis, processing, upload)
        job = database.get_job("job_sub_positive")

        playlist = hls_manager._generate_subtitle_playlist("job_sub_positive", job, 0)
        self.assertIn("#EXTINF:9.250,", playlist)
        self.assertIn("#EXT-X-TARGETDURATION:10", playlist)

    def test_generate_master_playlist_bandwidth_fallback_when_duration_missing(self):
        analysis, processing, upload = self._sample_payload("job_bw_zero")
        analysis.duration = 0
        processing.video_playlists = [("v.m3u8", "/tmp/v", 1280, 720, "")]
        analysis.file_size = 8_000_000
        hls_manager.register_job("job_bw_zero", analysis, processing, upload)

        playlist = hls_manager.generate_master_playlist("job_bw_zero", "https://cdn.example")
        line = next(line for line in playlist.splitlines() if line.startswith("#EXT-X-STREAM-INF:"))
        bandwidth = int(line.split("BANDWIDTH=")[1].split(",")[0])
        self.assertGreater(bandwidth, 0)
        self.assertLess(bandwidth, 50_000_001)
        self.assertNotEqual(bandwidth, analysis.file_size * 8)

    def test_generate_master_playlist_legacy_bandwidth_fallback_when_duration_missing(self):
        analysis = SimpleNamespace(
            file_path="/tmp/legacy_zero.mp4",
            duration=0,
            file_size=6_000_000,
            has_video=True,
            video_streams=[SimpleNamespace(codec_name="h264", width=640, height=480, index=0)],
            audio_streams=[],
            subtitle_streams=[],
        )
        processing = SimpleNamespace(video_playlists=[], audio_playlists=[], subtitle_files=[], segment_durations={})
        upload = SimpleNamespace(segments={
            "video/video_0001.ts": SimpleNamespace(file_id="fL", bot_index=0, file_size=10)
        })
        hls_manager.register_job("legacy_bw_zero", analysis, processing, upload)

        playlist = hls_manager.generate_master_playlist("legacy_bw_zero", "https://cdn.example")
        line = next(line for line in playlist.splitlines() if line.startswith("#EXT-X-STREAM-INF:"))
        bandwidth = int(line.split("BANDWIDTH=")[1].split(",")[0])
        self.assertGreater(bandwidth, 0)
        self.assertLess(bandwidth, 50_000_001)
        self.assertNotEqual(bandwidth, analysis.file_size * 8)

    def test_master_playlist_sanitizes_malicious_metadata(self):
        analysis, processing, upload = self._sample_payload("job_malicious")
        
        # Inject malicious metadata
        processing.audio_playlists = [
            ("a.m3u8", "/tmp/a", 'eng",DEFAULT=YES\n#EXT-X-INJECTED', 'Malicious\r\nTitle, with "quotes"', 2)
        ]
        processing.subtitle_files = [
            ("s.vtt", "/tmp/s", 'und', 'Sub\nTitle, "test"', 0, 3)
        ]
        
        hls_manager.register_job("job_malicious", analysis, processing, upload)
        playlist = hls_manager.generate_master_playlist("job_malicious", "http://localhost")
        
        # Verify sanitization in audio
        self.assertNotIn("\n#EXT-X-INJECTED", playlist)
        self.assertNotIn("\r", playlist)
        self.assertIn('LANGUAGE="eng\\" DEFAULT=YES #EXT-X-INJECTED"', playlist)
        self.assertIn('NAME="Malicious  Title  with \\"quotes\\""', playlist)
        
        # Verify sanitization in subtitle
        self.assertIn('NAME="Sub Title  \\"test\\""', playlist)

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


class TestMediaMetadata(TestDatabaseBase):
    """Tests for migration 004 — media_type, series_name, has_thumbnail."""

    def test_migration_004_columns_exist(self):
        conn = database._get_conn()
        cols = database._list_table_columns(conn, "jobs")
        self.assertIn("media_type", cols)
        self.assertIn("series_name", cols)
        self.assertIn("has_thumbnail", cols)

    def test_save_job_stores_media_type_and_series_name(self):
        analysis, processing, upload = self._sample_payload("job_meta")
        database.save_job("job_meta", analysis, processing, upload,
                          media_type="Series", series_name="Breaking Bad")
        job = database.get_job("job_meta")
        self.assertEqual(job["media_type"], "Series")
        self.assertEqual(job["series_name"], "Breaking Bad")
        self.assertEqual(job["has_thumbnail"], 0)

    def test_save_job_defaults_media_type_to_film(self):
        analysis, processing, upload = self._sample_payload("job_default")
        database.save_job("job_default", analysis, processing, upload)
        job = database.get_job("job_default")
        self.assertEqual(job["media_type"], "Film")
        self.assertEqual(job["series_name"], "")

    def test_update_job_thumbnail_sets_flag(self):
        analysis, processing, upload = self._sample_payload("job_thumb")
        database.save_job("job_thumb", analysis, processing, upload)
        self.assertEqual(database.get_job("job_thumb")["has_thumbnail"], 0)
        database.update_job_thumbnail("job_thumb")
        self.assertEqual(database.get_job("job_thumb")["has_thumbnail"], 1)

    def test_update_job_metadata(self):
        analysis, processing, upload = self._sample_payload("job_meta_edit")
        database.save_job("job_meta_edit", analysis, processing, upload,
                          media_type="Film", is_series=0)
        
        database.update_job_metadata(
            "job_meta_edit", media_type="Anime TV", series_name="New Series",
            is_series=1, season_number=2, episode_number=5, part_number=None
        )
        
        job = database.get_job("job_meta_edit")
        self.assertEqual(job["media_type"], "Anime TV")
        self.assertEqual(job["series_name"], "New Series")
        self.assertEqual(job["is_series"], 1)
        self.assertEqual(job["season_number"], 2)
        self.assertEqual(job["episode_number"], 5)
        self.assertIsNone(job["part_number"])

    def test_list_jobs_includes_media_metadata(self):
        analysis, processing, upload = self._sample_payload("job_list_meta")
        database.save_job("job_list_meta", analysis, processing, upload,
                          media_type="Anime", series_name="Naruto")
        database.update_job_thumbnail("job_list_meta")
        jobs = database.list_jobs()
        job = next((j for j in jobs if j["job_id"] == "job_list_meta"), None)
        self.assertIsNotNone(job)
        self.assertEqual(job["media_type"], "Anime")
        self.assertEqual(job["series_name"], "Naruto")
        self.assertEqual(job["has_thumbnail"], 1)


class TestMigration005(TestDatabaseBase):
    """Tests for migration 005 — is_series, season_number, episode_number, part_number."""

    def test_migration_005_columns_exist(self):
        conn = database._get_conn()
        cols = database._list_table_columns(conn, "jobs")
        self.assertIn("is_series", cols)
        self.assertIn("season_number", cols)
        self.assertIn("episode_number", cols)
        self.assertIn("part_number", cols)

    def test_save_job_stores_series_episode_metadata(self):
        analysis, processing, upload = self._sample_payload("job_ep")
        database.save_job("job_ep", analysis, processing, upload,
                          media_type="Series", series_name="Breaking Bad",
                          is_series=True, season_number=2, episode_number=5)
        job = database.get_job("job_ep")
        self.assertEqual(job["is_series"], 1)
        self.assertEqual(job["season_number"], 2)
        self.assertEqual(job["episode_number"], 5)
        self.assertIsNone(job["part_number"])

    def test_save_job_stores_part_number(self):
        analysis, processing, upload = self._sample_payload("job_part")
        database.save_job("job_part", analysis, processing, upload,
                          media_type="Film", is_series=True, part_number=3)
        job = database.get_job("job_part")
        self.assertEqual(job["is_series"], 1)
        self.assertEqual(job["part_number"], 3)
        self.assertIsNone(job["season_number"])
        self.assertIsNone(job["episode_number"])

    def test_save_job_defaults_is_series_false(self):
        analysis, processing, upload = self._sample_payload("job_noser")
        database.save_job("job_noser", analysis, processing, upload)
        job = database.get_job("job_noser")
        self.assertEqual(job["is_series"], 0)
        self.assertIsNone(job["season_number"])
        self.assertIsNone(job["episode_number"])
        self.assertIsNone(job["part_number"])

    def test_list_jobs_includes_episode_metadata(self):
        analysis, processing, upload = self._sample_payload("job_eplist")
        database.save_job("job_eplist", analysis, processing, upload,
                          media_type="Anime TV", series_name="One Piece",
                          is_series=True, season_number=1, episode_number=42)
        jobs = database.list_jobs()
        job = next((j for j in jobs if j["job_id"] == "job_eplist"), None)
        self.assertIsNotNone(job)
        self.assertEqual(job["is_series"], 1)
        self.assertEqual(job["season_number"], 1)
        self.assertEqual(job["episode_number"], 42)


class TestSearchAndFiltering(TestDatabaseBase):
    def setUp(self):
        super().setUp()
        # Create some sample data
        data = [
            ("job1", "Inception.mp4", "Film", ""),
            ("job2", "Interstellar.mp4", "Film", ""),
            ("job3", "Breaking Bad S01E01.mp4", "Series", "Breaking Bad"),
            ("job4", "Naruto Ep 01.mkv", "Anime Film", "Naruto"),
            ("job5", "One Piece 001.mp4", "Anime TV", "One Piece"),
        ]
        for jid, filename, mtype, sname in data:
            analysis, processing, upload = self._sample_payload(jid)
            # Ensure the filename is set correctly in analysis
            analysis.file_path = f"/tmp/{filename}"
            database.save_job(jid, analysis, processing, upload, media_type=mtype, series_name=sname)

    def test_list_jobs_search_filename(self):
        jobs = database.list_jobs(search="Inception")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["filename"], "Inception.mp4")

    def test_list_jobs_search_series_name(self):
        jobs = database.list_jobs(search="Breaking")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["series_name"], "Breaking Bad")

    def test_list_jobs_category_filter(self):
        jobs = database.list_jobs(category="Film")
        self.assertEqual(len(jobs), 2)
        filenames = {j["filename"] for j in jobs}
        self.assertIn("Inception.mp4", filenames)
        self.assertIn("Interstellar.mp4", filenames)

    def test_list_jobs_anime_legacy_matching(self):
        # Manually update one to 'Anime' legacy type
        conn = database._get_conn()
        with conn:
            conn.execute("UPDATE jobs SET media_type = 'Anime' WHERE job_id = 'job4'")
            
        # Should match in Anime Film
        jobs_film = database.list_jobs(category="Anime Film")
        self.assertEqual(len(jobs_film), 1)
        self.assertEqual(jobs_film[0]["job_id"], "job4")
        
        # Should also match in Anime TV
        jobs_tv = database.list_jobs(category="Anime TV")
        # job5 is 'Anime TV', job4 is 'Anime'
        self.assertEqual(len(jobs_tv), 2)

    def test_count_jobs_with_filters(self):
        self.assertEqual(database.count_jobs(search="Interstellar"), 1)
        self.assertEqual(database.count_jobs(category="Series"), 1)
        self.assertEqual(database.count_jobs(category="Film"), 2)

    def test_search_and_category_combined(self):
        jobs = database.list_jobs(search="Naruto", category="Anime Film")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "job4")
        
        jobs_empty = database.list_jobs(search="Naruto", category="Film")
        self.assertEqual(len(jobs_empty), 0)


class TestSettingsCRUD(TestDatabaseBase):
    def test_get_all_settings_empty(self):
        result = database.get_all_settings()
        self.assertEqual(result, {})

    def test_set_and_get_setting(self):
        database.set_setting("HLS_SEGMENT_DURATION", "8")
        result = database.get_all_settings()
        self.assertEqual(result["HLS_SEGMENT_DURATION"], "8")

    def test_set_setting_overwrite(self):
        database.set_setting("MAX_PARALLEL_ENCODES", "2")
        database.set_setting("MAX_PARALLEL_ENCODES", "4")
        result = database.get_all_settings()
        self.assertEqual(result["MAX_PARALLEL_ENCODES"], "4")

    def test_set_settings_bulk(self):
        database.set_settings({"KEY_A": "val1", "KEY_B": "val2"})
        result = database.get_all_settings()
        self.assertEqual(result["KEY_A"], "val1")
        self.assertEqual(result["KEY_B"], "val2")

    def test_delete_setting(self):
        database.set_setting("TO_DELETE", "yes")
        database.delete_setting("TO_DELETE")
        result = database.get_all_settings()
        self.assertNotIn("TO_DELETE", result)

    def test_delete_nonexistent_setting_does_not_raise(self):
        database.delete_setting("NONEXISTENT_KEY")  # should not raise


class TestBotsCRUD(TestDatabaseBase):
    def test_get_all_bots_empty(self):
        result = database.get_all_bots()
        self.assertEqual(result, [])

    def test_add_and_get_bot(self):
        bot_id = database.add_bot("123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100123, "Test")
        self.assertIsInstance(bot_id, int)
        bots = database.get_all_bots()
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["channel_id"], -100123)
        self.assertEqual(bots[0]["label"], "Test")
        self.assertEqual(bots[0]["id"], bot_id)

    def test_add_bot_default_label(self):
        database.add_bot("123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100123)
        bots = database.get_all_bots()
        self.assertEqual(bots[0]["label"], "")

    def test_add_duplicate_token_raises(self):
        database.add_bot("123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100123)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            database.add_bot("123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100456)

    def test_delete_bot(self):
        bot_id = database.add_bot("123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100123)
        database.delete_bot(bot_id)
        bots = database.get_all_bots()
        self.assertEqual(bots, [])

    def test_delete_nonexistent_bot_does_not_raise(self):
        database.delete_bot(9999)  # should not raise

    def test_bot_exists_true(self):
        token = "123456789:ABCdefGHIjklMNOpqrSTUvwXYZ012345678"
        database.add_bot(token, -100123)
        self.assertTrue(database.bot_exists(token))

    def test_bot_exists_false(self):
        self.assertFalse(database.bot_exists("999999999:XYZabcDEFghiJKLmnoPQRstuvWXYZ123456"))

    def test_multiple_bots_ordered_by_id(self):
        database.add_bot("111111111:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100001, "Bot 1")
        database.add_bot("222222222:ABCdefGHIjklMNOpqrSTUvwXYZ012345678", -100002, "Bot 2")
        bots = database.get_all_bots()
        self.assertEqual(len(bots), 2)
        self.assertEqual(bots[0]["label"], "Bot 1")
        self.assertEqual(bots[1]["label"], "Bot 2")


if __name__ == "__main__":
    unittest.main()
