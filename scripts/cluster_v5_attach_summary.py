"""为 v5b 簇追加 v1 prompt 07 生成的 ai_title / ai_summary 双段 / ai_key_points。

设计决策（用户 2026-04-29）：
  - prompt 07 完全不动（v1 优化版本，main 上的 prompts/07_cluster_summary.md）
  - summary_writer.py 完全不动（复用 _collect_member_docs / _parse_llm_json /
    _check_invalid_warnings / load_prompt 等 helper）
  - 本 wrapper 解决 3 个工程参数：
    1. max_tokens 2048 → 16384（充足空间给双段 summary + thinking 缓冲）
    2. timeout 60s → 180s（防超时）
    3. 加 429 指数退避 base=2s cap=60s 最多 6 次
  - 并发 20，每 worker 独立 retry（429 不影响其他 worker）
  - 跑 main DB 中 prompt_version='v5b' 标记的所有簇

写入字段：
  clusters.ai_title / ai_summary / ai_key_points / is_visible_in_feed /
  last_summary_warnings_json / live_version / last_updated_at

  is_visible_in_feed = 1 if (title AND summary)
  v5b 可见性由 Stage P / materialize 步骤决定；attach summary 只补写展示文案，
  不再用 doc_count>=2 或 prompt 07 warnings 反向隐藏单来源簇。

用法：
  python scripts/cluster_v5_attach_summary.py             # dry-run（不写 DB）
  python scripts/cluster_v5_attach_summary.py --apply
  python scripts/cluster_v5_attach_summary.py --apply --concurrency 20

跑完前端 http://127.0.0.1:3567 刷新即可看到双段 summary。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

# 复用 v1 summary_writer 的 helper
from clustering import summary_writer as sw  # type: ignore
from prompt_loader import load_prompt  # type: ignore


def _parse_llm_json_v5b(raw: str) -> dict | None:
    """v5b parser：不要求 key_points 字段（v3 prompt 已删除）。

    v1 sw._parse_llm_json 强制要求 key_points 非空 list，会让 v3 prompt 输出全 fail。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("```")).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    title = obj.get("title")
    summary = obj.get("summary")
    if not (isinstance(title, str) and title.strip()
            and isinstance(summary, str) and summary.strip()):
        return None
    warnings_raw = obj.get("warnings") or []
    warnings: list[str] = []
    if isinstance(warnings_raw, list):
        for w in warnings_raw:
            t = str(w).strip() if w is not None else ""
            if t:
                warnings.append(t)
    return {
        "title": title.strip(),
        "summary": summary.strip(),
        "warnings": warnings,
    }


def _has_v5b_summary_shape(summary: str) -> bool:
    """Prompt 07 v3 的前端展示契约：summary 必须是双段结构。"""
    return "【精华速览】" in summary and "【全文拆解】" in summary

# 工程参数（用户 2026-04-29 拍板）
LLM_MAX_TOKENS = 16384
LLM_TIMEOUT = 180
MAX_RETRIES_429 = 6
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0

DEFAULT_DB = REPO_ROOT / "data" / "feed.db"
DEFAULT_MODEL = "MiniMax-M2.7"
DEFAULT_API_BASE = "https://api.minimaxi.com/anthropic/v1"


_lock = threading.Lock()
_stats = {
    "completed": 0,
    "failed": 0,
    "rate_limited": 0,
    "warning_matched": 0,
    "parse_retried": 0,
}


def _bump(key: str, n: int = 1) -> None:
    with _lock:
        _stats[key] += n


def _call_with_retry(api_key: str, api_base: str, model: str,
                      system_prompt: str, user_content: str,
                      cluster_id: int) -> str:
    """v1 _call_llm_chat 包一层 429 指数退避 + 16k tokens + 180s timeout。"""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES_429 + 1):
        try:
            return sw._call_llm_chat(
                api_key=api_key, api_base=api_base, model=model,
                system_prompt=system_prompt, user_content=user_content,
                max_tokens=LLM_MAX_TOKENS, timeout=LLM_TIMEOUT,
            )
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < MAX_RETRIES_429:
                _bump("rate_limited")
                backoff = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1))) + random.random() * 2
                print(f"  cluster={cluster_id}: 撞 429，退避 {backoff:.1f}s (attempt {attempt})", flush=True)
                time.sleep(backoff)
                continue
            print(f"  cluster={cluster_id}: HTTP {e.code} 失败 (attempt {attempt}): {e}", flush=True)
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  cluster={cluster_id}: 异常 (attempt {attempt}): {e}", flush=True)
            if attempt < 3:
                time.sleep(2)
                continue
            raise
    raise RuntimeError(f"unreachable: last_err={last_err}")


