"""Privacy-safe manifests describing what summary inputs can use."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


_PLATFORM_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
}


def _detail(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _reference_entries(detail: dict[str, Any]) -> list[Any]:
    values = detail.get("referenced_urls")
    return values if isinstance(values, list) else []


def _reference_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("expanded_url", "url", "href"):
            url = str(value.get(key) or "").strip()
            if url:
                return url
    return ""


def _external_urls(detail: dict[str, Any]) -> list[str]:
    candidates: list[Any] = []
    for key in ("urls", "referenced_urls"):
        values = detail.get(key)
        if isinstance(values, list):
            candidates.extend(values)
    seen: set[str] = set()
    urls: list[str] = []
    for candidate in candidates:
        url = _reference_url(candidate)
        host = (urlparse(url).hostname or "").lower()
        if (
            not url.startswith(("http://", "https://"))
            or not host
            or host in _PLATFORM_HOSTS
            or any(host.endswith(f".{platform}") for platform in _PLATFORM_HOSTS)
            or url in seen
        ):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def build_item_summary_evidence(
    item: dict[str, Any],
    *,
    body_is_usable: bool | None = None,
) -> dict[str, Any]:
    """Describe input presence and fallback without copying content or URLs."""
    detail = _detail(item.get("detail_json"))
    references = _reference_entries(detail)
    full_text_count = sum(
        1
        for ref in references
        if isinstance(ref, dict)
        and isinstance(ref.get("full_text"), str)
        and len(ref["full_text"]) > 100
    )
    has_body = bool(str(item.get("content") or "").strip())
    if body_is_usable is None:
        body_is_usable = has_body
    if body_is_usable:
        fallback = "none"
    elif str(item.get("ai_summary") or "").strip():
        fallback = "ai_summary"
    else:
        fallback = "empty"
    quoted = detail.get("quotedTweet")
    return {
        "item_id": str(item.get("id") or ""),
        "has_body": has_body,
        "has_asr": bool(str(item.get("asr_text") or "").strip()),
        "has_readme": bool(str(detail.get("readme") or "").strip()),
        "has_quote": isinstance(quoted, dict) and bool(str(quoted.get("text") or "").strip()),
        "referenced_url_count": len(references),
        "referenced_full_text_count": full_text_count,
        # build_item_content currently selects the first sufficiently long external body.
        "selected_external_text_count": min(full_text_count, 1),
        "fallback": fallback,
    }


def build_cluster_summary_evidence(
    prompt_rows: list[Any],
    *,
    max_members: int = 20,
    singleton_fast_path: bool = False,
) -> dict[str, Any]:
    """Describe rows selected for a cluster prompt.

    Callers must pass rows in the same order used for prompt construction.
    The helper enforces the public 20-member and 5-external-link caps.
    """
    selected = list(prompt_rows)[: max(0, min(int(max_members), 20))]
    members = []
    for row in selected:
        detail = _detail(_row_get(row, "detail_json"))
        has_body = bool(str(_row_get(row, "content") or "").strip())
        has_summary = bool(str(_row_get(row, "ai_summary") or "").strip())
        members.append(
            {
                "item_id": str(_row_get(row, "id") or ""),
                "has_body": has_body,
                "has_asr": bool(str(_row_get(row, "asr_text") or "").strip()),
                "has_readme": bool(str(detail.get("readme") or "").strip()),
                "has_quote": isinstance(detail.get("quotedTweet"), dict),
                "external_url_count_selected": min(len(_external_urls(detail)), 5),
                "fallback": "none" if has_body else ("ai_summary" if has_summary else "empty"),
            }
        )
    singleton = len(selected) == 1
    return {
        "member_count_total": len(prompt_rows),
        "member_count_selected": len(selected),
        "members": members,
        "singleton": singleton,
        "singleton_fast_path": bool(singleton and singleton_fast_path),
    }
