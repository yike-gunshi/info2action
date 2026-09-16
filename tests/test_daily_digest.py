import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import daily_digest  # noqa: E402


def _candidate(cluster_id, *, title=None, source_count=2):
    return {
        "cluster_id": cluster_id,
        "ai_title": title or f"title-{cluster_id}",
        "why_read": f"why-{cluster_id}",
        "unique_source_count": source_count,
        "highlight_score": 80,
        "max_flag_score10": 8,
        "single_source": source_count < 2,
        "force_show": False,
        "links": [{
            "author": f"author-{cluster_id}",
            "platform": "example",
            "url": f"https://example.com/{cluster_id}",
        }],
    }


def _previous_entry(cluster_id):
    return {
        "rank": 1,
        "cluster_id": cluster_id,
        "title": f"old-{cluster_id}",
        "source_count": 1,
        "links": [],
    }


def test_rolling_keeps_previous_candidate_and_drops_force_hidden_candidate():
    candidates = [_candidate("new"), _candidate("keep")]
    selected = [{"cluster_id": "new", "pick_reason": "new event"}]

    entries = daily_digest.validate_and_build_entries(
        candidates,
        selected,
        previous_entries=[_previous_entry("keep"), _previous_entry("force-hidden")],
        rolling=True,
    )

    assert [entry["cluster_id"] for entry in entries] == ["new", "keep"]
    assert all(entry["cluster_id"] != "force-hidden" for entry in entries)
    assert entries[1] == {
        "rank": 2,
        "cluster_id": "keep",
        "title": "title-keep",
        "source_count": 2,
        "links": [{"url": "https://example.com/keep", "label": "author-keep"}],
    }


def test_rolling_trims_new_entries_before_previous_entries():
    old_ids = [f"old-{index}" for index in range(9)]
    candidates = [_candidate("new-a"), _candidate("new-b")] + [
        _candidate(cluster_id) for cluster_id in old_ids
    ]

    entries = daily_digest.validate_and_build_entries(
        candidates,
        [
            {"cluster_id": "new-a", "pick_reason": "new a"},
            {"cluster_id": "new-b", "pick_reason": "new b"},
        ],
        previous_entries=[_previous_entry(cluster_id) for cluster_id in old_ids],
        rolling=True,
        max_items=10,
    )

    assert len(entries) == 10
    assert {entry["cluster_id"] for entry in entries}.issuperset(old_ids)
    assert [entry["cluster_id"] for entry in entries if entry["cluster_id"].startswith("new-")] == [
        "new-a"
    ]


def test_selected_validation_uses_only_cluster_id_and_drops_invalid_or_duplicate_ids():
    candidates = [_candidate("a"), _candidate("b"), _candidate("c")]

    entries = daily_digest.validate_and_build_entries(
        candidates,
        [
            {"cluster_id": "a", "pick_reason": "valid"},
            {"cluster_id": "missing", "pick_reason": "invalid"},
            {"cluster_id": "b", "pick_reason": "valid"},
            {"cluster_id": "b", "pick_reason": "duplicate"},
        ],
        rolling=False,
    )

    assert [entry["cluster_id"] for entry in entries] == ["a", "b"]
    assert all("merged_cluster_ids" not in entry for entry in entries)


def test_selected_without_merged_fields_saves_entry_links_with_author_label(monkeypatch):
    candidate = _candidate("selected")
    writes = []
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: None)
    monkeypatch.setattr(daily_digest, "fetch_candidates", lambda _target_date: [candidate])
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: {
            "selected": [{"cluster_id": "selected", "pick_reason": "must know"}],
            "_model": "MiniMax-Test",
        },
    )
    monkeypatch.setattr(daily_digest, "save_digest", lambda **payload: writes.append(payload))

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "saved_editor"
    assert writes[0]["entries"] == [{
        "rank": 1,
        "cluster_id": "selected",
        "title": "title-selected",
        "source_count": 2,
        "links": [{
            "url": "https://example.com/selected",
            "label": "author-selected",
        }],
    }]


