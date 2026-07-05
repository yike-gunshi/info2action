"""FastAPI application entry point.

Start: uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
"""
import os
import sys
import time
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

# ── Environment ────────────────────────────────────────────
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'development')
IS_PRODUCTION = ENVIRONMENT == 'production'

logging.basicConfig(
    level=logging.WARNING if IS_PRODUCTION else logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'src'))

# ── Verify external scripts before any import ──────────────
def _verify_script_paths():
    required_scripts = [
        'src/ingest.py', 'src/enrich_items.py',
        'src/generate_summaries.py',
        # v4.0: score_items.py 老 enrichment 入口已废弃,功能由 enrich_items.py 接管;
        # 文件保留作 helper module(_TYPE_DIMENSIONS / _VALID_CONTENT_TYPES / compute_quality_score),
        # 不再作为入口脚本验证。老数据将通过 enrich_items.py 重跑回填。
        'src/fetch_feeds.py', 'src/generate_briefing.py', 'src/generate_actions.py',
        'scripts/probe_ai_provider.py', 'ops/fetch_all.sh',
    ]
    missing = [s for s in required_scripts if not os.path.exists(os.path.join(BASE, s))]
    if missing:
        print(f"FATAL: Missing required scripts:")
        for s in missing:
            print(f"   {s}")
        sys.exit(1)
    print(f"All {len(required_scripts)} external scripts verified")

_verify_script_paths()

import asset_cache
import db
import remote_db
from backend_fetch_scheduler import (
    BackendFetchScheduler, env_enabled, fetch_min_interval_seconds,
    fetch_tick_interval_minutes, seconds_until_next_interval,
)

# ── Route imports ───────────────────────────────────────────
from routes import feed, actions, submit, fetch, config, briefing, interests, health, terminal, context
from routes import auth, admin, user, asr, media
from routes import clusters  # v15.0 event aggregation


# ── Lifespan ────────────────────────────────────────────────
def _sqlite_startup_summary():
    conn = db.get_conn()
    stats = db.get_stats(conn)
    total = sum(s['total'] for s in stats.values()) if stats else 0

    # v13.0 F52: 一次性 migration — v12.3 只存了整块 asr_text_cn,v13 需要 asr_segments_cn。
    # 简化策略(RESEARCH.md §2.4):清空 asr_text_cn 让下次访问时调 translate_segments_cn。
    # 幂等(重复启动时符合条件的行变 0)。
    try:
        cur = conn.execute(
            "UPDATE items SET asr_text_cn = NULL "
            "WHERE asr_segments IS NOT NULL "
            "AND asr_text_cn IS NOT NULL "
            "AND asr_segments_cn IS NULL"
        )
        if cur.rowcount > 0:
            conn.commit()
            print(f'   [v13.0 migration] cleared asr_text_cn for {cur.rowcount} items '
                  f'(will re-translate on next access)')
    except Exception as _me:
        print(f'   [v13.0 migration] skipped: {_me}')
    finally:
        conn.close()

    return {
        'authority': 'local',
        'database': db.DB_PATH,
        'items': total,
    }


def _remote_startup_summary():
    readiness = remote_db.assert_remote_authority_ready()
    storage = remote_db.assert_storage_contract_ready()
    pipeline_write = remote_db.assert_pipeline_write_mode_ready()
    status = remote_db.status()
    counts = status.get('counts') or {}
    return {
        'authority': readiness.get('authority', remote_db.data_authority()),
        'storage_mode': storage.get('mode'),
        'database': f"{status.get('backend', 'remote')}:{status.get('schema', remote_db.remote_schema())}",
        'items': int(counts.get('items') or 0),
        'pipeline_write_mode': pipeline_write.get('mode'),
        'remote_sync_after_pipeline': pipeline_write.get('remote_sync_after_pipeline'),
    }


def _fallback_startup_summary():
    """BF-0515-3: 占位 startup summary，用于 _remote_startup_summary() 失败时降级。

    startup summary 仅作诊断日志输出（"Items in DB: N"），失败不应阻断服务启动。
    """
    try:
        authority = remote_db.data_authority() if remote_db.remote_authority_enabled() else 'local'
    except Exception:
        authority = 'unknown'
    return {
        'authority': authority,
        'storage_mode': None,
        'database': 'unknown (startup summary skipped)',
        'items': 0,
        'pipeline_write_mode': None,
        'remote_sync_after_pipeline': None,
    }


