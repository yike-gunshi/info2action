#!/usr/bin/env python3
"""Offline evaluation gate for the v26 item scoring prompt."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import enrich_items  # noqa: E402
from env_utils import load_project_env  # noqa: E402
from highlight_score_v26 import (  # noqa: E402
    compute_score10,
    is_flag_bearer,
    normalize_score_result,
)


FEATURE_DIR = ROOT / ".features" / "highlights-refactor-v26"
INPUT_FILE = FEATURE_DIR / "标注-input.json"
GOLD_FILE = FEATURE_DIR / "标注-金标.json"
PROMPT_FILE = ROOT / "prompts" / "15_item_score_v26.md"
RESULT_FILE = FEATURE_DIR / "门禁-结果.json"
REPORT_FILE = FEATURE_DIR / "门禁-报告.md"
THRESHOLDS = [round(4.0 + step * 0.25, 2) for step in range(13)]


def build_user_content(item: dict[str, Any]) -> str:
    return "\n".join([
        f"title: {item.get('title') or ''}",
        f"platform: {item.get('platform') or ''}",
        f"author: {item.get('author') or ''}",
        f"content: {item.get('excerpt') or ''}",
    ])


def load_labeled_items(*, dry_run: bool) -> list[dict[str, Any]]:
    inputs = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    gold = json.loads(GOLD_FILE.read_text(encoding="utf-8"))
    if not isinstance(inputs, list) or not isinstance(gold, dict):
        raise ValueError("标注输入必须是数组，金标必须是对象")
    if len(inputs) != 52 or len(gold) != 52:
        raise ValueError(f"标注集条数异常：input={len(inputs)} gold={len(gold)}，预期均为 52")

    rows: list[dict[str, Any]] = []
    for item in inputs[:3] if dry_run else inputs:
        n = item.get("n")
        gold_entry = gold.get(str(n))
        label = gold_entry.get("label") if isinstance(gold_entry, dict) else gold_entry
        if label not in {"进", "不进"}:
            raise ValueError(f"n={n} 的金标非法或缺失：{label!r}")
        missing = [key for key in ("n", "title", "author", "platform", "excerpt") if key not in item]
        if missing:
            raise ValueError(f"n={n} 缺少字段：{','.join(missing)}")
        rows.append({**item, "label": label, "runs": []})
    return rows


def load_env_values(base_dir: Path) -> dict[str, str]:
    """Load this worktree's .env, falling back to the primary worktree."""
    local_values = load_project_env(base_dir)
    git_marker = base_dir / ".git"
    if not git_marker.is_file():
        return local_values
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
        git_dir_text = marker.split("gitdir:", 1)[1].strip()
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = (base_dir / git_dir).resolve()
        primary_root = next(parent.parent for parent in git_dir.parents if parent.name == ".git")
    except (IndexError, OSError, StopIteration):
        return local_values
    values = load_project_env(primary_root)
    values.update(local_values)
    return values


def load_runtime() -> tuple[str, str, str]:
    for key, value in load_env_values(ROOT).items():
        os.environ.setdefault(key, value)
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        config.get("ai_summary", {})
    )
    if not api_key:
        raise RuntimeError("MiniMax API key missing in .env/config")
    return api_key, api_base, model


def score_one_run(
    item: dict[str, Any],
    *,
    api_key: str,
    api_base: str,
    model: str,
    system_prompt: str,
    rate_gate: enrich_items.MiniMaxRateLimitGate | None,
    call_fn: Callable[..., str] = enrich_items.call_minimax,
) -> dict[str, Any]:
    last_error = "unknown LLM error"
    for attempt in range(1, 4):
        try:
            raw = call_fn(
                api_key,
                api_base,
                model,
                system_prompt,
                build_user_content(item),
                max_tokens=2048,
                rate_gate=rate_gate,
                temperature=0.0,
            )
            normalized = normalize_score_result(raw)
            if normalized.get("error"):
                raise ValueError(str(normalized["error"]))
            return {
                "score10": compute_score10(normalized),
                "veto": normalized.get("veto"),
                "normalized": normalized,
                "error": None,
                "attempts": attempt,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:500]
    return {
        "score10": None,
        "veto": None,
        "normalized": None,
        "error": last_error,
        "attempts": 3,
    }


def _flag_for_run(run: dict[str, Any], threshold: float) -> bool:
    normalized = run.get("normalized")
    if run.get("error") or not isinstance(normalized, dict):
        return False
    return is_flag_bearer(normalized, run.get("score10"), threshold)


