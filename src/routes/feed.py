"""Feed, stats, trends, export, item status, and feedback endpoints."""

import csv
import io
import json
import os
import re
import threading
import time
import urllib.request
from collections import Counter

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import db
import feedback_store
import ranking
import remote_db
from authz import can_access_all, require_admin
from deps import BASE
from routes.public_response_cache import (
    get_public_json_response,
    is_public_get_request,
    set_public_json_response,
)
from time_utils import to_utc_iso

router = APIRouter()


def _get_user_id(request: Request):
    """Extract user_id from request state, or None if not authenticated."""
    user = getattr(request.state, 'user', None)
    return user['id'] if user else None


def _is_anonymous_public_request(request: Request) -> bool:
    """True for public anonymous reads; legacy token access is treated as trusted."""
    return _get_user_id(request) is None and not getattr(request.state, 'legacy_authenticated', False)


def _manual_owner_user_id(request: Request):
    """Regular users only see their own manual submissions; admin/legacy see all."""
    if can_access_all(request):
        return None
    return _get_user_id(request)


# ── helpers ──────────────────────────────────────────────────

def _safe_json_loads(val):
    """Parse a JSON string, returning original value on failure."""
    if val and isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def _normalize_library_item_entry(item, status_field: str) -> dict:
    payload = dict(item)
    for col in ('media_json', 'metrics_json', 'tags_json', 'ai_key_points'):
        payload[col] = _safe_json_loads(payload.get(col))
    payload.pop('detail_json', None)
    payload.pop('comments_json', None)
    occurred_at = to_utc_iso(payload.get(status_field)) or payload.get(status_field) or payload.get('fetched_at')
    return {
        'id': f"item:{payload.get('id')}",
        'type': 'item',
        'occurred_at': occurred_at,
        'item': payload,
    }


def _cluster_entry_from_row(row, *, status_field: str) -> dict:
    platforms = []
    try:
        platforms = json.loads(row['platforms_json'] or '[]')
    except Exception:
        platforms = []
    viewer_status = {
        'clicked_at': to_utc_iso(row['clicked_at']) if row['clicked_at'] else None,
        'starred_at': to_utc_iso(row['starred_at']) if row['starred_at'] else None,
        'last_seen_version': row['last_seen_version'],
    }
    cover_url = row['cover_url']
    cluster = {
        'id': row['id'],
        'ai_title': row['ai_title'],
        'ai_summary': row['ai_summary'],
        'doc_count': row['doc_count'],
        'unique_source_count': int(row['unique_source_count'] or 0),
        'platforms': platforms,
        'category': row['category'],
        'first_doc_at': to_utc_iso(row['first_doc_at']) or row['first_doc_at'],
        'last_doc_at': to_utc_iso(row['last_doc_at']) if row['last_doc_at'] else None,
        'cover_url': cover_url,
        'media_urls': [cover_url] if cover_url else [],
        'live_version': row['live_version'],
        'user_last_seen_version': row['last_seen_version'],
        'is_visible_in_feed': bool(row['is_visible_in_feed']),
        'viewer_status': viewer_status,
    }
    occurred_at = to_utc_iso(row[status_field]) or row[status_field]
    return {
        'id': f"cluster:{row['id']}",
        'type': 'cluster',
        'occurred_at': occurred_at,
        'cluster': cluster,
    }


def load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _github_display_min_stars() -> int:
    return db.github_min_stars_for_display()


def _disabled_platforms() -> set[str]:
    """Platforms with `enabled=false` in config.json — hidden from /api/feed/platforms.

    Why: 抓取下线的平台（如 v16.0 下线的 xiaohongshu）历史数据仍残留在 items / MV，
    单看 platforms 端点会有 ghost 入口（counts > 0），但 /api/feed?platform=X 已返 0。
    用 enabled 作为唯一开关，前端不需要再判断。
    """
    cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
    disabled = set()
    for platform, settings in cfg.items():
        if isinstance(settings, dict) and settings.get('enabled') is False:
            disabled.add(platform)
    return disabled


def _prune_disabled_platforms(result: dict) -> dict:
    """Drop disabled platforms from sections / *_counts in /api/feed/platforms result.

    remote_db.query_feed_platforms 走 singleflight，可能把同一 dict 引用交给多个并发
    请求，所以原地改会污染别的 caller。返回浅拷贝外层 + 对受改 bucket 做浅拷贝。
    """
    disabled = _disabled_platforms()
    if not disabled:
        return result
    out = dict(result)
    for key in ('sections', 'platform_counts', 'source_counts', 'category_counts', 'platform_next_cursors'):
        bucket = out.get(key)
        if isinstance(bucket, dict) and any(p in bucket for p in disabled):
            out[key] = {p: v for p, v in bucket.items() if p not in disabled}
    return out


def _remote_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({
        'error': 'Remote feed read failed',
        'detail': str(exc),
        'data_backend': remote_db.feed_read_backend(),
    }, status_code=503)


