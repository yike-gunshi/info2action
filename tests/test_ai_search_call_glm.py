import http.client
import io
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_digest as digest
from ai_provider_guard import guarded_urlopen


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {})
    for key in list(__import__('os').environ):
        if key.startswith(('AI_SEARCH_', 'GLM_CODING_')):
            monkeypatch.delenv(key)
    cfg = digest.settings()
    cfg['GLM_CODING_API_KEY'] = 'fake-test-key'
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen',
                        lambda *a, **kw: pytest.fail('live GLM forbidden'))
    return cfg


def chunk(delta=None, finish_reason=None, **fields):
    return {'choices': [{'delta': delta or {}, 'finish_reason': finish_reason}], **fields}


def stream_response(*events, done=True):
    lines = [event if isinstance(event, str) else
             'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events]
    if done:
        lines.append('data: [DONE]\n\n')
    return io.BytesIO(''.join(lines).encode('utf-8'))


def response():
    return stream_response(
        '\n: heartbeat\nevent: message\n',
        chunk({'role': 'assistant', 'reasoning_content': 'private thinking'}),
        chunk({'content': 'out'}, model='returned-model'),
        chunk({'content': 'put'}),
        chunk({'content': ''}, finish_reason='stop', usage={
            'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30,
            'prompt_tokens_details': {'cached_tokens': 5},
            'completion_tokens_details': {'reasoning_tokens': 15}}),
    )


class BrokenStream(io.BytesIO):
    def __init__(self, error, now, elapsed):
        super().__init__(b'data: {"choices":[{"delta":{"content":"discard me"}}]}\n')
        self.error, self.now, self.elapsed = error, now, elapsed

    def readline(self, *args):
        line = super().readline(*args)
        if not line:
            self.now[0] += self.elapsed
            raise self.error
        return line


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
    assert json.loads(req.data) == dict(model='glm-5.3', messages=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'user'}], max_tokens=131072, reasoning_effort='high', stream=True)
    assert kw['provider'] == 'glm-coding' and kw['source'] == 'ai_search'
    assert kw['timeout'] == 300 and kw['context'] is digest._SSL_CTX
    assert text == 'output' and model == 'returned-model'
    assert usage == {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30,
                     'prompt_tokens_details': {'cached_tokens': 5},
                     'completion_tokens_details': {'reasoning_tokens': 15}}


@pytest.mark.parametrize('error', [HTTPError('url', 503, 'unavailable', {}, io.BytesIO(b'busy')),
                                 URLError('connection reset'), http.client.RemoteDisconnected(),
                                 ConnectionError(), socket.timeout(), TimeoutError()])
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


def test_second_timeout_fails_as_digest_error(monkeypatch, config):
    calls, delays = [], []
    def open_(*args, **kw):
        calls.append(1)
        raise TimeoutError('fake-test-key must not be logged')
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', delays.append)
    with pytest.raises(digest.DigestError, match=r'读超时.*TimeoutError') as exc:
        digest.call_glm('s', 'u', settings=config)
    assert len(calls) == 2 and delays == [30]
    assert config['GLM_CODING_API_KEY'] not in str(exc.value)


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
    assert out['request_params'] == {'model': 'glm-5.3', 'max_tokens': 131072, 'reasoning_effort': 'high', 'stream': True}
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
        with pytest.raises(digest.DigestError):
            digest.call_glm('s', 'u', settings=config)
        assert len(calls) == 1 and delays == []


@pytest.mark.parametrize('done,finish_reason', [(True, None), (False, 'stop'), (False, 'length')])
def test_stream_completion_without_usage_and_model(monkeypatch, config, done, finish_reason):
    stream = stream_response(chunk({'content': '正文'}, finish_reason=finish_reason), done=done)
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', lambda *a, **kw: stream)
    assert digest.call_glm('s', 'u', settings=config) == ('正文', {}, config['AI_SEARCH_MODEL'])
    assert stream.closed


def test_done_stops_before_trailing_data(monkeypatch, config):
    stream = stream_response(chunk({'content': '正文'}))
    data = stream.getvalue() + b'data: {"error":"must not parse after DONE"}\n'
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', lambda *a, **kw: io.BytesIO(data))
    assert digest.call_glm('s', 'u', settings=config)[0] == '正文'


def test_active_stream_can_run_longer_than_fifteen_minutes(monkeypatch, config):
    now = [100.]
    monkeypatch.setattr(digest.time, 'monotonic', lambda: now[0])
    class ActiveStream(io.BytesIO):
        def readline(self, *args):
            now[0] += 90  # Every line arrives within the 300s idle timeout.
            return super().readline(*args)
    stream = ActiveStream(response().getvalue())
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', lambda *a, **kw: stream)
    assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
    assert 900 < now[0] - 100 < config['AI_SEARCH_JOB_TIMEOUT_S']


