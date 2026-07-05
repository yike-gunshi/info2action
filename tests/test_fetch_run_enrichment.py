import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import db as db_mod
from routes import fetch as fetch_route


@pytest.fixture(autouse=True)
def _force_local_fetch_backend(monkeypatch):
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_STORAGE_MODE', 'local')
    monkeypatch.setenv('INFO2ACTION_FETCH_WRITE_BACKEND', 'sqlite')


def test_run_fetch_enrichment_uses_bounded_run_limit(monkeypatch):
    # PL-5(B6): run 模式不再 --limit 0 无限量(2GB 小机内存保护),
    # 默认 800(INFO2ACTION_ENRICH_RUN_LIMIT 可调),漏网由下轮积压重试消化
    monkeypatch.delenv('INFO2ACTION_ENRICH_RUN_LIMIT', raising=False)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['timeout'] = kwargs.get('timeout')
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))

    ok = fetch_route._run_summaries(run_id=42, batch_size=5, workers=20)

    assert ok is True
    assert captured['cmd'] == [
        fetch_route._python_executable(),
        '-u',
        os.path.join(fetch_route.BASE, 'src', 'enrich_items.py'),
        '--limit',
        '800',
        '--run-id',
        '42',
        '--run-items-scope',
        'inserted',
        '--batch-size',
        '5',
        '--workers',
        '20',
    ]
    assert captured['timeout'] == 7200


def test_manual_enrichment_limit_is_preserved(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['timeout'] = kwargs.get('timeout')
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))

    ok = fetch_route._run_summaries(limit=25, run_id=42)

    assert ok is True
    assert '--limit' in captured['cmd']
    assert captured['cmd'][captured['cmd'].index('--limit') + 1] == '25'
    assert captured['timeout'] == 900


def test_run_summaries_surfaces_remote_db_transient_failure(monkeypatch, tmp_path):
    log_path = tmp_path / 'ai-enrich.log'
    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': 88, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[88] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress
    monkeypatch.setattr(fetch_route, '_active_provider_message', lambda *_args: None)

    def fake_run(_cmd, **kwargs):
        kwargs['stdout'].write(
            'remote_db_transient_exhausted '
            'operation=query_pending_enrichment_items_remote attempts=3 '
            'error=EDBHANDLEREXITED connection to database closed\n'
        )
        kwargs['stdout'].flush()
        return types.SimpleNamespace(returncode=1)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))

    ok = fetch_route._run_summaries(
        run_id=88,
        progress_stages=(2,),
        log_path=str(log_path),
        progress_run_id=88,
    )

    assert ok is False
    assert progress['stages'][2]['message'] == 'Supabase 连接异常，AI 队列读取/写入失败'
    assert progress['message'] == 'Supabase 连接异常，AI 队列读取/写入失败'
    fetch_route._fetch_active_runs.clear()


def test_run_env_exports_current_python_for_fetch_shell(monkeypatch):
    monkeypatch.setattr(fetch_route.sys, 'executable', '/tmp/i2a-venv/bin/python')

    env = fetch_route._run_env(42)

    assert env['PYTHON_BIN'] == '/tmp/i2a-venv/bin/python'
    assert env['PATH'].startswith('/tmp/i2a-venv/bin:')
    assert env['INFO2ACTION_DATA_DIR'].endswith('/data/run_sources/42')


def test_run_source_fetch_step_writes_to_output_root(monkeypatch, tmp_path):
    captured = {}
    run_root = tmp_path / 'run_sources' / '3110'

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(fetch_route, 'load_json', lambda _path: {'twitter': {'following_count': 17}})

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = kwargs.get('env')
        return types.SimpleNamespace(returncode=0)

    env = {'INFO2ACTION_DATA_DIR': str(run_root)}
    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))

    ok = fetch_route._run_source_fetch_step(
        'twitter',
        'following',
        output_root=str(run_root),
        env=env,
    )

    assert ok is True
    assert captured['cmd'][-1] == str(run_root / 'sources' / 'twitter' / '1-following-feed.json')
    assert captured['env'] is env


def test_has_active_fetch_runs_checks_remote_running_runs(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: True)
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    assert fetch_route.has_active_fetch_runs() is True


def test_has_active_fetch_runs_fails_closed_when_remote_guard_errors(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])

    def fail_guard():
        raise fetch_route.remote_db.RemoteDBError('pool checkout timeout')

    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', fail_guard)
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    assert fetch_route.has_active_fetch_runs() is True


def test_has_active_fetch_runs_recovers_stale_remote_runs_before_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: calls.append('recover') or [2358])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: calls.append('guard') or False)
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    assert fetch_route.has_active_fetch_runs() is False
    assert calls == ['recover', 'guard']


def test_recover_orphaned_fetch_runs_from_previous_process_marks_remote_rows(monkeypatch):
    from datetime import datetime, timezone

    cutoff = datetime(2026, 5, 20, 3, 10, 54, tzinfo=timezone.utc)
    calls = {}
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '1')
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)

    def fake_mark(*, started_before, heartbeat_stale_before, reason):
        calls['started_before'] = started_before
        calls['heartbeat_stale_before'] = heartbeat_stale_before
        calls['reason'] = reason
        return [1514]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_orphaned_fetch_runs_remote', fake_mark)

    assert fetch_route.recover_orphaned_fetch_runs_from_previous_process(cutoff) == [1514]
    assert calls['started_before'] == cutoff
    assert calls['heartbeat_stale_before'].tzinfo is not None
    assert 'previous backend process stopped' in calls['reason']


def test_recover_orphaned_fetch_runs_skips_remote_rows_when_scheduler_disabled(monkeypatch):
    from datetime import datetime, timezone

    cutoff = datetime(2026, 5, 25, 9, 39, 33, tzinfo=timezone.utc)
    calls = {}
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '0')
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)

    def fake_mark(*, started_before, heartbeat_stale_before, reason):
        calls['started_before'] = started_before
        calls['heartbeat_stale_before'] = heartbeat_stale_before
        calls['reason'] = reason
        return [1743]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_orphaned_fetch_runs_remote', fake_mark)

    assert fetch_route.recover_orphaned_fetch_runs_from_previous_process(cutoff) == []
    assert calls == {}


