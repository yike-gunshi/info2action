#!/usr/bin/env python3
"""Offline item-level scoring calibration for cluster-highlight-scoring-v2.

Pipeline (mirrors the LLM/derive split so config tweaks don't re-call the LLM):
  score    : snapshot.jsonl -> extract items -> LLM (prompt 12) -> items_scored.jsonl
  derive   : items_scored.jsonl + scoring_v2_draft.json -> quality/time/rule verdict
  analyze  : distribution + duplicate-sample stability (the 体检)
  review-server : blind-first manual review page (hide rule verdict until human label)

No mode writes remote tables, classification.json, or highlights_v1. Offline only.
Decisions: docs/讨论/highlights-refresh/2026-06-11-cluster-scoring-redesign-decisions.md
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items  # noqa: E402
from prompt_loader import load_prompt  # noqa: E402

DEFAULT_STATE_DIR = ROOT / "data" / "highlights_score_calibration"
DEFAULT_SNAPSHOT_FILE = DEFAULT_STATE_DIR / "snapshot.jsonl"
DEFAULT_SCORED_FILE = DEFAULT_STATE_DIR / "items_scored_v2.jsonl"
DEFAULT_DERIVED_FILE = DEFAULT_STATE_DIR / "items_derived_v2.jsonl"
DEFAULT_ANALYSIS_FILE = DEFAULT_STATE_DIR / "analysis_v2.json"
DEFAULT_REVIEW_FILE = DEFAULT_STATE_DIR / "human_review_v2_blind.csv"
DEFAULT_CONFIG_FILE = ROOT / "config" / "scoring_v2_draft.json"
PROMPT_FILE = "12_item_score_v2.md"

POSITIVE_DIMS = ("importance", "novelty", "credibility", "substance", "actionability")
VALUE_DIMS = ("importance", "novelty", "substance", "actionability")  # any=3 -> strong value
ALL_SCORE_DIMS = POSITIVE_DIMS + ("spam_score", "time_sensitivity")


# ----------------------------- io helpers -----------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                out.append(json.loads(text))
    return out


def load_scoring_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_env_file(path_value: str) -> int:
    """Load an optional .env file/dir into os.environ (no secret values printed)."""
    if not path_value:
        return 0
    path = Path(path_value).expanduser()
    values: dict[str, str] = {}
    if path.is_dir():
        from env_utils import load_project_env  # noqa: PLC0415
        values = load_project_env(path)
    elif path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line[len("export "):].strip() if line.startswith("export ") else line
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                v = v[1:-1]
            if k:
                values[k] = v
    else:
        raise FileNotFoundError(f"env file not found: {path}")
    loaded = 0
    for k, v in values.items():
        if k not in os.environ:
            os.environ[k] = v
            loaded += 1
    return loaded


# ----------------------------- item extraction -----------------------------

def _category_id(value: Any) -> str:
    return str(value or "").split("[", 1)[0].strip().lower()


def iter_snapshot_items(snapshot_path: Path, *, include_duplicates: bool) -> list[dict[str, Any]]:
    """Flatten cluster snapshot into per-item records, keeping cluster grouping.

    Duplicate cluster variants re-surface the same item ids; that is the
    item-level duplicate signal used for stability analysis.
    """
    items: list[dict[str, Any]] = []
    for cluster in _read_jsonl(snapshot_path):
        variant = str(cluster.get("sample_variant") or "original")
        if not include_duplicates and variant != "original":
            continue
        sample_key = str(cluster.get("sample_key") or cluster.get("cluster_id") or "")
        for source in cluster.get("sources") or []:
            if not isinstance(source, dict):
                continue
            items.append({
                "cluster_sample_key": sample_key,
                "sample_variant": variant,
                "cluster_id": cluster.get("cluster_id") or cluster.get("id") or "",
                "cluster_title": cluster.get("title") or cluster.get("ai_title") or "",
                "item_id": source.get("id") or "",
                "title": source.get("title") or "",
                "platform": source.get("platform") or "",
                "source": source.get("source") or source.get("author_name") or "",
                "url": source.get("url") or "",
                "ai_summary": source.get("ai_summary") or source.get("summary") or "",
                "ai_category": _category_id(source.get("ai_category")),
                "published_at": source.get("published_at") or "",
                "fetched_at": source.get("fetched_at") or "",
            })
    return items


def build_item_payload(item: dict[str, Any]) -> str:
    lines = [
        f"title: {item.get('title') or ''}",
        f"summary: {item.get('ai_summary') or ''}",
        f"platform: {item.get('platform') or ''}",
        f"source: {item.get('source') or ''}",
        f"published_at: {item.get('published_at') or ''}",
        f"ai_category: {item.get('ai_category') or ''}",
    ]
    return "\n".join(lines)


# ----------------------------- LLM scoring -----------------------------

def _first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return text


def _clamp_tier(value: Any) -> int:
    try:
        return max(1, min(3, int(round(float(value)))))
    except (TypeError, ValueError):
        return 2


def parse_score_response(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = json.loads(_first_json_object(text))
    if not isinstance(obj, dict):
        raise ValueError("score result must be a JSON object")
    parsed: dict[str, Any] = {dim: _clamp_tier(obj.get(dim)) for dim in ALL_SCORE_DIMS}
    parsed["ai_relevant"] = "no" if str(obj.get("ai_relevant") or "yes").strip().lower() == "no" else "yes"
    borderline = obj.get("borderline")
    parsed["borderline"] = [str(d).strip() for d in borderline if str(d).strip() in ALL_SCORE_DIMS] if isinstance(borderline, list) else []
    parsed["reason"] = str(obj.get("reason") or "")
    return parsed


def score_item(item: dict[str, Any], *, system_prompt: str, api_key: str, api_base: str,
               model: str, max_tokens: int, temperature: float,
               rate_gate: enrich_items.MiniMaxRateLimitGate) -> dict[str, Any]:
    raw = enrich_items.call_minimax(
        api_key, api_base, model, system_prompt, build_item_payload(item),
        max_tokens=max_tokens, temperature=temperature, rate_gate=rate_gate,
    )
    parsed = parse_score_response(raw)
    return {**item, **parsed, "scored_at": _iso_now()}


# ----------------------------- derive: quality / time / rule -----------------------------

def _normalize(score: int) -> float:
    return (max(1, min(3, int(score))) - 1) / 2.0  # 1->0, 2->0.5, 3->1


def _profile(config: dict[str, Any], category: str) -> dict[str, str]:
    profiles = config.get("category_profiles") or {}
    if category in profiles:
        return profiles[category]
    return config.get("default_profile") or {d: "中" for d in POSITIVE_DIMS}


def compute_quality(dims: dict[str, int], spam: int, category: str, config: dict[str, Any]) -> float:
    tier_w = config.get("tier_weights") or {"高": 3, "中": 2, "低": 1}
    profile = _profile(config, category)
    weights = {d: float(tier_w.get(profile.get(d, "中"), 2)) for d in POSITIVE_DIMS}
    total_w = sum(weights.values()) or 1.0
    base = sum(weights[d] * _normalize(dims.get(d, 2)) for d in POSITIVE_DIMS) / total_w
    spam_mult = float((config.get("spam") or {}).get("penalty_multiplier", {}).get(str(spam), 1.0))
    return round(base * spam_mult, 4)


def _age_hours(published_at: str, fetched_at: str, now: datetime) -> float | None:
    for value in (published_at, fetched_at):
        text = str(value or "").strip()
        if not text:
            continue
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - dt).total_seconds() / 3600.0)
        except ValueError:
            continue
    return None


def compute_time_factor(time_sensitivity: int, category: str, published_at: str,
                        fetched_at: str, config: dict[str, Any], now: datetime) -> float:
    decay = config.get("time_decay") or {}
    base = float((decay.get("base_half_life_hours") or {}).get(category, 168))
    scale = float((decay.get("sensitivity_scale") or {}).get(str(time_sensitivity), 1.0))
    eff_half = max(1.0, base * scale)
    age = _age_hours(published_at, fetched_at, now)
    if age is None:
        return 1.0  # G2: missing time -> no penalty
    return round(0.5 ** (age / eff_half), 4)


def item_verdict(rec: dict[str, Any], config: dict[str, Any], *, use_time_gate: bool = False) -> str:
    """featured / review / drop on a single item (rule v2, 2026-06-13)."""
    if str(rec.get("ai_relevant")) == "no":
        return "drop"  # 域门槛
    spam = int(rec.get("spam_score", 1))
    if spam >= int((config.get("spam") or {}).get("exclude_at", 3)):
        return "drop"  # 纯引流/无关卖货/无实质软广不参选
    cred = int(rec.get("credibility", 2))
    imp = int(rec.get("importance", 1))
    sub = int(rec.get("substance", 1))
    act = int(rec.get("actionability", 1))
    nov = int(rec.get("novelty", 1))
    borderline = set(rec.get("borderline") or [])

    strong_dims = [d for d in VALUE_DIMS if int(rec.get(d, 1)) == 3]
    strong_value_confident = any(d not in borderline for d in strong_dims)
    medium_value_count = sum(1 for d in VALUE_DIMS if int(rec.get(d, 1)) >= 2)
    multi_medium_value = medium_value_count >= 3
    time_ok = (not use_time_gate) or int(rec.get("time_sensitivity", 1)) <= 2  # K5: v0 默认不卡时效
    has_value_path = strong_value_confident or multi_medium_value

    if has_value_path and cred >= 2 and time_ok:
        return "featured"
    if imp == 1 and sub == 1 and act == 1 and nov == 1:
        return "drop"
    if cred == 1 and not has_value_path:
        return "drop"
    return "review"


_VERDICT_RANK = {"drop": 0, "review": 1, "featured": 2}


def derive_items(scored: list[dict[str, Any]], config: dict[str, Any], *,
                 use_time_gate: bool = False) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for rec in scored:
        if rec.get("error"):
            out.append({**rec})
            continue
        dims = {d: int(rec.get(d, 2)) for d in POSITIVE_DIMS}
        spam = int(rec.get("spam_score", 1))
        cat = rec.get("ai_category") or "other"
        quality = compute_quality(dims, spam, cat, config)
        time_factor = compute_time_factor(int(rec.get("time_sensitivity", 1)), cat,
                                           rec.get("published_at", ""), rec.get("fetched_at", ""), config, now)
        item_score = round(quality * time_factor, 4)
        verdict = item_verdict(rec, config, use_time_gate=use_time_gate)
        out.append({**rec, "quality_score": quality, "time_factor": time_factor,
                    "item_score": item_score, "item_verdict": verdict})
    return out


def aggregate_clusters(derived: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """cluster verdict = best item verdict; cluster score = max eligible item_score."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in derived:
        if rec.get("error"):
            continue
        groups[str(rec.get("cluster_sample_key") or "")].append(rec)
    clusters: dict[str, dict[str, Any]] = {}
    for key, recs in groups.items():
        originals = [r for r in recs if str(r.get("sample_variant") or "original") == "original"]
        pool = originals or recs
        best = max(pool, key=lambda r: (_VERDICT_RANK.get(r.get("item_verdict"), 0), r.get("item_score", 0)))
        eligible_scores = [r.get("item_score", 0.0) for r in pool if r.get("item_verdict") != "drop"]
        clusters[key] = {
            "cluster_sample_key": key,
            "cluster_title": best.get("cluster_title") or "",
            "cluster_verdict": best.get("item_verdict"),
            "cluster_score": round(max(eligible_scores), 4) if eligible_scores else 0.0,
            "representative_item_id": best.get("item_id"),
            "item_count": len(pool),
        }
    return clusters


