from contextlib import contextmanager
import inspect
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


def test_why_read_migration_adds_nullable_text_column():
    sql = Path(
        "supabase/migrations/0034_highlights_curation_v27_why_read.sql"
    ).read_text()

    assert "ALTER TABLE remote_poc.clusters" in sql
    assert "ADD COLUMN IF NOT EXISTS why_read text" in sql
    assert "NOT NULL" not in sql


def test_feedback_note_migration_adds_nullable_text_column():
    sql = Path(
        "supabase/migrations/0035_cluster_status_feedback_note.sql"
    ).read_text()

    assert "ALTER TABLE remote_poc.cluster_status" in sql
    assert "ADD COLUMN IF NOT EXISTS feedback_note text" in sql
    assert "DROP COLUMN IF EXISTS feedback_note" in sql
    assert "NOT NULL" not in sql


def test_manual_display_migration_adds_nullable_checked_columns():
    sql = Path(
        "supabase/migrations/0036_highlights_manual_display.sql"
    ).read_text()

    assert "ALTER TABLE remote_poc.highlight_cluster_decisions" in sql
    assert "ADD COLUMN IF NOT EXISTS manual_display text" in sql
    assert "manual_display IN ('force_show', 'force_hide')" in sql
    assert "ADD COLUMN IF NOT EXISTS manual_display_at timestamptz" in sql
    assert "CREATE TABLE" not in sql


class _CaptureConn:
    def __init__(self):
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.sqls.append(normalized)
        self.params.append(params or {})
        if "FROM remote_poc.highlights_read_model_state" in normalized:
            return _FakeCursor(
                row={
                    "version_id": "00000000-0000-0000-0000-00000000abcd",
                    "completed_at": "2026-07-15T00:00:00Z",
                    "window_days": 7,
                    "min_github_stars": 50,
                    "meta_json": {"read_model": "highlights_v1"},
                    "max_sort_at": "2026-07-15T00:00:00Z",
                }
            )
        if "max(delta_checkpoint_at) AS max_delta_checkpoint_at" in normalized:
            return _FakeCursor(
                row={
                    "clusters": 1,
                    "max_delta_checkpoint_at": "2026-07-15T01:00:00Z",
                }
            )
        if "SELECT count(*) AS scope_rows" in normalized:
            return _FakeCursor(row={"scope_rows": 1})
        if "SELECT count(*) AS n" in normalized:
            return _FakeCursor(row={"n": 1})
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = [] if rows is None else rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def test_remote_summary_draft_refreshes_why_read_in_same_update():
    conn = _CaptureConn()

    remote_db.write_cluster_summary_draft_remote(
        conn,
        301,
        title="新标题",
        summary="新总结",
        key_points=[],
        why_read=None,
        is_visible=True,
        warnings=[],
        run_id=None,
    )

    assert "why_read = %s" in conn.sqls[-1]
    assert conn.params[-1][3] is None


def _capture_decisions_sql():
    conn = _CaptureConn()
    remote_db._sync_highlight_cluster_decisions(
        conn,
        "remote_poc",
        window_days=7,
        min_github_stars=50,
    )
    return next(
        sql
        for sql in conn.sqls
        if "INSERT INTO remote_poc.highlight_cluster_decisions" in sql
    )


def test_decisions_store_highest_flagged_v26_score10_without_losing_score_inputs():
    sql = _capture_decisions_sql()

    assert "max_flag_score10" in sql
    assert "max((highlight_scores->'v26'->>'score10')::numeric)" in sql
    assert "WHERE highlight_include_in_highlights IS TRUE" in sql
    assert "jsonb_strip_nulls" in sql
    assert "||" in sql
    assert "'max_q'" in sql
    assert "'avg_q'" in sql
    assert "'scored_include_count'" in sql
    assert "'unique_source_count'" in sql


def test_display_threshold_missing_or_empty_keeps_gate_disabled():
    assert remote_db._highlights_display_threshold({}) is None
    assert (
        remote_db._highlights_display_threshold(
            {remote_db.HIGHLIGHTS_DISPLAY_THRESHOLD_ENV: "   "}
        )
        is None
    )