def scan_thresholds(
    items: list[dict[str, Any]],
    *,
    thresholds: list[float] = THRESHOLDS,
) -> list[dict[str, Any]]:
    positive_total = sum(item["label"] == "进" for item in items)
    negative_total = sum(item["label"] == "不进" for item in items)
    if not positive_total or not negative_total:
        raise ValueError("评测集必须同时包含“进”和“不进”金标")

    table: list[dict[str, Any]] = []
    for threshold in thresholds:
        positive_kept = sum(
            item["label"] == "进" and _flag_for_run(item["runs"][0], threshold)
            for item in items
        )
        negative_blocked = sum(
            item["label"] == "不进" and not _flag_for_run(item["runs"][0], threshold)
            for item in items
        )
        retention = positive_kept / positive_total
        interception = negative_blocked / negative_total
        table.append({
            "threshold": threshold,
            "retention_rate": round(retention, 4),
            "interception_rate": round(interception, 4),
            "positive_kept": positive_kept,
            "positive_total": positive_total,
            "negative_blocked": negative_blocked,
            "negative_total": negative_total,
            "meets_gate": retention >= 0.95 and interception >= 0.90,
        })
    return table


def choose_best_threshold(scan: list[dict[str, Any]]) -> dict[str, Any]:
    feasible = [row for row in scan if row["meets_gate"]]
    if feasible:
        best = max(feasible, key=lambda row: row["threshold"])
        feasible_thresholds = [row["threshold"] for row in feasible]
        interval = {"min": min(feasible_thresholds), "max": max(feasible_thresholds)}
    else:
        best = max(
            scan,
            key=lambda row: (
                row["retention_rate"] + row["interception_rate"],
                row["threshold"],
            ),
        )
        feasible_thresholds = []
        interval = None
    return {
        "best_threshold": best["threshold"],
        "best_metrics": best,
        "feasible_thresholds": feasible_thresholds,
        "feasible_interval": interval,
    }


