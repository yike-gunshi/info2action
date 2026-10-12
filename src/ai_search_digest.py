"""Assemble AI search input, call GLM Coding Plan, and validate its digest.

This module does not persist jobs. Callers own database writes and map
QuotaExhausted to unavailable, and other failures to failed.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import ai_provider_guard
from env_utils import load_project_env

BASE_DIR = Path(__file__).resolve().parents[1]
_SSL_CTX = ssl.create_default_context()
_DEFAULTS = {
    'GLM_CODING_API_KEY': '',
    'GLM_CODING_API_BASE': 'https://open.bigmodel.cn/api/coding/paas/v4',
    'AI_SEARCH_MODEL': 'glm-5.3',
    'AI_SEARCH_MAX_TOKENS': 131072,
    'AI_SEARCH_REASONING_EFFORT': 'high',
    'AI_SEARCH_ITEM_CHARS': 4000,
    'AI_SEARCH_INPUT_BUDGET_TOKENS': 120000,
    'AI_SEARCH_JOB_TIMEOUT_S': 2400,
    'AI_SEARCH_PROMPT_PATH': 'prompts/17_ai_search_digest.md',
}
_PLATFORMS = {
    'twitter': 'X', 'lingowhale': '公众号', 'rss': 'RSS', 'reddit': 'Reddit',
    'hackernews': 'Hacker News', 'github': 'GitHub', 'bilibili': 'B 站',
    'waytoagi': 'WaytoAGI',
}
_TIERS = ('content', 'key_points', 'summary')
_CITATION = re.compile(r'\[(\d+)\]')
_JSON_BLOCK = re.compile(r'^\s*```json[^\S\n]*\n(.*?)^\s*```[^\S\n]*$', re.M | re.S)
_CONTENT_FIELD = re.compile(r'"content_md"\s*:\s*("(?:[^"\\]|\\.)*")', re.S)


class DigestError(Exception):
    """The provider or digest response could not be used."""


class QuotaExhausted(DigestError):
    """The GLM Coding Plan quota is unavailable; do not retry this call."""


class ParseError(DigestError):
    """Unparseable output, retaining at most 200 KiB for the caller's log."""

    def __init__(self, raw: str):
        super().__init__('parse_error: missing or invalid content_md')
        self.raw = raw.encode('utf-8')[:200 * 1024].decode('utf-8', errors='ignore')


def settings() -> dict:
    """Resolve env keys using process environment, project .env, then defaults."""
    env = {**load_project_env(BASE_DIR), **os.environ}
    config = {}
    for key, default in _DEFAULTS.items():
        value = env.get(key) or default
        if isinstance(default, int):
            try:
                config[key] = int(value)
            except (ValueError, TypeError):
                config[key] = default
        else:
            config[key] = str(value).strip()
    return config


def load_prompt(path) -> tuple[str, str]:
    """Return the header version and everything after the first separator."""
    path = Path(path)
    if not path.is_absolute():
        path = BASE_DIR / path
    text = path.read_text(encoding='utf-8')
    header, separator, template = text.partition('\n---\n')
    version = re.search(r'^>\s*版本：\s*`([^`]+)`', header, re.M)
    if not separator or not version:
        raise DigestError(f'invalid prompt header: {path}')
    return version.group(1), template


def _clean(value) -> str:
    return ' '.join(str(value or '').split())


def _platform(item: dict) -> str:
    platform = item.get('platform') or ''
    return _PLATFORMS.get(platform, platform)


