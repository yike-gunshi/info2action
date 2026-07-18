"""BF-0710-1: info 读模型刷新防死循环加固。

覆盖核心三改(backlog 钦定范围):
(i)   失败指数退避 —— 刷新失败后重试间隔 10min→20→40→…→2h 封顶,成功归零。
      直接断言目标本身:失败后第二次尝试间隔翻倍(BF-0708-3 教训:别只验必要条件)。
(ii)  delta 时间窗分片 —— refresh_info_read_model_delta_in_place 每轮只吃
      (水位, min(min_start+窗口, now)] 的内容,循环追赶;每轮独立事务独立提交。
      ⚠️ 水位只推进到本轮实际物化的 max(fetched_at),绝不推到窗口上界
      (P0-1 checkpoint 语义:宁可少推进不可越过)。
(iii) 两层超时协同 —— 每轮 SET LOCAL statement_timeout = min(应用预算, 剩余墙钟),
      预算耗尽带部分进度收官(ok=True, caught_up=False),不再"10min 预算被 DB 暗杀"。

本地为 sqlite 模式、无 psycopg:全部走 fake connect 注入(BF-0708-1 范式),
不触真库;生产行为由部署后 runtime 观察(P6/P7)兜底。
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import remote_db  # noqa: E402


UTC = timezone.utc


class FakeClock:
    def __init__(self, start: float = 100000.0):
        self.t = start

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(remote_db.time, 'time', c.time)
    monkeypatch.setattr(remote_db.time, 'monotonic', c.monotonic)
    return c


# ══════════════════ (i) 失败指数退避 ══════════════════


@pytest.fixture
def _stale_env(monkeypatch):
    """让 refresh_info_read_model_if_stale 走到"需要刷新"的分支,不触真库。"""
    monkeypatch.setattr(remote_db, '_info_read_model_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, '_info_read_model_incremental_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'remote_poc')
    monkeypatch.setattr(remote_db, '_set_short_statement_timeout', lambda conn, ms=0: None)
    monkeypatch.setattr(
        remote_db, '_info_read_model_freshness',
        lambda conn, schema, **kw: {'stale': True, 'data_stale': True, 'sort_policy_stale': False},
    )

    @contextmanager
    def _fake_connect():
        yield object()

    monkeypatch.setattr(remote_db, 'connect', _fake_connect)
    # 每个测试从干净的退避状态出发
    monkeypatch.setattr(remote_db, '_INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT', 0.0)
    monkeypatch.setattr(remote_db, '_INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES', 0, raising=False)


def _failing_delta(monkeypatch, calls):
    def _boom(**kw):
        calls.append('attempt')
        raise remote_db.RemoteDBError(
            'info read model in-place delta refresh failed at insert_missing_scope_items: statement timeout'
        )
    monkeypatch.setattr(remote_db, 'refresh_info_read_model_delta_in_place', _boom)


def test_failed_refresh_doubles_second_attempt_interval(_stale_env, clock, monkeypatch):
    """RED 核心断言:第 1 次失败后,600s 时必须仍在退避(间隔已翻倍为 1200s)。

    现状(无退避)下 600s 后会原样重试 —— 这正是死循环发动机。
    """
    calls: list = []
    _failing_delta(monkeypatch, calls)

    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert calls == ['attempt']

    clock.advance(601)  # 常规间隔已过,但失败 1 次 → 有效间隔应为 1200
    result = remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert result.get('skipped') == 'recent_attempt', (
        f'失败 1 次后 601s 就重试 = 无退避原样重试(死循环发动机),实际返回 {result}'
    )
    assert calls == ['attempt'], '601s 时不得发起第二次真实尝试'

    clock.advance(600)  # 累计 1201s > 1200 → 允许第二次尝试
    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert calls == ['attempt', 'attempt'], '翻倍后的间隔一到必须恢复尝试(退避不是熔断)'


def test_backoff_caps_at_two_hours(_stale_env, clock, monkeypatch):
    """连续失败的有效间隔封顶 2h,不无限膨胀。"""
    calls: list = []
    _failing_delta(monkeypatch, calls)

    # 连续失败 5 次:600→1200→2400→4800→7200(cap)
    for _ in range(5):
        with pytest.raises(remote_db.RemoteDBError):
            remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
        clock.advance(7201)  # 每次都等过封顶间隔,保证下一次是真实尝试
    assert len(calls) == 5

    # 第 5 次失败后有效间隔应为 7200(cap):上面已 advance(7201) → 这次仍是真实尝试
    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert len(calls) == 6

    clock.advance(7199)
    skipped = remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert skipped.get('skipped') == 'recent_attempt'
    assert skipped.get('effective_interval_sec') == 7200, (
        f'退避间隔必须封顶 7200s,实际 {skipped.get("effective_interval_sec")}'
    )
    clock.advance(2)
    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)


def test_success_resets_backoff(_stale_env, clock, monkeypatch):
    """一次成功后退避计数归零,恢复常规 600s 节奏。"""
    calls: list = []

    def _flaky(**kw):
        calls.append('attempt')
        if len(calls) == 1:
            raise remote_db.RemoteDBError('boom')
        return {'ok': True, 'mode': 'delta_in_place', 'delta_items': 3}

    monkeypatch.setattr(remote_db, 'refresh_info_read_model_delta_in_place', _flaky)

    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    clock.advance(1201)
    ok = remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert ok.get('ok') is True and len(calls) == 2

    # 成功后:601s 就该允许下一次尝试(计数已归零),而不是 2400s
    clock.advance(601)
    remote_db.refresh_info_read_model_if_stale(min_interval_sec=600)
    assert len(calls) == 3, '成功后退避必须归零,601s 应恢复尝试'


# ══════════════════ (ii)+(iii) delta 时间窗分片 + 预算协同 ══════════════════


T0 = datetime(2026, 7, 9, 16, 24, 0, tzinfo=UTC)  # 生产事故时的真实水位形状


def test_delta_window_bounds_pure():
    """窗口纯函数:min(min_start+窗口, now);触到 now 即视为追平。"""
    now_ts = T0 + timedelta(hours=48)
    end, reached = remote_db._delta_window_bounds(T0, now_ts, 6)
    assert end == T0 + timedelta(hours=6) and reached is False

    end, reached = remote_db._delta_window_bounds(now_ts - timedelta(hours=3), now_ts, 6)
    assert end == now_ts and reached is True, '窗口越过 now 必须钳制到 now 并标记追平'


class _RoundFakeConn:
    """脚本化连接:按 SQL 关键字回放结果,记录全部 (sql, params)。"""

    def __init__(self, *, min_start, now_ts, delta_n, delta_max):
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0
        self._min_start = min_start
        self._now_ts = now_ts
        self._delta_n = delta_n
        self._delta_max = delta_max
        self._last_sql = ''

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last_sql = sql
        return self

    def fetchone(self):
        if 'AS min_start' in self._last_sql:
            return {'min_start': self._min_start, 'now_ts': self._now_ts}
        if 'count(*) AS n' in self._last_sql:
            return {'n': self._delta_n, 'max_fetched_at': self._delta_max}
        return {}

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _run_one_round(monkeypatch, fake):
    @contextmanager
    def _fake_connect():
        yield fake

    monkeypatch.setattr(remote_db, 'connect', _fake_connect)
    monkeypatch.setattr(remote_db, '_prune_info_read_model_versions', lambda conn, schema=None: None)
    return remote_db._refresh_info_read_model_delta_round(
        schema='remote_poc',
        active_version_id='11111111-1111-1111-1111-111111111111',
        watermark=T0,
        min_github_stars=50,
        window_hours=6,
        statement_timeout_ms=123000,
    )


def test_round_sql_is_window_bounded_and_watermark_uses_actual_max(monkeypatch):
    """单轮事务:materialize 必须带上界参数;水位参数必须等于本轮实际 max(fetched_at)。"""
    now_ts = T0 + timedelta(hours=48)
    delta_max = T0 + timedelta(hours=5, minutes=30)  # < 窗口上界 T0+6h
    fake = _RoundFakeConn(min_start=T0 + timedelta(minutes=1), now_ts=now_ts,
                          delta_n=1689, delta_max=delta_max)
    result = _run_one_round(monkeypatch, fake)

    assert result['status'] == 'applied' and result['delta_items'] == 1689
    assert result['reached_now'] is False

    # (iii) 每轮开头必须 SET LOCAL 传入的协同超时
    set_locals = [s for s, _ in fake.executed if 'SET LOCAL statement_timeout' in s]
    assert set_locals and "'123000ms'" in set_locals[0], (
        f'每轮必须以协同后的 statement_timeout 开头,实际 {set_locals}'
    )

    # (ii) materialize 的 SQL 必须有时间窗上界
    mat = [(s, p) for s, p in fake.executed if 'CREATE TEMP TABLE info_read_model_delta ' in s]
    assert mat, '未找到 materialize_delta 语句'
    mat_sql, mat_params = mat[0]
    assert 'i.fetched_at <= %(delta_window_end)s' in mat_sql, (
        'delta 条件必须加窗口上界,否则长积压又是一条巨型 SQL'
    )
    expected_end = T0 + timedelta(minutes=1) + timedelta(hours=6)
    assert mat_params['delta_window_end'] == expected_end
    assert mat_params['active_max_fetched_at'] == T0

    # ⚠️ 水位语义:UPDATE 版本表的参数必须是实际物化 max,绝不能是窗口上界
    upd = [(s, p) for s, p in fake.executed if 'SET max_fetched_at' in s]
    assert upd, '未找到 update_active_version 语句'
    _, upd_params = upd[0]
    assert upd_params['delta_max_fetched_at'] == delta_max, (
        f'水位必须推进到实际 max(fetched_at)={delta_max},'
        f'实际参数 {upd_params["delta_max_fetched_at"]}(若等于窗口上界即破坏 checkpoint 语义)'
    )
    assert upd_params['delta_max_fetched_at'] != expected_end

    assert fake.commits == 1, '每轮必须独立提交一次(失败只丢当轮,已提交轮次不回滚)'


def test_round_empty_backlog_returns_no_delta_without_touching_watermark(monkeypatch):
    """探针无剩余积压 → no_delta,不产生任何 UPDATE 水位语句。"""
    fake = _RoundFakeConn(min_start=None, now_ts=T0, delta_n=0, delta_max=None)
    result = _run_one_round(monkeypatch, fake)
    assert result['status'] == 'no_delta'
    assert not [s for s, _ in fake.executed if 'SET max_fetched_at' in s], (
        '空窗口绝不允许推进水位(宁可少推进不可越过)'
    )


@pytest.fixture
def _outer_env(monkeypatch):
    """让外层 refresh_info_read_model_delta_in_place 走到分轮循环,不触真库。"""
    monkeypatch.setattr(remote_db, '_info_read_model_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'remote_poc')
    monkeypatch.setattr(remote_db, 'clear_feed_cache_keys', lambda *a, **k: None)
    monkeypatch.setattr(remote_db, '_set_short_statement_timeout', lambda conn, ms=0: None)
    monkeypatch.setattr(
        remote_db, '_info_read_model_active_version',
        lambda conn, schema: {
            'version_id': '11111111-1111-1111-1111-111111111111',
            'max_fetched_at': T0,
            'meta_json': {'sort_policy': remote_db.INFO_READ_MODEL_SORT_POLICY, 'scope_profile': remote_db.INFO_READ_MODEL_SCOPE_PROFILE},
        },
    )

    @contextmanager
    def _fake_connect():
        yield _RoundFakeConn(min_start=None, now_ts=T0, delta_n=0, delta_max=None)

    monkeypatch.setattr(remote_db, 'connect', _fake_connect)


def test_multi_round_loop_advances_watermark_until_caught_up(_outer_env, clock, monkeypatch):
    """>6h 积压分多轮推进:每轮水位接力,追平(reached_now)即收官。"""
    seen_watermarks: list = []
    script = [
        {'status': 'applied', 'delta_items': 1000, 'max_fetched_at': T0 + timedelta(hours=6),
         'reached_now': False, 'timings_ms': {}},
        {'status': 'applied', 'delta_items': 800, 'max_fetched_at': T0 + timedelta(hours=12),
         'reached_now': False, 'timings_ms': {}},
        {'status': 'applied', 'delta_items': 120, 'max_fetched_at': T0 + timedelta(hours=13),
         'reached_now': True, 'timings_ms': {}},
    ]

    def _fake_round(**kw):
        seen_watermarks.append(kw['watermark'])
        return script[len(seen_watermarks) - 1]

    monkeypatch.setattr(remote_db, '_refresh_info_read_model_delta_round', _fake_round)

    result = remote_db.refresh_info_read_model_delta_in_place()

    assert result['ok'] is True and result['caught_up'] is True
    assert result['rounds'] == 3 and result['delta_items'] == 1920
    assert seen_watermarks == [T0, T0 + timedelta(hours=6), T0 + timedelta(hours=12)], (
        '每轮必须从上一轮实际推进到的水位接力,不得跳跃或重复'
    )


def test_budget_exhaustion_stops_loop_with_partial_progress(_outer_env, clock, monkeypatch):
    """(iii) 墙钟预算耗尽:带部分进度收官 ok=True/caught_up=False,且下一轮的
    statement_timeout 必须随剩余预算收缩(两层超时协同的直接断言)。"""
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV, '600000')
    seen_timeouts: list = []

    def _slow_round(**kw):
        seen_timeouts.append(kw['statement_timeout_ms'])
        clock.advance(400)  # 每轮耗 400s,预算 600s
        return {'status': 'applied', 'delta_items': 500,
                'max_fetched_at': T0 + timedelta(hours=6 * len(seen_timeouts)),
                'reached_now': False, 'timings_ms': {}}

    monkeypatch.setattr(remote_db, '_refresh_info_read_model_delta_round', _slow_round)

    result = remote_db.refresh_info_read_model_delta_in_place()

    assert result['ok'] is True, '预算耗尽是正常收官不是失败(已提交轮次的进度必须保留)'
    assert result['caught_up'] is False and result['rounds'] == 2
    assert seen_timeouts[0] == 600000, '首轮 statement_timeout = 全额预算'
    assert 190000 <= seen_timeouts[1] <= 210000, (
        f'第二轮 statement_timeout 必须收缩到剩余预算(~200000ms),实际 {seen_timeouts[1]}'
    )


def test_first_round_no_delta_preserves_skip_contract(_outer_env, clock, monkeypatch):
    """现网常态回归护栏:无积压时首轮即 no_delta,保持原 skipped 契约(秒级跳过)。"""
    monkeypatch.setattr(
        remote_db, '_refresh_info_read_model_delta_round',
        lambda **kw: {'status': 'no_delta', 'timings_ms': {}},
    )
    result = remote_db.refresh_info_read_model_delta_in_place()
    assert result['ok'] is True and result.get('skipped') == 'no_delta'
