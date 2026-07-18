from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import highlight_score_v26 as scoring  # noqa: E402


def _result(**overrides):
    result = {
        "reject": False,
        "content_type": "general",
        "content_type_confidence": 0.9,
        "dims": {
            "authority": 3,
            "substance": 3,
            "novelty": 3,
            "timeliness": 3,
            "audience_fit": 3,
        },
        "marketing": 0,
        "veto": "none",
        "uncertainty": "none",
        "value_path": "substantive",
    }
    result.update(overrides)
    return result


def test_prompt_file_matches_v26_contract():
    assert scoring.PROMPT_FILE == "15_item_score_v26.md"


def test_normalize_accepts_strict_json_and_markdown_code_fence():
    raw = _result(content_type="product_tool")

    normalized = scoring.normalize_score_result(
        f"```json\n{json.dumps(raw)}\n```"
    )

    assert "error" not in normalized
    assert normalized["content_type"] == "product_tool"
    assert normalized["dims"] == raw["dims"]


def test_normalize_extracts_terminal_json_fence_after_analysis_text():
    raw = _result(content_type="product_tool")

    normalized = scoring.normalize_score_result(
        "分析区：主体是一个可上手的工具。\n"
        f"```json\n{json.dumps(raw)}\n```"
    )

    assert "error" not in normalized
    assert normalized["content_type"] == "product_tool"


def test_normalize_uses_last_json_fence_when_response_has_two():
    first = _result(content_type="dynamic_news")
    last = _result(content_type="tutorial_method")

    normalized = scoring.normalize_score_result(
        f"```json\n{json.dumps(first)}\n```\n"
        "分析区：上一个对象只是草稿。\n"
        f"```\n{json.dumps(last)}\n```"
    )

    assert "error" not in normalized
    assert normalized["content_type"] == "tutorial_method"


def test_normalize_accepts_unfenced_strict_json():
    raw = _result(content_type="evaluation_report")

    normalized = scoring.normalize_score_result(json.dumps(raw))

    assert "error" not in normalized
    assert normalized["content_type"] == "evaluation_report"


def test_normalize_extracts_last_balanced_object_from_unfenced_text():
    draft = _result(content_type="opinion_case")
    last = _result(content_type="general")

    normalized = scoring.normalize_score_result(
        f"分析草稿：{json.dumps(draft)}\n最终结果：{json.dumps(last)}"
    )

    assert "error" not in normalized
    assert normalized["content_type"] == "general"


def test_normalize_accepts_whitespace_after_json_fence():
    raw = _result(content_type="product_tool")

    normalized = scoring.normalize_score_result(
        f"```json\n{json.dumps(raw)}\n```\n \t\n"
    )

    assert "error" not in normalized
    assert normalized["content_type"] == "product_tool"


def test_bad_json_returns_pending_error_result():
    result = scoring.normalize_score_result("not json {{{")

    assert "error" in result


def test_missing_required_field_returns_pending_error_result():
    raw = _result()
    del raw["uncertainty"]

    result = scoring.normalize_score_result(raw)

    assert "error" in result


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("marketing", 4),
        ("veto", "other"),
        ("uncertainty", "maybe"),
        ("value_path", "other"),
        ("content_type", "other"),
        ("content_type_confidence", 1.1),
        ("reject", 0),
    ],
)
def test_invalid_top_level_field_returns_pending_error_result(field, invalid_value):
    result = scoring.normalize_score_result(_result(**{field: invalid_value}))

    assert "error" in result


@pytest.mark.parametrize("invalid_value", [-1, 4, 1.5, True, "2"])
def test_invalid_dimension_returns_pending_error_result(invalid_value):
    dims = dict(_result()["dims"])
    dims["authority"] = invalid_value

    result = scoring.normalize_score_result(_result(dims=dims))

    assert "error" in result


def test_marketing_three_forces_marketing_veto():
    result = scoring.normalize_score_result(
        _result(marketing=3, veto="none")
    )

    assert "error" not in result
    assert result["veto"] == "marketing"


@pytest.mark.parametrize(
    ("content_type", "dims", "marketing", "expected"),
    [
        (
            "general",
            {"authority": 2, "substance": 0, "novelty": 1, "timeliness": 2, "audience_fit": 1},
            0,
            3.5,
        ),
        (
            "product_tool",
            {"authority": 1, "substance": 1, "novelty": 1, "timeliness": 2, "audience_fit": 1},
            2,
            1.5,
        ),
        (
            "tutorial_method",
            {"authority": 2, "substance": 3, "novelty": 2, "timeliness": 1, "audience_fit": 3},
            0,
            8.8,
        ),
        (
            "evaluation_report",
            {"authority": 2, "substance": 2, "novelty": 2, "timeliness": 2, "audience_fit": 3},
            0,
            7.3,
        ),
        (
            "dynamic_news",
            {"authority": 3, "substance": 3, "novelty": 3, "timeliness": 3, "audience_fit": 3},
            0,
            10.0,
        ),
    ],
    ids=["token_spend", "marketing_copy", "seo_tutorial", "deepswe_eval", "gpt_release"],
)
def test_section_4_6_calibration_examples(content_type, dims, marketing, expected):
    result = _result(
        content_type=content_type,
        dims=dims,
        marketing=marketing,
    )

    assert scoring.compute_score10(result) == expected


def test_low_content_type_confidence_falls_back_to_general_weights():
    result = _result(
        content_type="tutorial_method",
        content_type_confidence=0.59,
        dims={"authority": 2, "substance": 3, "novelty": 2, "timeliness": 1, "audience_fit": 3},
    )

    assert scoring.compute_score10(result) == 7.8


def test_authority_zero_hard_cap_applies():
    result = _result(
        content_type="dynamic_news",
        dims={"authority": 0, "substance": 3, "novelty": 3, "timeliness": 3, "audience_fit": 3},
    )

    assert scoring.compute_score10(result) == 4.9


def test_audience_fit_one_hard_cap_applies():
    result = _result(
        dims={"authority": 3, "substance": 3, "novelty": 3, "timeliness": 3, "audience_fit": 1},
    )

    assert scoring.compute_score10(result) == 4.9


def test_novelty_zero_hard_cap_applies():
    result = _result(
        dims={"authority": 3, "substance": 3, "novelty": 0, "timeliness": 3, "audience_fit": 3},
    )

    assert scoring.compute_score10(result) == 5.9


def test_score_is_clamped_to_zero_and_ten():
    lower = _result(
        content_type="product_tool",
        dims={"authority": 0, "substance": 0, "novelty": 0, "timeliness": 0, "audience_fit": 0},
        marketing=3,
        veto="marketing",
    )
    upper = _result(content_type="dynamic_news")

    assert scoring.compute_score10(lower) == 0.0
    assert scoring.compute_score10(upper) == 10.0


def test_reject_and_error_results_have_no_score():
    assert scoring.compute_score10(_result(reject=True)) is None
    assert scoring.compute_score10({"error": "pending"}) is None


def test_flag_bearer_requires_threshold_veto_uncertainty_and_non_reject():
    result = _result()

    assert scoring.is_flag_bearer(result, 5.5, 5.5) is True
    assert scoring.is_flag_bearer(result, 5.4, 5.5) is False
    assert scoring.is_flag_bearer({**result, "veto": "marketing"}, 9.0, 5.5) is False
    assert scoring.is_flag_bearer(
        {**result, "uncertainty": "unverified_major_claim"}, 9.0, 5.5
    ) is False
    assert scoring.is_flag_bearer({**result, "reject": True}, 9.0, 5.5) is False
    assert scoring.is_flag_bearer(result, None, 5.5) is False