def _env_enabled_compat(primary: str, legacy: str | None = None, *, default: bool = False) -> bool:
    if primary in os.environ:
        return env_enabled(primary, default=default)
    if legacy and legacy in os.environ:
        return env_enabled(legacy, default=default)
    return env_enabled(primary, default=default)


def _remote_prewarm_plan() -> dict[str, bool]:
    return {
        'platforms': _env_enabled_compat(
            'INFO2ACTION_PREWARM_PLATFORMS',
            'INFO2ACTION_PLATFORMS_CACHE_PREWARM',
            default=True,
        ),
        'events': _env_enabled_compat(
            'INFO2ACTION_PREWARM_EVENTS',
            'INFO2ACTION_EVENTS_CACHE_PREWARM',
            default=False,
        ),
        'posters': _env_enabled_compat(
            'INFO2ACTION_PREWARM_POSTERS',
            'INFO2ACTION_POSTER_CACHE_PREWARM',
            default=False,
        ),
    }


def _cache_prewarm_interval_sec() -> int:
    try:
        value = int(os.environ.get('INFO2ACTION_CACHE_PREWARM_INTERVAL_SEC', '600'))
    except ValueError:
        value = 600
    return max(30, value)


def _dynamic_fetch_tick_seconds() -> float:
    try:
        value = float(os.environ.get('INFO2ACTION_DYNAMIC_FETCH_TICK_SECONDS', '60'))
    except ValueError:
        value = 60.0
    return max(10.0, value)


def _remote_db_pressure_skip_reason() -> str | None:
    if not remote_db.remote_authority_enabled():
        return None
    try:
        probe = remote_db.remote_db_pressure()
    except Exception as exc:
        return f"remote_db_pressure_probe_failed:{str(exc)[:80]}"
    if not probe.get('pressure'):
        return None
    reasons = probe.get('reasons') or ['unknown']
    return f"remote_db_pressure:{','.join(str(reason) for reason in reasons)}"


def _run_remote_cache_prewarm_iteration(iteration: int) -> None:
    if fetch.has_local_active_fetch_runs():
        print(f"[prewarm #{iteration}] skipped: fetch running", flush=True)
        return
    pressure_reason = _remote_db_pressure_skip_reason()
    if pressure_reason:
        print(f"[prewarm #{iteration}] skipped: {pressure_reason}", flush=True)
        return
    plan = _remote_prewarm_plan()
    if plan['platforms']:
        try:
            t0 = time.time()
            platforms_result = remote_db.prewarm_platforms()
            print(f"[prewarm #{iteration} platforms] {platforms_result}, took={int((time.time()-t0)*1000)}ms", flush=True)
        except Exception as exc:
            print(f"[prewarm #{iteration} platforms] FAILED: {exc}", flush=True)
    else:
        print(f"[prewarm #{iteration} platforms] skipped", flush=True)

    if plan['events']:
        try:
            t0 = time.time()
            events_result = remote_db.prewarm_events_categories()
            ok_str = f"{events_result['success']}/{events_result['success']+events_result['failed']}"
            print(f"[prewarm #{iteration} events] success={ok_str}, took={int((time.time()-t0)*1000)}ms", flush=True)
        except Exception as exc:
            print(f"[prewarm #{iteration} events] FAILED: {exc}", flush=True)
    else:
        print(f"[prewarm #{iteration} events] skipped", flush=True)

    if plan['posters']:
        try:
            from routes import media as _media
            t0 = time.time()
            poster_result = _media.prewarm_recent_twitter_video_posters(limit=10)
            print(f"[prewarm #{iteration} posters] {poster_result}, took={int((time.time()-t0)*1000)}ms", flush=True)
        except Exception as exc:
            print(f"[prewarm #{iteration} posters] FAILED: {exc}", flush=True)
    else:
        print(f"[prewarm #{iteration} posters] skipped", flush=True)


# ── P0-1(C 端放量):多 worker 安全的 leader 锁 ──────────────
# 后台单例职责(fetch 调度、fetch 恢复、prewarm、tmux 恢复)只能有一个
# 进程执行。单 worker 时锁必然拿到,行为不变;INFO2ACTION_WEB_WORKERS>1
# 时只有 leader 跑这些,其余 worker 纯服务请求(进程内缓存靠共享的
# local read cache 兜底)。锁文件句柄持有至进程退出。
_web_leader_lock_handle = None


