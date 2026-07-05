"""Tests for src/clustering/embedding_provider.py

Covers:
- Provider ABC + factory get_provider(name)
- MiniMax embedding: disabled fail-fast guard
- DoubaoEmbeddingProvider / OpenRouterEmbeddingProvider / OpenAIEmbeddingProvider
- Case insensitive name lookup
- Unknown provider raises ValueError
"""
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402
import remote_db as remote_db_mod  # noqa: E402
from clustering import embedding_provider as ep  # noqa: E402


def _fake_urlopen_ok(vectors):
    """Build a context-manager fake response for urllib.request.urlopen."""
    payload = {"vectors": vectors}
    data = json.dumps(payload).encode('utf-8')
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = data
    cm.__exit__.return_value = False
    return cm


def _fake_openrouter_ok(vectors, prompt_tokens=12):
    payload = {
        "data": [
            {"embedding": vector, "index": idx, "object": "embedding"}
            for idx, vector in enumerate(vectors)
        ],
        "model": "openai/text-embedding-3-small",
        "object": "list",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }
    data = json.dumps(payload).encode('utf-8')
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = data
    cm.__exit__.return_value = False
    return cm


class TestFactory:
    def test_get_minimax_provider_is_disabled(self):
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.get_provider('minimax', api_key='k')

    def test_case_insensitive(self):
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.get_provider('MiniMax', api_key='k')

    def test_doubao_factory_returns_doubao_instance(self, monkeypatch):
        # QA-1 fix: production .env can set DOUBAO_EMBEDDING_MODEL=ep-... which
        # would override the default 'doubao-embedding-text-240715' and break
        # the model-name assertion. Force-unset for hermetic test.
        monkeypatch.delenv('DOUBAO_EMBEDDING_MODEL', raising=False)
        p = ep.get_provider('doubao', api_key='k')
        assert isinstance(p, ep.DoubaoEmbeddingProvider)
        assert p.name == 'doubao-embedding-text'
        assert p.api_base.endswith('/api/v3')
        assert 'doubao-embedding' in p.model

    def test_doubao_factory_raises_without_key(self):
        with pytest.raises(RuntimeError, match='DOUBAO_API_KEY'):
            ep.get_provider('doubao', api_key='')

    def test_openai_not_implemented(self):
        p = ep.get_provider('openai', api_key='k')
        with pytest.raises(NotImplementedError):
            p.embed(['hello'])

    def test_openrouter_factory_returns_provider(self):
        p = ep.get_provider('openrouter', api_key='k')
        assert isinstance(p, ep.OpenRouterEmbeddingProvider)
        assert p.name == 'openrouter-text-embedding-3-small'
        assert p.model == 'openai/text-embedding-3-small'
        assert p.dimensions == 1536

    def test_openrouter_factory_raises_without_key(self):
        with pytest.raises(RuntimeError, match='OPENROUTER_API_KEY'):
            ep.get_provider('openrouter', api_key='')

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            ep.get_provider('unknown', api_key='k')

    def test_provider_name_attribute(self):
        p = ep.get_provider('openrouter', api_key='k')
        assert p.name == 'openrouter-text-embedding-3-small'


class TestMiniMaxEmbeddingProvider:
    def test_direct_instantiation_is_disabled_before_http(self):
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.MiniMaxEmbeddingProvider(api_key='k')


