import os
import sys
import urllib.error
from io import BytesIO

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_query_pending_items_orders_window_by_published_at_desc(monkeypatch, tmp_path):
    import db as db_mod
    import enrich_items

    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    for item_id, published_at in (
        ("older", "2026-05-10T01:00:00+00:00"),
        ("newer", "2026-05-10T03:00:00+00:00"),
        ("outside", "2026-05-09T23:00:00+00:00"),
    ):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, published_at, title, content)
               VALUES (?, 'twitter', 'unit', '2026-05-10T04:00:00+00:00', ?, ?, ?)""",
            (item_id, published_at, item_id, item_id),
        )
    conn.commit()

    rows = enrich_items.query_pending_items(
        conn,
        run_id=None,
        window_start="2026-05-10T00:00:00+00:00",
        window_end="2026-05-10T04:00:00+00:00",
        require_published_at=True,
    )

    assert [row["id"] for row in rows] == ["newer", "older"]
    conn.close()


def test_query_pending_items_inserted_scope_ignores_retouched_old_items(monkeypatch, tmp_path):
    import db as db_mod
    import enrich_items

    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    conn.execute(
        "INSERT INTO fetch_runs (id, started_at, status) VALUES (77, datetime('now'), 'running')"
    )
    for item_id in ("old", "new"):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetch_run_id, fetched_at, title, content)
               VALUES (?, 'twitter', 'unit', 77, '2026-05-13T10:00:00+00:00', ?, ?)""",
            (item_id, item_id, item_id),
        )
    conn.execute(
        """INSERT INTO fetch_run_items (run_id, item_id, platform, source, was_inserted)
           VALUES (77, 'old', 'twitter', 'unit', 0),
                  (77, 'new', 'twitter', 'unit', 1)"""
    )
    conn.commit()

    rows = enrich_items.query_pending_items(
        conn,
        run_id=77,
        run_items_scope=enrich_items.RUN_ITEMS_SCOPE_INSERTED,
    )

    assert [row["id"] for row in rows] == ["new"]
    conn.close()


def test_remote_pending_enrichment_query_retries_transient_db_error(monkeypatch, capsys):
    import enrich_items

    monkeypatch.setenv("INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS", "3")
    monkeypatch.setattr(enrich_items.time, "sleep", lambda _delay: None)
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise enrich_items.remote_db.RemoteDBError(
                "EDBHANDLEREXITED connection to database closed"
            )
        return [{"id": "item-1"}]

    monkeypatch.setattr(
        enrich_items.remote_db,
        "query_pending_enrichment_items_remote",
        fake_query,
    )

    rows = enrich_items.query_pending_enrichment_items_remote_with_retry(run_id=1485)

    assert rows == [{"id": "item-1"}]
    assert len(calls) == 2
    assert "remote_db_transient_retry operation=query_pending_enrichment_items_remote attempt=1/3" in capsys.readouterr().out


def test_remote_pending_enrichment_query_does_not_retry_non_transient_db_error(monkeypatch):
    import enrich_items

    monkeypatch.setenv("INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS", "3")
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        raise enrich_items.remote_db.RemoteDBError("invalid input syntax for type json")

    monkeypatch.setattr(
        enrich_items.remote_db,
        "query_pending_enrichment_items_remote",
        fake_query,
    )

    with pytest.raises(enrich_items.remote_db.RemoteDBError, match="invalid input syntax"):
        enrich_items.query_pending_enrichment_items_remote_with_retry(run_id=1485)

    assert len(calls) == 1


