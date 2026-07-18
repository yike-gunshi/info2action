"""v25.0 F-B：cluster 级双因子重要性分（LLM 质量 × 独立源数证据强度）。

数值语义打在 Python 参考实现 compute_highlight_score 上（与 SQL 共享常量），
SQL 接线用 FakeConn 捕获断言（与 test_remote_event_backend.py 同模式）。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


# ---------- Python 参考实现：数值语义 ----------

def test_score_monotonic_in_sources():
    low = remote_db.compute_highlight_score(
        max_q=0.8, avg_q=0.7, scored_include_count=2, unique_source_count=1
    )
    high = remote_db.compute_highlight_score(
        max_q=0.8, avg_q=0.7, scored_include_count=2, unique_source_count=5
    )
    assert low is not None and high is not None
    assert high > low


def test_score_monotonic_in_quality():
    low = remote_db.compute_highlight_score(
        max_q=0.5, avg_q=0.4, scored_include_count=2, unique_source_count=3
    )
    high = remote_db.compute_highlight_score(
        max_q=0.9, avg_q=0.8, scored_include_count=2, unique_source_count=3
    )
    assert high > low


def test_score_shrinks_thin_evidence():
    # 同质量同源数，include 成员少的（证据薄）分数被向先验收缩
    thin = remote_db.compute_highlight_score(
        max_q=0.9, avg_q=0.9, scored_include_count=1, unique_source_count=3
    )
    solid = remote_db.compute_highlight_score(
        max_q=0.9, avg_q=0.9, scored_include_count=4, unique_source_count=3
    )
    assert thin < solid
    # 单条高分不得拿到未收缩的裸分
    raw_quality = 0.6 * 0.9 + 0.4 * 0.9
    import math

    raw = round(
        100
        * raw_quality
        * math.log1p(3)
        / math.log1p(remote_db.HIGHLIGHT_SCORE_EVIDENCE_NORM_SOURCES),
        2,
    )
    assert thin < raw


def test_score_none_when_no_scored_members():
    assert (
        remote_db.compute_highlight_score(
            max_q=None, avg_q=None, scored_include_count=0, unique_source_count=5
        )
        is None
    )


def test_score_zero_sources_treated_as_one():
    a = remote_db.compute_highlight_score(
        max_q=0.6, avg_q=0.6, scored_include_count=2, unique_source_count=0
    )
    b = remote_db.compute_highlight_score(
        max_q=0.6, avg_q=0.6, scored_include_count=2, unique_source_count=1
    )
    assert a == b and a is not None and a > 0


# ---------- SQL 接线：decisions 同步写入分数 ----------

def _run_refresh_and_capture(monkeypatch):
    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if "SELECT count(*) AS n FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(row={"n": 1})
            if "SELECT count(*) AS n FROM remote_poc.highlights_scopes" in normalized:
                return FakeCursor(row={"n": 1})
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)

    remote_db.refresh_highlights_read_model(window_days=30, min_github_stars=50)
    return fake


def test_decisions_sql_computes_and_upserts_highlight_score(monkeypatch):
    fake = _run_refresh_and_capture(monkeypatch)
    decisions_sql = next(
        sql
        for sql in fake.sqls
        if "INSERT INTO remote_poc.highlight_cluster_decisions" in sql
    )
    # 质量因子：include 成员的三核心维度分归一
    assert "highlight_scores" in decisions_sql
    assert "importance" in decisions_sql and "substance" in decisions_sql and "novelty" in decisions_sql
    # 证据因子 log 化 + 收缩常量来自共享常量
    assert "ln(" in decisions_sql
    assert str(remote_db.HIGHLIGHT_SCORE_W_MAX) in decisions_sql
    assert str(remote_db.HIGHLIGHT_SCORE_W_AVG) in decisions_sql
    # 落列 + 变更检测覆盖新列
    assert "highlight_score" in decisions_sql
    assert "score_inputs" in decisions_sql
    assert "target.highlight_score IS DISTINCT FROM excluded.highlight_score" in decisions_sql
    assert "target.score_inputs IS DISTINCT FROM excluded.score_inputs" in decisions_sql


# ---------- API 透出：读模型查询带四字段 ----------

def _fake_scope_row(extra=None):
    row = {
        "rank": 1,
        "cluster_id": 301,
        "sort_at": "2026-05-23T01:00:00+00:00",
        "card_json": {
            "id": 301,
            "ai_title": "Read model event",
            "ai_summary": "summary",
            "doc_count": 2,
            "unique_source_count": 2,
            "category": "products",
            "source_preview": [],
            "first_doc_at": "2026-05-23T01:00:00+00:00",
            "last_doc_at": "2026-05-23T01:00:00+00:00",
            "platforms": ["twitter"],
            "cover_url": None,
            "live_version": 5,
        },
    }
    if extra:
        row.update(extra)
    return row


def _run_fetch_events(monkeypatch, scope_row):
    remote_db.clear_feed_cache_keys()

    class FakeCursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = [] if rows is None else rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class FakeConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return FakeCursor(
                    row={
                        "version_id": "00000000-0000-0000-0000-00000000abcd",
                        "scope_key": "all",
                        "total_count": 1,
                    }
                )
            if "FROM remote_poc.highlights_scope_items" in normalized and "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-05-23", "n": 1}])
            if "FROM remote_poc.highlights_scope_items" in normalized:
                return FakeCursor(rows=[scope_row])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = FakeConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *a, **k: None)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
        categories=[],
    )
    remote_db.clear_feed_cache_keys()
    return result, fake


def test_fetch_events_read_model_exposes_quality_fields(monkeypatch):
    result, fake = _run_fetch_events(
        monkeypatch,
        _fake_scope_row(
            {
                "highlight_score": 72.5,
                "cluster_verdict": "featured",
                "value_path": "substantive",
                "featured_count": 2,
            }
        ),
    )

    event = result["events"][0]
    assert event["highlight_score"] == 72.5
    assert event["cluster_verdict"] == "featured"
    assert event["value_path"] == "substantive"
    assert event["featured_count"] == 2
    # 查询必须 join decisions（主键级）
    scope_sql = next(
        sql
        for sql in fake.sqls
        if "FROM remote_poc.highlights_scope_items" in sql and "GROUP BY day" not in sql
    )
    assert "highlight_cluster_decisions" in scope_sql


def test_fetch_events_read_model_quality_fields_null_degrade(monkeypatch):
    # decisions 无对应行（LEFT JOIN 空）→ 四字段 null，列表主体不受影响
    result, _ = _run_fetch_events(monkeypatch, _fake_scope_row())

    event = result["events"][0]
    assert event["id"] == 301
    assert event["highlight_score"] is None
    assert event["cluster_verdict"] is None
    assert event["value_path"] is None
    assert event["featured_count"] is None
