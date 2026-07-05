"""BF-0420-22 深层断言集:验证 set_status 修复在 submit / feed / 幂等 / 并发 /
状态转换等真实触发点上行为正确。不只测 'no crash',要断言 DB 状态、响应结构、
用户隔离、重复调用等业务语义。"""
import json
import os
import sqlite3
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod


def _make_item(**overrides):
    base = dict(
        id='test-1', platform='twitter', source='following',
        title='Test title', content='Test content',
        author_name='alice', author_id='a1', author_avatar='',
        url='https://x.com/alice/status/1', cover_url=None,
        media_json=None, metrics_json='{"likes":10}',
        tags_json=None, lang='en', detail_json=None,
        comments_json=None, ai_summary=None,
        relevance_score=5.0, fetched_at='2026-03-18T00:00:00',
        published_at='2026-03-18T00:00:00',
    )
    base.update(overrides)
    return base


@pytest.fixture()
def migrated_db(monkeypatch, tmp_path):
    """Composite-PK item_status(复现生产 schema),带 2 条 item 和 2 个 user。"""
    db_path = str(tmp_path / 'deep.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    db_mod._item_status_has_user_id = None
    conn = db_mod.get_conn()
    db_mod.batch_upsert(conn, [
        _make_item(id='item-A'),
        _make_item(id='item-B', title='Second'),
    ])
    db_mod.migrate_item_status_add_user_id(conn, 'legacy-user')
    yield conn
    conn.close()
    db_mod._item_status_has_user_id = None


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'route.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    monkeypatch.setenv('JWT_SECRET', 'test-secret-key-12345678901234567890')
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    db_mod._item_status_has_user_id = None
    conn = db_mod.get_conn()
    db_mod.batch_upsert(conn, [_make_item(id='item-A'), _make_item(id='item-B')])
    db_mod.migrate_item_status_add_user_id(conn, 'legacy-user')
    conn.close()
    db_mod._item_status_has_user_id = None

    from fastapi.testclient import TestClient
    from app import app
    if hasattr(app.state, 'limiter'):
        app.state.limiter.enabled = False
    yield TestClient(app)
    db_mod._item_status_has_user_id = None


# ── D1: schema & code path ──

class TestSchemaAndCodePath:
    """深层断言:修复是否以正确的方式短路(不是只 catch 异常)"""

    def test_schema_confirms_composite_pk_only(self, migrated_db):
        """证实 item_status 表确实**只有**复合 PK,没有单 item_id 的 UNIQUE。
        若后续 migration 加了单列 UNIQUE,整个 BF 的假设就变了,这个测试会亮灯。"""
        indexes = migrated_db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='item_status'"
        ).fetchall()
        # 不应有覆盖单 item_id 的 UNIQUE 索引
        for row in indexes:
            if row['sql']:
                assert 'UNIQUE' not in row['sql'].upper() or 'user_id' in row['sql'], \
                    f"发现 UNIQUE 索引不含 user_id: {row['sql']}"
        # 主键必须是 (user_id, item_id) 复合
        pk_cols = [r[1] for r in migrated_db.execute(
            "PRAGMA table_info(item_status)"
        ).fetchall() if r[5] > 0]
        assert sorted(pk_cols) == ['item_id', 'user_id'], f"PK columns = {pk_cols}"

    def test_has_user_id_cache_correct_after_migration(self, migrated_db):
        """_check_item_status_has_user_id cache 机制在 migration 后返回 True。
        若 cache 不 invalidate,set_status 可能走错分支。"""
        assert db_mod._check_item_status_has_user_id(migrated_db) is True

    def test_anon_short_circuits_before_sql(self, migrated_db):
        """匿名调用必须在发 SQL 之前 return,不能依赖 SQL 层报错被 catch。
        用 sqlite3 trace callback 抓所有 SQL,确认无 item_status 写入。"""
        executed = []
        migrated_db.set_trace_callback(executed.append)
        try:
            db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id=None)
        finally:
            migrated_db.set_trace_callback(None)

        # 不应发任何 INSERT 或 UPDATE 到 item_status
        write_sql = [s for s in executed if 'item_status' in s and
                     any(kw in s.upper() for kw in ('INSERT', 'UPDATE'))]
        assert write_sql == [], f"匿名调用不该写 SQL,但执行了: {write_sql}"


