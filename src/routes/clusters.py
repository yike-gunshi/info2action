"""v15.0/v15.1 event-aggregation REST endpoints.

Endpoints (PRD §6.12 + §6.13):
  GET  /api/feed/events                     — cluster timeline (visible confirmed events)
  GET  /api/clusters/{id}                   — cluster detail (merged_into redirect)
  GET  /api/clusters/{id}/sources           — member items (primary first)
  POST /api/clusters/{id}/click             — cluster click heartbeat (per-user)
  POST /api/clusters/{id}/seen              — v15.1 mark cluster as seen at live_version
  POST /api/clusters/{id}/star              — v18.1 toggle cluster favorite
  POST /api/clusters/{id}/actions           — SSE generate action point from cluster
  GET  /api/clusters/{id}/actions           — user's actions tied to this cluster
  GET  /api/search?q=&context=              — context-aware search (recommend=both)

SSE: Connection: close (feedback_sse_connection_close).
Auth: public reads for event timeline/detail/sources; per-user writes/actions
remain login required.

Confirmed-edge experiment visibility:
  /api/feed/events now trusts `is_visible_in_feed`. The clustering pipeline is
  responsible for setting that bit only after LLM-confirmed event validation;
  UI visibility is no longer blocked by doc_count or unique_source_count.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import queue
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import db
import remote_db
import action_quota
from authz import can_access_all, current_user_id
from category_taxonomy import ACTIVE_CATEGORY_IDS, canonicalize_category, expand_query_categories
from deps import BASE
from routes.public_response_cache import (
    get_public_json_response,
    is_public_get_request,
    set_public_json_response,
)
from time_utils import parse_datetime, sort_key, to_utc_iso


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

logger = logging.getLogger(__name__)

router = APIRouter()

_SSE_HEADERS = {
    'Cache-Control': 'no-cache',
    'Connection': 'close',
    'X-Accel-Buffering': 'no',
}

_CATEGORY_PRIORITY = {cid: idx for idx, cid in enumerate(ACTIVE_CATEGORY_IDS)}
_EVENT_SOURCE_PREVIEW_LIMIT = 3
_DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES = -480
_MAX_TIMEZONE_OFFSET_MINUTES = 14 * 60


def _timezone_offset_minutes(value: int | None) -> int:
    try:
        offset = int(value if value is not None else _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES)
    except (TypeError, ValueError):
        offset = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES
    return max(-_MAX_TIMEZONE_OFFSET_MINUTES, min(_MAX_TIMEZONE_OFFSET_MINUTES, offset))


def _timeline_date_key(value: Any, timezone_offset_minutes: int) -> str:
    dt = parse_datetime(value)
    if dt is None:
        return 'unknown'
    # JS Date#getTimezoneOffset is UTC - local time. Convert UTC timestamps into
    # the same local date key the browser uses for the timeline heading.
    local_dt = dt - timedelta(minutes=timezone_offset_minutes)
    return local_dt.strftime('%Y-%m-%d')


def _timeline_date_counts(rows, timezone_offset_minutes: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = _timeline_date_key(row['first_doc_at'] or row['last_doc_at'] or row['last_updated_at'], timezone_offset_minutes)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _config_flag(key_path: list[str], default: Any) -> Any:
    """Read a dotted key from config/config.json, safe against missing keys."""
    try:
        with open(os.path.join(BASE, 'config', 'config.json')) as f:
            cfg = json.load(f)
    except Exception:
        return default
    cur: Any = cfg
    for k in key_path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _require_user(request: Request):
    """Return (user_id, None) on success, (None, JSONResponse 401) on failure."""
    uid = current_user_id(request)
    if not uid:
        return None, JSONResponse({'error': 'Not authenticated'}, status_code=401)
    return uid, None


def _is_anonymous_public_request(request: Request) -> bool:
    """Anonymous public reads must not expose private/manual cluster members."""
    return (
        current_user_id(request) is None
        and not getattr(request.state, 'legacy_authenticated', False)
    )


def _public_cluster_sql_filter(cluster_alias: str = 'c') -> str:
    """SQL predicate excluding clusters that include private/manual items."""
    return f"""
      AND NOT EXISTS (
        SELECT 1
        FROM cluster_items ci_priv
        JOIN items i_priv ON i_priv.id = ci_priv.item_id
        WHERE ci_priv.cluster_id = {cluster_alias}.id
          AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
      )
    """


def _github_cluster_display_filter(cluster_alias: str = 'c') -> tuple[str, tuple]:
    """Hide GitHub-only event clusters unless a member repo meets star floor."""
    min_stars = _config_flag(['display', 'github_min_stars'], 50)
    try:
        min_stars = max(0, int(min_stars))
    except (TypeError, ValueError):
        min_stars = 50
    if min_stars <= 0:
        return '', ()
    return f"""
      AND (
        NOT EXISTS (
          SELECT 1
          FROM cluster_items ci_disp
          WHERE ci_disp.cluster_id = {cluster_alias}.id
        )
        OR
        EXISTS (
          SELECT 1
          FROM cluster_items ci_disp
          JOIN items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform != 'github'
        )
        OR EXISTS (
          SELECT 1
          FROM cluster_items ci_disp
          JOIN items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform = 'github'
            AND json_valid(i_disp.metrics_json)
            AND CAST(COALESCE(json_extract(i_disp.metrics_json, '$.stars'), 0) AS INTEGER) >= ?
        )
      )
    """, (min_stars,)


def _github_cluster_display_min_stars() -> int:
    min_stars = _config_flag(['display', 'github_min_stars'], 50)
    try:
        return max(0, int(min_stars))
    except (TypeError, ValueError):
        return 50


def _remote_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({
        'error': 'Remote event read failed',
        'detail': str(exc),
        'data_backend': remote_db.event_read_backend(),
    }, status_code=503)


# BF-0708-3: last-good snapshot of the events payload, keyed like the public
# response cache but never expiring. When the read model degrades and the query
# times out, serving 12-day-old events beats a spinner that never stops.
_LAST_GOOD_EVENTS: dict[tuple, Any] = {}
_LAST_GOOD_EVENTS_LOCK = threading.Lock()
_LAST_GOOD_EVENTS_MAX = 32


def _remember_last_good_events(key: tuple, payload: Any) -> None:
    if key is None or not isinstance(payload, dict):
        return
    with _LAST_GOOD_EVENTS_LOCK:
        if len(_LAST_GOOD_EVENTS) >= _LAST_GOOD_EVENTS_MAX and key not in _LAST_GOOD_EVENTS:
            _LAST_GOOD_EVENTS.pop(next(iter(_LAST_GOOD_EVENTS)))
        _LAST_GOOD_EVENTS[key] = copy.deepcopy(payload)


def _degraded_events_response(key: tuple | None) -> JSONResponse:
    """Serve stale data if we have any, otherwise an explicit empty state.

    Always HTTP 200: the request succeeded, the data is just old or missing.
    A 5xx here would make the frontend render an error instead of the feed.
    """
    snapshot = None
    if key is not None:
        with _LAST_GOOD_EVENTS_LOCK:
            snapshot = copy.deepcopy(_LAST_GOOD_EVENTS.get(key))

    if snapshot is not None:
        snapshot['degraded'] = True
        snapshot['stale'] = True
        return JSONResponse(snapshot, status_code=200, headers={'Cache-Control': 'no-store'})

    return JSONResponse(
        {'events': [], 'total': 0, 'degraded': True, 'stale': False},
        status_code=200,
        headers={'Cache-Control': 'no-store'},
    )


def _reset_last_good_events_for_test() -> None:
    with _LAST_GOOD_EVENTS_LOCK:
        _LAST_GOOD_EVENTS.clear()


# BF-0708-3: request-level budget. statement_timeout only bounds a single SQL
# statement; the 129s feed request was dozens of individually-fast queries in
# the live-aggregation fallback. Only a wall-clock budget keeps us under
# Cloudflare's ~100s cutoff.
FEED_EVENTS_REQUEST_TIMEOUT_SEC_ENV = 'INFO2ACTION_FEED_EVENTS_REQUEST_TIMEOUT_SEC'
_FEED_EVENTS_REQUEST_TIMEOUT_DEFAULT_SEC = 30.0
_FEED_EVENTS_REQUEST_TIMEOUT_CEILING_SEC = 90.0


def _feed_events_request_timeout_sec() -> float:
    raw = (os.environ.get(FEED_EVENTS_REQUEST_TIMEOUT_SEC_ENV) or '').strip()
    if not raw:
        return _FEED_EVENTS_REQUEST_TIMEOUT_DEFAULT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _FEED_EVENTS_REQUEST_TIMEOUT_DEFAULT_SEC
    if value <= 0:
        return _FEED_EVENTS_REQUEST_TIMEOUT_DEFAULT_SEC
    return min(value, _FEED_EVENTS_REQUEST_TIMEOUT_CEILING_SEC)


def _public_cache_scope() -> tuple[str, str]:
    return (str(BASE), str(getattr(db, "DB_PATH", "")))


def _cluster_has_private_members(conn, cluster_id: int) -> bool:
    row = conn.execute(
        """SELECT 1
           FROM cluster_items ci
           JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id = ?
             AND (i.platform = 'manual' OR i.user_id IS NOT NULL)
           LIMIT 1""",
        (cluster_id,),
    ).fetchone()
    return row is not None


def _category_l1(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if '[' in raw:
        raw = raw.split('[', 1)[0]
    category = canonicalize_category(raw)
    if not category or category == 'other' or category not in ACTIVE_CATEGORY_IDS:
        return None
    return category


def _build_event_source_metadata(rows) -> dict[int, dict]:
    """Return per-cluster display metadata for the relaxed highlights timeline.

    The event feed only needs a light preview: first 3 unique sources for the
    metadata line plus a dominant L1 category. Full source browsing remains in
    the cluster detail panel.
    """
    grouped: dict[int, dict] = {}
    for row in rows:
        cluster_id = int(row['cluster_id'])
        data = grouped.setdefault(
            cluster_id,
            {'source_preview': [], '_seen_sources': set(), '_category_counts': {}},
        )

        category = _category_l1(row['ai_category'])
        if category:
            data['_category_counts'][category] = data['_category_counts'].get(category, 0) + 1

        platform = row['platform'] or ''
        identity = (
            row['source_identity']
            or row['url']
            or f"{platform}:{row['author_name'] or row['source'] or row['item_id']}"
        )
        if identity in data['_seen_sources']:
            continue
        data['_seen_sources'].add(identity)
        if len(data['source_preview']) >= _EVENT_SOURCE_PREVIEW_LIMIT:
            continue
        data['source_preview'].append({
            'platform': platform,
            'author': row['author_name'],
            'source': row['source'],
        })

    result: dict[int, dict] = {}
    for cluster_id, data in grouped.items():
        category_counts = data['_category_counts']
        category = None
        if category_counts:
            category = sorted(
                category_counts.items(),
                key=lambda item: (-item[1], _CATEGORY_PRIORITY.get(item[0], 999), item[0]),
            )[0][0]
        result[cluster_id] = {
            'category': category,
            'source_preview': data['source_preview'],
        }
    return result


def _load_event_source_metadata(conn, cluster_ids: list[int]) -> dict[int, dict]:
    if not cluster_ids:
        return {}
    placeholders = ','.join('?' * len(cluster_ids))
    rows = conn.execute(
        f"""SELECT ci.cluster_id, ci.source_identity, ci.rank_in_cluster,
                  ci.is_primary_source,
                  i.id AS item_id, i.platform, i.author_name, i.source,
                  i.url, i.ai_category, i.published_at, i.fetched_at
           FROM cluster_items ci
           JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id IN ({placeholders})
           ORDER BY ci.cluster_id ASC,
                    COALESCE(ci.is_primary_source, 0) DESC,
                    COALESCE(ci.rank_in_cluster, 999999) ASC""",
        tuple(cluster_ids),
    ).fetchall()
    rows = sorted(
        rows,
        key=lambda r: (
            int(r['cluster_id']),
            -int(r['is_primary_source'] or 0),
            int(r['rank_in_cluster'] if r['rank_in_cluster'] is not None else 999999),
            -sort_key(r['published_at'] or r['fetched_at']),
        ),
    )
    return _build_event_source_metadata(rows)


def _media_urls_from_item(cover_url: Any, media_json: Any) -> list[str]:
    urls: list[str] = []

    def add_url(value: Any):
        if not value:
            return
        url = str(value).strip()
        if url and url not in urls:
            urls.append(url)

    add_url(cover_url)
    media = media_json
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except Exception:
            media = []
    if not isinstance(media, list):
        return urls

    for entry in media:
        if isinstance(entry, str):
            add_url(entry)
            continue
        if not isinstance(entry, dict):
            continue
        media_type = str(entry.get('type') or '').lower()
        if media_type in ('video', 'animated_gif'):
            continue
        add_url(entry.get('url') or entry.get('preview_image_url') or entry.get('src'))
    return urls


def _row_to_event(
    row,
    *,
    user_last_seen: dict[int, int | None],
    source_metadata: dict[int, dict] | None = None,
) -> dict:
    platforms = []
    try:
        platforms = json.loads(row['platforms_json'] or '[]')
    except Exception:
        pass
    lv = row['live_version'] or 0
    # v15.1 R7.2: last_seen_version is None when the user has no
    # cluster_status row yet (first-time viewer). The frontend SHALL NOT
    # display the update badge in that case → has_update=False.
    seen = user_last_seen.get(row['id'])
    has_update = bool(seen is not None and lv > seen)
    # v15.1: unique_source_count is the new visibility threshold input.
    # Pre-cutover clusters default to 0 (invisible until rebuilt).
    try:
        usc = int(row['unique_source_count'] or 0)
    except (KeyError, IndexError, TypeError):
        usc = 0
    metadata = (source_metadata or {}).get(int(row['id']), {})
    return {
        'id': row['id'],
        'ai_title': row['ai_title'],
        'ai_summary': row['ai_summary'],
        'why_read': row['why_read'],
        'doc_count': row['doc_count'],
        'unique_source_count': usc,
        'category': metadata.get('category'),
        'source_preview': metadata.get('source_preview', []),
        'first_doc_at': to_utc_iso(row['first_doc_at']) or row['first_doc_at'],
        'last_doc_at': to_utc_iso(row['last_doc_at']) if row['last_doc_at'] else None,
        'platforms': platforms,
        'cover_url': row['cover_url'],
        'has_update': has_update,
        'live_version': lv,
        'last_seen_version': seen,
    }


# ── GET /api/feed/events ────────────────────────────────────────────


def _parse_categories_filter(raw) -> list[str]:
    """v17.0: 解析 categories query param (comma-separated L1 ids)。

    - 去重 + canonicalize（兜底 v3.1 → v4.0 别名）
    - 排除 'other'（v17.0 默认规则 not in 'other'）
    - 返回扩展后的 DB 候选列表（含 legacy 别名，匹配 items.ai_category 旧数据）

    注：参数类型故意宽松（不严格 str | None）— FastAPI router 函数在被
    单元测试直接调用（不经 ASGI）时,默认参数会是 Query 对象而非 None,
    用 isinstance str 守卫规避 AttributeError。
    """
    if not isinstance(raw, str) or not raw:
        return []
    expanded: list[str] = []
    seen: set[str] = set()
    for part in raw.split(','):
        cid = canonicalize_category(part.strip())
        # 严格白名单守卫：仅接受 ACTIVE_CATEGORY_IDS 中的 L1 id (排除 other)
        # 防御性 — SQL 参数化已保证无注入,但白名单可拒绝无意义输入早返回空
        if not cid or cid == 'other' or cid not in ACTIVE_CATEGORY_IDS or cid in seen:
            continue
        seen.add(cid)
        for alias in expand_query_categories(cid):
            if alias not in expanded:
                expanded.append(alias)
    return expanded


def _optional_read_model_cursor(value):
    raw = value.strip() if isinstance(value, str) and value.strip() else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    version_id = data.get('version_id')
    scope_key = data.get('scope_key')
    if not isinstance(version_id, str) or not version_id.strip():
        return None
    if not isinstance(scope_key, str) or not scope_key.strip():
        return None
    try:
        rank_after = int(data.get('rank_after'))
    except (TypeError, ValueError):
        return None
    if rank_after < 0:
        return None
    return {
        'version_id': version_id.strip(),
        'scope_key': scope_key.strip(),
        'rank_after': rank_after,
    }


def _categories_sql_clause(categories: list[str]) -> tuple[str, list[str]]:
    """构造 EXISTS 子查询：cluster 至少含一个成员 item 命中 categories OR 之一。

    items.ai_category 可能是 'coding' 或 'coding[/coding_tool]'，
    用 SUBSTR + INSTR 提取 L1 段后 IN 比较。
    """
    if not categories:
        return '', []
    placeholders = ','.join('?' * len(categories))
    clause = f"""
        AND EXISTS (
            SELECT 1
            FROM cluster_items ci
            JOIN items i ON i.id = ci.item_id
            WHERE ci.cluster_id = c.id
              AND COALESCE(
                CASE
                  WHEN INSTR(i.ai_category, '[') > 0
                  THEN SUBSTR(i.ai_category, 1, INSTR(i.ai_category, '[') - 1)
                  ELSE i.ai_category
                END,
                ''
              ) IN ({placeholders})
        )
    """
    return clause, list(categories)


@router.get('/api/feed/events')
async def feed_events(
    request: Request,
    response: Response,
    page: int = Query(1, ge=1, le=500),
    limit: int = Query(20, ge=1, le=100),
    since_version_snapshot: int | None = Query(None, description='client snapshot anchor'),
    fetched_since: str | None = Query(None, description='only clusters touched by docs fetched since this timestamp'),
    cursor: str | None = Query(None, description='opaque read-model cursor for versioned event pagination'),
    categories: str | None = Query(None, description='v17.0: L1 ids comma-separated, OR 多选 (e.g. models,coding)'),
    timezone_offset_minutes: int = Query(
        _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
        ge=-_MAX_TIMEZONE_OFFSET_MINUTES,
        le=_MAX_TIMEZONE_OFFSET_MINUTES,
        description='browser Date#getTimezoneOffset minutes for timeline day counts',
    ),
):
    uid = current_user_id(request)
    public_only = _is_anonymous_public_request(request)
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    # v17.0: categories L1 筛选（精选 tab chip OR）— remote 和 local 路径共用
    categories_list = _parse_categories_filter(categories)
    read_model_cursor = _optional_read_model_cursor(cursor)
    min_github_stars = _github_cluster_display_min_stars()
    events_enabled = bool(_config_flag(['global', 'event_aggregation_ready'], False))
    events_remote = remote_db.events_read_from_remote()
    cache_key = None
    if (
        events_remote
        and is_public_get_request(request, public_only=public_only)
        and page == 1
        and limit == 20
        and since_version_snapshot is None
        and fetched_since is None
        and read_model_cursor is None
        and not categories_list
    ):
        cache_key = ("feed_events", _public_cache_scope(), tz_offset, min_github_stars, events_enabled)
        cached = get_public_json_response(cache_key)
        if cached is not None:
            return cached
    else:
        response.headers['Cache-Control'] = 'no-store'
    if events_remote:
        try:
            # BF-0708-3: wall-clock budget for the whole read. The slow path is
            # many individually-fast queries, so per-statement timeouts cannot
            # bound it. The worker thread keeps running after we give up — we
            # cannot cancel it — but the client gets an answer well before
            # Cloudflare's ~100s cutoff, which is what prevents the 524.
            result = await asyncio.wait_for(
                run_in_threadpool(
                    remote_db.fetch_events,
                    page=page,
                    limit=limit,
                    cursor=read_model_cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=uid,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=events_enabled,
                    categories=categories_list,
                    timezone_offset_minutes=tz_offset,
                ),
                timeout=_feed_events_request_timeout_sec(),
            )
            _remember_last_good_events(cache_key, result)
            if cache_key is not None:
                return set_public_json_response(cache_key, result)
            return result
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                'feed_events: request exceeded %.1fs budget, serving degraded response',
                _feed_events_request_timeout_sec(),
            )
            return _degraded_events_response(cache_key)
        except remote_db.RemoteDBTimeoutError as exc:
            # A single statement blew its statement_timeout. The DB is reachable,
            # this read was just too slow — degrade rather than surface an error.
            logger.warning('feed_events: read timed out, serving degraded response: %s', exc)
            return _degraded_events_response(cache_key)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    public_filter = _public_cluster_sql_filter('c') if public_only else ''
    github_display_filter, github_display_params = _github_cluster_display_filter('c')
    categories_clause, categories_params = _categories_sql_clause(categories_list)
    conn = db.get_conn()
    try:
        offset = (page - 1) * limit
        # Confirmed-edge experiment: visibility is decided by the pipeline via
        # is_visible_in_feed. Do not re-apply doc/source-count gates here.
        rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.doc_count,
                       c.unique_source_count, c.first_doc_at, c.last_doc_at,
                       c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), (
                         SELECT i.cover_url
                         FROM cluster_items ci
                         JOIN items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND NULLIF(i.cover_url, '') IS NOT NULL
                           AND i.platform != 'manual'
                           AND i.user_id IS NULL
                         ORDER BY COALESCE(ci.is_primary_source, 0) DESC,
                                  COALESCE(ci.rank_in_cluster, 999999) ASC
                         LIMIT 1
                       )) AS cover_url,
                       c.live_version,
                       c.last_updated_at
               FROM clusters c
               WHERE c.is_visible_in_feed = 1
                 AND c.published_at IS NOT NULL
                 AND c.archived = 0
                 AND c.merged_into IS NULL
                 AND c.last_updated_at > datetime('now', '-30 days')
                 AND (
                   ? IS NULL
                   OR EXISTS (
                     SELECT 1
                     FROM cluster_items ci
                     JOIN items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND i.fetched_at >= ?
                   )
                 )
                 {public_filter}
                 {github_display_filter}
                 {categories_clause}
               """,
            (fetched_since, fetched_since, *github_display_params, *categories_params),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda r: (
                sort_key(r['first_doc_at'] or r['last_updated_at']),
                sort_key(r['last_updated_at']),
                int(r['id']),
            ),
            reverse=True,
        )
        total_avail = len(rows)
        date_counts = _timeline_date_counts(rows, tz_offset)
        page_rows = rows[offset:offset + limit + 1]
        has_more = len(page_rows) > limit
        rows = page_rows[:limit]

        ids = [r['id'] for r in rows]
        # v15.1 R7.2: seen_map[cid] = None means "never seen" (no row in
        # cluster_status). This is distinct from last_seen_version=0, which
        # means seen at version 0. _row_to_event uses None to suppress the
        # update badge for first-time viewers.
        seen_map: dict[int, int | None] = {}
        if uid and ids:
            placeholders = ','.join('?' * len(ids))
            srows = conn.execute(
                f"""SELECT cluster_id, last_seen_version FROM cluster_status
                    WHERE user_id = ? AND cluster_id IN ({placeholders})""",
                (uid, *ids),
            ).fetchall()
            for sr in srows:
                seen_map[sr['cluster_id']] = sr['last_seen_version'] or 0

        source_metadata = _load_event_source_metadata(conn, [int(r['id']) for r in rows])
        events = [
            _row_to_event(r, user_last_seen=seen_map, source_metadata=source_metadata)
            for r in rows
        ]
        # new_since_last_fetch = clusters newer than client's snapshot anchor
        new_since = 0
        if since_version_snapshot is not None:
            new_since_row = conn.execute(
                f"""SELECT COUNT(*) AS n FROM clusters c
                   WHERE c.is_visible_in_feed = 1
                     AND c.published_at IS NOT NULL
                     AND c.archived = 0 AND c.merged_into IS NULL
                     AND c.id > ?
                     AND (
                       ? IS NULL
                       OR EXISTS (
                         SELECT 1
                         FROM cluster_items ci
                         JOIN items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND i.fetched_at >= ?
                       )
                     )
                     {public_filter}
                     {github_display_filter}
                     {categories_clause}""",
                (since_version_snapshot, fetched_since, fetched_since, *github_display_params, *categories_params),
            ).fetchone()
            new_since = new_since_row['n'] if new_since_row else 0

        result = {
            'enabled': events_enabled,
            'events': events,
            'next_cursor': (page + 1) if has_more else None,
            'new_since_last_fetch': new_since,
            'total_available_within_30d': total_avail,
            'date_counts': date_counts,
        }
        if cache_key is not None:
            return set_public_json_response(cache_key, result)
        return result
    finally:
        conn.close()


