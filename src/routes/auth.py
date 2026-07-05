"""Authentication endpoints: register, login, logout, refresh, me, verify-email, password-reset."""
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

import bcrypt as _bcrypt
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from slowapi import Limiter
from slowapi.util import get_remote_address

import db
import remote_db
from utils.email import send_verification_code, send_password_reset

router = APIRouter()
logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

# ── Environment & JWT config ───────────────────────────────
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'development')
IS_PRODUCTION = ENVIRONMENT == 'production'

JWT_SECRET = os.environ.get('JWT_SECRET', 'dev-secret-change-in-production')
JWT_ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRY = timedelta(minutes=30)
REFRESH_TOKEN_EXPIRY = timedelta(days=7)

# Fail-fast: reject insecure JWT_SECRET in production
if IS_PRODUCTION:
    if not os.environ.get('JWT_SECRET') or JWT_SECRET == 'dev-secret-change-in-production':
        raise RuntimeError(
            "FATAL: JWT_SECRET must be set to a strong random value in production. "
            "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )


def _generate_verification_code() -> str:
    """6 位验证码用 CSPRNG(review 2026-07-04:random 模块可预测,secrets 已在依赖)。"""
    return f"{secrets.randbelow(900000) + 100000}"


def _open_registration_enabled() -> bool:
    """P1-4 开放注册开关:为真时邀请码可选(填了仍会校验并消耗)。"""
    return (os.environ.get('INFO2ACTION_OPEN_REGISTRATION') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _send_verification_async(email: str, code: str, username: str) -> None:
    """P1-4 邮件异步化:注册响应不再等 Resend 往返(高峰期同步发信会占满
    线程池)。失败重试一次;仍失败靠 /api/auth/resend-code 用户自助兜底。"""
    def _run():
        for attempt in (1, 2):
            try:
                if send_verification_code(email, code, username):
                    return
                logger.warning("verification email attempt %d returned false for %s", attempt, email)
            except Exception as exc:
                logger.warning("verification email attempt %d failed for %s: %s", attempt, email, exc)
            time.sleep(2)
        logger.error("verification email failed twice for %s; user must use resend-code", email)

    threading.Thread(target=_run, daemon=True, name='send-verification-email').start()


def _make_token_pair(user_id: str, role: str):
    now = datetime.now(timezone.utc)
    access_jti = str(uuid.uuid4())
    refresh_jti = str(uuid.uuid4())
    access_expires = now + ACCESS_TOKEN_EXPIRY
    refresh_expires = now + REFRESH_TOKEN_EXPIRY

    access_payload = {
        'sub': user_id,
        'role': role,
        'jti': access_jti,
        'exp': access_expires,
        'iat': now,
    }
    refresh_payload = {
        'sub': user_id,
        'jti': refresh_jti,
        'type': 'refresh',
        'exp': refresh_expires,
        'iat': now,
    }

    access_token = jwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    refresh_token = jwt.encode(refresh_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return access_token, refresh_token, access_jti, refresh_jti, access_expires, refresh_expires


def _issue_tokens(user_id: str, role: str):
    """Create access + refresh token pair, store sessions in DB."""
    access_token, refresh_token, access_jti, refresh_jti, access_expires, refresh_expires = _make_token_pair(user_id, role)

    # Store sessions for revocation
    if remote_db.app_state_to_remote():
        remote_db.create_sessions_remote([
            (access_jti, user_id, 'access', access_expires.isoformat()),
            (refresh_jti, user_id, 'refresh', refresh_expires.isoformat()),
        ])
    else:
        conn = db.get_conn()
        try:
            db.create_session(conn, access_jti, user_id, 'access',
                              access_expires.isoformat())
            db.create_session(conn, refresh_jti, user_id, 'refresh',
                              refresh_expires.isoformat())
        finally:
            conn.close()

    return access_token, refresh_token


def _set_auth_cookies(response: JSONResponse, access_token: str, refresh_token: str):
    """Set HttpOnly cookies for both tokens."""
    response.set_cookie(
        'access_token', access_token,
        httponly=True, samesite='lax', path='/',
        secure=IS_PRODUCTION,
        max_age=int(ACCESS_TOKEN_EXPIRY.total_seconds()),
    )
    response.set_cookie(
        'refresh_token', refresh_token,
        httponly=True, samesite='lax', path='/api/auth/refresh',
        secure=IS_PRODUCTION,
        max_age=int(REFRESH_TOKEN_EXPIRY.total_seconds()),
    )
    return response


def _clear_auth_cookies(response: JSONResponse):
    """Clear auth cookies."""
    response.delete_cookie('access_token', path='/')
    response.delete_cookie('refresh_token', path='/api/auth/refresh')
    return response


def decode_access_token(token: str):
    """Decode and validate an access token. Returns payload or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get('type') == 'refresh':
            return None  # Don't accept refresh tokens as access tokens
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_current_user(request: Request):
    """Extract current user from request state (set by JWT middleware)."""
    return getattr(request.state, 'user', None)


@router.get("/api/auth/register-config")
async def register_config():
    """P1-4: 前端据此决定注册页是否展示邀请码输入框。"""
    return JSONResponse({'open_registration': _open_registration_enabled()})


@router.post("/api/auth/register")
@limiter.limit("5/minute")
async def register(request: Request):
    body = await request.json()
    username = (body.get('username') or '').strip()
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    invite_code = (body.get('invite_code') or '').strip().upper()

    # Validate fields
    if not username or len(username) < 3 or len(username) > 20:
        return JSONResponse({'error': '用户名需要 3-20 个字符'}, status_code=400)
    if not email or '@' not in email:
        return JSONResponse({'error': '请输入有效的邮箱地址'}, status_code=400)
    if len(password) < 8:
        return JSONResponse({'error': '密码至少 8 个字符'}, status_code=400)
    # P1-4 开放注册:开关打开时邀请码可选;填了仍走校验+消耗(老邀请链接不失效)
    if not invite_code and not _open_registration_enabled():
        return JSONResponse({'error': '请输入邀请码'}, status_code=400)

    if remote_db.app_state_to_remote():
        def _register_remote_blocking():
            if invite_code:
                code_record = remote_db.get_invite_code_remote(invite_code)
                if not code_record:
                    return ('error', 400, '邀请码无效')
                if code_record['used_count'] >= code_record['max_uses']:
                    return ('error', 400, '邀请码已被使用')
                if code_record['expires_at']:
                    if datetime.fromisoformat(code_record['expires_at'].replace('Z', '+00:00')) < datetime.now(timezone.utc):
                        return ('error', 400, '邀请码已过期')
            if remote_db.get_user_by_username_remote(username):
                return ('error', 400, '用户名已被占用')
            if remote_db.get_user_by_email_remote(email):
                return ('error', 400, '邮箱已被注册')

            user_id = str(uuid.uuid4())
            password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=12)).decode()
            code = _generate_verification_code()
            expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            # P1-4: 先建用户再后台发信(原先同步发信失败即整单失败,且高峰期
            # 会占满线程池);发信失败由 resend-code 兜底
            if invite_code:
                if not remote_db.create_user_with_invite_remote(
                    user_id,
                    username,
                    email,
                    password_hash,
                    invite_code,
                    code,
                    expires,
                ):
                    return ('error', 400, '邀请码已被使用')
            else:
                remote_db.create_user_open_remote(
                    user_id, username, email, password_hash, code, expires,
                )
            _send_verification_async(email, code, username)
            return ('ok',)

        try:
            result = await run_in_threadpool(_register_remote_blocking)
        except Exception as e:
            logger.error(f"Registration error: {e}", exc_info=True)
            return JSONResponse({'error': '注册失败，请稍后重试'}, status_code=500)
        if result[0] == 'error':
            return JSONResponse({'error': result[2]}, status_code=result[1])
        return JSONResponse({
            'ok': True,
            'verify_email': True,
            'email': email,
            'message': '验证码已发送到你的邮箱',
        })

    def _register_local_blocking():
        conn = db.get_conn()
        try:
            if invite_code:
                code_record = db.get_invite_code(conn, invite_code)
                if not code_record:
                    return ('error', 400, '邀请码无效')
                if code_record['used_count'] >= code_record['max_uses']:
                    return ('error', 400, '邀请码已被使用')
                if code_record['expires_at']:
                    if datetime.fromisoformat(code_record['expires_at']) < datetime.now(timezone.utc):
                        return ('error', 400, '邀请码已过期')
            if db.get_user_by_username(conn, username):
                return ('error', 400, '用户名已被占用')
            if db.get_user_by_email(conn, email):
                return ('error', 400, '邮箱已被注册')
            user_id = str(uuid.uuid4())
            password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=12)).decode()
            code = _generate_verification_code()
            expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            if invite_code:
                if not db.create_user_with_invite(
                    conn,
                    user_id,
                    username,
                    email,
                    password_hash,
                    invite_code,
                    code,
                    expires,
                ):
                    return ('error', 400, '邀请码已被使用')
            else:
                db.create_user_open(
                    conn, user_id, username, email, password_hash, code, expires,
                )
            _send_verification_async(email, code, username)
            return ('ok',)
        finally:
            conn.close()

    try:
        result = await run_in_threadpool(_register_local_blocking)
    except Exception as e:
        logger.error(f"Registration error: {e}", exc_info=True)
        return JSONResponse({'error': '注册失败，请稍后重试'}, status_code=500)
    if result[0] == 'error':
        return JSONResponse({'error': result[2]}, status_code=result[1])
    return JSONResponse({
        'ok': True,
        'verify_email': True,
        'email': email,
        'message': '验证码已发送到你的邮箱',
    })


@router.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(request: Request):
    body = await request.json()
    login_val = (body.get('login') or '').strip()
    password = body.get('password') or ''

    if not login_val or not password:
        return JSONResponse({'error': '请输入邮箱和密码'}, status_code=400)

    if remote_db.app_state_to_remote():
        try:
            user = await run_in_threadpool(remote_db.get_user_by_login_remote, login_val)
        except remote_db.RemoteDBError as exc:
            logger.warning("Remote login lookup failed: %s", exc)
            return JSONResponse({'error': '登录服务暂时不可用，请稍后重试'}, status_code=503)
        if not user:
            return JSONResponse({'error': '邮箱或密码错误'}, status_code=401)
        password_ok = await run_in_threadpool(
            _bcrypt.checkpw, password.encode(), user['password_hash'].encode()
        )
        if not password_ok:
            return JSONResponse({'error': '邮箱或密码错误'}, status_code=401)
        if not user.get('email_verified'):
            return JSONResponse({
                'error': '请先验证邮箱',
                'verify_email': True,
                'email': user['email'],
            }, status_code=403)
        access_token, refresh_token, access_jti, refresh_jti, access_expires, refresh_expires = _make_token_pair(user['id'], user['role'])
        try:
            profile = await run_in_threadpool(
                remote_db.finish_login_remote,
                user['id'],
                access_jti=access_jti,
                access_expires_at=access_expires.isoformat(),
                refresh_jti=refresh_jti,
                refresh_expires_at=refresh_expires.isoformat(),
                last_login_at=datetime.now(timezone.utc).isoformat(),
            )
        except remote_db.RemoteDBError as exc:
            logger.warning("Remote login finish failed: %s", exc)
            return JSONResponse({'error': '登录服务暂时不可用，请稍后重试'}, status_code=503)
        onboarding_completed = True if profile is None else bool(profile.get('onboarding_completed'))
        response = JSONResponse({
            'ok': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'role': user['role'],
                'has_discord_token': bool(user.get('discord_bot_token_enc')),
                'onboarding_completed': onboarding_completed,
            }
        })
        return _set_auth_cookies(response, access_token, refresh_token)

    def _local_login_blocking():
        conn = db.get_conn()
        try:
            user = db.get_user_by_login(conn, login_val)
            if not user:
                return ('error_401',)
            if not _bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
                return ('error_401',)
            if not user.get('email_verified'):
                return ('verify_email', user['email'])
            db.update_user(conn, user['id'], last_login_at=datetime.now(timezone.utc).isoformat())
            profile = db.get_user_profile(conn, user['id'])
            onboarding_completed = True if profile is None else bool(profile.get('onboarding_completed'))
            access_token, refresh_token = _issue_tokens(user['id'], user['role'])
            return ('ok', user, onboarding_completed, access_token, refresh_token)
        finally:
            conn.close()

    result = await run_in_threadpool(_local_login_blocking)
    if result[0] == 'error_401':
        return JSONResponse({'error': '邮箱或密码错误'}, status_code=401)
    if result[0] == 'verify_email':
        return JSONResponse({
            'error': '请先验证邮箱',
            'verify_email': True,
            'email': result[1],
        }, status_code=403)
    _, user, onboarding_completed, access_token, refresh_token = result
    response = JSONResponse({
        'ok': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'email': user['email'],
            'role': user['role'],
            'has_discord_token': bool(user.get('discord_bot_token_enc')),
            'onboarding_completed': onboarding_completed,
        }
    })
    return _set_auth_cookies(response, access_token, refresh_token)


@router.post("/api/auth/verify-email")
@limiter.limit("10/minute")
async def verify_email(request: Request):
    """Verify email with 6-digit code, then auto-login."""
    body = await request.json()
    email = (body.get('email') or '').strip().lower()
    code = (body.get('code') or '').strip()

    if not email or not code:
        return JSONResponse({'error': '请输入邮箱和验证码'}, status_code=400)

    if remote_db.app_state_to_remote():
        user = remote_db.get_user_by_email_remote(email)
        if not user:
            return JSONResponse({'error': '用户不存在'}, status_code=404)
        if user.get('email_verified'):
            return JSONResponse({'error': '邮箱已验证'}, status_code=400)
        if user.get('verification_code') != code:
            return JSONResponse({'error': '验证码错误'}, status_code=400)
        if user.get('verification_code_expires'):
            expires = datetime.fromisoformat(user['verification_code_expires'].replace('Z', '+00:00'))
            if expires < datetime.now(timezone.utc):
                return JSONResponse({'error': '验证码已过期，请重新发送'}, status_code=400)
        remote_db.update_user_remote(
            user['id'],
            email_verified=1,
            verification_code=None,
            verification_code_expires=None,
        )
        profile = remote_db.upsert_user_profile_remote(
            user['id'],
            onboarding_completed=False,
        )
        onboarding_completed = bool(profile and profile.get('onboarding_completed'))
        access_token, refresh_token = _issue_tokens(user['id'], user['role'])
        response = JSONResponse({
            'ok': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'role': user['role'],
                'has_discord_token': bool(user.get('discord_bot_token_enc')),
                'onboarding_completed': onboarding_completed,
            }
        })
        return _set_auth_cookies(response, access_token, refresh_token)

    conn = db.get_conn()
    try:
        user = db.get_user_by_email(conn, email)
        if not user:
            return JSONResponse({'error': '用户不存在'}, status_code=404)

        if user.get('email_verified'):
            return JSONResponse({'error': '邮箱已验证'}, status_code=400)

        if user.get('verification_code') != code:
            return JSONResponse({'error': '验证码错误'}, status_code=400)

        if user.get('verification_code_expires'):
            expires = datetime.fromisoformat(user['verification_code_expires'])
            if expires < datetime.now(timezone.utc):
                return JSONResponse({'error': '验证码已过期，请重新发送'}, status_code=400)

        # Mark verified, clear code
        db.update_user(conn, user['id'],
                       email_verified=1,
                       verification_code=None,
                       verification_code_expires=None)

        # Auto-login
        profile = db.upsert_user_profile(conn, user['id'], onboarding_completed=False)
        onboarding_completed = bool(profile and profile.get('onboarding_completed'))

        access_token, refresh_token = _issue_tokens(user['id'], user['role'])
        response = JSONResponse({
            'ok': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'role': user['role'],
                'has_discord_token': bool(user.get('discord_bot_token_enc')),
                'onboarding_completed': onboarding_completed,
            }
        })
        return _set_auth_cookies(response, access_token, refresh_token)
    finally:
        conn.close()


@router.post("/api/auth/resend-code")
@limiter.limit("3/10minutes")
async def resend_code(request: Request):
    """Resend verification code to email."""
    body = await request.json()
    email = (body.get('email') or '').strip().lower()

    if not email:
        return JSONResponse({'error': '请输入邮箱地址'}, status_code=400)

    if remote_db.app_state_to_remote():
        user = remote_db.get_user_by_email_remote(email)
        if not user:
            return JSONResponse({'error': '用户不存在'}, status_code=404)
        if user.get('email_verified'):
            return JSONResponse({'error': '邮箱已验证'}, status_code=400)
        code = _generate_verification_code()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        remote_db.update_user_remote(
            user['id'],
            verification_code=code,
            verification_code_expires=expires,
        )
        send_verification_code(email, code, user.get('username', ''))
        return JSONResponse({'ok': True, 'message': '验证码已重新发送'})

    conn = db.get_conn()
    try:
        user = db.get_user_by_email(conn, email)
        if not user:
            return JSONResponse({'error': '用户不存在'}, status_code=404)

        if user.get('email_verified'):
            return JSONResponse({'error': '邮箱已验证'}, status_code=400)

        # Generate new code
        code = _generate_verification_code()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        db.update_user(conn, user['id'],
                       verification_code=code,
                       verification_code_expires=expires)

        send_verification_code(email, code, user.get('username', ''))

        return JSONResponse({'ok': True, 'message': '验证码已重新发送'})
    finally:
        conn.close()


@router.post("/api/auth/forgot-password")
@limiter.limit("3/10minutes")
async def forgot_password(request: Request):
    """Send password reset email with a secure token."""
    body = await request.json()
    email = (body.get('email') or '').strip().lower()

    if not email or '@' not in email:
        return JSONResponse({'error': '请输入有效的邮箱地址'}, status_code=400)

    if remote_db.app_state_to_remote():
        try:
            user = remote_db.get_user_by_email_remote(email)
            if not user:
                return JSONResponse({'ok': True, 'message': '如果该邮箱已注册，你将收到重置链接'})
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            remote_db.update_user_remote(
                user['id'],
                reset_token=token,
                reset_token_expires=expires,
            )
            base_url = os.environ.get('APP_BASE_URL', 'http://localhost:8080')
            reset_url = f"{base_url}/#reset-password?token={token}"
            send_password_reset(email, reset_url, user.get('username', ''))
            return JSONResponse({'ok': True, 'message': '如果该邮箱已注册，你将收到重置链接'})
        except Exception as e:
            logger.error(f"Forgot password error: {e}", exc_info=True)
            return JSONResponse({'error': '发送失败，请稍后重试'}, status_code=500)

    conn = db.get_conn()
    try:
        user = db.get_user_by_email(conn, email)
        if not user:
            # Don't reveal whether email exists
            return JSONResponse({'ok': True, 'message': '如果该邮箱已注册，你将收到重置链接'})

        # Generate secure token
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        db.update_user(conn, user['id'],
                       reset_token=token,
                       reset_token_expires=expires)

        # Build reset URL — use server-configured base URL, NOT request Origin header
        # (Origin can be spoofed, leading to token theft via phishing link)
        base_url = os.environ.get('APP_BASE_URL', 'http://localhost:8080')
        reset_url = f"{base_url}/#reset-password?token={token}"
        send_password_reset(email, reset_url, user.get('username', ''))

        return JSONResponse({'ok': True, 'message': '如果该邮箱已注册，你将收到重置链接'})
    except Exception as e:
        logger.error(f"Forgot password error: {e}", exc_info=True)
        return JSONResponse({'error': '发送失败，请稍后重试'}, status_code=500)
    finally:
        conn.close()


@router.post("/api/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request):
    """Reset password using a valid token."""
    body = await request.json()
    token = (body.get('token') or '').strip()
    new_password = body.get('password') or ''

    if not token:
        return JSONResponse({'error': '重置链接无效'}, status_code=400)
    if len(new_password) < 8:
        return JSONResponse({'error': '密码至少 8 个字符'}, status_code=400)

    if remote_db.app_state_to_remote():
        def _reset_remote_blocking():
            user = remote_db.get_user_by_reset_token_remote(token)
            if not user:
                return ('error', 400, '重置链接无效或已过期')
            if user.get('reset_token_expires'):
                expires = datetime.fromisoformat(user['reset_token_expires'].replace('Z', '+00:00'))
                if expires < datetime.now(timezone.utc):
                    return ('error', 400, '重置链接已过期，请重新发送')
            password_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt(rounds=12)).decode()
            remote_db.update_user_remote(
                user['id'],
                password_hash=password_hash,
                reset_token=None,
                reset_token_expires=None,
            )
            remote_db.delete_user_sessions_remote(user['id'])
            return ('ok',)

        try:
            result = await run_in_threadpool(_reset_remote_blocking)
        except Exception as e:
            logger.error(f"Reset password error: {e}", exc_info=True)
            return JSONResponse({'error': '重置失败，请稍后重试'}, status_code=500)
        if result[0] == 'error':
            return JSONResponse({'error': result[2]}, status_code=result[1])
        return JSONResponse({'ok': True, 'message': '密码已重置，请登录'})

    def _reset_local_blocking():
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "SELECT id, username, reset_token_expires FROM users WHERE reset_token = ?",
                (token,))
            user = cur.fetchone()
            if not user:
                return ('error', 400, '重置链接无效或已过期')
            if user['reset_token_expires']:
                expires = datetime.fromisoformat(user['reset_token_expires'])
                if expires < datetime.now(timezone.utc):
                    return ('error', 400, '重置链接已过期，请重新发送')
            password_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt(rounds=12)).decode()
            db.update_user(conn, user['id'],
                           password_hash=password_hash,
                           reset_token=None,
                           reset_token_expires=None)
            db.delete_user_sessions(conn, user['id'])
            return ('ok',)
        finally:
            conn.close()

    try:
        result = await run_in_threadpool(_reset_local_blocking)
    except Exception as e:
        logger.error(f"Reset password error: {e}", exc_info=True)
        return JSONResponse({'error': '重置失败，请稍后重试'}, status_code=500)
    if result[0] == 'error':
        return JSONResponse({'error': result[2]}, status_code=result[1])
    return JSONResponse({'ok': True, 'message': '密码已重置，请登录'})


@router.post("/api/auth/logout")
async def logout(request: Request):
    user = get_current_user(request)
    if user:
        if remote_db.app_state_to_remote():
            remote_db.delete_user_sessions_remote(user['id'])
        else:
            conn = db.get_conn()
            try:
                db.delete_user_sessions(conn, user['id'])
            finally:
                conn.close()

    response = JSONResponse({'ok': True})
    return _clear_auth_cookies(response)


@router.get("/api/auth/me")
async def me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({
            'error': '未登录',
            'can_refresh': bool(request.cookies.get('refresh_token')),
        }, status_code=401)

    # Check onboarding status — legacy users without profile skip onboarding
    if '_onboarding_completed' in user:
        onboarding_completed = bool(user.get('_onboarding_completed'))
    elif remote_db.app_state_to_remote():
        # BE-1: 每次冷启动必发,远程往返离开事件循环
        profile = await run_in_threadpool(remote_db.get_user_profile_remote, user['id'])
        onboarding_completed = True if profile is None else bool(profile.get('onboarding_completed'))
    else:
        conn = db.get_conn()
        try:
            profile = db.get_user_profile(conn, user['id'])
        finally:
            conn.close()
        onboarding_completed = True if profile is None else bool(profile.get('onboarding_completed'))

    return {
        'id': user['id'],
        'username': user['username'],
        'email': user['email'],
        'role': user['role'],
        'has_discord_token': bool(user.get('discord_bot_token_enc')),
        'onboarding_completed': onboarding_completed,
    }


@router.post("/api/auth/refresh")
async def refresh(request: Request):
    refresh_token = request.cookies.get('refresh_token')
    if not refresh_token:
        return JSONResponse({'error': '登录已过期'}, status_code=401)

    try:
        payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        response = JSONResponse({'error': '登录已过期'}, status_code=401)
        return _clear_auth_cookies(response)
    except jwt.InvalidTokenError:
        return JSONResponse({'error': '登录已失效'}, status_code=401)

    if payload.get('type') != 'refresh':
        return JSONResponse({'error': '登录已失效'}, status_code=401)

    # Verify session exists (not revoked)
    if remote_db.app_state_to_remote():
        now = datetime.now(timezone.utc)
        new_jti = str(uuid.uuid4())
        access_payload = {
            'sub': payload['sub'],
            'role': payload.get('role', 'user'),
            'jti': new_jti,
            'exp': now + ACCESS_TOKEN_EXPIRY,
            'iat': now,
        }
        # BE-1: 每用户每 30 分钟必发的静默续期,远程往返离开事件循环
        user = await run_in_threadpool(
            remote_db.refresh_access_session_remote,
            refresh_jti=payload['jti'],
            user_id=payload['sub'],
            access_jti=new_jti,
            access_expires_at=(now + ACCESS_TOKEN_EXPIRY).isoformat(),
        )
        if not user:
            response = JSONResponse({'error': '登录已失效'}, status_code=401)
            return _clear_auth_cookies(response)
        access_payload['role'] = user['role']
        new_access_token = jwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    else:
        conn = db.get_conn()
        try:
            session = db.get_session(conn, payload['jti'])
            if not session:
                response = JSONResponse({'error': '登录已失效'}, status_code=401)
                return _clear_auth_cookies(response)

            user = db.get_user(conn, payload['sub'])
            if not user:
                return JSONResponse({'error': '用户不存在'}, status_code=401)
            now = datetime.now(timezone.utc)
            new_jti = str(uuid.uuid4())
            access_payload = {
                'sub': user['id'],
                'role': user['role'],
                'jti': new_jti,
                'exp': now + ACCESS_TOKEN_EXPIRY,
                'iat': now,
            }
            new_access_token = jwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
            db.create_session(conn, new_jti, user['id'], 'access',
                              (now + ACCESS_TOKEN_EXPIRY).isoformat())
        finally:
            conn.close()

    response = JSONResponse({'ok': True})
    response.set_cookie(
        'access_token', new_access_token,
        httponly=True, samesite='lax', path='/',
        secure=IS_PRODUCTION,
        max_age=int(ACCESS_TOKEN_EXPIRY.total_seconds()),
    )
    return response


@router.get("/api/auth/google")
async def google_redirect():
    """Reserved for Google OAuth — requires HTTPS."""
    return JSONResponse(
        {'error': 'Google 登录暂不可用，需要 HTTPS 配置'},
        status_code=501
    )


@router.get("/api/auth/google/callback")
async def google_callback():
    return JSONResponse(
        {'error': 'Google 登录暂不可用，需要 HTTPS 配置'},
        status_code=501
    )