def test_same_candidate_ids_skip_editor_and_write(monkeypatch):
    existing = {
        "status": "rolling",
        "source": "editor",
        "candidate_ids": ["a", "b"],
        "entries": [_previous_entry("a")],
    }
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: existing)
    monkeypatch.setattr(
        daily_digest,
        "fetch_candidates",
        lambda _target_date: [_candidate("a"), _candidate("b")],
    )
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: pytest.fail("unchanged candidates must skip LLM"),
    )
    monkeypatch.setattr(
        daily_digest,
        "save_digest",
        lambda *_args, **_kwargs: pytest.fail("unchanged candidates must not write"),
    )

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "skipped_unchanged_candidates"


def test_rules_fallback_retries_editor_when_candidate_ids_are_unchanged(monkeypatch):
    existing = {
        "status": "rolling",
        "source": "rules_fallback",
        "candidate_ids": ["a", "b"],
        "entries": [_previous_entry("a")],
    }
    editor_calls = []
    writes = []
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: existing)
    monkeypatch.setattr(
        daily_digest,
        "fetch_candidates",
        lambda _target_date: [_candidate("a"), _candidate("b")],
    )
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: editor_calls.append(True) or {
            "selected": [{"cluster_id": "a"}],
            "_model": "MiniMax-Test",
        },
    )
    monkeypatch.setattr(daily_digest, "save_digest", lambda **payload: writes.append(payload))

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "saved_editor"
    assert editor_calls == [True]
    assert writes[0]["source"] == "editor"


def test_rolling_rechecks_previous_entries_outside_top30_before_unchanged_skip(monkeypatch):
    existing = {
        "status": "rolling",
        "source": "editor",
        "candidate_ids": ["new"],
        "entries": [_previous_entry("eligible"), _previous_entry("force-hidden")],
    }
    eligibility_checks = []
    writes = []
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: existing)
    monkeypatch.setattr(daily_digest, "fetch_candidates", lambda _target_date: [_candidate("new")])
    monkeypatch.setattr(
        daily_digest,
        "fetch_eligible_previous_cluster_ids",
        lambda target_date, cluster_ids: eligibility_checks.append((target_date, cluster_ids)) or {"eligible"},
        raising=False,
    )
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: {
            "selected": [{"cluster_id": "new"}],
            "_model": "MiniMax-Test",
        },
    )
    monkeypatch.setattr(daily_digest, "save_digest", lambda **payload: writes.append(payload))

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "saved_editor"
    assert eligibility_checks == [(date(2026, 8, 1), ["eligible", "force-hidden"])]
    assert [entry["cluster_id"] for entry in writes[0]["entries"]] == ["new", "eligible"]
    assert writes[0]["entries"][1] == {
        "rank": 2,
        "cluster_id": "eligible",
        "title": "old-eligible",
        "source_count": 1,
        "links": [],
    }


def test_llm_failure_keeps_existing_snapshot_unchanged(monkeypatch):
    existing = {
        "status": "rolling",
        "candidate_ids": ["old"],
        "entries": [_previous_entry("old")],
    }
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: existing)
    monkeypatch.setattr(
        daily_digest,
        "fetch_candidates",
        lambda _target_date: [_candidate("old"), _candidate("new")],
    )
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("LLM timeout")),
    )
    monkeypatch.setattr(
        daily_digest,
        "save_digest",
        lambda *_args, **_kwargs: pytest.fail("existing snapshot must remain untouched"),
    )

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "llm_failed_kept_existing"


def test_llm_failure_without_snapshot_writes_rules_fallback(monkeypatch):
    candidates = [_candidate(f"c-{index}") for index in range(12)]
    writes = []
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: None)
    monkeypatch.setattr(daily_digest, "fetch_candidates", lambda _target_date: candidates)
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad response")),
    )
    monkeypatch.setattr(daily_digest, "save_digest", lambda **payload: writes.append(payload))

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "saved_rules_fallback"
    assert writes[0]["source"] == "rules_fallback"
    assert writes[0]["candidate_ids"] == [candidate["cluster_id"] for candidate in candidates]
    assert len(writes[0]["entries"]) == 10
    assert [entry["rank"] for entry in writes[0]["entries"]] == list(range(1, 11))