def _acquire_web_leader_lock() -> bool:
    global _web_leader_lock_handle
    if _web_leader_lock_handle is not None:
        return True
    try:
        import fcntl
    except ImportError:
        return True  # 非 POSIX 平台退化为单例假设(默认 workers=1)
    lock_path = os.environ.get(
        'INFO2ACTION_WEB_LEADER_LOCK',
        os.path.join(BASE, 'data', 'web-leader.lock'),
    )
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, 'w')
    except OSError as exc:
        # 锁文件建不出来 ≠ 有别的 leader;宁可假定单 worker 保住调度。
        print(f'   Leader lock unavailable ({exc}); assuming single worker')
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _web_leader_lock_handle = handle
    return True


def _threadpool_tokens() -> int:
    raw = (os.environ.get('INFO2ACTION_THREADPOOL_TOKENS') or '').strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    fetch_scheduler = None
    dynamic_fetch_scheduler = None

    # P0-1: 同步路由跑在 anyio 线程池(默认 40 tokens),100 并发下这是
    # 与 DB 连接池并列的瓶颈之一;仅当显式配置时才调整。
    tokens = _threadpool_tokens()
    if tokens > 0:
        try:
            import anyio.to_thread
            anyio.to_thread.current_default_thread_limiter().total_tokens = tokens
            print(f'   Threadpool: {tokens} tokens')
        except Exception as exc:
            print(f'   Threadpool resize skipped: {exc}')

    is_scheduler_leader = _acquire_web_leader_lock()
    if not is_scheduler_leader:
        print('   Scheduler role: follower (leader lock held by another worker)')
    startup_summary_timeout = float(os.environ.get('INFO2ACTION_STARTUP_SUMMARY_TIMEOUT_SEC', '8'))
    try:
        if remote_db.remote_authority_enabled() and not env_enabled('INFO2ACTION_STARTUP_SUMMARY', default=True):
            startup = _fallback_startup_summary()
        elif remote_db.remote_authority_enabled():
            startup = await asyncio.wait_for(
                asyncio.to_thread(_remote_startup_summary),
                timeout=startup_summary_timeout,
            )
        else:
            startup = _sqlite_startup_summary()
    except asyncio.TimeoutError:
        logging.getLogger(__name__).warning(
            "Startup summary timed out after %.1fs. Service will continue without startup summary; "
            "check /api/health for runtime status.",
            startup_summary_timeout,
        )
        startup = _fallback_startup_summary()
    except Exception as exc:
        # BF-0515-3: remote status() 在 transaction pooler 上可能撞 statement_timeout (120s)
        # 因 items 表 count(*) 全表扫描超时。startup summary 仅作诊断日志，失败不应阻断 lifespan。
        # health 路由有独立 try/except RemoteDBError fallback，运行时仍可用。
        logging.getLogger(__name__).warning(
            "Startup summary failed (likely transaction pooler statement_timeout on remote count): %s. "
            "Service will continue without startup summary; check /api/health for runtime status.",
            exc,
        )
        startup = _fallback_startup_summary()
    port = int(os.environ.get('PORT', 8080))
    print(f'Info Radar (FastAPI) -> http://localhost:{port}')
    print(f"   Data authority: {startup['authority']}")
    if startup.get('storage_mode'):
        print(f"   Storage mode: {startup['storage_mode']}")
    print(f"   Database: {startup['database']}")
    print(f"   Items in DB: {startup['items']}")
    if startup.get('pipeline_write_mode'):
        sync_state = 'on' if startup.get('remote_sync_after_pipeline') else 'off'
        print(f"   Pipeline writes: {startup['pipeline_write_mode']} (remote sync: {sync_state})")

    # Recover tmux sessions
    if is_scheduler_leader:
        terminal.recover_tmux_sessions()

    if is_scheduler_leader:
        try:
            recovered_runs = fetch.recover_orphaned_fetch_runs_from_previous_process()
            if recovered_runs:
                print(f"   Fetch recovery: marked interrupted orphaned runs {recovered_runs}")
        except Exception as exc:
            print(f"   Fetch recovery skipped: {exc}", flush=True)

    if is_scheduler_leader and env_enabled('INFO2ACTION_BACKEND_HOURLY_FETCH'):
        fetch_min_interval = fetch_min_interval_seconds()
        fetch_start_with_cooldown = env_enabled('INFO2ACTION_BACKEND_FETCH_START_WITH_COOLDOWN')
        fetch_tick_minutes = fetch_tick_interval_minutes()
        fetch_scheduler = BackendFetchScheduler(
            fetch.start_global_fetch,
            should_start=lambda: not fetch.has_active_fetch_runs(),
            sleep_until_next_tick=lambda: seconds_until_next_interval(fetch_tick_minutes),
            min_interval_seconds=fetch_min_interval,
            start_with_cooldown=fetch_start_with_cooldown,
        )
        fetch_scheduler.start()
        _tick_marks = ','.join(f'{m:02d}' for m in range(0, 60, fetch_tick_minutes))
        print(
            '   Backend fetch scheduler: enabled '
            f'(minute={_tick_marks}; min_interval={fetch_min_interval / 60:.0f}m; '
            f'start_cooldown={fetch_start_with_cooldown})'
        )
    else:
        print('   Backend fetch scheduler: disabled')

    if is_scheduler_leader and env_enabled('INFO2ACTION_DYNAMIC_FETCH_ENABLED'):
        dynamic_fetch_tick_seconds = _dynamic_fetch_tick_seconds()
        dynamic_fetch_scheduler = BackendFetchScheduler(
            fetch.start_dynamic_micro_fetch,
            should_start=lambda: not fetch.has_active_fetch_runs(),
            sleep_until_next_tick=lambda: dynamic_fetch_tick_seconds,
        )
        dynamic_fetch_scheduler.start()
        print(
            '   Dynamic micro fetch scheduler: enabled '
            f'(tick={dynamic_fetch_tick_seconds:.0f}s)'
        )
    else:
        print('   Dynamic micro fetch scheduler: disabled')

    # BF-0515-mv-pgcron + BF-0515-prewarm-renew: prewarm critical caches in
    # background so first user after backend restart hits warm cache (~47ms)
    # instead of cold path (~3.7s). Re-prewarm defaults to every 600 seconds
    # to keep steady-state I/O lower on small Supabase projects.
    #
    # Daemon thread does not block startup; in-flight requests during the
    # very first prewarm window (~10s post-restart) may pay cold cost.
    prewarm_stop_event = None
    if is_scheduler_leader and remote_db.remote_authority_enabled() and env_enabled('INFO2ACTION_CACHE_PREWARM', default=True):
        import threading as _threading
        prewarm_stop_event = _threading.Event()
        prewarm_interval_sec = _cache_prewarm_interval_sec()
        def _prewarm_loop():
            iteration = 0
            while not prewarm_stop_event.is_set():
                iteration += 1
                _run_remote_cache_prewarm_iteration(iteration)
                # Wait interval (interruptible). First sleep after iteration 1.
                if prewarm_stop_event.wait(prewarm_interval_sec):
                    break

        _threading.Thread(target=_prewarm_loop, daemon=True, name='prewarm-renew-loop').start()
        plan = _remote_prewarm_plan()
        print(
            f"   Cache prewarm: scheduled (every {prewarm_interval_sec}s; "
            f"platforms={'on' if plan['platforms'] else 'off'}, "
            f"events={'on' if plan['events'] else 'off'}, "
            f"posters={'on' if plan['posters'] else 'off'})"
        )
    elif remote_db.remote_authority_enabled():
        print('   Cache prewarm: disabled')

    try:
        yield
    finally:
        if fetch_scheduler:
            fetch_scheduler.stop()
        if dynamic_fetch_scheduler:
            dynamic_fetch_scheduler.stop()
        try:
            fetch_shutdown_grace_sec = int(os.environ.get('INFO2ACTION_FETCH_SHUTDOWN_GRACE_SEC', '7200'))
        except ValueError:
            fetch_shutdown_grace_sec = 7200
        fetch_shutdown_grace_sec = max(0, fetch_shutdown_grace_sec)
        try:
            if fetch_shutdown_grace_sec and fetch.has_local_active_fetch_runs():
                print(
                    f"   Fetch shutdown: waiting up to {fetch_shutdown_grace_sec}s for active runs to finish",
                    flush=True,
                )
                if fetch.wait_for_active_fetch_runs_to_finish(fetch_shutdown_grace_sec):
                    print("   Fetch shutdown: active runs finished before service exit", flush=True)
                else:
                    print("   Fetch shutdown: grace period expired; marking active runs interrupted", flush=True)
        except Exception as exc:
            print(f"   Fetch shutdown wait skipped: {exc}", flush=True)
        try:
            interrupted_runs = fetch.interrupt_active_fetch_runs_for_shutdown()
            if interrupted_runs:
                print(f"   Fetch shutdown: marked interrupted active runs {interrupted_runs}", flush=True)
        except Exception as exc:
            print(f"   Fetch shutdown recovery skipped: {exc}", flush=True)
        if prewarm_stop_event:
            prewarm_stop_event.set()


