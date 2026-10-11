"""AI search HTTP endpoints and durable, database-claimed background jobs."""
import asyncio
import hashlib
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.concurrency import run_in_threadpool

import ai_search_digest as digest
from ai_provider_guard import ProviderCooldown
import remote_db
from authz import can_access_all, current_user_id
from env_utils import load_project_env
from remote_db import ai_search as search
from routes.admin import _require_admin
from routes.clusters import _github_cluster_display_min_stars, _is_anonymous_public_request

BASE = Path(__file__).resolve().parents[2]
router = APIRouter()
# Also installed as app.state.limiter: one registry for defaults and these limits.
# RATELIMIT_DEFAULT 允许运维调全局默认限额而不发版。
#
# 默认 600/minute(10 rps/IP)是"防洪水"底线而非功能性限流:真实用户(哪怕图多的
# 首屏突发 + 状态轮询)都远够不到,而失控的重试循环 / 朴素爬虫会被挡下。刻意放宽
# 以避免运营商级 NAT(多用户共享出口 IP)下的误伤。要收紧到 per-route 严格限额,
# 单独给昂贵端点(search/submit-url/export/generate)加 @limiter.limit 并配
# RATELIMIT_STORAGE_URL 走 Redis(多 worker 共享计数)。见 docs 加固 backlog。
limiter = Limiter(key_func=get_remote_address,
                  default_limits=[(os.environ.get('RATELIMIT_DEFAULT') or '600/minute').strip()])
_AI_SEARCH_BG_TASKS = set()
_AI_SEARCH_EXECUTOR = False
# Keep this boundary fixed even if the first recovery needs to be retried.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def settings():
    env = {**load_project_env(BASE), **os.environ}
    def integer(key, default):
        try:
            return int(env.get(key, default))
        except (ValueError, TypeError):
            return default
    def number(key, default):
        try:
            return float(env.get(key, default))
        except (ValueError, TypeError):
            return default
    def flag(key, default):
        value = str(env.get(key) or '').strip().lower()
        return default if not value else value not in ('0', 'false', 'off', 'no')
    return {
        'enabled': flag('AI_SEARCH_ENABLED', True) and bool(str(env.get('GLM_CODING_API_KEY') or '').strip()),
        'worker_enabled': flag('AI_SEARCH_WORKER_ENABLED', False),
        'window_days': max(1, integer('AI_SEARCH_WINDOW_DAYS', 90)),
        'sim_min': number('AI_SEARCH_SIM_MIN', .45),
        'quality_min': number('AI_SEARCH_QUALITY_MIN', None),
        'max_items': max(1, integer('AI_SEARCH_MAX_ITEMS', 100)),
        'user_day': None if str(env.get('AI_SEARCH_USER_DAY', '2')).strip() == '' else max(0, integer('AI_SEARCH_USER_DAY', 2)),
        'concurrency': max(1, integer('AI_SEARCH_CONCURRENCY', 2)),
        'cap_day': None if str(env.get('AI_SEARCH_CAP_DAY', '5')).strip() == '' else max(0, integer('AI_SEARCH_CAP_DAY', 5)),
        'hard_timeout': max(integer('AI_SEARCH_HARD_TIMEOUT_MIN', 45),
                            math.ceil(digest.settings()['AI_SEARCH_JOB_TIMEOUT_S'] / 60) + 5),
        'ip_salt': env.get('AI_SEARCH_IP_SALT') or env.get('JWT_SECRET') or 'info2action-ai-search',
    }


def enabled():
    return settings()['enabled']


def _error(error, status=400, **extra):
    return JSONResponse({'error': error, **extra}, status_code=status)


def disabled_response():
    return _error('ai_search_disabled', 404)


def _visibility(request):
    return dict(public_only=_is_anonymous_public_request(request), user_id=current_user_id(request),
                manual_owner_user_id=None if can_access_all(request) else current_user_id(request),
                min_github_stars=_github_cluster_display_min_stars())


def _ip_hash(request, config):
    client_ip = ((request.headers.get('cf-connecting-ip') or '').strip()
                 or (request.headers.get('x-forwarded-for') or '').split(',')[0].strip())
    if not client_ip:
        client_ip = request.client.host if request.client else ''
    return hashlib.sha256((config['ip_salt'] + client_ip).encode()).hexdigest()[:16]


async def _quota_limit(request, config):
    if not can_access_all(request) and await run_in_threadpool(
            search.quota_exhausted, current_user_id(request), config['user_day'], config['cap_day']):
        return _error('quota_exhausted', 429)
    return None


