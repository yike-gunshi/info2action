"""Normalize and compose v26 item-level Highlights scores."""
from __future__ import annotations

import json
import re
from typing import Any


PROMPT_FILE = "15_item_score_v26.md"
PROMPT_VERSION = "item_score_v26_7_taste_anchors_2026_07_15"

DIMENSIONS = ("authority", "substance", "novelty", "timeliness", "audience_fit")
VALID_VETOES = {"none", "marketing", "rumor_unverified", "flamewar", "engagement_bait"}
VALID_UNCERTAINTIES = {"none", "thin_detail", "needs_source", "unverified_major_claim"}
VALID_VALUE_PATHS = {"substantive", "major_event", "lead_value", "none"}

SCORE_PROFILES = {
    "dynamic_news": ((25, 20, 20, 20, 15), 2.0),
    "product_tool": ((15, 30, 20, 5, 30), 3.0),
    "tutorial_method": ((15, 40, 10, 5, 30), 2.0),
    "evaluation_report": ((30, 35, 10, 5, 20), 2.5),
    "opinion_case": ((20, 35, 15, 5, 25), 2.5),
    "general": ((20, 30, 15, 15, 20), 2.5),
}
VALID_CONTENT_TYPES = set(SCORE_PROFILES)


def _error(message: str) -> dict[str, str]:
    return {"error": message}


def _json_text(raw: str) -> str:
    text = raw.strip()
    fenced = re.findall(
        r"```(?:json)?[ \t]*(?:\r?\n|(?=\{))(.*?)\s*```",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced[-1].strip()

    try:
        json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    else:
        return text

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\{", text):
        try:
            parsed, end = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append((end, -match.start(), text[match.start():end]))
    return max(candidates)[2] if candidates else text


def normalize_score_result(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse and validate one v26 LLM result without raising on bad output."""
    try:
        parsed = json.loads(_json_text(raw)) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error(f"json_parse_error: {exc}")

    if not isinstance(parsed, dict):
        return _error("response_not_object")

    required = {
        "reject",
        "content_type",
        "content_type_confidence",
        "dims",
        "marketing",
        "veto",
        "uncertainty",
        "value_path",
    }
    missing = sorted(required - parsed.keys())
    if missing:
        return _error("missing_required_fields: " + ",".join(missing))

    if type(parsed["reject"]) is not bool:
        return _error("invalid_reject")

    content_type = parsed["content_type"]
    if content_type not in VALID_CONTENT_TYPES:
        return _error(f"invalid_content_type: {content_type}")

    content_type_confidence = parsed["content_type_confidence"]
    if (
        isinstance(content_type_confidence, bool)
        or not isinstance(content_type_confidence, (int, float))
        or not 0 <= content_type_confidence <= 1
    ):
        return _error("invalid_content_type_confidence")

    dims = parsed["dims"]
    if not isinstance(dims, dict) or set(dims) != set(DIMENSIONS):
        return _error("invalid_dims")
    for name in DIMENSIONS:
        if type(dims[name]) is not int or not 0 <= dims[name] <= 3:
            return _error(f"invalid_dimension: {name}")

    marketing = parsed["marketing"]
    if type(marketing) is not int or not 0 <= marketing <= 3:
        return _error("invalid_marketing")
    if parsed["veto"] not in VALID_VETOES:
        return _error(f"invalid_veto: {parsed['veto']}")
    if parsed["uncertainty"] not in VALID_UNCERTAINTIES:
        return _error(f"invalid_uncertainty: {parsed['uncertainty']}")
    if parsed["value_path"] not in VALID_VALUE_PATHS:
        return _error(f"invalid_value_path: {parsed['value_path']}")

    normalized = dict(parsed)
    normalized["dims"] = dict(dims)
    if marketing == 3:
        normalized["veto"] = "marketing"
    return normalized


def compute_score10(result: dict[str, Any]) -> float | None:
    """Compose a normalized v26 result into a capped 0-10 score."""
    if result.get("error") or result.get("reject"):
        return None

    profile_id = result["content_type"]
    if result["content_type_confidence"] < 0.6:
        profile_id = "general"
    weights, marketing_penalty_cap = SCORE_PROFILES[profile_id]
    dims = result["dims"]

    score = sum(
        weight * dims[name] / 3
        for name, weight in zip(DIMENSIONS, weights)
    ) / 100 * 10
    score -= result["marketing"] / 3 * marketing_penalty_cap

    if dims["authority"] == 0:
        score = min(score, 4.9)
    if dims["audience_fit"] <= 1:
        score = min(score, 4.9)
    if dims["novelty"] == 0:
        score = min(score, 5.9)

    return round(max(0.0, min(10.0, score)), 1)


def is_flag_bearer(
    result: dict[str, Any],
    score10: float | None,
    threshold: float,
) -> bool:
    """Return whether an item can carry its cluster across the threshold."""
    return bool(
        score10 is not None
        and score10 >= threshold
        and result.get("veto") == "none"
        and result.get("uncertainty") != "unverified_major_claim"
        and not result.get("reject")
    )
