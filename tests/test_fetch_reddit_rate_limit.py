"""BF-0716-1: reddit RSS 兜底的限流感知 pacing + 429 退避重试。

生产实证（2026-07-16 ECS curl）：
- 数据中心 IP 被 reddit 全部 JSON 端点 403（www/old/api，与 UA 无关）；
- 该 IP 未认证 RSS 配额 = 每 ~60s 窗口 1 次（x-ratelimit-remaining: 0.0）；
- 间隔 2s 的第二个 RSS 请求必 429 → 每周期只有 ORDER BY id 最前的源成功。
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr("db.DB_PATH", tmp.name)
    import db as _db

    _db._item_status_has_user_id = None
    yield tmp.name
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


class _FakeResponse:
    def __init__(self, *, status_code=200, content=b"", json_data=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        return self._json_data


class _FakeClock:
    """sleep 推进 monotonic 的假时钟，记录每次 sleep 秒数。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if seconds > 0:
            self.now += seconds

    def monotonic(self):
        return self.now


def _insert_source(conn, source_key, *, status="active"):
    cur = conn.execute(
        "INSERT INTO sources(platform, source_key, display_name, status, config_json, origin) "
        "VALUES('reddit', ?, NULL, ?, NULL, 'test')",
        (source_key, status),
    )
    conn.commit()
    return cur.lastrowid


def _source_row(conn, source_id):
    return conn.execute(
        """SELECT status, consecutive_failures, last_success_at, last_error
           FROM sources WHERE id = ?""",
        (source_id,),
    ).fetchone()


def _patch_local_fetch_backend(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)


def _patch_requests_get(monkeypatch, get):
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))


def _patch_feedparser(monkeypatch, sub="OpenAI"):
    monkeypatch.setitem(
        sys.modules,
        "feedparser",
        SimpleNamespace(
            parse=lambda content: SimpleNamespace(
                entries=[
                    {
                        "id": "t3_abc",
                        "title": "RSS post",
                        "link": f"https://www.reddit.com/r/{sub}/comments/abc/post/",
                        "author": "rss-user",
                        "summary": "Body",
                    }
                ]
            ),
        ),
    )


def _patch_clock(monkeypatch, clock):
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds.time, "sleep", clock.sleep)
    monkeypatch.setattr(fetch_feeds.time, "monotonic", clock.monotonic)


# ============================================================
# 纯函数：_reddit_ratelimit_seconds
# ============================================================

def test_ratelimit_seconds_429_uses_reset_header():
    import fetch_feeds

    resp = _FakeResponse(status_code=429, headers={"x-ratelimit-reset": "30"})
    assert fetch_feeds._reddit_ratelimit_seconds(resp) == 30.0


def test_ratelimit_seconds_429_without_headers_uses_default():
    import fetch_feeds

    resp = _FakeResponse(status_code=429)
    assert (
        fetch_feeds._reddit_ratelimit_seconds(resp)
        == fetch_feeds.REDDIT_RSS_DEFAULT_BACKOFF_SEC
    )


def test_ratelimit_seconds_429_reset_is_capped():
    import fetch_feeds

    resp = _FakeResponse(status_code=429, headers={"x-ratelimit-reset": "3000"})
    assert (
        fetch_feeds._reddit_ratelimit_seconds(resp)
        == fetch_feeds.REDDIT_RSS_MAX_WAIT_SEC
    )


def test_ratelimit_seconds_200_exhausted_window_waits_reset_plus_one():
    import fetch_feeds

    resp = _FakeResponse(
        status_code=200,
        headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "33"},
    )
    assert fetch_feeds._reddit_ratelimit_seconds(resp) == 34.0


def test_ratelimit_seconds_200_with_quota_left_needs_no_wait():
    import fetch_feeds

    resp = _FakeResponse(
        status_code=200,
        headers={"x-ratelimit-remaining": "5", "x-ratelimit-reset": "33"},
    )
    assert fetch_feeds._reddit_ratelimit_seconds(resp) == 0.0


def test_ratelimit_seconds_tolerates_response_without_headers():
    import fetch_feeds

    resp = SimpleNamespace(status_code=200)
    assert fetch_feeds._reddit_ratelimit_seconds(resp) == 0.0


# ============================================================
# 步调器：总等待预算封顶
# ============================================================

