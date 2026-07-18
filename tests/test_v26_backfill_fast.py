import importlib.util
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v26_backfill_fast.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("v26_backfill_fast", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_module()


def _record(item_id: str, **overrides):
    record = {
        "item_id": item_id,
        "score10": 6.0,
        "runs": [6.0],
        "dims": {
            "authority": 1,
            "substance": 2,
            "novelty": 3,
            "timeliness": 2,
            "audience_fit": 1,
        },
        "marketing": 0,
        "veto": "none",
        "uncertainty": "none",
        "value_path": "substantive",
        "content_type": "tutorial_method",
        "reject": False,
        "reason": "useful",
        "confidence": 0.9,
        "flag_bearer": True,
        "error": None,
    }
    record.update(overrides)
    return record


def test_fast_backfill_script_exists():
    assert SCRIPT.exists()


def test_intersect_records_keeps_only_target_item_ids():
    records = [_record("a"), _record("b"), _record("c")]

    selected = backfill.intersect_records(records, {"b", "c", "missing"})

    assert [row["item_id"] for row in selected] == ["b", "c"]


def test_iter_batches_splits_without_losing_rows():
    rows = list(range(5))

    assert list(backfill.iter_batches(rows, 2)) == [[0, 1], [2, 3], [4]]


def test_target_query_is_scoped_to_recent_visible_unmerged_active_clusters():
    class Cursor:
        def fetchall(self):
            return [("a",), ("a",), ("b",)]

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params):
            self.calls.append((query.as_string(None), params))
            return Cursor()

    conn = Connection()

    item_ids = backfill.fetch_target_item_ids(conn, days=1, schema="remote_poc")

    assert item_ids == {"a", "b"}
    query, params = conn.calls[0]
    assert 'FROM "remote_poc"."clusters" AS c' in query
    assert 'JOIN "remote_poc"."cluster_items" AS ci' in query
    assert "c.is_visible_in_feed = true" in query
    assert "c.merged_into IS NULL" in query
    assert "(c.archived IS NULL OR c.archived = false)" in query
    assert "c.last_updated_at > now() - (%s * interval '1 day')" in query
    assert params == (1,)


def test_target_query_supports_half_open_cluster_last_updated_date_band():
    class Cursor:
        def fetchall(self):
            return [("a",), ("b",)]

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params):
            self.calls.append((query.as_string(None), params))
            return Cursor()

    conn = Connection()
    start_date = date(2026, 6, 17)
    end_date = date(2026, 7, 1)

    item_ids = backfill.fetch_target_item_ids(
        conn,
        days=None,
        start_date=start_date,
        end_date=end_date,
        schema="remote_poc",
    )

    assert item_ids == {"a", "b"}
    query, params = conn.calls[0]
    assert "c.last_updated_at >= %s::date" in query
    assert "c.last_updated_at < %s::date" in query
    assert "now() -" not in query
    assert params == (start_date, end_date)


def test_date_band_conflicts_with_days_and_requires_both_bounds():
    with pytest.raises(SystemExit) as conflict:
        backfill.parse_args([
            "--days",
            "3",
            "--start-date",
            "2026-06-17",
            "--end-date",
            "2026-07-01",
        ])
    with pytest.raises(SystemExit):
        backfill.parse_args(["--end-date", "2026-07-01"])

    assert conflict.value.code == 2


def test_resync_decisions_uses_only_clusters_linked_to_selected_items(monkeypatch):
    class Cursor:
        def fetchone(self):
            return (2,)

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
            self.calls.append((" ".join(rendered.split()), params))
            return Cursor()

    sync_calls = []
    monkeypatch.setattr(
        backfill.remote_db,
        "_sync_highlight_cluster_decisions",
        lambda conn, schema, **kwargs: sync_calls.append((conn, schema, kwargs)),
    )
    conn = Connection()

    affected = backfill.resync_affected_cluster_decisions(
        conn,
        item_ids={"item-a", "item-b"},
        schema="remote_poc",
    )

    assert affected == 2
    create_sql, create_params = conn.calls[0]
    assert "CREATE TEMP TABLE v26_backfill_decision_clusters" in create_sql
    assert 'FROM "remote_poc"."cluster_items" AS ci' in create_sql
    assert "ci.item_id::text = ANY(%s::text[])" in create_sql
    assert set(create_params[0]) == {"item-a", "item-b"}
    assert sync_calls == [(
        conn,
        "remote_poc",
        {
            "window_days": 365,
            "min_github_stars": backfill.remote_db.HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
            "delta_cluster_table": "pg_temp.v26_backfill_decision_clusters",
        },
    )]


