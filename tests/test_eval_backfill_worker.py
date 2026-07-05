from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

import eval_backfill_worker as worker  # noqa: E402


def test_iter_backward_windows_builds_oldest_cursor_from_checkpoint():
    cursor = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)

    windows = list(worker.iter_backward_windows(cursor, window_days=7, max_windows=3))

    assert [window.as_json() for window in windows] == [
        {"start": "2026-05-18T00:00:00Z", "end": "2026-05-25T00:00:00Z"},
        {"start": "2026-05-11T00:00:00Z", "end": "2026-05-18T00:00:00Z"},
        {"start": "2026-05-04T00:00:00Z", "end": "2026-05-11T00:00:00Z"},
    ]


def test_checkpoint_roundtrip_uses_next_window_start(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    window = worker.EvalBackfillWindow(
        start=datetime(2026, 5, 18, tzinfo=timezone.utc),
        end=datetime(2026, 5, 25, tzinfo=timezone.utc),
    )

    worker.save_checkpoint(
        checkpoint,
        next_cursor_until=window.start,
        window=window,
        payload={
            "output_file": "/tmp/window.json",
            "candidate_count": 12,
            "llm_checked": 10,
            "eval_hits": 3,
        },
    )

    assert worker.load_checkpoint(checkpoint, datetime(2026, 6, 1, tzinfo=timezone.utc)) == window.start


def test_output_path_for_window_is_stable(tmp_path):
    window = worker.EvalBackfillWindow(
        start=datetime(2026, 5, 18, 6, tzinfo=timezone.utc),
        end=datetime(2026, 5, 25, 6, tzinfo=timezone.utc),
    )

    assert worker.output_path_for_window(tmp_path, window) == (
        tmp_path / "20260518T060000Z__20260525T060000Z.json"
    )


def test_normalize_args_caps_worker_budget():
    args = types.SimpleNamespace(
        window_days=99,
        max_windows=99,
        candidate_limit=999,
        scan_limit=1,
        max_llm=999,
        max_tokens=9999,
        request_interval_sec=0.1,
        db_statement_timeout_sec=1,
        sleep_between_windows_sec=99999.0,
        info_refresh_timeout_sec=999,
        offline_limit=99999,
        classification_concurrency=99,
        list_only=False,
    )

    normalized = worker.normalize_args(args)

    assert normalized.window_days == 30
    assert normalized.max_windows == 24
    assert normalized.candidate_limit == 200
    assert normalized.scan_limit == 200
    assert normalized.max_llm == 200
    assert normalized.max_tokens == 2000
    assert normalized.request_interval_sec == 0.8
    assert normalized.db_statement_timeout_sec == 5
    assert normalized.sleep_between_windows_sec == 3600.0
    assert normalized.info_refresh_timeout_sec == 300
    assert normalized.offline_limit == 20000
    assert normalized.classification_concurrency == 20


def test_write_window_output_preserves_full_candidate_count(tmp_path):
    window = worker.EvalBackfillWindow(
        start=datetime(2026, 5, 18, tzinfo=timezone.utc),
        end=datetime(2026, 5, 25, tzinfo=timezone.utc),
    )
    args = types.SimpleNamespace(list_only=True)

    payload = worker.write_window_output(
        tmp_path / "window.json",
        window=window,
        args=args,
        candidate_count=40,
        outputs=[{"id": "a", "categories": ["eval"], "confidence": "medium"}],
    )

    assert payload["candidate_count"] == 40
    assert payload["output_item_count"] == 1
    assert payload["eval_hits"] == 1
    assert payload["apply"]["enabled"] is False


def test_select_apply_outputs_requires_eval_and_minimum_confidence():
    outputs = [
        {"id": "a", "categories": ["eval"], "confidence": "high"},
        {"id": "b", "categories": ["eval"], "confidence": "medium"},
        {"id": "c", "categories": ["models"], "confidence": "high"},
        {"id": "d", "categories": ["eval"], "confidence": "low"},
    ]

    selected = worker.select_apply_outputs(outputs, "medium")

    assert [item["id"] for item in selected] == ["a", "b"]


def test_normalized_eval_categories_puts_eval_first():
    item = {"categories": ["models", "eval", "eval"]}

    assert worker._normalized_eval_categories(item) == ["eval", "models"]


def test_write_window_output_records_apply_result(tmp_path):
    window = worker.EvalBackfillWindow(
        start=datetime(2026, 5, 18, tzinfo=timezone.utc),
        end=datetime(2026, 5, 25, tzinfo=timezone.utc),
    )
    args = types.SimpleNamespace(list_only=False, apply=True, min_confidence="high")

    payload = worker.write_window_output(
        tmp_path / "window.json",
        window=window,
        args=args,
        candidate_count=1,
        outputs=[{"id": "a", "categories": ["eval"], "confidence": "high"}],
        apply_result={"applied_count": 1, "applied_ids": ["a"]},
        info_refresh_result={"ok": True, "eval_total": 33},
    )

    assert payload["apply"]["enabled"] is True
    assert payload["apply"]["applied_count"] == 1
    assert payload["info_read_model_refresh"]["eval_total"] == 33


def test_build_apply_manifest_filters_existing_eval_by_default():
    outputs = [
        {
            "id": "new",
            "categories": ["eval"],
            "confidence": "high",
            "existing": {"ai_category": "models", "ai_categories": ["models"]},
        },
        {
            "id": "old",
            "categories": ["eval"],
            "confidence": "high",
            "existing": {"ai_category": "eval", "ai_categories": ["eval"]},
        },
        {
            "id": "weak",
            "categories": ["eval"],
            "confidence": "medium",
            "existing": {"ai_category": "models", "ai_categories": ["models"]},
        },
    ]

    manifest = worker.build_apply_manifest(outputs, minimum_confidence="high")

    assert manifest["classified_count"] == 3
    assert manifest["eval_hits"] == 3
    assert manifest["apply_count"] == 1
    assert [item["id"] for item in manifest["items"]] == ["new"]


def test_jsonl_roundtrip_preserves_rows(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]

    worker._write_jsonl(path, rows, append=False)

    assert worker._read_jsonl(path) == rows


def test_run_classify_offline_mode_supports_parallel_classification(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.jsonl"
    classified = tmp_path / "classified.jsonl"
    manifest = tmp_path / "manifest.json"
    rows = [
        {"id": "a", "title": "A", "content": "benchmark"},
        {"id": "b", "title": "B", "content": "eval"},
        {"id": "c", "title": "C", "content": "judge"},
    ]
    worker._write_jsonl(snapshot, rows, append=False)

    monkeypatch.setattr(
        worker,
        "_load_eval_classifier",
        lambda: ([], {"eval"}, {"model_eval"}, "key", "base", "model", "prompt"),
    )

    def fake_call_minimax(*_args, **_kwargs):
        payload = json.loads(_args[4])
        return json.dumps(
            {
                "categories": ["eval"],
                "subcategories": ["model_eval"],
                "confidence": "high",
                "reason": payload["id"],
            }
        )

    monkeypatch.setattr(worker.enrich_items, "call_minimax", fake_call_minimax)

    args = types.SimpleNamespace(
        snapshot_file=str(snapshot),
        classification_file=str(classified),
        manifest_file=str(manifest),
        resume_classification=False,
        append_output=False,
        offline_limit=0,
        request_interval_sec=0,
        classification_concurrency=3,
        max_tokens=200,
        min_confidence="high",
        include_existing_eval=False,
    )

    assert worker.run_classify_offline_mode(args) == 0
    outputs = worker._read_jsonl(classified)

    assert sorted(row["id"] for row in outputs) == ["a", "b", "c"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["apply_count"] == 3