# ── D2: user isolation ──

class TestUserIsolation:
    """深层断言:不同 user 的 status 互不干扰(复合 PK 的核心语义)"""

    def test_two_users_independent_starred_state(self, migrated_db):
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='alice')
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='bob')
        rows = migrated_db.execute(
            "SELECT user_id, starred_at FROM item_status WHERE item_id='item-A' ORDER BY user_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]['user_id'] == 'alice' and rows[0]['starred_at']
        assert rows[1]['user_id'] == 'bob' and rows[1]['starred_at']

    def test_user_a_toggle_does_not_touch_user_b(self, migrated_db):
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='alice')
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='bob')
        # alice toggle off
        db_mod.set_status(migrated_db, 'item-A', 'starred', user_id='alice')  # toggle
        alice = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE item_id='item-A' AND user_id='alice'"
        ).fetchone()
        bob = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE item_id='item-A' AND user_id='bob'"
        ).fetchone()
        assert alice['starred_at'] is None, "alice 的 starred 应被清空"
        assert bob['starred_at'] is not None, "bob 的 starred 不应被误触"

    def test_anon_call_does_not_overwrite_existing_user_status(self, migrated_db):
        """关键:匿名 set_status 绝不能误删/误覆盖其他用户已有的 status 记录。"""
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='alice')
        before = migrated_db.execute(
            "SELECT COUNT(*) as c FROM item_status"
        ).fetchone()['c']
        # 匿名调用 10 次,不改变 DB
        for _ in range(10):
            db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id=None)
            db_mod.set_status(migrated_db, 'item-A', 'clicked', user_id=None)
        after_rows = migrated_db.execute(
            "SELECT user_id, starred_at FROM item_status WHERE item_id='item-A'"
        ).fetchall()
        assert len(after_rows) == before
        assert after_rows[0]['user_id'] == 'alice'
        assert after_rows[0]['starred_at'] is not None


# ── D3: toggle & action semantics ──