async def _create_job(data, ip_hash, config, *, user_id, quota_exempt):
    fields = {key: data[key] for key in ('query', 'query_lexical', 'candidate_ids', 'candidate_meta', 'window_days')}
    fields.update({key: data.get(key, False if key == 'vector_ok' else 0)
                   for key in ('lexical_hits', 'vector_hits', 'vector_ok')})
    try:
        jid = await run_in_threadpool(search.create_job, **fields, ip_hash=ip_hash, user_id=user_id,
                                     cap_day=config['cap_day'], user_day=config['user_day'],
                                     quota_exempt=quota_exempt)
    except search.QuotaExceeded:
        return _error('quota_exhausted', 429)
    return jid


def _public_error(error: str | None) -> str | None:
    if not error:
        return None
    if error in {'parse_error', 'hard_timeout', 'candidates_unavailable', 'quota'}:
        return error
    lowered = error.lower()
    if 'timed out' in lowered or 'timeout' in lowered:
        return 'timeout'
    return 'internal'


async def _summary(job):
    status = job['status']
    summary = {'status': status}
    if status == 'queued':
        summary['position'] = await run_in_threadpool(search.queue_position, job['id'])
    if status == 'unavailable':
        summary['reason'] = _public_error(job.get('error'))
    return summary


async def _job_items(job, request):
    ids = job.get('candidate_ids') or []
    items = await run_in_threadpool(search.get_candidates, ids, **_visibility(request))
    # Summaries can quote private candidates. Do not expose a saved summary to
    # a caller who cannot read every source, even if they know the job UUID.
    if {item['id'] for item in items} != set(ids):
        return None
    meta = {item['id']: item for item in job.get('candidate_meta') or []}
    by_id = {item['id']: item for item in items}
    cited = list(dict.fromkeys(i for i in job.get('cited_ids') or [] if i in by_id))
    cited_set = set(cited)
    ordered_ids = cited + [i for i in ids if i not in cited_set]
    return [{**by_id[i], **{k: v for k, v in meta.get(i, {}).items() if k in ('sim', 'legs', 'hs10')}} for i in ordered_ids]


async def _job_response(job, items):
    state = await _summary(job)
    cited = job.get('cited_ids') or []
    return {'job_id': job['id'], 'query': job.get('query'), **state,
            'content_md': job.get('content_md_final') if job['status'] == 'done' else None,
            'cited_ids': cited, 'cited_count': len(cited), 'items': items,
            'usage': job.get('usage'), 'elapsed_ms': job.get('elapsed_ms'),
            'model': job.get('model'), 'error': _public_error(job.get('error'))}


@router.post('/api/ai-search')
@limiter.limit('10/minute')
async def post_search(request: Request):
    config = settings()
    if not config['enabled']:
        return _error('ai_search_disabled', 404)
    user_id = current_user_id(request)
    if user_id is None and not getattr(request.state, 'legacy_authenticated', False):
        return _error('login_required', 401)
    try:
        body = await request.json()
    except ValueError:
        return _error('invalid_query')
    q = body.get('q') if isinstance(body, dict) else None
    if not isinstance(q, str) or not 1 <= len(q.strip()) <= 200:
        return _error('invalid_query')
    q = q.strip()
    ip_hash = _ip_hash(request, config)
    error = await _quota_limit(request, config)
    if error is not None:
        return error
    result = await run_in_threadpool(search.retrieve, q, **{
        key: config[key] for key in ('window_days', 'sim_min', 'quality_min', 'max_items')}, **_visibility(request))
    response = {'job_id': None, 'query': q, 'window_days': config['window_days'], **result, 'summary': {'status': 'none'}}
    if not result['items']:
        return response
    data = {**response, 'candidate_ids': [item['id'] for item in result['items']],
            'candidate_meta': [{key: item.get(key) for key in ('id', 'sim', 'legs', 'hs10')} for item in result['items']]}
    jid = await _create_job(data, ip_hash, config, user_id=user_id, quota_exempt=can_access_all(request))
    if isinstance(jid, JSONResponse):
        return jid
    # Creation has committed and consumed quota. Subsequent transient failures
    # must still give the caller its durable ID; the sweeper can resume work.
    response.update(job_id=jid, summary={'status': 'queued'})
    try:
        if _is_executor():
            await _kick_next()
            job = await run_in_threadpool(search.get_job, jid)
            if job is not None:
                response['summary'] = await _summary(job)
        else:
            response['summary']['position'] = await run_in_threadpool(search.queue_position, jid)
    except Exception as exc:
        print(f'[warn] ai_search job {jid}: post-create followup failed: {exc}')
        response['summary'] = {'status': 'queued'}
    return response