def test_recover_orphaned_fetch_runs_honors_explicit_recovery_disable(monkeypatch):
    from datetime import datetime, timezone

    cutoff = datetime(2026, 6, 25, 1, 15, 56, tzinfo=timezone.utc)
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '1')
    monkeypatch.setenv('INFO2ACTION_FETCH_ORPHAN_RECOVERY', '0')
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'mark_orphaned_fetch_runs_remote',
        lambda **_kwargs: pytest.fail('startup recovery should honor explicit disable'),
    )

    assert fetch_route.recover_orphaned_fetch_runs_from_previous_process(cutoff) == []


def test_recover_orphaned_fetch_runs_defaults_to_process_start(monkeypatch):
    from datetime import datetime, timezone

    process_started_at = datetime(2026, 5, 20, 9, 43, 30, tzinfo=timezone.utc)
    calls = {}
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '1')
    monkeypatch.setattr(fetch_route, '_fetch_process_started_at', process_started_at)
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)

    def fake_mark(*, started_before, heartbeat_stale_before, reason):
        calls['started_before'] = started_before
        calls['heartbeat_stale_before'] = heartbeat_stale_before
        calls['reason'] = reason
        return [1524]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_orphaned_fetch_runs_remote', fake_mark)

    assert fetch_route.recover_orphaned_fetch_runs_from_previous_process() == [1524]
    assert calls['started_before'] == process_started_at
    assert calls['heartbeat_stale_before'].tzinfo is not None
    assert 'startup recovery' in calls['reason']


def test_recover_stale_remote_fetch_runs_marks_rows_with_expired_heartbeat(monkeypatch):
    calls = {}
    monkeypatch.setenv('INFO2ACTION_BACKEND_HOURLY_FETCH', '1')
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_run_heartbeat_grace_seconds', lambda: 600)

    def fake_mark(*, started_before, heartbeat_stale_before, reason):
        calls['started_before'] = started_before
        calls['heartbeat_stale_before'] = heartbeat_stale_before
        calls['reason'] = reason
        return [2358]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_orphaned_fetch_runs_remote', fake_mark)

    assert fetch_route.recover_stale_remote_fetch_runs() == [2358]
    assert calls['started_before'].tzinfo is not None
    assert calls['heartbeat_stale_before'].tzinfo is not None
    assert calls['started_before'] > calls['heartbeat_stale_before']
    assert 'heartbeat expired' in calls['reason']


def test_touch_fetch_run_heartbeat_uses_remote_owner(monkeypatch):
    calls = {}
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, '_fetch_process_owner', 'unit-host:123:456')

    def fake_touch(*, run_id, owner):
        calls['run_id'] = run_id
        calls['owner'] = owner

    monkeypatch.setattr(fetch_route.remote_db, 'touch_fetch_run_heartbeat_remote', fake_touch)

    fetch_route._touch_fetch_run_heartbeat(1748)

    assert calls == {'run_id': 1748, 'owner': 'unit-host:123:456'}


def test_interrupt_active_fetch_runs_for_shutdown_marks_remote_rows(monkeypatch):
    from datetime import datetime, timezone

    calls = {}
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)

    def fake_mark(*, run_ids, reason):
        calls['run_ids'] = run_ids
        calls['reason'] = reason
        return [1524]

    monkeypatch.setattr(fetch_route.remote_db, 'mark_fetch_runs_interrupted_remote', fake_mark)

    class FakeDateTime:
        @staticmethod
        def now(_tz=None):
            return datetime(2026, 5, 20, 9, 43, 27, tzinfo=timezone.utc)

    monkeypatch.setattr(fetch_route, 'datetime', FakeDateTime)

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': 1524, 'source': 'backend_30min_cron', 'started_at': '2026-05-20T09:30:01+00:00'})
    progress['stages'][2]['status'] = 'running'
    progress['current_stage'] = 2
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[1524] = {
        'source': 'backend_30min_cron',
        'started_at': progress['started_at'],
        'progress': progress,
    }
    fetch_route._fetch_progress = progress
    fetch_route._fetch_running = True

    marked = fetch_route.interrupt_active_fetch_runs_for_shutdown()

    assert marked == [1524]
    assert calls['run_ids'] == [1524]
    assert 'shutdown' in calls['reason']
    assert fetch_route._fetch_active_runs == {}
    assert fetch_route._fetch_running is False
    assert fetch_route._fetch_progress['result_status'] == 'interrupted'
    assert fetch_route._fetch_progress['stages'][2]['status'] == 'failed'


def test_wait_for_active_fetch_runs_to_finish_returns_true_when_idle():
    fetch_route._fetch_active_runs.clear()

    assert fetch_route.wait_for_active_fetch_runs_to_finish(0) is True


def test_wait_for_active_fetch_runs_to_finish_times_out_when_active(monkeypatch):
    monotonic_values = iter([10.0, 10.0, 10.6])
    sleeps = []
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[1525] = {'source': 'unit', 'started_at': 'now'}

    monkeypatch.setattr(fetch_route.time, 'monotonic', lambda: next(monotonic_values))
    monkeypatch.setattr(fetch_route.time, 'sleep', lambda seconds: sleeps.append(seconds))

    assert fetch_route.wait_for_active_fetch_runs_to_finish(0.5, poll_interval_sec=5) is False
    assert sleeps == [0.5]
    fetch_route._fetch_active_runs.clear()


