#!/usr/bin/env python3
"""Evaluate OpenRouter text-embedding-3-small for clustering thresholds.

The script samples existing Supabase cluster members, embeds the same structured
event text through the production embedding provider, writes normal usage logs, and emits
a reproducible Markdown/JSON report.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import remote_db  # noqa: E402
from clustering import embedding_provider as ep  # noqa: E402
from clustering.event_text import build_event_embedding_text  # noqa: E402
from env_utils import load_project_env  # noqa: E402


DEFAULT_OUT = ROOT / "docs" / "调研" / "embedding" / "openrouter-text-embedding-3-small"


def _apply_project_env() -> None:
    for key, value in load_project_env(ROOT).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("INFO2ACTION_EMBEDDING_USAGE_LOG", "1")
    # One-off reports should not take extra pool slots from the live service.
    os.environ.setdefault("INFO2ACTION_REMOTE_DB_POOL_DISABLED", "1")


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "n": int(arr.size),
        "min": round(float(arr.min()), 6),
        "p10": round(float(np.percentile(arr, 10)), 6),
        "p25": round(float(np.percentile(arr, 25)), 6),
        "mean": round(float(arr.mean()), 6),
        "median": round(float(np.median(arr)), 6),
        "p75": round(float(np.percentile(arr, 75)), 6),
        "p90": round(float(np.percentile(arr, 90)), 6),
        "max": round(float(arr.max()), 6),
    }


def _pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 2 or b.size < 2 or a.size != b.size:
        return None
    if float(np.std(a)) == 0.0 or float(np.std(b)) == 0.0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 6)


def _rankdata(arr: np.ndarray) -> np.ndarray:
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(len(arr), dtype=np.float64)
    sorted_values = arr[order]
    i = 0
    while i < len(arr):
        j = i + 1
        while j < len(arr) and sorted_values[j] == sorted_values[i]:
            j += 1
        rank = (i + j - 1) / 2.0 + 1.0
        ranks[order[i:j]] = rank
        i = j
    return ranks


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 2 or b.size < 2 or a.size != b.size:
        return None
    return _pearson(_rankdata(a), _rankdata(b))


def fetch_cluster_samples(*, clusters: int, per_cluster: int) -> list[dict[str, Any]]:
    schema = remote_db.remote_schema()
    with remote_db.connect() as conn:
        rows = conn.execute(
            f"""
            WITH eligible AS (
              SELECT
                ci.cluster_id,
                COUNT(*) AS n,
                MAX(COALESCE(i.published_at, i.fetched_at, i.created_at)) AS latest_at
              FROM {schema}.cluster_items ci
              JOIN {schema}.items i ON i.id = ci.item_id
              WHERE COALESCE(i.ai_summary, '') <> ''
              GROUP BY ci.cluster_id
              HAVING COUNT(*) >= %(per_cluster)s
              ORDER BY latest_at DESC NULLS LAST
              LIMIT %(clusters)s
            )
            SELECT
              ci.cluster_id,
              i.id,
              i.platform,
              i.source,
              i.title,
              i.content,
              i.ai_summary,
              i.ai_key_points,
              i.ai_keywords,
              i.ai_category,
              i.content_type,
              i.asr_text_cn,
              i.asr_text,
              i.url,
              i.published_at,
              i.fetched_at
            FROM eligible e
            JOIN {schema}.cluster_items ci ON ci.cluster_id = e.cluster_id
            JOIN {schema}.items i ON i.id = ci.item_id
            WHERE COALESCE(i.ai_summary, '') <> ''
            ORDER BY e.latest_at DESC NULLS LAST,
                     ci.cluster_id,
                     COALESCE(i.published_at, i.fetched_at, i.created_at) DESC NULLS LAST
            """,
            {"clusters": int(clusters), "per_cluster": int(per_cluster)},
        ).fetchall()

    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["cluster_id"], []).append(dict(row))

    samples: list[dict[str, Any]] = []
    for cluster_id, members in grouped.items():
        for member in members[:per_cluster]:
            text, meta = build_event_embedding_text(member)
            samples.append({
                "cluster_id": str(cluster_id),
                "item_id": str(member["id"]),
                "platform": member.get("platform"),
                "source": member.get("source"),
                "title": member.get("title") or "",
                "url": member.get("url") or "",
                "published_at": _ts(member.get("published_at") or member.get("fetched_at")),
                "embedding_text_chars": len(text),
                "embedding_text_meta": meta,
                "embedding_text": text,
            })
    return samples


def instantiate_provider(name: str):
    if name == "openrouter":
        api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        api_base = os.environ.get("OPENROUTER_EMBEDDING_BASE")
        return ep.get_provider("openrouter", api_key=api_key, api_base=api_base)
    raise ValueError(f"unsupported provider: {name}")


def embed_provider(
    *,
    provider_name: str,
    samples: list[dict[str, Any]],
    batch_size: int,
) -> dict[str, Any]:
    provider = instantiate_provider(provider_name)
    vectors: list[np.ndarray] = []
    texts = [s["embedding_text"] for s in samples]
    item_ids = [s["item_id"] for s in samples]
    usage_since = datetime.now(timezone.utc)
    started = time.monotonic()
    chunk_latencies: list[int] = []

    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        chunk_item_ids = item_ids[i : i + batch_size]
        chunk_started = time.monotonic()
        with ep.embedding_usage_context(
            source="embedding_shootout",
            stage=f"{provider_name}_comparison",
            item_ids=chunk_item_ids,
        ):
            arr = provider.embed(chunk, mode="db")
        chunk_latencies.append(int((time.monotonic() - chunk_started) * 1000))
        vectors.append(arr)

    matrix = np.vstack(vectors).astype(np.float32)
    total_latency = int((time.monotonic() - started) * 1000)
    tokens = ep.estimate_embedding_tokens(texts)
    price = ep._embedding_price_yuan_per_1k_tokens(provider.name, getattr(provider, "model", None))
    return {
        "provider_key": provider_name,
        "provider": provider.name,
        "model": getattr(provider, "model", None),
        "usage_log_since": usage_since.isoformat(),
        "output_dim": int(matrix.shape[1]),
        "input_count": len(texts),
        "input_chars": int(sum(len(t) for t in texts)),
        "estimated_tokens": int(tokens),
        "price_yuan_per_1k_tokens": float(price),
        "estimated_cost_yuan": round(tokens / 1000 * price, 8),
        "latency_ms": total_latency,
        "chunk_latency_ms": chunk_latencies,
        "vectors": matrix,
    }


def fetch_logged_usage(stage: str, since_iso: str) -> dict[str, Any] | None:
    """Read the usage rows just written by this report run."""
    schema = remote_db.remote_schema()
    try:
        since = datetime.fromisoformat(since_iso)
    except (TypeError, ValueError):
        return None
    try:
        with remote_db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                  COUNT(*) AS calls,
                  SUM(input_count) AS input_count,
                  SUM(input_chars) AS input_chars,
                  SUM(estimated_tokens) AS tokens,
                  SUM(estimated_cost_yuan) AS cost_yuan,
                  MIN(created_at) AS first_at,
                  MAX(created_at) AS last_at
                FROM {schema}.embedding_usage_logs
                WHERE source = 'embedding_shootout'
                  AND stage = %(stage)s
                  AND created_at >= %(since)s
                  AND status = 'success'
                """,
                {"stage": stage, "since": since},
            ).fetchone()
    except Exception:
        return None
    if not row or not row.get("calls"):
        return None
    return {
        "calls": int(row.get("calls") or 0),
        "input_count": int(row.get("input_count") or 0),
        "input_chars": int(row.get("input_chars") or 0),
        "tokens": int(row.get("tokens") or 0),
        "cost_yuan": round(float(row.get("cost_yuan") or 0.0), 8),
        "first_at": _ts(row.get("first_at")),
        "last_at": _ts(row.get("last_at")),
    }


