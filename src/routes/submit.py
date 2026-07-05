"""Submit URL endpoints: manual bookmark + AI summary pipeline.

v13.0 F52: URL 归一化 + Twitter/YouTube platform 分流。
- twitter 视频:fetch_url + run_asr_inline(复用 F31 管线 + 新 ASR 钩子)
- youtube:ingest_youtube_url(字幕优先 + ASR fallback)
- 其他:沿用 F31 通用抓取(不改)
"""

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime
from urllib.parse import urlparse as _urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db
import remote_db
from authz import can_access_all
from deps import BASE
from utils.url_normalize import normalize_url

router = APIRouter()

_BLOCKED_SUBMIT_HOSTS = {'localhost', 'localhost.localdomain'}


def _get_user_id(request: Request):
    user = getattr(request.state, 'user', None)
    return user['id'] if user else None


def _status_join_sql(conn, user_id):
    """Build user-scoped LEFT JOIN for item_status. Returns (join_sql, params)."""
    has_uid = db._check_item_status_has_user_id(conn)
    if has_uid and user_id:
        return "LEFT JOIN item_status s ON i.id = s.item_id AND s.user_id = ?", [user_id]
    return "LEFT JOIN item_status s ON i.id = s.item_id", []


def _can_access_manual_row(request: Request, row) -> bool:
    if not row:
        return False
    if row['platform'] != 'manual':
        return True
    if can_access_all(request):
        return True
    user_id = _get_user_id(request)
    return bool(user_id and str(row['user_id']) == str(user_id))


def _can_access_submit_task(request: Request, task: dict) -> bool:
    if can_access_all(request):
        return True
    user_id = _get_user_id(request)
    return bool(user_id and str(task.get('user_id')) == str(user_id))


def _is_blocked_submit_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return any((
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ))