def analyze_stability(items: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    unstable: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []
    for item in items:
        if len(item["runs"]) < 2:
            continue
        first, second = item["runs"][:2]
        if first.get("error") or second.get("error"):
            continue
        score1, score2 = first.get("score10"), second.get("score10")
        if score1 is not None and score2 is not None:
            delta = round(abs(score1 - score2), 1)
            if delta > 1.0:
                unstable.append({
                    "n": item["n"],
                    "title": item["title"],
                    "score_run1": score1,
                    "score_run2": score2,
                    "score_delta": delta,
                })
        flag1 = _flag_for_run(first, threshold)
        flag2 = _flag_for_run(second, threshold)
        if flag1 != flag2:
            flips.append({
                "n": item["n"],
                "title": item["title"],
                "flag_run1": flag1,
                "flag_run2": flag2,
            })
    return {
        "score_delta_gt_1": unstable,
        "flag_bearer_flip_count": len(flips),
        "flag_bearer_flips": flips,
    }


def build_dry_run_output(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in items:
        primary = item["runs"][0]
        rows.append({
            "n": item["n"],
            "title": item["title"],
            "金标": item["label"],
            "score10": primary.get("score10"),
            "veto": primary.get("veto"),
            "error": primary.get("error"),
            "runs": [{
                "run": number,
                "score10": run.get("score10"),
                "veto": run.get("veto"),
                "error": run.get("error"),
            } for number, run in enumerate(item["runs"], start=1)],
        })
    return {"dry_run": True, "items": rows}


def _wrong_reason(item: dict[str, Any], threshold: float, flag: bool) -> str:
    run = item["runs"][0]
    normalized = run.get("normalized") or {}
    reason = normalized.get("reason") or normalized.get("reject_reason") or ""
    if flag:
        return f"score10={run['score10']:.1f}≥{threshold:.2f}，且无否决或重大未核实限制"
    if run.get("error"):
        return f"LLM 评分失败：{run['error']}"
    if normalized.get("reject"):
        return f"reject=true；{reason}".rstrip("；")
    if normalized.get("veto") != "none":
        return f"veto={normalized.get('veto')}；{reason}".rstrip("；")
    if normalized.get("uncertainty") == "unverified_major_claim":
        return f"uncertainty=unverified_major_claim；{reason}".rstrip("；")
    score = run.get("score10")
    if score is None:
        return "无可用 score10"
    if score < threshold:
        return f"score10={score:.1f}<{threshold:.2f}；{reason}".rstrip("；")
    return reason or "未通过举旗条件"


def build_output(
    items: list[dict[str, Any]],
    scan: list[dict[str, Any]],
    choice: dict[str, Any],
    stability: dict[str, Any],
    runs: int,
) -> dict[str, Any]:
    threshold = choice["best_threshold"]
    output_items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in items:
        primary = item["runs"][0]
        flag = _flag_for_run(primary, threshold)
        wrong = (item["label"] == "进") != flag
        run_summaries = []
        for run_number, run in enumerate(item["runs"], start=1):
            run_summaries.append({
                "run": run_number,
                "score10": run.get("score10"),
                "veto": run.get("veto"),
                "flag_bearer": _flag_for_run(run, threshold),
                "error": run.get("error"),
            })
            if run.get("error"):
                errors.append({
                    "n": item["n"],
                    "title": item["title"],
                    "run": run_number,
                    "error": run["error"],
                })
        output_items.append({
            "n": item["n"],
            "title": item["title"],
            "score10": primary.get("score10"),
            "veto": primary.get("veto"),
            "flag_bearer@bestT": flag,
            "金标": item["label"],
            "是否错判": wrong,
            "错判原因": _wrong_reason(item, threshold, flag) if wrong else "",
            "error": primary.get("error"),
            "runs": run_summaries if runs > 1 else None,
        })
    return {
        "summary": {
            "runs": runs,
            "bestT": threshold,
            "retention_rate": choice["best_metrics"]["retention_rate"],
            "interception_rate": choice["best_metrics"]["interception_rate"],
            "gate_passed": bool(choice["feasible_thresholds"]),
            "feasible_threshold_interval": choice["feasible_interval"],
            "error_count": len(errors),
        },
        "items": output_items,
        "threshold_scan": scan,
        "stability": stability if runs > 1 else None,
        "errors": errors,
    }


def _table(rows: list[dict[str, Any]], headers: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return ["无。"]
    lines = [
        "| " + " | ".join(label for _, label in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        values = [str(row.get(key, "")).replace("|", "\\|").replace("\n", " ") for key, _ in headers]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def render_report(output: dict[str, Any]) -> str:
    summary = output["summary"]
    rows = output["items"]
    leaked = [
        {"n": row["n"], "title": row["title"], "score": row["score10"], "reason": row["错判原因"]}
        for row in rows if row["金标"] == "不进" and row["flag_bearer@bestT"]
    ]
    killed = [
        {"n": row["n"], "title": row["title"], "score": row["score10"], "reason": row["错判原因"]}
        for row in rows if row["金标"] == "进" and not row["flag_bearer@bestT"]
    ]
    if summary["gate_passed"]:
        interval = summary["feasible_threshold_interval"]
        conclusion = f"存在满足门禁的离散阈值区间 {interval['min']:.2f}–{interval['max']:.2f}。"
    else:
        conclusion = "扫描区间内没有阈值同时满足保留率 ≥95% 与拦截率 ≥90%；bestT 为两率之和最大的折中。"
    lines = [
        "# v26 评测门禁报告",
        "",
        "## 结论",
        "",
        f"- {conclusion}",
        f"- bestT：{summary['bestT']:.2f}",
        f"- 好内容保留率：{summary['retention_rate']:.2%}",
        f"- 垃圾拦截率：{summary['interception_rate']:.2%}",
        f"- LLM 失败记录：{summary['error_count']} 条次",
        "",
        "## 错判清单",
        "",
        "### 漏放的“不进”",
        "",
        *_table(leaked, [("n", "n"), ("title", "标题"), ("score", "score"), ("reason", "原因")]),
        "",
        "### 误杀的“进”",
        "",
        *_table(killed, [("n", "n"), ("title", "标题"), ("score", "score"), ("reason", "原因")]),
    ]
    stability = output.get("stability")
    if stability:
        lines.extend([
            "",
            "## 双跑稳定性",
            "",
            f"- 分差 >1.0：{len(stability['score_delta_gt_1'])} 条",
            f"- flag_bearer 翻转：{stability['flag_bearer_flip_count']} 条",
            "",
            *_table(
                stability["score_delta_gt_1"],
                [("n", "n"), ("title", "标题"), ("score_run1", "run1"), ("score_run2", "run2"), ("score_delta", "分差")],
            ),
        ])
    lines.extend([
        "",
        "## LLM 失败",
        "",
        *_table(output["errors"], [("n", "n"), ("title", "标题"), ("run", "run"), ("error", "错误")]),
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v26 Highlights evaluation gate")
    parser.add_argument("--dry-run", action="store_true", help="score only the first 3 rows and write nothing")
    parser.add_argument("--runs", type=int, choices=(1, 2), default=1, help="score each row once or twice")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items = load_labeled_items(dry_run=args.dry_run)
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    api_key, api_base, model = load_runtime()
    rate_gate = enrich_items.MiniMaxRateLimitGate()
    print(f"[v26-gate] model={model} items={len(items)} runs={args.runs} dry_run={args.dry_run}", flush=True)

    for index, item in enumerate(items, start=1):
        for run_number in range(1, args.runs + 1):
            result = score_one_run(
                item,
                api_key=api_key,
                api_base=api_base,
                model=model,
                system_prompt=system_prompt,
                rate_gate=rate_gate,
            )
            item["runs"].append(result)
            print(
                f"[v26-gate] {index}/{len(items)} n={item['n']} run={run_number} "
                f"score10={result['score10']} veto={result['veto']} error={result['error']}",
                flush=True,
            )

    if args.dry_run:
        print(json.dumps(build_dry_run_output(items), ensure_ascii=False, indent=2))
        return 0

    scan = scan_thresholds(items)
    choice = choose_best_threshold(scan)
    stability = analyze_stability(items, choice["best_threshold"])
    output = build_output(items, scan, choice, stability, args.runs)
    RESULT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_FILE.write_text(render_report(output), encoding="utf-8")
    print(f"[v26-gate] wrote {RESULT_FILE.relative_to(ROOT)}", flush=True)
    print(f"[v26-gate] wrote {REPORT_FILE.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