def evaluate_vectors(samples: list[dict[str, Any]], matrix: np.ndarray) -> dict[str, Any]:
    labels = [s["cluster_id"] for s in samples]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = matrix / norms
    cosine = normalized @ normalized.T

    same: list[float] = []
    different: list[float] = []
    pair_values: list[float] = []
    pair_labels: list[bool] = []
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            value = float(cosine[i, j])
            pair_values.append(value)
            is_same = labels[i] == labels[j]
            pair_labels.append(is_same)
            if is_same:
                same.append(value)
            else:
                different.append(value)

    eligible_top = 0
    top1_hits = 0
    top3_hits = 0
    nearest_examples: list[dict[str, Any]] = []
    for i, sample in enumerate(samples):
        if labels.count(labels[i]) < 2:
            continue
        eligible_top += 1
        order = np.argsort(-cosine[i])
        order = [int(idx) for idx in order if int(idx) != i]
        top1 = order[0] if order else None
        top3 = order[:3]
        if top1 is not None and labels[top1] == labels[i]:
            top1_hits += 1
        if any(labels[idx] == labels[i] for idx in top3):
            top3_hits += 1
        if top1 is not None and len(nearest_examples) < 8:
            nearest_examples.append({
                "query_item_id": sample["item_id"],
                "query_cluster_id": labels[i],
                "query_title": sample["title"][:120],
                "top1_item_id": samples[top1]["item_id"],
                "top1_cluster_id": labels[top1],
                "top1_title": samples[top1]["title"][:120],
                "top1_cosine": round(float(cosine[i, top1]), 6),
                "top1_same_cluster": labels[top1] == labels[i],
            })

    same_stats = _describe(same)
    diff_stats = _describe(different)
    separation = None
    if same and different:
        separation = round(float(np.mean(same) - np.mean(different)), 6)
    return {
        "same_cluster_cosine": same_stats,
        "different_cluster_cosine": diff_stats,
        "separation_mean": separation,
        "top1_same_cluster_accuracy": (
            round(top1_hits / eligible_top, 6) if eligible_top else None
        ),
        "top3_same_cluster_hit_rate": (
            round(top3_hits / eligible_top, 6) if eligible_top else None
        ),
        "topk_eligible_count": eligible_top,
        "pair_values": pair_values,
        "pair_labels": pair_labels,
        "nearest_examples": nearest_examples,
    }


