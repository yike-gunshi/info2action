#!/usr/bin/env python3
"""Offline evaluation gate for the v27 item scoring prompt."""
from __future__ import annotations

import argparse
from collections import Counter
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
from highlight_score_v26 import compute_score10, normalize_score_result  # noqa: E402


FEATURE_DIR = ROOT / ".features" / "highlights-curation-v27"
INPUT_FILE = FEATURE_DIR / "标注-input.json"
GOLD_FILE = FEATURE_DIR / "标注-金标.json"
PROMPT_FILE = ROOT / "prompts" / "15_item_score_v26.md"
RESULT_FILE = FEATURE_DIR / "门禁-结果.json"
REPORT_FILE = FEATURE_DIR / "门禁-报告.md"
THRESHOLDS = [round(6.0 + step * 0.25, 2) for step in range(9)]
POSITIVE_KIND = "收藏正例"
NEGATIVE_KINDS = ("拼盘日报", "名人一句话", "进度贴")
POSITIVE_TARGET = 0.95
NEGATIVE_TARGET = 0.90


def build_user_content(item: dict[str, Any]) -> str:
    return "\n".join([
        f"title: {item.get('title') or ''}",
        f"platform: {item.get('platform') or ''}",
        f"author: {item.get('author') or ''}",
        f"content: {item.get('excerpt') or ''}",
    ])


def load_labeled_items() -> list[dict[str, Any]]:
    inputs = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    gold = json.loads(GOLD_FILE.read_text(encoding="utf-8"))
    if not isinstance(inputs, list) or not isinstance(gold, dict):
        raise ValueError("标注输入必须是数组，金标必须是对象")
    if len(inputs) != len(gold) or len(inputs) < 10:
        raise ValueError(
            f"input 与 gold 条数必须一致且至少 10 条：input={len(inputs)} gold={len(gold)}"
        )

    rows: list[dict[str, Any]] = []
    for item in inputs:
        n = item.get("n")
        gold_entry = gold.get(str(n))
        if isinstance(gold_entry, dict):
            label = gold_entry.get("label")
            kind = gold_entry.get("kind")
            old_score10 = gold_entry.get("old_score10")
        else:
            label = gold_entry
            kind = None
            old_score10 = None
        if label not in {"进", "不进"}:
            raise ValueError(f"n={n} 的金标非法或缺失：{label!r}")
        missing = [
            key
            for key in ("n", "title", "author", "platform", "excerpt")
            if key not in item
        ]
        if missing:
            raise ValueError(f"n={n} 缺少字段：{','.join(missing)}")
        rows.append({
            **item,
            "label": label,
            "kind": kind,
            "old_score10": old_score10,
            "runs": [],
        })
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


def score_items(
    items: list[dict[str, Any]],
    *,
    runs: int,
    scorer: Callable[[dict[str, Any]], dict[str, Any]],
    on_result: Callable[[int, int, dict[str, Any], dict[str, Any]], None] | None = None,
) -> None:
    """Populate item runs with an injectable scorer."""
    for index, item in enumerate(items, start=1):
        for run_number in range(1, runs + 1):
            result = scorer(item)
            item["runs"].append(result)
            if on_result:
                on_result(index, run_number, item, result)


def _numeric_score(run: dict[str, Any]) -> float | None:
    score = run.get("score10")
    if run.get("error") or isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def _decision_for_run(run: dict[str, Any], display_threshold: float) -> str:
    if run.get("error"):
        return "评分失败"
    score = _numeric_score(run)
    if score is None or score < display_threshold:
        return "不进"
    return "进"


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def calculate_metrics(
    items: list[dict[str, Any]],
    *,
    display_threshold: float,
) -> dict[str, Any]:
    positives = [item for item in items if item.get("kind") == POSITIVE_KIND]
    positive_kept = sum(
        _decision_for_run(item["runs"][0], display_threshold) == "进"
        for item in positives
    )
    positive_rate = _rate(positive_kept, len(positives))
    positive_metrics = {
        "kind": POSITIVE_KIND,
        "kept": positive_kept,
        "total": len(positives),
        "rate": positive_rate,
        "target": POSITIVE_TARGET,
        "meets_target": positive_rate is not None and positive_rate >= POSITIVE_TARGET,
    }

    negative_metrics: dict[str, dict[str, Any]] = {}
    for kind in NEGATIVE_KINDS:
        negatives = [item for item in items if item.get("kind") == kind]
        blocked = sum(
            _decision_for_run(item["runs"][0], display_threshold) == "不进"
            for item in negatives
        )
        interception_rate = _rate(blocked, len(negatives))
        negative_metrics[kind] = {
            "kind": kind,
            "blocked": blocked,
            "total": len(negatives),
            "rate": interception_rate,
            "target": NEGATIVE_TARGET,
            "meets_target": (
                interception_rate is not None and interception_rate >= NEGATIVE_TARGET
            ),
        }

    return {
        "display_threshold": display_threshold,
        "positive_retention": positive_metrics,
        "negative_interception": negative_metrics,
        "gate_passed": (
            positive_metrics["meets_target"]
            and all(row["meets_target"] for row in negative_metrics.values())
        ),
    }