def test_cluster_pipeline_cmd_uses_configured_concurrency(monkeypatch, tmp_path):
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'config.json').write_text(
        json.dumps({
            'global': {
                'clustering': {
                    'stage2_judge_workers': 24,
                    'stage2_judge_min_interval_sec': 0.25,
                    'cluster_summary_workers': 8,
                    'pipeline_timeout_sec': 5400,
                },
            },
        })
    )
    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))

    cmd = fetch_route._cluster_pipeline_cmd(run_id=42)
    settings = fetch_route._cluster_pipeline_settings()

    assert cmd == [
        fetch_route._python_executable(),
        os.path.join(str(tmp_path), 'src', 'clustering', 'pipeline.py'),
        '--run-id',
        '42',
        '--run-items-scope',
        'inserted',
        '--judge-workers',
        '24',
        '--judge-min-interval-sec',
        '0.25',
        '--summary-workers',
        '8',
    ]
    assert settings['timeout_sec'] == 5400


def test_cluster_pipeline_cmd_env_overrides_configured_concurrency(monkeypatch, tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'config.json').write_text(
        json.dumps({
            'global': {
                'clustering': {
                    'stage2_judge_workers': 10,
                    'stage2_judge_min_interval_sec': 0.35,
                    'cluster_summary_workers': 10,
                    'pipeline_timeout_sec': 7200,
                },
            },
        })
    )
    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CLUSTER_JUDGE_WORKERS', '3')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_JUDGE_MIN_INTERVAL_SEC', '0.5')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_SUMMARY_WORKERS', '3')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_PIPELINE_TIMEOUT_SEC', '3600')

    cmd = fetch_route._cluster_pipeline_cmd(run_id=43)
    settings = fetch_route._cluster_pipeline_settings()

    assert cmd[cmd.index('--judge-workers') + 1] == '3'
    assert cmd[cmd.index('--judge-min-interval-sec') + 1] == '0.5'
    assert cmd[cmd.index('--summary-workers') + 1] == '3'
    assert settings['timeout_sec'] == 3600


def test_cluster_pipeline_cmd_defaults_to_previous_safe_values(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))

    cmd = fetch_route._cluster_pipeline_cmd(run_id=7)
    settings = fetch_route._cluster_pipeline_settings()

    assert cmd[cmd.index('--judge-workers') + 1] == '20'
    assert cmd[cmd.index('--judge-min-interval-sec') + 1] == '0.8'
    assert cmd[cmd.index('--summary-workers') + 1] == '1'
    assert settings['timeout_sec'] == 1800


def test_cluster_pipeline_cmd_can_write_stats_path(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))

    cmd = fetch_route._cluster_pipeline_cmd(run_id=7, stats_path='/tmp/event-cluster.json')

    assert cmd[cmd.index('--stats-path') + 1] == '/tmp/event-cluster.json'


def test_run_fetch_publishes_partial_events_after_cluster_timeout(monkeypatch, tmp_path):
    finished = {}
    published = []
    run_id = 77

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_env_enabled', lambda *_args, **_kwargs: False)

    def fake_finish(done_run_id, stats, error=None):
        finished['run_id'] = done_run_id
        finished['stats'] = stats
        finished['error'] = error

    def fake_publish(done_run_id, reason):
        published.append((done_run_id, reason))
        return 5

    def fake_run(cmd, **kwargs):
        if any(str(part).endswith('pipeline.py') for part in cmd):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get('timeout'))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', fake_finish)
    monkeypatch.setattr(fetch_route, '_publish_partial_event_run', fake_publish)
    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_cmd', lambda _run_id, **_kwargs: ['python3', 'pipeline.py'])
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {
        'judge_workers': 20,
        'judge_min_interval_sec': 0.8,
        'summary_workers': 1,
        'timeout_sec': 12,
    })

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    assert published == [(run_id, 'clustering pipeline timed out')]
    assert finished['error'] is None
    assert finished['stats']['_result_status'] == 'partial'
    assert finished['stats']['_new_items_count'] == 3
    assert finished['stats']['_published_clusters_count'] == 5
    assert '已发布 5 个已完成事件' in progress['message']
    fetch_route._fetch_active_runs.clear()


def test_run_fetch_records_event_cluster_stats(monkeypatch, tmp_path):
    finished = {}
    run_id = 78
    cluster_stats = {
        'pending_items': 3,
        'touched_clusters': 2,
        'timings_sec': {'recall_candidates': 1.2, 'publish_run': 0.3},
    }

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {})
    monkeypatch.setattr(fetch_route, '_env_enabled', lambda *_args, **_kwargs: False)

    def fake_finish(done_run_id, stats, error=None):
        finished['run_id'] = done_run_id
        finished['stats'] = stats
        finished['error'] = error

    def fake_run(cmd, **_kwargs):
        if any(str(part).endswith('pipeline.py') for part in cmd):
            stats_path = cmd[cmd.index('--stats-path') + 1]
            with open(stats_path, 'w') as f:
                json.dump(cluster_stats, f)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', fake_finish)
    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {
        'judge_workers': 20,
        'judge_min_interval_sec': 0.8,
        'summary_workers': 1,
        'timeout_sec': 12,
    })

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    assert finished['error'] is None
    assert finished['stats']['event_cluster'] == cluster_stats
    fetch_route._fetch_active_runs.clear()


def test_max_global_fetch_pipelines_defaults_to_one(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_MAX_FETCH_PIPELINES', raising=False)

    assert fetch_route._max_global_fetch_pipelines() == 1


def test_platform_prewarm_enabled_honors_current_and_legacy_env(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_PREWARM_PLATFORMS', raising=False)
    monkeypatch.delenv('INFO2ACTION_PLATFORMS_CACHE_PREWARM', raising=False)
    assert fetch_route._platform_prewarm_enabled() is True

    monkeypatch.setenv('INFO2ACTION_PLATFORMS_CACHE_PREWARM', '0')
    assert fetch_route._platform_prewarm_enabled() is False

    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '1')
    assert fetch_route._platform_prewarm_enabled() is True

    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    assert fetch_route._platform_prewarm_enabled() is False


def test_info_read_model_refresh_min_interval_is_configurable(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_INFO_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', raising=False)
    assert fetch_route._info_read_model_refresh_min_interval_sec() == 600

    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '1800')
    assert fetch_route._info_read_model_refresh_min_interval_sec() == 1800

    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '0')
    assert fetch_route._info_read_model_refresh_min_interval_sec() == 0

    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '-1')
    assert fetch_route._info_read_model_refresh_min_interval_sec() == 600