def test_display_threshold_invalid_warns_once_and_disables_gate(caplog):
    caplog.set_level("WARNING")

    threshold = remote_db._highlights_display_threshold(
        {remote_db.HIGHLIGHTS_DISPLAY_THRESHOLD_ENV: "not-a-score"}
    )

    assert threshold is None
    warnings = [
        record
        for record in caplog.records
        if remote_db.HIGHLIGHTS_DISPLAY_THRESHOLD_ENV in record.getMessage()
    ]
    assert len(warnings) == 1


def test_display_threshold_valid_value_builds_fail_closed_cluster_filter():
    threshold = remote_db._highlights_display_threshold(
        {remote_db.HIGHLIGHTS_DISPLAY_THRESHOLD_ENV: "7.0"}
    )

    sql = remote_db._highlights_display_cluster_filter(
        "remote_poc",
        "c",
        threshold=threshold,
    )
    assert threshold == 7.0
    assert "c.why_read IS NOT NULL" in sql
    assert "hcd.cluster_id = c.id" in sql
    assert "(hcd.score_inputs->>'max_flag_score10')::float >= 7.0" in sql
    assert remote_db._highlights_display_cluster_filter(
        "remote_poc",
        "c",
        threshold=None,
    ) != ""


def test_why_read_candidates_include_manual_force_show_below_threshold():
    source = (ROOT / "scripts" / "v27_why_read_backfill.py").read_text()

    assert "d.manual_display = 'force_show'" in source
    assert "OR d.manual_display = 'force_show'" in source


def test_display_threshold_partitions_feed_fallback_cache_keys():
    common = {
        "limit": 20,
        "public_only": True,
        "min_github_stars": 50,
        "enabled": True,
        "categories": [],
    }

    disabled = remote_db._events_snapshot_key(**common, display_threshold=None)
    enabled = remote_db._events_snapshot_key(**common, display_threshold=7.0)

    assert disabled != enabled
    assert "display=7.0" in enabled


def test_read_model_date_counts_cache_and_total_follow_display_threshold(monkeypatch):
    class _DateCountConn(_CaptureConn):
        def __init__(self):
            super().__init__()
            self.date_count = 5
            self.date_queries = 0

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                self.sqls.append(normalized)
                self.params.append(params or {})
                return _FakeCursor(
                    row={
                        "version_id": "00000000-0000-0000-0000-00000000abcd",
                        "scope_key": "all",
                        "total_count": 9,
                    }
                )
            if "GROUP BY day" in normalized:
                self.sqls.append(normalized)
                self.params.append(params or {})
                self.date_queries += 1
                return _FakeCursor(
                    rows=[{"day": "2026-07-15", "n": self.date_count}]
                )
            return super().execute(sql, params)

    fake = _DateCountConn()
    common = {
        "conn": fake,
        "schema": "remote_poc",
        "page": 1,
        "limit": 20,
        "cursor": None,
        "since_version_snapshot": None,
        "fetched_since": None,
        "user_id": None,
        "public_only": True,
        "min_github_stars": 50,
        "enabled": True,
        "categories": [],
        "timezone_offset_minutes": -480,
    }
    remote_db.clear_feed_cache_keys()
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")

    disabled = remote_db._query_highlights_read_model_events(
        **common,
        display_threshold=None,
    )
    fake.date_count = 2
    enabled = remote_db._query_highlights_read_model_events(
        **common,
        display_threshold=7.0,
    )
    remote_db.clear_feed_cache_keys()

    assert disabled["date_counts"] == {"2026-07-15": 5}
    assert disabled["total_available_within_30d"] == 9
    assert enabled["date_counts"] == {"2026-07-15": 2}
    assert enabled["total_available_within_30d"] == 2
    assert fake.date_queries == 2


def _capture_highlights_refresh(monkeypatch, *, delta):
    fake = _CaptureConn()

    @contextmanager
    def fake_connect():
        yield fake

    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", "7.0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "clear_feed_cache_keys", lambda: 0)

    if delta:
        remote_db.refresh_highlights_read_model_delta_in_place()
    else:
        remote_db.refresh_highlights_read_model()
    return fake


