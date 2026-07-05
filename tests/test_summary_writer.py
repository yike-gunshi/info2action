"""Tests for src/clustering/summary_writer.py

Covers:
- regenerate_and_swap: draft fields written first, then atomic swap to live
- live_version bumps by exactly 1 on success
- LLM failure leaves live fields untouched (V 原则, preserve last good)
- is_visible_in_feed = BF-0501-1 event display policy
- Stale actions updated via bump_cluster_version_and_stale_actions
"""
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402
from clustering import summary_writer as sw  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(sw.remote_db, 'cluster_to_remote', lambda: False)
    db_path = str(tmp_path / 'sw.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _seed_cluster_with_members(conn, *, doc_count=2, prior_live=0,
                               prior_title='OLD TITLE', prior_summary='OLD SUM',
                               unique_source_count=None, ai_category=None):
    """Create a cluster + N items mapped through cluster_items.

    BF-0428-1: visibility now gates on unique_source_count (NOT doc_count).
    Tests pre-BF used doc_count==unique_source_count semantics, so when
    unique_source_count is not given we mirror doc_count for backward compat.
    """
    if unique_source_count is None:
        unique_source_count = doc_count
    conn.execute(
        """INSERT INTO clusters (ai_title, ai_summary, ai_key_points,
                                 live_version, doc_count, unique_source_count,
                                 is_visible_in_feed, first_doc_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (prior_title, prior_summary, '["old kp"]', prior_live, doc_count,
         unique_source_count,
         1 if (unique_source_count >= 2 and prior_title and prior_summary) else 0,
         '2026-04-24T10:00:00'),
    )
    cid = conn.execute("SELECT id FROM clusters").fetchone()['id']
    for i in range(doc_count):
        iid = f'itm{i}'
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  content, author_name, ai_summary,
                                  ai_category)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)""",
            (iid, 'x' if i % 2 == 0 else 'reddit', 'following',
             f'body of doc {i}', f'author{i}', f'summary {i}',
             ai_category),
        )
        conn.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, ?, ?, ?)""",
            (cid, iid, i, 1 if i == 0 else 0),
        )
    conn.commit()
    return cid


_MOCK_LLM_OUTPUT = json.dumps({
    "title": "NEW TITLE Claude Max 发布",
    "summary": "Anthropic 发布 Claude 4.7 Max。"
               "多方报道证实新模型在长上下文和代码生成上显著提升。"
               "价格与 4.6 持平。",
    "breakdown": "**能力变化**\n"
                 "- 上下文扩到 200k\n"
                 "- 代码能力超 4.6\n\n"
                 "**关键信息**\n"
                 "- Anthropic 官方发布信息：确认 Claude 4.7 Max 的能力与定价变化",
    "key_points": ["上下文扩到 200k", "代码能力超 4.6", "定价不变"],
})


class _FakeLLMResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "content": [{"type": "text", "text": _MOCK_LLM_OUTPUT}]
        }).encode("utf-8")


def test_call_llm_chat_retries_transient_500(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                url="https://api.example.com/messages",
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=BytesIO(b"{}"),
            )
        return _FakeLLMResponse()

    monkeypatch.setattr(sw, "urlopen", fake_urlopen)
    monkeypatch.setattr(sw.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(sw.random, "uniform", lambda _start, _end: 0)

    raw = sw._call_llm_chat(
        api_key="k",
        api_base="https://api.example.com",
        model="m",
        system_prompt="s",
        user_content="u",
    )

    assert json.loads(raw)["title"] == "NEW TITLE Claude Max 发布"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_cluster_summary_llm_timeout_and_retry_env(monkeypatch):
    monkeypatch.delenv("INFO2ACTION_CLUSTER_SUMMARY_LLM_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("INFO2ACTION_CLUSTER_SUMMARY_LLM_MAX_RETRIES", raising=False)

    assert sw._cluster_summary_llm_timeout_sec() == 120
    assert sw._cluster_summary_llm_max_retries() == 1

    monkeypatch.setenv("INFO2ACTION_CLUSTER_SUMMARY_LLM_TIMEOUT_SEC", "45")
    monkeypatch.setenv("INFO2ACTION_CLUSTER_SUMMARY_LLM_MAX_RETRIES", "0")

    assert sw._cluster_summary_llm_timeout_sec() == 45
    assert sw._cluster_summary_llm_max_retries() == 0


def test_call_llm_chat_retries_token_plan_429_before_cooldown(monkeypatch):
    calls = []
    sleeps = []
    recorded = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                url="https://api.example.com/messages",
                code=429,
                msg="rate limited",
                hdrs={},
                fp=BytesIO(
                    b'{"error":{"message":"usage limit exceeded; resets at 2026-05-10T15:00:00+08:00 (123)"}}'
                ),
            )
        return _FakeLLMResponse()

    monkeypatch.setattr(sw, "urlopen", fake_urlopen)
    monkeypatch.setattr(sw.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(sw.random, "uniform", lambda _start, _end: 0)
    monkeypatch.setattr(sw.ai_provider_guard, "ensure_provider_available", lambda *_a, **_k: None)
    monkeypatch.setattr(sw.ai_provider_guard, "record_rate_limit", lambda *a, **k: recorded.append((a, k)))

    raw = sw._call_llm_chat(
        api_key="k",
        api_base="https://api.example.com",
        model="m",
        system_prompt="s",
        user_content="u",
    )

    assert json.loads(raw)["title"] == "NEW TITLE Claude Max 发布"
    assert len(calls) == 2
    assert sleeps == [60.0]
    assert recorded == []


def test_regenerate_repairs_missing_fields_output(tmp_db):
    cid = _seed_cluster_with_members(tmp_db)
    malformed = json.dumps({"title": "只有标题"})

    with patch.object(sw, '_call_llm_chat', side_effect=[malformed, _MOCK_LLM_OUTPUT]) as call_mock:
        ok = sw.regenerate_and_swap(
            tmp_db,
            cid,
            api_key='k',
            api_base=None,
            model='m',
        )

    assert ok is True
    assert call_mock.call_count == 2
    row = tmp_db.execute("SELECT ai_title FROM clusters WHERE id = ?", (cid,)).fetchone()
    assert row['ai_title'] == "NEW TITLE Claude Max 发布"


def test_parse_accepts_separate_summary_and_breakdown_without_key_points():
    parsed = sw._parse_llm_json(json.dumps({
        "title": "Claude Code 发布新版本",
        "summary": "Claude Code 发布新版本，增强终端协作能力。",
        "breakdown": "**版本变化**\n- 增强终端协作\n- 修复若干问题\n\n"
                     "**关键信息**\n- 官方 changelog：确认版本变化",
        "warnings": [],
    }, ensure_ascii=False))

    assert parsed is not None
    assert parsed['summary'].startswith('【精华速览】')
    assert '【全文拆解】' in parsed['summary']
    assert '**关键信息**' in parsed['summary']
    assert parsed['key_points'] == []


def test_cluster_summary_prompt_requests_direct_markdown_bold():
    prompt = sw.load_prompt('07_cluster_summary.md')

    assert 'bold_terms' not in prompt
    for section in ['## 角色', '## 背景', '## 目标', '## 任务说明', '## 执行步骤', '## 输入说明', '## 输出说明', '## Few-shot', '## 注意事项']:
        assert section in prompt
    assert '重点信息选择规范' in prompt
    assert '内容价值驱动' in prompt
    assert '按固定词类机械加粗' in prompt
    assert '**Impeccable**' in prompt


def test_parse_preserves_direct_markdown_bold_in_summary_and_breakdown():
    parsed = sw._parse_llm_json(json.dumps({
        "title": "OpenAI 发布 GPT-5",
        "summary": "**OpenAI** 发布 **GPT-5**，企业版价格为 **20 美元**。",
        "breakdown": "**产品发布**\n- **OpenAI** 表示 **GPT-5** 面向企业客户\n- 企业版价格为 **20 美元**",
        "warnings": [],
    }, ensure_ascii=False))

    assert parsed is not None
    assert "**OpenAI** 发布 **GPT-5**" in parsed["summary"]
    assert "- **OpenAI** 表示 **GPT-5** 面向企业客户" in parsed["summary"]
    assert "- 企业版价格为 **20 美元**" in parsed["summary"]
    assert parsed["bold_term_count"] == 0


def test_parse_ignores_legacy_bold_terms_for_cluster_summary():
    parsed = sw._parse_llm_json(json.dumps({
        "title": "OpenAI 发布 GPT-5",
        "summary": "OpenAI 发布 GPT-5，企业版价格为 20 美元。",
        "breakdown": "**产品发布**\n- OpenAI 表示 GPT-5 面向企业客户",
        "bold_terms": {
            "summary": ["OpenAI", "GPT-5", "20 美元"],
            "breakdown": ["OpenAI", "GPT-5", "20 美元"],
        },
        "warnings": [],
    }, ensure_ascii=False))

    assert parsed is not None
    assert "OpenAI 发布 GPT-5，企业版价格为 20 美元。" in parsed["summary"]
    assert "- OpenAI 表示 GPT-5 面向企业客户" in parsed["summary"]
    assert "**OpenAI**" not in parsed["summary"]
    assert parsed["bold_term_count"] == 0


def test_parse_keeps_cluster_summary_when_bold_terms_missing():
    parsed = sw._parse_llm_json(json.dumps({
        "title": "OpenAI 发布 GPT-5",
        "summary": "OpenAI 发布 GPT-5。",
        "breakdown": "**产品发布**\n- OpenAI 表示 GPT-5 面向企业客户",
        "warnings": [],
    }, ensure_ascii=False))

    assert parsed is not None
    assert "OpenAI 发布 GPT-5。" in parsed["summary"]
    assert "**OpenAI**" not in parsed["summary"]


def test_parse_extracts_json_from_surrounding_text():
    parsed = sw._parse_llm_json(
        '可以，结果如下：\n'
        + json.dumps({
            "title": "Claude Code 发布新版本",
            "summary": "【精华速览】\nClaude Code 发布新版本。\n\n【全文拆解】\n**版本变化**\n- 增强终端协作\n- 修复若干问题",
            "warnings": [],
        }, ensure_ascii=False)
        + '\n以上。'
    )

    assert parsed is not None
    assert parsed['title'] == 'Claude Code 发布新版本'


def test_parse_accepts_literal_newlines_inside_summary_string():
    parsed = sw._parse_llm_json(
        '{\n'
        '  "title": "TaxoBench 发布研究基准",\n'
        '  "summary": "【精华速览】\n'
        '**TaxoBench** 发布，用专家分类法评估 Deep Research Agent 的信息发现和组织能力。\n'
        '\n'
        '【全文拆解】\n'
        '**基准定位**\n'
        '- 评估研究代理是否能发现并组织信息\n'
        '- 聚焦 synthesis gap\n'
        '",\n'
        '  "warnings": []\n'
        '}'
    )

    assert parsed is not None
    assert parsed['title'] == 'TaxoBench 发布研究基准'
    assert '【全文拆解】' in parsed['summary']
    assert parsed['warnings'] == []


def test_parse_relaxed_literal_newlines_preserves_invalid_warning():
    parsed = sw._parse_llm_json(
        '{\n'
        '  "title": "telegram-api 仓库信息不足",\n'
        '  "summary": "【精华速览】\n'
        '该仓库只有标题，缺少 README 和功能说明。\n'
        '\n'
        '【全文拆解】\n'
        '**信息缺口**\n'
        '- 无正文描述\n'
        '- 无法判断具体功能\n'
        '",\n'
        '  "warnings": ["不建议展示：单来源信息量过少"]\n'
        '}'
    )

    assert parsed is not None
    assert parsed['warnings'] == ['不建议展示：单来源信息量过少']


def test_parse_invalid_warning_without_title_as_non_event():
    parsed = sw._parse_llm_json(json.dumps({
        "warnings": ["不建议展示：单条低信息提问，无具体事件动作"],
    }, ensure_ascii=False))

    assert parsed is not None
    assert parsed['is_event'] is False
    assert parsed['warnings'] == ['不建议展示：单条低信息提问，无具体事件动作']


class TestDraftLiveSwap:
    def test_happy_path_swaps_live_and_bumps_version(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')
        assert ok is True
        row = tmp_db.execute(
            "SELECT * FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['ai_title'] == 'NEW TITLE Claude Max 发布'
        assert 'Claude 4.7 Max' in row['ai_summary']
        assert json.loads(row['ai_key_points']) == ['上下文扩到 200k', '代码能力超 4.6', '定价不变']
        assert row['live_version'] == 1
        assert row['is_visible_in_feed'] == 1
        # drafts cleared after swap
        assert row['ai_title_draft'] is None
        assert row['ai_summary_draft'] is None

    def test_llm_failure_preserves_live(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db, doc_count=3, prior_live=5,
            prior_title='STABLE', prior_summary='stable summary',
        )
        with patch.object(sw, '_call_llm_chat', side_effect=Exception('API down')):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')
        assert ok is False
        row = tmp_db.execute(
            "SELECT * FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        # live untouched
        assert row['ai_title'] == 'STABLE'
        assert row['ai_summary'] == 'stable summary'
        assert row['live_version'] == 5

    def test_malformed_llm_json_preserves_live(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=2, prior_live=2,
                                          prior_title='KEEP', prior_summary='keep')
        with patch.object(sw, '_call_llm_chat', return_value='not json garbage'):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')
        assert ok is False
        row = tmp_db.execute(
            "SELECT live_version, ai_title FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['live_version'] == 2
        assert row['ai_title'] == 'KEEP'

    def test_doc_count_below_2_does_not_become_visible(self, tmp_db):
        # BF-0501-1: USC=1 is not automatically visible when the category is
        # unknown; high-value categories are covered by the next test.
        cid = _seed_cluster_with_members(tmp_db, doc_count=1,
                                          unique_source_count=1, prior_live=0)
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                    model='MiniMax-M2.7')
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['is_visible_in_feed'] == 0

    def test_high_value_singleton_can_become_visible(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=1,
            unique_source_count=1,
            prior_live=0,
            ai_category='products',
        )
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                    model='MiniMax-M2.7')
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['is_visible_in_feed'] == 1

    def test_high_value_singleton_uses_llm_by_default(self, tmp_db, monkeypatch):
        monkeypatch.delenv("INFO2ACTION_CLUSTER_SUMMARY_SINGLETON_FAST_PATH", raising=False)
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=1,
            unique_source_count=1,
            prior_live=0,
            ai_category='products',
        )
        tmp_db.execute(
            """UPDATE items
                  SET title = 'Raw launch tweet title that should not become the feed title',
                      content = NULL,
                      ai_summary = ?,
                      ai_key_points = ?
                WHERE id = 'itm0'""",
            (
                'Acme 发布一款面向开发者的本地 AI 调试工具，信息足够形成单条精选。',
                json.dumps(['本地日志分析', '复现步骤生成'], ensure_ascii=False),
            ),
        )
        tmp_db.commit()

        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT) as call_mock:
            ok = sw.regenerate_and_swap(
                tmp_db,
                cid,
                api_key='k',
                api_base=None,
                model='MiniMax-M2.7',
                publish_immediately=False,
                run_id=43,
            )

        row = tmp_db.execute(
            """SELECT ai_title_draft, pending_is_visible_in_feed, last_touched_run_id
                 FROM clusters WHERE id=?""",
            (cid,),
        ).fetchone()
        assert ok is True
        assert call_mock.call_count == 1
        assert row['ai_title_draft'] == 'NEW TITLE Claude Max 发布'
        assert row['pending_is_visible_in_feed'] == 1
        assert row['last_touched_run_id'] == 43

    def test_high_value_singleton_reuses_item_summary_without_llm(self, tmp_db, monkeypatch):
        monkeypatch.setenv("INFO2ACTION_CLUSTER_SUMMARY_SINGLETON_FAST_PATH", "1")
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=1,
            unique_source_count=1,
            prior_live=0,
            ai_category='products',
        )
        item_summary = (
            'Acme 发布一款面向开发者的本地 AI 调试工具，重点解决长日志定位和复现步骤整理。'
            '项目给出了安装方式、核心能力和适用场景，信息足够形成单条精选。'
        )
        expected_summary = (
            f'【精华速览】\n{item_summary}\n\n'
            '【全文拆解】\n'
            '**关键信息**\n'
            '- 本地日志分析\n'
            '- 复现步骤生成'
        )
        tmp_db.execute(
            """UPDATE items
                  SET title = 'Acme 发布本地 AI 调试工具',
                      content = NULL,
                      ai_summary = ?,
                      ai_key_points = ?
                WHERE id = 'itm0'""",
            (item_summary, json.dumps(['本地日志分析', '复现步骤生成'], ensure_ascii=False)),
        )
        tmp_db.commit()

        with patch.object(sw, '_call_llm_chat', side_effect=AssertionError('LLM should be skipped')):
            ok = sw.regenerate_and_swap(
                tmp_db,
                cid,
                api_key='k',
                api_base=None,
                model='MiniMax-M2.7',
                publish_immediately=False,
                run_id=43,
            )

        row = tmp_db.execute(
            """SELECT ai_title_draft, ai_summary_draft, ai_key_points_draft,
                      pending_is_visible_in_feed, last_touched_run_id
                 FROM clusters WHERE id=?""",
            (cid,),
        ).fetchone()
        assert ok is True
        assert row['ai_title_draft'] == 'Acme 发布本地 AI 调试工具'
        assert row['ai_summary_draft'] == expected_summary
        assert json.loads(row['ai_key_points_draft']) == ['本地日志分析', '复现步骤生成']
        assert row['pending_is_visible_in_feed'] == 1
        assert row['last_touched_run_id'] == 43

    def test_other_singleton_remains_hidden(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=1,
            unique_source_count=1,
            prior_live=0,
            ai_category='other',
        )
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                    model='MiniMax-M2.7')
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['is_visible_in_feed'] == 0

    def test_non_event_llm_result_hides_cluster(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=3,
            prior_live=4,
            prior_title='BAD OLD TITLE',
            prior_summary='bad old summary',
        )
        non_event = json.dumps({
            "is_event": False,
            "reason": "多条来源是松散主题，不是同一具体事件",
        }, ensure_ascii=False)
        with patch.object(sw, '_call_llm_chat', return_value=non_event):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')

        assert ok is True
        row = tmp_db.execute(
            "SELECT ai_title, ai_summary, live_version, is_visible_in_feed FROM clusters WHERE id=?",
            (cid,),
        ).fetchone()
        assert row['ai_title'] == 'BAD OLD TITLE'
        assert row['ai_summary'] == 'bad old summary'
        assert row['live_version'] == 4
        assert row['is_visible_in_feed'] == 0

    def test_low_information_singleton_skips_llm_and_writes_hidden_draft(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db,
            doc_count=1,
            unique_source_count=1,
            prior_live=0,
            ai_category='coding',
        )
        tmp_db.execute(
            """UPDATE items
                  SET title = 'Gabrielvcg/telegram-api',
                      content = NULL,
                      ai_summary = 'GitHub 仓库 telegram-api，无正文描述，无 README 内容，无法判断具体功能和技术细节。'
                WHERE id = 'itm0'"""
        )
        tmp_db.commit()

        with patch.object(sw, '_call_llm_chat', side_effect=AssertionError('LLM should be skipped')):
            ok = sw.regenerate_and_swap(
                tmp_db,
                cid,
                api_key='k',
                api_base=None,
                model='MiniMax-M2.7',
                publish_immediately=False,
                run_id=42,
            )

        row = tmp_db.execute(
            """SELECT pending_is_visible_in_feed, pending_summary_warnings_json,
                      ai_title_draft, last_touched_run_id
                 FROM clusters WHERE id=?""",
            (cid,),
        ).fetchone()
        assert ok is True
        assert row['pending_is_visible_in_feed'] == 0
        assert '不建议展示' in row['pending_summary_warnings_json']
        assert row['ai_title_draft'] is None
        assert row['last_touched_run_id'] == 42

    def test_legacy_non_event_title_is_treated_as_not_visible(self, tmp_db):
        parsed = sw._parse_llm_json(json.dumps({
            "title": "样本中无跨源聚合事件",
            "summary": "这些内容不是同一事件。",
            "key_points": ["来源互不相关"],
        }, ensure_ascii=False))

        assert parsed is not None
        assert parsed['is_event'] is False

    def test_usc_above_2_with_doc_count_1_becomes_visible(self, tmp_db):
        """BF-0428-1: USC>=2 with doc_count<2 (multi items same author)
        SHOULD now flip is_visible_in_feed=1. Pre-BF this was the silent skip."""
        cid = _seed_cluster_with_members(
            tmp_db, doc_count=1, unique_source_count=2, prior_live=0
        )
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert row['is_visible_in_feed'] == 1

    def test_stale_actions_marked_on_bump(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=2, prior_live=3)
        # Seed an existing cluster-sourced action at version 3
        tmp_db.execute(
            """INSERT INTO actions (id, source_type, source_id, cluster_version,
                                    title, action_type, prompt, is_stale)
               VALUES ('stale-a', 'cluster', ?, 3, 't', 'research', 'p', 0)""",
            (str(cid),),
        )
        tmp_db.commit()
        with patch.object(sw, '_call_llm_chat', return_value=_MOCK_LLM_OUTPUT):
            sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                    model='MiniMax-M2.7')
        row = tmp_db.execute(
            "SELECT is_stale FROM actions WHERE id='stale-a'"
        ).fetchone()
        assert row['is_stale'] == 1


class TestMemberDocCollection:
    def test_collects_newest_docs_before_old_primary_docs(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        rows = [
            (
                'old-primary',
                'Old Primary',
                'older primary source body',
                '2026-04-23T10:00:00Z',
                0,
                1,
            ),
            (
                'fresh-secondary',
                'Fresh Secondary',
                'newer secondary source body',
                'Sun, 26 Apr 2026 08:30:00 +0000',
                9999,
                0,
            ),
        ]
        for iid, title, content, published_at, rank, is_primary in rows:
            tmp_db.execute(
                """INSERT INTO items (id, platform, source, fetched_at, title,
                                      content, author_name, published_at)
                   VALUES (?, 'twitter', 'following', ?, ?, ?, 'a', ?)""",
                (iid, published_at, title, content, published_at),
            )
            tmp_db.execute(
                """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                             is_primary_source)
                   VALUES (?, ?, ?, ?)""",
                (cid, iid, rank, is_primary),
            )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=1)

        assert len(segs) == 1
        assert 'Fresh Secondary' in segs[0]

    def test_metadata_like_content_uses_existing_summary_and_key_points(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, ai_summary,
                                  ai_key_points)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)""",
            (
                'rss-meta',
                'rss',
                'rss',
                'Framework Laptop 13 Pro',
                'Article URL: https://example.com/framework\n'
                'Comments URL: https://news.ycombinator.com/item?id=123\n'
                'Points: 1319\nComments: 659',
                'Trollmann',
                'Framework Laptop 13 Pro 发布，延续模块化设计，搭载 Intel Core Ultra 处理器，'
                '用户可自行更换 CPU、内存和 SSD，但价格相对较高。',
                json.dumps([
                    '模块化设计降低维修门槛',
                    '1319 分和 659 条评论显示社区关注度高',
                ], ensure_ascii=False),
            ),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, 'rss-meta', 0, 1)""",
            (cid,),
        )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=5)

        assert len(segs) == 1
        assert 'Framework Laptop 13 Pro 发布，延续模块化设计' in segs[0]
        assert '模块化设计降低维修门槛' in segs[0]
        assert '1319 分和 659 条评论显示社区关注度高' in segs[0]
        assert 'Article URL:' not in segs[0]

    def test_collect_member_docs_keeps_full_body_after_1500_chars(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sw.remote_db, 'cluster_to_remote', lambda: False)
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        long_body = '开头内容' + ('A' * 1600) + '尾部关键事实：支持 Fork 二次开发'
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?)""",
            (
                'long-doc',
                'twitter',
                'following',
                'GitHub 教程长文',
                long_body,
                'author',
            ),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, 'long-doc', 0, 1)""",
            (cid,),
        )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=5)

        assert len(segs) == 1
        assert '尾部关键事实：支持 Fork 二次开发' in segs[0]

    def test_collect_member_docs_includes_source_url_in_header(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sw.remote_db, 'cluster_to_remote', lambda: False)
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, url)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)""",
            (
                'source-url-doc',
                'github',
                'rss',
                'Claude Code 发布说明',
                'Claude Code 新版本发布，官方 changelog 给出能力变化。',
                'anthropic',
                'https://github.com/anthropics/claude-code/releases/tag/v1.2.3',
            ),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, 'source-url-doc', 0, 1)""",
            (cid,),
        )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=5)

        assert len(segs) == 1
        assert 'url=https://github.com/anthropics/claude-code/releases/tag/v1.2.3' in segs[0]

    def test_collect_member_docs_includes_resolved_urls_from_detail_json(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sw.remote_db, 'cluster_to_remote', lambda: False)
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, url, detail_json)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)""",
            (
                'capcap-doc',
                'twitter',
                'following',
                'Capcap 开源 macOS 截图工具',
                'Capcap 由 @skyrin1008 开发，仓库见短链 https://t.co/kPkrXjdHDe',
                '小弟调调',
                'https://x.com/jaywcjlove/status/2058029318224396723',
                json.dumps({
                    'urls': ['https://github.com/realskyrin/capcap'],
                    'isRetweet': True,
                }, ensure_ascii=False),
            ),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, 'capcap-doc', 0, 1)""",
            (cid,),
        )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=5)

        assert len(segs) == 1
        assert 'url=https://x.com/jaywcjlove/status/2058029318224396723' in segs[0]
        assert 'resolved_urls=https://github.com/realskyrin/capcap' in segs[0]
        assert 'https://t.co/kPkrXjdHDe' not in segs[0].split('\n', 1)[0]

    def test_collect_member_docs_filters_platform_urls_from_resolved_urls(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sw.remote_db, 'cluster_to_remote', lambda: False)
        cid = _seed_cluster_with_members(tmp_db, doc_count=0)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, detail_json)
               VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)""",
            (
                'mixed-urls-doc',
                'twitter',
                'following',
                '工具发布',
                '正文',
                'author',
                json.dumps({
                    'urls': [
                        'https://',
                        'https://?missing-host=1',
                        'https://t.co/abc',
                        {'expanded_url': 'https://x.com/source/status/1'},
                        {'expanded_url': 'https://github.com/example/tool'},
                        {'expanded_url': 'https://examplex.com/not-platform'},
                    ],
                }, ensure_ascii=False),
            ),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster,
                                         is_primary_source)
               VALUES (?, 'mixed-urls-doc', 0, 1)""",
            (cid,),
        )
        tmp_db.commit()

        segs = sw._collect_member_docs(tmp_db, cid, limit=5)
        header = segs[0].split('\n', 1)[0]

        assert 'resolved_urls=https://github.com/example/tool' in header
        assert 'https://examplex.com/not-platform' in header
        assert 'missing-host' not in header
        assert 't.co' not in header
        assert 'x.com/source' not in header


class TestClusterSummaryPrompt:
    def test_prompt_asks_for_dense_event_brief_not_short_abstract(self):
        prompt = sw.load_prompt('07_cluster_summary.md', cluster_docs='DOCS')

        assert prompt is not None
        assert '250-450 字' in prompt
        # V4 — 输出字段拆成摘要 + 分点拆解，调用方再兼容成前端双段展示。
        assert '"summary"' in prompt
        assert '"breakdown"' in prompt
        assert '关键信息' in prompt
        assert '关键真相源' not in prompt
        assert '多来源' in prompt or '来源增量' in prompt
        assert '单来源也可以是有效事件' in prompt
        assert '不要只因为来源数为 1' in prompt
        assert '150-250 字' not in prompt
        # v5b — 全文拆解改为小节标题 + 2-10 条一级 bullet，并支持最多一层嵌套
        assert '小节标题用 `**标题**` 加粗' in prompt
        assert '2-10 条一级 bullet' in prompt
        assert '最多 1 级' in prompt
        assert '必须' in prompt and '加粗' in prompt
        assert '"key_points"' not in prompt


class TestCheckInvalidWarnings:
    """V2.3 §16.2 — keyword detection unit."""

    def test_keyword_hit_subject_mismatch(self):
        m = sw._check_invalid_warnings(['主体不一致：A 是发布、B 是教程'])
        assert m == ['主体不一致']

    def test_keyword_hit_event_mismatch(self):
        assert sw._check_invalid_warnings(['事件不一致']) == ['事件不一致']

    def test_keyword_hit_invalid_event(self):
        assert sw._check_invalid_warnings(['这两条无法构成同一事件']) == ['无法构成同一事件']

    def test_not_cross_source_is_not_invalid_by_itself(self):
        assert sw._check_invalid_warnings(['判定为非跨源事件']) == []

    def test_keyword_hit_not_same_event(self):
        assert sw._check_invalid_warnings(['不属于同一事件，仅是同主题']) == ['不属于同一事件']

    def test_keyword_hit_not_aggregation(self):
        assert sw._check_invalid_warnings(['整体不构成事件聚合']) == ['不构成事件聚合']

    def test_keyword_hit_not_specific_event(self):
        assert sw._check_invalid_warnings(['信息量极低，不构成具体事件']) == ['不构成具体事件', '信息量极低']

    def test_keyword_hit_not_recommended_display(self):
        assert sw._check_invalid_warnings(['不建议展示：内容低信息']) == ['不建议展示', '低信息']

    def test_keyword_hit_not_recommended_for_feed(self):
        assert sw._check_invalid_warnings(['不建议纳入最新事件流']) == ['不建议纳入']

    def test_keyword_hit_off_topic_trading_signal(self):
        matched = sw._check_invalid_warnings(['不属于AI/科技产品动态，只是交易信号喊单'])
        assert '不属于AI/科技' in matched
        assert '交易信号' in matched
        assert '喊单' in matched

    def test_multiple_keywords_in_one_warning(self):
        m = sw._check_invalid_warnings(['主体不一致且事件不一致'])
        assert '主体不一致' in m
        assert '事件不一致' in m

    def test_no_match_returns_empty(self):
        assert sw._check_invalid_warnings(['可选评价']) == []
        assert sw._check_invalid_warnings(['summary too short']) == []

    def test_empty_list(self):
        assert sw._check_invalid_warnings([]) == []

    def test_none(self):
        assert sw._check_invalid_warnings(None) == []

    def test_non_list_returns_empty(self):
        # Defensive: malformed warnings should not blow up
        assert sw._check_invalid_warnings('主体不一致') == []
        assert sw._check_invalid_warnings({'msg': '主体不一致'}) == []
        assert sw._check_invalid_warnings(123) == []

    def test_dedup_keywords_across_warnings(self):
        m = sw._check_invalid_warnings(['主体不一致 a', '主体不一致 b'])
        assert m == ['主体不一致']


class TestWarningsFallbackInRegenerateAndSwap:
    """V2.3 §16.2 — when LLM returns matched keywords, force is_visible_in_feed=0."""

    def _llm_output(self, warnings=None):
        out = {
            "title": "NEW TITLE Claude Max 发布",
            "summary": "Anthropic 发布 Claude 4.7 Max。多方报道证实新模型在长上下文和"
                       "代码生成上显著提升。价格与 4.6 持平。",
            "breakdown": "**能力变化**\n"
                         "- 上下文扩到 200k\n"
                         "- 代码能力超 4.6\n\n"
                         "**来源差异**\n"
                         "- 官博给出价格细则\n"
                         "- 社区主要在测代码生成",
            "key_points": ["上下文扩到 200k", "代码能力超 4.6", "定价不变"],
        }
        if warnings is not None:
            out['warnings'] = warnings
        return json.dumps(out, ensure_ascii=False)

    def test_matched_keyword_forces_invisible(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=['主体不一致：A 是发布、B 是教程'])
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            with patch.object(sw, '_log_event') as mock_log:
                ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                             model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed, ai_title, ai_summary FROM clusters WHERE id=?",
            (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 0
        # ai_title / ai_summary still preserved (audit trail per V2.3)
        assert row['ai_title'] == 'NEW TITLE Claude Max 发布'
        assert 'Claude 4.7 Max' in row['ai_summary']
        # cluster_invalid_by_summary log fired
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'cluster_invalid_by_summary' in events
        invalid_call = next(c for c in mock_log.call_args_list
                            if c.args[0] == 'cluster_invalid_by_summary')
        assert invalid_call.kwargs['matched_keywords'] == ['主体不一致']

    def test_empty_warnings_keeps_visible(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=[])
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        # doc_count=3 → base_visible = 1, no keywords → 1
        assert row['is_visible_in_feed'] == 1

    def test_unrelated_warnings_keeps_visible(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=['summary may exceed length budget'])
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 1

    def test_missing_warnings_field_keeps_visible(self, tmp_db):
        """Backward compat: old prompt output without warnings still works."""
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=None)  # no key
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 1

    def test_trading_signal_title_hides_even_without_warning(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db, doc_count=1, unique_source_count=1, ai_category='investment',
            prior_live=0,
        )
        raw = json.dumps({
            "title": "$BULLISH 代币获 OpenClaw AI 买入信号",
            "summary": "OpenClaw AI 给出 meme 币买入信号，目标涨幅 342%。",
            "breakdown": "**交易信号**\n- OpenClaw AI 给出买入信号\n- 目标涨幅 342%",
            "warnings": [],
        }, ensure_ascii=False)
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 0

    def test_technical_summary_can_mention_meme_coin_without_hiding(self, tmp_db):
        cid = _seed_cluster_with_members(
            tmp_db, doc_count=1, unique_source_count=1, ai_category='tech',
            prior_live=0,
        )
        raw = json.dumps({
            "title": "MCP+EIP-7702 让 AI 自主执行链上操作",
            "summary": "EIP-7702 与 MCP 结合后，AI Agent 可在授权后执行链上操作。"
                       "应用场景包括 DeFi、swap 和 meme 币追踪，但重点是账户抽象和工具调用。",
            "breakdown": "**技术变化**\n- EIP-7702 提供账户抽象能力\n- MCP 承接工具调用流程",
            "warnings": [],
        }, ensure_ascii=False)
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 1


class TestDualSectionSummaryAndWarningsPersistence:
    """V2.3 §7 / §13.4 / §16.2 / §16.3 — Eng-D Stage 4 contract.

    last_summary_warnings_json is written every successful regen (overwrite),
    and current cluster prompt output uses separate summary/breakdown fields.
    Storage still persists the frontend-compatible dual-section summary.
    """

    def _llm_output(self, *, summary=None, breakdown=None, warnings=None):
        out = {
            "title": "NEW TITLE Claude Max 发布",
            "summary": summary if summary is not None else (
                'Anthropic 发布 Claude 4.7 Max，上下文扩到 200k，'
                '定价与 4.6 持平。'
            ),
            "breakdown": breakdown if breakdown is not None else (
                '**能力变化**\n'
                '- 长上下文 200k，代码任务跑分超 4.6\n'
                '- 定价与 4.6 持平\n\n'
                '**来源差异**\n'
                '- 官博给出价格细则\n'
                '- 社区主要在测代码生成'
            ),
            "key_points": ["上下文扩到 200k", "代码能力超 4.6", "定价不变"],
        }
        if warnings is not None:
            out['warnings'] = warnings
        return json.dumps(out, ensure_ascii=False)

    def test_dual_section_summary_persists_warnings_empty(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=[])
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed, ai_summary, last_summary_warnings_json "
            "FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 1
        assert '【精华速览】' in row['ai_summary']
        assert '【全文拆解】' in row['ai_summary']
        # V2.3 §13.4 — last_summary_warnings_json overwritten to '[]' even
        # when warnings list is empty (so frontend can distinguish from NULL).
        assert row['last_summary_warnings_json'] == '[]'

    def test_long_breakdown_does_not_trigger_summary_length_warning(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        summary = 'Anthropic 发布 Claude 4.7 Max，' + ('能力边界和价格信息已经确认。' * 20)
        breakdown = '**能力变化**\n' + '\n'.join(
            f'- 详细拆解第 {idx} 条，保留多来源补充事实和关键上下文。'
            for idx in range(40)
        )
        raw = self._llm_output(summary=summary, breakdown=breakdown, warnings=[])

        with patch.object(sw, '_call_llm_chat', return_value=raw):
            with patch.object(sw, '_log_event') as mock_log:
                ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k',
                                             api_base=None, model='m')

        assert ok is True
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'cluster_summary_length_warning' not in events

    def test_warnings_keyword_persisted_to_db_and_invisible(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        raw = self._llm_output(warnings=['主体不一致：两条来源讲不同公司'])
        with patch.object(sw, '_call_llm_chat', return_value=raw):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is True
        row = tmp_db.execute(
            "SELECT is_visible_in_feed, last_summary_warnings_json "
            "FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['is_visible_in_feed'] == 0
        # full warning text is persisted as JSON list
        persisted = json.loads(row['last_summary_warnings_json'])
        assert persisted == ['主体不一致：两条来源讲不同公司']

    def test_flat_summary_triggers_schema_repair_before_write(self, tmp_db):
        """Current schema requires breakdown; flat output should be repaired
        before it is written to the frontend-compatible summary field."""
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        flat_summary = (
            'Anthropic 发布 Claude 4.7 Max。多方报道集中在能力边界和发布时间。'
            '上下文扩到 200k，定价与 4.6 持平，代码生成跑分超 4.6。'
            '官博与社区在版本兼容性上略有冲突，需要后续观察。'
        )
        raw = self._llm_output(summary=flat_summary, warnings=[])
        repaired = self._llm_output(summary=flat_summary, warnings=[])
        raw_obj = json.loads(raw)
        raw_obj.pop('breakdown', None)
        raw = json.dumps(raw_obj, ensure_ascii=False)
        with patch.object(sw, '_call_llm_chat', side_effect=[raw, repaired]) as call_mock:
            with patch.object(sw, '_log_event') as mock_log:
                ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k',
                                             api_base=None, model='m')
        assert ok is True
        assert call_mock.call_count == 2
        row = tmp_db.execute(
            "SELECT ai_summary FROM clusters WHERE id=?", (cid,),
        ).fetchone()
        assert row['ai_summary'].startswith('【精华速览】')
        assert flat_summary in row['ai_summary']
        assert '【全文拆解】' in row['ai_summary']
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'cluster_summary_schema_repair' in events

    def test_llm_failure_leaves_warnings_field_untouched(self, tmp_db):
        """Pre/post DB read: LLM failure must not touch
        last_summary_warnings_json (R5.3 — preserve last good)."""
        cid = _seed_cluster_with_members(tmp_db, doc_count=3, prior_live=0)
        # Seed a previous warnings JSON to confirm it's preserved on failure
        tmp_db.execute(
            "UPDATE clusters SET last_summary_warnings_json = ? WHERE id=?",
            (json.dumps(['previous warning'], ensure_ascii=False), cid),
        )
        tmp_db.commit()
        before = tmp_db.execute(
            "SELECT last_summary_warnings_json FROM clusters WHERE id=?",
            (cid,),
        ).fetchone()['last_summary_warnings_json']

        with patch.object(sw, '_call_llm_chat', side_effect=Exception('boom')):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='m')
        assert ok is False
        after = tmp_db.execute(
            "SELECT last_summary_warnings_json FROM clusters WHERE id=?",
            (cid,),
        ).fetchone()['last_summary_warnings_json']
        # Untouched on failure
        assert after == before


class TestNoDuplicateClusterDocsFeed:
    """V2.3 §13.2 fix: cluster_docs must NOT appear in system prompt anymore.
    System prompt = rules only; user_content carries the docs as the user
    message (single feed, not duplicated)."""

    def test_system_prompt_does_not_inline_member_docs(self, tmp_db):
        cid = _seed_cluster_with_members(tmp_db, doc_count=2, prior_live=0)
        # Member content is "body of doc 0" / "body of doc 1" per fixture.
        captured = {}

        def fake_call(*, api_key, api_base, model,
                      system_prompt, user_content, max_tokens=2048, timeout=60,
                      source='cluster_summary', max_retries=None):
            captured['system_prompt'] = system_prompt
            captured['user_content'] = user_content
            captured['timeout'] = timeout
            captured['source'] = source
            captured['max_retries'] = max_retries
            return _MOCK_LLM_OUTPUT

        with patch.object(sw, '_call_llm_chat', side_effect=fake_call):
            ok = sw.regenerate_and_swap(tmp_db, cid, api_key='k', api_base=None,
                                         model='MiniMax-M2.7')
        assert ok is True
        sys_p = captured['system_prompt']
        usr_c = captured['user_content']

        # Member docs MUST be in user_content...
        assert 'body of doc 0' in usr_c
        assert 'body of doc 1' in usr_c
        assert captured['timeout'] == 120
        assert captured['source'] == 'cluster_summary'
        assert captured['max_retries'] == 1

        # ...but MUST NOT also be inlined into the system prompt.
        assert 'body of doc 0' not in sys_p
        assert 'body of doc 1' not in sys_p

        # Sanity: system prompt still contains rule text.
        # BF-0428-7: 删除字数硬约束,改"自适应"指引;只校验 prompt 结构关键词
        assert '自适应' in sys_p
        assert '严格 JSON' in sys_p

    def test_prompt_template_has_no_cluster_docs_placeholder(self):
        """The prompt template itself should no longer reference {cluster_docs}."""
        prompt = sw.load_prompt('07_cluster_summary.md')
        assert prompt is not None
        assert '{cluster_docs}' not in prompt
