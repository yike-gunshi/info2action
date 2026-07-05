"""Cluster summary/title/key_points writer with draft -> live atomic swap.

Flow (PRD §4.8 V 原则):
  1. Gather up to `summary_max_docs` member items (by is_primary_source DESC,
     rank_in_cluster ASC), concat their content
  2. Call LLM with prompts/07_cluster_summary.md -> parse JSON
  3. Transaction:
       UPDATE clusters SET ai_*_draft = ... WHERE id = ?
       UPDATE clusters SET ai_title = ai_title_draft,
                           ai_summary = ai_summary_draft,
                           ai_key_points = ai_key_points_draft,
                           ai_title_draft = NULL, ...,
                           live_version = live_version + 1,
                           is_visible_in_feed = BF-0501-1 display policy
       bump_cluster_version_and_stale_actions(conn, cluster_id, live_version+1)
  4. LLM failure / malformed JSON -> return False, live fields untouched.
"""
from __future__ import annotations

import json
import os
import random
import re
import ssl
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import db
import ai_provider_guard
import ai_bolding
import remote_db
from clustering import visibility_policy
from prompt_loader import load_prompt
from time_utils import sort_key, to_utc_iso


def _log_event(event: str, **fields):
    """Structured JSONL log mirror of pipeline._log_event (fire-and-forget)."""
    try:
        base = Path(__file__).resolve().parents[2]
        logs = base / 'logs'
        logs.mkdir(exist_ok=True)
        line = json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event,
            **fields,
        }, ensure_ascii=False)
        with open(logs / 'cluster_events.jsonl', 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def _create_ssl_context() -> ssl.SSLContext:
    cafiles = [
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/ssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
    ]
    for cafile in cafiles:
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


_SSL_CTX = _create_ssl_context()

# Limit enforcement: summary_max_docs read from config.global.clustering at runtime;
# fallback defined here so unit tests don't depend on config.json fixtures.
_DEFAULT_SUMMARY_MAX_DOCS = 20
_TRANSIENT_LLM_HTTP_STATUS = {429, 500, 502, 503, 504}
_SUMMARY_LLM_MAX_RETRIES = 8
_SUMMARY_LLM_BASE_DELAY = 2.0
_SUMMARY_LLM_MAX_DELAY = 60.0
_SUMMARY_SCHEMA_REPAIR_RETRIES = 1
_METADATA_MARKERS = (
    'article url:',
    'comments url:',
    'points:',
    'comments:',
    'score:',
)
_NON_EVENT_TITLE_PATTERNS = (
    r'无.*聚合事件',
    r'无.*统一事件',
    r'无法构成',
    r'不构成同一事件',
    r'不是同一事件',
    r'独立.*信息',
    r'独立.*通告',
    r'无关联',
)
_LOW_INFORMATION_SINGLETON_PATTERNS = (
    '无正文描述',
    '无正文/描述',
    '无 README',
    '缺少 README',
    '无法判断具体功能',
    '无法判断具体',
    '信息量不足',
    '信息量极低',
    '仅有项目标题',
    '仅有仓库标题',
    '无实质功能',
)
_EXTERNAL_URL_RE = re.compile(r'^https?://', re.I)
_PLATFORM_LINK_HOSTS = (
    'x.com',
    'twitter.com',
    't.co',
    'pbs.twimg.com',
)

# Kept for tests and older callers; the canonical list lives in
# visibility_policy so pipeline and summary writing cannot drift.
CLUSTER_INVALID_KEYWORDS = visibility_policy.INVALID_FEED_WARNING_KEYWORDS


def _env_int(name: str, default: int, *, min_value: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(min_value, value)


def _cluster_summary_llm_timeout_sec() -> int:
    return _env_int("INFO2ACTION_CLUSTER_SUMMARY_LLM_TIMEOUT_SEC", 120, min_value=10)


def _cluster_summary_llm_max_retries() -> int:
    return _env_int("INFO2ACTION_CLUSTER_SUMMARY_LLM_MAX_RETRIES", 1, min_value=0)


def _singleton_summary_fast_path_enabled() -> bool:
    raw = os.environ.get("INFO2ACTION_CLUSTER_SUMMARY_SINGLETON_FAST_PATH")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _check_invalid_warnings(warnings) -> list[str]:
    """Return invalid-display keywords that appear in any warning string.

    Tolerant to:
      - ``warnings is None`` / missing field → []
      - non-list (str/dict/int) → [] (defensive)
      - non-string elements → coerced to str via ``str(item)``
    """
    return visibility_policy.invalid_feed_warnings(warnings)


def _call_llm_chat(*, api_key: str, api_base: str | None, model: str,
                   system_prompt: str, user_content: str,
                   max_tokens: int = 2048, timeout: int = 300,
                   source: str = 'cluster_summary',
                   max_retries: int | None = None) -> str:
    """Call MiniMax/Anthropic-compatible chat for cluster summary.

    Isolated from src.generate_summaries.call_minimax to avoid cross-concern
    coupling (per feedback_long_llm_call_isolate_from_shared_helper).
    """
    base = api_base or 'https://api.minimaxi.com/anthropic/v1'
    url = f"{base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }).encode('utf-8')
    req = Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })
    prompt_chars = len(system_prompt or '') + len(user_content or '')

    def retry_delay(attempt: int) -> float:
        return min(
            _SUMMARY_LLM_MAX_DELAY,
            _SUMMARY_LLM_BASE_DELAY * (2 ** attempt),
        ) + random.uniform(0, 0.5)

    def _retry_event_name() -> str:
        if source == 'cluster_judge':
            return 'cluster_judge_llm_retry'
        if source == 'cluster_summary':
            return 'cluster_summary_llm_retry'
        return 'cluster_llm_retry'

    effective_max_retries = (
        _SUMMARY_LLM_MAX_RETRIES
        if max_retries is None
        else max(0, int(max_retries))
    )
    for attempt in range(effective_max_retries + 1):
        attempt_started = time.time()
        try:
            ai_provider_guard.ensure_provider_available(
                ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                source=source,
            )
            with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            _log_event(
                'cluster_chat_http_call',
                source=source,
                status='ok',
                attempt=attempt + 1,
                elapsed_sec=round(time.time() - attempt_started, 2),
                timeout_sec=timeout,
                prompt_chars=prompt_chars,
            )
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                classified = ai_provider_guard.classify_minimax_chat_http_error(exc)
                retry_after = classified.get('retry_after_seconds')
                if attempt >= effective_max_retries:
                    state = ai_provider_guard.record_rate_limit(
                        ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                        source=source,
                        cooldown_seconds=int(retry_after or _SUMMARY_LLM_MAX_DELAY) + 5,
                        error=classified.get('error') or f'HTTP {exc.code}: {exc.reason}',
                        action=classified.get('action') or 'rate_limit',
                    )
                    raise ai_provider_guard.ProviderCooldown(
                        ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                        state.get('cooldown_until'),
                        ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER),
                    ) from exc
                delay = (
                    min(_SUMMARY_LLM_MAX_DELAY, retry_after)
                    if retry_after and retry_after > 0
                    else retry_delay(attempt)
                )
                _log_event(
                    _retry_event_name(),
                    source=source,
                    status=exc.code,
                    attempt=attempt + 1,
                    delay_sec=round(delay, 2),
                    elapsed_sec=round(time.time() - attempt_started, 2),
                    timeout_sec=timeout,
                    prompt_chars=prompt_chars,
                    action=classified.get('action') or 'rate_limit',
                    retry_after_sec=round(float(retry_after), 2) if retry_after is not None else None,
                )
                time.sleep(delay)
                continue
            if exc.code not in _TRANSIENT_LLM_HTTP_STATUS or attempt >= effective_max_retries:
                raise
            retry_after = None
            if getattr(exc, 'headers', None):
                try:
                    retry_after = float(exc.headers.get('Retry-After') or 0)
                except (TypeError, ValueError):
                    retry_after = None
            delay = (
                min(_SUMMARY_LLM_MAX_DELAY, retry_after)
                if retry_after and retry_after > 0
                else retry_delay(attempt)
            )
            _log_event(
                _retry_event_name(),
                source=source,
                status=exc.code,
                attempt=attempt + 1,
                delay_sec=round(delay, 2),
                elapsed_sec=round(time.time() - attempt_started, 2),
                timeout_sec=timeout,
                prompt_chars=prompt_chars,
            )
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt >= effective_max_retries:
                raise
            delay = retry_delay(attempt)
            _log_event(
                _retry_event_name(),
                source=source,
                reason=type(exc).__name__,
                attempt=attempt + 1,
                delay_sec=round(delay, 2),
                elapsed_sec=round(time.time() - attempt_started, 2),
                timeout_sec=timeout,
                prompt_chars=prompt_chars,
            )
            time.sleep(delay)
    else:
        body = {}
    for block in body.get('content', []):
        if block.get('type') == 'text':
            text = (block.get('text') or '').strip()
            _log_event(
                'cluster_chat_response',
                source=source,
                output_chars=len(text),
                max_tokens=max_tokens,
            )
            return text
    return ''


