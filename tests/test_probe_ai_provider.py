import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_probe_skips_when_not_in_cooldown(monkeypatch):
    import ai_provider_guard
    import probe_ai_provider

    monkeypatch.setattr(ai_provider_guard, "is_cooldown_active", lambda provider="minimax": False)
    monkeypatch.setattr(ai_provider_guard, "is_action_required", lambda provider="minimax": False)

    assert probe_ai_provider.should_probe(only_if_cooldown=True) is False


def test_probe_runs_when_in_cooldown(monkeypatch):
    import ai_provider_guard
    import probe_ai_provider

    monkeypatch.setattr(ai_provider_guard, "is_cooldown_active", lambda provider="minimax": True)
    monkeypatch.setattr(ai_provider_guard, "is_action_required", lambda provider="minimax": False)

    assert probe_ai_provider.should_probe(only_if_cooldown=True) is True


def test_probe_request_is_allowed_to_clear_cooldown(tmp_path, monkeypatch):
    import ai_provider_guard
    import probe_ai_provider

    monkeypatch.setattr(ai_provider_guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ai_provider_guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    ai_provider_guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    monkeypatch.setattr(ai_provider_guard.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    ok = probe_ai_provider.probe_minimax({
        "api_key": "key",
        "api_base": "https://api.example.com",
        "model": "model",
    })

    assert ok is True
    assert ai_provider_guard.load_state("minimax")["status"] == "ok"


def test_embedding_probe_clears_embedding_action_required(tmp_path, monkeypatch):
    import ai_provider_guard
    import probe_ai_provider

    monkeypatch.setattr(ai_provider_guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ai_provider_guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    ai_provider_guard.record_action_required(
        ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER,
        action="recharge_embedding",
        source="test",
    )

    assert probe_ai_provider.probe_minimax_embedding({}) is True
    state = ai_provider_guard.load_state(ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER)
    assert state["status"] == "ok"
