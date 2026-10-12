"""Embedding/rerank credentials resolve locally; all HTTP calls are stubbed."""
import socket
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_retrieval as retrieval
from clustering import embedding_provider as ep


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    for name in (
        'EMBEDDING_PROVIDER', 'OPENROUTER_API_KEY', 'OPENROUTER_API_BASE',
        'OPENROUTER_EMBEDDING_API_KEY', 'OPENROUTER_EMBEDDING_BASE',
        'OPENROUTER_RERANK_API_KEY',
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ep, '_BASE_DIR', str(tmp_path))
    monkeypatch.setattr(retrieval, 'BASE_DIR', tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError('real network/database access forbidden')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(ep, 'urlopen', forbidden)


KEY_CASES = [
    pytest.param({'OPENROUTER_API_KEY': 'legacy-process'}, {}, 'legacy-process', id='legacy-process'),
    pytest.param({'NEW_KEY': 'new-process', 'OPENROUTER_API_KEY': 'legacy-process'}, {},
                 'new-process', id='both-process'),
    pytest.param({'NEW_KEY': 'new-process'}, {}, 'new-process', id='new-process-only'),
    pytest.param({}, {'OPENROUTER_API_KEY': 'legacy-dotenv'}, 'legacy-dotenv', id='legacy-dotenv'),
    pytest.param({}, {'NEW_KEY': 'new-dotenv', 'OPENROUTER_API_KEY': 'legacy-dotenv'},
                 'new-dotenv', id='both-dotenv'),
    pytest.param({}, {'NEW_KEY': 'new-dotenv'}, 'new-dotenv', id='new-dotenv-only'),
    pytest.param({'NEW_KEY': 'new-process'}, {'NEW_KEY': 'new-dotenv'},
                 'new-process', id='new-process-over-dotenv'),
    pytest.param({'OPENROUTER_API_KEY': 'legacy-process'}, {'OPENROUTER_API_KEY': 'legacy-dotenv'},
                 'legacy-process', id='legacy-process-over-dotenv'),
    pytest.param({'OPENROUTER_API_KEY': 'legacy-process'}, {'NEW_KEY': 'new-dotenv'},
                 'new-dotenv', id='dedicated-dotenv-over-legacy-process'),
    pytest.param({'NEW_KEY': '', 'OPENROUTER_API_KEY': 'legacy-process'}, {},
                 'legacy-process', id='empty-new-key-falls-back'),
]


def configure_keys(monkeypatch, tmp_path, dedicated_name, process, dotenv):
    def rename(name):
        return dedicated_name if name == 'NEW_KEY' else name

    for name, value in process.items():
        monkeypatch.setenv(rename(name), value)
    (tmp_path / '.env').write_text(
        ''.join(f'{rename(name)}={value}\n' for name, value in dotenv.items()), encoding='utf-8',
    )


@pytest.mark.parametrize('process,dotenv,expected', KEY_CASES)
def test_embedding_key_precedence(monkeypatch, tmp_path, process, dotenv, expected):
    configure_keys(monkeypatch, tmp_path, 'OPENROUTER_EMBEDDING_API_KEY', process, dotenv)
    monkeypatch.setenv('OPENROUTER_EMBEDDING_BASE', 'https://embedding-gateway.example/api/v1')
    monkeypatch.setenv('OPENROUTER_API_BASE', 'https://rerank-gateway.example/api/v1')
    monkeypatch.setenv('OPENROUTER_RERANK_API_KEY', 'rerank-only')

    name, key, base = ep.resolve_runtime_provider()

    assert (name, key, base) == ('openrouter', expected, 'https://embedding-gateway.example/api/v1')
    provider = ep.get_provider(name, api_key=key, api_base=base)
    assert provider.name == 'openrouter-text-embedding-3-small'
    assert provider.model == 'openai/text-embedding-3-small'
    assert provider.dimensions == 1536


def test_embedding_base_process_over_dotenv(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text('OPENROUTER_EMBEDDING_BASE=https://dotenv.example/api/v1\n')
    assert ep.resolve_runtime_provider() == ('openrouter', '', 'https://dotenv.example/api/v1')
    monkeypatch.setenv('OPENROUTER_EMBEDDING_BASE', 'https://process.example/api/v1')
    assert ep.resolve_runtime_provider() == ('openrouter', '', 'https://process.example/api/v1')


@pytest.mark.parametrize('process,dotenv,expected', KEY_CASES)
@pytest.mark.parametrize('api_base,expected_base', [
    (None, 'https://openrouter.ai/api/v1'),
    ('https://rerank-gateway.example/api/v1/', 'https://rerank-gateway.example/api/v1'),
])
def test_rerank_key_and_endpoint(monkeypatch, tmp_path, process, dotenv, expected,
                                api_base, expected_base):
    configure_keys(monkeypatch, tmp_path, 'OPENROUTER_RERANK_API_KEY', process, dotenv)
    monkeypatch.setenv('OPENROUTER_EMBEDDING_API_KEY', 'embedding-only')
    monkeypatch.setenv('OPENROUTER_EMBEDDING_BASE', 'https://embedding-gateway.example/api/v1')
    if api_base is not None:
        monkeypatch.setenv('OPENROUTER_API_BASE', api_base)
    response = {'results': [{'index': 0, 'relevance_score': .9}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(retrieval, '_post_json', post)

    assert retrieval._rerank('query', ['document'], model='voyageai/rerank-2.5', timeout_s=7) is response

    post.assert_called_once_with(
        expected_base + '/rerank',
        {'model': 'voyageai/rerank-2.5', 'query': 'query', 'documents': ['document']},
        {'Authorization': f'Bearer {expected}'},
        timeout_s=7, provider='openrouter', record_429=False,
    )


def test_rerank_missing_key_names_both_variables(monkeypatch):
    monkeypatch.setenv('OPENROUTER_EMBEDDING_API_KEY', 'embedding-only')
    post = Mock()
    monkeypatch.setattr(retrieval, '_post_json', post)

    with pytest.raises(RuntimeError, match='OPENROUTER_RERANK_API_KEY.*OPENROUTER_API_KEY'):
        retrieval._rerank('query', ['document'], model='voyageai/rerank-2.5', timeout_s=7)

    post.assert_not_called()
