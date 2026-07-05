"""Integration tests for src/clustering/pipeline.py

Covers R7.1 / R7.2 / R7.3 from .features/event-aggregation-v15/feature-spec.md:
- Stage 0: un-embedded items get embedded in one batch
- Stage 1: high-confidence match (>=0.85) goes straight into existing cluster
- Stage 2: boundary (0.70-0.85) triggers LLM judge; "no" creates singleton
- Stage 2 LLM failure -> new singleton (宁漏不错合, R7.2)
- Stage 3: representative vector updates after add
- Stage 4: doc_count follows (platform, author) dedup (R7.3)
"""
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402
from clustering import pipeline as pl  # noqa: E402
from clustering import vector_utils as vu  # noqa: E402


class _ProviderForEmbeddingClusteringProfile:
    name = 'openrouter-text-embedding-3-small'
    model = 'openai/text-embedding-3-small'


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'pipeline.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    monkeypatch.setattr(pl.remote_db, 'embedding_to_remote', lambda: False)
    monkeypatch.setattr(pl.remote_db, 'cluster_to_remote', lambda: False)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _insert_item(conn, iid, *, platform='x', author='alice', content='hello world'):
    conn.execute(
        """INSERT INTO items (id, platform, source, fetched_at, content,
                              author_name, title, published_at, ai_summary)
           VALUES (?, ?, 'following', datetime('now'), ?, ?, ?,
                   datetime('now'), ?)""",
        (iid, platform, content, author, content[:30], content[:50]),
    )


def test_embedding_clustering_profile_overrides_runtime_threshold():
    profile_doc = {
        'active_profile': 'openrouter_text_embedding_3_small_conservative',
        'profiles': {
            'openrouter_text_embedding_3_small_conservative': {
                'embedding_provider': 'openrouter',
                'embedding_model': 'openai/text-embedding-3-small',
                'stage1_cosine_min': 0.75,
                'stage1_gray_cosine_min': 0.70,
                'stage1_shadow_cosine_min': 0.65,
                'stage1_gray_max_temporal_hours': 2,
                'stage1_top_k': 10,
                'rationale': 'embedding vectors and cluster aggregation are calibrated together',
                'offline_eval': {'report': 'docs/调研/embedding/report.md'},
            }
        },
    }

    out = pl._apply_embedding_clustering_profile(
        {
            'embedding_clustering_profile': 'openrouter_text_embedding_3_small_conservative',
            'stage1_cosine_min': 0.5,
            'stage1_top_k': 3,
        },
        provider_name='openrouter',
        provider=_ProviderForEmbeddingClusteringProfile(),
        profile_doc=profile_doc,
    )

    assert out['stage1_cosine_min'] == 0.75
    assert out['stage1_gray_cosine_min'] == 0.70
    assert out['stage1_shadow_cosine_min'] == 0.65
    assert out['stage1_gray_max_temporal_hours'] == 2
    assert out['stage1_top_k'] == 10
    assert out['embedding_clustering_profile'] == 'openrouter_text_embedding_3_small_conservative'
    assert out['_embedding_clustering_profile_report'] == 'docs/调研/embedding/report.md'


def test_embedding_clustering_profile_skips_mismatched_provider_and_finds_matching_profile():
    profile_doc = {
        'active_profile': 'minimax_embo_01_recalibrated',
        'profiles': {
            'minimax_embo_01_recalibrated': {
                'embedding_provider': 'minimax',
                'embedding_model': 'embo-01',
                'stage1_cosine_min': 0.9,
            },
            'openrouter_text_embedding_3_small_conservative': {
                'embedding_provider': 'openrouter',
                'embedding_model': 'openai/text-embedding-3-small',
                'stage1_cosine_min': 0.75,
            },
        },
    }

    out = pl._apply_embedding_clustering_profile(
        {'stage1_cosine_min': 0.5},
        provider_name='openrouter',
        provider=_ProviderForEmbeddingClusteringProfile(),
        profile_doc=profile_doc,
    )

    assert out['embedding_clustering_profile'] == 'openrouter_text_embedding_3_small_conservative'
    assert out['stage1_cosine_min'] == 0.75


def test_remote_summary_parallel_uses_remote_writer_without_sqlite_workers(monkeypatch):
    calls = []

    class NoSqliteConn:
        def commit(self):
            raise AssertionError('remote run should not commit the caller SQLite connection')

    def fail_local_worker_conn():
        raise AssertionError('remote summary workers should not open local SQLite connections')

    def fake_regenerate(conn, cluster_id, **_kwargs):
        calls.append((conn, cluster_id))
        return True

    monkeypatch.setattr(pl.remote_db, 'cluster_to_remote', lambda: True)
    monkeypatch.setattr(pl, '_embed_pending_items', lambda *args, **kwargs: 0)
    monkeypatch.setattr(pl, '_load_pending_cluster_items', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        pl,
        '_clusters_requiring_summary_remote',
        lambda *args, **kwargs: [101, 102, 103],
    )
    monkeypatch.setattr(pl.db, 'get_conn', fail_local_worker_conn)
    monkeypatch.setattr(pl.summary_writer, 'regenerate_and_swap', fake_regenerate)

    stats = pl.run_pipeline(
        NoSqliteConn(),
        provider=object(),
        api_key='k',
        api_base=None,
        model='m',
        run_id=777,
        summary_workers=3,
        publish=False,
    )

    assert stats['summary_regenerated'] == 3
    assert stats['summary_failed'] == 0
    assert sorted(cluster_id for _conn, cluster_id in calls) == [101, 102, 103]
    assert all(conn is None for conn, _cluster_id in calls)


