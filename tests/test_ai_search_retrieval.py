"""AI search v2 contract tests: all external operations are injected fakes."""
import importlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_retrieval as retrieval
import ai_provider_guard
import ai_search_digest as digest

GUARDED_URLOPEN = ai_provider_guard.guarded_urlopen
PRIMARY = retrieval.DEFAULTS['AI_SEARCH_RERANK_MODEL']

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
QUESTION = 'Muse 怎么用'


def response(**overrides):
    output = dict(type='entity', entities=[{'name': 'Muse', 'aliases': []}],
                  keyword_queries=['Muse'], semantic_queries=['Muse 的功能和使用体验'],
                  time_window_days=None)
    output.update(overrides)
    return '分析\n```json\n' + json.dumps(output, ensure_ascii=False) + '\n```'


class Fakes:
    def __init__(self, n=4):
        self.clock = 0.0
        self.raw = response()
        self.calls = []
        self.progress = []
        self.items = {str(i): dict(id=str(i), title=f'Muse {i}', ai_summary=' 摘要\n正文 ',
                                  published_at=NOW - timedelta(days=1)) for i in range(n)}
        self.ids = list(self.items)
        self.sims = {i: 1 - int(i) * .1 for i in self.ids}
        self.scores = {i: .9 - int(i) * .01 for i in self.ids}
        self.lengths = {i: 100 for i in self.ids}
        self.counts = {}
        self.lexical_rows = {}
        self.vector_rows = {}
        self.errors = {}
        self.delays = []

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.clock += seconds

    def invoke(self, name, key, **kwargs):
        self.calls.append((name, key, kwargs))
        error = self.errors.get((name, key), self.errors.get(name))
        if error:
            raise error

    def embed(self, texts, **kwargs):
        self.invoke('embed', tuple(texts), **kwargs)
        return [[float(i + 1), 0.] for i in range(len(texts))], {'prompt_tokens': len(texts) * 10}

    def vector_top(self, vector, k=200, **kwargs):
        self.invoke('vector', tuple(vector), k=k, **kwargs)
        return self.vector_rows.get(tuple(vector), [{'id': i, 'sim': self.sims.get(i)} for i in self.ids])[:k]

    def count_phrase(self, keyword, **kwargs):
        self.invoke('count', keyword, **kwargs)
        return self.counts.get(keyword, len(self.ids))

    def lexical_top(self, keyword, k=200, **kwargs):
        self.invoke('lexical', keyword, k=k, **kwargs)
        return self.lexical_rows.get(keyword, [{'id': i, 'score': 1} for i in self.ids])[:k]

    def fetch_meta(self, ids, **kwargs):
        self.invoke('meta', tuple(ids), **kwargs)
        return {i: self.items[i] for i in ids if i in self.items}

    def similarities(self, ids, vector, **kwargs):
        self.invoke('similarities', tuple(ids), **kwargs)
        return {i: self.sims[i] for i in ids if i in self.sims}

    def input_lengths(self, ids, **kwargs):
        self.invoke('lengths', tuple(ids), **kwargs)
        return {i: self.lengths[i] for i in ids if i in self.lengths}

    def minimax(self, system, user, **kwargs):
        self.invoke('minimax', user, system=system, **kwargs)
        return self.raw, {'input_tokens': 50, 'output_tokens': 70}, kwargs['model']

    def rerank(self, question, docs, **kwargs):
        self.invoke('rerank', kwargs['model'], question=question, docs=docs, **kwargs)
        return {'results': [{'index': i, 'relevance_score': self.scores[doc.split('\n')[0].split()[-1]]}
                            for i, doc in reversed(list(enumerate(docs)))],
                'usage': {'total_tokens': len(docs) * 20}}

    def dependencies(self):
        return retrieval.Dependencies(embed=self.embed, count_phrase=self.count_phrase,
                                      lexical_top=self.lexical_top, vector_top=self.vector_top,
                                      fetch_meta=self.fetch_meta, similarities=self.similarities,
                                      input_lengths=self.input_lengths, minimax=self.minimax,
                                      rerank=self.rerank, now=lambda: NOW, monotonic=lambda: self.clock,
                                      sleep=self.sleep)

    def run(self, **kwargs):
        return retrieval.run(QUESTION, deps=self.dependencies(), progress=self.progress.append, **kwargs)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('real network/database access forbidden')
    import urllib.request
    import ai_provider_guard
    from remote_db import _core
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(ai_provider_guard, 'guarded_urlopen', forbidden)
    monkeypatch.setattr(_core, 'connect', forbidden)
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {})


