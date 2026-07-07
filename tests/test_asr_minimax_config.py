"""BF-0705-6: asr_worker.load_minimax_config 必须 env 优先(oss-release F3c 约定).

生产 config.json 是 git 空模板,真 key 在 .env;asr_worker 曾是唯一漏改消费者
→ ASR 摘要/翻译在生产全 401(「摘要格式异常」+ 只有英文)。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import asr_worker


@pytest.fixture
def fake_base(tmp_path, monkeypatch):
    """隔离 BASE:config.json 空 key 模板,无 .env(模拟生产部署后状态)."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(json.dumps({
        "ai_summary": {
            "api_key": "",
            "api_base": "https://api.minimaxi.com/anthropic/v1",
            "model": "MiniMax-M2.7",
        }
    }))
    monkeypatch.setattr(asr_worker, "BASE", Path(tmp_path))
    return tmp_path


class TestLoadMinimaxConfigEnvPrecedence:
    def test_env_key_wins_over_empty_config(self, fake_base, monkeypatch):
        """config.json 空 key(生产实态)时必须取 MINIMAX_API_KEY env(红:现状返回空串)."""
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-env-key")
        cfg = asr_worker.load_minimax_config()
        assert cfg["api_key"] == "sk-test-env-key"

    def test_env_key_wins_over_stale_config_key(self, fake_base, monkeypatch):
        """config 有旧 key 时 env 新 key 优先(key 轮换即时生效,对齐 resolve_minimax_*)."""
        cfg_path = fake_base / "config" / "config.json"
        data = json.loads(cfg_path.read_text())
        data["ai_summary"]["api_key"] = "sk-stale-rotated-out"
        cfg_path.write_text(json.dumps(data))
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-fresh-env-key")
        cfg = asr_worker.load_minimax_config()
        assert cfg["api_key"] == "sk-fresh-env-key"

    def test_config_fallback_without_env(self, fake_base, monkeypatch):
        """无 env 时保留 config 值(本地老工作流不破坏)."""
        cfg_path = fake_base / "config" / "config.json"
        data = json.loads(cfg_path.read_text())
        data["ai_summary"]["api_key"] = "sk-from-config"
        cfg_path.write_text(json.dumps(data))
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_BASE", raising=False)
        monkeypatch.delenv("MINIMAX_MODEL", raising=False)
        cfg = asr_worker.load_minimax_config()
        assert cfg["api_key"] == "sk-from-config"
        assert cfg["api_base"] == "https://api.minimaxi.com/anthropic/v1"
        assert cfg["model"] == "MiniMax-M2.7"

    def test_env_base_and_model_overlay(self, fake_base, monkeypatch):
        """base/model 同样 env 优先(对齐 generate_summaries F3c 模式)."""
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-x")
        monkeypatch.setenv("MINIMAX_API_BASE", "https://alt.example.com/anthropic/v1/")
        monkeypatch.setenv("MINIMAX_MODEL", "MiniMax-M3")
        cfg = asr_worker.load_minimax_config()
        assert cfg["api_base"] == "https://alt.example.com/anthropic/v1"  # 尾斜杠归一
        assert cfg["model"] == "MiniMax-M3"