@pytest.mark.parametrize('events', [[], [chunk({'reasoning_content': 'thinking'})],
                                  [chunk({'content': 'partial'})]])
def test_stream_ended_early(monkeypatch, config, events):
    stream = stream_response(*events, done=False)
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', lambda *a, **kw: stream)
    monkeypatch.setattr(digest.time, 'sleep', lambda _: pytest.fail('must not retry incomplete stream'))
    with pytest.raises(digest.DigestError, match='^GLM stream ended early$'):
        digest.call_glm('s', 'u', settings=config)
    assert stream.closed


@pytest.mark.parametrize('source', ['stream', 'http', 'cooldown'])
def test_provider_errors_are_bounded_and_redacted(monkeypatch, config, source):
    system, user = 'private system prompt', 'private user question'
    detail = f"denied {config['GLM_CODING_API_KEY']} {user} {system} " + '长' * 400
    def open_(*args, **kw):
        if source == 'http':
            raise HTTPError('url', 429, 'error', {}, io.BytesIO(detail.encode()))
        if source == 'cooldown':
            raise digest.ai_provider_guard.ProviderCooldown('glm-coding', None, detail)
        return stream_response({'error': {'message': detail}})
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', lambda _: pytest.fail('must not retry provider error'))
    error_type = digest.DigestError if source == 'stream' else digest.QuotaExhausted
    with pytest.raises(error_type) as exc:
        digest.call_glm(system, user, settings=config)
    message = str(exc.value)
    assert 'denied' in message and '长' in message and len(message) <= 330
    assert all(secret not in message for secret in (config['GLM_CODING_API_KEY'], user, system))


@pytest.mark.parametrize('escaped', [False, True])
def test_error_redacts_question_inside_assembled_input(monkeypatch, config, escaped):
    question = '我的私密问题\n包含"引号"'
    assembled = digest.build_input(question, '2026-10-12', [], settings=config)['user_message']
    detail = json.dumps({'error': f'denied {question} {config["GLM_CODING_API_KEY"]}'},
                        ensure_ascii=escaped)
    def open_(*a, **kw):
        raise HTTPError('url', 400, 'error', {}, io.BytesIO(detail.encode()))
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    with pytest.raises(digest.DigestError) as exc:
        digest.call_glm('private prompt', assembled, settings=config)
    message = str(exc.value)
    assert 'denied' in message and '[redacted]' in message
    assert question not in message and json.dumps(question, ensure_ascii=escaped)[1:-1] not in message
    assert config['GLM_CODING_API_KEY'] not in message


@pytest.mark.parametrize('payload', ['not-json', '[]', '{}',
                                   '{"choices":[{"delta":{"content":123}}]}',
                                   '{"choices":[],"usage":"invalid"}'])
def test_invalid_stream_is_digest_error(monkeypatch, config, payload):
    stream = stream_response('data: ' + payload + '\n')
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', lambda *a, **kw: stream)
    with pytest.raises(digest.DigestError, match='invalid GLM response'):
        digest.call_glm('s', 'u', settings=config)


@pytest.mark.parametrize('elapsed', [119, 120, 904])
@pytest.mark.parametrize('error,timed_out', [
    (http.client.RemoteDisconnected('fake-test-key private question'), False),
    (ConnectionError('fake-test-key private question'), False),
    (socket.timeout('fake-test-key private question'), True),
    (TimeoutError('fake-test-key private question'), True),
    (URLError(TimeoutError('fake-test-key private question')), True),
])
def test_read_failure_retry_boundary(monkeypatch, config, elapsed, error, timed_out):
    now, calls, delays = [100.], [], []
    monkeypatch.setattr(digest.time, 'monotonic', lambda: now[0])
    stream = BrokenStream(error, now, elapsed)
    def open_(*args, **kw):
        calls.append(1)
        return stream if len(calls) == 1 else response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', delays.append)
    if elapsed < 120:
        assert digest.call_glm('s', 'private question', settings=config)[0] == 'output'
        assert len(calls) == 2 and delays == [30]
    else:
        with pytest.raises(digest.DigestError) as exc:
            digest.call_glm('s', 'private question', settings=config)
        message = str(exc.value)
        assert f'{elapsed} 秒后' in message and type(error).__name__ in message
        assert ('读超时' if timed_out else '断开') in message
        assert 'fake-test-key' not in message and 'private question' not in message
        assert len(calls) == 1 and delays == []
    assert stream.closed


@pytest.mark.parametrize('event', [': heartbeat\n', chunk({'reasoning_content': 'thinking'}),
                                  chunk({'content': 'text'}), 'data: [DONE]\n'])
