import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_digest as digest


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {})
    for key in list(__import__('os').environ):
        if key.startswith(('AI_SEARCH_', 'GLM_CODING_')):
            monkeypatch.delenv(key)


def item(n, **kw):
    return dict(id=f'id-{n}', platform='twitter', title='标题', content='正文', **kw)


def test_settings_defaults_and_precedence(monkeypatch):
    expected = {
        'GLM_CODING_API_KEY': '',
        'GLM_CODING_API_BASE': 'https://open.bigmodel.cn/api/coding/paas/v4',
        'AI_SEARCH_MODEL': 'glm-5.3', 'AI_SEARCH_MAX_TOKENS': 131072,
        'AI_SEARCH_REASONING_EFFORT': 'high', 'AI_SEARCH_ITEM_CHARS': 4000,
        'AI_SEARCH_INPUT_BUDGET_TOKENS': 120000, 'AI_SEARCH_JOB_TIMEOUT_S': 2400,
        'AI_SEARCH_PROMPT_PATH': 'prompts/17_ai_search_digest.md',
    }
    assert digest.settings() == expected
    monkeypatch.setattr(digest, 'load_project_env', lambda _: {
        'GLM_CODING_API_KEY': 'project', 'AI_SEARCH_MODEL': 'project-model',
        'AI_SEARCH_ITEM_CHARS': '123',
    })
    monkeypatch.setenv('GLM_CODING_API_KEY', 'env')
    monkeypatch.setenv('AI_SEARCH_ITEM_CHARS', '42')
    assert digest.settings()['GLM_CODING_API_KEY'] == 'env'
    assert digest.settings()['AI_SEARCH_MODEL'] == 'project-model'
    assert digest.settings()['AI_SEARCH_ITEM_CHARS'] == 42


@pytest.mark.parametrize('platform,label', [
    ('twitter', 'X'), ('lingowhale', '公众号'), ('rss', 'RSS'),
    ('reddit', 'Reddit'), ('hackernews', 'Hacker News'), ('github', 'GitHub'),
    ('bilibili', 'B 站'), ('waytoagi', 'WaytoAGI'), ('other', 'other'),
])
def test_input_format_and_platform(platform, label):
    candidate = dict(id='id', platform=platform, title='  标题\n 二\t三 ', content=' 正文\n 内容 ',
                     published_at='2026-09-18T12:00:00', fetched_at='2026-09-19')
    result = digest.build_input('怎么用', '2026-09-19', [candidate])
    assert result['user_message'] == (
        '用户问题：怎么用\n\n今天：2026-09-19\n\n候选条目（共 1 条）：\n\n'
        f'[1] {label} · 2026-09-18 · 标题 二 三\n正文：正文 内容'
    )
    assert result['tiers'] == {'id': 'content'}
    assert isinstance(result['est_tokens'], int)
    assert abs(result['est_tokens'] - len(result['user_message']) / 1.4) < 1


def test_dates_title_truncation_and_empty_items():
    candidates = [dict(id='a', title='  ' + '长' * 90, fetched_at='2026-09-17T00:00'), item(2)]
    out = digest.build_input('q', 'today', candidates)['user_message']
    assert '2026-09-17 · ' + '长' * 80 + '\n' in out
    assert '长' * 81 not in out
    assert '[2] X · 日期未知 · 标题' in out
    empty = digest.build_input('q', 'today', [])
    assert empty['tiers'] == {}
    assert empty['user_message'].endswith('候选条目（共 0 条）：\n\n')


def test_body_fallbacks_and_truncation(monkeypatch):
    monkeypatch.setenv('AI_SEARCH_ITEM_CHARS', '5')
    candidates = [
        dict(id='a', content=' 123456 ', ai_summary='summary'),
        dict(id='b', content=' \n ', ai_key_points='原样\n要点'),
        dict(id='c', ai_key_points=[{'title': '要点', 'points': ['一']}]),
        dict(id='d', ai_key_points=[], ai_summary='摘要'),
        dict(id='e'),
    ]
    original = copy.deepcopy(candidates)
    out = digest.build_input('q', 'today', candidates)
    assert '正文：12345…（已截）' in out['user_message']
    assert '正文：原样\n要点' in out['user_message']
    assert json.dumps(candidates[2]['ai_key_points'], ensure_ascii=False) in out['user_message']
    assert '正文：摘要' in out['user_message']
    assert out['tiers'] == dict(a='content', b='key_points', c='key_points', d='summary', e='summary')
    assert candidates == original


