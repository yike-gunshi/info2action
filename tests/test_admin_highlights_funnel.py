from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import remote_db  # noqa: E402
from highlight_verdict import VALID_VETOES  # noqa: E402


class _Rows:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _FunnelConn:
    def __init__(self, *, counts=None, total=0, rows=None):
        self.counts = counts or {}
        self.total = total
        self.rows = list(rows or [])
        self.queries: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        values = dict(params or {})
        self.queries.append((text, values))
        if "AS ingested_count" in text:
            return _Rows(row=self.counts)
        if "COUNT(*) AS total" in text:
            return _Rows(row={"total": self.total})
        return _Rows(rows=self.rows)


def _install_conn(monkeypatch, conn):
    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")


def _request(user=None, body=None):
    return SimpleNamespace(
        state=SimpleNamespace(user=user or {"id": "admin", "role": "admin"}),
        json=lambda: body,
    )


def _decode_response(response):
    return json.loads(response.body.decode("utf-8"))


def _panorama_row(**overrides):
    row = {
        "id": 84,
        "latest_at": datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
        "title": "待 why_read 的事件",
        "dominant_category": "efficiency_tools",
        "max_flag_score10": 7.2,
        "score_inputs": {
            "max_q": 0.9,
            "avg_q": 0.7,
            "scored_include_count": 1,
            "unique_source_count": 2,
        },
        "deciding_item_id": "item-1",
        "deciding_item_title": "代表条目",
        "deciding_item_scores": {"v26": {"score10": 7.2, "authority": 3}},
        "deciding_item_reason": "一手官方信息",
        "stage": "blocked_display",
        "blocked_reason": "awaiting_why_read",
        "displayed": False,
        "manual_display": "force_show",
        "feedback_kind": "should_feature",
        "feedback_note": "应展示",
        "members": [
            {
                "id": "item-1",
                "title": "代表条目",
                "url": "https://example.com/1",
                "platform": "x",
                "source": "official",
                "author_name": "alice",
                "fetched_at": "2026-07-15T09:00:00+00:00",
                "score10": 7.2,
                "highlight_scores": {"v26": {"authority": 3}},
                "veto": None,
                "uncertainty": "none",
                "highlight_reason": "一手官方信息",
                "verdict": "featured",
                "feedback_kind": "should_feature",
                "feedback_note": "收录",
            },
            {
                "id": "item-2",
                "title": "低分成员",
                "url": None,
                "platform": "rss",
                "source": "feed",
                "author_name": None,
                "fetched_at": "2026-07-15T08:00:00+00:00",
                "score10": 3.1,
                "highlight_scores": {"v26": {"substance": 1}},
                "veto": "marketing",
                "uncertainty": None,
                "highlight_reason": "营销通稿",
                "verdict": "drop",
                "feedback_kind": "should_drop",
                "feedback_note": "排除",
            },
        ],
    }
    row.update(overrides)
    return row