def test_remote_recall_receives_temporal_filters(monkeypatch):
    calls = {}

    def fake_recall(_conn, _vec, **kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(pl.remote_db, 'cluster_to_remote', lambda: True)
    monkeypatch.setattr(pl.remote_db, 'recall_top_k_clusters_remote', fake_recall)

    pl._recall_top_k_clusters(
        None,
        np.array([1.0, 0.0, 0.0], dtype=np.float32),
        k=5,
        window_days=30,
        cosine_min=0.75,
        item_time='2026-05-27T12:00:00+00:00',
        temporal_adjacency_days=2.5,
        max_merged_span_days=6.0,
    )

    assert calls['k'] == 5
    assert calls['window_days'] == 30
    assert calls['cosine_min'] == 0.75
    assert calls['item_time'] == '2026-05-27T12:00:00+00:00'
    assert calls['temporal_adjacency_days'] == 2.5
    assert calls['max_merged_span_days'] == 6.0


class FakeProvider:
    """Returns fixed embeddings per item content. Deterministic for tests.

    v15.1: pipeline now feeds structured event_embedding_text (title +
    ai_summary + ai_key_points + ... + content) instead of raw content. To stay
    backward-compatible we look up each mapping key as a substring of the
    actual text. Mapping keys are content/title fragments; first match wins.
    """

    name = 'fake-test'

    def __init__(self, mapping: dict):
        self.mapping = mapping

    def embed(self, texts, *, mode='db'):
        out = []
        for t in texts:
            vec = self._lookup(t)
            if vec is None:
                raise KeyError(f'FakeProvider has no mapping match for text: {t[:80]!r}')
            out.append(vec)
        return np.array(out, dtype=np.float32)

    def _lookup(self, text):
        # Exact match first (legacy callers); then substring (v15.1 callers).
        if text in self.mapping:
            return self.mapping[text]
        for key, vec in self.mapping.items():
            if key and key in text:
                return vec
        return None


class TestStage0Embedding:
    def test_stage0_embeds_all_unembedded_items(self, tmp_db):
        _insert_item(tmp_db, 'a', platform='x', author='alice', content='one')
        _insert_item(tmp_db, 'b', platform='reddit', author='bob', content='two')
        tmp_db.commit()
        provider = FakeProvider({'one': [1, 0, 0], 'two': [0, 1, 0]})
        n = pl._embed_pending_items(tmp_db, provider, batch_size=10)
        assert n == 2
        rows = tmp_db.execute(
            "SELECT id, embedding, embedding_provider FROM items WHERE embedding IS NOT NULL"
        ).fetchall()
        assert len(rows) == 2
        for r in rows:
            vec = vu.unpack_blob(r['embedding'])
            assert vec.shape == (3,)
            assert r['embedding_provider'] == 'fake-test'

    def test_stage0_batch_failure_falls_back_to_single_items(self, tmp_db):
        _insert_item(tmp_db, 'good-a', platform='x', author='alice', content='good one')
        _insert_item(tmp_db, 'bad', platform='x', author='alice', content='bad item')
        _insert_item(tmp_db, 'good-b', platform='x', author='alice', content='good two')
        tmp_db.commit()

        class FlakyProvider:
            name = 'flaky-test'

            def embed(self, texts, *, mode='db'):
                if len(texts) > 1:
                    raise RuntimeError('batch rejected')
                if 'bad item' in texts[0]:
                    raise RuntimeError('single item rejected')
                return np.array([[1.0, 0.0, 0.0]], dtype=np.float32)

        n = pl._embed_pending_items(tmp_db, FlakyProvider(), batch_size=3)

        assert n == 2
        rows = {
            row['id']: row['embedding'] is not None
            for row in tmp_db.execute("SELECT id, embedding FROM items").fetchall()
        }
        assert rows == {'good-a': True, 'bad': False, 'good-b': True}

    def test_run_scoped_embedding_is_not_capped_at_500(self, tmp_db):
        for idx in range(501):
            tmp_db.execute(
                """INSERT INTO items (id, platform, source, fetched_at, content,
                                      author_name, title, published_at, ai_summary,
                                      fetch_run_id)
                   VALUES (?, 'x', 'following', datetime('now'), ?, 'alice', ?,
                           datetime('now'), ?, 7)""",
                (f'run7-{idx}', f'content {idx}', f'title {idx}', f'summary {idx}'),
            )
        tmp_db.commit()

        class ConstantProvider:
            name = 'constant-test'

            def embed(self, texts, *, mode='db'):
                return np.ones((len(texts), 3), dtype=np.float32)

        n = pl._embed_pending_items(tmp_db, ConstantProvider(), batch_size=32, run_id=7)

        assert n == 501
        embedded = tmp_db.execute(
            "SELECT COUNT(*) FROM items WHERE fetch_run_id=7 AND embedding IS NOT NULL"
        ).fetchone()[0]
        assert embedded == 501

    def test_run_scoped_inserted_scope_embeds_only_new_fetch_items(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO fetch_runs (id, started_at, status) VALUES (77, datetime('now'), 'running')"
        )
        for iid in ("old", "new"):
            tmp_db.execute(
                """INSERT INTO items (id, platform, source, fetched_at, content,
                                      author_name, title, published_at, ai_summary,
                                      fetch_run_id)
                   VALUES (?, 'x', 'following', datetime('now'), ?, 'alice', ?,
                           datetime('now'), ?, 77)""",
                (iid, iid, iid, f'summary {iid}'),
            )
        tmp_db.execute(
            """INSERT INTO fetch_run_items (run_id, item_id, platform, source, was_inserted)
               VALUES (77, 'old', 'x', 'following', 0),
                      (77, 'new', 'x', 'following', 1)"""
        )
        tmp_db.commit()

        provider = FakeProvider({'old': [1, 0, 0], 'new': [0, 1, 0]})
        n = pl._embed_pending_items(
            tmp_db,
            provider,
            batch_size=10,
            run_id=77,
            run_items_scope='inserted',
        )

        assert n == 1
        rows = {
            row['id']: row['embedding'] is not None
            for row in tmp_db.execute("SELECT id, embedding FROM items").fetchall()
        }
        assert rows == {'old': False, 'new': True}

    def test_embedding_provider_error_does_not_mark_minimax_embedding_state(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(pl.ai_provider_guard, 'STATE_PATH', str(tmp_path / 'state.json'))
        monkeypatch.setattr(pl.ai_provider_guard, 'LOCK_PATH', str(tmp_path / 'state.lock'))
        monkeypatch.setattr(pl.remote_db, 'embedding_to_remote', lambda: False)
        _insert_item(tmp_db, 'a', platform='x', author='alice', content='one')
        tmp_db.commit()

        class BalanceProvider:
            name = 'balance-test'

            def embed(self, texts, *, mode='db'):
                raise ValueError('embedding provider failed')

        assert pl._embed_pending_items(tmp_db, BalanceProvider(), batch_size=10) == 0

        state = pl.ai_provider_guard.load_state(pl.ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER)
        assert state['status'] == 'ok'

    def test_windowed_pipeline_processes_pending_items_newest_first(self, tmp_db, monkeypatch):
        for iid, published_at in (
            ('older', '2026-05-10T01:00:00+00:00'),
            ('newer', '2026-05-10T03:00:00+00:00'),
            ('outside', '2026-05-09T23:00:00+00:00'),
        ):
            tmp_db.execute(
                """INSERT INTO items (
                       id, platform, source, fetched_at, content, author_name, title,
                       published_at, ai_summary, fetch_run_id, embedding
                   ) VALUES (?, 'x', 'following', '2026-05-10T04:00:00+00:00',
                             ?, 'alice', ?, ?, ?, 7, ?)""",
                (iid, iid, iid, published_at, f'summary {iid}', vu.pack_blob(np.array([1, 0, 0]))),
            )
        tmp_db.commit()
        seen: list[str] = []

        monkeypatch.setattr(pl, '_recall_top_k_clusters', lambda *_args, **_kwargs: [])

        def fake_create_singleton(_conn, item_id, *_args, **_kwargs):
            seen.append(item_id)
            return len(seen)

        monkeypatch.setattr(pl, '_create_singleton', fake_create_singleton)

        pl.run_pipeline(
            tmp_db,
            provider=FakeProvider({}),
            run_id=7,
            window_start='2026-05-10T00:00:00+00:00',
            window_end='2026-05-10T04:00:00+00:00',
            require_published_at=True,
            skip_summary=True,
            publish=False,
        )

        assert seen == ['newer', 'older']

    def test_run_scoped_inserted_scope_clusters_only_new_fetch_items(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO fetch_runs (id, started_at, status) VALUES (77, datetime('now'), 'running')"
        )
        for iid, vec in (
            ('old', np.array([1.0, 0.0, 0.0], dtype=np.float32)),
            ('new', np.array([0.0, 1.0, 0.0], dtype=np.float32)),
        ):
            tmp_db.execute(
                """INSERT INTO items (
                       id, platform, source, fetched_at, content, author_name, title,
                       published_at, ai_summary, fetch_run_id, embedding
                   ) VALUES (?, 'x', 'following', datetime('now'),
                             ?, 'alice', ?, datetime('now'), ?, 77, ?)""",
                (iid, iid, iid, f'summary {iid}', vu.pack_blob(vec)),
            )
        tmp_db.execute(
            """INSERT INTO fetch_run_items (run_id, item_id, platform, source, was_inserted)
               VALUES (77, 'old', 'x', 'following', 0),
                      (77, 'new', 'x', 'following', 1)"""
        )
        tmp_db.commit()

        stats = pl.run_pipeline(
            tmp_db,
            provider=FakeProvider({}),
            run_id=77,
            run_items_scope='inserted',
            skip_summary=True,
            publish=False,
        )

        rows = {
            row['id']: row['cluster_id']
            for row in tmp_db.execute("SELECT id, cluster_id FROM items").fetchall()
        }
        assert rows['old'] is None
        assert rows['new'] is not None
        assert stats['new_singletons'] == 1


class TestStage0EventEmbeddingTextV15_1:
    """Verify Stage 0 logs event_embedding_text_built per item and
    cluster_low_confidence_doc_allowed for fallback items (R1.2).
    Also verify embedding text length <= 10000 and comments_json never leaks.
    """

    def _insert_full_enriched(self, conn, iid):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at,
                                  ai_summary, ai_key_points, ai_keywords,
                                  ai_category, content_type, comments_json)
               VALUES (?, 'x', 'following', datetime('now'), ?, 'alice', ?,
                       datetime('now'), ?, ?, ?, ?, ?, ?)""",
            (
                iid,
                'GPT-5.5 正式发布，OpenAI 在发布会上宣布……（content body）',
                'GPT-5.5 正式发布',
                'OpenAI 今日发布 GPT-5.5，支持 1M context window。',
                json.dumps(['1M context', '速度提升 2x', '价格下调 30%'], ensure_ascii=False),
                'GPT-5.5, OpenAI',
                'tech',
                'news',
                json.dumps([{'text': 'COMMENTS_SHOULD_NOT_LEAK'}], ensure_ascii=False),
            ),
        )

    def _insert_missing_summary(self, conn, iid):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_key_points)
               VALUES (?, 'reddit', 'following', datetime('now'),
                       'A long body about a tech news event',
                       'bob', 'Bob title', datetime('now'),
                       ?)""",
            (iid, json.dumps(['only kp'], ensure_ascii=False)),
        )

    def _insert_missing_key_points(self, conn, iid):
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_summary)
               VALUES (?, 'youtube', 'following', datetime('now'),
                       'YT body',
                       'carol', 'Carol title', datetime('now'),
                       'just a summary, no key points')""",
            (iid,),
        )

    def test_stage0_logs_per_item_and_caps_text(self, tmp_db, monkeypatch):
        self._insert_full_enriched(tmp_db, 'a')
        self._insert_missing_summary(tmp_db, 'b')
        self._insert_missing_key_points(tmp_db, 'c')
        tmp_db.commit()

        captured = []

        def fake_log(event, **fields):
            captured.append((event, fields))

        monkeypatch.setattr(pl, '_log_event', fake_log)

        # Capture texts the provider sees, so we can assert constraints.
        seen_texts: list[str] = []

        class CapturingProvider:
            name = 'fake-cap'

            def embed(self, texts, *, mode='db'):
                seen_texts.extend(texts)
                return np.array([[1.0, 0.0, 0.0]] * len(texts), dtype=np.float32)

        n = pl._embed_pending_items(tmp_db, CapturingProvider(), batch_size=10)
        assert n == 3

        built_events = [f for ev, f in captured if ev == 'event_embedding_text_built']
        assert len(built_events) == 3, f'expected 3 build logs, got {len(built_events)}'
        # Each entry has the required keys.
        for ev in built_events:
            assert 'item_id' in ev
            assert 'has_ai_summary' in ev
            assert 'has_ai_key_points' in ev
            assert 'has_ai_keywords' in ev
            assert 'used_fallback_content' in ev
            assert 'embedding_text_chars' in ev
            assert ev['embedding_text_chars'] <= 10000

        low_conf_events = [f for ev, f in captured if ev == 'cluster_low_confidence_doc_allowed']
        # Items 'b' (no summary) and 'c' (no key points) trigger fallback log.
        assert len(low_conf_events) == 2
        item_ids = sorted(e['item_id'] for e in low_conf_events)
        assert item_ids == ['b', 'c']
        for e in low_conf_events:
            assert e['reason'] == 'missing_ai_summary_or_key_points'
            assert e['ai_understanding_status'] == 'fallback'

        # comments_json must never leak into any provider input.
        for t in seen_texts:
            assert 'COMMENTS_SHOULD_NOT_LEAK' not in t
            assert len(t) <= 10000

        # Item 'a' (fully enriched) SHALL NOT trigger fallback log.
        a_built = next(e for e in built_events if e['item_id'] == 'a')
        assert a_built['used_fallback_content'] is False
        assert a_built['has_ai_summary'] is True
        assert a_built['has_ai_key_points'] is True

    def test_stage0_old_3800_cap_no_longer_applied(self, tmp_db, monkeypatch):
        """Long content should now go up to 10000 chars (was 3800)."""
        long_body = 'X' * 9000
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  title, ai_summary, ai_key_points)
               VALUES ('huge', 'x', 'following', datetime('now'), ?, 'T', 'S',
                       ?)""",
            (long_body, json.dumps(['kp1'], ensure_ascii=False)),
        )
        tmp_db.commit()

        seen_texts: list[str] = []

        class CapturingProvider:
            name = 'fake-cap'

            def embed(self, texts, *, mode='db'):
                seen_texts.extend(texts)
                return np.array([[1.0, 0.0, 0.0]] * len(texts), dtype=np.float32)

        n = pl._embed_pending_items(tmp_db, CapturingProvider(), batch_size=10)
        assert n == 1
        assert len(seen_texts) == 1
        # New cap is 10000; old 3800 cap would have produced len <= 3800.
        assert len(seen_texts[0]) > 3800
        assert len(seen_texts[0]) <= 10000


