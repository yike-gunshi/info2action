import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_search_digest as digest


def candidates(n=8):
    return [dict(id=f'item-{i}', platform='twitter', title=f'标题{i}') for i in range(1, n + 1)]


def raw(content, **kw):
    return '```json\n' + json.dumps(dict(content_md=content, **kw), ensure_ascii=False) + '\n```'


def test_last_json_block_and_original_result():
    result = dict(content_md='新正文[2]。再引用[1][2]。', relevant_ids=[1, 2], marginal_ids=[], irrelevant_ids=[3], extra={'keep': True})
    out = digest.postprocess(raw('旧[3]') + '\n分析\n' + raw(result['content_md'], **{k: v for k, v in result.items() if k != 'content_md'}), candidates(3))
    assert out['result'] == result
    assert out['cited_numbers'] == [1, 2]
    assert out['cited_candidate_numbers'] == [2, 1]
    assert out['content_md_final'].startswith('新正文[1]。再引用[2][1]。')
    assert out['cited_ids'] == ['item-2', 'item-1']
    assert out['content_md_final'].endswith('\n\n基于 2 条内容')
    assert (out['relevant_count'], out['marginal_count'], out['irrelevant_count']) == (2, 0, 1)


@pytest.mark.parametrize('boundary', ['。', '！', '？', '；', '\n'])
def test_invalid_citations_removed_and_limit_resets_per_sentence(boundary):
    text = '- 内容[0][99][1][2][3][4][5][6][7]' + boundary + '- 新句[8][1][2][3][4][5]'
    out = digest.postprocess(raw(text), candidates())
    final = out['content_md_final']
    assert '[0]' not in final and '[99]' not in final
    assert '内容[1][2][3][4][5]' + boundary in final
    assert '- 新句[6][1][2][3][4]' in final
    assert '[7]' not in final and '[8]' not in final
    assert out['cited_numbers'] == [1, 2, 3, 4, 5, 6]
    assert out['cited_candidate_numbers'] == [1, 2, 3, 4, 5, 8]
    assert out['cited_ids'] == ['item-1', 'item-2', 'item-3', 'item-4', 'item-5', 'item-8']
    assert final.endswith('基于 6 条内容')


@pytest.mark.parametrize('existing', [False, True])
def test_append_uncited_to_existing_or_new_section(existing):
    text = '正文[2]。'
    if existing:
        text += '\n\n**其他相关内容**\n\n- 已有[3]\n\n**尚未找到资料的问题**\n\n- 价格'
    items = candidates(4)
    items[0]['title'] = ' 长' * 70
    out = digest.postprocess(raw(text, relevant_ids=[1, 2, 1, 99], marginal_ids=[3, 4, 0]), items)
    final = out['content_md_final']
    assert final.count('**其他相关内容**') == 1
    assert '- X · ' + ('长 ' * 70).strip()[:60] + (' [3]' if existing else ' [2]') in final
    assert '- X · 标题4 [4]' in final
    assert out['cited_numbers'] == [1, 2, 3, 4]
    assert out['cited_candidate_numbers'] == ([2, 3, 1, 4] if existing else [2, 1, 3, 4])
    if existing:
        assert final.index('[4]') < final.index('**尚未找到资料的问题**')
    assert final.endswith('基于 4 条内容')
    assert out['relevant_count'] == 2
    assert out['marginal_count'] == 2


def test_heading_conversion_and_empty_citation_footer():
    out = digest.postprocess(raw('# 标题\n正文\n## 子标题\n下一段'), [])
    assert out['content_md_final'] == '**标题**\n\n正文\n\n**子标题**\n\n下一段\n\n基于 0 条内容'
    assert out['cited_ids'] == []
    assert out['cited_numbers'] == out['cited_candidate_numbers'] == []


def test_first_appearance_numbers_match_cited_ids():
    out = digest.postprocess(raw('正文[42]。再引用[9]。重复[42]。'), candidates(50))
    assert out['content_md_final'] == '正文[1]。再引用[2]。重复[1]。\n\n基于 2 条内容'
    assert out['cited_numbers'] == [1, 2]
    assert out['cited_candidate_numbers'] == [42, 9]
    assert out['cited_ids'] == ['item-42', 'item-9']


def test_renumbering_swaps_numbers_in_a_single_pass():
    content, original = digest.renumber_citations('先[2]。后[1]。重复[2][1]', 2)
    assert original == [2, 1]
    assert content == '先[1]。后[2]。重复[1][2]'


def test_renumbering_preserves_unmapped_citations():
    content, original = digest.renumber_citations('无效[0][99]，来源[4]。再引[2][4]', 4)
    assert original == [4, 2]
    assert content == '无效[0][99]，来源[1]。再引[2][1]'


def test_regex_fallback_decodes_escaped_json_string():
    content = '正文说"好"\n路径 C:\\tmp [1]'
    broken = '```json\n{"content_md": ' + json.dumps(content) + ', broken}\n```'
    out = digest.postprocess(raw('旧[2]') + '\n' + broken, candidates(2))
    assert out['result']['content_md'] == content
    assert out['content_md_final'].startswith(content)
    assert out['cited_numbers'] == [1]
    assert digest.postprocess('"content_md": "无围栏[1]"', candidates())['cited_numbers'] == [1]


@pytest.mark.parametrize('value', ['没有 JSON', '```json\n[]\n```', raw(123), '"content_md": "bad\\escape"'])
def test_parse_error_retains_raw(value):
    with pytest.raises(digest.ParseError) as exc:
        digest.postprocess(value, candidates())
    assert exc.value.raw == value
    assert 'parse_error' in str(exc.value)


def test_parse_error_raw_is_bounded_in_bytes():
    value = '坏' * 210000
    with pytest.raises(digest.ParseError) as exc:
        digest.postprocess(value, [])
    assert len(exc.value.raw.encode('utf-8')) <= 200 * 1024
    assert value.startswith(exc.value.raw)


@pytest.mark.parametrize('text', ['#话题', '####### 标题'])
def test_non_heading_hash_lines_preserved(text):
    assert digest.postprocess(raw(text), [])['content_md_final'] == text + '\n\n基于 0 条内容'


@pytest.mark.parametrize('body', ['', ' ', '\n\t\r ', '\u3000\u00a0'])
@pytest.mark.parametrize('fenced', [True, False])
def test_whitespace_only_body_is_parse_error_even_with_related_items(body, fenced):
    value = raw(body, relevant_ids=[1]) if fenced else json.dumps({'content_md': body})
    with pytest.raises(digest.ParseError) as exc:
        digest.postprocess(value, candidates())
    assert exc.value.raw == value


@pytest.mark.parametrize('body', ['[99]', ' [0][99]\n\t', '[99]\u3000'])
def test_body_empty_after_citation_cleanup_is_parse_error(body):
    value = raw(body, relevant_ids=[99], marginal_ids=[0])
    with pytest.raises(digest.ParseError) as exc:
        digest.postprocess(value, candidates(2))
    assert exc.value.raw == value


def test_related_items_can_fill_body_after_invalid_citation_cleanup():
    out = digest.postprocess(raw('[99]', relevant_ids=[1]), candidates(2))
    assert out['content_md_final'] == '**其他相关内容**\n\n- X · 标题1 [1]\n\n基于 1 条内容'
    assert out['cited_ids'] == ['item-1']
