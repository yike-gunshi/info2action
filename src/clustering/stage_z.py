"""Stage Z — cosine 聚簇（v2 极简版）

设计稿 docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5.5.2

策略：宽召回 + 直径约束 + 硬上限。错合 OK，漏合不行；错合靠 Stage P 清洗。

算法：single-pass agglomerative
  for item in 待聚簇 items（fetched_at DESC）:
    候选 = filter(cosine(item, cluster.centroid) >= 0.60)
    sort by cos DESC
    for cluster in 候选:
      if len(cluster.members) >= 50: continue          # 硬上限
      if all(cosine(item, m) >= 0.55 for m in members):  # 直径约束
        加入 + 更新 centroid
        break
    若未加入：新建簇

时间窗：仅最近 14 天 fetched_at 的 enriched item 参与。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np

from .vector_utils import pack_blob, unpack_blob

LOGGER = logging.getLogger(__name__)

CANDIDATE_THRESHOLD = 0.60
DIAMETER_THRESHOLD = 0.55
MAX_MEMBERS_PER_CLUSTER = 50
DEFAULT_DAYS_WINDOW = 14
DEFAULT_DOMINANT_PRIORITY = (
    "products",
    "efficiency_tools",
    "coding",
    "skill",
    "models",
    "eval",
    "tech",
    "tutorials",
    "industry",
    "creator",
    "investment",
    "startup",
    "events",
    "other",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_pending_items(conn, *, days: int = DEFAULT_DAYS_WINDOW) -> list[dict]:
    """选出待聚簇的 enriched item。

    条件：
    - stage_a_state='done'（embedding 已生成）
    - fetched_at >= now - days
    - 不在已有 cluster_items_v2.removed_at IS NULL 的成员里（避免重复聚簇）
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    # v5 决策（2026-04-29）：限定 ai_categories IS NOT NULL，只 cluster v4 enriched doc。
    # 避免 v3 老 doc（只有 ai_summary 但没 v4 分类）混入污染对比基线。
    rows = conn.execute(
        """SELECT id, ai_category, embedding, fetched_at
           FROM items
           WHERE stage_a_state = 'done'
             AND embedding IS NOT NULL
             AND ai_categories IS NOT NULL
             AND fetched_at >= ?
             AND id NOT IN (
               SELECT item_id FROM cluster_items_v2 WHERE removed_at IS NULL
             )
           ORDER BY fetched_at DESC""",
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        vec = unpack_blob(r["embedding"])
        if vec is None or vec.shape != (1024,):
            continue
        out.append({
            "id": r["id"],
            "ai_category": r["ai_category"] or "",
            "embedding": vec.astype(np.float32),
            "fetched_at": r["fetched_at"],
        })
    return out


def _normalize_l2(vec: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(vec))
    if n == 0.0:
        return vec
    return (vec / n).astype(np.float32)


class _ClusterAccumulator:
    """聚簇过程中的内存表示。完成后批量写入 DB。"""

    def __init__(self, members: list[dict] | None = None):
        self.members = list(members or [])
        if self.members:
            self._update_centroid()
        else:
            self.centroid = np.zeros(1024, dtype=np.float32)
            self.member_vecs = np.zeros((0, 1024), dtype=np.float32)

    def _update_centroid(self) -> None:
        self.member_vecs = np.stack([m["embedding"] for m in self.members]).astype(np.float32)
        # 简单均值再 L2 归一化（保持跟成员同坐标系，cos 距离对比可比）
        self.centroid = _normalize_l2(self.member_vecs.mean(axis=0))

    def cosine_to_item(self, item: dict) -> float:
        return float(np.dot(self.centroid, item["embedding"]))

    def can_accept(self, item: dict) -> tuple[bool, float]:
        """返回 (是否能并入, 与成员的最低 cos)。失败原因记 log."""
        if len(self.members) >= MAX_MEMBERS_PER_CLUSTER:
            return False, 0.0
        if self.member_vecs.size == 0:
            return True, 1.0
        sims = self.member_vecs @ item["embedding"]
        min_sim = float(sims.min())
        return (min_sim >= DIAMETER_THRESHOLD, min_sim)

    def add(self, item: dict, joined_cosine: float) -> None:
        item_with_join = dict(item)
        item_with_join["joined_cosine"] = joined_cosine
        self.members.append(item_with_join)
        self._update_centroid()


def _dominant_category(members: list[dict]) -> str | None:
    """簇内多数 doc 的 category 决定 dominant_category；平局按优先级表 break。"""
    if not members:
        return None
    counts: dict[str, int] = {}
    for m in members:
        cat = (m.get("ai_category") or "").strip().lower()
        if not cat:
            continue
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return None
    max_n = max(counts.values())
    candidates = [c for c, n in counts.items() if n == max_n]
    if len(candidates) == 1:
        return candidates[0]
    for priority_cat in DEFAULT_DOMINANT_PRIORITY:
        if priority_cat in candidates:
            return priority_cat
    return sorted(candidates)[0]


def _persist_cluster(conn, accum: _ClusterAccumulator) -> int:
    now = _utc_now_iso()
    dominant = _dominant_category(accum.members)
    cur = conn.execute(
        """INSERT INTO clusters_v2
             (centroid, dominant_category, member_count, created_at,
              last_member_added_at, stage_p_state)
           VALUES (?, ?, ?, ?, ?, 'dirty')""",
        (pack_blob(accum.centroid), dominant, len(accum.members), now, now),
    )
    cluster_id = cur.lastrowid
    for m in accum.members:
        conn.execute(
            """INSERT INTO cluster_items_v2
                 (cluster_id, item_id, added_at, joined_cosine)
               VALUES (?, ?, ?, ?)""",
            (cluster_id, m["id"], now, m.get("joined_cosine")),
        )
    return cluster_id


def run_stage_z(conn, *, days: int = DEFAULT_DAYS_WINDOW) -> dict:
    """Stage Z 主入口。在内存里做单遍 agglomerative，最后批量写入 DB。

    Returns:
        {processed, created_clusters, total_members, max_cluster_size,
         single_doc_cluster_count, took_seconds}
    """
    started = time.time()
    items = query_pending_items(conn, days=days)
    LOGGER.info("Stage Z: 待聚簇 item 数 = %d (window=%d days)", len(items), days)

    accumulators: list[_ClusterAccumulator] = []

    for item in items:
        if not accumulators:
            accumulators.append(_ClusterAccumulator([item]))
            continue
        # 计算与所有现有 centroid 的 cos
        centroids = np.stack([a.centroid for a in accumulators]).astype(np.float32)
        sims = centroids @ item["embedding"]
        # 候选：cos >= CANDIDATE_THRESHOLD，sort DESC
        candidate_idx = [i for i, s in enumerate(sims) if s >= CANDIDATE_THRESHOLD]
        candidate_idx.sort(key=lambda i: -float(sims[i]))

        joined = False
        for idx in candidate_idx:
            accum = accumulators[idx]
            ok, min_sim = accum.can_accept(item)
            if ok:
                accum.add(item, joined_cosine=float(sims[idx]))
                joined = True
                break
        if not joined:
            accumulators.append(_ClusterAccumulator([item]))

    # 批量写入
    created = 0
    total_members = 0
    sizes = []
    single_doc_clusters = 0
    for accum in accumulators:
        cluster_id = _persist_cluster(conn, accum)
        created += 1
        total_members += len(accum.members)
        sizes.append(len(accum.members))
        if len(accum.members) == 1:
            single_doc_clusters += 1
    conn.commit()

    stats = {
        "processed": len(items),
        "created_clusters": created,
        "total_members": total_members,
        "max_cluster_size": max(sizes) if sizes else 0,
        "avg_cluster_size": round(total_members / created, 2) if created else 0,
        "single_doc_cluster_count": single_doc_clusters,
        "took_seconds": round(time.time() - started, 2),
    }
    LOGGER.info("Stage Z 完成: %s", json.dumps(stats, ensure_ascii=False))
    return stats


def reset_clusters_v2(conn) -> dict:
    """清空 v2 聚簇结果（保留 cluster_p_log 审计）。便于重跑。"""
    deleted_items = conn.execute("DELETE FROM cluster_items_v2").rowcount
    deleted_clusters = conn.execute("DELETE FROM clusters_v2").rowcount
    # 重置自增 ID
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('clusters_v2','cluster_items_v2')")
    conn.commit()
    return {"deleted_clusters": deleted_clusters, "deleted_cluster_items": deleted_items}