@router.get('/api/ai-search/status')
async def get_status(request: Request):
    config = settings()
    logged_in = current_user_id(request) is not None or bool(getattr(request.state, 'legacy_authenticated', False))
    reason = None
    if not config['enabled']:
        reason = 'disabled'
    elif not logged_in:
        reason = 'login_required'
    elif await _quota_limit(request, config) is not None:
        reason = 'quota_exhausted'
    return {'enabled': config['enabled'], 'logged_in': logged_in, 'can_search': reason is None,
            'reason': reason, 'user_day_limit': config['user_day']}


@router.get('/api/ai-search/history')
async def get_history(request: Request, limit: int = 10):
    if not enabled():
        return _error('ai_search_disabled', 404)
    user_id = current_user_id(request)
    if user_id is None:
        return {'items': [], 'anonymous': True}
    limit = max(1, min(50, limit))
    jobs = await run_in_threadpool(search.list_user_jobs, user_id, limit)
    return {'items': [{'job_id': str(job['id']), 'query': job['query'],
                       'created_at': job['created_at'], 'status': job['status']} for job in jobs],
            'anonymous': False, 'total': await run_in_threadpool(search.count_user_jobs, user_id)}


@router.get('/api/ai-search/{job_id}')
async def get_search(request: Request, job_id: str):
    if not enabled():
        return _error('ai_search_disabled', 404)
    job = await run_in_threadpool(search.get_job, job_id)
    if job is None:
        return _error('job_not_found', 404)
    items = await _job_items(job, request)
    if items is None:
        return _error('job_not_found', 404)
    return await _job_response(job, items)


@router.get('/api/admin/ai-search/jobs')
async def admin_jobs(request: Request, limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0), status: str = ''):
    _, error = _require_admin(request)
    if error is not None:
        return _error('admin_required', 403)
    if not remote_db.app_state_to_remote():
        return JSONResponse({'reason': 'remote_required'}, status_code=501)
    if not enabled():
        return _error('ai_search_disabled', 404)
    config, model = settings(), digest.settings()
    return {'jobs': await run_in_threadpool(search.list_jobs, limit, offset, status or None),
            'total': await run_in_threadpool(search.count_jobs, status or None), 'limit': limit, 'offset': offset,
            'quota': {**{k: config[k] for k in ('cap_day', 'user_day', 'concurrency')},
                      'used_day': await run_in_threadpool(search.count_all_day),
                      'used_month': await run_in_threadpool(search.count_all_month)},
            'params': {'model': model['AI_SEARCH_MODEL'], 'max_tokens': model['AI_SEARCH_MAX_TOKENS'],
                       'reasoning_effort': model['AI_SEARCH_REASONING_EFFORT'],
                       **{k: config[k] for k in ('sim_min', 'quality_min', 'window_days')}}}


@router.get('/api/admin/ai-search/jobs/{job_id}')
async def admin_job(request: Request, job_id: str):
    _, error = _require_admin(request)
    if error is not None:
        return _error('admin_required', 403)
    if not remote_db.app_state_to_remote():
        return JSONResponse({'reason': 'remote_required'}, status_code=501)
    if not enabled():
        return _error('ai_search_disabled', 404)
    job = await run_in_threadpool(search.get_job, job_id)
    return job if job is not None else _error('job_not_found', 404)


def _spawn_job(job_id):
    task = asyncio.create_task(_run_job(job_id))
    _AI_SEARCH_BG_TASKS.add(task)
    task.add_done_callback(_AI_SEARCH_BG_TASKS.discard)


def _is_executor():
    return _AI_SEARCH_EXECUTOR


async def _kick_next():
    if not _is_executor():
        return
    config = settings()
    # Each DB claim rechecks the shared running count, across all workers.
    for _ in range(config['concurrency']):
        if not _is_executor():
            break
        jid = await run_in_threadpool(search.claim_next_queued, config['concurrency'])
        if jid is None:
            break
        _spawn_job(jid)


def _with_tiers(job, tiers):
    meta = {item['id']: item for item in job.get('candidate_meta') or []}
    return [{**meta.get(i, {'id': i}), 'tier': tiers.get(i)} for i in job['candidate_ids']]


def _prepare_input(job, candidates, config):
    assembled = digest.build_input(job['query'], datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(), candidates)
    version, _ = digest.load_prompt(config['AI_SEARCH_PROMPT_PATH'])
    search.save_job_input(job['id'], input_text=assembled['user_message'],
                          candidate_meta=_with_tiers(job, assembled['tiers']),
                          model=config['AI_SEARCH_MODEL'], prompt_version=version,
                          request_params={'model': config['AI_SEARCH_MODEL'],
                                          'max_tokens': config['AI_SEARCH_MAX_TOKENS'],
                                          'reasoning_effort': config['AI_SEARCH_REASONING_EFFORT']})


