"""订阅配置 W2: fetch_feeds 从 sources 注册表读取抓取名单。"""
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


def _insert_source(conn, platform, source_key, *, status="active", name=None, config=None):
    cur = conn.execute(
        "INSERT INTO sources(platform, source_key, display_name, status, config_json, origin) "
        "VALUES(?,?,?,?,?,?)",
        (
            platform,
            source_key,
            name,
            status,
            json.dumps(config, ensure_ascii=False) if config is not None else None,
            "test",
        ),
    )
    conn.commit()
    return cur.lastrowid


class _FakeResponse:
    def __init__(self, *, status_code=200, content=b"", json_data=None, raise_error=None):
        self.status_code = status_code
        self.content = content
        self._json_data = json_data
        self._raise_error = raise_error

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error

    def json(self):
        return self._json_data


def _source_row(conn, source_id):
    return conn.execute(
        """SELECT status, consecutive_failures, last_success_at, last_error
           FROM sources WHERE id = ?""",
        (source_id,),
    ).fetchone()


def _set_source_failures(conn, source_id, failures, *, error="old error"):
    conn.execute(
        """UPDATE sources
           SET consecutive_failures = ?, last_error = ?
           WHERE id = ?""",
        (failures, error, source_id),
    )
    conn.commit()


def _patch_local_fetch_backend(monkeypatch):
    import remote_db

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)


def _patch_requests_get(monkeypatch, get):
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))


def _patch_feedparser(monkeypatch):
    def parse(content):
        return SimpleNamespace(
            feed={"title": "Fake Feed"},
            entries=[
                {
                    "id": "entry-1",
                    "title": "Entry 1",
                    "link": "https://example.test/entry-1",
                    "summary": "Summary",
                    "published": "2026-07-06T00:00:00Z",
                    "content": [{"value": "Body"}],
                    "tags": [{"term": "ai"}],
                }
            ],
        )

    monkeypatch.setitem(sys.modules, "feedparser", SimpleNamespace(parse=parse))


def test_active_rss_feeds_use_registry_and_skip_paused(tmp_db, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, "CONFIG", {
        "rss": {"feeds": [{"name": "Config Feed", "slug": "config", "url": "https://cfg.test/rss"}]}
    })
    conn = db.get_conn()
    _insert_source(
        conn, "rss", "https://a.test/rss",
        name="A Feed", config={"slug": "a"},
    )
    _insert_source(
        conn, "rss", "https://b.test/rss",
        name="B Feed", config={"slug": "b"},
    )
    _insert_source(
        conn, "rss", "https://paused.test/rss",
        status="paused", name="Paused Feed", config={"slug": "paused"},
    )

    feeds = fetch_feeds._active_rss_feeds(conn)

    assert feeds == [
        {"name": "A Feed", "slug": "a", "url": "https://a.test/rss"},
        {"name": "B Feed", "slug": "b", "url": "https://b.test/rss"},
    ]
    conn.close()


def test_active_reddit_subreddits_use_registry_and_skip_paused(tmp_db, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, "CONFIG", {
        "reddit": {"subreddits": ["ConfigSub"], "count": 9}
    })
    conn = db.get_conn()
    _insert_source(conn, "reddit", "OpenAI")
    _insert_source(conn, "reddit", "ClaudeAI")
    _insert_source(conn, "reddit", "PausedSub", status="paused")

    subs = fetch_feeds._active_reddit_subreddits(conn)

    assert subs == ["OpenAI", "ClaudeAI"]
    conn.close()


def test_active_github_awesome_repos_use_registry_and_skip_paused(tmp_db):
    import db
    import fetch_feeds

    conn = db.get_conn()
    _insert_source(conn, "github_repo", "owner/one")
    _insert_source(conn, "github_repo", "owner/two")
    _insert_source(conn, "github_repo", "owner/paused", status="paused")

    repos = fetch_feeds._active_github_awesome_repos(
        conn, tracking_cfg={"awesome_repos": ["config/repo"]}
    )

    assert repos == ["owner/one", "owner/two"]
    conn.close()


