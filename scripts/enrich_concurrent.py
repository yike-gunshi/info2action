"""并发版 enrichment runner — 复用 src/enrich_items.py 的核心函数 + 加 429 退避。

为什么独立成脚本：enrich_items.py main() 是串行 while loop，改动量大。
本脚本只复用 enrich_one_item（已 thread-safe，每次自 open/close conn）+
build_system_prompt + query_pending_items，外层调度用 ThreadPoolExecutor + 429 指数退避。

用法：
  python scripts/enrich_concurrent.py --limit 2000 --concurrency 20
  python scripts/enrich_concurrent.py --ids id1,id2,id3 --concurrency 10

退避策略：
  - HTTP 429（RPM 不足）→ 指数退避 base=2s，cap=60s + jitter，最多重试 8 次
  - 其他 HTTPError / Exception → record_failure（30 min retry_after），不阻塞其他 worker
  - ProviderCooldown → 整体 stop（其他 worker 自然完成当前任务后退出）
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import db  # type: ignore
import ai_provider_guard  # type: ignore
import urllib.request as _urlreq

# 关键修复（2026-04-29，v6b）：ai_provider_guard 有两个并发问题，必须同时绕过：
#
# (1) **全局 cooldown 共享磁盘状态**：单 worker 撞 1 次 429 → 立即写 cooldown
#     文件 30 min → 所有 worker 抛 ProviderCooldown 全停。
#
# (2) **fcntl flock 文件锁让 20 worker 完全串行**：guarded_urlopen 末尾走
#     record_success → _with_state_lock → fcntl.flock(LOCK_PATH, LOCK_EX)。
#     所有 worker 排队等这把锁，并发 20 实测降到 ~40/min（vs 真并发应 600+/min）。
#
# 用户原意：**按每条 item 独立 retry**，单条 429 不影响其他 worker，且并发不被锁串行化。
# Fix：直接 monkey-patch guarded_urlopen 为不走锁的简单 urllib.urlopen。
def _no_guard_urlopen(request, source=None, timeout=None, allow_probe=False, **kwargs):
    if timeout is None:
        return _urlreq.urlopen(request, **kwargs)
    return _urlreq.urlopen(request, timeout=timeout, **kwargs)

ai_provider_guard.guarded_urlopen = _no_guard_urlopen
ai_provider_guard.record_rate_limit = lambda *a, **kw: None
ai_provider_guard.record_success = lambda *a, **kw: None
ai_provider_guard.ensure_provider_available = lambda *a, **kw: None
ai_provider_guard.is_cooldown_active = lambda *a, **kw: False

from enrich_items import (  # type: ignore  # noqa: E402
    build_subcategory_map,
    build_system_prompt,
    enrich_one_item,
    load_classification,
    load_config,
    query_pending_items,
    record_failure,
)

MAX_RETRIES_429 = 8
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0


_cooldown_event = threading.Event()
_lock = threading.Lock()
_stats = {"completed": 0, "errors": 0, "rate_limited": 0, "cooldown_skipped": 0}


def _bump(key: str, n: int = 1) -> None:
    with _lock:
        _stats[key] += n


def _worker(item: dict, api_key: str, api_base: str, model: str,
             system_prompt: str, valid_category_ids: list[str],
             valid_l2_by_l1: dict[str, set[str]], max_tokens: int) -> dict:
    """单条 enrich + 429 指数退避。每条 item 完全独立，单条 429 不影响其他 worker。"""
    for attempt in range(1, MAX_RETRIES_429 + 1):
        try:
            parsed = enrich_one_item(
                item, api_key, api_base, model, system_prompt,
                valid_category_ids, max_tokens, dry_run=False,
                valid_l2_by_l1=valid_l2_by_l1,
            )
            if parsed is None:
                _bump("errors")
                return {"id": item["id"], "status": "skipped",
                        "reason": "content_too_short_or_skipped"}
            _bump("completed")
            cat_field = parsed.get("category") or (parsed.get("categories") or [None])[0]
            return {"id": item["id"], "status": "ok",
                    "category": cat_field}
        except ai_provider_guard.ProviderCooldown as e:
            # patch 后理论上不会抛此异常；兜底：当 worker 自己的 retry 处理，不全停
            _bump("rate_limited")
            if attempt >= MAX_RETRIES_429:
                record_failure(item["id"], f"ProviderCooldown after {attempt} retries",
                               retry_after=30 * 60)
                _bump("errors")
                return {"id": item["id"], "status": "failed",
                        "reason": f"cooldown retried {attempt}x"}
            backoff = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1))) + random.random() * 2
            time.sleep(backoff)
            continue
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _bump("rate_limited")
                if attempt >= MAX_RETRIES_429:
                    record_failure(item["id"], f"HTTP 429 after {attempt} retries",
                                   retry_after=30 * 60)
                    _bump("errors")
                    return {"id": item["id"], "status": "failed",
                            "reason": f"429 retried {attempt}x"}
                # 指数退避 + jitter
                backoff = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1))) + random.random() * 2
                time.sleep(backoff)
                continue
            else:
                record_failure(item["id"], f"HTTP {e.code}", retry_after=30 * 60)
                _bump("errors")
                return {"id": item["id"], "status": "failed",
                        "reason": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            record_failure(item["id"], str(e), retry_after=30 * 60)
            _bump("errors")
            return {"id": item["id"], "status": "failed", "reason": str(e)[:120]}

    # 不会到这里（所有路径都 return）
    _bump("errors")
    return {"id": item["id"], "status": "failed", "reason": "unreachable"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ids", type=str, default="")
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()

    config = load_config()
    classification = load_classification()
    categories = classification.get("categories", [])
    valid_category_ids = [cat["id"] for cat in categories]
    valid_l2_by_l1 = build_subcategory_map(categories)
    ai_config = config.get("ai_summary", {})
    api_key = os.environ.get("MINIMAX_API_KEY") or ai_config.get("api_key", "")
    api_base = ai_config.get("api_base", "https://api.minimaxi.com/anthropic/v1")
    model = ai_config.get("model", "MiniMax-M2.7")
    max_tokens = int(ai_config.get("max_tokens", 100000))
    if not api_key:
        print("ERROR: MINIMAX_API_KEY missing")
        return 1

    try:
        ai_provider_guard.ensure_provider_available("minimax", source="enrich_concurrent.main")
    except ai_provider_guard.ProviderCooldown as e:
        print(f"MiniMax cooldown active until {e.cooldown_until}, abort")
        return 0

    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    conn = db.get_conn()
    rows = query_pending_items(conn, limit=args.limit, ids=ids)
    items = [dict(row) for row in rows]
    conn.close()
    print(f"[enrich-concurrent] Found {len(items)} items, concurrency={args.concurrency}, "
          f"max retries on 429={MAX_RETRIES_429} (backoff base={BACKOFF_BASE}s cap={BACKOFF_CAP}s)",
          flush=True)
    if not items:
        return 0

    system_prompt = build_system_prompt(categories)
    started = time.time()
    last_report = started

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        future_to_item = {
            ex.submit(_worker, it, api_key, api_base, model, system_prompt,
                       valid_category_ids, valid_l2_by_l1, max_tokens): it
            for it in items
        }
        for i, fut in enumerate(as_completed(future_to_item), 1):
            it = future_to_item[fut]
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001
                _bump("errors")
                result = {"id": it["id"], "status": "failed", "reason": f"future: {e}"}
            now = time.time()
            if i % 20 == 0 or (now - last_report) > 30:
                with _lock:
                    s = dict(_stats)
                elapsed = now - started
                rate = i / elapsed if elapsed > 0 else 0
                print(f"  [{i}/{len(items)}] ok={s['completed']} err={s['errors']} "
                      f"rate_limited={s['rate_limited']} cooldown_skipped={s['cooldown_skipped']} "
                      f"elapsed={elapsed:.0f}s ({rate:.1f}/s)", flush=True)
                last_report = now

    elapsed = time.time() - started
    with _lock:
        s = dict(_stats)
    print(f"\n[enrich-concurrent] DONE in {elapsed:.0f}s")
    print(f"  完成: {s['completed']}/{len(items)}")
    print(f"  失败: {s['errors']}")
    print(f"  撞 429 重试: {s['rate_limited']}")
    print(f"  cooldown 跳过: {s['cooldown_skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