def scan_thresholds(
    items: list[dict[str, Any]],
    *,
    thresholds: list[float] = THRESHOLDS,
) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for threshold in thresholds:
        metrics = calculate_metrics(items, display_threshold=threshold)
        table.append({
            "threshold": threshold,
            "positive_retention": metrics["positive_retention"],
            "negative_interception": metrics["negative_interception"],
            "gate_passed": metrics["gate_passed"],
        })
    return table


def analyze_stability(items: list[dict[str, Any]]) -> dict[str, Any]:
    delta_rows: list[dict[str, Any]] = []
    unevaluable: list[dict[str, Any]] = []
    distribution: Counter[str] = Counter()
    within_1_count = 0
    for item in items:
        if len(item["runs"]) < 2:
            continue
        first, second = item["runs"][:2]
        score1 = _numeric_score(first)
        score2 = _numeric_score(second)
        if score1 is None or score2 is None:
            unevaluable.append({
                "n": item["n"],
                "title": item["title"],
                "run1_error": first.get("error"),
                "run2_error": second.get("error"),
            })
            continue
        delta = round(abs(score1 - score2), 1)
        within_1 = delta <= 1.0
        within_1_count += within_1
        distribution[f"{delta:.1f}"] += 1
        delta_rows.append({
            "n": item["n"],
            "kind": item.get("kind"),
            "score_run1": score1,
            "score_run2": score2,
            "abs_delta": delta,
            "within_1": within_1,
        })

    evaluated_count = len(delta_rows)
    ordered_distribution = dict(
        sorted(distribution.items(), key=lambda pair: float(pair[0]))
    )
    return {
        "evaluated_count": evaluated_count,
        "unevaluable_count": len(unevaluable),
        "within_1_count": within_1_count,
        "within_1_rate": _rate(within_1_count, evaluated_count),
        "delta_distribution": ordered_distribution,
        "items": delta_rows,
        "unevaluable": unevaluable,
    }


def build_dry_run_output(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dry_run": True,
        "samples": [{
            "n": item["n"],
            "kind": item.get("kind"),
            "金标": item["label"],
            "title": item["title"],
            "author": item["author"],
            "platform": item["platform"],
            "excerpt": item["excerpt"],
        } for item in items],
    }