# ── GET /api/clusters/{id} ──────────────────────────────────────────

@router.get('/api/clusters/{cluster_id}')
async def cluster_detail(request: Request, cluster_id: int):
    uid = current_user_id(request)
    public_only = _is_anonymous_public_request(request)
    if remote_db.events_read_from_remote():
        try:
            body = await run_in_threadpool(
                remote_db.cluster_detail,
                cluster_id=cluster_id,
                public_only=public_only,
                user_id=uid,
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.ai_key_points, c.doc_count,
                      unique_source_count,
                      c.platforms_json,
                      COALESCE(NULLIF(c.cover_url, ''), (
                        SELECT i.cover_url
                        FROM cluster_items ci
                        JOIN items i ON i.id = ci.item_id
                        WHERE ci.cluster_id = c.id
                          AND NULLIF(i.cover_url, '') IS NOT NULL
                          AND i.platform != 'manual'
                          AND i.user_id IS NULL
                        ORDER BY COALESCE(ci.is_primary_source, 0) DESC,
                                 COALESCE(ci.rank_in_cluster, 999999) ASC
                        LIMIT 1
                      )) AS cover_url,
                      c.first_doc_at, c.last_doc_at,
                      c.live_version, c.merged_into, c.is_visible_in_feed
               FROM clusters c WHERE c.id = ?""",
            (cluster_id,),
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        if public_only and _cluster_has_private_members(conn, cluster_id):
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        metadata = _load_event_source_metadata(conn, [cluster_id]).get(cluster_id, {})

        platforms = []
        try:
            platforms = json.loads(row['platforms_json'] or '[]')
        except Exception:
            pass
        kps = []
        try:
            kps = json.loads(row['ai_key_points'] or '[]')
        except Exception:
            pass

        seen_row = None
        if uid:
            seen_row = conn.execute(
                "SELECT clicked_at, starred_at, last_seen_version, feedback_kind, feedback_note FROM cluster_status "
                "WHERE user_id=? AND cluster_id=?",
                (uid, cluster_id),
            ).fetchone()
        user_last_seen = seen_row['last_seen_version'] if seen_row else None
        viewer_status = {
            'clicked_at': to_utc_iso(seen_row['clicked_at']) if seen_row and seen_row['clicked_at'] else None,
            'starred_at': to_utc_iso(seen_row['starred_at']) if seen_row and seen_row['starred_at'] else None,
            'last_seen_version': user_last_seen,
            'feedback_kind': seen_row['feedback_kind'] if seen_row else None,
            'feedback_note': seen_row['feedback_note'] if seen_row else None,
        }

        body = {
            'id': row['id'],
            'ai_title': row['ai_title'],
            'ai_summary': row['ai_summary'],
            'why_read': row['why_read'],
            'ai_key_points': kps,
            'doc_count': row['doc_count'],
            # BF-0428-1: expose unique_source_count to frontend (header "来源 N"
            # in ClusterDetailPanel / ClusterRightPanel reads this field).
            'unique_source_count': int(row['unique_source_count'] or 0),
            'platforms': platforms,
            'category': metadata.get('category'),
            'first_doc_at': to_utc_iso(row['first_doc_at']) or row['first_doc_at'],
            'last_doc_at': to_utc_iso(row['last_doc_at']) if row['last_doc_at'] else None,
            'cover_url': row['cover_url'],
            'media_urls': _media_urls_from_item(row['cover_url'], None),
            'live_version': row['live_version'],
            'user_last_seen_version': user_last_seen,
            'viewer_status': viewer_status,
            'is_visible_in_feed': bool(row['is_visible_in_feed']),
        }
        if row['merged_into']:
            body['redirect_to'] = row['merged_into']
        return body
    finally:
        conn.close()


# ── GET /api/clusters/{id}/sources ──────────────────────────────────

@router.get('/api/clusters/{cluster_id}/sources')
async def cluster_sources(
    request: Request,
    cluster_id: int,
    page: int = Query(1, ge=1, le=500),
    limit: int = Query(20, ge=1, le=100),
):
    public_only = _is_anonymous_public_request(request)
    if remote_db.events_read_from_remote():
        try:
            body = await run_in_threadpool(
                remote_db.cluster_sources,
                cluster_id=cluster_id,
                page=page,
                limit=limit,
                public_only=public_only,
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    conn = db.get_conn()
    try:
        if not conn.execute(
            "SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone():
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        if public_only and _cluster_has_private_members(conn, cluster_id):
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        offset = (page - 1) * limit
        rows = conn.execute(
            """SELECT i.id AS item_id, i.title, i.author_name, i.platform,
                      i.published_at, i.fetched_at, i.url, ci.is_primary_source,
                      i.cover_url, i.media_json,
                      SUBSTR(COALESCE(i.ai_summary, i.content, ''), 1, 200) AS snippet
               FROM cluster_items ci JOIN items i ON i.id = ci.item_id
               WHERE ci.cluster_id = ?
               """,
            (cluster_id,),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda r: (
                sort_key(r['published_at'] or r['fetched_at']),
                int(r['is_primary_source'] or 0),
            ),
            reverse=True,
        )
        page_rows = rows[offset:offset + limit + 1]
        has_more = len(page_rows) > limit
        rows = page_rows[:limit]
        # authority_badge is a simple rule for v1: platform-derived
        sources = []
        for r in rows:
            badge = None
            plat = r['platform'] or ''
            if plat in ('openai', 'anthropic', 'official'):
                badge = 'official'
            elif plat in ('hackernews',):
                badge = 'community'
            sources.append({
                'item_id': r['item_id'],
                'title': r['title'],
                'author': r['author_name'],
                'platform': plat,
                'published_at': to_utc_iso(r['published_at'] or r['fetched_at']),
                'url': r['url'],
                'cover_url': r['cover_url'],
                'media_urls': _media_urls_from_item(r['cover_url'], r['media_json']),
                'is_primary_source': int(r['is_primary_source'] or 0),
                'authority_badge': badge,
                'snippet': (r['snippet'] or '').strip(),
            })
        return {
            'sources': sources,
            'next_cursor': (page + 1) if has_more else None,
        }
    finally:
        conn.close()


# ── GET /api/clusters/{id}/bundle ──────────────────────────────────

@router.get('/api/clusters/{cluster_id}/bundle')
async def cluster_bundle(
    request: Request,
    cluster_id: int,
    page: int = Query(1, ge=1, le=500),
    limit: int = Query(20, ge=1, le=100),
):
    uid = current_user_id(request)
    public_only = _is_anonymous_public_request(request)
    if remote_db.events_read_from_remote():
        try:
            body = await run_in_threadpool(
                remote_db.cluster_bundle,
                cluster_id=cluster_id,
                page=page,
                limit=limit,
                public_only=public_only,
                user_id=uid,
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    detail = await cluster_detail(request, cluster_id)
    if isinstance(detail, JSONResponse):
        return detail
    sources = await cluster_sources(request, cluster_id, page=page, limit=limit)
    if isinstance(sources, JSONResponse):
        return sources
    return {
        'cluster': detail,
        'sources': sources.get('sources', []),
        'sources_next_cursor': sources.get('next_cursor'),
    }


# ── POST /api/clusters/{id}/click ──────────────────────────────────

@router.post('/api/clusters/{cluster_id}/click')
async def cluster_click(request: Request, cluster_id: int):
    uid = current_user_id(request)
    if not uid:
        return {'ok': True, 'last_seen_version': 0}
    if remote_db.events_read_from_remote() or remote_db.status_write_to_remote():
        try:
            # BE-1: 每次打开事件卡片必发,远程往返离开事件循环
            body = await run_in_threadpool(
                remote_db.mark_cluster_clicked, cluster_id=cluster_id, user_id=uid)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT live_version FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        lv = row['live_version'] or 0
        conn.execute(
            """INSERT INTO cluster_status (user_id, cluster_id, clicked_at,
                                           last_seen_version)
               VALUES (?, ?, datetime('now'), ?)
               ON CONFLICT(user_id, cluster_id) DO UPDATE SET
                 clicked_at = excluded.clicked_at,
                 last_seen_version = excluded.last_seen_version""",
            (uid, cluster_id, lv),
        )
        conn.commit()
        return {'ok': True, 'last_seen_version': lv}
    finally:
        conn.close()


CLUSTER_FEEDBACK_KINDS = {'positive', 'irrelevant', 'low_quality', 'should_feature'}


@router.post('/api/clusters/{cluster_id}/feedback')
async def cluster_feedback(request: Request, cluster_id: int):
    """v25.0 F-D — per-user cluster 质量反馈：幂等（同 kind 再提交=撤销），只落库不改排序。"""
    uid, err = _require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = body if isinstance(body, dict) else {}
    kind = str(payload.get('kind') or '').strip()
    if kind not in CLUSTER_FEEDBACK_KINDS:
        return JSONResponse({'error': 'invalid feedback kind'}, status_code=400)
    raw_note = payload.get('note')
    if raw_note is not None and not isinstance(raw_note, str):
        return JSONResponse({'error': 'feedback note must be a string'}, status_code=400)
    note = raw_note.strip() if raw_note is not None else None
    note = note or None
    if note is not None and len(note) > 500:
        return JSONResponse({'error': 'feedback note exceeds 500 characters'}, status_code=400)

    if remote_db.events_read_from_remote() or remote_db.status_write_to_remote():
        try:
            result = await run_in_threadpool(
                remote_db.set_cluster_feedback,
                cluster_id=cluster_id,
                user_id=uid,
                kind=kind,
                note=note,
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if result is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return result

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)

        status = conn.execute(
            "SELECT feedback_kind FROM cluster_status WHERE user_id=? AND cluster_id=?",
            (uid, cluster_id),
        ).fetchone()
        if status and status['feedback_kind'] == kind:
            conn.execute(
                "UPDATE cluster_status "
                "SET feedback_kind = NULL, feedback_at = NULL, feedback_note = NULL "
                "WHERE user_id=? AND cluster_id=?",
                (uid, cluster_id),
            )
            conn.commit()
            return {'ok': True, 'feedback_kind': None, 'feedback_note': None}

        conn.execute(
            """INSERT INTO cluster_status (
                 user_id, cluster_id, feedback_kind, feedback_at, feedback_note
               )
               VALUES (?, ?, ?, datetime('now'), ?)
               ON CONFLICT(user_id, cluster_id) DO UPDATE SET
                 feedback_kind = excluded.feedback_kind,
                 feedback_at = excluded.feedback_at,
                 feedback_note = excluded.feedback_note""",
            (uid, cluster_id, kind, note),
        )
        conn.commit()
        return {'ok': True, 'feedback_kind': kind, 'feedback_note': note}
    finally:
        conn.close()


@router.post('/api/clusters/{cluster_id}/star')
async def cluster_star(request: Request, cluster_id: int):
    """v18.1 — toggle per-user favorite state for a cluster."""
    uid, err = _require_user(request)
    if err:
        return err
    if remote_db.events_read_from_remote() or remote_db.status_write_to_remote():
        try:
            # BE-1: 收藏切换,远程往返离开事件循环
            body = await run_in_threadpool(
                remote_db.set_cluster_star, cluster_id=cluster_id, user_id=uid)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)

        status = conn.execute(
            "SELECT starred_at FROM cluster_status WHERE user_id=? AND cluster_id=?",
            (uid, cluster_id),
        ).fetchone()
        if status and status['starred_at']:
            conn.execute(
                "UPDATE cluster_status SET starred_at = NULL WHERE user_id=? AND cluster_id=?",
                (uid, cluster_id),
            )
            conn.commit()
            return {'ok': True, 'starred_at': None}

        conn.execute(
            """INSERT INTO cluster_status (user_id, cluster_id, starred_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id, cluster_id) DO UPDATE SET
                 starred_at = excluded.starred_at""",
            (uid, cluster_id),
        )
        conn.commit()
        starred = conn.execute(
            "SELECT starred_at FROM cluster_status WHERE user_id=? AND cluster_id=?",
            (uid, cluster_id),
        ).fetchone()
        return {
            'ok': True,
            'starred_at': to_utc_iso(starred['starred_at']) if starred and starred['starred_at'] else None,
        }
    finally:
        conn.close()


# ── POST /api/clusters/{id}/seen ───────────────────────────────────

@router.post('/api/clusters/{cluster_id}/seen')
async def cluster_seen(request: Request, cluster_id: int):
    """v15.1 R7.1 — mark cluster as seen at its current live_version.

    Idempotent UPSERT against cluster_status. Used by the frontend when the
    user opens the cluster modal so the update badge clears next mount.

    Authenticated cluster missing → 404. Anonymous users receive a no-op
    success before lookup so public pages do not create avoidable 401 noise or
    expose private cluster-id existence through this write-style endpoint.
    """
    uid = current_user_id(request)
    if not uid:
        return {'cluster_id': cluster_id, 'last_seen_version': 0}
    if remote_db.events_read_from_remote() or remote_db.status_write_to_remote():
        try:
            # BE-1: 每次打开事件弹窗必发,远程往返离开事件循环
            body = await run_in_threadpool(
                remote_db.mark_cluster_seen, cluster_id=cluster_id, user_id=uid)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if body is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        return body

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT live_version FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        lv = row['live_version'] or 0
        # UPSERT — preserves clicked_at if already present (we only touch
        # last_seen_version here; click endpoint touches both).
        conn.execute(
            """INSERT INTO cluster_status (user_id, cluster_id, last_seen_version)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, cluster_id) DO UPDATE SET
                 last_seen_version = excluded.last_seen_version""",
            (uid, cluster_id, lv),
        )
        conn.commit()
        _log_event('cluster_seen_marked',
                   user_id=uid, cluster_id=cluster_id, live_version=lv)
        return {'cluster_id': cluster_id, 'last_seen_version': lv}
    finally:
        conn.close()


# ── GET /api/clusters/{id}/actions ──────────────────────────────────

@router.get('/api/clusters/{cluster_id}/actions')
async def cluster_actions_list(request: Request, cluster_id: int):
    uid = current_user_id(request)
    if not uid:
        return {'actions': []}
    # BF-0706-3: 生产走 remote(Supabase),行动在远端库;只读本地会返回空 → 事件弹窗
    # 已生成行动点不显示。补 remote 分支(镜像本地查询)。
    if remote_db.app_state_to_remote():
        try:
            actions = await run_in_threadpool(
                remote_db.get_cluster_actions_remote, cluster_id, user_id=uid)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        return {'actions': actions}
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT id, title, action_type, prompt, priority, status,
                      cluster_version, is_stale, created_at
               FROM actions
               WHERE source_type = 'cluster' AND source_id = ? AND user_id = ?
               ORDER BY created_at DESC""",
            (str(cluster_id), uid),
        ).fetchall()
        return {'actions': [dict(r) for r in rows]}
    finally:
        conn.close()


