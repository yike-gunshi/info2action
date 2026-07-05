"""BF-0515-twitter-image-perf — tests for in-memory LRU cache on Twitter image proxy.

Scope:
- _TwitterImageLRU class: get/put/eviction/maxsize/thread-safety
- twitter_photo_proxy: warm hit avoids Supabase Storage; cold path still works
- twitter_poster: warm hit avoids ensure_twitter_poster_cached
- 304 If-None-Match short-circuits before LRU read (existing behavior preserved)
- Fallback when Storage download fails: cold path engages
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from routes import media as media_mod  # noqa: E402
import asset_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_asset_cache(tmp_path, monkeypatch):
    """B1: media 代理接入磁盘缓存后,测试必须隔离缓存目录与负缓存。"""
    monkeypatch.setenv(asset_cache.ASSET_CACHE_DIR_ENV, str(tmp_path / 'asset_cache'))
    asset_cache.clear()
    yield
    asset_cache.clear()



# ── _TwitterImageLRU unit tests ────────────────────────────────────────


def test_lru_get_miss_returns_none():
    lru = media_mod._TwitterImageLRU(maxsize=8)
    assert lru.get(("photo", "abc", 0)) is None


def test_lru_put_then_get_returns_value():
    lru = media_mod._TwitterImageLRU(maxsize=8)
    lru.put(("photo", "abc", 0), b"xx", "image/jpeg")
    got = lru.get(("photo", "abc", 0))
    assert got is not None
    data, ctype = got
    assert data == b"xx"
    assert ctype == "image/jpeg"


def test_lru_evicts_oldest_when_full():
    """maxsize=3 + insert 4 distinct entries → first one evicted (LRU)."""
    lru = media_mod._TwitterImageLRU(maxsize=3)
    lru.put(("photo", "a", 0), b"a", "image/jpeg")
    lru.put(("photo", "b", 0), b"b", "image/jpeg")
    lru.put(("photo", "c", 0), b"c", "image/jpeg")
    lru.put(("photo", "d", 0), b"d", "image/jpeg")  # evict ("photo","a",0)

    assert lru.get(("photo", "a", 0)) is None  # evicted
    assert lru.get(("photo", "d", 0)) is not None
    assert len(lru) == 3


def test_lru_get_promotes_to_recent():
    """Touching an entry should move it to MRU end so it's not next victim."""
    lru = media_mod._TwitterImageLRU(maxsize=3)
    lru.put(("k", 1), b"1", "image/jpeg")
    lru.put(("k", 2), b"2", "image/jpeg")
    lru.put(("k", 3), b"3", "image/jpeg")
    # touch oldest
    lru.get(("k", 1))
    # adding a 4th should evict ("k", 2) (now oldest), not ("k", 1)
    lru.put(("k", 4), b"4", "image/jpeg")
    assert lru.get(("k", 1)) is not None
    assert lru.get(("k", 2)) is None
    assert lru.get(("k", 3)) is not None
    assert lru.get(("k", 4)) is not None


def test_lru_clear():
    lru = media_mod._TwitterImageLRU(maxsize=8)
    lru.put(("k", 1), b"1", "image/jpeg")
    lru.put(("k", 2), b"2", "image/jpeg")
    lru.clear()
    assert lru.get(("k", 1)) is None
    assert len(lru) == 0