def test_display_gate_filters_full_and_delta_highlights_read_model_builds(monkeypatch):
    full = _capture_highlights_refresh(monkeypatch, delta=False)
    full_scope_sql = next(
        sql
        for sql in full.sqls
        if "INSERT INTO remote_poc.highlights_scopes" in sql
    )

    delta = _capture_highlights_refresh(monkeypatch, delta=True)
    delta_scope_sql = next(
        sql
        for sql in delta.sqls
        if "CREATE TEMP TABLE highlights_read_model_delta_scope_rows" in sql
    )

    for sql in (full_scope_sql, delta_scope_sql):
        assert "c.why_read IS NOT NULL" in sql
        assert "(hcd.score_inputs->>'max_flag_score10')::float >= 7.0" in sql


def _capture_live_fetch_sql(monkeypatch, threshold):
    class _LiveConn(_CaptureConn):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return _FakeCursor()
            if "SELECT count(*) AS n FROM remote_poc.clusters c" in normalized:
                return _FakeCursor(row={"n": 0})
            if "GROUP BY day" in normalized:
                return _FakeCursor(rows=[])
            if "FROM remote_poc.clusters c" in normalized:
                return _FakeCursor(rows=[])
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = _LiveConn()

    @contextmanager
    def fake_connect():
        yield fake

    remote_db.clear_feed_cache_keys()
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "0")
    if threshold is None:
        monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", threshold)
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    remote_db.fetch_events(
        page=2,
        limit=10,
        public_only=True,
        min_github_stars=50,
        categories=[],
    )
    remote_db.clear_feed_cache_keys()
    return next(
        sql
        for sql in fake.sqls
        if "SELECT c.id, c.ai_title" in sql
        and "FROM remote_poc.clusters c" in sql
    )


def test_fetch_events_display_gate_is_opt_in_and_fail_closed(monkeypatch):
    disabled_sql = _capture_live_fetch_sql(monkeypatch, None)
    enabled_sql = _capture_live_fetch_sql(monkeypatch, "7.0")

    assert "c.why_read IS NOT NULL" not in disabled_sql
    assert "(hcd.score_inputs->>'max_flag_score10')::float" not in disabled_sql
    assert "c.why_read IS NOT NULL" in enabled_sql
    assert "(hcd.score_inputs->>'max_flag_score10')::float >= 7.0" in enabled_sql


def test_fetch_events_read_model_query_rechecks_enabled_display_gate(monkeypatch):
    fake = _CaptureConn()

    @contextmanager
    def fake_connect():
        yield fake

    remote_db.clear_feed_cache_keys()
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", "7.0")
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    remote_db.fetch_events(
        page=2,
        limit=10,
        public_only=True,
        min_github_stars=50,
        categories=[],
    )
    remote_db.clear_feed_cache_keys()

    scope_sql = next(
        sql
        for sql in fake.sqls
        if "SELECT h.rank" in sql
        and "FROM remote_poc.highlights_scope_items h" in sql
    )
    assert "JOIN remote_poc.clusters c ON c.id = h.cluster_id" in scope_sql
    assert "c.why_read IS NOT NULL" in scope_sql
    assert "(hcd.score_inputs->>'max_flag_score10')::float >= 7.0" in scope_sql


def test_display_gate_is_not_referenced_by_non_highlights_surfaces():
    unaffected = (
        remote_db._query_feed_platforms_read_model,
        remote_db.search_recommend_remote,
        remote_db.query_highlight_cluster_decisions_remote,
        remote_db.get_action_source_items_remote,
    )

    for func in unaffected:
        source = inspect.getsource(func)
        assert "HIGHLIGHTS_DISPLAY_THRESHOLD_ENV" not in source
        assert "_highlights_display_cluster_filter" not in source


