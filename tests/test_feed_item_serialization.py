"""BF-0424-EMB-BLOB regression tests.

After v15.0 added an `embedding BLOB` column to `items`, the
`GET /api/feed/item/{id}` endpoint started returning HTTP 500
(`TypeError: Object of type bytes is not JSON serializable`) for any item
whose embedding had been populated by the clustering pipeline.

The same root cause affected 4 endpoints in `src/routes/submit.py` that all
do `SELECT i.* … → dict(row) → JSONResponse`.

These tests exercise:
  - V1: BLOB embedding item → 200 + JSON body has no `embedding` key
  - V5: NULL embedding item → 200 (boundary, the strip helper must not break the path)
  - submit-history: BLOB embedding manual item → 200 + no `embedding` key

We do NOT spin up the LLM or clustering pipeline; we directly seed an item
with a fake bytes embedding that mimics what the real provider would write.
"""
import os
import struct
import sys
import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod  # noqa: E402

PASSWORD = 'password123'


def _fake_embedding_bytes(dim: int = 1536) -> bytes:
    """Mimic clustering provider output: packed float32 array."""
    return struct.pack(f'{dim}f', *([0.1] * dim))


@pytest.fixture()
def feed_item_env(monkeypatch, tmp_path):
    monkeypatch.setenv('JWT_SECRET', 'feed-item-test-secret-ent-enough-32-char!')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    monkeypatch.setattr(db_mod, 'DB_PATH', str(tmp_path / 'feed_item.db'))
    db_mod._item_status_has_user_id = None

    conn = db_mod.get_conn()
    try:
        user_id = str(uuid.uuid4())
        hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        db_mod.create_user(conn, user_id, 'alice', 'alice@test.local', hashed, role='user')
        db_mod.update_user(conn, user_id, email_verified=1)

        # Item A: has BLOB embedding (the original repro case)
        conn.execute(
            """INSERT INTO items (id, platform, source, title, content, ai_summary,
                                  embedding, embedding_provider,
                                  fetched_at, created_at)
               VALUES ('itm_blob', 'twitter', 'test',
                       'BF-0424 BLOB serialization', 'fake content', 'fake summary',
                       ?, 'fake', datetime('now'), datetime('now'))""",
            (_fake_embedding_bytes(),),
        )

        # Item B: NULL embedding (boundary case — strip helper must be safe)
        conn.execute(
            """INSERT INTO items (id, platform, source, title, content, ai_summary,
                                  fetched_at, created_at)
               VALUES ('itm_null', 'twitter', 'test',
                       'BF-0424 NULL embedding boundary', 'no embedding', 'no summary',
                       datetime('now'), datetime('now'))"""
        )

        # Item C: manual platform with BLOB embedding (covers /api/submit-history
        # and /api/submit-status DB fallback path)
        conn.execute(
            """INSERT INTO items (id, user_id, platform, source, title, content, ai_summary,
                                  embedding, embedding_provider,
                                  fetched_at, created_at)
               VALUES ('itm_manual_blob', ?, 'manual', 'submit',
                       'BF-0424 manual blob', 'manual content', 'manual summary',
                       ?, 'fake', datetime('now'), datetime('now'))""",
            (user_id, _fake_embedding_bytes(),),
        )
        conn.commit()
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route
    monkeypatch.setattr(auth_route, 'JWT_SECRET', 'feed-item-test-secret-ent-enough-32-char!')
    monkeypatch.setattr(auth_mw, '_AUTH_TOKEN', '')
    app_mod.app.state.limiter.enabled = False

    return {'app': app_mod.app, 'user_id': user_id}


def _client(app) -> TestClient:
    c = TestClient(app)
    resp = c.post('/api/auth/login', json={'login': 'alice@test.local', 'password': PASSWORD})
    assert resp.status_code == 200, resp.text
    return c


class TestFeedItemSerialization:
    def test_v1_blob_embedding_item_returns_200_without_embedding_field(self, feed_item_env):
        """The original repro: item with BLOB embedding → was 500, now 200,
        and the response JSON must not contain the `embedding` field."""
        c = _client(feed_item_env['app'])
        r = c.get('/api/feed/item/itm_blob')
        assert r.status_code == 200, f'expected 200, got {r.status_code}: {r.text[:300]}'
        body = r.json()
        assert body['id'] == 'itm_blob'
        assert body['title'] == 'BF-0424 BLOB serialization'
        # Critical: embedding BLOB must be stripped (not serialized as base64,
        # not present at all). Frontend never reads it.
        assert 'embedding' not in body, (
            f'embedding key should be stripped from response, got keys: {list(body.keys())}'
        )
        # embedding_provider is TEXT (JSON-safe) — currently kept for any future
        # "served by which provider" debug surface; this assertion documents
        # current behavior so future strip-list changes are intentional.
        assert 'embedding_provider' in body

    def test_v5_null_embedding_item_still_returns_200(self, feed_item_env):
        """Boundary: items with NULL embedding (the entire pre-v15 corpus) must
        keep working. The strip helper is a no-op when the column is NULL."""
        c = _client(feed_item_env['app'])
        r = c.get('/api/feed/item/itm_null')
        assert r.status_code == 200, f'expected 200, got {r.status_code}: {r.text[:300]}'
        body = r.json()
        assert body['id'] == 'itm_null'
        assert 'embedding' not in body  # also stripped (was None anyway)

    def test_submit_history_strips_blob_for_manual_items(self, feed_item_env):
        """`/api/submit-history` returns user's manual items via SELECT i.*. Same
        BLOB serialization risk as feed item endpoint."""
        c = _client(feed_item_env['app'])
        r = c.get('/api/submit-history')
        assert r.status_code == 200, f'expected 200, got {r.status_code}: {r.text[:300]}'
        body = r.json()
        assert body['ok'] is True
        assert len(body['items']) >= 1
        manual_item = next(it for it in body['items'] if it['id'] == 'itm_manual_blob')
        assert 'embedding' not in manual_item, (
            f'embedding must be stripped from submit-history response, '
            f'got keys: {list(manual_item.keys())}'
        )


class TestStripBlobColumnsHelper:
    """Direct unit tests for the helper itself."""

    def test_strips_embedding_when_present(self):
        item = {'id': 'x', 'title': 't', 'embedding': b'\x00\x01\x02'}
        out = db_mod.strip_blob_columns(item)
        assert 'embedding' not in out
        assert out['id'] == 'x'

    def test_noop_when_embedding_absent(self):
        item = {'id': 'x', 'title': 't'}
        out = db_mod.strip_blob_columns(item)
        assert out == {'id': 'x', 'title': 't'}

    def test_noop_when_embedding_is_none(self):
        item = {'id': 'x', 'title': 't', 'embedding': None}
        out = db_mod.strip_blob_columns(item)
        # None is JSON-safe but we still strip to keep payload clean
        assert 'embedding' not in out

    def test_safe_on_non_dict(self):
        assert db_mod.strip_blob_columns(None) is None
        assert db_mod.strip_blob_columns('foo') == 'foo'