class TestOpenRouterEmbeddingProvider:
    def test_embed_returns_np_float32_array_and_records_provider_tokens(self, tmp_path, monkeypatch):
        monkeypatch.setenv('INFO2ACTION_EMBEDDING_USAGE_LOG', '1')
        monkeypatch.setattr(remote_db_mod, 'remote_authority_enabled', lambda: False)
        monkeypatch.setattr(remote_db_mod, 'app_state_to_remote', lambda: False)
        monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed.db'))
        fake_vectors = [[0.1] * 1536, [0.2] * 1536]
        p = ep.OpenRouterEmbeddingProvider(api_key='k')

        with ep.embedding_usage_context(source='unit-test', stage='shootout', run_id=99):
            with patch.object(ep, 'urlopen', return_value=_fake_openrouter_ok(fake_vectors, prompt_tokens=17)):
                out = p.embed(['hello', 'world'])

        assert out.dtype == np.float32
        assert out.shape == (2, 1536)

        conn = db_mod.get_conn()
        row = conn.execute("SELECT * FROM embedding_usage_logs").fetchone()
        conn.close()

        assert row['provider'] == 'openrouter-text-embedding-3-small'
        assert row['model'] == 'openai/text-embedding-3-small'
        assert row['source'] == 'unit-test'
        assert row['stage'] == 'shootout'
        assert row['run_id'] == 99
        assert row['status'] == 'success'
        assert row['estimated_tokens'] == 17
        assert row['token_estimator'] == 'openrouter.usage.prompt_tokens'
        assert row['output_count'] == 2
        assert row['output_dim'] == 1536

    def test_embed_truncates_oversized_openrouter_inputs(self, monkeypatch):
        monkeypatch.setenv('OPENROUTER_EMBEDDING_MAX_INPUT_CHARS', '80')
        fake_vectors = [[0.1] * 1536]
        captured_payloads = []
        p = ep.OpenRouterEmbeddingProvider(api_key='k')

        def fake_urlopen(req, *args, **kwargs):
            captured_payloads.append(json.loads(req.data.decode('utf-8')))
            return _fake_openrouter_ok(fake_vectors, prompt_tokens=8)

        with patch.object(ep, 'urlopen', side_effect=fake_urlopen):
            out = p.embed(['A' * 200])

        assert out.shape == (1, 1536)
        assert len(captured_payloads) == 1
        sent = captured_payloads[0]['input'][0]
        assert len(sent) == 80
        assert sent.startswith('A')
        assert sent.endswith('A')
        assert 'embedding input trimmed' in sent


class TestFakeEmbeddingProvider:
    def test_factory_returns_fake_instance(self):
        p = ep.get_provider('fake')
        assert isinstance(p, ep.FakeEmbeddingProvider)
        assert p.name == 'fake-sha256-1536'

    def test_returns_deterministic_vector(self):
        p = ep.FakeEmbeddingProvider()
        v1 = p.embed(['hello world'])
        v2 = p.embed(['hello world'])
        assert v1.shape == (1, 1536)
        assert v1.dtype == np.float32
        assert np.array_equal(v1, v2)

    def test_different_text_gives_different_vector(self):
        p = ep.FakeEmbeddingProvider()
        out = p.embed(['alpha', 'beta'])
        assert out.shape == (2, 1536)
        # Different inputs should produce different vectors.
        assert not np.array_equal(out[0], out[1])

    def test_l2_normalized(self):
        p = ep.FakeEmbeddingProvider()
        out = p.embed(['some text', 'another text'])
        norms = np.linalg.norm(out, axis=1)
        # tolerate float32 precision
        assert np.allclose(norms, 1.0, atol=1e-3)

    def test_empty_input(self):
        p = ep.FakeEmbeddingProvider()
        out = p.embed([])
        assert out.shape == (0,)


