from datetime import datetime
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_seconds_until_next_half_hour_targets_wall_clock_ticks():
    from backend_fetch_scheduler import seconds_until_next_half_hour

    assert seconds_until_next_half_hour(datetime(2026, 5, 13, 9, 12, 0)) == 1080
    assert seconds_until_next_half_hour(datetime(2026, 5, 13, 9, 58, 23)) == 97
    assert seconds_until_next_half_hour(datetime(2026, 5, 13, 9, 0, 0)) == 1800
    assert seconds_until_next_half_hour(datetime(2026, 5, 13, 9, 30, 0)) == 1800


def test_seconds_until_min_interval_elapsed_waits_after_recent_trigger():
    from backend_fetch_scheduler import seconds_until_min_interval_elapsed

    assert seconds_until_min_interval_elapsed(
        last_triggered_at=100.0,
        now=3700.0,
        min_interval_seconds=5400.0,
    ) == 1800.0
    assert seconds_until_min_interval_elapsed(
        last_triggered_at=100.0,
        now=5500.0,
        min_interval_seconds=5400.0,
    ) == 0.0


def test_seconds_until_next_scheduler_trigger_respects_min_interval():
    from backend_fetch_scheduler import seconds_until_next_scheduler_trigger

    assert seconds_until_next_scheduler_trigger(
        next_tick_seconds=720.0,
        last_triggered_at=100.0,
        now=3880.0,
        min_interval_seconds=5400.0,
    ) == 1620.0


def test_fetch_min_interval_seconds_reads_env(monkeypatch):
    from backend_fetch_scheduler import fetch_min_interval_seconds

    monkeypatch.delenv("INFO2ACTION_BACKEND_FETCH_MIN_INTERVAL_MINUTES", raising=False)
    assert fetch_min_interval_seconds() == 1800.0

    monkeypatch.setenv("INFO2ACTION_BACKEND_FETCH_MIN_INTERVAL_MINUTES", "90")
    assert fetch_min_interval_seconds() == 5400.0


def test_scheduler_can_start_with_cooldown():
    from backend_fetch_scheduler import BackendFetchScheduler

    scheduler = BackendFetchScheduler(
        lambda source: {"ok": True, "source": source},
        min_interval_seconds=5400.0,
        monotonic=lambda: 123.0,
        start_with_cooldown=True,
    )

    assert scheduler._last_triggered_at == 123.0


def test_env_enabled_accepts_explicit_truthy(monkeypatch):
    from backend_fetch_scheduler import env_enabled

    monkeypatch.setenv("INFO2ACTION_BACKEND_HOURLY_FETCH", "1")
    assert env_enabled("INFO2ACTION_BACKEND_HOURLY_FETCH") is True

    monkeypatch.setenv("INFO2ACTION_BACKEND_HOURLY_FETCH", "0")
    assert env_enabled("INFO2ACTION_BACKEND_HOURLY_FETCH") is False


def test_scheduler_skips_tick_when_fetch_already_running():
    from backend_fetch_scheduler import BackendFetchScheduler

    calls = []
    scheduler = BackendFetchScheduler(
        lambda source: calls.append(source) or {"ok": True},
        should_start=lambda: False,
        sleep_until_next_tick=lambda: 0,
    )

    scheduler.start()
    time.sleep(0.05)
    scheduler.stop()

    assert calls == []


def test_global_fetch_poll_seconds_defaults_and_reads_env(monkeypatch):
    from backend_fetch_scheduler import global_fetch_poll_seconds

    monkeypatch.delenv("INFO2ACTION_BACKEND_FETCH_POLL_SECONDS", raising=False)
    assert global_fetch_poll_seconds() == 30.0

    monkeypatch.setenv("INFO2ACTION_BACKEND_FETCH_POLL_SECONDS", "45")
    assert global_fetch_poll_seconds() == 45.0

    # 非法值回退默认，避免打错配置把调度器卡死
    monkeypatch.setenv("INFO2ACTION_BACKEND_FETCH_POLL_SECONDS", "abc")
    assert global_fetch_poll_seconds() == 30.0
    monkeypatch.setenv("INFO2ACTION_BACKEND_FETCH_POLL_SECONDS", "0")
    assert global_fetch_poll_seconds() == 30.0


def test_scheduler_retries_after_skip_instead_of_dropping_the_slot():
    """上一轮超时不再吞掉下一轮:should_start 一转 true 就起,不等下一个墙钟刻度。"""
    from backend_fetch_scheduler import BackendFetchScheduler

    calls = []
    running = {"value": True}
    probes = {"count": 0}

    def _should_start():
        probes["count"] += 1
        # 第一次轮询时上一轮还在跑,第二次才结束
        if probes["count"] >= 2:
            running["value"] = False
        return not running["value"]

    scheduler = BackendFetchScheduler(
        lambda source: calls.append(source) or {"ok": True},
        should_start=_should_start,
        sleep_until_next_tick=lambda: 0.01,
    )

    scheduler.start()
    time.sleep(2.5)
    scheduler.stop()

    assert calls, "上一轮结束后调度器应当补起下一轮,而不是丢弃这个槽位"
    assert probes["count"] >= 2


def test_app_global_fetch_scheduler_polls_instead_of_aligning_wall_clock():
    """全局抓取改为轮询式:墙钟对齐会让超时的一轮吞掉下一个刻度。"""
    text = (ROOT / "src" / "app.py").read_text()

    assert "global_fetch_poll_seconds" in text
    assert "seconds_until_next_interval(fetch_tick_minutes)" not in text
