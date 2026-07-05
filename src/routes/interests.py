"""Interests API: CRUD + scan + keyword generation."""

import threading

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

import db
import interest_engine
import remote_db
from authz import current_user_id, owner_scope_user_id, require_admin

router = APIRouter()


@router.get('/api/interests')
def list_interests(request: Request):
    if remote_db.app_state_to_remote():
        interests = remote_db.list_interests_remote(user_id=owner_scope_user_id(request))
        for interest in interests:
            stats = remote_db.get_interest_match_stats_remote(interest['id'])
            interest['match_count'] = stats['total']
            interest['new_count'] = stats['new_count']
        return {'interests': interests}

    conn = db.get_conn()
    interests = db.list_interests(conn, user_id=owner_scope_user_id(request))
    for interest in interests:
        stats = db.get_interest_match_stats(conn, interest['id'])
        interest['match_count'] = stats['total']
        interest['new_count'] = stats['new_count']
    conn.close()
    return {'interests': interests}


@router.get('/api/interests/{interest_id}/matches')
def get_interest_matches(request: Request, interest_id: int, limit: int = Query(30), offset: int = Query(0)):
    if remote_db.app_state_to_remote():
        interest = remote_db.get_interest_remote(interest_id, user_id=owner_scope_user_id(request))
        if not interest:
            return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
        sort = interest.get('sort', 'relevance')
        matches = remote_db.get_interest_matches_remote(interest_id, sort, limit, offset)
        remote_db.mark_interest_matches_read_remote(interest_id)
        return {'matches': matches, 'interest': interest}

    conn = db.get_conn()
    interest = db.get_interest(conn, interest_id, user_id=owner_scope_user_id(request))
    if not interest:
        conn.close()
        return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
    sort = interest.get('sort', 'relevance')
    matches = db.get_interest_matches(conn, interest_id, sort, limit, offset)
    db.mark_interest_matches_read(conn, interest_id)
    conn.close()
    return {'matches': matches, 'interest': interest}


@router.post('/api/interests')
async def create_interest(request: Request):
    body = await request.json()
    name = body.get('name', '').strip()
    if not name:
        return JSONResponse({'error': '名称不能为空'}, status_code=400)
    if remote_db.app_state_to_remote():
        try:
            interest_id = remote_db.create_interest_remote(
                name=name,
                description=body.get('description'),
                keywords=body.get('keywords', []),
                sort=body.get('sort', 'relevance'),
                item_limit=body.get('item_limit', 30),
                scope=body.get('scope', 'all'),
                user_id=current_user_id(request),
            )
            interest = remote_db.get_interest_remote(interest_id)
            return {'ok': True, 'interest': interest}
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        interest_id = db.create_interest(
            conn, name,
            description=body.get('description'),
            keywords=body.get('keywords', []),
            sort=body.get('sort', 'relevance'),
            item_limit=body.get('item_limit', 30),
            scope=body.get('scope', 'all'),
            user_id=current_user_id(request),
        )
        interest = db.get_interest(conn, interest_id)
        return {'ok': True, 'interest': interest}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


@router.post('/api/interests/generate-keywords')
async def generate_keywords(request: Request):
    body = await request.json()
    description = body.get('description', '').strip()
    if not description:
        return JSONResponse({'error': '描述不能为空'}, status_code=400)
    try:
        keywords = interest_engine.generate_keywords(description)
        return {'ok': True, 'keywords': keywords}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/interests/{interest_id}')
async def update_interest(interest_id: int, request: Request):
    body = await request.json()
    scope_user_id = owner_scope_user_id(request)
    if body.get('_method') == 'DELETE':
        if remote_db.app_state_to_remote():
            ok = remote_db.delete_interest_remote(interest_id, owner_user_id=scope_user_id)
            if not ok:
                return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
            return {'ok': ok}

        conn = db.get_conn()
        try:
            ok = db.delete_interest(conn, interest_id, owner_user_id=scope_user_id)
            if not ok:
                return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
            return {'ok': ok}
        finally:
            conn.close()
    # PATCH: update interest config
    if remote_db.app_state_to_remote():
        try:
            ok = remote_db.update_interest_remote(
                interest_id,
                owner_user_id=scope_user_id,
                name=body.get('name'),
                description=body.get('description'),
                keywords=body.get('keywords'),
                sort=body.get('sort'),
                item_limit=body.get('item_limit'),
                scope=body.get('scope'),
                enabled=body.get('enabled'),
            )
            if not ok:
                return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
            interest = remote_db.get_interest_remote(interest_id, user_id=scope_user_id)
            return {'ok': ok, 'interest': interest}
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        ok = db.update_interest(conn, interest_id,
            owner_user_id=scope_user_id,
            name=body.get('name'),
            description=body.get('description'),
            keywords=body.get('keywords'),
            sort=body.get('sort'),
            item_limit=body.get('item_limit'),
            scope=body.get('scope'),
            enabled=body.get('enabled'))
        if not ok:
            return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
        interest = db.get_interest(conn, interest_id, user_id=scope_user_id)
        return {'ok': ok, 'interest': interest}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


@router.delete('/api/interests/{interest_id}')
def delete_interest(interest_id: int, request: Request):
    if remote_db.app_state_to_remote():
        ok = remote_db.delete_interest_remote(interest_id, owner_user_id=owner_scope_user_id(request))
        if not ok:
            return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
        return {'ok': ok}

    conn = db.get_conn()
    try:
        ok = db.delete_interest(conn, interest_id, owner_user_id=owner_scope_user_id(request))
        if not ok:
            return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
        return {'ok': ok}
    finally:
        conn.close()


@router.post('/api/interests/{interest_id}/scan')
def scan_interest(interest_id: int, request: Request):
    err = require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        interest = remote_db.get_interest_remote(interest_id)
    else:
        conn = db.get_conn()
        interest = db.get_interest(conn, interest_id)
        conn.close()
    if not interest:
        return JSONResponse({'error': '兴趣配置不存在'}, status_code=404)
    if interest.get('scan_status') == 'scanning':
        return {'ok': False, 'msg': '扫描已在进行中'}

    def _bg_scan():
        try:
            interest_engine.scan_interest(interest_id)
        except Exception as e:
            print(f"兴趣扫描错误: {e}")
            if remote_db.app_state_to_remote():
                remote_db.update_interest_remote(interest_id, scan_status='done')
            else:
                c = db.get_conn()
                db.update_interest(c, interest_id, scan_status='done')
                c.close()
    threading.Thread(target=_bg_scan, daemon=True).start()
    return {'ok': True, 'msg': '扫描已启动'}
