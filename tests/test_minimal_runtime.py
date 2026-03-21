import importlib
import os
import sys
import tempfile
import threading
import types
import unittest
import asyncio
from unittest.mock import patch


def _install_stub_modules():
    flask_mod = types.ModuleType("flask")
    werkzeug_mod = types.ModuleType("werkzeug")
    werkzeug_utils_mod = types.ModuleType("werkzeug.utils")
    telegram_mod = types.ModuleType("telegram")
    telegram_error_mod = types.ModuleType("telegram.error")
    telegram_request_mod = types.ModuleType("telegram.request")
    aiohttp_mod = types.ModuleType("aiohttp")
    dotenv_mod = types.ModuleType("dotenv")

    class StubFlask:
        def __init__(self, *_args, **_kwargs):
            self.config = {}

        def route(self, *_args, **_kwargs):
            return lambda func: func

        def after_request(self, func):
            return func

        def teardown_request(self, func):
            return func

    class StubResponse:
        def __init__(self, data=None, content_type=None, headers=None):
            self.data = data
            self.content_type = content_type
            self.headers = headers or {}

    class StubClientSession:
        def __init__(self, *args, **kwargs):
            self.closed = False

        async def close(self):
            self.closed = True

    class StubBot:
        def __init__(self, *args, **kwargs):
            pass

    class StubHTTPXRequest:
        def __init__(self, *args, **kwargs):
            pass

    flask_mod.Flask = StubFlask
    flask_mod.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
    flask_mod.render_template = lambda *_args, **_kwargs: ""
    flask_mod.request = types.SimpleNamespace(headers={}, args={}, remote_addr="127.0.0.1", path="/")
    flask_mod.Response = StubResponse
    flask_mod.stream_with_context = lambda iterable: iterable
    werkzeug_utils_mod.secure_filename = lambda name: name
    telegram_mod.Bot = StubBot
    telegram_request_mod.HTTPXRequest = StubHTTPXRequest
    telegram_error_mod.RetryAfter = Exception
    telegram_error_mod.NetworkError = Exception
    telegram_error_mod.TimedOut = Exception
    telegram_error_mod.BadRequest = Exception
    telegram_error_mod.Forbidden = Exception
    aiohttp_mod.ClientSession = StubClientSession
    dotenv_mod.load_dotenv = lambda *args, **kwargs: False

    sys.modules["flask"] = flask_mod
    sys.modules["werkzeug"] = werkzeug_mod
    sys.modules["werkzeug.utils"] = werkzeug_utils_mod
    sys.modules["telegram"] = telegram_mod
    sys.modules["telegram.error"] = telegram_error_mod
    sys.modules["telegram.request"] = telegram_request_mod
    sys.modules["aiohttp"] = aiohttp_mod
    sys.modules["dotenv"] = dotenv_mod


class TestMinimalRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stub_modules()
        for name in ("config", "metrics", "telegram_uploader", "database", "app"):
            sys.modules.pop(name, None)
        cls._orig_run_coroutine_threadsafe = asyncio.run_coroutine_threadsafe

        def _run_immediately(coro, _loop):
            result = asyncio.new_event_loop().run_until_complete(coro)

            class _CompletedFuture:
                def result(self, timeout=None):
                    return result

            return _CompletedFuture()

        asyncio.run_coroutine_threadsafe = _run_immediately
        cls.config = importlib.import_module("config")
        cls.metrics = importlib.import_module("metrics")
        cls.telegram_uploader = importlib.import_module("telegram_uploader")
        cls.database = importlib.import_module("database")
        cls.app = importlib.import_module("app")
        asyncio.run_coroutine_threadsafe = cls._orig_run_coroutine_threadsafe

    @classmethod
    def tearDownClass(cls):
        cls.database._close_all_connections()
        asyncio.run_coroutine_threadsafe = cls._orig_run_coroutine_threadsafe
        cls.app._shutdown_persistent_loop()

    def test_imports_succeed_with_dependency_stubs(self):
        self.assertTrue(hasattr(self.app, "app"))
        self.assertTrue(hasattr(self.app, "api_metrics"))
        self.assertTrue(hasattr(self.telegram_uploader, "TelegramUploader"))
        self.assertTrue(hasattr(self.database, "backup_database"))
        self.assertTrue(hasattr(self.database, "export_database_json"))

    def test_database_backup_and_export_work_with_minimal_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "runtime.db")
            backup_path = os.path.join(temp_dir, "runtime.sqlite3")
            export_path = os.path.join(temp_dir, "runtime.json")

            with patch.object(self.database, "DB_PATH", db_path):
                self.database._close_all_connections()
                self.database._local = threading.local()
                self.database.init_db()
                backup_result = self.database.backup_database(backup_path)
                export_result = self.database.export_database_json(export_path)

            self.assertEqual(backup_result, backup_path)
            self.assertEqual(export_result, export_path)
            self.assertTrue(os.path.exists(backup_path))
            self.assertTrue(os.path.exists(export_path))