def test_highlights_read_model_refresh_min_interval_is_configurable(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', raising=False)
    assert fetch_route._highlights_read_model_refresh_min_interval_sec() == 600

    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '1800')
    assert fetch_route._highlights_read_model_refresh_min_interval_sec() == 1800

    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '0')
    assert fetch_route._highlights_read_model_refresh_min_interval_sec() == 0

    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '-1')
    assert fetch_route._highlights_read_model_refresh_min_interval_sec() == 600


def test_micro_highlights_read_model_refresh_min_interval_is_configurable(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', raising=False)
    assert fetch_route._micro_highlights_read_model_refresh_min_interval_sec() == 120

    monkeypatch.setenv('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '300')
    assert fetch_route._micro_highlights_read_model_refresh_min_interval_sec() == 300

    monkeypatch.setenv('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '0')
    assert fetch_route._micro_highlights_read_model_refresh_min_interval_sec() == 0

    monkeypatch.setenv('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '-1')
    assert fetch_route._micro_highlights_read_model_refresh_min_interval_sec() == 120


def test_run_fetch_refreshes_info_read_model_when_platform_prewarm_disabled(monkeypatch, tmp_path):
    run_id = 1673
    refresh_calls = []

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '1')
    monkeypatch.delenv('INFO2ACTION_REFRESH_PLATFORM_MV_AFTER_FETCH', raising=False)
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {})
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_cmd', lambda _run_id, **_kwargs: ['python3', 'pipeline.py'])

    def fake_run(_cmd, **_kwargs):
        return types.SimpleNamespace(returncode=0)

    def fake_prewarm_platforms(**_kwargs):
        pytest.fail('platform cache prewarm should remain disabled')

    def fake_refresh_info_read_model_if_stale(**kwargs):
        refresh_calls.append(kwargs)
        return {'ok': True}

    class InlineThread:
        def __init__(self, *, target, daemon=None, name=None, args=(), kwargs=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route.remote_db, 'prewarm_platforms', fake_prewarm_platforms)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_info_read_model_if_stale',
        fake_refresh_info_read_model_if_stale,
    )
    monkeypatch.setattr(fetch_route.threading, 'Thread', InlineThread)

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    assert refresh_calls == [{'min_interval_sec': 600}]
    fetch_route._fetch_active_runs.clear()


def test_run_fetch_honors_info_read_model_refresh_flag(monkeypatch, tmp_path):
    run_id = 1675

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL_REFRESH', '0')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '0')
    monkeypatch.delenv('INFO2ACTION_REFRESH_PLATFORM_MV_AFTER_FETCH', raising=False)
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {})
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_cmd', lambda _run_id, **_kwargs: ['python3', 'pipeline.py'])

    def fake_run(_cmd, **_kwargs):
        return types.SimpleNamespace(returncode=0)

    def fake_refresh_info_read_model_if_stale(**_kwargs):
        pytest.fail('info read model refresh should honor INFO2ACTION_INFO_READ_MODEL_REFRESH=0')

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_info_read_model_if_stale',
        fake_refresh_info_read_model_if_stale,
    )

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    fetch_route._fetch_active_runs.clear()


def test_run_fetch_refreshes_highlights_read_model_when_platform_prewarm_disabled(monkeypatch, tmp_path):
    run_id = 1674
    refresh_calls = []
    thread_daemons = []

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '0')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '1800')
    monkeypatch.delenv('INFO2ACTION_REFRESH_PLATFORM_MV_AFTER_FETCH', raising=False)
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {})
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_cmd', lambda _run_id, **_kwargs: ['python3', 'pipeline.py'])

    def fake_run(_cmd, **_kwargs):
        return types.SimpleNamespace(returncode=0)

    def fake_prewarm_platforms(**_kwargs):
        pytest.fail('platform cache prewarm should remain disabled')

    def fake_refresh_info_read_model_if_stale(**_kwargs):
        pytest.fail('info read model refresh should remain disabled')

    def fake_refresh_highlights_read_model_if_stale(**kwargs):
        refresh_calls.append(kwargs)
        return {'ok': True}

    class InlineThread:
        def __init__(self, *, target, daemon=None, name=None, args=(), kwargs=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
            thread_daemons.append(daemon)

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route.remote_db, 'prewarm_platforms', fake_prewarm_platforms)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_info_read_model_if_stale',
        fake_refresh_info_read_model_if_stale,
    )
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_highlights_read_model_if_stale',
        fake_refresh_highlights_read_model_if_stale,
    )
    monkeypatch.setattr(fetch_route.threading, 'Thread', InlineThread)

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    assert refresh_calls == [{'min_interval_sec': 1800}]
    # PL-11(2026-07-04): refresh 线程改 daemon=True——非 daemon 会在 systemd
    # stop 时阻塞解释器最长 180s,破坏秒级重启;refresh 幂等,下轮自动补跑。
    assert thread_daemons == [True]
    fetch_route._fetch_active_runs.clear()


