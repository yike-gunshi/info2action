"""Display-ready read model helpers for action detail payloads."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

READ_MODEL_VERSION = 1
DETAIL_PAYLOAD_FIELDS = (
    "steps",
    "source_items",
    "source_item_count",
    "execution_status",
)
LIST_PREFETCH_PER_DIRECTION = 20
LIST_PREFETCH_TOTAL = 24


def viewer_scope_for(*, can_view_all: bool) -> str:
    return "admin" if can_view_all else "owner"


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def parse_source_item_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*")


def extract_action_steps(action: dict[str, Any], *, limit: int | None = None) -> list[str]:
    raw_steps = action.get("steps")
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except (TypeError, ValueError):
            raw_steps = None
    if isinstance(raw_steps, list):
        steps = [str(step).strip() for step in raw_steps if str(step or "").strip()]
    else:
        prompt = str(action.get("prompt") or "").strip()
        steps = []
        for line in prompt.splitlines():
            text = _BULLET_RE.sub("", line).strip()
            if text:
                steps.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for step in steps:
        compact = " ".join(step.split())
        if not compact or compact in seen:
            continue
        seen.add(compact)
        deduped.append(compact)
    return deduped if limit is None else deduped[:limit]


def _normalize_source_item(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(item.get("id") or ""),
        "platform": item.get("platform") or "",
        "title": item.get("title") or "",
        "ai_summary": item.get("ai_summary") or "",
        "url": item.get("url") or "",
        "referenced_urls": item.get("referenced_urls") or [],
    }
    if not isinstance(out["referenced_urls"], list):
        out["referenced_urls"] = []
    return out


def build_action_detail_payload(
    action: dict[str, Any],
    *,
    source_items: list[dict[str, Any]] | None = None,
    execution_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(action)
    source_ids = parse_source_item_ids(payload.get("source_item_ids"))
    normalized_sources = [_normalize_source_item(item) for item in (source_items or [])]

    payload["source_item_ids"] = source_ids
    payload["source_items"] = normalized_sources
    payload["source_item_count"] = len(normalized_sources)
    payload["steps"] = extract_action_steps(payload)
    payload["type"] = payload.get("type") or payload.get("action_type") or "investigate"
    if execution_status is not None:
        payload["execution_status"] = execution_status
    payload.pop("user_id", None)
    return payload


def merge_action_with_detail_payload(
    action: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge display-ready detail fields into a fresh action row.

    The action row wins for volatile fields like status/title/priority, while
    detail-only fields keep the prebuilt modal payload complete.
    """
    if not isinstance(payload, dict):
        return dict(action)
    merged = dict(payload)
    merged.update({key: value for key, value in action.items() if value is not None})
    for key in DETAIL_PAYLOAD_FIELDS:
        if key in payload:
            merged[key] = payload[key]
    return merged


def select_list_prefetch_action_ids(
    actions: list[dict[str, Any]],
    *,
    per_direction: int = LIST_PREFETCH_PER_DIRECTION,
    total: int = LIST_PREFETCH_TOTAL,
) -> list[str]:
    """Choose the action ids that are visible before "展开更多" in lane view."""
    grouped: dict[str, list[str]] = {}
    direction_order: list[str] = []
    counts: dict[str, int] = {}
    for action in actions:
        action_id = action.get("id")
        if not action_id:
            continue
        direction = str(action.get("direction") or "_uncategorized")
        if direction not in grouped:
            grouped[direction] = []
            direction_order.append(direction)
        if counts.get(direction, 0) >= per_direction:
            continue
        counts[direction] = counts.get(direction, 0) + 1
        grouped[direction].append(str(action_id))

    out: list[str] = []
    offset = 0
    while len(out) < total:
        added = False
        for direction in direction_order:
            ids = grouped.get(direction) or []
            if offset >= len(ids):
                continue
            out.append(ids[offset])
            added = True
            if len(out) >= total:
                break
        if not added:
            break
        offset += 1
    return out