def _make_top_k_judge(matches_by_cluster: dict | None = None,
                       *, raise_exc: Exception | None = None,
                       return_error: dict | None = None):
    """Helper: build a fake top_k_judge for tests.

    ``matches_by_cluster`` maps cluster_id → match dict spec. Default match
    is same_event=False / unrelated. Pass ``raise_exc=...`` to simulate a
    raise; ``return_error={'error': ..., 'detail': ...}`` to simulate a
    parsed-failure judge response.
    """
    matches_by_cluster = matches_by_cluster or {}

    def judge(item_row, candidates):
        if raise_exc is not None:
            raise raise_exc
        if return_error is not None:
            return dict(return_error, estimated_input_tokens=100)
        out_matches = []
        for c in candidates:
            cid = c['cluster_id']
            spec = matches_by_cluster.get(cid, {})
            out_matches.append({
                'cluster_id': cid,
                'same_event': spec.get('same_event', False),
                'confidence': spec.get('confidence', 'low'),
                'relationship': spec.get('relationship', 'unrelated'),
                'subject_check': spec.get('subject_check', ''),
                'action_check': spec.get('action_check', ''),
                'time_check': spec.get('time_check', ''),
                'rationale': spec.get('rationale', ''),
            })
        return {
            'fingerprint': {'subject': 'x', 'action': 'y'},
            'matches': out_matches,
            'estimated_input_tokens': 100,
        }
    return judge


