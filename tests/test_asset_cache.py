"""P0-4 (C端放量) — tests for the disk-backed remote asset cache.

Scope:
- get_or_fetch: miss -> fetch once -> disk hit without re-fetch
- negative cache: fetch None cached for TTL, expires afterwards
- singleflight: concurrent misses on the same key trigger one fetch
- eviction: size cap enforced, oldest-mtime entries removed first
- disabled mode passes through to fetch every time
- fetch exceptions propagate and are not cached
- /images and /lw routes reuse cached bytes instead of re-downloading
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import asset_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(asset_cache.ASSET_CACHE_DIR_ENV, str(tmp_path / "asset_cache"))
    monkeypatch.delenv(asset_cache.ASSET_CACHE_DISABLED_ENV, raising=False)
    monkeypatch.delenv(asset_cache.ASSET_CACHE_MAX_MB_ENV, raising=False)
    monkeypatch.delenv(asset_cache.ASSET_CACHE_NEGATIVE_TTL_ENV, raising=False)
    asset_cache.clear()
    yield
    asset_cache.clear()


class _CountingFetch:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        return self.result


# ── hit / miss ──────────────────────────────────────────────


def test_miss_fetches_then_hits_disk():
    fetch = _CountingFetch(b"payload")
    assert asset_cache.get_or_fetch("images/a.jpg", fetch) == b"payload"
    assert asset_cache.get_or_fetch("images/a.jpg", fetch) == b"payload"
    assert fetch.calls == 1


def test_distinct_keys_do_not_collide():
    a = _CountingFetch(b"aaa")
    b = _CountingFetch(b"bbb")
    assert asset_cache.get_or_fetch("images/a.jpg", a) == b"aaa"
    assert asset_cache.get_or_fetch("images/b.jpg", b) == b"bbb"
    assert (a.calls, b.calls) == (1, 1)


def test_cache_survives_module_state_reset():
    """Disk entries outlive process-local state (restart simulation)."""
    fetch = _CountingFetch(b"persist")
    asset_cache.get_or_fetch("images/persist.jpg", fetch)
    with asset_cache._negative_lock:
        asset_cache._negative.clear()
    fetch2 = _CountingFetch(b"other")
    assert asset_cache.get_or_fetch("images/persist.jpg", fetch2) == b"persist"
    assert fetch2.calls == 0


# ── negative cache ──────────────────────────────────────────


def test_missing_object_is_negative_cached():
    fetch = _CountingFetch(None)
    assert asset_cache.get_or_fetch("images/missing.jpg", fetch) is None
    assert asset_cache.get_or_fetch("images/missing.jpg", fetch) is None
    assert fetch.calls == 1


def test_negative_cache_expires(monkeypatch):
    fetch = _CountingFetch(None)
    asset_cache.get_or_fetch("images/gone.jpg", fetch)
    # Force-expire the entry rather than sleeping.
    with asset_cache._negative_lock:
        asset_cache._negative["images/gone.jpg"] = time.monotonic() - 1
    asset_cache.get_or_fetch("images/gone.jpg", fetch)
    assert fetch.calls == 2


def test_negative_ttl_zero_disables_negative_cache(monkeypatch):
    monkeypatch.setenv(asset_cache.ASSET_CACHE_NEGATIVE_TTL_ENV, "0")
    fetch = _CountingFetch(None)
    asset_cache.get_or_fetch("images/nocache.jpg", fetch)
    asset_cache.get_or_fetch("images/nocache.jpg", fetch)
    assert fetch.calls == 2


# ── singleflight ────────────────────────────────────────────


def test_concurrent_misses_fetch_once():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_fetch():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return b"slow"

    results = []

    def worker():
        results.append(asset_cache.get_or_fetch("images/slow.jpg", slow_fetch))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    assert started.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert results == [b"slow"] * 4
    assert len(calls) == 1


# ── eviction ────────────────────────────────────────────────


def test_sweep_evicts_oldest_when_over_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(asset_cache.ASSET_CACHE_MAX_MB_ENV, "1")  # 1MB cap
    payload = b"x" * (300 * 1024)  # 300KB each

    for i in range(4):  # 1.2MB total
        asset_cache.get_or_fetch(f"images/{i}.jpg", _CountingFetch(payload))
        # Distinct mtimes so LRU order is deterministic.
        path = asset_cache._entry_path(f"images/{i}.jpg")
        os.utime(path, (time.time() + i, time.time() + i))

    # Force the throttle to trigger on the next write, then write one more.
    with asset_cache._bytes_since_sweep_lock:
        asset_cache._bytes_since_sweep = 10 ** 9
    asset_cache.get_or_fetch("images/final.jpg", _CountingFetch(payload))

    total = sum(size for _, size, _ in asset_cache._iter_entries(asset_cache._cache_dir()))
    assert total <= 1024 * 1024
    # Oldest entry must be gone.
    assert not os.path.exists(asset_cache._entry_path("images/0.jpg"))


# ── disabled / error paths ──────────────────────────────────


def test_disabled_mode_passes_through(monkeypatch):
    monkeypatch.setenv(asset_cache.ASSET_CACHE_DISABLED_ENV, "1")
    fetch = _CountingFetch(b"data")
    asset_cache.get_or_fetch("images/x.jpg", fetch)
    asset_cache.get_or_fetch("images/x.jpg", fetch)
    assert fetch.calls == 2


def test_fetch_exception_propagates_and_not_cached():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("storage down")

    with pytest.raises(RuntimeError):
        asset_cache.get_or_fetch("images/err.jpg", boom)
    with pytest.raises(RuntimeError):
        asset_cache.get_or_fetch("images/err.jpg", boom)
    assert len(calls) == 2


# ── route integration ───────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    from fastapi.testclient import TestClient
    from app import app
    app.state.limiter.enabled = False
    # No context manager: match project convention and skip lifespan startup.
    return TestClient(app)


def test_serve_image_uses_cache(client):
    import remote_db
    with patch.object(remote_db, 'asset_storage_to_remote', return_value=True), \
         patch.object(remote_db, 'download_asset_bytes_remote',
                      return_value=b'\x89PNG fake') as dl:
        r1 = client.get('/images/covers/test-cache.png')
        r2 = client.get('/images/covers/test-cache.png')
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content == b'\x89PNG fake'
    assert dl.call_count == 1
    assert r1.headers['cache-control'] == 'public, max-age=86400'


def test_serve_image_missing_returns_404_once_downloaded(client):
    import remote_db
    with patch.object(remote_db, 'asset_storage_to_remote', return_value=True), \
         patch.object(remote_db, 'download_asset_bytes_remote', return_value=None) as dl:
        r1 = client.get('/images/covers/nope.png')
        r2 = client.get('/images/covers/nope.png')
    assert r1.status_code == 404 and r2.status_code == 404
    assert dl.call_count == 1  # negative cache absorbs the second request


def test_lingowhale_article_uses_cache(client):
    import remote_db
    with patch.object(remote_db, 'asset_storage_to_remote', return_value=True), \
         patch.object(remote_db, 'download_asset_bytes_remote',
                      return_value=b'<html>hi</html>') as dl:
        r1 = client.get('/lw/abc_123')
        r2 = client.get('/lw/abc_123')
    assert r1.status_code == 200 and r2.status_code == 200
    assert dl.call_count == 1
