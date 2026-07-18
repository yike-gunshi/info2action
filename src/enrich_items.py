#!/usr/bin/env python3
"""Unified item enrichment: summary + scoring in one MiniMax request."""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import ai_provider_guard
import ai_bolding
import db
import highlight_score_v26
import highlight_verdict
import remote_db
import generate_summaries
import score_items
from env_utils import load_project_env
from prompt_loader import load_prompt


CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
CLASSIFICATION_PATH = os.path.join(BASE_DIR, "config", "classification.json")
AI_RETRY_READY_SQL = "(ai_retry_after IS NULL OR ai_retry_after <= datetime('now'))"
RUN_ITEMS_SCOPE_TAGGED = "tagged"
RUN_ITEMS_SCOPE_INSERTED = "inserted"
RUN_ITEMS_SCOPE_CHOICES = (RUN_ITEMS_SCOPE_TAGGED, RUN_ITEMS_SCOPE_INSERTED)

# v16.0: GitHub README 拼接到 enrich user prompt 时的二次截断阈值(chars)。
# fetch_feeds 抓取阶段已按 readme_max_tokens(默认 200k tokens ≈ 800k bytes)初截过,
# 这里在 LLM 输入阶段再压一次,留出 buffer 给 title/description/metadata,
# 总输入 ≈ readme_chars + 5000 buffer < ai_summary.max_tokens(100000)。
GITHUB_README_ENRICH_MAX_CHARS = 80_000
GITHUB_README_V26_MAX_CHARS = 8_000
HIGHLIGHT_V26_PASS2_DAILY_CAP_DEFAULT = 500

logger = logging.getLogger(__name__)
_SSL_CTX = ssl.create_default_context()
_DEFAULT_MINIMAX_CHAT_BASE = "https://api.minimaxi.com/anthropic/v1"
_DEFAULT_MINIMAX_CHAT_MODEL = "MiniMax-M3"
MINIMAX_429_MAX_RETRIES = 8
MINIMAX_429_BASE_DELAY = 2.0
MINIMAX_429_MAX_DELAY = 60.0
ENRICH_RETRY_BACKLOG_LIMIT_DEFAULT = 500
ENRICH_RETRY_LOOKBACK_HOURS_DEFAULT = 72.0
REMOTE_DB_TRANSIENT_ATTEMPTS_DEFAULT = 3
REMOTE_DB_TRANSIENT_MAX_DELAY_SEC = 5.0
REMOTE_DB_TRANSIENT_ERROR_HINTS = (
    "edbhandlerexited",
    "connection to database closed",
    "server closed the connection",
    "connection closed",
    "pool checkout",
    "checkout timeout",
    "connection timeout",
    "connection/query failed",
    "connection failed",
    "terminating connection",
)
_PLATFORM_LABELS = {
    "twitter": "X",
    "x": "X",
    "xiaohongshu": "小红书",
    "bilibili": "B站",
    "lingowhale": "公众号",
    "reddit": "Reddit",
    "hackernews": "Hacker News",
    "github": "GitHub",
    "rss": "RSS",
    "waytoagi": "waytoagi",
}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _retry_window_start_iso() -> str | None:
    hours = _positive_float_env(
        "INFO2ACTION_ENRICH_RETRY_LOOKBACK_HOURS",
        ENRICH_RETRY_LOOKBACK_HOURS_DEFAULT,
    )
    if hours <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _remote_db_transient_attempts() -> int:
    return max(
        1,
        _positive_int_env(
            "INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS",
            REMOTE_DB_TRANSIENT_ATTEMPTS_DEFAULT,
        ),
    )


def _remote_db_retry_delay(attempt: int) -> float:
    return min(REMOTE_DB_TRANSIENT_MAX_DELAY_SEC, 0.5 * max(1, attempt))


def _remote_db_error_summary(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip()[:240]


def _is_transient_remote_db_error(exc: Exception) -> bool:
    if not isinstance(exc, remote_db.RemoteDBError):
        return False
    msg = str(exc).lower()
    return any(hint in msg for hint in REMOTE_DB_TRANSIENT_ERROR_HINTS)


def _with_remote_db_transient_retry(operation: str, fn):
    attempts = _remote_db_transient_attempts()
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except remote_db.RemoteDBError as exc:
            if not _is_transient_remote_db_error(exc):
                raise
            summary = _remote_db_error_summary(exc)
            if attempt >= attempts:
                print(
                    f"remote_db_transient_exhausted operation={operation} "
                    f"attempts={attempts} error={summary}",
                    flush=True,
                )
                raise
            print(
                f"remote_db_transient_retry operation={operation} "
                f"attempt={attempt}/{attempts} error={summary}",
                flush=True,
            )
            time.sleep(_remote_db_retry_delay(attempt))
    raise RuntimeError(f"unreachable remote DB retry state for {operation}")


def query_pending_enrichment_items_remote_with_retry(**kwargs):
    return _with_remote_db_transient_retry(
        "query_pending_enrichment_items_remote",
        lambda: remote_db.query_pending_enrichment_items_remote(**kwargs),
    )


def _merge_items_by_id(
    primary: list[dict],
    retry_items: list[dict],
    *,
    max_new: int | None = None,
) -> list[dict]:
    seen = {item["id"] for item in primary}
    merged = list(primary)
    added = 0
    for item in retry_items:
        if item["id"] in seen:
            continue
        if max_new is not None and added >= max_new:
            break
        seen.add(item["id"])
        merged.append(item)
        added += 1
    return merged


class MiniMaxRateLimitGate:
    """Process-local shared gate so concurrent workers back off together."""

    def __init__(
        self,
        *,
        min_interval: float = 0.0,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
        jitter_fn=random.uniform,
    ):
        self._min_interval = max(0.0, float(min_interval or 0.0))
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._jitter = jitter_fn
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._monotonic()
                delay = self._next_allowed_at - now
                if delay <= 0:
                    if self._min_interval:
                        self._next_allowed_at = now + self._min_interval
                    return
            self._sleep(delay)

    def pause_after_429(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and retry_after > 0:
            delay = min(MINIMAX_429_MAX_DELAY, retry_after)
        else:
            delay = min(MINIMAX_429_MAX_DELAY, MINIMAX_429_BASE_DELAY * (2 ** max(0, attempt)))
            delay += self._jitter(0, min(1.0, delay * 0.1))
        with self._lock:
            self._next_allowed_at = max(self._next_allowed_at, self._monotonic() + delay)
        return delay


_MINIMAX_RATE_GATE = MiniMaxRateLimitGate()
_HIGHLIGHT_V26_PASS2_LOCK = threading.Lock()
_HIGHLIGHT_V26_PASS2_USAGE = {"day": None, "count": 0}


def _claim_highlight_v26_pass2_slot() -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    daily_cap = _positive_int_env(
        "INFO2ACTION_HIGHLIGHT_V26_PASS2_DAILY_CAP",
        HIGHLIGHT_V26_PASS2_DAILY_CAP_DEFAULT,
    )
    with _HIGHLIGHT_V26_PASS2_LOCK:
        if _HIGHLIGHT_V26_PASS2_USAGE.get("day") != day:
            _HIGHLIGHT_V26_PASS2_USAGE.update(day=day, count=0)
        if _HIGHLIGHT_V26_PASS2_USAGE["count"] >= daily_cap:
            return False
        _HIGHLIGHT_V26_PASS2_USAGE["count"] += 1
        return True


def _parse_retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, AttributeError):
        return None


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _parse_minimax_reset_seconds(body: str) -> float | None:
    # Source of truth lives in ai_provider_guard so MiniMax chat callers share
    # the same Token Plan reset parsing.
    return ai_provider_guard.parse_minimax_reset_seconds(body)


def _parse_429_wait_seconds(exc: urllib.error.HTTPError) -> float | None:
    retry_after = _parse_retry_after_seconds(exc)
    body_wait = _parse_minimax_reset_seconds(_read_http_error_body(exc))
    if body_wait is None:
        return retry_after
    if retry_after is None:
        return body_wait
    return max(retry_after, body_wait)


def _display_platform(platform: str | None) -> str:
    value = str(platform or "").strip()
    if not value:
        return "全部平台"
    return _PLATFORM_LABELS.get(value.lower(), value)


def _chunk_platform(chunk: list[dict]) -> str:
    labels = []
    for item in chunk:
        label = _display_platform(item.get("platform"))
        if label != "全部平台" and label not in labels:
            labels.append(label)
    if not labels:
        return "全部平台"
    if len(labels) == 1:
        return labels[0]
    return "混合平台"


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_classification() -> dict:
    with open(CLASSIFICATION_PATH, "r") as f:
        return json.load(f)


def resolve_minimax_runtime_config(ai_config: dict | None) -> tuple[str, str, str]:
    """Resolve MiniMax chat runtime config for batch enrichment.

    The local `.env` is the authoritative place for secrets. `config.json`
    still carries legacy defaults and non-secret tuning, so keep it as fallback
    while ensuring a rotated `MINIMAX_API_KEY` takes effect immediately.
    """
    ai_config = ai_config or {}
    project_env = load_project_env(BASE_DIR)
    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or project_env.get("MINIMAX_API_KEY")
        or ai_config.get("api_key")
        or ""
    ).strip()
    api_base = (
        os.environ.get("MINIMAX_API_BASE")
        or project_env.get("MINIMAX_API_BASE")
        or ai_config.get("api_base")
        or _DEFAULT_MINIMAX_CHAT_BASE
    ).strip().rstrip("/")
    model = (
        os.environ.get("MINIMAX_MODEL")
        or project_env.get("MINIMAX_MODEL")
        or ai_config.get("model")
        or _DEFAULT_MINIMAX_CHAT_MODEL
    ).strip()
    return api_key, api_base, model