# ── POST /api/clusters/{id}/actions (SSE) ──────────────────────────


def _build_cluster_action_prompt(cluster_title: str, cluster_summary: str,
                                  cluster_key_points: list, user_hint: str,
                                  action_type_hint: str,
                                  member_rows: list) -> str:
    """Compose the user content payload for the cluster action LLM call.

    The prompt asks the model to think, then output a JSON action object
    (title / action_type / prompt / priority / reason). 中文 prompt 与
    v10.1 generate_from_item 风格一致。
    """
    parts: list[str] = []
    parts.append('## 事件聚合上下文')
    parts.append(f'**事件标题**: {cluster_title}')
    if cluster_summary:
        parts.append(f'**AI 综合摘要**:\n{cluster_summary}')
    if cluster_key_points:
        kp_lines = '\n'.join(f'- {kp}' for kp in cluster_key_points if kp)
        if kp_lines:
            parts.append(f'**关键要点**:\n{kp_lines}')
    if member_rows:
        src_lines = []
        for idx, r in enumerate(member_rows, start=1):
            title = (r['title'] or '').strip()[:120]
            snippet = (r['ai_summary'] or '').strip().replace('\n', ' ')[:200]
            plat = r['platform'] or '?'
            url = (r['url'] or '').strip() if 'url' in r.keys() else ''
            line = f'{idx}. [{plat}] {title}'
            if url:
                line += f'\n   链接: {url}'
            line += f'\n   摘要: {snippet}'
            src_lines.append(line)
        parts.append('**主要来源**:\n' + '\n'.join(src_lines))
    if user_hint:
        parts.append(f'**用户预期方向**: {user_hint}')
    if action_type_hint:
        parts.append(
            f'**用户指定行动类型**: {action_type_hint} '
            '（输出 JSON 的 action_type 必须与此一致）'
        )

    parts.append(
        '## 任务\n'
        '基于上述事件聚合的多源信息，为用户生成 **一个** 可执行的行动点。'
        '深入思考事件背景、各来源差异、可借鉴/可警醒的点，再输出。'
        '行动点必须能交给执行 Agent 立即开工，并且必须有可验收产出物。'
    )
    parts.append(
        '## 输出格式\n'
        '严格输出一个 JSON 对象（不要 markdown 代码块包裹），前后不得有额外文字。\n'
        '**JSON 合法性铁律**：字符串值内部禁止裸 ASCII 双引号 `"`——需要引用词/短语时用中文引号「」，'
        '确需 ASCII 双引号时转义为 \\"；换行/反斜杠也必须转义,保证 json.loads 一次解析成功。字段：\n'
        '- `title` (string): 行动点简短标题，<= 36 字；必须包含产出物或决策目标，不要只写“调研/深入了解/关注/分析某事件”\n'
        '- `action_type` (string): "investigate"(调研) | "implement"(实践) | "content"(创作) | "track"(跟踪,值得关注但暂不落地) 四选一,由你根据内容判定\n'
        '- `steps` (string[]): 给人看的行动点，3-6 条，每条一句话，用户 10 秒判断是否要做\n'
        '- `prompt` (string): 交给本地 Agent 的**自包含**可执行指令，粘到全新会话即可跑，'
        '不得依赖平台上下文。必须含：① 事件来源标题 + 上面列出的完整链接（指示先访问获取全文）；'
        '② 一两句事件背景摘要；③ **联网深度调研**：明确指示 Agent 不要只看给定链接，'
        '要用 web 搜索补充 2-4 个独立权威来源交叉验证事实，并主动找替代方案、反面观点/风险、最新进展；'
        '④ **给出多种可能路径/选项**（各自取舍与适用场景），而非单一结论；⑤ 任务目标、产出物形态与验收标准\n'
        '- `priority` (string): "high" | "medium" | "low"\n'
        '- `reason` (string): 1-2 句说明为何值得做，必须点明多源事件带来的决策价值\n\n'
        '禁止输出没有明确产出物的泛行动。例如不要输出“Claude Narf 行为调研”，'
        '应输出“产出 Claude Narf 异常根因对照表与产品防线清单”。'
        '\n\n'
        '只输出 JSON，不要其他解释文字。'
    )
    return '\n\n'.join(parts)


