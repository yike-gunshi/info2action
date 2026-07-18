from contextlib import contextmanager
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import remote_db


def _enable_info_read_model(monkeypatch):
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_ENV, "1")
    remote_db.clear_feed_cache_keys()


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _CaptureConn:
    def __init__(self, rows):
        self.rows = rows
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        if "SET LOCAL" in " ".join(sql.split()):
            return _FakeResult([])
        return _FakeResult(self.rows)


def _card(item_id):
    return {
        "id": item_id,
        "title": item_id,
        "url": f"https://example.test/{item_id}",
        "source": "following",
        "platform": "twitter",
        "fetched_at": "2026-07-06T00:00:00+00:00",
    }


def _make_conn(rows):
    conn = _CaptureConn(rows)

    @contextmanager
    def fake_connect():
        yield conn

    return conn, fake_connect


def _query_sql(conn):
    return "\n".join(sql for sql in conn.sqls if "WITH active_version AS" in sql)


def _summary_sql(sql):
    normalized = " ".join(sql.split())
    return normalized.split("page_rows AS", 1)[0]


def test_feed_more_timeout_env_defaults_overrides_and_clamps(monkeypatch):
    _enable_info_read_model(monkeypatch)
    assert remote_db._feed_more_timeout_ms({}) == 6000
    assert remote_db._feed_more_timeout_ms({remote_db.FEED_MORE_TIMEOUT_MS_ENV: "7500"}) == 7500
    assert remote_db._feed_more_timeout_ms({remote_db.FEED_MORE_TIMEOUT_MS_ENV: "250"}) == 1000


def test_category_more_beyond_first_page_falls_through_to_live(monkeypatch):
    """perf-v27 P4: 读模型只物化每 scope 首屏 TOP_N——offset>0 的续页必须
    返回 None 落到 live 现场查(90 天冷数据),不得再从模型分页。
    (原测试断言 offset=64 从模型取页+scope total,该契约已随首屏预算废止。)"""
    _enable_info_read_model(monkeypatch)

    def fail_connect():
        raise AssertionError("offset>0 不应触达读模型查询,应直接 None 落 live")

    monkeypatch.setattr(remote_db, "connect", fail_connect)

    result = remote_db._query_feed_by_category_read_model(
        schema="remote_poc",
        category="coding",
        keyword=None,
        search=None,
        subcategory=None,
        offset=64,
        limit=20,
        cursor=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result is None


def test_platform_more_uses_scope_total_count_without_recounting_cards(monkeypatch):
    _enable_info_read_model(monkeypatch)
    conn, fake_connect = _make_conn([
        {
            "version_id": "00000000-0000-0000-0000-000000000001",
            "generated_at": "2026-07-06T00:00:00+00:00",
            "max_fetched_at": "2026-07-06T00:00:00+00:00",
            "total_count": 9298,
            "rank": 65,
            "item_id": "tw_65",
            "sort_at": "2026-07-06T00:00:00+00:00",
            "fetched_at": "2026-07-06T00:00:00+00:00",
            "relevance_score": 1,
            "card_json": _card("tw_65"),
        }
    ])
    timeouts = []

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(
        remote_db,
        "_set_short_statement_timeout",
        lambda conn, timeout_ms=0: timeouts.append(timeout_ms),
    )

    result = remote_db._query_feed_by_platform_read_model(
        schema="remote_poc",
        platform="twitter",
        offset=64,
        limit=20,
        source=None,
        group=None,
        category=None,
        search=None,
        exclude_ids=None,
        cursor=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    sql = _query_sql(conn)
    summary = _summary_sql(sql)
    assert result is not None
    assert result["total"] == 9298
    assert timeouts == [6000]
    assert "sc.total_count" in sql
    assert "sr.total_count" in sql
    assert "JOIN remote_poc.info_scope_items si" not in summary
    assert "JOIN remote_poc.info_card_items ci" not in summary