def build_category_block(categories: list[dict]) -> str:
    """Inject L1 + L2 hierarchy for v4.0 prompt.

    Format:
        ## L1: <id> (<name>)
        定位: <description>
        边界规则: <boundary_rule>
        L2:
          - <l2_id> (<l2_name>) 例: a, b, c
          - ...
    """
    lines = []
    for cat in categories:
        cid = cat.get("id", "")
        name = cat.get("name", "")
        desc = cat.get("description", "")
        rule = cat.get("boundary_rule", "")
        subs = cat.get("subcategories") or []
        lines.append(f"## L1: {cid}({name})")
        if desc:
            lines.append(f"定位: {desc}")
        if rule:
            lines.append(f"边界规则: {rule}")
        if subs:
            lines.append("L2:")
            for sub in subs:
                sid = sub.get("id", "")
                sname = sub.get("name", "")
                examples = sub.get("examples") or []
                ex_text = f" 例: {', '.join(examples[:6])}" if examples else ""
                lines.append(f"  - {sid}({sname}){ex_text}")
        lines.append("")  # blank line between L1
    return "\n".join(lines)


def build_subcategory_map(categories: list[dict]) -> dict[str, set[str]]:
    """Map L1 id -> set of valid L2 ids."""
    return {
        cat.get("id", ""): {sub.get("id", "") for sub in (cat.get("subcategories") or []) if sub.get("id")}
        for cat in categories
    }


def build_system_prompt(categories: list[dict]) -> str:
    category_block = build_category_block(categories)
    prompt = load_prompt("03_enrich_item.md", categories=category_block)
    if prompt:
        return prompt
    return f"""请一次性输出摘要、分类和评分。分类体系：
{category_block}
只输出 JSON。"""


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


