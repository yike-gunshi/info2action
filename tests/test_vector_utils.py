"""Tests for src/clustering/vector_utils.py

Covers:
- cosine_similarity: orthogonal / parallel / anti-parallel / high-dim
- weighted_mean_with_decay: exp(-age/tau) weighting monotonic in age
- pack_blob / unpack_blob: round-trip numpy float32 vector
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from clustering import vector_utils as vu  # noqa: E402


class TestCosineSimilarity:
    def test_parallel_same_vector_is_one(self):
        v = np.array([0.3, 0.4, 0.5], dtype=np.float32)
        assert vu.cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_anti_parallel_is_minus_one(self):
        v = np.array([1.0, 0.0], dtype=np.float32)
        assert vu.cosine_similarity(v, -v) == pytest.approx(-1.0, abs=1e-6)

    def test_orthogonal_is_zero(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        assert vu.cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_zero_vector_returns_zero(self):
        z = np.zeros(3, dtype=np.float32)
        v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        # No NaN, safe fallback (important for cold-start).
        assert vu.cosine_similarity(z, v) == 0.0
        assert vu.cosine_similarity(v, z) == 0.0

    def test_dimension_mismatch_returns_zero(self):
        a = np.ones(1536, dtype=np.float32)
        b = np.ones(2048, dtype=np.float32)
        assert vu.cosine_similarity(a, b) == 0.0


class TestWeightedMeanWithDecay:
    def test_single_vector_returns_itself(self):
        v = np.array([1.0, 2.0], dtype=np.float32)
        ts_now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        out = vu.weighted_mean_with_decay(
            [v], [ts_now], now=ts_now, tau_hours=24
        )
        assert np.allclose(out, v, atol=1e-6)

    def test_equal_timestamps_equal_weight_gives_simple_mean(self):
        vecs = [
            np.array([1.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
        ]
        t = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        out = vu.weighted_mean_with_decay(vecs, [t, t], now=t, tau_hours=24)
        assert np.allclose(out, np.array([0.5, 0.5]), atol=1e-6)

    def test_older_vector_has_smaller_weight(self):
        """Newer docs dominate the representative vector (R7.3 spirit)."""
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        fresh = np.array([1.0, 0.0], dtype=np.float32)
        stale = np.array([0.0, 1.0], dtype=np.float32)
        # stale is 48h older (2 tau half-lives)
        stale_ts = now - timedelta(hours=48)
        out = vu.weighted_mean_with_decay(
            [fresh, stale], [now, stale_ts], now=now, tau_hours=24
        )
        assert out[0] > out[1], "fresh vector should dominate after exp decay"

    def test_invalid_input_returns_none(self):
        assert vu.weighted_mean_with_decay([], [], now=datetime.now(timezone.utc)) is None


class TestBlobRoundTrip:
    def test_pack_unpack_preserves_values(self):
        v = np.random.RandomState(42).randn(768).astype(np.float32)
        blob = vu.pack_blob(v)
        assert isinstance(blob, (bytes, bytearray, memoryview))
        restored = vu.unpack_blob(blob)
        assert restored.dtype == np.float32
        assert np.allclose(restored, v, atol=1e-6)

    def test_unpack_none_returns_none(self):
        assert vu.unpack_blob(None) is None
