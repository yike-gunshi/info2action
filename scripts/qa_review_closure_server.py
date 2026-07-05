#!/usr/bin/env python3
"""Start a seeded local QA server for the 2026-04-24 review closure pass."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import uvicorn


PASSWORD = "password123"
JWT_SECRET = "qa-review-closure-secret-with-enough-entropy"


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


def _create_user(db, conn, username: str, email: str, role: str) -> str:
    user_id = str(uuid.uuid4())
    db.create_user(conn, user_id, username, email, _hash_password(PASSWORD), role=role)
    db.update_user(conn, user_id, email_verified=1)
    return user_id


def _insert_item(conn, *, item_id: str, user_id: str | None, platform: str, title: str, summary: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO items (
            id, user_id, platform, source, title, content, url, ai_summary,
            ai_category, fetched_at, published_at, asr_text, asr_status,
            asr_text_cn, media_json, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            user_id,
            platform,
            "qa-review",
            title,
            f"{title} body content",
            f"https://example.com/{item_id}",
            summary,
            "ai",
            now,
            now,
            f"{title} transcript",
            "success",
            f"{title} 中文转写",
            "[]",
            "{}",
        ),
    )
    conn.commit()


def seed_database(db_path: Path, feedback_db_path: Path, meta_path: Path) -> dict:
    os.environ["JWT_SECRET"] = JWT_SECRET
    os.environ.pop("AUTH_TOKEN", None)

    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    sys.path.insert(0, str(src_dir))

    import db  # noqa: PLC0415
    import feedback_store  # noqa: PLC0415

    db.DB_PATH = str(db_path)
    db._item_status_has_user_id = None
    feedback_store.FB_DB_PATH = str(feedback_db_path)

    conn = db.get_conn()
    try:
        admin_id = _create_user(db, conn, "qa-admin", "qa-admin@test.local", "admin")
        alice_id = _create_user(db, conn, "qa-alice", "qa-alice@test.local", "user")
        bob_id = _create_user(db, conn, "qa-bob", "qa-bob@test.local", "user")
        db.migrate_item_status_add_user_id(conn, admin_id)
        db._item_status_has_user_id = None

        _insert_item(
            conn,
            item_id="public-review-item",
            user_id=None,
            platform="twitter",
            title="Public Review Item",
            summary="Public item visible to anonymous users",
        )
        _insert_item(
            conn,
            item_id="manual-alice-review",
            user_id=alice_id,
            platform="manual",
            title="Alice Private Manual",
            summary="Private manual item for Alice",
        )
        db.set_status(conn, "manual-alice-review", "starred", user_id=alice_id)

        alice_action_id = db.create_action(
            conn,
            source_type="manual",
            title="Alice Secret Action",
            action_type="implementation",
            prompt="Do sensitive host work",
            source_item_ids=["manual-alice-review"],
            reason="QA seed",
            priority="high",
            related_project="/tmp/alice-private-project",
            user_id=alice_id,
        )
        alice_interest_id = db.create_interest(
            conn,
            "Alice Secret Interest",
            description="private interest",
            keywords=["secret"],
            user_id=alice_id,
        )
        bob_action_id = db.create_action(
            conn,
            source_type="manual",
            title="Bob Visible Action",
            action_type="research",
            prompt="Bob work",
            source_item_ids=["public-review-item"],
            reason="QA seed",
            priority="medium",
            user_id=bob_id,
        )
        bob_interest_id = db.create_interest(
            conn,
            "Bob Visible Interest",
            description="bob interest",
            keywords=["bob"],
            user_id=bob_id,
        )
    finally:
        conn.close()

    meta = {
        "password": PASSWORD,
        "users": {
            "admin": {"id": admin_id, "email": "qa-admin@test.local"},
            "alice": {"id": alice_id, "email": "qa-alice@test.local"},
            "bob": {"id": bob_id, "email": "qa-bob@test.local"},
        },
        "items": {
            "public": "public-review-item",
            "alice_manual": "manual-alice-review",
        },
        "actions": {
            "alice": alice_action_id,
            "bob": bob_action_id,
        },
        "interests": {
            "alice": alice_interest_id,
            "bob": bob_interest_id,
        },
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--feedback-db", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    db_path = Path(args.db)
    feedback_db_path = Path(args.feedback_db)
    meta_path = Path(args.meta)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_db_path.parent.mkdir(parents=True, exist_ok=True)

    os.environ["JWT_SECRET"] = JWT_SECRET
    os.environ.pop("AUTH_TOKEN", None)
    os.environ["PORT"] = str(args.port)

    seed_database(db_path, feedback_db_path, meta_path)

    import db  # noqa: PLC0415
    import feedback_store  # noqa: PLC0415
    import app as app_module  # noqa: PLC0415
    import routes.briefing as briefing_route  # noqa: PLC0415
    import routes.fetch as fetch_route  # noqa: PLC0415
    import routes.actions as actions_route  # noqa: PLC0415

    db.DB_PATH = str(db_path)
    feedback_store.FB_DB_PATH = str(feedback_db_path)

    # If a gate regresses, keep QA side effects harmless while preserving status-code checks.
    fetch_route._run_fetch = lambda: None

    class _NoopPopen:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def poll(self):
            return 0

    briefing_route.subprocess.Popen = _NoopPopen
    actions_route.execute_action.start_execution = lambda action_id, tool="codex": {
        "ok": True,
        "action_id": action_id,
        "tool": tool,
        "qa_stub": True,
    }

    uvicorn.run(app_module.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