def _strip_json_array_text(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _valid_dimensions(content_type: str, raw_dimensions: object) -> dict:
    if not isinstance(raw_dimensions, dict):
        raw_dimensions = {}
    expected = score_items._TYPE_DIMENSIONS.get(content_type, set())
    dimensions = {}
    for dim_name in expected:
        value = raw_dimensions.get(dim_name, 2)
        try:
            dimensions[dim_name] = max(1, min(3, int(value)))
        except (TypeError, ValueError):
            dimensions[dim_name] = 2
    return dimensions


def _coerce_str_list(value, max_items: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(v).strip() for v in value if str(v).strip()]
    if max_items is not None:
        out = out[:max_items]
    return out


def parse_enrichment_response(
    raw: str,
    valid_category_ids: list[str],
    valid_l2_by_l1: dict[str, set[str]] | None = None,
) -> dict:
    """Parse v4.0 LLM output. Falls back to v1.0 (single category) if LLM returns old format."""
    obj = json.loads(_strip_json_text(raw))
    if not isinstance(obj, dict):
        raise ValueError("enrichment response is not an object")

    summary = str(obj.get("summary") or "").strip()
    key_points = obj.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = []
    normalized_points = []
    for point in key_points:
        if not isinstance(point, dict):
            continue
        title = str(point.get("title") or "").strip()
        points = point.get("points") or []
        if not isinstance(points, list):
            points = []
        normalized_points.append({
            "title": title,
            "points": [
                str(p).strip()
                for p in points
                if str(p).strip()
            ],
        })

    # v4.0: L1 multi-tag (categories array); fallback to v1.0 single category
    raw_categories = obj.get("categories")
    if isinstance(raw_categories, list):
        categories = [str(c).strip().lower() for c in raw_categories if str(c).strip()]
    else:
        legacy_cat = str(obj.get("category") or "").strip().lower()
        categories = [legacy_cat] if legacy_cat else []
    # Validate against L1 whitelist; drop invalid
    categories = [c for c in categories if c in valid_category_ids]
    # Cap at 3 (per Q19)
    categories = categories[:3] if categories else []
    primary_category = categories[0] if categories else None

    # multi_l1_reason required when len > 1
    multi_l1_reason = obj.get("multi_l1_reason")
    if multi_l1_reason is not None:
        multi_l1_reason = str(multi_l1_reason).strip()[:500] or None
    if len(categories) > 1 and not multi_l1_reason:
        multi_l1_reason = "(LLM did not provide multi_l1_reason)"

    # v4.0: L2 multi-tag, validate against parent L1's L2 whitelist
    raw_subs = obj.get("subcategories") or []
    if isinstance(raw_subs, list):
        subcategories_input = [str(s).strip().lower() for s in raw_subs if str(s).strip()]
    else:
        subcategories_input = []
    subcategories = []
    if valid_l2_by_l1 is not None and categories:
        allowed = set()
        for c in categories:
            allowed.update(valid_l2_by_l1.get(c, set()))
        subcategories = [s for s in subcategories_input if s in allowed]
    else:
        subcategories = subcategories_input

    # ai_extracted (skills / models / event_card)
    raw_extracted = obj.get("ai_extracted") or {}
    if not isinstance(raw_extracted, dict):
        raw_extracted = {}
    ai_extracted = {
        "skills": _coerce_str_list(raw_extracted.get("skills"), max_items=10),
        "models": _coerce_str_list(raw_extracted.get("models"), max_items=10),
        "event_card": raw_extracted.get("event_card") if isinstance(raw_extracted.get("event_card"), dict) else None,
    }

    # visible flag (filter layer)
    visible_raw = obj.get("visible")
    if isinstance(visible_raw, bool):
        visible = visible_raw
    elif isinstance(visible_raw, (int, float)):
        visible = bool(visible_raw)
    else:
        visible = True  # default visible

    # other_reason / suggested_new_subcategory
    other_reason = obj.get("other_reason")
    other_reason = str(other_reason).strip()[:500] if other_reason else None
    suggested_new = obj.get("suggested_new_subcategory")
    suggested_new = str(suggested_new).strip()[:200] if suggested_new else None

    # Auto-fill: 如果 LLM 选了任一 'other' 但漏填 other_reason,从 reason 字段兜底
    # (LLM 在 L2-level other 时容易漏,见 dry-run 验证发现的 Bug 1)
    has_other_l1 = "other" in categories
    has_other_l2 = any(s == "other" or s.endswith("_other") for s in subcategories)
    if (has_other_l1 or has_other_l2) and not other_reason:
        fallback_reason = str(obj.get("reason") or "").strip()
        if fallback_reason:
            other_reason = f"[auto-filled from reason] {fallback_reason}"[:500]
        else:
            other_reason = "(LLM 漏填 other_reason; 待 prompt 迭代)"
    if other_reason and not suggested_new:
        suggested_new = "(待 LLM 给建议)"

    content_type = str(obj.get("content_type") or "post").strip().lower()
    if content_type not in score_items._VALID_CONTENT_TYPES:
        content_type = "post"

    dimensions = _valid_dimensions(content_type, obj.get("dimensions"))
    quality_score = score_items.compute_quality_score(content_type, dimensions)
    if primary_category:
        legacy_score = score_items.compute_weighted_score_legacy(
            primary_category,
            dimensions.get("credibility", 2),
            dimensions.get("novelty", 2),
            dimensions.get("depth", 2),
        )
    else:
        legacy_score = None

    keywords = _coerce_str_list(obj.get("keywords"), max_items=5)

    return {
        "summary": summary,
        "key_points": normalized_points,
        # Legacy single-value field, derived from categories[0] for backwards compat
        "category": primary_category,
        # v4.0 fields
        "categories": categories,
        "subcategories": subcategories,
        "multi_l1_reason": multi_l1_reason,
        "ai_extracted": ai_extracted,
        "visible": visible,
        "other_reason": other_reason,
        "suggested_new_subcategory": suggested_new,
        "content_type": content_type,
        "dimensions": dimensions,
        "quality_score": quality_score,
        "relevance_score": legacy_score,
        "reason": str(obj.get("reason") or "").strip()[:500],
        "keywords": keywords,
        "bold_term_count": 0,
    }


def parse_batch_response(
    raw: str,
    expected_ids: list[str],
    valid_category_ids: list[str] | None = None,
    valid_l2_by_l1: dict[str, set[str]] | None = None,
) -> dict:
    data = json.loads(_strip_json_array_text(raw))
    if not isinstance(data, list):
        raise ValueError("batch response is not an array")
    expected = set(expected_ids)
    parsed = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "").strip()
        if item_id not in expected:
            continue
        if valid_category_ids is None:
            parsed[item_id] = entry
        else:
            parsed[item_id] = parse_enrichment_response(
                json.dumps(entry, ensure_ascii=False),
                valid_category_ids,
                valid_l2_by_l1=valid_l2_by_l1,
            )
    return parsed


def _window_sql_filter(
    window_start: str | None = None,
    window_end: str | None = None,
    *,
    require_published_at: bool = False,
) -> tuple[str, list[str]]:
    if not window_start and not window_end:
        return "", []
    expr = (
        "datetime(NULLIF(published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(published_at, '')), datetime(NULLIF(fetched_at, '')))"
    )
    clauses: list[str] = []
    params: list[str] = []
    if require_published_at:
        clauses.append(" AND datetime(NULLIF(published_at, '')) IS NOT NULL")
    if window_start:
        clauses.append(f" AND {expr} >= datetime(?)")
        params.append(window_start)
    if window_end:
        clauses.append(f" AND {expr} < datetime(?)")
        params.append(window_end)
    return "".join(clauses), params


def _window_time_expr(*, require_published_at: bool = False) -> str:
    return (
        "datetime(NULLIF(published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(published_at, '')), datetime(NULLIF(fetched_at, '')))"
    )


def _run_item_scope_sql(run_id, run_items_scope=RUN_ITEMS_SCOPE_TAGGED):
    if run_id is None:
        return "", []
    if run_items_scope == RUN_ITEMS_SCOPE_INSERTED:
        return (
            """ AND EXISTS (
                    SELECT 1
                      FROM fetch_run_items fri
                     WHERE fri.run_id = ?
                       AND fri.item_id = items.id
                       AND fri.was_inserted = 1
                  )""",
            [run_id],
        )
    if run_items_scope != RUN_ITEMS_SCOPE_TAGGED:
        raise ValueError(f"Unsupported run_items_scope={run_items_scope!r}")
    return " AND fetch_run_id = ?", [run_id]