# ── App ─────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

# CORS — restrict origins in production
_default_origins = 'http://localhost:5173,http://localhost:8080,http://localhost:3000'
_cors_origins = os.environ.get('CORS_ORIGINS', _default_origins).split(',')
_cors_origins = [o.strip() for o in _cors_origins if o.strip()]
if IS_PRODUCTION and _cors_origins == _default_origins.split(','):
    logging.getLogger(__name__).warning(
        "CORS_ORIGINS not set in production — using localhost defaults. "
        "Set CORS_ORIGINS env var to your production domain(s)."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


def _limiter_default_limits() -> list[str]:
    """RATELIMIT_DEFAULT 允许运维调全局默认限额而不发版。"""
    return [(os.environ.get('RATELIMIT_DEFAULT') or '100/minute').strip()]


# P1-5(C 端放量):限流计数默认在进程内存——多 worker/多机后各进程独立
# 计数,限流形同虚设。共享存储用 slowapi 原生 env(所有 Limiter 实例在
# import 时各自读取,包括 routes/auth.py:24 的第二个实例——不要在这里传
# storage_uri kwargs,那只覆盖本实例):
#   RATELIMIT_STORAGE_URL=redis://127.0.0.1:6379
#   RATELIMIT_IN_MEMORY_FALLBACK_ENABLED=true   # Redis 挂了退化为进程内
#   RATELIMIT_ENABLED=false                     # 测试关闭限流(须在 import 前)
limiter = Limiter(key_func=get_remote_address, default_limits=_limiter_default_limits())
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# AUTH_TOKEN middleware
from middleware.auth import AuthTokenMiddleware
app.add_middleware(AuthTokenMiddleware)

# ── API routes ──────────────────────────────────────────────
app.include_router(feed.router)
app.include_router(actions.router)
app.include_router(submit.router)
app.include_router(fetch.router)
app.include_router(config.router)
app.include_router(briefing.router)
app.include_router(interests.router)
app.include_router(health.router)
app.include_router(terminal.router)
app.include_router(context.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(user.router)
app.include_router(asr.router)
app.include_router(media.router)
app.include_router(clusters.router)  # v15.0 /api/feed/events, /api/clusters/..., /api/search

# ── v12.2 ASR state: Semaphore(3) per user + SSE event buses per item ──
app.state.user_asr_sems = {}  # type: ignore[assignment]
app.state.asr_event_buses = {}  # type: ignore[assignment]

# ── v12.2 ASR credentials fail-fast check ──
_ASR_REQUIRED_ENV = [
    "DOUBAO_ASR_API_KEY",
    "ALIYUN_OSS_AK",
    "ALIYUN_OSS_SK",
]
_missing_asr_env = [k for k in _ASR_REQUIRED_ENV if not os.environ.get(k)]
if _missing_asr_env:
    import sys as _sys
    print(f"[asr] ⚠️  missing env vars: {_missing_asr_env}. ASR endpoints will fail until set.",
          file=_sys.stderr)


# ── Static files & SPA fallback ─────────────────────────────

# Content-type map for assets
_CT_MAP = {
    '.js': 'application/javascript', '.css': 'text/css',
    '.svg': 'image/svg+xml', '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.woff2': 'font/woff2', '.woff': 'font/woff',
    '.webp': 'image/webp', '.gif': 'image/gif', '.avif': 'image/avif',
    '.json': 'application/manifest+json',
}


def _safe_file_under(root: str, path: str) -> str | None:
    """Resolve path under root and reject traversal/symlink escapes."""
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, path))
    try:
        if os.path.commonpath([root_real, target]) != root_real:
            return None
    except ValueError:
        return None
    if not os.path.isfile(target):
        return None
    return target