def test_normal_pipeline_last_json_prompt_input_usage_and_progress():
    f = Fakes()
    f.raw = '```json\n{"keyword_queries":["wrong"]}\n```\n' + response()
    result = f.run()
    assert result['outcome'] == 'ok' and result['candidate_ids'] == f.ids
    assert result['lexical_hits'] == 4 and result['vector_hits'] == 8 and result['vector_ok']
    assert result['query_lexical'] == 'Muse' and result['window_days'] == 90
    assert f.progress == [{'stage': 'searching', 'keywords': ['Muse']}]
    call = next(c for c in f.calls if c[0] == 'minimax')
    assert call[1] == '问题：Muse 怎么用\n\n站内参考（用原问题初步检索到的前 20 条标题）：\n' + '\n'.join(f'- Muse {i}' for i in range(4))
    assert call[2]['model'] == 'MiniMax-M3' and call[2]['temperature'] == .01
    assert call[2]['max_tokens'] == 4096 and call[2]['timeout_s'] == 15
    assert '# AI 搜索拆问题 Prompt' not in call[2]['system']
    assert result['retrieval']['understanding']['prompt_version'] == 'ai_search_query_v1_0_2026_10_11'
    assert not result['retrieval']['understanding']['fallback']
    assert result['retrieval']['understanding']['usage']['output_tokens'] == 70
    assert result['retrieval']['rerank']['above_threshold'] == 4
    assert result['retrieval']['rerank']['threshold'] == .65
    assert result['retrieval']['rerank']['min_count'] == 3
    assert {'preview', 'understanding', 'keyword_cleanup', 'lexical', 'vector',
            'merge_filter', 'rerank', 'selection', 'insufficient', 'total'} <= result['retrieval']['timings_s'].keys()
    assert all(row['score'] is not None and row['sim'] is not None and row['legs'] for row in result['candidate_meta'])
    json.dumps(result['retrieval'], allow_nan=False)
    assert all(call[2]['question'] == QUESTION for call in f.calls if call[0] == 'rerank')


@pytest.mark.parametrize('doc_chars', [700, 123])
def test_all_metadata_reads_receive_rerank_summary_allowance(doc_chars):
    f = Fakes()
    f.run(config={'AI_SEARCH_RERANK_DOC_CHARS': doc_chars})
    calls = [call for call in f.calls if call[0] == 'meta']
    assert len(calls) == 2  # Preview titles and the complete merged candidate set.
    assert all(call[2]['summary_chars'] == doc_chars + 300 for call in calls)


@pytest.mark.parametrize('failure', ['timeout', 'bad_json', 'not_object', 'no_fence'])
def test_understanding_failure_uses_rule_keyword_original_vector_and_90_days(failure):
    f = Fakes()
    if failure == 'timeout':
        f.errors['minimax'] = TimeoutError('MiniMax timeout')
    else:
        f.raw = {'bad_json': '```json\n{bad}\n```', 'not_object': '```json\n[]\n```',
                 'no_fence': '{"keyword_queries":["wrong"]}'}[failure]
    result = f.run()
    assert result['retrieval']['understanding']['fallback']
    assert result['query_lexical'] == 'Muse' and result['window_days'] == 90
    assert [c[1] for c in f.calls if c[0] == 'embed'] == [(QUESTION,)]
    assert len([c for c in f.calls if c[0] == 'vector']) == 1
    assert f.progress == [{'stage': 'searching', 'keywords': ['Muse']}]


@pytest.mark.parametrize('fields', [dict(), dict(keyword_queries=123, semantic_queries={}, entities='bad',
                                                time_window_days=True, type=[]),
                                  dict(keyword_queries=[], semantic_queries=[], time_window_days=0)])
def test_missing_or_wrong_fields_fall_back_individually(fields):
    f = Fakes()
    f.raw = '```json\n' + json.dumps(fields) + '\n```'
    result = f.run()
    out = result['retrieval']['understanding']['output']
    assert out['keyword_queries'] == ['Muse'] and out['semantic_queries'] == []
    assert out['entities'] == [] and out['time_window_days'] == 90
    assert result['retrieval']['understanding']['field_fallbacks']
    assert not result['retrieval']['understanding']['fallback']
    assert len([c for c in f.calls if c[0] == 'vector']) == 1


def test_bad_field_does_not_discard_valid_fields_or_window():
    f = Fakes()
    f.raw = response(keyword_queries={}, time_window_days=7)
    out = f.run()['retrieval']['understanding']['output']
    assert out['keyword_queries'] == ['Muse'] and out['semantic_queries'] == ['Muse 的功能和使用体验']
    assert out['time_window_days'] == 7


def test_understanding_failure_keeps_90_day_original_vector_even_with_short_default():
    f = Fakes()
    f.raw = 'invalid output'
    f.items['0']['published_at'] = NOW - timedelta(days=60)
    result = f.run(config={'AI_SEARCH_WINDOW_DAYS': 7})
    vector_calls = [kwargs for name, _, kwargs in f.calls if name == 'vector']
    assert len(vector_calls) == 1 and vector_calls[0]['window_days'] == 90
    assert result['window_days'] == 90 and '0' in result['candidate_ids']


@pytest.mark.parametrize('suffix', ['介绍', '用法', '做法', '写法', '编写', '教程', '案例', '怎么用'])
def test_clean_suffix_then_deduplicate(suffix):
    assert retrieval.clean_keywords([f'Muse {suffix}', 'muse'], []) == ['Muse']


