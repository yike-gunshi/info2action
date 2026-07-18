"""Health check and credential sync endpoints."""

import base64
import functools
import json
import os
import subprocess
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
import remote_db
from authz import require_admin
from deps import BASE
from health_freshness import classify_platform_freshness, platform_freshness_message

router = APIRouter()

_REMOTE_HEALTH_CACHE: dict[str, object] = {"ts": 0.0, "data": None}
_REMOTE_HEALTH_TTL_SEC = 60

LOCAL_BIN_PATH = ':'.join([
    os.path.expanduser('~/.local/bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
])


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _env_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _remote_authority_health(*, skip_db_check: bool = False):
    from datetime import datetime as _dt

    if skip_db_check:
        remote_status = {
            'status': 'skipped',
            'reason': 'remote_health_db_check_disabled',
            'backend': remote_db.status_backend(),
            'schema': remote_db.remote_schema(),
        }
        counts = {}
        overall = 'degraded'
        asr_quota = None
    else:
        try:
            status = remote_db.status()
            counts = status.get('counts') or {}
            remote_status = {
                'status': 'ok',
                'backend': status.get('backend'),
                'event_backend': status.get('event_backend'),
                'feed_backend': status.get('feed_backend'),
                'status_backend': status.get('status_backend'),
                'schema': status.get('schema'),
                'counts': counts,
                'postgres_version': status.get('postgres_version'),
            }
            overall = 'ok'
        except remote_db.RemoteDBError as exc:
            remote_status = {
                'status': 'error',
                'message': str(exc),
            }
            counts = {}
            overall = 'error'
        try:
            quota = remote_db.get_asr_usage_today_remote(user_id=0)
            asr_quota = {
                'used_hours': quota['used_hours'],
                'remaining_hours': quota['remaining_hours'],
                'over_limit': quota['over_limit'],
                'reset_at': quota['reset_at'],
                'daily_quota_hours': round(quota['daily_quota_sec'] / 3600, 1),
            }
        except Exception:
            asr_quota = None

    return {
        'overall': overall,
        'last_check': _dt.now().isoformat(),
        'data_authority': remote_db.data_authority(),
        'remote_db': remote_status,
        'platforms': {},
        'proxy': {'status': 'unknown', 'message': 'remote-authority mode skips local proxy checks'},
        'asr_quota': asr_quota,
        'items_count': int(counts.get('items') or 0),
    }


@router.get('/api/health')
def get_health(recheck: str = Query(None)):
    if remote_db.remote_authority_enabled():
        now = time.monotonic()
        cached = _REMOTE_HEALTH_CACHE.get("data")
        if recheck is None and cached is not None and now - float(_REMOTE_HEALTH_CACHE.get("ts") or 0) < _REMOTE_HEALTH_TTL_SEC:
            return cached
        fresh = _remote_authority_health(
            skip_db_check=not _env_enabled('INFO2ACTION_REMOTE_HEALTH_DB_CHECK', default=True)
        )
        _REMOTE_HEALTH_CACHE["ts"] = now
        _REMOTE_HEALTH_CACHE["data"] = fresh
        return fresh

    health_path = os.path.join(BASE, 'data', 'health.json')
    # Snapshot previous statuses for change detection
    _prev_health = load_json(health_path) or {}
    _prev_statuses = {}
    for _pk, _pv in _prev_health.get('platforms', {}).items():
        _prev_statuses[_pk] = _pv.get('status', 'unknown')
    _prev_statuses['_proxy'] = _prev_health.get('proxy', {}).get('status', 'unknown')

    health = load_json(health_path) or {
        'platforms': {},
        'proxy': {'status': 'unknown', 'message': '尚未执行健康检查'},
        'last_check': None,
        'overall': 'unknown'
    }

    # Enrich with per-platform last fetch time + item count from SQLite
    now_epoch = int(time.time())
    conn = db.get_conn()
    for plat in ('twitter', 'xiaohongshu', 'bilibili', 'rss', 'hackernews', 'reddit', 'github', 'lingowhale'):
        row = conn.execute(
            "SELECT MAX(fetched_at) as last_fetch, COUNT(*) as items_count "
            "FROM items WHERE platform = ?", (plat,)
        ).fetchone()
        plat_info = health.get('platforms', {}).get(plat, {'status': 'ok', 'message': ''})
        plat_info['last_fetch'] = row['last_fetch'] if row and row['last_fetch'] else None
        plat_info['items_count'] = row['items_count'] if row else 0
        # Staleness detection
        if plat_info['last_fetch']:
            from datetime import datetime
            try:
                last_dt = datetime.fromisoformat(plat_info['last_fetch'])
                age_hours = (datetime.now() - last_dt).total_seconds() / 3600
                freshness_level = classify_platform_freshness(age_hours)
                if freshness_level == 'crit':
                    plat_info['status'] = 'error'
                    plat_info['message'] = platform_freshness_message(age_hours)
                elif freshness_level == 'warn':
                    plat_info['status'] = 'warning'
                    plat_info['message'] = platform_freshness_message(age_hours)
                else:
                    plat_info['status'] = 'ok'
                    plat_info['message'] = ''
            except Exception:
                pass
        elif plat_info['items_count'] == 0:
            plat_info['status'] = 'error'
            plat_info['message'] = '无数据'
        health.setdefault('platforms', {})[plat] = plat_info
    conn.close()

    # Check Twitter/XHS CLI auth (cached, max once per 5min)
    cli_cache_path = os.path.join(BASE, 'data', 'cli_auth_cache.json')
    cli_cache = load_json(cli_cache_path) or {}
    do_recheck = recheck is not None
    cli_cache_age = now_epoch - cli_cache.get('checked_at', 0)
    if do_recheck or cli_cache_age > 300:
        # Proxy check
        _hp = os.environ.get('http_proxy', os.environ.get('HTTP_PROXY', ''))
        _pp = _hp.rsplit(':', 1)[-1].rstrip('/') if ':' in _hp else '7897'
        proxy_port = os.environ.get('PROXY_PORT', _pp) or '7897'
        proxy_info = {'status': 'ok', 'message': ''}
        try:
            pr = subprocess.run(
                ['curl', '-x', f'http://127.0.0.1:{proxy_port}', 'https://x.com', '-I',
                 '--max-time', '10', '-s', '-o', '/dev/null', '-w', '%{http_code}'],
                capture_output=True, text=True, timeout=15)
            code = pr.stdout.strip()
            if code == '000' or pr.returncode != 0:
                proxy_info = {'status': 'error', 'message': f'代理不可用，无法连接 127.0.0.1:{proxy_port}'}
            elif code.isdigit() and int(code) >= 400 and code != '403':
                proxy_info = {'status': 'warning', 'message': f'代理返回 HTTP {code}，可能不稳定'}
        except Exception:
            proxy_info = {'status': 'unknown', 'message': '代理检测失败'}
        health['proxy'] = proxy_info

        cli_env = {k: v for k, v in os.environ.items()
                   if not any(p in k for p in ('PYTHON', 'VIRTUAL_ENV', '__PYVENV'))}
        for cli_name, cli_cmd in [('twitter', ['twitter', '--compact', 'whoami', '--json']),
                                   ('xiaohongshu', ['xhs', 'feed', '--json'])]:
            try:
                r = subprocess.run(cli_cmd, capture_output=True, text=True, timeout=15, env=cli_env)
                output = r.stdout + r.stderr
                if 'not_authenticated' in output or 'expired' in output.lower() or r.returncode != 0:
                    cli_cache[cli_name] = {'auth': 'expired', 'msg': 'Cookie/Session 已过期，需重新登录'}
                else:
                    cli_cache[cli_name] = {'auth': 'ok', 'msg': '认证正常'}
            except FileNotFoundError:
                cli_cache[cli_name] = {'auth': 'missing', 'msg': 'CLI 工具未安装'}
            except subprocess.TimeoutExpired:
                cli_cache[cli_name] = {'auth': 'timeout', 'msg': 'CLI 超时'}
            except Exception as e:
                cli_cache[cli_name] = {'auth': 'error', 'msg': str(e)[:100]}
        cli_cache['checked_at'] = now_epoch
        try:
            with open(cli_cache_path, 'w') as f:
                json.dump(cli_cache, f)
        except Exception:
            pass

    # Merge CLI auth status into platform info
    for cli_plat in ('twitter', 'xiaohongshu'):
        cli_info = cli_cache.get(cli_plat, {})
        plat_info = health.get('platforms', {}).get(cli_plat, {})
        if cli_info.get('auth') == 'expired':
            plat_info['status'] = 'error'
            plat_info['message'] = cli_info.get('msg', 'Cookie 已过期')
        elif cli_info.get('auth') == 'missing':
            plat_info['status'] = 'error'
            plat_info['message'] = cli_info.get('msg', 'CLI 未安装')
        elif cli_info.get('auth') == 'ok':
            if plat_info.get('status') == 'error' and plat_info.get('message', '') in (
                'Cookie/Session 已过期，需重新登录', 'CLI 工具未安装', 'CLI 未安装', 'CLI 超时'
            ):
                plat_info['status'] = 'ok'
                plat_info['message'] = ''
        health.setdefault('platforms', {})[cli_plat] = plat_info

    # Check lingowhale JWT token expiry
    try:
        lw_cfg = (load_json(os.path.join(BASE, 'config', 'config.json')) or {}).get('lingowhale', {})
        auth_token = lw_cfg.get('auth_token', '')
        if auth_token:
            parts = auth_token.split('.')
            if len(parts) >= 2:
                payload = parts[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                exp = decoded.get('exp', 0)
                lw_plat = health.get('platforms', {}).get('lingowhale', {'status': 'ok', 'message': ''})
                if exp and exp < now_epoch:
                    lw_plat['status'] = 'error'
                    lw_plat['message'] = 'JWT token 已过期'
                elif exp and exp - now_epoch < 3 * 86400:
                    lw_plat['status'] = 'warning'
                    lw_plat['message'] = f'JWT token 将在 {(exp - now_epoch) // 86400} 天后过期'
                health.setdefault('platforms', {})['lingowhale'] = lw_plat
    except Exception:
        pass

    # Check wechat-article-exporter auth key
    try:
        wx_cfg = (load_json(os.path.join(BASE, 'config', 'config.json')) or {}).get('wechat_exporter', {})
        # oss-release F3c: env/.env 优先，config.json 只留空模板
        from env_utils import load_project_env
        wx_key = (
            os.environ.get('WECHAT_EXPORTER_AUTH_KEY')
            or load_project_env(BASE).get('WECHAT_EXPORTER_AUTH_KEY')
            or wx_cfg.get('auth_key', '')
            or ''
        ).strip()
        wx_status = {'status': 'unknown', 'message': '未配置 auth_key'}
        if wx_key:
            import urllib.request as _ureq
            import ssl as _ssl
            wx_req = _ureq.Request(
                'https://down.mptext.top/api/public/v1/authkey',
                headers={'X-Auth-Key': wx_key, 'User-Agent': 'info2action/1.0'}
            )
            ssl_ctx = _ssl.create_default_context()
            try:
                with _ureq.urlopen(wx_req, timeout=10, context=ssl_ctx) as wx_resp:
                    wx_data = json.loads(wx_resp.read().decode('utf-8'))
                    if wx_resp.status == 200 and not wx_data.get('error'):
                        wx_status = {'status': 'ok', 'message': 'auth_key 有效'}
                    else:
                        wx_status = {'status': 'error', 'message': f'auth_key 无效: {wx_data.get("error", "unknown")}'}
            except Exception as wx_err:
                wx_status = {'status': 'error', 'message': f'auth_key 验证失败: {wx_err}'}
        health.setdefault('platforms', {})['wechat_exporter'] = wx_status
    except Exception:
        pass

    # v13.0 F52: ASR 配额快照(静默 fallback,查失败不影响整体 /api/health)
    try:
        _q_conn = db.get_conn()
        _q = db.get_asr_usage_today(_q_conn, user_id=0)
        _q_conn.close()
        health['asr_quota'] = {
            'used_hours': _q['used_hours'],
            'remaining_hours': _q['remaining_hours'],
            'over_limit': _q['over_limit'],
            'reset_at': _q['reset_at'],
            'daily_quota_hours': round(_q['daily_quota_sec'] / 3600, 1),
        }
    except Exception as _qe:
        print(f"[health] asr_quota query non-fatal: {_qe}", flush=True)
        # 不设置字段 → 老前端完全兼容

    # Compute overall status + last_check
    from datetime import datetime as _dt
    health['last_check'] = _dt.now().isoformat()
    statuses = [p.get('status', 'unknown') for p in health.get('platforms', {}).values()]
    proxy_status = health.get('proxy', {}).get('status')
    if proxy_status:
        statuses.append(proxy_status)
    if any(s == 'error' for s in statuses):
        health['overall'] = 'error'
    elif any(s == 'warning' for s in statuses):
        health['overall'] = 'warning'
    elif all(s == 'ok' for s in statuses):
        health['overall'] = 'ok'
    else:
        health['overall'] = 'unknown'

    # Log state changes to health_log table
    try:
        _log_conn = db.get_conn()
        for _plat, _pinfo in health.get('platforms', {}).items():
            _new_st = _pinfo.get('status', 'unknown')
            _old_st = _prev_statuses.get(_plat, 'unknown')
            if _new_st != _old_st:
                _log_conn.execute(
                    "INSERT INTO health_log (platform, old_status, new_status, message, source) VALUES (?,?,?,?,?)",
                    (_plat, _old_st, _new_st, _pinfo.get('message', ''), 'api')
                )
        _proxy_new = health.get('proxy', {}).get('status', 'unknown')
        _proxy_old = _prev_statuses.get('_proxy', 'unknown')
        if _proxy_new != _proxy_old:
            _log_conn.execute(
                "INSERT INTO health_log (platform, old_status, new_status, message, source) VALUES (?,?,?,?,?)",
                ('proxy', _proxy_old, _proxy_new, health.get('proxy', {}).get('message', ''), 'api')
            )
        _log_conn.commit()
        _log_conn.close()
    except Exception:
        pass

    # Persist enriched health data back to health.json
    try:
        with open(health_path, 'w') as _hf:
            json.dump(health, _hf, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return health


@router.post('/api/health/sync-credentials')
async def sync_credentials(request: Request):
    error = require_admin(request)
    if error:
        return error
    body = await request.json()
    platform = body.get('platform', '')
    try:
        script = os.path.join(BASE, 'sync_credentials.sh')
        if not os.path.exists(script):
            return {'ok': False, 'message': 'sync_credentials.sh 不存在'}
        env = os.environ.copy()
        env['PATH'] = LOCAL_BIN_PATH + ':' + env.get('PATH', '')
        r = await run_in_threadpool(
            functools.partial(
                subprocess.run,
                ['bash', script, '--extract'],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                cwd=BASE,
            )
        )
        output = r.stdout + r.stderr
        if r.returncode == 0:
            return {'ok': True, 'message': '凭证同步完成'}
        else:
            lines = [l.strip() for l in output.split('\n') if l.strip() and '\u274c' in l]
            msg = lines[0] if lines else f'同步失败 (exit {r.returncode})'
            return {'ok': False, 'message': msg}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'message': '同步超时（30s）'}
    except Exception as e:
        return {'ok': False, 'message': str(e)[:200]}