def test_active_wechat_feeds_use_registry_and_skip_paused(tmp_db):
    import db
    import fetch_feeds

    conn = db.get_conn()
    _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/a.xml",
        name="A 公众号",
    )
    _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/b.xml",
        name="B 公众号",
    )
    _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/paused.xml",
        status="paused", name="Paused 公众号",
    )

    feeds = fetch_feeds._active_wechat_feeds(conn)

    assert feeds == [
        {"name": "A 公众号", "url": "https://wechat.example.com/a.xml"},
        {"name": "B 公众号", "url": "https://wechat.example.com/b.xml"},
    ]
    conn.close()


def test_active_wechat_feeds_only_include_rss_backend_sources(tmp_db):
    import db
    import fetch_feeds

    conn = db.get_conn()
    _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/rss.xml",
        name="RSS 公众号", config={"backend": "rss"},
    )
    _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/legacy.xml",
        name="Legacy RSS 公众号",
    )
    _insert_source(
        conn, "wechat_mp", "lw-channel-1",
        name="语鲸公众号", config={"backend": "lingowhale"},
    )

    feeds = fetch_feeds._active_wechat_feeds(conn)

    assert feeds == [
        {"name": "RSS 公众号", "url": "https://wechat.example.com/rss.xml"},
        {"name": "Legacy RSS 公众号", "url": "https://wechat.example.com/legacy.xml"},
    ]
    conn.close()


def test_fetch_source_lists_fallback_to_config_when_registry_empty(tmp_db, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, "CONFIG", {
        "rss": {"feeds": [
            {"name": "Config RSS", "slug": "config-rss", "url": "https://cfg.test/rss"}
        ]},
        "reddit": {"subreddits": ["ConfigSub"], "count": 3},
    })
    conn = db.get_conn()

    assert fetch_feeds._active_rss_feeds(conn) == [
        {"name": "Config RSS", "slug": "config-rss", "url": "https://cfg.test/rss"}
    ]
    assert fetch_feeds._active_reddit_subreddits(conn) == ["ConfigSub"]
    assert fetch_feeds._active_github_awesome_repos(
        conn, tracking_cfg={"awesome_repos": ["config/repo"]}
    ) == ["config/repo"]
    conn.close()


def test_resolve_source_maps_wechat_rss_lingowhale_items(tmp_db):
    import db

    conn = db.get_conn()
    rss_id = _insert_source(
        conn, "wechat_mp", "https://wechat.example.com/feed.xml",
        name="RSS 公众号", config={"backend": "rss"},
    )
    lw_id = _insert_source(
        conn, "wechat_mp", "lw-channel-1",
        name="语鲸公众号", config={"backend": "lingowhale"},
    )

    idx = db.load_source_index(conn)

    assert db.resolve_source(
        idx, "lingowhale", "wechat:https://wechat.example.com/feed.xml"
    ) == (rss_id, "active")
    assert db.resolve_source(
        idx, "lingowhale", "lingowhale:lw-channel-1"
    ) == (lw_id, "active")
    assert db.resolve_source(
        idx, "lingowhale", "subscription", channel_id="lw-channel-1"
    ) == (lw_id, "active")
    assert db.resolve_source(idx, "lingowhale", "subscription") == (None, None)
    assert db.resolve_source(idx, "twitter", "following") == (None, None)
    assert db.resolve_source(idx, "twitter", "for_you") == (None, None)
    conn.close()