def test_keyword_normalization_fragments_stopwords_order_entities_and_limit():
    words = ['GPT-6', 'gpt 6', 'GPT6', ' voice   cloning ', 'voice-cloning', 'V9', '3.5', 'Pro', 'Max',
             'AI', 'Agent', '模型', '大模型', '工具', '产品', '发布', '教程', '技巧', '用法',
             '语音克隆', 'Muse', 'Cue', 'Instinct', 'Nova', 'Echo', 'Vox']
    assert retrieval.clean_keywords(words, [{'name': 'Other', 'aliases': ['Alias']}]) == [
        'GPT-6', 'voice cloning', '语音克隆', 'Muse', 'Cue', 'Instinct', 'Nova', 'Echo']
    assert retrieval.clean_keywords(['Cue'], [{'name': 'Muse'}, {'name': 'cue'}], max_keywords=3) == ['Cue', 'Muse']
    assert retrieval.clean_keywords(['AI Agent', 'GPT 3.5', 'X Pro', 'V9', '3.5'], []) == ['AI Agent', 'GPT 3.5', 'X Pro']
    assert retrieval.clean_keywords(['V-9', 'v 9', '3 .5', 'Pro', 'Max'], []) == []


def test_df_cap_skips_generic_phrase_but_boundary_is_kept():
    f = Fakes()
    f.raw = response(keyword_queries=['Personal Agent', 'Muse'])
    f.counts.update({'Personal Agent': 1501, 'Muse': 1500})
    result = f.run()
    lex = [leg for leg in result['retrieval']['legs'] if leg['type'] == 'lexical']
    assert lex[0]['skipped'] and lex[0]['hits'] == 1501 and lex[0]['taken'] == 0
    assert lex[0]['skip_reason'] == 'df_cap' and lex[0]['hits_capped'] is True
    assert result['retrieval']['df_cap'] == 1500
    assert all(c[2]['cap'] == 1501 for c in f.calls if c[0] == 'count')
    assert not lex[1]['skipped'] and lex[1]['taken'] == 4
    assert [c[1] for c in f.calls if c[0] == 'lexical'] == ['Muse']


def test_understanding_bounds_keywords_entities_and_semantic_legs():
    f = Fakes()
    long = '长' * 101
    queries = [str(i) + long for i in range(40)]
    f.raw = response(keyword_queries=['字' * 41, 'K' * 40, 'Cue'],
                     entities=[{'name': '实体' * 21}, {'name': 'Muse'}], semantic_queries=queries)
    result = f.run()
    record = result['retrieval']
    assert record['keywords'] == ['K' * 40, 'Cue', 'Muse']
    assert record['understanding']['output']['keyword_queries'] == ['K' * 40, 'Cue']
    assert record['understanding']['output']['entities'] == [{'name': 'Muse', 'aliases': []}]
    assert record['semantic_queries'] == [q[:100] for q in queries[:6]]
    semantic_embed = [c[1] for c in f.calls if c[0] == 'embed'][1]
    assert semantic_embed == tuple(record['semantic_queries'])
    assert len([leg for leg in record['legs'] if leg['type'] == 'vector']) == 7
    assert all(len(leg['query']) == 100 for leg in record['legs'] if leg['type'] == 'vector' and leg['query'] != QUESTION)
    assert retrieval.clean_keywords(['字' * 41, 'X' * 40], [{'name': '名' * 41}]) == ['X' * 40]


@pytest.mark.parametrize('elapsed', [5., 6., 7.])
def test_lexical_count_and_top_share_six_second_deadline(elapsed):
    f = Fakes()
    f.raw = response(keyword_queries=['Muse', 'Cue'], entities=[], semantic_queries=[])
    def count(keyword, **kwargs):
        assert kwargs['timeout_s'] <= 6
        value = f.count_phrase(keyword, **kwargs)
        if keyword == 'Muse':
            f.clock += elapsed
        return value
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), count_phrase=count))
    muse = next(leg for leg in result['retrieval']['legs'] if leg['type'] == 'lexical' and leg['query'] == 'Muse')
    calls = [c for c in f.calls if c[0] == 'lexical' and c[1] == 'Muse']
    if elapsed == 5:
        assert len(calls) == 1 and 0 < calls[0][2]['timeout_s'] <= 1
        assert muse['error'] is None
    else:
        assert not calls and 'TimeoutError' in muse['error']
    assert next(leg for leg in result['retrieval']['legs'] if leg['query'] == 'Cue')['error'] is None
    assert result['outcome'] == 'ok'


def test_lexical_top_timeout_also_respects_total_remaining_time():
    f = Fakes()
    def count(keyword, **kwargs):
        f.clock += 5
        return f.count_phrase(keyword, **kwargs)
    retrieval.run(QUESTION, deps=replace(f.dependencies(), count_phrase=count), timeout_s=5.5)
    assert next(c[2]['timeout_s'] for c in f.calls if c[0] == 'lexical') == .5


def test_lexical_results_returning_after_leg_deadline_are_discarded():
    f = Fakes()
    f.raw = response(keyword_queries=['Muse', 'Cue'], entities=[], semantic_queries=[])
    def count(keyword, **kwargs):
        if keyword == 'Muse':
            f.clock += 5
        return f.count_phrase(keyword, **kwargs)
    def lexical(keyword, **kwargs):
        rows = f.lexical_top(keyword, **kwargs)
        if keyword == 'Muse':
            f.clock += 1.1
        return rows
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), count_phrase=count, lexical_top=lexical))
    leg = next(leg for leg in result['retrieval']['legs'] if leg['query'] == 'Muse')
    assert 'TimeoutError' in leg['error'] and leg['taken'] == 0
    assert all('lexical:Muse' not in label for meta in result['candidate_meta'] for label in meta['legs'])
    assert result['lexical_hits'] == 4 and result['outcome'] == 'ok'