def _build_cluster_action_system_prompt() -> str:
    return (
        '你是一个产品行动点生成助手。基于事件聚合（多个新闻/信息源汇成的同一主题事件），'
        '为用户生成一个可执行的行动点。'
        '\n\n'
        '## 原则\n'
        '- 行动点必须可执行、有明确产出物（不是泛泛而谈）\n'
        '- 优先选择「调研型」(investigate) — 多源事件特别适合做趋势/对比/影响面调研\n'
        '- 如果事件涉及具体工具/方案落地，可选「实施型」(implement)\n'
        '- 如果事件适合写一篇深度文章/笔记，可选「内容型」(content)\n'
        '- 不要重复事件本身的信息，要给出「下一步要做什么」\n'
        '- 不要产出“了解一下/调研一下/关注一下”这类不可验收行动；每个行动都要有明确交付物\n'
        '\n【语言要求】你的所有内部思考过程(thinking)以及最终输出的所有字段，必须使用简体中文。\n'
    )


def _parse_cluster_action_response(result_text: str | None, ga_module=None) -> dict | None:
    """Parse a single action object from the cluster LLM response.

    v10.1 action generation usually returns an action array, while this cluster
    prompt asks for one JSON object. Accept both shapes so tests protect the
    contract instead of relying on one model formatting habit.
    """
    if not result_text:
        return None

    text = result_text.strip()
    if text.startswith('```'):
        import re
        text = re.sub(r'^```\w*\n?', '', text)
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = None

    if isinstance(obj, dict):
        actions = obj.get('actions')
        if isinstance(actions, list) and actions and isinstance(actions[0], dict):
            return actions[0]
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]

    if ga_module is not None:
        try:
            parsed = ga_module.parse_actions_response(text)
            if parsed and isinstance(parsed[0], dict):
                return parsed[0]
        except Exception:
            pass

    try:
        import re
        match = re.search(r'\{[\s\S]*?\}', text)
        if match:
            obj = json.loads(match.group())
            if isinstance(obj, dict):
                return obj
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _refresh_cluster_action_read_model(action_id: str, uid: str) -> None:
    """Rebuild the action detail read model after a cluster action is persisted,
    and invalidate the actions list payload cache so the行动 Tab shows it promptly.
    Best-effort: never let a read-model hiccup fail the SSE stream."""
    try:
        if remote_db.app_state_to_remote():
            remote_db.build_action_detail_read_model_remote(
                action_id, request_user_id=uid, can_view_all=False,
                owner_user_id=uid, persist=True,
            )
        else:
            conn = db.get_conn()
            try:
                db.build_action_detail_read_model(
                    conn, action_id, request_user_id=uid, can_view_all=False,
                    owner_user_id=uid, persist=True,
                )
            finally:
                conn.close()
    except Exception as exc:
        print(f"[warn] cluster action read model refresh failed for {action_id}: {exc}")
    try:
        import routes.actions as _actions_route
        _actions_route._clear_actions_payload_cache()
    except Exception:
        pass


