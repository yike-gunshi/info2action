"""T4a: micro source dispatch supports lingowhale.

Feature: incremental-fetch-15min (v20.0), Ring ② 前置.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from routes import fetch as fetch_route  # noqa: E402


def test_lingowhale_micro_dispatch_invokes_fetch_lingowhale(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append([str(a) for a in args])

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(fetch_route.subprocess, "run", fake_run)
    ok = fetch_route._run_source_fetch_step("lingowhale", "subscription", output_root="/tmp/x")
    assert ok is True
    assert any("fetch_lingowhale.py" in " ".join(c) for c in calls), \
        "lingowhale micro source 应调用 fetch_lingowhale.py"


def test_unknown_micro_source_returns_false(monkeypatch):
    # 回归:未支持的平台仍返回 False(不静默假装成功)
    monkeypatch.setattr(fetch_route.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用")))
    ok = fetch_route._run_source_fetch_step("nonexistent_platform", "whatever", output_root="/tmp/x")
    assert ok is False
