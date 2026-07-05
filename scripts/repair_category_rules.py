#!/usr/bin/env python3
"""Apply deterministic category repairs for high-confidence taxonomy boundaries.

This does not call any LLM. It is intended for local preview/backfill after the
taxonomy boundary changes, especially when old rows were already classified.
"""
from __future__ import annotations

import argparse
import os
import sqlite3


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "feed.db")


def _text(row: sqlite3.Row) -> str:
    return " ".join(
        str(row[key] or "")
        for key in ("platform", "source", "title", "content", "ai_summary")
    ).lower()


def _repair_category(row: sqlite3.Row) -> str | None:
    current = row["ai_category"]
    text = _text(row)
    title = str(row["title"] or "").lower()
    platform = str(row["platform"] or "").lower()

    if platform == "github":
        if current == "tutorials" or any(term in title for term in ("tutorial", "教程", "beginners", "course", "cookbook")):
            return None
        return "ai_tools"

    if current != "products":
        return None

    if "codex" in text and any(term in text for term in ("实测", "120 小时", "120小时", "developer", "工程师")):
        return "ai_tools"

    tutorial_title_terms = (
        "case",
        "cases",
        "案例",
        "prompt",
        "prompts",
        "提示词",
        "实测",
        "效果对比",
        "正面pk",
        "玩法",
        "asked gpt image",
        "fed gpt",
        "gpt-image-2 vs",
        "nano banana",
    )
    if any(term in title for term in tutorial_title_terms):
        return "tutorials"

    if any(name in title for name in ("gpt image", "gpt-image")) and any(term in title for term in ("测评", "评测", "sota")):
        return "models"

    if any(term in text for term in ("cost analysis", "成本分析", "benchmark", "benchmarks", "eval")):
        return "models"

    if any(term in text for term in ("gpt-rosalind", "world model", "世界模型", "mimo-v2", "sota model")):
        return "models"

    if any(term in title for term in ("tpu 8", "gpu", "nvidia", "芯片", "算力")):
        return "industry"

    if any(term in text for term in ("pr comment", "compromised claude code", "zero audit trail")):
        return "tech"

    if platform == "reddit" and any(term in title for term in ("gpt image", "chatgpt image", "image 2")):
        return "other"

    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair known category boundary mistakes without LLM calls.")
    parser.add_argument("--db", default=DB_PATH, help="SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, platform, source, title, content, ai_summary, ai_category
        FROM items
        WHERE ai_category IS NOT NULL AND ai_category != ''
        """
    ).fetchall()

    changes: list[tuple[str, str, str, str]] = []
    for row in rows:
        repaired = _repair_category(row)
        if repaired and repaired != row["ai_category"]:
            changes.append((repaired, row["ai_category"], row["id"], row["title"] or ""))

    for new_category, old_category, item_id, title in changes:
        print(f"{old_category} -> {new_category}: {title[:100]}")
        if not args.dry_run:
            conn.execute("UPDATE items SET ai_category=? WHERE id=?", (new_category, item_id))

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"{'Would repair' if args.dry_run else 'Repaired'} {len(changes)} items")


if __name__ == "__main__":
    main()