def test_fetch_source_lists_fallback_to_config_when_registry_raises(monkeypatch):
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, "CONFIG", {
        "rss": {"feeds": [
            {"name": "Config RSS", "slug": "config-rss", "url": "https://cfg.test/rss"}
        ]},
        "reddit": {"subreddits": ["ConfigSub"]},
    })

    def boom(platform, conn=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(fetch_feeds, "_registry_sources", boom)

    assert fetch_feeds._active_rss_feeds() == [
        {"name": "Config RSS", "slug": "config-rss", "url": "https://cfg.test/rss"}
    ]
    assert fetch_feeds._active_reddit_subreddits() == ["ConfigSub"]
    assert fetch_feeds._active_github_awesome_repos(
        tracking_cfg={"awesome_repos": ["config/repo"]}
    ) == ["config/repo"]


def test_lingowhale_registry_channel_ids_use_local_sources(tmp_db, monkeypatch):
    import db
    import fetch_lingowhale
    import remote_db

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    conn = db.get_conn()
    try:
        _insert_source(conn, "wechat_mp", "lw-explicit", config={"backend": "lingowhale"})
        _insert_source(conn, "wechat_mp", "lw-legacy")
        _insert_source(conn, "wechat_mp", "https://wechat.test/rss.xml", config={"backend": "rss"})
        _insert_source(conn, "wechat_mp", "lw-paused", status="paused", config={"backend": "lingowhale"})
    finally:
        conn.close()

    assert fetch_lingowhale._registry_lingowhale_channel_ids() == [
        "lw-explicit",
        "lw-legacy",
    ]


def test_lingowhale_registry_channel_ids_use_remote_sources(monkeypatch):
    import db
    import fetch_lingowhale
    import remote_db

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(
        remote_db,
        "list_active_sources_remote",
        lambda platform, *, fail_open=True: [
            {"id": 1, "source_key": "lw-remote", "config_json": {"backend": "lingowhale"}},
            {"id": 2, "source_key": "lw-legacy-remote", "config_json": {}},
            {"id": 3, "source_key": "https://wechat.test/rss.xml", "config_json": {"backend": "rss"}},
        ],
    )
    monkeypatch.setattr(db, "get_conn", lambda: pytest.fail("opened local db"))

    assert fetch_lingowhale._registry_lingowhale_channel_ids() == [
        "lw-remote",
        "lw-legacy-remote",
    ]


def test_fetch_subscription_feed_includes_registry_lingowhale_channels(monkeypatch):
    import fetch_lingowhale as lw

    calls = []
    groups_info = [
        {"name": "每日查看", "channels": [{"channel_id": "from-group", "name": "分组号"}]},
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        calls.append((endpoint, tuple(channel_ids), label))
        return ([], 1, "done")

    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {
        "from-registry": 10,
        "from-group": 11,
    })
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw, "_record_lingowhale_result", lambda source_id, *, ok, error=None: None)
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed(groups_info)

    current_channel_calls = [
        channel_ids
        for endpoint, channel_ids, label in calls
        if endpoint == lw.FEED_ENDPOINTS[0]
    ]
    assert current_channel_calls == [("from-registry",), ("from-group",)]


def test_fetch_subscription_feed_registry_only_uses_registry_channels(monkeypatch):
    import fetch_lingowhale as lw

    calls = []
    groups_info = [
        {"name": "每日查看", "channels": [{"channel_id": "from-group", "name": "分组号"}]},
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        calls.append((endpoint, tuple(channel_ids), label))
        return ([{"entry_id": f"{channel_ids[0]}-1", "pub_time": 1}], 1, "done")

    monkeypatch.delenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", raising=False)
    monkeypatch.setattr(
        lw,
        "_priority_channel_ids",
        lambda: ["outside-registry", "from-registry-b"],
    )
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {
        "from-registry-a": 10,
        "from-registry-b": 11,
    })
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw, "_record_lingowhale_result", lambda source_id, *, ok, error=None: None)
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed(groups_info)

    assert [
        channel_ids
        for endpoint, channel_ids, label in calls
        if endpoint == lw.FEED_ENDPOINTS[0]
    ] == [("from-registry-b",), ("from-registry-a",)]
    assert (lw.FEED_ENDPOINTS[1], ("all",), "legacy all") not in calls


def test_fetch_subscription_feed_empty_registry_stays_registry_only(monkeypatch):
    import fetch_lingowhale as lw

    calls = []
    groups_info = [
        {"name": "每日查看", "channels": [{"channel_id": "from-group", "name": "分组号"}]},
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        calls.append((endpoint, tuple(channel_ids), label))
        return ([], 1, "done")

    monkeypatch.delenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", raising=False)
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {})
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    result = lw.fetch_subscription_feed(groups_info)

    assert result == []
    assert calls == []