def _optional_query_str(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _public_cache_scope() -> tuple[str, str]:
    return (str(BASE), str(getattr(db, "DB_PATH", "")))


def _optional_read_model_cursor(value):
    raw = _optional_query_str(value)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    version_id = _optional_query_str(data.get('version_id'))
    scope_key = _optional_query_str(data.get('scope_key'))
    try:
        rank_after = int(data.get('rank_after'))
    except (TypeError, ValueError):
        return None
    if not version_id or not scope_key or rank_after < 0:
        return None
    exclude_ids = []
    for raw_item_id in data.get('exclude_ids') or []:
        item_id = _optional_query_str(str(raw_item_id or ''))
        if item_id and item_id not in exclude_ids:
            exclude_ids.append(item_id)
        if len(exclude_ids) >= 200:
            break
    cursor = {
        'version_id': version_id,
        'scope_key': scope_key,
        'rank_after': rank_after,
    }
    if exclude_ids:
        cursor['exclude_ids'] = exclude_ids
    return cursor


@router.get("/api/admin/info-read-model/freshness")
def get_info_read_model_freshness(request: Request):
    """Read-only admin probe for Info Tab read-model freshness."""
    err = require_admin(request)
    if err:
        return err
    if not remote_db.feed_read_from_remote():
        return {
            "enabled": False,
            "read_model": "info_platforms_v1",
            "data_backend": remote_db.feed_read_backend(),
            "reason": "remote_feed_disabled",
        }
    try:
        return remote_db.info_read_model_freshness_remote(
            min_github_stars=_github_display_min_stars(),
        )
    except remote_db.RemoteDBError as exc:
        return _remote_error_response(exc)


# ============================================================
# EX-3: Trend computation (24h keyword frequency + hot authors)
# ============================================================
_TREND_STOP = {
    # 中文常见虚词
    '的','了','在','是','和','与','为','有','到','从','被','对','就','也','都',
    '还','个','这','那','我','你','他','它','们','不','很','会','能','要',
    '可以','已经','一个','什么','怎么','如何','但是','所以','因为','而且',
    '或者','通过','使用','进行','可能','需要','关于','以及','支持','功能',
    '内容','视频','分享','推荐','目前','非常','真的','这个','那个','自己',
    '没有','一下','大家','介绍','学习','方法','提供','了解','发布','更新',
    '开始','实现','教程','文章',
    # 英文常见词 & URL 碎片
    'https','http','www','com','org','net','html','t.co','pic',
    'the','this','that','with','from','your','have','has','had','been',
    'for','are','was','were','will','can','not','but','and','you',
    'its','our','all','new','one','two','get','got','use','how',
    'what','why','who','just','more','also','about','into','over',
    'out','now','like','even','still','much','very','really','actually',
    'pretty','good','great','best','well','need','want','try','work',
    'way','thing','look','check','make','made','been','does','did',
    'using','used','based',
    'vibe','coding','tutorial',
}


def _filter_trends_via_ai(candidates):
    """Use MiniMax to filter trend keywords, keeping only AI/tech-relevant ones."""
    try:
        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        ai = cfg.get('ai_summary', {})
        api_key = ai.get('api_key', '')
        api_base = ai.get('api_base', 'https://api.minimaxi.com/v1')
        model = ai.get('model', 'MiniMax-Text-01')
        if not api_key:
            return candidates[:10]

        words = [c['word'] for c in candidates]
        from prompt_loader import load_prompt
        prompt = load_prompt('06b_trend_filtering.md')
        if not prompt:
            prompt = (
                "你是AI新闻编辑。从以下关键词列表中，只保留与AI/科技行业相关的有意义词汇。\n"
                "保留：产品名、模型名、公司名、技术术语、具体工具名\n"
                "移除：虚词（值得关注、实际上、具体等）、太宽泛的词（产品、技术、发布等）、日常用语\n"
                "返回格式：每行一个词，只返回保留的词，不要解释"
            )
        content = "关键词列表：\n" + "\n".join(words)

        url = f"{api_base}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content}
            ],
            "max_tokens": 200,
            "temperature": 0.1
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = data['choices'][0]['message']['content'].strip()
            kept = {line.strip().lower() for line in text.split('\n') if line.strip()}
            return [c for c in candidates if c['word'].lower() in kept][:10]
    except Exception as e:
        print(f"Trend AI filter error: {e}")
        return candidates[:10]


_trend_cache = {}


def _compute_trends(conn, public_only=False, manual_owner_user_id=None):
    """Return top-10 keywords and top-5 authors from items fetched in the last 24h."""
    now_ts = time.time()
    cache_key = ('public' if public_only else 'scope', manual_owner_user_id or 'all')
    cached = _trend_cache.get(cache_key)
    if cached and cached['keywords'] and (now_ts - cached['ts']) < 3600:
        return {'keywords': cached['keywords'], 'authors': [], 'item_count': cached.get('item_count', 0)}

    where = ["fetched_at > datetime('now', '-24 hours')"]
    params = []
    if public_only:
        where.append("platform != 'manual'")
    elif manual_owner_user_id:
        where.append("(platform != 'manual' OR user_id = ?)")
        params.append(manual_owner_user_id)

    rows = conn.execute(
        "SELECT title, ai_summary, author_name FROM items WHERE " + " AND ".join(where),
        params,
    ).fetchall()
    if not rows:
        return {'keywords': [], 'authors': [], 'item_count': 0}

    kw_counter = Counter()
    author_counter = Counter()
    for r in rows:
        title = r['title'] or ''
        summary = r['ai_summary'] or ''
        txt = title + ' ' + summary
        # Chinese 2-4 char segments
        cn = re.findall(r'[\u4e00-\u9fff]{2,4}', txt)
        # English tokens
        en = re.findall(r'[A-Za-z][A-Za-z0-9.]{2,15}', txt)
        for w in cn + en:
            k = w.lower()
            if k not in _TREND_STOP and len(k) >= 2:
                kw_counter[k] += 1
        author = r['author_name']
        if author:
            author_counter[author] += 1

    candidates = [{'word': w, 'count': c} for w, c in kw_counter.most_common(20) if c >= 2]
    top_kw = _filter_trends_via_ai(candidates) if candidates else []
    result = {'keywords': top_kw, 'authors': [], 'item_count': len(rows)}
    _trend_cache[cache_key] = {'keywords': top_kw, 'ts': time.time(), 'item_count': len(rows)}
    return result