def test_remote_enrichment_write_retries_transient_db_error(monkeypatch, capsys):
    import enrich_items

    monkeypatch.setenv("INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS", "3")
    monkeypatch.setattr(enrich_items.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(enrich_items.remote_db, "enrich_to_remote", lambda: True)
    writes = []

    def fake_write(conn, item_id, parsed):
        writes.append((conn, item_id, parsed["summary"]))
        if len(writes) == 1:
            raise enrich_items.remote_db.RemoteDBError("pool checkout timeout")

    monkeypatch.setattr(enrich_items.remote_db, "write_enrichment_remote", fake_write)
    monkeypatch.setattr(
        enrich_items.remote_db,
        "record_ai_failure_remote",
        lambda *_args, **_kwargs: pytest.fail("transient write retry should not record AI failure"),
    )

    enrich_items.write_enrichment_current("item-1", {"summary": "ok"})

    assert writes == [(None, "item-1", "ok"), (None, "item-1", "ok")]
    assert "remote_db_transient_retry operation=write_enrichment_remote attempt=1/3" in capsys.readouterr().out


def test_enrich_one_item_writes_highlight_verdict_after_enrichment(monkeypatch):
    import enrich_items

    raw_responses = [
        """
        {"summary":"摘要","key_points":[{"title":"主题","points":["要点"]}],
         "category":"tools","content_type":"article",
         "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
         "reason":"理由","keywords":["Claude Code"]}
        """,
        """
        {"reason":"①实质收获：给出可照做方法","verdict":"featured",
         "value_path":"substantive","uncertainty":"none","confidence":0.82,
         "scores":{"importance":2,"novelty":2,"credibility":3,"substance":3,"actionability":3},
         "ai_relevant":"yes","spam":1}
        """,
    ]
    calls = []

    def fake_call_minimax(*_args, **_kwargs):
        return raw_responses.pop(0)

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call_minimax)
    monkeypatch.setattr(
        enrich_items,
        "write_enrichment_current",
        lambda item_id, parsed: calls.append(("enrichment", item_id, parsed["summary"])),
    )
    monkeypatch.setattr(
        enrich_items,
        "write_highlight_verdict_current",
        lambda item_id, result: calls.append(
            ("highlight", item_id, result["cluster_verdict"], result["highlight_include_in_highlights"])
        ),
    )

    parsed = enrich_items.enrich_one_item(
        {
            "id": "item-1",
            "platform": "twitter",
            "source": "unit",
            "title": "Claude Code 实践",
            "content": "这里有足够长的正文内容，说明如何使用 Claude Code。",
            "url": "https://example.test/item-1",
        },
        api_key="key",
        api_base="https://example.test",
        model="model",
        system_prompt="system",
        valid_category_ids=["tools"],
        max_tokens=4096,
        dry_run=False,
    )

    assert parsed["summary"] == "摘要"
    assert calls == [
        ("enrichment", "item-1", "摘要"),
        ("highlight", "item-1", "featured", True),
    ]


def test_parse_valid_enrichment_json():
    import enrich_items

    raw = """
    {"summary":"摘要","key_points":[{"title":"主题","points":["要点"]}],
     "category":"tools","content_type":"article",
     "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
     "reason":"理由","keywords":["Claude Code"]}
    """

    parsed = enrich_items.parse_enrichment_response(raw, valid_category_ids=["tools"])

    assert parsed["summary"] == "摘要"
    assert parsed["category"] == "tools"
    assert parsed["quality_score"] is not None
    assert parsed["keywords"] == ["Claude Code"]


def test_parse_enrichment_response_preserves_direct_markdown_bold():
    import enrich_items

    raw = """
    {"summary":"**OpenAI** 发布 **GPT-5**，企业版价格为 **20 美元**。",
     "key_points":[{"title":"产品发布","points":["**OpenAI** 表示 **GPT-5** 面向企业客户"]}],
     "category":"tools","content_type":"article",
     "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
     "reason":"理由","keywords":["GPT-5"]}
    """

    parsed = enrich_items.parse_enrichment_response(raw, valid_category_ids=["tools"])

    assert parsed["summary"] == "**OpenAI** 发布 **GPT-5**，企业版价格为 **20 美元**。"
    assert parsed["key_points"][0]["points"] == ["**OpenAI** 表示 **GPT-5** 面向企业客户"]
    assert parsed["bold_term_count"] == 0


def test_parse_enrichment_response_ignores_legacy_bold_terms():
    import enrich_items

    raw = """
    {"summary":"OpenAI 发布 GPT-5。",
     "key_points":[{"title":"产品发布","points":["OpenAI 表示 GPT-5 面向企业客户"]}],
     "bold_terms":{"summary":["OpenAI","GPT-5"],"key_points":["OpenAI","GPT-5"]},
     "category":"tools","content_type":"article",
     "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
     "reason":"理由","keywords":["GPT-5"]}
    """

    parsed = enrich_items.parse_enrichment_response(raw, valid_category_ids=["tools"])

    assert parsed["summary"] == "OpenAI 发布 GPT-5。"
    assert parsed["key_points"][0]["points"] == ["OpenAI 表示 GPT-5 面向企业客户"]
    assert parsed["bold_term_count"] == 0


def test_item_enrichment_prompt_uses_summary_and_breakdown_style_constraints():
    import enrich_items

    prompt = enrich_items.build_system_prompt([
        {
            "id": "coding",
            "name": "Coding",
            "description": "AI coding tools",
            "subcategories": [{"id": "coding_tool", "name": "Coding tool"}],
        }
    ])

    assert '"summary"' in prompt
    assert '"key_points"' in prompt
    assert 'bold_terms' not in prompt
    for section in ['## 角色', '## 背景', '## 目标', '## 任务说明', '## 执行步骤', '## 输入说明', '## 输出说明', '## Few-shot', '## 注意事项']:
        assert section in prompt
    assert '重点信息选择规范' in prompt
    assert '内容价值驱动' in prompt
    assert '按固定词类机械加粗' in prompt
    assert '**Impeccable**' in prompt
    assert '摘要字段' in prompt
    assert '分点拆解字段' in prompt
    assert '结构化分组完整拆解原文信息' in prompt
    assert '没有展开的结构化细节' not in prompt
    assert '关键信息' in prompt
    assert '关键真相源' not in prompt
    assert 'v16.0 added' not in prompt
    assert '非 AI 内容分类指引' not in prompt
    assert '开源教程优先 `tutorials`' in prompt
    assert '"breakdown"' not in prompt
    assert '"dimensions"' not in prompt
    assert '评分维度' not in prompt
    assert 'novelty' not in prompt
    assert prompt.index('## 角色') < prompt.index('## 分类体系')
    assert prompt.index('## 输出说明') < prompt.index('## 分类体系')