# ----------------------------- analyze (stability 体检) -----------------------------

def analyze(derived: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in derived if not r.get("error")]
    clusters = aggregate_clusters(ok)
    dist = {
        "item_count": len(ok),
        "error_count": sum(1 for r in derived if r.get("error")),
        "ai_relevant_counts": dict(Counter(str(r.get("ai_relevant")) for r in ok)),
        "item_verdict_counts": dict(Counter(str(r.get("item_verdict")) for r in ok)),
        "cluster_verdict_counts": dict(Counter(str(c.get("cluster_verdict")) for c in clusters.values())),
        "cluster_count": len(clusters),
        "borderline_rate": round(sum(1 for r in ok if r.get("borderline")) / len(ok), 4) if ok else None,
    }

    # duplicate stability: same item_id scored across cluster variants
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ok:
        if r.get("item_id"):
            by_item[str(r.get("item_id"))].append(r)
    dup = {k: v for k, v in by_item.items() if len(v) >= 2}
    verdict_consistent = 0
    score_ranges: list[float] = []
    dim_flip = Counter()
    for recs in dup.values():
        verdicts = {r.get("item_verdict") for r in recs}
        if len(verdicts) <= 1:
            verdict_consistent += 1
        scores = [float(r.get("item_score", 0.0)) for r in recs]
        score_ranges.append(max(scores) - min(scores))
        for d in ALL_SCORE_DIMS:
            vals = [int(r.get(d, 2)) for r in recs]
            if max(vals) - min(vals) >= 1:
                dim_flip[d] += 1
    stability = {
        "duplicate_item_count": len(dup),
        "verdict_consistency_rate": round(verdict_consistent / len(dup), 4) if dup else None,
        "item_score_range_avg": round(sum(score_ranges) / len(score_ranges), 4) if score_ranges else None,
        "item_score_range_max": round(max(score_ranges), 4) if score_ranges else None,
        "dimension_flip_counts": dict(dim_flip),
    }
    return {"distribution": dist, "stability": stability, "analyzed_at": _iso_now()}