class TestStage1StraightMatch:
    def test_high_cosine_no_match_creates_singleton_v2(self, tmp_db):
        """V2 R2.1: high cosine alone NEVER auto-joins. LLM judge decides."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 "
            "WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.commit()

        new_vec = np.array([0.95, 0.1, 0.05], dtype=np.float32)  # cos ~0.99 with rep
        provider = FakeProvider({'two': new_vec.tolist()})

        # LLM says "no match" — V2 must create singleton even though cosine is
        # essentially 1.0. (V1 would have auto-joined; V2 forbids that.)
        judge = _make_top_k_judge({})

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        r = tmp_db.execute(
            "SELECT cluster_id FROM items WHERE id='new'"
        ).fetchone()
        assert r['cluster_id'] != 1, 'V2 SHALL NOT auto-join on high cosine alone'

    def test_llm_says_yes_high_confidence_joins_cluster(self, tmp_db):
        """V2: LLM same_event=true + confidence=high → join the existing cluster."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 "
            "WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.commit()

        new_vec = np.array([0.95, 0.1, 0.05], dtype=np.float32)
        provider = FakeProvider({'two': new_vec.tolist()})
        judge = _make_top_k_judge({1: {
            'same_event': True, 'confidence': 'high', 'relationship': 'same_event',
        }})

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        r = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert r['cluster_id'] == 1
        row = tmp_db.execute("SELECT doc_count FROM clusters WHERE id=1").fetchone()
        assert row['doc_count'] == 2

    def test_join_recomputes_cluster_time_bounds_to_canonical_utc(self, tmp_db):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, 'Wed, 22 Apr 2026 16:11:23 +0000',
                       'Wed, 22 Apr 2026 16:11:23 +0000',
                       datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            """UPDATE items SET embedding = ?, embedding_provider='fake-test',
                                cluster_id=1, published_at='Wed, 22 Apr 2026 16:11:23 +0000'
               WHERE id='existing'""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.execute(
            "UPDATE items SET published_at='2026-04-25 09:35' WHERE id='new'"
        )
        tmp_db.commit()

        provider = FakeProvider({'two': [0.95, 0.1, 0.05]})
        judge = _make_top_k_judge({1: {
            'same_event': True, 'confidence': 'high', 'relationship': 'same_event',
        }})
        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        row = tmp_db.execute(
            "SELECT first_doc_at, last_doc_at FROM clusters WHERE id=1"
        ).fetchone()
        assert row['first_doc_at'] == '2026-04-22T16:11:23Z'
        assert row['last_doc_at'] == '2026-04-25T01:35:00Z'


class TestBoundaryLLM:
    def test_llm_says_no_creates_singleton(self, tmp_db):
        """V2 R3.2: LLM same_event=false on every candidate → singleton."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 "
            "WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.commit()

        boundary_vec = np.array([0.75, 0.65, 0.1], dtype=np.float32)
        provider = FakeProvider({'two': boundary_vec.tolist()})
        judge = _make_top_k_judge({})  # all candidates same_event=False

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        r = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert r['cluster_id'] != 1
        clusters = tmp_db.execute(
            "SELECT id, doc_count FROM clusters ORDER BY id"
        ).fetchall()
        assert len(clusters) == 2

    def test_llm_judge_error_creates_singleton(self, tmp_db):
        """V2 R3.3: Stage 2 LLM failure -> new singleton, NOT silent merge."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.commit()

        boundary_vec = np.array([0.75, 0.65, 0.1], dtype=np.float32)
        provider = FakeProvider({'two': boundary_vec.tolist()})

        judge = _make_top_k_judge(return_error={
            'error': 'llm_failed', 'detail': 'simulated timeout',
        })

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        r = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert r['cluster_id'] != 1
        # cluster_judge_log SHALL have a row with selection_reason='llm-failed-fallback-singleton'.
        rows = tmp_db.execute(
            "SELECT selection_reason FROM cluster_judge_log WHERE item_id='new'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['selection_reason'] == 'llm-failed-fallback-singleton'

    def test_run_scoped_provider_rate_limit_defers_item_without_singleton(self, tmp_db):
        """Run-scoped 429 leaves the item pending instead of publishing a false singleton."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.execute("UPDATE items SET fetch_run_id=77 WHERE id='new'")
        tmp_db.commit()

        provider = FakeProvider({'two': [0.75, 0.65, 0.1]})
        judge = _make_top_k_judge(return_error={
            'error': 'provider_rate_limited',
            'detail': 'HTTP 429',
            'retryable': True,
        })

        stats = pl.run_pipeline(
            tmp_db, provider=provider, top_k_judge=judge,
            api_key='k', api_base=None, model='m', run_id=77,
        )

        row = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert row['cluster_id'] is None
        assert stats['judge_llm_failed'] == 1

    def test_run_scoped_llm_failure_defers_without_singleton(self, tmp_db):
        """Run-scoped judge failures remain pending so a later retry can cluster them correctly."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'new', platform='reddit', author='bob', content='two')
        tmp_db.execute("UPDATE items SET fetch_run_id=77 WHERE id='new'")
        tmp_db.commit()

        provider = FakeProvider({'two': [0.75, 0.65, 0.1]})
        judge = _make_top_k_judge(return_error={
            'error': 'llm_failed',
            'detail': 'HTTP 500',
        })

        stats = pl.run_pipeline(
            tmp_db, provider=provider, top_k_judge=judge,
            api_key='k', api_base=None, model='m', run_id=77,
        )

        row = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert row['cluster_id'] is None
        assert stats['judge_llm_failed'] == 1
        assert tmp_db.execute(
            "SELECT COUNT(*) FROM cluster_judge_log WHERE item_id='new'"
        ).fetchone()[0] == 0

    def test_judge_top_k_marks_transient_5xx_retryable_after_retries(self, monkeypatch):
        def fail_500(**_kwargs):
            raise urllib.error.HTTPError(
                url="https://api.example.com/messages",
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=BytesIO(b"{}"),
            )

        monkeypatch.setattr(pl.summary_writer, "_call_llm_chat", fail_500)

        result = pl._judge_top_k(
            {
                "id": "new",
                "title": "title",
                "content": "content",
                "ai_summary": "summary",
                "ai_key_points": "[]",
                "ai_keywords": "",
                "ai_category": "tech",
                "content_type": "post",
                "platform": "reddit",
                "author_name": "bob",
                "published_at": "2026-05-01T00:00:00Z",
            },
            [{"cluster_id": 1, "doc_count": 1, "cosine": 0.8}],
            api_key="k",
            api_base="https://api.example.com",
            model="m",
            max_5xx_retries=1,
        )

        assert result["error"] == "provider_transient_5xx"
        assert result["retryable"] is True
        assert result["detail"] == "HTTP Error 500: Internal Server Error"

    def test_judge_top_k_retries_transient_5xx_then_succeeds(self, monkeypatch):
        calls = {"n": 0}
        sleeps: list[float] = []

        def flaky_500(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    url="https://api.example.com/messages",
                    code=500,
                    msg="Internal Server Error",
                    hdrs={},
                    fp=BytesIO(b"{}"),
                )
            return '{"new_doc_fingerprint": {}, "matches": []}'

        monkeypatch.setattr(pl.summary_writer, "_call_llm_chat", flaky_500)
        monkeypatch.setattr(pl.time, "sleep", lambda delay: sleeps.append(delay))

        result = pl._judge_top_k(
            {
                "id": "new",
                "title": "title",
                "content": "content",
                "ai_summary": "summary",
                "ai_key_points": "[]",
                "ai_keywords": "",
                "ai_category": "tech",
                "content_type": "post",
                "platform": "reddit",
                "author_name": "bob",
                "published_at": "2026-05-01T00:00:00Z",
            },
            [{"cluster_id": 1, "doc_count": 1, "cosine": 0.8}],
            api_key="k",
            api_base="https://api.example.com",
            model="m",
        )

        assert result["matches"] == []
        assert calls["n"] == 2
        assert sleeps == [2.0]


class TestGrayRecallCandidates:
    def _seed_existing_cluster(self, conn, *, cluster_id=1, author='alice',
                               category='coding'):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        conn.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version,
                                     ai_title, ai_summary, ai_key_points)
               VALUES (?, '2026-05-28T00:00:00Z', '2026-05-28T00:00:00Z',
                       '2026-05-28T00:00:00Z', ?, 1, 0,
                       'Codex Claude Code agent plan',
                       'Codex and Claude Code agent planning workflow',
                       '["Codex", "Claude Code"]')""",
            (cluster_id, vu.pack_blob(rep)),
        )
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_summary,
                                  ai_key_points, ai_keywords, ai_category,
                                  embedding, cluster_id)
               VALUES ('existing', 'x', 'following', '2026-05-28T00:00:00Z',
                       'Codex Claude Code agent plan', ?, 'Codex plan',
                       '2026-05-28T00:00:00Z',
                       'Codex and Claude Code planning',
                       '["Codex", "Claude Code"]',
                       '["Codex", "Claude Code"]',
                       ?, ?, ?)""",
            (author, category, vu.pack_blob(rep), cluster_id),
        )
        conn.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, is_primary_source,
                                          rank_in_cluster)
               VALUES (?, 'existing', 1, 0)""",
            (cluster_id,),
        )

    def _insert_new_item(self, conn, *, cosine: float, author='alice',
                         category='coding'):
        vec = np.array(
            [cosine, float(np.sqrt(1 - cosine * cosine)), 0.0],
            dtype=np.float32,
        )
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_summary,
                                  ai_key_points, ai_keywords, ai_category,
                                  embedding)
               VALUES ('new', 'x', 'following', '2026-05-28T00:09:00Z',
                       'Codex Claude Code single agent design', ?,
                       'Codex single agent design',
                       '2026-05-28T00:09:00Z',
                       'Codex and Claude Code single agent design',
                       '["Codex", "Claude Code"]',
                       '["Codex", "Claude Code"]',
                       ?, ?)""",
            (author, category, vu.pack_blob(vec)),
        )

    def test_gray_candidate_with_same_author_category_and_entity_enters_judge(self, tmp_db):
        self._seed_existing_cluster(tmp_db)
        self._insert_new_item(tmp_db, cosine=0.72)
        tmp_db.commit()

        seen_candidates: list[list[dict]] = []

        def judge_yes(_item_row, candidates):
            seen_candidates.append(candidates)
            return {'matches': [
                {'cluster_id': c['cluster_id'], 'same_event': True,
                 'confidence': 'high', 'relationship': 'same_event',
                 'rationale': 'gray candidate should be judged'}
                for c in candidates
            ]}

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db,
                provider=FakeProvider({}),
                top_k_judge=judge_yes,
                api_key='k',
                api_base=None,
                model='m',
                skip_summary=True,
                cosine_min=0.75,
                gray_cosine_min=0.70,
                shadow_cosine_min=0.65,
            )

        assert seen_candidates
        assert seen_candidates[0][0]['cluster_id'] == 1
        assert seen_candidates[0][0]['recall_band'] == 'gray'
        row = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert row['cluster_id'] == 1

    def test_shadow_candidate_is_logged_but_not_sent_to_judge(self, tmp_db, monkeypatch):
        self._seed_existing_cluster(tmp_db)
        self._insert_new_item(tmp_db, cosine=0.67)
        tmp_db.commit()

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(pl, '_log_event', lambda event, **fields: events.append((event, fields)))

        def boom(_item_row, _candidates):
            raise AssertionError('shadow candidates must not be sent to LLM judge')

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db,
                provider=FakeProvider({}),
                top_k_judge=boom,
                api_key='k',
                api_base=None,
                model='m',
                skip_summary=True,
                cosine_min=0.75,
                gray_cosine_min=0.70,
                shadow_cosine_min=0.65,
            )

        row = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert row['cluster_id'] != 1
        shadow_events = [fields for event, fields in events if event == 'stage1_shadow_candidates_observed']
        assert shadow_events
        assert shadow_events[0]['candidate_cluster_ids'] == [1]
        assert shadow_events[0]['top_cosines'] == [pytest.approx(0.67, abs=1e-5)]


class TestDocCountDedup:
    def test_same_platform_same_author_does_not_double_count(self, tmp_db):
        """R7.3: Reddit thread 100 comments from same author = doc_count 1.

        V2 path: LLM says same_event=high → joins cluster 1. doc_count is
        platforms+authors dedup, so 2 reddit/user_A items collapse to 1.
        """
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'r1', platform='reddit', author='user_A', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, embedding_provider='fake-test', cluster_id=1 WHERE id='r1'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'r1', 1)"
        )
        _insert_item(tmp_db, 'r2', platform='reddit', author='user_A', content='two')
        tmp_db.commit()

        high = np.array([0.99, 0.01, 0.0], dtype=np.float32)
        provider = FakeProvider({'two': high.tolist()})
        judge = _make_top_k_judge({1: {
            'same_event': True, 'confidence': 'high', 'relationship': 'same_event',
        }})

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        row = tmp_db.execute("SELECT doc_count FROM clusters WHERE id=1").fetchone()
        assert row['doc_count'] == 1
        mapped = tmp_db.execute(
            "SELECT COUNT(*) AS n FROM cluster_items WHERE cluster_id=1"
        ).fetchone()['n']
        assert mapped == 2


class TestFinalizeClusterStateLastDocAt:
    """Feature-spec R4.1: _finalize_cluster_state must recompute last_doc_at
    = MAX(member published_at, fetched_at), normalized to canonical UTC."""

    def test_recomputes_last_doc_at_to_max_published(self, tmp_db):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, representative_vector,
                                     doc_count, live_version)
               VALUES (1, '2026-04-20T10:00:00', '2026-04-20T10:00:00',
                       datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        # Two members with different published_at
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, embedding, cluster_id)
               VALUES ('older', 'x', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'older body', ?, 1)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, embedding, cluster_id)
               VALUES ('newer', 'reddit', 'following', '2026-04-25T09:00:00',
                       '2026-04-25T15:45:00', 'newer body', ?, 1)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, is_primary_source)
               VALUES (1, 'older', 1), (1, 'newer', 0)"""
        )
        tmp_db.commit()

        pl._finalize_cluster_state(tmp_db, 1, tau_hours=24.0)

        row = tmp_db.execute(
            "SELECT last_doc_at FROM clusters WHERE id=1"
        ).fetchone()
        assert row['last_doc_at'] == '2026-04-25T07:45:00Z'

    def test_falls_back_to_fetched_at_when_published_null(self, tmp_db):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, representative_vector,
                                     doc_count, live_version)
               VALUES (1, '2026-04-20T10:00:00', '2026-04-20T10:00:00',
                       datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        # Both members have NULL published_at — must fall back to fetched_at.
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  content, embedding, cluster_id)
               VALUES ('a', 'x', 'following', '2026-04-22T11:00:00',
                       'a body', ?, 1)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  content, embedding, cluster_id)
               VALUES ('b', 'reddit', 'following', '2026-04-26T20:00:00',
                       'b body', ?, 1)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, is_primary_source)
               VALUES (1, 'a', 1), (1, 'b', 0)"""
        )
        tmp_db.commit()

        pl._finalize_cluster_state(tmp_db, 1, tau_hours=24.0)

        row = tmp_db.execute(
            "SELECT last_doc_at FROM clusters WHERE id=1"
        ).fetchone()
        assert row['last_doc_at'] == '2026-04-26T12:00:00Z'

    def test_emits_cluster_state_finalized_log(self, tmp_db):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, representative_vector,
                                     doc_count, live_version)
               VALUES (1, '2026-04-20T10:00:00', '2026-04-20T10:00:00',
                       datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, embedding, cluster_id)
               VALUES ('only', 'x', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'only body', ?, 1)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, is_primary_source)
               VALUES (1, 'only', 1)"""
        )
        tmp_db.commit()

        from unittest.mock import patch
        with patch.object(pl, '_log_event') as mock_log:
            pl._finalize_cluster_state(tmp_db, 1, tau_hours=24.0)
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'cluster_state_finalized' in events
        finalize_call = next(c for c in mock_log.call_args_list
                             if c.args[0] == 'cluster_state_finalized')
        assert finalize_call.kwargs['cluster_id'] == 1
        assert finalize_call.kwargs['last_doc_at'] == '2026-04-21T23:30:00Z'
        assert finalize_call.kwargs['doc_count'] == 1


