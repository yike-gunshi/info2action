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


# ── Module-level state ──────────────────────────────────────
_fetch_lock = threading.Lock()
_fetch_running = False
_fetch_finished_at = None
_fetch_progress = {'stages': [], 'current_stage': 0, 'total_new': 0}
_fetch_active_runs = {}
_remote_last_fetch_cache = {'ts': 0.0, 'data': None}
_dynamic_micro_last_started = {}
_REMOTE_LAST_FETCH_TTL_SEC = 5
_DEFAULT_DYNAMIC_MICRO_FETCH_SOURCES = 'twitter:following:5,twitter:for_you:5'
_fetch_process_started_at = datetime.now(timezone.utc)
_fetch_process_owner = f"{socket.gethostname()}:{os.getpid()}:{int(_fetch_process_started_at.timestamp())}"


GLOBAL_FETCH_STAGE_DEFS = [
    ('source_fetch', '抓取来源', 0),
    ('ingest', '入库处理', 35),
    ('ai_enrich', 'AI 统一理解', 50),
    ('event_cluster', '事件聚合', 80),
]

MICRO_FETCH_STAGE_DEFS = [
    ('source_fetch', '抓取来源', 0),
    ('ingest', '入库处理', 35),
    ('ai_enrich', 'AI 统一理解', 55),
    ('event_cluster', '事件聚合', 80),
]

FETCH_LOG_MARKERS = [
    ('Twitter', 'source_fetch', 'X', 8),
    ('小红书', 'source_fetch', '小红书', 16),
    ('B站', 'source_fetch', 'B站', 24),
    ('公众号', 'source_fetch', '公众号', 28),
    ('RSS / HN / Reddit / GitHub', 'source_fetch', 'RSS/HN/Reddit/GitHub', 31),
    ('WayToAGI', 'source_fetch', 'waytoagi', 34),
    ('抓取完成! 开始入库', 'ingest', '全部平台', 38),
    ('AI 统一理解', 'ai_enrich', '全部平台', 55),
    ('事件聚合 (', 'event_cluster', '全部平台', 85),
    ('全部完成', 'event_cluster', '全部平台', 100),
]

AI_ENRICH_PERCENT_START = 55
AI_ENRICH_PERCENT_END = 80
AI_ENRICH_FOUND_RE = re.compile(r'Found\s+(\d+)\s+items to enrich')
AI_ENRICH_PROGRESS_RE = re.compile(
    r'^\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]\s+platform=([^\s]+)',
    re.MULTILINE,
)
AI_ENRICH_DONE_RE = re.compile(r'Done!\s+enriched=(\d+),\s*errors=(\d+)')
AI_REMOTE_DB_FAILURE_RE = re.compile(
    r'remote_db_transient_exhausted|EDBHANDLEREXITED|'
    r'Remote DB connection/query failed|connection to database closed|'
    r'pool checkout timeout',
    re.IGNORECASE,
)
AI_REMOTE_DB_FAILURE_MESSAGE = 'Supabase 连接异常，AI 队列读取/写入失败'


def _active_provider_message(*providers):
    for provider in providers or (
        ai_provider_guard.MINIMAX_CHAT_PROVIDER,
    ):
        try:
            if ai_provider_guard.is_action_required(provider) or ai_provider_guard.is_cooldown_active(provider):
                return ai_provider_guard.provider_message(provider)
        except Exception:
            continue
    return None


def _make_global_fetch_progress():
    return {
        'mode': 'global',
        'stages': [
            {'id': stage_id, 'name': name, 'status': 'pending', 'platform': '全部平台', 'percent': percent, 'new_count': 0}
            for stage_id, name, percent in GLOBAL_FETCH_STAGE_DEFS
        ],
        'current_stage': 0,
        'total_new': 0,
        'platform': '全部平台',
        'percent': 0,
        'result_status': 'running',
        'message': '',
    }


def _micro_fetch_run_source(platform, source):
    normalized_source = source or 'all'
    return f'micro:{platform}:{normalized_source}'


def _micro_fetch_thread_name(platform, source):
    safe_source = (source or 'all').replace(':', '-').replace('/', '-').replace('_', '-')
    safe_platform = (platform or 'unknown').replace(':', '-').replace('/', '-').replace('_', '-')
    return f'info2action-micro-fetch-{safe_platform}-{safe_source}'


def _make_micro_fetch_progress(platform, source, *, run_id=None, run_source=None):
    source_name = source or 'all'
    label = f'{platform}/{source_name}'
    return {
        'mode': 'micro',
        'stages': [
            {'id': stage_id, 'name': name, 'status': 'pending', 'platform': label, 'percent': percent, 'new_count': 0}
            for stage_id, name, percent in MICRO_FETCH_STAGE_DEFS
        ],
        'current_stage': 0,
        'total_new': 0,
        'platform': platform,
        'source_name': source_name,
        'source': run_source or _micro_fetch_run_source(platform, source),
        'percent': 0,
        'result_status': 'running',
        'message': '',
        **({'run_id': run_id} if run_id is not None else {}),
    }


def _max_global_fetch_pipelines():
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_MAX_FETCH_PIPELINES'),
        1,
        minimum=1,
    )


def _micro_enrich_workers() -> int:
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_MICRO_ENRICH_WORKERS'),
        3,
        minimum=1,
    )


def _dynamic_micro_fetch_sources():
    raw = os.environ.get('INFO2ACTION_DYNAMIC_FETCH_SOURCES', _DEFAULT_DYNAMIC_MICRO_FETCH_SOURCES)
    specs = []
    for entry in raw.split(','):
        text = entry.strip()
        if not text or ':' not in text:
            continue
        source_part, interval_raw = text.rsplit(':', 1)
        if ':' not in source_part:
            continue
        platform, source = source_part.split(':', 1)
        platform = platform.strip()
        source = source.strip()
        try:
            interval_minutes = float(interval_raw)
        except ValueError:
            continue
        if not platform or not source or interval_minutes <= 0:
            continue
        specs.append({
            'platform': platform,
            'source': source,
            'interval_minutes': interval_minutes,
        })
    return specs


def _utc_now():
    return datetime.now(timezone.utc)


def _coerce_utc_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_micro_fetch_started_at_current_backend(platform, source):
    source_name = source or 'all'
    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as conn:
            row = conn.execute(
                f"""SELECT started_at
                      FROM {remote_db.remote_schema()}.fetch_runs
                     WHERE stats_json->>'_pipeline_mode' = 'micro'
                       AND stats_json->'_micro_source'->>'platform' = %s
                       AND COALESCE(NULLIF(stats_json->'_micro_source'->>'source', ''), 'all') = %s
                     ORDER BY started_at DESC
                     LIMIT 1""",
                (platform, source_name),
            ).fetchone()
    else:
        conn = db.get_conn()
        try:
            row = conn.execute(
                """SELECT started_at
                     FROM fetch_runs
                    WHERE json_extract(stats_json, '$._pipeline_mode') = 'micro'
                      AND json_extract(stats_json, '$._micro_source.platform') = ?
                      AND COALESCE(NULLIF(json_extract(stats_json, '$._micro_source.source'), ''), 'all') = ?
                    ORDER BY datetime(started_at) DESC
                    LIMIT 1""",
                (platform, source_name),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    value = row.get('started_at') if hasattr(row, 'get') else row[0]
    return _coerce_utc_datetime(value)


def start_dynamic_micro_fetch(source: str = 'dynamic_micro_scheduler') -> dict:
    specs = _dynamic_micro_fetch_sources()
    if not specs:
        return {
            'ok': False,
            'msg': 'Dynamic micro fetch skipped (no sources configured)',
            'skip_reason': 'dynamic_micro_no_sources',
        }
    now = time.monotonic()
    now_utc = _utc_now()
    for spec in specs:
        key = f"{spec['platform']}:{spec['source']}"
        interval_seconds = spec['interval_minutes'] * 60
        last_started = _dynamic_micro_last_started.get(key)
        if last_started is not None and now - last_started < interval_seconds:
            continue
        try:
            persisted_started = _latest_micro_fetch_started_at_current_backend(
                spec['platform'],
                spec['source'],
            )
        except Exception as exc:
            print(f"[fetch] dynamic micro cooldown lookup failed for {key}: {exc}", flush=True)
            persisted_started = None
        if persisted_started is not None:
            age_seconds = max(0.0, (now_utc - persisted_started).total_seconds())
            if age_seconds < interval_seconds:
                _dynamic_micro_last_started[key] = now - age_seconds
                continue
        result = start_source_micro_fetch(spec['platform'], spec['source'])
        result['platform'] = spec['platform']
        result['source_name'] = spec['source']
        result['dynamic_scheduler_source'] = source
        if result.get('ok'):
            _dynamic_micro_last_started[key] = now
        return result
    return {
        'ok': False,
        'msg': 'Dynamic micro fetch skipped (no due source)',
        'skip_reason': 'dynamic_micro_no_due_source',
    }


def _platform_prewarm_enabled() -> bool:
    if 'INFO2ACTION_PREWARM_PLATFORMS' in os.environ:
        return _env_enabled('INFO2ACTION_PREWARM_PLATFORMS', default=True)
    if 'INFO2ACTION_PLATFORMS_CACHE_PREWARM' in os.environ:
        return _env_enabled('INFO2ACTION_PLATFORMS_CACHE_PREWARM', default=True)
    return True


def _info_read_model_refresh_min_interval_sec() -> int:
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_INFO_READ_MODEL_REFRESH_MIN_INTERVAL_SEC'),
        600,
        minimum=0,
    )


def _highlights_read_model_refresh_min_interval_sec() -> int:
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC'),
        600,
        minimum=0,
    )


def _micro_highlights_read_model_refresh_min_interval_sec() -> int:
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC'),
        120,
        minimum=0,
    )


def _backend_fetch_finish_gap_minutes() -> int:
    return _coerce_positive_int(
        os.environ.get('INFO2ACTION_BACKEND_FETCH_FINISH_GAP_MINUTES'),
        30,
        minimum=0,
    )


def _format_remote_pressure_reason(probe: dict) -> str:
    reasons = probe.get('reasons') or []
    if reasons:
        return ','.join(str(reason) for reason in reasons)
    if probe.get('error'):
        return f"error:{str(probe.get('error'))[:80]}"
    return 'unknown'


def _remote_db_pressure_skip_reason() -> str | None:
    if not (remote_db.fetch_write_to_remote() or remote_db.remote_authority_enabled()):
        return None
    try:
        probe = remote_db.remote_db_pressure()
    except Exception as exc:
        print(f"[fetch] remote pressure probe failed closed: {exc}", flush=True)
        return "remote_db_pressure_probe_failed"
    if probe.get('pressure'):
        return f"remote_db_pressure:{_format_remote_pressure_reason(probe)}"
    return None


def _scheduler_finish_gap_skip_reason(source: str) -> str | None:
    if source != 'backend_30min_cron':
        return None
    gap_minutes = _backend_fetch_finish_gap_minutes()
    if gap_minutes <= 0 or not remote_db.fetch_write_to_remote():
        return None
    try:
        if remote_db.has_recent_finished_fetch_remote(minutes=gap_minutes):
            return f"remote_fetch_finish_gap:{gap_minutes}m"
    except Exception as exc:
        print(f"[fetch] remote finish-gap guard failed closed: {exc}", flush=True)
        return "remote_fetch_finish_gap_unavailable"
    return None