# ----------------------------- review CSV / server -----------------------------

REVIEW_FIELDS = [
    "cluster_sample_key", "cluster_title", "cluster_verdict", "cluster_score",
    "representative_item_id", "items_json",
    "human_verdict", "rule_agree", "error_kind", "human_notes",
]
HUMAN_VERDICT_VALUES = {"featured", "drop"}
RULE_AGREE_VALUES = {"agree", "disagree", "unsure", "unchecked"}
ERROR_KIND_VALUES = {"none", "rule_wrong", "score_wrong", "both", "unchecked"}
EDITABLE_REVIEW_FIELDS = {"human_verdict", "rule_agree", "error_kind", "human_notes"}


def normalize_human_verdict(value: Any) -> str:
    verdict = str(value or "").strip()
    return verdict if verdict in HUMAN_VERDICT_VALUES else ""


def build_review_rows(derived: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in derived:
        if r.get("error") or str(r.get("sample_variant") or "original") != "original":
            continue
        by_cluster[str(r.get("cluster_sample_key") or "")].append(r)
    clusters = aggregate_clusters(derived)
    rows: list[dict[str, Any]] = []
    for key, agg in clusters.items():
        items = [{
            "item_id": r.get("item_id"), "title": r.get("title"), "platform": r.get("platform"),
            "source": r.get("source"), "url": r.get("url"), "ai_summary": r.get("ai_summary"),
            "ai_category": r.get("ai_category"), "ai_relevant": r.get("ai_relevant"),
            "scores": {d: r.get(d) for d in ALL_SCORE_DIMS}, "borderline": r.get("borderline") or [],
            "quality_score": r.get("quality_score"), "time_factor": r.get("time_factor"),
            "item_score": r.get("item_score"), "item_verdict": r.get("item_verdict"), "reason": r.get("reason"),
        } for r in by_cluster.get(key, [])]
        rows.append({
            "cluster_sample_key": key,
            "cluster_title": agg.get("cluster_title"),
            "cluster_verdict": agg.get("cluster_verdict"),
            "cluster_score": agg.get("cluster_score"),
            "representative_item_id": agg.get("representative_item_id"),
            "items_json": json.dumps(items, ensure_ascii=False),
            "human_verdict": "", "rule_agree": "unchecked", "error_kind": "unchecked", "human_notes": "",
        })
    rows.sort(key=lambda r: str(r.get("cluster_sample_key") or ""))
    return rows


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in REVIEW_FIELDS})