def test_funnel_counts_reuse_shared_display_condition_and_accept_three_days(monkeypatch):
    conn = _FunnelConn(
        counts={
            "ingested_count": 12,
            "scored_count": 9,
            "clustered_count": 7,
            "summarized_count": 5,
            "displayed_count": 3,
            "anomalies_count": 2,
        }
    )
    _install_conn(monkeypatch, conn)
    monkeypatch.setattr(
        remote_db,
        "_runtime_env",
        lambda: {"INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD": "6.5"},
    )

    result = remote_db.query_admin_highlights_funnel_remote(
        days=3,
        q="Claude",
        tag="tools",
    )

    assert result["diffs"] == [
        {"key": "scoring", "count": 3},
        {"key": "summary", "count": 2},
        {"key": "display", "count": 2},
    ]
    sql, params = conn.queries[-1]
    shared = " ".join(
        remote_db._highlights_display_cluster_condition(
            "remote_poc", "c", threshold=6.5
        ).split()
    )
    assert shared in sql
    assert "manual_display = 'force_hide'" in shared
    assert "manual_display = 'force_show'" in shared
    assert "c.why_read IS NOT NULL" in shared
    window_sql, search_sql = sql.split("matching_cluster_ids AS", 1)
    assert "%(query)s" not in window_sql
    assert "%(search_pattern)s" not in window_sql
    assert "selected_window_items AS" in search_sql
    assert "FROM selected_window_items i" in search_sql
    assert "funnel_cluster_ids AS" in sql
    assert "funnel_terminal_items AS" in sql
    assert "funnel_scored_items AS" in sql
    assert "funnel_clustered_clusters AS" in sql
    assert "funnel_summarized_clusters AS" in sql
    assert "funnel_displayed_clusters AS" in sql
    for source in (
        "funnel_terminal_items",
        "funnel_scored_items",
        "funnel_clustered_clusters",
        "funnel_summarized_clusters",
        "funnel_displayed_clusters",
    ):
        assert f"SELECT COUNT(*) FROM {source}" in sql
    shared_tag = " ".join(remote_db._admin_highlights_tag_filter("s").split())
    assert shared_tag in sql
    assert shared_tag in " ".join(remote_db._admin_highlights_panorama_filter().split())
    assert "SELECT COUNT(*) FROM anomaly_items" in sql
    assert params["days"] == 3
    assert params["query"] == "Claude"
    assert params["tag"] == "efficiency_tools"
    assert conn.queries[0] == ("SET LOCAL statement_timeout = '15000ms'", {})


def test_funnel_counts_empty_tag_keeps_unfiltered_contract(monkeypatch):
    conn = _FunnelConn(counts={})
    _install_conn(monkeypatch, conn)

    remote_db.query_admin_highlights_funnel_remote(days=1, q="")

    sql, params = conn.queries[-1]
    assert params["tag"] == ""
    assert "%(tag)s = ''" in sql
    assert "FROM anomaly_items" in sql


def test_funnel_counts_reject_unsupported_tag():
    with pytest.raises(ValueError, match="unsupported funnel tag"):
        remote_db.query_admin_highlights_funnel_remote(tag="not-a-category")


def test_gate_disabled_is_fail_open_except_manual_hide(monkeypatch):
    condition = " ".join(
        remote_db._highlights_display_cluster_condition(
            "remote_poc", "c", threshold=None
        ).split()
    )
    display_filter = " ".join(
        remote_db._highlights_display_cluster_filter(
            "remote_poc", "c", threshold=None
        ).split()
    )

    assert condition in display_filter
    assert "manual_display = 'force_hide'" in condition
    assert "max_flag_score10" not in condition
    assert "why_read" not in condition


def test_panorama_rows_anchor_all_window_clusters_and_apply_orthogonal_filters(monkeypatch):
    conn = _FunnelConn(total=1, rows=[_panorama_row()])
    _install_conn(monkeypatch, conn)
    monkeypatch.setattr(
        remote_db,
        "_runtime_env",
        lambda: {"INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD": "6.5"},
    )

    result = remote_db.query_admin_highlights_funnel_rows_remote(
        view="panorama",
        days=3,
        q="模型",
        tag="tools",
        display="hidden",
        stage="blocked_summary",
        page=2,
        limit=100,
        user_id="admin",
    )

    assert result["granularity"] == "cluster"
    assert result["total"] == 1
    assert result["page"] == 2
    assert result["gate_disabled"] is False
    item = result["items"][0]
    assert item["stage"] == "blocked_display"
    assert item["blocked_reason"] == "awaiting_why_read"
    assert item["displayed"] is False
    assert item["manual_display"] == "force_show"
    assert item["dominant_category"] == "efficiency_tools"
    assert item["score_inputs"]["unique_source_count"] == 2
    assert item["deciding_item"]["id"] == "item-1"
    assert [member["id"] for member in item["members"]] == ["item-1", "item-2"]
    assert item["members"][1]["feedback"] == {"kind": "should_drop", "note": "排除"}

    count_sql, params = conn.queries[-2]
    select_sql = conn.queries[-1][0]
    for sql in (count_sql, select_sql):
        assert "FROM panorama_clusters s" in sql
        assert "%(display)s = 'all'" in sql
        assert "%(tag)s = ''" in sql
        assert "%(stage)s = ''" in sql
        assert "s.has_drop_member" in sql
    assert "jsonb_agg" in select_sql
    assert "i_member.cluster_id = s.cluster_id" in select_sql
    assert "i_member.fetched_at >= now() - (%(days)s::int * interval '1 day')" in select_sql
    assert "src/category_taxonomy.py" in select_sql
    assert params["tag"] == "efficiency_tools"
    assert params["display"] == "hidden"
    assert params["stage"] == "blocked_summary"
    assert params["limit"] == 100
    assert params["offset"] == 100


