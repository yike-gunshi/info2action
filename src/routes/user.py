"""User settings endpoints: Discord token management + user profile."""
import urllib.request
import urllib.error
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
import remote_db
from routes.auth import get_current_user
from utils.crypto import encrypt, decrypt, mask_token

router = APIRouter()


@router.get("/api/user/settings")
async def get_settings(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    discord_masked = None
    if user.get('discord_bot_token_enc'):
        try:
            token = decrypt(user['discord_bot_token_enc'])
            discord_masked = mask_token(token)
        except Exception:
            discord_masked = '***decryption error***'

    return {
        'username': user['username'],
        'email': user['email'],
        'discord_bot_token': discord_masked,
        'has_discord_token': bool(user.get('discord_bot_token_enc')),
        'discord_channel_id': user.get('discord_channel_id') or '',
    }


@router.put("/api/user/settings")
async def update_settings(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    body = await request.json()
    if remote_db.app_state_to_remote():
        try:
            updates = {}
            if 'discord_bot_token' in body:
                token = body['discord_bot_token']
                updates['discord_bot_token_enc'] = encrypt(token) if token else None
            if 'discord_channel_id' in body:
                updates['discord_channel_id'] = (body['discord_channel_id'] or '').strip() or None
            if updates:
                await run_in_threadpool(remote_db.update_user_remote, user['id'], **updates)  # BE-1
            return {'ok': True}
        except RuntimeError as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    conn = db.get_conn()
    try:
        updates = {}

        # Handle Discord token update
        if 'discord_bot_token' in body:
            token = body['discord_bot_token']
            if token:
                encrypted = encrypt(token)
                updates['discord_bot_token_enc'] = encrypted
            else:
                # Clear token
                updates['discord_bot_token_enc'] = None

        # v21.0: per-user Discord 派发频道
        if 'discord_channel_id' in body:
            updates['discord_channel_id'] = (body['discord_channel_id'] or '').strip() or None

        if updates:
            db.update_user(conn, user['id'], **updates)

        return {'ok': True}
    except RuntimeError as e:
        # ENCRYPTION_KEY not set
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        conn.close()


@router.post("/api/user/settings/discord/verify")
async def verify_discord_token(request: Request):
    """Verify Discord Bot Token by calling Discord API /users/@me."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    if not user.get('discord_bot_token_enc'):
        return JSONResponse({'error': 'No Discord token configured'}, status_code=400)

    try:
        token = decrypt(user['discord_bot_token_enc'])
    except Exception:
        return JSONResponse({'error': 'Failed to decrypt token'}, status_code=500)

    try:
        req = urllib.request.Request(
            'https://discord.com/api/v10/users/@me',
            headers={'Authorization': f'Bot {token}'}
        )
        def _fetch_discord_user():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()

        payload = await run_in_threadpool(_fetch_discord_user)
        data = json.loads(payload)
        return {
            'ok': True,
            'bot_username': data.get('username'),
            'bot_id': data.get('id'),
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return JSONResponse({'error': 'Invalid Discord token'}, status_code=400)
        return JSONResponse({'error': f'Discord API error: {e.code}'}, status_code=502)
    except Exception as e:
        return JSONResponse({'error': f'Connection error: {str(e)}'}, status_code=502)


# ── User Profile (v12.0) ──

@router.get("/api/user/profile")
async def get_profile(request: Request):
    """Get current user's profile (roles, interests, tools, manifest)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    if remote_db.app_state_to_remote():
        profile = await run_in_threadpool(remote_db.get_user_profile_remote, user['id'])  # BE-1
    else:
        conn = db.get_conn()
        try:
            profile = db.get_user_profile(conn, user['id'])
        finally:
            conn.close()
    if not profile:
        return {'profile': None, 'onboarding_completed': False}
    return {
        'profile': {
            'role': profile.get('role'),
            'interests': profile.get('interests') or [],
            'tools': profile.get('tools') or [],
            'manifest': profile.get('manifest'),
        },
        'onboarding_completed': bool(profile.get('onboarding_completed')),
    }


# 稳定性加固(2026-07-10): profile 字段落库并复制到 Supabase Micro(1GB,有 OOM 前科)。
# 无长度上限时,用户反复 PUT 巨型 manifest / 超大 interests 数组就能持久撑大生产库——
# 廉价攻击、持久伤害,正是既往 feed-blank/OOM 事故的形态。这里做服务端上限校验。
_MANIFEST_MAX_CHARS = 64 * 1024
_PROFILE_TEXT_MAX_CHARS = 500
_PROFILE_LIST_MAX_ITEMS = 100
_PROFILE_LIST_ITEM_MAX_CHARS = 200


def _profile_field_error(fields: dict) -> str | None:
    manifest = fields.get('manifest')
    if isinstance(manifest, str) and len(manifest) > _MANIFEST_MAX_CHARS:
        return f'manifest too long (max {_MANIFEST_MAX_CHARS} chars)'
    role = fields.get('role')
    if isinstance(role, str) and len(role) > _PROFILE_TEXT_MAX_CHARS:
        return f'role too long (max {_PROFILE_TEXT_MAX_CHARS} chars)'
    for key in ('interests', 'tools'):
        val = fields.get(key)
        if isinstance(val, list):
            if len(val) > _PROFILE_LIST_MAX_ITEMS:
                return f'{key} has too many items (max {_PROFILE_LIST_MAX_ITEMS})'
            for item in val:
                if isinstance(item, str) and len(item) > _PROFILE_LIST_ITEM_MAX_CHARS:
                    return f'{key} item too long (max {_PROFILE_LIST_ITEM_MAX_CHARS} chars)'
    return None


@router.put("/api/user/profile")
async def update_profile(request: Request):
    """Create or update user profile. Used by Onboarding flow and Settings page."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    body = await request.json()
    # Only pass fields that are present in the request body
    kwargs = {}
    for key in ('role', 'interests', 'tools', 'manifest', 'onboarding_completed'):
        if key in body:
            kwargs[key] = body[key]
    field_error = _profile_field_error(kwargs)
    if field_error:
        return JSONResponse({'error': field_error}, status_code=400)
    if remote_db.app_state_to_remote():
        profile = await run_in_threadpool(remote_db.upsert_user_profile_remote, user['id'], **kwargs)  # BE-1
        return {'ok': True, 'profile': profile}

    conn = db.get_conn()
    try:
        profile = db.upsert_user_profile(conn, user['id'], **kwargs)
        return {'ok': True, 'profile': profile}
    finally:
        conn.close()


@router.put("/api/user/profile/manifest")
async def update_manifest(request: Request):
    """Update only the MANIFEST document."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)

    body = await request.json()
    manifest = body.get('manifest', '')
    field_error = _profile_field_error({'manifest': manifest})
    if field_error:
        return JSONResponse({'error': field_error}, status_code=400)
    if remote_db.app_state_to_remote():
        await run_in_threadpool(remote_db.upsert_user_profile_remote, user['id'], manifest=manifest)  # BE-1
        return {'ok': True}

    conn = db.get_conn()
    try:
        profile = db.upsert_user_profile(conn, user['id'], manifest=manifest)
        return {'ok': True}
    finally:
        conn.close()
