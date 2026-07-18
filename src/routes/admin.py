"""Admin endpoints: invite code management, user listing."""
import functools
import secrets
import string

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
import remote_db
from routes.auth import get_current_user

router = APIRouter()


def _require_admin(request: Request):
    """Check that current user is an admin. Returns user or error response."""
    user = get_current_user(request)
    if not user:
        return None, JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if user.get('role') != 'admin':
        return None, JSONResponse({'error': 'Admin access required'}, status_code=403)
    return user, None


def _generate_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


def _positive_int_from_body(body, name, default, *, max_value=None, max_message=None):
    raw = body.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, JSONResponse({'error': f'{name} must be a positive integer'}, status_code=400)
    if value < 1:
        return None, JSONResponse({'error': f'{name} must be a positive integer'}, status_code=400)
    if max_value is not None and value > max_value:
        message = max_message or f'{name} must be at most {max_value}'
        return None, JSONResponse({'error': message}, status_code=400)
    return value, None


@router.post("/api/admin/invite-codes")
async def create_invite_codes(request: Request):
    user, err = _require_admin(request)
    if err:
        return err

    body = await request.json()
    count, err = _positive_int_from_body(
        body,
        'count',
        1,
        max_value=50,
        max_message='单次最多生成 50 个邀请码',
    )
    if err:
        return err
    max_uses, err = _positive_int_from_body(body, 'max_uses', 1)
    if err:
        return err
    expires_at = body.get('expires_at')  # ISO 8601 or None

    if remote_db.app_state_to_remote():
        codes = []
        for _ in range(count):
            code = _generate_code()
            await run_in_threadpool(
                functools.partial(
                    remote_db.create_invite_code_remote,
                    code,
                    user['id'],
                    max_uses=max_uses,
                    expires_at=expires_at,
                )
            )
            codes.append(code)
        return {'ok': True, 'codes': codes}

    conn = db.get_conn()
    try:
        codes = []
        for _ in range(count):
            code = _generate_code()
            db.create_invite_code(conn, code, user['id'], max_uses=max_uses, expires_at=expires_at)
            codes.append(code)
        return {'ok': True, 'codes': codes}
    finally:
        conn.close()


