#!/usr/bin/env python3
"""
Re-run AI summaries for lingowhale articles with poor/short summaries.
Targets items where content is long but summary is too short (likely generated
from previously truncated content).

Usage: python3 scripts/rerun_lingowhale_summaries.py [--all] [--dry-run] [--limit N]

Without --all: only processes priority items (very short summaries <50 or truncated content)
With --all: processes all lingowhale items with summary < 100 chars and content > 500 chars
"""

import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
import db

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")

BATCH_SIZE = 8
BATCH_PAUSE_SEC = 15
REQUEST_STAGGER_SEC = 0.8


def main():
    dry_run = "--dry-run" in sys.argv
    process_all = "--all" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # Load config
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    ai_config = config.get("ai_summary", {})
    api_key = ai_config.get("api_key", "")
    api_base = ai_config.get("api_base", "https://api.minimaxi.com/anthropic/v1")
    model = ai_config.get("model", "MiniMax-M2.7")
    max_tokens = ai_config.get("max_tokens", 2048)

    if not api_key:
        print("ERROR: No API key in config.json ai_summary section")
        sys.exit(1)

    from generate_summaries import build_prompt, call_minimax, parse_summary_response

    conn = db.get_conn()
    if process_all:
        query = """SELECT id, platform, title, content, ai_summary, ai_category as category,
                          detail_json, fetched_at
                   FROM items
                   WHERE platform = 'lingowhale'
                     AND length(content) > 500
                     AND (ai_summary IS NULL OR length(ai_summary) < 100)
                   ORDER BY fetched_at DESC"""
        desc = "all lingowhale with summary < 100"
    else:
        query = """SELECT id, platform, title, content, ai_summary, ai_category as category,
                          detail_json, fetched_at
                   FROM items
                   WHERE platform = 'lingowhale' AND (
                       (length(content) > 500 AND length(ai_summary) < 50)
                       OR (length(content) BETWEEN 100 AND 200)
                   )
                   ORDER BY fetched_at DESC"""
        desc = "priority lingowhale (summary<50 or truncated content)"

    rows = conn.execute(query).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    if limit:
        items = items[:limit]

    total = len(items)
    print(f"Found {total} items [{desc}]")

    if total == 0:
        print("Nothing to re-run. Done.")
        return

    if dry_run:
        print(f"\n[DRY RUN] Would re-run {total} items:")
        for it in items[:20]:
            sl = len(it.get("ai_summary") or "")
            cl = len(it.get("content") or "")
            print(f"  content={cl:5d} summary={sl:3d} | {(it.get('title') or '')[:50]}")
        if total > 20:
            print(f"  ... and {total - 20} more")
        return

    # Build content for processing
    to_process = []
    for item in items:
        title = item.get("title") or ""
        content = item.get("content") or ""

        enriched_text = ""
        dj_raw = item.get("detail_json")
        if dj_raw:
            try:
                dj = json.loads(dj_raw) if isinstance(dj_raw, str) else dj_raw
                ref_urls = dj.get("referenced_urls", [])
                for ref in ref_urls:
                    ft = ref.get("full_text", "")
                    if ft and len(ft) > 100:
                        ref_title = ref.get("title", "")
                        enriched_text += f"\n\n--- 外链正文: {ref_title} ---\n{ft}"
                        break
            except (ValueError, TypeError, AttributeError):
                pass

        content_text = f"标题: {title}\n正文: {content or ''}"
        if enriched_text:
            content_text += enriched_text
        content_text = content_text[:12000]

        if len(content_text) < 20:
            continue

        to_process.append({
            "id": item["id"],
            "platform": item.get("platform", ""),
            "category": item.get("category", ""),
            "content_text": content_text,
            "title": title[:100],
            "fetched_at": item.get("fetched_at", ""),
            "old_summary_len": len(item.get("ai_summary") or ""),
        })

    total = len(to_process)
    print(f"Will re-run {total} items in batches of {BATCH_SIZE}")
    print(f"Estimated time: {total * BATCH_PAUSE_SEC / BATCH_SIZE / 60:.0f} minutes\n")

    completed = 0
    errors = 0
    improved = 0
    lock = threading.Lock()
    start_time = time.time()

    def process_item(item):
        nonlocal completed, errors, improved
        try:
            time.sleep(REQUEST_STAGGER_SEC)
            prompt = build_prompt(item.get("platform", ""), item.get("category", ""))
            raw = call_minimax(api_key, api_base, model, prompt, item["content_text"], max_tokens)
            parsed = parse_summary_response(raw)
            summary = parsed["preview"]
            key_points = parsed.get("key_points", [])

            is_error = summary.startswith("[总结生成失败")
            if is_error:
                with lock:
                    errors += 1
                print(f"  [FAIL] {item['title'][:30]}: {summary[:60]}", flush=True)
                return

            # Write to DB
            item_conn = db.get_conn()
            db.update_ai_summary(item_conn, item["id"], summary, key_points)
            item_conn.close()

            new_len = len(summary)
            old_len = item.get("old_summary_len", 0)

            with lock:
                completed += 1
                if new_len > old_len:
                    improved += 1
                if completed % 10 == 0:
                    elapsed = time.time() - start_time
                    rpm = completed / max(elapsed / 60, 0.1)
                    print(f"  Progress: {completed}/{total} done | {improved} improved | {errors} errors | {rpm:.0f} RPM", flush=True)

        except Exception as e:
            with lock:
                errors += 1
            print(f"  [ERROR] {item.get('title', '?')[:30]}: {e}", flush=True)

    # Process in batches
    num_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_idx in range(num_batches):
        batch_start = batch_idx * BATCH_SIZE
        batch_end = min(batch_start + BATCH_SIZE, total)
        batch = to_process[batch_start:batch_end]

        batch_num = batch_idx + 1
        print(f"Batch {batch_num}/{num_batches} ({len(batch)} items)", flush=True)

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
            futures = [executor.submit(process_item, item) for item in batch]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"  [THREAD ERROR] {e}", flush=True)

        if batch_idx < num_batches - 1:
            time.sleep(BATCH_PAUSE_SEC)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Done! {elapsed:.0f}s elapsed")
    print(f"  Completed:  {completed}")
    print(f"  Improved:   {improved}")
    print(f"  Errors:     {errors}")
    print(f"  Total:      {completed + errors}/{total}")


if __name__ == "__main__":
    main()
