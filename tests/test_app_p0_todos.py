import base64
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

# Provide a lightweight telegram stub so importing app works in test envs.
telegram_mod = types.ModuleType("telegram")
telegram_error_mod = types.ModuleType("telegram.error")
telegram_request_mod = types.ModuleType("telegram.request")


class StubBot:
    def __init__(self, *args, **kwargs):
        pass


class StubHTTPXRequest:
    def __init__(self, *args, **kwargs):
        pass


telegram_mod.Bot = StubBot
telegram_request_mod.HTTPXRequest = StubHTTPXRequest
telegram_error_mod.RetryAfter = Exception
telegram_error_mod.NetworkError = Exception
telegram_error_mod.TimedOut = Exception
telegram_error_mod.BadRequest = Exception
telegram_error_mod.Forbidden = Exception
sys.modules.setdefault("telegram", telegram_mod)
sys.modules.setdefault("telegram.error", telegram_error_mod)
sys.modules.setdefault("telegram.request", telegram_request_mod)

import app as app_module
import database


def _reset_state():
    app_module._pending_uploads.clear()
    app_module._pending_filenames.clear()
    app_module._active_jobs.clear()
    app_module._upload_locks.clear()
    app_module._rate_limit_hits.clear()
    app_module._pending_uploads_per_ip.clear()
    app_module._segment_prefetch_inflight.clear()
    app_module._segment_cache.clear()