class TestRuntimeKeyResolution:
    def test_minimax_factory_raises_without_key(self):
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.get_provider('minimax', api_key='')

    def test_minimax_factory_raises_with_whitespace_key(self):
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.get_provider('minimax', api_key='   ')

    def test_resolve_runtime_provider_fake_short_circuits(self):
        ep_base = ep._BASE_DIR
        try:
            ep._BASE_DIR = '/tmp/empty-info2action-test-env'
            name, key, base = ep.resolve_runtime_provider({
                'global': {'embedding_provider': 'fake'},
                'ai_summary': {'api_key': 'should-not-be-used'},
            })
        finally:
            ep._BASE_DIR = ep_base
        assert name == 'fake'
        assert key == ''

    def test_minimax_embedding_env_is_disabled(self, monkeypatch):
        monkeypatch.setenv('EMBEDDING_PROVIDER', 'minimax')
        monkeypatch.setenv('MINIMAX_EMBEDDING_API_KEY', 'sk-api-native-embed')
        monkeypatch.setenv('MINIMAX_EMBEDDING_BASE', 'https://api.minimaxi.com')
        # chat env present at the same time — must NOT leak through.
        monkeypatch.setenv('MINIMAX_API_KEY', 'sk-cp-chat-compat')
        monkeypatch.setenv('MINIMAX_BASE_URL', 'https://api.minimax.chat')
        with pytest.raises(RuntimeError, match='MiniMax embedding is disabled'):
            ep.resolve_runtime_provider({})

    def test_project_dotenv_minimax_embedding_key_is_ignored_by_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv('MINIMAX_EMBEDDING_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_BASE', raising=False)
        monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_BASE_URL', raising=False)
        monkeypatch.delenv('EMBEDDING_PROVIDER', raising=False)
        monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
        monkeypatch.delenv('OPENROUTER_EMBEDDING_BASE', raising=False)
        monkeypatch.setattr(ep, '_BASE_DIR', str(tmp_path))
        (tmp_path / '.env').write_text(
            'MINIMAX_EMBEDDING_API_KEY=dotenv-embed-key\n'
            'MINIMAX_EMBEDDING_BASE=https://dotenv.emb\n'
            'MINIMAX_API_KEY=dotenv-chat-key\n',
            encoding='utf-8',
        )

        name, key, base = ep.resolve_runtime_provider({})

        assert name == 'openrouter'
        assert key == ''
        assert base is None

    def test_embedding_does_not_fall_back_to_chat_key(self, monkeypatch):
        """Hard isolation: only MINIMAX_API_KEY set -> embedding must NOT use it."""
        monkeypatch.setattr(ep, '_BASE_DIR', '/tmp/empty-info2action-test-env')
        monkeypatch.delenv('EMBEDDING_PROVIDER', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_BASE', raising=False)
        monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
        monkeypatch.setenv('MINIMAX_API_KEY', 'sk-cp-chat-only')
        monkeypatch.setenv('MINIMAX_BASE_URL', 'https://api.minimax.chat')
        name, key, base = ep.resolve_runtime_provider({})
        assert name == 'openrouter'
        assert key == ''
        assert base is None

    def test_embedding_does_not_fall_back_to_config_ai_summary(self, monkeypatch):
        """Hard isolation: config.ai_summary.api_key (chat config) must NOT leak
        into embedding."""
        monkeypatch.setattr(ep, '_BASE_DIR', '/tmp/empty-info2action-test-env')
        monkeypatch.delenv('EMBEDDING_PROVIDER', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_EMBEDDING_BASE', raising=False)
        monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
        monkeypatch.delenv('MINIMAX_BASE_URL', raising=False)
        monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
        name, key, _ = ep.resolve_runtime_provider({
            'ai_summary': {'api_key': 'config-chat-key'},
            'global': {'embedding_base_url': 'https://config.example.com'},
        })
        assert name == 'openrouter'
        assert key == ''

    def test_resolve_runtime_provider_openrouter_reads_openrouter_env(self, monkeypatch):
        monkeypatch.setattr(ep, '_BASE_DIR', '/tmp/empty-info2action-test-env')
        monkeypatch.setenv('EMBEDDING_PROVIDER', 'openrouter')
        monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
        monkeypatch.setenv('OPENROUTER_EMBEDDING_BASE', 'https://openrouter.example/api/v1')

        name, key, base = ep.resolve_runtime_provider({})

        assert name == 'openrouter'
        assert key == 'or-key'
        assert base == 'https://openrouter.example/api/v1'
