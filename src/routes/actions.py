"""Action-related API routes (migrated from serve.py)."""
import copy
import json
import os
import ssl as _ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

import db
import execute_action
import remote_db
import action_detail_read_model
from authz import can_access_all, current_user_id, owner_scope_user_id, require_admin
from deps import BASE

router = APIRouter()

# ── Helpers ──────────────────────────────────────────────────


def _load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


_ACTION_TYPE_LABELS = {
    'investigate': '调研',
    'implement': '实施',
    'content': '内容生成',
}
_VALID_ACTION_TYPES = set(_ACTION_TYPE_LABELS.keys())
_VALID_PRIORITIES = {'high', 'medium', 'low', 'bug'}
_ACTION_BOARD_LANES = (
    {
        'slug': 'pending',
        'label': '待处理',
        'statuses': {'pending'},
    },
    {
        'slug': 'in_progress',
        'label': '执行中',
        'statuses': {'confirmed', 'executing', 'dispatched'},
    },
    {
        'slug': 'done',
        'label': '已完成',
        'statuses': {'done'},
    },
)
_ACTION_BOARD_VISIBLE_STATUSES = {
    status
    for lane in _ACTION_BOARD_LANES
    for status in lane['statuses']
}


def _one_line(value, *, limit=300):
    text = str(value or '').strip()
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + '...'


def _coerce_manual_action_type(value):
    action_type = str(value or '').strip()
    return action_type if action_type in _VALID_ACTION_TYPES else 'investigate'


def _manual_fallback_prompt(item, action_type, user_hint):
    title = _one_line(item.get('title') or item.get('id') or '这条信息', limit=120)
    summary = _one_line(item.get('ai_summary') or item.get('content'), limit=400)
    focus_by_type = {
        'investigate': '判断这条信息是否值得继续跟进，并列出下一步调研问题。',
        'implement': '判断这条信息是否能转成一个小的落地动作，并列出最小验证步骤。',
        'content': '判断这条信息是否适合转成内容素材，并给出可产出的角度。',
    }
    parts = [
        '这是一次用户手动触发的单条行动点生成。模型未返回可解析行动点，请给出保守但可执行的下一步。',
        f'标题：{title}',
    ]
    if summary:
        parts.append(f'摘要：{summary}')
    if user_hint:
        parts.append(f'用户预期：{_one_line(user_hint, limit=180)}')
    parts.append(f'目标：{focus_by_type.get(action_type, focus_by_type["investigate"])}')
    parts.append('完成标准：产出是否值得继续投入的判断，以及一个明确的继续/停止建议。')
    return '\n'.join(parts)


def _build_manual_fallback_action(item, item_id, req_action_type, req_user_hint):
    action_type = _coerce_manual_action_type(req_action_type)
    title_base = _one_line(item.get('title') or item_id or '这条信息', limit=60)
    prefix_by_type = {
        'investigate': '深入了解',
        'implement': '评估落地',
        'content': '围绕',
    }
    if action_type == 'content':
        title = f'{prefix_by_type[action_type]} {title_base} 创作内容'
    else:
        title = f'{prefix_by_type[action_type]} {title_base}'
    return {
        'title': title,
        'action_type': action_type,
        'prompt': _manual_fallback_prompt(item, action_type, req_user_hint),
        'reason': '单条手动生成无门槛：模型未返回可解析行动点，已生成保守兜底方向。',
        'priority': 'medium',
        'source_item_ids': [item_id],
        'direction': '_uncategorized',
        'direction_label': '待归类',
    }


def _normalize_manual_action(action, item, item_id, req_action_type, req_user_hint):
    normalized = dict(action or {})
    action_type = _coerce_manual_action_type(req_action_type or normalized.get('action_type'))
    fallback = _build_manual_fallback_action(item, item_id, action_type, req_user_hint)

    title = _one_line(normalized.get('title') or fallback['title'], limit=140)
    prompt = str(normalized.get('prompt') or fallback['prompt']).strip()
    reason = str(normalized.get('reason') or fallback['reason']).strip()
    priority = str(normalized.get('priority') or 'medium').strip()
    if priority not in _VALID_PRIORITIES:
        priority = 'medium'

    source_ids = normalized.get('source_item_ids') or []
    if isinstance(source_ids, str):
        try:
            source_ids = json.loads(source_ids)
        except (json.JSONDecodeError, TypeError):
            source_ids = []
    if not isinstance(source_ids, list):
        source_ids = []
    source_ids = [str(sid) for sid in source_ids if sid]
    if item_id not in source_ids:
        source_ids = [item_id] + source_ids

    normalized.update({
        'title': title,
        'action_type': action_type,
        'prompt': prompt,
        'reason': reason,
        'priority': priority,
        'source_item_ids': source_ids,
        'direction': normalized.get('direction') or '_uncategorized',
        'direction_label': normalized.get('direction_label') or '待归类',
    })
    return normalized


def _persist_manual_item_action(action, request):
    if remote_db.app_state_to_remote():
        action_id = remote_db.create_action_remote(
            source_type='manual',
            title=action['title'],
            action_type=action['action_type'],
            prompt=action['prompt'],
            source_item_ids=action.get('source_item_ids') or [],
            reason=action.get('reason', ''),
            priority=action.get('priority', 'medium'),
            related_project=action.get('related_project'),
            direction=action.get('direction') or '_uncategorized',
            direction_label=action.get('direction_label') or '待归类',
            user_id=current_user_id(request),
        )
        persisted = dict(action)
        persisted['id'] = action_id
        persisted['source_type'] = 'manual'
        return persisted

    conn = db.get_conn()
    try:
        action_id = db.create_action(
            conn,
            source_type='manual',
            title=action['title'],
            action_type=action['action_type'],
            prompt=action['prompt'],
            source_item_ids=action.get('source_item_ids') or [],
            reason=action.get('reason', ''),
            priority=action.get('priority', 'medium'),
            related_project=action.get('related_project'),
            direction=action.get('direction') or '_uncategorized',
            direction_label=action.get('direction_label') or '待归类',
            user_id=current_user_id(request),
        )
    finally:
        conn.close()

    persisted = dict(action)
    persisted['id'] = action_id
    persisted['source_type'] = 'manual'
    return persisted


# ── Discord Forum dispatch helpers ───────────────────────────

_discord_tags_cache = None  # {tag_name_lower: tag_id}


def _load_discord_config():
    """Load Discord config from config.json + .env bot token."""
    cfg = _load_json(os.path.join(BASE, 'config', 'config.json')) or {}
    dcfg = cfg.get('discord', {})
    token = os.environ.get('DISCORD_BOT_TOKEN', '')
    if not token:
        env_path = os.path.join(BASE, '.env')
        if os.path.exists(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('DISCORD_BOT_TOKEN='):
                            token = line.split('=', 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    dcfg['bot_token'] = token
    return dcfg


def _discord_api(method, endpoint, payload=None, bot_token=None):
    """Call Discord API via urllib.request. Returns parsed JSON or raises.
    bot_token: override token (e.g. per-user encrypted token). Falls back to global config.
    """
    if not bot_token:
        dcfg = _load_discord_config()
        bot_token = dcfg.get('bot_token', '')
    if not bot_token:
        raise ValueError('DISCORD_BOT_TOKEN not configured')
    url = 'https://discord.com/api/v10' + endpoint
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'Bot ' + bot_token)
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'InfoFeed/9.0')
    ssl_ctx = _ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
        return json.loads(resp.read().decode())


