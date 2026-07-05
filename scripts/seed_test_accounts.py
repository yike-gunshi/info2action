"""Seed 3 test accounts (PM / ML engineer / Indie dev) for v15.0 demos.

Idempotent: existing users (matched by email) are skipped with a notice.

Password resolution:
  - SEED_ACCOUNT_PASSWORD env var (used for ALL accounts) takes precedence
  - otherwise a fresh random 16-char alphanumeric password is generated
    PER account and printed to stdout (capture this immediately)

Usage:
    python3 scripts/seed_test_accounts.py
    SEED_ACCOUNT_PASSWORD=hunter2 python3 scripts/seed_test_accounts.py

NEVER hardcode passwords in this file.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import db as db_mod  # noqa: E402

SEED_ACCOUNTS = [
    {
        'username': 'pm-test',
        'email': 'pm@info2act.test',
        'role': 'product_manager',
        'interests': ['AI products', 'user research'],
        'tools': ['Figma', 'Notion'],
    },
    {
        'username': 'ml-test',
        'email': 'ml@info2act.test',
        'role': 'ml_engineer',
        'interests': ['LLM training', 'embedding models'],
        'tools': ['PyTorch', 'wandb'],
    },
    {
        'username': 'indie-test',
        'email': 'indie@info2act.test',
        'role': 'indie_developer',
        'interests': ['solo SaaS', 'TypeScript'],
        'tools': ['Cursor', 'Vercel'],
    },
]


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _hash_password(password: str) -> str:
    """Match whatever the auth layer expects. We try to import the project's
    actual hasher to avoid drift; fall back to a sha256 placeholder if unavailable
    (test/CI environments)."""
    try:
        from auth import hash_password  # type: ignore
        return hash_password(password)
    except Exception:
        # Fallback only for tests that don't exercise login.
        import hashlib
        return 'sha256$' + hashlib.sha256(password.encode('utf-8')).hexdigest()


def seed_one(conn, account: dict, *, password: str) -> dict:
    """Create / skip one account. Returns {created: bool, user_id, ...}."""
    existing = db_mod.get_user_by_email(conn, account['email'])
    if existing:
        print(f"[seed] {account['email']} already exists — skipping (id={existing['id']})", flush=True)
        return {'created': False, 'user_id': existing['id'],
                'email': account['email'], 'username': existing.get('username')}

    # Username collision check (separate index)
    by_uname = db_mod.get_user_by_username(conn, account['username'])
    if by_uname:
        print(f"[seed] username {account['username']} taken by other email — skipping",
              flush=True)
        return {'created': False, 'user_id': by_uname['id'],
                'email': by_uname.get('email'), 'username': account['username']}

    user_id = str(uuid.uuid4())
    db_mod.create_user(
        conn, user_id, account['username'], account['email'],
        _hash_password(password), role='user',
    )
    db_mod.update_user(conn, user_id, email_verified=1)
    db_mod.upsert_user_profile(
        conn, user_id,
        role=account['role'],
        interests=account['interests'],
        tools=account['tools'],
        onboarding_completed=True,
    )
    print(
        f"[seed] CREATED user={account['username']} email={account['email']} "
        f"role={account['role']} id={user_id}",
        flush=True,
    )
    return {'created': True, 'user_id': user_id,
            'email': account['email'], 'username': account['username'],
            'password': password}


def main(argv: list[str] | None = None) -> int:
    shared_password = os.environ.get('SEED_ACCOUNT_PASSWORD', '').strip()
    use_shared = bool(shared_password)
    if use_shared:
        print("[seed] using SEED_ACCOUNT_PASSWORD env var for all accounts", flush=True)
    else:
        print("[seed] no SEED_ACCOUNT_PASSWORD env — generating random per account", flush=True)

    conn = db_mod.get_conn()
    try:
        results: list[dict] = []
        for acct in SEED_ACCOUNTS:
            pw = shared_password or _gen_password()
            r = seed_one(conn, acct, password=pw)
            results.append(r)
        created = sum(1 for r in results if r['created'])
        skipped = len(results) - created
        print(
            f"\n[seed] done: created={created} skipped={skipped}",
            flush=True,
        )
        if created and not use_shared:
            print(
                "[seed] PASSWORDS (capture now — won't be shown again):",
                flush=True,
            )
            for r in results:
                if r['created']:
                    print(f"  {r['email']}  password={r['password']}", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