def test_parse_enrichment_response_defaults_dimensions_when_prompt_omits_scores():
    import enrich_items

    raw = """
    {"summary":"摘要","key_points":[{"title":"主题","points":["要点"]}],
     "categories":["tools"],"subcategories":[],"content_type":"post",
     "visible":true,"reason":"理由","keywords":["Claude Code"]}
    """

    parsed = enrich_items.parse_enrichment_response(raw, valid_category_ids=["tools"])

    assert parsed["summary"] == "摘要"
    assert parsed["category"] == "tools"
    assert parsed["dimensions"]
    assert parsed["quality_score"] is not None


def test_parse_partial_json_keeps_summary_when_category_invalid():
    import enrich_items

    raw = '{"summary":"摘要","key_points":[],"category":"bad","content_type":"post","dimensions":{}}'

    parsed = enrich_items.parse_enrichment_response(raw, valid_category_ids=["tools"])

    assert parsed["summary"] == "摘要"
    assert parsed["category"] is None


def test_enrich_one_item_retries_500_with_compact_content(monkeypatch):
    import enrich_items

    calls = []

    def fake_call_minimax(_api_key, _api_base, _model, _system_prompt, user_content, **kwargs):
        calls.append((len(user_content), kwargs.get("max_tokens")))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                url="https://api.example.com/messages",
                code=500,
                msg="server error",
                hdrs={},
                fp=BytesIO(b"{}"),
            )
        return """
        {"summary":"摘要","key_points":[{"title":"主题","points":["要点"]}],
         "category":"tools","content_type":"article",
         "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
         "reason":"理由","keywords":["Claude Code"]}
        """

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call_minimax)

    parsed = enrich_items.enrich_one_item(
        {
            "id": "item-1",
            "platform": "rss",
            "source": "unit",
            "title": "标题",
            "content": "正文" * 3000,
        },
        "key",
        "https://api.example.com",
        "model",
        "system",
        ["tools"],
        100000,
        True,
    )

    assert parsed["summary"] == "摘要"
    assert len(calls) == 2
    assert calls[1][0] < calls[0][0]
    assert calls[1][1] == 4096


def test_enrich_one_item_retries_500_with_minimal_content(monkeypatch):
    import enrich_items

    calls = []

    def fake_call_minimax(_api_key, _api_base, _model, _system_prompt, user_content, **kwargs):
        calls.append((user_content, kwargs.get("max_tokens")))
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                url="https://api.example.com/messages",
                code=500,
                msg="server error",
                hdrs={},
                fp=BytesIO(b"{}"),
            )
        return """
        {"summary":"摘要","key_points":[{"title":"主题","points":["要点"]}],
         "category":"tools","content_type":"article",
         "dimensions":{"novelty":2,"credibility":3,"spam_score":1,"depth":2,"actionability":2},
         "reason":"理由","keywords":["Claude Code"]}
        """

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call_minimax)

    parsed = enrich_items.enrich_one_item(
        {
            "id": "item-2",
            "platform": "rss",
            "source": "unit",
            "title": "标题",
            "content": "正文" * 3000,
        },
        "key",
        "https://api.example.com",
        "model",
        "system",
        ["tools"],
        100000,
        True,
    )

    assert parsed["summary"] == "摘要"
    assert len(calls) == 3
    assert "正文摘要输入" in calls[2][0]
    assert calls[2][1] == 2048


def test_enrich_one_item_uses_hidden_fallback_after_repeated_5xx(monkeypatch):
    import enrich_items

    def fake_call_minimax(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="https://api.example.com/messages",
            code=500,
            msg="server error",
            hdrs={},
            fp=BytesIO(b"{}"),
        )

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call_minimax)

    parsed = enrich_items.enrich_one_item(
        {
            "id": "item-3",
            "platform": "reddit",
            "source": "unit",
            "title": "Qwen vs Gemma benchmark",
            "content": "benchmark body",
        },
        "key",
        "https://api.example.com",
        "model",
        "system",
        ["models", "other"],
        100000,
        True,
    )

    assert parsed["category"] == "other"
    assert parsed["visible"] is False
    assert parsed["reason"] == "provider_5xx_conservative_fallback"