def test_panorama_rows_clamp_limit_over_100(monkeypatch):
    conn = _FunnelConn()
    _install_conn(monkeypatch, conn)

    remote_db.query_admin_highlights_funnel_rows_remote(limit=101)

    assert conn.queries[-1][1]["limit"] == 100


@pytest.mark.parametrize(
    ("stage", "reason"),
    [
        ("pending", "pending_scoring"),
        ("displayed", None),
        ("blocked_display", "below_threshold"),
        ("blocked_summary", "summary_gate_filtered"),
        ("blocked_scoring", "all_members_dropped"),
    ],
)
def test_panorama_payload_preserves_each_stage_enum(stage, reason):
    payload = remote_db._admin_highlights_cluster_payload(
        _panorama_row(stage=stage, blocked_reason=reason)
    )

    assert payload["stage"] == stage
    assert payload["blocked_reason"] == reason


def test_panorama_stage_featured_plus_pending_keeps_displayed_stage():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    assert "all_window_cluster_ids AS" in ctes
    assert "panorama_clusters AS" in ctes
    assert "WHEN aw.terminal_count = 0 THEN 'pending'" in ctes
    assert "WHEN aw.has_pending_member THEN 'pending'" not in ctes
    assert ctes.index("THEN 'displayed'") < ctes.index("THEN 'blocked_display'")


def test_panorama_stage_all_pending_uses_terminal_count_and_stays_out_of_stations():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    assert ctes.count("WHEN aw.terminal_count = 0 THEN 'pending'") == 1
    assert ctes.count("WHEN aw.terminal_count = 0 THEN 'pending_scoring'") == 1
    assert "terminal_items AS ( SELECT * FROM selected_window_items i WHERE i.highlight_verdict IS NOT NULL" in ctes


def test_panorama_all_actionable_error_cluster_is_anomaly_only_and_out_of_stations():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    actionable_error = (
        "COUNT(*) FILTER ( WHERE i.highlight_last_error IS NOT NULL AND "
        "( i.highlight_error_count >= 3 OR i.highlight_retry_after IS NULL OR "
        "i.highlight_retry_after <= now() ) )::int AS error_member_count"
    )
    assert actionable_error in ctes
    assert (
        "WHERE NOT ( aw.terminal_count = 0 AND aw.error_member_count > 0 )"
        in ctes
    )
    assert (
        "terminal_items AS ( SELECT * FROM selected_window_items i "
        "WHERE i.highlight_verdict IS NOT NULL"
        in ctes
    )


def test_panorama_featured_plus_actionable_error_uses_terminal_stage():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    assert (
        "WHERE NOT ( aw.terminal_count = 0 AND aw.error_member_count > 0 )"
        in ctes
    )
    assert ctes.index("WHEN aw.terminal_count = 0 THEN 'pending'") < ctes.index(
        "WHEN shown.cluster_id IS NOT NULL THEN 'displayed'"
    )
    assert "WHEN aw.error_member_count > 0 THEN 'pending'" not in ctes


def test_panorama_all_pending_without_error_remains_pending():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    assert ctes.count("WHEN aw.terminal_count = 0 THEN 'pending'") == 1
    assert ctes.count("WHEN aw.terminal_count = 0 THEN 'pending_scoring'") == 1
    assert "aw.error_member_count > 0" in ctes


