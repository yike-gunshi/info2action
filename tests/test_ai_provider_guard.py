import json
import os
import sys
import urllib.error
from io import BytesIO

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _http_error(code: int):
    return urllib.error.HTTPError(
        url="https://api.example.com",
        code=code,
        msg="err",
        hdrs=None,
        fp=BytesIO(b'{"error":"rate"}'),
    )


def _state_file(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


def test_record_rate_limit_enters_cooldown(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    state = guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    assert state["status"] == "cooldown"
    assert state["provider"] == "minimax-chat"
    assert state["consecutive_429"] == 1
    assert _state_file(tmp_path)["providers"]["minimax-chat"]["status"] == "cooldown"


def test_ensure_provider_available_raises_during_cooldown(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    with pytest.raises(guard.ProviderCooldown):
        guard.ensure_provider_available("minimax")


def test_guarded_urlopen_records_429(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    def fake_urlopen(*_args, **_kwargs):
        raise _http_error(429)

    monkeypatch.setattr(guard.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        guard.guarded_urlopen(object(), source="unit-test", timeout=1)

    assert _state_file(tmp_path)["providers"]["minimax-chat"]["status"] == "cooldown"


def test_guarded_urlopen_can_defer_429_recording(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    def fake_urlopen(*_args, **_kwargs):
        raise _http_error(429)

    monkeypatch.setattr(guard.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        guard.guarded_urlopen(object(), source="unit-test", timeout=1, record_429=False)

    assert guard.load_state()["status"] == "ok"


def test_record_success_clears_cooldown(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    state = guard.record_success("minimax", source="probe")

    assert state["status"] == "ok"
    assert state["consecutive_429"] == 0
    guard.ensure_provider_available("minimax")


def test_inflight_success_does_not_clear_new_cooldown(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    class FakeResponse:
        def read(self):
            return b"{}"

    def fake_urlopen(*_args, **_kwargs):
        guard.record_rate_limit("minimax", source="parallel-worker", cooldown_seconds=60)
        return FakeResponse()

    monkeypatch.setattr(guard.urllib.request, "urlopen", fake_urlopen)

    guard.guarded_urlopen(object(), source="unit-test", timeout=1)

    state = json.loads((tmp_path / "state.json").read_text())
    state = state["providers"]["minimax-chat"]
    assert state["status"] == "cooldown"
    assert state["last_source"] == "parallel-worker"


def test_provider_state_scopes_do_not_overwrite_each_other(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    guard.record_rate_limit("minimax", source="chat", cooldown_seconds=60)
    guard.record_action_required(
        guard.MINIMAX_EMBEDDING_PROVIDER,
        action="recharge_embedding",
        source="embedding",
        error="MiniMax embedding error 1008: insufficient balance",
    )

    data = _state_file(tmp_path)["providers"]
    assert data["minimax-chat"]["status"] == "cooldown"
    assert data["minimax-embedding"]["status"] == "action_required"
    assert "已禁用" in guard.provider_message(guard.MINIMAX_EMBEDDING_PROVIDER)


def test_chat_token_plan_429_classifies_wait_until_reset(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    exc = urllib.error.HTTPError(
        url="https://api.example.com/messages",
        code=429,
        msg="rate limited",
        hdrs={},
        fp=BytesIO(
            b'{"error":{"message":"usage limit exceeded; resets at 2026-05-10T15:00:00+08:00 (123)"}}'
        ),
    )

    classified = guard.classify_minimax_chat_http_error(exc)
    state = guard.record_rate_limit(
        guard.MINIMAX_CHAT_PROVIDER,
        source="unit-test",
        cooldown_seconds=int(classified["retry_after_seconds"]) + 5,
        action=classified["action"],
    )

    assert classified["action"] == "wait_until_reset"
    assert state["provider"] == "minimax-chat"
    assert state["cooldown_seconds"] == 128
    assert "Token Plan" in guard.provider_message(guard.MINIMAX_CHAT_PROVIDER)


def test_embedding_1008_records_action_required(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))

    state = guard.record_action_required(
        guard.MINIMAX_EMBEDDING_PROVIDER,
        action="recharge_embedding",
        source="unit-test",
        error="MiniMax embedding error 1008: insufficient balance",
    )

    assert state["status"] == "action_required"
    with pytest.raises(guard.ProviderActionRequired):
        guard.ensure_provider_available(guard.MINIMAX_EMBEDDING_PROVIDER)
