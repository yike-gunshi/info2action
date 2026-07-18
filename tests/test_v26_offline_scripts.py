from __future__ import annotations

import importlib
from datetime import date
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _offline_module():
    return importlib.import_module("v26_offline_rescore")


def _backfill_module():
    return importlib.import_module("v26_backfill")


def _run_result(score10: float, **overrides):
    result = {
        "score10": score10,
        "reject": False,
        "content_type": "tutorial_method",
        "content_type_confidence": 0.9,
        "dims": {
            "authority": 2,
            "substance": 3,
            "novelty": 2,
            "timeliness": 1,
            "audience_fit": 3,
        },
        "marketing": 0,
        "veto": "none",
        "uncertainty": "none",
        "value_path": "substantive",
        "reason": "可复用的完整教程",
        "confidence": 0.85,
    }
    result.update(overrides)
    return result


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


def test_fetch_query_keeps_existing_days_filter_unchanged():
    offline = _offline_module()

    query, params = offline.build_fetch_query(
        days=3,
        start_date=None,
        end_date=None,
        skip_scored=False,
        limit=50,
    )

    normalized = _normalize_sql(query)
    assert "fetched_at > now() - interval '3 days'" in normalized
    assert "highlight_scores ? 'v26'" not in normalized
    assert params == (50,)


def test_fetch_query_supports_half_open_date_band_and_skip_scored():
    offline = _offline_module()
    start_date = date(2026, 6, 17)
    end_date = date(2026, 7, 1)

    query, params = offline.build_fetch_query(
        days=None,
        start_date=start_date,
        end_date=end_date,
        skip_scored=True,
        limit=5000,
    )

    normalized = _normalize_sql(query)
    assert "fetched_at >= %s::date" in normalized
    assert "fetched_at < %s::date" in normalized
    assert "AND NOT (highlight_scores ? 'v26')" in normalized
    assert params == (start_date, end_date, 5000)


def test_date_band_conflicts_with_days():
    offline = _offline_module()

    with pytest.raises(SystemExit) as exc_info:
        offline.parse_args([
            "--days",
            "3",
            "--start-date",
            "2026-06-17",
            "--end-date",
            "2026-07-01",
        ])

    assert exc_info.value.code == 2


def test_date_band_requires_both_bounds_and_orders_them():
    offline = _offline_module()

    with pytest.raises(SystemExit):
        offline.parse_args(["--start-date", "2026-06-17"])
    with pytest.raises(SystemExit):
        offline.parse_args([
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-01",
        ])


@pytest.mark.parametrize(
    ("first_score", "second_score"),
    [(4.0, 6.0), (6.0, 4.0)],
)
def test_edge_band_reruns_at_inclusive_boundaries_and_averages(
    first_score,
    second_score,
):
    offline = _offline_module()
    results = iter([_run_result(first_score), _run_result(second_score)])
    calls = []

    def fake_scorer(item):
        calls.append(item["id"])
        return next(results)

    record = offline.rescore_item(
        {"id": "item-edge", "title": "边缘条目", "highlight_include_in_highlights": False},
        threshold=5.0,
        scorer=fake_scorer,
    )

    assert calls == ["item-edge", "item-edge"]
    assert record["runs"] == [first_score, second_score]
    assert record["score10"] == 5.0
    assert record["flag_bearer"] is True


@pytest.mark.parametrize("first_score", [3.9, 6.1])
def test_scores_outside_edge_band_do_not_rerun(first_score):
    offline = _offline_module()
    calls = []

    def fake_scorer(item):
        calls.append(item["id"])
        return _run_result(first_score)

    record = offline.rescore_item(
        {"id": "item-stable", "title": "稳定条目", "highlight_include_in_highlights": True},
        threshold=5.0,
        scorer=fake_scorer,
    )

    assert calls == ["item-stable"]
    assert record["runs"] == [first_score]
    assert record["score10"] == first_score