def test_run_fetch_honors_highlights_read_model_refresh_flag(monkeypatch, tmp_path):
    run_id = 1676

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '0')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH', '0')
    monkeypatch.delenv('INFO2ACTION_REFRESH_PLATFORM_MV_AFTER_FETCH', raising=False)
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 3)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {})
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_cmd', lambda _run_id, **_kwargs: ['python3', 'pipeline.py'])

    def fake_run(_cmd, **_kwargs):
        return types.SimpleNamespace(returncode=0)

    def fake_refresh_highlights_read_model_if_stale(**_kwargs):
        pytest.fail('highlights refresh should honor INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH=0')

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_highlights_read_model_if_stale',
        fake_refresh_highlights_read_model_if_stale,
    )

    progress = fetch_route._make_global_fetch_progress()
    progress.update({'run_id': run_id, 'source': 'unit', 'started_at': 'now'})
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'unit',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_fetch(run_id, 'unit')

    fetch_route._fetch_active_runs.clear()


def test_start_global_fetch_allows_configured_two_global_pipelines(monkeypatch):
    started = []
    run_ids = iter([101, 102])

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(fetch_route.db, 'get_conn', lambda: DummyConn())
    monkeypatch.setattr(fetch_route.db, 'start_fetch_run', lambda _conn: next(run_ids))
    monkeypatch.setenv('INFO2ACTION_MAX_FETCH_PIPELINES', '2')

    class DummyThread:
        def __init__(self, *, target, args=(), name, daemon):
            self.record = {
                'target': target,
                'args': args,
                'name': name,
                'daemon': daemon,
            }

        def start(self):
            self.record['called'] = True
            started.append(self.record)

    monkeypatch.setattr(fetch_route.threading, 'Thread', DummyThread)
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    first = fetch_route.start_global_fetch('unit')
    second = fetch_route.start_global_fetch('unit')
    skipped = fetch_route.start_global_fetch('unit')

    assert first['ok'] is True
    assert first['run_id'] == 101
    assert second['ok'] is True
    assert second['run_id'] == 102
    assert skipped['ok'] is False
    assert 'concurrency limit 2/2' in skipped['msg']
    assert fetch_route._fetch_running is True
    assert len(fetch_route._fetch_active_runs) == 2
    assert started[0]['target'] is fetch_route._run_fetch
    assert started[0]['args'] == (101, 'unit')
    assert started[0]['name'] == 'info2action-fetch-unit'
    assert started[0]['daemon'] is True
    assert started[0]['called'] is True


def test_start_global_fetch_remote_backend_blocks_when_remote_run_active(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: True)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('remote start should be blocked by running guard'),
    )
    monkeypatch.setenv('INFO2ACTION_MAX_FETCH_PIPELINES', '1')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_global_fetch('unit')

    assert result['ok'] is False
    assert result['msg'] == 'Fetch already running (remote guard)'
    assert result['running_count'] == 0
    assert result['max_concurrent'] == 1
    assert fetch_route._fetch_active_runs == {}


def test_start_global_fetch_remote_backend_recovers_stale_runs_before_guard(monkeypatch):
    calls = []
    started = []

    class DummyThread:
        def __init__(self, *, target, args=(), name, daemon):
            self.record = {
                'target': target,
                'args': args,
                'name': name,
                'daemon': daemon,
            }

        def start(self):
            self.record['called'] = True
            started.append(self.record)

    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: calls.append('recover') or [2358])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: calls.append('guard') or False)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'remote_db_pressure',
        lambda: {'ok': True, 'pressure': False, 'reasons': []},
    )
    monkeypatch.setattr(fetch_route.remote_db, 'start_fetch_run_remote', lambda _conn=None: 2359)
    monkeypatch.setattr(fetch_route.threading, 'Thread', DummyThread)
    monkeypatch.setenv('INFO2ACTION_MAX_FETCH_PIPELINES', '1')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_global_fetch('unit')

    assert result['ok'] is True
    assert result['run_id'] == 2359
    assert calls == ['recover', 'guard']
    assert started[0]['args'] == (2359, 'unit')


def test_start_global_fetch_remote_backend_skips_stale_recovery_when_local_run_active(monkeypatch):
    calls = []

    def fail_recovery():
        pytest.fail('local active runs should not be marked stale by start guard')

    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', fail_recovery)
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: calls.append('guard') or True)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('remote start should be blocked by running guard'),
    )
    monkeypatch.setenv('INFO2ACTION_MAX_FETCH_PIPELINES', '2')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[2359] = {'source': 'unit', 'started_at': 'now'}
    fetch_route._fetch_running = True

    result = fetch_route.start_global_fetch('unit')

    assert result['ok'] is False
    assert result['msg'] == 'Fetch already running (remote guard)'
    assert result['running_count'] == 1
    assert result['max_concurrent'] == 2
    assert calls == ['guard']
    assert sorted(fetch_route._fetch_active_runs) == [2359]

    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False


def test_start_global_fetch_remote_backend_fails_closed_when_remote_guard_errors(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])

    def fail_guard():
        raise fetch_route.remote_db.RemoteDBError('pool checkout timeout')

    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', fail_guard)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('remote start should be blocked when guard errors'),
    )
    monkeypatch.setenv('INFO2ACTION_MAX_FETCH_PIPELINES', '1')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_global_fetch('unit')

    assert result['ok'] is False
    assert result['msg'] == 'Fetch already running (remote guard unavailable)'
    assert result['running_count'] == 0
    assert result['max_concurrent'] == 1
    assert fetch_route._fetch_active_runs == {}

    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False


def test_start_global_fetch_remote_backend_skips_when_remote_db_pressure_active(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: False)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'remote_db_pressure',
        lambda: {'ok': True, 'pressure': True, 'reasons': ['recent_statement_timeout']},
    )
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('remote start should be skipped under pressure'),
    )
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_global_fetch('backend_30min_cron')

    assert result['ok'] is False
    assert result['skip_reason'] == 'remote_db_pressure:recent_statement_timeout'
    assert 'Fetch skipped' in result['msg']
    assert fetch_route._fetch_active_runs == {}


