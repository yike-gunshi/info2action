"""PL-3 — run 产物磁盘清理:run_sources 保留 N 个、jsonl 轮转。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

os.environ.setdefault('RATELIMIT_ENABLED', 'false')

from routes import fetch as fetch_mod  # noqa: E402


def test_run_sources_keeps_most_recent_n(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, 'BASE', str(tmp_path))
    monkeypatch.setenv('INFO2ACTION_RUN_SOURCES_KEEP', '3')
    root = tmp_path / 'data' / 'run_sources'
    root.mkdir(parents=True)
    for i in range(6):
        d = root / f'run-{i}'
        d.mkdir()
        (d / 'twitter.json').write_text('{}')
        ts = time.time() - (6 - i) * 3600  # run-5 最新
        os.utime(d, (ts, ts))

    fetch_mod._cleanup_run_artifacts()

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ['run-3', 'run-4', 'run-5']


def test_cluster_events_jsonl_rotates_over_50mb(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, 'BASE', str(tmp_path))
    logs = tmp_path / 'logs'
    logs.mkdir()
    jsonl = logs / 'cluster_events.jsonl'
    jsonl.write_bytes(b'x' * (51 * 1024 * 1024))

    fetch_mod._cleanup_run_artifacts()

    assert not jsonl.exists()
    assert (logs / 'cluster_events.jsonl.1').stat().st_size == 51 * 1024 * 1024


def test_small_jsonl_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, 'BASE', str(tmp_path))
    logs = tmp_path / 'logs'
    logs.mkdir()
    jsonl = logs / 'cluster_events.jsonl'
    jsonl.write_text('line\n')

    fetch_mod._cleanup_run_artifacts()

    assert jsonl.exists()