def _build_input(query, today, candidates, config: dict, *, degrade=True) -> dict:
    headers, bodies, levels = [], [], []
    item_chars = max(0, int(config['AI_SEARCH_ITEM_CHARS']))
    for number, item in enumerate(candidates, 1):
        published = item.get('published_at') or item.get('fetched_at')
        date = str(published)[:10] if published else '日期未知'
        headers.append(f'[{number}] {_platform(item)} · {date} · {_clean(item.get("title"))[:80]}\n正文：')
        content = _clean(item.get('content'))
        if len(content) > item_chars:
            content = content[:item_chars] + '…（已截）'
        points = item.get('ai_key_points')
        if isinstance(points, str):
            points = points if points.strip() else ''
        else:
            points = json.dumps(points, ensure_ascii=False) if points else ''
        bodies.append((content, points, _clean(item.get('ai_summary'))))
        levels.append(0 if content else 1 if points else 2)

    prefix = f'用户问题：{query}\n\n今天：{today}\n\n候选条目（共 {len(candidates)} 条）：\n\n'
    char_count = len(prefix) + 2 * max(0, len(candidates) - 1)
    char_count += sum(len(header) + len(body[level]) for header, body, level in zip(headers, bodies, levels))
    budget_chars = config['AI_SEARCH_INPUT_BUDGET_TOKENS'] * 1.4
    # Each round changes each item by at most one available tier, tail first.
    # Levels strictly increase, even if the fallback happens to be longer.
    while degrade and char_count > budget_chars and any(level < 2 for level in levels):
        for i in range(len(candidates) - 1, -1, -1):
            if levels[i] == 2:
                continue
            old = levels[i]
            levels[i] += 1
            if levels[i] == 1 and not bodies[i][1]:
                levels[i] = 2
            char_count += len(bodies[i][levels[i]]) - len(bodies[i][old])
            if char_count <= budget_chars:
                break
    message = prefix + '\n\n'.join(header + body[level] for header, body, level in zip(headers, bodies, levels))
    return {
        'user_message': message,
        'tiers': {item['id']: _TIERS[level] for item, level in zip(candidates, levels)},
        'est_tokens': math.ceil(len(message) / 1.4),
    }


def build_input(query, today, candidates, *, degrade=True, settings=None) -> dict:
    """Assemble ordered candidates with bounded content and tier degradation."""
    config = settings if settings is not None else globals()['settings']()
    return _build_input(query, today, candidates, config, degrade=degrade)


def _render_prompt(template, query, today, item_count):
    variables = {'query': query, 'today': today, 'item_count': str(item_count), 'items': ''}
    # One pass preserves literal placeholders inside user-supplied text.
    return re.sub(r'\{(query|today|item_count|items)\}', lambda m: variables[m.group(1)], template)


def trim_input_candidates(query, today, candidates, config) -> tuple[list, list]:
    """Recheck v2's current full-text budget, retaining at least one item."""
    from ai_search_retrieval import settings as retrieval_settings
    limits = retrieval_settings()
    _, template = load_prompt(config['AI_SEARCH_PROMPT_PATH'])
    retained, dropped = list(candidates), []
    while len(retained) > 1:
        message = _build_input(query, today, retained, config, degrade=False)['user_message']
        system = _render_prompt(template, query, today, len(retained))
        tokens = math.ceil((len(message) + len(system)) / limits['AI_SEARCH_CHARS_PER_TOKEN'])
        if tokens <= limits['AI_SEARCH_INPUT_MAX_TOKENS']:
            break
        dropped.append(retained.pop()['id'])
    return retained, dropped


def _request_params(config: dict) -> dict:
    return {
        'model': config['AI_SEARCH_MODEL'],
        'max_tokens': config['AI_SEARCH_MAX_TOKENS'],
        'reasoning_effort': config['AI_SEARCH_REASONING_EFFORT'],
        'stream': True,
    }


def _stream_idle_timeout(config: dict) -> int:
    # Keep legacy settings snapshots stable; this transport setting is optional.
    env = {**load_project_env(BASE_DIR), **os.environ}
    value = config.get('AI_SEARCH_STREAM_IDLE_TIMEOUT_S', env.get('AI_SEARCH_STREAM_IDLE_TIMEOUT_S'))
    try:
        return max(30, int(value if value not in (None, '') else 300))
    except (TypeError, ValueError):
        return 300


