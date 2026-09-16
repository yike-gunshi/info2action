"""Fetch runner endpoints: trigger and monitor data fetching."""

import copy
import json
import os
import re
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

import db
import remote_db
import ai_provider_guard
from authz import require_admin
from deps import BASE

router = APIRouter()


# ── 后台编排段（exec 组合，勿直接 import；说明见 src/fetch_orchestrator.py 顶部）──
from pathlib import Path as _SegPath


def _load_orchestrator_segment() -> None:
    _path = _SegPath(__file__).resolve().parent.parent / 'fetch_orchestrator.py'
    _code = compile(_path.read_text(encoding='utf-8'), str(_path), 'exec')
    exec(_code, globals())


_load_orchestrator_segment()

# ── Routes ──────────────────────────────────────────────────

@router.get('/api/fetch/status')
def get_fetch_status():
    remote_status_degraded = False
    remote_status_error = None
    if remote_db.remote_authority_enabled() or remote_db.status_write_to_remote():
        try:
            now = time.monotonic()
            cached = _remote_last_fetch_cache.get('data')
            with _fetch_lock:
                has_local_active = bool(_fetch_active_runs)
            if _env_enabled('INFO2ACTION_FETCH_STATUS_LIVE_DISABLED', default=False):
                last = copy.deepcopy(cached)
                remote_status_degraded = True
                remote_status_error = 'fetch_status_live_disabled'
            elif cached is not None and not has_local_active and now - float(_remote_last_fetch_cache.get('ts') or 0) < _REMOTE_LAST_FETCH_TTL_SEC:
                last = copy.deepcopy(cached)
            else:
                last = remote_db.get_last_fetch_remote()
                _remote_last_fetch_cache['ts'] = now
                _remote_last_fetch_cache['data'] = copy.deepcopy(last)
        except remote_db.RemoteDBError as exc:
            cached = _remote_last_fetch_cache.get('data')
            if cached is None:
                return JSONResponse({'error': str(exc), 'data_backend': remote_db.status_backend()}, status_code=503)
            last = copy.deepcopy(cached)
            remote_status_degraded = True
            remote_status_error = str(exc)
    else:
        conn = db.get_conn()
        last = db.get_last_fetch(conn)
        conn.close()
    with _fetch_lock:
        running_count = len(_fetch_active_runs)
        running = running_count > 0
        finished_at = _fetch_finished_at
        active_snapshot = [
            {
                'id': run_id,
                'source': active.get('source'),
                'started_at': active.get('started_at'),
                'progress': copy.deepcopy(active.get('progress') or {}),
            }
            for run_id, active in sorted(
                _fetch_active_runs.items(),
                key=lambda item: (item[1].get('started_at') or '', item[0]),
            )
        ]
        if active_snapshot:
            progress = copy.deepcopy(active_snapshot[-1]['progress'])
        else:
            progress = copy.deepcopy(_fetch_progress) if _fetch_progress else None
        max_concurrent = _max_global_fetch_pipelines()
    if running and progress:
        progress = _decorate_progress_from_log(progress)
    if progress:
        progress.pop('_fetch_log_path', None)
        progress.pop('_ai_log_path', None)
    active_runs = []
    for active in active_snapshot:
        active_progress = active.get('progress') or {}
        active_stage = None
        for stage in active_progress.get('stages', []):
            if stage.get('status') == 'running':
                active_stage = stage
                break
        active_runs.append({
            'id': active.get('id'),
            'source': active.get('source'),
            'started_at': active.get('started_at'),
            'stage': (active_stage or {}).get('id') or (active_stage or {}).get('name'),
            'percent': active_progress.get('percent'),
            'result_status': active_progress.get('result_status'),
        })
    result = {
        'running': running,
        'running_count': running_count,
        'max_concurrent': max_concurrent,
        'active_runs': active_runs,
        'last_run': last,
        'finished_at': finished_at,
    }
    if remote_status_degraded:
        result['remote_status_degraded'] = True
        result['remote_status_error'] = remote_status_error
    if progress:
        result['progress'] = progress
    return result