def _extract_keywords_via_ai(title, summary, topic_name, existing_kw):
    """Call MiniMax to extract 2-3 new keywords from an item's title+summary."""
    try:
        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        ai = cfg.get('ai_summary', {})
        api_key = ai.get('api_key', '')
        api_base = ai.get('api_base', 'https://api.minimaxi.com/v1')
        model = ai.get('model', 'MiniMax-Text-01')
        if not api_key:
            return []

        from prompt_loader import load_prompt
        prompt = load_prompt('06_keyword_extraction.md',
                             topic_name=topic_name,
                             existing_keywords=', '.join(existing_kw))
        if not prompt:
            prompt = (
                "你是关键词提取助手。从下面的标题和摘要中提取2-3个高价值关键词，"
                "用于在社交媒体上搜索更多同类内容。\n"
                "要求：\n"
                "1. 提取专有名词（产品名、技术名、公司名）和核心主题词\n"
                "2. 不要提取过于宽泛的词（如\"AI\"、\"技术\"、\"产品\"）\n"
                "3. 不要和已有关键词重复\n"
                "4. 只返回关键词，每行一个，不要编号不要解释\n\n"
                f"当前主题：{topic_name}\n"
                f"已有关键词：{', '.join(existing_kw)}"
            )
        content = f"标题：{title}\n摘要：{summary}"

        url = f"{api_base}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content[:4000]}
            ],
            "max_tokens": 100,
            "temperature": 0.3
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            text = data['choices'][0]['message']['content'].strip()
            # Parse: one keyword per line
            keywords = [kw.strip().strip('-').strip('*').strip() for kw in text.split('\n') if kw.strip()]
            # Filter out empties and duplicates with existing
            existing_lower = {k.lower() for k in existing_kw}
            keywords = [kw for kw in keywords if kw and kw.lower() not in existing_lower and len(kw) >= 2]
            return keywords[:3]
    except Exception as e:
        print(f"Keyword extraction error: {e}")
        return []


# ============================================================
# Endpoints
# ============================================================