def call_glm(system_prompt, user_message, *, settings) -> tuple[str, dict, str]:
    """Call Coding Plan; settings['retry_delay_s'] can override the 30s delay."""
    if not settings['GLM_CODING_API_KEY']:
        raise DigestError('missing GLM_CODING_API_KEY')
    secrets = [settings['GLM_CODING_API_KEY'], system_prompt, user_message]
    query = re.match(r'^用户问题：(.*?)\n\n今天：', user_message, re.S)
    if query:
        secrets.append(query.group(1))

    def error_summary(value):
        detail = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            for encoded in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                detail = detail.replace(encoded, '[redacted]')
        return detail[:300]

    idle_timeout = _stream_idle_timeout(settings)
    total_started = time.monotonic()

    def check_total_timeout():
        elapsed = time.monotonic() - total_started
        if elapsed > settings['AI_SEARCH_JOB_TIMEOUT_S']:
            raise DigestError(f'GLM job timeout after {elapsed:.0f} seconds')

    payload = {
        **_request_params(settings),
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
    }
    req = urllib.request.Request(
        settings['GLM_CODING_API_BASE'].rstrip('/') + '/chat/completions',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {settings["GLM_CODING_API_KEY"]}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    for attempt in range(2):
        check_total_timeout()
        started = time.monotonic()
        try:
            with ai_provider_guard.guarded_urlopen(
                req, provider='glm-coding', source='ai_search',
                timeout=idle_timeout, context=_SSL_CTX,
            ) as response:
                parts, usage, model = [], {}, settings['AI_SEARCH_MODEL']
                done, finished = False, False
                while True:
                    check_total_timeout()
                    line = response.readline()
                    check_total_timeout()
                    if not line:
                        break
                    if not line.startswith(b'data:'):
                        continue
                    data = line[5:].strip()
                    if data == b'[DONE]':
                        done = True
                        break
                    try:
                        result = json.loads(data)
                        if not isinstance(result, dict):
                            raise ValueError('invalid chunk')
                        if 'error' in result:
                            raise DigestError(f'GLM stream error: {error_summary(result["error"])}')
                        model = result.get('model') or model
                        if 'usage' in result:
                            usage = result['usage'] or {}
                            if not isinstance(usage, dict):
                                raise ValueError('invalid usage')
                        choices = result['choices']
                        if not isinstance(choices, list):
                            raise ValueError('invalid choices')
                        for choice in choices:
                            finished = finished or choice.get('finish_reason') is not None
                            content = choice['delta'].get('content')
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ValueError('invalid content')
                                parts.append(content)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        raise DigestError('invalid GLM response') from None
                if not done and not finished:
                    raise DigestError('GLM stream ended early')
            return ''.join(parts), usage, model
        except ai_provider_guard.ProviderCooldown as exc:
            raise QuotaExhausted(error_summary(str(exc))) from None
        except urllib.error.HTTPError as exc:
            body = error_summary(ai_provider_guard.read_http_error_body(exc))
            if exc.code == 429:
                raise QuotaExhausted(f'HTTP 429: {body}') from None
            if not (500 <= exc.code < 600) or attempt == 1 or time.monotonic() - started >= 120:
                raise DigestError(f'HTTP {exc.code}: {body}') from None
        except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError,
                socket.timeout, TimeoutError) as exc:
            elapsed = time.monotonic() - started
            if attempt == 1 or elapsed >= 120:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                kind = type(exc).__name__
                if isinstance(reason, BaseException) and reason is not exc:
                    kind += f'/{type(reason).__name__}'
                failure = '读超时' if isinstance(reason, (socket.timeout, TimeoutError)) else '断开'
                raise DigestError(f'GLM 连接在 {elapsed:.0f} 秒后{failure}（{kind}）') from None
        time.sleep(settings.get('retry_delay_s', 30))
    raise DigestError('GLM retry exhausted')  # Defensive: loop returns or raises.


def _parse(raw: str) -> dict:
    blocks = list(_JSON_BLOCK.finditer(raw))
    # A broken final block must never silently select an earlier draft.
    source = blocks[-1].group(1) if blocks else raw
    if blocks:
        try:
            result = json.loads(source)
            if isinstance(result, dict) and isinstance(result.get('content_md'), str):
                return result
        except ValueError:
            pass
    for match in reversed(list(_CONTENT_FIELD.finditer(source))):
        try:
            return {'content_md': json.loads(match.group(1))}
        except ValueError:
            pass
    raise ParseError(raw)


def _valid_numbers(values, count: int) -> list[int]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(n for n in values if type(n) is int and 1 <= n <= count))


def _clean_citations(content: str, count: int) -> str:
    used = 0

    def replace(match):
        nonlocal used
        if match.group(0) in '。！？；\n':
            used = 0
            return match.group(0)
        number = int(match.group(1))
        if not 1 <= number <= count:
            return ''
        used += 1
        return match.group(0) if used <= 5 else ''

    return re.sub(r'\[(\d+)\]|[。！？；\n]', replace, content)