def _get_forum_tags():
    """Fetch and cache available tags from the Discord forum channel."""
    global _discord_tags_cache
    if _discord_tags_cache is not None:
        return _discord_tags_cache
    dcfg = _load_discord_config()
    channel_id = dcfg.get('forum_channel_id', '')
    if not channel_id:
        return {}
    try:
        data = _discord_api('GET', f'/channels/{channel_id}')
        tags = {}
        for tag in data.get('available_tags', []):
            name = (tag.get('name', '') or '').lower()
            tags[name] = str(tag['id'])
        _discord_tags_cache = tags
        print(f'[discord] cached {len(tags)} forum tags: {list(tags.keys())}')
        return tags
    except Exception as e:
        print(f'[discord] failed to fetch forum tags: {e}')
        return {}


def _resolve_tag_ids(action):
    """Pick tag IDs for a dispatched action based on priority and action_type."""
    dcfg = _load_discord_config()
    tag_map = dcfg.get('tags', {})
    forum_tags = _get_forum_tags()
    tag_ids = []

    # Priority tag
    prio = action.get('priority', 'medium')
    prio_tid = tag_map.get(prio)
    if prio_tid:
        tag_ids.append(int(prio_tid))
    elif prio in ('high',):
        for name, tid in forum_tags.items():
            if '高' in name or 'high' in name:
                tag_ids.append(int(tid))
                break

    # Action type tag
    atype = action.get('action_type', '')
    atype_tid = tag_map.get(atype)
    if atype_tid:
        tag_ids.append(int(atype_tid))

    # "queued" tag
    queued_tid = tag_map.get('queued')
    if queued_tid:
        tag_ids.append(int(queued_tid))

    return tag_ids[:5]  # Discord limit: max 5 tags


def _dispatch_to_discord(action, bot_token=None):
    """Create a Discord Forum thread with Embed for this action.
    Returns (thread_id, thread_url) or raises on error.
    bot_token: per-user token override."""
    dcfg = _load_discord_config()
    channel_id = dcfg.get('forum_channel_id', '')
    manager_id = dcfg.get('manager_user_id', '')
    if not channel_id:
        raise ValueError('discord.forum_channel_id not configured')

    prio_emoji = {'high': '\U0001f534', 'medium': '\U0001f7e1', 'low': '\U0001f7e2'}.get(
        action.get('priority', ''), '\u26aa')
    type_label = {
        'implement': '\u2699\ufe0f 实施',
        'investigate': '\U0001f50d 调研',
        'content': '\u270d\ufe0f 内容',
    }.get(action.get('action_type', ''), action.get('action_type', ''))
    dir_label = action.get('direction_label', '') or action.get('direction', '')

    reason = action.get('reason', '') or ''
    if len(reason) > 200:
        reason = reason[:200] + '...'

    prompt = action.get('prompt', '') or ''
    steps_text = prompt[:800] if prompt else '(无详细步骤)'

    embed = {
        'title': action.get('title', ''),
        'color': {'high': 0xDC2626, 'medium': 0xF59E0B, 'low': 0x10B981}.get(
            action.get('priority', ''), 0x6366F1),
        'fields': [
            {'name': '优先级', 'value': f'{prio_emoji} {action.get("priority", "medium")}', 'inline': True},
            {'name': '类型', 'value': type_label, 'inline': True},
            {'name': '方向', 'value': dir_label or '-', 'inline': True},
            {'name': '决策理由', 'value': reason or '-', 'inline': False},
            {'name': '行动步骤', 'value': steps_text, 'inline': False},
        ],
        'footer': {'text': f'info2action v9.0 | action_id: {action.get("id", "")[:8]}'}
    }

    mention = f'<@{manager_id}> ' if manager_id else ''
    content = f'{mention}新任务派发：{action.get("title", "")}'

    tag_ids = _resolve_tag_ids(action)

    payload = {
        'name': (action.get('title', '') or 'Untitled')[:100],
        'message': {
            'content': content,
            'embeds': [embed],
        },
        'applied_tags': tag_ids,
    }
    data = _discord_api('POST', f'/channels/{channel_id}/threads', payload, bot_token=bot_token)
    thread_id = data.get('id', '')
    guild_id = data.get('guild_id', '')
    thread_url = (f'https://discord.com/channels/{guild_id}/{thread_id}'
                  if guild_id else f'https://discord.com/channels/@me/{thread_id}')
    return thread_id, thread_url


# ── SSE headers ──────────────────────────────────────────────

_SSE_HEADERS = {
    'Cache-Control': 'no-cache',
    'Connection': 'close',
    'X-Accel-Buffering': 'no',
}

_ACTIONS_PAYLOAD_CACHE_TTL_SEC_ENV = 'INFO2ACTION_ACTIONS_PAYLOAD_CACHE_TTL_SEC'
_ACTIONS_PAYLOAD_CACHE_TTL_SEC_DEFAULT = 5
_ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_ENV = 'INFO2ACTION_ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC'
_ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_DEFAULT = 60
_ACTIONS_PAYLOAD_LOCK = threading.Lock()
# BE-12(B3): key 空间 = 5 个过滤参数 × user × 分页的笛卡尔组合,原 dict 无界
# (仅 mutation 时整体 clear);改 OrderedDict 有界 LRU。
from collections import OrderedDict as _OrderedDict

_ACTIONS_PAYLOAD_CACHE: "_OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]]" = _OrderedDict()
_ACTIONS_PAYLOAD_CACHE_MAX_ENTRIES = 256
_ACTIONS_PAYLOAD_INFLIGHT: dict[tuple[Any, ...], dict[str, Any]] = {}


def _actions_payload_cache_ttl_sec() -> int:
    raw = os.environ.get(_ACTIONS_PAYLOAD_CACHE_TTL_SEC_ENV, '').strip()
    if not raw:
        return _ACTIONS_PAYLOAD_CACHE_TTL_SEC_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _ACTIONS_PAYLOAD_CACHE_TTL_SEC_DEFAULT


def _actions_payload_stale_fallback_max_sec() -> int:
    raw = os.environ.get(_ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_ENV, '').strip()
    if not raw:
        return _ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _ACTIONS_PAYLOAD_STALE_FALLBACK_MAX_SEC_DEFAULT


def _copy_actions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(payload)


