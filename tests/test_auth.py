"""Tests for P2 user auth system: auth endpoints, admin, user settings, per-user isolation."""
import hashlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import db as db_mod


@pytest.fixture(autouse=True)
def _reset_test_state(monkeypatch):
    """Disable rate limiting and reset DB caches for test isolation."""
    monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
    # Reset cached DB schema check so each test gets fresh state
    db_mod._item_status_has_user_id = None
    try:
        from app import app
        app.state.limiter._default_limits = []
        app.state.limiter.enabled = False
    except Exception:
        pass


# ── Password hashing helper ──
# Use bcrypt directly instead of passlib to avoid version compat issues
import bcrypt as _bcrypt


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()


def _verify_password(password: str, pw_hash: str) -> bool:
    return _bcrypt.checkpw(password.encode(), pw_hash.encode())


# ── Fixtures ──

@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """Provide a fresh SQLite DB in a temp directory."""
    db_path = str(tmp_path / 'test_auth.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _make_item(**overrides):
    base = dict(
        id='item-1', platform='twitter', source='following',
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


def _create_admin(conn, username='admin', email='admin@test.com'):
    user_id = str(uuid.uuid4())
    pw_hash = _hash_password('password123')
    db_mod.create_user(conn, user_id, username, email, pw_hash, role='admin')
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


def _create_user(conn, username='user1', email='user1@test.com'):
    user_id = str(uuid.uuid4())
    pw_hash = _hash_password('password123')
    db_mod.create_user(conn, user_id, username, email, pw_hash, role='user')
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


# ── User CRUD ──

class TestUserCRUD:
    def test_create_and_get_user(self, tmp_db):
        uid = _create_admin(tmp_db)
        user = db_mod.get_user(tmp_db, uid)
        assert user is not None
        assert user['username'] == 'admin'
        assert user['role'] == 'admin'

    def test_get_user_by_login_email(self, tmp_db):
        _create_admin(tmp_db)
        user = db_mod.get_user_by_login(tmp_db, 'admin@test.com')
        assert user is not None
        assert user['username'] == 'admin'

    def test_get_user_by_login_username(self, tmp_db):
        _create_admin(tmp_db)
        user = db_mod.get_user_by_login(tmp_db, 'admin')
        assert user is not None
        assert user['email'] == 'admin@test.com'

    def test_get_user_by_username(self, tmp_db):
        _create_admin(tmp_db)
        user = db_mod.get_user_by_username(tmp_db, 'admin')
        assert user is not None

    def test_get_user_by_email(self, tmp_db):
        _create_admin(tmp_db)
        user = db_mod.get_user_by_email(tmp_db, 'admin@test.com')
        assert user is not None

    def test_update_user(self, tmp_db):
        uid = _create_admin(tmp_db)
        db_mod.update_user(tmp_db, uid, last_login_at='2026-03-30T12:00:00')
        user = db_mod.get_user(tmp_db, uid)
        assert user['last_login_at'] == '2026-03-30T12:00:00'

    def test_list_users(self, tmp_db):
        _create_admin(tmp_db)
        _create_user(tmp_db)
        users = db_mod.list_users(tmp_db)
        assert len(users) == 2

    def test_duplicate_username_fails(self, tmp_db):
        _create_admin(tmp_db)
        with pytest.raises(Exception):
            _create_admin(tmp_db, username='admin', email='other@test.com')

    def test_duplicate_email_fails(self, tmp_db):
        _create_admin(tmp_db)
        with pytest.raises(Exception):
            _create_admin(tmp_db, username='other', email='admin@test.com')

    def test_get_nonexistent_user(self, tmp_db):
        assert db_mod.get_user(tmp_db, 'nonexistent') is None


# ── Invite codes ──

class TestInviteCodes:
    def test_create_and_get(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.create_invite_code(tmp_db, 'TESTCODE', admin_id, max_uses=3)
        code = db_mod.get_invite_code(tmp_db, 'TESTCODE')
        assert code is not None
        assert code['max_uses'] == 3
        assert code['used_count'] == 0

    def test_use_invite_code(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.create_invite_code(tmp_db, 'USE1', admin_id, max_uses=1)
        user_id = _create_user(tmp_db)
        assert db_mod.use_invite_code(tmp_db, 'USE1', user_id) is True
        code = db_mod.get_invite_code(tmp_db, 'USE1')
        assert code['used_count'] == 1

    def test_use_invite_code_does_not_exceed_max_uses(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.create_invite_code(tmp_db, 'LIMIT1', admin_id, max_uses=1)
        user_id = _create_user(tmp_db)
        other_user_id = _create_user(tmp_db, username='user2', email='user2@test.com')
        assert db_mod.use_invite_code(tmp_db, 'LIMIT1', user_id) is True
        assert db_mod.use_invite_code(tmp_db, 'LIMIT1', other_user_id) is False
        code = db_mod.get_invite_code(tmp_db, 'LIMIT1')
        assert code['used_count'] == 1

    def test_list_invite_codes(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.create_invite_code(tmp_db, 'CODE1', admin_id)
        db_mod.create_invite_code(tmp_db, 'CODE2', admin_id)
        codes = db_mod.list_invite_codes(tmp_db)
        assert len(codes) == 2

    def test_delete_invite_code(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.create_invite_code(tmp_db, 'DEL1', admin_id)
        db_mod.delete_invite_code(tmp_db, 'DEL1')
        assert db_mod.get_invite_code(tmp_db, 'DEL1') is None

    def test_get_nonexistent_code(self, tmp_db):
        assert db_mod.get_invite_code(tmp_db, 'NOPE') is None


# ── Sessions ──

class TestSessions:
    def test_create_and_get_session(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        jti = str(uuid.uuid4())
        db_mod.create_session(tmp_db, jti, admin_id, 'access', '2026-04-01T00:00:00')
        session = db_mod.get_session(tmp_db, jti)
        assert session is not None
        assert session['user_id'] == admin_id
        assert session['token_type'] == 'access'

    def test_delete_user_sessions(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        jti1 = str(uuid.uuid4())
        jti2 = str(uuid.uuid4())
        db_mod.create_session(tmp_db, jti1, admin_id, 'access', '2026-04-01T00:00:00')
        db_mod.create_session(tmp_db, jti2, admin_id, 'refresh', '2026-04-07T00:00:00')
        db_mod.delete_user_sessions(tmp_db, admin_id)
        assert db_mod.get_session(tmp_db, jti1) is None
        assert db_mod.get_session(tmp_db, jti2) is None

    def test_cleanup_expired_sessions(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        jti_expired = str(uuid.uuid4())
        jti_valid = str(uuid.uuid4())
        db_mod.create_session(tmp_db, jti_expired, admin_id, 'access', '2020-01-01T00:00:00')
        db_mod.create_session(tmp_db, jti_valid, admin_id, 'access', '2030-01-01T00:00:00')
        db_mod.cleanup_expired_sessions(tmp_db)
        assert db_mod.get_session(tmp_db, jti_expired) is None
        assert db_mod.get_session(tmp_db, jti_valid) is not None


# ── JWT token utilities ──

class TestJWT:
    def test_issue_and_decode(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        from routes.auth import _issue_tokens, decode_access_token
        access, refresh = _issue_tokens(admin_id, 'admin')
        payload = decode_access_token(access)
        assert payload is not None
        assert payload['sub'] == admin_id
        assert payload['role'] == 'admin'

    def test_refresh_token_rejected_as_access(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        from routes.auth import _issue_tokens, decode_access_token
        _, refresh = _issue_tokens(admin_id, 'admin')
        # refresh token has type='refresh', should be rejected by decode_access_token
        payload = decode_access_token(refresh)
        assert payload is None

    def test_invalid_token_returns_none(self):
        from routes.auth import decode_access_token
        assert decode_access_token('garbage.token.here') is None
        assert decode_access_token('') is None


# ── Password hashing ──

class TestPassword:
    def test_bcrypt_verify(self):
        pw_hash = _hash_password('mypassword')
        assert _verify_password('mypassword', pw_hash)
        assert not _verify_password('wrongpassword', pw_hash)


# ── Crypto (encrypt/decrypt) ──

class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        # Set a test encryption key (32 bytes = 64 hex chars)
        test_key = 'a' * 64
        monkeypatch.setenv('ENCRYPTION_KEY', test_key)
        # Force reimport to pick up env var
        from utils import crypto
        import importlib
        importlib.reload(crypto)

        plaintext = 'my-secret-discord-token-12345'
        encrypted = crypto.encrypt(plaintext)
        assert encrypted != plaintext
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == plaintext

    def test_mask_token(self):
        from utils.crypto import mask_token
        assert mask_token('abcdefghijklmnop') == 'abcdef***mnop'
        assert mask_token('short') == '***'

    def test_encrypt_without_key_raises(self, monkeypatch):
        monkeypatch.delenv('ENCRYPTION_KEY', raising=False)
        from utils import crypto
        import importlib
        importlib.reload(crypto)
        with pytest.raises(RuntimeError):
            crypto.encrypt('test')


# ── item_status migration & per-user isolation ──

class TestItemStatusMigration:
    def test_migration_adds_user_id_column(self, tmp_db):
        # Before migration: no user_id column
        cols_before = [r[1] for r in tmp_db.execute("PRAGMA table_info(item_status)").fetchall()]
        assert 'user_id' not in cols_before

        admin_id = _create_admin(tmp_db)
        db_mod.migrate_item_status_add_user_id(tmp_db, admin_id)

        cols_after = [r[1] for r in tmp_db.execute("PRAGMA table_info(item_status)").fetchall()]
        assert 'user_id' in cols_after

    def test_migration_preserves_existing_data(self, tmp_db):
        # Insert items and set status before migration
        db_mod.batch_upsert(tmp_db, [_make_item(id='m-1'), _make_item(id='m-2')])
        db_mod.set_status(tmp_db, 'm-1', 'starred')
        db_mod.set_status(tmp_db, 'm-2', 'clicked')

        admin_id = _create_admin(tmp_db)
        db_mod.migrate_item_status_add_user_id(tmp_db, admin_id)

        # All existing data should be assigned to admin
        rows = tmp_db.execute("SELECT * FROM item_status WHERE user_id = ?", (admin_id,)).fetchall()
        assert len(rows) == 2

    def test_migration_idempotent(self, tmp_db):
        admin_id = _create_admin(tmp_db)
        db_mod.migrate_item_status_add_user_id(tmp_db, admin_id)
        # Running again should be a no-op
        db_mod.migrate_item_status_add_user_id(tmp_db, admin_id)
        cols = [r[1] for r in tmp_db.execute("PRAGMA table_info(item_status)").fetchall()]
        assert 'user_id' in cols


class TestPerUserIsolation:
    @pytest.fixture()
    def migrated_db(self, tmp_db):
        """DB with migration applied and test items."""
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='iso-1'),
            _make_item(id='iso-2'),
            _make_item(id='iso-3'),
        ])
        admin_id = _create_admin(tmp_db)
        db_mod.migrate_item_status_add_user_id(tmp_db, admin_id)
        return tmp_db, admin_id

    def test_set_status_per_user(self, migrated_db):
        conn, admin_id = migrated_db
        user_id = _create_user(conn)

        db_mod.set_status(conn, 'iso-1', 'starred', user_id=admin_id)
        db_mod.set_status(conn, 'iso-2', 'starred', user_id=user_id)

        # Each user should only see their own starred items
        admin_starred = db_mod.query_feed(conn, starred=True, user_id=admin_id)
        user_starred = db_mod.query_feed(conn, starred=True, user_id=user_id)
        assert len(admin_starred) == 1
        assert admin_starred[0]['id'] == 'iso-1'
        assert len(user_starred) == 1
        assert user_starred[0]['id'] == 'iso-2'

    def test_unread_per_user(self, migrated_db):
        conn, admin_id = migrated_db
        user_id = _create_user(conn)

        # Admin clicks iso-1
        db_mod.set_status(conn, 'iso-1', 'clicked', user_id=admin_id)

        # Admin has 2 unread, user has 3 unread
        admin_unread = db_mod.query_feed(conn, unread=True, user_id=admin_id)
        user_unread = db_mod.query_feed(conn, unread=True, user_id=user_id)
        assert len(admin_unread) == 2
        assert len(user_unread) == 3

    def test_toggle_star_per_user(self, migrated_db):
        conn, admin_id = migrated_db
        user_id = _create_user(conn)

        # Star, then unstar for user
        db_mod.set_status(conn, 'iso-1', 'starred', user_id=user_id)
        starred = db_mod.query_feed(conn, starred=True, user_id=user_id)
        assert len(starred) == 1

        db_mod.set_status(conn, 'iso-1', 'starred', user_id=user_id)  # toggle off
        starred = db_mod.query_feed(conn, starred=True, user_id=user_id)
        assert len(starred) == 0

    def test_stats_per_user(self, migrated_db):
        conn, admin_id = migrated_db
        user_id = _create_user(conn)

        db_mod.set_status(conn, 'iso-1', 'clicked', user_id=admin_id)
        db_mod.set_status(conn, 'iso-2', 'clicked', user_id=admin_id)

        admin_stats = db_mod.get_stats(conn, user_id=admin_id)
        user_stats = db_mod.get_stats(conn, user_id=user_id)
        assert admin_stats['twitter']['unread'] == 1
        assert user_stats['twitter']['unread'] == 3

    def test_same_item_different_status_per_user(self, migrated_db):
        conn, admin_id = migrated_db
        user_id = _create_user(conn)

        # Admin stars iso-1, user clicks iso-1
        db_mod.set_status(conn, 'iso-1', 'starred', user_id=admin_id)
        db_mod.set_status(conn, 'iso-1', 'clicked', user_id=user_id)

        admin_items = db_mod.query_feed(conn, user_id=admin_id)
        user_items = db_mod.query_feed(conn, user_id=user_id)

        admin_iso1 = next(i for i in admin_items if i['id'] == 'iso-1')
        user_iso1 = next(i for i in user_items if i['id'] == 'iso-1')

        assert admin_iso1['starred_at'] is not None
        assert admin_iso1['clicked_at'] is None
        assert user_iso1['starred_at'] is None
        assert user_iso1['clicked_at'] is not None


# ── FastAPI endpoint integration (using TestClient) ──

class TestAuthEndpoints:
    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        """FastAPI TestClient with fresh DB."""
        db_path = str(tmp_path / 'test_api.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.delenv('AUTH_TOKEN', raising=False)

        # Initialize DB
        conn = db_mod.get_conn()
        conn.close()

        from fastapi.testclient import TestClient
        from app import app
        # Disable rate limiting in tests
        app.state.limiter.enabled = False
        return TestClient(app)

    def _setup_admin_and_invite(self, code='TESTINV1'):
        """Create admin + invite code, return admin_id."""
        conn = db_mod.get_conn()
        admin_id = _create_admin(conn)
        db_mod.create_invite_code(conn, code, admin_id)
        conn.close()
        return admin_id

    def test_register_with_valid_invite(self, client):
        self._setup_admin_and_invite()
        resp = client.post('/api/auth/register', json={
            'username': 'newuser',
            'email': 'new@test.com',
            'password': 'password123',
            'invite_code': 'TESTINV1',
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['verify_email'] is True
        assert data['email'] == 'new@test.com'
        # Should NOT have auth cookies (not logged in until verified)
        assert 'access_token' not in resp.cookies

    def test_register_remote_creates_user_and_invite_in_one_transaction(self, client, monkeypatch):
        import routes.auth as auth_route

        calls = []
        monkeypatch.setattr(auth_route.remote_db, 'app_state_to_remote', lambda: True)
        monkeypatch.setattr(
            auth_route.remote_db,
            'get_invite_code_remote',
            lambda code: {'code': code, 'used_count': 0, 'max_uses': 1, 'expires_at': None},
        )
        monkeypatch.setattr(auth_route.remote_db, 'get_user_by_username_remote', lambda username: None)
        monkeypatch.setattr(auth_route.remote_db, 'get_user_by_email_remote', lambda email: None)
        monkeypatch.setattr(auth_route, 'send_verification_code', lambda *args, **kwargs: True)

        def _create_user_with_invite(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        monkeypatch.setattr(auth_route.remote_db, 'create_user_with_invite_remote', _create_user_with_invite)
        monkeypatch.setattr(
            auth_route.remote_db,
            'use_invite_code_remote',
            lambda *args, **kwargs: pytest.fail('remote register should use transactional helper'),
        )

        resp = client.post('/api/auth/register', json={
            'username': 'remoteuser',
            'email': 'remote@test.com',
            'password': 'password123',
            'invite_code': 'REMOTE01',
        })

        assert resp.status_code == 200
        assert calls
        args, _ = calls[0]
        assert args[1] == 'remoteuser'
        assert args[2] == 'remote@test.com'
        assert args[4] == 'REMOTE01'

    def test_register_local_creates_user_and_invite_in_one_transaction(self, client, monkeypatch):
        import routes.auth as auth_route

        self._setup_admin_and_invite('LOCAL001')
        calls = []
        original = auth_route.db.create_user_with_invite

        def _create_user_with_invite(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(auth_route.db, 'create_user_with_invite', _create_user_with_invite)
        monkeypatch.setattr(
            auth_route.db,
            'use_invite_code',
            lambda *args, **kwargs: pytest.fail('local register should use transactional helper'),
        )

        resp = client.post('/api/auth/register', json={
            'username': 'localuser',
            'email': 'local@test.com',
            'password': 'password123',
            'invite_code': 'LOCAL001',
        })

        assert resp.status_code == 200
        assert calls
        args, _ = calls[0]
        assert args[2] == 'localuser'
        assert args[3] == 'local@test.com'
        assert args[5] == 'LOCAL001'

    def test_register_then_verify_email(self, client):
        self._setup_admin_and_invite()
        # Register
        client.post('/api/auth/register', json={
            'username': 'verifyuser',
            'email': 'verify@test.com',
            'password': 'password123',
            'invite_code': 'TESTINV1',
        })
        # Get code from DB
        import db as _db
        conn = _db.get_conn()
        user = _db.get_user_by_email(conn, 'verify@test.com')
        code = user['verification_code']
        conn.close()
        # Verify
        resp = client.post('/api/auth/verify-email', json={
            'email': 'verify@test.com',
            'code': code,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['user']['username'] == 'verifyuser'
        assert data['user']['onboarding_completed'] is False
        assert 'access_token' in resp.cookies
        me_resp = client.get('/api/auth/me')
        assert me_resp.status_code == 200
        assert me_resp.json()['onboarding_completed'] is False
        conn = _db.get_conn()
        profile = _db.get_user_profile(conn, user['id'])
        conn.close()
        assert profile is not None
        assert profile['onboarding_completed'] == 0

    def test_verify_email_remote_initializes_onboarding_profile(self, client, monkeypatch):
        from routes import auth as auth_route

        user = {
            'id': str(uuid.uuid4()),
            'username': 'remoteverify',
            'email': 'remoteverify@test.com',
            'role': 'user',
            'discord_bot_token_enc': None,
            'email_verified': 0,
            'verification_code': '123456',
            'verification_code_expires': '2999-01-01T00:00:00+00:00',
        }
        calls = {}

        def _upsert_profile(user_id, **fields):
            calls['profile'] = (user_id, fields)
            return {'onboarding_completed': 0}

        monkeypatch.setattr(auth_route.remote_db, 'app_state_to_remote', lambda: True)
        monkeypatch.setattr(auth_route.remote_db, 'get_user_by_email_remote', lambda email: user)
        monkeypatch.setattr(
            auth_route.remote_db,
            'update_user_remote',
            lambda user_id, **fields: calls.setdefault('update', (user_id, fields)),
        )
        monkeypatch.setattr(auth_route.remote_db, 'upsert_user_profile_remote', _upsert_profile)
        monkeypatch.setattr(
            auth_route.remote_db,
            'create_sessions_remote',
            lambda sessions: calls.setdefault('sessions', sessions),
        )

        resp = client.post('/api/auth/verify-email', json={
            'email': 'remoteverify@test.com',
            'code': '123456',
        })

        assert resp.status_code == 200
        assert resp.json()['user']['onboarding_completed'] is False
        assert calls['profile'][0] == user['id']
        assert calls['profile'][1] == {'onboarding_completed': False}
        assert calls['update'][1]['email_verified'] == 1
        assert calls['update'][1]['verification_code'] is None

    def test_login_remote_db_error_returns_503(self, client, monkeypatch):
        from routes import auth as auth_route

        monkeypatch.setattr(auth_route.remote_db, 'app_state_to_remote', lambda: True)

        def _raise_remote_error(_login):
            raise auth_route.remote_db.RemoteDBError('pool checkout timeout')

        monkeypatch.setattr(auth_route.remote_db, 'get_user_by_login_remote', _raise_remote_error)

        resp = client.post('/api/auth/login', json={
            'login': 'remote@test.com',
            'password': 'password123',
        })

        assert resp.status_code == 503
        assert '暂时不可用' in resp.json()['error']

    def test_login_unverified_email_returns_403(self, client):
        self._setup_admin_and_invite()
        # Register (unverified)
        client.post('/api/auth/register', json={
            'username': 'unverified',
            'email': 'unverified@test.com',
            'password': 'password123',
            'invite_code': 'TESTINV1',
        })
        # Try login
        resp = client.post('/api/auth/login', json={
            'login': 'unverified',
            'password': 'password123',
        })
        assert resp.status_code == 403
        assert resp.json()['verify_email'] is True

    def test_register_with_bad_invite(self, client):
        resp = client.post('/api/auth/register', json={
            'username': 'newuser',
            'email': 'new@test.com',
            'password': 'password123',
            'invite_code': 'BADCODE1',
        })
        assert resp.status_code == 400

    def test_register_short_password(self, client):
        self._setup_admin_and_invite()
        resp = client.post('/api/auth/register', json={
            'username': 'newuser',
            'email': 'new@test.com',
            'password': '123',
            'invite_code': 'TESTINV1',
        })
        assert resp.status_code == 400
        assert '密码' in resp.json()['error'] or 'Password' in resp.json()['error']

    def test_login_and_me(self, client):
        self._setup_admin_and_invite()
        # Login as admin
        resp = client.post('/api/auth/login', json={
            'login': 'admin',
            'password': 'password123',
        })
        assert resp.status_code == 200
        assert resp.json()['ok'] is True
        assert 'access_token' in resp.cookies

        # GET /api/auth/me with cookie
        me_resp = client.get('/api/auth/me')
        assert me_resp.status_code == 200
        assert me_resp.json()['username'] == 'admin'
        assert me_resp.json()['role'] == 'admin'

    def test_login_wrong_password(self, client):
        self._setup_admin_and_invite()
        resp = client.post('/api/auth/login', json={
            'login': 'admin',
            'password': 'wrongpassword',
        })
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post('/api/auth/login', json={
            'login': 'nobody',
            'password': 'password123',
        })
        assert resp.status_code == 401

    def test_logout_clears_cookies(self, client):
        self._setup_admin_and_invite()
        # Login first
        client.post('/api/auth/login', json={
            'login': 'admin', 'password': 'password123',
        })
        # Logout
        resp = client.post('/api/auth/logout')
        assert resp.status_code == 200
        # After logout, /api/auth/me should 401
        me_resp = client.get('/api/auth/me')
        assert me_resp.status_code == 401

    def test_me_unauthenticated(self, client):
        resp = client.get('/api/auth/me')
        assert resp.status_code == 401
        assert resp.json()['can_refresh'] is False

    def test_google_oauth_reserved(self, client):
        resp = client.get('/api/auth/google')
        assert resp.status_code == 501


class TestAdminEndpoints:
    @pytest.fixture()
    def admin_client(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / 'test_admin_api.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.delenv('AUTH_TOKEN', raising=False)
        conn = db_mod.get_conn()
        conn.close()

        from fastapi.testclient import TestClient
        from app import app
        app.state.limiter.enabled = False
        client = TestClient(app)

        # Create admin and login
        conn = db_mod.get_conn()
        _create_admin(conn)
        conn.close()
        client.post('/api/auth/login', json={'login': 'admin', 'password': 'password123'})
        return client

    def test_create_invite_codes(self, admin_client):
        resp = admin_client.post('/api/admin/invite-codes', json={'count': 3, 'max_uses': 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert len(data['codes']) == 3
        conn = db_mod.get_conn()
        try:
            created = [db_mod.get_invite_code(conn, code) for code in data['codes']]
        finally:
            conn.close()
        assert all(code['max_uses'] == 2 for code in created)

    def test_create_invite_codes_rejects_invalid_count(self, admin_client):
        resp = admin_client.post('/api/admin/invite-codes', json={'count': 0, 'max_uses': 1})
        assert resp.status_code == 400

    def test_list_invite_codes(self, admin_client):
        admin_client.post('/api/admin/invite-codes', json={'count': 2})
        resp = admin_client.get('/api/admin/invite-codes')
        assert resp.status_code == 200
        assert len(resp.json()['codes']) == 2

    def test_delete_invite_code(self, admin_client):
        create_resp = admin_client.post('/api/admin/invite-codes', json={'count': 1})
        code = create_resp.json()['codes'][0]
        del_resp = admin_client.delete(f'/api/admin/invite-codes/{code}')
        assert del_resp.status_code == 200

    def test_list_users(self, admin_client):
        resp = admin_client.get('/api/admin/users')
        assert resp.status_code == 200
        assert len(resp.json()['users']) >= 1

    def test_non_admin_rejected(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / 'test_nonadmin.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.delenv('AUTH_TOKEN', raising=False)
        conn = db_mod.get_conn()
        admin_id = _create_admin(conn)
        db_mod.create_invite_code(conn, 'REG00001', admin_id)
        conn.close()

        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)

        # Register as regular user
        client.post('/api/auth/register', json={
            'username': 'regular',
            'email': 'reg@test.com',
            'password': 'password123',
            'invite_code': 'REG00001',
        })
        # Verify email so we can login
        conn2 = db_mod.get_conn()
        user = db_mod.get_user_by_email(conn2, 'reg@test.com')
        code = user['verification_code']
        conn2.close()
        client.post('/api/auth/verify-email', json={
            'email': 'reg@test.com',
            'code': code,
        })
        # Try admin endpoint as non-admin
        resp = client.get('/api/admin/users')
        assert resp.status_code == 403


class TestUserSettingsEndpoints:
    @pytest.fixture()
    def user_client(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / 'test_settings.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.setenv('ENCRYPTION_KEY', 'a' * 64)
        monkeypatch.delenv('AUTH_TOKEN', raising=False)
        conn = db_mod.get_conn()
        admin_id = _create_admin(conn)
        db_mod.create_invite_code(conn, 'SET00001', admin_id)
        conn.close()

        from fastapi.testclient import TestClient
        from app import app
        app.state.limiter.enabled = False
        client = TestClient(app)

        # Register
        client.post('/api/auth/register', json={
            'username': 'settingsuser',
            'email': 'settings@test.com',
            'password': 'password123',
            'invite_code': 'SET00001',
        })
        # Verify email to get logged in
        conn2 = db_mod.get_conn()
        user = db_mod.get_user_by_email(conn2, 'settings@test.com')
        code = user['verification_code']
        conn2.close()
        client.post('/api/auth/verify-email', json={
            'email': 'settings@test.com',
            'code': code,
        })
        return client

    def test_get_settings(self, user_client):
        resp = user_client.get('/api/user/settings')
        assert resp.status_code == 200
        data = resp.json()
        assert data['username'] == 'settingsuser'
        assert data['has_discord_token'] is False

    def test_set_and_get_discord_token(self, user_client):
        # Set token
        resp = user_client.put('/api/user/settings', json={
            'discord_bot_token': 'my-secret-bot-token-123',
        })
        assert resp.status_code == 200

        # Get settings — token should be masked
        resp = user_client.get('/api/user/settings')
        data = resp.json()
        assert data['has_discord_token'] is True
        assert data['discord_bot_token'] is not None
        assert 'my-secret-bot-token-123' not in data['discord_bot_token']  # must be masked

    def test_clear_discord_token(self, user_client):
        # Set then clear
        user_client.put('/api/user/settings', json={'discord_bot_token': 'token123'})
        user_client.put('/api/user/settings', json={'discord_bot_token': ''})
        resp = user_client.get('/api/user/settings')
        assert resp.json()['has_discord_token'] is False

    def test_unauthenticated_rejected(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / 'test_unauth_settings.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.delenv('AUTH_TOKEN', raising=False)
        conn = db_mod.get_conn()
        conn.close()

        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        assert client.get('/api/user/settings').status_code == 401