def test_total_timeout_checked_during_read(monkeypatch, config, event):
    config['AI_SEARCH_JOB_TIMEOUT_S'] = 5
    now, calls = [100.], []
    monkeypatch.setattr(digest.time, 'monotonic', lambda: now[0])
    class SlowStream(io.BytesIO):
        def readline(self, *args):
            now[0] += 6
            return super().readline(*args)
    stream = SlowStream(stream_response(event).getvalue())
    def open_(*args, **kw):
        calls.append(kw)
        return stream
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', lambda _: pytest.fail('must not retry job timeout'))
    with pytest.raises(digest.DigestError, match=r'(?i)GLM.*timeout'):
        digest.call_glm('s', 'u', settings=config)
    assert len(calls) == 1 and stream.closed


def test_total_timeout_includes_retry_delay(monkeypatch, config):
    config['AI_SEARCH_JOB_TIMEOUT_S'] = 100
    now, calls = [100.], []
    monkeypatch.setattr(digest.time, 'monotonic', lambda: now[0])
    def open_(*args, **kw):
        calls.append(1)
        now[0] += 80
        raise ConnectionError()
    def sleep(_):
        now[0] += 30
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    monkeypatch.setattr(digest.time, 'sleep', sleep)
    with pytest.raises(digest.DigestError, match=r'(?i)GLM.*timeout'):
        digest.call_glm('s', 'u', settings=config)
    assert len(calls) == 1


@pytest.mark.parametrize('value,expected', [(None, 300), ('bad', 300), ('', 300),
                                          ('0', 30), ('-1', 30), ('29', 30),
                                          ('30', 30), ('450', 450)])
def test_stream_idle_timeout_config(monkeypatch, config, value, expected):
    if value is not None:
        monkeypatch.setenv('AI_SEARCH_STREAM_IDLE_TIMEOUT_S', value)
    seen = []
    def open_(*args, **kw):
        seen.append(kw['timeout'])
        return response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
    assert seen == [expected]


@pytest.mark.parametrize('override,expected', [(None, 60), (10, 30), (90, 90)])
def test_stream_idle_timeout_precedence(monkeypatch, config, override, expected):
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {'AI_SEARCH_STREAM_IDLE_TIMEOUT_S': '45'})
    monkeypatch.setenv('AI_SEARCH_STREAM_IDLE_TIMEOUT_S', '60')
    if override is not None:
        config['AI_SEARCH_STREAM_IDLE_TIMEOUT_S'] = override
    seen = []
    def open_(*args, **kw):
        seen.append(kw['timeout'])
        return response()
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', open_)
    digest.call_glm('s', 'u', settings=config)
    assert seen == [expected]
    monkeypatch.delenv('AI_SEARCH_STREAM_IDLE_TIMEOUT_S')
    config.pop('AI_SEARCH_STREAM_IDLE_TIMEOUT_S', None)
    digest.call_glm('s', 'u', settings=config)
    assert seen[-1] == 45


def test_guard_success_returns_stream_without_reading_and_preserves_cooldown(monkeypatch, config):
    # Exercise the real guard with fake I/O and fake provider state only.
    monkeypatch.setattr(digest.ai_provider_guard, 'guarded_urlopen', guarded_urlopen)
    available, success, rate_limits = [], [], []
    monkeypatch.setattr(digest.ai_provider_guard, 'ensure_provider_available',
                        lambda *a, **kw: available.append((a, kw)))
    monkeypatch.setattr(digest.ai_provider_guard, 'record_success',
                        lambda *a, **kw: success.append((a, kw)))
    monkeypatch.setattr(digest.ai_provider_guard, 'record_rate_limit',
                        lambda *a, **kw: rate_limits.append((a, kw)))
    monkeypatch.setattr(digest.ai_provider_guard, 'is_cooldown_active', lambda _: False)
    def open_(*a, **kw):
        stream = response()
        assert stream.tell() == 0 and not success
        return stream
    monkeypatch.setattr(digest.urllib.request, 'urlopen', open_)
    assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
    assert available == [(('glm-coding',), {'source': 'ai_search'})]
    assert success == [(('glm-coding',), {'source': 'ai_search'})]
    success.clear()
    monkeypatch.setattr(digest.ai_provider_guard, 'is_cooldown_active', lambda _: True)
    assert digest.call_glm('s', 'u', settings=config)[0] == 'output'
    assert not success
    def rate_limited(*a, **kw):
        raise HTTPError('url', 429, 'quota', {}, io.BytesIO(b'quota exhausted'))
    monkeypatch.setattr(digest.urllib.request, 'urlopen', rate_limited)
    with pytest.raises(digest.QuotaExhausted):
        digest.call_glm('s', 'u', settings=config)
    assert rate_limits == [(('glm-coding',), {'source': 'ai_search', 'error': 'HTTP 429: quota'})]