def test_lru_thread_safety_simple():
    """Concurrent put/get from many threads should not raise + final size <= maxsize."""
    lru = media_mod._TwitterImageLRU(maxsize=50)

    def worker(n):
        for i in range(100):
            lru.put((n, i), bytes([i & 0xFF]), "image/jpeg")
            lru.get((n, i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # size MUST be exactly the maxsize cap (not exceed it)
    assert len(lru) <= 50


def test_lru_size_from_env(monkeypatch):
    """TWITTER_IMAGE_LRU_SIZE env var overrides default."""
    monkeypatch.setenv("TWITTER_IMAGE_LRU_SIZE", "5")
    size = media_mod._lru_size_from_env()
    assert size == 5


def test_lru_size_from_env_default(monkeypatch):
    monkeypatch.delenv("TWITTER_IMAGE_LRU_SIZE", raising=False)
    size = media_mod._lru_size_from_env()
    assert size == 200  # documented default


def test_lru_size_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("TWITTER_IMAGE_LRU_SIZE", "not-a-number")
    size = media_mod._lru_size_from_env()
    assert size == 200


# ── Cold media path pool protection ───────────────────────────────────


def test_media_cold_path_singleflight_coalesces_same_key():
    call_count = 0
    counter_lock = threading.Lock()

    def slow_fetch():
        nonlocal call_count
        with counter_lock:
            call_count += 1
        time.sleep(0.1)
        return b"shared"

    results = [None] * 8
    threads = [
        threading.Thread(
            target=lambda i=i: results.__setitem__(
                i,
                media_mod._run_media_cold_path(("photo", "same", 0), slow_fetch),
            )
        )
        for i in range(len(results))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert call_count == 1
    assert results == [b"shared"] * len(results)


def test_media_cold_path_semaphore_limits_different_keys(monkeypatch):
    monkeypatch.setattr(media_mod, "_media_cold_path_slots", threading.BoundedSemaphore(1))

    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def slow_fetch(key):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.08)
        with active_lock:
            active -= 1
        return key

    results = []
    threads = [
        threading.Thread(
            target=lambda key=key: results.append(
                media_mod._run_media_cold_path(("photo", key, 0), slow_fetch, key)
            )
        )
        for key in ("a", "b", "c")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["a", "b", "c"]
    assert max_active == 1


# ── Endpoint integration: photo warm hit avoids Supabase Storage ─────────


def _build_test_client(monkeypatch):
    """Spin up FastAPI app with media routes only."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Force remote-storage mode so the hot path is the one we're optimising.
    monkeypatch.setattr(media_mod.remote_db, "asset_storage_to_remote", lambda: True)
    monkeypatch.setattr(media_mod.remote_db, "app_state_to_remote", lambda: False)
    monkeypatch.setattr(media_mod.remote_db, "feed_read_from_remote", lambda: False)

    # Stub _get_twitter_photo_url so we never need a DB row.
    monkeypatch.setattr(
        media_mod,
        "_get_twitter_photo_url",
        lambda item_id, idx: f"https://pbs.twimg.com/media/fake-{item_id}-{idx}.jpg",
    )

    # Reset the module-level LRU between tests so independent runs don't share state.
    media_mod._twitter_image_lru.clear()

    app = FastAPI()
    app.include_router(media_mod.router)
    return TestClient(app)


def test_photo_lru_hit_skips_storage(monkeypatch):
    """Second request for same key reads from LRU, not Supabase Storage."""
    client = _build_test_client(monkeypatch)

    storage_calls = {"n": 0}

    def fake_download(path):
        storage_calls["n"] += 1
        return b"\xff\xd8\xff\xe0fake-jpeg"

    monkeypatch.setattr(media_mod.remote_db, "download_asset_bytes_remote", fake_download)
    monkeypatch.setattr(
        media_mod.remote_db,
        "upload_asset_bytes_remote",
        MagicMock(),
    )

    # First request — LRU miss, Storage download invoked.
    r1 = client.get("/api/media/twitter-photo/12345/0.jpg")
    assert r1.status_code == 200
    assert r1.content == b"\xff\xd8\xff\xe0fake-jpeg"
    assert "ETag" in r1.headers
    etag1 = r1.headers["ETag"]
    assert storage_calls["n"] == 1

    # Second request — should hit LRU, no new Storage call.
    r2 = client.get("/api/media/twitter-photo/12345/0.jpg")
    assert r2.status_code == 200
    assert r2.content == b"\xff\xd8\xff\xe0fake-jpeg"
    assert r2.headers["ETag"] == etag1
    assert storage_calls["n"] == 1, "second request must NOT call Supabase Storage"


def test_photo_if_none_match_returns_304_before_lru(monkeypatch):
    """If-None-Match should short-circuit to 304 even on first request."""
    client = _build_test_client(monkeypatch)

    storage_calls = {"n": 0}

    def fake_download(path):
        storage_calls["n"] += 1
        return b"some-bytes"

    monkeypatch.setattr(media_mod.remote_db, "download_asset_bytes_remote", fake_download)

    etag = media_mod._make_etag("photo", "12345", 0)
    r = client.get(
        "/api/media/twitter-photo/12345/0.jpg",
        headers={"If-None-Match": etag},
    )
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers.get("ETag") == etag
    assert storage_calls["n"] == 0, "304 path must not touch Storage"


def test_photo_wrong_if_none_match_returns_200(monkeypatch):
    """Wrong ETag in If-None-Match → still serve full bytes."""
    client = _build_test_client(monkeypatch)
    monkeypatch.setattr(
        media_mod.remote_db,
        "download_asset_bytes_remote",
        lambda p: b"jpeg-bytes",
    )

    r = client.get(
        "/api/media/twitter-photo/12345/0.jpg",
        headers={"If-None-Match": '"wrong-etag"'},
    )
    assert r.status_code == 200
    assert r.content == b"jpeg-bytes"


def test_photo_storage_miss_falls_back_to_cdn(monkeypatch):
    """Storage download returns None → must fall back to fetching Twitter CDN bytes."""
    client = _build_test_client(monkeypatch)

    monkeypatch.setattr(
        media_mod.remote_db,
        "download_asset_bytes_remote",
        lambda p: None,
    )
    monkeypatch.setattr(media_mod.remote_db, "upload_asset_bytes_remote", MagicMock())

    fake_resp = MagicMock()
    fake_resp.read.return_value = b"cdn-bytes"
    fake_resp.headers = {"Content-Type": "image/jpeg"}
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None

    fake_opener = MagicMock()
    fake_opener.open.return_value = fake_resp
    monkeypatch.setattr(media_mod, "_twitter_cdn_opener", lambda: fake_opener)

    r = client.get("/api/media/twitter-photo/99/0.jpg")
    assert r.status_code == 200
    assert r.content == b"cdn-bytes"


def test_photo_storage_miss_runs_cdn_fetch_in_threadpool(monkeypatch):
    """Cold CDN fetch/upload guard must not run on the FastAPI event loop."""
    client = _build_test_client(monkeypatch)
    threadpool_calls = []

    async def fake_run_in_threadpool(func, *args, **kwargs):
        threadpool_calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)

    monkeypatch.setattr(media_mod, "run_in_threadpool", fake_run_in_threadpool)
    monkeypatch.setattr(
        media_mod.remote_db,
        "download_asset_bytes_remote",
        lambda p: None,
    )
    monkeypatch.setattr(media_mod.remote_db, "upload_asset_bytes_remote", MagicMock())

    fake_resp = MagicMock()
    fake_resp.read.return_value = b"cdn-bytes"
    fake_resp.headers = {"Content-Type": "image/jpeg"}
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None

    fake_opener = MagicMock()
    fake_opener.open.return_value = fake_resp
    monkeypatch.setattr(media_mod, "_twitter_cdn_opener", lambda: fake_opener)

    r = client.get("/api/media/twitter-photo/100/0.jpg")
    assert r.status_code == 200
    assert r.content == b"cdn-bytes"
    assert "_run_media_cold_path" in threadpool_calls


def test_photo_storage_miss_uses_cold_path_guard(monkeypatch):
    client = _build_test_client(monkeypatch)
    guard_calls = []

    def fake_guard(key, func, *args):
        guard_calls.append((key, getattr(func, "__name__", repr(func)), args))
        return func(*args)

    monkeypatch.setattr(media_mod, "_run_media_cold_path", fake_guard)
    monkeypatch.setattr(
        media_mod.remote_db,
        "download_asset_bytes_remote",
        lambda p: None,
    )
    monkeypatch.setattr(media_mod.remote_db, "upload_asset_bytes_remote", MagicMock())

    fake_resp = MagicMock()
    fake_resp.read.return_value = b"cdn-bytes"
    fake_resp.headers = {"Content-Type": "image/jpeg"}
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None

    fake_opener = MagicMock()
    fake_opener.open.return_value = fake_resp
    monkeypatch.setattr(media_mod, "_twitter_cdn_opener", lambda: fake_opener)

    r = client.get("/api/media/twitter-photo/101/0.jpg")
    assert r.status_code == 200
    assert guard_calls == [
        (("photo", "101", 0), "_fetch_twitter_photo_from_cdn", ("101", 0))
    ]


def test_photo_lru_distinct_keys(monkeypatch):
    """Different (item_id, idx) tuples must not share cache entries."""
    client = _build_test_client(monkeypatch)

    calls = []

    def fake_download(path):
        calls.append(path)
        return ("BYTES-" + path).encode()

    monkeypatch.setattr(media_mod.remote_db, "download_asset_bytes_remote", fake_download)
    monkeypatch.setattr(media_mod.remote_db, "upload_asset_bytes_remote", MagicMock())

    r1 = client.get("/api/media/twitter-photo/aaa/0.jpg")
    r2 = client.get("/api/media/twitter-photo/aaa/1.jpg")
    r3 = client.get("/api/media/twitter-photo/aaa/0.jpg")  # warm

    assert r1.content == b"BYTES-twitter_photos/aaa/0.jpg"
    assert r2.content == b"BYTES-twitter_photos/aaa/1.jpg"
    assert r3.content == b"BYTES-twitter_photos/aaa/0.jpg"
    # Storage hit exactly twice (idx 0 once, idx 1 once); third request served from LRU.
    assert len(calls) == 2


# ── Endpoint integration: poster warm hit avoids ensure_*_cached ─────────


def test_poster_lru_hit_skips_ensure(monkeypatch):
    client = _build_test_client(monkeypatch)
    ensure_calls = {"n": 0}

    def fake_ensure(item_id):
        ensure_calls["n"] += 1
        return b"poster-bytes"

    monkeypatch.setattr(media_mod, "ensure_twitter_poster_cached", fake_ensure)

    r1 = client.get("/api/media/twitter-poster/v1.jpg")
    assert r1.status_code == 200
    assert ensure_calls["n"] == 1

    r2 = client.get("/api/media/twitter-poster/v1.jpg")
    assert r2.status_code == 200
    assert ensure_calls["n"] == 1, "warm poster must not re-enter ensure_twitter_poster_cached"


def test_poster_storage_miss_uses_cold_path_guard(monkeypatch):
    client = _build_test_client(monkeypatch)
    guard_calls = []

    def fake_guard(key, func, *args):
        guard_calls.append((key, getattr(func, "__name__", repr(func)), args))
        return func(*args)

    def fake_ensure_twitter_poster_cached(item_id):
        return b"poster"

    monkeypatch.setattr(media_mod, "_run_media_cold_path", fake_guard)
    monkeypatch.setattr(media_mod, "ensure_twitter_poster_cached", fake_ensure_twitter_poster_cached)

    r = client.get("/api/media/twitter-poster/v2.jpg")
    assert r.status_code == 200
    assert r.content == b"poster"
    assert guard_calls == [
        (("poster", "v2"), "fake_ensure_twitter_poster_cached", ("v2",))
    ]