@app.get("/lw/{entry_id}")
async def lingowhale_article(entry_id: str):
    """Serve cached Lingowhale WeChat article HTML."""
    if not all(c.isalnum() or c == '_' for c in entry_id):
        return Response(status_code=400)
    if remote_db.asset_storage_to_remote():
        object_path = f"lingowhale/html/{entry_id}.html"
        data = await run_in_threadpool(
            asset_cache.get_or_fetch,
            object_path,
            lambda: remote_db.download_asset_bytes_remote(object_path),
        )
        if data:
            return Response(
                content=data,
                media_type='text/html; charset=utf-8',
                headers={'Cache-Control': 'public, max-age=86400'},
            )
        return Response(status_code=404)
    html_path = os.path.join(BASE, 'data', 'lingowhale', 'html', f'{entry_id}.html')
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type='text/html; charset=utf-8')
    return Response(status_code=404)


@app.get("/images/{path:path}")
async def serve_image(path: str):
    """Serve localized images from data/images/."""
    if remote_db.asset_storage_to_remote():
        object_path = f"images/{path}"
        try:
            data = await run_in_threadpool(
                asset_cache.get_or_fetch,
                object_path,
                lambda: remote_db.download_asset_bytes_remote(object_path),
            )
        except remote_db.RemoteDBConfigError:
            return Response(status_code=404)
        if data:
            ext = os.path.splitext(path)[1].lower()
            ct = _CT_MAP.get(ext, 'application/octet-stream')
            return Response(content=data, media_type=ct, headers={
                'Cache-Control': 'public, max-age=86400',
            })
        return Response(status_code=404)
    img_path = _safe_file_under(os.path.join(BASE, 'data', 'images'), path)
    if img_path:
        ext = os.path.splitext(img_path)[1].lower()
        ct = _CT_MAP.get(ext, 'application/octet-stream')
        return FileResponse(img_path, media_type=ct, headers={
            'Cache-Control': 'public, max-age=86400',
        })
    return Response(status_code=404)