def test_pacer_budget_bounds_total_sleep():
    import fetch_feeds

    clock = _FakeClock()
    pacer = fetch_feeds._RedditRssPacer(
        budget_sec=50.0, sleep=clock.sleep, monotonic=clock.monotonic
    )
    resp_429 = _FakeResponse(status_code=429, headers={"x-ratelimit-reset": "40"})

    assert pacer.backoff_for_retry(resp_429) is True  # 40s，预算剩 10
    assert pacer.backoff_for_retry(resp_429) is True  # 只剩 10s，封顶等待
    assert pacer.backoff_for_retry(resp_429) is False  # 预算耗尽，不再等待
    assert sum(clock.sleeps) <= 50.0


# ============================================================
# fetch_reddit 集成：429 退避重试 + 跨源 pacing
# ============================================================

def test_fetch_reddit_rss_429_backs_off_and_retries_success(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    _patch_local_fetch_backend(monkeypatch)
    _patch_feedparser(monkeypatch)
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)

    calls = []
    rss_responses = [
        _FakeResponse(status_code=429, headers={"x-ratelimit-reset": "30"}),
        _FakeResponse(status_code=200, content=b"<rss />"),
    ]

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/.rss"):
            return rss_responses.pop(0)
        return _FakeResponse(status_code=403, json_data={})

    _patch_requests_get(monkeypatch, fake_get)

    conn = db.get_conn()
    source_id = _insert_source(conn, "OpenAI", status="broken")
    conn.execute(
        "UPDATE sources SET consecutive_failures = 20, last_error = 'JSON HTTP 403; RSS HTTP 429' WHERE id = ?",
        (source_id,),
    )
    conn.commit()
    conn.close()

    fetch_feeds.fetch_reddit()

    # 429 后必须退避重试一次（现状代码只发一次 RSS 请求即判死）
    assert calls == [
        "https://www.reddit.com/r/OpenAI/hot.json?limit=1",
        "https://www.reddit.com/r/OpenAI/.rss",
        "https://www.reddit.com/r/OpenAI/.rss",
    ]
    # 退避时长来自 x-ratelimit-reset
    assert 30.0 in clock.sleeps

    posts = json.loads((tmp_path / "sources" / "reddit" / "OpenAI.json").read_text())
    assert posts[0]["id"] == "abc"

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["status"] == "active"  # broken 源借退避重试自愈
        assert row["last_error"] is None
    finally:
        conn.close()


def test_fetch_reddit_paces_next_source_by_ratelimit_window(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    _patch_local_fetch_backend(monkeypatch)
    _patch_feedparser(monkeypatch)
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, clock.now))
        if url.endswith("/.rss"):
            return _FakeResponse(
                status_code=200,
                content=b"<rss />",
                headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "30"},
            )
        return _FakeResponse(status_code=403, json_data={})

    _patch_requests_get(monkeypatch, fake_get)

    conn = db.get_conn()
    first_id = _insert_source(conn, "ClaudeAI")
    second_id = _insert_source(conn, "OpenAI", status="broken")
    conn.close()

    fetch_feeds.fetch_reddit()

    rss_times = [at for (url, at) in calls if url.endswith("/.rss")]
    assert len(rss_times) == 2
    # 第一个 RSS 响应宣告 remaining=0/reset=30 → 第二个源的 RSS 请求必须等窗口重置(≥30s)
    assert rss_times[1] - rss_times[0] >= 30.0

    conn = db.get_conn()
    try:
        assert _source_row(conn, first_id)["consecutive_failures"] == 0
        row = _source_row(conn, second_id)
        assert row["consecutive_failures"] == 0
        assert row["status"] == "active"
    finally:
        conn.close()


def test_fetch_reddit_budget_exhausted_still_records_failure(
    tmp_db, tmp_path, monkeypatch
):
    """预算耗尽退化为现状：请求照发、失败照记，不无限等待。"""
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    monkeypatch.setattr(fetch_feeds, "REDDIT_RSS_WAIT_BUDGET_SEC", 0.0)
    _patch_local_fetch_backend(monkeypatch)
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/.rss"):
            return _FakeResponse(status_code=429, headers={"x-ratelimit-reset": "30"})
        return _FakeResponse(status_code=403, json_data={})

    _patch_requests_get(monkeypatch, fake_get)

    conn = db.get_conn()
    source_id = _insert_source(conn, "OpenAI")
    conn.close()

    fetch_feeds.fetch_reddit()

    # 预算 0 → 不退避、不重试，每源仍只有 json+rss 两个请求
    assert calls == [
        "https://www.reddit.com/r/OpenAI/hot.json?limit=1",
        "https://www.reddit.com/r/OpenAI/.rss",
    ]
    assert sum(clock.sleeps) == 0.0

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 1
        assert row["last_error"] == "JSON HTTP 403; RSS HTTP 429"
    finally:
        conn.close()