def _read_review_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def preserve_review(new_rows: list[dict[str, Any]], existing: list[dict[str, Any]]) -> None:
    by_key = {str(r.get("cluster_sample_key") or ""): r for r in existing}
    for row in new_rows:
        prev = by_key.get(str(row.get("cluster_sample_key") or ""))
        if not prev:
            continue
        for f in EDITABLE_REVIEW_FIELDS:
            val = str(prev.get(f) or "").strip()
            if val:
                row[f] = normalize_human_verdict(val) if f == "human_verdict" else val


def apply_review_updates(path: Path, updates: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _read_review_csv(path)
    by_key = {str(r.get("cluster_sample_key") or ""): r for r in rows}
    updated, missing = 0, []
    for upd in updates:
        key = str(upd.get("cluster_sample_key") or "").strip()
        if key not in by_key:
            missing.append(key)
            continue
        row = by_key[key]
        if "human_verdict" in upd:
            row["human_verdict"] = normalize_human_verdict(upd["human_verdict"])
        if "rule_agree" in upd:
            v = str(upd["rule_agree"] or "").strip()
            row["rule_agree"] = v if v in RULE_AGREE_VALUES else "unchecked"
        if "error_kind" in upd:
            v = str(upd["error_kind"] or "").strip()
            row["error_kind"] = v if v in ERROR_KIND_VALUES else "unchecked"
        if "human_notes" in upd:
            row["human_notes"] = str(upd["human_notes"] or "").strip()
        updated += 1
    write_review_csv(path, rows)
    return {"updated": updated, "missing": missing}


# ----------------------------- mode runners -----------------------------

def run_score(args: argparse.Namespace) -> int:
    items = iter_snapshot_items(Path(args.snapshot_file), include_duplicates=args.include_duplicates)
    if args.limit:
        items = items[:args.limit]
    if not items:
        print(f"[item-cal] no items in {args.snapshot_file}", flush=True)
        return 0
    system_prompt = load_prompt(PROMPT_FILE)
    if not system_prompt:
        raise RuntimeError(f"prompt missing: {PROMPT_FILE}")
    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(config.get("ai_summary", {}))
    if getattr(args, "model", ""):
        model = args.model  # 离线实验显式覆盖（如切到 MiniMax-M3.0）
    if not api_key:
        raise RuntimeError("MiniMax API key missing (use --env-file or export keys)")
    print(f"[item-cal] model={model} max_tokens={args.max_tokens} base={api_base}", flush=True)
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=args.request_interval_sec)
    out_path = Path(args.scored_file)
    if out_path.exists():
        out_path.unlink()
    concurrency = max(1, min(args.concurrency, len(items)))
    print(f"[item-cal] scoring items={len(items)} concurrency={concurrency}", flush=True)

    def work(item: dict[str, Any]) -> dict[str, Any]:
        try:
            return score_item(item, system_prompt=system_prompt, api_key=api_key, api_base=api_base,
                              model=model, max_tokens=args.max_tokens, temperature=args.temperature, rate_gate=gate)
        except Exception as exc:  # noqa: BLE001
            return {**item, "error": f"score_error: {str(exc)[:200]}", "scored_at": _iso_now()}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(work, it): i for i, it in enumerate(items)}
        for n, fut in enumerate(as_completed(futures), start=1):
            rec = fut.result()
            _write_jsonl(out_path, [rec], append=True)
            if n % 20 == 0 or n == len(items):
                print(f"[item-cal] [{n}/{len(items)}] last={str(rec.get('title') or '')[:40]}", flush=True)
    print(f"[item-cal] scored -> {out_path}", flush=True)
    return 0


def run_derive(args: argparse.Namespace) -> int:
    scored = _read_jsonl(Path(args.scored_file))
    if not scored:
        print(f"[item-cal] no scored rows: {args.scored_file}", flush=True)
        return 0
    config = load_scoring_config(Path(args.config_file))
    derived = derive_items(scored, config, use_time_gate=args.use_time_gate)
    _write_jsonl(Path(args.derived_file), derived, append=False)
    print(f"[item-cal] derived rows={len(derived)} -> {args.derived_file}", flush=True)
    return 0


def run_analyze(args: argparse.Namespace) -> int:
    derived = _read_jsonl(Path(args.derived_file))
    if not derived:
        print(f"[item-cal] no derived rows: {args.derived_file}", flush=True)
        return 0
    summary = analyze(derived)
    Path(args.analysis_file).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def run_export_review(args: argparse.Namespace) -> int:
    derived = _read_jsonl(Path(args.derived_file))
    rows = build_review_rows(derived)
    out = Path(args.review_file)
    if out.exists():
        preserve_review(rows, _read_review_csv(out))
    write_review_csv(out, rows)
    print(f"[item-cal] review rows={len(rows)} -> {out}", flush=True)
    return 0


