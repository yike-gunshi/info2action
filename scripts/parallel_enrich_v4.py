#!/usr/bin/env python3
"""Concurrent v4.0 enrichment with adaptive backoff.

- ThreadPoolExecutor with --workers (default 20).
- On HTTP 429 / ProviderCooldown / JSON-decode error: per-task retry with
  exponential backoff. After N consecutive 429 across the pool, the whole pool
  pauses for a cool-down window before resuming (RPM/TPM saturation guard).
- All other exceptions: log + record_failure + skip.
- Reuses enrich_items.enrich_one_item / build_system_prompt / parsers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import ai_provider_guard  # noqa: E402
import db  # noqa: E402
import enrich_items  # noqa: E402


# ── adaptive global pause state ────────────────────────────────────────────
_pause_lock = threading.Lock()
_pause_until = 0.0  # epoch seconds
_consecutive_429 = 0
_BACKOFF_INITIAL = 30.0
_BACKOFF_MAX = 300.0
_TRIGGER_429 = 5  # how many 429s to count before pausing the pool


def _wait_if_paused() -> None:
    while True:
        with _pause_lock:
            remaining = _pause_until - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5.0))


def _trigger_pool_pause(reason: str) -> None:
    """Pause the whole pool with exponential backoff."""
    global _pause_until, _consecutive_429
    with _pause_lock:
        _consecutive_429 += 1
        backoff = min(
            _BACKOFF_INITIAL * (2 ** max(0, _consecutive_429 - _TRIGGER_429)),
            _BACKOFF_MAX,
        )
        _pause_until = max(_pause_until, time.time() + backoff)
        until_ts = _pause_until
    print(
        f"[pool-pause] {reason} (429 count={_consecutive_429}); "
        f"pool pauses {backoff:.0f}s until {time.strftime('%H:%M:%S', time.localtime(until_ts))}",
        flush=True,
    )


def _reset_429_streak() -> None:
    global _consecutive_429
    with _pause_lock:
        _consecutive_429 = 0


# ── per-item enrichment with retry ─────────────────────────────────────────


def enrich_with_retry(
    item: dict,
    api_key: str,
    api_base: str,
    model: str,
    system_prompt: str,
    valid_category_ids: list[str],
    valid_l2_by_l1: dict,
    max_tokens: int,
    max_retries: int = 3,
) -> tuple[str, dict | None, str | None]:
    """Returns (item_id, parsed_or_None, error_str_or_None)."""
    last_err: str | None = None
    for attempt in range(max_retries + 1):
        _wait_if_paused()
        try:
            parsed = enrich_items.enrich_one_item(
                item, api_key, api_base, model, system_prompt,
                valid_category_ids, max_tokens, dry_run=False,
                valid_l2_by_l1=valid_l2_by_l1,
            )
            _reset_429_streak()
            return item["id"], parsed, None
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429:
                _trigger_pool_pause(f"HTTP 429 on {item['id'][:20]}")
                # Exponential backoff for this task too
                time.sleep(min(5 * (2 ** attempt), 60))
                continue
            elif 500 <= e.code < 600:
                # Transient server error: retry with shorter backoff
                time.sleep(2 + attempt * 3)
                continue
            else:
                # Non-retryable HTTP (4xx other than 429)
                break
        except ai_provider_guard.ProviderCooldown as e:
            _trigger_pool_pause(f"ProviderCooldown until {e.cooldown_until}")
            time.sleep(min(5 * (2 ** attempt), 60))
            continue
        except (json.JSONDecodeError, ValueError) as e:
            last_err = f"parse: {str(e)[:80]}"
            if attempt < max_retries:
                # Often transient (truncated LLM output) — retry
                time.sleep(2 + attempt * 2)
                continue
            else:
                break
        except Exception as e:
            last_err = str(e)[:120]
            break

    # Exhausted retries: record failure
    try:
        enrich_items.record_failure(item["id"], last_err or "unknown", retry_after=30 * 60)
    except Exception:
        pass
    return item["id"], None, last_err


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent v4.0 enrichment")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap total pending items (default: all)")
    parser.add_argument("--platform", type=str, default=None,
                        help="filter to one platform")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    config = enrich_items.load_config()
    classification = enrich_items.load_classification()
    categories = classification.get("categories", [])
    valid_category_ids = [c["id"] for c in categories]
    valid_l2_by_l1 = enrich_items.build_subcategory_map(categories)
    ai_config = config.get("ai_summary", {})
    api_key = os.environ.get("MINIMAX_API_KEY") or ai_config.get("api_key", "")
    api_base = ai_config.get("api_base", "https://api.minimaxi.com/anthropic/v1")
    model = ai_config.get("model", "MiniMax-M2.7")
    max_tokens = int(ai_config.get("max_tokens", 100000))
    if not api_key:
        print("ERROR: no MINIMAX_API_KEY", flush=True)
        return 1

    try:
        ai_provider_guard.ensure_provider_available("minimax", source="parallel_enrich_v4.main")
    except ai_provider_guard.ProviderCooldown as e:
        print(f"MiniMax cooldown until {e.cooldown_until}; abort", flush=True)
        return 0

    conn = db.get_conn()
    extra_where = ""
    extra_params: list = []
    if args.platform:
        extra_where = " AND platform = ?"
        extra_params.append(args.platform)
    limit_clause = " LIMIT ?" if args.limit else ""
    limit_params = [args.limit] if args.limit else []
    rows = conn.execute(
        f"""SELECT id, platform, source, author_name, metrics_json, url, title, content,
                   ai_summary, ai_category as category, detail_json, asr_text
            FROM items
            WHERE platform != 'bilibili'
              AND (ai_retry_after IS NULL OR ai_retry_after <= datetime('now'))
              AND (ai_summary IS NULL OR ai_summary = ''
                   OR ai_quality_score IS NULL
                   OR ai_category IS NULL OR ai_category = '')
              {extra_where}
            ORDER BY fetched_at DESC{limit_clause}""",
        extra_params + limit_params,
    ).fetchall()
    items = [dict(r) for r in rows]
    conn.close()

    print(f"Pending: {len(items)} items, workers={args.workers}", flush=True)
    if not items:
        return 0

    system_prompt = enrich_items.build_system_prompt(categories)
    completed = 0
    errors = 0
    started = time.time()
    cat_counter: Counter = Counter()
    hidden = 0
    multi_l1 = 0
    other_count = 0
    progress_step = max(10, len(items) // 20)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                enrich_with_retry,
                item, api_key, api_base, model, system_prompt,
                valid_category_ids, valid_l2_by_l1, max_tokens,
                args.max_retries,
            ): item["id"]
            for item in items
        }
        for fut in as_completed(futures):
            item_id, parsed, err = fut.result()
            if parsed:
                completed += 1
                cats = parsed.get("categories") or []
                for c in cats:
                    cat_counter[c] += 1
                if not parsed.get("visible", True):
                    hidden += 1
                if len(cats) > 1:
                    multi_l1 += 1
                if "other" in cats or any(s.endswith("other") for s in (parsed.get("subcategories") or [])):
                    other_count += 1
            else:
                errors += 1
                print(f"  [ERR {errors}] {item_id[:24]}: {err}", flush=True)
            done = completed + errors
            if done % progress_step == 0 or done == len(items):
                rate = done / max(1, time.time() - started)
                print(
                    f"  [{done}/{len(items)}] ok={completed} err={errors} "
                    f"hidden={hidden} multi_l1={multi_l1} other={other_count} "
                    f"rate={rate:.1f}/s",
                    flush=True,
                )

    elapsed = time.time() - started
    print()
    print(f"Done in {elapsed:.0f}s")
    print(f"Total: ok={completed} err={errors} hidden={hidden} "
          f"multi_l1={multi_l1} other={other_count}")
    print("L1 distribution:")
    for cat, n in cat_counter.most_common():
        print(f"  {cat:24s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
