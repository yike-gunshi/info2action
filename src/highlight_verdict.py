"""Item-level Highlights verdict policy.

The LLM gives a coarse verdict and value path. Code only normalizes the output
and applies the positive-borderline inclusion rule that feeds the read model.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from prompt_loader import load_prompt


PROMPT_FILE = "14_item_verdict_v3_1.md"
PROMPT_VERSION = "item_verdict_v3_7_2_ai_toolchain_scope_2026_06_17"

VALID_VERDICTS = {"featured", "borderline", "drop"}
VALID_VALUE_PATHS = {"substantive", "major_event", "lead_value", "none"}
VALID_UNCERTAINTIES = {"none", "thin_detail", "needs_source", "unverified_major_claim"}
VALID_AI_RELEVANCE = {"yes", "no"}
POSITIVE_BORDERLINE_PATHS = {"substantive", "major_event", "lead_value"}
VALID_SCORE_KEYS = ("importance", "novelty", "credibility", "substance", "actionability")


def load_system_prompt() -> str:
    prompt = load_prompt(PROMPT_FILE)
    if not prompt:
        raise FileNotFoundError(f"missing prompt file: {PROMPT_FILE}")
    return prompt


def _strip_json_text(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _coerce_score(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 1
    return max(1, min(3, number))


def _coerce_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _pending_result(error: str, *, raw_text: str | None = None) -> dict[str, Any]:
    return {
        "highlight_verdict": None,
        "highlight_value_path": None,
        "highlight_uncertainty": None,
        "highlight_include_in_highlights": False,
        "highlight_reason": "",
        "highlight_scores": {},
        "highlight_ai_relevant": None,
        "highlight_spam": None,
        "highlight_confidence": None,
        "highlight_prompt_version": PROMPT_VERSION,
        "highlight_scored_at": datetime.now(timezone.utc).isoformat(),
        "cluster_verdict": "pending",
        "highlight_last_error": error,
        "raw_text": raw_text,
    }


def _cluster_verdict(verdict: str, value_path: str, uncertainty: str) -> str:
    if verdict == "featured":
        return "featured"
    if verdict == "drop":
        return "drop"
    if value_path in POSITIVE_BORDERLINE_PATHS and uncertainty != "unverified_major_claim":
        return "positive_borderline"
    return "risk_borderline"


def normalize_verdict_result(raw: str | dict[str, Any]) -> dict[str, Any]:
    raw_text: str | None = None
    try:
        if isinstance(raw, str):
            raw_text = raw
            obj = json.loads(_strip_json_text(raw))
        else:
            obj = dict(raw)
    except Exception as exc:
        return _pending_result(f"json_parse_error: {exc}", raw_text=raw_text)
    if not isinstance(obj, dict):
        return _pending_result("response_not_object", raw_text=raw_text)

    missing = [
        key
        for key in ("verdict", "value_path", "uncertainty")
        if str(obj.get(key) or "").strip() == ""
    ]
    if missing:
        return _pending_result(
            "missing_required_fields: " + ",".join(missing),
            raw_text=raw_text,
        )

    verdict = str(obj.get("verdict") or "").strip().lower()
    value_path = str(obj.get("value_path") or "").strip().lower()
    uncertainty = str(obj.get("uncertainty") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        return _pending_result(f"invalid_verdict: {verdict}", raw_text=raw_text)
    if value_path not in VALID_VALUE_PATHS:
        return _pending_result(f"invalid_value_path: {value_path}", raw_text=raw_text)
    if uncertainty not in VALID_UNCERTAINTIES:
        return _pending_result(f"invalid_uncertainty: {uncertainty}", raw_text=raw_text)

    scores_raw = obj.get("scores") if isinstance(obj.get("scores"), dict) else {}
    scores = {key: _coerce_score(scores_raw.get(key)) for key in VALID_SCORE_KEYS}
    cluster_verdict = _cluster_verdict(verdict, value_path, uncertainty)
    include = cluster_verdict in {"featured", "positive_borderline"}
    spam = obj.get("spam")
    try:
        spam_value = max(1, min(3, int(round(float(spam)))))
    except (TypeError, ValueError):
        spam_value = None
    ai_relevant = str(obj.get("ai_relevant") or "").strip().lower()
    if ai_relevant not in VALID_AI_RELEVANCE:
        ai_relevant = None

    return {
        "highlight_verdict": verdict,
        "highlight_value_path": value_path,
        "highlight_uncertainty": uncertainty,
        "highlight_include_in_highlights": include,
        "highlight_reason": str(obj.get("reason") or "").strip()[:1000],
        "highlight_scores": scores,
        "highlight_ai_relevant": ai_relevant,
        "highlight_spam": spam_value,
        "highlight_confidence": _coerce_confidence(obj.get("confidence")),
        "highlight_prompt_version": PROMPT_VERSION,
        "highlight_scored_at": datetime.now(timezone.utc).isoformat(),
        "cluster_verdict": cluster_verdict,
        "highlight_last_error": None,
        "raw_text": raw_text,
    }


def build_item_content(item: dict[str, Any], *, content_char_limit: int = 4000) -> str:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or item.get("ai_summary") or "").strip()
    content = re.sub(r"\s+", " ", content)[:content_char_limit]
    parts = [
        f"id: {item.get('id')}",
        f"title: {title}",
        f"platform: {item.get('platform') or ''}",
        f"source: {item.get('source') or item.get('author_name') or ''}",
        f"url: {item.get('url') or ''}",
        f"category: {item.get('category') or item.get('ai_category') or ''}",
        f"summary_or_content: {content}",
    ]
    return "\n".join(parts)