def test_budget_downgrades_tail_first_across_rounds(monkeypatch):
    candidates = [dict(id=str(n), content='c' * 200, ai_key_points='k' * 80, ai_summary='s') for n in range(3)]
    full = digest.build_input('q', 'today', candidates)
    # One content -> key_points reduction is 120 characters.
    monkeypatch.setenv('AI_SEARCH_INPUT_BUDGET_TOKENS', str(int((len(full['user_message']) - 110) / 1.4)))
    out = digest.build_input('q', 'today', candidates)
    assert out['tiers'] == {'0': 'content', '1': 'content', '2': 'key_points'}
    monkeypatch.setenv('AI_SEARCH_INPUT_BUDGET_TOKENS', str(int((len(full['user_message']) - 400) / 1.4)))
    out = digest.build_input('q', 'today', candidates)
    assert out['tiers'] == {'0': 'key_points', '1': 'key_points', '2': 'summary'}
    monkeypatch.setenv('AI_SEARCH_INPUT_BUDGET_TOKENS', '1')
    out = digest.build_input('q', 'today', candidates)
    assert set(out['tiers'].values()) == {'summary'}
    assert out['est_tokens'] > 1


def test_v2_disables_total_budget_degradation_but_keeps_per_item_fallbacks(monkeypatch):
    monkeypatch.setenv('AI_SEARCH_INPUT_BUDGET_TOKENS', '1')
    monkeypatch.setenv('AI_SEARCH_ITEM_CHARS', '5')
    candidates = [dict(id='body', content='123456', ai_key_points='k', ai_summary='s'),
                  dict(id='points', ai_key_points='p', ai_summary='s'), dict(id='summary', ai_summary='s')]
    out = digest.build_input('q', 'today', candidates, degrade=False)
    assert out['tiers'] == {'body': 'content', 'points': 'key_points', 'summary': 'summary'}
    assert '12345…（已截）' in out['user_message'] and out['est_tokens'] > 1
    assert digest.build_input('q', 'today', candidates)['tiers'] == {
        'body': 'summary', 'points': 'summary', 'summary': 'summary'}


def test_writer_budget_counts_rendered_system_prompt_and_removes_whole_tail(monkeypatch):
    import ai_search_retrieval as retrieval
    config = digest.settings()
    monkeypatch.setattr(digest, 'load_prompt', lambda _: ('test', '{query}|{today}|{item_count}|{items}|系统'))
    candidates = [dict(id=str(i), content='正文' * 100, ai_summary='短摘要') for i in range(3)]
    message = digest.build_input('question', '2026-10-11', candidates[:2], settings=config, degrade=False)['user_message']
    system = 'question|2026-10-11|2||系统'
    exact = len(message) + len(system)
    monkeypatch.setattr(retrieval, 'settings', lambda: {
        'AI_SEARCH_CHARS_PER_TOKEN': 1., 'AI_SEARCH_INPUT_MAX_TOKENS': exact})
    original = copy.deepcopy(candidates)
    kept, dropped = digest.trim_input_candidates('question', '2026-10-11', candidates, config)
    assert kept == candidates[:2] and dropped == ['2'] and candidates == original
    out = digest.build_input('question', '2026-10-11', kept, settings=config, degrade=False)
    assert set(out['tiers'].values()) == {'content'}
    assert len(out['user_message']) + len(system) == exact


def test_writer_budget_retains_at_least_one_candidate(monkeypatch):
    import ai_search_retrieval as retrieval
    monkeypatch.setattr(retrieval, 'settings', lambda: {
        'AI_SEARCH_CHARS_PER_TOKEN': 1.7, 'AI_SEARCH_INPUT_MAX_TOKENS': 1})
    candidates = [dict(id=str(i), content='长' * 4000) for i in range(3)]
    kept, dropped = digest.trim_input_candidates('q', 'today', candidates, digest.settings())
    assert kept == candidates[:1] and dropped == ['2', '1']


def test_budget_skips_missing_tiers_and_terminates_when_tiers_grow(monkeypatch):
    monkeypatch.setenv('AI_SEARCH_INPUT_BUDGET_TOKENS', '1')
    out = digest.build_input('q', 'today', [dict(id='a', content='a', ai_summary='s' * 200),
                                           dict(id='b', content='b', ai_key_points='k' * 300, ai_summary='s')])
    assert out['tiers'] == {'a': 'summary', 'b': 'summary'}


@pytest.mark.parametrize('key,default', [
    ('AI_SEARCH_MAX_TOKENS', 131072), ('AI_SEARCH_ITEM_CHARS', 4000),
    ('AI_SEARCH_INPUT_BUDGET_TOKENS', 120000), ('AI_SEARCH_JOB_TIMEOUT_S', 2400),
])
@pytest.mark.parametrize('value', ['invalid', '1.5', '   '])
@pytest.mark.parametrize('source', ['process', 'project'])
def test_integer_settings_invalid_values_fall_back(monkeypatch, key, default, value, source):
    if source == 'process':
        monkeypatch.setenv(key, value)
    else:
        monkeypatch.setattr(digest, 'load_project_env', lambda _: {key: value})
    assert digest.settings()[key] == default