@router.get("/api/admin/invite-codes")
async def list_invite_codes(request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        return {'codes': await run_in_threadpool(remote_db.list_invite_codes_remote)}

    conn = db.get_conn()
    try:
        codes = db.list_invite_codes(conn)
        return {'codes': codes}
    finally:
        conn.close()


@router.delete("/api/admin/invite-codes/{code}")
async def delete_invite_code(code: str, request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        existing = await run_in_threadpool(remote_db.get_invite_code_remote, code)
        if not existing:
            return JSONResponse({'error': 'Code not found'}, status_code=404)
        await run_in_threadpool(remote_db.delete_invite_code_remote, code)
        return {'ok': True}

    conn = db.get_conn()
    try:
        existing = db.get_invite_code(conn, code)
        if not existing:
            return JSONResponse({'error': 'Code not found'}, status_code=404)
        db.delete_invite_code(conn, code)
        return {'ok': True}
    finally:
        conn.close()


@router.get("/api/admin/users")
async def list_users(request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        return {'users': await run_in_threadpool(remote_db.list_users_remote)}

    conn = db.get_conn()
    try:
        users = db.list_users(conn)
        return {'users': users}
    finally:
        conn.close()


@router.get("/api/admin/overview")
async def admin_overview(request: Request, include_embedding: bool = False):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        return await run_in_threadpool(
            functools.partial(
                remote_db.admin_overview_remote,
                fetch_run_limit=20,
                fetch_run_offset=0,
                embedding_hours=24,
                embedding_limit=50,
                include_embedding=include_embedding,
            )
        )

    conn = db.get_conn()
    try:
        return {
            'codes': db.list_invite_codes(conn),
            'users': db.list_users(conn),
            'fetch_runs': {
                'runs': db.list_fetch_run_audits(conn, limit=20, offset=0),
                'limit': 20,
                'offset': 0,
            },
            'embedding_usage': db.get_embedding_usage_audit(conn, hours=24, limit=50) if include_embedding else {
                'hours': 24,
                'run_id': None,
                'summary': {},
                'by_source': [],
                'by_run': [],
                'logs': [],
                'limit': 50,
            },
        }
    finally:
        conn.close()


@router.get("/api/admin/console/summary")
async def admin_console_summary(request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if not remote_db.app_state_to_remote():
        return {'available': False, 'reason': 'remote_required'}

    try:
        return await run_in_threadpool(remote_db.admin_console_summary_remote)
    except Exception as exc:
        return JSONResponse({
            'available': False,
            'reason': 'remote_error',
            'error': str(exc),
        }, status_code=503)


@router.get("/api/admin/highlights/funnel")
async def admin_highlights_funnel(
    request: Request,
    days: int = 1,
    q: str = "",
    tag: str = "",
):
    _, err = _require_admin(request)
    if err:
        return err

    if not remote_db.app_state_to_remote():
        return JSONResponse({
            'reason': 'remote_required',
            'stations': [],
            'diffs': [],
            'anomalies_count': 0,
            'gate_disabled': False,
        }, status_code=501)

    try:
        return await run_in_threadpool(
            functools.partial(
                remote_db.query_admin_highlights_funnel_remote,
                days=days,
                q=q,
                tag=tag,
            )
        )
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({
            'reason': 'remote_error',
            'error': str(exc),
        }, status_code=503)


@router.get("/api/admin/highlights/funnel/rows")
async def admin_highlights_funnel_rows(
    request: Request,
    view: str = "panorama",
    days: int = 1,
    q: str = "",
    tag: str = "",
    display: str = "all",
    stage: str = "",
    page: int = 1,
    limit: int = 50,
):
    user, err = _require_admin(request)
    if err:
        return err

    safe_page = max(1, int(page or 1))
    safe_limit = max(1, min(int(limit or 50), 100))
    if not remote_db.app_state_to_remote():
        return JSONResponse({
            'reason': 'remote_required',
            'granularity': 'item' if view == 'anomaly' else 'cluster',
            'items': [],
            'total': 0,
            'page': safe_page,
        }, status_code=501)

    try:
        return await run_in_threadpool(
            functools.partial(
                remote_db.query_admin_highlights_funnel_rows_remote,
                view=view,
                days=days,
                q=q,
                tag=tag,
                display=display,
                stage=stage,
                page=safe_page,
                limit=safe_limit,
                user_id=user['id'],
            )
        )
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({
            'reason': 'remote_error',
            'error': str(exc),
        }, status_code=503)


@router.post("/api/admin/highlights/clusters/{cluster_id}/override")
async def admin_highlight_cluster_override(request: Request, cluster_id: int):
    user, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = body if isinstance(body, dict) else {}
    action = str(payload.get('action') or '').strip()
    if action not in {'force_show', 'force_hide', 'clear'}:
        return JSONResponse({'error': 'invalid override action'}, status_code=400)
    raw_note = payload.get('note')
    if raw_note is not None and not isinstance(raw_note, str):
        return JSONResponse({'error': 'override note must be a string'}, status_code=400)
    note = raw_note.strip() if raw_note is not None else None
    note = note or None
    if note is not None and len(note) > 500:
        return JSONResponse({'error': 'override note exceeds 500 characters'}, status_code=400)
    if not remote_db.app_state_to_remote():
        return JSONResponse({'reason': 'remote_required'}, status_code=501)
    try:
        result = await run_in_threadpool(
            functools.partial(
                remote_db.set_admin_highlight_cluster_override_remote,
                cluster_id=cluster_id,
                user_id=user['id'],
                action=action,
                note=note,
            )
        )
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({'reason': 'remote_error', 'error': str(exc)}, status_code=503)
    if result is None:
        return JSONResponse({'error': 'Cluster not found'}, status_code=404)
    return result


@router.get("/api/admin/remote-db/status")
async def remote_database_status(request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if not remote_db.any_remote_backend_enabled():
        return {
            'remote_enabled': False,
            'event_backend': remote_db.event_read_backend(),
            'feed_backend': remote_db.feed_read_backend(),
            'status_backend': remote_db.status_backend(),
        }
    try:
        status = await run_in_threadpool(remote_db.status)
        return {
            'remote_enabled': True,
            **status,
        }
    except remote_db.RemoteDBError as exc:
        return JSONResponse({
            'remote_enabled': True,
            'backend': remote_db.event_read_backend(),
            'error': str(exc),
        }, status_code=503)


@router.get("/api/admin/fetch-runs")
async def list_fetch_runs(request: Request, limit: int = 50, offset: int = 0):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        return {
            'runs': await run_in_threadpool(
                functools.partial(
                    remote_db.list_fetch_run_audits_remote,
                    limit=limit,
                    offset=offset,
                )
            ),
            'limit': max(1, min(int(limit or 50), 100)),
            'offset': max(0, int(offset or 0)),
        }

    conn = db.get_conn()
    try:
        return {
            'runs': db.list_fetch_run_audits(conn, limit=limit, offset=offset),
            'limit': max(1, min(int(limit or 50), 100)),
            'offset': max(0, int(offset or 0)),
        }
    finally:
        conn.close()


@router.get("/api/admin/fetch-runs/{run_id}")
async def get_fetch_run(run_id: int, request: Request):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        run = await run_in_threadpool(remote_db.get_fetch_run_audit_remote, run_id)
        if not run:
            return JSONResponse({'error': 'Fetch run not found'}, status_code=404)
        return {'run': run}

    conn = db.get_conn()
    try:
        run = db.get_fetch_run_audit(conn, run_id)
        if not run:
            return JSONResponse({'error': 'Fetch run not found'}, status_code=404)
        return {'run': run}
    finally:
        conn.close()


@router.get("/api/admin/fetch-runs/{run_id}/items")
async def list_fetch_run_items(
    run_id: int,
    request: Request,
    platform: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        result = await run_in_threadpool(
            functools.partial(
                remote_db.query_fetch_run_audit_items_remote,
                run_id,
                platform=platform,
                source=source,
                limit=limit,
                offset=offset,
            )
        )
        if result.pop('missing_run', False):
            return JSONResponse({'error': 'Fetch run not found'}, status_code=404)
        return {
            'run_id': run_id,
            'platform': platform,
            'source_name': source,
            **result,
        }

    conn = db.get_conn()
    try:
        if not db.get_fetch_run_audit(conn, run_id):
            return JSONResponse({'error': 'Fetch run not found'}, status_code=404)
        result = db.query_fetch_run_audit_items(
            conn,
            run_id,
            platform=platform,
            source=source,
            limit=limit,
            offset=offset,
        )
        return {
            'run_id': run_id,
            'platform': platform,
            'source_name': source,
            **result,
        }
    finally:
        conn.close()


@router.get("/api/admin/embedding-usage")
async def get_embedding_usage(
    request: Request,
    hours: float = 24,
    run_id: int | None = None,
    limit: int = 100,
):
    _, err = _require_admin(request)
    if err:
        return err

    if remote_db.app_state_to_remote():
        return await run_in_threadpool(
            functools.partial(
                remote_db.get_embedding_usage_audit_remote,
                hours=max(0.0, min(float(hours or 24), 24 * 30)),
                run_id=run_id,
                limit=limit,
            )
        )

    conn = db.get_conn()
    try:
        return db.get_embedding_usage_audit(
            conn,
            hours=max(0.0, min(float(hours or 24), 24 * 30)),
            run_id=run_id,
            limit=limit,
        )
    finally:
        conn.close()
