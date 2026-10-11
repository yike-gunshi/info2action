import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_digest as digest


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {})
    for key in list(__import__('os').environ):
        if key.startswith(('AI_SEARCH_', 'GLM_CODING_')):
            monkeypatch.delenv(key)
    cfg = digest.settings()
    cfg['GLM_CODING_API_KEY'] = 'fake-test-key'
    return cfg


def response():
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'output'}}],
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 20,
                                           'completion_tokens_details': {'reasoning_tokens': 15}},
                                 'model': 'glm-5.3'}).encode())


def test_request_contract(monkeypatch, config):
    seen = []
    def open_(req, **kw):
        seen.append((req, kw))
        return response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    text, usage, model = digest.call_glm('system', 'user', settings=config)
    req, kw = seen[0]
    assert req.full_url == 'https://open.bigmodel.cn/api/coding/paas/v4/chat/completions'
    assert req.get_method() == 'POST'
    assert req.get_header('Authorization') == 'Bearer fake-test-key'
    assert req.get_header('Content-type') == 'application/json'
    assert json.loads(req.data) == dict(model='glm-5.3', messages=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'user'}], max_tokens=131072, reasoning_effort='high')
    assert kw['provider'] == 'glm-coding' and kw['source'] == 'ai_search'
    assert kw['timeout'] == 2400 and kw['context'] is digest._SSL_CTX
    assert text == 'output' and model == 'glm-5.3'
    assert usage['completion_tokens_details']['reasoning_tokens'] == 15


@pytest.mark.parametrize('error', [HTTPError('url', 503, 'unavailable', {}, io.BytesIO(b'busy')), URLError('connection reset')])
def test_transient_retries_once_then_succeeds(monkeypatch, config, error):
    calls, delays = [], []
    def open_(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', delays.append)
    assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
    assert len(calls) == 2 and delays == [30]


def test_second_5xx_fails_and_delay_is_injectable(monkeypatch, config):
    calls, delays = [], []
    def open_(*args, **kw):
        calls.append(1)
        raise HTTPError('url', 500, 'error', {}, io.BytesIO(b'broken'))
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', delays.append)
    config['retry_delay_s'] = 0
    with pytest.raises(digest.DigestError, match='500'):
        digest.call_glm('s', 'u', settings=config)
    assert len(calls) == 2 and delays == [0]


@pytest.mark.parametrize('status,exception', [(429, 'QuotaExhausted'), (400, 'DigestError'), (401, 'DigestError')])
def test_nonretryable_http_error(monkeypatch, config, status, exception):
    body = '额度耗尽' * 100
    def open_(*args, **kw):
        raise HTTPError('url', status, 'error', {}, io.BytesIO(body.encode()))
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', lambda _: pytest.fail('must not retry'))
    with pytest.raises(getattr(digest, exception)) as exc:
        digest.call_glm('s', 'u', settings=config)
    assert body[:300] in str(exc.value)
    assert body[:301] not in str(exc.value)


def test_timeout_propagates_without_retry(monkeypatch, config):
    def open_(*args, **kw):
        raise TimeoutError('timeout')
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', lambda _: pytest.fail('must not retry timeout'))
    with pytest.raises(TimeoutError):
        digest.call_glm('s', 'u', settings=config)


def test_prompt_contract_and_run_orchestration(monkeypatch, config, tmp_path):
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('header\n> 版本：`test-v1`\n\n---\n{query}|{today}|{item_count}|{items}\n{"literal":true}\n---\nend', encoding='utf-8')
    version, template = digest.load_prompt(prompt)
    assert version == 'test-v1'
    assert template == '{query}|{today}|{item_count}|{items}\n{"literal":true}\n---\nend'
    config['AI_SEARCH_PROMPT_PATH'] = str(prompt)
    config['AI_SEARCH_ITEM_CHARS'] = 2
    seen = []
    raw = '```json\n{"content_md":"正文[1]", "relevant_ids":[1]}\n```'
    def call(system, user, *, settings):
        seen.append((system, user, settings))
        return raw, {'completion_tokens': 20}, 'returned-model'
    out = digest.run_digest({'query': '问题{today}'}, [dict(id='a', content='12345')], settings=config,
                           call=call, now=lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert seen[0][0] == '问题{today}|2026-09-19|1|\n{"literal":true}\n---\nend'
    assert '正文：12…（已截）' in seen[0][1]
    assert out['input_text'] == seen[0][1] and out['tiers'] == {'a': 'content'}
    assert out['raw'] == raw and out['usage'] == {'completion_tokens': 20}
    assert out['model'] == 'returned-model' and out['prompt_version'] == 'test-v1'
    assert out['elapsed_ms'] >= 0 and out['cited_ids'] == ['a']
    assert out['request_params'] == {'model': 'glm-5.3', 'max_tokens': 131072, 'reasoning_effort': 'high'}
    assert 'fake-test-key' not in json.dumps(out)


def test_run_uses_default_call_and_propagates_quota(monkeypatch, config):
    error = digest.QuotaExhausted('quota')
    def call(*args, **kw):
        raise error
    monkeypatch.setattr(digest, 'call_glm', call)
    with pytest.raises(digest.QuotaExhausted) as exc:
        digest.run_digest({'query': 'q'}, [], settings=config)
    assert exc.value is error


@pytest.mark.parametrize('elapsed', [119, 120, 200])
@pytest.mark.parametrize('http', [True, False])
def test_transient_retry_only_for_fast_failure(monkeypatch, config, elapsed, http):
    calls, delays = [], []
    now = [100.]
    monkeypatch.setattr(digest.time, 'monotonic', lambda: now[0])
    def open_(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            now[0] += elapsed
            if http:
                raise HTTPError('url', 503, 'error', {}, io.BytesIO(b'busy'))
            raise URLError('connection reset')
        return response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', delays.append)
    if elapsed < 120:
        assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
        assert len(calls) == 2 and delays == [30]
    else:
        with pytest.raises(digest.DigestError if http else URLError):
            digest.call_glm('s', 'u', settings=config)
        assert len(calls) == 1 and delays == []