def _summary_parse_failure_reason(raw: str) -> str:
    try:
        text = (raw or '').strip()
        if text.startswith('```'):
            text = '\n'.join(ln for ln in text.splitlines()
                             if not ln.startswith('```')).strip()
        json.loads(text)
        return 'missing_fields'
    except Exception:
        return 'json_parse_fail'


def _is_escaped(text: str, idx: int) -> bool:
    backslashes = 0
    pos = idx - 1
    while pos >= 0 and text[pos] == '\\':
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def _decode_relaxed_json_string(value: str) -> str:
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')


def _extract_relaxed_string_field(text: str, field: str) -> str | None:
    marker = f'"{field}"'
    marker_idx = text.find(marker)
    if marker_idx < 0:
        return None
    colon_idx = text.find(':', marker_idx + len(marker))
    if colon_idx < 0:
        return None
    quote_idx = text.find('"', colon_idx + 1)
    if quote_idx < 0:
        return None
    start = quote_idx + 1
    pos = start
    while pos < len(text):
        if text[pos] == '"' and not _is_escaped(text, pos):
            lookahead = text[pos + 1:].lstrip()
            if lookahead.startswith(',') or lookahead.startswith('}'):
                return _decode_relaxed_json_string(text[start:pos])
        pos += 1
    return None