def test_batch_update_uses_placeholders_and_nested_v26_without_top_level_pollution():
    records = [
        _record("featured", score10=4.75, flag_bearer=1),
        _record("borderline", score10=4.74, flag_bearer=False),
        _record("vetoed", score10=9.0, veto="marketing", flag_bearer=True),
        _record("rejected", score10=9.0, reject=True, flag_bearer=False),
    ]

    query, params = backfill.build_batch_update(records, threshold=4.75, schema="remote_poc")
    rendered = query.as_string(None)

    assert rendered.count("(%s, %s, %s, %s, %s, %s, %s, %s, %s)") == 4
    assert 'UPDATE "remote_poc"."items" AS i' in rendered
    assert "jsonb_build_object('v26', v.v26::jsonb)" in rendered
    assert "importance" not in rendered
    assert "SET substance" not in rendered
    assert "SET novelty" not in rendered
    assert len(params) == 36

    rows = [params[offset : offset + 9] for offset in range(0, len(params), 9)]
    assert [row[0] for row in rows] == ["featured", "borderline", "vetoed", "rejected"]
    assert [row[2] for row in rows] == [True, False, True, False]
    assert [row[3] for row in rows] == ["featured", "borderline", "drop", "drop"]
    assert all(row[8] == backfill.highlight_score_v26.PROMPT_VERSION for row in rows)

    nested = json.loads(rows[0][1])
    assert nested == {
        "authority": 1,
        "substance": 2,
        "novelty": 3,
        "timeliness": 2,
        "audience_fit": 1,
        "marketing": 0,
        "score10": 4.75,
        "content_type": "tutorial_method",
        "reject": False,
        "veto": "none",
        "runs": [6.0],
        "pass2_error": None,
    }


def test_run_refuses_writes_without_yes_before_connecting(tmp_path, monkeypatch, capsys):
    input_file = tmp_path / "scores.jsonl"
    input_file.write_text(json.dumps(_record("a")) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        backfill,
        "connect_database",
        lambda: pytest.fail("must not connect without --yes"),
    )
    args = SimpleNamespace(
        input=input_file,
        days=1,
        start_date=None,
        end_date=None,
        batch=200,
        threshold=4.75,
        dry_run=False,
        yes=False,
        resync_decisions=False,
    )

    assert backfill.run(args) == 2
    assert "without --yes" in capsys.readouterr().out


def test_dry_run_reads_targets_but_never_updates(tmp_path, monkeypatch, capsys):
    input_file = tmp_path / "scores.jsonl"
    input_file.write_text(
        "\n".join(json.dumps(_record(item_id)) for item_id in ("a", "b")) + "\n",
        encoding="utf-8",
    )

    class Connection:
        def __init__(self):
            self.commits = 0

        def commit(self):
            self.commits += 1

    class Context:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, *_args):
            return False

    conn = Connection()
    monkeypatch.setattr(backfill, "connect_database", lambda: Context(conn))
    monkeypatch.setattr(
        backfill,
        "fetch_target_item_ids",
        lambda _conn, *, days, start_date, end_date, schema: {"b"},
    )
    monkeypatch.setattr(
        backfill,
        "write_batch",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not update"),
    )
    monkeypatch.setattr(backfill.remote_db, "remote_schema", lambda: "remote_poc")
    args = SimpleNamespace(
        input=input_file,
        days=1,
        start_date=None,
        end_date=None,
        batch=200,
        threshold=4.75,
        dry_run=True,
        yes=False,
        resync_decisions=False,
    )

    assert backfill.run(args) == 0
    output = capsys.readouterr().out
    assert "target_rows=1" in output
    assert '"item_id": "b"' in output
    assert "dry-run: no database writes" in output
    assert conn.commits == 0


def test_run_resyncs_decisions_after_applying_selected_scores(
    tmp_path,
    monkeypatch,
):
    input_file = tmp_path / "scores.jsonl"
    input_file.write_text(json.dumps(_record("a")) + "\n", encoding="utf-8")
    events = []

    class Connection:
        def commit(self):
            events.append("commit")

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(backfill, "connect_database", Context)
    monkeypatch.setattr(backfill.remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(
        backfill,
        "fetch_target_item_ids",
        lambda *_args, **_kwargs: {"a"},
    )
    monkeypatch.setattr(
        backfill,
        "write_batch",
        lambda *_args, **_kwargs: events.append("write") or 1,
    )
    monkeypatch.setattr(
        backfill,
        "resync_affected_cluster_decisions",
        lambda _conn, *, item_ids, schema: events.append(
            ("resync", item_ids, schema)
        ) or 1,
    )
    args = SimpleNamespace(
        input=input_file,
        days=None,
        start_date=date(2026, 6, 17),
        end_date=date(2026, 7, 1),
        batch=200,
        threshold=4.75,
        dry_run=False,
        yes=True,
        resync_decisions=True,
    )

    assert backfill.run(args) == 0
    assert events == [
        "write",
        "commit",
        ("resync", {"a"}, "remote_poc"),
        "commit",
    ]
