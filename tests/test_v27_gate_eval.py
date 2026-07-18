from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _module():
    return importlib.import_module("v27_gate_eval")


def _input_row(n: int) -> dict[str, object]:
    return {
        "n": n,
        "title": f"title-{n}",
        "author": f"author-{n}",
        "platform": "twitter",
        "excerpt": f"excerpt-{n}",
    }


def _run(score10: float | None, *, error: str | None = None) -> dict[str, object]:
    return {
        "score10": score10,
        "veto": "none",
        "normalized": {
            "dims": {
                "authority": 2,
                "substance": 3,
                "novelty": 2,
                "timeliness": 1,
                "audience_fit": 3,
            },
            "veto": "none",
        },
        "error": error,
        "attempts": 1,
    }


def _labeled_item(
    n: int,
    kind: str,
    label: str,
    *scores: float | None,
) -> dict[str, object]:
    return {
        **_input_row(n),
        "label": label,
        "kind": kind,
        "old_score10": 5.5,
        "runs": [_run(score) for score in scores],
    }


def _gate_fixture_items() -> list[dict[str, object]]:
    items = [
        _labeled_item(n, "收藏正例", "进", 7.0 if n <= 19 else 6.9)
        for n in range(1, 21)
    ]
    for offset, kind in enumerate(("拼盘日报", "名人一句话", "进度贴"), start=1):
        base = offset * 100
        items.extend(
            _labeled_item(base + index, kind, "不进", 6.9 if index <= 9 else 7.0)
            for index in range(1, 11)
        )
    return items


def test_load_labeled_items_reads_dict_label_kind_and_old_score(tmp_path, monkeypatch):
    gate = _module()
    inputs = [_input_row(n) for n in range(1, 11)]
    gold = {
        str(n): {
            "label": "进" if n <= 5 else "不进",
            "kind": "收藏正例" if n <= 5 else "进度贴",
            "old_score10": n / 10,
        }
        for n in range(1, 11)
    }
    input_file = tmp_path / "标注-input.json"
    gold_file = tmp_path / "标注-金标.json"
    input_file.write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")
    gold_file.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gate, "INPUT_FILE", input_file)
    monkeypatch.setattr(gate, "GOLD_FILE", gold_file)

    rows = gate.load_labeled_items()

    assert len(rows) == 10
    assert rows[0]["label"] == "进"
    assert rows[0]["kind"] == "收藏正例"
    assert rows[0]["old_score10"] == 0.1
    assert rows[0]["runs"] == []


@pytest.mark.parametrize(("input_count", "gold_count"), [(9, 9), (10, 9)])
def test_load_labeled_items_requires_matching_counts_and_at_least_ten(
    tmp_path,
    monkeypatch,
    input_count,
    gold_count,
):
    gate = _module()
    input_file = tmp_path / "input.json"
    gold_file = tmp_path / "gold.json"
    input_file.write_text(
        json.dumps([_input_row(n) for n in range(1, input_count + 1)]),
        encoding="utf-8",
    )
    gold_file.write_text(
        json.dumps({str(n): "进" for n in range(1, gold_count + 1)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "INPUT_FILE", input_file)
    monkeypatch.setattr(gate, "GOLD_FILE", gold_file)

    with pytest.raises(ValueError, match="input 与 gold 条数必须一致且至少 10 条"):
        gate.load_labeled_items()


def test_calculate_metrics_uses_display_threshold_and_kind_targets():
    gate = _module()

    metrics = gate.calculate_metrics(_gate_fixture_items(), display_threshold=7.0)

    assert metrics["positive_retention"] == {
        "kind": "收藏正例",
        "kept": 19,
        "total": 20,
        "rate": 0.95,
        "target": 0.95,
        "meets_target": True,
    }
    assert {
        kind: (row["blocked"], row["total"], row["rate"], row["meets_target"])
        for kind, row in metrics["negative_interception"].items()
    } == {
        "拼盘日报": (9, 10, 0.9, True),
        "名人一句话": (9, 10, 0.9, True),
        "进度贴": (9, 10, 0.9, True),
    }
    assert metrics["gate_passed"] is True


def test_threshold_scan_covers_six_to_eight_in_quarter_steps():
    gate = _module()

    scan = gate.scan_thresholds(_gate_fixture_items())

    assert [row["threshold"] for row in scan] == [
        6.0,
        6.25,
        6.5,
        6.75,
        7.0,
        7.25,
        7.5,
        7.75,
        8.0,
    ]
    at_seven = next(row for row in scan if row["threshold"] == 7.0)
    assert at_seven["positive_retention"]["rate"] == 0.95
    assert at_seven["negative_interception"]["拼盘日报"]["rate"] == 0.9


def test_score_items_accepts_fake_scorer_and_stability_reports_delta_distribution():
    gate = _module()
    items = [
        _labeled_item(1, "收藏正例", "进"),
        _labeled_item(2, "收藏正例", "进"),
        _labeled_item(3, "进度贴", "不进"),
    ]
    scores = iter((7.0, 7.0, 6.5, 7.5, 6.0, 7.2))
    calls = []

    def fake_scorer(item):
        calls.append(item["n"])
        return _run(next(scores))

    gate.score_items(items, runs=2, scorer=fake_scorer)
    stability = gate.analyze_stability(items)

    assert calls == [1, 1, 2, 2, 3, 3]
    assert stability["evaluated_count"] == 3
    assert stability["within_1_count"] == 2
    assert stability["within_1_rate"] == 0.6667
    assert stability["delta_distribution"] == {"0.0": 1, "1.0": 1, "1.2": 1}


def test_report_contains_kind_metrics_and_required_detail_columns():
    gate = _module()
    items = _gate_fixture_items()
    metrics = gate.calculate_metrics(items, display_threshold=7.0)
    output = gate.build_output(
        items,
        metrics=metrics,
        scan=gate.scan_thresholds(items),
        stability=None,
        runs=1,
        display_threshold=7.0,
    )

    report = gate.render_report(output)

    assert "正例保留率（收藏正例）" in report
    assert "拼盘日报" in report
    assert "名人一句话" in report
    assert "进度贴" in report
    assert "| n | kind | old_score10 | new_score10 | 维度分 | veto | 判定 | 是否符合金标 |" in report


def test_dry_run_prints_sample_list_without_loading_llm_runtime(monkeypatch, capsys):
    gate = _module()
    items = [
        _labeled_item(n, "收藏正例", "进")
        for n in range(1, 11)
    ]
    monkeypatch.setattr(gate, "load_labeled_items", lambda: items)
    monkeypatch.setattr(
        gate,
        "load_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not load runtime")),
    )
    monkeypatch.setattr(sys, "argv", ["v27_gate_eval.py", "--dry-run"])

    assert gate.main() == 0

    output = capsys.readouterr().out
    assert "[v27-gate] dry-run samples=10" in output
    assert "title-1" in output
    assert "title-10" in output
