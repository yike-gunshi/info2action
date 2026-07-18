#!/usr/bin/env python3
"""Sync admin feedback into a golden set and replay the current item scorer."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(ROOT / "scripts"))

import enrich_items  # noqa: E402
import remote_db  # noqa: E402
from v26_offline_rescore import database_url, load_runtime  # noqa: E402


GOLDEN_FILE = ROOT / "evals" / "highlights" / "golden.jsonl"
REPORT_DIR = GOLDEN_FILE.parent
READ_OPTIONS = "-c statement_timeout=180000 -c default_transaction_read_only=on"
FALSE_POSITIVE_KINDS = {"irrelevant", "low_quality", "should_drop"}
V26_DIMS = ("authority", "substance", "novelty", "timeliness", "audience_fit")
V38_DIMS = ("importance", "novelty", "credibility", "substance", "actionability")


def derive_expectation(
    feedback_kind: str,
    *,
    control: bool = False,
) -> tuple[str, dict[str, bool]]:
    if control:
        return "control", {"include_in_highlights": True}
    if feedback_kind == "should_feature":
        return "miss", {"include_in_highlights": True}
    if feedback_kind in FALSE_POSITIVE_KINDS:
        return "false_positive", {"include_in_highlights": False}
    raise ValueError(f"unsupported feedback kind: {feedback_kind}")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _score_snapshot(row: dict[str, Any]) -> tuple[float | None, dict[str, Any], Any]:
    scores = _json_object(row.get("highlight_scores"))
    v26 = scores.get("v26") if isinstance(scores.get("v26"), dict) else None
    current = v26 or scores
    keys = V26_DIMS if v26 is not None else V38_DIMS
    dims = {key: current.get(key) for key in keys}
    raw_score = current.get("score10")
    if raw_score is None:
        raw_score = row.get("max_flag_score10")
    try:
        score10 = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        score10 = None
    return score10, dims, current.get("veto") or scores.get("veto")


def _pipeline_stage(row: dict[str, Any]) -> str:
    if row.get("entity_type") == "item" and row.get("highlight_verdict") == "drop":
        return "scoring"
    if row.get("cluster_id") is None:
        return "clustering"
    if row.get("cluster_decision") != "included" or not row.get("is_visible_in_feed"):
        return "summary"
    threshold = row.get("display_threshold")
    score = row.get("max_flag_score10")
    below_threshold = False
    if threshold is not None:
        try:
            below_threshold = score is None or float(score) < float(threshold)
        except (TypeError, ValueError):
            below_threshold = True
    if below_threshold or row.get("why_read") is None:
        return "display"
    return "displayed"


def build_case(row: dict[str, Any], *, control: bool = False) -> dict[str, Any]:
    entity_type = str(row.get("entity_type") or "")
    entity_id = str(row.get("entity_id") or "")
    if entity_type not in {"item", "cluster"} or not entity_id:
        raise ValueError("feedback row needs item|cluster entity_type and entity_id")
    feedback_kind = str(row.get("feedback_kind") or "")
    kind, expected = derive_expectation(feedback_kind, control=control)
    raw_content = str(row.get("content") or "").strip()
    excerpt_source = raw_content or str(row.get("ai_summary") or "").strip()
    excerpt = " ".join(excerpt_source.split())[:500]
    score10, dims, veto = _score_snapshot(row)
    created_at = _iso(row.get("feedback_at")) or datetime.now(timezone.utc).isoformat()
    return {
        "case_id": f"fb-{entity_type}-{entity_id}",
        "kind": kind,
        "content_snapshot": {
            "item_id": str(row.get("item_id") or ""),
            "title": row.get("title"),
            "excerpt": excerpt,
            "source": row.get("source") or row.get("platform"),
            "platform": row.get("platform"),
            "url": row.get("url"),
            "published_at": _iso(row.get("published_at")),
        },
        "pipeline_snapshot": {
            "verdict": row.get("highlight_verdict"),
            "score10": score10,
            "dims": dims,
            "veto": veto,
            "uncertainty": row.get("highlight_uncertainty"),
            "stage": _pipeline_stage(row),
            "cluster_id": row.get("cluster_id"),
            "cluster_title": row.get("cluster_title"),
            "cluster_decision": row.get("cluster_decision"),
            "cluster_verdict": row.get("cluster_verdict"),
            "prompt_version": row.get("highlight_prompt_version"),
        },
        "user_judgment": {
            "kind": feedback_kind,
            "note": row.get("feedback_note"),
            "at": created_at,
        },
        "expected": expected,
        "created_at": created_at,
        "snapshot_partial": not bool(raw_content),
    }


def merge_cases(
    existing: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(existing)
    positions = {str(case.get("case_id")): index for index, case in enumerate(merged)}
    for case in incoming:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            raise ValueError("golden case is missing case_id")
        if case_id not in positions:
            positions[case_id] = len(merged)
            merged.append(case)
            continue
        current = merged[positions[case_id]]
        if current.get("kind") == "control" and case.get("kind") != "control":
            merged[positions[case_id]] = case
    return merged


def load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid golden JSONL line {line_number}: {exc}") from exc
        if not isinstance(case, dict) or not case.get("case_id"):
            raise ValueError(f"invalid golden case at line {line_number}")
        cases.append(case)
    return cases


def write_cases(path: Path, cases: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _connect_read_only():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg is required; install project requirements") from exc
    return psycopg.connect(
        database_url(),
        row_factory=dict_row,
        options=READ_OPTIONS,
        connect_timeout=15,
    )


def fetch_remote_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the primary admin's cluster feedback and the dedicated item action."""
    schema = remote_db.remote_schema()
    display_threshold = remote_db._highlights_display_threshold()
    summary_filter = remote_db._highlights_summary_cluster_filter(schema, "c")
    display_filter = remote_db._highlights_display_cluster_filter(
        schema,
        "c",
        threshold=display_threshold,
    )
    representative = f"""JOIN LATERAL (
                SELECT i.id AS item_id,
                       i.title,
                       i.content,
                       i.ai_summary,
                       i.author_name AS source,
                       i.platform,
                       i.url,
                       COALESCE(i.published_at, i.fetched_at) AS published_at,
                       i.highlight_verdict,
                       i.highlight_scores,
                       i.highlight_uncertainty,
                       i.highlight_prompt_version
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = c.id
                 ORDER BY (i.id = d.deciding_item_id) DESC,
                          COALESCE(ci.is_primary_source, false) DESC,
                          ci.rank_in_cluster ASC NULLS LAST,
                          i.id DESC
                 LIMIT 1
              ) rep ON true"""
    with _connect_read_only() as conn:
        admin = conn.execute(
            f"""SELECT id
                  FROM {schema}.users
                 WHERE role = 'admin'
                 ORDER BY created_at ASC NULLS LAST, id ASC
                 LIMIT 1"""
        ).fetchone()
        if not admin:
            raise RuntimeError("no admin user found in remote database")
        admin_id = str(admin["id"])
        cluster_rows = conn.execute(
            f"""SELECT 'cluster' AS entity_type,
                       c.id AS entity_id,
                       rep.*,
                       c.id AS cluster_id,
                       c.ai_title AS cluster_title,
                       c.is_visible_in_feed,
                       c.why_read,
                       d.decision AS cluster_decision,
                       d.cluster_verdict,
                       NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric
                         AS max_flag_score10,
                       cs.feedback_kind,
                       cs.feedback_note,
                       cs.feedback_at
                  FROM {schema}.cluster_status cs
                  JOIN {schema}.clusters c ON c.id = cs.cluster_id
                  LEFT JOIN {schema}.highlight_cluster_decisions d ON d.cluster_id = c.id
                  {representative}
                 WHERE cs.user_id = %s
                   AND cs.feedback_kind IN ('should_feature', 'irrelevant', 'low_quality')
                 ORDER BY cs.feedback_at ASC NULLS LAST, c.id ASC""",
            (admin_id,),
        ).fetchall()
        # item_feedback has no actor column in the existing schema. These two actions
        # are dedicated to the admin funnel, so sync consumes both semantic labels.
        item_rows = conn.execute(
            f"""SELECT DISTINCT ON (fb.item_id)
                       'item' AS entity_type,
                       i.id AS entity_id,
                       i.id AS item_id,
                       i.title,
                       i.content,
                       i.ai_summary,
                       i.author_name AS source,
                       i.platform,
                       i.url,
                       COALESCE(i.published_at, i.fetched_at) AS published_at,
                       i.highlight_verdict,
                       i.highlight_scores,
                       i.highlight_uncertainty,
                       i.highlight_prompt_version,
                       i.cluster_id,
                       c.ai_title AS cluster_title,
                       c.is_visible_in_feed,
                       c.why_read,
                       d.decision AS cluster_decision,
                       d.cluster_verdict,
                       NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric
                         AS max_flag_score10,
                       fb.action AS feedback_kind,
                       fb.reason AS feedback_note,
                       fb.created_at AS feedback_at
                  FROM {schema}.item_feedback fb
                  JOIN {schema}.items i ON i.id = fb.item_id
                  LEFT JOIN {schema}.clusters c ON c.id = i.cluster_id
                  LEFT JOIN {schema}.highlight_cluster_decisions d ON d.cluster_id = c.id
                 WHERE fb.action IN ('should_feature', 'should_drop')
                 ORDER BY fb.item_id, fb.created_at DESC, fb.id DESC"""
        ).fetchall()
        control_rows = conn.execute(
            f"""SELECT 'cluster' AS entity_type,
                       c.id AS entity_id,
                       rep.*,
                       c.id AS cluster_id,
                       c.ai_title AS cluster_title,
                       c.is_visible_in_feed,
                       c.why_read,
                       d.decision AS cluster_decision,
                       d.cluster_verdict,
                       NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric
                         AS max_flag_score10,
                       CASE WHEN cs.starred_at IS NOT NULL THEN 'starred' ELSE 'clicked' END
                         AS feedback_kind,
                       NULL::text AS feedback_note,
                       GREATEST(cs.clicked_at, cs.starred_at) AS feedback_at
                  FROM {schema}.cluster_status cs
                  JOIN {schema}.clusters c ON c.id = cs.cluster_id
                  JOIN {schema}.highlight_cluster_decisions d ON d.cluster_id = c.id
                  {representative}
                 WHERE cs.user_id = %s
                   AND (cs.clicked_at IS NOT NULL OR cs.starred_at IS NOT NULL)
                   AND cs.feedback_kind IS NULL
                   AND d.manual_display IS NULL
                   AND c.is_visible_in_feed = true
                   AND c.published_at IS NOT NULL
                   AND COALESCE(c.archived, false) = false
                   AND c.merged_into IS NULL
                   AND ({summary_filter})
                   {display_filter}
                   AND NOT EXISTS (
                     SELECT 1
                       FROM {schema}.cluster_items ci_feedback
                       JOIN {schema}.item_feedback fb
                         ON fb.item_id = ci_feedback.item_id
                        AND fb.action IN ('should_feature', 'should_drop')
                      WHERE ci_feedback.cluster_id = c.id
                   )
                 ORDER BY c.id ASC""",
            (admin_id,),
        ).fetchall()
    feedback = [dict(row) for row in [*cluster_rows, *item_rows]]
    controls = [dict(row) for row in control_rows]
    for row in [*feedback, *controls]:
        row["display_threshold"] = display_threshold
    return feedback, controls