def test_final_snapshot_is_immutable_in_every_mode(monkeypatch):
    monkeypatch.setattr(
        daily_digest,
        "load_digest",
        lambda _target_date: {"status": "final", "candidate_ids": [], "entries": []},
    )
    monkeypatch.setattr(
        daily_digest,
        "fetch_candidates",
        lambda _target_date: pytest.fail("final date must stop before querying candidates"),
    )

    result = daily_digest.generate_for_date(date(2026, 7, 31), status="rolling")

    assert result["outcome"] == "skipped_final_exists"


def test_auto_after_0200_self_heals_yesterday_before_today(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daily_digest,
        "generate_for_date",
        lambda target_date, *, status: calls.append((target_date, status)) or {"outcome": "ok"},
    )
    now = datetime(2026, 8, 1, 2, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    daily_digest.run_auto(now=now)

    assert calls == [
        (date(2026, 7, 31), "final"),
        (date(2026, 8, 1), "rolling"),
    ]


def test_auto_before_0200_only_runs_today(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daily_digest,
        "generate_for_date",
        lambda target_date, *, status: calls.append((target_date, status)) or {"outcome": "ok"},
    )
    now = datetime(2026, 8, 1, 1, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    daily_digest.run_auto(now=now)

    assert calls == [(date(2026, 8, 1), "rolling")]


def test_backfill_runs_previous_days_as_final_oldest_first(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daily_digest,
        "generate_for_date",
        lambda target_date, *, status: calls.append((target_date, status)) or {"outcome": "ok"},
    )
    now = datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    daily_digest.run_backfill(3, now=now)

    assert calls == [
        (date(2026, 7, 29), "final"),
        (date(2026, 7, 30), "final"),
        (date(2026, 7, 31), "final"),
    ]


def test_fetch_candidates_reuses_highlights_display_gate(monkeypatch):
    executed = []

    class Cursor:
        def fetchall(self):
            return []

    class Conn:
        def execute(self, sql, params):
            executed.append((sql, params))
            return Cursor()

    @contextmanager
    def fake_connect():
        yield Conn()

    monkeypatch.setattr(daily_digest.remote_db, "connect", fake_connect)
    monkeypatch.setattr(daily_digest.remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(daily_digest.remote_db, "_highlights_display_threshold", lambda: 4.75)
    monkeypatch.setattr(
        daily_digest.remote_db,
        "_highlights_display_cluster_condition",
        lambda schema, alias, *, threshold: "REUSED_DISPLAY_GATE",
    )

    daily_digest.fetch_candidates(date(2026, 8, 1))

    assert "REUSED_DISPLAY_GATE" in executed[0][0]
    assert "AT TIME ZONE 'Asia/Shanghai'" in executed[0][0]
    assert "LIMIT 30" in executed[0][0]
    assert "ORDER BY (hcd.manual_display = 'force_show') DESC," in executed[0][0]
    assert "hcd.highlight_score DESC NULLS LAST, c.id DESC" in executed[0][0]
    assert "c.ai_summary" in executed[0][0]
    assert "c.ai_key_points" in executed[0][0]
    assert "'author', member.author_name" in executed[0][0]


def test_save_digest_guards_final_row_inside_upsert(monkeypatch):
    executed = []

    class Conn:
        def execute(self, sql, params):
            executed.append((sql, params))

    @contextmanager
    def fake_connect():
        yield Conn()

    monkeypatch.setattr(daily_digest.remote_db, "connect", fake_connect)
    monkeypatch.setattr(daily_digest.remote_db, "remote_schema", lambda: "remote_poc")

    daily_digest.save_digest(
        target_date=date(2026, 8, 1),
        status="rolling",
        entries=[],
        candidate_ids=[],
        source="editor",
    )

    assert "INSERT INTO remote_poc.daily_digest AS existing_digest" in executed[0][0]
    assert "WHERE existing_digest.status <> 'final'" in executed[0][0]


def test_save_digest_serializes_decimal_values(monkeypatch):
    executed = []

    class Conn:
        def execute(self, sql, params):
            executed.append((sql, params))

    @contextmanager
    def fake_connect():
        yield Conn()

    monkeypatch.setattr(daily_digest.remote_db, "connect", fake_connect)
    monkeypatch.setattr(daily_digest.remote_db, "remote_schema", lambda: "remote_poc")

    daily_digest.save_digest(
        target_date=date(2026, 8, 1),
        status="rolling",
        entries=[{"cluster_id": Decimal("123"), "score": Decimal("80.25")}],
        candidate_ids=[Decimal("123")],
        source="editor",
    )

    assert json.loads(executed[0][1][2]) == [{"cluster_id": 123.0, "score": 80.25}]
    assert json.loads(executed[0][1][3]) == [123.0]


def test_missing_table_error_points_to_migration_0038(monkeypatch):
    @contextmanager
    def missing_table_connect():
        raise Exception('relation "remote_poc.daily_digest" does not exist')
        yield

    monkeypatch.setattr(daily_digest.remote_db, "connect", missing_table_connect)
    monkeypatch.setattr(daily_digest.remote_db, "remote_schema", lambda: "remote_poc")

    with pytest.raises(RuntimeError, match="请先跑迁移 0038"):
        daily_digest.load_digest(date(2026, 8, 1))


def test_editor_uses_shared_minimax_config_temperature_zero_and_last_json(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        daily_digest.enrich_items,
        "load_config",
        lambda: {"ai_summary": {"provider": "minimax"}},
    )
    monkeypatch.setattr(
        daily_digest.enrich_items,
        "resolve_minimax_runtime_config",
        lambda _config: ("key", "https://minimax.test", "MiniMax-Test"),
    )

    def fake_load_prompt(filename, **kwargs):
        calls["prompt"] = (filename, kwargs)
        return f"payload={kwargs['payload_json']} max={kwargs['max_items']}"

    def fake_call(*args, **kwargs):
        calls["call"] = (args, kwargs)
        return """analysis draft {\"selected\":[{\"cluster_id\":\"wrong\"}]}
```json
{\"selected\":[{\"cluster_id\":\"right\",\"pick_reason\":\"must know\"}]}
```"""

    monkeypatch.setattr(daily_digest, "load_prompt", fake_load_prompt)
    monkeypatch.setattr(daily_digest.enrich_items, "call_minimax", fake_call)

    result = daily_digest.call_editor(
        date(2026, 8, 1),
        [_candidate("right")],
        [],
        is_final_pass=False,
    )

    assert result["selected"][0]["cluster_id"] == "right"
    assert result["_model"] == "MiniMax-Test"
    assert calls["prompt"][0] == daily_digest.PROMPT_FILE
    assert calls["prompt"][1]["max_items"] == 10
    assert json.loads(calls["prompt"][1]["payload_json"])["candidates"][0]["cluster_id"] == "right"
    assert calls["call"][1]["max_tokens"] == 8192
    assert calls["call"][1]["temperature"] == 0.0


def test_editor_uses_configured_prompt_and_current_version_from_header():
    assert daily_digest.PROMPT_FILE == "16_daily_digest_editor_v2.md"
    assert daily_digest.PROMPT_VERSION == "daily_digest_editor_v3_2_2026_08_02"


def test_editor_payload_contains_v2_candidate_context_and_serializes_decimal_scores():
    candidate = _candidate("rich")
    candidate.update({
        "ai_summary": "摘" * 401,
        "ai_key_points": json.dumps([f"point-{index}" for index in range(6)]),
        "highlight_score": Decimal("80.25"),
        "links": [
            {
                "author": "Alice",
                "platform": "x",
                "url": "https://example.com/primary",
            },
            {
                "author": "",
                "platform": "github",
                "url": "https://github.com/example/repo",
            },
        ],
    })

    payload = json.loads(daily_digest._json_dumps(daily_digest._editor_payload(
        date(2026, 8, 1),
        [candidate],
        [],
        is_final_pass=False,
    )))

    payload_candidate = payload["candidates"][0]
    assert payload_candidate["summary"] == "摘" * 400 + "…"
    assert payload_candidate["key_points"] == [f"point-{index}" for index in range(5)]
    assert payload_candidate["links"] == candidate["links"]
    assert payload_candidate["highlight_score"] == 80.25


def test_editor_payload_uses_null_for_missing_summary_and_key_points():
    payload = daily_digest._editor_payload(
        date(2026, 8, 1),
        [_candidate("sparse")],
        [],
        is_final_pass=False,
    )

    assert payload["candidates"][0]["summary"] is None
    assert payload["candidates"][0]["key_points"] is None


def test_editor_payload_serializes_decimal_candidate_scores(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        daily_digest.enrich_items,
        "load_config",
        lambda: {"ai_summary": {"provider": "minimax"}},
    )
    monkeypatch.setattr(
        daily_digest.enrich_items,
        "resolve_minimax_runtime_config",
        lambda _config: ("key", "https://minimax.test", "MiniMax-Test"),
    )

    def fake_load_prompt(_filename, **kwargs):
        calls["payload"] = json.loads(kwargs["payload_json"])
        return "prompt"

    monkeypatch.setattr(daily_digest, "load_prompt", fake_load_prompt)
    monkeypatch.setattr(
        daily_digest.enrich_items,
        "call_minimax",
        lambda *_args, **_kwargs: '{"selected": []}',
    )
    candidate = _candidate("decimal")
    candidate["highlight_score"] = Decimal("80.25")
    candidate["max_flag_score10"] = Decimal("9.5")

    daily_digest.call_editor(
        date(2026, 8, 1),
        [candidate],
        [],
        is_final_pass=False,
    )

    payload_candidate = calls["payload"]["candidates"][0]
    assert payload_candidate["highlight_score"] == 80.25
    assert payload_candidate["source_count"] == 2.0
    assert "max_flag_score10" not in payload_candidate


def test_empty_candidate_pool_does_not_call_editor_or_write(monkeypatch):
    monkeypatch.setattr(daily_digest, "load_digest", lambda _target_date: None)
    monkeypatch.setattr(daily_digest, "fetch_candidates", lambda _target_date: [])
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: pytest.fail("empty pool must not call LLM"),
    )
    monkeypatch.setattr(
        daily_digest,
        "save_digest",
        lambda *_args, **_kwargs: pytest.fail("empty pool must not write a row"),
    )

    result = daily_digest.generate_for_date(date(2026, 8, 1), status="rolling")

    assert result["outcome"] == "skipped_no_candidates"


@pytest.mark.parametrize("status", ["rolling", "final"])
def test_empty_candidate_pool_clears_and_finalizes_existing_snapshot(monkeypatch, status):
    writes = []
    monkeypatch.setattr(
        daily_digest,
        "load_digest",
        lambda _target_date: {
            "status": "rolling",
            "entries": [_previous_entry("stale")],
            "candidate_ids": ["stale"],
            "source": "rules_fallback",
        },
    )
    monkeypatch.setattr(daily_digest, "fetch_candidates", lambda _target_date: [])
    monkeypatch.setattr(
        daily_digest,
        "call_editor",
        lambda *_args, **_kwargs: pytest.fail("empty pool must not call LLM"),
    )
    monkeypatch.setattr(daily_digest, "save_digest", lambda **payload: writes.append(payload))

    result = daily_digest.generate_for_date(date(2026, 8, 1), status=status)

    assert result["outcome"] == "saved_empty"
    assert writes == [{
        "target_date": date(2026, 8, 1),
        "status": status,
        "entries": [],
        "candidate_ids": [],
        "source": "rules_fallback",
        "model": None,
    }]


def test_daily_digest_migration_matches_schema_contract():
    text = (ROOT / "supabase" / "migrations" / "0038_daily_digest.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS remote_poc.daily_digest" in text
    assert "digest_date date PRIMARY KEY" in text
    assert "status text NOT NULL" in text
    assert "status IN ('rolling', 'final')" in text
    assert "entries jsonb NOT NULL DEFAULT '[]'::jsonb" in text
    assert "source IN ('editor', 'rules_fallback')" in text


def test_hourly_pipeline_runs_daily_digest_without_failing_pipeline():
    text = (ROOT / "ops" / "cron_hourly_pipeline_light.sh").read_text()

    assert "-m src.daily_digest --mode auto" in text
    assert "daily digest status=" in text
    assert text.index("-m src.daily_digest --mode auto") > text.index("-- remote sync")


@pytest.fixture()
def daily_digest_client(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", "daily-digest-test-secret-long-enough-32")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("INFO2ACTION_DATA_AUTHORITY", "local")
    monkeypatch.setenv("INFO2ACTION_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_EVENT_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_FEED_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_STATUS_BACKEND", "sqlite")

    import app as app_mod
    import db as db_mod
    import middleware.auth as auth_mw
    import routes.clusters as clusters_route

    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "daily-digest.db"))
    monkeypatch.setattr(auth_mw, "_AUTH_TOKEN", "")
    app_mod.app.state.limiter.enabled = False
    yield TestClient(app_mod.app), clusters_route


@pytest.mark.parametrize(
    "query",
    [
        "start=bad&end=2026-08-01",
        "start=2026-08-02&end=2026-08-01",
        "start=2026-06-01&end=2026-08-01",
    ],
)
def test_daily_digest_api_rejects_invalid_ranges(daily_digest_client, query):
    client, _route = daily_digest_client

    response = client.get(f"/api/feed/daily-digest?{query}")

    assert response.status_code == 422


def test_daily_digest_api_returns_descending_snapshots(daily_digest_client, monkeypatch):
    client, clusters_route = daily_digest_client
    rows = [
        {
            "date": "2026-08-01",
            "status": "rolling",
            "entries": [_previous_entry("a")],
            "updated_at": "2026-08-01T02:00:00+00:00",
        }
    ]
    monkeypatch.setattr(clusters_route.daily_digest, "list_digests", lambda start, end: rows)

    response = client.get(
        "/api/feed/daily-digest?start=2026-07-31&end=2026-08-01"
    )

    assert response.status_code == 200
    assert response.json() == {"digests": rows}


def test_daily_digest_api_accepts_exactly_31_days(daily_digest_client, monkeypatch):
    client, clusters_route = daily_digest_client
    monkeypatch.setattr(clusters_route.daily_digest, "list_digests", lambda start, end: [])

    response = client.get(
        "/api/feed/daily-digest?start=2026-07-01&end=2026-08-01"
    )

    assert response.status_code == 200
    assert response.json() == {"digests": []}


@pytest.fixture()
def digest_category_db(monkeypatch):
    """Execute the read SQL against an isolated relational fixture.

    Only PostgreSQL parameter/array syntax is adapted; joins, voting, priority,
    JSON extraction and window functions execute in SQLite (no remote DB).
    """
    import re
    import sqlite3

    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ':memory:' AS remote_poc")
    conn.create_function('split_part', 3, lambda s, sep, n: s.split(sep)[n - 1])
    conn.executescript('''
        CREATE TABLE remote_poc.daily_digest (
            digest_date TEXT, status TEXT, entries TEXT, updated_at TEXT);
        CREATE TABLE remote_poc.highlights_read_model_state (key TEXT, active_version_id TEXT);
        CREATE TABLE remote_poc.highlights_read_model_versions (version_id TEXT, status TEXT);
        CREATE TABLE remote_poc.highlights_scope_items (
            version_id TEXT, scope_key TEXT, cluster_id INTEGER, card_json TEXT);
        CREATE TABLE remote_poc.cluster_items (cluster_id INTEGER, item_id INTEGER);
        CREATE TABLE remote_poc.items (id INTEGER PRIMARY KEY, ai_category TEXT);
    ''')
    conn.execute('INSERT INTO remote_poc.highlights_read_model_versions VALUES (?, ?)', ('current', 'complete'))
    conn.execute('INSERT INTO remote_poc.highlights_read_model_state VALUES (?, ?)',
                 (daily_digest.remote_db.HIGHLIGHTS_READ_MODEL_STATE_KEY, 'current'))
    calls = []

    class ReadConnection:
        def execute(self, sql, params):
            assert sql.lstrip().upper().startswith(('SELECT', 'WITH')), 'digest reads must never write'
            calls.append((sql, params))
            sql = re.sub(r'=\s*ANY\(%\((\w+)\)s(?:::\w+\[\])?\)',
                         r'IN (SELECT value FROM json_each(:\1))', sql)
            sql = re.sub(r'%\((\w+)\)s', r':\1', sql)
            sql = sql.replace('%s', '?')
            if isinstance(params, dict):
                params = {key: json.dumps(value) if isinstance(value, list) else value
                          for key, value in params.items()}
            else:
                params = tuple(value.isoformat() if isinstance(value, date) else value for value in params)
            return conn.execute(sql, params)

    @contextmanager
    def connect():
        yield ReadConnection()

    monkeypatch.setattr(daily_digest.remote_db, 'connect', connect)
    monkeypatch.setattr(daily_digest.remote_db, 'remote_schema', lambda: 'remote_poc')

    def save(day, entries):
        conn.execute('INSERT INTO remote_poc.daily_digest VALUES (?, ?, ?, ?)',
                     (day, 'final', json.dumps(entries), f'{day}T10:00:00Z'))

    def snapshot(cluster_id, category, *, version='current', scope='all'):
        conn.execute('INSERT INTO remote_poc.highlights_scope_items VALUES (?, ?, ?, ?)',
                     (version, scope, cluster_id, json.dumps({'category': category})))

    def members(cluster_id, categories):
        for category in categories:
            cursor = conn.execute('INSERT INTO remote_poc.items (ai_category) VALUES (?)', (category,))
            conn.execute('INSERT INTO remote_poc.cluster_items VALUES (?, ?)', (cluster_id, cursor.lastrowid))

    yield conn, save, snapshot, members, calls
    conn.close()


def test_digest_categories_batch_current_projection_then_historical_members(digest_category_db):
    conn, save, snapshot, members, calls = digest_category_db
    original = [{**_previous_entry(i), 'rank': rank} for rank, i in enumerate(range(101, 108), 1)]
    save('2026-08-01', original)
    save('2026-07-01', [_previous_entry(101), _previous_entry(103)])
    snapshot(101, 'products')
    members(101, ['models'] * 3)  # Current projection wins over changed members.
    snapshot(102, None)
    members(102, ['products'])  # A known null in the projection must remain null.
    members(103, ['coding', 'products'])  # Tie follows highlights ACTIVE_CATEGORY_IDS priority.
    members(104, ['tools[/automation]', 'ai_tools', 'coding'])
    members(105, ['other'] * 5 + ['obsolete', 'insights[/research]'])
    members(106, ['products', 'coding', 'coding'])
    snapshot(106, 'models', version='old')
    snapshot(106, 'products', scope='category:products')
    # 107 has no surviving members and remains readable only in All.

    result = daily_digest.list_digests(date(2026, 7, 1), date(2026, 8, 1))

    assert [row['date'] for row in result] == ['2026-08-01', '2026-07-01']
    assert [entry['category'] for entry in result[0]['entries']] == [
        'products', None, 'products', 'efficiency_tools', 'tech', 'coding', None]
    assert [entry['category'] for entry in result[1]['entries']] == ['products', 'products']
    assert [{k: v for k, v in entry.items() if k != 'category'} for entry in result[0]['entries']] == original
    assert json.loads(conn.execute("SELECT entries FROM remote_poc.daily_digest WHERE digest_date='2026-08-01'").fetchone()[0]) == original
    assert len(calls) == 2  # One date-range read, one batch, regardless of days/entries.
    assert sorted(calls[1][1]['cluster_ids']) == list(range(101, 108))


@pytest.mark.parametrize('state', ['missing', 'building'])
def test_digest_categories_without_complete_projection_use_original_members(digest_category_db, state):
    conn, save, snapshot, members, _calls = digest_category_db
    save('2026-07-01', [_previous_entry(101)])
    snapshot(101, 'models')
    members(101, ['coding'])
    if state == 'missing':
        conn.execute('DELETE FROM remote_poc.highlights_read_model_state')
    else:
        conn.execute("UPDATE remote_poc.highlights_read_model_versions SET status='building'")
    assert daily_digest.list_digests(date(2026, 7, 1), date(2026, 7, 1))[0]['entries'][0]['category'] == 'coding'


def test_digest_categories_cover_every_visible_category(digest_category_db):
    from category_taxonomy import ACTIVE_CATEGORY_IDS
    _conn, save, _snapshot, members, _calls = digest_category_db
    categories = [c for c in ACTIVE_CATEGORY_IDS if c != 'other']
    for i, category in enumerate(categories, 1):
        members(i, [category, 'other'])
    save('2026-08-01', [_previous_entry(i) for i in range(1, len(categories) + 1)])
    result = daily_digest.list_digests(date(2026, 8, 1), date(2026, 8, 1))
    assert [entry['category'] for entry in result[0]['entries']] == categories


def test_digest_categories_empty_range_and_empty_entries_skip_category_query(digest_category_db):
    _conn, save, _snapshot, _members, calls = digest_category_db
    assert daily_digest.list_digests(date(2026, 8, 1), date(2026, 8, 1)) == []
    assert len(calls) == 1
    calls.clear()
    save('2026-08-01', [])
    assert daily_digest.list_digests(date(2026, 8, 1), date(2026, 8, 1))[0]['entries'] == []
    assert len(calls) == 1


def test_digest_api_enriches_response_without_mutating_final_snapshot(daily_digest_client, digest_category_db):
    client, _route = daily_digest_client
    _conn, save, _snapshot, members, _calls = digest_category_db
    save('2026-08-01', [_previous_entry(101), _previous_entry(102)])
    members(101, ['models'])
    response = client.get('/api/feed/daily-digest?start=2026-08-01&end=2026-08-01')
    assert response.status_code == 200
    assert [entry['category'] for entry in response.json()['digests'][0]['entries']] == ['models', None]


def test_digest_api_category_query_failure_is_503(daily_digest_client, monkeypatch):
    client, _route = daily_digest_client

    class Conn:
        def execute(self, sql, params):
            if 'FROM remote_poc.daily_digest' in sql:
                class Cursor:
                    def fetchall(self):
                        return [{'digest_date': '2026-08-01', 'status': 'final',
                                 'entries': [_previous_entry(101)], 'updated_at': '2026-08-01T10:00:00Z'}]
                return Cursor()
            raise RuntimeError('category query unavailable')

    @contextmanager
    def connect():
        yield Conn()
    monkeypatch.setattr(daily_digest.remote_db, 'connect', connect)
    monkeypatch.setattr(daily_digest.remote_db, 'remote_schema', lambda: 'remote_poc')
    response = client.get('/api/feed/daily-digest?start=2026-08-01&end=2026-08-01')
    assert response.status_code == 503
    assert 'digests' not in response.json()