@router.get('/api/admin/fetch/stale-platforms')
def admin_stale_platforms(request: Request, runs: int = Query(3, ge=1, le=20)):
    """LOG-1: 找出连续 N 个成功抓取里都 0 条的平台（reddit 21h 静默类问题）。

    需要 _platform_new_counts 已经写进 stats_json（finish_fetch_run_remote 之后）。
    历史 fetch_runs 没这个字段会被算作"没数据"——只看最新 N 个成功 run。
    """
    err = require_admin(request)
    if err:
        return err

    if remote_db.fetch_write_to_remote():
        schema = remote_db.remote_schema()
        with remote_db.connect() as conn:
            rows = conn.execute(
                f"""SELECT id, started_at, finished_at, stats_json
                      FROM {schema}.fetch_runs
                     WHERE status = 'done'
                     ORDER BY id DESC
                     LIMIT %(n)s""",
                {'n': runs},
            ).fetchall()
    else:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, stats_json FROM fetch_runs "
                "WHERE status = 'done' ORDER BY id DESC LIMIT ?",
                (runs,),
            ).fetchall()
        finally:
            conn.close()

    examined = []
    platforms_seen: set[str] = set()
    zero_counts: dict[str, int] = {}
    runs_with_data = 0
    for row in rows:
        rid = row['id'] if isinstance(row, dict) else row[0]
        started_at = row['started_at'] if isinstance(row, dict) else row[1]
        finished_at = row['finished_at'] if isinstance(row, dict) else row[2]
        stats_raw = row['stats_json'] if isinstance(row, dict) else row[3]
        if isinstance(stats_raw, str):
            try:
                stats = json.loads(stats_raw)
            except (json.JSONDecodeError, TypeError):
                stats = {}
        else:
            stats = stats_raw or {}
        if '_platform_new_counts' not in stats:
            examined.append({
                'run_id': rid,
                'started_at': str(started_at) if started_at else None,
                'finished_at': str(finished_at) if finished_at else None,
                'platform_new_counts': None,
                'skipped_reason': 'missing',
            })
            continue
        per_plat = stats.get('_platform_new_counts') or {}
        if not isinstance(per_plat, dict) or per_plat.get('_error'):
            examined.append({
                'run_id': rid,
                'started_at': str(started_at) if started_at else None,
                'finished_at': str(finished_at) if finished_at else None,
                'platform_new_counts': None,
                'skipped_reason': per_plat.get('_error') if isinstance(per_plat, dict) else 'invalid_type',
            })
            continue
        runs_with_data += 1
        clean = {k: int(v) for k, v in per_plat.items() if not k.startswith('_')}
        examined.append({
            'run_id': rid,
            'started_at': str(started_at) if started_at else None,
            'finished_at': str(finished_at) if finished_at else None,
            'platform_new_counts': clean,
        })
        for plat, cnt in clean.items():
            platforms_seen.add(plat)
            if cnt == 0:
                zero_counts[plat] = zero_counts.get(plat, 0) + 1

    stale = sorted(
        plat for plat, zeros in zero_counts.items() if zeros >= runs_with_data and runs_with_data > 0
    )
    return {
        'threshold_runs': runs,
        'runs_examined': len(examined),
        'runs_with_data': runs_with_data,
        'platforms_seen': sorted(platforms_seen),
        'stale_platforms': stale,
        'runs': examined,
    }


@router.post('/api/fetch')
async def post_fetch(request: Request):
    err = require_admin(request)
    if err:
        return err
    return start_global_fetch('api')