def _extract_relaxed_warnings(text: str) -> list[str]:
    marker_idx = text.find('"warnings"')
    if marker_idx < 0:
        return []
    colon_idx = text.find(':', marker_idx + len('"warnings"'))
    if colon_idx < 0:
        return []
    start = text.find('[', colon_idx + 1)
    if start < 0:
        return []
    depth = 0
    in_string = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if ch == '"' and not _is_escaped(text, pos):
            in_string = not in_string
        if in_string:
            continue
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                raw = text[start:pos + 1]
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    return [
                        _decode_relaxed_json_string(match.group(1)).strip()
                        for match in re.finditer(r'"((?:\\.|[^"\\])*)"', raw, flags=re.S)
                        if _decode_relaxed_json_string(match.group(1)).strip()
                    ]
    return []


def _parse_relaxed_llm_json(text: str) -> dict[str, Any] | None:
    """Parse common LLM "JSON-like" output with literal newlines in strings."""
    first = text.find('{')
    last = text.rfind('}')
    if first != -1 and last > first:
        text = text[first:last + 1]
    title = _extract_relaxed_string_field(text, 'title')
    summary = _extract_relaxed_string_field(text, 'summary')
    breakdown = _extract_relaxed_string_field(text, 'breakdown')
    warnings = _extract_relaxed_warnings(text)
    if title and summary and (breakdown or '【全文拆解】' in summary):
        return {
            'title': title,
            'summary': _compose_summary_sections(summary, breakdown),
            'key_points': [],
            'warnings': warnings,
        }
    if warnings and _check_invalid_warnings(warnings):
        return {
            'warnings': warnings,
        }
    return None


def _build_summary_repair_user_content(*, original_docs: str, bad_output: str,
                                       failure_reason: str) -> str:
    return (
        "上一轮事件摘要输出不符合 JSON schema，不能入库发布。\n"
        f"失败原因: {failure_reason}\n\n"
        "必须只返回一个 JSON object，字段如下：\n"
        "{\n"
        '  "title": "不超过 36 个中文字符的事件标题",\n'
        '  "summary": "摘要字段：一段或多段连贯摘要，不要写分点拆解",\n'
        '  "breakdown": "**主题标题**\\n- 分点拆解\\n\\n**关键信息**\\n- 资源名称（资源类型）：支撑的信息",\n'
        '  "warnings": []\n'
        "}\n\n"
        "上一轮错误输出：\n"
        f"{(bad_output or '')[:3000]}\n\n"
        "原始事件材料：\n"
        f"{original_docs}"
    )


def _compose_summary_sections(summary: str, breakdown: str | None = None) -> str:
    """Persist separate LLM fields in the existing frontend dual-section format."""
    summary_text = (summary or '').strip()
    breakdown_text = (breakdown or '').strip()
    if not breakdown_text:
        return summary_text
    summary_text = re.sub(r'^【精华速览】\s*', '', summary_text).strip()
    breakdown_text = re.sub(r'^【全文拆解】\s*', '', breakdown_text).strip()
    return f"【精华速览】\n{summary_text}\n\n【全文拆解】\n{breakdown_text}".strip()