async def _run_job(job_id):
    result = None
    try:
        job = await run_in_threadpool(search.get_job, job_id)
        if not job or job['status'] != 'running':
            return
        candidates = await run_in_threadpool(search.get_candidates, job['candidate_ids'],
                                            full_text=True, public_only=False, min_github_stars=0)
        if [c['id'] for c in candidates] != job['candidate_ids']:
            raise digest.DigestError('candidates_unavailable')
        config = digest.settings()
        await run_in_threadpool(_prepare_input, job, candidates, config)
        result = await run_in_threadpool(digest.run_digest, job, candidates, settings=config)
        retry_delays = (1, 3, 9)
        for attempt in range(len(retry_delays) + 1):
            try:
                await run_in_threadpool(search.finish_job, job_id,
                                        **{k: result[k] for k in ('input_text', 'result', 'content_md_final', 'cited_ids',
                                           'usage', 'elapsed_ms', 'model', 'prompt_version', 'request_params')},
                                        candidate_meta=_with_tiers(job, result['tiers']),
                                        points_estimate=search.points_estimate(result['usage']))
                break
            except Exception:
                if attempt == len(retry_delays):
                    raise
                await asyncio.sleep(retry_delays[attempt])
    except asyncio.CancelledError:
        print(f'[warn] ai_search job {job_id}: cancelled; retained for startup recovery')
        raise
    except Exception as exc:
        print(f'[warn] ai_search job {job_id}: {exc}')
        quota = isinstance(exc, (digest.QuotaExhausted, ProviderCooldown)) or (
            isinstance(exc, HTTPError) and exc.code == 429)
        fallback = None
        if result is not None:
            fallback = {**result['result'], 'content_md_final': result['content_md_final'], 'raw': result['raw']}
        elif quota:
            fallback = {'error_detail': str(exc)[:500]}
        error = 'quota' if quota else 'parse_error' if isinstance(exc, digest.ParseError) else str(exc)[:500]
        status = 'unavailable' if quota else 'failed'
        raw = exc.raw if isinstance(exc, digest.ParseError) else None
        try:
            await run_in_threadpool(search.fail_job, job_id,
                                    error=error, status=status, raw=raw, result=fallback)
        except Exception as failure:
            print(f'[warn] ai_search job {job_id}: failed to persist failure: {failure}')
            if fallback is not None or raw is not None:
                # Content may be the cause of both failed writes. Make one last
                # attempt to persist the terminal state without that content.
                try:
                    await run_in_threadpool(search.fail_job, job_id,
                                            error=error, status=status, raw=None, result=None)
                except Exception as minimal_failure:
                    print(f'[warn] ai_search job {job_id}: failed to persist terminal state: {minimal_failure}')
    finally:
        # Cancellation must not enqueue more work while the process is closing.
        if not asyncio.current_task().cancelling():
            try:
                await _kick_next()
            except Exception as exc:
                print(f'[warn] ai_search job {job_id}: queue drain failed: {exc}')


async def _sweep():
    startup = True
    next_recovery = 0.
    while True:
        try:
            config = settings()
            if time.monotonic() >= next_recovery:
                await run_in_threadpool(search.recover_stale, config['hard_timeout'],
                                        requeue_before=_PROCESS_STARTED_AT if startup else None)
                startup = False
                next_recovery = time.monotonic() + 60
            await _kick_next()
        except Exception as exc:
            print(f'[warn] ai_search job sweeper: {exc}')
        await asyncio.sleep(5)


def start_ai_search_sweeper(*, is_leader=False):
    global _AI_SEARCH_EXECUTOR
    config = settings()
    _AI_SEARCH_EXECUTOR = bool(is_leader and config['worker_enabled'] and config['enabled'])
    if is_leader and config['enabled'] and not config['worker_enabled']:
        print('[warn] AI search is enabled but its worker is disabled: jobs will remain queued. '
              'Set AI_SEARCH_WORKER_ENABLED=1 in production .env and restart.', flush=True)
    return asyncio.create_task(_sweep()) if _AI_SEARCH_EXECUTOR else None


def stop_ai_search_sweeper():
    """Disable new claims before cancelling the sweeper and waiting for shutdown."""
    global _AI_SEARCH_EXECUTOR
    _AI_SEARCH_EXECUTOR = False