def run_sync(
    *,
    golden_path: Path = GOLDEN_FILE,
    dry_run: bool = False,
    fetcher: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = fetch_remote_rows,
) -> dict[str, Any]:
    existing = load_cases(golden_path)
    feedback_rows, control_rows = fetcher()
    feedback_cases = [build_case(row) for row in feedback_rows]
    merged = merge_cases(existing, feedback_cases)
    feedback_count = sum(case.get("kind") != "control" for case in merged)
    control_count = sum(case.get("kind") == "control" for case in merged)
    needed_controls = max(0, feedback_count - control_count)
    seen = {str(case.get("case_id")) for case in merged}
    selected_controls: list[dict[str, Any]] = []
    for row in control_rows if needed_controls else []:
        case = build_case(row, control=True)
        if case["case_id"] in seen:
            continue
        selected_controls.append(case)
        seen.add(case["case_id"])
        if len(selected_controls) >= needed_controls:
            break
    merged = merge_cases(merged, selected_controls)
    if not dry_run:
        write_cases(golden_path, merged)
    return {
        "existing": len(existing),
        "feedback": sum(case.get("kind") != "control" for case in merged),
        "controls": sum(case.get("kind") == "control" for case in merged),
        "added": max(0, len(merged) - len(existing)),
        "total": len(merged),
        "dry_run": dry_run,
    }


