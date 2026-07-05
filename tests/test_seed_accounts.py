"""Tests for scripts/seed_test_accounts.py.

Coverage:
- All 3 accounts created on first run
- Re-running is idempotent (no dupes, skip notices)
- SEED_ACCOUNT_PASSWORD env honored
- Random password generated when env unset
- Profile fields persisted (role / interests / tools / onboarding_completed)
- email_verified flagged
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    import db as db_mod
    db_mod.DB_PATH = str(tmp_path / 'test.db')
    conn = db_mod.get_conn()
    yield conn
    conn.close()


class TestSeedAccounts:
    def test_first_run_creates_three_accounts(self, tmp_db, monkeypatch):
        monkeypatch.delenv('SEED_ACCOUNT_PASSWORD', raising=False)
        from scripts import seed_test_accounts as sa
        rc = sa.main([])
        assert rc == 0
        n = tmp_db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert n == 3
        emails = sorted(r[0] for r in tmp_db.execute("SELECT email FROM users").fetchall())
        assert emails == sorted(['pm@info2act.test', 'ml@info2act.test', 'indie@info2act.test'])

    def test_idempotent_second_run_skips(self, tmp_db, monkeypatch):
        monkeypatch.setenv('SEED_ACCOUNT_PASSWORD', 'pw-test-1234567890')
        from scripts import seed_test_accounts as sa
        sa.main([])
        sa.main([])  # again
        n = tmp_db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert n == 3

    def test_email_verified_flag_set(self, tmp_db, monkeypatch):
        monkeypatch.setenv('SEED_ACCOUNT_PASSWORD', 'pw-1234567890ab')
        from scripts import seed_test_accounts as sa
        sa.main([])
        rows = tmp_db.execute("SELECT email, email_verified FROM users").fetchall()
        assert all(r['email_verified'] == 1 for r in rows), rows

    def test_user_profile_persisted(self, tmp_db, monkeypatch):
        monkeypatch.setenv('SEED_ACCOUNT_PASSWORD', 'pw-12345-abcdef')
        from scripts import seed_test_accounts as sa
        import db as db_mod
        sa.main([])
        for email, expected_role in [
            ('pm@info2act.test', 'product_manager'),
            ('ml@info2act.test', 'ml_engineer'),
            ('indie@info2act.test', 'indie_developer'),
        ]:
            user = db_mod.get_user_by_email(tmp_db, email)
            assert user is not None
            profile = db_mod.get_user_profile(tmp_db, user['id'])
            assert profile is not None
            assert profile['role'] == expected_role
            assert profile['onboarding_completed'] == 1
            assert isinstance(profile['interests'], list) and len(profile['interests']) >= 2
            assert isinstance(profile['tools'], list) and len(profile['tools']) >= 2

    def test_random_password_when_env_unset(self, tmp_db, monkeypatch, capsys):
        monkeypatch.delenv('SEED_ACCOUNT_PASSWORD', raising=False)
        from scripts import seed_test_accounts as sa
        sa.main([])
        out = capsys.readouterr().out
        # Random per account → printed lines containing "password="
        assert out.count('password=') >= 3

    def test_shared_password_does_not_print(self, tmp_db, monkeypatch, capsys):
        monkeypatch.setenv('SEED_ACCOUNT_PASSWORD', 'shared-pw-987654')
        from scripts import seed_test_accounts as sa
        sa.main([])
        out = capsys.readouterr().out
        assert 'shared-pw-987654' not in out

    def test_password_generator_alphanumeric(self):
        from scripts import seed_test_accounts as sa
        for _ in range(5):
            pw = sa._gen_password(20)
            assert len(pw) == 20
            assert pw.isalnum()
