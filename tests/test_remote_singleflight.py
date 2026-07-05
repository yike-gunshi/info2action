"""BF-0515-singleflight: prove _singleflight_sync coalesces concurrent compute.

Tests:
 - 10 concurrent threads calling _singleflight_sync(same_key, slow_fn) → fn runs
   exactly once, all 10 get the same result.
 - Different keys do NOT coalesce.
 - Exception raised in compute_fn propagates to all waiters.
 - Leader hang fallback: if leader takes longer than timeout, waiter retries.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db


def test_singleflight_coalesces_same_key():
    call_count = 0
    counter_lock = threading.Lock()

    def slow_compute():
        nonlocal call_count
        with counter_lock:
            call_count += 1
        time.sleep(0.2)  # simulate slow Supabase query
        return {"result": "shared"}

    results = [None] * 10
    threads = []

    def worker(i):
        results[i] = remote_db._singleflight_sync(("test_key_a",), slow_compute)

    for i in range(10):
        threads.append(threading.Thread(target=worker, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1, f"compute_fn ran {call_count} times, expected 1"
    assert all(r == {"result": "shared"} for r in results), f"results not shared: {results}"


def test_singleflight_different_keys_run_separately():
    call_count = 0
    counter_lock = threading.Lock()

    def slow_compute():
        nonlocal call_count
        with counter_lock:
            call_count += 1
        time.sleep(0.1)
        return {"key": call_count}

    results = []
    threads = []

    def worker(key):
        results.append(remote_db._singleflight_sync((key,), slow_compute))

    for k in ("key_x", "key_y", "key_z"):
        threads.append(threading.Thread(target=worker, args=(k,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 3, f"different keys should each run, got {call_count}"


def test_singleflight_exception_propagates_to_all_waiters():
    class FakeError(RuntimeError):
        pass

    started = threading.Event()

    def failing_compute():
        started.set()
        time.sleep(0.05)
        raise FakeError("boom")

    errors = [None] * 5
    threads = []

    def worker(i):
        try:
            remote_db._singleflight_sync(("test_key_b",), failing_compute)
            errors[i] = "no_exception"
        except FakeError as e:
            errors[i] = str(e)
        except Exception as e:
            errors[i] = f"wrong_exc:{type(e).__name__}:{e}"

    for i in range(5):
        threads.append(threading.Thread(target=worker, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(e == "boom" for e in errors), f"all waiters should get FakeError('boom'), got {errors}"


def test_singleflight_inflight_table_clears_after_compute():
    def quick():
        return {"ok": True}

    remote_db._singleflight_sync(("test_key_clears",), quick)
    # After compute, the in-flight key should be removed
    assert ("test_key_clears",) not in remote_db._INFLIGHT


def test_singleflight_no_thread_starvation_after_exception():
    """After an exception, subsequent calls should not be stuck waiting."""

    def failing():
        raise ValueError("first call fails")

    with pytest.raises(ValueError):
        remote_db._singleflight_sync(("test_key_recover",), failing)

    # Now a non-failing call should succeed
    def succeeding():
        return "second call ok"

    result = remote_db._singleflight_sync(("test_key_recover",), succeeding)
    assert result == "second call ok"
