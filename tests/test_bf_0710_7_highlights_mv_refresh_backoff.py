"""BF-0710-7: highlights 刷新守卫失败无退避加固(BF-0710-1 同款模式移植)。

BF-0710-1 只修了 info 路径的失败指数退避;同模板的守卫
`refresh_highlights_read_model_if_stale` 仍是"只记尝试时间、无失败计数"
——失败后固定 min_interval 原样重试,与 BF-0708 系列把偶发拥塞滚成
饱和死循环的发动机完全同构。
(perf-v27 P0: 原第三处守卫 refresh_platforms_mv_if_stale 已随 MV 删除,
其测试段一并移除。)

三条断言 × 两个守卫(直接断言目标本身,BF-0708-3 教训:别只验必要条件):
(a) 失败后第二次尝试间隔翻倍(601s 时必须仍在退避);
(b) 连续失败封顶 2h,跳过返回带 effective_interval_sec 便于现网观察;
(c) 一次成功后计数归零,恢复常规 600s 节奏。

全部走 monkeypatch 注入(BF-0710-1 范式),不触真库。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import remote_db  # noqa: E402


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


# ══════════════════ highlights 守卫 ══════════════════


@pytest.fixture
def _hl_env(monkeypatch):
    """让 refresh_highlights_read_model_if_stale 走到真实刷新分支,不触真库。"""
    monkeypatch.setattr(remote_db, '_highlights_read_model_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, '_highlights_read_model_incremental_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, '_highlights_refresh_skip_during_fetch_enabled', lambda env=None: False)
    # 每个测试从干净的退避状态出发
    monkeypatch.setattr(remote_db, '_HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT', 0.0)
    monkeypatch.setattr(
        remote_db, '_HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES', 0, raising=False,
    )


def _failing_hl_delta(monkeypatch, calls):
    def _boom(**kw):
        calls.append('attempt')
        raise remote_db.RemoteDBError(
            'highlights read model in-place delta refresh failed at insert_missing: statement timeout'
        )
    monkeypatch.setattr(remote_db, 'refresh_highlights_read_model_delta_in_place', _boom)


def test_hl_failed_refresh_doubles_second_attempt_interval(_hl_env, clock, monkeypatch):
    """RED 核心断言:第 1 次失败后,601s 时必须仍在退避(间隔已翻倍为 1200s)。"""
    calls: list = []
    _failing_hl_delta(monkeypatch, calls)

    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert calls == ['attempt']

    clock.advance(601)  # 常规间隔已过,但失败 1 次 → 有效间隔应为 1200
    result = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert result.get('skipped') == 'recent_attempt', (
        f'失败 1 次后 601s 就重试 = 无退避原样重试(死循环发动机),实际返回 {result}'
    )
    assert calls == ['attempt'], '601s 时不得发起第二次真实尝试'

    clock.advance(600)  # 累计 1201s > 1200 → 允许第二次尝试
    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert calls == ['attempt', 'attempt'], '翻倍后的间隔一到必须恢复尝试(退避不是熔断)'


def test_hl_backoff_caps_at_two_hours(_hl_env, clock, monkeypatch):
    """连续失败的有效间隔封顶 2h,且跳过返回带观测字段。"""
    calls: list = []
    _failing_hl_delta(monkeypatch, calls)

    for _ in range(6):
        with pytest.raises(remote_db.RemoteDBError):
            remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
        clock.advance(7201)
    assert len(calls) == 6

    clock.advance(-7201 + 7199)  # 最后一次失败后只过 7199s
    skipped = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert skipped.get('skipped') == 'recent_attempt'
    assert skipped.get('consecutive_failures') == 6
    assert skipped.get('effective_interval_sec') == 7200, (
        f'退避间隔必须封顶 7200s,实际 {skipped.get("effective_interval_sec")}'
    )
    clock.advance(2)
    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)


def test_hl_success_resets_backoff(_hl_env, clock, monkeypatch):
    """一次成功后退避计数归零,恢复常规 600s 节奏。"""
    calls: list = []

    def _flaky(**kw):
        calls.append('attempt')
        if len(calls) == 1:
            raise remote_db.RemoteDBError('boom')
        return {'ok': True, 'mode': 'delta_in_place'}

    monkeypatch.setattr(remote_db, 'refresh_highlights_read_model_delta_in_place', _flaky)

    with pytest.raises(remote_db.RemoteDBError):
        remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    clock.advance(1201)
    ok = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert ok.get('ok') is True and len(calls) == 2

    clock.advance(601)
    remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert len(calls) == 3, '成功后退避必须归零,601s 应恢复尝试'


def test_hl_early_skip_paths_do_not_touch_backoff_state(_hl_env, clock, monkeypatch):
    """disabled / fetch_running 的早退不得计失败、也不得记尝试时间。"""
    calls: list = []
    _failing_hl_delta(monkeypatch, calls)
    monkeypatch.setattr(remote_db, '_highlights_refresh_skip_during_fetch_enabled', lambda env=None: True)
    monkeypatch.setattr(remote_db, 'has_recent_running_fetch_remote', lambda: True)

    result = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=600)
    assert result.get('skipped') == 'fetch_running' and calls == []
    assert remote_db._HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT == 0.0
    assert remote_db._HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES == 0


# ══════════════════ 回归:info 守卫行为不受重构影响 ══════════════════


def test_info_backoff_helper_unchanged():
    """共享退避函数语义回归:600→1200→…→7200 封顶。"""
    f = remote_db._info_read_model_refresh_effective_interval
    assert f(600, 0) == 600
    assert f(600, 1) == 1200
    assert f(600, 4) == 7200 or f(600, 4) == 9600  # 4 次:9600 超 cap → 7200
    assert f(600, 4) == 7200
    assert f(600, 16) == 7200
