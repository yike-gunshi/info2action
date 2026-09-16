from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_item_evidence_records_inputs_without_body_text():
    import summary_evidence

    item = {
        "id": "item-1",
        "content": "PRIVATE BODY SENTINEL",
        "asr_text": "PRIVATE ASR SENTINEL",
        "ai_summary": "",
        "detail_json": json.dumps(
            {
                "readme": "PRIVATE README SENTINEL",
                "quotedTweet": {
                    "id": "quoted-1",
                    "text": "PRIVATE QUOTE SENTINEL",
                },
                "referenced_urls": [
                    {
                        "url": "https://example.com/one",
                        "full_text": "x" * 101,
                    },
                    {
                        "url": "https://example.com/two",
                        "full_text": "y" * 101,
                    },
                ],
            }
        ),
    }

    evidence = summary_evidence.build_item_summary_evidence(item)
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert evidence == {
        "item_id": "item-1",
        "has_body": True,
        "has_asr": True,
        "has_readme": True,
        "has_quote": True,
        "referenced_url_count": 2,
        "referenced_full_text_count": 2,
        "selected_external_text_count": 1,
        "fallback": "none",
    }
    assert "PRIVATE" not in serialized
    assert "https://example.com" not in serialized


def test_item_evidence_marks_empty_and_ai_summary_fallbacks():
    import summary_evidence

    assert summary_evidence.build_item_summary_evidence(
        {"id": "empty", "content": "", "asr_text": "", "ai_summary": ""}
    )["fallback"] == "empty"
    assert summary_evidence.build_item_summary_evidence(
        {
            "id": "summary",
            "content": "https://example.com only metadata",
            "asr_text": "",
            "ai_summary": "A useful summary",
        },
        body_is_usable=False,
    )["fallback"] == "ai_summary"


def test_enrich_builds_content_and_privacy_safe_evidence_together():
    import enrich_items

    content, evidence = enrich_items.build_item_content_with_evidence(
        {
            "id": "item-input",
            "platform": "reddit",
            "title": "Demo",
            "content": "SENSITIVE BODY",
            "asr_text": "",
            "detail_json": json.dumps(
                {
                    "referenced_urls": [
                        {
                            "url": "https://example.com/source",
                            "full_text": "z" * 101,
                        }
                    ]
                }
            ),
        }
    )

    assert "SENSITIVE BODY" in content
    assert evidence["item_id"] == "item-input"
    assert evidence["selected_external_text_count"] == 1
    assert "SENSITIVE" not in json.dumps(evidence)
    assert "example.com" not in json.dumps(evidence)


def test_cluster_evidence_caps_members_and_external_links():
    import summary_evidence

    rows = []
    for idx in range(25):
        rows.append(
            {
                "id": f"item-{idx}",
                "content": "body",
                "ai_summary": "",
                "detail_json": {
                    "referenced_urls": [
                        {"url": f"https://example.com/{idx}/{link_idx}"}
                        for link_idx in range(8)
                    ]
                },
            }
        )

    evidence = summary_evidence.build_cluster_summary_evidence(
        rows,
        max_members=20,
    )

    assert evidence["member_count_total"] == 25
    assert evidence["member_count_selected"] == 20
    assert len(evidence["members"]) == 20
    assert all(member["external_url_count_selected"] == 5 for member in evidence["members"])
    assert all("detail_json" not in member for member in evidence["members"])


def test_cluster_evidence_marks_singleton_fast_path():
    import summary_evidence

    evidence = summary_evidence.build_cluster_summary_evidence(
        [
            {
                "id": "only",
                "content": "",
                "ai_summary": "summary",
                "detail_json": {},
            }
        ],
        singleton_fast_path=True,
    )

    assert evidence["singleton"] is True
    assert evidence["singleton_fast_path"] is True
    assert evidence["members"][0]["fallback"] == "ai_summary"


