from datetime import date
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v27_why_read_backfill.py"


def test_v27_why_read_backfill_script_exists():
    assert SCRIPT.exists()


def _load_module():
    spec = importlib.util.spec_from_file_location("v27_why_read_backfill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_module()


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows_by_day=None):
        self.rows_by_day = rows_by_day or {}
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        return FakeCursor(self.rows_by_day.get(params["day_start"], []))


class FakeContext:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_args):
        return False


def _candidate(cluster_id, *, event_at, score=7.5):
    return {
        "id": cluster_id,
        "ai_title": f"cluster-{cluster_id}",
        "event_at": event_at,
        "max_flag_score10": score,
    }


def test_parse_args_defaults_and_configurable_sleep():
    defaults = backfill.parse_args([])

    assert defaults.days == 1
    assert defaults.threshold == 7.0
    assert defaults.limit == 200
    assert defaults.sleep_seconds == 1.0
    assert defaults.dry_run is False
    assert defaults.force is False

    overridden = backfill.parse_args(["--sleep-seconds", "2.5", "--dry-run", "--force"])
    assert overridden.sleep_seconds == 2.5
    assert overridden.dry_run is True
    assert overridden.force is True


def test_iter_days_is_reverse_chronological():
    assert backfill.iter_days(3, today=date(2026, 7, 15)) == [
        date(2026, 7, 15),
        date(2026, 7, 14),
        date(2026, 7, 13),
    ]


@pytest.mark.parametrize(
    ("force", "expects_missing_why_read_filter"),
    [(False, True), (True, False)],
)
def test_fetch_day_candidates_scopes_why_read_filter_by_force(
    force,
    expects_missing_why_read_filter,
):
    target_day = date(2026, 7, 15)
    conn = FakeConn({
        target_day: [_candidate(301, event_at="2026-07-15T09:00:00Z")],
    })

    rows = backfill.fetch_day_candidates(
        conn,
        day=target_day,
        threshold=7.0,
        limit=20,
        schema="remote_poc",
        force=force,
    )

    assert [row["id"] for row in rows] == [301]
    query, params = conn.calls[0]
    normalized = " ".join(query.split())
    assert "JOIN remote_poc.highlight_cluster_decisions d ON d.cluster_id = c.id" in normalized
    assert "c.is_visible_in_feed IS TRUE" in normalized
    assert ("c.why_read IS NULL" in normalized) is expects_missing_why_read_filter
    assert "(d.score_inputs->>'max_flag_score10')::float >= %(threshold)s" in normalized
    assert "ORDER BY event_at DESC, c.id DESC" in normalized
    assert params == {
        "day_start": target_day,
        "threshold": 7.0,
        "limit": 20,
    }


def test_dry_run_walks_days_in_reverse_and_never_calls_llm(monkeypatch, capsys):
    today = date(2026, 7, 15)
    yesterday = date(2026, 7, 14)
    conn = FakeConn({
        today: [
            _candidate(303, event_at="2026-07-15T09:00:00Z"),
            _candidate(302, event_at="2026-07-15T08:00:00Z"),
        ],
        yesterday: [_candidate(301, event_at="2026-07-14T23:00:00Z")],
    })
    monkeypatch.setattr(backfill, "configure_environment", lambda: None)
    monkeypatch.setattr(backfill, "connect_database", lambda: FakeContext(conn))
    monkeypatch.setattr(
        backfill,
        "resolve_summary_runtime",
        lambda: pytest.fail("dry-run must not resolve LLM runtime"),
    )
    monkeypatch.setattr(
        backfill.summary_writer,
        "regenerate_and_swap",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not call LLM summary chain"),
    )
    args = SimpleNamespace(
        days=2,
        threshold=7.0,
        limit=3,
        sleep_seconds=0.0,
        dry_run=True,
        force=True,
    )

    assert backfill.run(args, today=today) == 0

    assert [params["day_start"] for _query, params in conn.calls] == [today, yesterday]
    assert all("c.why_read IS NULL" not in query for query, _params in conn.calls)
    output = capsys.readouterr().out
    assert "day=2026-07-15 candidates=2 selected_total=2/3" in output
    assert "day=2026-07-14 candidates=1 selected_total=3/3" in output
    assert output.index('"id": 303') < output.index('"id": 301')
    assert "dry-run: no LLM calls or database writes" in output


def test_write_mode_reuses_summary_writer_entrypoint_and_sleeps_between_clusters(
    monkeypatch,
):
    today = date(2026, 7, 15)
    conn = FakeConn({
        today: [
            _candidate(303, event_at="2026-07-15T09:00:00Z"),
            _candidate(302, event_at="2026-07-15T08:00:00Z"),
        ],
    })
    calls = []
    sleeps = []
    monkeypatch.setattr(backfill, "configure_environment", lambda: None)
    monkeypatch.setattr(backfill, "connect_database", lambda: FakeContext(conn))
    monkeypatch.setattr(backfill.remote_db, "cluster_to_remote", lambda: True)
    monkeypatch.setattr(
        backfill,
        "resolve_summary_runtime",
        lambda: ("api-key", "https://example.test", "model", 20),
    )
    monkeypatch.setattr(
        backfill.summary_writer,
        "regenerate_and_swap",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(backfill.time, "sleep", lambda seconds: sleeps.append(seconds))
    args = SimpleNamespace(
        days=1,
        threshold=7.0,
        limit=200,
        sleep_seconds=2.5,
        dry_run=False,
        force=False,
    )

    assert backfill.run(args, today=today) == 0

    assert [call[0][1] for call in calls] == [303, 302]
    assert all(call[1]["publish_immediately"] is True for call in calls)
    assert all(call[1]["api_key"] == "api-key" for call in calls)
    assert sleeps == [2.5]
