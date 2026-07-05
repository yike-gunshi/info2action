#!/usr/bin/env python3
"""Enrich a recent, bounded doc set with one process and controlled workers."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import ai_provider_guard
import db
import enrich_items


def _chunks(items: list[dict], batch_size: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    pending_batch: list[dict] = []
    for item in items:
        if enrich_items.batch_group_key(item) == 'batch':
            pending_batch.append(item)
            if len(pending_batch) >= batch_size:
                chunks.append(pending_batch)
                pending_batch = []
            continue
        if pending_batch:
            chunks.append(pending_batch)
            pending_batch = []
        chunks.append([item])
    if pending_batch:
        chunks.append(pending_batch)
    return chunks


def _query_recent_items(conn, fetched_since: str, limit: int | None) -> list[dict]:
    limit_clause = ' LIMIT ?' if limit else ''
    params: tuple[object, ...] = (fetched_since, limit) if limit else (fetched_since,)
    rows = conn.execute(
        f"""SELECT id, platform, source, author_name, metrics_json, url, title, content,
                   ai_summary, ai_category AS category, detail_json, asr_text
            FROM items
            WHERE fetched_at >= ?
              AND cluster_id IS NOT NULL
              AND (ai_summary IS NULL OR ai_summary = ''
                   OR ai_quality_score IS NULL
                   OR ai_category IS NULL OR ai_category = '')
            ORDER BY fetched_at DESC{limit_clause}""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _process_chunk(chunk: list[dict], runtime: dict, dry_run: bool) -> tuple[int, int]:
    api_key = runtime['api_key']
    api_base = runtime['api_base']
    model = runtime['model']
    system_prompt = runtime['system_prompt']
    valid_category_ids = runtime['valid_category_ids']
    max_tokens = runtime['max_tokens']
    try:
        if len(chunk) > 1:
            parsed = enrich_items.enrich_batch_items(
                chunk,
                api_key,
                api_base,
                model,
                system_prompt,
                valid_category_ids,
                max_tokens,
                dry_run,
            )
            return (len(parsed), 0)
        parsed = enrich_items.enrich_one_item(
            chunk[0],
            api_key,
            api_base,
            model,
            system_prompt,
            valid_category_ids,
            max_tokens,
            dry_run,
        )
        return (1 if parsed else 0, 0 if parsed else 1)
    except urllib.error.HTTPError as exc:
        if exc.code != 429 and not dry_run:
            for item in chunk:
                enrich_items.record_failure(item['id'], f'HTTP {exc.code}', retry_after=30 * 60)
        raise
    except Exception:
        if len(chunk) <= 1:
            if not dry_run:
                enrich_items.record_failure(chunk[0]['id'], 'parallel_enrich_failed', retry_after=30 * 60)
            return (0, 1)
        done = 0
        errors = 0
        for item in chunk:
            try:
                parsed = enrich_items.enrich_one_item(
                    item,
                    api_key,
                    api_base,
                    model,
                    system_prompt,
                    valid_category_ids,
                    max_tokens,
                    dry_run,
                )
                done += 1 if parsed else 0
                errors += 0 if parsed else 1
            except Exception as single_exc:
                if not dry_run:
                    enrich_items.record_failure(item['id'], str(single_exc), retry_after=30 * 60)
                errors += 1
        return (done, errors)


def main() -> int:
    parser = argparse.ArgumentParser(description='Parallel enrich recent docs')
    parser.add_argument('--fetched-since', default='2026-04-26')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--batch-size', type=int, default=5)
    parser.add_argument('--max-workers', type=int, default=3)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    cfg = enrich_items.load_config()
    classification = enrich_items.load_classification()
    categories = classification.get('categories', [])
    ai_config = cfg.get('ai_summary', {})
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(ai_config)
    if not api_key:
        print('ERROR: missing MiniMax API key in env/.env/config', file=sys.stderr)
        return 2
    ai_provider_guard.ensure_provider_available('minimax', source='enrich_latest_docs_parallel')

    conn = db.get_conn()
    try:
        items = _query_recent_items(conn, args.fetched_since, args.limit or None)
    finally:
        conn.close()
    chunks = _chunks(items, max(1, min(5, args.batch_size)))
    print(f'Found {len(items)} items in {len(chunks)} chunks')
    if not items:
        return 0

    runtime = {
        'api_key': api_key,
        'api_base': api_base,
        'model': model,
        'system_prompt': enrich_items.build_system_prompt(categories),
        'valid_category_ids': [cat['id'] for cat in categories],
        'max_tokens': int(ai_config.get('max_tokens', 100000)),
    }

    started = time.time()
    done = 0
    errors = 0
    workers = max(1, min(5, args.max_workers))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for index, chunk in enumerate(chunks):
            futures.append(executor.submit(_process_chunk, chunk, runtime, args.dry_run))
            time.sleep(0.2 if index < workers else 0)
        for future in as_completed(futures):
            try:
                ok_count, err_count = future.result()
            except urllib.error.HTTPError as exc:
                print(f'[HTTP {exc.code}] stopping; completed={done} errors={errors}', flush=True)
                return 1 if exc.code != 429 else 0
            done += ok_count
            errors += err_count
            processed = done + errors
            elapsed = max(time.time() - started, 0.1)
            print(
                f'  [{processed}/{len(items)}] enriched={done} errors={errors} '
                f'rate={done / elapsed * 60:.1f}/min',
                flush=True,
            )

    print(f'Done! enriched={done}, errors={errors}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