def test_rejected_result_with_no_score_is_recorded_without_retry():
    offline = _offline_module()
    calls = []

    def fake_scorer(item):
        calls.append(item["id"])
        return _run_result(None, reject=True)

    record = offline.rescore_item(
        {"id": "item-rejected", "highlight_include_in_highlights": False},
        threshold=5.0,
        scorer=fake_scorer,
    )

    assert calls == ["item-rejected"]
    assert record["runs"] == [None]
    assert record["score10"] is None
    assert record["flag_bearer"] is False
    assert record["error"] is None


def test_single_item_failure_retries_twice_then_records_error():
    offline = _offline_module()
    calls = []

    def fake_scorer(item):
        calls.append(item["id"])
        raise RuntimeError("temporary MiniMax failure")

    record = offline.rescore_item(
        {"id": "item-error", "highlight_include_in_highlights": True},
        threshold=5.0,
        scorer=fake_scorer,
    )

    assert calls == ["item-error", "item-error", "item-error"]
    assert record["score10"] is None
    assert record["old_include"] is True
    assert record["error"] == "temporary MiniMax failure"


def test_checkpoint_ids_skip_already_scored_items(tmp_path):
    offline = _offline_module()
    scores_file = tmp_path / "scores.jsonl"
    scores_file.write_text(
        '\n'.join([
            json.dumps({"item_id": "done-1", "score10": 7.0}),
            "not-json",
            json.dumps({"score10": 4.0}),
        ])
        + "\n",
        encoding="utf-8",
    )

    completed = offline.load_checkpoint_item_ids(scores_file)
    pending = offline.filter_pending_items(
        [{"id": "done-1"}, {"id": "todo-1"}],
        completed,
    )

    assert completed == {"done-1"}
    assert pending == [{"id": "todo-1"}]


def test_diff_classifies_newly_included_removed_and_unchanged():
    offline = _offline_module()
    records = [
        {"item_id": "new", "old_include": False, "flag_bearer": True, "error": None},
        {"item_id": "removed", "old_include": True, "flag_bearer": False, "error": None},
        {"item_id": "same-in", "old_include": True, "flag_bearer": True, "error": None},
        {"item_id": "same-out", "old_include": False, "flag_bearer": False, "error": None},
    ]

    classified = offline.classify_diff(records)

    assert [row["item_id"] for row in classified["newly_included"]] == ["new"]
    assert [row["item_id"] for row in classified["removed"]] == ["removed"]
    assert [row["item_id"] for row in classified["unchanged"]] == ["same-in", "same-out"]


class _FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    def commit(self):
        self.calls.append(("commit", None))


def test_backfill_uses_nested_v26_sql_params_without_top_level_pollution(monkeypatch):
    backfill = _backfill_module()
    monkeypatch.setattr(backfill.remote_db, "_maybe_jsonb", lambda value: value)
    conn = _FakeConn()
    record = {
        "item_id": "item-1",
        "score10": 7.2,
        "dims": {
            "authority": 2,
            "substance": 3,
            "novelty": 2,
            "timeliness": 1,
            "audience_fit": 3,
        },
        "marketing": 0,
        "veto": "none",
        "uncertainty": "none",
        "value_path": "substantive",
        "content_type": "tutorial_method",
        "reject": False,
        "reason": "可复用的完整教程",
        "confidence": 0.85,
        "flag_bearer": True,
        "error": None,
    }

    backfill.apply_record(conn, record, threshold=4.5)

    sql, params = conn.calls[0]
    nested_v26 = params[0]
    assert "jsonb_build_object('v26', %s::jsonb)" in sql
    assert "jsonb_build_object('importance'" not in sql
    assert "jsonb_build_object('substance'" not in sql
    assert "jsonb_build_object('novelty'" not in sql
    assert nested_v26["substance"] == 3
    assert nested_v26["novelty"] == 2
    assert "importance" not in nested_v26
    assert params[1] is True
    assert params[2] == "featured"
    assert conn.calls[-1] == ("commit", None)