def run_review_server(args: argparse.Namespace) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse
    review_file = Path(args.review_file)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                body = REVIEW_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/rows":
                self._json(_read_review_csv(review_file))
                return
            self._json({"error": "not_found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/label":
                self._json({"error": "not_found"}, status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = apply_review_updates(review_file, payload.get("updates") or [])
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, status=400)
                return
            self._json(result)

        def log_message(self, *a: Any) -> None:
            if not args.quiet:
                super().log_message(*a)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[item-cal] review server: http://{args.host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[item-cal] stopped", flush=True)
    return 0


REVIEW_HTML = """<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Item Score v2 Review</title>
<style>
:root{--bg:#eef1f5;--panel:#fff;--line:#e3e7ee;--line-soft:#eef1f5;--text:#16202c;--text2:#3b4654;--muted:#7b8694;--blue:#2563eb;--blue-soft:#eef4ff;--green:#15803d;--green-soft:#e9f7ef;--green-line:#a7d8ba;--red:#c33425;--red-soft:#fdf0ee;--red-line:#eeb0a8;--amber:#b06000}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased}
header{flex:0 0 auto;z-index:3;display:flex;gap:16px;align-items:center;padding:11px 20px;background:rgba(255,255,255,.92);backdrop-filter:saturate(1.4) blur(8px);border-bottom:1px solid var(--line);box-shadow:0 1px 3px rgba(16,24,40,.04)}
.brand{display:flex;align-items:baseline;gap:9px;min-width:188px}.brand b{font-size:17px;letter-spacing:.2px}.brand span{font-size:12px;color:var(--muted)}
.filters{display:flex;align-items:center;gap:2px;padding:3px;border:1px solid var(--line);border-radius:10px;background:#f4f6f9}
.filter-btn{border:0;background:transparent;color:#56616f;border-radius:7px;padding:6px 12px;font-size:13px;font-weight:600;transition:.12s}
.filter-btn:hover{color:var(--text)}.filter-btn.active{background:#fff;color:var(--blue);box-shadow:0 1px 2px rgba(16,24,40,.14)}
.kbd-hint{font-size:12px;color:var(--muted);display:flex;gap:10px;align-items:center}.kbd-hint b{color:var(--text2);font-weight:700}
kbd{font-family:inherit;font-size:11px;font-weight:700;color:var(--text2);background:#f1f3f7;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;margin:0 1px}
.progress{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);font-weight:600}
.bar{width:120px;height:7px;border-radius:99px;background:#dde3ea;overflow:hidden}.bar>i{display:block;height:100%;background:linear-gradient(90deg,#2563eb,#22a565);transition:width .25s}
main{flex:1 1 auto;min-height:0;display:grid;grid-template-columns:312px minmax(540px,1fr) 348px}
aside{overflow-y:auto;min-height:0;border-right:1px solid var(--line);background:var(--panel)}
.row{position:relative;padding:13px 16px 13px 19px;border-bottom:1px solid var(--line-soft);cursor:pointer;font-size:13px;background:#fff;transition:background .1s}
.row:hover{background:#f7f9fc}.row.active{background:var(--blue-soft)}.row.active:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--blue)}
.row-title{font-weight:600;line-height:1.46;margin-bottom:9px;color:#1d2733;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tag,.status{display:inline-flex;align-items:center;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700;line-height:1.4;letter-spacing:.2px}
.tag{color:#fff}.featured{background:var(--green)}.review{background:var(--amber)}.drop{background:var(--red)}
.status.featured{background:var(--green-soft);color:var(--green)}.status.drop{background:var(--red-soft);color:var(--red)}.status.empty{background:#eef2f6;color:#8a94a2}
section{overflow-y:auto;min-height:0}
.content{padding:32px 40px 80px}#detail{max-width:720px;margin:0 auto}
.side{border-left:1px solid var(--line);background:var(--panel);padding:26px 24px 60px}
.detail-head h2,.panel-head h2{margin:0;font-size:21px;line-height:1.4;letter-spacing:-.2px}
.hint{margin:9px 0 0;color:var(--muted);font-size:13px;line-height:1.5}
.card{border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:18px 0;background:var(--panel);box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h3,.card h4{margin:0 0 4px;font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.doc{padding:18px 0;border-top:1px solid var(--line-soft)}.doc:first-of-type{border-top:0;padding-top:14px}
.doc-title{color:#11253f;text-decoration:none;font-weight:650;line-height:1.5;font-size:16px}.doc-title:hover,.links a:hover{text-decoration:underline}
.meta{display:block;margin-top:5px;color:var(--muted);font-size:12.5px;font-weight:500}
.summary{margin-top:11px;color:var(--text2);line-height:1.72;font-size:15px;white-space:pre-wrap}
.links{margin-top:10px}.links a{font-size:13px;color:var(--blue);text-decoration:none;margin-right:14px;font-weight:600}
button{font:inherit;cursor:pointer}.muted{color:var(--muted);font-size:12.5px;line-height:1.55}
label{display:block;font-size:13px;color:var(--text2);font-weight:650;margin:16px 0 7px}
select,textarea{width:100%;border:1px solid #d3dae3;border-radius:9px;padding:10px 11px;background:#fff;color:var(--text);font-size:14px;transition:.12s}
select:focus,textarea:focus{outline:0;border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
textarea{resize:vertical;line-height:1.6}
.verdict-actions{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0 12px}
.verdict-btn{position:relative;border:1.5px solid var(--line);border-radius:11px;padding:16px 10px 14px;background:#fff;font-weight:750;font-size:15px;transition:.12s;display:flex;flex-direction:column;align-items:center;gap:3px}
.verdict-btn .vk{font-size:11px;font-weight:600;color:var(--muted)}
.verdict-btn[data-verdict=featured]{color:var(--green)}.verdict-btn[data-verdict=drop]{color:var(--red)}
.verdict-btn:hover{transform:translateY(-1px);box-shadow:0 3px 10px rgba(16,24,40,.08)}
.verdict-btn.active[data-verdict=featured]{background:var(--green-soft);border-color:var(--green-line);box-shadow:0 0 0 1px var(--green-line)}
.verdict-btn.active[data-verdict=drop]{background:var(--red-soft);border-color:var(--red-line);box-shadow:0 0 0 1px var(--red-line)}
.verdict-btn:disabled{opacity:.5;cursor:wait;transform:none}
.secondary{border:1px solid #d3dae3;border-radius:9px;padding:9px 14px;background:#fff;color:var(--text2);font-weight:600}.secondary:hover{background:#f7f9fc}
.diagnostic-actions{margin-top:14px}
.scores{display:flex;flex-wrap:wrap;gap:5px;font-size:12px;color:var(--text2);margin:7px 0}
.scores span{background:#f1f4f8;border-radius:6px;padding:2px 7px;font-weight:600}
.bd{color:var(--amber);font-weight:700}
details summary{cursor:pointer;font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;list-style:none}
details summary::-webkit-details-marker{display:none}details summary:before{content:"▸ ";color:var(--blue)}details[open] summary:before{content:"▾ "}
#saved{min-height:18px;font-size:13px;color:var(--green);font-weight:600;opacity:0;transition:opacity .15s;margin-top:8px}#saved.show{opacity:1}
.empty-state{padding:48px 30px;color:var(--muted);text-align:center;font-size:14px}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#cbd3dd;border-radius:99px;border:3px solid var(--panel)}aside::-webkit-scrollbar-thumb,.side::-webkit-scrollbar-thumb{border-color:#fff}
</style></head><body>
<header><div class=brand><b>Item Score v2</b><span>留出集盲判</span></div>
<div class=filters id=filters>
<button type=button class="filter-btn active" data-filter="">全部</button>
<button type=button class=filter-btn data-filter=unlabeled>未标</button>
<button type=button class=filter-btn data-filter=labeled>已标</button>
<button type=button class=filter-btn data-filter=human_featured>进精选</button>
<button type=button class=filter-btn data-filter=human_drop>不进精选</button>
</div>
<div class=kbd-hint><kbd>F</kbd>进精选 <kbd>D</kbd>不进精选 <kbd>J</kbd>/<kbd>K</kbd>切换</div>
<div id=stats class=progress><span id=stat-txt>0/0 已标</span><span class=bar><i id=stat-bar style=width:0%></i></span></div></header>
<main><aside id=list></aside>
<section class=content><div id=detail class=empty-state>选择左侧 cluster</div></section>
<section class=side><div id=fb class=empty-state>—</div></section></main>
<script>
let rows=[],key="",filterValue="";
const L=document.getElementById('list'),D=document.getElementById('detail'),F=document.getElementById('fb'),ST=document.getElementById('stats'),FILTERS=document.getElementById('filters');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pj=(v,d)=>{try{return JSON.parse(v||'')}catch{return d}};
const humanText=v=>v==='featured'?'进精选':(v==='drop'?'不进精选':'未标');
const visibleRows=()=>rows.filter(r=>{const lb=(r.human_verdict||'').trim();if(filterValue==='unlabeled'&&lb)return false;if(filterValue==='labeled'&&!lb)return false;if(filterValue==='human_featured'&&lb!=='featured')return false;if(filterValue==='human_drop'&&lb!=='drop')return false;return true;});
function currentRow(){return rows.find(x=>x.cluster_sample_key===key)}
function render(){
  const vis=visibleRows();if(!vis.some(r=>r.cluster_sample_key===key))key=vis[0]?.cluster_sample_key||"";
  const done=rows.filter(r=>(r.human_verdict||'').trim()).length;const pct=rows.length?Math.round(done/rows.length*100):0;
  document.getElementById('stat-txt').textContent=done+'/'+rows.length+' 已标';document.getElementById('stat-bar').style.width=pct+'%';
  FILTERS.querySelectorAll('.filter-btn').forEach(b=>b.classList.toggle('active',b.dataset.filter===filterValue));
  L.innerHTML=vis.map(r=>{const lb=(r.human_verdict||'').trim();return `<div class="row ${r.cluster_sample_key===key?'active':''}" data-k="${esc(r.cluster_sample_key)}"><div class=row-title>${esc(r.cluster_title||r.cluster_sample_key)}</div><span class="status ${lb?esc(lb):'empty'}">${esc(humanText(lb))}</span></div>`}).join('')||'<div class=empty-state>没有匹配样本</div>';
  L.querySelectorAll('.row').forEach(n=>n.onclick=()=>show(n.dataset.k));
  const ar=L.querySelector('.row.active');if(ar)ar.scrollIntoView({block:'nearest'});
  const r=currentRow();if(r)renderDetail(r);else{D.innerHTML='<div class=empty-state>没有匹配样本</div>';F.innerHTML='<div class=empty-state>—</div>';}
}
function show(k){key=k;render();}
function renderDetail(r){
  const items=pj(r.items_json,[]);
  const verdict=(r.human_verdict||'').trim();
  const hasVerdict=['featured','drop'].includes(verdict);
  const docs=items.map(it=>`<div class=doc><div>${it.item_id?`<a class=doc-title href="https://www.info2act.com/#v=info&d=${encodeURIComponent(it.item_id)}" target=_blank rel=noopener>${esc(it.title)}</a>`:`<b>${esc(it.title)}</b>`}<span class=meta>${esc(it.platform)} · ${esc(it.source)}</span></div><div class=links>${it.url?`<a href="${esc(it.url)}" target=_blank rel=noopener>原文 ↗</a>`:''}</div><div class=summary>${esc(it.ai_summary)}</div></div>`).join('');
  const diag=hasVerdict?`<details class=card open><summary>评分框架结果</summary><div class=muted>规则裁决：<span class="tag ${esc(r.cluster_verdict)}">${esc(r.cluster_verdict)}</span> · cluster_score=${esc(r.cluster_score)}</div>${items.map(it=>`<div class=doc><b>${esc(it.title)}</b> → <span class="tag ${esc(it.item_verdict)}">${esc(it.item_verdict)}</span> 分=${esc(it.item_score)}<div class=scores>${Object.entries(it.scores||{}).map(([k,v])=>`${k}:${v}`).join(' / ')} ${(it.borderline||[]).length?'<span class=bd>borderline:'+esc((it.borderline||[]).join(','))+'</span>':''}</div><div class=muted>${esc(it.reason)}</div></div>`).join('')}</details>`:`<div class=card><b>评分诊断已隐藏</b><div class=muted>保存判断后再查看评分框架和错因定位。</div></div>`;
  D.innerHTML=`<div class=detail-head><h2>${esc(r.cluster_title)}</h2><p class=hint>先看原始 docs 做判断，保存后再对照评分框架。</p></div><div class=card><h3>原始 docs（${items.length}）</h3>${docs}</div>${diag}`;
  const reviewFields=hasVerdict?`<div class=card><h4>评分诊断反馈</h4>
  <label>是否同意规则裁决（${esc(r.cluster_verdict)}）</label>
  <select id=ra><option value=unchecked>未检查</option><option value=agree>同意</option><option value=disagree>不同意</option><option value=unsure>不确定</option></select>
  <label>主要问题</label>
  <select id=ek><option value=unchecked>未检查</option><option value=none>没问题</option><option value=rule_wrong>规则过严/过松</option><option value=score_wrong>LLM 打分不准</option><option value=both>规则和打分都有问题</option></select>
  <div class=diagnostic-actions><button type=button class=secondary id=save-diagnostic>保存诊断</button></div></div>`:`<div class=card><b>诊断反馈稍后填写</b><div class=muted>保存人工判断后，这里会出现评分一致性和主要问题。</div></div>`;
  F.innerHTML=`<div class=panel-head><h2>人工反馈</h2><p class=hint>${hasVerdict?'已判断：'+humanText(verdict):'先写备注，再点判断按钮。'}</p></div>
  <label>整体反馈</label><textarea id=nt rows=5 placeholder="边界样本可以写一句为什么；明确样本可留空。">${esc(r.human_notes||'')}</textarea>
  <div class=verdict-actions><button type=button class="verdict-btn ${verdict==='featured'?'active':''}" data-verdict=featured>进精选<span class=vk>F</span></button><button type=button class="verdict-btn ${verdict==='drop'?'active':''}" data-verdict=drop>不进精选<span class=vk>D</span></button></div>
  <div id=saved></div>${reviewFields}`;
  if(document.getElementById('ra'))document.getElementById('ra').value=r.rule_agree||'unchecked';
  if(document.getElementById('ek'))document.getElementById('ek').value=r.error_kind||'unchecked';
  F.querySelectorAll('.verdict-btn').forEach(btn=>btn.onclick=()=>chooseVerdict(btn.dataset.verdict));
  if(document.getElementById('save-diagnostic'))document.getElementById('save-diagnostic').onclick=saveDiagnostic;
}
function nextUnlabeledAfter(currentKey){
  if(!rows.length)return null;const start=Math.max(0,rows.findIndex(r=>r.cluster_sample_key===currentKey));
  for(let i=1;i<=rows.length;i++){const r=rows[(start+i)%rows.length];if(!(r.human_verdict||'').trim())return r;}
  return null;
}
function toast(text){const sv=document.getElementById('saved');if(!sv)return;sv.textContent=text;sv.classList.add('show');setTimeout(()=>sv.classList.remove('show'),1500);}
async function postUpdate(u){
  const res=await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates:[u]})});
  if(!res.ok){alert('保存失败: '+await res.text());return false;}
  const out=await res.json();if(!out.updated){alert('保存未生效（服务端 updated=0）');return false;}return true;
}
async function chooseVerdict(verdict){
  const r=currentRow();if(!r)return;const buttons=F.querySelectorAll('.verdict-btn');buttons.forEach(b=>b.disabled=true);
  const u={cluster_sample_key:key,human_verdict:verdict,human_notes:document.getElementById('nt')?.value||''};
  if(document.getElementById('ra'))u.rule_agree=document.getElementById('ra').value;
  if(document.getElementById('ek'))u.error_kind=document.getElementById('ek').value;
  try{if(!await postUpdate(u))return;Object.assign(r,u);const next=nextUnlabeledAfter(r.cluster_sample_key);key=next?.cluster_sample_key||r.cluster_sample_key;render();toast(next?'已保存，已跳到下一条':'已保存，未标样本已完成');}
  catch(e){alert('保存出错: '+e);}
  finally{F.querySelectorAll('.verdict-btn').forEach(b=>b.disabled=false);}
}
async function saveDiagnostic(){
  const r=currentRow();if(!r)return;const btn=document.getElementById('save-diagnostic');btn.disabled=true;btn.textContent='保存中...';
  const u={cluster_sample_key:key,human_verdict:r.human_verdict||'',human_notes:document.getElementById('nt')?.value||'',rule_agree:document.getElementById('ra')?.value||'unchecked',error_kind:document.getElementById('ek')?.value||'unchecked'};
  try{if(!await postUpdate(u))return;Object.assign(r,u);render();toast('诊断已保存');}
  catch(e){alert('保存出错: '+e);}
  finally{const b=document.getElementById('save-diagnostic');if(b){b.disabled=false;b.textContent='保存诊断';}}
}
FILTERS.querySelectorAll('.filter-btn').forEach(btn=>btn.onclick=()=>{filterValue=btn.dataset.filter||'';render();});
function moveSel(dir){const vis=visibleRows();if(!vis.length)return;let i=vis.findIndex(r=>r.cluster_sample_key===key);i=i<0?0:(i+dir+vis.length)%vis.length;show(vis[i].cluster_sample_key);}
document.addEventListener('keydown',e=>{const t=(e.target.tagName||'');if(t==='TEXTAREA'||t==='SELECT'||t==='INPUT')return;if(e.metaKey||e.ctrlKey||e.altKey)return;const k=e.key.toLowerCase();
  if(k==='f'){e.preventDefault();chooseVerdict('featured');}
  else if(k==='d'){e.preventDefault();chooseVerdict('drop');}
  else if(k==='j'||k==='arrowdown'){e.preventDefault();moveSel(1);}
  else if(k==='k'||k==='arrowup'){e.preventDefault();moveSel(-1);}});
fetch('/api/rows').then(r=>r.json()).then(d=>{rows=d;render();});
</script></body></html>"""


# ----------------------------- CLI -----------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--snapshot-file", default=str(DEFAULT_SNAPSHOT_FILE))
    common.add_argument("--scored-file", default=str(DEFAULT_SCORED_FILE))
    common.add_argument("--derived-file", default=str(DEFAULT_DERIVED_FILE))
    common.add_argument("--analysis-file", default=str(DEFAULT_ANALYSIS_FILE))
    common.add_argument("--review-file", default=str(DEFAULT_REVIEW_FILE))
    common.add_argument("--config-file", default=str(DEFAULT_CONFIG_FILE))
    common.add_argument("--env-file", default="", help="optional .env file or project dir; values stay out of logs")

    p = argparse.ArgumentParser(description="Item-level scoring calibration (scoring v2, offline)")
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("score", parents=[common])
    s.add_argument("--include-duplicates", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--request-interval-sec", type=float, default=1.0)
    s.add_argument("--max-tokens", type=int, default=2048)  # M2.7 thinking 先吃预算，800 会截断正文
    s.add_argument("--temperature", type=float, default=0.0)
    s.add_argument("--model", default="", help="覆盖 MINIMAX_MODEL（离线实验用，如 MiniMax-M3.0）")

    d = sub.add_parser("derive", parents=[common])
    d.add_argument("--use-time-gate", action="store_true", help="K5: 把时效纳入精选规则（默认关，离线先验质量）")

    sub.add_parser("analyze", parents=[common])
    sub.add_parser("export-review", parents=[common])

    rv = sub.add_parser("review-server", parents=[common])
    rv.add_argument("--host", default="127.0.0.1")
    rv.add_argument("--port", type=int, default=8766)
    rv.add_argument("--quiet", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    loaded = load_env_file(getattr(args, "env_file", ""))
    if loaded:
        print(f"[item-cal] env-file loaded keys={loaded}", flush=True)
    return {
        "score": run_score, "derive": run_derive, "analyze": run_analyze,
        "export-review": run_export_review, "review-server": run_review_server,
    }[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