def _attach_one(cluster_id: int, db_path: str, api_key: str,
                 api_base: str, model: str, system_prompt: str,
                 dry_run: bool, max_member_docs: int = 12) -> dict:
    """对单个 cluster 跑 prompt 07 → 写回 DB。每 worker 自管 sqlite conn（thread-safe）。"""
    started = time.time()
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        # 复用 summary_writer 的 _collect_member_docs（读 cluster_items 老表）
        segs = sw._collect_member_docs(conn, cluster_id, max_member_docs)
        if not segs:
            _bump("failed")
            return {"cluster_id": cluster_id, "status": "no_members",
                    "took_seconds": round(time.time() - started, 2)}
        user_content = "\n\n---\n\n".join(segs)

        # 调 LLM（带 429 retry）+ parse retry。
        raw = ""
        parsed = None
        last_err: Exception | None = None
        for parse_attempt in range(1, 4):
            try:
                raw = _call_with_retry(
                    api_key=api_key, api_base=api_base, model=model,
                    system_prompt=system_prompt, user_content=user_content,
                    cluster_id=cluster_id,
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                break
            parsed = _parse_llm_json_v5b(raw)
            if parsed and _has_v5b_summary_shape(parsed["summary"]):
                break
            if parse_attempt < 3:
                _bump("parse_retried")
                print(f"  cluster={cluster_id}: parse_fail，重试 LLM ({parse_attempt}/3)", flush=True)
                time.sleep(1.0 + random.random())

        if last_err is not None:
            _bump("failed")
            return {"cluster_id": cluster_id, "status": "llm_error",
                    "reason": str(last_err)[:200],
                    "took_seconds": round(time.time() - started, 2)}
        if not parsed or not _has_v5b_summary_shape(parsed["summary"]):
            _bump("failed")
            return {"cluster_id": cluster_id, "status": "parse_fail",
                    "raw_chars": len(raw or ""),
                    "took_seconds": round(time.time() - started, 2)}

        title = parsed.get("title", "")
        summary = parsed.get("summary", "")
        warnings = parsed.get("warnings") or []

        # warnings 关键字白名单检查仅记录，不再反向隐藏 v5b 卡。
        matched_invalid = sw._check_invalid_warnings(warnings)
        if matched_invalid:
            _bump("warning_matched")

        if dry_run:
            _bump("completed")
            return {
                "cluster_id": cluster_id, "status": "dry_run",
                "title": title[:60],
                "summary_chars": len(summary),
                "warnings": warnings,
                "matched_invalid": matched_invalid,
                "took_seconds": round(time.time() - started, 2),
            }

        # apply: 写回 clusters 表（draft → live 一步到位）
        cur = conn.execute(
            "SELECT live_version, doc_count FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if not cur:
            _bump("failed")
            return {"cluster_id": cluster_id, "status": "no_cluster_row",
                    "took_seconds": round(time.time() - started, 2)}
        new_version = (cur["live_version"] or 0) + 1
        is_visible = 1 if (title and summary) else 0

        warnings_json = json.dumps(warnings, ensure_ascii=False)
        # v5b: 不再写 ai_key_points（v3 prompt 已删除该字段）；保持 NULL 让前端不渲染圆点列表
        conn.execute(
            """UPDATE clusters SET
                 ai_title = ?,
                 ai_summary = ?,
                 ai_key_points = NULL,
                 live_version = ?,
                 is_visible_in_feed = ?,
                 last_summary_warnings_json = ?,
                 last_updated_at = datetime('now')
               WHERE id = ?""",
            (title, summary, new_version, is_visible,
             warnings_json, cluster_id),
        )
        conn.commit()
        _bump("completed")
        return {
            "cluster_id": cluster_id, "status": "ok",
            "title": title[:60],
            "summary_chars": len(summary),
            "warnings": warnings,
            "matched_invalid": matched_invalid,
            "is_visible": is_visible,
            "took_seconds": round(time.time() - started, 2),
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真的写主仓库 DB（默认 dry-run）")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 个最大 doc_count 的簇（默认全部 v5b）")
    ap.add_argument("--only-missing-summary", action="store_true",
                    help="只重跑摘要缺失、标题占位或缺少 v5b 双段结构的簇")
    ap.add_argument("--max-member-docs", type=int, default=12,
                    help="每簇喂给 LLM 的 doc 数上限（v1 默认 20，缩到 12 减少 token 浪费）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    db_path = str(args.db)
    if not args.db.exists():
        raise SystemExit(f"DB 不存在：{db_path}")

    # API key
    api_key = os.environ.get("MINIMAX_API_KEY", "")
    if not api_key:
        # fallback: read config
        try:
            with open(REPO_ROOT / "config" / "config.json") as f:
                api_key = json.load(f).get("ai_summary", {}).get("api_key", "")
        except Exception:
            pass
    if not api_key:
        raise SystemExit("MINIMAX_API_KEY missing in env or config.json")

    # 加载 prompt 07（v1 优化版本，不改）
    system_prompt = load_prompt("07_cluster_summary.md")
    if not system_prompt:
        raise SystemExit("prompts/07_cluster_summary.md not found")
    print(f"[attach-summary] prompt 07 已加载 ({len(system_prompt)} chars)")

    # 选 v5b 簇
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "prompt_version='v5b'"
    if args.only_missing_summary:
        where += """ AND (
            ai_title IS NULL OR TRIM(ai_title) = ''
            OR ai_summary IS NULL OR TRIM(ai_summary) = ''
            OR ai_summary = ai_title
            OR ai_summary NOT LIKE '%【精华速览】%'
            OR ai_summary NOT LIKE '%【全文拆解】%'
        )"""
    sql = f"""SELECT id, doc_count FROM clusters
              WHERE {where}
              ORDER BY doc_count DESC"""
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    if not rows:
        raise SystemExit("没有找到 prompt_version='v5b' 的簇（先跑 merge_cluster_v2_to_main.py）")

    print(f"[attach-summary] 找到 {len(rows)} 个 v5b 簇 (doc_count: max={rows[0]['doc_count']}, min={rows[-1]['doc_count']})")
    print(f"[attach-summary] concurrency={args.concurrency}, max_tokens={LLM_MAX_TOKENS}, "
          f"timeout={LLM_TIMEOUT}s, max_member_docs={args.max_member_docs}, dry_run={not args.apply}")
    print()

    cluster_ids = [r["id"] for r in rows]
    started = time.time()
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        future_to_cid = {
            ex.submit(_attach_one, cid, db_path, api_key,
                       DEFAULT_API_BASE, DEFAULT_MODEL, system_prompt,
                       not args.apply, args.max_member_docs): cid
            for cid in cluster_ids
        }
        for i, fut in enumerate(as_completed(future_to_cid), 1):
            cid = future_to_cid[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                _bump("failed")
                r = {"cluster_id": cid, "status": "future_error", "reason": str(e)}
            results.append(r)
            status = r.get("status", "?")
            title = r.get("title", "")
            sc = r.get("summary_chars", "-")
            took = r.get("took_seconds", 0)
            print(f"  [{i}/{len(cluster_ids)}] cluster={cid}: {status} "
                  f"summary={sc}字 took={took}s | {title}", flush=True)

    elapsed = time.time() - started
    print()
    with _lock:
        s = dict(_stats)
    print(f"[attach-summary] DONE in {elapsed:.0f}s")
    print(f"  完成: {s['completed']}/{len(cluster_ids)}")
    print(f"  失败: {s['failed']}")
    print(f"  撞 429 重试: {s['rate_limited']}")
    print(f"  parse 重试: {s['parse_retried']}")
    print(f"  warnings 命中但保留可见: {s['warning_matched']}")

    if not args.apply:
        print()
        print("[dry-run] 用 --apply 真的写主仓库 DB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
