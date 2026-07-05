#!/usr/bin/env python3
"""Candidate review and classification-only backfill for the eval category.

Default mode is intentionally read-only:
- query recent visible items from the remote DB
- run a compact classification-only prompt with the current taxonomy
- print clickable local item links for human review

Use --apply to update only items classified as eval. Non-eval candidates are
never changed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items  # noqa: E402
import remote_db  # noqa: E402


LOCAL_ITEM_BASE = "http://127.0.0.1:3783/#item="
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}

EVAL_TERMS = [
    "eval",
    "evals",
    "evaluation",
    "benchmark",
    "benchmarks",
    "leaderboard",
    "arena",
    "rubric",
    "LLM-as-a-Judge",
    "评测",
    "测评",
    "实测",
    "评估",
    "评价指标",
    "基准",
    "测试集",
    "数据集",
    "榜单",
    "排行榜",
    "SWE-Bench",
    "Terminal-Bench",
    "ProgramBench",
    "SWE Atlas",
    "HumanEval",
    "LiveCodeBench",
    "CursorBench",
    "MMLU",
    "GPQA",
    "LMArena",
    "Chatbot Arena",
    "WebArena",
    "GAIA",
    "OSWorld",
    "tau-bench",
    "BrowserGym",
    "NewsBench",
    "ESI-Bench",
    "HealthBench",
    "red team",
    "jailbreak",
]

HIGH_SIGNAL_TERMS = [
    "SWE-Bench",
    "Terminal-Bench",
    "ProgramBench",
    "SWE Atlas",
    "HumanEval",
    "LiveCodeBench",
    "LMArena",
    "Chatbot Arena",
    "WebArena",
    "GAIA",
    "OSWorld",
    "tau-bench",
    "NewsBench",
    "ESI-Bench",
    "HealthBench",
    "LLM-as-a-Judge",
    "eval harness",
    "eval suite",
    "rubric",
    "benchmark",
    "leaderboard",
    "评测方法",
    "评估体系",
    "评价指标",
    "数据集",
    "榜单",
]


def _safe_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    start = text.find("{")
    if start >= 0:
        text = text[start:]
    data, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    return data


def _text_blob(item: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get(key) or "")
        for key in ("title", "ai_summary", "description", "content")
    )


def _score_candidate(item: dict[str, Any]) -> int:
    text = _text_blob(item).lower()
    score = 0
    for term in HIGH_SIGNAL_TERMS:
        if term.lower() in text:
            score += 4
    for term in EVAL_TERMS:
        if term.lower() in text:
            score += 1
    # Existing model/tutorial labels are useful, but do not swamp benchmark signals.
    cats = json.dumps(item.get("ai_categories") or item.get("ai_category") or "", ensure_ascii=False).lower()
    if "models" in cats:
        score += 1
    if "tutorials" in cats:
        score += 1
    return score


def _candidate_query(days: int, limit: int, scan_limit: int, statement_timeout_sec: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: list[Any] = [cutoff, scan_limit]
    candidate_sql = """
        SELECT
          i.id,
          i.platform,
          i.title,
          i.url,
          i.author_name,
          left(coalesce(i.description, ''), 1000) AS description,
          left(coalesce(i.ai_summary, ''), 1600) AS ai_summary,
          i.ai_category,
          i.ai_categories,
          i.ai_subcategories,
          i.fetched_at,
          i.published_at,
          coalesce(i.published_at, i.fetched_at) AS sort_at
        FROM items i
        WHERE i.visible = 1
          AND i.fetched_at >= %s
        ORDER BY i.fetched_at DESC
        LIMIT %s
    """
    with remote_db.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_sec)}s'")
        rows = conn.execute(candidate_sql, params).fetchall()
        candidates = [dict(row) for row in rows]
        candidates = [item for item in candidates if _score_candidate(item) > 0]
        candidates.sort(key=lambda item: (_score_candidate(item), str(item.get("sort_at") or "")), reverse=True)
        candidates = candidates[:limit]
        ids = [row["id"] for row in candidates]
        content_by_id: dict[str, str] = {}
        if ids:
            detail_rows = conn.execute(
                """
                SELECT i.id, left(coalesce(i.content, ''), 3500) AS content
                  FROM items i
                 WHERE i.id = ANY(%s)
                """,
                [ids],
            ).fetchall()
            content_by_id = {row["id"]: row.get("content") or "" for row in detail_rows}
    for item in candidates:
        item["content"] = content_by_id.get(item["id"], "")
    candidates.sort(key=lambda item: (_score_candidate(item), str(item.get("sort_at") or "")), reverse=True)
    return candidates


def _build_prompt(categories: list[dict[str, Any]]) -> str:
    category_block = enrich_items.build_category_block(categories)
    return f"""你是 info2action 的分类 dry-run 审核员。你只判断分类字段，不写摘要。