def _prediction(result: dict[str, Any]) -> tuple[bool, Any, Any]:
    error = result.get("error") or result.get("highlight_last_error")
    if error:
        raise RuntimeError(str(error))
    include = result.get("highlight_include_in_highlights")
    if not isinstance(include, bool):
        include = result.get("is_flag_bearer")
    if not isinstance(include, bool):
        include = result.get("flag_bearer")
    if not isinstance(include, bool):
        raise ValueError("scorer result is missing an inclusion decision")
    verdict = result.get("highlight_verdict")
    if verdict is None:
        verdict = "featured" if include else "drop"
    score10 = result.get("score10")
    return include, verdict, score10


def replay_cases(
    cases: Sequence[dict[str, Any]],
    *,
    scorer: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in cases:
        snapshot = _json_object(case.get("content_snapshot"))
        expected = bool(_json_object(case.get("expected")).get("include_in_highlights"))
        item = {
            "id": str(case.get("case_id") or ""),
            "title": snapshot.get("title"),
            "content": snapshot.get("excerpt"),
            "ai_summary": snapshot.get("excerpt"),
            "source": snapshot.get("source"),
            "author_name": snapshot.get("source"),
            "platform": snapshot.get("platform"),
            "url": snapshot.get("url"),
            "published_at": snapshot.get("published_at"),
        }
        try:
            raw = scorer(item)
            if not isinstance(raw, dict):
                raise ValueError("scorer result is not an object")
            predicted, verdict, score10 = _prediction(raw)
            status = "pass" if predicted == expected else "fail"
            error = None
        except Exception as exc:  # noqa: BLE001
            predicted = None
            verdict = None
            score10 = None
            status = "error"
            error = str(exc)[:500]
        results.append({
            "case_id": str(case.get("case_id") or ""),
            "kind": case.get("kind"),
            "title": snapshot.get("title"),
            "expected_include": expected,
            "predicted_include": predicted,
            "status": status,
            "verdict": verdict,
            "score10": score10,
            "error": error,
        })
    return results


def build_current_scorer() -> Callable[[dict[str, Any]], dict[str, Any]]:
    api_key, api_base, model = load_runtime()
    rate_gate = enrich_items.MiniMaxRateLimitGate()

    def score(item: dict[str, Any]) -> dict[str, Any]:
        result = enrich_items.enrich_highlight_score_for_item(
            item,
            api_key,
            api_base,
            model,
            dry_run=True,
            rate_gate=rate_gate,
        )
        if result is None:
            raise RuntimeError("current scorer returned no result")
        return result

    return score


def _markdown(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _metric_line(label: str, rows: list[dict[str, Any]]) -> str:
    passed = sum(row.get("status") == "pass" for row in rows)
    errors = sum(row.get("status") == "error" for row in rows)
    if not rows:
        return f"- {label}：0/0（N/A，error 0）"
    rate = passed / len(rows) * 100
    return f"- {label}：{passed}/{len(rows)}（{rate:.1f}%，error {errors}）"


def render_report(results: Sequence[dict[str, Any]], *, report_date: str) -> str:
    groups = {
        kind: [row for row in results if row.get("kind") == kind]
        for kind in ("miss", "false_positive", "control")
    }
    lines = [
        f"# 精选金标回放 · {report_date}",
        "",
        "## 汇总",
        "",
        _metric_line("漏放修正率", groups["miss"]),
        _metric_line("误放清除率", groups["false_positive"]),
        _metric_line("对照保持率", groups["control"]),
        "",
        "## 逐案例明细",
        "",
        "| case_id | kind | title | expected | predicted | result | verdict | score10 | error |",
        "|---|---|---|---|---|---|---|---:|---|",
    ]
    for row in results:
        expected = "进精选" if row.get("expected_include") else "不进精选"
        predicted_value = row.get("predicted_include")
        predicted = (
            "error"
            if predicted_value is None
            else "进精选" if predicted_value else "不进精选"
        )
        lines.append(
            "| "
            + " | ".join([
                _markdown(row.get("case_id")),
                _markdown(row.get("kind")),
                _markdown(row.get("title")),
                expected,
                predicted,
                _markdown(row.get("status")),
                _markdown(row.get("verdict")),
                _markdown(row.get("score10")),
                _markdown(row.get("error")),
            ])
            + " |"
        )
    if not results:
        lines.append("| — | — | — | — | — | — | — | — | 无案例 |")
    return "\n".join(lines) + "\n"


def run_replay(
    *,
    golden_path: Path = GOLDEN_FILE,
    report_dir: Path = REPORT_DIR,
    dry_run: bool = False,
    scorer_factory: Callable[[], Callable[[dict[str, Any]], dict[str, Any]]] = build_current_scorer,
    report_date: str | None = None,
) -> dict[str, Any]:
    cases = load_cases(golden_path)
    if dry_run:
        return {"cases": len(cases), "dry_run": True, "report_path": None}
    if not cases:
        return {"cases": 0, "dry_run": False, "report_path": None, "errors": 0}
    results = replay_cases(cases, scorer=scorer_factory())
    day = report_date or date.today().isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"replay-{day}.md"
    report_path.write_text(render_report(results, report_date=day), encoding="utf-8")
    return {
        "cases": len(cases),
        "dry_run": False,
        "report_path": report_path,
        "errors": sum(row.get("status") == "error" for row in results),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync and replay Highlights golden cases")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("sync", "replay"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--dry-run",
            action="store_true",
            help="show the planned work without writing repository artifacts",
        )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "sync":
        summary = run_sync(dry_run=args.dry_run)
        print(
            "[highlights-golden] sync "
            f"existing={summary['existing']} feedback={summary['feedback']} "
            f"controls={summary['controls']} added={summary['added']} "
            f"total={summary['total']} dry_run={summary['dry_run']}",
            flush=True,
        )
        if summary["total"] == 0:
            print("[highlights-golden] no admin feedback or control cases found", flush=True)
        return 0
    result = run_replay(dry_run=args.dry_run)
    if result["dry_run"]:
        print(f"[highlights-golden] replay dry-run cases={result['cases']}", flush=True)
    elif result["report_path"] is None:
        print("[highlights-golden] replay skipped: golden set is empty", flush=True)
    else:
        print(
            f"[highlights-golden] replay cases={result['cases']} "
            f"errors={result['errors']} wrote={result['report_path']}",
            flush=True,
        )
        if result["errors"] == result["cases"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
