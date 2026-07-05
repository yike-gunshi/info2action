from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import highlight_scoring as scoring  # noqa: E402


def _raw_result(**overrides):
    base = {
        "hard_gate": "pass",
        "content_type": "dynamic_news",
        "content_type_confidence": 0.9,
        "dimension_scores": {
            "information_value": 5,
            "usefulness": 5,
            "timeliness": 5,
            "authority_trust": 5,
            "content_depth": 5,
            "domain_fit": 5,
            "cluster_incremental_value": 5,
        },
        "dimension_evidence": {
            "information_value": ["发布了明确的新能力"],
            "usefulness": ["给出可执行影响"],
            "timeliness": ["24 小时内"],
            "authority_trust": ["来自官方公告"],
            "content_depth": ["包含机制和影响"],
            "domain_fit": ["AI 产品相关"],
            "cluster_incremental_value": ["多来源互补"],
        },
        "marketing_noise": 1,
        "bucket": "featured_candidate",
        "confidence": 0.82,
        "reason_codes": ["official_release"],
    }
    base.update(overrides)
    return base


def test_derived_score_uses_anchored_1_to_5_scale_not_llm_percent():
    derived = scoring.derive_highlight_score(_raw_result())

    assert derived["derived_score"] == 100.0
    assert derived["final_threshold"] is None
    assert derived["scoring_range"] == "derived_0_100"

    low = _raw_result(
        dimension_scores={
            "information_value": 1,
            "usefulness": 1,
            "timeliness": 1,
            "authority_trust": 1,
            "content_depth": 1,
            "domain_fit": 1,
            "cluster_incremental_value": 1,
        }
    )

    assert scoring.derive_highlight_score(low)["derived_score"] == 0.0


def test_marketing_noise_subtracts_type_specific_penalty():
    noisy_tool = _raw_result(content_type="product_tool", marketing_noise=5)

    derived = scoring.derive_highlight_score(noisy_tool)

    assert derived["profile_id"] == "product_tool"
    assert derived["positive_score"] == 100.0
    assert derived["noise_penalty"] == 30.0
    assert derived["score_before_caps"] == 70.0
    assert derived["derived_score"] == 49.0


def test_hard_caps_keep_low_domain_fit_out_of_high_score_area():
    weak_domain = _raw_result(
        dimension_scores={
            "information_value": 5,
            "usefulness": 5,
            "timeliness": 5,
            "authority_trust": 5,
            "content_depth": 5,
            "domain_fit": 2,
            "cluster_incremental_value": 5,
        }
    )

    derived = scoring.derive_highlight_score(weak_domain)

    assert derived["derived_score"] == 59.0
    assert derived["applied_caps"][0]["id"] == "low_domain_fit"


def test_hard_caps_keep_weak_authority_out_of_high_score_area():
    weak_authority = _raw_result(
        dimension_scores={
            "information_value": 5,
            "usefulness": 5,
            "timeliness": 5,
            "authority_trust": 2,
            "content_depth": 5,
            "domain_fit": 5,
            "cluster_incremental_value": 5,
        }
    )

    derived = scoring.derive_highlight_score(weak_authority)

    assert derived["derived_score"] == 64.0
    assert derived["applied_caps"][0]["id"] == "low_authority_trust"


def test_hard_caps_keep_weak_incremental_value_out_of_high_score_area():
    weak_increment = _raw_result(
        dimension_scores={
            "information_value": 5,
            "usefulness": 5,
            "timeliness": 5,
            "authority_trust": 5,
            "content_depth": 5,
            "domain_fit": 5,
            "cluster_incremental_value": 2,
        }
    )

    derived = scoring.derive_highlight_score(weak_increment)

    assert derived["derived_score"] == 69.0
    assert derived["applied_caps"][0]["id"] == "weak_incremental_value"


def test_hard_caps_keep_marketing_noise_out_of_high_score_area():
    noisy = _raw_result(marketing_noise=4)

    derived = scoring.derive_highlight_score(noisy)

    assert derived["derived_score"] == 49.0
    assert derived["applied_caps"][0]["id"] == "high_marketing_noise"


def test_reject_gate_returns_zero_without_thresholding():
    rejected = _raw_result(hard_gate="reject", bucket="reject", hard_gate_reason="纯营销")

    derived = scoring.derive_highlight_score(rejected)

    assert derived["derived_score"] == 0.0
    assert derived["hard_gate"] == "reject"
    assert derived["advisory_bucket"] == "reject"
    assert derived["diagnostic_band"] == "reject"
    assert derived["final_threshold"] is None


def test_low_content_type_confidence_falls_back_to_general_profile():
    raw = _raw_result(content_type="tutorial_method", content_type_confidence=0.2)

    derived = scoring.derive_highlight_score(raw)

    assert derived["profile_id"] == "general"
    assert derived["profile_fallback_reason"] == "low_content_type_confidence"


def test_missing_content_type_confidence_falls_back_to_general_profile():
    raw = _raw_result(content_type="tutorial_method", content_type_confidence=0)

    derived = scoring.derive_highlight_score(raw)

    assert derived["profile_id"] == "general"
    assert derived["profile_fallback_reason"] == "low_content_type_confidence"


def test_normalize_llm_result_maps_list_evidence_to_dimensions():
    raw = {
        **_raw_result(),
        "dimension_evidence": ["a", "b", "c", "d", "e", "f", "g"],
    }

    normalized = scoring.normalize_llm_result(raw)

    assert normalized["dimension_evidence"]["information_value"] == ["a"]
    assert normalized["dimension_evidence"]["cluster_incremental_value"] == ["g"]


def test_normalize_llm_result_keeps_odd_evidence_list_as_general():
    raw = {
        **_raw_result(),
        "dimension_evidence": ["a", "b"],
    }

    normalized = scoring.normalize_llm_result(raw)

    assert normalized["dimension_evidence"] == {"general": ["a", "b"]}


def test_normalize_llm_result_extracts_first_json_object_from_extra_text():
    raw = '{"hard_gate":"pass","content_type":"dynamic_news","dimension_scores":{},"marketing_noise":1} trailing text'

    normalized = scoring.normalize_llm_result(raw)

    assert normalized["hard_gate"] == "pass"
    assert normalized["content_type"] == "dynamic_news"


def test_reliability_analysis_flags_duplicate_instability():
    rows = [
        {
            "sample_key": "cluster-a",
            "content_type": "dynamic_news",
            "bucket": "featured_candidate",
            "derived_score": 84,
            "dimension_scores": {"usefulness": 4, "timeliness": 5},
        },
        {
            "sample_key": "cluster-a",
            "content_type": "dynamic_news",
            "bucket": "candidate",
            "derived_score": 72,
            "dimension_scores": {"usefulness": 3, "timeliness": 5},
        },
        {
            "sample_key": "cluster-b",
            "content_type": "tutorial_method",
            "bucket": "candidate",
            "derived_score": 68,
            "dimension_scores": {"usefulness": 4},
        },
        {
            "sample_key": "cluster-b",
            "content_type": "tutorial_method",
            "bucket": "candidate",
            "derived_score": 69,
            "dimension_scores": {"usefulness": 4},
        },
    ]

    summary = scoring.analyze_reliability(rows, score_jump_threshold=10)

    assert summary["duplicate_group_count"] == 2
    assert summary["content_type_consistency_rate"] == 1.0
    assert summary["bucket_consistency_rate"] == 0.5
    assert summary["diagnostic_band_consistency_rate"] == 0.5
    assert summary["unstable_score_group_count"] == 1
    assert summary["dimension_jump_group_count"] == 1