def test_panorama_stage_drop_plus_pending_uses_terminal_drop_members():
    ctes = " ".join(
        remote_db._admin_highlights_funnel_ctes(
            "remote_poc", display_threshold=6.5
        ).split()
    )

    assert "WHEN aw.terminal_count = aw.drop_count AND aw.terminal_count > 0 THEN 'blocked_scoring'" in ctes
    assert "WHEN aw.terminal_count = aw.drop_count AND aw.terminal_count > 0 THEN 'all_members_dropped'" in ctes
    assert ctes.index("THEN 'blocked_summary'") < ctes.index("THEN 'blocked_scoring'")
    assert "ELSE 'blocked_summary'" in ctes
    assert "all_members_dropped" in ctes
    assert "awaiting_why_read" in ctes
    assert "manual_hide" in ctes


def test_dominant_category_sql_cleans_suffixes_aliases_and_other_fallback():
    ctes = remote_db._admin_highlights_funnel_ctes(
        "remote_poc", display_threshold=None
    )

    assert "split_part" in ctes
    assert "WHEN 'ai_tools' THEN 'efficiency_tools'" in ctes
    assert "WHEN 'tools' THEN 'efficiency_tools'" in ctes
    assert "WHEN 'insights' THEN 'tech'" in ctes
    assert "NOT IN ('', 'other')" in ctes
    assert "COALESCE(dc.category_id, 'other') AS dominant_category" in ctes


def test_search_matches_cluster_then_derives_stage_category_and_members_from_full_window(monkeypatch):
    members = [
        {
            "id": "target-drop",
            "title": "target low score",
            "fetched_at": "2026-07-15T09:00:00+00:00",
            "score10": 2.0,
            "highlight_scores": {"v26": {"score10": 2.0}},
            "veto": None,
            "highlight_reason": "low score",
            "verdict": "drop",
        },
        {
            "id": "product-featured-1",
            "title": "product release one",
            "fetched_at": "2026-07-15T08:00:00+00:00",
            "score10": 8.0,
            "highlight_scores": {"v26": {"score10": 8.0}},
            "veto": None,
            "highlight_reason": "official release",
            "verdict": "featured",
        },
        {
            "id": "product-featured-2",
            "title": "product release two",
            "fetched_at": "2026-07-15T07:00:00+00:00",
            "score10": 7.5,
            "highlight_scores": {"v26": {"score10": 7.5}},
            "veto": None,
            "highlight_reason": "official release",
            "verdict": "featured",
        },
    ]
    conn = _FunnelConn(
        total=1,
        rows=[_panorama_row(
            stage="displayed",
            blocked_reason=None,
            displayed=True,
            dominant_category="products",
            members=members,
        )],
    )
    _install_conn(monkeypatch, conn)

    result = remote_db.query_admin_highlights_funnel_rows_remote(q="target")

    row = result["items"][0]
    assert row["stage"] == "displayed"
    assert row["dominant_category"] == "products"
    assert [member["id"] for member in row["members"]] == [
        "target-drop",
        "product-featured-1",
        "product-featured-2",
    ]
    sql = conn.queries[-1][0]
    window_sql, search_sql = sql.split("matching_cluster_ids AS", 1)
    assert "%(query)s" not in window_sql
    assert "%(search_pattern)s" not in window_sql
    assert "i.title ILIKE %(search_pattern)s" in search_sql
    assert "i.cluster_title ILIKE %(search_pattern)s" in search_sql
    assert "selected_window_items AS" in sql
    assert "FROM selected_window_items i" in sql


@pytest.mark.parametrize("raw_veto", [None, "", "none"])
def test_panorama_rows_normalize_empty_veto_to_null(raw_veto):
    member = dict(_panorama_row()["members"][0], veto=raw_veto)

    payload = remote_db._admin_highlights_cluster_payload(
        _panorama_row(members=[member])
    )

    assert payload["members"][0]["veto"] is None


def test_frontend_veto_mapping_covers_backend_contract():
    source = (
        Path(__file__).parents[1]
        / "frontend-react/src/components/admin/HighlightsFilteredTab.tsx"
    ).read_text(encoding="utf-8")
    mapping = source.split("function humanVeto", 1)[1].split(
        "function categoryLabel", 1
    )[0]

    for veto in VALID_VETOES - {"none"}:
        assert f"{veto}:" in mapping