class TestColdStartNoExistingClusters:
    def test_first_item_creates_singleton(self, tmp_db):
        _insert_item(tmp_db, 'alone', platform='x', author='alice', content='solo')
        tmp_db.commit()
        provider = FakeProvider({'solo': [0.5, 0.5, 0.7]})

        # No candidates exist → V2 SHALL skip the LLM and create a singleton.
        # We pass a judge that would explode if called to assert this.
        def boom(item_row, candidates):
            raise AssertionError('LLM SHOULD NOT be called when there are no candidates')

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=boom,
                api_key='k', api_base=None, model='m',
            )

        clusters = tmp_db.execute("SELECT id, doc_count FROM clusters").fetchall()
        assert len(clusters) == 1
        assert clusters[0]['doc_count'] == 1
        r = tmp_db.execute("SELECT cluster_id FROM items WHERE id='alone'").fetchone()
        assert r['cluster_id'] == clusters[0]['id']


class TestRecallTopKClusters:
    """V2 Stage 1: cosine top-K candidate recall (feature-spec R2.1 / R2.2 / R2.3).

    SHALL filter archived=0 / merged_into IS NULL / representative_vector IS NOT NULL /
    last_updated_at within window. SHALL order by cosine DESC and take top K.
    SHALL apply NO 0.85/0.70 threshold cutoff.
    """

    def _seed_cluster(self, conn, cid, vec, *, archived=0, merged_into=None,
                      last_updated_at=None, rep_blob=None, ai_title='T',
                      ai_summary='S', ai_key_points='[]', doc_count=1,
                      first_doc_at=None, last_doc_at=None):
        if rep_blob is None:
            rep_blob = vu.pack_blob(vec)
        first_doc_expr = "datetime('now')"
        first_doc_params = []
        if first_doc_at is not None:
            first_doc_expr = "?"
            first_doc_params.append(first_doc_at)
        last_doc_col = ""
        last_doc_placeholder = ""
        last_doc_params = []
        if last_doc_at is not None:
            last_doc_col = ", last_doc_at"
            last_doc_placeholder = ", ?"
            last_doc_params.append(last_doc_at)
        if last_updated_at is None:
            last_updated_at = "datetime('now', '-1 days')"
            sql = (
                f"INSERT INTO clusters (id, first_doc_at{last_doc_col}, last_updated_at, "
                "archived, merged_into, representative_vector, doc_count, "
                "live_version, ai_title, ai_summary, ai_key_points) "
                f"VALUES (?, {first_doc_expr}{last_doc_placeholder}, {last_updated_at}, "
                "?, ?, ?, ?, 0, ?, ?, ?)"
            )
            conn.execute(
                sql,
                (cid, *first_doc_params, *last_doc_params, archived, merged_into,
                 rep_blob, doc_count, ai_title, ai_summary, ai_key_points),
            )
        else:
            conn.execute(
                f"INSERT INTO clusters (id, first_doc_at{last_doc_col}, last_updated_at, "
                "archived, merged_into, representative_vector, doc_count, "
                "live_version, ai_title, ai_summary, ai_key_points) "
                f"VALUES (?, {first_doc_expr}{last_doc_placeholder}, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (cid, *first_doc_params, *last_doc_params, last_updated_at,
                 archived, merged_into, rep_blob, doc_count, ai_title,
                 ai_summary, ai_key_points),
            )

    def test_returns_top_k_when_more_candidates_than_k(self, tmp_db):
        # 12 candidate clusters with descending cosine vs new_vec=[1,0,0]
        for i in range(12):
            # Vector tilts away from [1,0,0] as i grows.
            theta = i * 0.05
            vx = float(np.cos(theta))
            vy = float(np.sin(theta))
            self._seed_cluster(tmp_db, 100 + i,
                               np.array([vx, vy, 0.0], dtype=np.float32))
        tmp_db.commit()

        new_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        out = pl._recall_top_k_clusters(tmp_db, new_vec, k=10, window_days=30)
        assert len(out) == 10
        cosines = [c['cosine'] for c in out]
        assert cosines == sorted(cosines, reverse=True)
        # Top result must be the cluster whose vector is exactly [1,0,0] (i=0).
        assert out[0]['cluster_id'] == 100
        assert out[0]['cosine'] == pytest.approx(1.0, abs=1e-5)

    def test_returns_all_when_fewer_than_k(self, tmp_db):
        for i in range(5):
            self._seed_cluster(tmp_db, 200 + i,
                               np.array([1.0, i * 0.1, 0.0], dtype=np.float32))
        tmp_db.commit()

        new_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        out = pl._recall_top_k_clusters(tmp_db, new_vec, k=10, window_days=30)
        assert len(out) == 5

    def test_returns_empty_when_no_candidates(self, tmp_db):
        new_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        out = pl._recall_top_k_clusters(tmp_db, new_vec, k=10, window_days=30)
        assert out == []

    def test_excludes_archived(self, tmp_db):
        self._seed_cluster(tmp_db, 300,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32),
                           archived=1)
        self._seed_cluster(tmp_db, 301,
                           np.array([0.9, 0.1, 0.0], dtype=np.float32),
                           archived=0)
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        assert [c['cluster_id'] for c in out] == [301]

    def test_excludes_merged(self, tmp_db):
        # merged_into references another cluster (FK), so seed parent first.
        self._seed_cluster(tmp_db, 399,
                           np.array([0.5, 0.5, 0.0], dtype=np.float32))
        self._seed_cluster(tmp_db, 400,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32),
                           merged_into=399)
        self._seed_cluster(tmp_db, 401,
                           np.array([0.9, 0.1, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        # Cluster 400 (merged) excluded. 399 (parent) and 401 included.
        assert 400 not in [c['cluster_id'] for c in out]
        assert 401 in [c['cluster_id'] for c in out]

    def test_excludes_null_representative_vector(self, tmp_db):
        # representative_vector explicitly NULL — should be filtered out.
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     archived, merged_into,
                                     representative_vector, doc_count,
                                     live_version)
               VALUES (500, datetime('now'), datetime('now'),
                       0, NULL, NULL, 1, 0)"""
        )
        self._seed_cluster(tmp_db, 501,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        assert [c['cluster_id'] for c in out] == [501]

    def test_excludes_outside_window(self, tmp_db):
        self._seed_cluster(tmp_db, 600,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32),
                           last_updated_at='2020-01-01T00:00:00')
        self._seed_cluster(tmp_db, 601,
                           np.array([0.9, 0.1, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        assert [c['cluster_id'] for c in out] == [601]

    def test_cosine_field_populated(self, tmp_db):
        self._seed_cluster(tmp_db, 700,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32))
        self._seed_cluster(tmp_db, 701,
                           np.array([0.0, 1.0, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        assert len(out) == 2
        # Cluster 700 has cosine ~1.0; 701 has cosine ~0.0.
        by_id = {c['cluster_id']: c for c in out}
        assert by_id[700]['cosine'] == pytest.approx(1.0, abs=1e-5)
        assert by_id[701]['cosine'] == pytest.approx(0.0, abs=1e-5)

    def test_no_threshold_cutoff_low_cosine_still_returned(self, tmp_db):
        """V15.1 baseline: cosine_min=0 default → even 0 cosine is in top-K."""
        self._seed_cluster(tmp_db, 800,
                           np.array([0.0, 1.0, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,
        )
        assert len(out) == 1
        assert out[0]['cluster_id'] == 800
        # cosine close to 0 — well below V1 0.70 boundary — must still surface.
        assert out[0]['cosine'] < 0.70

    def test_bf_0428_3_cosine_min_floor_filters_low_cosine(self, tmp_db):
        """BF-0428-3: cosine_min hard floor pre-Stage 2.

        With cosine_min=0.75, only candidates with cosine >= 0.75 pass.
        Candidates below threshold are dropped before LLM judge sees them,
        cutting误合 (Moxt vs HappyHorse) at recall layer.
        """
        # cluster A: parallel to query → cosine=1.0 (passes)
        self._seed_cluster(tmp_db, 850,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32))
        # cluster B: 30° off → cosine=cos(30°)≈0.866 (passes 0.75)
        self._seed_cluster(tmp_db, 851,
                           np.array([0.866, 0.5, 0.0], dtype=np.float32))
        # cluster C: 60° off → cosine=0.5 (filtered out, < 0.75)
        self._seed_cluster(tmp_db, 852,
                           np.array([0.5, 0.866, 0.0], dtype=np.float32))
        # cluster D: orthogonal → cosine=0 (filtered out)
        self._seed_cluster(tmp_db, 853,
                           np.array([0.0, 1.0, 0.0], dtype=np.float32))
        tmp_db.commit()

        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30, cosine_min=0.75,
        )
        # Only 850 and 851 should pass the 0.75 floor.
        ids = [c['cluster_id'] for c in out]
        assert ids == [850, 851]
        assert all(c['cosine'] >= 0.75 for c in out)

    def test_bf_0428_3_cosine_min_zero_default_keeps_all(self, tmp_db):
        """BF-0428-3: default cosine_min=0 preserves V15.1 baseline (no filter)."""
        self._seed_cluster(tmp_db, 860,
                           np.array([1.0, 0.0, 0.0], dtype=np.float32))
        self._seed_cluster(tmp_db, 861,
                           np.array([0.0, 1.0, 0.0], dtype=np.float32))
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, np.array([1.0, 0.0, 0.0], dtype=np.float32),
            k=10, window_days=30,  # cosine_min defaults to 0.0
        )
        # Both should pass (V15.1 baseline behavior unchanged).
        assert len(out) == 2

    def test_returns_metadata_fields_for_stage2(self, tmp_db):
        vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, archived, merged_into,
                                     representative_vector, doc_count,
                                     live_version, ai_title, ai_summary,
                                     ai_key_points)
               VALUES (900, '2026-04-20T08:00:00', '2026-04-25T19:00:00',
                       datetime('now', '-1 days'), 0, NULL, ?, 3, 7,
                       'GPT-5.5 Launch', 'OpenAI 发布 GPT-5.5', '["1M ctx"]')""",
            (vu.pack_blob(vec),),
        )
        tmp_db.commit()
        out = pl._recall_top_k_clusters(
            tmp_db, vec, k=10, window_days=30,
        )
        assert len(out) == 1
        c = out[0]
        assert c['cluster_id'] == 900
        assert c['ai_title'] == 'GPT-5.5 Launch'
        assert c['ai_summary'] == 'OpenAI 发布 GPT-5.5'
        assert c['ai_key_points'] == '["1M ctx"]'
        assert c['doc_count'] == 3
        assert c['live_version'] == 7
        assert c['first_doc_at'] == '2026-04-20T08:00:00'
        assert c['last_doc_at'] == '2026-04-25T19:00:00'

    def test_temporal_adjacency_filters_clusters_by_item_time(self, tmp_db):
        vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self._seed_cluster(
            tmp_db, 910, vec,
            first_doc_at='2026-05-10T00:00:00Z',
            last_doc_at='2026-05-14T00:00:00Z',
        )
        self._seed_cluster(
            tmp_db, 911, vec,
            first_doc_at='2026-05-01T00:00:00Z',
            last_doc_at='2026-05-02T00:00:00Z',
        )
        tmp_db.commit()

        out = pl._recall_top_k_clusters(
            tmp_db, vec, k=10, window_days=30,
            item_time='2026-05-17T00:00:00Z',
            temporal_adjacency_days=3,
            max_merged_span_days=7,
        )

        assert [c['cluster_id'] for c in out] == [910]

    def test_temporal_adjacency_rejects_chain_growth_past_max_span(self, tmp_db):
        vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self._seed_cluster(
            tmp_db, 920, vec,
            first_doc_at='2026-05-10T00:00:00Z',
            last_doc_at='2026-05-15T00:00:00Z',
        )
        tmp_db.commit()

        rejected = pl._recall_top_k_clusters(
            tmp_db, vec, k=10, window_days=30,
            item_time='2026-05-18T00:00:00Z',
            temporal_adjacency_days=3,
            max_merged_span_days=7,
        )
        accepted_without_span_cap = pl._recall_top_k_clusters(
            tmp_db, vec, k=10, window_days=30,
            item_time='2026-05-18T00:00:00Z',
            temporal_adjacency_days=3,
            max_merged_span_days=None,
        )

        assert rejected == []
        assert [c['cluster_id'] for c in accepted_without_span_cap] == [920]

    def test_run_pipeline_does_not_join_time_distant_candidate(self, tmp_db):
        vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_summary,
                                  embedding, cluster_id)
               VALUES ('old', 'x', 'following', '2026-05-01T00:00:00Z',
                       'old event', 'alice', 'old event',
                       '2026-05-01T00:00:00Z', 'old summary', ?, 1)""",
            (vu.pack_blob(vec),),
        )
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, archived, merged_into,
                                     representative_vector, doc_count,
                                     live_version, ai_title, ai_summary,
                                     ai_key_points)
               VALUES (1, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z',
                       datetime('now'), 0, NULL, ?, 1, 0,
                       'old', 'old summary', '[]')""",
            (vu.pack_blob(vec),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) VALUES (1, 'old', 1)"
        )
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, ai_summary,
                                  embedding)
               VALUES ('new', 'x', 'following', '2026-05-12T00:00:00Z',
                       'new event', 'alice', 'new event',
                       '2026-05-12T00:00:00Z', 'new summary', ?)""",
            (vu.pack_blob(vec),),
        )
        tmp_db.commit()

        def judge_yes(_item_row, candidates):
            return {'matches': [
                {'cluster_id': c['cluster_id'], 'same_event': True,
                 'confidence': 'high', 'relationship': 'same_event',
                 'reason': 'test'}
                for c in candidates
            ]}

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=FakeProvider({}), top_k_judge=judge_yes,
                api_key='k', api_base=None, model='m', skip_summary=True,
                temporal_adjacency_days=3, max_merged_span_days=7,
            )

        row = tmp_db.execute("SELECT cluster_id FROM items WHERE id='new'").fetchone()
        assert row['cluster_id'] != 1


class TestStage2Judge:
    """V2 Stage 2: top-K LLM judge + code-layer selection (R3.1 / R3.2 / R3.3).

    Covers _select_cluster_from_matches sorting, run_pipeline integration with
    mocked top_k_judge, and cluster_judge_log writes.
    """

    def _make_candidates(self, *cluster_ids_with_cosine):
        """Helper: build a list of candidate dicts for _select_cluster_from_matches."""
        return [
            {'cluster_id': cid, 'cosine': cos}
            for cid, cos in cluster_ids_with_cosine
        ]

    def _make_match(self, cid, **fields):
        return {
            'cluster_id': cid,
            'same_event': fields.get('same_event', False),
            'confidence': fields.get('confidence', 'low'),
            'relationship': fields.get('relationship', 'unrelated'),
        }

    def test_select_picks_high_over_medium_low(self):
        candidates = self._make_candidates((1, 0.9), (2, 0.85), (3, 0.80))
        matches = [
            self._make_match(1, same_event=True, confidence='medium',
                             relationship='same_event'),
            self._make_match(2, same_event=True, confidence='high',
                             relationship='same_event'),
            self._make_match(3, same_event=True, confidence='low',
                             relationship='same_event'),
        ]
        sel, reason, possible = pl._select_cluster_from_matches(matches, candidates)
        assert sel == 2  # high beats medium beats low
        assert reason == 'top-confidence-match'
        # medium goes into possible_merge_candidates; low (below threshold) excluded.
        assert possible == [1]

    def test_select_low_only_returns_none(self):
        candidates = self._make_candidates((1, 0.9), (2, 0.8))
        matches = [
            self._make_match(1, same_event=True, confidence='low',
                             relationship='same_event'),
            self._make_match(2, same_event=True, confidence='low',
                             relationship='same_event'),
        ]
        sel, reason, possible = pl._select_cluster_from_matches(matches, candidates)
        assert sel is None
        assert reason == 'all-low-confidence'
        assert possible == []

    def test_select_no_same_event_returns_none(self):
        candidates = self._make_candidates((1, 0.95))
        matches = [self._make_match(1, same_event=False, confidence='high',
                                     relationship='same_topic_only')]
        sel, reason, possible = pl._select_cluster_from_matches(matches, candidates)
        assert sel is None
        assert reason == 'no-same-event-match'
        assert possible == []

    def test_select_relationship_directness_tiebreaker(self):
        """Same confidence — same_event > follow_up_update > direct_commentary."""
        candidates = self._make_candidates((1, 0.9), (2, 0.9), (3, 0.9))
        matches = [
            self._make_match(1, same_event=True, confidence='high',
                             relationship='direct_commentary'),
            self._make_match(2, same_event=True, confidence='high',
                             relationship='same_event'),
            self._make_match(3, same_event=True, confidence='high',
                             relationship='follow_up_update'),
        ]
        sel, _reason, possible = pl._select_cluster_from_matches(matches, candidates)
        assert sel == 2  # same_event most direct
        # possible_merge_candidates ordered: follow_up_update > direct_commentary
        assert possible == [3, 1]

    def test_select_cosine_tiebreaker(self):
        """Equal confidence + relationship — higher cosine wins."""
        candidates = self._make_candidates((1, 0.91), (2, 0.97), (3, 0.85))
        matches = [
            self._make_match(1, same_event=True, confidence='high',
                             relationship='same_event'),
            self._make_match(2, same_event=True, confidence='high',
                             relationship='same_event'),
            self._make_match(3, same_event=True, confidence='high',
                             relationship='same_event'),
        ]
        sel, _reason, possible = pl._select_cluster_from_matches(matches, candidates)
        assert sel == 2  # highest cosine
        assert possible == [1, 3]  # 0.91 > 0.85

    def test_run_pipeline_writes_cluster_judge_log_on_success(self, tmp_db):
        """R3.1: Stage 2 success SHALL write a cluster_judge_log row.

        With matches_json containing the LLM raw output."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) "
            "VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'newdoc', platform='reddit', author='bob', content='two')
        tmp_db.commit()

        new_vec = np.array([0.99, 0.01, 0.0], dtype=np.float32)
        provider = FakeProvider({'two': new_vec.tolist()})
        judge = _make_top_k_judge({1: {
            'same_event': True, 'confidence': 'high', 'relationship': 'same_event',
        }})

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='test-model',
            )

        rows = tmp_db.execute(
            "SELECT * FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r['selected_cluster_id'] == 1
        assert r['selection_reason'] == 'top-confidence-match'
        assert r['decision_model'] == 'test-model'
        cand_ids = json.loads(r['candidate_cluster_ids'])
        assert cand_ids == [1]
        matches_json = json.loads(r['matches_json'])
        assert len(matches_json) == 1
        assert matches_json[0]['cluster_id'] == 1
        assert matches_json[0]['same_event'] is True
        assert matches_json[0]['confidence'] == 'high'

    def test_run_pipeline_writes_cluster_judge_log_on_no_match(self, tmp_db):
        """matches[] all same_event=false → singleton, but log row SHALL exist."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) "
            "VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'newdoc', platform='reddit', author='bob', content='two')
        tmp_db.commit()
        provider = FakeProvider({'two': [0.95, 0.1, 0.0]})
        judge = _make_top_k_judge({})  # all same_event=False

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        rows = tmp_db.execute(
            "SELECT selection_reason, selected_cluster_id, possible_merge_candidates "
            "FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['selection_reason'] == 'no-same-event-match'
        assert rows[0]['selected_cluster_id'] is None
        assert json.loads(rows[0]['possible_merge_candidates']) == []

    def test_run_pipeline_llm_failure_writes_fallback_log(self, tmp_db):
        """R3.3: LLM failure SHALL write log with selection_reason
        'llm-failed-fallback-singleton'. SHALL NOT silently return False."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) "
            "VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'newdoc', platform='reddit', author='bob', content='two')
        tmp_db.commit()
        provider = FakeProvider({'two': [0.9, 0.1, 0.0]})
        judge = _make_top_k_judge(
            return_error={'error': 'llm_failed', 'detail': 'simulated timeout'},
        )

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        rows = tmp_db.execute(
            "SELECT selection_reason, selected_cluster_id, matches_json "
            "FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['selection_reason'] == 'llm-failed-fallback-singleton'
        assert rows[0]['selected_cluster_id'] is None
        # matches_json stays NULL on failure (we don't have a parsed matches list).
        assert rows[0]['matches_json'] is None

        # New item still got a cluster (singleton), not joined to 1.
        r = tmp_db.execute("SELECT cluster_id FROM items WHERE id='newdoc'").fetchone()
        assert r['cluster_id'] != 1
        assert r['cluster_id'] is not None

    def test_run_pipeline_llm_uncaught_exception_creates_singleton(self, tmp_db):
        """If top_k_judge raises (we wrapped it), SHALL still write log + singleton."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) "
            "VALUES (1, 'existing', 1)"
        )
        _insert_item(tmp_db, 'newdoc', platform='reddit', author='bob', content='two')
        tmp_db.commit()
        provider = FakeProvider({'two': [0.9, 0.1, 0.0]})
        judge = _make_top_k_judge(raise_exc=RuntimeError('boom'))

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        rows = tmp_db.execute(
            "SELECT selection_reason FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['selection_reason'] == 'llm-failed-fallback-singleton'

    def test_judge_top_k_parser_handles_markdown_fences(self):
        raw = '```json\n{"new_doc_fingerprint": {"subject":"x"},"matches":[]}\n```'
        parsed = pl._parse_top_k_response(raw)
        assert parsed is not None
        assert parsed['matches'] == []

    def test_judge_top_k_parser_rejects_non_dict(self):
        assert pl._parse_top_k_response('[]') is None
        assert pl._parse_top_k_response('null') is None
        assert pl._parse_top_k_response('') is None
        assert pl._parse_top_k_response('not json') is None

    def test_judge_top_k_parser_drops_invalid_match(self):
        raw = json.dumps({
            'new_doc_fingerprint': {},
            'matches': [
                {'cluster_id': 5, 'same_event': True, 'confidence': 'high',
                 'relationship': 'same_event'},
                {'cluster_id': 'not-an-int', 'same_event': True,
                 'confidence': 'high', 'relationship': 'same_event'},
            ],
        })
        parsed = pl._parse_top_k_response(raw)
        assert parsed is not None
        assert len(parsed['matches']) == 1
        assert parsed['matches'][0]['cluster_id'] == 5

    def test_judge_top_k_parser_coerces_invalid_enums(self):
        raw = json.dumps({
            'new_doc_fingerprint': {},
            'matches': [{
                'cluster_id': 9, 'same_event': True,
                'confidence': 'BIZARRE',  # invalid → defaults to 'low'
                'relationship': 'wat',     # invalid → 'unrelated' → forces same_event=False
            }],
        })
        parsed = pl._parse_top_k_response(raw)
        m = parsed['matches'][0]
        assert m['confidence'] == 'low'
        assert m['relationship'] == 'unrelated'
        # Inconsistency was resolved conservatively.
        assert m['same_event'] is False

    def test_run_pipeline_persists_source_identity_and_join_decision_id(self, tmp_db):
        """V2 R8: cluster_items.source_identity SHALL be populated on join."""
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep),),
        )
        # Existing item already has cluster_items row (no source_identity yet).
        _insert_item(tmp_db, 'existing', platform='x', author='alice', content='one')
        tmp_db.execute(
            "UPDATE items SET embedding = ?, cluster_id=1 WHERE id='existing'",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            "INSERT INTO cluster_items (cluster_id, item_id, is_primary_source) "
            "VALUES (1, 'existing', 1)"
        )
        # New doc with a Twitter URL.
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, content,
                                  author_name, title, published_at, url)
               VALUES ('newdoc', 'twitter', 'following', datetime('now'),
                       'two', 'bob', 'tweet title', datetime('now'),
                       'https://x.com/bob/status/12345?ref=share')"""
        )
        tmp_db.commit()
        provider = FakeProvider({'two': [0.99, 0.01, 0.0]})
        judge = _make_top_k_judge({1: {
            'same_event': True, 'confidence': 'high', 'relationship': 'same_event',
        }})

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        row = tmp_db.execute(
            "SELECT source_identity, join_decision_id FROM cluster_items "
            "WHERE item_id='newdoc'"
        ).fetchone()
        assert row is not None
        # Twitter canonical_url collapses ?ref=share — should be stable form.
        assert row['source_identity'] == 'https://x.com/bob/status/12345'
        assert row['join_decision_id'] is not None
        # join_decision_id matches the cluster_judge_log row id we wrote.
        log_row = tmp_db.execute(
            "SELECT id FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchone()
        assert str(log_row['id']) == row['join_decision_id']

    def test_run_pipeline_logs_possible_merge_when_multiple_match(self, tmp_db):
        """When 2+ candidates qualify, possible_merge_candidates SHALL surface."""
        rep1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        rep2 = np.array([0.95, 0.1, 0.0], dtype=np.float32)
        tmp_db.execute(
            """INSERT INTO clusters (id, first_doc_at, last_updated_at,
                                     representative_vector, doc_count, live_version)
               VALUES (1, datetime('now'), datetime('now'), ?, 1, 0),
                      (2, datetime('now'), datetime('now'), ?, 1, 0)""",
            (vu.pack_blob(rep1), vu.pack_blob(rep2)),
        )
        _insert_item(tmp_db, 'newdoc', platform='reddit', author='bob', content='two')
        tmp_db.commit()
        provider = FakeProvider({'two': [0.99, 0.05, 0.0]})

        # Both clusters: same_event=True high; cluster 1 cosine higher → wins.
        judge = _make_top_k_judge({
            1: {'same_event': True, 'confidence': 'high',
                'relationship': 'same_event'},
            2: {'same_event': True, 'confidence': 'high',
                'relationship': 'same_event'},
        })

        with patch.object(pl.summary_writer, 'regenerate_and_swap', return_value=True):
            pl.run_pipeline(
                tmp_db, provider=provider, top_k_judge=judge,
                api_key='k', api_base=None, model='m',
            )

        rows = tmp_db.execute(
            "SELECT selected_cluster_id, possible_merge_candidates "
            "FROM cluster_judge_log WHERE item_id='newdoc'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['selected_cluster_id'] == 1  # higher cosine
        assert json.loads(rows[0]['possible_merge_candidates']) == [2]


class TestSourceIdentity:
    """V2 R8: source_identity priority chain (canonical_url → normalized_url
    → original_url → content_fingerprint → item_id).

    items table currently exposes only `url` and `id`, so we test the actual
    behavior of _compute_source_identity against those columns:
      - Twitter URL → normalize_url canonical form (e.g. x.com/<user>/status/<id>)
      - YouTube URL → normalize_url canonical form (youtube.com/watch?v=<id>)
      - Generic URL → raw url (RSS/HN/Reddit feeds already canonical)
      - No URL → item_id fallback
    """

    def test_twitter_url_normalized_to_canonical(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, url)
               VALUES ('a', 'twitter', 'following', datetime('now'),
                       'https://x.com/alice/status/9988?ref=share')"""
        )
        tmp_db.commit()
        row = tmp_db.execute("SELECT id, url FROM items WHERE id='a'").fetchone()
        ident = pl._compute_source_identity(row)
        assert ident == 'https://x.com/alice/status/9988'

    def test_youtube_url_normalized_to_canonical(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, url)
               VALUES ('y', 'youtube', 'following', datetime('now'),
                       'https://youtu.be/dQw4w9WgXcQ?t=42s')"""
        )
        tmp_db.commit()
        row = tmp_db.execute("SELECT id, url FROM items WHERE id='y'").fetchone()
        ident = pl._compute_source_identity(row)
        assert ident == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

    def test_generic_url_passes_through_raw(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, url)
               VALUES ('h', 'rss', 'following', datetime('now'),
                       'https://example.com/article/123')"""
        )
        tmp_db.commit()
        row = tmp_db.execute("SELECT id, url FROM items WHERE id='h'").fetchone()
        ident = pl._compute_source_identity(row)
        assert ident == 'https://example.com/article/123'

    def test_missing_url_falls_back_to_item_id(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at)
               VALUES ('noid', 'manual', 'following', datetime('now'))"""
        )
        tmp_db.commit()
        row = tmp_db.execute("SELECT id, url FROM items WHERE id='noid'").fetchone()
        ident = pl._compute_source_identity(row)
        assert ident == 'noid'

    def test_blank_url_falls_back_to_item_id(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, url)
               VALUES ('blank', 'manual', 'following', datetime('now'), '   ')"""
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT id, url FROM items WHERE id='blank'"
        ).fetchone()
        ident = pl._compute_source_identity(row)
        assert ident == 'blank'

    def test_singleton_creation_writes_source_identity(self, tmp_db):
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at, url)
               VALUES ('seed', 'rss', 'following', datetime('now'),
                       'https://blog.example.com/post-42')"""
        )
        tmp_db.commit()
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        cid = pl._create_singleton(tmp_db, 'seed', rep, '2026-04-27T10:00:00')
        row = tmp_db.execute(
            "SELECT source_identity FROM cluster_items WHERE item_id='seed'"
        ).fetchone()
        assert row['source_identity'] == 'https://blog.example.com/post-42'
        # And the cluster's unique_source_count was set to 1 by finalize.
        cluster_row = tmp_db.execute(
            "SELECT unique_source_count FROM clusters WHERE id=?", (cid,)
        ).fetchone()
        assert cluster_row['unique_source_count'] == 1