def test_fetch_subscription_feed_registry_error_fails_closed(monkeypatch):
    import fetch_lingowhale as lw
    import remote_db

    fail_open_values = []

    def fail_registry(platform, *, fail_open=True):
        fail_open_values.append(fail_open)
        raise RuntimeError("registry unavailable")

    monkeypatch.delenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", raising=False)
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(remote_db, "list_active_sources_remote", fail_registry)
    monkeypatch.setattr(
        lw,
        "_fetch_subscription_feed_from_endpoint",
        lambda *args, **kwargs: pytest.fail("called a feed endpoint after registry failure"),
    )

    with pytest.raises(RuntimeError, match="registry unavailable"):
        lw.fetch_subscription_feed(groups_info=None)

    assert fail_open_values == [False]


def test_fetch_subscription_feed_ignores_retired_legacy_override(monkeypatch):
    import fetch_lingowhale as lw

    calls = []
    groups_info = [
        {"name": "每日查看", "channels": [{"channel_id": "from-group", "name": "分组号"}]},
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        calls.append((endpoint, tuple(channel_ids), label))
        return ([], 1, "done")

    monkeypatch.setenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", "0")
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"from-registry": 10})
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw, "_record_lingowhale_result", lambda source_id, *, ok, error=None: None)
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed(groups_info)

    assert calls == [(lw.FEED_ENDPOINTS[0], ("from-registry",), "channel 1/1")]


def test_fetch_subscription_feed_records_registry_channel_success(monkeypatch):
    import fetch_lingowhale as lw

    records = []
    groups_info = [
        {
            "name": "每日查看",
            "channels": [
                {"channel_id": "mapped-channel", "name": "已登记"},
                {"channel_id": "unmapped-channel", "name": "未登记"},
            ],
        },
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        channel_id = channel_ids[0]
        if channel_id == "all":
            return ([], 1, "legacy")
        return ([{"entry_id": f"{channel_id}-1", "pub_time": 1}], 1, "done")

    def fake_record(source_id, *, ok, error=None):
        records.append((source_id, ok, error))

    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"mapped-channel": 42}, raising=False)
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw, "_record_lingowhale_result", fake_record, raising=False)
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed(groups_info)

    assert records == [(42, True, None)]


def test_lingowhale_result_fallback_uses_remote_authority(monkeypatch):
    import db
    import fetch_lingowhale as lw
    import ingest
    import remote_db

    calls = []
    monkeypatch.delattr(ingest, "record_source_fetch_result_current_backend")
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(db, "_broken_after_threshold", lambda: 7)
    monkeypatch.setattr(
        remote_db,
        "record_source_fetch_result_remote",
        lambda source_id, **kwargs: calls.append((source_id, kwargs)),
    )
    monkeypatch.setattr(
        db,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("local sqlite must not be opened")),
    )

    lw._record_lingowhale_result(42, ok=False, error="remote failure")

    assert calls == [
        (42, {"ok": False, "error": "remote failure", "broken_after": 7})
    ]


def test_fetch_subscription_feed_skips_result_record_when_registry_map_empty(monkeypatch):
    import fetch_lingowhale as lw

    records = []
    groups_info = [
        {"name": "每日查看", "channels": [{"channel_id": "from-group", "name": "分组号"}]},
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        return ([], 1, "done")

    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {}, raising=False)
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, error)),
        raising=False,
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed(groups_info)

    assert records == []


def test_registry_sources_uses_remote_when_fetch_write_enabled(monkeypatch):
    import db
    import fetch_feeds
    import remote_db

    sentinel = [{"id": 101, "source_key": "https://remote.test/rss"}]

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(remote_db, "list_active_sources_remote", lambda platform: sentinel)
    monkeypatch.setattr(db, "get_conn", lambda: pytest.fail("opened local db"))
    monkeypatch.setattr(db, "list_active_sources", lambda conn, platform: pytest.fail("used local sources"))

    assert fetch_feeds._registry_sources("rss") is sentinel