def test_start_global_fetch_scheduler_observes_finish_gap(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: False)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'remote_db_pressure',
        lambda: {'ok': True, 'pressure': False, 'reasons': []},
    )
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_finished_fetch_remote', lambda *, minutes: True)
    monkeypatch.setenv('INFO2ACTION_BACKEND_FETCH_FINISH_GAP_MINUTES', '30')
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('scheduler start should be skipped during finish gap'),
    )
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_global_fetch('backend_30min_cron')

    assert result['ok'] is False
    assert result['skip_reason'] == 'remote_fetch_finish_gap:30m'
    assert fetch_route._fetch_active_runs == {}


def test_start_source_micro_fetch_remote_pressure_skips_before_creating_run(monkeypatch):
    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: False)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'remote_db_pressure',
        lambda: {'ok': True, 'pressure': True, 'reasons': ['recent_statement_timeout']},
    )
    monkeypatch.setattr(
        fetch_route.remote_db,
        'start_fetch_run_remote',
        lambda _conn=None: pytest.fail('micro run should not start under DB pressure'),
    )
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_source_micro_fetch('twitter', 'following')

    assert result['ok'] is False
    assert result['skip_reason'] == 'remote_db_pressure:recent_statement_timeout'
    assert fetch_route._fetch_active_runs == {}


def test_start_source_micro_fetch_creates_formal_micro_run(monkeypatch):
    started = []

    class DummyThread:
        def __init__(self, *, target, args=(), name, daemon):
            self.record = {
                'target': target,
                'args': args,
                'name': name,
                'daemon': daemon,
            }

        def start(self):
            self.record['called'] = True
            started.append(self.record)

    monkeypatch.setattr(fetch_route.remote_db, 'fetch_write_to_remote', lambda: True)
    monkeypatch.setattr(fetch_route, 'recover_stale_remote_fetch_runs', lambda: [])
    monkeypatch.setattr(fetch_route.remote_db, 'has_recent_running_fetch_remote', lambda: False)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'remote_db_pressure',
        lambda: {'ok': True, 'pressure': False, 'reasons': []},
    )
    monkeypatch.setattr(fetch_route.remote_db, 'start_fetch_run_remote', lambda _conn=None: 3101)
    monkeypatch.setattr(fetch_route.threading, 'Thread', DummyThread)
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False

    result = fetch_route.start_source_micro_fetch('twitter', 'following')

    assert result['ok'] is True
    assert result['run_id'] == 3101
    assert result['source'] == 'micro:twitter:following'
    assert fetch_route._fetch_running is True
    progress = fetch_route._fetch_active_runs[3101]['progress']
    assert progress['mode'] == 'micro'
    assert progress['platform'] == 'twitter'
    assert progress['source_name'] == 'following'
    assert [stage['id'] for stage in progress['stages']] == [
        'source_fetch',
        'ingest',
        'ai_enrich',
        'event_cluster',
    ]
    assert started[0]['target'] is fetch_route._run_source_micro_fetch
    assert started[0]['args'] == (3101, 'twitter', 'following', 'micro:twitter:following')
    assert started[0]['name'] == 'info2action-micro-fetch-twitter-following'
    assert started[0]['daemon'] is True
    assert started[0]['called'] is True
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False