def _emit_cluster_action_sse(uid: str, cluster_id: int, live_version: int,
                              cluster_title: str, cluster_summary: str,
                              cluster_key_points: list, user_hint: str,
                              action_type_hint: str,
                              member_rows: list, source_item_ids: list):
    """Generator yielding SSE bytes — runs the LLM streaming call in a
    background thread and forwards thinking deltas, then emits result+done.

    Event structure mirrors v10.1 generate-from-item:
      thinking → stage(active) → thinking-ai* → stage(done) → result → done
    """

    def sse(event: str, data: dict) -> str:
        return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

    # Stage 0 — content analysis (instant, deterministic)
    yield sse('thinking', {'stage': 0, 'text': f'事件: {cluster_title}'})
    yield sse('thinking', {'stage': 0, 'text': f'{len(member_rows)} 条来源已读入'})
    if cluster_key_points:
        for kp in cluster_key_points[:3]:
            if kp:
                yield sse('thinking', {'stage': 0, 'text': str(kp)[:80]})
    yield sse('stage', {'index': 0, 'status': 'done'})

    # Stage 1 — assemble prompt
    yield sse('stage', {'index': 1, 'status': 'active'})
    try:
        # Lazy import to keep route import-time light + avoid heavy deps in tests
        import sys
        sys.path.insert(0, os.path.join(BASE, 'src'))
        import generate_actions as ga  # noqa: WPS433
    except Exception as e:  # pragma: no cover — bootstrap error
        yield sse('error', {'error': f'failed to load generate_actions: {e}'})
        return

    cfg = ga.load_config()
    ai_cfg = cfg.get('ai_summary', {})
    api_key, api_base, model = ga.resolve_minimax_chat_config(ai_cfg)

    system_prompt = _build_cluster_action_system_prompt()
    user_content = _build_cluster_action_prompt(
        cluster_title, cluster_summary, cluster_key_points, user_hint,
        action_type_hint, member_rows,
    )
    # M3 默认英文思考,首尾夹中文强指令强制中文 thinking/输出(实测有效)
    user_content = ga.CHINESE_ONLY_PREFIX + user_content + ga.CHINESE_ONLY_SUFFIX
    yield sse('thinking', {'stage': 1, 'text': f'综合事件 + {len(member_rows)} 条来源'})
    yield sse('thinking', {'stage': 1, 'text': f'已组装 prompt（{len(user_content)} 字符）'})
    yield sse('stage', {'index': 1, 'status': 'done'})

    # Stage 2 — call LLM streaming. We can't yield from inside on_thinking
    # (different thread), so we push to a queue and pull from generator.
    yield sse('stage', {'index': 2, 'status': 'active'})
    yield sse('thinking', {'stage': 2, 'text': f'调用 {model} 综合分析多源信息'})

    if not api_key:
        yield sse('error', {'error': 'MiniMax api_key is not configured'})
        return
    else:
        thinking_q: queue.Queue = queue.Queue()
        result_holder: dict = {}
        sentinel = object()

        def on_thinking(text: str):
            try:
                thinking_q.put(text, timeout=1)
            except Exception:
                pass

        def runner():
            try:
                txt = ga.call_minimax_streaming(
                    api_key, api_base, model, system_prompt, user_content,
                    max_tokens=8000, on_thinking=on_thinking,
                )
                result_holder['text'] = txt
            except ga.ProviderAuthenticationError as e:
                result_holder['auth_error'] = str(e)
            except Exception as e:
                result_holder['error'] = str(e)
            finally:
                thinking_q.put(sentinel)

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        # Forward thinking deltas as they arrive
        while True:
            try:
                item = thinking_q.get(timeout=120)
            except queue.Empty:
                yield sse('error', {'error': 'LLM stream timeout (120s)'})
                return
            if item is sentinel:
                break
            text = str(item).strip()
            if text:
                yield sse('thinking-ai', {'stage': 2, 'text': text[:500]})
        t.join(timeout=2)
        if result_holder.get('auth_error'):
            yield sse('error', {'error': result_holder['auth_error']})
            return
        if result_holder.get('error'):
            yield sse('error', {
                'error': f'LLM 调用异常: {result_holder["error"]}',
            })
            return
        result_text = result_holder.get('text')

    yield sse('stage', {'index': 2, 'status': 'done'})

    # Stage 3 — parse + persist
    yield sse('stage', {'index': 3, 'status': 'active'})
    parsed_action = _parse_cluster_action_response(result_text, ga)

    # Build final action dict. Cluster generation must not persist fake fallback
    # actions; an unparsable model response should be visible as an error.
    action_id = str(uuid.uuid4())
    if parsed_action and parsed_action.get('title') and parsed_action.get('prompt'):
        action_title = str(parsed_action.get('title', ''))[:120].strip()
        action_type = action_type_hint or parsed_action.get('action_type', 'investigate')
        if action_type not in ('investigate', 'implement', 'content', 'track'):
            action_type = 'investigate'
        action_prompt = str(parsed_action.get('prompt', '')).strip()
        priority = parsed_action.get('priority', 'medium')
        if priority not in ('high', 'medium', 'low'):
            priority = 'medium'
        # BF-0706-#3: 结构化 reason 存 JSON(不能 str() 成单引号 repr → 前端乱码);纯字符串截断 300
        _raw_reason = parsed_action.get('reason', 'generated from event cluster')
        if isinstance(_raw_reason, (list, dict)):
            reason = json.dumps(_raw_reason, ensure_ascii=False)
        else:
            reason = str(_raw_reason)[:300]
        # v21.0: 结构化行动点(人看),与自包含 prompt(机器执行)分离
        raw_steps = parsed_action.get('steps')
        if isinstance(raw_steps, str):
            try:
                raw_steps = json.loads(raw_steps)
            except (json.JSONDecodeError, TypeError):
                raw_steps = None
        action_steps = (
            [str(s).strip() for s in raw_steps if str(s or '').strip()]
            if isinstance(raw_steps, list) else None
        )
        yield sse('thinking', {'stage': 3, 'text': f'生成行动点: {action_title}'})
    else:
        yield sse('error', {'error': 'LLM 输出未能解析为可执行 action JSON'})
        return

    # v21.0 修复:生产 Supabase 模式必须写 remote,否则 cluster 行动点落错库、
    # 不出现在行动 Tab。local/remote 双路径 + 持久化后刷新 detail read model。
    if remote_db.app_state_to_remote():
        action_id = remote_db.create_action_remote(
            source_type='cluster',
            title=action_title,
            action_type=action_type,
            prompt=action_prompt,
            source_item_ids=source_item_ids,
            reason=reason,
            priority=priority,
            status='pending',
            user_id=uid,
            source_id=str(cluster_id),
            cluster_version=live_version,
            steps=action_steps,
        )
    else:
        conn2 = db.get_conn()
        try:
            conn2.execute(
                """INSERT INTO actions
                     (id, user_id, source_type, source_id, cluster_version,
                      source_item_ids, title, action_type, prompt, steps, reason,
                      priority, status, is_stale)
                   VALUES (?, ?, 'cluster', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)""",
                (action_id, uid, str(cluster_id), live_version,
                 json.dumps(source_item_ids, ensure_ascii=False),
                 action_title, action_type, action_prompt,
                 json.dumps(action_steps, ensure_ascii=False) if action_steps else None,
                 reason, priority),
            )
            conn2.commit()
        finally:
            conn2.close()

    _refresh_cluster_action_read_model(action_id, uid)

    yield sse('stage', {'index': 3, 'status': 'done'})

    # Final result + done — match v10.1 envelope
    action_payload = {
        'id': action_id,
        'title': action_title,
        'action_type': action_type,
        'prompt': action_prompt,
        'steps': action_steps,
        'priority': priority,
        'reason': reason,
        'source_type': 'cluster',
        'source_id': cluster_id,
        'cluster_version': live_version,
        'source_item_ids': source_item_ids,
    }
    yield sse('result', {'ok': True, 'action': action_payload})
    yield sse('done', {
        'action_id': action_id,
        'title': action_title,
        'source_type': 'cluster',
        'source_id': cluster_id,
        'cluster_version': live_version,
    })