@router.post('/api/fetch/quick')
async def post_fetch_quick(request: Request):
    err = require_admin(request)
    if err:
        return err
    global _fetch_running
    body = await request.json()
    platform = body.get('platform', '')
    source = body.get('source', '')
    mode = body.get('mode', '')
    topic_name = body.get('topic', '')
    category_id = body.get('category_id', '')

    if mode == 'category' and category_id:
        clf = load_json(os.path.join(BASE, 'config', 'classification.json')) or {}
        cat = None
        for c in clf.get('categories', []):
            if c.get('id') == category_id:
                cat = c
                break
        if not cat:
            return JSONResponse({'error': f'Category not found: {category_id}'}, status_code=400)

        with _fetch_lock:
            if _fetch_running:
                return {'ok': False, 'msg': 'Fetch already running'}
            _fetch_running = True

        def _run_category_fetch():
            global _fetch_running, _fetch_finished_at, _fetch_progress
            try:
                _fetch_progress = {
                    'stages': [
                        {'name': '抓取数据', 'status': 'running'},
                        {'name': '入库处理', 'status': 'pending'},
                    ],
                    'current_stage': 0, 'total_new': 0
                }
                sq = cat.get('search_queries', {})
                xhs_queries = sq.get('xiaohongshu', [])
                xhs_enabled = _is_platform_enabled('xiaohongshu')
                errors = []
                try:
                    _run_registry_x_fetch()
                except Exception as e:
                    errors.append(f"X registry fetch: {e}")
                if xhs_enabled:
                    for q in xhs_queries[:3]:
                        safe = q.replace(' ', '_')[:50]
                        try:
                            _run_to_file([CLI['xhs'], 'search', q, '--sort', 'latest', '--json'],
                                os.path.join(BASE, 'data', 'sources', 'xiaohongshu', f'search-{safe}.json'))
                        except Exception as e:
                            errors.append(f"XHS search: {e}")
                    try:
                        _run_to_file([CLI['xhs'], 'feed', '--json'],
                            os.path.join(BASE, 'data', 'sources', 'xiaohongshu', 'feed.json'))
                    except Exception as e:
                        errors.append(f"XHS feed: {e}")
                _fetch_progress['stages'][0]['status'] = 'done'
                _fetch_progress['stages'][1]['status'] = 'running'
                _fetch_progress['current_stage'] = 1
                count_before = _count_items_current_backend()
                subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'ingest.py')],
                    cwd=BASE, timeout=120, stderr=subprocess.DEVNULL)
                count_after = _count_items_current_backend()
                new_count = max(0, count_after - count_before)
                _fetch_progress['stages'][1]['status'] = 'done'
                _fetch_progress['stages'][1]['new_count'] = new_count
                _fetch_progress['total_new'] = new_count
                if errors:
                    print(f"Category fetch warnings: {'; '.join(errors)}")
                print(f"Category fetch complete: {cat.get('name', category_id)}, new items: {new_count} — summaries deferred to cron")
            except Exception as e:
                print(f"Category fetch error: {e}")
                _update_health_status('twitter', 'error', f'分类抓取失败: {str(e)[:100]}')
                _update_health_status('xiaohongshu', 'error', f'分类抓取失败: {str(e)[:100]}')
            finally:
                with _fetch_lock:
                    _fetch_running = False
                    _fetch_finished_at = datetime.now(timezone.utc).isoformat()

        try:
            t = threading.Thread(target=_run_category_fetch, daemon=True)
            t.start()
        except Exception:
            with _fetch_lock:
                _fetch_running = False
        return {'ok': True, 'msg': f'Category fetch: {cat.get("name", category_id)}'}

    if mode == 'topic' and topic_name:
        with _fetch_lock:
            if _fetch_running:
                return {'ok': False, 'msg': 'Fetch already running'}
            _fetch_running = True
        try:
            t = threading.Thread(target=_run_topic_fetch, args=(topic_name,), daemon=True)
            t.start()
        except Exception:
            with _fetch_lock:
                _fetch_running = False
        return {'ok': True, 'msg': f'Topic fetch: {topic_name}'}

    elif mode == 'recommend':
        with _fetch_lock:
            if _fetch_running:
                return {'ok': False, 'msg': 'Fetch already running'}
            _fetch_running = True
        try:
            t = threading.Thread(target=_run_recommend_fetch, daemon=True)
            t.start()
        except Exception:
            with _fetch_lock:
                _fetch_running = False
        return {'ok': True, 'msg': 'Quick fetch: recommend feeds'}

    elif mode == 'all':
        with _fetch_lock:
            if _fetch_running:
                return {'ok': False, 'msg': 'Fetch already running'}
            _fetch_running = True
        try:
            t = threading.Thread(target=_run_fetch, daemon=True)
            t.start()
        except Exception:
            with _fetch_lock:
                _fetch_running = False
        return {'ok': True, 'msg': 'Global fetch: all sources'}

    elif platform:
        with _fetch_lock:
            if _fetch_running:
                return {'ok': False, 'msg': 'Fetch already running'}
            _fetch_running = True
        try:
            t = threading.Thread(target=_run_quick_fetch, args=(platform, source), daemon=True)
            t.start()
        except Exception:
            with _fetch_lock:
                _fetch_running = False
        label = f'{platform}/{source}' if source else f'{platform} (all)'
        return {'ok': True, 'msg': f'Quick fetch: {label}'}

    else:
        return JSONResponse({'error': 'platform+source or mode required'}, status_code=400)