def _fetch_read_model_event(monkeypatch, max_flag_score10, why_read="读模型中的必读理由"):
    class _ReadModelConn(_CaptureConn):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return _FakeCursor()
            if "FROM remote_poc.highlights_read_model_state" in normalized:
                return _FakeCursor(
                    row={
                        "version_id": "00000000-0000-0000-0000-00000000abcd",
                        "scope_key": "all",
                        "total_count": 1,
                    }
                )
            if "GROUP BY day" in normalized:
                return _FakeCursor(rows=[{"day": "2026-07-15", "n": 1}])
            if "SELECT h.rank" in normalized:
                return _FakeCursor(
                    rows=[
                        {
                            "rank": 1,
                            "cluster_id": 301,
                            "sort_at": "2026-07-15T01:00:00Z",
                            "card_json": {
                                "id": 301,
                                "ai_title": "Read model event",
                                "ai_summary": "summary",
                                "doc_count": 2,
                                "unique_source_count": 2,
                                "category": "products",
                                "source_preview": [],
                                "first_doc_at": "2026-07-15T01:00:00Z",
                                "last_doc_at": "2026-07-15T01:00:00Z",
                                "platforms": ["twitter"],
                                "cover_url": None,
                                "live_version": 5,
                            },
                            "highlight_score": 72.5,
                            "cluster_verdict": "featured",
                            "value_path": "substantive",
                            "featured_count": 2,
                            "max_flag_score10": max_flag_score10,
                            "why_read": why_read,
                        }
                    ]
                )
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = _ReadModelConn()

    @contextmanager
    def fake_connect():
        yield fake

    remote_db.clear_feed_cache_keys()
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "1")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK", "0")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", raising=False)
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    result = remote_db.fetch_events(
        page=2,
        limit=10,
        public_only=True,
        min_github_stars=50,
        categories=[],
    )
    remote_db.clear_feed_cache_keys()
    return result["events"][0], fake


def test_fetch_events_read_model_exposes_rounded_display_score(monkeypatch):
    event, fake = _fetch_read_model_event(monkeypatch, 7.34)

    assert event["display_score"] == 73
    assert event["why_read"] == "读模型中的必读理由"
    assert event["highlight_score"] == 72.5
    assert event["cluster_verdict"] == "featured"
    assert event["value_path"] == "substantive"
    assert event["featured_count"] == 2
    scope_sql = next(sql for sql in fake.sqls if "SELECT h.rank" in sql)
    assert "d.score_inputs->>'max_flag_score10'" in scope_sql
    assert "c.why_read AS why_read" in scope_sql


def test_fetch_events_read_model_display_score_is_null_when_score_missing(monkeypatch):
    event, _ = _fetch_read_model_event(monkeypatch, None, why_read=None)

    assert event["id"] == 301
    assert event["display_score"] is None
    assert event["why_read"] is None


def test_fetch_events_live_fallback_exposes_rounded_display_score(monkeypatch):
    class _LiveEventConn(_CaptureConn):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.sqls.append(normalized)
            self.params.append(params or {})
            if normalized.startswith("SET LOCAL"):
                return _FakeCursor()
            if "SELECT count(*) AS n FROM remote_poc.clusters c" in normalized:
                return _FakeCursor(row={"n": 1})
            if "GROUP BY day" in normalized:
                return _FakeCursor(rows=[{"day": "2026-07-15", "n": 1}])
            if "SELECT c.id, c.ai_title" in normalized:
                return _FakeCursor(
                    rows=[
                        {
                            "id": 302,
                            "ai_title": "Live event",
                            "ai_summary": "summary",
                            "doc_count": 1,
                            "unique_source_count": 1,
                            "first_doc_at": "2026-07-15T02:00:00Z",
                            "last_doc_at": None,
                            "platforms_json": ["twitter"],
                            "cover_url": None,
                            "live_version": 1,
                            "last_updated_at": "2026-07-15T02:00:00Z",
                            "max_flag_score10": 7.34,
                            "why_read": "live fallback 中的必读理由",
                        }
                    ]
                )
            raise AssertionError(f"unexpected SQL: {normalized}")

    fake = _LiveEventConn()

    @contextmanager
    def fake_connect():
        yield fake

    remote_db.clear_feed_cache_keys()
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHTS_READ_MODEL", "0")
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD", raising=False)
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *args: {})

    result = remote_db.fetch_events(
        page=2,
        limit=10,
        public_only=True,
        min_github_stars=50,
        categories=[],
    )
    remote_db.clear_feed_cache_keys()

    event = result["events"][0]
    assert event["id"] == 302
    assert event["ai_title"] == "Live event"
    assert event["display_score"] == 73
    assert event["why_read"] == "live fallback 中的必读理由"
    event_sql = next(sql for sql in fake.sqls if "SELECT c.id, c.ai_title" in sql)
    assert "d.score_inputs->>'max_flag_score10'" in event_sql
    assert "c.why_read" in event_sql