@app.get("/assets/{path:path}")
async def serve_assets(path: str):
    """Serve React build static assets with immutable cache."""
    asset_path = _safe_file_under(os.path.join(BASE, 'frontend-react', 'dist', 'assets'), path)
    if asset_path:
        ext = os.path.splitext(asset_path)[1].lower()
        ct = _CT_MAP.get(ext, 'application/octet-stream')
        return FileResponse(asset_path, media_type=ct, headers={
            'Cache-Control': 'public, max-age=31536000, immutable',
        })
    return Response(status_code=404)


@app.get("/favicon.svg")
@app.get("/favicon-32.png")
@app.get("/icon-192.svg")
@app.get("/icon-192.png")
@app.get("/icon-512.svg")
@app.get("/icon-512.png")
@app.get("/manifest.json")
@app.get("/sw.js")
async def serve_root_static(request: Request):
    """Serve root-level app shell assets that Vite copies outside /assets."""
    filename = request.url.path.lstrip("/")
    for root in (
        os.path.join(BASE, 'frontend-react', 'dist'),
        os.path.join(BASE, 'frontend'),
    ):
        static_path = _safe_file_under(root, filename)
        if static_path:
            ext = os.path.splitext(static_path)[1].lower()
            ct = _CT_MAP.get(ext, 'application/octet-stream')
            cache_control = 'no-cache'
            return FileResponse(static_path, media_type=ct, headers={
                'Cache-Control': cache_control,
            })
    return Response(status_code=404)


# SPA fallback — must be LAST
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """Serve React SPA index.html for all non-API routes."""
    react_index = os.path.join(BASE, 'frontend-react', 'dist', 'index.html')
    if os.path.exists(react_index):
        return FileResponse(react_index, media_type='text/html',
                            headers={'Cache-Control': 'no-cache'})
    # Fallback to legacy frontend
    legacy = os.path.join(BASE, 'frontend', 'dashboard.html')
    if os.path.exists(legacy):
        return FileResponse(legacy, media_type='text/html',
                            headers={'Cache-Control': 'no-cache'})
    return Response(content="No frontend build found", status_code=404)


# ── Main (direct run) ──────────────────────────────────────
if __name__ == '__main__':
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    os.environ['PORT'] = str(port)
    # P0-1: worker 数可配。>1 时 leader 锁保证后台单例职责不重复;
    # 注意 ASR SSE 与内存限流在多 worker 下按进程割裂,开多 worker 前
    # 先落地 Redis 限流(P1-5)并评估 ASR 影响。
    try:
        workers = max(1, int(os.environ.get('INFO2ACTION_WEB_WORKERS', '1')))
    except ValueError:
        workers = 1
    uvicorn.run('app:app', host='0.0.0.0', port=port, workers=workers)
