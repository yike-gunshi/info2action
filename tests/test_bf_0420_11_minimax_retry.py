"""BF-0420-11: _call_minimax_translate 自适应 retry 测试。

覆盖:
- 首次成功:1 次调用返文本,不 sleep
- 429 → 重试 → 成功:2 次调用,第 1 次 sleep 指数退避
- 持续 500:重试到 max_retries 后返 None
- 401 不可重试:立即返 None
- URLError/TimeoutError:重试
- 不可解析响应:立即返 None
"""
import json
import os
import sys
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Mock load_minimax_config 避免读真实 env/config.json。"""
    import asr_worker
    monkeypatch.setattr(asr_worker, 'load_minimax_config', lambda: {
        'api_key': 'test-key',
        'api_base': 'https://api.example.com/anthropic/v1',
        'model': 'test-model',
    })


def _json_response(data: dict):
    """构造 urlopen() 可用的响应 context manager。"""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode('utf-8')
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, body: str = ''):
    import urllib.error
    return urllib.error.HTTPError(
        url='https://api.example.com',
        code=code,
        msg='err',
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body.encode('utf-8')),
    )


class TestMiniMaxRetry:
    def test_first_try_success_no_sleep(self):
        import asr_worker
        good = _json_response({'content': [{'type': 'text', 'text': '[1] hello'}]})
        with patch('urllib.request.urlopen', return_value=good) as m, \
             patch('time.sleep') as sleep_spy:
            out = asr_worker._call_minimax_translate('[1] hi', 'translate')
            assert out == '[1] hello'
            assert m.call_count == 1
            sleep_spy.assert_not_called()

    def test_429_then_success_retries_with_backoff(self):
        import asr_worker
        good = _json_response({'content': [{'type': 'text', 'text': 'ok'}]})
        with patch('urllib.request.urlopen', side_effect=[_http_error(429, '{"error":"rate"}'), good]) as m, \
             patch('time.sleep') as sleep_spy:
            out = asr_worker._call_minimax_translate('x', 's')
            assert out == 'ok'
            assert m.call_count == 2
            sleep_spy.assert_called_once()
            # backoff: 2^0 + jitter ∈ [1, 2]
            slept = sleep_spy.call_args[0][0]
            assert 1.0 <= slept < 2.1

    def test_500_persistent_exhausts_retries_returns_none(self):
        import asr_worker
        errors = [_http_error(500) for _ in range(3)]
        with patch('urllib.request.urlopen', side_effect=errors) as m, \
             patch('time.sleep'):
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 3  # max_retries 默认 3

    def test_401_not_retried(self):
        import asr_worker
        with patch('urllib.request.urlopen', side_effect=_http_error(401)) as m, \
             patch('time.sleep') as sleep_spy:
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 1
            sleep_spy.assert_not_called()

    def test_400_bad_request_not_retried(self):
        import asr_worker
        with patch('urllib.request.urlopen', side_effect=_http_error(400, '{"error":"bad"}')) as m, \
             patch('time.sleep'):
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 1

    def test_timeout_retried_then_success(self):
        import asr_worker
        good = _json_response({'content': [{'type': 'text', 'text': 'ok'}]})
        with patch('urllib.request.urlopen', side_effect=[TimeoutError('slow'), good]) as m, \
             patch('time.sleep'):
            out = asr_worker._call_minimax_translate('x', 's')
            assert out == 'ok'
            assert m.call_count == 2

    def test_url_error_retried_exhausted(self):
        import asr_worker
        import urllib.error
        errs = [urllib.error.URLError('conn refused')] * 3
        with patch('urllib.request.urlopen', side_effect=errs) as m, \
             patch('time.sleep'):
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 3

    def test_malformed_json_not_retried(self):
        import asr_worker
        bad = MagicMock()
        bad.read.return_value = b'not-json'
        bad.__enter__ = MagicMock(return_value=bad)
        bad.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=bad) as m:
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 1  # 解析异常立即放弃,不重试

    def test_empty_text_block_returns_none_no_retry(self):
        import asr_worker
        # 返回了 response 但 content[].text 为空
        empty = _json_response({'content': [{'type': 'text', 'text': ''}]})
        with patch('urllib.request.urlopen', return_value=empty) as m, \
             patch('time.sleep') as sleep_spy:
            out = asr_worker._call_minimax_translate('x', 's')
            assert out is None
            assert m.call_count == 1
            sleep_spy.assert_not_called()

    def test_max_retries_1_means_no_retry(self):
        import asr_worker
        with patch('urllib.request.urlopen', side_effect=_http_error(503)) as m, \
             patch('time.sleep') as sleep_spy:
            out = asr_worker._call_minimax_translate('x', 's', max_retries=1)
            assert out is None
            assert m.call_count == 1
            sleep_spy.assert_not_called()

    def test_backoff_monotonic_increasing(self):
        """三次 500 之间的 sleep 应按 2^attempt 递增(2 次 sleep)。"""
        import asr_worker
        errs = [_http_error(503) for _ in range(3)]
        with patch('urllib.request.urlopen', side_effect=errs), \
             patch('time.sleep') as sleep_spy:
            asr_worker._call_minimax_translate('x', 's')
            sleeps = [c[0][0] for c in sleep_spy.call_args_list]
            assert len(sleeps) == 2
            # 2^0 ∈ [1,2], 2^1 ∈ [2,3]
            assert 1.0 <= sleeps[0] < 2.1
            assert 2.0 <= sleeps[1] < 3.1
            assert sleeps[1] > sleeps[0]


class TestTranslateSegmentsCnEndToEnd:
    """BF-0420-11 端到端:translate_segments_cn 在 retry 成功时返回完整列表。"""

    def test_translate_segments_parses_numbered_output(self, monkeypatch):
        """粘合:retry 内层透明化,translate_segments_cn 拿到 _call_minimax_translate
        的返回后正常 parse。验证整个链路从 mock 到最终列表正确。"""
        import asr_worker
        segments = [
            {'text': 'Hello world', 'start_ms': 0, 'end_ms': 1000},
            {'text': 'How are you', 'start_ms': 1000, 'end_ms': 2000},
        ]
        monkeypatch.setattr(asr_worker, '_call_minimax_translate',
                            lambda *a, **kw: '[1] 你好 world\n[2] 你好吗')
        out = asr_worker.translate_segments_cn(segments)
        assert out == ['你好 world', '你好吗']

    def test_translate_segments_end_to_end_with_retry_wrapped_urlopen(self, monkeypatch):
        """端到端:_call_minimax_translate 内部 retry 生效,
        外层 translate_segments_cn 看到的是"瞬时抖动已被吞掉"的结果。"""
        import asr_worker
        segments = [
            {'text': 'Hello', 'start_ms': 0, 'end_ms': 500},
            {'text': 'World', 'start_ms': 500, 'end_ms': 1000},
        ]
        good = _json_response({'content': [{'type': 'text',
                                            'text': '[1] 你好\n[2] 世界'}]})
        with patch('urllib.request.urlopen',
                   side_effect=[_http_error(429), good]) as m, \
             patch('time.sleep'):
            out = asr_worker.translate_segments_cn(segments)
            assert out == ['你好', '世界']
            assert m.call_count == 2  # retry 生效

    def test_translate_segments_all_retries_fail_returns_none(self, monkeypatch):
        import asr_worker
        segments = [{'text': 'x', 'start_ms': 0, 'end_ms': 1}]
        monkeypatch.setattr(asr_worker, '_call_minimax_translate', lambda *a, **kw: None)
        out = asr_worker.translate_segments_cn(segments)
        assert out is None
