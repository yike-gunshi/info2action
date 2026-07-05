"""Vector math utilities for event-aggregation clustering.

Invariants:
- All vectors are numpy float32 (match BLOB storage in `items.embedding`).
- weighted_mean_with_decay uses exp(-age_h/tau) so newer docs dominate.
  (PRD §4.8 "代表向量加权平均 + τ=24h 新度衰减")
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence

import numpy as np


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Zero vectors return 0.0 (no NaN)."""
    if a.shape != b.shape:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def weighted_mean_with_decay(
    vectors: Sequence[np.ndarray],
    timestamps: Sequence[datetime],
    *,
    now: datetime,
    tau_hours: float = 24.0,
) -> np.ndarray | None:
    """Weighted mean where weight = exp(-age_hours / tau_hours).

    Returns None when `vectors` is empty.
    All timestamps must be timezone-aware or all naive (treated as UTC).
    """
    if not vectors or len(vectors) != len(timestamps):
        return None
    weights = []
    for ts in timestamps:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        weights.append(math.exp(-age_h / tau_hours))
    w = np.asarray(weights, dtype=np.float32)
    total = float(w.sum())
    if total == 0.0:
        # Extremely old vectors; return simple mean as fallback.
        stacked = np.stack(vectors).astype(np.float32)
        return stacked.mean(axis=0)
    stacked = np.stack(vectors).astype(np.float32)
    return (stacked * w[:, None]).sum(axis=0) / total


def pack_blob(vector: np.ndarray) -> bytes:
    """Serialize a numpy vector to bytes for SQLite BLOB storage."""
    v = np.asarray(vector, dtype=np.float32)
    return v.tobytes()


def unpack_blob(blob: bytes | None) -> np.ndarray | None:
    """Deserialize BLOB back to numpy float32 array. Returns None if blob is None."""
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)
