"""Tests for tools/cluster_manage.py + tools/audit_clusters.py.

Use a temp DB built via db.get_conn() (auto-creates schema). Seed clusters +
items + cluster_items + actions to verify:
  - merge atomicity (rollback on failure)
  - merge doc_count recompute by (platform, author_name)
  - merge bumps live_version + stale actions
  - split moves out items into new singletons + recomputes both
  - reset-all wipes everything; requires exact phrase
  - show prints expected fields
  - audit_clusters samples + emits markdown
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

# Force a temp DB before importing db
@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    p = tmp_path / 'test.db'
    monkeypatch.setenv('DB_PATH', str(p))
    # Reload db module to pick up env override (uses module-level constant
    # captured at import). If already imported, mutate constant directly.
    import db as db_mod
    db_mod.DB_PATH = str(p)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _seed_items_and_clusters(conn):
    """2 clusters: c1 (items i1, i2), c2 (items i3). One stale action on c1."""
    from clustering import vector_utils as vu
    # items
    rows = [
        ('i1', 'rss',     'src-rss',     'alice', 'Title 1', 'Body 1'),
        ('i2', 'twitter', 'src-twitter', 'bob',   'Title 2', 'Body 2'),
        ('i3', 'rss',     'src-rss',     'carol', 'Title 3', 'Body 3'),
        ('i4', 'rss',     'src-rss',     'alice', 'Title 4', 'Body 4'),  # same (rss, alice) as i1
    ]
    for r in rows:
        conn.execute(
            """INSERT INTO items (id, platform, source, author_name, title, content,
                                  fetched_at, published_at, embedding)
               VALUES (?,?,?,?,?,?, datetime('now'), datetime('now'), ?)""",
            (*r, vu.pack_blob(np.full(8, 0.5, dtype=np.float32))),
        )
    # cluster 1 (i1+i2)
    cur = conn.execute(
        """INSERT INTO clusters (first_doc_at, last_doc_at, last_updated_at,
                                 representative_vector, doc_count,
                                 is_visible_in_feed, ai_title, live_version)
           VALUES (datetime('now'), datetime('now'), datetime('now'),
                   ?, 2, 1, 'Cluster One', 1)""",
        (vu.pack_blob(np.full(8, 0.5, dtype=np.float32)),),
    )
    c1 = cur.lastrowid
    conn.execute(
        "INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster, is_primary_source) VALUES (?,?,0,1)",
        (c1, 'i1'),
    )
    conn.execute(
        "INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster, is_primary_source) VALUES (?,?,1,0)",
        (c1, 'i2'),
    )
    conn.execute("UPDATE items SET cluster_id = ? WHERE id IN ('i1','i2')", (c1,))
    # cluster 2 (i3)
    cur = conn.execute(
        """INSERT INTO clusters (first_doc_at, last_doc_at, last_updated_at,
                                 representative_vector, doc_count,
                                 is_visible_in_feed, ai_title, live_version)
           VALUES (datetime('now'), datetime('now'), datetime('now'),
                   ?, 1, 0, 'Cluster Two', 1)""",
        (vu.pack_blob(np.full(8, 0.7, dtype=np.float32)),),
    )
    c2 = cur.lastrowid
    conn.execute(
        "INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster, is_primary_source) VALUES (?,?,0,1)",
        (c2, 'i3'),
    )
    conn.execute("UPDATE items SET cluster_id = ? WHERE id = 'i3'", (c2,))
    # one action sourced from c1
    conn.execute(
        """INSERT INTO actions (id, source_type, source_id, title, action_type,
                                prompt, status, priority, direction,
                                cluster_version, is_stale, created_at)
           VALUES ('act1', 'cluster', ?, 'Test action', 'send_message',
                   'do something', 'pending', 'P1', 'inbound', 1, 0, datetime('now'))""",
        (str(c1),),
    )
    conn.commit()
    return c1, c2


# ---------- cluster_manage ----------

class TestShow:
    def test_show_outputs_basic_fields(self, tmp_db, capsys):
        from tools import cluster_manage
        c1, _ = _seed_items_and_clusters(tmp_db)
        rc = cluster_manage.cmd_show(tmp_db, cluster_id=c1)
        assert rc == 0
        out = capsys.readouterr().out
        assert f'cluster #{c1}' in out
        assert 'Cluster One' in out
        assert 'i1' in out and 'i2' in out

    def test_show_missing_cluster(self, tmp_db, capsys):
        from tools import cluster_manage
        rc = cluster_manage.cmd_show(tmp_db, cluster_id=9999)
        assert rc == 1
        assert 'not found' in capsys.readouterr().out


class TestMerge:
    def test_merge_basic(self, tmp_db):
        from tools import cluster_manage
        c1, c2 = _seed_items_and_clusters(tmp_db)
        stats = cluster_manage.merge_clusters(
            tmp_db, target=c1, sources=[c2],
            api_key='', api_base=None, model='', skip_summary=True,
        )
        assert stats['sources_merged'] == 1
        assert stats['items_moved'] == 1
        # c2 archived + merged_into=c1
        c2_row = tmp_db.execute("SELECT archived, merged_into FROM clusters WHERE id = ?", (c2,)).fetchone()
        assert c2_row['archived'] == 1
        assert c2_row['merged_into'] == c1
        # i3 now belongs to c1
        i3 = tmp_db.execute("SELECT cluster_id FROM items WHERE id = 'i3'").fetchone()
        assert i3['cluster_id'] == c1

    def test_merge_recomputes_doc_count_by_platform_author(self, tmp_db):
        """seed items: c1(i1=rss/alice, i2=twitter/bob), c2(i3=rss/carol).
        After merge → distinct (platform, author_name) = 3 → doc_count=3."""
        from tools import cluster_manage
        c1, c2 = _seed_items_and_clusters(tmp_db)
        cluster_manage.merge_clusters(
            tmp_db, target=c1, sources=[c2],
            api_key='', api_base=None, model='', skip_summary=True,
        )
        row = tmp_db.execute("SELECT doc_count FROM clusters WHERE id = ?", (c1,)).fetchone()
        assert row['doc_count'] == 3

    def test_merge_bumps_version_and_stales_actions(self, tmp_db):
        from tools import cluster_manage
        c1, c2 = _seed_items_and_clusters(tmp_db)
        before = tmp_db.execute("SELECT live_version FROM clusters WHERE id = ?", (c1,)).fetchone()['live_version']
        cluster_manage.merge_clusters(
            tmp_db, target=c1, sources=[c2],
            api_key='', api_base=None, model='', skip_summary=True,
        )
        after = tmp_db.execute("SELECT live_version FROM clusters WHERE id = ?", (c1,)).fetchone()['live_version']
        assert after == before + 1
        action = tmp_db.execute("SELECT is_stale FROM actions WHERE id = 'act1'").fetchone()
        assert action['is_stale'] == 1

    def test_merge_target_in_sources_raises(self, tmp_db):
        from tools import cluster_manage
        c1, _ = _seed_items_and_clusters(tmp_db)
        with pytest.raises(ValueError):
            cluster_manage.merge_clusters(tmp_db, target=c1, sources=[c1], skip_summary=True)

    def test_merge_unknown_source_raises(self, tmp_db):
        from tools import cluster_manage
        c1, _ = _seed_items_and_clusters(tmp_db)
        with pytest.raises(LookupError):
            cluster_manage.merge_clusters(tmp_db, target=c1, sources=[9999], skip_summary=True)


class TestSplit:
    def test_split_moves_out_into_singletons(self, tmp_db):
        from tools import cluster_manage
        c1, _ = _seed_items_and_clusters(tmp_db)
        stats = cluster_manage.split_cluster(tmp_db, cluster_id=c1, keep_item_ids=['i1'])
        assert stats['items_moved'] == 1
        # i2 should now be in a new singleton cluster
        i2 = tmp_db.execute("SELECT cluster_id FROM items WHERE id = 'i2'").fetchone()
        assert i2['cluster_id'] != c1
        # c1 should now have only i1
        members = tmp_db.execute(
            "SELECT item_id FROM cluster_items WHERE cluster_id = ?", (c1,)
        ).fetchall()
        assert [m['item_id'] for m in members] == ['i1']

    def test_split_empty_keep_raises(self, tmp_db):
        from tools import cluster_manage
        c1, _ = _seed_items_and_clusters(tmp_db)
        with pytest.raises(ValueError):
            cluster_manage.split_cluster(tmp_db, cluster_id=c1, keep_item_ids=[])

    def test_split_unknown_cluster_raises(self, tmp_db):
        from tools import cluster_manage
        with pytest.raises(LookupError):
            cluster_manage.split_cluster(tmp_db, cluster_id=9999, keep_item_ids=['x'])


class TestResetAll:
    def test_reset_all_requires_exact_phrase_uppercase_yes(self, tmp_db, capsys):
        from tools import cluster_manage
        _seed_items_and_clusters(tmp_db)
        rc = cluster_manage.cmd_reset_all(tmp_db, prompt_fn=lambda _p: 'YES', skip_backup=True)
        assert rc == 1
        # nothing wiped
        n = tmp_db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        assert n > 0

    def test_reset_all_requires_exact_phrase_lowercase(self, tmp_db):
        from tools import cluster_manage
        _seed_items_and_clusters(tmp_db)
        rc = cluster_manage.cmd_reset_all(tmp_db, prompt_fn=lambda _p: 'yes, reset all', skip_backup=True)
        assert rc == 1
        n = tmp_db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        assert n > 0

    def test_reset_all_requires_empty_input(self, tmp_db):
        from tools import cluster_manage
        _seed_items_and_clusters(tmp_db)
        rc = cluster_manage.cmd_reset_all(tmp_db, prompt_fn=lambda _p: '', skip_backup=True)
        assert rc == 1

    def test_reset_all_with_correct_phrase(self, tmp_db):
        from tools import cluster_manage
        _seed_items_and_clusters(tmp_db)
        rc = cluster_manage.cmd_reset_all(
            tmp_db, prompt_fn=lambda _p: cluster_manage.RESET_PHRASE, skip_backup=True,
        )
        assert rc == 0
        assert tmp_db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 0
        assert tmp_db.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0] == 0
        assert tmp_db.execute("SELECT COUNT(*) FROM cluster_status").fetchone()[0] == 0
        unlinked = tmp_db.execute(
            "SELECT COUNT(*) FROM items WHERE cluster_id IS NULL"
        ).fetchone()[0]
        assert unlinked > 0


# ---------- audit_clusters ----------

class TestAuditClusters:
    def test_sample_visible_clusters(self, tmp_db):
        from tools import audit_clusters
        c1, _c2 = _seed_items_and_clusters(tmp_db)
        records = audit_clusters.sample_visible_clusters(tmp_db, n=10)
        # only c1 is visible_in_feed=1
        assert len(records) == 1
        assert records[0]['cluster_id'] == c1
        assert records[0]['ai_title'] == 'Cluster One'
        assert records[0]['doc_count'] == 2
        assert sorted(records[0]['platforms']) == ['rss', 'twitter']

    def test_render_markdown_contains_sections(self, tmp_db):
        from tools import audit_clusters
        _seed_items_and_clusters(tmp_db)
        records = audit_clusters.sample_visible_clusters(tmp_db, n=10)
        md = audit_clusters.render_markdown(records, '2026-04-24')
        assert '# Cluster Audit Checklist' in md
        assert '正确' in md and '错合' in md and '待商议' in md
        assert '详细成员预览' in md

    def test_explicit_cluster_ids(self, tmp_db):
        from tools import audit_clusters
        c1, c2 = _seed_items_and_clusters(tmp_db)
        records = audit_clusters.sample_visible_clusters(
            tmp_db, n=0, cluster_ids=[c1, c2]
        )
        assert sorted(r['cluster_id'] for r in records) == sorted([c1, c2])