class TestStage4SummaryCandidates:
    """BF-0501-1: summary candidates are not gated only by source count."""

    def _seed_candidate(self, conn, cid, *, ai_category, unique_source_count=1,
                        run_id=7, published_at='2026-05-10T00:10:00Z'):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        conn.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, representative_vector,
                                     doc_count, unique_source_count,
                                     last_touched_run_id)
               VALUES (?, '2026-05-10T00:00:00Z',
                       '2026-05-10T00:00:00Z', datetime('now'), ?,
                       1, ?, ?)""",
            (cid, vu.pack_blob(rep), unique_source_count, run_id),
        )
        iid = f'cand-{cid}'
        conn.execute(
            """INSERT INTO items (id, platform, source, fetched_at, title,
                                  content, author_name, ai_summary,
                                  ai_category, published_at)
               VALUES (?, 'x', 'following', datetime('now'), ?,
                       ?, 'alice', ?, ?, ?)""",
            (iid, f'title {cid}', f'content {cid}', f'summary {cid}',
             ai_category, published_at),
        )
        conn.execute(
            """INSERT INTO cluster_items (cluster_id, item_id,
                                          source_identity, is_primary_source)
               VALUES (?, ?, ?, 1)""",
            (cid, iid, f'https://example.com/{cid}'),
        )
        conn.commit()

    def test_high_value_singleton_requires_summary(self, tmp_db):
        self._seed_candidate(tmp_db, 1, ai_category='products',
                             unique_source_count=1)

        assert pl._clusters_requiring_summary(tmp_db, set(), 7) == [1]

    def test_other_singleton_does_not_require_summary(self, tmp_db):
        self._seed_candidate(tmp_db, 1, ai_category='other',
                             unique_source_count=1)

        assert pl._clusters_requiring_summary(tmp_db, set(), 7) == []

    def test_multi_source_still_requires_summary_even_for_other(self, tmp_db):
        self._seed_candidate(tmp_db, 1, ai_category='other',
                             unique_source_count=2)

        assert pl._clusters_requiring_summary(tmp_db, set(), 7) == [1]

    def test_window_filter_keeps_summary_to_requested_slice(self, tmp_db):
        self._seed_candidate(
            tmp_db, 1, ai_category='products',
            published_at='2026-05-10T00:10:00Z',
        )
        self._seed_candidate(
            tmp_db, 2, ai_category='products',
            published_at='2026-05-10T02:10:00Z',
        )

        assert pl._clusters_requiring_summary(
            tmp_db,
            set(),
            7,
            window_start='2026-05-10T00:00:00Z',
            window_end='2026-05-10T01:00:00Z',
            require_published_at=True,
        ) == [1]


class TestStage3UniqueSourceCount:
    """V2 R4.1 / R8: _finalize_cluster_state recomputes unique_source_count =
    COUNT(DISTINCT source_identity)."""

    def _seed_cluster_with_rep(self, conn, cid):
        rep = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        conn.execute(
            """INSERT INTO clusters (id, first_doc_at, last_doc_at,
                                     last_updated_at, representative_vector,
                                     doc_count, live_version)
               VALUES (?, '2026-04-20T08:00:00', '2026-04-20T08:00:00',
                       datetime('now'), ?, 0, 0)""",
            (cid, vu.pack_blob(rep)),
        )
        return rep

    def test_two_items_same_canonical_url_counts_one(self, tmp_db):
        rep = self._seed_cluster_with_rep(tmp_db, 1)
        # Two Twitter URLs that normalize_url collapses to the same canonical.
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, url, embedding,
                                  cluster_id)
               VALUES ('a1', 'twitter', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'a',
                       'https://x.com/alice/status/123?ref=feed', ?, 1),
                      ('a2', 'twitter', 'following', '2026-04-22T09:00:00',
                       '2026-04-22T08:30:00', 'b',
                       'https://twitter.com/alice/status/123', ?, 1)""",
            (vu.pack_blob(rep), vu.pack_blob(rep)),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, source_identity,
                                          is_primary_source)
               VALUES (1, 'a1', 'https://x.com/alice/status/123', 1),
                      (1, 'a2', 'https://x.com/alice/status/123', 0)"""
        )
        tmp_db.commit()
        pl._finalize_cluster_state(tmp_db, 1, tau_hours=24.0)
        row = tmp_db.execute(
            "SELECT unique_source_count FROM clusters WHERE id=1"
        ).fetchone()
        assert row['unique_source_count'] == 1

    def test_two_items_different_canonical_counts_two(self, tmp_db):
        rep = self._seed_cluster_with_rep(tmp_db, 2)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, url, embedding,
                                  cluster_id)
               VALUES ('b1', 'twitter', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'a',
                       'https://x.com/alice/status/123', ?, 2),
                      ('b2', 'rss', 'following', '2026-04-22T09:00:00',
                       '2026-04-22T08:30:00', 'b',
                       'https://blog.example.com/article-1', ?, 2)""",
            (vu.pack_blob(rep), vu.pack_blob(rep)),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, source_identity,
                                          is_primary_source)
               VALUES (2, 'b1', 'https://x.com/alice/status/123', 1),
                      (2, 'b2', 'https://blog.example.com/article-1', 0)"""
        )
        tmp_db.commit()
        pl._finalize_cluster_state(tmp_db, 2, tau_hours=24.0)
        row = tmp_db.execute(
            "SELECT unique_source_count FROM clusters WHERE id=2"
        ).fetchone()
        assert row['unique_source_count'] == 2

    def test_one_with_url_one_without_counts_two(self, tmp_db):
        """Item without canonical URL falls back to item_id — distinct from URL."""
        rep = self._seed_cluster_with_rep(tmp_db, 3)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, url, embedding,
                                  cluster_id)
               VALUES ('c1', 'twitter', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'a',
                       'https://x.com/alice/status/444', ?, 3),
                      ('c2', 'manual', 'following', '2026-04-22T09:00:00',
                       '2026-04-22T08:30:00', 'b',
                       NULL, ?, 3)""",
            (vu.pack_blob(rep), vu.pack_blob(rep)),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, source_identity,
                                          is_primary_source)
               VALUES (3, 'c1', 'https://x.com/alice/status/444', 1),
                      (3, 'c2', 'c2', 0)"""
        )
        tmp_db.commit()
        pl._finalize_cluster_state(tmp_db, 3, tau_hours=24.0)
        row = tmp_db.execute(
            "SELECT unique_source_count FROM clusters WHERE id=3"
        ).fetchone()
        assert row['unique_source_count'] == 2

    def test_null_source_identity_excluded_from_count(self, tmp_db):
        """source_identity NULL rows are excluded from COUNT(DISTINCT)."""
        rep = self._seed_cluster_with_rep(tmp_db, 4)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, url, embedding,
                                  cluster_id)
               VALUES ('d1', 'rss', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'a', NULL, ?, 4),
                      ('d2', 'rss', 'following', '2026-04-22T09:00:00',
                       '2026-04-22T08:30:00', 'b', NULL, ?, 4)""",
            (vu.pack_blob(rep), vu.pack_blob(rep)),
        )
        # Both rows have source_identity NULL — count is 0.
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, source_identity,
                                          is_primary_source)
               VALUES (4, 'd1', NULL, 1),
                      (4, 'd2', NULL, 0)"""
        )
        tmp_db.commit()
        pl._finalize_cluster_state(tmp_db, 4, tau_hours=24.0)
        row = tmp_db.execute(
            "SELECT unique_source_count FROM clusters WHERE id=4"
        ).fetchone()
        assert row['unique_source_count'] == 0

    def test_finalize_log_carries_unique_source_count(self, tmp_db):
        rep = self._seed_cluster_with_rep(tmp_db, 5)
        tmp_db.execute(
            """INSERT INTO items (id, platform, source, fetched_at,
                                  published_at, content, url, embedding,
                                  cluster_id)
               VALUES ('e1', 'rss', 'following', '2026-04-22T08:00:00',
                       '2026-04-22T07:30:00', 'a', 'https://a.example/p1', ?, 5)""",
            (vu.pack_blob(rep),),
        )
        tmp_db.execute(
            """INSERT INTO cluster_items (cluster_id, item_id, source_identity,
                                          is_primary_source)
               VALUES (5, 'e1', 'https://a.example/p1', 1)"""
        )
        tmp_db.commit()
        from unittest.mock import patch as _patch
        with _patch.object(pl, '_log_event') as mock_log:
            pl._finalize_cluster_state(tmp_db, 5, tau_hours=24.0)
        finalize_call = next(
            c for c in mock_log.call_args_list
            if c.args[0] == 'cluster_state_finalized'
        )
        assert finalize_call.kwargs['unique_source_count'] == 1