def query_pending_items(
    conn,
    limit=None,
    ids=None,
    run_id=None,
    run_items_scope=RUN_ITEMS_SCOPE_TAGGED,
    window_start=None,
    window_end=None,
    require_published_at=False,
):
    select_cols = """id, platform, source, author_name, metrics_json, url, title, content,
                     ai_summary, ai_category as category, detail_json, asr_text"""
    if ids:
        placeholders = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT {select_cols} FROM items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    run_filter, run_params = _run_item_scope_sql(run_id, run_items_scope)
    window_filter, window_params = _window_sql_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    limit_clause = " LIMIT ?" if limit else ""
    params = list(run_params)
    params.extend(window_params)
    if limit:
        params.append(limit)
    window_active = bool(window_start or window_end)
    order_expr = _window_time_expr(require_published_at=require_published_at)
    order_clause = f"{order_expr} DESC" if window_active else "fetched_at DESC"
    return conn.execute(
        f"""SELECT {select_cols}
            FROM items
            WHERE platform != 'bilibili'
              {run_filter}
              {window_filter}
              AND {AI_RETRY_READY_SQL}
              AND (
                ai_summary IS NULL OR ai_summary = ''
                OR ai_quality_score IS NULL
                OR ai_category IS NULL OR ai_category = ''
                OR ai_categories IS NULL
              )
            ORDER BY {order_clause}{limit_clause}""",
        tuple(params),
    ).fetchall()


def _truncate_readme_tail_safe(text: str, max_chars: int) -> str:
    """Tail-truncate README text on UTF-8 char boundary.

    Mirrors `fetch_feeds._truncate_readme_safe` semantics (keep tail, drop head)
    but operates on char count instead of token count, and is inlined here to
    avoid pulling fetch_feeds as a transitive import at enrich time.

    决策稿一致:从尾部截,保留最后 max_chars 字符;UTF-8 多字节字符在 Python str
    层面以 codepoint 为单位,无需手动跳过 continuation byte(那是 bytes 层面的事)。
    """
    if not text:
        return text or ""
    if len(text) <= max_chars:
        return text
    # Tail-truncation on str: codepoint-aligned by definition.
    return text[-max_chars:]


def _extract_github_readme(item: dict) -> str:
    """Return README text from item.detail_json for GitHub items, '' otherwise.

    `detail_json.readme` is populated by W2.T4 ingest path. Defensive: return
    '' on parse failures, missing field, or non-GitHub platform.
    """
    if (item.get("platform") or "").lower() != "github":
        return ""
    detail_raw = item.get("detail_json")
    if not detail_raw:
        return ""
    try:
        detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
    except (ValueError, TypeError):
        return ""
    if not isinstance(detail, dict):
        return ""
    readme = detail.get("readme") or ""
    if not isinstance(readme, str):
        return ""
    return readme


def build_item_content(item: dict, *, content_char_limit: int = 12000) -> str:
    title = item.get("title") or ""
    content = item.get("content") or ""
    asr_text = item.get("asr_text") or ""
    enriched_text = ""
    detail_raw = item.get("detail_json")
    if detail_raw:
        try:
            detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
            for ref in detail.get("referenced_urls", []):
                full_text = ref.get("full_text", "")
                if full_text and len(full_text) > 100:
                    enriched_text = f"\n\n--- 外链正文: {ref.get('title', '')} ---\n{full_text}"
                    break
        except (ValueError, TypeError, AttributeError):
            pass

    meta = {
        "platform": item.get("platform") or "",
        "source": item.get("source") or "",
        "author": item.get("author_name") or "",
        "metrics": score_items.format_metrics(item.get("metrics_json"), item.get("platform") or ""),
        "url": item.get("url") or "",
    }
    meta_text = "\n".join(f"{k}: {v}" for k, v in meta.items() if v)
    if asr_text.strip():
        body = (
            f"{meta_text}\n标题: {title}\n视频简介/正文: {content or '(无)'}\n\n"
            f"ASR transcript:\n{asr_text}"
        )
        return body[:200000]

    base = f"{meta_text}\n标题: {title}\n正文: {content or ''}{enriched_text}"[:content_char_limit]

    # v16.0: GitHub items 拼接完整 README 到 enrich 输入,让 LLM 用 README 内容
    # 做正确的 L1/L2 分类(尤其是非 AI 主题的开源教程/工具/库)。
    readme = _extract_github_readme(item)
    if readme:
        truncated = _truncate_readme_tail_safe(readme, GITHUB_README_ENRICH_MAX_CHARS)
        logger.info(
            "enrich github item %s with README (%d chars)",
            item.get("id"),
            len(truncated),
        )
        return f"{base}\n\n【完整 README】\n{truncated}"

    return base


def build_item_content_v26(item: dict) -> str:
    """Build the v26 scoring input with quoted tweets and GitHub README."""
    detail_raw = item.get("detail_json")
    try:
        detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}

    base_item = item
    if "readme" in detail:
        base_item = dict(item)
        base_detail = dict(detail)
        base_detail.pop("readme", None)
        base_item["detail_json"] = base_detail

    sections = [build_item_content(base_item)]
    quoted_tweet = detail.get("quotedTweet")
    if isinstance(quoted_tweet, dict):
        quoted_text = quoted_tweet.get("text")
        if isinstance(quoted_text, str) and quoted_text.strip():
            sections.append(f"quoted: {quoted_text.strip()}")

    if (item.get("platform") or "").lower() == "github":
        readme = detail.get("readme")
        if isinstance(readme, str) and readme.strip():
            sections.append(f"readme: {readme[:GITHUB_README_V26_MAX_CHARS]}")

    return "\n\n".join(sections)


def _minimal_item_content(item: dict) -> str:
    meta = {
        "platform": item.get("platform") or "",
        "source": item.get("source") or "",
        "author": item.get("author_name") or "",
        "url": item.get("url") or "",
    }
    meta_text = "\n".join(f"{k}: {v}" for k, v in meta.items() if v)
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    if len(content) > 280:
        content = content[:280]
    return f"{meta_text}\n标题: {title}\n正文摘要输入: {content}".strip()


def _provider_failure_fallback(item: dict, valid_category_ids: list[str]) -> dict:
    title = (item.get("title") or "").strip()
    content = re.sub(r"\s+", " ", (item.get("content") or "").strip())
    snippet = content[:220]
    summary = f"{title}。{snippet}" if snippet else title
    summary = summary[:320] or "AI 服务连续失败，已使用保守兜底处理。"
    category = "other" if "other" in valid_category_ids else (valid_category_ids[0] if valid_category_ids else None)
    dimensions = {
        "novelty": 1,
        "credibility": 1,
        "spam_score": 4,
        "depth": 1,
        "actionability": 1,
    }
    return {
        "summary": summary,
        "key_points": [{"title": "保守兜底", "points": ["AI 服务连续 5xx，未进入公开事件展示"]}],
        "category": category,
        "categories": [category] if category else [],
        "subcategories": [],
        "multi_l1_reason": None,
        "ai_extracted": {},
        "visible": False,
        "other_reason": "AI 服务连续 5xx，使用保守兜底并隐藏",
        "suggested_new_subcategory": None,
        "content_type": "post",
        "dimensions": dimensions,
        "quality_score": score_items.compute_quality_score("post", dimensions),
        "relevance_score": None,
        "reason": "provider_5xx_conservative_fallback",
        "keywords": [],
    }


