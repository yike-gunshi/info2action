"""T7: configurable batch scheduler tick (方案 X, 全源 15 分钟).

Feature: incremental-fetch-15min (v20.0), Ring ② 全源.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import backend_fetch_scheduler as sched  # noqa: E402


def test_interval_15_targets_quarter_hour_ticks():
    f = sched.seconds_until_next_interval
    assert f(15, datetime(2026, 5, 13, 9, 12, 0)) == 180    # → 9:15
    assert f(15, datetime(2026, 5, 13, 9, 15, 0)) == 900    # 正好 :15 → 下一个 9:30
    assert f(15, datetime(2026, 5, 13, 9, 46, 0)) == 840    # → 10:00
    assert f(15, datetime(2026, 5, 13, 9, 0, 0)) == 900     # → 9:15


def test_interval_30_matches_legacy_half_hour():
    # 向后兼容:interval=30 与旧 seconds_until_next_half_hour 一致
    f = sched.seconds_until_next_interval
    assert f(30, datetime(2026, 5, 13, 9, 12, 0)) == 1080
    assert f(30, datetime(2026, 5, 13, 9, 58, 23)) == 97
    assert f(30, datetime(2026, 5, 13, 9, 0, 0)) == 1800
    assert f(30, datetime(2026, 5, 13, 9, 30, 0)) == 1800
    # legacy wrapper 仍在且等价
    assert sched.seconds_until_next_half_hour(datetime(2026, 5, 13, 9, 12, 0)) == 1080


def test_tick_interval_minutes_reads_env(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_BACKEND_FETCH_TICK_MINUTES', raising=False)
    assert sched.fetch_tick_interval_minutes({}) == 30           # 默认 30(不改行为)
    assert sched.fetch_tick_interval_minutes(
        {'INFO2ACTION_BACKEND_FETCH_TICK_MINUTES': '15'}) == 15
    # 非法值 → 回退默认
    assert sched.fetch_tick_interval_minutes(
        {'INFO2ACTION_BACKEND_FETCH_TICK_MINUTES': 'abc'}) == 30