def _copy_actions_payload_if_stale_fallback_allowed(
    cached: tuple[float, dict[str, Any]] | None,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    if not cached:
        return None
    max_age_sec = _actions_payload_stale_fallback_max_sec()
    if max_age_sec <= 0:
        return None
    now_ts = time.time() if now is None else now
    age_sec = max(0, now_ts - cached[0])
    if age_sec > max_age_sec:
        return None
    payload = _copy_actions_payload(cached[1])
    payload['stale_cache'] = True
    payload['stale_cache_age_sec'] = int(age_sec)
    return payload


def _parse_action_source_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
    return []


def _clear_actions_payload_cache() -> None:
    with _ACTIONS_PAYLOAD_LOCK:
        _ACTIONS_PAYLOAD_CACHE.clear()


def _fast_execution_status_for(action: dict[str, Any] | None) -> dict[str, Any]:
    status = (action or {}).get('status')
    if status in ('confirmed', 'executing'):
        return execute_action.get_execution_status((action or {}).get('id'))
    return {'executing': False, 'queued': False}


def _refresh_action_detail_read_model(action_id: str, request: Request) -> None:
    """Rebuild display payload after mutations; best-effort so writes still succeed."""
    try:
        if remote_db.app_state_to_remote():
            action = remote_db.get_action_remote(action_id, user_id=None)
            if not action:
                _delete_action_detail_read_model(action_id)
                return
            owner_user_id = action.get('user_id')
            remote_db.build_action_detail_read_model_remote(
                action_id,
                request_user_id=owner_user_id,
                can_view_all=False,
                owner_user_id=owner_user_id,
                persist=True,
            )
            remote_db.build_action_detail_read_model_remote(
                action_id,
                request_user_id=current_user_id(request),
                can_view_all=True,
                owner_user_id=None,
                persist=True,
            )
            _clear_actions_payload_cache()
            return
        conn = db.get_conn()
        try:
            action = db.get_action(conn, action_id, user_id=None)
            if not action:
                db.delete_action_detail_read_model(conn, action_id)
                _clear_actions_payload_cache()
                return
            owner_user_id = action.get('user_id')
            db.build_action_detail_read_model(
                conn,
                action_id,
                request_user_id=owner_user_id,
                can_view_all=False,
                owner_user_id=owner_user_id,
                persist=True,
            )
            db.build_action_detail_read_model(
                conn,
                action_id,
                request_user_id=current_user_id(request),
                can_view_all=True,
                owner_user_id=None,
                persist=True,
            )
        finally:
            conn.close()
        _clear_actions_payload_cache()
    except Exception as exc:
        print(f"[warn] refresh action detail read model failed for {action_id}: {exc}")


def _delete_action_detail_read_model(action_id: str) -> None:
    try:
        if remote_db.app_state_to_remote():
            remote_db.delete_action_detail_read_model_remote(action_id)
        else:
            conn = db.get_conn()
            try:
                db.delete_action_detail_read_model(conn, action_id)
            finally:
                conn.close()
        _clear_actions_payload_cache()
    except Exception as exc:
        print(f"[warn] delete action detail read model failed for {action_id}: {exc}")


def _get_actions_remote_payload(
    *,
    status: str | None,
    priority: str | None,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    user_id: str | None,
    request_user_id: str | None,
    can_view_all: bool,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    key = (status, priority, action_type, direction, source_filter, user_id, request_user_id, can_view_all, limit, offset)
    now = time.time()
    cache_ttl_sec = _actions_payload_cache_ttl_sec()
    with _ACTIONS_PAYLOAD_LOCK:
        cached = _ACTIONS_PAYLOAD_CACHE.get(key)
        if cached and cache_ttl_sec > 0 and now - cached[0] <= cache_ttl_sec:
            _ACTIONS_PAYLOAD_CACHE.move_to_end(key)  # B3: LRU touch
            return _copy_actions_payload(cached[1])
        holder = _ACTIONS_PAYLOAD_INFLIGHT.get(key)
        leader = holder is None
        if leader:
            holder = {'event': threading.Event()}
            _ACTIONS_PAYLOAD_INFLIGHT[key] = holder

    if not leader:
        event = holder['event']
        if event.wait(timeout=35):
            if holder.get('error'):
                with _ACTIONS_PAYLOAD_LOCK:
                    stale = _ACTIONS_PAYLOAD_CACHE.get(key)
                stale_payload = _copy_actions_payload_if_stale_fallback_allowed(stale)
                if stale_payload is not None:
                    return stale_payload
                raise holder['error']
            return _copy_actions_payload(holder['result'])
        with _ACTIONS_PAYLOAD_LOCK:
            stale = _ACTIONS_PAYLOAD_CACHE.get(key)
        stale_payload = _copy_actions_payload_if_stale_fallback_allowed(stale)
        if stale_payload is not None:
            return stale_payload
        raise TimeoutError("Timed out waiting for in-flight action payload query")

    last_error: Exception | None = None
    try:
        for attempt in range(2):
            try:
                result = remote_db.get_actions_payload_remote(
                    status=status,
                    priority=priority,
                    action_type=action_type,
                    direction=direction,
                    source_filter=source_filter,
                    user_id=user_id,
                    request_user_id=request_user_id,
                    can_view_all=can_view_all,
                    include_source_items=False,
                    include_detail_payloads=True,
                    limit=limit,
                    offset=offset,
                )
                with _ACTIONS_PAYLOAD_LOCK:
                    _ACTIONS_PAYLOAD_CACHE[key] = (time.time(), result)
                    _ACTIONS_PAYLOAD_CACHE.move_to_end(key)
                    while len(_ACTIONS_PAYLOAD_CACHE) > _ACTIONS_PAYLOAD_CACHE_MAX_ENTRIES:
                        _ACTIONS_PAYLOAD_CACHE.popitem(last=False)  # B3: 有界 LRU
                    holder['result'] = result
                return _copy_actions_payload(result)
            except remote_db.RemoteDBError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.25)
        raise last_error or RuntimeError("Remote action payload query failed")
    except Exception as exc:
        with _ACTIONS_PAYLOAD_LOCK:
            holder['error'] = exc
            stale = _ACTIONS_PAYLOAD_CACHE.get(key)
        stale_payload = _copy_actions_payload_if_stale_fallback_allowed(stale)
        if stale_payload is not None:
            return stale_payload
        raise
    finally:
        with _ACTIONS_PAYLOAD_LOCK:
            if _ACTIONS_PAYLOAD_INFLIGHT.get(key) is holder:
                _ACTIONS_PAYLOAD_INFLIGHT.pop(key, None)
            holder['event'].set()


def _get_actions_local_payload(
    *,
    status: str | None,
    priority: str | None,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    user_id: str | None,
    can_view_all: bool,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        actions = db.get_actions(conn, status=status, priority=priority,
                                 action_type=action_type, direction=direction,
                                 user_id=user_id)
        if source_filter == 'with-source':
            actions = [action for action in actions if _action_source_ids_for_filter(action)]
        elif source_filter == 'no-source':
            actions = [action for action in actions if not _action_source_ids_for_filter(action)]
        actions = actions[offset:offset + limit]
        viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
        detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(actions)
        detail_payloads = db.get_action_detail_read_models(
            conn,
            detail_action_ids,
            viewer_scope=viewer_scope,
            owner_user_id=user_id,
        )
        actions = [
            action_detail_read_model.merge_action_with_detail_payload(
                action,
                detail_payloads.get(str(action.get('id'))),
            )
            for action in actions
        ]
        counts = db.get_action_counts(conn, user_id=user_id)
        direction_where = "WHERE status IN ('pending','confirmed','executing','dispatched')"
        direction_params = []
        if user_id:
            direction_where += " AND user_id = ?"
            direction_params.append(user_id)
        dir_rows = conn.execute(
            "SELECT direction, direction_label, COUNT(*) as cnt "
            f"FROM actions {direction_where} "
            "GROUP BY direction ORDER BY cnt DESC",
            direction_params,
        ).fetchall()
        directions = [{'slug': r[0], 'label': r[1], 'count': r[2]} for r in dir_rows]
        return {
            'actions': actions,
            'counts': counts,
            'directions': directions,
            'meta': {
                'limit': limit,
                'offset': offset,
                'degraded': False,
                'query_strategy': 'legacy_actions_paginated',
            },
        }
    finally:
        conn.close()


def _is_inside_action_date_filter(action: dict[str, Any], date_filter: str | None) -> bool:
    if date_filter not in {'today', 'week'}:
        return True
    raw = action.get('created_at')
    if not raw:
        return True
    try:
        created = datetime.fromisoformat(str(raw).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return True
    now = datetime.now()
    if date_filter == 'today':
        return created.date() == now.date()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    return created >= start


def _action_source_ids_for_filter(action: dict[str, Any]) -> list[str]:
    value = action.get('source_item_ids')
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item]


def _shape_actions_board_payload(
    *,
    actions: list[dict[str, Any]],
    counts: dict[str, Any],
    limit_per_direction: int,
    offset: int,
    status: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit_per_direction or 20), 50))
    start = max(0, int(offset or 0))
    lanes = _action_board_lanes_for_status(status)
    grouped: dict[str, dict[str, Any]] = {
        lane['slug']: {
            'slug': lane['slug'],
            'label': lane['label'],
            'count': 0,
            'items': [],
        }
        for lane in lanes
    }
    for action in actions:
        slug = _action_board_lane_for_status(str(action.get('status') or ''))
        if slug not in grouped:
            continue
        entry = grouped[slug]
        entry['count'] += 1
        entry['items'].append(action)

    directions = list(grouped.values())
    board_directions: list[dict[str, Any]] = []
    for entry in directions:
        total = int(entry['count'])
        entry['items'].sort(key=_action_created_at_sort_key, reverse=True)
        items = entry['items'][start:start + limit]
        loaded_until = start + len(items)
        board_directions.append({
            'slug': entry['slug'],
            'label': entry['label'],
            'count': total,
            'items': items,
            'has_more': total > loaded_until,
            'next_offset': loaded_until if total > loaded_until else None,
        })

    shaped_counts = {status: 0 for status in _ACTION_BOARD_VISIBLE_STATUSES}
    shaped_counts.update({str(key): int(value or 0) for key, value in counts.items()})
    shaped_counts['total'] = sum(int(v or 0) for v in counts.values())
    shaped_counts['in_progress'] = sum(
        int(shaped_counts.get(status, 0) or 0)
        for status in {'confirmed', 'executing', 'dispatched'}
    )
    return {
        'counts': shaped_counts,
        'directions': board_directions,
        'meta': {
            'limit_per_direction': limit,
            'offset': start,
            'degraded': False,
            'read_model': False,
            'query_strategy': 'status_lanes',
        },
    }


