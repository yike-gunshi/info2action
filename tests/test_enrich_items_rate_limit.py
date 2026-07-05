import os
import sys
import threading
import urllib.error
from io import BytesIO


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"content":[{"type":"text","text":"OK"}]}'


def _http_429():
    return urllib.error.HTTPError(
        url="https://api.example.com/messages",
        code=429,
        msg="rate limited",
        hdrs={},
        fp=BytesIO(b'{"error":"rate"}'),
    )


def _http_429_quota_reset(seconds=123):
    return urllib.error.HTTPError(
        url="https://api.example.com/messages",
        code=429,
        msg="rate limited",
        hdrs={},
        fp=BytesIO(
            (
                '{"type":"error","error":{"message":"usage limit exceeded, '
                f'resets at 2026-05-10T05:00:00+08:00 ({seconds})"}}'
            ).encode()
        ),
    )


def _gate_without_real_sleep():
    import enrich_items

    now = [0.0]
    sleeps = []

    def sleep_fn(delay):
        sleeps.append(delay)
        now[0] += delay

    gate = enrich_items.MiniMaxRateLimitGate(
        sleep_fn=sleep_fn,
        monotonic_fn=lambda: now[0],
        jitter_fn=lambda _start, _end: 0,
    )
    return gate, sleeps


def test_call_minimax_retries_429_then_succeeds(monkeypatch):
    import enrich_items

    calls = []
    recorded = []

    def fake_guarded_urlopen(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _http_429()
        return FakeResponse()

    monkeypatch.setattr(enrich_items.ai_provider_guard, "guarded_urlopen", fake_guarded_urlopen)
    monkeypatch.setattr(enrich_items.ai_provider_guard, "record_rate_limit", lambda *a, **k: recorded.append((a, k)))
    gate, sleeps = _gate_without_real_sleep()

    text = enrich_items.call_minimax(
        "key",
        "https://api.example.com",
        "model",
        "system",
        "content",
        rate_gate=gate,
        max_429_retries=1,
    )

    assert text == "OK"
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert recorded == []
    assert all(call["record_429"] is False for call in calls)


def test_call_minimax_records_cooldown_after_retry_budget(monkeypatch):
    import enrich_items

    recorded = []

    def fake_guarded_urlopen(*_args, **_kwargs):
        raise _http_429()

    monkeypatch.setattr(enrich_items.ai_provider_guard, "guarded_urlopen", fake_guarded_urlopen)
    monkeypatch.setattr(enrich_items.ai_provider_guard, "record_rate_limit", lambda *a, **k: recorded.append((a, k)))
    gate, sleeps = _gate_without_real_sleep()

    try:
        enrich_items.call_minimax(
            "key",
            "https://api.example.com",
            "model",
            "system",
            "content",
            rate_gate=gate,
            max_429_retries=1,
        )
    except urllib.error.HTTPError as exc:
        assert exc.code == 429
    else:
        raise AssertionError("expected HTTP 429")

    assert sleeps == [2.0]
    assert len(recorded) == 1
    assert recorded[0][0][0] == "minimax-chat"
    assert recorded[0][1]["source"] == "enrich_items"


def test_call_minimax_retries_quota_window_429_before_recording(monkeypatch):
    import enrich_items

    calls = []
    recorded = []

    def fake_guarded_urlopen(*_args, **_kwargs):
        calls.append(1)
        raise _http_429_quota_reset(seconds=123)

    monkeypatch.setattr(enrich_items.ai_provider_guard, "guarded_urlopen", fake_guarded_urlopen)
    monkeypatch.setattr(enrich_items.ai_provider_guard, "record_rate_limit", lambda *a, **k: recorded.append((a, k)))
    gate, sleeps = _gate_without_real_sleep()

    try:
        enrich_items.call_minimax(
            "key",
            "https://api.example.com",
            "model",
            "system",
            "content",
            rate_gate=gate,
            max_429_retries=1,
        )
    except urllib.error.HTTPError as exc:
        assert exc.code == 429
    else:
        raise AssertionError("expected HTTP 429")

    assert len(calls) == 2
    assert sleeps == [60.0]
    assert recorded[0][1]["cooldown_seconds"] == 128
    assert recorded[0][1]["action"] == "wait_until_reset"


def test_rate_gate_spaces_request_slots():
    import enrich_items

    now = [0.0]
    sleeps = []

    def sleep_fn(delay):
        sleeps.append(delay)
        now[0] += delay

    gate = enrich_items.MiniMaxRateLimitGate(
        min_interval=0.5,
        sleep_fn=sleep_fn,
        monotonic_fn=lambda: now[0],
        jitter_fn=lambda _start, _end: 0,
    )

    gate.wait()
    gate.wait()
    gate.wait()

    assert sleeps == [0.5, 0.5]


def test_bounded_concurrency_does_not_submit_all_chunks_at_once():
    import enrich_items

    chunks = [[i] for i in range(8)]
    started = []
    results = []
    first_window_started = threading.Event()
    release = threading.Event()

    def process_chunk(chunk):
        started.append(chunk[0])
        if len(started) == 3:
            first_window_started.set()
        release.wait(timeout=2)
        return ("ok", 1, 0, f"done {chunk[0]}")

    def handle_result(chunk, result, exc):
        assert exc is None
        results.append((chunk[0], result))
        return False

    runner = threading.Thread(
        target=enrich_items._run_bounded_concurrent,
        args=(chunks, 3, process_chunk, handle_result),
    )
    runner.start()
    assert first_window_started.wait(timeout=2)
    assert len(started) == 3

    release.set()
    runner.join(timeout=5)

    assert not runner.is_alive()
    assert sorted(started) == list(range(8))
    assert len(results) == 8