def has_local_active_fetch_runs() -> bool:
    with _fetch_lock:
        return bool(_fetch_active_runs)


def has_active_fetch_runs() -> bool:
    if has_local_active_fetch_runs():
        return True
    if remote_db.fetch_write_to_remote():
        try:
            recovered_runs = recover_stale_remote_fetch_runs()
            if recovered_runs:
                print(f"[fetch] recovered stale remote running runs {recovered_runs}", flush=True)
            return remote_db.has_recent_running_fetch_remote()
        except remote_db.RemoteDBError as exc:
            print(f"[fetch] remote running-run guard failed closed: {exc}", flush=True)
            return True
        except Exception as exc:
            print(f"[fetch] remote running-run guard failed closed: {exc}", flush=True)
            return True
    return False


def recover_orphaned_fetch_runs_from_previous_process(started_before=None) -> list[int]:
    if not remote_db.fetch_write_to_remote():
        return []
    if not _fetch_orphan_recovery_enabled():
        return []
    cutoff = started_before or _fetch_process_started_at
    reason = (
        "previous backend process stopped before finishing this fetch run; "
        "marked interrupted during startup recovery so scheduling can resume"
    )
    heartbeat_stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=remote_db.fetch_run_heartbeat_grace_seconds()
    )
    return remote_db.mark_orphaned_fetch_runs_remote(
        started_before=cutoff,
        heartbeat_stale_before=heartbeat_stale_before,
        reason=reason,
    )


def recover_stale_remote_fetch_runs() -> list[int]:
    if not remote_db.fetch_write_to_remote():
        return []
    if not _fetch_orphan_recovery_enabled():
        return []
    now = datetime.now(timezone.utc)
    reason = (
        "remote fetch run heartbeat expired before scheduler/start guard; "
        "marked interrupted so scheduling can resume"
    )
    heartbeat_stale_before = now - timedelta(
        seconds=remote_db.fetch_run_heartbeat_grace_seconds()
    )
    return remote_db.mark_orphaned_fetch_runs_remote(
        started_before=now,
        heartbeat_stale_before=heartbeat_stale_before,
        reason=reason,
    )


def _fetch_orphan_recovery_enabled() -> bool:
    # Only the authoritative scheduler process should clean up remote running runs.
    # Local dev backends may point at production Supabase for reads, but they do
    # not know whether a production ECS fetch is still actively running.
    if 'INFO2ACTION_FETCH_ORPHAN_RECOVERY' in os.environ:
        return _env_enabled('INFO2ACTION_FETCH_ORPHAN_RECOVERY', default=True)
    return _env_enabled('INFO2ACTION_BACKEND_HOURLY_FETCH', default=False)


def _fetch_heartbeat_interval_sec() -> float:
    return max(
        5.0,
        float(_coerce_positive_int(
            os.environ.get('INFO2ACTION_FETCH_RUN_HEARTBEAT_INTERVAL_SEC'),
            60,
            minimum=5,
        )),
    )


def _touch_fetch_run_heartbeat(run_id: int) -> None:
    if run_id is None:
        return
    if not remote_db.fetch_write_to_remote():
        return
    remote_db.touch_fetch_run_heartbeat_remote(
        run_id=run_id,
        owner=_fetch_process_owner,
    )


def _start_fetch_run_heartbeat(run_id: int):
    if run_id is None:
        return None
    if not remote_db.fetch_write_to_remote():
        return None
    stop_event = threading.Event()
    interval_sec = _fetch_heartbeat_interval_sec()

    def _heartbeat_loop():
        while not stop_event.is_set():
            try:
                _touch_fetch_run_heartbeat(run_id)
            except Exception as exc:
                print(f"[fetch] heartbeat update failed for run #{run_id}: {exc}", flush=True)
            if stop_event.wait(interval_sec):
                break

    thread = threading.Thread(
        target=_heartbeat_loop,
        daemon=True,
        name=f'fetch-run-heartbeat-{run_id}',
    )
    thread.start()
    return stop_event


def _mark_progress_interrupted(progress, reason):
    if not isinstance(progress, dict):
        return
    for stage in progress.get('stages', []):
        if stage.get('status') == 'running':
            stage['status'] = 'failed'
            stage['message'] = reason[:120]
            break
    progress['result_status'] = 'interrupted'
    progress['message'] = reason


def interrupt_active_fetch_runs_for_shutdown(reason=None) -> list[int]:
    """Mark in-process fetch runs interrupted before the backend exits."""
    global _fetch_running, _fetch_finished_at, _fetch_progress
    interrupted_reason = reason or (
        "backend service stopped while this fetch run was active; "
        "marked interrupted during shutdown so scheduling can resume"
    )
    with _fetch_lock:
        active_run_ids = sorted(int(run_id) for run_id in _fetch_active_runs.keys())
        finished_progress = None
        for run_id in active_run_ids:
            active = _fetch_active_runs.get(run_id) or {}
            progress = active.get('progress')
            _mark_progress_interrupted(progress, interrupted_reason)
            if progress is not None:
                finished_progress = copy.deepcopy(progress)
        if active_run_ids:
            _fetch_active_runs.clear()
            if finished_progress is not None:
                _fetch_progress = finished_progress
            else:
                _mark_progress_interrupted(_fetch_progress, interrupted_reason)
            _fetch_running = False
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()

    if not active_run_ids:
        return []

    if remote_db.fetch_write_to_remote():
        try:
            return remote_db.mark_fetch_runs_interrupted_remote(
                run_ids=active_run_ids,
                reason=interrupted_reason,
            )
        except Exception as exc:
            print(f"[fetch] shutdown interruption marker failed: {exc}", flush=True)
            return active_run_ids

    for run_id in active_run_ids:
        try:
            _finish_fetch_run_current_backend(
                run_id,
                {'_result_status': 'interrupted', '_interrupted_reason': interrupted_reason},
                interrupted_reason,
            )
        except Exception as exc:
            print(f"[fetch] local shutdown interruption marker failed for run #{run_id}: {exc}", flush=True)
    return active_run_ids


def wait_for_active_fetch_runs_to_finish(timeout_sec: float, *, poll_interval_sec: float = 5.0) -> bool:
    """Wait until in-process fetch runs finish, returning False on timeout."""
    timeout = max(0.0, float(timeout_sec or 0.0))
    poll_interval = max(0.1, float(poll_interval_sec or 5.0))
    deadline = time.monotonic() + timeout
    while True:
        with _fetch_lock:
            if not _fetch_active_runs:
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval, remaining))


def _run_data_dir(run_id):
    return os.path.join(BASE, 'data', 'run_sources', str(run_id))


def _fetch_log_path(run_id):
    return f'/tmp/info-radar-fetch-{run_id}.log'


def _ai_log_path(run_id):
    return f'/tmp/info-radar-ai-enrich-{run_id}.log'


def _run_env(run_id):
    env = _unbuffered_env()
    env['INFO2ACTION_DATA_DIR'] = _run_data_dir(run_id)
    _inject_python_runtime(env)
    return env


def _python_executable():
    return sys.executable or 'python3'


def _inject_python_runtime(env):
    python_bin = _python_executable()
    env['PYTHON_BIN'] = python_bin
    bin_dir = os.path.dirname(python_bin)
    if bin_dir:
        env['PATH'] = f"{bin_dir}:{env.get('PATH', '')}"
        venv_dir = os.path.dirname(bin_dir)
        if os.path.exists(os.path.join(venv_dir, 'pyvenv.cfg')):
            env.setdefault('VIRTUAL_ENV', venv_dir)
    return env