def _is_blocked_submit_target(hostname: str) -> bool:
    host = (hostname or '').strip().lower().rstrip('.')
    if not host:
        return True
    if host in _BLOCKED_SUBMIT_HOSTS:
        return True
    if _is_blocked_submit_ip(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return any(_is_blocked_submit_ip(info[4][0]) for info in infos)

# Module-level state
_submit_tasks = {}  # task_id -> {status, url, title, error}


@router.post('/api/submit-url')
async def post_submit_url(request: Request):
    body = await request.json()
    url = body.get('url', '').strip()
    manual_content = body.get('content', '').strip()
    manual_title = body.get('title', '').strip()
    if not url:
        return JSONResponse({'error': 'url required'}, status_code=400)

    _up = _urlparse(url)
    if _up.scheme not in ('http', 'https') or not _up.netloc:
        return JSONResponse({'error': '无效的链接格式'}, status_code=400)
    if _is_blocked_submit_target(_up.hostname or ''):
        return JSONResponse({'error': '不支持提交内网或本机地址'}, status_code=400)

    # v13.0 F52: URL 归一化 + platform 分流
    norm = normalize_url(url)
    # Twitter/YouTube 用稳定 ID 做主键;其他继续走 md5(url) 兼容 F31
    item_id = norm.item_id if norm.platform in ('twitter', 'youtube') else hashlib.md5(url.encode()).hexdigest()

    # If this URL is already being processed, return existing task
    if item_id in _submit_tasks and _submit_tasks[item_id]['status'] in ('fetching', 'processing'):
        if not _can_access_submit_task(request, _submit_tasks[item_id]):
            return JSONResponse({'error': 'task not found'}, status_code=404)
        return {'ok': True, 'task_id': item_id, 'status': _submit_tasks[item_id]['status']}

    # Check if already exists with good content
    use_remote = remote_db.app_state_to_remote()
    conn = None if use_remote else db.get_conn()
    existing = (
        remote_db.get_submit_existing_item_remote(item_id, url)
        if use_remote
        else conn.execute(
            "SELECT id, user_id, platform, title, content, ai_summary FROM items WHERE id=? OR url=?",
            (item_id, url),
        ).fetchone()
    )
    if existing:
        if not _can_access_manual_row(request, existing):
            if conn:
                conn.close()
            return JSONResponse({'error': 'item not found'}, status_code=404)
        use_id = existing['id']
        if use_id in _submit_tasks and _submit_tasks[use_id]['status'] in ('fetching', 'processing'):
            if not _can_access_submit_task(request, _submit_tasks[use_id]):
                if conn:
                    conn.close()
                return JSONResponse({'error': 'task not found'}, status_code=404)
            if conn:
                conn.close()
            return {'ok': True, 'task_id': use_id, 'status': _submit_tasks[use_id]['status']}
        ex_content = existing['content'] or ''
        ex_title = existing['title'] or ''
        has_good = (len(ex_content) > 50 and not ex_content.startswith('http')
                    and len(ex_title) > 5 and not ex_title.startswith('http')
                    and existing['ai_summary'])
        if has_good:
            user_id = _get_user_id(request)
            if use_remote:
                remote_db.set_status(
                    item_id=use_id,
                    action='starred',
                    force=True,
                    user_id=str(user_id) if user_id else None,
                    can_access_all=can_access_all(request),
                )
                row = remote_db.get_feed_item(
                    item_id=use_id,
                    public_only=False,
                    can_access_all=can_access_all(request),
                    user_id=str(user_id) if user_id else None,
                    min_github_stars=0,
                )
                return {'ok': True, 'item': row, 'done': True}
            db.set_status(conn, use_id, 'starred', force=True, user_id=user_id)
            join_sql, join_params = _status_join_sql(conn, user_id)
            row = conn.execute(f"SELECT i.*, s.starred_at FROM items i {join_sql} WHERE i.id=?", join_params + [use_id]).fetchone()
            conn.close()
            return {'ok': True, 'item': db.strip_blob_columns(dict(row)), 'done': True}
        item_id = use_id
    if conn:
        conn.close()

    # Start background processing
    submit_user_id = _get_user_id(request)
    _submit_tasks[item_id] = {
        'status': 'fetching',
        'url': url,
        'title': '',
        'error': '',
        'user_id': submit_user_id,
    }

    def _bg_submit(sid, surl, has_existing, bg_user_id=None, norm_platform='manual', norm_canonical=None):
        task = _submit_tasks[sid]
        conn2 = None
        use_remote_bg = remote_db.app_state_to_remote()

        # v13.0 F52: YouTube 手动上传走独立管线(字幕优先 + ASR fallback)
        if norm_platform == 'youtube':
            try:
                conn2 = None if use_remote_bg else db.get_conn()
                import ingest
                canonical = norm_canonical or surl
                task['status'] = 'processing'
                result = ingest.ingest_youtube_url(conn2, sid, canonical)
                if result.get('status') == 'error':
                    task['status'] = 'error'
                    task['error'] = result.get('error', 'YouTube 抓取失败')
                    return
                # 跑统一 AI 理解(摘要 + 分类评分)
                r1 = subprocess.run([sys.executable or 'python3', os.path.join(BASE, 'src', 'enrich_items.py'), '--ids', sid],
                                    cwd=BASE, timeout=180, capture_output=True, text=True)
                ai_errors = []
                if r1.returncode != 0:
                    ai_errors.append('AI理解失败')
                # 自动收藏给 submitter
                if use_remote_bg:
                    remote_db.set_status(
                        item_id=sid,
                        action='starred',
                        force=True,
                        user_id=str(bg_user_id) if bg_user_id else None,
                        can_access_all=False,
                    )
                    row = remote_db.get_feed_item(
                        item_id=sid,
                        public_only=False,
                        can_access_all=True,
                        user_id=str(bg_user_id) if bg_user_id else None,
                        min_github_stars=0,
                    )
                    task['title'] = row.get('title') if row else canonical
                else:
                    db.set_status(conn2, sid, 'starred', force=True, user_id=bg_user_id)
                    row = conn2.execute("SELECT title FROM items WHERE id=?", (sid,)).fetchone()
                    task['title'] = row['title'] if row else canonical
                task['status'] = 'done'
                if ai_errors:
                    task['error'] = '; '.join(ai_errors)
                return
            except Exception as e:
                task['status'] = 'error'
                task['error'] = f'YouTube 处理异常: {e}'
                print(f"Submit URL YouTube background error: {e}")
                return
            finally:
                if conn2:
                    try: conn2.close()
                    except: pass

        try:
            import fetch_url as fu
            page = fu.fetch_url(surl)

            page_content = page.get('content', '')
            page_error = page.get('_error', '')
            if not page_content or len(page_content) < 20:
                if page_error == 'auth_expired':
                    task['status'] = 'error'
                    task['error'] = 'Twitter 认证已过期，无法抓取推文内容'
                    return
                if page_error == 'tweet_not_found':
                    task['status'] = 'error'
                    task['error'] = '推文不存在或已被删除'
                    return
                if page_error == 'wechat_verify':
                    task['status'] = 'error'
                    task['error'] = '微信文章触发了反爬验证，无法直接抓取'
                    return
                if not page_content:
                    task['status'] = 'error'
                    task['error'] = '内容抓取失败，页面无法解析'
                    return

            task['status'] = 'processing'

            now = datetime.now().isoformat()
            conn2 = None if use_remote_bg else db.get_conn()

            if has_existing:
                if page.get('content') and len(page['content']) > 50:
                    updates = {}
                    if page.get('title') and not page['title'].startswith('http'):
                        updates['title'] = page['title']
                    updates['content'] = page['content']
                    if page.get('author'):
                        updates['author_name'] = page['author']
                    if page.get('cover_url'):
                        updates['cover_url'] = page['cover_url']
                    if updates:
                        if use_remote_bg:
                            remote_db.update_item_light_fields_remote(None, sid, updates)
                        else:
                            set_clause = ', '.join(f"{k}=?" for k in updates)
                            conn2.execute(f"UPDATE items SET {set_clause} WHERE id=?",
                                list(updates.values()) + [sid])
                            conn2.commit()
            else:
                # v13.0 F52: Twitter 手动上传用 platform='twitter' 而非 'manual',
                # 保持与 cron 抓的 Twitter item 同表 / 可被 feed platform 过滤
                platform_for_db = 'twitter' if norm_platform == 'twitter' else 'manual'
                item_payload = {
                    'id': sid, 'platform': platform_for_db, 'source': 'user-submit',
                    'user_id': bg_user_id if platform_for_db == 'manual' else None,
                    'title': page.get('title') or surl,
                    'content': page.get('content') or '',
                    'author_name': page.get('author') or '',
                    'url': surl,
                    'cover_url': page.get('cover_url') or '',
                    'media_json': json.dumps(page.get('media')) if page.get('media') else None,
                    'fetched_at': now,
                    'published_at': page.get('published_at') or now,
                }
                if use_remote_bg:
                    remote_db.upsert_item_remote(None, item_payload)
                else:
                    db.upsert_item(conn2, item_payload)
                    conn2.commit()

            # v13.0 F52: Twitter 视频帖子 → 立即跑 ASR(手动触发走 ingest 默认配额检查,
            # 不像详情页 ✦ 按钮 bypass)。失败不阻塞后续 AI 统一理解
            if norm_platform == 'twitter':
                try:
                    if use_remote_bg:
                        row_media = remote_db.get_media_item_remote(sid)
                        row_asr = remote_db.get_item_asr_state_remote(sid) or {}
                    else:
                        row_media = conn2.execute(
                            "SELECT media_json, asr_status FROM items WHERE id=?", (sid,)
                        ).fetchone()
                        row_asr = row_media or {}
                    has_video = False
                    if row_media and row_media['media_json']:
                        try:
                            _media = row_media['media_json']
                            if isinstance(_media, str):
                                _media = json.loads(_media)
                            has_video = any(
                                isinstance(m, dict) and m.get('type') == 'video' and m.get('url')
                                for m in (_media if isinstance(_media, list) else [])
                            )
                        except (ValueError, TypeError):
                            pass
                    asr_status = None
                    if row_asr:
                        asr_status = row_asr.get('asr_status') if isinstance(row_asr, dict) else row_asr['asr_status']
                    # 已跑过(asr_status 非 NULL)不再重复
                    if has_video and asr_status is None:
                        import asr_worker as _aw
                        try:
                            _aw.run_asr_inline(sid, bypass_quota=False, conn=conn2,
                                               max_wait_sec=900, user_id=bg_user_id or 0)
                        except Exception as _e:
                            print(f"[submit] run_asr_inline non-fatal for {sid}: {_e}",
                                  flush=True)
                except Exception as _e:
                    print(f"[submit] twitter ASR hook non-fatal: {_e}", flush=True)

            # Run AI pipeline
            ai_errors = []
            r1 = subprocess.run([sys.executable or 'python3', os.path.join(BASE, 'src', 'enrich_items.py'), '--ids', sid],
                cwd=BASE, timeout=180, capture_output=True, text=True)
            if r1.returncode != 0:
                ai_errors.append('AI理解失败')

            if use_remote_bg:
                remote_db.set_status(
                    item_id=sid,
                    action='starred',
                    force=True,
                    user_id=str(bg_user_id) if bg_user_id else None,
                    can_access_all=False,
                )
                row = remote_db.get_feed_item(
                    item_id=sid,
                    public_only=False,
                    can_access_all=True,
                    user_id=str(bg_user_id) if bg_user_id else None,
                    min_github_stars=0,
                )
                task['title'] = row.get('title') if row else surl
            else:
                db.set_status(conn2, sid, 'starred', force=True, user_id=bg_user_id)
                row = conn2.execute("SELECT title FROM items WHERE id=?", (sid,)).fetchone()
                task['title'] = row['title'] if row else surl
            task['status'] = 'done'
            if ai_errors:
                task['error'] = '; '.join(ai_errors)
        except Exception as e:
            task['status'] = 'error'
            task['error'] = str(e)
            print(f"Submit URL background error: {e}")
        finally:
            if conn2:
                try: conn2.close()
                except: pass

    t = threading.Thread(
        target=_bg_submit,
        args=(item_id, url, bool(existing), submit_user_id,
              norm.platform, norm.canonical_url),
        daemon=True,
    )
    t.start()
    return {'ok': True, 'task_id': item_id, 'status': 'fetching', 'platform': norm.platform}


@router.post('/api/submit-url/status')
async def post_submit_url_status(request: Request):
    body = await request.json()
    task_id = body.get('task_id', '')
    task = _submit_tasks.get(task_id)
    if task and not _can_access_submit_task(request, task):
        return JSONResponse({'error': 'task not found'}, status_code=404)
    if not task:
        # BF-0419-19: _submit_tasks 内存态在后端重启时丢失,但 DB 里可能已入库
        # → fallback 查 DB,有 item 就当 done 返回(前端 UI 从"分析中"切完成)
        user_id = _get_user_id(request)
        if remote_db.app_state_to_remote():
            row = remote_db.get_feed_item(
                item_id=task_id,
                public_only=False,
                can_access_all=can_access_all(request),
                user_id=str(user_id) if user_id else None,
                min_github_stars=0,
            )
            if row:
                return {
                    'status': 'done',
                    'url': row.get('url') or '',
                    'title': row.get('title') or '',
                    'error': '',
                    'item': row,
                }
            return JSONResponse({'error': 'task not found'}, status_code=404)
        conn = db.get_conn()
        join_sql, join_params = _status_join_sql(conn, user_id)
        row = conn.execute(
            f"SELECT i.*, s.starred_at FROM items i {join_sql} WHERE i.id=?",
            join_params + [task_id]
        ).fetchone()
        conn.close()
        if row and _can_access_manual_row(request, row):
            return {
                'status': 'done',
                'url': row['url'] or '',
                'title': row['title'] or '',
                'error': '',
                'item': db.strip_blob_columns(dict(row)),
            }
        return JSONResponse({'error': 'task not found'}, status_code=404)
    resp = dict(task)
    if task['status'] == 'done':
        user_id = _get_user_id(request)
        if remote_db.app_state_to_remote():
            row = remote_db.get_feed_item(
                item_id=task_id,
                public_only=False,
                can_access_all=can_access_all(request),
                user_id=str(user_id) if user_id else None,
                min_github_stars=0,
            )
            resp['item'] = row
            del _submit_tasks[task_id]
            return resp
        conn = db.get_conn()
        join_sql, join_params = _status_join_sql(conn, user_id)
        row = conn.execute(f"SELECT i.*, s.starred_at FROM items i {join_sql} WHERE i.id=?", join_params + [task_id]).fetchone()
        conn.close()
        resp['item'] = db.strip_blob_columns(dict(row)) if row and _can_access_manual_row(request, row) else None
        del _submit_tasks[task_id]
    elif task['status'] == 'error':
        del _submit_tasks[task_id]
    return resp


@router.get('/api/submit-history')
def get_submit_history(request: Request):
    user_id = _get_user_id(request)
    if remote_db.app_state_to_remote():
        body = remote_db.query_feed(
            platform='manual',
            limit=50,
            offset=0,
            user_id=str(user_id) if user_id else None,
            public_only=False,
            manual_owner_user_id=None if can_access_all(request) else (str(user_id) if user_id else None),
            min_github_stars=0,
        )
        return {'ok': True, 'items': body.get('items', [])}
    conn = db.get_conn()
    join_sql, join_params = _status_join_sql(conn, user_id)
    where = "i.platform='manual'"
    params = list(join_params)
    if not can_access_all(request):
        where += " AND i.user_id = ?"
        params.append(user_id)
    rows = conn.execute(
        f"SELECT i.*, s.starred_at FROM items i "
        f"{join_sql} "
        f"WHERE {where} ORDER BY i.fetched_at DESC LIMIT 50",
        params,
    ).fetchall()
    conn.close()
    return {'ok': True, 'items': [db.strip_blob_columns(dict(r)) for r in rows]}