def batch_group_key(item: dict) -> str:
    if (item.get("asr_text") or "").strip():
        return "single"
    content = item.get("content") or ""
    if len(content) > 12000:
        return "single"
    return "batch"


def build_batch_content(items: list[dict]) -> str:
    payload = [
        {
            "id": item["id"],
            "content": build_item_content(item),
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False)


def build_batch_system_prompt(system_prompt: str) -> str:
    return (
        system_prompt
        + "\n\n你会收到一个 JSON 数组，每个对象包含 id 和 content。"
        + "请返回 JSON 数组，每个对象必须保留原 id，并包含与单条输出相同的字段。"
    )


def call_minimax(
    api_key,
    api_base,
    model,
    system_prompt,
    user_content,
    max_tokens=4096,
    *,
    rate_gate: MiniMaxRateLimitGate | None = None,
    max_429_retries: int = MINIMAX_429_MAX_RETRIES,
    temperature: float = 0.2,
):
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })
    gate = rate_gate or _MINIMAX_RATE_GATE
    for attempt in range(max_429_retries + 1):
        gate.wait()
        try:
            with ai_provider_guard.guarded_urlopen(
                req,
                provider=ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                source="enrich_items",
                timeout=90,
                context=_SSL_CTX,
                record_429=False,
            ) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        return block["text"].strip()
                return ""
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            classified = ai_provider_guard.classify_minimax_chat_http_error(exc)
            retry_after = classified.get("retry_after_seconds")
            if attempt >= max_429_retries:
                ai_provider_guard.record_rate_limit(
                    ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                    source="enrich_items",
                    cooldown_seconds=int(retry_after or MINIMAX_429_MAX_DELAY) + 5,
                    error=classified.get("error") or f"HTTP {exc.code}: {exc.reason}",
                    action=classified.get("action") or "rate_limit",
                )
                raise
            delay = gate.pause_after_429(attempt, retry_after)
            print(
                f"  [429] MiniMax rate limited; retry {attempt + 1}/{max_429_retries} after {delay:.1f}s",
                flush=True,
            )
    return ""


def write_enrichment(conn, item_id: str, parsed: dict) -> None:
    key_points_json = json.dumps(parsed["key_points"], ensure_ascii=False) if parsed["key_points"] else None
    keywords_json = json.dumps(parsed["keywords"], ensure_ascii=False) if parsed["keywords"] else None
    dimensions_json = json.dumps(parsed["dimensions"], ensure_ascii=False) if parsed["dimensions"] else None
    # v4.0 fields
    categories_json = json.dumps(parsed.get("categories") or [], ensure_ascii=False) if parsed.get("categories") else None
    subcategories_json = json.dumps(parsed.get("subcategories") or [], ensure_ascii=False) if parsed.get("subcategories") else None
    ai_extracted = parsed.get("ai_extracted") or {}
    # Only write ai_extracted JSON if any field is non-empty
    has_extracted = bool(
        ai_extracted.get("skills")
        or ai_extracted.get("models")
        or ai_extracted.get("event_card")
    )
    ai_extracted_json = json.dumps(ai_extracted, ensure_ascii=False) if has_extracted else None
    visible_int = 1 if parsed.get("visible", True) else 0
    multi_l1_reason = parsed.get("multi_l1_reason")
    conn.execute(
        """UPDATE items
           SET ai_summary = ?,
               ai_key_points = ?,
               ai_category = COALESCE(?, ai_category),
               content_type = ?,
               ai_dimensions = ?,
               ai_quality_score = ?,
               relevance_score = COALESCE(?, relevance_score),
               ai_keywords = ?,
               ai_categories = ?,
               ai_subcategories = ?,
               multi_l1_reason = ?,
               ai_extracted = ?,
               visible = ?,
               ai_error_count = 0,
               ai_last_error = NULL,
               ai_last_error_at = NULL,
               ai_retry_after = NULL
           WHERE id = ?""",
        (
            parsed["summary"],
            key_points_json,
            parsed["category"],
            parsed["content_type"],
            dimensions_json,
            parsed["quality_score"],
            parsed["relevance_score"],
            keywords_json,
            categories_json,
            subcategories_json,
            multi_l1_reason,
            ai_extracted_json,
            visible_int,
            item_id,
        ),
    )
    conn.commit()


def write_enrichment_current(item_id: str, parsed: dict) -> None:
    """Write enrichment output to the configured enrichment backend."""
    ai_bolding.record_bolding_stats(
        source="item",
        record_id=item_id,
        candidate_count=int(parsed.get("bold_term_count") or 0),
        stats=ai_bolding.summarize_item_bolding(parsed.get("summary"), parsed.get("key_points")),
    )
    if remote_db.enrich_to_remote():
        _with_remote_db_transient_retry(
            "write_enrichment_remote",
            lambda: remote_db.write_enrichment_remote(None, item_id, parsed),
        )
        return
    item_conn = db.get_conn()
    try:
        write_enrichment(item_conn, item_id, parsed)
    finally:
        item_conn.close()


def write_highlight_verdict_current(item_id: str, result: dict) -> None:
    """Write the item-level Highlights verdict to Supabase when available."""
    if remote_db.enrich_to_remote():
        _with_remote_db_transient_retry(
            "write_highlight_verdict_remote",
            lambda: remote_db.write_highlight_verdict_remote(None, item_id, result),
        )


def write_highlight_score_v26_current(
    item_id: str,
    result: dict,
    threshold: float,
) -> None:
    """Write the v26 score to Supabase when remote enrichment is enabled."""
    if remote_db.enrich_to_remote():
        _with_remote_db_transient_retry(
            "write_highlight_score_v26_remote",
            lambda: remote_db.write_highlight_score_v26_remote(
                None,
                item_id,
                result,
                threshold=threshold,
            ),
        )


def record_highlight_verdict_failure_current(
    item_id: str,
    error: str,
    retry_after=30 * 60,
) -> None:
    if remote_db.enrich_to_remote():
        remote_db.record_highlight_verdict_failure_remote(
            None,
            item_id,
            error,
            retry_after=retry_after,
        )


def enrich_highlight_verdict_for_item(
    item: dict,
    api_key: str,
    api_base: str,
    model: str,
    *,
    dry_run: bool,
    rate_gate: MiniMaxRateLimitGate | None = None,
) -> dict | None:
    try:
        raw = call_minimax(
            api_key,
            api_base,
            model,
            highlight_verdict.load_system_prompt(),
            highlight_verdict.build_item_content(item),
            max_tokens=2048,
            rate_gate=rate_gate,
            temperature=0.0,
        )
        result = highlight_verdict.normalize_verdict_result(raw)
        result["highlight_model"] = model
        if not dry_run:
            write_highlight_verdict_current(item["id"], result)
        return result
    except Exception as exc:
        if not dry_run:
            record_highlight_verdict_failure_current(item["id"], str(exc)[:500])
        return None