def test_anomaly_rows_remain_item_granularity(monkeypatch):
    conn = _FunnelConn(
        total=1,
        rows=[{
            "id": "broken-item",
            "ingested_at": datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            "title": "打分失败",
            "url": None,
            "cluster_id": None,
            "cluster_title": None,
            "highlight_scores": {},
            "uncertainty": None,
            "reason": "provider timeout",
            "stuck_at": "scoring",
            "error_summary": "provider timeout",
            "feedback_kind": None,
            "feedback_note": None,
        }],
    )
    _install_conn(monkeypatch, conn)

    result = remote_db.query_admin_highlights_funnel_rows_remote(view="anomaly")

    assert result["granularity"] == "item"
    assert result["items"][0]["stuck_at"] == "scoring"
    assert result["items"][0]["error_summary"] == "provider timeout"


@pytest.mark.parametrize("days", [0, 2, 8])
def test_funnel_rejects_unsupported_days(days):
    with pytest.raises(ValueError, match="days must be 1, 3, or 7"):
        remote_db.query_admin_highlights_funnel_remote(days=days)


@pytest.mark.parametrize("view", ["diff:display", "station:displayed", "unknown"])
def test_funnel_rows_reject_v28_and_unknown_views(view):
    with pytest.raises(ValueError, match="unsupported funnel view"):
        remote_db.query_admin_highlights_funnel_rows_remote(view=view)


def test_admin_funnel_endpoints_require_remote_mode(monkeypatch):
    import routes.admin as admin

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: False)

    response = asyncio.run(admin.admin_highlights_funnel_rows(_request(), view="panorama"))

    assert response.status_code == 501
    body = _decode_response(response)
    assert body["reason"] == "remote_required"
    assert body["granularity"] == "cluster"


def test_admin_funnel_routes_forward_tag_and_clamp_limit_to_100(monkeypatch):
    import routes.admin as admin

    calls = []

    async def fake_run_in_threadpool(func):
        return func()

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(admin, "run_in_threadpool", fake_run_in_threadpool)
    monkeypatch.setattr(
        admin.remote_db,
        "query_admin_highlights_funnel_remote",
        lambda **kwargs: calls.append(("counts", kwargs)) or {},
    )
    monkeypatch.setattr(
        admin.remote_db,
        "query_admin_highlights_funnel_rows_remote",
        lambda **kwargs: calls.append(("rows", kwargs)) or {},
    )

    asyncio.run(admin.admin_highlights_funnel(_request(), days=3, q="Claude", tag="tools"))
    asyncio.run(admin.admin_highlights_funnel_rows(_request(), limit=101))

    assert calls[0] == ("counts", {"days": 3, "q": "Claude", "tag": "tools"})
    assert calls[1][0] == "rows"
    assert calls[1][1]["limit"] == 100


class _AsyncRequest:
    def __init__(self, body, user=None):
        self.state = SimpleNamespace(user=user or {"id": "admin", "role": "admin"})
        self._body = body

    async def json(self):
        return self._body


def test_override_endpoint_validates_admin_and_calls_remote_atomic_writer(monkeypatch):
    import routes.admin as admin

    calls = []
    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        admin.remote_db,
        "set_admin_highlight_cluster_override_remote",
        lambda **kwargs: calls.append(kwargs) or {
            "ok": True,
            "manual_display": "force_show",
            "manual_display_at": "2026-07-17T00:00:00Z",
            "feedback_kind": "should_feature",
            "feedback_note": "应展示",
        },
    )

    result = asyncio.run(
        admin.admin_highlight_cluster_override(
            _AsyncRequest({"action": "force_show", "note": "  应展示  "}),
            42,
        )
    )

    assert result["manual_display"] == "force_show"
    assert calls == [{
        "cluster_id": 42,
        "user_id": "admin",
        "action": "force_show",
        "note": "应展示",
    }]