@pytest.mark.parametrize('first_elapsed,expected,failure', [
    (5., 3, '429'), (5.01, 3, '429'), (15., 3, '429'), (15., 2, 'timeout')])
def test_real_rerank_429_keeps_retry_and_cohere_out_of_cooldown(monkeypatch, tmp_path, first_elapsed, expected, failure):
    f = Fakes()
    monkeypatch.setattr(ai_provider_guard, 'STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(ai_provider_guard, 'LOCK_PATH', str(tmp_path / 'state.lock'))
    monkeypatch.setattr(ai_provider_guard, 'guarded_urlopen', GUARDED_URLOPEN)
    rate_limit = Mock(wraps=ai_provider_guard.record_rate_limit)
    monkeypatch.setattr(ai_provider_guard, 'record_rate_limit', rate_limit)
    monkeypatch.setattr(retrieval, '_runtime_env', lambda: {'OPENROUTER_API_KEY': 'fake-test-key'})
    requests = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({'results': [{'index': i, 'relevance_score': .1} for i in range(4)]}).encode()
    def urlopen(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload['model'])
        if len(requests) == 1:
            f.clock += first_elapsed
        if payload['model'].startswith('voyageai/'):
            if failure == 'timeout':
                raise TimeoutError('slow provider')
            raise urllib.error.HTTPError(request.full_url, 429, 'rate limited', {}, None)
        return Response()
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=retrieval._rerank))
    assert requests == [PRIMARY] * (expected - 1) + ['cohere/rerank-4-pro']
    assert f.delays == ([8] if expected == 3 else [])
    assert [a['waited_s'] for a in result['retrieval']['rerank']['attempts']] == (
        [8, 0, 0] if expected == 3 else [0, 0])
    assert ai_provider_guard.load_state('openrouter')['status'] == 'ok'
    state = json.loads((tmp_path / 'state.json').read_text())['providers']['openrouter']
    assert state['consecutive_429'] == 0 and 'cooldown_until' not in state
    rate_limit.assert_not_called()
    assert result['outcome'] == 'ok' and result['retrieval']['rerank']['model'] == 'cohere/rerank-4-pro'
    assert not result['retrieval']['rerank']['threshold_applied']


@pytest.mark.parametrize('retry_after,wait_s', [
    (None, 8), ('3', 3), ('0.5', .5), (' 2.5 ', 2.5), ('15', 15), ('30', 15),
    ('', 8), ('0', 8), ('-1', 8), ('not-seconds', 8),
    ('Sun, 11 Oct 2026 12:00:00 GMT', 8), ('nan', 8), ('inf', 8), ('-inf', 8), ('1e999', 8),
])
def test_primary_429_waits_then_retry_still_checks_insufficient(capsys, retry_after, wait_s):
    f = Fakes()
    f.scores = {i: .1 for i in f.ids}
    headers = None
    if retry_after is not None:
        headers = Message()
        headers['retry-after'] = retry_after
    models = []
    def rerank(query, docs, **kwargs):
        models.append(kwargs['model'])
        if len(models) == 1:
            raise urllib.error.HTTPError('https://example.invalid/rerank', 429, 'rate limited', headers, None)
        return f.rerank(query, docs, **kwargs)
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))
    rr = result['retrieval']['rerank']
    assert models == [PRIMARY] * 2
    assert f.delays == [wait_s]
    assert [a['waited_s'] for a in rr['attempts']] == [wait_s, 0]
    assert rr['attempts'][0]['error'].startswith('HTTPError: HTTP Error 429:')
    assert rr['attempts'][1]['error'] is None
    assert rr['model'] == PRIMARY and not rr['fallback']
    assert rr['threshold_applied'] and rr['above_threshold'] == 0
    assert result['outcome'] == 'insufficient'
    assert rr['elapsed_s'] == wait_s and result['retrieval']['timings_s']['total'] == wait_s
    assert '[warn] ai_search rerank fell back' not in capsys.readouterr().out


@pytest.mark.parametrize('retry_after,wait_s,remaining_s,retry', [
    (None, 8, 27.99, False), (None, 8, 28, True),
    ('30', 15, 34.99, False), ('30', 15, 35, True),
    ('4.5', 4.5, 24.49, False), ('4.5', 4.5, 24.5, True),
])
def test_primary_429_reserves_twenty_seconds_after_wait(capsys, retry_after, wait_s, remaining_s, retry):
    f = Fakes()
    f.scores = {i: .1 for i in f.ids}
    headers = {} if retry_after is None else {'Retry-After': retry_after}
    models, timeouts = [], []
    def rerank(query, docs, **kwargs):
        models.append(kwargs['model'])
        timeouts.append(kwargs['timeout_s'])
        if len(models) == 1:
            # Account for time already spent in the pipeline, not just a short configured limit.
            f.clock = 60 - remaining_s
            raise urllib.error.HTTPError('https://example.invalid/rerank', 429, 'rate limited', headers, None)
        return f.rerank(query, docs, **kwargs)
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))
    rr = result['retrieval']['rerank']
    assert models == [PRIMARY, PRIMARY if retry else 'cohere/rerank-4-pro']
    assert f.delays == ([wait_s] if retry else [])
    assert [a['waited_s'] for a in rr['attempts']] == [wait_s if retry else 0, 0]
    assert rr['attempts'][0]['error'].startswith('HTTPError: HTTP Error 429:')
    assert rr['attempts'][1]['error'] is None
    assert rr['threshold_applied'] is retry and rr['fallback'] is not retry
    assert result['outcome'] == ('insufficient' if retry else 'ok')
    assert timeouts == [15, 15]
    assert capsys.readouterr().out.count('[warn] ai_search rerank fell back') == (0 if retry else 1)