def test_registry_sources_uses_local_when_fetch_write_disabled(monkeypatch):
    import db
    import fetch_feeds
    import remote_db

    sentinel = [{"id": 102, "source_key": "https://local.test/rss"}]
    conn = object()

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(
        remote_db,
        "list_active_sources_remote",
        lambda platform: pytest.fail("used remote sources"),
    )

    def fake_list_active_sources(got_conn, platform):
        assert got_conn is conn
        assert platform == "rss"
        return sentinel

    monkeypatch.setattr(db, "list_active_sources", fake_list_active_sources)

    assert fetch_feeds._registry_sources("rss", conn) is sentinel


def test_fetch_rss_success_records_source_result(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    _patch_local_fetch_backend(monkeypatch)
    _patch_feedparser(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(status_code=200, content=b"<rss />"),
    )

    conn = db.get_conn()
    source_id = _insert_source(
        conn,
        "rss",
        "https://rss.test/feed.xml",
        name="RSS Feed",
        config={"slug": "rss-feed"},
    )
    _set_source_failures(conn, source_id, 3)
    conn.close()

    fetch_feeds.fetch_rss()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"]
        assert row["last_error"] is None
    finally:
        conn.close()


def test_fetch_rss_http_error_records_failure(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(
            status_code=500,
            content=b"",
            raise_error=RuntimeError("HTTP 500"),
        ),
    )

    conn = db.get_conn()
    source_id = _insert_source(
        conn,
        "rss",
        "https://rss.test/fail.xml",
        name="RSS Fail",
        config={"slug": "rss-fail"},
    )
    _set_source_failures(conn, source_id, 2)
    conn.close()

    fetch_feeds.fetch_rss()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 3
        assert "HTTP 500" in row["last_error"]
        assert row["last_success_at"] is None
    finally:
        conn.close()


def test_fetch_rss_marks_source_broken_at_threshold(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "_broken_after_threshold", lambda: 5)
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(
            status_code=500,
            content=b"",
            raise_error=RuntimeError("HTTP 500"),
        ),
    )

    conn = db.get_conn()
    source_id = _insert_source(
        conn,
        "rss",
        "https://rss.test/broken.xml",
        name="RSS Broken",
        config={"slug": "rss-broken"},
    )
    _set_source_failures(conn, source_id, 4)
    conn.close()

    fetch_feeds.fetch_rss()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 5
        assert row["status"] == "broken"
    finally:
        conn.close()


def test_fetch_reddit_http_error_records_failure(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(status_code=403, json_data={}),
    )

    conn = db.get_conn()
    source_id = _insert_source(conn, "reddit", "OpenAI")
    _set_source_failures(conn, source_id, 1)
    conn.close()

    fetch_feeds.fetch_reddit()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 2
        assert row["last_error"] == "JSON HTTP 403; RSS HTTP 403"
        assert row["last_success_at"] is None
    finally:
        conn.close()