class TestP0TodoFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _reset_state()
        database._close_all_connections()
        database._local = threading.local()
        database.init_db()
        self.upload_dir_patch = patch.object(app_module.Config, "UPLOAD_DIR", self.temp.name)
        self.chunk_size_patch = patch.object(app_module.Config, "UPLOAD_CHUNK_SIZE", 4)
        self.max_upload_size_patch = patch.object(app_module.Config, "MAX_UPLOAD_SIZE", 1024 * 1024)
        self.upload_dir_patch.start()
        self.chunk_size_patch.start()
        self.max_upload_size_patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        database._close_all_connections()
        self.upload_dir_patch.stop()
        self.chunk_size_patch.stop()
        self.max_upload_size_patch.stop()
        self.temp.cleanup()

    def _init_upload(self, filename="sample.bin", total_size=8, total_chunks=2):
        response = self.client.post(
            "/api/upload/init",
            json={"filename": filename, "total_size": total_size, "total_chunks": total_chunks},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["upload_id"]

    # ─── _is_job_cancelled ───

    def test_is_job_cancelled_uses_explicit_flag_not_error_text(self):
        app_module._active_jobs["job1"] = {
            "status": "error",
            "error": "operation timed out while uploading",
        }
        self.assertFalse(app_module._is_job_cancelled("job1"))

        app_module._active_jobs["job1"]["timed_out"] = True
        self.assertTrue(app_module._is_job_cancelled("job1"))

    def test_is_job_cancelled_missing_job_returns_true(self):
        self.assertTrue(app_module._is_job_cancelled("no_such_job"))

    def test_is_job_cancelled_cancelled_flag(self):
        app_module._active_jobs["job2"] = {"status": "processing", "cancelled": True}
        self.assertTrue(app_module._is_job_cancelled("job2"))

    def test_is_job_cancelled_active_job_returns_false(self):
        app_module._active_jobs["job3"] = {"status": "processing"}
        self.assertFalse(app_module._is_job_cancelled("job3"))

    # ─── _job_timed_out ───

    def test_job_timed_out_no_started_ts(self):
        self.assertFalse(app_module._job_timed_out({}))

    def test_job_timed_out_recent_start(self):
        job = {"started_ts": time.time()}
        self.assertFalse(app_module._job_timed_out(job))

    def test_job_timed_out_old_start(self):
        job = {"started_ts": time.time() - 999999}
        with patch.object(app_module.Config, "JOB_TIMEOUT_SECONDS", 1):
            self.assertTrue(app_module._job_timed_out(job))

    # ─── _get_client_ip ───

    def test_get_client_ip_no_proxy(self):
        with patch.object(app_module.Config, "BEHIND_PROXY", False):
            with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "1.2.3.4"}):
                ip = app_module._get_client_ip()
        self.assertEqual(ip, "1.2.3.4")

    def test_get_client_ip_behind_proxy(self):
        with patch.object(app_module.Config, "BEHIND_PROXY", True):
            with app_module.app.test_request_context(
                "/", headers={"X-Forwarded-For": "10.0.0.1, 192.168.1.1"}
            ):
                ip = app_module._get_client_ip()
        self.assertEqual(ip, "10.0.0.1")

    def test_get_client_ip_behind_proxy_no_header(self):
        with patch.object(app_module.Config, "BEHIND_PROXY", True):
            with app_module.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "9.9.9.9"}
            ):
                ip = app_module._get_client_ip()
        self.assertEqual(ip, "9.9.9.9")

    # ─── _is_origin_allowed ───

    def test_is_origin_allowed_empty_origin(self):
        self.assertFalse(app_module._is_origin_allowed(""))

    def test_is_origin_allowed_wildcard(self):
        with patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", ["*"]):
            self.assertTrue(app_module._is_origin_allowed("https://any.example.com"))

    def test_is_origin_allowed_specific_match(self):
        with patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", ["https://good.example.com"]):
            self.assertTrue(app_module._is_origin_allowed("https://good.example.com"))

    def test_is_origin_allowed_specific_no_match(self):
        with patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", ["https://good.example.com"]):
            self.assertFalse(app_module._is_origin_allowed("https://evil.example.com"))

    def test_is_origin_allowed_empty_list(self):
        with patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", []):
            self.assertFalse(app_module._is_origin_allowed("https://any.example.com"))

    # ─── _get_base_url ───

    def test_get_base_url_force_https(self):
        with patch.object(app_module.Config, "FORCE_HTTPS", True):
            with app_module.app.test_request_context("/", base_url="http://myhost:5050"):
                url = app_module._get_base_url()
        self.assertTrue(url.startswith("https://"))

    def test_get_base_url_plain_http(self):
        with patch.object(app_module.Config, "FORCE_HTTPS", False), \
             patch.object(app_module.Config, "BEHIND_PROXY", False):
            with app_module.app.test_request_context("/", base_url="http://localhost:5050"):
                url = app_module._get_base_url()
        self.assertTrue(url.startswith("http://"))

    def test_get_base_url_behind_proxy_uses_forwarded_proto(self):
        with patch.object(app_module.Config, "FORCE_HTTPS", False), \
             patch.object(app_module.Config, "BEHIND_PROXY", True):
            with app_module.app.test_request_context(
                "/", headers={"X-Forwarded-Proto": "https"}
            ):
                url = app_module._get_base_url()
        self.assertTrue(url.startswith("https://"))

    def test_health_request_teardown_closes_db_connection(self):
        database.close_conn()
        self.assertEqual(database.open_connection_count(), 0)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(database.open_connection_count(), 0)

    def test_repeated_health_requests_do_not_accumulate_db_connections(self):
        database.close_conn()
        self.assertEqual(database.open_connection_count(), 0)

        for _ in range(5):
            response = self.client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(database.open_connection_count(), 0)

    # ─── _is_upload_authorized ───

    def test_is_upload_authorized_no_auth_config(self):
        with patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""):
            with app_module.app.test_request_context("/"):
                self.assertTrue(app_module._is_upload_authorized())

    def test_is_upload_authorized_api_key_success(self):
        with patch.object(app_module.Config, "UPLOAD_API_KEY", "secret"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""):
            with app_module.app.test_request_context("/", headers={"X-API-Key": "secret"}):
                self.assertTrue(app_module._is_upload_authorized())

    def test_is_upload_authorized_api_key_wrong(self):
        with patch.object(app_module.Config, "UPLOAD_API_KEY", "secret"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""):
            with app_module.app.test_request_context("/", headers={"X-API-Key": "wrong"}):
                self.assertFalse(app_module._is_upload_authorized())

    def test_is_upload_authorized_basic_auth_success(self):
        creds = base64.b64encode(b"user:pass").decode()
        with patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", "user"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_PASSWORD", "pass"):
            with app_module.app.test_request_context(
                "/", headers={"Authorization": f"Basic {creds}"}
            ):
                self.assertTrue(app_module._is_upload_authorized())

    def test_is_upload_authorized_basic_auth_wrong_password(self):
        creds = base64.b64encode(b"user:wrongpass").decode()
        with patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", "user"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_PASSWORD", "pass"):
            with app_module.app.test_request_context(
                "/", headers={"Authorization": f"Basic {creds}"}
            ):
                self.assertFalse(app_module._is_upload_authorized())

    def test_is_upload_authorized_malformed_basic_auth(self):
        with patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", "user"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_PASSWORD", "pass"):
            with app_module.app.test_request_context(
                "/", headers={"Authorization": "Basic notbase64!!!"}
            ):
                self.assertFalse(app_module._is_upload_authorized())

    # ─── /api/upload/init ───

    def test_upload_init_success(self):
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "test.mp4", "total_size": 100, "total_chunks": 25},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("upload_id", data)
        self.assertIn("chunk_size", data)

    def test_upload_init_missing_fields(self):
        resp = self.client.post("/api/upload/init", json={"filename": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_upload_init_zero_total_size_rejected(self):
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "test.mp4", "total_size": 0},
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_init_negative_total_size_rejected(self):
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "test.mp4", "total_size": -1},
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_init_file_too_large(self):
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "huge.mp4", "total_size": 10 * 1024 * 1024 * 1024},
        )
        self.assertEqual(resp.status_code, 413)

    def test_upload_init_duplicate_filename_rejected(self):
        self._init_upload("dup.bin", total_size=8, total_chunks=2)
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "dup.bin", "total_size": 8, "total_chunks": 2},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already in progress", resp.get_json()["error"])

    def test_upload_init_invalid_total_size_type(self):
        resp = self.client.post(
            "/api/upload/init",
            json={"filename": "x.mp4", "total_size": "big"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_init_requires_upload_auth_when_configured(self):
        with patch.object(app_module.Config, "UPLOAD_API_KEY", "key123"), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""):
            resp = self.client.post(
                "/api/upload/init",
                json={"filename": "x.mp4", "total_size": 10},
            )
        self.assertEqual(resp.status_code, 401)

    # ─── /api/upload/chunk ───

    def test_upload_chunk_rejects_negative_chunk_index(self):
        upload_id = self._init_upload()
        resp = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "-1"},
            data=b"AAAA",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be >= 0", resp.get_json()["error"])

    def test_upload_chunk_rejects_out_of_order_gap(self):
        upload_id = self._init_upload()

        # Send chunk 1 before chunk 0 — creates a gap
        out_of_order = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "1"},
            data=b"BBBB",
        )
        self.assertEqual(out_of_order.status_code, 409)
        self.assertIn("file gap", out_of_order.get_json()["error"])

        # Send in correct order
        first = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"AAAA",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "1"},
            data=b"BBBB",
        )
        self.assertEqual(second.status_code, 200)

        upload_path = app_module._pending_uploads[upload_id]["path"]
        with open(upload_path, "rb") as f:
            self.assertEqual(f.read(), b"AAAABBBB")

    def test_upload_chunk_retry_does_not_double_count(self):
        upload_id = self._init_upload(total_size=4, total_chunks=1)

        first = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"ABCD",
        )
        self.assertEqual(first.status_code, 200)
        first_data = first.get_json()
        self.assertEqual(first_data["received_chunks"], 1)
        self.assertFalse(first_data["is_retry"])

        retry = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"ABCD",
        )
        self.assertEqual(retry.status_code, 200)
        retry_data = retry.get_json()
        self.assertEqual(retry_data["received_chunks"], 1)
        self.assertTrue(retry_data["is_retry"])

    def test_upload_chunk_rejects_overlap_non_retry(self):
        upload_id = self._init_upload(total_size=8, total_chunks=2)
        self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"AAAA",
        )
        self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "1"},
            data=b"BBBB",
        )

        app_module._pending_uploads[upload_id]["received_chunk_indices"].discard(0)
        overlap = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"AAAA",
        )
        self.assertEqual(overlap.status_code, 409)
        self.assertIn("overlaps", overlap.get_json()["error"])

    def test_upload_chunk_rejects_boundary_violations(self):
        upload_id = self._init_upload(total_size=8, total_chunks=2)

        out_of_bounds = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "2"},
            data=b"AAAA",
        )
        self.assertEqual(out_of_bounds.status_code, 400)
        self.assertIn("exceeds file size", out_of_bounds.get_json()["error"])

        too_large = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "1"},
            data=b"BBBBB",
        )
        self.assertEqual(too_large.status_code, 400)
        self.assertIn("exceeds declared total size", too_large.get_json()["error"])

    def test_upload_chunk_rejects_non_final_partial_chunk(self):
        upload_id = self._init_upload(total_size=12, total_chunks=3)
        resp = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"AA",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid chunk size", resp.get_json()["error"])

    def test_upload_chunk_unknown_upload_id(self):
        resp = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": "unknown123", "X-Chunk-Index": "0"},
            data=b"AAAA",
        )
        self.assertEqual(resp.status_code, 404)

    def test_upload_chunk_missing_headers(self):
        resp = self.client.post("/api/upload/chunk", data=b"AAAA")
        self.assertEqual(resp.status_code, 400)

    def test_upload_chunk_non_integer_chunk_index(self):
        upload_id = self._init_upload()
        resp = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "abc"},
            data=b"AAAA",
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_chunk_empty_body(self):
        upload_id = self._init_upload()
        resp = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"",
        )
        self.assertEqual(resp.status_code, 400)

    # ─── /api/upload/status ───

    def test_upload_status_known_upload(self):
        upload_id = self._init_upload(total_size=8, total_chunks=2)
        resp = self.client.get(f"/api/upload/status/{upload_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total_chunks"], 2)
        self.assertEqual(data["total_size"], 8)

    def test_upload_status_unknown_upload(self):
        resp = self.client.get("/api/upload/status/nonexistent")
        self.assertEqual(resp.status_code, 404)

    # ─── /api/upload/finalize ───

    def test_upload_finalize_size_mismatch(self):
        upload_id = self._init_upload(total_size=8, total_chunks=2)
        # Send only one chunk (4 bytes) but declared 8 bytes
        self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "0"},
            data=b"AAAA",
        )
        resp = self.client.post("/api/upload/finalize", json={"upload_id": upload_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Incomplete upload", resp.get_json()["error"])

    def test_upload_finalize_unknown_id(self):
        resp = self.client.post("/api/upload/finalize", json={"upload_id": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_upload_finalize_no_upload_id(self):
        # Empty JSON body → upload_id is None → 404
        resp = self.client.post("/api/upload/finalize", json={})
        self.assertEqual(resp.status_code, 404)

    # ─── /api/status/<job_id> ───

    def test_job_status_active_job(self):
        app_module._active_jobs["jid123"] = {"status": "processing", "progress": 50}
        resp = self.client.get("/api/status/jid123")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "processing")

    def test_job_status_unknown_job(self):
        with patch("app.get_job", return_value=None):
            resp = self.client.get("/api/status/unknownjob")
        self.assertEqual(resp.status_code, 404)

    def test_job_status_complete_from_db(self):
        db_job = {"job_id": "dbjob", "filename": "vid.mp4"}
        with patch("app.get_job", return_value=db_job):
            resp = self.client.get("/api/status/dbjob")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["progress"], 100)

    # ─── /api/jobs ───

    def test_jobs_list_empty(self):
        with patch("app.list_jobs", return_value={}), patch("app.count_jobs", return_value=0):
            resp = self.client.get("/api/jobs")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("jobs", data)
        self.assertEqual(data["total"], 0)

    def test_jobs_list_pagination_params(self):
        with patch("app.list_jobs", return_value={}), patch("app.count_jobs", return_value=0):
            resp = self.client.get("/api/jobs?page=2&limit=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["limit"], 5)

    def test_jobs_list_invalid_params_use_defaults(self):
        with patch("app.list_jobs", return_value={}), patch("app.count_jobs", return_value=0):
            resp = self.client.get("/api/jobs?page=abc&limit=xyz")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["limit"], 20)

    def test_jobs_list_has_more_flag(self):
        jobs = {f"job{i}": {"job_id": f"job{i}"} for i in range(3)}
        with patch("app.list_jobs", return_value=jobs), patch("app.count_jobs", return_value=10):
            resp = self.client.get("/api/jobs?limit=3")
        data = resp.get_json()
        self.assertTrue(data["has_more"])

    # ─── /api/cancel/<job_id> ───

    def test_cancel_job_not_found(self):
        resp = self.client.post("/api/cancel/no_such_job")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_active_job(self):
        app_module._active_jobs["cjob"] = {"status": "processing"}
        resp = self.client.post("/api/cancel/cjob")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(app_module._active_jobs["cjob"]["status"], "error")
        self.assertTrue(app_module._active_jobs["cjob"]["cancelled"])

    def test_cancel_finished_job_rejected(self):
        app_module._active_jobs["fjob"] = {
            "status": "complete",
            "finished_ts": time.time(),
        }
        resp = self.client.post("/api/cancel/fjob")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cannot cancel", resp.get_json()["error"])

    # ─── /api/jobs/<job_id> DELETE ───

    def test_delete_job_not_in_db(self):
        with patch("app.get_job", return_value=None):
            resp = self.client.delete("/api/jobs/nope")
        self.assertEqual(resp.status_code, 404)

    def test_delete_job_active_job_rejected(self):
        app_module._active_jobs["activejob"] = {"status": "processing"}
        with patch("app.get_job", return_value={"job_id": "activejob"}):
            resp = self.client.delete("/api/jobs/activejob")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cancel it first", resp.get_json()["error"])

    def test_delete_completed_job(self):
        app_module._active_jobs["donejob"] = {"status": "complete", "finished_ts": time.time()}
        with patch("app.get_job", return_value={"job_id": "donejob"}), \
             patch("app.db.delete_job") as mock_delete:
            resp = self.client.delete("/api/jobs/donejob")
        self.assertEqual(resp.status_code, 200)
        mock_delete.assert_called_once_with("donejob")

    # ─── HLS playlist endpoints ───

    def test_master_playlist_not_found(self):
        with patch("app.generate_master_playlist", return_value=None):
            resp = self.client.get("/hls/nojob/master.m3u8")
        self.assertEqual(resp.status_code, 404)

    def test_master_playlist_served(self):
        with patch("app.generate_master_playlist", return_value="#EXTM3U\n"):
            resp = self.client.get("/hls/goodjob/master.m3u8")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("mpegurl", resp.content_type)

    def test_video_playlist_not_found(self):
        with patch("app.generate_media_playlist", return_value=None):
            resp = self.client.get("/hls/nojob/video.m3u8")
        self.assertEqual(resp.status_code, 404)

    def test_audio_playlist_served(self):
        with patch("app.generate_media_playlist", return_value="#EXTM3U\n"):
            resp = self.client.get("/hls/goodjob/audio_0.m3u8")
        self.assertEqual(resp.status_code, 200)

    def test_subtitle_playlist_served(self):
        with patch("app.generate_media_playlist", return_value="#EXTM3U\n"):
            resp = self.client.get("/hls/goodjob/sub_0.m3u8")
        self.assertEqual(resp.status_code, 200)

    def test_video_tier_playlist_not_found(self):
        with patch("app.generate_media_playlist", return_value=None):
            resp = self.client.get("/hls/nojob/video_0.m3u8")
        self.assertEqual(resp.status_code, 404)

    # ─── /segment/<job_id>/<segment_key> ───

    def test_serve_segment_not_found(self):
        with patch("app.get_segment_info", return_value=None):
            resp = self.client.get("/segment/nojob/video/seg.ts")
        self.assertEqual(resp.status_code, 404)

    def test_serve_segment_ts_content_type(self):
        with patch("app.get_segment_info", return_value={"file_id": "fid", "bot_index": 0}), \
             patch.object(app_module._segment_cache, "get", return_value=b"fakedata"), \
             patch("app._schedule_segment_prefetch") as schedule_prefetch:
            resp = self.client.get("/segment/job1/video/seg.ts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("video/mp2t", resp.content_type)
        schedule_prefetch.assert_called_once_with("job1", "video/seg.ts")

    def test_serve_segment_vtt_content_type(self):
        with patch("app.get_segment_info", return_value={"file_id": "fid", "bot_index": 0}), \
             patch.object(app_module._segment_cache, "get", return_value=b"WEBVTT"), \
             patch("app._schedule_segment_prefetch") as schedule_prefetch:
            resp = self.client.get("/segment/job1/sub_0/subs.vtt")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("vtt", resp.content_type)
        schedule_prefetch.assert_called_once_with("job1", "sub_0/subs.vtt")

    def test_serve_segment_schedules_prefetch_after_download(self):
        with patch("app.get_segment_info", return_value={"file_id": "fid", "bot_index": 0}), \
             patch.object(app_module._segment_cache, "get", return_value=None), \
             patch("app._run_async", return_value=b"fakedata"), \
             patch.object(app_module._segment_cache, "put"), \
             patch("app._schedule_segment_prefetch") as schedule_prefetch:
            resp = self.client.get("/segment/job1/video/seg.ts")
        self.assertEqual(resp.status_code, 200)
        schedule_prefetch.assert_called_once_with("job1", "video/seg.ts")

    def test_serve_segment_download_failure(self):
        def _close_coro(coro):
            coro.close()
            return None
        with patch("app.get_segment_info", return_value={"file_id": "fid", "bot_index": 0}), \
             patch.object(app_module._segment_cache, "get", return_value=None), \
             patch("app._run_async", side_effect=_close_coro):
            resp = self.client.get("/segment/job1/video/seg.ts")
        self.assertEqual(resp.status_code, 500)

    def test_schedule_segment_prefetch_skips_when_disabled(self):
        with patch.object(app_module.Config, "SEGMENT_PREFETCH_COUNT", 0), \
             patch("app.db.get_segments_for_prefix") as get_segments:
            app_module._schedule_segment_prefetch("job1", "video/video_0001.ts")
        get_segments.assert_not_called()

    def test_schedule_segment_prefetch_schedules_next_uncached_segment(self):
        segments = [
            {"segment_key": "video/video_0001.ts", "duration": 4},
            {"segment_key": "video/video_0002.ts", "duration": 4},
            {"segment_key": "video/video_0003.ts", "duration": 4},
        ]
        with patch.object(app_module.Config, "SEGMENT_PREFETCH_COUNT", 2), \
             patch.object(app_module.Config, "SEGMENT_PREFETCH_MIN_FREE_BYTES", 0), \
             patch("app.db.get_segments_for_prefix", return_value=segments), \
             patch.object(app_module._segment_cache, "get", side_effect=[None, None]), \
             patch.object(app_module._async_loop, "call_soon_threadsafe") as call_soon:
            app_module._schedule_segment_prefetch("job1", "video/video_0001.ts")
        self.assertEqual(call_soon.call_count, 2)
        scheduled_args = [call.args[2] for call in call_soon.call_args_list]
        self.assertEqual(scheduled_args, ["video/video_0002.ts", "video/video_0003.ts"])

    def test_schedule_segment_prefetch_skips_already_cached_next_segment(self):
        segments = [
            {"segment_key": "video/video_0001.ts", "duration": 4},
            {"segment_key": "video/video_0002.ts", "duration": 4},
        ]
        with patch.object(app_module.Config, "SEGMENT_PREFETCH_COUNT", 1), \
             patch.object(app_module.Config, "SEGMENT_PREFETCH_MIN_FREE_BYTES", 0), \
             patch("app.db.get_segments_for_prefix", return_value=segments), \
             patch.object(app_module._segment_cache, "get", return_value=b"cached"), \
             patch.object(app_module._async_loop, "call_soon_threadsafe") as call_soon:
            app_module._schedule_segment_prefetch("job1", "video/video_0001.ts")
        call_soon.assert_not_called()

    def test_schedule_segment_prefetch_dedupes_inflight_segment(self):
        segments = [
            {"segment_key": "video/video_0001.ts", "duration": 4},
            {"segment_key": "video/video_0002.ts", "duration": 4},
        ]
        with patch.object(app_module.Config, "SEGMENT_PREFETCH_COUNT", 1), \
             patch.object(app_module.Config, "SEGMENT_PREFETCH_MIN_FREE_BYTES", 0), \
             patch("app.db.get_segments_for_prefix", return_value=segments), \
             patch.object(app_module._segment_cache, "get", return_value=None), \
             patch.object(app_module._async_loop, "call_soon_threadsafe") as call_soon:
            app_module._schedule_segment_prefetch("job1", "video/video_0001.ts")
            app_module._schedule_segment_prefetch("job1", "video/video_0001.ts")
        call_soon.assert_called_once()

    def test_prefetch_segment_failure_does_not_leave_inflight_marker(self):
        cache_key = "job1/video/video_0002.ts"
        app_module._segment_prefetch_inflight.add(cache_key)
        with patch.object(app_module._segment_cache, "get", return_value=None), \
             patch("app.get_segment_info", return_value={"file_id": "fid", "bot_index": 0}), \
             patch("app._download_segment_bytes", side_effect=RuntimeError("boom")):
            app_module._run_async(app_module._prefetch_segment("job1", "video/video_0002.ts"))
        self.assertNotIn(cache_key, app_module._segment_prefetch_inflight)

    # ─── CORS headers ───

    def test_cors_header_on_hls_endpoint_allowed_origin(self):
        with patch("app.generate_master_playlist", return_value="#EXTM3U\n"), \
             patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", ["https://player.example.com"]):
            resp = self.client.get(
                "/hls/job1/master.m3u8",
                headers={"Origin": "https://player.example.com"},
            )
        self.assertIn("Access-Control-Allow-Origin", resp.headers)
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "https://player.example.com")

    def test_cors_header_not_set_for_disallowed_origin(self):
        with patch("app.generate_master_playlist", return_value="#EXTM3U\n"), \
             patch.object(app_module.Config, "CORS_ALLOWED_ORIGINS", ["https://allowed.example.com"]):
            resp = self.client.get(
                "/hls/job1/master.m3u8",
                headers={"Origin": "https://evil.example.com"},
            )
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    # ─── _cleanup_expired_pending_uploads ───

    def test_cleanup_expired_removes_stale_uploads(self):
        upload_id = self._init_upload(total_size=4, total_chunks=1)
        # Manually age the upload beyond the TTL
        app_module._pending_uploads[upload_id]["last_activity_ts"] = time.time() - 999999
        with patch.object(app_module.Config, "PENDING_UPLOAD_TTL_SECONDS", 1):
            app_module._cleanup_expired_pending_uploads(force=True)
        self.assertNotIn(upload_id, app_module._pending_uploads)

    def test_cleanup_fresh_upload_not_removed(self):
        upload_id = self._init_upload(total_size=4, total_chunks=1)
        with patch.object(app_module.Config, "PENDING_UPLOAD_TTL_SECONDS", 9999):
            app_module._cleanup_expired_pending_uploads(force=True)
        self.assertIn(upload_id, app_module._pending_uploads)

    # ─── Rate limiting ───

    def test_rate_limit_disabled_when_max_requests_zero(self):
        with patch.object(app_module.Config, "UPLOAD_RATE_LIMIT_MAX_REQUESTS", 0):
            with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "1.2.3.4"}):
                result = app_module._check_rate_limit()
        self.assertIsNone(result)

    def test_rate_limit_allows_under_threshold(self):
        with patch.object(app_module.Config, "UPLOAD_RATE_LIMIT_MAX_REQUESTS", 5), \
             patch.object(app_module.Config, "UPLOAD_RATE_LIMIT_WINDOW", 60), \
             patch.object(app_module.Config, "BEHIND_PROXY", False):
            # First 5 requests should pass
            for _ in range(5):
                with app_module.app.test_request_context(
                    "/", environ_base={"REMOTE_ADDR": "5.5.5.5"}
                ):
                    self.assertIsNone(app_module._check_rate_limit())

    def test_rate_limit_rejects_on_threshold_exceeded(self):
        with patch.object(app_module.Config, "UPLOAD_RATE_LIMIT_MAX_REQUESTS", 2), \
             patch.object(app_module.Config, "UPLOAD_RATE_LIMIT_WINDOW", 60), \
             patch.object(app_module.Config, "BEHIND_PROXY", False):
            for _ in range(2):
                with app_module.app.test_request_context(
                    "/", environ_base={"REMOTE_ADDR": "6.6.6.6"}
                ):
                    app_module._check_rate_limit()
            # Third request should be rate-limited
            with app_module.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "6.6.6.6"}
            ):
                result = app_module._check_rate_limit()
            self.assertIsNotNone(result)