class TestToggleSemantics:
    """深层断言:toggle / force / clicked 语义在修复后仍正确"""

    def test_starred_toggle_cycle(self, migrated_db):
        """starred 默认是 toggle,第二次调用应清空。"""
        db_mod.set_status(migrated_db, 'item-A', 'starred', user_id='u1')
        s1 = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE user_id='u1'"
        ).fetchone()
        assert s1['starred_at'] is not None

        db_mod.set_status(migrated_db, 'item-A', 'starred', user_id='u1')  # toggle off
        s2 = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE user_id='u1'"
        ).fetchone()
        assert s2['starred_at'] is None

    def test_force_true_always_sets(self, migrated_db):
        """force=True 不做 toggle,始终 set 时间戳。两次调用时间戳应更新。"""
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='u1')
        t1 = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE user_id='u1'"
        ).fetchone()['starred_at']
        time.sleep(0.01)
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='u1')
        t2 = migrated_db.execute(
            "SELECT starred_at FROM item_status WHERE user_id='u1'"
        ).fetchone()['starred_at']
        assert t2 >= t1, "force=True 应始终更新时间戳"

    def test_clicked_always_updates(self, migrated_db):
        db_mod.set_status(migrated_db, 'item-A', 'clicked', user_id='u1')
        db_mod.set_status(migrated_db, 'item-A', 'clicked', user_id='u1')
        rows = migrated_db.execute(
            "SELECT * FROM item_status WHERE user_id='u1' AND item_id='item-A'"
        ).fetchall()
        assert len(rows) == 1, "clicked 调两次不该复制行"
        assert rows[0]['clicked_at'] is not None

    def test_all_four_actions_work_for_same_user_item(self, migrated_db):
        """read / clicked / starred / hidden 四列独立,同 (user, item) 只一行。"""
        db_mod.set_status(migrated_db, 'item-A', 'read', user_id='u1')
        db_mod.set_status(migrated_db, 'item-A', 'clicked', user_id='u1')
        db_mod.set_status(migrated_db, 'item-A', 'starred', force=True, user_id='u1')
        db_mod.set_status(migrated_db, 'item-A', 'hidden', force=True, user_id='u1')
        rows = migrated_db.execute(
            "SELECT read_at, clicked_at, starred_at, hidden_at FROM item_status "
            "WHERE user_id='u1' AND item_id='item-A'"
        ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert all(r[k] is not None for k in ('read_at', 'clicked_at', 'starred_at', 'hidden_at'))


# ── D4: route-level with assertions on body/status/db ──

class TestRouteDeepAssertions:
    """深层断言:路由不只 200,响应体 + DB 行数 + 后续查询一致性"""

    def test_anon_post_status_response_body_structure(self, client):
        resp = client.post('/api/status', json={'item_id': 'item-A', 'action': 'starred'})
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get('ok') is True
        assert 'error' not in body, f"匿名成功响应不应含 error 字段: {body}"

    def test_anon_post_status_invalid_action_still_validates(self, client):
        """回归:修复不应绕过 400 validation。"""
        resp = client.post('/api/status', json={'item_id': 'item-A', 'action': 'INVALID'})
        assert resp.status_code == 400
        assert 'action' in resp.json().get('error', '').lower()

    def test_anon_post_status_empty_item_id_still_400(self, client):
        resp = client.post('/api/status', json={'item_id': '', 'action': 'starred'})
        assert resp.status_code == 400

    def test_anon_repeated_calls_keep_db_clean(self, client):
        """连续 20 次匿名调用,DB 仍应无残留行。"""
        for i in range(20):
            resp = client.post('/api/status', json={'item_id': 'item-A', 'action': 'starred'})
            assert resp.status_code == 200

        conn = db_mod.get_conn()
        count = conn.execute("SELECT COUNT(*) FROM item_status").fetchone()[0]
        conn.close()
        assert count == 0, f"20 次匿名调用后 item_status 应为空,实际 {count} 行"


# ── D5: concurrency smoke ──

class TestConcurrencySmoke:
    """深层断言:多线程并发调用不会互相踩(ON CONFLICT 原子性)"""

    def test_concurrent_mixed_anon_and_user_calls(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / 'concurrent.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        db_mod._item_status_has_user_id = None
        conn = db_mod.get_conn()
        db_mod.batch_upsert(conn, [_make_item(id='item-A')])
        db_mod.migrate_item_status_add_user_id(conn, 'legacy-user')
        conn.close()
        db_mod._item_status_has_user_id = None

        errors = []
        def worker(user_id, n):
            try:
                c = db_mod.get_conn()
                for _ in range(n):
                    db_mod.set_status(c, 'item-A', 'clicked', user_id=user_id)
                c.close()
            except Exception as e:
                errors.append((user_id, str(e)))

        threads = [
            threading.Thread(target=worker, args=('alice', 10)),
            threading.Thread(target=worker, args=('bob', 10)),
            threading.Thread(target=worker, args=(None, 10)),       # anon
            threading.Thread(target=worker, args=('', 10)),          # empty str anon
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发调用出错: {errors}"

        c = db_mod.get_conn()
        rows = c.execute(
            "SELECT user_id, clicked_at FROM item_status WHERE item_id='item-A' ORDER BY user_id"
        ).fetchall()
        c.close()
        # 只应有 alice + bob 两行,不应有 NULL/'' user_id 的行
        user_ids = [r['user_id'] for r in rows]
        assert 'alice' in user_ids and 'bob' in user_ids
        assert None not in user_ids
        assert '' not in user_ids


# ── D6: query correctness after fix ──

class TestQueriesUseUserScope:
    """深层断言:修复不破坏查询对 user_id 的 scoping"""

    def test_query_feed_user_scope_isolates_clicked(self, migrated_db):
        """alice 点击 item-A,bob 查 feed 不应看到 item-A 已读状态。"""
        db_mod.set_status(migrated_db, 'item-A', 'clicked', user_id='alice')

        alice_feed = db_mod.query_feed(migrated_db, user_id='alice')
        bob_feed = db_mod.query_feed(migrated_db, user_id='bob')
        anon_feed = db_mod.query_feed(migrated_db, user_id=None)

        alice_a = next(i for i in alice_feed if i['id'] == 'item-A')
        bob_a = next(i for i in bob_feed if i['id'] == 'item-A')

        assert alice_a['clicked_at'] is not None
        assert bob_a['clicked_at'] is None
        # 匿名 query 不绑 user,拿到全表 LEFT JOIN 结果(可能有任意 user 的状态)
        # 这里不断言匿名查询的具体值,只确认不抛异常
        assert isinstance(anon_feed, list)
