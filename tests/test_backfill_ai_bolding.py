import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_select_local_item_candidates_filters_recent_visible_unbolded(monkeypatch, tmp_path):
    import db as db_mod
    from scripts import backfill_ai_bolding

    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    rows = [
        ("needs", "twitter", 1, "2026-06-03T01:00:00+00:00", "OpenAI 发布 GPT-5", '[]'),
        ("bolded", "twitter", 1, "2026-06-03T02:00:00+00:00", "**OpenAI** 发布 **GPT-5**", '[]'),
        ("hidden", "twitter", 0, "2026-06-03T03:00:00+00:00", "OpenAI 发布 GPT-5", '[]'),
        ("old", "twitter", 1, "2026-06-01T01:00:00+00:00", "OpenAI 发布 GPT-5", '[]'),
    ]
    for row in rows:
        conn.execute(
            """INSERT INTO items
               (id, platform, source, visible, fetched_at, title, content, ai_summary, ai_key_points)
               VALUES (?, ?, 'unit', ?, ?, 't', 'c', ?, ?)""",
            row,
        )
    conn.commit()

    candidates = backfill_ai_bolding.select_local_item_candidates(
        conn,
        since_iso="2026-06-02T00:00:00+00:00",
    )

    assert [c["id"] for c in candidates] == ["needs"]
    conn.close()


def test_select_local_cluster_candidates_filters_heading_only(monkeypatch, tmp_path):
    import db as db_mod
    from scripts import backfill_ai_bolding

    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db_mod.get_conn()
    summaries = {
        "heading_only": "【精华速览】\nOpenAI 发布 GPT-5。\n\n【全文拆解】\n**产品发布**\n- OpenAI 发布 GPT-5",
        "body_bold": "【精华速览】\n**OpenAI** 发布 GPT-5。\n\n【全文拆解】\n**产品发布**\n- OpenAI 发布 **GPT-5**",
        "no_bold": "【精华速览】\nOpenAI 发布 GPT-5。\n\n【全文拆解】\n- OpenAI 发布 GPT-5",
    }
    for title, summary in summaries.items():
        conn.execute(
            """INSERT INTO clusters
               (ai_title, ai_summary, doc_count, unique_source_count, first_doc_at,
                last_updated_at, published_at, is_visible_in_feed, archived, merged_into)
               VALUES (?, ?, 2, 2, '2026-06-03T01:00:00+00:00',
                       '2026-06-03T02:00:00+00:00', '2026-06-03T02:00:00+00:00',
                       1, 0, NULL)""",
            (title, summary),
        )
    conn.commit()

    candidates = backfill_ai_bolding.select_local_cluster_candidates(
        conn,
        since_iso="2026-06-02T00:00:00+00:00",
    )

    assert [c["ai_title"] for c in candidates] == ["heading_only", "no_bold"]
    conn.close()


def test_row_to_dict_accepts_sqlite_row_like_without_get():
    from scripts import backfill_ai_bolding

    class RowLike:
        def __iter__(self):
            return iter([("id", "item-1"), ("title", "Title")])

    assert backfill_ai_bolding._row_to_dict(RowLike()) == {
        "id": "item-1",
        "title": "Title",
    }