@pytest.mark.parametrize('fallback', ['cohere', 'similarity'])
def test_repeated_429_falls_back_once_without_logging_sensitive_errors(capsys, fallback):
    f = Fakes()
    f.scores = {i: .1 for i in f.ids}
    sentinel = 'private-log-sentinel'
    f.errors[('rerank', PRIMARY)] = urllib.error.HTTPError(
        f'https://example.invalid/rerank?token={sentinel}', 429,
        f'rate limited for {QUESTION}; Authorization: Bearer {sentinel}', {}, None)
    if fallback == 'similarity':
        f.errors[('rerank', 'cohere/rerank-4-pro')] = RuntimeError(f'{QUESTION}: {sentinel}')
    result = f.run()
    rr = result['retrieval']['rerank']
    assert [a['model'] for a in rr['attempts']] == [
        PRIMARY, PRIMARY, 'cohere/rerank-4-pro']
    assert [a['waited_s'] for a in rr['attempts']] == [8, 0, 0] and f.delays == [8]
    assert all(a['error'].startswith('HTTPError: HTTP Error 429:') for a in rr['attempts'][:2])
    assert rr['fallback'] and not rr['threshold_applied'] and result['outcome'] == 'ok'
    assert rr['model'] == ('cohere/rerank-4-pro' if fallback == 'cohere' else 'similarity')
    if fallback == 'similarity':
        assert rr['attempts'][2]['error'].startswith('RuntimeError:')
        assert all(row['score'] is None for row in result['candidate_meta'])
    else:
        assert rr['attempts'][2]['error'] is None
    warnings = [line for line in capsys.readouterr().out.splitlines()
                if line.startswith('[warn] ai_search rerank fell back')]
    assert len(warnings) == 1
    warning = warnings[0]
    assert rr['model'] in warning and warning.count(PRIMARY) == 2
    assert 'cohere/rerank-4-pro' in warning and warning.count('HTTPError HTTP 429') == 2
    assert ('RuntimeError' if fallback == 'similarity' else 'ok') in warning
    assert QUESTION not in warning and sentinel not in warning and 'Authorization' not in warning


@pytest.mark.parametrize('failure,elapsed,retry', [
    (urllib.error.HTTPError('https://example.invalid', 503, 'unavailable', {}, None), 0, True),
    (urllib.error.HTTPError('https://example.invalid', 503, 'unavailable', {}, None), 5, True),
    (urllib.error.URLError('connection failed'), 2, True),
    (urllib.error.HTTPError('https://example.invalid', 503, 'unavailable', {}, None), 5.01, False),
    (TimeoutError('slow provider'), 15, False),
])
def test_non_429_preserves_fast_retry_and_slow_fallback(capsys, failure, elapsed, retry):
    f = Fakes()
    f.scores = {i: .1 for i in f.ids}
    models = []
    def rerank(query, docs, **kwargs):
        models.append(kwargs['model'])
        if len(models) == 1:
            f.clock += elapsed
            raise failure
        return f.rerank(query, docs, **kwargs)
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))
    rr = result['retrieval']['rerank']
    assert models == [PRIMARY, PRIMARY if retry else 'cohere/rerank-4-pro']
    assert f.delays == ([1] if retry else [])
    assert [a['waited_s'] for a in rr['attempts']] == [1 if retry else 0, 0]
    assert rr['attempts'][0]['error'].startswith(type(failure).__name__ + ':')
    assert rr['threshold_applied'] is retry and rr['fallback'] is not retry
    assert result['outcome'] == ('insufficient' if retry else 'ok')
    assert capsys.readouterr().out.count('[warn] ai_search rerank fell back') == (0 if retry else 1)


def test_waited_s_records_actual_sleep_duration():
    f = Fakes()
    models = []
    def rerank(query, docs, **kwargs):
        models.append(kwargs['model'])
        if len(models) == 1:
            raise urllib.error.HTTPError('https://example.invalid', 429, 'rate limited', {}, None)
        return f.rerank(query, docs, **kwargs)
    def oversleep(seconds):
        f.sleep(seconds)
        f.clock += .25
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank, sleep=oversleep))
    assert f.delays == [8]
    assert [a['waited_s'] for a in result['retrieval']['rerank']['attempts']] == [8.25, 0]


@pytest.mark.parametrize('n', [0, 4])
def test_no_fallback_warning_when_no_candidates_or_primary_succeeds(capsys, n):
    result = Fakes(n=n).run()
    rr = result['retrieval']['rerank']
    assert not rr['fallback']
    assert all(a['waited_s'] == 0 for a in rr['attempts'])
    assert '[warn] ai_search rerank fell back' not in capsys.readouterr().out


