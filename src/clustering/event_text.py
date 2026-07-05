"""Event embedding text construction (v15.1 / Stage 0 upgrade).

Builds the structured-first text fed into the embedding provider for event
clustering. Replaces the old `content > ai_summary > title` fallback chain
that capped at 3800 chars.

Field priority (V2.3 §3 + feature-spec R1.1):
  1. title
  2. ai_summary
  3. ai_key_points (JSON array → bullet list)
  4. ai_keywords
  5. ai_category / content_type
  6. content / asr_text_cn / asr_text (fallback / supplement)

Hard constraints (feature-spec R1.1 / R1.2 / R1.3 + V2.3 §0.7):
  - Total length SHALL be <= MAX_CHARS (10000)
  - comments_json SHALL NOT be included
  - When ai_summary OR ai_key_points missing: still produce text using
    title + content/transcript fallback, set used_fallback_content=True
  - Long content (#6) is truncated head+tail (head 70% + tail 30% with
    `[...trimmed...]` marker), NOT head-only — to preserve event tail signals
    (outcome / conclusion).

Returns `(text, metadata)` where metadata records what was filled in (for
observability logging in pipeline.py).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

MAX_CHARS = 10000

# Reserve at least this many chars for content/transcript section even when
# structured fields are large. Keeps the fallback meaningful.
_MIN_CONTENT_BUDGET = 600

_TRIM_MARKER = '\n[...trimmed...]\n'

# Common platform-prefix patterns the title cleaner strips (e.g., "[YouTube]
# Foo", "【B站】Bar"). Conservative: only strips bracketed prefix at start.
_PLATFORM_PREFIX_RE = re.compile(
    r'^\s*[\[\【][^\]\】]{1,32}[\]\】]\s*'
)


def _as_str(val: Any) -> str:
    if val is None:
        return ''
    if isinstance(val, (bytes, bytearray)):
        try:
            return val.decode('utf-8', errors='ignore')
        except Exception:
            return ''
    return str(val).strip()


def _clean_title(raw: str) -> str:
    """Strip leading platform brackets but keep the human-readable core."""
    if not raw:
        return ''
    cleaned = _PLATFORM_PREFIX_RE.sub('', raw)
    return cleaned.strip()


def _parse_key_points(raw: Any) -> list[str]:
    """Parse ai_key_points (TEXT column storing JSON array). Tolerant.

    Returns list of non-empty stripped strings. Returns [] on:
    - None / empty
    - JSON decode failure
    - non-list shapes
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        candidates = raw
    else:
        s = _as_str(raw)
        if not s or s in ('[]', 'null'):
            return []
        try:
            candidates = json.loads(s)
        except (json.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(candidates, list):
            return []
    out = []
    for c in candidates:
        cs = _as_str(c)
        if cs:
            out.append(cs)
    return out


def _get(item: Mapping[str, Any] | Any, key: str) -> Any:
    """Read a field from dict / sqlite3.Row uniformly."""
    if item is None:
        return None
    # sqlite3.Row supports __getitem__ but raises IndexError for missing keys
    # in some sqlite versions; check keys() if available.
    try:
        keys = item.keys() if hasattr(item, 'keys') else None
    except Exception:
        keys = None
    if keys is not None and key not in keys:
        return None
    try:
        return item[key]
    except (KeyError, IndexError, TypeError):
        return None


def _truncate_head_tail(text: str, budget: int) -> str:
    """Keep first 70% + marker + last 30% of `budget`."""
    if budget <= len(_TRIM_MARKER) + 8:
        return text[:max(0, budget)]
    if len(text) <= budget:
        return text
    inner = budget - len(_TRIM_MARKER)
    head_n = int(inner * 0.7)
    tail_n = inner - head_n
    head = text[:head_n].rstrip()
    tail = text[-tail_n:].lstrip() if tail_n > 0 else ''
    out = head + _TRIM_MARKER + tail
    # Defensive: if rounding produced over-budget output, hard-cap.
    if len(out) > budget:
        out = out[:budget]
    return out


def build_event_embedding_text(item: Mapping[str, Any] | Any) -> tuple[str, dict]:
    """Construct structured-first embedding input text for one item.

    Args:
        item: dict / sqlite3.Row with optional fields:
              title, ai_summary, ai_key_points (JSON str), ai_keywords,
              ai_category, content_type, content, asr_text_cn, asr_text

    Returns:
        (text, metadata) where metadata = {
            has_ai_summary: bool,
            has_ai_key_points: bool,
            has_ai_keywords: bool,
            used_fallback_content: bool,
            embedding_text_chars: int,
        }

    Behavior:
      - Always non-empty (falls back to title or item id placeholder if all empty).
      - comments_json never read (V2.3 §0.7 hard constraint).
      - Total length <= MAX_CHARS.
      - When ai_summary or ai_key_points empty → used_fallback_content=True.
    """
    title = _clean_title(_as_str(_get(item, 'title')))
    ai_summary = _as_str(_get(item, 'ai_summary'))
    key_points = _parse_key_points(_get(item, 'ai_key_points'))
    ai_keywords = _as_str(_get(item, 'ai_keywords'))
    ai_category = _as_str(_get(item, 'ai_category'))
    content_type = _as_str(_get(item, 'content_type'))

    # Content fallback chain: prefer translated ASR > raw ASR > content.
    raw_content = (
        _as_str(_get(item, 'content'))
        or _as_str(_get(item, 'asr_text_cn'))
        or _as_str(_get(item, 'asr_text'))
    )

    has_ai_summary = bool(ai_summary)
    has_ai_key_points = bool(key_points)
    has_ai_keywords = bool(ai_keywords)
    used_fallback_content = not (has_ai_summary and has_ai_key_points)

    # Build structured prefix (priority 1-5). These are SHALL-keep per R1.3.
    parts: list[str] = []
    if title:
        parts.append(f'标题: {title}')
    cat_bits = []
    if ai_category:
        cat_bits.append(f'分类: {ai_category}')
    if content_type:
        cat_bits.append(f'内容类型: {content_type}')
    if cat_bits:
        parts.append(' | '.join(cat_bits))
    if ai_summary:
        parts.append(f'AI摘要:\n{ai_summary}')
    if key_points:
        bullets = '\n'.join(f'- {kp}' for kp in key_points)
        parts.append(f'结构化要点:\n{bullets}')
    if ai_keywords:
        parts.append(f'关键词: {ai_keywords}')

    structured = '\n\n'.join(parts).strip()

    # Hard cap structured part defensively (extreme summary/keypoints could
    # in theory exceed MAX_CHARS - _MIN_CONTENT_BUDGET; truncate at boundary).
    structured_budget = MAX_CHARS - _MIN_CONTENT_BUDGET
    if len(structured) > structured_budget:
        structured = structured[:structured_budget].rstrip()

    # Compute remaining budget for content/transcript.
    sep = '\n\n正文/转写:\n' if structured else '正文/转写:\n'
    remaining = MAX_CHARS - len(structured) - len(sep)

    if raw_content and remaining > 0:
        content_section = _truncate_head_tail(raw_content, remaining)
        body = (structured + sep + content_section) if structured else (sep + content_section)
    else:
        body = structured

    # Final hard cap (defensive).
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]

    # If everything is empty (no title, no summary, no content) — fallback to
    # placeholder to keep embedding provider happy. used_fallback_content stays
    # True per the missing-summary check above.
    if not body.strip():
        item_id = _as_str(_get(item, 'id')) or 'unknown'
        body = f'(empty item {item_id})'

    metadata = {
        'has_ai_summary': has_ai_summary,
        'has_ai_key_points': has_ai_key_points,
        'has_ai_keywords': has_ai_keywords,
        'used_fallback_content': used_fallback_content,
        'embedding_text_chars': len(body),
    }
    return body, metadata