@router.get("/api/feed")
def get_feed(
    request: Request,
    platform: str = Query(None),
    source: str = Query(None),
    unread: bool = Query(False),
    starred: bool = Query(False),
    clicked: bool = Query(False),
    search: str = Query(None),
    limit: int = Query(0),
    offset: int = Query(0),
):
    user_id = _get_user_id(request)
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    if remote_db.feed_read_from_remote():
        try:
            body = remote_db.query_feed(
                platform=platform,
                source=source,
                unread=unread,
                starred=starred,
                clicked=clicked,
                search=search,
                limit=limit,
                offset=offset,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=_github_display_min_stars(),
            )
            return JSONResponse(body)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        items = db.query_feed(
            conn, platform=platform, source=source,
            unread=unread, starred=starred, clicked=clicked,
            limit=limit, offset=offset, search=search,
            user_id=user_id, public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
        )
        # Count total matching items (same filters, no limit/offset)
        where, cnt_params = [], []
        # Build user-scoped join for count query
        cnt_join, join_params = db._item_status_join(conn, user_id)
        cnt_params.extend(join_params)
        if search:
            like = f'%{search}%'
            where.append("(i.title LIKE ? OR i.content LIKE ? OR i.author_name LIKE ? OR i.ai_summary LIKE ?)")
            cnt_params += [like, like, like, like]
        if platform:
            where.append("i.platform = ?"); cnt_params.append(platform)
        if public_only:
            where.append("i.platform != 'manual'")
        elif manual_owner_user_id:
            where.append("(i.platform != 'manual' OR i.user_id = ?)")
            cnt_params.append(manual_owner_user_id)
        db._add_display_visibility(where, cnt_params)
        if source:
            where.append("i.source = ?"); cnt_params.append(source)
        if unread:
            where.append("(s.item_id IS NULL OR (s.clicked_at IS NULL AND s.hidden_at IS NULL))")
        if starred:
            where.append("s.starred_at IS NOT NULL")
        if clicked:
            where.append("s.clicked_at IS NOT NULL")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM items i {cnt_join} {where_sql}",
            cnt_params,
        ).fetchone()[0]

        # Parse JSON string columns for frontend consumption
        # Exclude heavy detail_json and comments_json from list response
        for item in items:
            for col in ('media_json', 'metrics_json', 'tags_json', 'ai_key_points'):
                val = item.get(col)
                if val and isinstance(val, str):
                    try:
                        item[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
            item.pop('detail_json', None)
            item.pop('comments_json', None)
    finally:
        conn.close()

    return JSONResponse({'items': items, 'total': total, 'offset': offset, 'limit': limit})


@router.get("/api/library")
def get_library(
    request: Request,
    view: str = Query('history'),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Personal library feed for history/favorites.

    v18.1 keeps item and cluster semantics separate: entries are returned as
    `type=item|cluster` instead of coercing clusters into FeedItem.
    """
    if view not in ('history', 'starred'):
        return JSONResponse({'error': 'view must be history or starred'}, status_code=400)
    user_id = _get_user_id(request)
    if not user_id:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    if remote_db.feed_read_from_remote() or remote_db.events_read_from_remote():
        try:
            body = remote_db.query_library(
                view=view,
                limit=limit,
                offset=offset,
                user_id=user_id,
                manual_owner_user_id=_manual_owner_user_id(request),
                min_github_stars=_github_display_min_stars(),
            )
            return JSONResponse(body)
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    status_field = 'clicked_at' if view == 'history' else 'starred_at'
    conn = db.get_conn()
    try:
        items = db.query_feed(
            conn,
            clicked=(view == 'history'),
            starred=(view == 'starred'),
            limit=0,
            offset=0,
            user_id=user_id,
            public_only=False,
            manual_owner_user_id=_manual_owner_user_id(request),
        )
        item_entries = [
            _normalize_library_item_entry(item, status_field)
            for item in items
            if item.get(status_field)
        ]

        cluster_rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.doc_count,
                      c.unique_source_count, c.platforms_json,
                      COALESCE(NULLIF(c.cover_url, ''), (
                        SELECT i.cover_url
                        FROM cluster_items ci_cover
                        JOIN items i ON i.id = ci_cover.item_id
                        WHERE ci_cover.cluster_id = c.id
                          AND NULLIF(i.cover_url, '') IS NOT NULL
                        ORDER BY COALESCE(ci_cover.is_primary_source, 0) DESC,
                                 COALESCE(ci_cover.rank_in_cluster, 999999) ASC
                        LIMIT 1
                      )) AS cover_url,
                      c.first_doc_at, c.last_doc_at, c.live_version,
                      c.is_visible_in_feed,
                      (
                        SELECT i.ai_category
                        FROM cluster_items ci_cat
                        JOIN items i ON i.id = ci_cat.item_id
                        WHERE ci_cat.cluster_id = c.id
                          AND NULLIF(i.ai_category, '') IS NOT NULL
                        GROUP BY i.ai_category
                        ORDER BY COUNT(*) DESC
                        LIMIT 1
                      ) AS category,
                      s.clicked_at, s.starred_at, s.last_seen_version
               FROM cluster_status s
               JOIN clusters c ON c.id = s.cluster_id
               WHERE s.user_id = ?
                 AND s.{status_field} IS NOT NULL
                 AND COALESCE(c.archived, 0) = 0
                 AND c.merged_into IS NULL
                 AND NOT EXISTS (
                   SELECT 1
                     FROM cluster_items ci_priv
                     JOIN items i_priv ON i_priv.id = ci_priv.item_id
                    WHERE ci_priv.cluster_id = c.id
                      AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
                      AND COALESCE(i_priv.user_id, '') <> ?
                 )""",
            (user_id, user_id),
        ).fetchall()
        cluster_entries = [
            _cluster_entry_from_row(row, status_field=status_field)
            for row in cluster_rows
        ]

        entries = item_entries + cluster_entries
        entries.sort(key=lambda entry: entry.get('occurred_at') or '', reverse=True)
        total = len(entries)
        page_entries = entries[offset:offset + limit]
        return JSONResponse({
            'entries': page_entries,
            'total': total,
            'offset': offset,
            'limit': limit,
            'view': view,
        })
    finally:
        conn.close()




def _get_user_weights(conn, user_id):
    """Load user profile and convert to ranking weights. Returns None for anonymous."""
    if not user_id:
        return None
    profile = db.get_user_profile(conn, user_id)
    if not profile or not profile.get('onboarding_completed'):
        return None
    return ranking.profile_to_weights(profile)


@router.get("/api/feed/sections")
def get_feed_sections(request: Request, search: str = Query(None)):
    """Return items grouped by ai_category (max 50 per category + real totals).

    Ranking: quality × engagement × freshness_decay (× match_score for logged-in users with profile).
    """
    user_id = _get_user_id(request)
    search = _optional_query_str(search)
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    min_github_stars = _github_display_min_stars()
    feed_remote = remote_db.feed_read_from_remote()
    cache_key = None
    if feed_remote and search is None and is_public_get_request(request, public_only=public_only):
        cache_key = ("feed_sections", _public_cache_scope(), min_github_stars)
        cached = get_public_json_response(cache_key)
        if cached is not None:
            return cached
    if feed_remote:
        try:
            result = remote_db.query_feed_sections(
                per_category=50,
                search=search,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
            if cache_key is not None:
                return set_public_json_response(cache_key, result)
            return result
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        sections, cat_counts = db.query_feed_sections(conn, user_id=user_id, per_category=50,
                                                       public_only=public_only,
                                                       manual_owner_user_id=manual_owner_user_id)
        if search:
            needle = search.lower()
            sections = {
                cat: [
                    item for item in items
                    if needle in (item.get('title') or '').lower()
                    or needle in (item.get('content') or '').lower()
                    or needle in (item.get('author_name') or '').lower()
                    or needle in (item.get('ai_summary') or '').lower()
                ][:50]
                for cat, items in sections.items()
            }
            cat_counts = {cat: len(items) for cat, items in sections.items()}
        total = sum(cat_counts.values())
        user_weights = _get_user_weights(conn, user_id)
        personalized = user_weights is not None
        # Rank items within each section and parse JSON columns
        for cat, cat_items in sections.items():
            ranking.rank_items(cat_items, personalized=personalized, user_weights=user_weights)
            for item in cat_items:
                for col in ('metrics_json',):
                    val = item.get(col)
                    if val and isinstance(val, str):
                        try:
                            item[col] = json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
        result = {'sections': sections, 'total': total, 'cat_counts': cat_counts, 'personalized': personalized}
        if cache_key is not None:
            return set_public_json_response(cache_key, result)
        return result
    finally:
        conn.close()


@router.get("/api/feed/sections/more")
def get_feed_sections_more(
    request: Request,
    category: str = Query(...),
    offset: int = Query(0),
    limit: int = Query(50),
    keyword: str = Query(None),
    search: str = Query(None),
    subcategory: str = Query(None),
    cursor: str = Query(None),
):
    """Load more items for a specific category (server-side pagination).

    Fetches all items for the category (optionally filtered by keyword or L2 subcategory),
    ranks them, then slices by offset/limit so pagination follows ranking_score.
    Returns total so callers can show accurate filtered counts.

    v4.0: subcategory param matches against ai_subcategories JSON array.
    """
    user_id = _get_user_id(request)
    keyword = _optional_query_str(keyword)
    search = _optional_query_str(search)
    subcategory = _optional_query_str(subcategory)
    read_model_cursor = _optional_read_model_cursor(cursor)
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    if remote_db.feed_read_from_remote():
        try:
            return remote_db.query_feed_by_category(
                category=category,
                offset=offset,
                limit=limit,
                keyword=keyword,
                search=search,
                subcategory=subcategory,
                cursor=read_model_cursor,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=_github_display_min_stars(),
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        all_items = db.query_feed_by_category(conn, category=category,
                                              user_id=user_id, keyword=keyword,
                                              subcategory=subcategory,
                                              public_only=public_only,
                                              manual_owner_user_id=manual_owner_user_id)
        user_weights = _get_user_weights(conn, user_id)
        ranking.rank_items(all_items, personalized=user_weights is not None, user_weights=user_weights)
        total = len(all_items)
        safe_offset = max(0, offset)
        safe_limit = max(1, limit)
        items = all_items[safe_offset:safe_offset + safe_limit]
        for item in items:
            for col in ('metrics_json',):
                val = item.get(col)
                if val and isinstance(val, str):
                    try:
                        item[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
        next_offset = safe_offset + len(items)
        return {
            'items': items,
            'category': category,
            'total': total,
            'offset': safe_offset,
            'limit': safe_limit,
            'has_more': next_offset < total,
            'next_offset': next_offset if next_offset < total else None,
        }
    finally:
        conn.close()


# v16.0 W3.T7: per-platform L1 category_counts cache (5min)
# Why cache: get_category_counts() 走 json_each(ai_categories) 全表扫近 7 天，
# 每个 platform 一次 SQL；前端频繁刷 platforms 端点（pill bar 重渲染）会重复算。
# 缓存 key 必须包含 user_id 维度（item_status join 影响数据），但不包含具体平台
# —— 一次 query 统计所有平台。
_CATEGORY_COUNTS_CACHE = {}
_CATEGORY_COUNTS_TTL_SEC = 300


def _get_category_counts_for_all_platforms(conn, *, user_id, public_only,
                                           manual_owner_user_id, platforms):
    """聚合所有 section 的 L1 category_counts（5min cache）。

    返回 dict[platform, dict[l1, count]]。前端按 PLATFORM_ORDER 决定展示哪些
    section 的 pill（参照 decision-anchor #18：L1 维度 section 才用 category_counts）。
    """
    now_ts = time.time()
    cache_key = (
        user_id or '',
        'public' if public_only else 'auth',
        manual_owner_user_id or '',
        tuple(sorted(platforms)),
    )
    cached = _CATEGORY_COUNTS_CACHE.get(cache_key)
    if cached and (now_ts - cached['ts']) < _CATEGORY_COUNTS_TTL_SEC:
        return cached['data']

    out = {}
    for plat in platforms:
        out[plat] = db.get_category_counts(
            conn, plat, user_id=user_id, public_only=public_only,
            manual_owner_user_id=manual_owner_user_id, days=None,
            # BF-0512-6: days=None 全 DB 对账（含「未分类」NULL 历史 item）；
            # 7d 窗口在 7d 内 NULL=0 时让用户看不到「未分类」pill 解释 N 条去哪
        )
    _CATEGORY_COUNTS_CACHE[cache_key] = {'data': out, 'ts': now_ts}
    return out


@router.get("/api/feed/platforms")
def get_feed_platforms(request: Request, search: str = Query(None)):
    """Get items grouped by platform (each platform up to 50 items) + platform_counts.

    v16.0 W3.T7: 同时返回 category_counts (每平台近 7 天 L1 分布) 给所有平台,
    前端按 PLATFORM_ORDER 区分 source 维度 vs L1 维度 section（decision-anchor #17/#18），
    L1 维度 section（github/reddit/rss/hackernews/waytoagi/manual）的 pill bar 用之。
    """
    user_id = _get_user_id(request)
    search = _optional_query_str(search)
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    min_github_stars = _github_display_min_stars()
    feed_remote = remote_db.feed_read_from_remote()
    cache_key = None
    if feed_remote and search is None and is_public_get_request(request, public_only=public_only):
        cache_key = ("feed_platforms", _public_cache_scope(), min_github_stars)
        cached = get_public_json_response(cache_key)
        if cached is not None:
            return cached
    if feed_remote:
        try:
            result = remote_db.query_feed_platforms(
                per_platform=50,
                search=search,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
            result = _prune_disabled_platforms(result)
            if cache_key is not None:
                return set_public_json_response(cache_key, result)
            return result
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        sections, platform_counts, source_counts = db.query_feed_platforms(
            conn, user_id=user_id, per_platform=50, public_only=public_only,
            manual_owner_user_id=manual_owner_user_id)
        if search:
            needle = search.lower()
            filtered_sections = {}
            platform_counts = {}
            source_counts = {}
            for plat, items in sections.items():
                matches = [
                    item for item in items
                    if needle in (item.get('title') or '').lower()
                    or needle in (item.get('content') or '').lower()
                    or needle in (item.get('author_name') or '').lower()
                    or needle in (item.get('ai_summary') or '').lower()
                ][:50]
                if matches:
                    filtered_sections[plat] = matches
                    platform_counts[plat] = len(matches)
                    for item in matches:
                        source_counts.setdefault(plat, {})[item.get('source') or ''] = source_counts.setdefault(plat, {}).get(item.get('source') or '', 0) + 1
            sections = filtered_sections
        for plat, items in sections.items():
            for item in items:
                for col in ('metrics_json',):
                    val = item.get(col)
                    if val and isinstance(val, str):
                        try:
                            item[col] = json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
        category_counts = _get_category_counts_for_all_platforms(
            conn, user_id=user_id, public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            platforms=list(sections.keys()),
        )
        result = _prune_disabled_platforms({
            'sections': sections,
            'platform_counts': platform_counts,
            'source_counts': source_counts,
            'category_counts': category_counts,
        })
        if cache_key is not None:
            return set_public_json_response(cache_key, result)
        return result
    finally:
        conn.close()


@router.get("/api/feed/platforms/more")
def get_feed_platforms_more(
    request: Request,
    platform: str = Query(...),
    offset: int = Query(0),
    limit: int = Query(50),
    source: str = Query(None),  # 按 source 过滤（频道页 pill 切换用）
    group: str = Query(None),  # BF-0419-10: 按 detail_json.group 过滤(公众号订阅分组)
    category: str = Query(None),  # v16.0 W3.T7: L1 维度 pill 切换（github/reddit/rss/hackernews/waytoagi/manual）
    search: str = Query(None),
    exclude_ids: str = Query(None),
    cursor: str = Query(None),
):
    """Load more items for a specific platform (server-side pagination).
    可选 source 参数：当频道页 pill 切换到某个 source 时走服务端过滤，
    避免前端在已加载的 50 条里客户端 filter（BF-0418-9 结构性修复）。
    可选 group 参数(BF-0419-10): 公众号平台按订阅分组过滤,数据从 ingest 时
    存进 detail_json.group 字段。
    可选 category 参数(v16.0 W3.T7): L1 维度 section 的 pill 切换，按
    ai_categories JSON array 任意元素过滤；前端不会同时传 source + category，
    若同时传则 source/group/category 由 db helper 各自 AND（容错处理）。
    """
    user_id = _get_user_id(request)
    source = _optional_query_str(source)
    group = _optional_query_str(group)
    category = _optional_query_str(category)
    search = _optional_query_str(search)
    read_model_cursor = _optional_read_model_cursor(cursor)
    excluded_item_ids = [
        item_id.strip()
        for item_id in (exclude_ids or "").split(",")
        if item_id.strip()
    ][:200]
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    if remote_db.feed_read_from_remote():
        try:
            return remote_db.query_feed_by_platform(
                platform=platform,
                offset=offset,
                limit=limit,
                source=source,
                group=group,
                category=category,
                search=search,
                exclude_ids=excluded_item_ids,
                cursor=read_model_cursor,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=_github_display_min_stars(),
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        total = db.count_feed_by_platform(conn, platform=platform,
                                          source=source, group=group,
                                          category=category,
                                          public_only=public_only,
                                          manual_owner_user_id=manual_owner_user_id)
        local_offset = 0 if excluded_item_ids else offset
        local_limit = limit + len(excluded_item_ids) if excluded_item_ids else limit
        items = db.query_feed_by_platform(conn, platform=platform,
                                          offset=local_offset, limit=local_limit,
                                          user_id=user_id, source=source, group=group,
                                          category=category,
                                          public_only=public_only,
                                          manual_owner_user_id=manual_owner_user_id)
        if excluded_item_ids:
            excluded = set(excluded_item_ids)
            items = [item for item in items if str(item.get("id")) not in excluded][:limit]
        for item in items:
            for col in ('metrics_json',):
                val = item.get(col)
                if val and isinstance(val, str):
                    try:
                        item[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
        next_offset = max(0, offset) + len(items)
        return {
            'items': items,
            'platform': platform,
            'category': category,
            'total': total,
            'offset': max(0, offset),
            'limit': limit,
            'has_more': next_offset < total,
            'next_offset': next_offset if next_offset < total else None,
        }
    finally:
        conn.close()


@router.get("/api/feed/items/bundle")
def get_feed_items_bundle(request: Request, ids: str = Query("")):
    item_ids = [part.strip() for part in ids.split(",") if part.strip()]
    item_ids = item_ids[:30]
    if not item_ids:
        return {"items": []}

    user_id = _get_user_id(request)
    public_only = _is_anonymous_public_request(request)
    if remote_db.feed_read_from_remote():
        try:
            items = remote_db.get_feed_items(
                item_ids=item_ids,
                public_only=public_only,
                can_access_all=can_access_all(request),
                user_id=user_id,
                min_github_stars=_github_display_min_stars(),
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        return {"items": items}

    conn = db.get_conn()
    try:
        join_sql, join_params = db._item_status_join(conn, user_id)
        placeholders = ",".join(["?"] * len(item_ids))
        rows = conn.execute(
            "SELECT i.*, s.read_at, s.clicked_at, s.starred_at, s.hidden_at "
            f"FROM items i {join_sql} "
            f"WHERE i.id IN ({placeholders})",
            (*join_params, *item_ids),
        ).fetchall()
        by_id = {dict(row)["id"]: db.strip_blob_columns(dict(row)) for row in rows}
        items = []
        for item_id in item_ids:
            item = by_id.get(item_id)
            if not item:
                continue
            if public_only and item.get("platform") == "manual":
                continue
            if item.get("platform") == "manual" and not can_access_all(request):
                if not user_id or item.get("user_id") != user_id:
                    continue
            for col in ('media_json', 'metrics_json', 'tags_json', 'detail_json', 'comments_json', 'ai_key_points', 'asr_segments', 'asr_segments_cn'):
                item[col] = _safe_json_loads(item.get(col))
            items.append(item)
    finally:
        conn.close()
    return {"items": items}


@router.get("/api/feed/item/{item_id}")
def get_feed_item(item_id: str, request: Request):
    user_id = _get_user_id(request)
    public_only = _is_anonymous_public_request(request)
    if remote_db.feed_read_from_remote():
        try:
            item = remote_db.get_feed_item(
                item_id=item_id,
                public_only=public_only,
                can_access_all=can_access_all(request),
                user_id=user_id,
                min_github_stars=_github_display_min_stars(),
            )
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)
        if item is None:
            return JSONResponse({'error': 'Item not found'}, status_code=404)
        return JSONResponse(item)

    conn = db.get_conn()
    try:
        join_sql, join_params = db._item_status_join(conn, user_id)
        row = conn.execute(
            "SELECT i.*, s.read_at, s.clicked_at, s.starred_at, s.hidden_at "
            f"FROM items i {join_sql} "
            "WHERE i.id = ?",
            (*join_params, item_id),
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Item not found'}, status_code=404)
        item = db.strip_blob_columns(dict(row))
        if public_only and item.get('platform') == 'manual':
            return JSONResponse({'error': 'Item not found'}, status_code=404)
        if item.get('platform') == 'manual' and not can_access_all(request):
            if not user_id or item.get('user_id') != user_id:
                return JSONResponse({'error': 'Item not found'}, status_code=404)
        for col in ('media_json', 'metrics_json', 'tags_json', 'detail_json', 'comments_json', 'ai_key_points', 'asr_segments', 'asr_segments_cn'):
            val = item.get(col)
            if val and isinstance(val, str):
                try:
                    item[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
    finally:
        conn.close()
    return JSONResponse(item)


@router.get("/api/stats")
def get_stats(request: Request):
    user_id = _get_user_id(request)
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    if remote_db.feed_read_from_remote():
        try:
            return JSONResponse(remote_db.get_stats(
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=_github_display_min_stars(),
            ))
        except remote_db.RemoteDBError as exc:
            return _remote_error_response(exc)

    conn = db.get_conn()
    try:
        stats = db.get_stats(conn, user_id=user_id, public_only=public_only,
                             manual_owner_user_id=manual_owner_user_id)
    finally:
        conn.close()
    return JSONResponse(stats)


@router.get("/api/trends")
def get_trends(request: Request):
    public_only = _is_anonymous_public_request(request)
    manual_owner_user_id = _manual_owner_user_id(request)
    conn = db.get_conn()
    try:
        trends = _compute_trends(
            conn, public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
        )
    finally:
        conn.close()
    return JSONResponse(trends)


@router.get("/api/export")
def export_csv(
    request: Request,
    fmt: str = Query("csv", alias="format"),
    platform: str = Query(None),
    starred: bool = Query(False),
):
    if fmt != "csv":
        return JSONResponse({'error': 'Unsupported format. Use format=csv'}, status_code=400)

    conn = db.get_conn()
    try:
        user_id = _get_user_id(request)
        where, qp = [], []
        status_user_id = user_id if user_id else None
        join_sql, join_params = db._item_status_join(conn, status_user_id)
        qp.extend(join_params)
        if platform:
            where.append("i.platform = ?"); qp.append(platform)
        if not can_access_all(request):
            where.append("(i.platform != 'manual' OR i.user_id = ?)")
            qp.append(user_id)
        if starred:
            where.append("s.starred_at IS NOT NULL")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            "SELECT i.id, i.platform, i.source, i.title, i.url, "
            "i.published_at, i.fetched_at, i.ai_category, i.relevance_score, "
            "i.ai_summary, i.ai_keywords "
            f"FROM items i {join_sql} "
            + where_sql + " ORDER BY i.fetched_at DESC",
            qp,
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    fields = [
        'id', 'platform', 'source', 'title', 'url',
        'published_at', 'fetched_at', 'ai_category',
        'relevance_score', 'ai_summary', 'ai_keywords',
    ]
    writer.writerow(fields)
    for r in rows:
        writer.writerow([r[f] for f in fields])

    body = buf.getvalue().encode('utf-8-sig')
    return StreamingResponse(
        io.BytesIO(body),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="info2action-export.csv"'},
    )


# ── Item status ─────────────────────────────────────────────

@router.post("/api/status")
async def set_item_status(request: Request):
    user_id = _get_user_id(request)
    body = await request.json()
    item_id = body.get('item_id', '')
    action = body.get('action', '')
    if not item_id or action not in ('clicked', 'starred', 'hidden', 'read'):
        return JSONResponse({'error': 'item_id and valid action required'}, status_code=400)
    if remote_db.status_write_to_remote():
        try:
            # BE-1: 每次点击/收藏必发的最高频写,远程往返必须离开事件循环
            result = await run_in_threadpool(
                remote_db.set_status,
                item_id=item_id,
                action=action,
                user_id=user_id,
                can_access_all=can_access_all(request),
            )
        except remote_db.RemoteDBError as e:
            return _remote_error_response(e)
        if result.get('not_found'):
            return JSONResponse({'error': 'Item not found'}, status_code=404)
        return result

    conn = db.get_conn()
    try:
        item = conn.execute(
            "SELECT platform, user_id FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if item and item['platform'] == 'manual' and not can_access_all(request):
            if not user_id or item['user_id'] != user_id:
                return JSONResponse({'error': 'Item not found'}, status_code=404)
        db.set_status(conn, item_id, action, user_id=user_id)
        return {'ok': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    finally:
        conn.close()


# ── Feedback ────────────────────────────────────────────────

_topics_lock = threading.Lock()


def _update_topics_keywords(topic_name, new_keywords, author_name=None):
    """Add new keywords to a topic in topics.json. Optionally boost an author."""
    topics_path = os.path.join(BASE, 'config', 'topics.json')
    with _topics_lock:
        try:
            with open(topics_path) as f:
                cfg = json.load(f)
        except Exception:
            return False

        changed = False
        for t in cfg.get('topics', []):
            if t['name'] == topic_name:
                existing = set(t.get('keywords', []))
                for kw in new_keywords:
                    if kw not in existing:
                        t['keywords'].append(kw)
                        changed = True
                if author_name and author_name not in t.get('boost_authors', []):
                    t.setdefault('boost_authors', []).append(author_name)
                    changed = True
                break

        if changed:
            with open(topics_path, 'w') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        return changed


@router.get("/api/feedback")
def get_feedback(request: Request):
    err = require_admin(request)
    if err:
        return err
    if remote_db.app_state_to_remote():
        return remote_db.get_feedback_scores_remote()
    conn = db.get_conn()
    scores = db.get_feedback_scores(conn)
    conn.close()
    return scores


@router.post("/api/feedback")
async def post_feedback(request: Request):
    body = await request.json()
    item_id = body.get('item_id', '')
    fb_type = body.get('type', '')
    topic = body.get('topic', '')
    text = body.get('text', '')
    if not item_id or fb_type not in ('positive', 'irrelevant', 'low_quality', 'text'):
        return JSONResponse({'error': 'item_id and valid type required'}, status_code=400)
    if remote_db.app_state_to_remote():
        try:
            row = remote_db.get_feedback_item_context_remote(item_id)
            if not row:
                return JSONResponse({'error': 'Item not found'}, status_code=404)
            if row['platform'] == 'manual' and not can_access_all(request):
                user_id = _get_user_id(request)
                if not user_id or str(row['user_id']) != str(user_id):
                    return JSONResponse({'error': 'Item not found'}, status_code=404)
            remote_db.add_feedback_remote(item_id, fb_type, topic or None, text or None)
            remote_db.record_item_feedback_remote(
                item_id=item_id,
                action=fb_type,
                platform=row.get('platform'),
                title=row.get('title'),
                author=row.get('author_name'),
                url=row.get('url'),
                reason=text or None,
                topic=topic or None,
            )
            result = {'ok': True, 'data_backend': remote_db.app_state_backend()}
            if fb_type == 'positive' and topic and can_access_all(request):
                result['extracting_keywords'] = False
                result['remote_topics_note'] = 'topics.json is local config; remote-only mode records feedback remotely but does not mutate local config.'
            return result
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=400)
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT platform, user_id, title, author_name, url, ai_summary FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return JSONResponse({'error': 'Item not found'}, status_code=404)
        if row['platform'] == 'manual' and not can_access_all(request):
            user_id = _get_user_id(request)
            if not user_id or row['user_id'] != user_id:
                return JSONResponse({'error': 'Item not found'}, status_code=404)

        db.add_feedback(conn, item_id, fb_type, topic or None, text or None)
        result = {'ok': True}

        # Write to independent feedback store
        try:
            fb_conn = feedback_store.get_conn()
            feedback_store.record_item_feedback(
                fb_conn, item_id, fb_type,
                platform=row['platform'],
                title=row['title'],
                author=row['author_name'],
                url=row['url'],
                reason=text or None,
                topic=topic or None,
            )
            fb_conn.close()
        except Exception as e:
            print(f"Feedback store write error: {e}")

        # On positive feedback: extract keywords and update topics.json
        if fb_type == 'positive' and topic and can_access_all(request):
            title_val = row['title'] or ''
            summary_val = row['ai_summary'] or ''
            author_val = row['author_name'] or ''
            topics_cfg = _safe_json_loads(
                open(os.path.join(BASE, 'config', 'topics.json')).read()
            ) if os.path.exists(os.path.join(BASE, 'config', 'topics.json')) else {}
            existing_kw = []
            for t in (topics_cfg.get('topics', []) if isinstance(topics_cfg, dict) else []):
                if t['name'] == topic:
                    existing_kw = t.get('keywords', [])
                    break

            def _bg_extract():
                new_kw = _extract_keywords_via_ai(title_val, summary_val, topic, existing_kw)
                if new_kw:
                    _update_topics_keywords(topic, new_kw, author_val)
                    print(f"[feedback] +keywords for '{topic}': {new_kw}")
            threading.Thread(target=_bg_extract, daemon=True).start()
            result['extracting_keywords'] = True

        return result
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    finally:
        conn.close()
