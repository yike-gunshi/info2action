"""Backend-owned fetch scheduler.

The scheduler lives inside the FastAPI process, so scheduled fetches only run
while the backend service is alive. It intentionally reuses the normal fetch
entrypoint instead of calling lower-level scripts, preserving fetch_runs audit.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Mapping

logger = logging.getLogger(__name__)

BACKEND_FETCH_MIN_INTERVAL_MINUTES_ENV = "INFO2ACTION_BACKEND_FETCH_MIN_INTERVAL_MINUTES"
DEFAULT_BACKEND_FETCH_MIN_INTERVAL_MINUTES = 30.0


def env_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def fetch_min_interval_seconds(env: Mapping[str, str] | None = None) -> float:
    values = os.environ if env is None else env
    raw = values.get(BACKEND_FETCH_MIN_INTERVAL_MINUTES_ENV)
    if raw is None or not raw.strip():
        minutes = DEFAULT_BACKEND_FETCH_MIN_INTERVAL_MINUTES
    else:
        try:
            minutes = float(raw)
        except ValueError:
            minutes = DEFAULT_BACKEND_FETCH_MIN_INTERVAL_MINUTES
    if minutes <= 0:
        minutes = DEFAULT_BACKEND_FETCH_MIN_INTERVAL_MINUTES
    return minutes * 60


BACKEND_FETCH_TICK_MINUTES_ENV = "INFO2ACTION_BACKEND_FETCH_TICK_MINUTES"
DEFAULT_BACKEND_FETCH_TICK_MINUTES = 30


def fetch_tick_interval_minutes(env: Mapping[str, str] | None = None) -> int:
    """墙钟对齐 tick 间隔(分钟)。默认 30(:00/:30);设 15 → :00/:15/:30/:45(v20.0 全源提速)。"""
    values = os.environ if env is None else env
    raw = values.get(BACKEND_FETCH_TICK_MINUTES_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_BACKEND_FETCH_TICK_MINUTES
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BACKEND_FETCH_TICK_MINUTES
    return minutes if minutes > 0 else DEFAULT_BACKEND_FETCH_TICK_MINUTES


def seconds_until_next_interval(interval_minutes: int = 30,
                                now: datetime | None = None) -> float:
    """距下一个墙钟对齐 tick 的秒数。interval=30 → :00/:30;interval=15 → :00/:15/:30/:45。"""
    interval = max(1, int(interval_minutes))
    current = now or datetime.now()
    base = current.replace(second=0, microsecond=0)
    next_multiple = ((current.minute // interval) + 1) * interval
    if next_multiple >= 60:
        next_tick = base.replace(minute=0) + timedelta(
            hours=next_multiple // 60, minutes=next_multiple % 60)
    else:
        next_tick = base.replace(minute=next_multiple)
    if next_tick <= current:
        next_tick += timedelta(minutes=interval)
    return max(0.0, (next_tick - current).total_seconds())


def seconds_until_next_half_hour(now: datetime | None = None) -> float:
    """向后兼容包装:等价 seconds_until_next_interval(30)。"""
    return seconds_until_next_interval(30, now)


def seconds_until_min_interval_elapsed(
    *,
    last_triggered_at: float | None,
    now: float,
    min_interval_seconds: float,
) -> float:
    if last_triggered_at is None or min_interval_seconds <= 0:
        return 0.0
    elapsed = max(0.0, now - last_triggered_at)
    return max(0.0, min_interval_seconds - elapsed)


def seconds_until_next_scheduler_trigger(
    *,
    next_tick_seconds: float,
    last_triggered_at: float | None,
    now: float,
    min_interval_seconds: float,
) -> float:
    cooldown_seconds = seconds_until_min_interval_elapsed(
        last_triggered_at=last_triggered_at,
        now=now,
        min_interval_seconds=min_interval_seconds,
    )
    return max(0.0, next_tick_seconds, cooldown_seconds)


class BackendFetchScheduler:
    def __init__(
        self,
        start_fetch: Callable[[str], dict],
        *,
        should_start: Callable[[], bool] | None = None,
        sleep_until_next_tick: Callable[[], float] = seconds_until_next_half_hour,
        min_interval_seconds: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
        start_with_cooldown: bool = False,
    ):
        self._start_fetch = start_fetch
        self._should_start = should_start
        self._sleep_until_next_tick = sleep_until_next_tick
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._monotonic = monotonic
        self._last_triggered_at: float | None = self._monotonic() if start_with_cooldown else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="info2action-backend-scheduled-fetch",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            next_tick_seconds = self._sleep_until_next_tick()
            delay = seconds_until_next_scheduler_trigger(
                next_tick_seconds=next_tick_seconds,
                last_triggered_at=self._last_triggered_at,
                now=self._monotonic(),
                min_interval_seconds=self._min_interval_seconds,
            )
            if delay > next_tick_seconds:
                logger.info(
                    "backend scheduled fetch delayed: min interval cooldown %.0fs remaining",
                    delay,
                )
            if self._stop.wait(delay):
                break
            try:
                if self._should_start is not None and not self._should_start():
                    logger.info("backend scheduled fetch skipped: fetch already running")
                    self._stop.wait(1.0)
                    continue
                result = self._start_fetch("backend_30min_cron")
                self._last_triggered_at = self._monotonic()
                logger.info("backend scheduled fetch trigger result: %s", result)
            except Exception:
                logger.exception("backend scheduled fetch trigger failed")
            # Avoid double-triggering if the loop wakes very close to the wall-clock tick.
            self._stop.wait(1.0)


# Backward-compatible import name for older tests or local scripts.
BackendHourlyFetchScheduler = BackendFetchScheduler