@router.post('/api/clusters/{cluster_id}/actions')
async def cluster_generate_action(request: Request, cluster_id: int):
    uid, err = _require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_hint = (body or {}).get('user_hint') or ''
    req_action_type = (body or {}).get('action_type') or ''
    if req_action_type not in ('investigate', 'implement', 'content', 'track'):
        req_action_type = ''

    # BF: 生产走 remote(Supabase),cluster 不在本地 sqlite;必须 remote 取数,
    # 否则 POST 生成一律 404(GET 走 remote 正常,POST 之前只读本地)。
    if remote_db.events_read_from_remote():
        try:
            detail = await run_in_threadpool(
                remote_db.cluster_detail, cluster_id=cluster_id, user_id=uid)
            mrows = await run_in_threadpool(
                remote_db.collect_cluster_member_rows_remote, None, cluster_id)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if detail is None:
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        cluster_title = detail.get('ai_title') or '(untitled event)'
        cluster_summary = detail.get('ai_summary') or ''
        kp = detail.get('ai_key_points')
        cluster_key_points = kp if isinstance(kp, list) else []
        live_version = int(detail.get('live_version') or 0)
        mrows_sorted = sorted(
            mrows,
            key=lambda r: (
                0 if r.get('is_primary_source') else 1,
                r.get('rank_in_cluster') if r.get('rank_in_cluster') is not None else 9999,
            ),
        )[:10]
        member_list = [
            {'id': r.get('id'), 'title': r.get('title'), 'ai_summary': r.get('ai_summary'),
             'url': r.get('url'), 'platform': r.get('platform')}
            for r in mrows_sorted
        ]
        source_item_ids = [r['id'] for r in member_list]
    else:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT id, ai_title, ai_summary, ai_key_points, live_version "
            "FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if not row:
            conn.close()
            return JSONResponse({'error': 'Cluster not found'}, status_code=404)
        cluster_title = row['ai_title'] or '(untitled event)'
        cluster_summary = row['ai_summary'] or ''
        try:
            cluster_key_points = json.loads(row['ai_key_points'] or '[]')
            if not isinstance(cluster_key_points, list):
                cluster_key_points = []
        except (json.JSONDecodeError, TypeError):
            cluster_key_points = []
        live_version = row['live_version'] or 0

        # Gather member snippets for action prompt context
        member_rows = conn.execute(
            """SELECT i.id, i.title, i.ai_summary, i.url, i.platform
               FROM cluster_items ci JOIN items i ON i.id = ci.item_id
               WHERE ci.cluster_id = ?
               ORDER BY ci.is_primary_source DESC,
                        COALESCE(ci.rank_in_cluster, 9999) ASC
               LIMIT 10""",
            (cluster_id,),
        ).fetchall()
        member_list = [dict(r) for r in member_rows]
        source_item_ids = [r['id'] for r in member_list]
        conn.close()

    # v21.0 限额:非 admin 每日 5 次(item + cluster 合计),发起即计。
    allowed, quota = action_quota.try_consume_for_request(request)
    if not allowed:
        return JSONResponse(
            {'error': f"今日生成次数已用完({quota['used']}/{quota['limit']}),明天再来", 'quota': quota},
            status_code=429,
        )

    return StreamingResponse(
        _emit_cluster_action_sse(
            uid=uid,
            cluster_id=cluster_id,
            live_version=live_version,
            cluster_title=cluster_title,
            cluster_summary=cluster_summary,
            cluster_key_points=cluster_key_points,
            user_hint=user_hint,
            action_type_hint=req_action_type,
            member_rows=member_list,
            source_item_ids=source_item_ids,
        ),
        media_type='text/event-stream',
        headers=_SSE_HEADERS,
    )