def test_fetch_reddit_json_403_falls_back_to_rss_success(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    monkeypatch.setattr(fetch_feeds.time, "sleep", lambda seconds: None)
    _patch_local_fetch_backend(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/.rss"):
            return _FakeResponse(status_code=200, content=b"<rss />")
        return _FakeResponse(status_code=403, json_data={})

    _patch_requests_get(monkeypatch, fake_get)
    monkeypatch.setitem(
        sys.modules,
        "feedparser",
        SimpleNamespace(
            parse=lambda content: SimpleNamespace(entries=[{
                "id": "t3_abc",
                "title": "RSS fallback post",
                "link": "https://www.reddit.com/r/OpenAI/comments/abc/post/",
                "author": "rss-user",
                "summary": "Fallback body",
                "published": "2026-07-06T00:00:00Z",
            }]),
        ),
    )

    conn = db.get_conn()
    source_id = _insert_source(conn, "reddit", "OpenAI")
    _set_source_failures(conn, source_id, 3)
    conn.close()

    fetch_feeds.fetch_reddit()

    assert calls == [
        "https://www.reddit.com/r/OpenAI/hot.json?limit=1",
        "https://www.reddit.com/r/OpenAI/.rss",
    ]
    posts = json.loads((tmp_path / "sources" / "reddit" / "OpenAI.json").read_text())
    assert posts[0]["id"] == "abc"
    assert posts[0]["title"] == "RSS fallback post"

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"]
        assert row["last_error"] is None
    finally:
        conn.close()


def test_fetch_reddit_success_records_source_result(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "CONFIG", {"reddit": {"count": 1}})
    monkeypatch.setattr(fetch_feeds.time, "sleep", lambda seconds: None)
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(
            status_code=200,
            json_data={
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "abc",
                                "title": "Hello",
                                "author": "user",
                                "url": "https://reddit.test/post",
                                "created_utc": 1,
                            }
                        }
                    ]
                }
            },
        ),
    )

    conn = db.get_conn()
    source_id = _insert_source(conn, "reddit", "OpenAI")
    _set_source_failures(conn, source_id, 3)
    conn.close()

    fetch_feeds.fetch_reddit()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"]
        assert row["last_error"] is None
    finally:
        conn.close()


def test_fetch_wechat_rss_success_records_source_result(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    _patch_local_fetch_backend(monkeypatch)
    _patch_feedparser(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(status_code=200, content=b"<rss />"),
    )

    conn = db.get_conn()
    source_id = _insert_source(
        conn,
        "wechat_mp",
        "https://wechat.test/feed.xml",
        name="公众号 RSS",
        config={"backend": "rss"},
    )
    _set_source_failures(conn, source_id, 2)
    conn.close()

    fetch_feeds.fetch_wechat_rss()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"]
        assert row["last_error"] is None
    finally:
        conn.close()


def test_fetch_wechat_rss_http_error_records_failure(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(
            status_code=500,
            content=b"",
            raise_error=RuntimeError("HTTP 500"),
        ),
    )

    conn = db.get_conn()
    source_id = _insert_source(
        conn,
        "wechat_mp",
        "https://wechat.test/fail.xml",
        name="公众号 Fail",
        config={"backend": "rss"},
    )
    _set_source_failures(conn, source_id, 1)
    conn.close()

    fetch_feeds.fetch_wechat_rss()

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 2
        assert "HTTP 500" in row["last_error"]
        assert row["last_success_at"] is None
    finally:
        conn.close()


def test_fetch_github_awesome_repo_http_error_records_failure(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "BASE", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "github_tracking.json").write_text(json.dumps({"awesome_repos": []}))
    _patch_local_fetch_backend(monkeypatch)
    _patch_requests_get(
        monkeypatch,
        lambda url, **kwargs: _FakeResponse(status_code=500, json_data={}),
    )

    conn = db.get_conn()
    source_id = _insert_source(conn, "github_repo", "owner/repo")
    _set_source_failures(conn, source_id, 2)
    conn.close()

    assert fetch_feeds.fetch_github_awesome_repos() == []

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 3
        assert row["last_error"] == "HTTP 500"
        assert row["last_success_at"] is None
    finally:
        conn.close()


def test_fetch_github_awesome_repo_invalid_entry_records_failure(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_feeds

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_feeds, "BASE", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "github_tracking.json").write_text(json.dumps({"awesome_repos": []}))
    _patch_local_fetch_backend(monkeypatch)

    conn = db.get_conn()
    source_id = _insert_source(conn, "github_repo", "no-slash")
    _set_source_failures(conn, source_id, 1)
    conn.close()

    assert fetch_feeds.fetch_github_awesome_repos() == []

    conn = db.get_conn()
    try:
        row = _source_row(conn, source_id)
        assert row["consecutive_failures"] == 2
        assert row["last_error"] == "invalid github repo entry"
        assert row["last_success_at"] is None
    finally:
        conn.close()