def enrich_highlight_score_v26_for_item(
    item: dict,
    api_key: str,
    api_base: str,
    model: str,
    *,
    threshold: float,
    dry_run: bool,
    rate_gate: MiniMaxRateLimitGate | None = None,
) -> dict | None:
    try:
        system_prompt = load_prompt(highlight_score_v26.PROMPT_FILE)
        if not system_prompt:
            raise ValueError(f"missing prompt: {highlight_score_v26.PROMPT_FILE}")
        item_content = build_item_content_v26(item)
        raw = call_minimax(
            api_key,
            api_base,
            model,
            system_prompt,
            item_content,
            max_tokens=2048,
            rate_gate=rate_gate,
            temperature=0.0,
        )
        result = highlight_score_v26.normalize_score_result(raw)
        if result.get("error"):
            raise ValueError(result["error"])
        score10 = highlight_score_v26.compute_score10(result)
        result["runs"] = [score10]

        if (
            score10 is not None
            and abs(score10 - threshold) <= 1.0
            and _claim_highlight_v26_pass2_slot()
        ):
            try:
                pass2_raw = call_minimax(
                    api_key,
                    api_base,
                    model,
                    system_prompt,
                    item_content,
                    max_tokens=2048,
                    rate_gate=rate_gate,
                    temperature=0.0,
                )
                pass2_result = highlight_score_v26.normalize_score_result(pass2_raw)
                if pass2_result.get("error"):
                    raise ValueError(pass2_result["error"])
                pass2_score10 = highlight_score_v26.compute_score10(pass2_result)
                if pass2_score10 is None:
                    raise ValueError("score10_missing")
                if pass2_score10 < score10:
                    result = pass2_result
                result["runs"] = [score10, pass2_score10]
                score10 = round((score10 + pass2_score10) / 2, 2)
                logger.info(
                    "highlight_v26_pass2 item_id=%s s1=%s s2=%s",
                    item.get("id"),
                    result["runs"][0],
                    result["runs"][1],
                )
            except Exception as exc:
                result["pass2_error"] = str(exc)[:500]

        result["score10"] = score10
        result["is_flag_bearer"] = highlight_score_v26.is_flag_bearer(
            result,
            score10,
            threshold,
        )
        if not dry_run:
            write_highlight_score_v26_current(item["id"], result, threshold)
        return result
    except Exception as exc:
        if not dry_run:
            record_highlight_verdict_failure_current(item["id"], str(exc)[:500])
        return None


def resolve_highlight_scorer_config() -> tuple[str, float | None]:
    project_env = load_project_env(BASE_DIR)
    scorer = (
        os.environ.get("INFO2ACTION_HIGHLIGHT_SCORER")
        or project_env.get("INFO2ACTION_HIGHLIGHT_SCORER")
        or "v38"
    ).strip().lower()
    if scorer == "v38":
        return scorer, None
    if scorer != "v26":
        raise ValueError(
            "INFO2ACTION_HIGHLIGHT_SCORER must be 'v38' or 'v26'"
        )

    raw_threshold = (
        os.environ.get("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD")
        or project_env.get("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD")
    )
    try:
        threshold = float(raw_threshold) if raw_threshold and raw_threshold.strip() else None
    except (TypeError, ValueError):
        threshold = None
    if threshold is None or not math.isfinite(threshold):
        raise ValueError(
            "INFO2ACTION_HIGHLIGHT_V26_THRESHOLD must be a finite float when v26 is enabled"
        )
    return scorer, threshold


def enrich_highlight_score_for_item(
    item: dict,
    api_key: str,
    api_base: str,
    model: str,
    *,
    dry_run: bool,
    rate_gate: MiniMaxRateLimitGate | None = None,
) -> dict | None:
    scorer, threshold = resolve_highlight_scorer_config()
    if scorer == "v26":
        return enrich_highlight_score_v26_for_item(
            item,
            api_key,
            api_base,
            model,
            threshold=threshold,
            dry_run=dry_run,
            rate_gate=rate_gate,
        )
    return enrich_highlight_verdict_for_item(
        item,
        api_key,
        api_base,
        model,
        dry_run=dry_run,
        rate_gate=rate_gate,
    )


def record_failure(item_id: str, error: str, retry_after=30 * 60, increment=True) -> None:
    if remote_db.enrich_to_remote():
        remote_db.record_ai_failure_remote(
            None,
            item_id,
            error,
            retry_after=retry_after,
            increment=increment,
        )
        return
    conn = db.get_conn()
    try:
        db.record_ai_failure(conn, item_id, error, retry_after=retry_after, increment=increment)
    finally:
        conn.close()


def enrich_one_item(
    item,
    api_key,
    api_base,
    model,
    system_prompt,
    valid_category_ids,
    max_tokens,
    dry_run,
    valid_l2_by_l1=None,
    rate_gate: MiniMaxRateLimitGate | None = None,
):
    content = build_item_content(item)
    if len(re.sub(r"\s+", "", content)) < 15:
        if not dry_run:
            record_failure(item["id"], "content_too_short", retry_after=24 * 3600, increment=False)
        return None

    fallback_inputs = [
        (content, max_tokens),
        (build_item_content(item, content_char_limit=1500), min(max_tokens, 4096)),
        (_minimal_item_content(item), min(max_tokens, 2048)),
    ]
    raw = ""
    last_http_error: urllib.error.HTTPError | None = None
    for idx, (candidate_content, candidate_tokens) in enumerate(fallback_inputs):
        try:
            raw = call_minimax(
                api_key,
                api_base,
                model,
                system_prompt,
                candidate_content,
                max_tokens=candidate_tokens,
                rate_gate=rate_gate,
            )
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (500, 502, 503, 504):
                raise
            last_http_error = exc
    if not raw and last_http_error is not None:
        parsed = _provider_failure_fallback(item, valid_category_ids)
        if not dry_run:
            write_enrichment_current(item["id"], parsed)
        return parsed
    parsed = parse_enrichment_response(raw, valid_category_ids, valid_l2_by_l1=valid_l2_by_l1)
    if not parsed["summary"]:
        raise ValueError("missing summary")
    if not dry_run:
        write_enrichment_current(item["id"], parsed)
        enrich_highlight_score_for_item(
            item,
            api_key,
            api_base,
            model,
            dry_run=dry_run,
            rate_gate=rate_gate,
        )
    return parsed