class TestPlaybackAuth(unittest.TestCase):
    """P4a: HMAC playback token generation and route enforcement."""

    def setUp(self):
        _reset_state()
        self.client = app_module.app.test_client()

    def test_generate_playback_token_disabled_returns_none(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", ""):
            self.assertIsNone(app_module._generate_playback_token("job1"))

    def test_generate_playback_token_returns_hex_string(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "supersecret"):
            token = app_module._generate_playback_token("job1")
            self.assertIsNotNone(token)
            self.assertEqual(len(token), 64)  # SHA-256 hex digest
            int(token, 16)  # must be valid hex

    def test_generate_playback_token_is_deterministic(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "s3cr3t"):
            t1 = app_module._generate_playback_token("abc")
            t2 = app_module._generate_playback_token("abc")
            self.assertEqual(t1, t2)

    def test_generate_playback_token_differs_per_job(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "s3cr3t"):
            self.assertNotEqual(
                app_module._generate_playback_token("job1"),
                app_module._generate_playback_token("job2"),
            )

    def test_require_playback_auth_disabled_returns_none(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", ""):
            with app_module.app.test_request_context("/?token=whatever"):
                result = app_module._require_playback_auth("job1")
        self.assertIsNone(result)

    def test_require_playback_auth_valid_token_returns_none(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "mykey"):
            token = app_module._generate_playback_token("job1")
            with app_module.app.test_request_context(f"/?token={token}"):
                result = app_module._require_playback_auth("job1")
        self.assertIsNone(result)

    def test_require_playback_auth_missing_token_returns_403(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "mykey"):
            with app_module.app.test_request_context("/"):
                response, status = app_module._require_playback_auth("job1")
        self.assertEqual(status, 403)

    def test_require_playback_auth_wrong_token_returns_403(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "mykey"):
            with app_module.app.test_request_context("/?token=wrongtoken"):
                response, status = app_module._require_playback_auth("job1")
        self.assertEqual(status, 403)

    def test_master_playlist_requires_token_when_secret_set(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "testkey"), \
             patch("app.generate_master_playlist", return_value="#EXTM3U\n"):
            r = self.client.get("/hls/job1/master.m3u8")
            self.assertEqual(r.status_code, 403)

    def test_master_playlist_accessible_without_secret(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", ""), \
             patch("app.generate_master_playlist", return_value="#EXTM3U\n"):
            r = self.client.get("/hls/job1/master.m3u8")
            self.assertEqual(r.status_code, 200)

    def test_master_playlist_accessible_with_valid_token(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "testkey"), \
             patch("app.generate_master_playlist", return_value="#EXTM3U\n"):
            token = app_module._generate_playback_token("job1")
            r = self.client.get(f"/hls/job1/master.m3u8?token={token}")
            self.assertEqual(r.status_code, 200)

    def test_token_endpoint_returns_token(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", "testkey"), \
             patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""), \
             patch("app.get_job", return_value={"job_id": "job1"}):
            r = self.client.get("/api/jobs/job1/token")
            self.assertEqual(r.status_code, 200)
            data = r.get_json()
            self.assertIn("token", data)
            self.assertEqual(data["token"], app_module._generate_playback_token("job1"))

    def test_token_endpoint_returns_null_when_secret_unset(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", ""), \
             patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""), \
             patch("app.get_job", return_value={"job_id": "job1"}):
            r = self.client.get("/api/jobs/job1/token")
            self.assertEqual(r.status_code, 200)
            data = r.get_json()
            self.assertIsNone(data["token"])

    def test_token_endpoint_404_for_missing_job(self):
        with patch.object(app_module.Config, "PLAYBACK_SECRET", ""), \
             patch.object(app_module.Config, "UPLOAD_API_KEY", ""), \
             patch.object(app_module.Config, "UPLOAD_BASIC_USER", ""), \
             patch("app.get_job", return_value=None):
            r = self.client.get("/api/jobs/nosuchjob/token")
            self.assertEqual(r.status_code, 404)


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.client = app_module.app.test_client()

    def test_health_ok(self):
        healthy_bots = [
            {"index": 0, "channel_id": -1001, "ok": True, "error": None},
            {"index": 1, "channel_id": -1002, "ok": True, "error": None},
        ]
        with patch("app.db.get_job", return_value=None), \
             patch.object(app_module._telegram_uploader, "bots", [{}, {}]), \
             patch.object(app_module._telegram_uploader, "probe_health", new=AsyncMock(return_value=healthy_bots)):
            resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["db"])
        self.assertEqual(data["bots_configured"], 2)
        self.assertEqual(data["bots_healthy"], 2)
        self.assertEqual(data["bots"], healthy_bots)

    def test_health_db_failure(self):
        healthy_bots = [{"index": 0, "channel_id": -1001, "ok": True, "error": None}]
        with patch("app.db.get_job", side_effect=Exception("db broken")), \
             patch.object(app_module._telegram_uploader, "bots", [{}]), \
             patch.object(app_module._telegram_uploader, "probe_health", new=AsyncMock(return_value=healthy_bots)):
            resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["db"])

    def test_health_degraded_when_any_bot_unhealthy(self):
        bot_results = [
            {"index": 0, "channel_id": -1001, "ok": True, "error": None},
            {"index": 1, "channel_id": -1002, "ok": False, "error": "forbidden"},
        ]
        with patch("app.db.get_job", return_value=None), \
             patch.object(app_module._telegram_uploader, "bots", [{}, {}]), \
             patch.object(app_module._telegram_uploader, "probe_health", new=AsyncMock(return_value=bot_results)):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["bots_healthy"], 1)
        self.assertEqual(data["bots"], bot_results)

    def test_health_degraded_when_no_bots_configured(self):
        with patch("app.db.get_job", return_value=None), \
             patch.object(app_module._telegram_uploader, "bots", []), \
             patch.object(app_module._telegram_uploader, "probe_health", new=AsyncMock(return_value=[])):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["bots_configured"], 0)
        self.assertEqual(data["bots_healthy"], 0)

    def test_health_degraded_when_probe_times_out(self):
        with patch("app.db.get_job", return_value=None), \
             patch.object(app_module._telegram_uploader, "bots", [{}]), \
             patch("app._run_async", side_effect=app_module.concurrent.futures.TimeoutError):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["bots_healthy"], 0)
        self.assertEqual(data["bots"][0]["error"], "timeout")

    def test_health_degraded_when_probe_raises(self):
        with patch("app.db.get_job", return_value=None), \
             patch.object(app_module._telegram_uploader, "bots", [{}]), \
             patch("app._run_async", side_effect=RuntimeError("boom")):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["bots"][0]["error"], "probe_error: RuntimeError")


if __name__ == "__main__":
    unittest.main()