def test_real_minimax_still_records_429_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_provider_guard, 'STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(ai_provider_guard, 'LOCK_PATH', str(tmp_path / 'state.lock'))
    monkeypatch.setattr(ai_provider_guard, 'guarded_urlopen', GUARDED_URLOPEN)
    monkeypatch.setattr(retrieval, '_runtime_env', lambda: {'MINIMAX_API_KEY': 'fake-test-key'})
    def urlopen(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 429, 'rate limited', {}, None)
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    with pytest.raises(urllib.error.HTTPError):
        retrieval._minimax('system', 'user', model='MiniMax-M3', temperature=.01, max_tokens=20, timeout_s=1)
    assert ai_provider_guard.load_state('minimax')['status'] == 'cooldown'


def test_slow_primary_timeout_goes_directly_to_fallback():
    f = Fakes()
    def rerank(query, docs, **kwargs):
        f.invoke('rerank', kwargs['model'], **kwargs)
        if kwargs['model'].startswith('voyageai/'):
            f.clock += 15
            raise TimeoutError('slow provider')
        return {'results': [{'index': i, 'relevance_score': .1} for i in range(len(docs))]}
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))
    assert [c[1] for c in f.calls if c[0] == 'rerank'] == [PRIMARY, 'cohere/rerank-4-pro']
    assert not f.delays and result['outcome'] == 'ok'


def test_budget_estimate_uses_configured_digest_prompt(monkeypatch, tmp_path):
    prompt = tmp_path / 'custom.md'
    prompt.write_text('> 版本：`custom`\n\n---\n' + '系统' * 10000)
    monkeypatch.setenv('AI_SEARCH_PROMPT_PATH', str(prompt))
    result = Fakes().run(config={'AI_SEARCH_INPUT_MAX_TOKENS': 1000})
    assert result['retrieval']['selection']['system_prompt_chars'] == 20000
    assert not result['candidate_ids']


@pytest.mark.parametrize('failed', ['count', 'lexical', 'vector'])
def test_one_leg_failure_keeps_other_legs(failed):
    f = Fakes()
    f.raw = response(keyword_queries=['Muse', 'Cue'])
    if failed == 'vector':
        f.errors[('vector', (1., 0.))] = TimeoutError('one leg timeout')
    else:
        f.errors[(failed, 'Cue')] = TimeoutError('one leg timeout')
    result = f.run()
    assert result['outcome'] == 'ok' and result['candidate_ids'] == f.ids
    assert any(leg['error'] and 'one leg timeout' in leg['error'] for leg in result['retrieval']['legs'])


@pytest.mark.parametrize('semantic_only', [False, True])
def test_embedding_failure_uses_only_fulltext(semantic_only):
    f = Fakes()
    f.errors[('embed', ('Muse 的功能和使用体验',)) if semantic_only else 'embed'] = RuntimeError('embedding failed')
    result = f.run()
    assert result['outcome'] == 'ok' and result['vector_hits'] == 0 and not result['vector_ok']
    assert all(all(leg.startswith('lexical:') for leg in row['legs']) for row in result['candidate_meta'])
    if not semantic_only:
        user = next(c[1] for c in f.calls if c[0] == 'minimax')
        assert user.endswith('\n（无）')
        assert not any(c[0] == 'vector' for c in f.calls)


def test_all_legs_failed_raises_and_empty_success_is_not_failure():
    f = Fakes()
    f.errors.update(count=TimeoutError('db down'), vector=TimeoutError('db down'))
    with pytest.raises(retrieval.RetrievalError, match='找资料失败') as exc:
        f.run()
    assert exc.value.code == 'retrieval_failed'
    assert exc.value.retrieval['legs']
    empty = Fakes(n=0).run()
    assert empty['outcome'] == 'insufficient' and empty['candidate_ids'] == []
    assert not [leg for leg in empty['retrieval']['legs'] if leg['error']]


def test_all_embedding_and_lexical_failed_raises():
    f = Fakes()
    f.errors.update(embed=RuntimeError('embedding failed'), lexical=TimeoutError('db down'))
    with pytest.raises(retrieval.RetrievalError, match='找资料失败'):
        f.run()


def test_only_df_skips_are_successful_empty_retrieval():
    f = Fakes()
    f.errors['embed'] = RuntimeError('embedding down')
    f.counts['Muse'] = 1501
    result = f.run()
    assert result['outcome'] == 'insufficient' and not result['candidate_ids']


def test_seven_day_window_filters_all_candidates_including_preview_and_passes_scope():
    f = Fakes()
    f.raw = response(time_window_days=7)
    f.items['0']['published_at'] = NOW - timedelta(days=8)
    f.items['1']['published_at'] = (NOW - timedelta(days=7)).isoformat()
    f.items['2']['published_at'] = None
    result = f.run(public_only=False, user_id='viewer', manual_owner_user_id='owner', min_github_stars=123)
    assert result['candidate_ids'] == ['1', '3'] and result['window_days'] == 7
    for name, _, kwargs in f.calls:
        if name in {'count', 'lexical', 'vector', 'meta', 'similarities', 'lengths'}:
            assert kwargs['public_only'] is False and kwargs['user_id'] == 'viewer'
            assert kwargs['manual_owner_user_id'] == 'owner' and kwargs['min_github_stars'] == 123
            assert kwargs['window_days'] in {7, 90} and kwargs['timeout_s'] > 0
    assert all(kwargs['window_days'] == 7 for name, _, kwargs in f.calls if name in {'count', 'lexical', 'lengths'})