def test_run_source_micro_fetch_uses_run_scoped_ingest_enrich_and_cluster(monkeypatch, tmp_path):
    run_id = 3111
    commands = []
    source_calls = []
    enrich_calls = []
    finished = {}

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setattr(fetch_route, '_start_fetch_run_heartbeat', lambda _run_id: None)
    def fake_source_fetch_step(platform, source, **kwargs):
        source_calls.append((platform, source, kwargs.get('output_root'), kwargs.get('env', {}).get('INFO2ACTION_DATA_DIR')))
        return True

    monkeypatch.setattr(fetch_route, '_run_source_fetch_step', fake_source_fetch_step)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 4)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {'twitter': 4})
    monkeypatch.setattr(fetch_route, '_env_enabled', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(fetch_route, '_remote_db_pressure_skip_reason', lambda: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(
        fetch_route,
        '_cluster_pipeline_cmd',
        lambda _run_id, **_kwargs: ['python3', 'pipeline.py', '--run-id', str(_run_id)],
    )

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return types.SimpleNamespace(returncode=0)

    def fake_run_summaries(**kwargs):
        enrich_calls.append(kwargs)
        return True

    def fake_finish(done_run_id, stats, error=None):
        finished['run_id'] = done_run_id
        finished['stats'] = stats
        finished['error'] = error

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route, '_run_summaries', fake_run_summaries)
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', fake_finish)

    progress = fetch_route._make_micro_fetch_progress('twitter', 'following', run_id=run_id, run_source='micro:twitter:following')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'micro:twitter:following',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_source_micro_fetch(run_id, 'twitter', 'following', 'micro:twitter:following')

    expected_run_dir = str(tmp_path / 'data' / 'run_sources' / str(run_id))
    assert source_calls == [('twitter', 'following', expected_run_dir, expected_run_dir)]
    ingest_cmd = next(cmd for cmd in commands if any(str(part).endswith('ingest.py') for part in cmd))
    assert ingest_cmd[ingest_cmd.index('--run-id') + 1] == str(run_id)
    assert '--skip-image-download' in ingest_cmd
    assert enrich_calls[0]['run_id'] == run_id
    assert enrich_calls[0]['batch_size'] == 5
    assert enrich_calls[0]['workers'] == 3
    assert any(any(str(part).endswith('pipeline.py') for part in cmd) for cmd in commands)
    assert finished['error'] is None
    assert finished['stats']['_pipeline_mode'] == 'micro'
    assert finished['stats']['_micro_source'] == {'platform': 'twitter', 'source': 'following'}
    assert finished['stats']['_result_status'] == 'success'
    assert finished['stats']['_new_items_count'] == 4
    assert finished['stats']['_platform_new_counts'] == {'twitter': 4}
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False


@pytest.mark.parametrize(
    ('published_clusters', 'expected_refresh_calls', 'expected_cache_clears'),
    [
        (
            1,
            [{'min_interval_sec': 0}],
            [{'clear_remote_snapshots': True}, {'clear_remote_snapshots': True}],
        ),
        (
            0,
            [],
            [{'clear_remote_snapshots': True}],
        ),
    ],
)
def test_run_source_micro_fetch_refreshes_highlights_read_model_after_success(
    monkeypatch,
    tmp_path,
    published_clusters,
    expected_refresh_calls,
    expected_cache_clears,
):
    run_id = 3112
    refresh_calls = []
    cache_clears = []

    monkeypatch.setattr(fetch_route, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '0')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '0')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '0')
    monkeypatch.setenv('INFO2ACTION_MICRO_HIGHLIGHTS_READ_MODEL_REFRESH_MIN_INTERVAL_SEC', '0')
    monkeypatch.setattr(fetch_route, '_start_fetch_run_heartbeat', lambda _run_id: None)
    monkeypatch.setattr(fetch_route, '_run_source_fetch_step', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_count_inserted_run_items', lambda _run_id: 2)
    monkeypatch.setattr(fetch_route, '_fetch_run_stats_current_backend', lambda: {})
    monkeypatch.setattr(fetch_route, '_per_platform_new_counts_current_backend', lambda _started_at: {'twitter': 2})
    monkeypatch.setattr(fetch_route, '_remote_db_pressure_skip_reason', lambda: None)
    monkeypatch.setattr(fetch_route, '_cluster_pipeline_settings', lambda: {'timeout_sec': 60})
    monkeypatch.setattr(
        fetch_route,
        '_cluster_pipeline_cmd',
        lambda _run_id, **kwargs: ['python3', 'pipeline.py', '--stats-path', kwargs['stats_path']],
    )
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: True)
    monkeypatch.setattr(fetch_route, '_finish_fetch_run_current_backend', lambda *_args, **_kwargs: None)

    def fake_run(cmd, **_kwargs):
        if any(str(part).endswith('pipeline.py') for part in cmd):
            stats_path = cmd[cmd.index('--stats-path') + 1]
            os.makedirs(os.path.dirname(stats_path), exist_ok=True)
            with open(stats_path, 'w') as f:
                json.dump({'published_clusters': published_clusters}, f)
        return types.SimpleNamespace(returncode=0)

    def fake_refresh_highlights_read_model_if_stale(**kwargs):
        refresh_calls.append(kwargs)
        return {'ok': True}

    class InlineThread:
        def __init__(self, *, target, daemon=None, name=None, args=(), kwargs=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route.threading, 'Thread', InlineThread)
    monkeypatch.setattr(fetch_route.remote_db, 'clear_feed_cache_keys', lambda **kwargs: cache_clears.append(kwargs) or 0)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'refresh_highlights_read_model_if_stale',
        fake_refresh_highlights_read_model_if_stale,
    )

    progress = fetch_route._make_micro_fetch_progress('twitter', 'following', run_id=run_id, run_source='micro:twitter:following')
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_active_runs[run_id] = {
        'source': 'micro:twitter:following',
        'started_at': 'now',
        'progress': progress,
    }
    fetch_route._fetch_progress = progress

    fetch_route._run_source_micro_fetch(run_id, 'twitter', 'following', 'micro:twitter:following')

    assert refresh_calls == expected_refresh_calls
    assert cache_clears == expected_cache_clears
    fetch_route._fetch_active_runs.clear()
    fetch_route._fetch_running = False


def test_dynamic_micro_fetch_sources_parse_platform_source_and_interval(monkeypatch):
    monkeypatch.setenv(
        'INFO2ACTION_DYNAMIC_FETCH_SOURCES',
        'twitter:following:5,xiaohongshu:search:AI_agents:15,bilibili:hot:60,bad-entry',
    )

    specs = fetch_route._dynamic_micro_fetch_sources()

    assert specs == [
        {'platform': 'twitter', 'source': 'following', 'interval_minutes': 5.0},
        {'platform': 'xiaohongshu', 'source': 'search:AI_agents', 'interval_minutes': 15.0},
        {'platform': 'bilibili', 'source': 'hot', 'interval_minutes': 60.0},
    ]


