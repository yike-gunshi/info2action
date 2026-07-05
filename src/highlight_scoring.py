"""Cluster highlight scoring policy for offline calibration.

This module deliberately does not decide whether a cluster enters 精选. The LLM
returns 1-5 anchored judgments; code maps those anchors into an internal 0-100
diagnostic score for small-sample calibration and human review.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy
from statistics import mean
from typing import Any


DIMENSIONS: tuple[str, ...] = (
    "information_value",
    "usefulness",
    "timeliness",
    "authority_trust",
    "content_depth",
    "domain_fit",
    "cluster_incremental_value",
)

NOISE_FIELD = "marketing_noise"
MIN_CONTENT_TYPE_CONFIDENCE = 0.6

CONTENT_TYPE_LABELS: dict[str, str] = {
    "dynamic_news": "动态新闻/发布事件",
    "product_tool": "产品工具/资源发现",
    "tutorial_method": "教程方法/实践指南",
    "evaluation_report": "评测报告/数据分析",
    "opinion_case": "观点案例/经验复盘",
    "general": "通用兜底",
    "reject": "拒绝类",
}

DEFAULT_POLICY: dict[str, Any] = {
    "version": "cluster_highlight_scoring_calibration_v1",
    "dimension_scale": [1, 5],
    "score_range": "derived_0_100",
    "final_threshold": None,
    "profiles": {
        "dynamic_news": {
            "label": CONTENT_TYPE_LABELS["dynamic_news"],
            "marketing_noise_cap": 20,
            "weights": {
                "information_value": 22,
                "usefulness": 8,
                "timeliness": 18,
                "authority_trust": 18,
                "content_depth": 8,
                "domain_fit": 12,
                "cluster_incremental_value": 14,
            },
        },
        "product_tool": {
            "label": CONTENT_TYPE_LABELS["product_tool"],
            "marketing_noise_cap": 30,
            "weights": {
                "information_value": 16,
                "usefulness": 24,
                "timeliness": 8,
                "authority_trust": 14,
                "content_depth": 12,
                "domain_fit": 14,
                "cluster_incremental_value": 12,
            },
        },
        "tutorial_method": {
            "label": CONTENT_TYPE_LABELS["tutorial_method"],
            "marketing_noise_cap": 20,
            "weights": {
                "information_value": 8,
                "usefulness": 28,
                "timeliness": 4,
                "authority_trust": 14,
                "content_depth": 24,
                "domain_fit": 14,
                "cluster_incremental_value": 8,
            },
        },
        "evaluation_report": {
            "label": CONTENT_TYPE_LABELS["evaluation_report"],
            "marketing_noise_cap": 25,
            "weights": {
                "information_value": 18,
                "usefulness": 14,
                "timeliness": 5,
                "authority_trust": 24,
                "content_depth": 22,
                "domain_fit": 9,
                "cluster_incremental_value": 8,
            },
        },
        "opinion_case": {
            "label": CONTENT_TYPE_LABELS["opinion_case"],
            "marketing_noise_cap": 25,
            "weights": {
                "information_value": 18,
                "usefulness": 24,
                "timeliness": 5,
                "authority_trust": 14,
                "content_depth": 18,
                "domain_fit": 14,
                "cluster_incremental_value": 9,
            },
        },
        "general": {
            "label": CONTENT_TYPE_LABELS["general"],
            "marketing_noise_cap": 25,
            "weights": {
                "information_value": 18,
                "usefulness": 18,
                "timeliness": 12,
                "authority_trust": 14,
                "content_depth": 14,
                "domain_fit": 14,
                "cluster_incremental_value": 10,
            },
        },
    },
    "hard_caps": {
        "low_domain_fit": {
            "dimension": "domain_fit",
            "max_value": 2,
            "max_score": 59,
            "reason": "领域匹配不足，不能进入高分区。",
        },
        "low_incremental_value": {
            "dimension": "cluster_incremental_value",
            "max_value": 1,
            "max_score": 59,
            "reason": "cluster 没有明显新信息增量或去重价值。",
        },
        "weak_incremental_value": {
            "dimension": "cluster_incremental_value",
            "max_value": 2,
            "max_score": 69,
            "reason": "cluster 增量弱，只能作为候选或复核，不能进入高分区。",
        },
        "low_authority_trust": {
            "dimension": "authority_trust",
            "max_value": 2,
            "max_score": 64,
            "reason": "权威可信不足，不能进入高分区。",
        },
        "weak_authority_for_claims": {
            "dimension": "authority_trust",
            "max_value": 2,
            "requires_flag": "factual_claims_or_data",
            "max_score": 64,
            "reason": "事实或数据主张缺少可信来源支撑。",
        },
        "high_marketing_noise": {
            "dimension": "marketing_noise",
            "min_value": 4,
            "max_score": 49,
            "reason": "营销噪声明显，不能进入候选高分区。",
        },
    },
}


def policy() -> dict[str, Any]:
    return deepcopy(DEFAULT_POLICY)


def _clamp_number(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(high, max(low, number))


def _anchor_to_unit(value: Any) -> float:
    score = _clamp_number(value, 1, 5, 1)
    return (score - 1) / 4


def _normalize_score(value: Any) -> int:
    return int(round(_clamp_number(value, 1, 5, 1)))


def _confidence_to_float(value: Any) -> float:
    if isinstance(value, str):
        rank = {"low": 0.25, "medium": 0.55, "high": 0.85}
        return rank.get(value.strip().lower(), 0.0)
    return _clamp_number(value, 0, 1, 0)


def _profile_for(raw_result: dict[str, Any], scoring_policy: dict[str, Any]) -> tuple[str, str | None]:
    hard_gate = str(raw_result.get("hard_gate") or "").strip().lower()
    content_type = str(raw_result.get("content_type") or "").strip()
    profiles = scoring_policy["profiles"]
    if hard_gate == "reject" or content_type == "reject":
        return "reject", None
    if content_type not in profiles:
        return "general", "unknown_content_type"
    confidence = _confidence_to_float(raw_result.get("content_type_confidence"))
    if confidence < MIN_CONTENT_TYPE_CONFIDENCE:
        return "general", "low_content_type_confidence"
    return content_type, None


def _normalized_dimensions(raw_result: dict[str, Any]) -> dict[str, int]:
    raw_scores = raw_result.get("dimension_scores") or {}
    if not isinstance(raw_scores, dict):
        raw_scores = {}
    return {
        dimension: _normalize_score(raw_scores.get(dimension))
        for dimension in DIMENSIONS
    }


def _normalize_evidence(value: Any) -> dict[str, list[str]]:
    if isinstance(value, dict):
        evidence: dict[str, list[str]] = {}
        for key, raw_items in value.items():
            if isinstance(raw_items, list):
                items = [str(item) for item in raw_items if str(item).strip()]
            elif raw_items is None:
                items = []
            else:
                items = [str(raw_items)]
            evidence[str(key)] = items
        return evidence
    if isinstance(value, list):
        items = [str(item) for item in value if str(item).strip()]
        if len(items) == len(DIMENSIONS):
            return {dimension: [items[idx]] for idx, dimension in enumerate(DIMENSIONS)}
        return {"general": items}
    if value:
        return {"general": [str(value)]}
    return {}


def _first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return text


def _round(value: float) -> float:
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return float(int(rounded))
    return rounded


def _apply_caps(
    score: float,
    dimensions: dict[str, int],
    raw_result: dict[str, Any],
    scoring_policy: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    capped = score
    applied: list[dict[str, Any]] = []
    for cap_id, cap in scoring_policy.get("hard_caps", {}).items():
        flag = cap.get("requires_flag")
        if flag and not raw_result.get(flag):
            continue
        dimension = cap.get("dimension")
        if not dimension:
            continue
        current_value = dimensions.get(dimension, 5)
        if cap.get("min_value") is not None:
            condition_matches = current_value >= int(cap["min_value"])
        else:
            condition_matches = current_value <= int(cap.get("max_value", 0))
        if condition_matches and capped > float(cap["max_score"]):
            capped = float(cap["max_score"])
            applied.append({
                "id": cap_id,
                "dimension": dimension,
                "max_score": float(cap["max_score"]),
                "reason": cap.get("reason") or "",
            })
    return capped, applied


def diagnostic_band(score: float, *, hard_gate: str = "pass") -> str:
    if hard_gate == "reject":
        return "reject"
    if score >= 80:
        return "score_80_plus"
    if score >= 65:
        return "score_65_79"
    if score >= 50:
        return "score_50_64"
    return "score_below_50"


def derive_highlight_score(
    raw_result: dict[str, Any],
    scoring_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive an internal percent score from LLM 1-5 anchors.

    The returned score is for offline calibration. ``final_threshold`` remains
    ``None`` until human review confirms a global or type-specific threshold.
    """
    scoring_policy = deepcopy(scoring_policy or DEFAULT_POLICY)
    profile_id, fallback_reason = _profile_for(raw_result, scoring_policy)
    hard_gate = str(raw_result.get("hard_gate") or "review").strip().lower()
    llm_bucket = str(raw_result.get("bucket") or "").strip()
    if profile_id == "reject":
        return {
            "policy_version": scoring_policy["version"],
            "hard_gate": "reject",
            "profile_id": "reject",
            "profile_label": CONTENT_TYPE_LABELS["reject"],
            "profile_fallback_reason": fallback_reason,
            "positive_score": 0.0,
            "noise_penalty": 0.0,
            "derived_score": 0.0,
            "applied_caps": [],
            "advisory_bucket": "reject",
            "diagnostic_band": "reject",
            "llm_bucket": llm_bucket or "reject",
            "final_threshold": scoring_policy.get("final_threshold"),
            "scoring_range": scoring_policy["score_range"],
        }

    profile = scoring_policy["profiles"][profile_id]
    weights = profile["weights"]
    dimensions = _normalized_dimensions(raw_result)
    noise = _normalize_score(raw_result.get(NOISE_FIELD, raw_result.get("noise_level", 1)))
    cap_dimensions = {**dimensions, NOISE_FIELD: noise}
    positive = sum(weights[dimension] * _anchor_to_unit(dimensions[dimension]) for dimension in DIMENSIONS)
    noise_cap = float(profile.get("marketing_noise_cap") or 0)
    noise_penalty = noise_cap * _anchor_to_unit(noise)
    score_before_caps = max(0.0, min(100.0, positive - noise_penalty))
    capped, applied_caps = _apply_caps(score_before_caps, cap_dimensions, raw_result, scoring_policy)
    final_score = max(0.0, min(100.0, capped))

    return {
        "policy_version": scoring_policy["version"],
        "hard_gate": hard_gate or "review",
        "profile_id": profile_id,
        "profile_label": profile["label"],
        "profile_fallback_reason": fallback_reason,
        "dimension_scores": dimensions,
        "positive_score": _round(positive),
        "marketing_noise": noise,
        "noise_penalty": _round(noise_penalty),
        "score_before_caps": _round(score_before_caps),
        "derived_score": _round(final_score),
        "applied_caps": applied_caps,
        "advisory_bucket": llm_bucket or "review",
        "diagnostic_band": diagnostic_band(final_score, hard_gate=hard_gate),
        "llm_bucket": llm_bucket,
        "final_threshold": scoring_policy.get("final_threshold"),
        "scoring_range": scoring_policy["score_range"],
    }


