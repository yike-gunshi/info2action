#!/usr/bin/env python3
"""Embedding evaluation harness for v2 design Module #1.

Reads docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §4 for spec.

Pipeline (single script, idempotent per step):
  Step 1  sample()        : pick latest items per platform → sample.jsonl
  Step 2  build_inputs()  : 3 input variants × N items   → inputs/{variant}.jsonl
  Step 3  embed()         : 3 models × 3 inputs           → embeddings/{model}__{variant}.npz
                          + v1 baseline pulled from items.embedding (MiniMax structured-first)
  Step 4  pick_queries()  : 20 stratified random queries → queries.json
  Step 5  topk_report()   : 20 queries × 10 sets × top-10 → report-{ts}.md
  Step 6  cluster_stats() : cosine ≥ 0.75 components      → cluster_stats.md

Usage:
  uv run python scripts/embedding_eval.py sample
  uv run python scripts/embedding_eval.py inputs
  uv run python scripts/embedding_eval.py embed --model minimax --variant raw
  uv run python scripts/embedding_eval.py embed --model e5 --variant aikw
  uv run python scripts/embedding_eval.py embed --model bge-m3 --variant aisum
  uv run python scripts/embedding_eval.py embed --model v1-baseline   # pulls existing items.embedding
  uv run python scripts/embedding_eval.py queries
  uv run python scripts/embedding_eval.py report
  uv run python scripts/embedding_eval.py cluster
  uv run python scripts/embedding_eval.py all          # runs everything in order
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

OUT_DIR = Path(os.environ.get("EMB_EVAL_OUT_DIR", "/tmp/info2action-embedding-eval"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_PATH = OUT_DIR / "sample.jsonl"
INPUTS_DIR = OUT_DIR / "inputs"
EMB_DIR = OUT_DIR / "embeddings"
QUERIES_PATH = OUT_DIR / "queries.json"


# ---------- platform sampling caps ----------
# Phase 1: small sample (~412) for fast iteration.
# Phase 2: large sample (~2000) for production-grade validation.
_PHASE = os.environ.get("EMB_EVAL_PHASE", "phase1")
if _PHASE == "phase2":
    PLATFORM_CAPS = {
        "twitter": 800,
        "reddit": 250,
        "hackernews": 250,
        "github": 250,    # only ~44 available, will take all
        "waytoagi": 250,  # only ~18 available, will take all
        "bilibili": 250,
        "lingowhale": 250,
        "rss": 250,       # only ~154 available, will take all
    }
    # Phase 2 picks more queries (50, distributed by platform).
    _PHASE2_QUERY_COUNTS = True
else:
    PLATFORM_CAPS = {
        "twitter": 100,
        "reddit": 50,
        "hackernews": 50,
        "github": 50,
        "waytoagi": 50,
        "bilibili": 50,
        "lingowhale": 50,
        "rss": 50,
    }
    _PHASE2_QUERY_COUNTS = False

# ---------- model registry ----------
MODELS = {
    "minimax": "MiniMax-Text-Embedding-API",  # via existing helper
    "e5": "intfloat/multilingual-e5-large-instruct",
    "bge-m3": "BAAI/bge-m3",
    "v1-baseline": "v1 stored MiniMax embedding (structured-first input)",
}

VARIANTS = ["raw", "aikw", "aisum"]
# raw   = title + content
# aikw  = title + ai_summary + ai_keywords
# aisum = title + ai_summary


# =============================================================================
# Step 1: sample
# =============================================================================
def step_sample() -> None:
    conn = sqlite3.connect(BASE / "data" / "feed.db")
    conn.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    summary: dict[str, int] = {}
    for platform, cap in PLATFORM_CAPS.items():
        cur = conn.execute(
            """SELECT id, platform, source, author_name, title, content,
                       url, published_at, fetched_at,
                       ai_summary, ai_key_points, ai_keywords, ai_category,
                       content_type
                FROM items
               WHERE platform = ?
                 AND embedding IS NOT NULL
               ORDER BY COALESCE(published_at, fetched_at) DESC
               LIMIT ?""",
            (platform, cap),
        )
        platform_rows = [dict(r) for r in cur.fetchall()]
        summary[platform] = len(platform_rows)
        rows.extend(platform_rows)
    conn.close()

    with SAMPLE_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[sample] wrote {len(rows)} items → {SAMPLE_PATH}")
    for k, v in summary.items():
        cap = PLATFORM_CAPS[k]
        flag = " ⚠ under cap" if v < cap else ""
        print(f"  {k:12s} {v:4d} / {cap}{flag}")


# =============================================================================
# Step 2: build inputs (3 variants)
# =============================================================================
def _load_sample() -> list[dict[str, Any]]:
    if not SAMPLE_PATH.exists():
        raise SystemExit("sample.jsonl missing — run `sample` first")
    return [json.loads(line) for line in SAMPLE_PATH.read_text().split("\n") if line.strip()]


def _clean(s: Any) -> str:
    return (s or "").strip() if isinstance(s, str) else ""


def _build_variant(item: dict, variant: str) -> str:
    title = _clean(item.get("title"))
    content = _clean(item.get("content"))
    ai_summary = _clean(item.get("ai_summary"))
    ai_keywords = _clean(item.get("ai_keywords"))
    if variant == "raw":
        parts = [title, content]
    elif variant == "aikw":
        parts = [title, ai_summary, f"关键词: {ai_keywords}" if ai_keywords else ""]
    elif variant == "aisum":
        parts = [title, ai_summary]
    else:
        raise ValueError(f"unknown variant {variant}")
    return "\n\n".join(p for p in parts if p)


def step_inputs() -> None:
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    items = _load_sample()
    for variant in VARIANTS:
        out_path = INPUTS_DIR / f"{variant}.jsonl"
        n_empty = 0
        with out_path.open("w") as f:
            for item in items:
                text = _build_variant(item, variant)
                if not text:
                    n_empty += 1
                    text = f"(empty item {item['id']})"
                f.write(json.dumps({
                    "id": item["id"],
                    "platform": item["platform"],
                    "input_text": text,
                    "input_variant": variant,
                    "input_chars": len(text),
                }, ensure_ascii=False) + "\n")
        print(f"[inputs] {variant:6s} → {out_path}  empties={n_empty}/{len(items)}")


# =============================================================================
# Step 3: embed
# =============================================================================
def _load_inputs(variant: str) -> list[dict]:
    path = INPUTS_DIR / f"{variant}.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} missing — run `inputs` first")
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]


def _emb_path(model: str, variant: str) -> Path:
    EMB_DIR.mkdir(parents=True, exist_ok=True)
    return EMB_DIR / f"{model}__{variant}.npz"


def _save_embeddings(model: str, variant: str, ids: list[str], matrix: np.ndarray) -> None:
    path = _emb_path(model, variant)
    np.savez(path, ids=np.array(ids, dtype=object), matrix=matrix.astype(np.float32))
    print(f"[embed] {model}/{variant} → {path}  shape={matrix.shape}")


def _load_embeddings(model: str, variant: str) -> tuple[list[str], np.ndarray] | None:
    path = _emb_path(model, variant)
    if not path.exists():
        return None
    data = np.load(path, allow_pickle=True)
    return list(data["ids"]), data["matrix"]


def _embed_minimax(rows: list[dict]) -> np.ndarray:
    from clustering import embedding_provider as ep  # noqa: E402

    cfg_path = BASE / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    name, api_key, api_base = ep.resolve_runtime_provider(cfg)
    if name.lower() != "minimax":
        print(f"[embed] WARNING: runtime provider is '{name}', forcing 'minimax'")
        name = "minimax"
        api_key = os.environ.get("MINIMAX_API_KEY") or api_key
    if not api_key:
        raise SystemExit("MINIMAX_API_KEY missing — set in .env")
    provider = ep.get_provider(name, api_key=api_key, api_base=api_base)
    texts = [r["input_text"] for r in rows]
    print(f"[embed] minimax: encoding {len(texts)} texts via {provider.name}…", flush=True)
    arr = provider.embed(texts, mode="db")
    return np.asarray(arr, dtype=np.float32)


def _embed_local(model_id: str, rows: list[dict], *, prefix: str = "",
                 max_seq_length: int = 1024) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "sentence-transformers not installed. Run:\n"
            "  uv pip install sentence-transformers\n"
            f"(import error: {e})"
        ) from e
    print(f"[embed] loading {model_id} (this may download model on first run)…", flush=True)
    started = time.time()
    model = SentenceTransformer(model_id)
    # Cap context length to prevent OOM on long docs (e.g. waytoagi 10k+ char content).
    # 1024 tokens ≈ 700-800 中文字 / 1500 英文 chars，足够覆盖 title + ai_summary + 摘要级正文。
    if hasattr(model, "max_seq_length"):
        old = model.max_seq_length
        model.max_seq_length = max_seq_length
        print(f"[embed] {model_id} max_seq_length: {old} → {max_seq_length}", flush=True)
    print(f"[embed] {model_id} loaded in {time.time() - started:.1f}s", flush=True)
    texts = [(prefix + r["input_text"]) if prefix else r["input_text"] for r in rows]
    arr = model.encode(
        texts,
        batch_size=8,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return np.asarray(arr, dtype=np.float32)


def _embed_v1_baseline(rows: list[dict]) -> np.ndarray:
    """Pull existing items.embedding BLOB (MiniMax structured-first)."""
    from clustering import vector_utils as vu  # noqa: E402

    conn = sqlite3.connect(BASE / "data" / "feed.db")
    conn.row_factory = sqlite3.Row
    out: list[np.ndarray] = []
    missing = 0
    for r in rows:
        cur = conn.execute(
            "SELECT embedding FROM items WHERE id = ?", (r["id"],)
        ).fetchone()
        if cur is None or cur["embedding"] is None:
            missing += 1
            out.append(np.zeros(1024, dtype=np.float32))
            continue
        vec = vu.unpack_blob(cur["embedding"])
        out.append(np.asarray(vec, dtype=np.float32))
    conn.close()
    if missing:
        print(f"[embed] v1-baseline: {missing} items missing existing embedding (zeros)")
    return np.stack(out).astype(np.float32)


def step_embed(model: str, variant: str | None) -> None:
    if model == "v1-baseline":
        rows = _load_inputs("raw")  # variant is irrelevant; uses stored vector
        arr = _embed_v1_baseline(rows)
        _save_embeddings("v1-baseline", "stored", [r["id"] for r in rows], arr)
        return
    if variant is None:
        raise SystemExit("--variant required for non-baseline models")
    rows = _load_inputs(variant)
    if model == "minimax":
        arr = _embed_minimax(rows)
    elif model == "e5":
        # multilingual-e5 needs `query: ` / `passage: ` prefix.
        # For symmetric similarity (doc-doc), use `query: ` for both sides.
        arr = _embed_local("intfloat/multilingual-e5-large-instruct", rows, prefix="query: ")
    elif model == "bge-m3":
        arr = _embed_local("BAAI/bge-m3", rows, prefix="")
    else:
        raise SystemExit(f"unknown model {model}")
    _save_embeddings(model, variant, [r["id"] for r in rows], arr)


# =============================================================================
# Step 4: pick queries
# =============================================================================
if _PHASE == "phase2":
    QUERY_PLATFORMS = {
        "twitter": 12,
        "reddit": 7,
        "hackernews": 7,
        "github": 5,
        "waytoagi": 5,
        "bilibili": 5,
        "lingowhale": 7,
        "rss": 2,
    }
else:
    QUERY_PLATFORMS = {
        "twitter": 5,
        "reddit": 3,
        "hackernews": 3,
        "github": 2,
        "waytoagi": 2,
        "bilibili": 2,
        "lingowhale": 2,
        "rss": 1,
    }


def step_queries(seed: int = 42) -> None:
    items = _load_sample()
    by_platform: dict[str, list[dict]] = {}
    for it in items:
        by_platform.setdefault(it["platform"], []).append(it)
    random.seed(seed)
    chosen: list[dict] = []
    for p, n in QUERY_PLATFORMS.items():
        pool = by_platform.get(p, [])
        if not pool:
            print(f"[queries] {p:12s} pool empty — skipped")
            continue
        take = min(n, len(pool))
        chosen.extend(random.sample(pool, take))
    QUERIES_PATH.write_text(
        json.dumps([q["id"] for q in chosen], ensure_ascii=False, indent=2)
    )
    print(f"[queries] picked {len(chosen)} → {QUERIES_PATH}")


# =============================================================================
# Step 5: top-k report
# =============================================================================
def _normalize(matrix: np.ndarray) -> np.ndarray:
    norm = np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return matrix / norm


def _all_embedding_sets() -> list[tuple[str, str]]:
    """Return list of (model, variant) that exist on disk."""
    sets: list[tuple[str, str]] = []
    for model in ["minimax", "e5", "bge-m3"]:
        for variant in VARIANTS:
            if _emb_path(model, variant).exists():
                sets.append((model, variant))
    if _emb_path("v1-baseline", "stored").exists():
        sets.append(("v1-baseline", "stored"))
    return sets


def _truncate(text: str, n: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def step_report(top_k: int = 10) -> None:
    if not QUERIES_PATH.exists():
        raise SystemExit("queries.json missing — run `queries` first")
    sets = _all_embedding_sets()
    if not sets:
        raise SystemExit("no embedding sets found — run `embed` first")
    items = _load_sample()
    by_id = {it["id"]: it for it in items}
    query_ids: list[str] = json.loads(QUERIES_PATH.read_text())

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = OUT_DIR / f"report-{ts}.md"
    f = report_path.open("w")
    f.write(f"# Embedding 评估报告 — {ts}\n\n")
    f.write(
        "**用途**：对比 3 个模型 × 3 种输入构造（+ v1 baseline）的 top-10 召回质量。\n\n"
        "**人眼审方法**：每条 query 下面有多组召回，对每条召回结果勾"
        "「同事件 / 不同事件 / 不确定」。验收门槛：top-10 中 ≥ 6 条同事件 → 通过。\n\n"
    )
    f.write(f"## 评估配置\n\n")
    f.write(f"- 样本池：{len(items)} 条（按平台分层）\n")
    f.write(f"- query 数：{len(query_ids)}\n")
    f.write(f"- top_k：{top_k}\n")
    f.write(f"- embedding 集合：{len(sets)}\n\n")
    for m, v in sets:
        f.write(f"  - `{m}` × `{v}`\n")
    f.write("\n---\n\n")

    # Pre-load all embedding sets in normalized form, indexed by id.
    cache: dict[tuple[str, str], tuple[list[str], np.ndarray, dict[str, int]]] = {}
    for m, v in sets:
        ids, mat = _load_embeddings(m, v)
        mat = _normalize(mat)
        idx = {i: k for k, i in enumerate(ids)}
        cache[(m, v)] = (ids, mat, idx)

    for qi, qid in enumerate(query_ids, 1):
        q_item = by_id.get(qid)
        if q_item is None:
            f.write(f"## Query #{qi}: {qid} (not in sample)\n\n")
            continue
        f.write(f"## Query #{qi}：[{q_item['platform']}] {q_item.get('title') or '(无题)'}\n\n")
        f.write(f"- item_id: `{qid}`\n")
        f.write(f"- url: {q_item.get('url') or '—'}\n")
        f.write(f"- 时间: {q_item.get('published_at') or q_item.get('fetched_at')}\n")
        f.write(f"- ai_summary: {_truncate(q_item.get('ai_summary'), 400)}\n")
        f.write(f"- 关键词: {q_item.get('ai_keywords') or '—'}\n\n")
        f.write(f"<details><summary>原文（点击展开）</summary>\n\n")
        f.write(f"```\n{(q_item.get('content') or '')[:2000]}\n```\n\n")
        f.write(f"</details>\n\n")

        for m, v in sets:
            ids, mat, idx = cache[(m, v)]
            if qid not in idx:
                f.write(f"### `{m}` × `{v}`：query 不在该 set 内 — 跳过\n\n")
                continue
            qvec = mat[idx[qid]]
            sims = mat @ qvec
            order = np.argsort(-sims)
            seen = 0
            f.write(f"### `{m}` × `{v}`\n\n")
            for rank_idx in order:
                cand_id = ids[rank_idx]
                if cand_id == qid:
                    continue
                cand = by_id.get(cand_id)
                if cand is None:
                    continue
                seen += 1
                f.write(
                    f"- **#{seen} cosine={float(sims[rank_idx]):.4f}** "
                    f"`[{cand['platform']}]` {cand.get('title') or '(无题)'} "
                    f"`{cand_id}` ☐同事件 ☐不同事件 ☐不确定\n"
                )
                f.write(f"  - ai_summary: {_truncate(cand.get('ai_summary'), 220)}\n")
                if seen >= top_k:
                    break
            f.write("\n")
        f.write("---\n\n")
    f.close()
    print(f"[report] wrote → {report_path}")


# =============================================================================
# Step 6: cluster stats (cosine ≥ 0.75 connected components)
# =============================================================================
def step_cluster(threshold: float = 0.75) -> None:
    sets = _all_embedding_sets()
    if not sets:
        raise SystemExit("no embedding sets found — run `embed` first")
    items = _load_sample()
    by_id = {it["id"]: it for it in items}

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"cluster-stats-{ts}.md"
    f = out_path.open("w")
    f.write(f"# 自然簇统计（辅助参考）— {ts}\n\n")
    f.write(f"对每套 embedding，cosine ≥ {threshold} 的成对连接做 union-find。\n\n")
    for m, v in sets:
        ids, mat = _load_embeddings(m, v)
        mat = _normalize(mat)
        n = len(ids)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        sims = mat @ mat.T
        np.fill_diagonal(sims, 0)
        idx = np.argwhere(sims >= threshold)
        for i, j in idx:
            if i >= j:
                continue
            ri, rj = find(int(i)), find(int(j))
            if ri != rj:
                parent[ri] = rj
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        sizes = [len(g) for g in groups.values()]
        sizes.sort(reverse=True)
        non_singleton = [s for s in sizes if s >= 2]

        f.write(f"## `{m}` × `{v}`\n\n")
        f.write(f"- 簇总数（含单元素）：{len(sizes)}\n")
        f.write(f"- 非单元素簇数：{len(non_singleton)}\n")
        f.write(f"- 最大簇大小：{sizes[0] if sizes else 0}\n")
        f.write(f"- 簇大小分布 top-10：{sizes[:10]}\n\n")
        f.write(f"### 最大 5 个簇成员（节选）\n\n")
        big5 = sorted(groups.values(), key=len, reverse=True)[:5]
        for ci, group in enumerate(big5, 1):
            if len(group) < 2:
                break
            f.write(f"#### 簇 {ci}（{len(group)} 成员）\n\n")
            for k in group[:8]:
                it = by_id[ids[k]]
                f.write(f"- `[{it['platform']}]` {it.get('title') or '(无题)'} `{ids[k]}`\n")
            if len(group) > 8:
                f.write(f"- … 还有 {len(group) - 8} 条\n")
            f.write("\n")
        f.write("---\n\n")
    f.close()
    print(f"[cluster] wrote → {out_path}")


# =============================================================================
# Step 7: pairwise cosine histogram (per embedding set)
# =============================================================================
def step_hist() -> None:
    sets = _all_embedding_sets()
    if not sets:
        raise SystemExit("no embedding sets found")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"cosine-histogram-{ts}.md"
    f = out_path.open("w")
    f.write(f"# Pairwise cosine 分布直方图 — {ts}\n\n")
    f.write("每套 embedding 的所有 N×(N-1)/2 对 doc-doc cosine 分布，按 0.05 分桶。\n\n")
    f.write("**用途**：找每个模型的『事件 vs 噪声』分界拐点，避免拍脑袋定阈值。\n\n")
    bins = np.arange(-0.05, 1.05, 0.05)
    for m, v in sets:
        ids, mat = _load_embeddings(m, v)
        mat = _normalize(mat)
        sims = mat @ mat.T
        n = len(ids)
        iu = np.triu_indices(n, k=1)
        pair_sims = sims[iu]
        f.write(f"## `{m}` × `{v}`\n\n")
        f.write(f"- 对数：{len(pair_sims):,}\n")
        f.write(f"- min: {pair_sims.min():.4f}  max: {pair_sims.max():.4f}  "
                f"mean: {pair_sims.mean():.4f}  median: {np.median(pair_sims):.4f}\n")
        f.write(f"- 标准差: {pair_sims.std():.4f}\n")
        # Percentiles for picking thresholds.
        pcts = [50, 75, 90, 95, 99, 99.5, 99.9, 99.95, 99.99]
        f.write(f"- 百分位：" + " | ".join(
            f"P{p}={np.percentile(pair_sims, p):.4f}" for p in pcts
        ) + "\n\n")
        f.write("| cosine 区间 | 对数 | 占比 |\n|---|---|---|\n")
        for i in range(len(bins) - 1):
            lo, hi = bins[i], bins[i + 1]
            n_in = int(((pair_sims >= lo) & (pair_sims < hi)).sum())
            pct = 100.0 * n_in / len(pair_sims) if len(pair_sims) else 0.0
            bar = "█" * int(pct / 1.0)  # 1% per bar char, capped
            f.write(f"| [{lo:.2f}, {hi:.2f}) | {n_in:,} | {pct:5.2f}% {bar[:40]} |\n")
        f.write("\n---\n\n")
    f.close()
    print(f"[hist] wrote → {out_path}")


# =============================================================================
# Step 8: anchor-based threshold sweep
# =============================================================================
# Hand-curated anchor pairs from v1 cluster review (§1.5):
# Each entry: (a_id, b_id, expected_label, comment)
#   expected_label: 1 = same_event (must merge), 0 = not_same_event (must NOT merge)
ANCHOR_PAIRS_DEFAULT_PATH = OUT_DIR / "anchor_pairs.json"


def _load_anchor_pairs() -> list[dict]:
    """Load human-curated anchor pairs (id_a, id_b, label).
    If file missing, build a default set from current sample by matching
    title keywords (rough auto-anchors — user should hand-edit later).
    """
    if ANCHOR_PAIRS_DEFAULT_PATH.exists():
        return json.loads(ANCHOR_PAIRS_DEFAULT_PATH.read_text())
    # Auto-build rough anchors based on simple keyword groups.
    items = _load_sample()
    by_id = {it["id"]: it for it in items}
    titles = {it["id"]: (it.get("title") or "") for it in items}

    def find_ids(*needles: str, limit: int = 8) -> list[str]:
        hits = []
        for iid, t in titles.items():
            tl = t.lower()
            if all(n.lower() in tl for n in needles):
                hits.append(iid)
            if len(hits) >= limit:
                break
        return hits

    same_groups = [
        # (group label, [needles])
        ("DeepSeek-V4-release", ["deepseek", "v4"]),
        ("GPT-Image-2", ["image 2"]),
        ("Trump-NSF", ["national science"]),
        ("OpenClaw-2026.4.24", ["openclaw"]),
    ]
    not_same_groups = [
        # (id_a from group1, id_b from group2): cross-group must NOT merge
        (("DeepSeek-V4-release", 0), ("GPT-Image-2", 0)),
        (("DeepSeek-V4-release", 0), ("Trump-NSF", 0)),
        (("OpenClaw-2026.4.24", 0), ("GPT-Image-2", 0)),
    ]
    pairs: list[dict] = []
    group_ids: dict[str, list[str]] = {}
    for label, needles in same_groups:
        ids = find_ids(*needles)
        group_ids[label] = ids
        # Within-group: all unordered pairs are same_event (label=1)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.append({
                    "a": ids[i], "b": ids[j], "label": 1,
                    "comment": f"same_event:{label}",
                })
    for (gA, iA), (gB, iB) in not_same_groups:
        ids_a = group_ids.get(gA, [])
        ids_b = group_ids.get(gB, [])
        if iA < len(ids_a) and iB < len(ids_b):
            pairs.append({
                "a": ids_a[iA], "b": ids_b[iB], "label": 0,
                "comment": f"not_same:{gA}↔{gB}",
            })
    ANCHOR_PAIRS_DEFAULT_PATH.write_text(
        json.dumps(pairs, ensure_ascii=False, indent=2)
    )
    print(f"[anchor] auto-built {len(pairs)} pairs → {ANCHOR_PAIRS_DEFAULT_PATH} "
          f"(hand-edit if needed)")
    return pairs


def step_anchor() -> None:
    pairs = _load_anchor_pairs()
    n_pos = sum(1 for p in pairs if p["label"] == 1)
    n_neg = sum(1 for p in pairs if p["label"] == 0)
    print(f"[anchor] {len(pairs)} pairs (pos={n_pos}, neg={n_neg})")
    if n_pos == 0 or n_neg == 0:
        raise SystemExit("anchor set must contain both pos and neg pairs")
    sets = _all_embedding_sets()
    items = _load_sample()
    by_id = {it["id"]: it for it in items}

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"anchor-sweep-{ts}.md"
    f = out_path.open("w")
    f.write(f"# 锚点对阈值扫描 — {ts}\n\n")
    f.write(
        f"标注集：{n_pos} 个 same_event 正例 + {n_neg} 个 not_same 负例。\n"
        "对每套 embedding 扫描阈值 0.50→0.99（步长 0.01），算 P / R / F1。\n\n"
    )

    # Show sample pairs.
    f.write("## 锚点对样本\n\n")
    f.write("### 正例（same_event=1，应该被同一阈值合并）\n")
    pos_pairs = [p for p in pairs if p["label"] == 1][:10]
    for p in pos_pairs:
        a = by_id.get(p["a"])
        b = by_id.get(p["b"])
        if not a or not b:
            continue
        f.write(
            f"- `{p['comment']}`：[{a['platform']}] {a.get('title','')[:60]} ↔ "
            f"[{b['platform']}] {b.get('title','')[:60]}\n"
        )
    f.write("\n### 负例（not_same=0，不应该被合并）\n")
    neg_pairs = [p for p in pairs if p["label"] == 0]
    for p in neg_pairs:
        a = by_id.get(p["a"])
        b = by_id.get(p["b"])
        if not a or not b:
            continue
        f.write(
            f"- `{p['comment']}`：[{a['platform']}] {a.get('title','')[:60]} ↔ "
            f"[{b['platform']}] {b.get('title','')[:60]}\n"
        )
    f.write("\n---\n\n")

    summary_rows = []
    for m, v in sets:
        ids, mat = _load_embeddings(m, v)
        mat = _normalize(mat)
        idx = {i: k for k, i in enumerate(ids)}
        labels = []
        cosines = []
        for p in pairs:
            ia, ib = idx.get(p["a"]), idx.get(p["b"])
            if ia is None or ib is None:
                continue
            cos = float(mat[ia] @ mat[ib])
            cosines.append(cos)
            labels.append(p["label"])
        cosines = np.array(cosines)
        labels = np.array(labels)
        if len(cosines) == 0:
            continue

        # Sweep thresholds.
        thresholds = np.arange(0.50, 1.0, 0.01)
        rows = []
        best_f1 = -1.0
        best_t = 0.5
        best_p = 0.0
        best_r = 0.0
        for t in thresholds:
            pred = (cosines >= t).astype(int)
            tp = int(((pred == 1) & (labels == 1)).sum())
            fp = int(((pred == 1) & (labels == 0)).sum())
            fn = int(((pred == 0) & (labels == 1)).sum())
            tn = int(((pred == 0) & (labels == 0)).sum())
            prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
            rows.append((t, prec, rec, f1, tp, fp, fn, tn))
            if f1 > best_f1:
                best_f1 = f1
                best_t = t
                best_p = prec
                best_r = rec

        f.write(f"## `{m}` × `{v}`\n\n")
        f.write(f"- 正例 cosine：min={cosines[labels==1].min():.4f} "
                f"mean={cosines[labels==1].mean():.4f} max={cosines[labels==1].max():.4f}\n")
        if (labels == 0).any():
            f.write(f"- 负例 cosine：min={cosines[labels==0].min():.4f} "
                    f"mean={cosines[labels==0].mean():.4f} max={cosines[labels==0].max():.4f}\n")
        f.write(f"- **最佳阈值 t\\* = {best_t:.2f}** "
                f"→ P={best_p:.3f} R={best_r:.3f} F1={best_f1:.3f}\n\n")
        summary_rows.append({
            "set": f"{m} × {v}",
            "best_t": best_t,
            "best_f1": best_f1,
            "best_p": best_p,
            "best_r": best_r,
            "pos_min": cosines[labels==1].min(),
            "pos_mean": cosines[labels==1].mean(),
            "neg_max": cosines[labels==0].max() if (labels == 0).any() else 0.0,
            "neg_mean": cosines[labels==0].mean() if (labels == 0).any() else 0.0,
        })
        f.write("| 阈值 | P | R | F1 | TP | FP | FN | TN |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        # Show every 2nd threshold around the best to keep table compact.
        for t, prec, rec, f1, tp, fp, fn, tn in rows:
            if abs(t - best_t) < 0.06 or abs(int(t * 100) % 5) == 0:
                star = " ⭐" if abs(t - best_t) < 1e-6 else ""
                f.write(f"| {t:.2f}{star} | {prec:.3f} | {rec:.3f} | {f1:.3f} "
                        f"| {tp} | {fp} | {fn} | {tn} |\n")
        f.write("\n---\n\n")

    # Final ranking
    f.write("## 总排名（按各模型在自己最佳阈值下的 F1）\n\n")
    f.write("| 模型 × 输入 | 最佳阈值 | F1 | P | R | 正例平均 | 负例最大 | 区分度 |\n")
    f.write("|---|---|---|---|---|---|---|---|\n")
    summary_rows.sort(key=lambda x: -x["best_f1"])
    for r in summary_rows:
        gap = r["pos_mean"] - r["neg_mean"]
        f.write(
            f"| {r['set']} | {r['best_t']:.2f} | **{r['best_f1']:.3f}** | "
            f"{r['best_p']:.3f} | {r['best_r']:.3f} | "
            f"{r['pos_mean']:.3f} | {r['neg_max']:.3f} | {gap:+.3f} |\n"
        )
    f.close()
    print(f"[anchor] wrote → {out_path}")


# =============================================================================
# CLI
# =============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=[
        "sample", "inputs", "embed", "queries", "report", "cluster",
        "hist", "anchor", "all",
    ])
    ap.add_argument("--model", choices=["minimax", "e5", "bge-m3", "v1-baseline"])
    ap.add_argument("--variant", choices=VARIANTS)
    ap.add_argument("--threshold", type=float, default=0.75)
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    if args.step == "sample":
        step_sample()
    elif args.step == "inputs":
        step_inputs()
    elif args.step == "embed":
        if not args.model:
            raise SystemExit("--model required for embed")
        step_embed(args.model, args.variant)
    elif args.step == "queries":
        step_queries()
    elif args.step == "report":
        step_report(top_k=args.top_k)
    elif args.step == "cluster":
        step_cluster(threshold=args.threshold)
    elif args.step == "hist":
        step_hist()
    elif args.step == "anchor":
        step_anchor()
    elif args.step == "all":
        step_sample()
        step_inputs()
        for v in VARIANTS:
            step_embed("minimax", v)
        step_embed("v1-baseline", None)
        for v in VARIANTS:
            step_embed("e5", v)
        for v in VARIANTS:
            step_embed("bge-m3", v)
        step_queries()
        step_report(top_k=args.top_k)
        step_cluster(threshold=args.threshold)
        step_hist()
        step_anchor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
