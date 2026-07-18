from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "highlights_golden_replay.py"


def _module():
    assert SCRIPT.exists(), "Wave 3 replay script is missing"
    spec = importlib.util.spec_from_file_location("highlights_golden_replay_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(
    entity_id: str = "item-1",
    *,
    feedback_kind: str = "should_feature",
    note: str = "这条应该进精选",
) -> dict[str, object]:
    return {
        "entity_type": "item",
        "entity_id": entity_id,
        "item_id": entity_id,
        "cluster_id": 17,
        "title": "一个值得保留的案例",
        "content": "正文" * 400,
        "ai_summary": "摘要",
        "source": "可信作者",
        "platform": "x",
        "url": "https://example.com/item",
        "published_at": "2026-07-15T08:00:00Z",
        "cluster_title": "案例簇",
        "highlight_verdict": "drop",
        "highlight_scores": {
            "v26": {
                "score10": 5.8,
                "authority": 2,
                "substance": 3,
                "novelty": 2,
                "timeliness": 2,
                "audience_fit": 3,
                "veto": "none",
            }
        },
        "highlight_uncertainty": "none",
        "highlight_prompt_version": "item_score_v26_7",
        "cluster_decision": "excluded",
        "cluster_verdict": "drop",
        "max_flag_score10": 5.8,
        "is_visible_in_feed": True,
        "why_read": None,
        "feedback_kind": feedback_kind,
        "feedback_note": note,
        "feedback_at": "2026-07-16T09:30:00Z",
    }


@pytest.mark.parametrize(
    ("feedback_kind", "control", "expected"),
    [
        ("should_feature", False, ("miss", {"include_in_highlights": True})),
        ("irrelevant", False, ("false_positive", {"include_in_highlights": False})),
        ("low_quality", False, ("false_positive", {"include_in_highlights": False})),
        ("should_drop", False, ("false_positive", {"include_in_highlights": False})),
        ("clicked", True, ("control", {"include_in_highlights": True})),
    ],
)
def test_derive_expectation(feedback_kind, control, expected):
    replay = _module()

    assert replay.derive_expectation(feedback_kind, control=control) == expected


def test_build_case_captures_bounded_content_and_pipeline_snapshot():
    replay = _module()

    case = replay.build_case(_row())

    assert case["case_id"] == "fb-item-item-1"
    assert case["kind"] == "miss"
    assert len(case["content_snapshot"]["excerpt"]) == 500
    assert case["content_snapshot"]["source"] == "可信作者"
    assert case["pipeline_snapshot"] == {
        "verdict": "drop",
        "score10": 5.8,
        "dims": {
            "authority": 2,
            "substance": 3,
            "novelty": 2,
            "timeliness": 2,
            "audience_fit": 3,
        },
        "veto": "none",
        "uncertainty": "none",
        "stage": "scoring",
        "cluster_id": 17,
        "cluster_title": "案例簇",
        "cluster_decision": "excluded",
        "cluster_verdict": "drop",
        "prompt_version": "item_score_v26_7",
    }
    assert case["user_judgment"] == {
        "kind": "should_feature",
        "note": "这条应该进精选",
        "at": "2026-07-16T09:30:00Z",
    }
    assert case["expected"] == {"include_in_highlights": True}
    assert case["snapshot_partial"] is False


def test_merge_cases_is_incremental_idempotent_and_preserves_judgment():
    replay = _module()
    existing = replay.build_case(_row(note="第一次同步的原文"))
    changed = replay.build_case(_row(note="远端后来变化"))
    added = replay.build_case(_row("item-2"))

    merged = replay.merge_cases([existing], [changed, added])

    assert [case["case_id"] for case in merged] == [
        "fb-item-item-1",
        "fb-item-item-2",
    ]
    assert merged[0]["user_judgment"]["note"] == "第一次同步的原文"
    assert replay.merge_cases(merged, [changed, added]) == merged


def _case(case_id: str, kind: str, expected: bool) -> dict[str, object]:
    return {
        "case_id": case_id,
        "kind": kind,
        "content_snapshot": {
            "item_id": case_id,
            "title": f"title-{case_id}",
            "excerpt": f"excerpt-{case_id}",
            "source": "source",
            "platform": "x",
            "url": "https://example.com",
            "published_at": "2026-07-15T00:00:00Z",
        },
        "expected": {"include_in_highlights": expected},
    }


def _control_row(cluster_id: int) -> dict[str, object]:
    row = _row(f"rep-{cluster_id}")
    row.update({
        "entity_type": "cluster",
        "entity_id": cluster_id,
        "cluster_id": cluster_id,
        "feedback_kind": "clicked",
        "feedback_note": None,
        "feedback_at": "2026-07-16T10:00:00Z",
        "cluster_decision": "included",
        "cluster_verdict": "featured",
        "why_read": "值得阅读",
    })
    return row