def _cosine_matrix(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = matrix / norms
    return normalized @ normalized.T


def _component_labels(cosine: np.ndarray, threshold: float) -> list[int]:
    n = int(cosine.shape[0])
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if float(cosine[i, j]) >= threshold:
                union(i, j)
    roots: dict[int, int] = {}
    labels: list[int] = []
    for i in range(n):
        root = find(i)
        if root not in roots:
            roots[root] = len(roots)
        labels.append(roots[root])
    return labels


def _pairwise_cluster_metrics(true_labels: list[str], pred_labels: list[int]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for i in range(len(true_labels)):
        for j in range(i + 1, len(true_labels)):
            same_true = true_labels[i] == true_labels[j]
            same_pred = pred_labels[i] == pred_labels[j]
            if same_true and same_pred:
                tp += 1
            elif (not same_true) and same_pred:
                fp += 1
            elif same_true and not same_pred:
                fn += 1
            else:
                tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0

    pred_groups: dict[int, list[int]] = {}
    true_to_pred: dict[str, set[int]] = {}
    for idx, pred in enumerate(pred_labels):
        pred_groups.setdefault(pred, []).append(idx)
        true_to_pred.setdefault(true_labels[idx], set()).add(pred)

    weighted_purity = 0
    overmerged = 0
    singleton = 0
    for indices in pred_groups.values():
        if len(indices) == 1:
            singleton += 1
        counts: dict[str, int] = {}
        for idx in indices:
            counts[true_labels[idx]] = counts.get(true_labels[idx], 0) + 1
        weighted_purity += max(counts.values())
        if len(counts) > 1:
            overmerged += 1

    split_true = sum(1 for preds in true_to_pred.values() if len(preds) > 1)
    sizes = [len(v) for v in pred_groups.values()]
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "predicted_cluster_count": len(pred_groups),
        "singleton_predicted_clusters": singleton,
        "overmerged_predicted_clusters": overmerged,
        "split_true_clusters": split_true,
        "weighted_purity": round(weighted_purity / len(true_labels), 6) if true_labels else None,
        "predicted_cluster_size": _describe([float(s) for s in sizes]),
    }


def evaluate_clustering(
    samples: list[dict[str, Any]],
    matrix: np.ndarray,
    *,
    thresholds: list[float],
) -> dict[str, Any]:
    true_labels = [s["cluster_id"] for s in samples]
    cosine = _cosine_matrix(matrix)
    sweep = []
    for threshold in thresholds:
        pred_labels = _component_labels(cosine, threshold)
        row = _pairwise_cluster_metrics(true_labels, pred_labels)
        row["threshold"] = round(float(threshold), 4)
        sweep.append(row)
    best = max(sweep, key=lambda r: (r["f1"], r["precision"], r["recall"]))
    production_threshold = min(sweep, key=lambda r: abs(r["threshold"] - 0.75))
    best_labels = _component_labels(cosine, float(best["threshold"]))
    production_labels = _component_labels(cosine, float(production_threshold["threshold"]))
    return {
        "threshold_sweep": sweep,
        "best": best,
        "production_075": production_threshold,
        "best_examples": clustering_examples(samples, best_labels, limit=6),
        "production_075_examples": clustering_examples(samples, production_labels, limit=6),
    }


def clustering_examples(
    samples: list[dict[str, Any]],
    pred_labels: list[int],
    *,
    limit: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    true_labels = [s["cluster_id"] for s in samples]
    pred_groups: dict[int, list[int]] = {}
    for idx, pred in enumerate(pred_labels):
        pred_groups.setdefault(pred, []).append(idx)

    good: list[dict[str, Any]] = []
    overmerged: list[dict[str, Any]] = []
    for pred, indices in pred_groups.items():
        if len(indices) < 2:
            continue
        counts: dict[str, int] = {}
        for idx in indices:
            counts[true_labels[idx]] = counts.get(true_labels[idx], 0) + 1
        entry = {
            "predicted_cluster": int(pred),
            "size": len(indices),
            "true_cluster_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "items": [
                {
                    "item_id": samples[idx]["item_id"],
                    "true_cluster_id": true_labels[idx],
                    "platform": samples[idx].get("platform"),
                    "title": (samples[idx].get("title") or "")[:140],
                }
                for idx in indices[:6]
            ],
        }
        if len(counts) == 1:
            good.append(entry)
        else:
            overmerged.append(entry)

    true_groups: dict[str, list[int]] = {}
    for idx, label in enumerate(true_labels):
        true_groups.setdefault(label, []).append(idx)
    split: list[dict[str, Any]] = []
    for true_label, indices in true_groups.items():
        pred_set = sorted({pred_labels[idx] for idx in indices})
        if len(pred_set) <= 1:
            continue
        split.append({
            "true_cluster_id": true_label,
            "size": len(indices),
            "predicted_clusters": [int(x) for x in pred_set],
            "items": [
                {
                    "item_id": samples[idx]["item_id"],
                    "predicted_cluster": int(pred_labels[idx]),
                    "platform": samples[idx].get("platform"),
                    "title": (samples[idx].get("title") or "")[:140],
                }
                for idx in indices[:6]
            ],
        })

    good.sort(key=lambda x: (-x["size"], str(x["predicted_cluster"])))
    overmerged.sort(key=lambda x: (-x["size"], str(x["predicted_cluster"])))
    split.sort(key=lambda x: (-x["size"], x["true_cluster_id"]))
    return {
        "good": good[:limit],
        "overmerged": overmerged[:limit],
        "split": split[:limit],
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(
    *,
    samples: list[dict[str, Any]],
    provider_results: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    clustering: dict[str, dict[str, Any]],
    agreement: dict[str, Any],
    run_config: dict[str, Any],
    out_dir: Path,
) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    sample_clusters = sorted({s["cluster_id"] for s in samples})
    lines: list[str] = [
        "# Embedding 聚合阈值评估：OpenRouter text-embedding-3-small",
        "",
        f"- 生成时间：`{generated}`",
        f"- 样本：`{len(samples)}` 条 item，来自 `{len(sample_clusters)}` 个既有 cluster",
        "- 输入构造：复用线上 `build_event_embedding_text()`，即 title / AI摘要 / 结构化要点 / 关键词 / 分类 / 正文转写 fallback",
        "- 审计口径：本次调用会写入 `embedding_usage_logs`，`source=embedding_shootout`，`stage=<provider>_comparison`",
        "",
        "## 1. 监控与日志口径",
        "",
        "本次代码把 embedding 调用统一落到同一张 usage 表。每一次 provider 调用记录：`provider`、`model`、`mode`、`source/stage/run_id`、`caller_file/caller_func`、`input_count`、`input_chars`、`input_bytes`、`estimated_tokens`、`token_estimator`、`output_count`、`output_dim`、`status/error`、`latency_ms`、`price_yuan_per_1k_tokens`、`estimated_cost_yuan`、`item_ids_json`。",
        "",
        "在 Supabase 远程主库模式下优先写 `remote_poc.embedding_usage_logs`；如果 Postgres 连接池满，会尝试 Supabase REST 写入；如果远程仍失败，再降级写本地 SQLite，避免因为日志系统影响抓取主流程。",
        "",
        "## 2. Provider 结果",
        "",
        "| Provider | Model | 维度 | 样本数 | 输入字符 | 本地估算 tokens | 日志 tokens | 日志/估算成本(元) | 总延迟(ms) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in provider_results:
        r = provider_results[key]
        logged = r.get("logged_usage") or {}
        logged_tokens = logged.get("tokens")
        cost = logged.get("cost_yuan") if logged.get("cost_yuan") is not None else r["estimated_cost_yuan"]
        lines.append(
            "| {provider} | `{model}` | {dim} | {count} | {chars} | {tokens} | {logged_tokens} | {cost:.8f} | {latency} |".format(
                provider=r["provider"],
                model=r.get("model") or "",
                dim=r["output_dim"],
                count=r["input_count"],
                chars=r["input_chars"],
                tokens=r["estimated_tokens"],
                logged_tokens=logged_tokens if logged_tokens is not None else "N/A",
                cost=cost,
                latency=r["latency_ms"],
            )
        )

    lines.extend([
        "",
        "补充：如果 provider 返回了真实 usage，`results.json` 会在 `logged_usage` 中记录远程表聚合值；OpenRouter 的消费分析应优先看该值。",
        "",
        "## 3. 聚类代理指标",
        "",
        "这里用既有 cluster membership 当弱标签：同 cluster 的 pair 应更相似，不同 cluster 的 pair 应更远。它不能替代人工验收，但足够发现明显退化。",
        "",
        "| Provider | 同 cluster mean | 异 cluster mean | mean gap | Top1 同 cluster | Top3 命中 | pair 数(同/异) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for key in provider_results:
        m = metrics[key]
        same = m["same_cluster_cosine"]
        diff = m["different_cluster_cosine"]
        lines.append(
            "| {provider} | {same_mean} | {diff_mean} | {gap} | {top1} | {top3} | {same_n}/{diff_n} |".format(
                provider=provider_results[key]["provider"],
                same_mean=_fmt(same.get("mean")),
                diff_mean=_fmt(diff.get("mean")),
                gap=_fmt(m.get("separation_mean")),
                top1=_fmt(m.get("top1_same_cluster_accuracy")),
                top3=_fmt(m.get("top3_same_cluster_hit_rate")),
                same_n=same.get("n", 0),
                diff_n=diff.get("n", 0),
            )
        )

    lines.extend([
        "",
        "## 4. 向量分布一致性",
        "",
        "当前生产评估只跑 OpenRouter，不再调用 MiniMax embedding；如需跨模型一致性，请先接入新的非 MiniMax 候选 provider。",
        "",
        "## 5. 离线聚合效果",
        "",
        "这里把同一 provider 产出的向量按阈值连边，然后用 connected components 得到离线预测 cluster。对照标签是线上已有 `cluster_items` membership，因此是弱标签，但能直观看出拆散和误合。",
        "",
        "| Provider | 阈值口径 | threshold | 预测 cluster | 单点 cluster | overmerge | split true | Precision | Recall | F1 | Purity |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key in provider_results:
        provider = provider_results[key]["provider"]
        for label, row in (
            ("best", clustering[key]["best"]),
            ("0.75", clustering[key]["production_075"]),
        ):
            lines.append(
                "| {provider} | {label} | {threshold:.2f} | {clusters} | {singletons} | {over} | {split} | {precision} | {recall} | {f1} | {purity} |".format(
                    provider=provider,
                    label=label,
                    threshold=row["threshold"],
                    clusters=row["predicted_cluster_count"],
                    singletons=row["singleton_predicted_clusters"],
                    over=row["overmerged_predicted_clusters"],
                    split=row["split_true_clusters"],
                    precision=_fmt(row["precision"]),
                    recall=_fmt(row["recall"]),
                    f1=_fmt(row["f1"]),
                    purity=_fmt(row["weighted_purity"]),
                )
            )

    lines.extend([
        "",
        "### 阈值扫描",
        "",
        "| Provider | threshold | Precision | Recall | F1 | pred clusters | overmerge | split true |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    focus_thresholds = {0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9}
    for key in provider_results:
        provider = provider_results[key]["provider"]
        for row in clustering[key]["threshold_sweep"]:
            if round(float(row["threshold"]), 2) not in focus_thresholds:
                continue
            lines.append(
                "| {provider} | {threshold:.2f} | {precision} | {recall} | {f1} | {clusters} | {over} | {split} |".format(
                    provider=provider,
                    threshold=row["threshold"],
                    precision=_fmt(row["precision"]),
                    recall=_fmt(row["recall"]),
                    f1=_fmt(row["f1"]),
                    clusters=row["predicted_cluster_count"],
                    over=row["overmerged_predicted_clusters"],
                    split=row["split_true_clusters"],
                )
            )

    lines.extend([
        "",
        "### 典型聚合案例（best threshold）",
        "",
    ])
    for key in provider_results:
        lines.append(f"#### {provider_results[key]['provider']}")
        examples = clustering[key]["best_examples"]
        if examples["good"]:
            lines.append("")
            lines.append("聚合正确示例：")
            for ex in examples["good"][:3]:
                title_bits = " / ".join((item["title"] or "")[:36].replace("|", "\\|") for item in ex["items"][:3])
                lines.append(
                    f"- pred `{ex['predicted_cluster']}` size={ex['size']} true={ex['true_cluster_counts']}：{title_bits}"
                )
        if examples["overmerged"]:
            lines.append("")
            lines.append("误合示例：")
            for ex in examples["overmerged"][:3]:
                title_bits = " / ".join((item["title"] or "")[:36].replace("|", "\\|") for item in ex["items"][:3])
                lines.append(
                    f"- pred `{ex['predicted_cluster']}` size={ex['size']} true={ex['true_cluster_counts']}：{title_bits}"
                )
        if examples["split"]:
            lines.append("")
            lines.append("拆散示例：")
            for ex in examples["split"][:3]:
                title_bits = " / ".join((item["title"] or "")[:36].replace("|", "\\|") for item in ex["items"][:3])
                lines.append(
                    f"- true `{ex['true_cluster_id']}` split={ex['predicted_clusters']}：{title_bits}"
                )
        lines.append("")

    lines.extend([
        "## 6. 样本示例",
        "",
        "| cluster | item | platform | title | chars |",
        "|---|---|---|---|---:|",
    ])
    for sample in samples[:12]:
        title = (sample["title"] or "").replace("|", "\\|").replace("\n", " ")[:90]
        lines.append(
            f"| `{sample['cluster_id']}` | `{sample['item_id']}` | {sample.get('platform') or ''} | {title} | {sample['embedding_text_chars']} |"
        )

    lines.extend([
        "",
        "## 7. 初步结论",
        "",
        "- 成本：OpenRouter `openai/text-embedding-3-small` 的实际扣费以平台账单和 `embedding_usage_logs` 为准。",
        "- 聚合建议：继续把 embedding provider 与 `stage1_cosine_min` 作为一组配置调参，当前生产保守阈值为 `0.75`。",
        "- 安全边界：本脚本不再实例化 MiniMax embedding，避免误触发 `embo-01` 消耗。",
        "",
        "## 8. 外部资料",
        "",
        "- OpenRouter Embeddings API: https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings",
        "- OpenRouter embedding model list / pricing fields: https://openrouter.ai/docs/api/api-reference/embeddings/list-embeddings-models",
        "",
        "",
        "## 9. 复跑方式",
        "",
        "```bash",
        "OPENROUTER_API_KEY=*** INFO2ACTION_REMOTE_DB_POOL_DISABLED=1 \\",
        "  python3 scripts/compare_embedding_providers.py "
        f"--clusters {run_config.get('clusters')} --per-cluster {run_config.get('per_cluster')} "
        f"--batch-size {run_config.get('batch_size')} --thresholds \"{run_config.get('thresholds')}\"",
        "```",
        "",
        "不要把 key 写进命令历史以外的输出；报告和 JSON 都不会保存 key。",
        "",
    ])

    report = "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    return report


def write_results(
    *,
    samples: list[dict[str, Any]],
    provider_results: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    clustering: dict[str, dict[str, Any]],
    agreement: dict[str, Any],
    run_config: dict[str, Any],
    out_dir: Path,
) -> None:
    clean_provider_results = {}
    for key, result in provider_results.items():
        clean_provider_results[key] = {
            k: _jsonable(v)
            for k, v in result.items()
            if k != "vectors"
        }
    clean_metrics = {}
    for key, metric in metrics.items():
        clean_metrics[key] = {
            k: _jsonable(v)
            for k, v in metric.items()
            if k not in {"pair_values", "pair_labels"}
        }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples": [
            {k: v for k, v in sample.items() if k != "embedding_text"}
            for sample in samples
        ],
        "run_config": _jsonable(run_config),
        "provider_results": clean_provider_results,
        "metrics": clean_metrics,
        "clustering": _jsonable(clustering),
        "agreement": agreement,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clusters", type=int, default=8)
    parser.add_argument("--per-cluster", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--thresholds",
        default="0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.90,0.95",
        help="comma-separated cosine thresholds for offline clustering evaluation",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args()


def _parse_thresholds(raw: str) -> list[float]:
    out: list[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]


def main() -> int:
    args = parse_args()
    _apply_project_env()

    samples = fetch_cluster_samples(clusters=args.clusters, per_cluster=args.per_cluster)
    if len(samples) < 4:
        raise SystemExit(f"not enough samples: {len(samples)}")

    provider_results = {
        "openrouter": embed_provider(
            provider_name="openrouter",
            samples=samples,
            batch_size=max(1, args.batch_size),
        ),
    }
    for key, result in provider_results.items():
        logged = fetch_logged_usage(
            stage=f"{key}_comparison",
            since_iso=result.get("usage_log_since") or "",
        )
        if logged:
            result["logged_usage"] = logged
    metrics = {
        key: evaluate_vectors(samples, result["vectors"])
        for key, result in provider_results.items()
    }
    thresholds = _parse_thresholds(args.thresholds)
    clustering = {
        key: evaluate_clustering(samples, result["vectors"], thresholds=thresholds)
        for key, result in provider_results.items()
    }
    agreement = {}

    render_report(
        samples=samples,
        provider_results=provider_results,
        metrics=metrics,
        clustering=clustering,
        agreement=agreement,
        run_config={
            "clusters": args.clusters,
            "per_cluster": args.per_cluster,
            "batch_size": args.batch_size,
            "thresholds": args.thresholds,
        },
        out_dir=args.out,
    )
    write_results(
        samples=samples,
        provider_results=provider_results,
        metrics=metrics,
        clustering=clustering,
        agreement=agreement,
        run_config={
            "clusters": args.clusters,
            "per_cluster": args.per_cluster,
            "batch_size": args.batch_size,
            "thresholds": args.thresholds,
        },
        out_dir=args.out,
    )
    print(f"Wrote {args.out / 'report.md'}")
    print(f"Wrote {args.out / 'results.json'}")
    for key, result in provider_results.items():
        print(
            f"{key}: dim={result['output_dim']} tokens={result['estimated_tokens']} "
            f"latency_ms={result['latency_ms']} est_cost_yuan={result['estimated_cost_yuan']:.8f}"
        )
    for key, metric in metrics.items():
        print(
            f"{key}: top1={metric['top1_same_cluster_accuracy']} "
            f"top3={metric['top3_same_cluster_hit_rate']} gap={metric['separation_mean']}"
        )
    for key, cluster_metric in clustering.items():
        best = cluster_metric["best"]
        prod = cluster_metric["production_075"]
        print(
            f"{key}: cluster_best_t={best['threshold']} f1={best['f1']} "
            f"precision={best['precision']} recall={best['recall']} "
            f"prod075_f1={prod['f1']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
