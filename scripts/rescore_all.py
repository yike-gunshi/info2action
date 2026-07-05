#!/usr/bin/env python3
"""
Full re-score: clear AI fields for all items, then re-run classification + scoring.

Usage:
  python scripts/rescore_all.py                  # clear + re-score all (batch of 100)
  python scripts/rescore_all.py --batch 200      # process 200 at a time
  python scripts/rescore_all.py --dry-run        # show what would be cleared, don't modify DB
  python scripts/rescore_all.py --clear-only     # only clear fields, don't run scoring

This script:
1. Clears ai_category, relevance_score, ai_keywords, ai_dimensions for all items
2. Preserves: user feedback (feedback table), read status (clicked_at), stars (starred_at)
3. Runs score_items.py in batches until all items are re-scored
4. Prints distribution stats at the end
"""

import argparse
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import db


def clear_ai_fields(conn, dry_run=False, limit=None):
    """Clear all AI-generated fields to force re-scoring."""
    count = conn.execute("SELECT COUNT(*) FROM items WHERE ai_category IS NOT NULL AND ai_category != ''").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    print(f"Total items in DB: {total}")
    print(f"Items with AI classification: {count}")

    target = limit if limit else total

    if dry_run:
        print(f"[DRY RUN] Would clear ai_category, relevance_score, ai_keywords, ai_dimensions for {target} items")
        return target

    if limit:
        conn.execute(f"""
            UPDATE items SET
                ai_category = NULL,
                relevance_score = NULL,
                ai_keywords = NULL,
                ai_dimensions = NULL
            WHERE id IN (
                SELECT id FROM items ORDER BY fetched_at DESC LIMIT {limit}
            )
        """)
    else:
        conn.execute("""
            UPDATE items SET
                ai_category = NULL,
                relevance_score = NULL,
                ai_keywords = NULL,
                ai_dimensions = NULL
        """)
    conn.commit()
    print(f"Cleared AI fields for {target} items")
    return target


def run_scoring_batch(batch_size, dry_run=False):
    """Run score_items.py for one batch. Returns number of items processed."""
    cmd = [sys.executable, os.path.join(BASE_DIR, "score_items.py"), "--limit", str(batch_size)]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # Check if there are remaining items
    if "All items already scored" in result.stdout:
        return 0
    # Parse completed count from output
    for line in result.stdout.split("\n"):
        if "Found" in line and "items" in line:
            try:
                return int(line.split("Found")[1].split("items")[0].strip())
            except (ValueError, IndexError):
                pass
    return -1  # unknown


def print_stats(conn):
    """Print distribution statistics after re-scoring."""
    print("\n" + "=" * 60)
    print("Re-scoring complete. Distribution stats:")
    print("=" * 60)

    # Category distribution
    rows = conn.execute("""
        SELECT ai_category, COUNT(*) as cnt, ROUND(AVG(relevance_score), 1) as avg_score
        FROM items
        WHERE ai_category IS NOT NULL AND ai_category != ''
        GROUP BY ai_category
        ORDER BY cnt DESC
    """).fetchall()

    print(f"\n{'Category':<20} {'Count':>8} {'Avg Score':>10}")
    print("-" * 40)
    total_scored = 0
    for r in rows:
        print(f"{r[0]:<20} {r[1]:>8} {r[2] or 0:>10.1f}")
        total_scored += r[1]

    # Unscored
    unscored = conn.execute("SELECT COUNT(*) FROM items WHERE ai_category IS NULL OR ai_category = ''").fetchone()[0]
    print("-" * 40)
    print(f"{'TOTAL scored':<20} {total_scored:>8}")
    print(f"{'Unscored':<20} {unscored:>8}")

    # Score distribution
    print(f"\n{'Score Range':<20} {'Count':>8}")
    print("-" * 30)
    for low, high, label in [(8, 10, "8-10 (high)"), (5, 8, "5-7.9 (medium)"), (3, 5, "3-4.9 (low)"), (1, 3, "1-2.9 (noise)")]:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM items WHERE relevance_score >= ? AND relevance_score < ?",
            (low, high)
        ).fetchone()[0]
        print(f"{label:<20} {cnt:>8}")


def main():
    parser = argparse.ArgumentParser(description="Full re-score: clear AI fields and re-run classification")
    parser.add_argument("--batch", type=int, default=100, help="Batch size per scoring run (default: 100)")
    parser.add_argument("--limit", type=int, default=None, help="Only re-score this many items (most recent first). Omit to re-score all.")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would happen without modifying DB")
    parser.add_argument("--clear-only", action="store_true", help="Only clear AI fields, don't run scoring")
    args = parser.parse_args()

    conn = db.get_conn()

    # Step 1: Clear AI fields
    print("Step 1: Clearing AI fields...")
    total = clear_ai_fields(conn, dry_run=args.dry_run, limit=args.limit)

    if args.clear_only:
        print("\n--clear-only: stopping after clearing fields.")
        conn.close()
        return

    if args.dry_run:
        print("\n--dry-run: would then run scoring in batches of", args.batch)
        conn.close()
        return

    # Step 2: Run scoring in batches
    max_batches = (args.limit // args.batch + 1) if args.limit else (total // args.batch + 2)
    print(f"\nStep 2: Running scoring in batches of {args.batch} (max {max_batches} batches)...")
    batch_num = 0
    while batch_num < max_batches:
        batch_num += 1
        print(f"\n--- Batch {batch_num}/{max_batches} ---")
        processed = run_scoring_batch(args.batch)
        if processed == 0:
            break
        if processed < 0:
            print("Warning: could not determine batch size, running one more batch...")
            if batch_num >= max_batches:
                print("Safety limit reached, stopping.")
                break

    # Step 3: Print stats
    conn = db.get_conn()
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