def build_output(
    items: list[dict[str, Any]],
    *,
    metrics: dict[str, Any],
    scan: list[dict[str, Any]],
    stability: dict[str, Any] | None,
    runs: int,
    display_threshold: float,
) -> dict[str, Any]:
    output_items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in items:
        primary = item["runs"][0]
        decision = _decision_for_run(primary, display_threshold)
        normalized = primary.get("normalized")
        dimensions = normalized.get("dims") if isinstance(normalized, dict) else None
        run_summaries = []
        for run_number, run in enumerate(item["runs"], start=1):
            run_summaries.append({
                "run": run_number,
                "score10": run.get("score10"),
                "veto": run.get("veto"),
                "判定": _decision_for_run(run, display_threshold),
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
            "kind": item.get("kind"),
            "old_score10": item.get("old_score10"),
            "new_score10": primary.get("score10"),
            "维度分": dimensions,
            "veto": primary.get("veto"),
            "判定": decision,
            "金标": item["label"],
            "是否符合金标": decision == item["label"],
            "error": primary.get("error"),
            "runs": run_summaries if runs > 1 else None,
        })
    return {
        "summary": {
            "runs": runs,
            "display_threshold": display_threshold,
            "gate_passed": metrics["gate_passed"],
            "error_count": len(errors),
        },
        "metrics": metrics,
        "items": output_items,
        "threshold_scan": scan,
        "stability": stability,
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
        values = [
            str(row.get(key, "")).replace("|", "\\|").replace("\n", " ")
            for key, _ in headers
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def _percent(rate: float | None) -> str:
    return "N/A" if rate is None else f"{rate:.2%}"


def render_report(output: dict[str, Any]) -> str:
    summary = output["summary"]
    metrics = output["metrics"]
    positive = metrics["positive_retention"]
    negative = metrics["negative_interception"]
    metric_rows = [{
        "kind": POSITIVE_KIND,
        "metric": "保留率",
        "passed": f"{positive['kept']}/{positive['total']}",
        "rate": _percent(positive["rate"]),
        "target": "≥95%",
        "meets": "是" if positive["meets_target"] else "否",
    }]
    metric_rows.extend({
        "kind": kind,
        "metric": "拦截率",
        "passed": f"{negative[kind]['blocked']}/{negative[kind]['total']}",
        "rate": _percent(negative[kind]["rate"]),
        "target": "≥90%",
        "meets": "是" if negative[kind]["meets_target"] else "否",
    } for kind in NEGATIVE_KINDS)

    scan_rows = []
    for row in output["threshold_scan"]:
        scan_rows.append({
            "threshold": f"{row['threshold']:.2f}",
            "positive": _percent(row["positive_retention"]["rate"]),
            **{
                kind: _percent(row["negative_interception"][kind]["rate"])
                for kind in NEGATIVE_KINDS
            },
            "meets": "是" if row["gate_passed"] else "否",
        })

    detail_rows = []
    for row in output["items"]:
        detail_rows.append({
            "n": row["n"],
            "kind": row["kind"] or "",
            "old": "" if row["old_score10"] is None else row["old_score10"],
            "new": "" if row["new_score10"] is None else row["new_score10"],
            "dims": (
                "" if row["维度分"] is None
                else json.dumps(row["维度分"], ensure_ascii=False, separators=(",", ":"))
            ),
            "veto": row["veto"] or "",
            "decision": row["判定"],
            "matches": "是" if row["是否符合金标"] else "否",
        })

    lines = [
        "# v27 展示闸门禁评测报告",
        "",
        "## 结论",
        "",
        f"- 展示线：score10 ≥ {summary['display_threshold']:.2f}",
        f"- 门禁结论：{'通过' if summary['gate_passed'] else '未通过'}",
        f"- LLM 失败记录：{summary['error_count']} 条次",
        "",
        "## 分 kind 门禁指标",
        "",
        f"- 正例保留率（收藏正例）：{positive['kept']}/{positive['total']} = {_percent(positive['rate'])}",
        *_table(
            metric_rows,
            [
                ("kind", "kind"),
                ("metric", "指标"),
                ("passed", "命中/总数"),
                ("rate", "比例"),
                ("target", "目标"),
                ("meets", "达标"),
            ],
        ),
        "",
        "## 多阈值扫描（选线参考）",
        "",
        *_table(
            scan_rows,
            [
                ("threshold", "阈值"),
                ("positive", "收藏正例保留率"),
                ("拼盘日报", "拼盘日报拦截率"),
                ("名人一句话", "名人一句话拦截率"),
                ("进度贴", "进度贴拦截率"),
                ("meets", "全部达标"),
            ],
        ),
    ]

    stability = output.get("stability")
    if stability:
        distribution_rows = [
            {"delta": delta, "count": count}
            for delta, count in stability["delta_distribution"].items()
        ]
        lines.extend([
            "",
            "## 双跑稳定性",
            "",
            f"- 可比较：{stability['evaluated_count']} 条",
            f"- 不可比较：{stability['unevaluable_count']} 条",
            f"- |差值|≤1：{stability['within_1_count']}/{stability['evaluated_count']} = {_percent(stability['within_1_rate'])}",
            "",
            *_table(distribution_rows, [("delta", "|score10 差值|"), ("count", "条数")]),
        ])

    lines.extend([
        "",
        "## 样本明细",
        "",
        *_table(
            detail_rows,
            [
                ("n", "n"),
                ("kind", "kind"),
                ("old", "old_score10"),
                ("new", "new_score10"),
                ("dims", "维度分"),
                ("veto", "veto"),
                ("decision", "判定"),
                ("matches", "是否符合金标"),
            ],
        ),
        "",
        "## LLM 失败",
        "",
        *_table(
            output["errors"],
            [("n", "n"), ("title", "标题"), ("run", "run"), ("error", "错误")],
        ),
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v27 Highlights display gate")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the labeled sample list without calling the LLM or writing files",
    )
    parser.add_argument(
        "--runs",
        type=int,
        choices=(1, 2),
        default=1,
        help="score each row once or twice",
    )
    parser.add_argument(
        "--display-threshold",
        type=float,
        default=7.0,
        help="minimum score10 displayed by the gate (default: 7.0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items = load_labeled_items()
    if args.dry_run:
        print(f"[v27-gate] dry-run samples={len(items)}", flush=True)
        print(json.dumps(build_dry_run_output(items), ensure_ascii=False, indent=2))
        return 0

    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    api_key, api_base, model = load_runtime()
    rate_gate = enrich_items.MiniMaxRateLimitGate()
    print(
        f"[v27-gate] model={model} items={len(items)} runs={args.runs} "
        f"display_threshold={args.display_threshold:.2f}",
        flush=True,
    )

    def scorer(item: dict[str, Any]) -> dict[str, Any]:
        return score_one_run(
            item,
            api_key=api_key,
            api_base=api_base,
            model=model,
            system_prompt=system_prompt,
            rate_gate=rate_gate,
        )

    def print_result(
        index: int,
        run_number: int,
        item: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        print(
            f"[v27-gate] {index}/{len(items)} n={item['n']} run={run_number} "
            f"score10={result['score10']} veto={result['veto']} error={result['error']}",
            flush=True,
        )

    score_items(items, runs=args.runs, scorer=scorer, on_result=print_result)
    metrics = calculate_metrics(items, display_threshold=args.display_threshold)
    scan = scan_thresholds(items)
    stability = analyze_stability(items) if args.runs == 2 else None
    output = build_output(
        items,
        metrics=metrics,
        scan=scan,
        stability=stability,
        runs=args.runs,
        display_threshold=args.display_threshold,
    )
    RESULT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_FILE.write_text(render_report(output), encoding="utf-8")
    print(f"[v27-gate] wrote {RESULT_FILE.relative_to(ROOT)}", flush=True)
    print(f"[v27-gate] wrote {REPORT_FILE.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
