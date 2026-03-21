"""Lightweight in-process metrics collection for operational visibility."""

import collections
import threading
import time


def _normalize_error_label(error):
    if error is None:
        return None
    if isinstance(error, BaseException):
        name = error.__class__.__name__.strip().lower()
        return name or "error"
    text = str(error).strip().lower()
    return text or "error"


class MetricsCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = collections.Counter()
        self._timings = {}

    def reset(self):
        with self._lock:
            self._counters = collections.Counter()
            self._timings = {}

    def increment(self, name, amount=1):
        with self._lock:
            self._counters[name] += amount

    def record_timing(self, name, duration_ms, error=None):
        with self._lock:
            record = self._timings.setdefault(name, {
                "calls": 0,
                "errors": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "error_counts": collections.Counter(),
            })
            record["calls"] += 1
            record["total_ms"] += float(duration_ms)
            record["max_ms"] = max(record["max_ms"], float(duration_ms))
            label = _normalize_error_label(error)
            if label is not None:
                record["errors"] += 1
                record["error_counts"][label] += 1

    def timer(self, name):
        return _Timer(self, name)

    def get_counter(self, name):
        with self._lock:
            return int(self._counters.get(name, 0))

    def get_timing(self, name):
        with self._lock:
            record = self._timings.get(name)
            if record is None:
                return {
                    "calls": 0,
                    "errors": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                    "error_counts": {},
                }
            return {
                "calls": int(record["calls"]),
                "errors": int(record["errors"]),
                "total_ms": round(record["total_ms"], 3),
                "max_ms": round(record["max_ms"], 3),
                "error_counts": dict(record["error_counts"]),
            }


class _Timer:
    def __init__(self, collector, name):
        self._collector = collector
        self._name = name
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        self._collector.record_timing(self._name, duration_ms, error=exc)
        return False


metrics = MetricsCollector()
