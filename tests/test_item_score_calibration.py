from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import item_score_calibration as calibration  # noqa: E402


def _config() -> dict:
    return {"spam": {"exclude_at": 3}}


def _record(**overrides):
    base = {
        "cluster_sample_key": "cluster-a",
        "sample_variant": "original",
        "cluster_id": "cluster-a",
        "cluster_title": "Cluster A",
        "item_id": "item-a",
        "title": "Useful AI tool",
        "platform": "rss",
        "source": "Example",
        "url": "https://example.com/a",
        "ai_summary": "A useful summary.",
        "ai_category": "efficiency_tools",
        "ai_relevant": "yes",
        "importance": 1,
        "novelty": 2,
        "credibility": 2,
        "substance": 2,
        "actionability": 2,
        "spam_score": 2,
        "time_sensitivity": 1,
        "borderline": [],
        "quality_score": 0.5,
        "time_factor": 1,
        "item_score": 0.5,
        "item_verdict": "featured",
        "reason": "Useful but not broadly important.",
    }
    base.update(overrides)
    return base


def test_default_review_file_is_new_blind_csv():
    assert calibration.DEFAULT_REVIEW_FILE.name == "human_review_v2_blind.csv"


def test_item_verdict_uses_multi_path_value_without_importance_gate():
    assert calibration.item_verdict(_record(), _config()) == "featured"

    low_cred = _record(credibility=1)
    assert calibration.item_verdict(low_cred, _config()) == "review"

    all_low = _record(importance=1, novelty=1, substance=1, actionability=1, credibility=2)
    assert calibration.item_verdict(all_low, _config()) == "drop"

    assert calibration.item_verdict(_record(ai_relevant="no"), _config()) == "drop"
    assert calibration.item_verdict(_record(spam_score=3), _config()) == "drop"


def test_build_review_rows_uses_blind_order_not_rule_score_order():
    derived = [
        _record(
            cluster_sample_key="b",
            cluster_id="b",
            cluster_title="B",
            item_id="i-b",
            item_score=0.9,
            item_verdict="featured",
        ),
        _record(
            cluster_sample_key="a",
            cluster_id="a",
            cluster_title="A",
            item_id="i-a",
            item_score=0.1,
            item_verdict="drop",
        ),
    ]

    rows = calibration.build_review_rows(derived)

    assert [row["cluster_sample_key"] for row in rows] == ["a", "b"]
    assert all(row["human_verdict"] == "" for row in rows)
    assert json.loads(rows[0]["items_json"])[0]["title"] == "Useful AI tool"


def test_apply_review_updates_accepts_two_choice_labels_only(tmp_path):
    path = tmp_path / "review.csv"
    rows = calibration.build_review_rows([_record(cluster_sample_key="a", cluster_id="a")])
    calibration.write_review_csv(path, rows)

    calibration.apply_review_updates(path, [{"cluster_sample_key": "a", "human_verdict": "review"}])
    loaded = list(csv.DictReader(path.open("r", encoding="utf-8")))
    assert loaded[0]["human_verdict"] == ""

    calibration.apply_review_updates(path, [{"cluster_sample_key": "a", "human_verdict": "featured"}])
    loaded = list(csv.DictReader(path.open("r", encoding="utf-8")))
    assert loaded[0]["human_verdict"] == "featured"

    calibration.apply_review_updates(path, [{"cluster_sample_key": "a", "human_verdict": "drop"}])
    loaded = list(csv.DictReader(path.open("r", encoding="utf-8")))
    assert loaded[0]["human_verdict"] == "drop"


def test_preserve_review_keeps_human_fields_on_reexport():
    rows = calibration.build_review_rows([_record(cluster_sample_key="a", cluster_id="a")])
    existing = [
        {
            "cluster_sample_key": "a",
            "human_verdict": "featured",
            "rule_agree": "disagree",
            "error_kind": "rule_wrong",
            "human_notes": "useful tool",
        }
    ]

    calibration.preserve_review(rows, existing)

    assert rows[0]["human_verdict"] == "featured"
    assert rows[0]["rule_agree"] == "disagree"
    assert rows[0]["error_kind"] == "rule_wrong"
    assert rows[0]["human_notes"] == "useful tool"


def test_review_html_starts_blind_and_reveals_diagnostics_after_label():
    html = calibration.REVIEW_HTML

    assert "规则:进精选" not in html
    assert "<option value=review>" not in html
    assert "<select id=hv" not in html
    assert "<option value=featured>进精选</option>" not in html
    assert "data-verdict=featured" in html
    assert "data-verdict=drop" in html
    assert "chooseVerdict(verdict)" in html
    assert "human_verdict:verdict" in html
    assert "nextUnlabeledAfter" in html
    assert "评分诊断已隐藏" in html
    assert "const hasVerdict=['featured','drop'].includes" in html
    assert "cluster_score=" in html