def _summary_intro_for_length_check(summary: str) -> str:
    """Return the speed-review section, excluding the detailed breakdown."""
    text = (summary or '').strip()
    if '【全文拆解】' in text:
        text = text.split('【全文拆解】', 1)[0]
    text = re.sub(r'^【精华速览】\s*', '', text).strip()
    return text


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    """Extract JSON object from LLM output.

    Tolerates markdown code fences (```json ... ```), and requires title plus
    either:
      - ``summary`` + ``breakdown`` (current prompt schema), or
      - legacy ``summary`` already containing the dual-section markers.

    ``key_points`` is optional because prompt 07 stores detail in the
    breakdown/dual-section summary; if absent it is stored as [].
    ``warnings`` is optional (V2.3 §16.2); if absent or malformed → empty list.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith('```'):
        lines = [ln for ln in text.splitlines() if not ln.startswith('```')]
        text = '\n'.join(lines).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        first = text.find('{')
        last = text.rfind('}')
        if first == -1 or last <= first:
            return None
        try:
            obj = json.loads(text[first:last + 1])
        except (json.JSONDecodeError, ValueError):
            obj = _parse_relaxed_llm_json(text)
            if obj is None:
                return None
    if not isinstance(obj, dict):
        return None
    warnings_raw = obj.get('warnings')
    warnings: list[str] = []
    if isinstance(warnings_raw, list):
        for w in warnings_raw:
            try:
                t = str(w).strip() if w is not None else ''
            except Exception:
                continue
            if t:
                warnings.append(t)
    is_event = obj.get('is_event', True)
    if isinstance(is_event, str):
        is_event = is_event.strip().lower() not in {'false', 'no', '0'}
    if is_event is False:
        return {
            'is_event': False,
            'title': '',
            'summary': '',
            'key_points': [],
            'reason': str(obj.get('reason') or '').strip(),
            'warnings': warnings,
        }
    title = obj.get('title')
    summary = obj.get('summary')
    breakdown = obj.get('breakdown')
    kps = obj.get('key_points')
    # BF-0428-2: kps 允许为空 list (空 [] 也算合法输出)。
    # BF-0428-5: kps 元素允许 string 或 {title, points: []} 嵌套对象,与单 doc
    # enrich (03_enrich_item.md) schema 一致。
    if not (isinstance(title, str) and title.strip()
            and isinstance(summary, str) and summary.strip()):
        if _check_invalid_warnings(warnings):
            return {
                'is_event': False,
                'title': '',
                'summary': '',
                'key_points': [],
                'reason': '; '.join(warnings),
                'warnings': warnings,
            }
        return None
    normalized_title = title.strip()
    normalized_summary_input = summary.strip()
    lowered_title = normalized_title.lower()
    if any(re.search(pattern, lowered_title) for pattern in _NON_EVENT_TITLE_PATTERNS):
        return {
            'is_event': False,
            'title': '',
            'summary': '',
            'key_points': [],
            'reason': normalized_title,
            'warnings': warnings,
        }
    invalid_warning_keywords = _check_invalid_warnings(warnings)
    if (
        invalid_warning_keywords
        and breakdown is None
        and '【全文拆解】' not in normalized_summary_input
    ):
        return {
            'is_event': False,
            'title': '',
            'summary': '',
            'key_points': [],
            'reason': '; '.join(warnings),
            'warnings': warnings,
        }
    if breakdown is not None and not isinstance(breakdown, str):
        return None
    if isinstance(breakdown, str):
        breakdown = breakdown.strip()
    if isinstance(breakdown, str) and not breakdown.strip():
        return None
    if breakdown is None and '【全文拆解】' not in normalized_summary_input:
        return None
    if kps is None:
        kps = []
    if not isinstance(kps, list):
        return None
    normalized_summary = _compose_summary_sections(normalized_summary_input, breakdown)
    # BF-0428-5: preserve nested {title, points: []} dicts; sanitize strings.
    cleaned_kps: list[Any] = []
    for x in kps:
        if isinstance(x, str):
            t = x.strip()
            if t:
                cleaned_kps.append(t)
        elif isinstance(x, dict):
            grp_title = str(x.get('title') or '').strip()
            grp_points_raw = x.get('points')
            grp_points: list[str] = []
            if isinstance(grp_points_raw, list):
                for p in grp_points_raw:
                    pt = str(p).strip() if p is not None else ''
                    if pt:
                        grp_points.append(pt)
            if grp_title or grp_points:
                cleaned_kps.append({'title': grp_title, 'points': grp_points})
    return {
        'is_event': True,
        'title': normalized_title,
        'summary': normalized_summary,
        'key_points': cleaned_kps,
        'warnings': warnings,
        'bold_term_count': 0,
    }


def _parse_key_points(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []

    points: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            text = item.strip()
            if text:
                points.append(text)
        elif isinstance(item, dict):
            title = str(item.get('title') or '').strip()
            children = item.get('points')
            if title:
                points.append(title)
            if isinstance(children, list):
                for child in children:
                    text = str(child).strip()
                    if text:
                        points.append(text)
    return points


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _single_member_item_summary_payload(rows) -> dict[str, Any] | None:
    if len(rows) != 1:
        return None
    row = rows[0]
    title = str(_row_get(row, 'title') or '').strip()
    summary = str(_row_get(row, 'ai_summary') or '').strip()
    key_points = _parse_key_points(_row_get(row, 'ai_key_points'))
    if not (title and summary and key_points):
        return None
    if '【精华速览】' in summary and '【全文拆解】' in summary:
        normalized_summary = summary
    else:
        breakdown = "**关键信息**\n" + "\n".join(f"- {point}" for point in key_points[:8])
        normalized_summary = _compose_summary_sections(summary, breakdown)
    return {
        'is_event': True,
        'title': title,
        'summary': normalized_summary,
        'key_points': key_points,
        'warnings': [],
        'bold_term_count': 0,
    }


def _looks_like_metadata(content: str) -> bool:
    text = content.strip()
    if not text:
        return True
    lowered = text.lower()
    marker_hits = sum(1 for marker in _METADATA_MARKERS if marker in lowered)
    if marker_hits >= 2:
        return True
    if len(text) < 180 and ('http://' in lowered or 'https://' in lowered):
        return True
    return False


def _doc_body_for_prompt(content: str | None, ai_summary: str | None,
                         ai_key_points: str | None) -> str:
    raw_content = (content or '').strip()
    summary = (ai_summary or '').strip()
    key_points = _parse_key_points(ai_key_points)

    if raw_content and not _looks_like_metadata(raw_content):
        return raw_content

    richer_parts = []
    if summary:
        richer_parts.append(summary)
    if key_points:
        richer_parts.append('关键要点:\n' + '\n'.join(f'- {p}' for p in key_points))
    if richer_parts:
        return '\n\n'.join(richer_parts)
    return raw_content


def _detail_json_value(detail_json: Any) -> dict[str, Any]:
    if isinstance(detail_json, dict):
        return detail_json
    if not detail_json:
        return {}
    try:
        parsed = json.loads(detail_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _detail_url_candidate(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('expanded_url', 'url', 'href'):
            text = str(value.get(key) or '').strip()
            if text:
                return text
    return ''


def _is_external_resolved_url(url: str) -> bool:
    if not _EXTERNAL_URL_RE.match(url):
        return False
    host = (urlparse(url).hostname or '').lower()
    if not host:
        return False
    return not any(host == platform_host or host.endswith(f'.{platform_host}') for platform_host in _PLATFORM_LINK_HOSTS)


def _extract_resolved_urls(detail_json: Any) -> list[str]:
    detail = _detail_json_value(detail_json)
    if not detail:
        return []

    candidates: list[str] = []
    for key in ('urls', 'referenced_urls'):
        values = detail.get(key)
        if isinstance(values, list):
            candidates.extend(_detail_url_candidate(value) for value in values)

    quoted = detail.get('quotedTweet')
    if isinstance(quoted, dict):
        candidates.append(_detail_url_candidate(quoted.get('url')))

    seen: set[str] = set()
    urls: list[str] = []
    for raw in candidates:
        url = raw.rstrip('.,;:!?)')
        if not url or url in seen or not _is_external_resolved_url(url):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _collect_member_rows(conn, cluster_id: int):
    if remote_db.cluster_to_remote():
        return remote_db.collect_cluster_member_rows_remote(None, cluster_id)
    return conn.execute(
        """SELECT i.id, i.title, i.content, i.author_name, i.platform, i.url,
                  i.detail_json,
                  i.ai_summary, i.ai_key_points, i.ai_category,
                  i.published_at, i.fetched_at,
                  ci.is_primary_source, ci.rank_in_cluster
           FROM items i JOIN cluster_items ci ON ci.item_id = i.id
           WHERE ci.cluster_id = ?
           """,
        (cluster_id,),
    ).fetchall()


def _collect_member_docs_from_rows(rows, limit: int) -> list[str]:
    """Pick newest member items, with primary/rank as tie-breakers."""
    rows = sorted(
        rows,
        key=lambda r: (
            sort_key(r['published_at'] or r['fetched_at']),
            int(r['is_primary_source'] or 0),
            -int(r['rank_in_cluster'] if r['rank_in_cluster'] is not None else 9999),
        ),
        reverse=True,
    )[:limit]
    segs = []
    for idx, r in enumerate(rows, 1):
        title = (r['title'] or '').strip()
        body = _doc_body_for_prompt(
            r['content'],
            r['ai_summary'],
            r['ai_key_points'],
        )
        platform = r['platform']
        author = r['author_name'] or '?'
        url = (r['url'] or '').strip()
        header_parts = [f"[{idx}] platform={platform}", f"author={author}"]
        if url:
            header_parts.append(f"url={url}")
        resolved_urls = _extract_resolved_urls(r['detail_json'])
        if resolved_urls:
            header_parts.append(f"resolved_urls={', '.join(resolved_urls[:5])}")
        segs.append(
            f"{' '.join(header_parts)}\n"
            f"title: {title}\n"
            f"body: {body}"
        )
    return segs


def _collect_member_docs(conn, cluster_id: int, limit: int) -> list[str]:
    return _collect_member_docs_from_rows(_collect_member_rows(conn, cluster_id), limit)


def _low_information_singleton_warning(rows) -> str | None:
    if len(rows) != 1:
        return None
    row = rows[0]
    text = " ".join(
        str(_row_get(row, field) or '')
        for field in ('title', 'content', 'ai_summary', 'ai_key_points', 'ai_category')
    )
    normalized = text.replace('readme', 'README')
    hits = [pattern for pattern in _LOW_INFORMATION_SINGLETON_PATTERNS if pattern in normalized]
    if len(hits) < 2 and not (
        '无法判断' in normalized
        and ('无正文' in normalized or '无 README' in normalized or '缺少 README' in normalized)
    ):
        return None
    return f"不建议展示：单来源信息量过少（{'、'.join(hits[:3]) or '缺少可判断内容'}）"


def _mark_cluster_hidden(
    conn,
    cluster_id: int,
    *,
    warning: str,
    publish_immediately: bool,
    run_id: int | None,
) -> None:
    if remote_db.cluster_to_remote():
        remote_db.mark_cluster_hidden_remote(
            None,
            cluster_id,
            warning=warning,
            publish_immediately=publish_immediately,
            run_id=run_id,
        )
        return
    warnings_json = json.dumps([warning], ensure_ascii=False)
    if not publish_immediately:
        conn.execute(
            """UPDATE clusters SET
                 pending_is_visible_in_feed = 0,
                 pending_summary_warnings_json = ?,
                 last_touched_run_id = COALESCE(?, last_touched_run_id)
               WHERE id = ?""",
            (warnings_json, run_id, cluster_id),
        )
        conn.commit()
        return
    now_iso = to_utc_iso(datetime.now(timezone.utc))
    conn.execute(
        """UPDATE clusters SET
             is_visible_in_feed = 0,
             last_summary_warnings_json = ?,
             last_updated_at = ?,
             published_at = ?,
             published_run_id = COALESCE(?, published_run_id)
           WHERE id = ?""",
        (warnings_json, now_iso, now_iso, run_id, cluster_id),
    )
    conn.commit()


def regenerate_and_swap(
    conn,
    cluster_id: int,
    *,
    api_key: str,
    api_base: str | None,
    model: str,
    summary_max_docs: int = _DEFAULT_SUMMARY_MAX_DOCS,
    publish_immediately: bool = True,
    run_id: int | None = None,
) -> bool:
    """Regenerate cluster ai_title/summary/key_points and swap draft→live.

    Returns True on success (live version bumped), False on any failure
    (LLM error, malformed JSON, no members). Live fields untouched on failure.
    """
    member_rows = _collect_member_rows(conn, cluster_id)
    segs = _collect_member_docs_from_rows(member_rows, summary_max_docs)
    if not segs:
        _log_event('cluster_summary_fail', cluster_id=cluster_id, reason='no_members')
        return False
    low_info_warning = _low_information_singleton_warning(member_rows)
    if low_info_warning:
        _mark_cluster_hidden(
            conn,
            cluster_id,
            warning=low_info_warning,
            publish_immediately=publish_immediately,
            run_id=run_id,
        )
        _log_event(
            'cluster_summary_low_info_skipped',
            cluster_id=cluster_id,
            warning=low_info_warning,
        )
        return True
    parsed = (
        _single_member_item_summary_payload(member_rows)
        if _singleton_summary_fast_path_enabled()
        else None
    )
    if parsed:
        _log_event('cluster_summary_singleton_fast_path', cluster_id=cluster_id)
    else:
        user_content = "\n\n---\n\n".join(segs)
        # V2.3 §13.2 fix: do NOT inject cluster_docs into system prompt anymore.
        # System prompt = rules + output spec only; user_content carries the
        # member docs as the user message (one feed, not duplicated).
        system_prompt = load_prompt('07_cluster_summary.md') or ''
        _log_event('cluster_summary_prompt_built',
                   cluster_id=cluster_id,
                   cluster_docs_chars=len(user_content),
                   system_prompt_chars=len(system_prompt),
                   member_count=len(segs))
        summary_timeout = _cluster_summary_llm_timeout_sec()
        summary_max_retries = _cluster_summary_llm_max_retries()

        try:
            raw = _call_llm_chat(
                api_key=api_key, api_base=api_base, model=model,
                system_prompt=system_prompt, user_content=user_content,
                max_tokens=8192,  # prompt v5.1 长文拆解可达 2000 字,4096 有 JSON 截断风险 (BF-0428-7 曾因 2048 截断)
                timeout=summary_timeout,
                source='cluster_summary',
                max_retries=summary_max_retries,
            )
        except Exception as e:
            _log_event('cluster_summary_fail', cluster_id=cluster_id,
                       reason='llm_error', err=str(e))
            return False

        parsed = _parse_llm_json(raw)
        repair_attempt = 0
        while not parsed and repair_attempt < _SUMMARY_SCHEMA_REPAIR_RETRIES:
            repair_attempt += 1
            failure_reason = _summary_parse_failure_reason(raw)
            _log_event(
                'cluster_summary_schema_repair',
                cluster_id=cluster_id,
                attempt=repair_attempt,
                reason=failure_reason,
                raw_chars=len(raw or ''),
            )
            try:
                raw = _call_llm_chat(
                    api_key=api_key, api_base=api_base, model=model,
                    system_prompt=system_prompt,
                    user_content=_build_summary_repair_user_content(
                        original_docs=user_content,
                        bad_output=raw,
                        failure_reason=failure_reason,
                    ),
                    max_tokens=8192,
                    timeout=summary_timeout,
                    source='cluster_summary',
                    max_retries=summary_max_retries,
                )
            except Exception as e:
                _log_event('cluster_summary_fail', cluster_id=cluster_id,
                           reason='schema_repair_llm_error', err=str(e))
                break
            parsed = _parse_llm_json(raw)

    if not parsed:
        # Distinguish parse failure (could not load JSON) vs missing required keys.
        # _parse_llm_json returns None for both; re-check JSON loadability here.
        reason = _summary_parse_failure_reason(raw)
        _log_event('cluster_summary_fail', cluster_id=cluster_id,
                   reason=reason, raw_chars=len(raw or ''),
                   raw_preview=(raw or '')[:240])
        return False

    if parsed.get('is_event') is False:
        parsed_warnings = parsed.get('warnings') or []
        reason = (
            '; '.join(str(item).strip() for item in parsed_warnings if str(item).strip())
            or str(parsed.get('reason') or '').strip()
            or '不建议展示：LLM 判定不是有效事件'
        )
        _mark_cluster_hidden(
            conn,
            cluster_id,
            warning=reason,
            publish_immediately=publish_immediately,
            run_id=run_id,
        )
        return True

    if remote_db.cluster_to_remote():
        cur = remote_db.get_cluster_summary_context_remote(None, cluster_id)
    else:
        cur = conn.execute(
            "SELECT live_version, doc_count, unique_source_count FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
    if not cur:
        _log_event('cluster_summary_fail', cluster_id=cluster_id, reason='no_cluster_row')
        return False
    new_version = (cur['live_version'] or 0) + 1
    unique_source_count = cur['unique_source_count'] or 0
    dominant_category = (
        remote_db.cluster_dominant_category_remote(None, cluster_id)
        if remote_db.cluster_to_remote()
        else visibility_policy.cluster_dominant_category(conn, cluster_id)
    )

    # Expect the persisted summary to contain both frontend section markers.
    # Current prompt schema requires summary + breakdown and repairs malformed
    # output before write, so this warning primarily catches future prompt drift.
    summary_text = parsed['summary']
    has_speed_review = '【精华速览】' in summary_text
    has_full_breakdown = '【全文拆解】' in summary_text
    if not (has_speed_review and has_full_breakdown):
        _log_event(
            'cluster_summary_format_warning',
            cluster_id=cluster_id,
            has_speed_review=has_speed_review,
            has_full_breakdown=has_full_breakdown,
            summary_chars=len(summary_text),
        )
    # Length envelope check applies only to the speed-review section. The
    # breakdown is intentionally detailed and can be much longer.
    speed_review_text = _summary_intro_for_length_check(summary_text)
    if not (200 <= len(speed_review_text) <= 600):
        _log_event(
            'cluster_summary_length_warning',
            cluster_id=cluster_id,
            summary_chars=len(speed_review_text),
        )

    # BF-0501-1 — source count is not a hard display gate. Multi-source
    # clusters still pass, and high-value singleton categories can pass too.
    # Invalid warnings / `other` / low certainty remain hide signals.
    warnings = parsed.get('warnings') or []
    matched = _check_invalid_warnings(warnings)
    is_visible = 1 if visibility_policy.is_displayable_event(
        title=parsed['title'],
        summary=parsed['summary'],
        unique_source_count=unique_source_count,
        category=dominant_category,
        warnings=warnings,
    ) else 0
    if matched:
        _log_event(
            'cluster_invalid_by_summary',
            cluster_id=cluster_id,
            matched_keywords=matched,
            warnings_raw=warnings,
        )
    if not is_visible:
        _log_event(
            'cluster_hidden_by_visibility_policy',
            cluster_id=cluster_id,
            unique_source_count=unique_source_count,
            dominant_category=dominant_category,
            matched_keywords=matched,
        )

    # V2.3 §13.4 / Q20 / feature-spec R5.2 — last_summary_warnings_json
    # is overwritten unconditionally on every successful summary regen.
    # Empty warnings → '[]' so the frontend can distinguish "had warnings
    # but cleared this run" from "never generated yet" (NULL).
    warnings_json = json.dumps(warnings, ensure_ascii=False)
    ai_bolding.record_bolding_stats(
        source="cluster",
        record_id=cluster_id,
        candidate_count=int(parsed.get('bold_term_count') or 0),
        stats=ai_bolding.summarize_cluster_bolding(parsed.get('summary')),
    )

    if remote_db.cluster_to_remote():
        remote_db.write_cluster_summary_draft_remote(
            None,
            cluster_id,
            title=parsed['title'],
            summary=parsed['summary'],
            key_points=parsed['key_points'],
            is_visible=bool(is_visible),
            warnings=warnings,
            run_id=run_id,
        )
    else:
        conn.execute(
            """UPDATE clusters SET
                 ai_title_draft = ?,
                 ai_summary_draft = ?,
                 ai_key_points_draft = ?,
                 pending_is_visible_in_feed = ?,
                 pending_summary_warnings_json = ?,
                 last_touched_run_id = COALESCE(?, last_touched_run_id)
               WHERE id = ?""",
            (parsed['title'], parsed['summary'],
             json.dumps(parsed['key_points'], ensure_ascii=False),
             is_visible, warnings_json, run_id,
             cluster_id),
        )
    if not publish_immediately:
        if not remote_db.cluster_to_remote():
            conn.commit()
        return True

    if remote_db.cluster_to_remote():
        remote_db.publish_cluster_summary_live_remote(
            None,
            cluster_id,
            is_visible=bool(is_visible),
            warnings=warnings,
            run_id=run_id,
            new_version=new_version,
        )
        return True

    now_iso = to_utc_iso(datetime.now(timezone.utc))
    conn.execute(
        """UPDATE clusters SET
             ai_title = ai_title_draft,
             ai_summary = ai_summary_draft,
             ai_key_points = ai_key_points_draft,
             ai_title_draft = NULL,
             ai_summary_draft = NULL,
             ai_key_points_draft = NULL,
             is_visible_in_feed = ?,
             last_summary_warnings_json = ?,
             pending_is_visible_in_feed = NULL,
             pending_summary_warnings_json = NULL,
             last_updated_at = ?,
             published_at = ?,
             published_run_id = COALESCE(?, published_run_id)
           WHERE id = ?""",
        (is_visible, warnings_json, now_iso, now_iso, run_id, cluster_id),
    )
    db.bump_cluster_version_and_stale_actions(conn, cluster_id, new_version)
    return True


def publish_run(conn, run_id: int) -> int:
    """Promote all draft cluster summaries touched by a completed fetch run.

    During run-scoped clustering we keep regenerated event copy in draft fields
    so the feed never sees half-finished results. This function is the explicit
    batch publish gate: draft -> live, visibility update, live_version bump.
    """
    if remote_db.cluster_to_remote():
        return remote_db.publish_run_remote(None, run_id)
    rows = conn.execute(
        """SELECT id, live_version
             FROM clusters
            WHERE last_touched_run_id = ?
              AND (
                ai_title_draft IS NOT NULL
                OR ai_summary_draft IS NOT NULL
                OR ai_key_points_draft IS NOT NULL
                OR pending_is_visible_in_feed IS NOT NULL
              )
            ORDER BY id ASC""",
        (run_id,),
    ).fetchall()
    published = 0
    for row in rows:
        cluster_id = row['id']
        new_version = (row['live_version'] or 0) + 1
        now_iso = to_utc_iso(datetime.now(timezone.utc))
        conn.execute(
            """UPDATE clusters SET
                 ai_title = COALESCE(ai_title_draft, ai_title),
                 ai_summary = COALESCE(ai_summary_draft, ai_summary),
                 ai_key_points = COALESCE(ai_key_points_draft, ai_key_points),
                 ai_title_draft = NULL,
                 ai_summary_draft = NULL,
                 ai_key_points_draft = NULL,
                 is_visible_in_feed = COALESCE(pending_is_visible_in_feed, is_visible_in_feed),
                 last_summary_warnings_json = COALESCE(pending_summary_warnings_json, last_summary_warnings_json),
                 pending_is_visible_in_feed = NULL,
                 pending_summary_warnings_json = NULL,
                 last_updated_at = ?,
                 published_at = ?,
                 published_run_id = ?
               WHERE id = ?""",
            (now_iso, now_iso, run_id, cluster_id),
        )
        db.bump_cluster_version_and_stale_actions(conn, cluster_id, new_version)
        published += 1
        _log_event('cluster_published', cluster_id=cluster_id, run_id=run_id,
                   live_version=new_version)
    return published