@pytest.mark.parametrize('window', [None, -1, 0, True, '7', 7.5, 90, 365])
def test_invalid_or_long_window_uses_configured_default(window):
    f = Fakes()
    f.raw = response(time_window_days=window)
    assert f.run(config={'AI_SEARCH_WINDOW_DAYS': 120})['window_days'] == 120


@pytest.mark.parametrize('fallback', ['cohere', 'similarity'])
def test_rerank_fallback_chain_and_no_insufficient_check(fallback):
    f = Fakes()
    f.scores = {i: .1 for i in f.ids}
    f.errors[('rerank', PRIMARY)] = RuntimeError('voyage failed')
    if fallback == 'similarity':
        f.errors[('rerank', 'cohere/rerank-4-pro')] = RuntimeError('cohere failed')
    result = f.run()
    assert result['outcome'] == 'ok'
    rr = result['retrieval']['rerank']
    assert rr['fallback'] and rr['model'] == ('cohere/rerank-4-pro' if fallback == 'cohere' else 'similarity')
    assert not rr['threshold_applied']
    assert [c[1] for c in f.calls if c[0] == 'rerank'] == [
        PRIMARY, PRIMARY, 'cohere/rerank-4-pro']
    if fallback == 'similarity':
        assert result['candidate_ids'] == f.ids
        assert all(row['score'] is None for row in result['candidate_meta'])


def test_primary_retry_success_still_checks_insufficient():
    f = Fakes()
    calls = []
    def rerank(*args, **kwargs):
        calls.append(kwargs['model'])
        if len(calls) == 1:
            raise TimeoutError('transient')
        return {'results': [{'index': i, 'relevance_score': .2} for i in range(len(args[1]))]}
    deps = replace(f.dependencies(), rerank=rerank)
    result = retrieval.run(QUESTION, deps=deps)
    assert result['outcome'] == 'insufficient'
    assert calls == [PRIMARY] * 2
    assert not result['retrieval']['rerank']['fallback']


def test_tied_scores_use_original_similarity_missing_last_and_negative_before_missing():
    f = Fakes()
    f.scores = {i: .8 for i in f.ids}
    f.sims = {'0': -.1, '1': .5, '2': .9}
    result = f.run()
    assert result['candidate_ids'] == ['2', '1', '0', '3']
    assert result['candidate_meta'][-1]['sim'] is None
    assert len([c for c in f.calls if c[0] == 'similarities']) == 1


def test_semantic_similarity_never_substitutes_for_original_similarity():
    f = Fakes()
    f.scores = {i: .8 for i in f.ids}
    f.vector_rows[(1., 0.)] = [{'id': i, 'sim': .99} for i in reversed(f.ids)]
    f.sims = {i: int(i) / 10 for i in f.ids}
    assert f.run()['candidate_ids'] == list(reversed(f.ids))


def test_rerank_doc_uses_150_char_title_summary_whitespace_and_700_cap():
    f = Fakes()
    f.items['0'].update(title='T' * 200, ai_summary='  summary\n' + 'S' * 1000)
    def rerank(query, docs, **kwargs):
        assert docs[0].startswith('T' * 150 + '\nsummary ')
        assert len(docs[0]) == 700 and '\n\n' not in docs[0]
        return {'results': [{'index': i, 'relevance_score': .8} for i in range(len(docs))]}
    assert retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))['outcome'] == 'ok'


def test_batch_size_four_workers_and_reversed_indices_stay_aligned():
    f = Fakes(n=1101)
    f.scores = {i: .8 + int(i) / 10000 for i in f.ids}
    f.sims = {i: .5 for i in f.ids}
    lock = threading.Lock()
    active = 0
    peak = 0
    batch_sizes = []
    def rerank(query, docs, **kwargs):
        nonlocal active, peak
        assert query == QUESTION
        with lock:
            active += 1
            peak = max(peak, active)
            batch_sizes.append(len(docs))
        time.sleep(.01)
        data = f.rerank(query, docs, **kwargs)
        with lock:
            active -= 1
        return data
    deps = replace(f.dependencies(), rerank=rerank)
    result = retrieval.run(QUESTION, deps=deps, config={'AI_SEARCH_LEG_LIMIT': 1200})
    assert sorted(batch_sizes) == [101, 250, 250, 250, 250]
    assert 2 <= peak <= 4
    assert result['candidate_ids'] == [str(i) for i in range(1100, 900, -1)]


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'nan', 'bad_index'])
def test_malformed_rerank_scores_trigger_fallback(bad):
    f = Fakes()
    def rerank(query, docs, *, model, **kwargs):
        if model.startswith('cohere'):
            return f.rerank(query, docs, model=model, **kwargs)
        rows = [{'index': i, 'relevance_score': .8} for i in range(len(docs))]
        if bad == 'missing':
            rows.pop()
        elif bad == 'duplicate':
            rows[-1]['index'] = 0
        elif bad == 'nan':
            rows[-1]['relevance_score'] = float('nan')
        else:
            rows[-1]['index'] = len(docs)
        return {'results': rows}
    result = retrieval.run(QUESTION, deps=replace(f.dependencies(), rerank=rerank))
    assert result['retrieval']['rerank']['model'] == 'cohere/rerank-4-pro'


