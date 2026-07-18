import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import remote_db


def test_cluster_bundle_cache_ttl_default_and_override(monkeypatch):
    monkeypatch.delenv("INFO2ACTION_CLUSTER_BUNDLE_CACHE_TTL_SEC", raising=False)
    assert remote_db._cluster_bundle_cache_ttl_sec() == 300
    # longer than the generic 180s result cache (repeat-open / cold-read relief)
    assert remote_db._cluster_bundle_cache_ttl_sec() >= remote_db._remote_cache_ttl()

    monkeypatch.setenv("INFO2ACTION_CLUSTER_BUNDLE_CACHE_TTL_SEC", "900")
    assert remote_db._cluster_bundle_cache_ttl_sec() == 900

    monkeypatch.setenv("INFO2ACTION_CLUSTER_BUNDLE_CACHE_TTL_SEC", "-5")
    assert remote_db._cluster_bundle_cache_ttl_sec() == 0


def test_cluster_bundle_reads_and_writes_ttl_cache(monkeypatch):
    # cluster_bundle should serve a warmed entry without a second DB checkout.
    remote_db._cache_clear_all()
    key = (
        "cluster_bundle",
        remote_db.remote_schema(),
        999001,
        1,
        20,
        True,
        "",
    )
    payload = {"cluster": {"id": 999001}, "sources": [], "data_backend": "x"}
    remote_db._cache_set_copy_with_ttl(key, payload, remote_db._cluster_bundle_cache_ttl_sec())

    connect_calls = []
    orig_connect = remote_db.connect

    def _tracking_connect(*a, **k):
        connect_calls.append(1)
        return orig_connect(*a, **k)

    monkeypatch.setattr(remote_db, "connect", _tracking_connect)
    got = remote_db.cluster_bundle(cluster_id=999001, page=1, limit=20, public_only=True)
    assert got is not None
    assert got["cluster"]["id"] == 999001
    assert connect_calls == []  # served from cache, no DB checkout
    remote_db._cache_clear_all()