请判断输入 item 是否应该进入 `eval`（评测）以及对应 L2。输出必须是 JSON object，不要 markdown。

核心口径：
- `eval` 只用于 AI 领域评测知识资产：让读者学习评测经验、评测知识、评测结论、评测资源或 benchmark。
- 当主体是“怎么评、用什么评、评出了什么、评测是否可信”时，归 `eval`。
- 普通产品体验、上手测评、模型发布顺带列分数、非 AI benchmark、AI 竞赛/性能优化新闻，不进 `eval`。
- 普通行业报告、投研叙事、就业/资本开支数据的 fact-check 或可信度质疑，即使与 AI 有关，也不进 `eval_reliability`；只有 benchmark、eval suite、榜单、评测集或 AI 评测方法本身的可信度争议才进 `eval`。
- 如果内容只是可能启发评测体系，但没有直接讲评测，也可以进 `eval`，但 reason 必须说明可借鉴点。

输出字段：
{{
  "categories": ["eval"],
  "subcategories": ["model_eval", "eval_methods"],
  "confidence": "high" | "medium" | "low",
  "reason": "50字以内，说明为什么进或不进 eval"
}}

分类体系：
{category_block}
"""


def _item_payload(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            "id": item.get("id"),
            "platform": item.get("platform"),
            "title": item.get("title"),
            "url": item.get("url"),
            "author": item.get("author_name"),
            "existing_ai_category": item.get("ai_category"),
            "existing_ai_categories": item.get("ai_categories"),
            "existing_ai_subcategories": item.get("ai_subcategories"),
            "ai_summary": item.get("ai_summary"),
            "description": item.get("description"),
            "content_excerpt": item.get("content"),
        },
        ensure_ascii=False,
    )


def _normalize_result(raw: str, valid_l1: set[str], valid_l2: set[str]) -> dict[str, Any]:
    data = _safe_json(raw)
    categories = data.get("categories") or []
    if not isinstance(categories, list):
        categories = []
    categories = [str(cat).strip().lower() for cat in categories if str(cat).strip().lower() in valid_l1]
    subcategories = data.get("subcategories") or []
    if not isinstance(subcategories, list):
        subcategories = []
    subcategories = [str(sub).strip().lower() for sub in subcategories if str(sub).strip().lower() in valid_l2]
    confidence = str(data.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    return {
        "categories": categories[:3],
        "subcategories": subcategories,
        "confidence": confidence,
        "reason": str(data.get("reason") or "").strip()[:200],
    }


def _confidence_passes(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK.get(minimum, 2)


def _apply_eval_updates(outputs: list[dict[str, Any]], minimum_confidence: str) -> list[dict[str, Any]]:
    updates = [
        item for item in outputs
        if "eval" in item.get("categories", [])
        and _confidence_passes(str(item.get("confidence") or ""), minimum_confidence)
    ]
    if not updates:
        return []
    schema = remote_db.remote_schema()
    with remote_db.connect() as conn:
        conn.execute("SET LOCAL statement_timeout = '15s'")
        for item in updates:
            categories = item.get("categories") or ["eval"]
            subcategories = item.get("subcategories") or ["other"]
            multi_l1_reason = item.get("reason") if len(categories) > 1 else None
            conn.execute(
                f"""
                UPDATE {schema}.items
                   SET ai_category = %s,
                       ai_categories = %s::jsonb,
                       ai_subcategories = %s::jsonb,
                       multi_l1_reason = %s
                 WHERE id = %s
                """,
                (
                    categories[0],
                    json.dumps(categories, ensure_ascii=False),
                    json.dumps(subcategories, ensure_ascii=False),
                    multi_l1_reason,
                    item["id"],
                ),
            )
        conn.commit()
    remote_db.clear_feed_cache_keys(clear_remote_snapshots=True)
    return updates


def run(args: argparse.Namespace) -> int:
    classification = enrich_items.load_classification()
    categories = classification.get("categories") or []
    valid_l1 = {str(cat.get("id") or "") for cat in categories}
    valid_l2 = {
        str(sub.get("id") or "")
        for cat in categories
        for sub in (cat.get("subcategories") or [])
    }
    candidates = _candidate_query(
        args.days,
        args.candidate_limit,
        args.scan_limit,
        args.db_statement_timeout_sec,
    )
    print(
        f"[eval-backfill] days={args.days} candidates={len(candidates)} "
        f"candidate_limit={args.candidate_limit} scan_limit={args.scan_limit} apply={int(args.apply)}",
        flush=True,
    )
    if args.list_only:
        for item in candidates[: args.max_llm]:
            print(f"- score={_score_candidate(item):02d} {item['id']} {item.get('title')}")
        return 0

    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        config.get("ai_summary", {})
    )
    if not api_key:
        print("ERROR: MiniMax API key missing", file=sys.stderr)
        return 1

    prompt = _build_prompt(categories)
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=args.request_interval_sec)
    output_path = Path(args.output) if args.output else None
    resumed_by_id: dict[str, dict[str, Any]] = {}
    if args.resume_output and output_path and output_path.exists():
        try:
            existing_outputs = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(existing_outputs, list):
                resumed_by_id = {
                    str(item.get("id")): item
                    for item in existing_outputs
                    if isinstance(item, dict) and item.get("id")
                }
        except (OSError, ValueError, TypeError):
            resumed_by_id = {}
    outputs: list[dict[str, Any]] = []
    for idx, item in enumerate(candidates[: args.max_llm], 1):
        if item["id"] in resumed_by_id:
            output = dict(resumed_by_id[item["id"]])
            output.pop("applied", None)
        else:
            try:
                raw = enrich_items.call_minimax(
                    api_key,
                    api_base,
                    model,
                    prompt,
                    _item_payload(item),
                    max_tokens=args.max_tokens,
                    rate_gate=gate,
                )
                result = _normalize_result(raw, valid_l1, valid_l2)
            except Exception as exc:  # noqa: BLE001
                result = {
                    "categories": [],
                    "subcategories": [],
                    "confidence": "low",
                    "reason": f"parse_or_llm_error: {str(exc)[:120]}",
                }
            output = {
                "id": item["id"],
                "title": item.get("title") or "",
                "platform": item.get("platform") or "",
                "sort_at": str(item.get("sort_at") or item.get("published_at") or item.get("fetched_at") or ""),
                "existing": {
                    "ai_category": item.get("ai_category"),
                    "ai_categories": item.get("ai_categories"),
                    "ai_subcategories": item.get("ai_subcategories"),
                },
                "score": _score_candidate(item),
                "link": f"{LOCAL_ITEM_BASE}{item['id']}",
                **result,
            }
        outputs.append(output)
        mark = "EVAL" if "eval" in output["categories"] else "skip"
        print(
            f"[{idx:02d}/{min(args.max_llm, len(candidates)):02d}] {mark} "
            f"{output['id']} L1={output['categories']} L2={output['subcategories']} "
            f"conf={output['confidence']} title={output['title'][:80]}"
            f"{' [resume]' if item['id'] in resumed_by_id else ''}",
            flush=True,
        )
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(outputs, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if output_path:
        print(f"[eval-backfill] wrote {output_path}")

    eval_outputs = [item for item in outputs if "eval" in item["categories"]]
    applied: list[dict[str, Any]] = []
    if args.apply:
        applied = _apply_eval_updates(outputs, args.min_confidence)
        applied_ids = {item["id"] for item in applied}
        for item in outputs:
            item["applied"] = item["id"] in applied_ids
        if args.output:
            path = Path(args.output)
            path.write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[eval-backfill] llm_checked={len(outputs)} eval_hits={len(eval_outputs)} "
        f"applied={len(applied)} min_confidence={args.min_confidence}"
    )
    print("\n# Eval hits")
    for item in eval_outputs:
        print(
            f"- {item['title']} | L2={','.join(item['subcategories']) or '<none>'} | "
            f"{item['reason']} | {item['link']}"
        )
    print("\n# Skipped / borderline")
    for item in outputs:
        if "eval" in item["categories"]:
            continue
        print(f"- {item['title']} | {item['reason']} | {item['link']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run eval category candidate review")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--candidate-limit", type=int, default=60)
    parser.add_argument("--scan-limit", type=int, default=1000)
    parser.add_argument("--max-llm", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--request-interval-sec", type=float, default=0.8)
    parser.add_argument("--db-statement-timeout-sec", type=int, default=12)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--resume-output", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write eval classification fields")
    parser.add_argument(
        "--min-confidence",
        choices=("low", "medium", "high"),
        default="medium",
        help="minimum confidence required for --apply",
    )
    args = parser.parse_args()

    args.days = max(1, min(args.days, 60))
    args.candidate_limit = max(1, min(args.candidate_limit, 200))
    args.scan_limit = max(args.candidate_limit, min(args.scan_limit, 3000))
    args.max_llm = max(1, min(args.max_llm, args.candidate_limit))
    args.db_statement_timeout_sec = max(5, min(args.db_statement_timeout_sec, 60))
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