def test_real_cluster_prompt_builder_logs_privacy_safe_capped_evidence(monkeypatch):
    from clustering import summary_writer

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        summary_writer,
        "_log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    rows = [
        {
            "id": f"item-{idx}",
            "title": "SENSITIVE TITLE",
            "content": "SENSITIVE BODY",
            "author_name": "author",
            "platform": "reddit",
            "url": f"https://reddit.com/sensitive/{idx}",
            "detail_json": json.dumps(
                {
                    "referenced_urls": [
                        {"url": f"https://external.example/{idx}/{link_idx}"}
                        for link_idx in range(8)
                    ]
                }
            ),
            "ai_summary": "",
            "ai_key_points": None,
            "published_at": f"2026-07-{idx + 1:02d}T00:00:00Z",
            "fetched_at": f"2026-07-{idx + 1:02d}T00:00:00Z",
            "is_primary_source": 0,
            "rank_in_cluster": idx,
        }
        for idx in range(21)
    ]

    docs = summary_writer._collect_member_docs_from_rows(rows, limit=30)

    assert len(docs) == 20
    event, fields = events[-1]
    assert event == "cluster_summary_input_evidence"
    assert fields["evidence"]["member_count_selected"] == 20
    assert all(
        member["external_url_count_selected"] == 5
        for member in fields["evidence"]["members"]
    )
    serialized = json.dumps(fields["evidence"], ensure_ascii=False)
    assert "SENSITIVE BODY" not in serialized
    assert "reddit.com" not in serialized
    assert "external.example" not in serialized


def test_real_cluster_prompt_evidence_respects_limit_below_twenty(monkeypatch):
    from clustering import summary_writer

    events = []
    monkeypatch.setattr(
        summary_writer,
        "_log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    rows = [
        {
            "id": f"item-{idx}",
            "title": "Title",
            "content": "Body",
            "author_name": "author",
            "platform": "reddit",
            "url": "",
            "detail_json": None,
            "ai_summary": "",
            "ai_key_points": None,
            "published_at": f"2026-07-{idx + 1:02d}T00:00:00Z",
            "fetched_at": f"2026-07-{idx + 1:02d}T00:00:00Z",
            "is_primary_source": 0,
            "rank_in_cluster": idx,
        }
        for idx in range(6)
    ]

    docs = summary_writer._collect_member_docs_from_rows(rows, limit=3)

    assert len(docs) == 3
    assert events[-1][1]["evidence"]["member_count_selected"] == 3


def test_cluster_prompt_includes_asr_even_when_post_body_exists(monkeypatch):
    from clustering import summary_writer

    monkeypatch.setattr(summary_writer, "_log_event", lambda *_args, **_kwargs: None)
    docs = summary_writer._collect_member_docs_from_rows(
        [
            {
                "id": "reddit-video",
                "title": "Demo",
                "content": "作者写下的帖子正文",
                "asr_text": "视频里明确提到三个月完成了可玩的赛车原型",
                "author_name": "author",
                "platform": "reddit",
                "url": "https://reddit.com/r/demo/comments/video",
                "detail_json": None,
                "ai_summary": "旧摘要",
                "ai_key_points": None,
                "published_at": "2026-07-24T00:00:00Z",
                "fetched_at": "2026-07-24T00:00:00Z",
                "is_primary_source": 1,
                "rank_in_cluster": 1,
            }
        ],
        limit=20,
    )

    assert "作者写下的帖子正文" in docs[0]
    assert "视频语音转写:" in docs[0]
    assert "三个月完成了可玩的赛车原型" in docs[0]


def test_local_and_remote_cluster_member_queries_select_asr_text(monkeypatch):
    import remote_db
    from clustering import summary_writer

    class Cursor:
        def fetchall(self):
            return []

    class Conn:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, _params):
            self.sql = " ".join(sql.split())
            return Cursor()

    local = Conn()
    monkeypatch.setattr(summary_writer.remote_db, "cluster_to_remote", lambda: False)
    summary_writer._collect_member_rows(local, 1)
    assert "i.asr_text" in local.sql

    remote = Conn()
    remote_db.collect_cluster_member_rows_remote(remote, 1)
    assert "i.asr_text" in remote.sql