def _action_counts_from_actions(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        status = str(action.get('status') or 'unknown')
        counts[status] = counts.get(status, 0) + 1
    return counts


def _action_board_lane_for_status(status: str) -> str | None:
    for lane in _ACTION_BOARD_LANES:
        if status in lane['statuses']:
            return str(lane['slug'])
    return None


def _action_board_lanes_for_status(status: str | None) -> tuple[dict[str, Any], ...]:
    if not status:
        return _ACTION_BOARD_LANES
    if status == 'in_progress':
        return tuple(lane for lane in _ACTION_BOARD_LANES if lane['slug'] == 'in_progress')
    lane_slug = _action_board_lane_for_status(status)
    if not lane_slug:
        return ()
    return tuple(lane for lane in _ACTION_BOARD_LANES if lane['slug'] == lane_slug)


def _action_created_at_sort_key(action: dict[str, Any]) -> float:
    value = action.get('created_at')
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0.0


def _get_actions_board_local_payload(
    *,
    status: str | None,
    priority: str | None,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    date_filter: str | None,
    user_id: str | None,
    can_view_all: bool,
    limit_per_direction: int,
    offset: int,
    include_detail_payloads: bool = False,
) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        all_actions = db.get_actions(
            conn,
            status=status,
            priority=priority,
            action_type=action_type,
            direction=direction,
            user_id=user_id,
        )
        if source_filter == 'with-source':
            all_actions = [action for action in all_actions if _action_source_ids_for_filter(action)]
        elif source_filter == 'no-source':
            all_actions = [action for action in all_actions if not _action_source_ids_for_filter(action)]
        filtered_actions = [
            action for action in all_actions
            if _is_inside_action_date_filter(action, date_filter)
            and str(action.get('status') or '') in _ACTION_BOARD_VISIBLE_STATUSES
        ]
        payload = _shape_actions_board_payload(
            actions=filtered_actions,
            counts=_action_counts_from_actions(filtered_actions),
            limit_per_direction=limit_per_direction,
            offset=offset,
            status=status,
        )
        if include_detail_payloads:
            viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
            board_actions = [
                action
                for direction_payload in payload['directions']
                for action in direction_payload['items']
            ]
            detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(board_actions)
            detail_payloads = db.get_action_detail_read_models(
                conn,
                detail_action_ids,
                viewer_scope=viewer_scope,
                owner_user_id=user_id,
            )
            for direction_payload in payload['directions']:
                direction_payload['items'] = [
                    action_detail_read_model.merge_action_with_detail_payload(
                        action,
                        detail_payloads.get(str(action.get('id'))),
                    )
                    for action in direction_payload['items']
                ]
        payload.setdefault('meta', {})['detail_included'] = bool(include_detail_payloads)
        return payload
    finally:
        conn.close()


def _get_actions_by_item_local(item_id: str, user_id: str | None) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        scope_clause = "AND user_id = ?" if user_id else ""
        params = [f'%{item_id}%']
        if user_id:
            params.append(user_id)
        rows = conn.execute(f"""
            SELECT id, title, action_type, priority, status, reason, created_at, source_item_ids
            FROM actions
            WHERE source_item_ids LIKE ? {scope_clause}
            ORDER BY created_at DESC
        """, params).fetchall()
        actions = []
        for r in rows:
            d = dict(r)
            if item_id in _parse_action_source_ids(d.get('source_item_ids', '[]')):
                d['source_item_ids'] = _parse_action_source_ids(d.get('source_item_ids', '[]'))
                actions.append(d)
        return actions
    finally:
        conn.close()


def _get_action_detail_remote_payload(
    action_id: str,
    *,
    scope_user_id: str | None,
    request_user_id: str | None,
    can_view_all: bool,
) -> dict[str, Any] | None:
    viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
    cached = remote_db.get_action_detail_read_model_remote(
        action_id,
        viewer_scope=viewer_scope,
        owner_user_id=scope_user_id,
    )
    if cached:
        cached['execution_status'] = _fast_execution_status_for(cached)
        return cached
    payload = remote_db.build_action_detail_read_model_remote(
        action_id,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
        owner_user_id=scope_user_id,
        persist=True,
    )
    if not payload:
        return None
    payload['execution_status'] = _fast_execution_status_for(payload)
    return payload


def _get_action_detail_local_payload(
    action_id: str,
    *,
    scope_user_id: str | None,
    request_user_id: str | None,
    can_view_all: bool,
) -> dict[str, Any] | None:
    viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
    conn = db.get_conn()
    try:
        cached = db.get_action_detail_read_model(
            conn,
            action_id,
            viewer_scope=viewer_scope,
            owner_user_id=scope_user_id,
        )
        if cached:
            cached['execution_status'] = _fast_execution_status_for(cached)
            return cached
        payload = db.build_action_detail_read_model(
            conn,
            action_id,
            request_user_id=request_user_id,
            can_view_all=can_view_all,
            owner_user_id=scope_user_id,
            persist=True,
        )
        if not payload:
            return None
        payload['execution_status'] = _fast_execution_status_for(payload)
        return payload
    finally:
        conn.close()


# ── GET endpoints ────────────────────────────────────────────


@router.get('/api/actions')
async def get_actions(
    request: Request,
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    source_filter: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    normalized_source_filter = source_filter if source_filter in {'with-source', 'no-source'} else None
    try:
        scope_user_id = owner_scope_user_id(request)
        if remote_db.app_state_to_remote():
            return await run_in_threadpool(
                _get_actions_remote_payload,
                status=status,
                priority=priority,
                action_type=action_type,
                direction=direction,
                source_filter=normalized_source_filter,
                user_id=scope_user_id,
                request_user_id=current_user_id(request),
                can_view_all=can_access_all(request),
                limit=limit,
                offset=offset,
            )
        return await run_in_threadpool(
            _get_actions_local_payload,
            status=status,
            priority=priority,
            action_type=action_type,
            direction=direction,
            source_filter=normalized_source_filter,
            user_id=scope_user_id,
            can_view_all=can_access_all(request),
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        print(f"[error] GET /api/actions failed: {e}")
        return JSONResponse(
            {'error': f'Database error: {str(e)[:200]}', 'actions': [], 'counts': {}, 'directions': []},
            status_code=500,
        )


@router.get('/api/actions/board')
async def get_actions_board(
    request: Request,
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    source_filter: Optional[str] = Query(None),
    date_filter: Optional[str] = Query(None),
    limit_per_direction: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    include_detail: bool = Query(False),
):
    normalized_date_filter = date_filter if date_filter in {'today', 'week'} else None
    normalized_source_filter = source_filter if source_filter in {'with-source', 'no-source'} else None
    try:
        scope_user_id = owner_scope_user_id(request)
        if remote_db.app_state_to_remote():
            return await run_in_threadpool(
                remote_db.get_actions_board_payload_remote,
                status=status,
                priority=priority,
                action_type=action_type,
                direction=direction,
                source_filter=normalized_source_filter,
                date_filter=normalized_date_filter,
                user_id=scope_user_id,
                request_user_id=current_user_id(request),
                can_view_all=can_access_all(request),
                limit_per_direction=limit_per_direction,
                offset=offset,
                include_detail_payloads=include_detail,
            )
        return await run_in_threadpool(
            _get_actions_board_local_payload,
            status=status,
            priority=priority,
            action_type=action_type,
            direction=direction,
            source_filter=normalized_source_filter,
            date_filter=normalized_date_filter,
            user_id=scope_user_id,
            can_view_all=can_access_all(request),
            limit_per_direction=limit_per_direction,
            offset=offset,
            include_detail_payloads=include_detail,
        )
    except Exception as e:
        print(f"[error] GET /api/actions/board failed: {e}")
        return JSONResponse(
            {
                'error': f'Database error: {str(e)[:200]}',
                'counts': {},
                'directions': [],
                'meta': {'degraded': True},
            },
            status_code=500,
        )


@router.get('/api/admin/actions/read-model/freshness')
async def get_actions_read_model_freshness(request: Request):
    """Read-only admin probe for Action Tab detail read-model freshness."""
    err = require_admin(request)
    if err:
        return err
    if not remote_db.app_state_to_remote():
        return {
            "enabled": False,
            "read_model": "action_detail_read_model",
            "data_backend": remote_db.feed_read_backend(),
            "reason": "remote_actions_disabled",
        }
    try:
        return await run_in_threadpool(remote_db.action_detail_read_model_freshness_remote)
    except remote_db.RemoteDBError as exc:
        return JSONResponse({
            'error': 'Remote action read-model probe failed',
            'detail': str(exc),
            'data_backend': remote_db.feed_read_backend(),
        }, status_code=503)


@router.get('/api/actions/by-item')
async def get_actions_by_item(request: Request, item_id: Optional[str] = Query(None)):
    if not item_id:
        return JSONResponse({'error': 'item_id required'}, status_code=400)
    if not can_access_all(request) and not current_user_id(request):
        return {'actions': []}
    try:
        scope_user_id = owner_scope_user_id(request)
        if remote_db.app_state_to_remote():
            return {
                'actions': await run_in_threadpool(
                    remote_db.get_actions_by_item_remote,
                    item_id,
                    user_id=scope_user_id,
                )
            }

        actions = await run_in_threadpool(_get_actions_by_item_local, item_id, scope_user_id)
        return {'actions': actions}
    except Exception as e:
        print(f"[error] GET /api/actions/by-item failed: {e}")
        return JSONResponse(
            {'error': f'Database error: {str(e)[:200]}', 'actions': []},
            status_code=500,
        )


@router.get('/api/actions/{action_id}')
async def get_action_detail(action_id: str, request: Request):
    try:
        scope_user_id = owner_scope_user_id(request)
        request_user_id = current_user_id(request)
        can_view_all = can_access_all(request)
        if remote_db.app_state_to_remote():
            action = await run_in_threadpool(
                _get_action_detail_remote_payload,
                action_id,
                scope_user_id=scope_user_id,
                request_user_id=request_user_id,
                can_view_all=can_view_all,
            )
            if not action:
                return JSONResponse({'error': 'Action not found'}, status_code=404)
            return action

        action = await run_in_threadpool(
            _get_action_detail_local_payload,
            action_id,
            scope_user_id=scope_user_id,
            request_user_id=request_user_id,
            can_view_all=can_view_all,
        )
        if not action:
            return JSONResponse({'error': 'Action not found'}, status_code=404)
        return action
    except Exception as e:
        print(f"[error] GET /api/actions/{action_id} failed: {e}")
        return JSONResponse({'error': f'Database error: {str(e)[:200]}'}, status_code=500)


@router.get('/api/actions/{action_id}/stream')
async def get_action_stream(action_id: str, request: Request, offset: int = Query(0)):
    err = require_admin(request)
    if err:
        return err
    lines, total = execute_action.read_stream_log(action_id, offset)
    exec_status = execute_action.get_execution_status(action_id)
    return {
        'lines': lines,
        'total': total,
        'offset': offset,
        'executing': exec_status.get('executing', False),
    }


# ── POST endpoints ───────────────────────────────────────────


@router.post('/api/actions/auto-generate')
async def auto_generate_actions(request: Request):
    err = require_admin(request)
    if err:
        return err

    def _bg_generate():
        try:
            subprocess.run(
                [sys.executable or 'python3', os.path.join(BASE, 'src', 'generate_actions.py')],
                cwd=BASE, timeout=600, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            print(f"Action generation error: {e}")

    threading.Thread(target=_bg_generate, daemon=True).start()
    return {'ok': True, 'msg': 'Action generation started'}


@router.post('/api/actions/generate-from-item')
async def generate_from_item(request: Request):
    err = require_admin(request)
    if err:
        return err

    body = await request.json()
    item_id = body.get('item_id', '')
    if not item_id:
        return JSONResponse({'error': 'item_id required'}, status_code=400)

    req_action_type = body.get('action_type', '')
    req_user_hint = body.get('user_hint', '')

    if remote_db.app_state_to_remote():
        row = remote_db.get_item_action_context_remote(item_id)
    else:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT id, platform, title, content, ai_summary, ai_key_points, ai_category, detail_json "
            "FROM items WHERE id = ?", (item_id,),
        ).fetchone()
        conn.close()
    if not row:
        return JSONResponse({'error': 'Item not found'}, status_code=404)
    item = dict(row)

    def generator():
        def sse(event, data):
            yield f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

        # We can't yield from nested sse() calls directly — use inline yields instead
        try:
            import generate_actions as ga
            stages = []
            t0 = time.time()

            # Stage 1: Content analysis
            yield f'event: thinking\ndata: {json.dumps({"stage": 0, "text": item.get("title", "") or "(无标题)"}, ensure_ascii=False)}\n\n'
            item_platform = item.get('platform', '未知')
            item_category = item.get('ai_category', '未知')
            yield f'event: thinking\ndata: {json.dumps({"stage": 0, "text": f"来源 {item_platform}，分类为{item_category}"}, ensure_ascii=False)}\n\n'

            kp = item.get('ai_key_points', '')
            if kp:
                try:
                    kp_list = json.loads(kp) if isinstance(kp, str) else kp
                    if isinstance(kp_list, list) and kp_list:
                        for kp_item in kp_list[:3]:
                            if isinstance(kp_item, dict):
                                t = kp_item.get('title', '')
                                pts = kp_item.get('points', [])
                                if t:
                                    yield f'event: thinking\ndata: {json.dumps({"stage": 0, "text": t}, ensure_ascii=False)}\n\n'
                                for pt in (pts[:2] if isinstance(pts, list) else []):
                                    yield f'event: thinking\ndata: {json.dumps({"stage": 0, "text": pt}, ensure_ascii=False)}\n\n'
                            elif isinstance(kp_item, str):
                                yield f'event: thinking\ndata: {json.dumps({"stage": 0, "text": kp_item}, ensure_ascii=False)}\n\n'
                except (json.JSONDecodeError, TypeError):
                    pass
            yield f'event: stage\ndata: {json.dumps({"index": 0, "status": "done"})}\n\n'
            stages.append({'name': '内容分析', 'duration_ms': int((time.time() - t0) * 1000)})

            # Stage 2: Load project context
            t1 = time.time()
            yield f'event: stage\ndata: {json.dumps({"index": 1, "status": "active"})}\n\n'
            cfg = _load_json(os.path.join(BASE, 'config', 'config.json')) or {}
            ai = cfg.get('ai_summary', {})
            api_key, api_base, model = ga.resolve_minimax_chat_config(ai)

            yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": "已读取 WORKSPACE-MANIFEST.md，获取项目定位与方向"}, ensure_ascii=False)}\n\n'
            manifest = ga.load_manifest()
            manifest_len = len(manifest) if manifest else 0
            yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": f"已读取 WORKSPACE-PULSE.json，获取实时工作状态 ({manifest_len} chars)"}, ensure_ascii=False)}\n\n'
            pulse_fields = ga.load_pulse()
            active_work = pulse_fields.get('pulse_active_work', '')
            if active_work:
                first_line = active_work.split('\n')[0]
                yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": f"当前活跃工作 {first_line}"}, ensure_ascii=False)}\n\n'
            yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": "已读取 directions.yaml，获取行动方向框架"}, ensure_ascii=False)}\n\n'
            _, directions_text = ga.load_directions()

            user_guidance = ""
            if req_action_type or req_user_hint:
                parts = []
                if req_action_type:
                    action_type_label = _ACTION_TYPE_LABELS.get(req_action_type, req_action_type)
                    parts.append(f"- 用户指定行动类型：**{action_type_label}**（action_type 必须为 {req_action_type}）")
                    yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": f"用户指定类型为{action_type_label}"}, ensure_ascii=False)}\n\n'
                if req_user_hint:
                    parts.append(f"- 用户预期方向：**{req_user_hint}**")
                    yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": f"用户预期 {req_user_hint}"}, ensure_ascii=False)}\n\n'
                parts.append("- **重要**：生成的行动点必须与用户指定的类型和预期方向一致。如果用户明确了方向，即使你认为其他方向更有价值，也应优先按用户意图生成。")
                user_guidance = "\n".join(parts)
            system_prompt = ga.build_analysis_prompt(manifest, "", directions_text, pulse_fields, user_guidance=user_guidance)
            yield f'event: thinking\ndata: {json.dumps({"stage": 1, "text": f"上下文组装完成，prompt 共 {len(system_prompt)} 字符"}, ensure_ascii=False)}\n\n'
            yield f'event: stage\ndata: {json.dumps({"index": 1, "status": "done"})}\n\n'
            stages.append({'name': '读取项目上下文', 'duration_ms': int((time.time() - t1) * 1000)})

            # Stage 3: AI evaluation
            t2 = time.time()
            yield f'event: stage\ndata: {json.dumps({"index": 2, "status": "active"})}\n\n'
            yield f'event: thinking\ndata: {json.dumps({"stage": 2, "text": f"调用 {model} 进行五维评估"}, ensure_ascii=False)}\n\n'
            yield f'event: thinking\ndata: {json.dumps({"stage": 2, "text": "评估维度 相关性、可行动性、时效性、增量价值、投入产出"}, ensure_ascii=False)}\n\n'

            # Collect thinking-ai events to yield later
            thinking_events = []

            def on_thinking(text):
                thinking_events.append(text)

            result_tuple = ga.process_single_item_streaming(
                item, api_key, api_base, model, system_prompt,
                on_thinking=on_thinking,
            )
            # Flush accumulated thinking events
            for text in thinking_events:
                yield f'event: thinking-ai\ndata: {json.dumps({"stage": 2, "text": text}, ensure_ascii=False)}\n\n'

            print(
                f"[sse] process_single_item_streaming returned: action={'yes' if result_tuple[0] else 'None'}, "
                f"scores={result_tuple[1] is not None}",
                flush=True,
            )
            action, scores = result_tuple[0], result_tuple[1]
            log_path = result_tuple[2] if len(result_tuple) > 2 else None
            if scores:
                total = sum(int(v) for v in scores.values() if isinstance(v, (int, float)))
                yield f'event: thinking\ndata: {json.dumps({"stage": 2, "text": f"评分结果 {total}/50"}, ensure_ascii=False)}\n\n'
            yield f'event: stage\ndata: {json.dumps({"index": 2, "status": "done"})}\n\n'
            stages.append({'name': 'AI 评估', 'duration_ms': int((time.time() - t2) * 1000)})

            # Stage 4: Finalize
            t3 = time.time()
            yield f'event: stage\ndata: {json.dumps({"index": 3, "status": "active"})}\n\n'
            if action:
                action = _normalize_manual_action(action, item, item_id, req_action_type, req_user_hint)
            else:
                action = _build_manual_fallback_action(item, item_id, req_action_type, req_user_hint)
                yield f'event: thinking\ndata: {json.dumps({"stage": 3, "text": "模型未返回行动点，已生成保守兜底方向"}, ensure_ascii=False)}\n\n'

            persisted_action = _persist_manual_item_action(action, request)
            _refresh_action_detail_read_model(persisted_action['id'], request)
            action_title = persisted_action.get('title', '')
            yield f'event: thinking\ndata: {json.dumps({"stage": 3, "text": f"生成行动点 {action_title}"}, ensure_ascii=False)}\n\n'
            yield f'event: stage\ndata: {json.dumps({"index": 3, "status": "done"})}\n\n'
            stages.append({'name': '整理结果', 'duration_ms': int((time.time() - t3) * 1000)})
            yield f'event: result\ndata: {json.dumps({"ok": True, "action": persisted_action, "scores": scores, "stages": stages, "log_path": log_path}, ensure_ascii=False)}\n\n'
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f'event: error\ndata: {json.dumps({"error": str(e)[:200]}, ensure_ascii=False)}\n\n'

    return StreamingResponse(generator(), media_type='text/event-stream', headers=_SSE_HEADERS)


@router.post('/api/actions')
async def create_action(request: Request):
    body = await request.json()
    title = body.get('title', '').strip()
    prompt = body.get('prompt', '').strip()
    action_type = body.get('action_type', 'implement')
    if not title or not prompt:
        return JSONResponse({'error': 'title and prompt required'}, status_code=400)
    if remote_db.app_state_to_remote():
        try:
            action_id = remote_db.create_action_remote(
                source_type=body.get('source_type', 'manual'),
                title=title,
                action_type=action_type,
                prompt=prompt,
                source_item_ids=body.get('source_item_ids', []),
                reason=body.get('reason', ''),
                priority=body.get('priority', 'medium'),
                related_project=body.get('related_project'),
                direction=body.get('direction', '_uncategorized'),
                direction_label=body.get('direction_label', '待归类'),
                user_id=current_user_id(request),
            )
            await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
            return {'ok': True, 'id': action_id}
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        action_id = db.create_action(
            conn,
            source_type=body.get('source_type', 'manual'),
            title=title,
            action_type=action_type,
            prompt=prompt,
            source_item_ids=body.get('source_item_ids', []),
            reason=body.get('reason', ''),
            priority=body.get('priority', 'medium'),
            related_project=body.get('related_project'),
            direction=body.get('direction', '_uncategorized'),
            direction_label=body.get('direction_label', '待归类'),
            user_id=current_user_id(request),
        )
        await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
        return {'ok': True, 'id': action_id}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


@router.post('/api/actions/{action_id}/dispatch')
async def dispatch_action(action_id: str, request: Request):
    # Require per-user Discord bot token — no fallback to global
    user = getattr(request.state, 'user', None)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if not user.get('discord_bot_token_enc'):
        return JSONResponse({'error': '请先在设置中配置 Discord Bot Token'}, status_code=400)
    try:
        from utils.crypto import decrypt
        user_bot_token = decrypt(user['discord_bot_token_enc'])
    except Exception:
        return JSONResponse({'error': 'Discord Token 解密失败，请重新配置'}, status_code=500)

    scope_user_id = owner_scope_user_id(request)

    def _dispatch_blocking():
        conn = None
        try:
            if remote_db.app_state_to_remote():
                action = remote_db.get_action_remote(action_id, user_id=scope_user_id)
            else:
                conn = db.get_conn()
                action = db.get_action(conn, action_id, user_id=scope_user_id)
            if not action:
                return ('error', 404, 'Action not found')
            if action['status'] not in ('pending',):
                return ('error', 400, f"Cannot dispatch action in status '{action['status']}'")
            thread_id, thread_url = _dispatch_to_discord(action, bot_token=user_bot_token)
            now = datetime.now().isoformat()
            if remote_db.app_state_to_remote():
                remote_db.update_action_remote(
                    action_id,
                    status='dispatched',
                    discord_thread_id=thread_id,
                    discord_thread_url=thread_url,
                    dispatched_at=now,
                )
                remote_db.log_action_event_remote(None, action_id, 'dispatched', {
                    'thread_id': thread_id,
                    'thread_url': thread_url,
                })
            else:
                db.update_action(conn, action_id,
                                 status='dispatched',
                                 discord_thread_id=thread_id,
                                 discord_thread_url=thread_url,
                                 dispatched_at=now)
                db._log_action_event(conn, action_id, 'dispatched', {
                    'thread_id': thread_id,
                    'thread_url': thread_url,
                })
            return ('ok', thread_id, thread_url)
        finally:
            if conn is not None:
                conn.close()

    try:
        result = await run_in_threadpool(_dispatch_blocking)
    except Exception as e:
        print(f'[dispatch] error: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
    if result[0] == 'error':
        return JSONResponse({'error': result[2]}, status_code=result[1])
    _, thread_id, thread_url = result
    await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
    return {'ok': True, 'thread_id': thread_id, 'thread_url': thread_url}


@router.post('/api/actions/{action_id}/done')
async def mark_action_done(action_id: str, request: Request):
    try:
        scope_user_id = owner_scope_user_id(request)
        if remote_db.app_state_to_remote():
            action = remote_db.get_action_remote(action_id, user_id=scope_user_id)
        else:
            conn = db.get_conn()
            action = db.get_action(conn, action_id, user_id=scope_user_id)
        if not action:
            return JSONResponse({'error': 'Action not found'}, status_code=404)
        if action['status'] not in ('dispatched', 'executing', 'confirmed'):
            return JSONResponse(
                {'error': f"Cannot mark done: action in status '{action['status']}'"},
                status_code=400,
            )
        now = datetime.now().isoformat()
        if remote_db.app_state_to_remote():
            remote_db.update_action_remote(
                action_id,
                owner_user_id=scope_user_id,
                status='done',
                completed_at=now,
            )
            remote_db.log_action_event_remote(None, action_id, 'done', {})
        else:
            db.update_action(conn, action_id, owner_user_id=scope_user_id,
                             status='done', completed_at=now)
            db._log_action_event(conn, action_id, 'done', {})
        await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
        return {'ok': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        if not remote_db.app_state_to_remote():
            conn.close()


@router.post('/api/actions/{action_id}/confirm')
async def confirm_action(action_id: str, request: Request):
    err = require_admin(request)
    if err:
        return err

    body = await request.json()
    tool = body.get('tool', 'codex')
    conn = None
    if remote_db.app_state_to_remote():
        action = remote_db.get_action_remote(action_id)
    else:
        conn = db.get_conn()
        action = db.get_action(conn, action_id)
    if not action:
        if conn:
            conn.close()
        return JSONResponse({'error': 'Action not found'}, status_code=404)
    if action['status'] not in ('pending',):
        if conn:
            conn.close()
        return JSONResponse(
            {'error': f"Cannot confirm action in status '{action['status']}'"},
            status_code=400,
        )
    if remote_db.app_state_to_remote():
        remote_db.update_action_remote(
            action_id,
            status='confirmed',
            confirmed_at=datetime.now().isoformat(),
        )
        remote_db.log_action_event_remote(None, action_id, 'confirmed', {'tool': tool})
    else:
        db.update_action(conn, action_id, status='confirmed',
                         confirmed_at=datetime.now().isoformat())
        db._log_action_event(conn, action_id, 'confirmed', {'tool': tool})
        conn.close()
    await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
    result = execute_action.start_execution(action_id, tool=tool)
    return result


@router.post('/api/actions/{action_id}/dismiss')
async def dismiss_action(action_id: str, request: Request):
    body = await request.json()
    scope_user_id = owner_scope_user_id(request)
    if remote_db.app_state_to_remote():
        action = remote_db.get_action_remote(action_id, user_id=scope_user_id)
    else:
        conn = db.get_conn()
        action = db.get_action(conn, action_id, user_id=scope_user_id)
    if not action:
        if not remote_db.app_state_to_remote():
            conn.close()
        return JSONResponse({'error': 'Action not found'}, status_code=404)
    event_detail = {
        'feedback_type': body.get('feedback_type', ''),
        'feedback_text': body.get('feedback_text', ''),
    }
    if remote_db.app_state_to_remote():
        remote_db.update_action_remote(
            action_id,
            owner_user_id=scope_user_id,
            status='dismissed',
            dismissed_at=datetime.now().isoformat(),
        )
        remote_db.log_action_event_remote(None, action_id, 'dismissed', event_detail)
    else:
        db.update_action(conn, action_id, owner_user_id=scope_user_id, status='dismissed',
                         dismissed_at=datetime.now().isoformat())
        db._log_action_event(conn, action_id, 'dismissed', event_detail)
        conn.close()
    await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
    return {'ok': True}


@router.post('/api/actions/{action_id}/execute')
async def execute_action_endpoint(action_id: str, request: Request):
    err = require_admin(request)
    if err:
        return err

    body = await request.json()
    tool = body.get('tool', 'codex')
    conn = None
    if remote_db.app_state_to_remote():
        action = remote_db.get_action_remote(action_id)
    else:
        conn = db.get_conn()
        action = db.get_action(conn, action_id)
    if not action:
        if conn:
            conn.close()
        return JSONResponse({'error': 'Action not found'}, status_code=404)
    if remote_db.app_state_to_remote():
        remote_db.update_action_remote(
            action_id,
            status='confirmed',
            execution_result=None,
            execution_exit_code=None,
            completed_at=None,
            confirmed_at=datetime.now().isoformat(),
        )
    else:
        db.update_action(conn, action_id, status='confirmed',
                         execution_result=None, execution_exit_code=None,
                         completed_at=None, confirmed_at=datetime.now().isoformat())
        conn.close()
    await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
    result = execute_action.start_execution(action_id, tool=tool)
    return result


@router.post('/api/actions/{action_id}/feedback')
async def action_feedback(action_id: str, request: Request):
    body = await request.json()
    phase = body.get('phase', '')
    rating = body.get('rating', '')
    comment = body.get('comment', '')
    if not phase or not rating:
        return JSONResponse({'error': 'phase and rating required'}, status_code=400)
    if remote_db.app_state_to_remote():
        try:
            if not remote_db.get_action_remote(action_id, user_id=owner_scope_user_id(request)):
                return JSONResponse({'error': 'Action not found'}, status_code=404)
            remote_db.add_action_feedback_remote(action_id, phase, rating, comment or None)
            return {'ok': True}
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        if not db.get_action(conn, action_id, user_id=owner_scope_user_id(request)):
            return JSONResponse({'error': 'Action not found'}, status_code=404)
        db.add_action_feedback(conn, action_id, phase, rating, comment or None)
        return {'ok': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


# ── PATCH endpoints ──────────────────────────────────────────


@router.patch('/api/actions/{action_id}/priority')
async def update_action_priority(action_id: str, request: Request):
    body = await request.json()
    new_priority = body.get('priority', '')
    if new_priority not in ('high', 'medium', 'low', 'bug'):
        return JSONResponse({'error': 'Invalid priority'}, status_code=400)
    scope_user_id = owner_scope_user_id(request)
    if remote_db.app_state_to_remote():
        action = remote_db.get_action_remote(action_id, user_id=scope_user_id)
    else:
        conn = db.get_conn()
        action = db.get_action(conn, action_id, user_id=scope_user_id)
    if not action:
        if not remote_db.app_state_to_remote():
            conn.close()
        return JSONResponse({'error': 'Action not found'}, status_code=404)
    old_priority = action.get('priority', '')
    if old_priority != new_priority:
        event = {'field': 'priority', 'before': old_priority, 'after': new_priority}
        if remote_db.app_state_to_remote():
            remote_db.update_action_remote(
                action_id,
                owner_user_id=scope_user_id,
                priority=new_priority,
            )
            remote_db.log_action_event_remote(None, action_id, 'edited', event)
        else:
            db.update_action(conn, action_id, owner_user_id=scope_user_id, priority=new_priority)
            db._log_action_event(conn, action_id, 'edited', event)
    elif not remote_db.app_state_to_remote():
        db.update_action(conn, action_id, owner_user_id=scope_user_id, priority=new_priority)
    if not remote_db.app_state_to_remote():
        conn.close()
    await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
    return {'ok': True}


@router.patch('/api/actions/{action_id}')
async def update_action(action_id: str, request: Request):
    err = require_admin(request)
    if err:
        return err

    body = await request.json()
    if remote_db.app_state_to_remote():
        try:
            action = remote_db.get_action_remote(action_id)
            if action:
                for field in ('title', 'prompt', 'reason', 'priority', 'action_type'):
                    new_val = body.get(field)
                    if new_val is not None and new_val != action.get(field):
                        remote_db.log_action_event_remote(None, action_id, 'edited', {
                            'field': field,
                            'before': action.get(field),
                            'after': new_val,
                        })
            ok = remote_db.update_action_remote(action_id, **{
                k: body[k] for k in ('title', 'prompt', 'reason', 'priority',
                                      'status', 'action_type', 'related_project')
                if k in body
            })
            if ok:
                await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
            return {'ok': ok}
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        action = db.get_action(conn, action_id)
        if action:
            for field in ('title', 'prompt', 'reason', 'priority', 'action_type'):
                new_val = body.get(field)
                if new_val is not None and new_val != action.get(field):
                    db._log_action_event(conn, action_id, 'edited', {
                        'field': field,
                        'before': action.get(field),
                        'after': new_val,
                    })
        ok = db.update_action(conn, action_id, **{
            k: body[k] for k in ('title', 'prompt', 'reason', 'priority',
                                  'status', 'action_type', 'related_project')
            if k in body
        })
        if ok:
            await run_in_threadpool(_refresh_action_detail_read_model, action_id, request)
        return {'ok': ok}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


# ── DELETE endpoint ──────────────────────────────────────────


@router.delete('/api/actions/{action_id}')
async def delete_action(action_id: str, request: Request):
    if remote_db.app_state_to_remote():
        ok = remote_db.delete_action_remote(action_id, owner_user_id=owner_scope_user_id(request))
        if not ok:
            return JSONResponse({'error': 'Action not found'}, status_code=404)
        await run_in_threadpool(_delete_action_detail_read_model, action_id)
        return {'ok': ok}

    conn = db.get_conn()
    try:
        ok = db.delete_action(conn, action_id, owner_user_id=owner_scope_user_id(request))
        if not ok:
            return JSONResponse({'error': 'Action not found'}, status_code=404)
        await run_in_threadpool(_delete_action_detail_read_model, action_id)
        return {'ok': ok}
    finally:
        conn.close()