# ── POST /api/items/{id}/actions (alias for cluster doc-source flow) ────

@router.post('/api/items/{item_id}/actions')
async def item_generate_action(request: Request, item_id: str):
    """Thin alias to the existing generate-from-item SSE flow (PRD §6.12).

    The underlying implementation stays in routes.actions.generate_from_item;
    we forward by rebuilding the request body so SSE streaming is preserved.
    """
    _, err = _require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    import routes.actions as actions_route
    # Reuse the existing flow: generate_from_item reads body from request.json()
    # so we simply rewrite the body + delegate via a small wrapper.
    async def _mock_json():
        return {'item_id': item_id, **(body or {})}
    request._json = None  # force re-parse inside the delegate
    request.state._override_body = {'item_id': item_id, **(body or {})}
    # Prefer delegating directly to keep behavior identical
    return await actions_route.generate_from_item(request)


# ── GET /api/search?q=&context= ────────────────────────────────────

@router.get('/api/search')
async def context_search(
    request: Request,
    q: str = Query('', max_length=200, description='search keyword'),
    context: str = Query('recommend', pattern='^(recommend|channel|collection|history)$'),
    limit: int = Query(30, ge=1, le=100),
    categories: str | None = Query(None, description='v17.0: 精选 tab pill 筛选叠加搜索, comma-separated L1 ids'),
    events_only: bool = False,
):
    # v17.0: recommend context 允许匿名访问（与 /api/feed/events 一致策略，精选 tab 公开可见）
    # channel/collection/history 三个上下文是登录用户专属（与各自 tab 一致）
    if context != 'recommend':
        _, err = _require_user(request)
        if err:
            return err
    q = (q or '').strip()
    if not q:
        base = {'docs': [], 'docs_total': 0}
        if context == 'recommend':
            base.update({'events': [], 'events_total': 0})
        return base
    # v17.0: categories filter（仅在 cluster 搜索时生效，doc 搜索 v17.0 不支持）
    categories_list = _parse_categories_filter(categories)
    public_only = _is_anonymous_public_request(request)
    min_github_stars = _github_cluster_display_min_stars()
    remote_search = (
        remote_db.feed_read_from_remote()
        or remote_db.events_read_from_remote()
        or remote_db.remote_authority_enabled()
    )
    cache_key = None
    if (
        remote_search
        and context == 'recommend'
        and events_only
        and limit == 20
        and not categories_list
        and is_public_get_request(request, public_only=public_only)
    ):
        cache_key = ("context_search", _public_cache_scope(), q, context, limit, events_only, min_github_stars)
        cached = get_public_json_response(cache_key)
        if cached is not None:
            return cached
    # v17.0: 远程 Supabase 路径 — 用户主诉"所有数据交互应走远程"。
    # remote_authority_enabled 或 events_read_from_remote 任一为真，搜索走 remote。
    if remote_search:
        try:
            # BE-1: ILIKE 搜索是最贵的交互查询之一,必须离开事件循环
            result = await run_in_threadpool(
                remote_db.context_search,
                q=q,
                context=context,
                limit=limit,
                user_id=current_user_id(request),
                public_only=public_only,
                manual_owner_user_id=None if can_access_all(request) else current_user_id(request),
                min_github_stars=min_github_stars,
                categories=categories_list,
                events_only=events_only,
            )
            if cache_key is not None:
                return set_public_json_response(cache_key, result)
            return result
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
    pattern = f'%{q}%'
    cat_clause, cat_params = _categories_sql_clause(categories_list)
    # 将 c. 前缀改成 cluster 表别名为 'clusters'（搜索 SQL 用 clusters 表名，不取别名）
    # _categories_sql_clause 默认用 'c.id' 参考；这里改用 clusters.id
    cat_clause = cat_clause.replace('c.id', 'clusters.id') if cat_clause else ''
    conn = db.get_conn()
    try:
        # Docs search (all contexts)
        if events_only and context == 'recommend':
            out: dict[str, Any] = {'docs': [], 'docs_total': 0}
        else:
            doc_rows = conn.execute(
                """SELECT id, platform, title, author_name, published_at, ai_summary
                   FROM items
                   WHERE title LIKE ? OR ai_summary LIKE ? OR content LIKE ?
                   ORDER BY COALESCE(published_at, fetched_at) DESC
                   LIMIT ?""",
                (pattern, pattern, pattern, limit),
            ).fetchall()
            docs_total = conn.execute(
                """SELECT COUNT(*) FROM items
                   WHERE title LIKE ? OR ai_summary LIKE ? OR content LIKE ?""",
                (pattern, pattern, pattern),
            ).fetchone()[0]
            out = {
                'docs': [dict(r) for r in doc_rows],
                'docs_total': docs_total,
            }
        if context == 'recommend':
            # v15.1 visibility threshold parity (PRD §5.17): /api/search must
            # apply the same `unique_source_count >= 2` gate as
            # /api/feed/events; otherwise pre-cutover V1 clusters
            # (unique_source_count=0 by DEFAULT) leak via search even though
            # the feed correctly hides them.
            ev_rows = conn.execute(
                f"""SELECT id, ai_title, ai_summary, why_read, doc_count,
                          unique_source_count, first_doc_at, last_doc_at,
                          platforms_json, cover_url, live_version
                   FROM clusters
                   WHERE is_visible_in_feed = 1 AND unique_source_count >= 2
                     AND published_at IS NOT NULL
                     AND archived = 0 AND merged_into IS NULL
                     AND (ai_title LIKE ? OR ai_summary LIKE ?)
                     {cat_clause}
                   ORDER BY first_doc_at DESC
                   LIMIT ?""",
                (pattern, pattern, *cat_params, limit),
            ).fetchall()
            ev_total = conn.execute(
                f"""SELECT COUNT(*) FROM clusters
                   WHERE is_visible_in_feed = 1 AND unique_source_count >= 2
                     AND published_at IS NOT NULL
                     AND archived = 0 AND merged_into IS NULL
                     AND (ai_title LIKE ? OR ai_summary LIKE ?)
                     {cat_clause}""",
                (pattern, pattern, *cat_params),
            ).fetchone()[0]
            source_metadata = _load_event_source_metadata(conn, [int(r['id']) for r in ev_rows])
            out['events'] = [
                _row_to_event(r, user_last_seen={}, source_metadata=source_metadata)
                for r in ev_rows
            ]
            out['events_total'] = ev_total
        if cache_key is not None:
            return set_public_json_response(cache_key, out)
        return out
    finally:
        conn.close()