def test_dynamic_micro_fetch_sources_default_to_twitter_hot_lanes(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_DYNAMIC_FETCH_SOURCES', raising=False)

    specs = fetch_route._dynamic_micro_fetch_sources()

    assert specs == [
        {'platform': 'twitter', 'source': 'following', 'interval_minutes': 5.0},
        {'platform': 'twitter', 'source': 'for_you', 'interval_minutes': 5.0},
    ]


def test_start_dynamic_micro_fetch_uses_due_source_and_cooldown(monkeypatch):
    calls = []
    now = {'value': 1000.0}

    monkeypatch.setenv('INFO2ACTION_DYNAMIC_FETCH_SOURCES', 'twitter:following:5')
    monkeypatch.setattr(fetch_route.time, 'monotonic', lambda: now['value'])
    monkeypatch.setattr(
        fetch_route,
        'start_source_micro_fetch',
        lambda platform, source: calls.append((platform, source)) or {
            'ok': True,
            'run_id': 3201,
            'source': f'micro:{platform}:{source}',
        },
    )
    fetch_route._dynamic_micro_last_started.clear()

    first = fetch_route.start_dynamic_micro_fetch('unit')
    now['value'] = 1100.0
    skipped = fetch_route.start_dynamic_micro_fetch('unit')
    now['value'] = 1301.0
    second = fetch_route.start_dynamic_micro_fetch('unit')

    assert first['ok'] is True
    assert first['platform'] == 'twitter'
    assert first['source_name'] == 'following'
    assert skipped['ok'] is False
    assert skipped['skip_reason'] == 'dynamic_micro_no_due_source'
    assert second['ok'] is True
    assert calls == [('twitter', 'following'), ('twitter', 'following')]
    fetch_route._dynamic_micro_last_started.clear()


def test_start_dynamic_micro_fetch_uses_persisted_cooldown_after_restart(monkeypatch):
    calls = []
    now_wall = datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setenv(
        'INFO2ACTION_DYNAMIC_FETCH_SOURCES',
        'twitter:following:30,twitter:for_you:30',
    )
    monkeypatch.setattr(fetch_route.time, 'monotonic', lambda: 1000.0)
    monkeypatch.setattr(fetch_route, '_utc_now', lambda: now_wall)
    monkeypatch.setattr(
        fetch_route,
        '_latest_micro_fetch_started_at_current_backend',
        lambda platform, source: now_wall - timedelta(minutes=5) if source == 'following' else None,
    )
    monkeypatch.setattr(
        fetch_route,
        'start_source_micro_fetch',
        lambda platform, source: calls.append((platform, source)) or {
            'ok': True,
            'run_id': 3202,
            'source': f'micro:{platform}:{source}',
        },
    )
    fetch_route._dynamic_micro_last_started.clear()

    result = fetch_route.start_dynamic_micro_fetch('unit')

    assert result['ok'] is True
    assert result['platform'] == 'twitter'
    assert result['source_name'] == 'for_you'
    assert calls == [('twitter', 'for_you')]
    fetch_route._dynamic_micro_last_started.clear()


def test_start_dynamic_micro_fetch_skips_when_only_persisted_source_is_cooling(monkeypatch):
    now_wall = datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setenv('INFO2ACTION_DYNAMIC_FETCH_SOURCES', 'twitter:for_you:30')
    monkeypatch.setattr(fetch_route.time, 'monotonic', lambda: 1000.0)
    monkeypatch.setattr(fetch_route, '_utc_now', lambda: now_wall)
    monkeypatch.setattr(
        fetch_route,
        '_latest_micro_fetch_started_at_current_backend',
        lambda _platform, _source: now_wall - timedelta(minutes=5),
    )
    monkeypatch.setattr(
        fetch_route,
        'start_source_micro_fetch',
        lambda _platform, _source: pytest.fail('source should still be cooling'),
    )
    fetch_route._dynamic_micro_last_started.clear()

    result = fetch_route.start_dynamic_micro_fetch('unit')

    assert result['ok'] is False
    assert result['skip_reason'] == 'dynamic_micro_no_due_source'
    fetch_route._dynamic_micro_last_started.clear()


def test_latest_micro_fetch_started_at_reads_local_fetch_run_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'fetch.db'))
    conn = db_mod.get_conn()
    conn.execute(
        """INSERT INTO fetch_runs (started_at, status, stats_json)
           VALUES (?, 'done', ?)""",
        (
            '2026-06-28T09:53:16+00:00',
            json.dumps({
                '_pipeline_mode': 'micro',
                '_micro_source': {'platform': 'twitter', 'source': 'for_you'},
            }),
        ),
    )
    conn.execute(
        """INSERT INTO fetch_runs (started_at, status, stats_json)
           VALUES (?, 'done', ?)""",
        (
            '2026-06-28T10:01:25+00:00',
            json.dumps({
                '_pipeline_mode': 'micro',
                '_micro_source': {'platform': 'twitter', 'source': 'following'},
            }),
        ),
    )
    conn.commit()
    conn.close()

    started_at = fetch_route._latest_micro_fetch_started_at_current_backend('twitter', 'for_you')

    assert started_at == datetime(2026, 6, 28, 9, 53, 16, tzinfo=timezone.utc)


def test_count_inserted_run_items_uses_run_item_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'fetch.db'))
    conn = db_mod.get_conn()
    conn.execute(
        "INSERT INTO fetch_runs (id, started_at, status) VALUES (77, datetime('now'), 'running')"
    )
    conn.execute(
        "INSERT INTO fetch_runs (id, started_at, status) VALUES (78, datetime('now'), 'running')"
    )
    for item_id in ('old', 'new-a', 'new-b', 'other-run'):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title, content)
               VALUES (?, 'twitter', 'unit', datetime('now'), ?, ?)""",
            (item_id, item_id, item_id),
        )
    conn.execute(
        """INSERT INTO fetch_run_items (run_id, item_id, platform, source, was_inserted)
           VALUES (77, 'old', 'twitter', 'unit', 0),
                  (77, 'new-a', 'twitter', 'unit', 1),
                  (77, 'new-b', 'reddit', 'unit', 1),
                  (78, 'other-run', 'rss', 'unit', 1)"""
    )
    conn.commit()
    conn.close()

    assert fetch_route._count_inserted_run_items(77) == 2


def test_run_fetch_does_not_publish_partial_events_when_ai_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'fetch.db'))
    conn = db_mod.get_conn()
    conn.close()

    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if any(str(part).endswith('pipeline.py') for part in cmd):
            raise AssertionError('pipeline should not publish when AI enrichment fails')
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_route.subprocess, 'run', fake_run)
    # B5(PL-7): 长时段 stage 已改走 _run_killpg(进程组捕杀),同样路由到 fake
    monkeypatch.setattr(fetch_route, '_run_killpg', lambda cmd, **kw: fake_run(cmd, **kw))
    monkeypatch.setattr(fetch_route, '_run_summaries', lambda **_kwargs: False)
    monkeypatch.setattr(fetch_route, '_notify', lambda _msg: None)

    fetch_route._run_fetch()

    ingest_cmd = next(cmd for cmd in commands if any(str(part).endswith('ingest.py') for part in cmd))
    assert '--skip-image-download' in ingest_cmd
    assert not any(any(str(part).endswith('pipeline.py') for part in cmd) for cmd in commands)
    assert fetch_route._fetch_progress['result_status'] == 'partial'
    assert fetch_route._fetch_progress['message'] == '本轮部分完成，AI 总结失败，未发布本轮事件'
    event_stage = next(
        stage for stage in fetch_route._fetch_progress['stages']
        if stage.get('id') == 'event_cluster'
    )
    assert event_stage['status'] == 'warning'
