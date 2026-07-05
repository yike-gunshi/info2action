import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items
import generate_briefing
import interest_engine
from clustering import pipeline as clustering_pipeline


def test_enrich_items_minimax_config_prefers_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://env.example/anthropic/v1")
    monkeypatch.setenv("MINIMAX_MODEL", "EnvModel")

    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config({
        "api_key": "config-key",
        "api_base": "https://config.example/anthropic/v1",
        "model": "ConfigModel",
    })

    assert api_key == "env-key"
    assert api_base == "https://env.example/anthropic/v1"
    assert model == "EnvModel"


def test_enrich_items_minimax_config_reads_project_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_BASE", raising=False)
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    monkeypatch.setattr(enrich_items, "BASE_DIR", str(tmp_path))
    (tmp_path / ".env").write_text(
        "\n".join([
            "MINIMAX_API_KEY=dotenv-key",
            "MINIMAX_API_BASE=https://dotenv.example/anthropic/v1",
            "MINIMAX_MODEL=DotenvModel",
        ]),
        encoding="utf-8",
    )

    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config({
        "api_key": "config-key",
        "api_base": "https://config.example/anthropic/v1",
        "model": "ConfigModel",
    })

    assert api_key == "dotenv-key"
    assert api_base == "https://dotenv.example/anthropic/v1"
    assert model == "DotenvModel"


def test_generate_briefing_minimax_config_prefers_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://env.example/anthropic/v1")
    monkeypatch.setenv("MINIMAX_MODEL", "EnvModel")

    api_key, api_base, model = generate_briefing.resolve_minimax_runtime_config({
        "api_key": "config-key",
        "api_base": "https://config.example/anthropic/v1",
        "model": "ConfigModel",
    })

    assert api_key == "env-key"
    assert api_base == "https://env.example/anthropic/v1"
    assert model == "EnvModel"


def test_interest_engine_minimax_config_prefers_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://env.example/anthropic/v1")
    monkeypatch.setenv("MINIMAX_MODEL", "EnvModel")

    api_key, api_base, model = interest_engine.resolve_minimax_runtime_config({
        "api_key": "config-key",
        "api_base": "https://config.example/anthropic/v1",
        "model": "ConfigModel",
    })

    assert api_key == "env-key"
    assert api_base == "https://env.example/anthropic/v1"
    assert model == "EnvModel"


def test_embedding_base_url_does_not_override_chat_base(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.chat")
    monkeypatch.delenv("MINIMAX_API_BASE", raising=False)
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)

    ai_config = {
        "api_key": "config-key",
        "api_base": "https://api.minimaxi.com/anthropic/v1",
        "model": "MiniMax-M2.7",
    }

    for module in (enrich_items, generate_briefing, interest_engine):
        api_key, api_base, model = module.resolve_minimax_runtime_config(ai_config)
        assert api_key == "env-key"
        assert api_base == "https://api.minimaxi.com/anthropic/v1"
        assert model == "MiniMax-M2.7"


def test_clustering_pipeline_chat_config_uses_chat_key_not_embedding_key(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "chat-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://chat.example/anthropic/v1")
    monkeypatch.setenv("MINIMAX_MODEL", "ChatModel")
    monkeypatch.setenv("MINIMAX_EMBEDDING_API_KEY", "embedding-key")
    monkeypatch.setenv("MINIMAX_EMBEDDING_BASE", "https://embedding.example")

    api_key, api_base, model = clustering_pipeline.resolve_minimax_chat_runtime_config({
        "api_key": "config-chat-key",
        "api_base": "https://config.example/anthropic/v1",
        "model": "ConfigModel",
    })

    assert api_key == "chat-key"
    assert api_base == "https://chat.example/anthropic/v1"
    assert model == "ChatModel"


def test_clustering_pipeline_chat_config_reads_project_env_before_config(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_BASE", raising=False)
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)

    api_key, api_base, model = clustering_pipeline.resolve_minimax_chat_runtime_config(
        {
            "api_key": "config-chat-key",
            "api_base": "https://config.example/anthropic/v1",
            "model": "ConfigModel",
        },
        project_env={
            "MINIMAX_API_KEY": "dotenv-chat-key",
            "MINIMAX_API_BASE": "https://dotenv.example/anthropic/v1",
            "MINIMAX_MODEL": "DotenvModel",
            "MINIMAX_EMBEDDING_API_KEY": "dotenv-embedding-key",
        },
    )

    assert api_key == "dotenv-chat-key"
    assert api_base == "https://dotenv.example/anthropic/v1"
    assert model == "DotenvModel"