def test_replay_continues_after_one_case_errors():
    replay = _module()
    cases = [
        _case("miss-1", "miss", True),
        _case("fp-1", "false_positive", False),
        _case("control-1", "control", True),
    ]
    seen = []

    def scorer(item):
        seen.append(item["id"])
        if item["id"] == "control-1":
            raise RuntimeError("LLM | timeout")
        return {
            "highlight_include_in_highlights": item["id"] == "miss-1",
            "highlight_verdict": "featured" if item["id"] == "miss-1" else "drop",
            "score10": 7.1 if item["id"] == "miss-1" else 3.2,
        }

    results = replay.replay_cases(cases, scorer=scorer)

    assert seen == ["miss-1", "fp-1", "control-1"]
    assert [result["status"] for result in results] == ["pass", "pass", "error"]
    assert results[2]["error"] == "LLM | timeout"


def test_render_report_has_three_rates_and_per_case_details():
    replay = _module()
    results = [
        {
            "case_id": "miss-1",
            "kind": "miss",
            "title": "漏放案例",
            "expected_include": True,
            "predicted_include": True,
            "status": "pass",
            "verdict": "featured",
            "score10": 7.1,
            "error": None,
        },
        {
            "case_id": "fp-1",
            "kind": "false_positive",
            "title": "误放案例",
            "expected_include": False,
            "predicted_include": True,
            "status": "fail",
            "verdict": "featured",
            "score10": 7.5,
            "error": None,
        },
        {
            "case_id": "control-1",
            "kind": "control",
            "title": "对照案例",
            "expected_include": True,
            "predicted_include": None,
            "status": "error",
            "verdict": None,
            "score10": None,
            "error": "LLM | timeout",
        },
    ]

    report = replay.render_report(results, report_date="2026-07-16")

    assert "# 精选金标回放 · 2026-07-16" in report
    assert "漏放修正率：1/1（100.0%，error 0）" in report
    assert "误放清除率：0/1（0.0%，error 0）" in report
    assert "对照保持率：0/1（0.0%，error 1）" in report
    assert "| case_id | kind | title | expected | predicted | result | verdict | score10 | error |" in report
    assert "漏放案例" in report
    assert "LLM \\| timeout" in report


def test_sync_dry_run_merges_without_writing(tmp_path):
    replay = _module()
    golden = tmp_path / "golden.jsonl"
    existing = replay.build_case(_row())
    original = json.dumps(existing, ensure_ascii=False) + "\n"
    golden.write_text(original, encoding="utf-8")

    summary = replay.run_sync(
        golden_path=golden,
        dry_run=True,
        fetcher=lambda: ([_row("item-2")], []),
    )

    assert summary == {
        "existing": 1,
        "feedback": 2,
        "controls": 0,
        "added": 1,
        "total": 2,
        "dry_run": True,
    }
    assert golden.read_text(encoding="utf-8") == original


def test_sync_samples_controls_to_feedback_scale_without_growth(tmp_path):
    replay = _module()
    golden = tmp_path / "golden.jsonl"

    def fetcher():
        return (
            [_row("item-1"), _row("item-2")],
            [_control_row(31), _control_row(32), _control_row(33)],
        )

    first = replay.run_sync(golden_path=golden, fetcher=fetcher)
    first_payload = golden.read_text(encoding="utf-8")
    second = replay.run_sync(golden_path=golden, fetcher=fetcher)

    assert first == {
        "existing": 0,
        "feedback": 2,
        "controls": 2,
        "added": 4,
        "total": 4,
        "dry_run": False,
    }
    assert second["feedback"] == 2
    assert second["controls"] == 2
    assert second["added"] == 0
    assert golden.read_text(encoding="utf-8") == first_payload


def test_remote_sync_reads_both_item_actions_and_excludes_override_controls():
    replay = _module()
    source = Path(replay.__file__).read_text()

    assert "fb.action IN ('should_feature', 'should_drop')" in source
    assert "d.manual_display IS NULL" in source
    assert "fb.action IN ('should_feature', 'should_drop')" in source[source.index("AND NOT EXISTS ("):]


def test_replay_dry_run_skips_scorer_and_report_write(tmp_path):
    replay = _module()
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps(_case("miss-1", "miss", True), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"

    result = replay.run_replay(
        golden_path=golden,
        report_dir=report_dir,
        dry_run=True,
        scorer_factory=lambda: (_ for _ in ()).throw(
            AssertionError("dry-run must not initialize the LLM scorer")
        ),
        report_date="2026-07-16",
    )

    assert result == {"cases": 1, "dry_run": True, "report_path": None}
    assert not report_dir.exists()


def test_replay_main_returns_nonzero_when_every_case_errors(monkeypatch, tmp_path):
    replay = _module()
    report = tmp_path / "replay.md"
    monkeypatch.setattr(
        replay,
        "run_replay",
        lambda *, dry_run: {
            "cases": 3,
            "dry_run": False,
            "report_path": report,
            "errors": 3,
        },
    )

    assert replay.main(["replay"]) == 1
