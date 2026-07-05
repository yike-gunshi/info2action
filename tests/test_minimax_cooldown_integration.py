import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture()
def isolated_provider_state(tmp_path, monkeypatch):
    import ai_provider_guard as guard

    monkeypatch.setattr(guard, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(guard, "LOCK_PATH", str(tmp_path / "state.lock"))
    return guard


def test_generate_summaries_skips_minimax_call_during_cooldown(isolated_provider_state, monkeypatch):
    import generate_summaries

    guard = isolated_provider_state
    guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("urlopen should not be called during cooldown")

    monkeypatch.setattr(guard.urllib.request, "urlopen", fail_if_called)

    out = generate_summaries.call_minimax(
        "key", "https://api.example.com", "model", "prompt", "content"
    )

    assert out.startswith("[总结生成失败: minimax cooldown")


def test_score_items_raises_cooldown_before_minimax_call(isolated_provider_state, monkeypatch):
    import score_items

    guard = isolated_provider_state
    guard.record_rate_limit("minimax", source="test", cooldown_seconds=60)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("urlopen should not be called during cooldown")

    monkeypatch.setattr(guard.urllib.request, "urlopen", fail_if_called)

    with pytest.raises(guard.ProviderCooldown):
        score_items.call_minimax("key", "https://api.example.com", "model", "prompt", "content")