def enrich_batch_items(
    items,
    api_key,
    api_base,
    model,
    system_prompt,
    valid_category_ids,
    max_tokens,
    dry_run,
    valid_l2_by_l1=None,
    rate_gate: MiniMaxRateLimitGate | None = None,
):
    raw = call_minimax(
        api_key,
        api_base,
        model,
        build_batch_system_prompt(system_prompt),
        build_batch_content(items),
        max_tokens=max_tokens,
        rate_gate=rate_gate,
    )
    parsed_map = parse_batch_response(
        raw,
        [item["id"] for item in items],
        valid_category_ids,
        valid_l2_by_l1=valid_l2_by_l1,
    )
    for item in items:
        parsed = parsed_map.get(item["id"])
        if not parsed or not parsed.get("summary"):
            raise ValueError(f"missing batch enrichment for {item['id']}")
        if not dry_run:
            write_enrichment_current(item["id"], parsed)
            enrich_highlight_score_for_item(
                item,
                api_key,
                api_base,
                model,
                dry_run=dry_run,
                rate_gate=rate_gate,
            )
    return parsed_map


def _print_dry_run_summary(item_id: str, parsed: dict) -> None:
    """Compact dry-run output: focus on v4.0 classification fields."""
    cats = parsed.get("categories") or []
    subs = parsed.get("subcategories") or []
    visible = parsed.get("visible", True)
    other_reason = parsed.get("other_reason")
    suggested = parsed.get("suggested_new_subcategory")
    extracted = parsed.get("ai_extracted") or {}
    skills = extracted.get("skills") or []
    models_ext = extracted.get("models") or []
    multi_l1 = parsed.get("multi_l1_reason")

    flag = "" if visible else " [HIDDEN]"
    cat_str = ",".join(cats) if cats else "<none>"
    sub_str = ",".join(subs) if subs else "<none>"
    line = f"  [DRY] {item_id[:24]}{flag} L1=[{cat_str}] L2=[{sub_str}] q={parsed.get('quality_score')}"
    if multi_l1:
        line += f" multi_l1='{multi_l1[:60]}'"
    if other_reason:
        line += f" other_reason='{other_reason[:60]}'"
    if suggested:
        line += f" suggest='{suggested[:60]}'"
    if skills:
        line += f" skills={skills}"
    if models_ext:
        line += f" models={models_ext}"
    print(line)