def test_override_endpoint_is_501_outside_remote_mode(monkeypatch):
    import routes.admin as admin

    monkeypatch.setattr(admin.remote_db, "app_state_to_remote", lambda: False)

    response = asyncio.run(
        admin.admin_highlight_cluster_override(
            _AsyncRequest({"action": "clear"}),
            42,
        )
    )

    assert response.status_code == 501
    assert _decode_response(response)["reason"] == "remote_required"


class _OverrideConn:
    def __init__(self, *, action):
        self.action = action
        self.calls = []
        self.events = []
        self.commits = 0

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        self.events.append(("execute", normalized))
        if "SELECT 1 FROM remote_poc.clusters" in normalized:
            return _Rows(row={"exists": 1})
        if "RETURNING manual_display" in normalized:
            return _Rows(row={
                "manual_display": None if self.action == "clear" else self.action,
                "manual_display_at": None if self.action == "clear" else "2026-07-17T00:00:00Z",
            })
        return _Rows()

    def commit(self):
        self.commits += 1
        self.events.append(("commit", None))


@pytest.mark.parametrize(
    ("action", "feedback_kind"),
    [("force_show", "should_feature"), ("force_hide", "irrelevant")],
)
def test_override_writer_double_writes_atomically_and_is_idempotent(
    monkeypatch, action, feedback_kind
):
    conn = _OverrideConn(action=action)
    _install_conn(monkeypatch, conn)
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda _user_id: 0)

    first = remote_db.set_admin_highlight_cluster_override_remote(
        cluster_id=42,
        user_id="admin",
        action=action,
        note="编辑判断",
    )
    second = remote_db.set_admin_highlight_cluster_override_remote(
        cluster_id=42,
        user_id="admin",
        action=action,
        note="编辑判断",
    )

    assert first == second
    assert first["manual_display"] == action
    assert first["feedback_kind"] == feedback_kind
    assert conn.commits == 2
    sql = " ".join(call[0] for call in conn.calls)
    assert "INSERT INTO remote_poc.highlight_cluster_decisions" in sql
    assert "manual_display_at = CASE" in sql
    assert "INSERT INTO remote_poc.cluster_status" in sql
    assert any(
        params and params.get("feedback_kind") == feedback_kind
        for _, params in conn.calls
    )


def test_override_clear_double_clears_and_is_idempotent(monkeypatch):
    conn = _OverrideConn(action="clear")
    _install_conn(monkeypatch, conn)
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda _user_id: 0)

    result = remote_db.set_admin_highlight_cluster_override_remote(
        cluster_id=42,
        user_id="admin",
        action="clear",
    )

    assert result["manual_display"] is None
    assert result["feedback_kind"] is None
    sql = " ".join(call[0] for call in conn.calls)
    assert "SET manual_display = NULL, manual_display_at = NULL" in sql
    assert "SET feedback_kind = NULL, feedback_at = NULL, feedback_note = NULL" in sql
    assert conn.commits == 1


@pytest.mark.parametrize("action", ["force_show", "force_hide", "clear"])
def test_override_writer_bumps_cluster_before_transaction_commit(monkeypatch, action):
    conn = _OverrideConn(action=action)
    _install_conn(monkeypatch, conn)
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)
    monkeypatch.setattr(remote_db, "clear_user_cache_keys", lambda _user_id: 0)

    remote_db.set_admin_highlight_cluster_override_remote(
        cluster_id=42,
        user_id="admin",
        action=action,
        note="刷新读模型候选",
    )

    bump_sql = (
        "UPDATE remote_poc.clusters SET last_updated_at = now() "
        "WHERE id = %(cluster_id)s"
    )
    statements = [sql for sql, _ in conn.calls]
    assert bump_sql in statements
    assert conn.events[-1] == ("commit", None)
    assert conn.events.index(("execute", bump_sql)) < len(conn.events) - 1
    assert conn.commits == 1


def test_old_station_diff_types_and_calls_are_deleted():
    source = open("frontend-react/src/lib/api.ts", encoding="utf-8").read()

    assert "`station:${AdminHighlightsStationKey}`" not in source
    assert "`diff:${AdminHighlightsDiffKey}`" not in source
    assert "view: 'panorama' | 'anomaly'" in source