def test_top_n_then_300000_token_budget_removes_whole_tail():
    f = Fakes(n=8)
    # Five items leave room for the real digest system prompt; six do not.
    f.lengths = {i: 99000 for i in f.ids}
    f.scores = {i: .9 - int(i) / 100 for i in f.ids}
    result = f.run(config={'AI_SEARCH_TOP_N': 7})
    assert result['candidate_ids'] == f.ids[:5]
    trunc = result['retrieval']['selection']
    assert trunc['top_n_count'] == 7 and trunc['dropped_count'] == 2
    assert trunc['estimated_tokens'] <= 300000
    assert trunc['system_prompt_chars'] > 0
    assert len(result['candidate_meta']) == 5


def test_system_prompt_question_and_separators_are_included_in_exact_budget():
    f = Fakes()
    _, prompt = retrieval.load_prompt('prompts/17_ai_search_digest.md')
    prefix = f'用户问题：{QUESTION}\n\n今天：2026-10-11\n\n候选条目（共 4 条）：\n\n'
    exact_chars = sum(f.lengths.values()) + len(prompt) + len(prefix) + 6
    result = f.run(config={'AI_SEARCH_CHARS_PER_TOKEN': 1., 'AI_SEARCH_INPUT_MAX_TOKENS': exact_chars})
    assert result['candidate_ids'] == f.ids
    assert result['retrieval']['selection']['estimated_tokens'] == exact_chars
    result = f.run(config={'AI_SEARCH_CHARS_PER_TOKEN': 1., 'AI_SEARCH_INPUT_MAX_TOKENS': exact_chars - 1})
    assert result['candidate_ids'] == f.ids[:3]
    assert result['retrieval']['selection']['dropped_count'] == 1


@pytest.mark.parametrize(('qualified', 'outcome'), [(2, 'insufficient'), (3, 'ok')])
@pytest.mark.parametrize('primary', [None, 'voyageai/rerank-2.5'])
def test_insufficient_threshold_inclusive_boundary_and_only_five_cards(qualified, outcome, primary):
    f = Fakes(n=8)
    f.scores = {i: .65 if int(i) < qualified else .649999 for i in f.ids}
    result = f.run(config={'AI_SEARCH_RERANK_MODEL': primary} if primary is not None else {})
    assert result['outcome'] == outcome
    rr = result['retrieval']['rerank']
    assert rr['model'] == (PRIMARY if primary is None else primary)
    assert rr['threshold_applied'] and not rr['fallback']
    assert rr['above_threshold'] == qualified
    assert len(result['candidate_ids']) == (5 if outcome == 'insufficient' else 8)


def test_config_reads_all_spec_defaults_and_env(monkeypatch):
    for key in retrieval.DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    config = retrieval.settings()
    assert config == retrieval.DEFAULTS
    assert config['AI_SEARCH_RERANK_MODEL'] == 'voyageai/rerank-3'
    monkeypatch.setenv('AI_SEARCH_QU_MODEL', 'custom')
    monkeypatch.setenv('AI_SEARCH_TOP_N', '10')
    monkeypatch.setenv('AI_SEARCH_CHARS_PER_TOKEN', '2.5')
    monkeypatch.setenv('AI_SEARCH_QU_TIMEOUT_S', 'bad')
    assert retrieval.settings()['AI_SEARCH_QU_MODEL'] == 'custom'
    assert retrieval.settings()['AI_SEARCH_TOP_N'] == 10
    assert retrieval.settings()['AI_SEARCH_CHARS_PER_TOKEN'] == 2.5
    assert retrieval.settings()['AI_SEARCH_QU_TIMEOUT_S'] == 15


@pytest.mark.parametrize('step', ['embed', 'minimax', 'count', 'lexical', 'vector', 'meta', 'similarities', 'rerank', 'lengths'])
def test_total_timeout_is_checked_after_every_external_call(step):
    f = Fakes()
    deps = f.dependencies()
    old = getattr(deps, step if step not in {'count', 'lexical', 'vector', 'meta', 'lengths'} else
                  {'count': 'count_phrase', 'lexical': 'lexical_top', 'vector': 'vector_top',
                   'meta': 'fetch_meta', 'lengths': 'input_lengths'}[step])
    def slow(*args, **kwargs):
        result = old(*args, **kwargs)
        f.clock = 61
        return result
    name = {'count': 'count_phrase', 'lexical': 'lexical_top', 'vector': 'vector_top',
            'meta': 'fetch_meta', 'lengths': 'input_lengths'}.get(step, step)
    with pytest.raises(retrieval.RetrievalTimeout, match='找资料超时') as exc:
        retrieval.run(QUESTION, deps=replace(deps, **{name: slow}))
    assert exc.value.code == 'retrieval_timeout'


def test_explicit_short_total_deadline_caps_external_timeout():
    f = Fakes()
    def minimax(*args, **kwargs):
        assert kwargs['timeout_s'] <= 2
        f.clock = 2.1
        raise TimeoutError('late')
    with pytest.raises(retrieval.RetrievalTimeout):
        retrieval.run(QUESTION, deps=replace(f.dependencies(), minimax=minimax), timeout_s=2)


def test_default_dependencies_import_and_assembly_are_inert():
    importlib.reload(retrieval)
    deps = retrieval.default_dependencies()
    assert callable(deps.embed) and callable(deps.vector_top) and callable(deps.rerank)