def _headings(content: str) -> str:
    content = re.sub(
        r'^#{1,6}[^\S\n]+(.*?)[^\S\n]*$',
        lambda m: '\n\n**' + m.group(1) + '**\n\n', content, flags=re.M,
    )
    return re.sub(r'\n{3,}', '\n\n', content).strip()


def _append_related(content: str, rows: list[str]) -> str:
    if not rows:
        return content
    lines = content.splitlines()
    heading = next((i for i, line in enumerate(lines) if line.strip() == '**其他相关内容**'), None)
    if heading is None:
        return content.rstrip() + '\n\n**其他相关内容**\n\n' + '\n'.join(rows)
    end = heading + 1
    while end < len(lines) and (not lines[end].strip() or re.match(r'^\s*(?:[-*•]|\d+[.、])\s', lines[end])):
        end += 1
    # Keep new rows within the existing list, before any following section.
    before = '\n'.join(lines[:end]).rstrip()
    after = '\n'.join(lines[end:]).lstrip()
    return before + '\n' + '\n'.join(rows) + ('\n\n' + after if after else '')


def renumber_citations(content: str, candidate_count: int) -> tuple[str, list[int]]:
    """Number citations by first appearance, retaining their candidate positions.

    Unknown citations survive unchanged; cleaning belongs to the caller. One
    regex pass prevents newly assigned numbers from being substituted again.
    """
    cited_numbers = list(dict.fromkeys(
        int(n) for n in _CITATION.findall(content) if 1 <= int(n) <= candidate_count
    ))
    number_map = {old: new for new, old in enumerate(cited_numbers, 1)}
    content = _CITATION.sub(
        lambda match: f'[{number_map[int(match.group(1))]}]'
        if int(match.group(1)) in number_map else match.group(0),
        content,
    )
    return content, cited_numbers


def postprocess(raw_text, candidates) -> dict:
    """Parse the last JSON answer and enforce citation/rendering contracts."""
    result = _parse(raw_text)
    if not result['content_md'].strip():
        raise ParseError(raw_text)
    content = _clean_citations(result['content_md'], len(candidates))
    content = _headings(content)
    present = {int(n) for n in _CITATION.findall(content)}
    relevant = _valid_numbers(result.get('relevant_ids'), len(candidates))
    marginal = _valid_numbers(result.get('marginal_ids'), len(candidates))
    irrelevant = _valid_numbers(result.get('irrelevant_ids'), len(candidates))
    missing = [n for n in dict.fromkeys(relevant + marginal) if n not in present]
    rows = []
    for number in missing:
        item = candidates[number - 1]
        rows.append(f'- {_platform(item)} · {_clean(item.get("title"))[:60]} [{number}]')
    content = _append_related(content, rows).strip()
    if not content:
        raise ParseError(raw_text)
    content, cited_numbers = renumber_citations(content, len(candidates))
    return {
        'content_md_final': content + f'\n\n基于 {len(cited_numbers)} 条内容',
        'cited_ids': [candidates[n - 1]['id'] for n in cited_numbers],
        'cited_numbers': list(range(1, len(cited_numbers) + 1)),
        'cited_candidate_numbers': cited_numbers,
        'result': result,
        'relevant_count': len(relevant),
        'marginal_count': len(marginal),
        'irrelevant_count': len(irrelevant),
    }


def run_digest(job: dict, candidates: list, *, settings=None, call=None, now=None) -> dict:
    """Run the pure digest pipeline; `now` may be a date/datetime or callable."""
    config = settings if settings is not None else globals()['settings']()
    started = time.monotonic()
    current = now() if callable(now) else now
    if current is None:
        current = datetime.now(ZoneInfo('Asia/Shanghai'))
    today = current.isoformat()[:10]
    query = job['query']
    assembled = _build_input(query, today, candidates, config, degrade=job.get('retrieval') is None)
    version, template = load_prompt(config['AI_SEARCH_PROMPT_PATH'])
    system = _render_prompt(template, query, today, len(candidates))
    raw, usage, model = (call or call_glm)(system, assembled['user_message'], settings=config)
    processed = postprocess(raw, candidates)
    return {
        'input_text': assembled['user_message'], 'tiers': assembled['tiers'],
        'raw': raw, 'usage': usage, 'elapsed_ms': int((time.monotonic() - started) * 1000),
        'model': model, 'prompt_version': version, 'request_params': _request_params(config),
        **processed,
    }
