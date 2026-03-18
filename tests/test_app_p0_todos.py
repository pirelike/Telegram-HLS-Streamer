import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Provide a lightweight telegram stub so importing app works in test envs.
telegram_mod = types.ModuleType("telegram")
telegram_error_mod = types.ModuleType("telegram.error")
telegram_mod.Bot = object
telegram_error_mod.RetryAfter = Exception
telegram_error_mod.NetworkError = Exception
telegram_error_mod.TimedOut = Exception
telegram_error_mod.BadRequest = Exception
telegram_error_mod.Unauthorized = Exception
sys.modules.setdefault("telegram", telegram_mod)
sys.modules.setdefault("telegram.error", telegram_error_mod)

import app as app_module


class TestP0TodoFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app_module._pending_uploads.clear()
        app_module._active_jobs.clear()
        self.upload_dir_patch = patch.object(app_module.Config, "UPLOAD_DIR", self.temp.name)
        self.chunk_size_patch = patch.object(app_module.Config, "UPLOAD_CHUNK_SIZE", 4)
        self.max_upload_size_patch = patch.object(app_module.Config, "MAX_UPLOAD_SIZE", 1024 * 1024)
        self.upload_dir_patch.start()
        self.chunk_size_patch.start()
        self.max_upload_size_patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
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

    def test_is_job_cancelled_uses_explicit_flag_not_error_text(self):
        app_module._active_jobs["job1"] = {
            "status": "error",
            "error": "operation timed out while uploading",
        }
        self.assertFalse(app_module._is_job_cancelled("job1"))

        app_module._active_jobs["job1"]["timed_out"] = True
        self.assertTrue(app_module._is_job_cancelled("job1"))

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

        out_of_order = self.client.post(
            "/api/upload/chunk",
            headers={"X-Upload-Id": upload_id, "X-Chunk-Index": "1"},
            data=b"BBBB",
        )
        self.assertEqual(out_of_order.status_code, 409)
        self.assertIn("file gap", out_of_order.get_json()["error"])

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


if __name__ == "__main__":
    unittest.main()
