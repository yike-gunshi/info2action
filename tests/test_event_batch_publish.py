import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402
from clustering import pipeline as pl  # noqa: E402
from clustering import summary_writer  # noqa: E402
from clustering import vector_utils as vu  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv('INFO2ACTION_DATA_AUTHORITY', 'local')
    monkeypatch.setenv('INFO2ACTION_STORAGE_MODE', 'local')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_BACKEND', 'sqlite')
    monkeypatch.setenv('INFO2ACTION_EMBEDDING_BACKEND', 'sqlite')
    db_path = str(tmp_path / 'event_batch_publish.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _item(iid, *, run_id=None, content='hello'):
    return {
        'id': iid,
        'platform': 'twitter',
        'source': 'test',
        'fetch_run_id': run_id,
        'title': content,
        'content': content,
        'author_name': 'alice',
        'fetched_at': '2026-05-09T10:00:00',
        'published_at': '2026-05-09T09:55:00',
    }


class FakeProvider:
    name = 'fake'

    def embed(self, texts, *, mode='db'):
        rows = []
        for text in texts:
            if 'run one' in text:
                rows.append([1.0, 0.0, 0.0])
            elif 'run two' in text:
                rows.append([0.0, 1.0, 0.0])
            else:
                rows.append([0.0, 0.0, 1.0])
        return np.array(rows, dtype=np.float32)


def test_batch_upsert_tags_items_with_fetch_run_id(tmp_db):
    assert 'fetch_run_id' in _columns(tmp_db, 'items')

    db_mod.batch_upsert(tmp_db, [_item('same', content='first')], fetch_run_id=7)
    db_mod.batch_upsert(tmp_db, [_item('same', content='second')], fetch_run_id=8)

    row = tmp_db.execute(
        "SELECT fetch_run_id, fetched_at FROM items WHERE id='same'"
    ).fetchone()
    assert row['fetch_run_id'] == 8
    assert row['fetched_at'] == '2026-05-09T10:00:00'


def test_summary_draft_is_not_visible_until_publish(tmp_db, monkeypatch):
    for col in (
        'last_touched_run_id',
        'published_run_id',
        'published_at',
        'pending_is_visible_in_feed',
        'pending_summary_warnings_json',
    ):
        assert col in _columns(tmp_db, 'clusters')

    tmp_db.execute(
        """INSERT INTO clusters
             (id, first_doc_at, last_doc_at, last_updated_at, unique_source_count)
           VALUES (1, '2026-05-09T09:00:00', '2026-05-09T09:30:00',
                   '2026-05-09T09:30:00', 2)"""
    )
    for iid, platform in (('a', 'twitter'), ('b', 'reddit')):
        item = _item(iid, content=f'{platform} same event')
        item['platform'] = platform
        item['ai_summary'] = f'{platform} summary'
        db_mod.batch_upsert(tmp_db, [item], fetch_run_id=42)
        tmp_db.execute(
            """INSERT INTO cluster_items
                 (cluster_id, item_id, source_identity, is_primary_source)
               VALUES (1, ?, ?, 1)""",
            (iid, iid),
        )
    tmp_db.commit()

    monkeypatch.setattr(
        summary_writer,
        '_call_llm_chat',
        lambda **_: json.dumps({
            'is_event': True,
            'title': 'Batch Event',
            'summary': '【精华速览】本轮事件摘要。【全文拆解】这是完整拆解内容。',
            'key_points': ['A', 'B'],
            'warnings': [],
        }, ensure_ascii=False),
    )

    ok = summary_writer.regenerate_and_swap(
        tmp_db, 1, api_key='k', api_base=None, model='m',
        publish_immediately=False, run_id=42,
    )
    assert ok is True

    draft = tmp_db.execute(
        """SELECT ai_title, ai_title_draft, is_visible_in_feed,
                  pending_is_visible_in_feed, live_version, published_at
             FROM clusters WHERE id=1"""
    ).fetchone()
    assert draft['ai_title'] is None
    assert draft['ai_title_draft'] == 'Batch Event'
    assert draft['is_visible_in_feed'] == 0
    assert draft['pending_is_visible_in_feed'] == 1
    assert draft['live_version'] == 0
    assert draft['published_at'] is None

    assert summary_writer.publish_run(tmp_db, 42) == 1
    live = tmp_db.execute(
        """SELECT ai_title, ai_title_draft, is_visible_in_feed,
                  pending_is_visible_in_feed, live_version,
                  published_run_id, published_at
             FROM clusters WHERE id=1"""
    ).fetchone()
    assert live['ai_title'] == 'Batch Event'
    assert live['ai_title_draft'] is None
    assert live['is_visible_in_feed'] == 1
    assert live['pending_is_visible_in_feed'] is None
    assert live['live_version'] == 1
    assert live['published_run_id'] == 42
    assert live['published_at'] is not None


def test_pipeline_run_id_only_processes_current_run(tmp_db):
    item1 = _item('run1', run_id=1, content='run one')
    item1['ai_summary'] = 'run one summary'
    item2 = _item('run2', run_id=2, content='run two')
    item2['ai_summary'] = 'run two summary'
    db_mod.batch_upsert(tmp_db, [item1], fetch_run_id=1)
    db_mod.batch_upsert(tmp_db, [item2], fetch_run_id=2)

    stats = pl.run_pipeline(
        tmp_db,
        provider=FakeProvider(),
        top_k_judge=lambda *_: (_ for _ in ()).throw(AssertionError('no candidates expected')),
        api_key='k',
        api_base=None,
        model='m',
        run_id=1,
        skip_summary=True,
    )

    assert stats['embedded'] == 1
    assert stats['new_singletons'] == 1
    rows = {
        row['id']: row['cluster_id']
        for row in tmp_db.execute("SELECT id, cluster_id FROM items").fetchall()
    }
    assert rows['run1'] is not None
    assert rows['run2'] is None


def test_pipeline_publishes_completed_drafts_when_summary_fails(tmp_db, monkeypatch):
    rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    tmp_db.execute(
        """INSERT INTO clusters
             (id, first_doc_at, last_updated_at, representative_vector,
              doc_count, unique_source_count, live_version)
           VALUES (1, '2026-05-09T09:55:00Z', datetime('now'), ?, 1, 1, 0)""",
        (vu.pack_blob(rep),),
    )
    existing = _item('existing', content='run one existing')
    existing['platform'] = 'twitter'
    existing['ai_summary'] = 'existing summary'
    db_mod.batch_upsert(tmp_db, [existing])
    tmp_db.execute(
        """UPDATE items
              SET embedding = ?, embedding_provider = 'fake', cluster_id = 1
            WHERE id = 'existing'""",
        (vu.pack_blob(rep),),
    )
    tmp_db.execute(
        """INSERT INTO cluster_items
             (cluster_id, item_id, source_identity, is_primary_source)
           VALUES (1, 'existing', 'twitter:alice', 1)"""
    )
    incoming = _item('incoming', run_id=42, content='run one incoming')
    incoming['platform'] = 'reddit'
    incoming['author_name'] = 'bob'
    incoming['ai_summary'] = 'incoming summary'
    db_mod.batch_upsert(tmp_db, [incoming], fetch_run_id=42)
    tmp_db.commit()

    monkeypatch.setattr(summary_writer, 'regenerate_and_swap', lambda *_args, **_kwargs: False)
    publish_calls = []

    def fake_publish(_conn, run_id):
        publish_calls.append(run_id)
        return 0

    monkeypatch.setattr(summary_writer, 'publish_run', fake_publish)

    stats = pl.run_pipeline(
        tmp_db,
        provider=FakeProvider(),
        top_k_judge=lambda *_args: {
            'matches': [{'cluster_id': 1, 'same_event': True, 'confidence': 'high'}],
        },
        api_key='k',
        api_base=None,
        model='m',
        run_id=42,
    )

    assert stats['summary_failed'] == 1
    assert stats['published_clusters'] == 0
    assert publish_calls == [42]


def test_pipeline_summarizes_new_singletons_even_when_existing_cluster_was_bumped(tmp_db, monkeypatch):
    rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    tmp_db.execute(
        """INSERT INTO clusters
             (id, first_doc_at, last_updated_at, representative_vector,
              doc_count, unique_source_count, live_version)
           VALUES (1, datetime('now'), datetime('now'), ?, 1, 2, 0)""",
        (vu.pack_blob(rep),),
    )
    existing = _item('existing', content='run one existing')
    existing['ai_summary'] = 'existing summary'
    existing['ai_category'] = 'products'
    existing['ai_categories'] = '["products"]'
    existing['ai_quality_score'] = 80
    db_mod.batch_upsert(tmp_db, [existing])
    tmp_db.execute(
        """UPDATE items
              SET embedding = ?, embedding_provider = 'fake', cluster_id = 1
            WHERE id = 'existing'""",
        (vu.pack_blob(rep),),
    )
    tmp_db.execute(
        """INSERT INTO cluster_items
             (cluster_id, item_id, source_identity, is_primary_source)
           VALUES (1, 'existing', 'twitter:existing', 1)"""
    )
    join_item = _item('join', run_id=42, content='run one join')
    join_item['published_at'] = '2026-05-09T09:58:00'
    join_item['ai_summary'] = 'join summary'
    join_item['ai_category'] = 'products'
    join_item['ai_categories'] = '["products"]'
    join_item['ai_quality_score'] = 80
    solo_item = _item('solo', run_id=42, content='solo launch')
    solo_item['published_at'] = '2026-05-09T09:57:00'
    solo_item['ai_summary'] = 'solo summary'
    solo_item['ai_category'] = 'products'
    solo_item['ai_categories'] = '["products"]'
    solo_item['ai_quality_score'] = 80
    db_mod.batch_upsert(tmp_db, [join_item, solo_item], fetch_run_id=42)
    tmp_db.execute(
        """UPDATE items
              SET ai_category='products',
                  ai_categories='["products"]',
                  ai_quality_score=80
            WHERE id IN ('join', 'solo')"""
    )
    tmp_db.commit()

    recall_calls = {'count': 0}

    def fake_recall(*_args, **_kwargs):
        recall_calls['count'] += 1
        if recall_calls['count'] == 1:
            return [{'cluster_id': 1, 'cosine': 0.9}]
        return []

    summarized: list[int] = []
    monkeypatch.setattr(pl, '_recall_top_k_clusters', fake_recall)
    monkeypatch.setattr(
        summary_writer,
        'regenerate_and_swap',
        lambda _conn, cid, **_kwargs: summarized.append(cid) or True,
    )

    stats = pl.run_pipeline(
        tmp_db,
        provider=FakeProvider(),
        top_k_judge=lambda *_args: {
            'matches': [{'cluster_id': 1, 'same_event': True, 'confidence': 'high'}],
        },
        api_key='k',
        api_base=None,
        model='m',
        run_id=42,
        publish=False,
        summary_workers=2,
    )

    assert stats['judged_with_match'] == 1
    assert stats['new_singletons'] == 1
    assert len(summarized) == 2
    assert 1 in summarized


def test_pipeline_run_scoped_summary_failure_continues_later_clusters(tmp_db, monkeypatch):
    rows = []
    for iid, published_at in (
        ('solo-a', '2026-05-09T09:58:00'),
        ('solo-b', '2026-05-09T09:57:00'),
    ):
        item = _item(iid, run_id=42, content=iid)
        item['published_at'] = published_at
        item['ai_summary'] = f'{iid} summary'
        rows.append(item)
    db_mod.batch_upsert(tmp_db, rows, fetch_run_id=42)
    tmp_db.execute(
        """UPDATE items
              SET ai_category='products',
                  ai_categories='["products"]',
                  ai_quality_score=80
            WHERE id IN ('solo-a', 'solo-b')"""
    )
    tmp_db.commit()

    monkeypatch.setattr(pl, '_recall_top_k_clusters', lambda *_args, **_kwargs: [])
    summary_calls: list[int] = []
    monkeypatch.setattr(
        summary_writer,
        'regenerate_and_swap',
        lambda _conn, cid, **_kwargs: summary_calls.append(cid) and False,
    )
    publish_calls = []

    def fake_publish(_conn, run_id):
        publish_calls.append(run_id)
        return 0

    monkeypatch.setattr(summary_writer, 'publish_run', fake_publish)

    stats = pl.run_pipeline(
        tmp_db,
        provider=FakeProvider(),
        api_key='k',
        api_base=None,
        model='m',
        run_id=42,
    )

    assert stats['new_singletons'] == 2
    assert stats['summary_failed'] == 2
    assert len(summary_calls) == 2
    assert stats['published_clusters'] == 0
    assert publish_calls == [42]


def test_run_scoped_summary_recovery_targets_missing_drafts(tmp_db):
    rows = [
        (1, 42, None, 'draft title', 'draft summary', '[]', 1),
        (2, 42, None, None, None, None, None),
        (3, 42, None, None, None, None, 0),
        (4, 42, 42, None, None, None, None),
    ]
    for cid, touched_run, published_run, draft_title, draft_summary, draft_points, pending_visible in rows:
        tmp_db.execute(
            """INSERT INTO clusters
                 (id, first_doc_at, last_updated_at, unique_source_count,
                  last_touched_run_id, published_run_id, ai_title_draft,
                  ai_summary_draft, ai_key_points_draft, pending_is_visible_in_feed)
               VALUES (?, datetime('now'), datetime('now'), 2, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                touched_run,
                published_run,
                draft_title,
                draft_summary,
                draft_points,
                pending_visible,
            ),
        )
    tmp_db.commit()

    assert pl._clusters_requiring_summary(tmp_db, set(), 42) == [2]


def test_pipeline_retries_recent_unclustered_item_from_previous_run(tmp_db, monkeypatch):
    now_iso = datetime.now(timezone.utc).isoformat()
    item = _item('retry-prev-run', run_id=41, content='retry previous run')
    item['fetched_at'] = now_iso
    item['published_at'] = now_iso
    item['ai_summary'] = 'retry previous run summary'
    item['ai_category'] = 'products'
    item['ai_categories'] = '["products"]'
    item['ai_quality_score'] = 80
    db_mod.batch_upsert(tmp_db, [item], fetch_run_id=41)
    tmp_db.commit()

    monkeypatch.setenv('INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT', '10')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_RETRY_LOOKBACK_HOURS', '24')

    stats = pl.run_pipeline(
        tmp_db,
        provider=FakeProvider(),
        top_k_judge=lambda *_: (_ for _ in ()).throw(AssertionError('no candidates expected')),
        api_key='k',
        api_base=None,
        model='m',
        run_id=42,
        skip_summary=True,
    )

    row = tmp_db.execute(
        "SELECT cluster_id, embedding IS NOT NULL AS has_embedding FROM items WHERE id='retry-prev-run'"
    ).fetchone()
    assert stats['embedded'] == 1
    assert stats['new_singletons'] == 1
    assert row['has_embedding'] == 1
    assert row['cluster_id'] is not None


def test_run_scoped_summary_recovery_includes_recent_previous_run_failures(tmp_db, monkeypatch):
    now_iso = datetime.now(timezone.utc).isoformat()
    tmp_db.execute(
        """INSERT INTO clusters
             (id, first_doc_at, last_doc_at, last_updated_at, unique_source_count,
              last_touched_run_id, published_run_id, pending_is_visible_in_feed)
           VALUES (10, ?, ?, ?, 2, 41, NULL, NULL)""",
        (now_iso, now_iso, now_iso),
    )
    for iid, platform in (('old-a', 'twitter'), ('old-b', 'reddit')):
        item = _item(iid, run_id=41, content=f'{platform} retry summary')
        item['platform'] = platform
        item['fetched_at'] = now_iso
        item['published_at'] = now_iso
        item['ai_summary'] = f'{platform} summary'
        item['ai_category'] = 'products'
        item['ai_categories'] = '["products"]'
        item['ai_quality_score'] = 80
        db_mod.batch_upsert(tmp_db, [item], fetch_run_id=41)
        tmp_db.execute(
            """INSERT INTO cluster_items
                 (cluster_id, item_id, source_identity, is_primary_source)
               VALUES (10, ?, ?, 1)""",
            (iid, f'{platform}:alice'),
        )
    tmp_db.commit()

    monkeypatch.setenv('INFO2ACTION_CLUSTER_SUMMARY_RETRY_LIMIT', '10')
    monkeypatch.setenv('INFO2ACTION_CLUSTER_RETRY_LOOKBACK_HOURS', '24')

    assert pl._clusters_requiring_summary(tmp_db, set(), 42) == [10]