def _run_bounded_concurrent(chunks, workers, process_chunk, handle_result) -> None:
    """Run at most `workers` futures at a time and stop submitting on demand."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    workers = max(1, int(workers or 1))
    chunk_iter = iter(chunks)
    in_flight = {}

    def submit_next(executor) -> bool:
        try:
            chunk = next(chunk_iter)
        except StopIteration:
            return False
        in_flight[executor.submit(process_chunk, chunk)] = chunk
        return True

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers):
            if not submit_next(executor):
                break

        while in_flight:
            done, _pending = wait(tuple(in_flight.keys()), return_when=FIRST_COMPLETED)
            should_stop = False
            for future in done:
                chunk = in_flight.pop(future)
                try:
                    result = future.result()
                    exc = None
                except Exception as error:
                    result = None
                    exc = error
                if handle_result(chunk, result, exc):
                    should_stop = True

            if should_stop:
                for future in in_flight:
                    future.cancel()
                return

            while len(in_flight) < workers:
                if not submit_next(executor):
                    break


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified item enrichment")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--ids", type=str, default="")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--run-id", type=int, default=None,
                        help="only enrich items tagged with this fetch run")
    parser.add_argument("--run-items-scope", choices=RUN_ITEMS_SCOPE_CHOICES,
                        default=RUN_ITEMS_SCOPE_TAGGED,
                        help="run item set: tagged=items.fetch_run_id, inserted=fetch_run_items.was_inserted=1")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent batch workers (1=sequential, default=1; "
                             "global fetch currently calls this with 10)")
    parser.add_argument("--window-start", default=None,
                        help="only enrich items at/after this UTC/local datetime")
    parser.add_argument("--window-end", default=None,
                        help="only enrich items before this UTC/local datetime")
    parser.add_argument("--window-require-published-at", action="store_true",
                        help="window by real published_at only; defer undated fetched snapshots")
    parser.add_argument("--request-interval-sec", type=float, default=None,
                        help="override MiniMax chat shared request gate interval")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_highlight_scorer_config()

    config = load_config()
    classification = load_classification()
    categories = classification.get("categories", [])
    valid_category_ids = [cat["id"] for cat in categories]
    valid_l2_by_l1 = build_subcategory_map(categories)
    ai_config = config.get("ai_summary", {})
    provider = ai_config.get("provider", "minimax")
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)
    max_tokens = int(ai_config.get("max_tokens", 100000))
    request_interval = float(
        args.request_interval_sec
        if args.request_interval_sec is not None
        else ai_config.get("request_interval", 0.8)
    )

    if provider != "minimax":
        print(f"Unified enrichment currently supports minimax only, got provider={provider}", flush=True)
        return 1
    if not api_key:
        print("ERROR: No MiniMax API key configured in .env or config.json", flush=True)
        return 1

    try:
        ai_provider_guard.ensure_provider_available(
            ai_provider_guard.MINIMAX_CHAT_PROVIDER,
            source="enrich_items.main",
        )
    except (ai_provider_guard.ProviderCooldown, ai_provider_guard.ProviderActionRequired) as e:
        until = getattr(e, "cooldown_until", None)
        suffix = f" until {until}" if until else ""
        print(f"MiniMax provider unavailable, skipping enrichment{suffix}", flush=True)
        print(ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER), flush=True)
        return 0

    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    if remote_db.enrich_to_remote():
        items = query_pending_enrichment_items_remote_with_retry(
            limit=args.limit,
            ids=ids,
            run_id=args.run_id,
            run_items_scope=args.run_items_scope,
            window_start=args.window_start,
            window_end=args.window_end,
            require_published_at=args.window_require_published_at,
        )
        if args.run_id is not None and ids is None:
            retry_limit = _positive_int_env(
                "INFO2ACTION_ENRICH_RETRY_BACKLOG_LIMIT",
                ENRICH_RETRY_BACKLOG_LIMIT_DEFAULT,
            )
            retry_window_start = args.window_start or _retry_window_start_iso()
            if retry_limit > 0 and retry_window_start:
                retry_items = query_pending_enrichment_items_remote_with_retry(
                    limit=retry_limit + len(items),
                    ids=None,
                    run_id=None,
                    window_start=retry_window_start,
                    window_end=args.window_end,
                    require_published_at=args.window_require_published_at,
                )
                if retry_items:
                    before = len(items)
                    items = _merge_items_by_id(items, retry_items, max_new=retry_limit)
                    print(
                        "Loaded enrichment retry backlog: "
                        f"{len(items) - before}/{len(retry_items)} items "
                        f"window_start={retry_window_start}",
                        flush=True,
                    )
    else:
        conn = db.get_conn()
        rows = query_pending_items(
            conn,
            limit=args.limit,
            ids=ids,
            run_id=args.run_id,
            window_start=args.window_start,
            window_end=args.window_end,
            require_published_at=args.window_require_published_at,
        )
        items = [dict(row) for row in rows]
        if args.run_id is not None and ids is None:
            retry_limit = _positive_int_env(
                "INFO2ACTION_ENRICH_RETRY_BACKLOG_LIMIT",
                ENRICH_RETRY_BACKLOG_LIMIT_DEFAULT,
            )
            retry_window_start = args.window_start or _retry_window_start_iso()
            if retry_limit > 0 and retry_window_start:
                retry_rows = query_pending_items(
                    conn,
                    limit=retry_limit + len(items),
                    ids=None,
                    run_id=None,
                    window_start=retry_window_start,
                    window_end=args.window_end,
                    require_published_at=args.window_require_published_at,
                )
                retry_items = [dict(row) for row in retry_rows]
                if retry_items:
                    before = len(items)
                    items = _merge_items_by_id(items, retry_items, max_new=retry_limit)
                    print(
                        "Loaded enrichment retry backlog: "
                        f"{len(items) - before}/{len(retry_items)} items "
                        f"window_start={retry_window_start}",
                        flush=True,
                    )
        conn.close()
    print(f"Found {len(items)} items to enrich", flush=True)
    if not items:
        return 0

    system_prompt = build_system_prompt(categories)
    completed = 0
    errors = 0
    batch_size = max(1, min(5, int(args.batch_size or 1)))
    gate_interval = request_interval if int(args.workers or 1) > 1 else 0.0
    rate_gate = MiniMaxRateLimitGate(min_interval=gate_interval)

    # Pre-slice items into chunks (preserve original batch_group_key grouping
    # so non-batch items still go through enrich_one_item path).
    chunks: list[list[dict]] = []
    index = 0
    while index < len(items):
        item = items[index]
        chunk = [item]
        if batch_size > 1 and batch_group_key(item) == "batch":
            cursor = index + 1
            while cursor < len(items) and len(chunk) < batch_size:
                if batch_group_key(items[cursor]) == "batch":
                    chunk.append(items[cursor])
                else:
                    break
                cursor += 1
        chunks.append(chunk)
        index += len(chunk)

    def process_chunk(chunk: list[dict]) -> tuple[str, int, int, str]:
        """Worker function: returns (status, n_completed, n_errors, msg)."""
        try:
            if len(chunk) > 1:
                parsed_map = enrich_batch_items(
                    chunk, api_key, api_base, model, system_prompt,
                    valid_category_ids, max_tokens, args.dry_run,
                    valid_l2_by_l1=valid_l2_by_l1,
                    rate_gate=rate_gate,
                )
                if args.dry_run:
                    for it_id, p in parsed_map.items():
                        _print_dry_run_summary(it_id, p)
                return ("ok", len(parsed_map), 0, f"batch size={len(chunk)} ok")
            parsed = enrich_one_item(
                chunk[0], api_key, api_base, model, system_prompt,
                valid_category_ids, max_tokens, args.dry_run,
                valid_l2_by_l1=valid_l2_by_l1,
                rate_gate=rate_gate,
            )
            if parsed:
                if args.dry_run:
                    _print_dry_run_summary(chunk[0]["id"], parsed)
                msg = f"{chunk[0]['id'][:20]} {parsed['category']}/{parsed['content_type']} q={parsed['quality_score']}"
                return ("ok", 1, 0, msg)
            return ("ok", 0, 1, f"{chunk[0]['id'][:20]} no_summary")
        except ai_provider_guard.ProviderCooldown as e:
            return ("cooldown", 0, len(chunk), f"cooldown until {e.cooldown_until}")
        except urllib.error.HTTPError as e:
            if e.code != 429 and not args.dry_run:
                for failed in chunk:
                    record_failure(failed["id"], f"HTTP {e.code}", retry_after=30 * 60)
            tag = "http429" if e.code == 429 else "http_err"
            return (tag, 0, len(chunk), f"HTTP {e.code} batch_start={chunk[0]['id'][:20]}")
        except Exception as e:
            if len(chunk) > 1:
                # batch fallback: degrade to one-by-one inside this worker
                fb_done = 0
                fb_err = 0
                for fb in chunk:
                    try:
                        parsed = enrich_one_item(
                            fb, api_key, api_base, model, system_prompt,
                            valid_category_ids, max_tokens, args.dry_run,
                            valid_l2_by_l1=valid_l2_by_l1,
                            rate_gate=rate_gate,
                        )
                        if parsed:
                            if args.dry_run:
                                _print_dry_run_summary(fb["id"], parsed)
                            fb_done += 1
                        else:
                            fb_err += 1
                    except ai_provider_guard.ProviderCooldown as cooldown_error:
                        return (
                            "cooldown",
                            fb_done,
                            fb_err + 1,
                            f"cooldown until {cooldown_error.cooldown_until}",
                        )
                    except urllib.error.HTTPError as http_error:
                        if http_error.code != 429 and not args.dry_run:
                            record_failure(fb["id"], f"HTTP {http_error.code}", retry_after=30 * 60)
                        tag = "http429" if http_error.code == 429 else "http_err"
                        return (
                            tag,
                            fb_done,
                            fb_err + 1,
                            f"HTTP {http_error.code} fallback_start={fb['id'][:20]}",
                        )
                    except Exception as single_error:
                        if not args.dry_run:
                            record_failure(fb["id"], str(single_error), retry_after=30 * 60)
                        fb_err += 1
                return ("ok", fb_done, fb_err, f"BATCH FALLBACK size={len(chunk)}: {str(e)[:60]}")
            if not args.dry_run:
                record_failure(chunk[0]["id"], str(e), retry_after=30 * 60)
            return ("ok", 0, 1, f"ERR {chunk[0]['id'][:20]}: {str(e)[:60]}")

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        # Sequential path (kept for parity with --workers 1).
        for chunk in chunks:
            status, c, e, msg = process_chunk(chunk)
            completed += c
            errors += e
            if msg:
                print(f"  [{completed + errors}/{len(items)}] platform={_chunk_platform(chunk)} {msg}", flush=True)
            if status == "cooldown" or status == "http429":
                print(f"  [STOP] {msg}", flush=True)
                break
            time.sleep(request_interval)
    else:
        # Concurrent path: keep a bounded sliding window instead of submitting
        # all chunks at once. This prevents an 800+ chunk run from amplifying a
        # provider-side 429 before the first rate-limit signal returns.
        def handle_result(chunk, result, exc) -> bool:
            nonlocal completed, errors
            if exc is not None:
                errors += len(chunk) if chunk else 1
                print(f"  [WORKER EXC] {exc!r}", flush=True)
                return False
            status, c, e, msg = result
            completed += c
            errors += e
            if msg:
                print(f"  [{completed + errors}/{len(items)}] platform={_chunk_platform(chunk)} {msg}", flush=True)
            if status in ("cooldown", "http429"):
                print(f"  [STOP] {msg}", flush=True)
                return True
            return False

        _run_bounded_concurrent(chunks, workers, process_chunk, handle_result)

    print(f"Done! enriched={completed}, errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