def _count_inserted_run_items(run_id):
    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS count
                      FROM {remote_db.remote_schema()}.fetch_run_items
                     WHERE run_id = %s
                       AND was_inserted = 1""",
                (run_id,),
            ).fetchone()
        return int(row['count'] if row else 0)

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM fetch_run_items WHERE run_id = ? AND was_inserted = 1",
            (run_id,),
        ).fetchone()
        return int(row['count'] if row else 0)
    finally:
        conn.close()


def _start_fetch_run_current_backend():
    if remote_db.fetch_write_to_remote():
        return remote_db.start_fetch_run_remote(None)
    conn = db.get_conn()
    try:
        return db.start_fetch_run(conn)
    finally:
        conn.close()


def _finish_fetch_run_current_backend(run_id, stats, error=None):
    if remote_db.fetch_write_to_remote():
        return remote_db.finish_fetch_run_remote(None, run_id, stats, error)
    conn = db.get_conn()
    try:
        return db.finish_fetch_run(conn, run_id, stats, error)
    finally:
        conn.close()


def _publish_partial_event_run(run_id: int, reason: str) -> int:
    """Publish completed cluster drafts after a run-scoped clustering failure."""
    try:
        if remote_db.cluster_to_remote():
            published = remote_db.publish_run_remote(None, run_id)
        else:
            from clustering import summary_writer

            conn = db.get_conn()
            try:
                published = summary_writer.publish_run(conn, run_id)
            finally:
                conn.close()
        published_count = int(published or 0)
        print(
            f"[fetch] {reason}; published {published_count} completed event drafts for run #{run_id}",
            flush=True,
        )
        return published_count
    except Exception as exc:
        print(
            f"[fetch] {reason}; partial event publish failed for run #{run_id}: {exc}",
            flush=True,
        )
        return 0


def _cleanup_run_artifacts():
    """PL-3: run 产物无界增长治理(生产曾积到 18GB 差点打满 47GB 盘)。

    每次 run 收尾顺手执行,全部容错:
    - data/run_sources/ 只保留最近 N 个 run 目录(默认 20,env 可调);
    - /tmp/info-radar-*.log 删除 7 天以前的;
    - logs/cluster_events.jsonl 超过 50MB 轮转一代(.1)。
    """
    import shutil

    try:
        keep = int(os.environ.get('INFO2ACTION_RUN_SOURCES_KEEP', '20'))
    except ValueError:
        keep = 20
    keep = max(1, keep)
    try:
        root = os.path.join(BASE, 'data', 'run_sources')
        if os.path.isdir(root):
            run_dirs = [
                entry for entry in os.scandir(root)
                if entry.is_dir(follow_symlinks=False)
            ]
            run_dirs.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
            for entry in run_dirs[keep:]:
                shutil.rmtree(entry.path, ignore_errors=True)
    except Exception as exc:
        print(f"[fetch] run_sources cleanup skipped: {exc}", flush=True)

    try:
        import glob as _glob
        cutoff = time.time() - 7 * 86400
        for log_path in _glob.glob('/tmp/info-radar-*.log'):
            try:
                if os.path.getmtime(log_path) < cutoff:
                    os.remove(log_path)
            except OSError:
                continue
    except Exception as exc:
        print(f"[fetch] tmp log cleanup skipped: {exc}", flush=True)

    try:
        jsonl = os.path.join(BASE, 'logs', 'cluster_events.jsonl')
        if os.path.isfile(jsonl) and os.path.getsize(jsonl) > 50 * 1024 * 1024:
            os.replace(jsonl, jsonl + '.1')  # 保留一代,旧的 .1 被覆盖
    except Exception as exc:
        print(f"[fetch] cluster_events rotate skipped: {exc}", flush=True)

    # PL-10(B9): fetch_runs 表无界增长(~120 行/天,行含全平台 stats_json),
    # micro 调度每 60s 还要对它做 jsonb 表达式全扫——保留 90 天。
    try:
        if remote_db.fetch_write_to_remote():
            schema = remote_db.remote_schema()
            with remote_db.connect() as conn:
                remote_db._set_short_statement_timeout(conn, 5000)
                cur = conn.execute(
                    f"""DELETE FROM {schema}.fetch_runs
                         WHERE started_at < now() - interval '90 days'"""
                )
                conn.commit()
                if getattr(cur, 'rowcount', 0):
                    print(f"[fetch] pruned {cur.rowcount} fetch_runs older than 90d", flush=True)
    except Exception as exc:
        print(f"[fetch] fetch_runs prune skipped: {exc}", flush=True)


def _clear_feed_caches_safely():
    try:
        # BF-0515-cache-scoped-invalidation: clear feed-content caches at
        # every layer. Auth/profile caches survive.
        remote_db.clear_feed_cache_keys(clear_remote_snapshots=True)
    except Exception:
        pass


def _schedule_post_fetch_read_model_refresh(
    run_id: int,
    *,
    highlights_read_model_refresh_min_interval_sec: int | None = None,
):
    # BF-0515-mv-pgcron: refresh read models / materialized views after
    # fetch runs that can publish newly visible rows. Keep platform cache
    # prewarm optional: disabling INFO2ACTION_PREWARM_PLATFORMS should not
    # stop the 信息 read model from publishing newly fetched rows.
    try:
        refresh_skip_reason = _remote_db_pressure_skip_reason()
        platform_prewarm_enabled = (
            _env_enabled('INFO2ACTION_CACHE_PREWARM', default=True)
            and _platform_prewarm_enabled()
        )
        read_model_refresh_enabled = (
            _env_enabled('INFO2ACTION_INFO_READ_MODEL', default=False)
            and _env_enabled('INFO2ACTION_INFO_READ_MODEL_REFRESH', default=True)
        )
        read_model_refresh_min_interval_sec = _info_read_model_refresh_min_interval_sec()
        highlights_read_model_refresh_enabled = (
            _env_enabled('INFO2ACTION_HIGHLIGHTS_READ_MODEL', default=False)
            and _env_enabled('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH', default=True)
        )
        if highlights_read_model_refresh_min_interval_sec is None:
            highlights_read_model_refresh_min_interval_sec = _highlights_read_model_refresh_min_interval_sec()
        if refresh_skip_reason:
            print(
                f"[mv] post-fetch prewarm skipped: {refresh_skip_reason}",
                flush=True,
            )
            return
        if not (platform_prewarm_enabled or read_model_refresh_enabled or highlights_read_model_refresh_enabled):
            return

        def _bg_prewarm():
            try:
                refreshed = False
                if platform_prewarm_enabled:
                    result = remote_db.prewarm_platforms(
                        refresh_mv=_env_enabled('INFO2ACTION_REFRESH_PLATFORM_MV_AFTER_FETCH', default=False),
                        refresh_read_model=read_model_refresh_enabled,
                        refresh_read_model_min_interval_sec=read_model_refresh_min_interval_sec,
                        refresh_highlights_read_model=highlights_read_model_refresh_enabled,
                        refresh_highlights_read_model_min_interval_sec=highlights_read_model_refresh_min_interval_sec,
                    )
                    refreshed = (
                        bool(read_model_refresh_enabled)
                        or bool(highlights_read_model_refresh_enabled)
                    )
                    print(f"[mv] prewarm after fetch_run: {result}", flush=True)
                else:
                    if read_model_refresh_enabled:
                        result = remote_db.refresh_info_read_model_if_stale(
                            min_interval_sec=read_model_refresh_min_interval_sec
                        )
                        refreshed = True
                        print(f"[mv] read model refresh after fetch_run: {result}", flush=True)
                    if highlights_read_model_refresh_enabled:
                        result = remote_db.refresh_highlights_read_model_if_stale(
                            min_interval_sec=highlights_read_model_refresh_min_interval_sec
                        )
                        refreshed = True
                        print(f"[mv] highlights read model refresh after fetch_run: {result}", flush=True)
                if refreshed:
                    _clear_feed_caches_safely()
            except Exception as _e:
                print(f"[mv] prewarm exc: {_e!r}", flush=True)

        # PL-11: daemon=True——refresh 幂等且下轮会补跑;非 daemon 线程会让
        # systemd stop 等待最长 180s,破坏"秒级重启"。
        threading.Thread(target=_bg_prewarm, daemon=True, name=f'post-fetch-read-model-refresh-{run_id}').start()
    except Exception:
        pass


def _fetch_run_stats_current_backend():
    if remote_db.fetch_write_to_remote():
        schema = remote_db.remote_schema()
        stats = {}
        # PL-8(B9): 全表 GROUP BY 是随表增长的顺序扫描,每个 run 收尾跑一次;
        # 加 timeout + 容错——诊断统计失败不应影响 run 收尾。
        try:
            with remote_db.connect() as conn:
                remote_db._set_short_statement_timeout(conn, 5000)
                rows = conn.execute(
                    f"""SELECT platform, COUNT(*) AS total
                          FROM {schema}.items
                         GROUP BY platform"""
                ).fetchall()
        except Exception as exc:
            print(f"[fetch] run stats skipped: {exc}", flush=True)
            return {}
        for row in rows:
            platform = row['platform'] if isinstance(row, dict) else row[0]
            total = row['total'] if isinstance(row, dict) else row[1]
            stats[platform] = {'total': int(total or 0), 'unread': 0}
        return stats

    conn = db.get_conn()
    try:
        return db.get_stats(conn)
    finally:
        conn.close()


def _per_platform_new_counts_current_backend(started_at_iso):
    """每平台本轮新增条数 (fetched_at >= started_at_iso)，用于 LOG-1 失败告警。

    返回 dict[platform -> count]。Reddit 静默 21h 0 条这类问题靠这个被察觉。
    SQL 失败不阻塞主流程，吞错误返回 {'_error': ...}.
    """
    if not started_at_iso:
        return {}
    try:
        if remote_db.fetch_write_to_remote():
            schema = remote_db.remote_schema()
            with remote_db.connect() as conn:
                rows = conn.execute(
                    f"""SELECT platform, COUNT(*) AS cnt
                          FROM {schema}.items
                         WHERE fetched_at >= %(t)s
                         GROUP BY platform""",
                    {'t': started_at_iso},
                ).fetchall()
            return {
                ((r['platform'] if isinstance(r, dict) else r[0]) or '_unknown'):
                    int((r['cnt'] if isinstance(r, dict) else r[1]) or 0)
                for r in rows
            }
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT platform, COUNT(*) AS cnt FROM items "
                "WHERE fetched_at >= ? GROUP BY platform",
                (started_at_iso,),
            ).fetchall()
            return {(row['platform'] or '_unknown'): int(row['cnt'] or 0) for row in rows}
        finally:
            conn.close()
    except Exception as exc:
        return {'_error': str(exc)[:200]}


def _stage_index(progress, stage_id):
    for idx, stage in enumerate(progress.get('stages', [])):
        if stage.get('id') == stage_id:
            return idx
    return None


def _set_progress_stage(progress, stage_id, *, status=None, platform=None, percent=None, message=None, new_count=None):
    idx = _stage_index(progress, stage_id)
    if idx is None:
        return
    stage = progress['stages'][idx]
    if status is not None:
        stage['status'] = status
    if platform is not None:
        stage['platform'] = platform
        progress['platform'] = platform
    if percent is not None:
        stage['percent'] = percent
        progress['percent'] = percent
    if message is not None:
        stage['message'] = message
        progress['message'] = message
    if new_count is not None:
        stage['new_count'] = new_count
    if status == 'running':
        progress['current_stage'] = idx


def _set_global_stage(stage_id, *, status=None, platform=None, percent=None, message=None, new_count=None):
    with _fetch_lock:
        _set_progress_stage(
            _fetch_progress,
            stage_id,
            status=status,
            platform=platform,
            percent=percent,
            message=message,
            new_count=new_count,
        )


def _set_run_stage(run_id, stage_id, *, status=None, platform=None, percent=None, message=None, new_count=None):
    with _fetch_lock:
        active = _fetch_active_runs.get(run_id)
        progress = active.get('progress') if active else None
        if progress is None:
            progress = _fetch_progress
        _set_progress_stage(
            progress,
            stage_id,
            status=status,
            platform=platform,
            percent=percent,
            message=message,
            new_count=new_count,
        )


def _derive_fetch_log_progress(log_text):
    """Best-effort display progress for the monolithic fetch_all.sh log."""
    current = None
    for marker, stage_id, platform, percent in FETCH_LOG_MARKERS:
        if marker in log_text:
            current = {
                'stage_id': stage_id,
                'platform': platform,
                'percent': percent,
            }
    if current and current['stage_id'] == 'event_cluster':
        return current
    ai_progress = _derive_ai_enrich_progress(log_text)
    if ai_progress:
        return ai_progress
    return current


def _percent_between(done, total, start=AI_ENRICH_PERCENT_START, end=AI_ENRICH_PERCENT_END):
    try:
        done_num = max(0, int(done))
        total_num = max(0, int(total))
    except (TypeError, ValueError):
        return start
    if total_num <= 0:
        return start
    done_num = min(done_num, total_num)
    if done_num <= 0:
        return start
    span = end - start
    step = (span * done_num + total_num - 1) // total_num
    return max(start, min(end, start + step))


def _derive_ai_enrich_progress(log_text):
    """Parse enrich_items.py progress and map it to the global 55-80% range."""
    if not log_text:
        return None

    progress_matches = list(AI_ENRICH_PROGRESS_RE.finditer(log_text))
    if progress_matches:
        match = progress_matches[-1]
        done = int(match.group(1))
        total = int(match.group(2))
        platform = match.group(3) or '全部平台'
        return {
            'stage_id': 'ai_enrich',
            'platform': platform,
            'percent': _percent_between(done, total),
        }

    done_match = AI_ENRICH_DONE_RE.search(log_text)
    if done_match:
        return {
            'stage_id': 'ai_enrich',
            'platform': '全部平台',
            'percent': AI_ENRICH_PERCENT_END,
        }

    if 'AI 统一理解' in log_text or AI_ENRICH_FOUND_RE.search(log_text):
        return {
            'stage_id': 'ai_enrich',
            'platform': '全部平台',
            'percent': AI_ENRICH_PERCENT_START,
        }

    return None


def _decorate_progress_from_log(progress):
    if not progress or progress.get('mode') != 'global':
        return progress
    log_texts = []
    fetch_log_text = ''
    fetch_log_path = progress.get('_fetch_log_path') or '/tmp/info-radar-fetch.log'
    ai_log_path = progress.get('_ai_log_path') or '/tmp/info-radar-ai-enrich.log'
    try:
        with open(fetch_log_path, 'r') as f:
            fetch_log_text = f.read()
            log_texts.append(fetch_log_text)
    except Exception:
        pass
    should_read_ai_log = (
        progress.get('current_stage', 0) >= 2
        or 'AI 统一理解' in fetch_log_text
        or '事件聚合 (' in fetch_log_text
        or '全部完成' in fetch_log_text
    )
    if should_read_ai_log:
        try:
            with open(ai_log_path, 'r') as f:
                log_texts.append(f.read())
        except Exception:
            pass
    if not log_texts:
        return progress
    derived = _derive_fetch_log_progress('\n'.join(log_texts))
    if not derived:
        return progress
    idx = _stage_index(progress, derived['stage_id'])
    if idx is None:
        return progress
    for prior_idx in range(idx):
        if progress['stages'][prior_idx].get('status') in ('pending', 'running'):
            progress['stages'][prior_idx]['status'] = 'done'
    stage = progress['stages'][idx]
    if stage.get('status') in ('pending', 'running'):
        stage['status'] = 'running'
    stage['platform'] = derived['platform']
    stage['percent'] = derived['percent']
    progress['current_stage'] = idx
    progress['platform'] = derived['platform']
    progress['percent'] = derived['percent']
    return progress


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _coerce_positive_int(value, default, *, minimum=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _coerce_non_negative_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _env_enabled(name, *, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _clustering_config():
    cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
    clustering = cfg.get('global', {}).get('clustering', {})
    return clustering if isinstance(clustering, dict) else {}


def _cluster_pipeline_settings():
    clustering = _clustering_config()
    configured_judge_workers = _coerce_positive_int(
        clustering.get('stage2_judge_workers'),
        20,
    )
    configured_judge_min_interval_sec = _coerce_non_negative_float(
        clustering.get('stage2_judge_min_interval_sec'),
        0.8,
    )
    configured_summary_workers = _coerce_positive_int(
        clustering.get('cluster_summary_workers'),
        1,
    )
    configured_timeout_sec = _coerce_positive_int(
        clustering.get('pipeline_timeout_sec'),
        1800,
        minimum=60,
    )
    return {
        'judge_workers': _coerce_positive_int(
            os.environ.get('INFO2ACTION_CLUSTER_JUDGE_WORKERS'),
            configured_judge_workers,
        ),
        'judge_min_interval_sec': _coerce_non_negative_float(
            os.environ.get('INFO2ACTION_CLUSTER_JUDGE_MIN_INTERVAL_SEC'),
            configured_judge_min_interval_sec,
        ),
        'summary_workers': _coerce_positive_int(
            os.environ.get('INFO2ACTION_CLUSTER_SUMMARY_WORKERS'),
            configured_summary_workers,
        ),
        'timeout_sec': _coerce_positive_int(
            os.environ.get('INFO2ACTION_CLUSTER_PIPELINE_TIMEOUT_SEC'),
            configured_timeout_sec,
            minimum=60,
        ),
    }


def _cluster_pipeline_cmd(run_id, *, stats_path=None):
    settings = _cluster_pipeline_settings()
    cmd = [
        _python_executable(),
        os.path.join(BASE, 'src', 'clustering', 'pipeline.py'),
        '--run-id',
        str(run_id),
        '--run-items-scope',
        'inserted',
        '--judge-workers',
        str(settings['judge_workers']),
        '--judge-min-interval-sec',
        str(settings['judge_min_interval_sec']),
        '--summary-workers',
        str(settings['summary_workers']),
    ]
    if stats_path:
        cmd.extend(['--stats-path', stats_path])
    return cmd


def _load_event_cluster_stats(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[fetch] failed to read event cluster stats {path}: {exc}", flush=True)
        return None
    return data if isinstance(data, dict) else None


def _event_cluster_published_count(stats):
    if not isinstance(stats, dict):
        return 0
    try:
        return max(0, int(stats.get('published_clusters') or 0))
    except (TypeError, ValueError):
        return 0


def _clean_env():
    """Create env without Python 3.7 pollution for uv-managed tools."""
    env = os.environ.copy()
    for k in ['PYTHONHOME', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONSTARTUP', '__PYVENV_LAUNCHER__']:
        env.pop(k, None)
    return _inject_python_runtime(env)


def _unbuffered_env():
    env = _clean_env()
    env['PYTHONUNBUFFERED'] = '1'
    return env


def _is_platform_enabled(platform):
    """Check if a platform is enabled in config (default: True)."""
    cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
    return cfg.get(platform, {}).get('enabled', True)


LOCAL_BIN_PATH = ':'.join([
    os.path.expanduser('~/.local/bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
])


def _resolve_bin(name):
    for candidate in [
        os.path.expanduser(f'~/.local/bin/{name}'),
        f'/opt/homebrew/bin/{name}',
        f'/usr/local/bin/{name}',
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


CLI = {
    'xhs': _resolve_bin('xhs'),
    'twitter': _resolve_bin('twitter'),
    'bili': _resolve_bin('bili'),
}


def _output_data_path(output_root, *parts):
    root = output_root or os.path.join(BASE, 'data')
    path = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _run_killpg(cmd, *, timeout, check=False, **kwargs):
    """PL-7(B5): 超时杀整个进程组——subprocess.run 只杀直接子进程,卡死的
    孙进程(平台抓取 CLI/ffmpeg)会被 re-parent 后继续吃 CPU/内存/代理,并与
    下一轮 run 并发。语义与 subprocess.run(timeout=, check=) 对齐。"""
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)
    return subprocess.CompletedProcess(cmd, returncode)


def _run_to_file(args, outfile, *, env=None):
    """Run command and capture stdout to file."""
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    with open(outfile, 'w') as f:
        # PL-7(B5): 原实现超时后连 kill 都没有(半僵尸+fd 泄漏)
        _run_killpg(args, timeout=120, stdout=f, stderr=subprocess.DEVNULL,
                    env=env if env is not None else _clean_env())


def _update_health_status(platform, status, message, source='fetch'):
    """Update health.json for a platform."""
    health_path = os.path.join(BASE, 'data', 'health.json')
    try:
        health = load_json(health_path) or {'platforms': {}, 'proxy': {}, 'overall': 'unknown'}
        if platform == 'proxy':
            old_status = health.get('proxy', {}).get('status', 'unknown')
            health['proxy'] = {'status': status, 'message': message}
        else:
            old_status = health.get('platforms', {}).get(platform, {}).get('status', 'unknown')
            health.setdefault('platforms', {})[platform] = {'status': status, 'message': message}
        health['last_check'] = datetime.now(timezone.utc).isoformat()
        all_statuses = [health.get('proxy', {}).get('status', 'unknown')]
        all_statuses += [p.get('status', 'unknown') for p in health.get('platforms', {}).values()]
        if any(s == 'error' for s in all_statuses):
            health['overall'] = 'error'
        elif any(s == 'warning' for s in all_statuses):
            health['overall'] = 'warning'
        elif all(s == 'ok' for s in all_statuses):
            health['overall'] = 'ok'
        else:
            health['overall'] = 'unknown'
        with open(health_path, 'w') as f:
            json.dump(health, f, ensure_ascii=False, indent=2)
        if old_status != status:
            try:
                conn = db.get_conn()
                conn.execute(
                    "INSERT INTO health_log (platform, old_status, new_status, message, source) VALUES (?,?,?,?,?)",
                    (platform, old_status, status, message, source)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
    except Exception as e:
        print(f"[health] Failed to update health status: {e}")


def _notify(msg):
    """Send macOS system notification (best-effort)."""
    try:
        subprocess.run([
            'osascript', '-e',
            f'display notification "{msg}" with title "Info Radar"'
        ], timeout=5, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ai_enrich_failure_message_from_log(log_path: str | None) -> str | None:
    if not log_path:
        return None
    try:
        with open(log_path, 'r') as f:
            text = f.read()
    except OSError:
        return None
    if AI_REMOTE_DB_FAILURE_RE.search(text):
        return AI_REMOTE_DB_FAILURE_MESSAGE
    return None


def _run_summaries(limit=None, progress_stages=None, run_id=None, batch_size=None, workers=None, log_path=None, progress_run_id=None):
    """Run AI enrichment for the current fetch run before event publishing."""
    cmd = [_python_executable(), '-u', os.path.join(BASE, 'src', 'enrich_items.py')]
    if limit:
        cmd.extend(['--limit', str(limit)])
    elif run_id is not None:
        # PL-5(B6): run 模式不再无限量——全平台大 run + 积压会把 pending
        # (含 content+detail_json)全量载入 2GB 小机内存;与 legacy fetch_all.sh
        # 一致取 800,漏网条目由下轮积压重试消化。
        try:
            run_limit = int(os.environ.get('INFO2ACTION_ENRICH_RUN_LIMIT', '800'))
        except ValueError:
            run_limit = 800
        cmd.extend(['--limit', str(max(0, run_limit))])
    if run_id is not None:
        cmd.extend(['--run-id', str(run_id)])
    if run_id is not None and limit is None:
        cmd.extend(['--run-items-scope', 'inserted'])
    if batch_size:
        cmd.extend(['--batch-size', str(batch_size)])
    if workers:
        cmd.extend(['--workers', str(workers)])
    steps = [
        (
            'AI 统一理解',
            cmd,
            7200 if run_id is not None and not limit else 900,
        ),
    ]
    ok = True
    with open(log_path or '/tmp/info-radar-ai-enrich.log', 'a') as log_f:
        for idx, (name, cmd, timeout) in enumerate(steps):
            stage_idx = progress_stages[idx] if progress_stages and idx < len(progress_stages) else None
            if stage_idx is not None:
                with _fetch_lock:
                    target_progress = (
                        _fetch_active_runs.get(progress_run_id, {}).get('progress')
                        if progress_run_id is not None
                        else None
                    ) or _fetch_progress
                    target_progress['stages'][stage_idx]['status'] = 'running'
                    target_progress['current_stage'] = stage_idx
            print(f"[fetch] {name} started")
            log_f.write(f"\n===== {datetime.now(timezone.utc).isoformat()} {name} =====\n")
            log_f.flush()
            try:
                result = _run_killpg(  # PL-7
                    cmd,
                    cwd=BASE,
                    timeout=timeout,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=_unbuffered_env(),
                )
                if result.returncode == 0:
                    print(f"[fetch] {name} done")
                    if stage_idx is not None:
                        with _fetch_lock:
                            target_progress = (
                                _fetch_active_runs.get(progress_run_id, {}).get('progress')
                                if progress_run_id is not None
                                else None
                            ) or _fetch_progress
                            target_progress['stages'][stage_idx]['status'] = 'done'
                else:
                    ok = False
                    print(f"[fetch] {name} failed with exit code {result.returncode}")
                    provider_message = _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                    failure_message = _ai_enrich_failure_message_from_log(log_path) or provider_message
                    if stage_idx is not None:
                        with _fetch_lock:
                            target_progress = (
                                _fetch_active_runs.get(progress_run_id, {}).get('progress')
                                if progress_run_id is not None
                                else None
                            ) or _fetch_progress
                            target_progress['stages'][stage_idx]['status'] = 'failed'
                            if failure_message:
                                target_progress['stages'][stage_idx]['message'] = failure_message
                                target_progress['message'] = failure_message
            except subprocess.TimeoutExpired:
                ok = False
                print(f"[fetch] {name} timed out after {timeout}s")
                if stage_idx is not None:
                    with _fetch_lock:
                        target_progress = (
                            _fetch_active_runs.get(progress_run_id, {}).get('progress')
                            if progress_run_id is not None
                            else None
                        ) or _fetch_progress
                        target_progress['stages'][stage_idx]['status'] = 'failed'
            except Exception as e:
                ok = False
                print(f"[fetch] {name} error: {e}")
                if stage_idx is not None:
                    with _fetch_lock:
                        target_progress = (
                            _fetch_active_runs.get(progress_run_id, {}).get('progress')
                            if progress_run_id is not None
                            else None
                        ) or _fetch_progress
                        target_progress['stages'][stage_idx]['status'] = 'failed'
    return ok


def _run_recommend_fetch():
    """Quick fetch: multi-batch recommend feeds + latest search to maximize fresh content."""
    global _fetch_running, _fetch_finished_at, _fetch_progress
    try:
        _fetch_progress = {
            'stages': [
                {'name': '抓取推荐流', 'status': 'running'},
                {'name': '入库处理', 'status': 'pending'},
            ],
            'current_stage': 0, 'total_new': 0
        }
        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        tw_fyu = str(cfg.get('twitter', {}).get('for_you_count', 50))
        tw_fol = str(cfg.get('twitter', {}).get('following_count', 50))
        tw_dir = os.path.join(BASE, 'data', 'sources', 'twitter')
        xhs_dir = os.path.join(BASE, 'data', 'sources', 'xiaohongshu')

        subprocess.run([CLI['twitter'], 'feed', '-t', 'for-you', '-n', tw_fyu, '-o',
            os.path.join(tw_dir, '2-for-you-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
        subprocess.run([CLI['twitter'], 'feed', '-t', 'following', '-n', tw_fol, '-o',
            os.path.join(tw_dir, '1-following-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
        xhs_enabled = _is_platform_enabled('xiaohongshu')
        if xhs_enabled:
            _run_to_file([CLI['xhs'], 'feed', '--json'],
                os.path.join(xhs_dir, '1-recommend-feed.json'))

        # BF-0418-NEW 子项 B：按 topics.json 分关键词抓取，产 search-{safe}.json（无 -latest 后缀）
        # 向下兼容 DB 里 source='search:XXX' 的 pill（ingest.py:156 会把 _ 还原为空格）
        since_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        topics_cfg_path = os.path.join(BASE, 'config', 'topics.json')
        search_kws = []
        try:
            with open(topics_cfg_path) as _tf:
                _tc = json.load(_tf)
            for _t in _tc.get('topics', []):
                for _q in _t.get('search_queries', []):
                    if _q not in search_kws:
                        search_kws.append(_q)
        except Exception:
            search_kws = cfg.get('global', {}).get('search_keywords', [])[:4]
        for kw in search_kws:
            safe = kw.replace(' ', '_')
            try:
                subprocess.run([CLI['twitter'], 'search', kw, '-t', 'latest',
                    '--since', since_date, '-n', '30', '-o',
                    os.path.join(tw_dir, f'search-{safe}.json')],
                    stderr=subprocess.DEVNULL, timeout=60, env=_clean_env())
            except Exception:
                pass
            if xhs_enabled:
                try:
                    _run_to_file([CLI['xhs'], 'search', kw, '--sort', 'latest', '--json'],
                        os.path.join(xhs_dir, f'search-{safe}.json'))
                except Exception:
                    pass

        _fetch_progress['stages'][0]['status'] = 'done'
        _fetch_progress['stages'][1]['status'] = 'running'
        _fetch_progress['current_stage'] = 1
        conn_pre = db.get_conn()
        count_before = conn_pre.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_pre.close()
        subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'ingest.py')],
            cwd=BASE, timeout=120, stderr=subprocess.DEVNULL)
        conn_post = db.get_conn()
        count_after = conn_post.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_post.close()
        new_count = max(0, count_after - count_before)
        _fetch_progress['stages'][1]['status'] = 'done'
        _fetch_progress['stages'][1]['new_count'] = new_count
        _fetch_progress['total_new'] = new_count
        print(f"Recommend fetch complete: {new_count} new items (summaries/scoring handled by background cron)")
    except Exception as e:
        print(f"Recommend fetch error: {e}")
        _update_health_status('twitter', 'error', f'推荐流抓取失败: {str(e)[:100]}')
        _update_health_status('xiaohongshu', 'error', f'推荐流抓取失败: {str(e)[:100]}')
    finally:
        with _fetch_lock:
            _fetch_running = False
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()


def _run_topic_fetch(topic_name):
    """Fetch content for a specific topic using its search keywords from topics.json."""
    global _fetch_running, _fetch_finished_at, _fetch_progress
    try:
        _fetch_progress = {
            'stages': [
                {'name': '抓取数据', 'status': 'running'},
                {'name': '入库处理', 'status': 'pending'},
            ],
            'current_stage': 0, 'total_new': 0
        }
        topics_path = os.path.join(BASE, 'config', 'topics.json')
        with open(topics_path) as f:
            topics_cfg = json.load(f)
        topic = None
        for t in topics_cfg.get('topics', []):
            if t['name'] == topic_name:
                topic = t
                break
        if not topic:
            print(f"Topic not found: {topic_name}")
            return

        queries = topic.get('search_queries', [])
        if not queries:
            print(f"No search queries for topic: {topic_name}")
            return

        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        tw_fyu = str(cfg.get('twitter', {}).get('for_you_count', 50))
        tw_fol = str(cfg.get('twitter', {}).get('following_count', 50))
        xhs_enabled = _is_platform_enabled('xiaohongshu')
        try:
            subprocess.run([CLI['twitter'], 'feed', '-t', 'for-you', '-n', tw_fyu, '-o',
                os.path.join(BASE, 'data', 'sources', 'twitter', '2-for-you-feed.json')],
                stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
            subprocess.run([CLI['twitter'], 'feed', '-t', 'following', '-n', tw_fol, '-o',
                os.path.join(BASE, 'data', 'sources', 'twitter', '1-following-feed.json')],
                stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
            if xhs_enabled:
                _run_to_file([CLI['xhs'], 'feed', '--json'],
                    os.path.join(BASE, 'data', 'sources', 'xiaohongshu', '1-recommend-feed.json'))
        except Exception:
            pass

        since_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        for q in queries:
            safe = q.replace(' ', '_')
            if xhs_enabled:
                try:
                    _run_to_file([CLI['xhs'], 'search', q, '--sort', 'latest', '--json'],
                        os.path.join(BASE, 'data', 'sources', 'xiaohongshu', f'search-{safe}.json'))
                except Exception as e:
                    print(f"XHS search '{q}' error: {e}")
            try:
                subprocess.run([CLI['twitter'], 'search', q, '-t', 'latest',
                    '--since', since_date, '-n', '30', '-o',
                    os.path.join(BASE, 'data', 'sources', 'twitter', f'search-{safe}.json')],
                    stderr=subprocess.DEVNULL, timeout=60, env=_clean_env())
            except Exception as e:
                print(f"Twitter search '{q}' error: {e}")

        _fetch_progress['stages'][0]['status'] = 'done'
        _fetch_progress['stages'][1]['status'] = 'running'
        _fetch_progress['current_stage'] = 1
        conn_pre = db.get_conn()
        count_before = conn_pre.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_pre.close()
        subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'ingest.py')],
            cwd=BASE, timeout=120, stderr=subprocess.DEVNULL)
        conn_post = db.get_conn()
        count_after = conn_post.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_post.close()
        new_count = max(0, count_after - count_before)
        _fetch_progress['stages'][1]['status'] = 'done'
        _fetch_progress['stages'][1]['new_count'] = new_count
        _fetch_progress['total_new'] = new_count
        print(f"Topic fetch complete: {topic_name} ({len(queries)} queries, {new_count} new) — summaries deferred to cron")
    except Exception as e:
        print(f"Topic fetch error: {e}")
        _update_health_status('twitter', 'error', f'主题抓取失败: {str(e)[:100]}')
        _update_health_status('xiaohongshu', 'error', f'主题抓取失败: {str(e)[:100]}')
    finally:
        with _fetch_lock:
            _fetch_running = False
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()


def _run_platform_all(platform, *, output_root=None, env=None):
    """Fetch all sources for a platform when no specific source is given."""
    command_env = env if env is not None else _clean_env()
    cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
    if platform == 'twitter':
        n_fy = str(cfg.get('twitter', {}).get('for_you_count', 50))
        n_fl = str(cfg.get('twitter', {}).get('following_count', 50))
        subprocess.run([CLI['twitter'], 'feed', '-t', 'for-you', '-n', n_fy, '-o',
            _output_data_path(output_root, 'sources', 'twitter', '2-for-you-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=command_env)
        subprocess.run([CLI['twitter'], 'feed', '-t', 'following', '-n', n_fl, '-o',
            _output_data_path(output_root, 'sources', 'twitter', '1-following-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=command_env)
        kws = cfg.get('global', {}).get('search_keywords', []) + cfg.get('twitter', {}).get('search', {}).get('extra_keywords', [])
        for kw in dict.fromkeys(kws):
            subprocess.run([CLI['twitter'], 'search', kw, '-t', 'latest',
                '-n', '30', '--exclude', 'retweets', '--filter', '--json', '-o',
                _output_data_path(output_root, 'sources', 'twitter', f'search-{kw.replace(" ", "_")}.json')],
                stderr=subprocess.DEVNULL, timeout=60, env=command_env)
    elif platform == 'xiaohongshu':
        if not _is_platform_enabled('xiaohongshu'):
            print("[fetch] xiaohongshu is disabled in config, skipping")
            return
        _run_to_file([CLI['xhs'], 'feed', '--json'],
            _output_data_path(output_root, 'sources', 'xiaohongshu', '1-recommend-feed.json'),
            env=command_env)
        kws = cfg.get('global', {}).get('search_keywords', []) + cfg.get('xiaohongshu', {}).get('search', {}).get('extra_keywords', [])
        for kw in dict.fromkeys(kws):
            safe = kw.replace(' ', '_')
            _run_to_file([CLI['xhs'], 'search', kw, '--sort', 'latest', '--json'],
                _output_data_path(output_root, 'sources', 'xiaohongshu', f'search-{safe}.json'),
                env=command_env)
    elif platform == 'bilibili':
        # 仅抓 hot；rank/search/UP 主因 WBI 签名 + IP 风控问题下线（BF-0418-9 子项 α）
        _run_to_file([CLI['bili'], 'hot', '--json'],
            _output_data_path(output_root, 'sources', 'bilibili', '3-hot.json'),
            env=command_env)
    elif platform in ('hackernews', 'reddit', 'github', 'rss'):
        flag_map = {'hackernews': '--hn', 'reddit': '--reddit', 'github': '--github', 'rss': '--rss'}
        subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'fetch_feeds.py'), flag_map[platform]],
            cwd=BASE, timeout=120, stderr=subprocess.DEVNULL, env=command_env)


def _run_source_fetch_step(platform, source, *, output_root=None, env=None):
    """Run the raw fetch command for one platform/source pair."""
    command_env = env if env is not None else _clean_env()
    if source is None or source == '':
        _run_platform_all(platform, output_root=output_root, env=command_env)
        return True
    if platform == 'xiaohongshu' and not _is_platform_enabled('xiaohongshu'):
        print("[fetch] xiaohongshu is disabled in config, skipping")
        return True
    if platform == 'xiaohongshu' and source.startswith('search:'):
        keyword = source.replace('search:', '').replace('_', ' ')
        safe = keyword.replace(' ', '_')
        _run_to_file([CLI['xhs'], 'search', keyword, '--sort', 'latest', '--json'],
            _output_data_path(output_root, 'sources', 'xiaohongshu', f'search-{safe}.json'),
            env=command_env)
        return True
    if platform == 'xiaohongshu' and source == 'recommend':
        _run_to_file([CLI['xhs'], 'feed', '--json'],
            _output_data_path(output_root, 'sources', 'xiaohongshu', '1-recommend-feed.json'),
            env=command_env)
        return True
    if platform == 'twitter' and source == 'for_you':
        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        n = str(cfg.get('twitter', {}).get('for_you_count', 50))
        subprocess.run([CLI['twitter'], 'feed', '-t', 'for-you', '-n', n, '-o',
            _output_data_path(output_root, 'sources', 'twitter', '2-for-you-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=command_env)
        return True
    if platform == 'twitter' and source == 'following':
        cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
        n = str(cfg.get('twitter', {}).get('following_count', 50))
        subprocess.run([CLI['twitter'], 'feed', '-t', 'following', '-n', n, '-o',
            _output_data_path(output_root, 'sources', 'twitter', '1-following-feed.json')],
            stderr=subprocess.DEVNULL, timeout=90, env=command_env)
        return True
    if platform == 'bilibili' and source == 'hot':
        _run_to_file([CLI['bili'], 'hot', '--json'],
            _output_data_path(output_root, 'sources', 'bilibili', '3-hot.json'),
            env=command_env)
        return True
    if platform == 'bilibili' and source.startswith('search:'):
        keyword = source.replace('search:', '').replace('_', ' ')
        safe = keyword.replace(' ', '_')
        _run_to_file(['curl', '-s',
            f'https://api.bilibili.com/x/web-interface/search/all/v2?keyword={keyword}&page=1&order=totalrank',
            '-H', 'Cookie: buvid3=rand'],
            _output_data_path(output_root, 'sources', 'bilibili', f'search-{safe}.json'),
            env=command_env)
        return True
    if platform == 'lingowhale':
        # v20.0 增量抓取:语鲸走 micro 高频。fetch_lingowhale.py 写全局
        # data/lingowhale/feed.json(不用 output_root),后续 ingest.py --run-id 入库。
        # 是否只拉增量由 INFO2ACTION_LINGOWHALE_INCREMENTAL 开关决定(默认关=全窗口)。
        subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'fetch_lingowhale.py')],
            cwd=BASE, timeout=300, stderr=subprocess.DEVNULL, env=command_env)
        return True
    return False


def _run_quick_fetch(platform, source):
    """Quick fetch for a single platform/source combination."""
    global _fetch_running, _fetch_finished_at, _fetch_progress
    try:
        _fetch_progress = {
            'stages': [
                {'name': '抓取数据', 'status': 'running'},
                {'name': '入库处理', 'status': 'pending'},
            ],
            'current_stage': 0, 'total_new': 0
        }

        if not _run_source_fetch_step(platform, source):
            return
        _fetch_progress['stages'][0]['status'] = 'done'
        _fetch_progress['stages'][1]['status'] = 'running'
        _fetch_progress['current_stage'] = 1
        conn_pre = db.get_conn()
        count_before = conn_pre.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_pre.close()
        subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'ingest.py')],
            cwd=BASE, timeout=120, stderr=subprocess.DEVNULL)
        conn_post = db.get_conn()
        count_after = conn_post.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn_post.close()
        new_count = max(0, count_after - count_before)
        _fetch_progress['stages'][1]['status'] = 'done'
        _fetch_progress['stages'][1]['new_count'] = new_count
        _fetch_progress['total_new'] = new_count
        print(f"Quick fetch complete: {platform}/{source}, {new_count} new items — summaries deferred to cron")
    except Exception as e:
        print(f"Quick fetch error: {e}")
        _update_health_status(platform, 'error', f'抓取失败: {str(e)[:100]}')
    finally:
        with _fetch_lock:
            _fetch_running = False
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()


def _run_fetch(run_id=None, source='api'):
    global _fetch_running, _fetch_finished_at, _fetch_progress
    if run_id is None:
        run_id = _start_fetch_run_current_backend()
        with _fetch_lock:
            progress = _make_global_fetch_progress()
            progress.update({
                'run_id': run_id,
                'source': source,
                'started_at': datetime.now(timezone.utc).isoformat(),
                '_fetch_log_path': _fetch_log_path(run_id),
                '_ai_log_path': _ai_log_path(run_id),
            })
            progress['stages'][0]['status'] = 'running'
            progress['percent'] = 1
            _fetch_active_runs[run_id] = {
                'source': source,
                'started_at': progress['started_at'],
                'progress': progress,
            }
            _fetch_progress = progress
            _fetch_running = True
    else:
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _make_global_fetch_progress()
            progress['stages'][0]['status'] = 'running'
            progress['percent'] = 1
            _fetch_progress = progress

    os.makedirs(_run_data_dir(run_id), exist_ok=True)
    run_env = _run_env(run_id)
    fetch_log_path = _fetch_log_path(run_id)
    ai_log_path = _ai_log_path(run_id)
    fetch_all_timed_out = False
    fetch_all_failed = False
    ai_ok = True
    cluster_ok = True
    partial_published_clusters = 0
    event_cluster_stats = None
    stage_durations = {}
    stage_started = {}
    # PL-1: 只有本轮真的改变了 feed 内容才清缓存;预置 True 使异常路径
    # fail-safe(宁可多清不可漏清),成功路径按实际新增/发布计算。
    feed_content_changed = True
    heartbeat_stop_event = _start_fetch_run_heartbeat(run_id)

    def _stage_start(stage_id):
        stage_started[stage_id] = time.monotonic()

    def _stage_finish(stage_id):
        started = stage_started.pop(stage_id, None)
        if started is not None:
            stage_durations[stage_id] = round(time.monotonic() - started, 2)

    try:
        _stage_start('source_fetch')
        _set_run_stage(run_id, 'source_fetch', status='running', platform='全部平台', percent=1, message='全局抓取已启动')

        with open(fetch_log_path, 'w') as log_f:
            try:
                _run_killpg(  # PL-7
                    ['bash', os.path.join(BASE, 'ops', 'fetch_all.sh'), '--raw-only', '--run-id', str(run_id)],
                    cwd=BASE, timeout=1800,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=run_env,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                fetch_all_timed_out = True
                print("[fetch] fetch_all.sh timed out after 1800s, continuing with ingest")
            except subprocess.CalledProcessError as e:
                fetch_all_failed = True
                print(f"[fetch] fetch_all.sh failed with exit code {e.returncode}, continuing with ingest")

        if fetch_all_timed_out:
            _set_run_stage(run_id,
                'source_fetch',
                status='warning',
                percent=34,
                message='fetch_all.sh 超时，已进入补偿入库流程',
            )
        elif fetch_all_failed:
            _set_run_stage(run_id,
                'source_fetch',
                status='warning',
                percent=34,
                message='fetch_all.sh 异常退出，已进入补偿入库流程',
            )
        else:
            _set_run_stage(run_id, 'source_fetch', status='done', platform='全部平台', percent=35, message='')
        _stage_finish('source_fetch')

        _stage_start('ingest')
        _set_run_stage(run_id, 'ingest', status='running', platform='全部平台', percent=38, message='正在入库')

        ingest_ok = True
        try:
            subprocess.run([
                _python_executable(),
                os.path.join(BASE, 'src', 'ingest.py'),
                '--run-id',
                str(run_id),
                '--skip-image-download',
            ],
                cwd=BASE, timeout=600, stderr=subprocess.DEVNULL, env=run_env, check=True)
        except subprocess.TimeoutExpired:
            ingest_ok = False
            print("[fetch] ingest.py timed out after 600s, continuing")
            _set_run_stage(run_id, 'ingest', status='warning', message='ingest.py 超时，继续后续流程')
        except subprocess.CalledProcessError as e:
            ingest_ok = False
            print(f"[fetch] ingest.py failed with exit code {e.returncode}, continuing")
            _set_run_stage(run_id, 'ingest', status='warning', message='ingest.py 异常退出，继续后续流程')
        except Exception as e:
            ingest_ok = False
            print(f"[fetch] ingest.py error: {e}")
            _set_run_stage(run_id, 'ingest', status='warning', message=f'入库异常: {str(e)[:80]}')

        new_count = _count_inserted_run_items(run_id)

        if ingest_ok:
            _set_run_stage(run_id, 'ingest', status='done', platform='全部平台', percent=50, new_count=new_count, message='')
        else:
            _set_run_stage(run_id, 'ingest', percent=50, new_count=new_count)
        _stage_finish('ingest')
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            progress['total_new'] = new_count

        print(f"[fetch] Ingest complete: {new_count} new items — running full-run AI enrichment")

        _stage_start('ai_enrich')
        _set_run_stage(run_id, 'ai_enrich', status='running', platform='全部平台', percent=55, message='正在 AI 总结')
        ai_ok = _run_summaries(
            progress_stages=(2,),
            run_id=run_id,
            batch_size=5,
            workers=10,
            log_path=ai_log_path,
            progress_run_id=run_id,
        )
        if ai_ok:
            _set_run_stage(run_id, 'ai_enrich', status='done', platform='全部平台', percent=80, message='')
        else:
            ai_message = (
                _ai_enrich_failure_message_from_log(ai_log_path)
                or _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                or 'AI 统一理解失败'
            )
            _set_run_stage(run_id, 'ai_enrich', status='failed', platform='全部平台', percent=80, message=ai_message)
        _stage_finish('ai_enrich')

        if not ai_ok:
            cluster_ok = False
            print("[fetch] AI enrichment failed; skipping run-scoped event publish")
            _set_run_stage(run_id,
                'event_cluster',
                status='warning',
                platform='全部平台',
                percent=80,
                message=_active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER) or 'AI 总结未完成，未发布本轮事件',
            )
        else:
            _stage_start('event_cluster')
            _set_run_stage(run_id, 'event_cluster', status='running', platform='全部平台', percent=85, message='正在事件聚合')
            cluster_stats_path = os.path.join(_run_data_dir(run_id), 'event_cluster_stats.json')
            cluster_cmd = _cluster_pipeline_cmd(run_id, stats_path=cluster_stats_path)
            cluster_timeout = _cluster_pipeline_settings()['timeout_sec']
            try:
                with open('/tmp/info-radar-clustering.log', 'a') as log_f:
                    log_f.write(f"\n===== {datetime.now(timezone.utc).isoformat()} 事件聚合 run #{run_id} =====\n")
                    log_f.write(f"[fetch] command: {' '.join(cluster_cmd)} timeout={cluster_timeout}s\n")
                    log_f.flush()
                    _run_killpg(  # PL-7
                        cluster_cmd,
                        cwd=BASE,
                        timeout=cluster_timeout,
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        env=_unbuffered_env(),
                        check=True,
                    )
                event_cluster_stats = _load_event_cluster_stats(cluster_stats_path)
                _set_run_stage(run_id, 'event_cluster', status='done', platform='全部平台', percent=100, message='')
            except subprocess.TimeoutExpired:
                cluster_ok = False
                print(f"[fetch] clustering pipeline timed out after {cluster_timeout}s")
                partial_published_clusters = _publish_partial_event_run(run_id, "clustering pipeline timed out")
                _set_run_stage(run_id,
                    'event_cluster',
                    status='failed',
                    percent=90,
                    message=(
                        _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                        or (
                            f'事件聚合超时，已发布 {partial_published_clusters} 个已完成事件'
                            if partial_published_clusters
                            else '事件聚合超时'
                        )
                    ),
                )
            except subprocess.CalledProcessError as e:
                cluster_ok = False
                print(f"[fetch] clustering pipeline failed with exit code {e.returncode}")
                partial_published_clusters = _publish_partial_event_run(run_id, "clustering pipeline failed")
                _set_run_stage(run_id,
                    'event_cluster',
                    status='failed',
                    percent=90,
                    message=(
                        _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                        or (
                            f'事件聚合失败，已发布 {partial_published_clusters} 个已完成事件'
                            if partial_published_clusters
                            else '事件聚合失败'
                        )
                    ),
                )
            except Exception as e:
                cluster_ok = False
                print(f"[fetch] clustering pipeline error: {e}")
                partial_published_clusters = _publish_partial_event_run(run_id, "clustering pipeline error")
                message = (
                    f'事件聚合异常，已发布 {partial_published_clusters} 个已完成事件'
                    if partial_published_clusters
                    else f'事件聚合异常: {str(e)[:80]}'
                )
                _set_run_stage(run_id, 'event_cluster', status='failed', percent=90, message=message)
            finally:
                _stage_finish('event_cluster')

        if fetch_all_timed_out or fetch_all_failed or not ingest_ok or not ai_ok or not cluster_ok:
            with _fetch_lock:
                active = _fetch_active_runs.get(run_id)
                progress = active.get('progress') if active else _fetch_progress
                progress['result_status'] = 'partial'
                if not ai_ok:
                    progress['message'] = (
                        _ai_enrich_failure_message_from_log(ai_log_path)
                        or _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                        or '本轮部分完成，AI 总结失败，未发布本轮事件'
                    )
                elif not cluster_ok:
                    progress['message'] = (
                        _active_provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER)
                        or (
                            f'本轮部分完成，已发布 {partial_published_clusters} 个已完成事件，剩余事件聚合待重试'
                            if partial_published_clusters
                            else '本轮部分完成，事件聚合未完成'
                        )
                    )
                else:
                    progress['message'] = '本轮部分完成，抓取脚本进入补偿流程'
        else:
            with _fetch_lock:
                active = _fetch_active_runs.get(run_id)
                progress = active.get('progress') if active else _fetch_progress
                progress['result_status'] = 'success'
                progress['message'] = '本轮抓取完成'

        stats = _fetch_run_stats_current_backend()
        run_stats = {k: v for k, v in stats.items()}
        run_stats['_stage_durations_sec'] = stage_durations
        if event_cluster_stats:
            run_stats['event_cluster'] = event_cluster_stats
        if partial_published_clusters:
            run_stats['_published_clusters_count'] = partial_published_clusters
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            run_stats['_result_status'] = progress.get('result_status')
            run_stats['_new_items_count'] = progress.get('total_new')
            started_at_iso = (active or {}).get('started_at') or progress.get('started_at')
        run_stats['_platform_new_counts'] = _per_platform_new_counts_current_backend(started_at_iso)
        feed_content_changed = bool(
            (run_stats.get('_new_items_count') or 0) > 0
            or partial_published_clusters > 0
            or _event_cluster_published_count(event_cluster_stats) > 0
        )
        _finish_fetch_run_current_backend(run_id, run_stats)

    except Exception as e:
        for stage_id in list(stage_started.keys()):
            _stage_finish(stage_id)
        _finish_fetch_run_current_backend(run_id, {'_stage_durations_sec': stage_durations}, str(e))
        _notify(f'抓取失败: {str(e)[:80]}')
        for plat in ('twitter', 'xiaohongshu', 'bilibili'):
            _update_health_status(plat, 'error', f'全量抓取失败: {str(e)[:80]}')
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            current = progress['current_stage']
            if current < len(progress['stages']):
                progress['stages'][current]['status'] = 'failed'
            progress['result_status'] = 'failed'
            progress['message'] = str(e)[:100]
    finally:
        if heartbeat_stop_event:
            heartbeat_stop_event.set()
        # PL-1: no-op run 不再打冷全站缓存(原先每轮无条件清空+删远端
        # snapshot,叠加 micro 节奏 = 用户每 5 分钟吃一次 3.7s 冷路径)。
        if feed_content_changed:
            _clear_feed_caches_safely()
        else:
            print(f"[fetch] run #{run_id} no-op (0 新增/0 发布),保留 feed 缓存", flush=True)
        _schedule_post_fetch_read_model_refresh(run_id)
        _cleanup_run_artifacts()
        with _fetch_lock:
            finished_progress = copy.deepcopy(
                (_fetch_active_runs.get(run_id) or {}).get('progress') or _fetch_progress
            )
            _fetch_active_runs.pop(run_id, None)
            if _fetch_active_runs:
                latest = max(
                    _fetch_active_runs.values(),
                    key=lambda item: item.get('started_at') or '',
                )
                _fetch_progress = latest.get('progress') or finished_progress
            else:
                _fetch_progress = finished_progress
            _fetch_running = bool(_fetch_active_runs)
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()


def _run_source_micro_fetch(run_id, platform, source, run_source=None):
    global _fetch_running, _fetch_finished_at, _fetch_progress
    run_source = run_source or _micro_fetch_run_source(platform, source)
    source_label = f'{platform}/{source or "all"}'
    run_env = _run_env(run_id)
    source_ok = True
    ingest_ok = True
    ai_ok = True
    cluster_ok = True
    refresh_after_micro = False
    event_cluster_stats = None
    stage_durations = {}
    stage_started = {}
    # PL-1: 同全局 run——no-op micro(约每 5 分钟一次)不清缓存;
    # 预置 True 让异常路径 fail-safe。
    feed_content_changed = True
    heartbeat_stop_event = _start_fetch_run_heartbeat(run_id)

    with _fetch_lock:
        active = _fetch_active_runs.get(run_id)
        if active:
            progress = active.get('progress') or _make_micro_fetch_progress(
                platform,
                source,
                run_id=run_id,
                run_source=run_source,
            )
        else:
            started_at = datetime.now(timezone.utc).isoformat()
            progress = _make_micro_fetch_progress(
                platform,
                source,
                run_id=run_id,
                run_source=run_source,
            )
            progress['started_at'] = started_at
            _fetch_active_runs[run_id] = {
                'source': run_source,
                'started_at': started_at,
                'progress': progress,
            }
        progress['stages'][0]['status'] = 'running'
        progress['percent'] = 1
        _fetch_progress = progress
        _fetch_running = True

    def _stage_start(stage_id):
        stage_started[stage_id] = time.monotonic()

    def _stage_finish(stage_id):
        started = stage_started.pop(stage_id, None)
        if started is not None:
            stage_durations[stage_id] = round(time.monotonic() - started, 2)

    try:
        os.makedirs(_run_data_dir(run_id), exist_ok=True)

        _stage_start('source_fetch')
        _set_run_stage(run_id, 'source_fetch', status='running', platform=source_label, percent=1, message='单源抓取已启动')
        source_ok = bool(_run_source_fetch_step(
            platform,
            source,
            output_root=_run_data_dir(run_id),
            env=run_env,
        ))
        if source_ok:
            _set_run_stage(run_id, 'source_fetch', status='done', platform=source_label, percent=35, message='')
        else:
            _set_run_stage(run_id, 'source_fetch', status='failed', platform=source_label, percent=35, message='不支持的 micro source')
        _stage_finish('source_fetch')

        _stage_start('ingest')
        _set_run_stage(run_id, 'ingest', status='running', platform=source_label, percent=38, message='正在入库')
        try:
            subprocess.run([
                _python_executable(),
                os.path.join(BASE, 'src', 'ingest.py'),
                '--run-id',
                str(run_id),
                '--skip-image-download',
            ],
                cwd=BASE, timeout=600, stderr=subprocess.DEVNULL, env=run_env, check=True)
        except subprocess.TimeoutExpired:
            ingest_ok = False
            print(f"[fetch] micro ingest.py timed out for run #{run_id}", flush=True)
            _set_run_stage(run_id, 'ingest', status='warning', message='ingest.py 超时，继续后续流程')
        except subprocess.CalledProcessError as exc:
            ingest_ok = False
            print(f"[fetch] micro ingest.py failed with exit code {exc.returncode}", flush=True)
            _set_run_stage(run_id, 'ingest', status='warning', message='ingest.py 异常退出，继续后续流程')
        new_count = _count_inserted_run_items(run_id)
        _set_run_stage(
            run_id,
            'ingest',
            status='done' if ingest_ok else None,
            platform=source_label,
            percent=55,
            new_count=new_count,
            message='' if ingest_ok else None,
        )
        _stage_finish('ingest')
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            progress['total_new'] = new_count

        if new_count > 0 and source_ok:
            _stage_start('ai_enrich')
            _set_run_stage(run_id, 'ai_enrich', status='running', platform=source_label, percent=60, message='正在 AI 总结')
            ai_ok = _run_summaries(
                progress_stages=(2,),
                run_id=run_id,
                batch_size=5,
                workers=_micro_enrich_workers(),
                log_path=_ai_log_path(run_id),
                progress_run_id=run_id,
            )
            if ai_ok:
                _set_run_stage(run_id, 'ai_enrich', status='done', platform=source_label, percent=80, message='')
            else:
                _set_run_stage(run_id, 'ai_enrich', status='failed', platform=source_label, percent=80, message='AI 总结失败')
            _stage_finish('ai_enrich')

            if ai_ok:
                _stage_start('event_cluster')
                _set_run_stage(run_id, 'event_cluster', status='running', platform=source_label, percent=85, message='正在事件聚合')
                cluster_stats_path = os.path.join(_run_data_dir(run_id), 'event_cluster_stats.json')
                cluster_cmd = _cluster_pipeline_cmd(run_id, stats_path=cluster_stats_path)
                cluster_timeout = _cluster_pipeline_settings()['timeout_sec']
                try:
                    with open('/tmp/info-radar-clustering.log', 'a') as log_f:
                        log_f.write(f"\n===== {datetime.now(timezone.utc).isoformat()} micro event clustering run #{run_id} {source_label} =====\n")
                        log_f.write(f"[fetch] command: {' '.join(cluster_cmd)} timeout={cluster_timeout}s\n")
                        log_f.flush()
                        _run_killpg(  # PL-7
                            cluster_cmd,
                            cwd=BASE,
                            timeout=cluster_timeout,
                            stdout=log_f,
                            stderr=subprocess.STDOUT,
                            env=_unbuffered_env(),
                            check=True,
                        )
                    event_cluster_stats = _load_event_cluster_stats(cluster_stats_path)
                    _set_run_stage(run_id, 'event_cluster', status='done', platform=source_label, percent=100, message='')
                except subprocess.TimeoutExpired:
                    cluster_ok = False
                    _set_run_stage(run_id, 'event_cluster', status='failed', percent=90, message='事件聚合超时')
                except subprocess.CalledProcessError as exc:
                    cluster_ok = False
                    print(f"[fetch] micro clustering failed with exit code {exc.returncode}", flush=True)
                    _set_run_stage(run_id, 'event_cluster', status='failed', percent=90, message='事件聚合失败')
                finally:
                    _stage_finish('event_cluster')
        else:
            _set_run_stage(run_id, 'ai_enrich', status='done', platform=source_label, percent=80, message='无新增入库，跳过 AI')
            _set_run_stage(run_id, 'event_cluster', status='done', platform=source_label, percent=100, message='无新增入库，跳过聚类')

        result_status = 'success' if (source_ok and ingest_ok and ai_ok and cluster_ok) else 'partial'
        published_clusters = _event_cluster_published_count(event_cluster_stats)
        refresh_after_micro = bool(
            new_count > 0
            and result_status == 'success'
            and published_clusters > 0
        )
        feed_content_changed = bool(new_count > 0 or published_clusters > 0)
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            progress['result_status'] = result_status
            progress['message'] = 'micro-run 完成' if result_status == 'success' else 'micro-run 部分完成'
            started_at_iso = (active or {}).get('started_at') or progress.get('started_at')

        run_stats = _fetch_run_stats_current_backend()
        run_stats['_pipeline_mode'] = 'micro'
        run_stats['_micro_source'] = {'platform': platform, 'source': source or 'all'}
        run_stats['_stage_durations_sec'] = stage_durations
        run_stats['_result_status'] = result_status
        run_stats['_new_items_count'] = new_count
        run_stats['_platform_new_counts'] = _per_platform_new_counts_current_backend(started_at_iso)
        if event_cluster_stats:
            run_stats['event_cluster'] = event_cluster_stats
        if published_clusters:
            run_stats['_published_clusters_count'] = published_clusters
        _finish_fetch_run_current_backend(run_id, run_stats)
    except Exception as exc:
        for stage_id in list(stage_started.keys()):
            _stage_finish(stage_id)
        _finish_fetch_run_current_backend(
            run_id,
            {
                '_pipeline_mode': 'micro',
                '_micro_source': {'platform': platform, 'source': source or 'all'},
                '_stage_durations_sec': stage_durations,
            },
            str(exc),
        )
        with _fetch_lock:
            active = _fetch_active_runs.get(run_id)
            progress = active.get('progress') if active else _fetch_progress
            current = progress.get('current_stage', 0)
            if current < len(progress.get('stages', [])):
                progress['stages'][current]['status'] = 'failed'
            progress['result_status'] = 'failed'
            progress['message'] = str(exc)[:100]
    finally:
        if heartbeat_stop_event:
            heartbeat_stop_event.set()
        # PL-1: no-op micro 不清缓存(见全局 run 同款注释)。
        if feed_content_changed:
            _clear_feed_caches_safely()
        else:
            print(f"[fetch] micro run #{run_id} no-op (0 新增/0 发布),保留 feed 缓存", flush=True)
        _cleanup_run_artifacts()
        if refresh_after_micro:
            _schedule_post_fetch_read_model_refresh(
                run_id,
                highlights_read_model_refresh_min_interval_sec=(
                    _micro_highlights_read_model_refresh_min_interval_sec()
                ),
            )
        with _fetch_lock:
            finished_progress = copy.deepcopy(
                (_fetch_active_runs.get(run_id) or {}).get('progress') or _fetch_progress
            )
            _fetch_active_runs.pop(run_id, None)
            if _fetch_active_runs:
                latest = max(
                    _fetch_active_runs.values(),
                    key=lambda item: item.get('started_at') or '',
                )
                _fetch_progress = latest.get('progress') or finished_progress
            else:
                _fetch_progress = finished_progress
            _fetch_running = bool(_fetch_active_runs)
            _fetch_finished_at = datetime.now(timezone.utc).isoformat()


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


def start_global_fetch(source: str = 'api') -> dict:
    """Start an audited global fetch, failing closed when a remote run is active."""
    global _fetch_running, _fetch_progress
    with _fetch_lock:
        max_concurrent = _max_global_fetch_pipelines()
        running_count = len(_fetch_active_runs)
        if running_count >= max_concurrent:
            return {
                'ok': False,
                'msg': f'Fetch already running (concurrency limit {running_count}/{max_concurrent})',
                'running_count': running_count,
                'max_concurrent': max_concurrent,
            }
        if remote_db.fetch_write_to_remote():
            try:
                if running_count == 0:
                    recovered_runs = recover_stale_remote_fetch_runs()
                    if recovered_runs:
                        print(f"[fetch] recovered stale remote running runs {recovered_runs}", flush=True)
                remote_running = remote_db.has_recent_running_fetch_remote()
            except Exception as exc:
                print(f"[fetch] remote running-run start guard failed closed: {exc}", flush=True)
                return {
                    'ok': False,
                    'msg': 'Fetch already running (remote guard unavailable)',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                }
            if remote_running:
                return {
                    'ok': False,
                    'msg': 'Fetch already running (remote guard)',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                }
            pressure_skip_reason = _remote_db_pressure_skip_reason()
            if pressure_skip_reason:
                return {
                    'ok': False,
                    'msg': f'Fetch skipped ({pressure_skip_reason})',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                    'skip_reason': pressure_skip_reason,
                }
            finish_gap_skip_reason = _scheduler_finish_gap_skip_reason(source)
            if finish_gap_skip_reason:
                return {
                    'ok': False,
                    'msg': f'Fetch skipped ({finish_gap_skip_reason})',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                    'skip_reason': finish_gap_skip_reason,
                }
        run_id = _start_fetch_run_current_backend()
        started_at = datetime.now(timezone.utc).isoformat()
        progress = _make_global_fetch_progress()
        progress.update({
            'run_id': run_id,
            'source': source,
            'started_at': started_at,
            '_fetch_log_path': _fetch_log_path(run_id),
            '_ai_log_path': _ai_log_path(run_id),
        })
        _fetch_active_runs[run_id] = {
            'source': source,
            'started_at': started_at,
            'progress': progress,
        }
        _fetch_progress = progress
        _fetch_running = True
    try:
        t = threading.Thread(
            target=_run_fetch,
            args=(run_id, source),
            name=f'info2action-fetch-{source}',
            daemon=True,
        )
        t.start()
    except Exception:
        with _fetch_lock:
            _fetch_active_runs.pop(run_id, None)
            _fetch_running = bool(_fetch_active_runs)
        raise
    return {
        'ok': True,
        'msg': 'Fetch started',
        'run_id': run_id,
        'running_count': running_count + 1,
        'max_concurrent': max_concurrent,
    }


def start_source_micro_fetch(platform: str, source: str = '') -> dict:
    """Start an audited single-source micro run."""
    global _fetch_running, _fetch_progress
    platform = (platform or '').strip()
    source = (source or '').strip()
    run_source = _micro_fetch_run_source(platform, source)
    with _fetch_lock:
        running_count = len(_fetch_active_runs)
        max_concurrent = 1
        if running_count >= max_concurrent:
            return {
                'ok': False,
                'msg': f'Micro fetch already running (concurrency limit {running_count}/{max_concurrent})',
                'running_count': running_count,
                'max_concurrent': max_concurrent,
            }
        if remote_db.fetch_write_to_remote():
            try:
                if running_count == 0:
                    recovered_runs = recover_stale_remote_fetch_runs()
                    if recovered_runs:
                        print(f"[fetch] recovered stale remote running runs {recovered_runs}", flush=True)
                remote_running = remote_db.has_recent_running_fetch_remote()
            except Exception as exc:
                print(f"[fetch] micro remote running-run start guard failed closed: {exc}", flush=True)
                return {
                    'ok': False,
                    'msg': 'Micro fetch already running (remote guard unavailable)',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                }
            if remote_running:
                return {
                    'ok': False,
                    'msg': 'Micro fetch already running (remote guard)',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                }
            pressure_skip_reason = _remote_db_pressure_skip_reason()
            if pressure_skip_reason:
                return {
                    'ok': False,
                    'msg': f'Micro fetch skipped ({pressure_skip_reason})',
                    'running_count': running_count,
                    'max_concurrent': max_concurrent,
                    'skip_reason': pressure_skip_reason,
                }
        run_id = _start_fetch_run_current_backend()
        started_at = datetime.now(timezone.utc).isoformat()
        progress = _make_micro_fetch_progress(
            platform,
            source,
            run_id=run_id,
            run_source=run_source,
        )
        progress.update({'started_at': started_at})
        _fetch_active_runs[run_id] = {
            'source': run_source,
            'started_at': started_at,
            'progress': progress,
        }
        _fetch_progress = progress
        _fetch_running = True
    try:
        thread = threading.Thread(
            target=_run_source_micro_fetch,
            args=(run_id, platform, source, run_source),
            name=_micro_fetch_thread_name(platform, source),
            daemon=True,
        )
        thread.start()
    except Exception:
        with _fetch_lock:
            _fetch_active_runs.pop(run_id, None)
            _fetch_running = bool(_fetch_active_runs)
        raise
    return {
        'ok': True,
        'msg': f'Micro fetch started: {platform}/{source or "all"}',
        'run_id': run_id,
        'source': run_source,
        'running_count': running_count + 1,
        'max_concurrent': max_concurrent,
    }


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
                tw_queries = sq.get('twitter', [])
                xhs_queries = sq.get('xiaohongshu', [])
                xhs_enabled = _is_platform_enabled('xiaohongshu')
                since_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                errors = []
                try:
                    cfg = load_json(os.path.join(BASE, 'config', 'config.json')) or {}
                    subprocess.run([CLI['twitter'], 'feed', '-t', 'for-you', '-n',
                        str(cfg.get('twitter', {}).get('for_you_count', 50)), '-o',
                        os.path.join(BASE, 'data', 'sources', 'twitter', '2-for-you-feed.json')],
                        stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
                    subprocess.run([CLI['twitter'], 'feed', '-t', 'following', '-n',
                        str(cfg.get('twitter', {}).get('following_count', 50)), '-o',
                        os.path.join(BASE, 'data', 'sources', 'twitter', '1-following-feed.json')],
                        stderr=subprocess.DEVNULL, timeout=90, env=_clean_env())
                except Exception as e:
                    errors.append(f"Twitter feed: {e}")
                for q in tw_queries[:3]:
                    safe = q.replace(' ', '_')[:50]
                    try:
                        subprocess.run([CLI['twitter'], 'search', q, '-t', 'latest',
                            '--since', since_date, '-n', '30', '-o',
                            os.path.join(BASE, 'data', 'sources', 'twitter', f'search-{safe}.json')],
                            stderr=subprocess.DEVNULL, timeout=60, env=_clean_env())
                    except Exception as e:
                        errors.append(f"Twitter search: {e}")
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
                conn_pre = db.get_conn()
                count_before = conn_pre.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                conn_pre.close()
                subprocess.run([_python_executable(), os.path.join(BASE, 'src', 'ingest.py')],
                    cwd=BASE, timeout=120, stderr=subprocess.DEVNULL)
                conn_post = db.get_conn()
                count_after = conn_post.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                conn_post.close()
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