def normalize_llm_result(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse strict JSON-ish LLM output and normalize expected fields."""
    if isinstance(raw, dict):
        parsed = deepcopy(raw)
    else:
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = json.loads(_first_json_object(text))
    if not isinstance(parsed, dict):
        raise ValueError("LLM result must be a JSON object")

    parsed["hard_gate"] = str(parsed.get("hard_gate") or "review").strip().lower()
    if parsed["hard_gate"] not in {"pass", "reject", "review"}:
        parsed["hard_gate"] = "review"
    parsed["content_type"] = str(parsed.get("content_type") or "general").strip()
    parsed["dimension_scores"] = _normalized_dimensions(parsed)
    parsed["dimension_evidence"] = _normalize_evidence(parsed.get("dimension_evidence"))
    parsed[NOISE_FIELD] = _normalize_score(parsed.get(NOISE_FIELD, parsed.get("noise_level", 1)))
    parsed["content_type_confidence"] = _confidence_to_float(parsed.get("content_type_confidence", parsed.get("confidence", 0)))
    parsed["confidence"] = _confidence_to_float(parsed.get("confidence", 0))
    if not isinstance(parsed.get("reason_codes"), list):
        parsed["reason_codes"] = []
    return parsed


def _row_score(row: dict[str, Any]) -> float | None:
    for key in ("derived_score", "score"):
        if row.get(key) is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                return None
    derived = row.get("derived")
    if isinstance(derived, dict) and derived.get("derived_score") is not None:
        try:
            return float(derived["derived_score"])
        except (TypeError, ValueError):
            return None
    return None


def _row_bucket(row: dict[str, Any]) -> str:
    return str(row.get("advisory_bucket") or row.get("bucket") or "").strip()


def _row_diagnostic_band(row: dict[str, Any]) -> str:
    existing = str(row.get("diagnostic_band") or "").strip()
    if existing:
        return existing
    score = _row_score(row)
    if score is None:
        return "unknown"
    return diagnostic_band(score, hard_gate=str(row.get("hard_gate") or "pass"))


def _row_dimensions(row: dict[str, Any]) -> dict[str, int]:
    raw = row.get("dimension_scores")
    if not isinstance(raw, dict):
        raw = row.get("raw_result", {}).get("dimension_scores") if isinstance(row.get("raw_result"), dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {key: _normalize_score(value) for key, value in raw.items()}


def analyze_reliability(rows: list[dict[str, Any]], *, score_jump_threshold: float = 10) -> dict[str, Any]:
    """Summarize duplicate/rerun stability for calibration artifacts."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row.get("sample_key") or row.get("cluster_id") or row.get("id") or "").strip()
        if key:
            grouped[key].append(row)

    duplicate_groups = {key: value for key, value in grouped.items() if len(value) >= 2}
    type_consistent = 0
    bucket_consistent = 0
    diagnostic_band_consistent = 0
    unstable_score_groups: list[dict[str, Any]] = []
    dimension_jump_groups: list[dict[str, Any]] = []
    score_ranges: list[float] = []

    for key, group in duplicate_groups.items():
        content_types = {str(row.get("content_type") or row.get("profile_id") or "") for row in group}
        buckets = {_row_bucket(row) for row in group}
        diagnostic_bands = {_row_diagnostic_band(row) for row in group}
        if len(content_types) <= 1:
            type_consistent += 1
        if len(buckets) <= 1:
            bucket_consistent += 1
        if len(diagnostic_bands) <= 1:
            diagnostic_band_consistent += 1

        scores = [score for score in (_row_score(row) for row in group) if score is not None]
        if scores:
            score_range = max(scores) - min(scores)
            score_ranges.append(score_range)
            if score_range >= score_jump_threshold:
                unstable_score_groups.append({
                    "sample_key": key,
                    "score_range": _round(score_range),
                    "scores": [_round(score) for score in scores],
                })

        dimension_names = {
            dimension
            for row in group
            for dimension in _row_dimensions(row).keys()
        }
        jumped: dict[str, int] = {}
        for dimension in sorted(dimension_names):
            values = [_row_dimensions(row).get(dimension) for row in group]
            values = [value for value in values if value is not None]
            if values and max(values) - min(values) >= 1:
                jumped[dimension] = max(values) - min(values)
        if jumped:
            dimension_jump_groups.append({"sample_key": key, "dimension_jumps": jumped})

    group_count = len(duplicate_groups)
    return {
        "row_count": len(rows),
        "sample_key_count": len(grouped),
        "duplicate_group_count": group_count,
        "content_type_consistency_rate": round(type_consistent / group_count, 4) if group_count else None,
        "bucket_consistency_rate": round(bucket_consistent / group_count, 4) if group_count else None,
        "diagnostic_band_consistency_rate": round(diagnostic_band_consistent / group_count, 4) if group_count else None,
        "unstable_score_group_count": len(unstable_score_groups),
        "unstable_score_groups": unstable_score_groups,
        "dimension_jump_group_count": len(dimension_jump_groups),
        "dimension_jump_groups": dimension_jump_groups,
        "score_range_avg": _round(mean(score_ranges)) if score_ranges else None,
        "score_range_max": _round(max(score_ranges)) if score_ranges else None,
        "final_threshold": None,
    }
