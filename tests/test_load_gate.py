from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOAD_GATE_PATH = ROOT / "scripts" / "perf" / "load_gate.py"


def load_gate_module():
    spec = importlib.util.spec_from_file_location("load_gate", LOAD_GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_join_url_keeps_base_path_and_query():
    load_gate = load_gate_module()

    assert (
        load_gate.join_url("https://example.com/app/", "/api/feed/events?page=1&limit=20")
        == "https://example.com/app/api/feed/events?page=1&limit=20"
    )


def test_public_read_profile_expands_weighted_endpoints():
    load_gate = load_gate_module()

    endpoints = load_gate.expand_profile("public-read")
    paths = [endpoint.path for endpoint in endpoints]

    assert "/api/feed/events?page=1&limit=20" in paths
    assert "/api/feed/platforms?per_platform=20" in paths
    assert "/api/feed/sections?per_category=20" in paths
    assert "/api/actions/board" not in paths
    assert sum(endpoint.weight for endpoint in endpoints) == 100


def test_percentiles_and_stage_summary_are_calculated_from_results():
    load_gate = load_gate_module()
    endpoint = load_gate.Endpoint(path="/api/health", weight=100, label="health")
    results = [
        load_gate.RequestResult(endpoint=endpoint, status=200, elapsed_ms=100.0),
        load_gate.RequestResult(endpoint=endpoint, status=200, elapsed_ms=200.0),
        load_gate.RequestResult(endpoint=endpoint, status=503, elapsed_ms=900.0),
        load_gate.RequestResult(endpoint=endpoint, status=None, elapsed_ms=1000.0, error="timed out"),
    ]

    summary = load_gate.summarize_stage(
        stage_name="E1",
        concurrency=1,
        duration_sec=60,
        results=results,
        thresholds=load_gate.Thresholds(),
    )

    assert summary.requests == 4
    assert summary.http_2xx == 2
    assert summary.http_5xx == 1
    assert summary.timeouts == 1
    assert summary.p50_ms == 200.0
    assert summary.p95_ms == 1000.0
    assert summary.stop_reason == "5xx_detected"


def test_production_guard_blocks_without_explicit_allowance():
    load_gate = load_gate_module()

    args = load_gate.parse_args(["--base-url", "https://info2act.com", "--dry-run"])
    load_gate.validate_args(args)

    args = load_gate.parse_args(["--base-url", "https://info2act.com", "--steps", "1"])
    try:
        load_gate.validate_args(args)
    except SystemExit as exc:
        assert "requires --allow-production" in str(exc)
    else:
        raise AssertionError("production run without --allow-production should fail")


def test_hard_stop_detects_timeout_rate_without_real_network():
    load_gate = load_gate_module()
    endpoint = load_gate.Endpoint(path="/api/health", weight=100, label="health")
    results = [
        load_gate.RequestResult(endpoint=endpoint, status=200, elapsed_ms=100.0),
        load_gate.RequestResult(endpoint=endpoint, status=None, elapsed_ms=8000.0, error="timed out"),
    ]

    summary = load_gate.summarize_stage(
        stage_name="E1",
        concurrency=1,
        duration_sec=60,
        results=results,
        thresholds=load_gate.Thresholds(),
    )

    assert summary.stop_reason == "timeout_rate_exceeded"
    assert load_gate.should_stop(summary)


def test_run_stage_accepts_fake_request_runner_without_network():
    load_gate = load_gate_module()
    endpoint = load_gate.Endpoint(path="/api/health", weight=100, label="health")
    calls = []

    def fake_request(base_url, endpoint_arg, timeout_sec):
        calls.append((base_url, endpoint_arg.path, timeout_sec))
        return load_gate.RequestResult(endpoint=endpoint_arg, status=200, elapsed_ms=12.0)

    results = load_gate.run_stage(
        base_url="https://example.com",
        endpoints=[endpoint],
        concurrency=2,
        duration_sec=60,
        timeout_sec=8,
        request_func=fake_request,
        max_requests_per_worker=2,
    )

    assert len(results) == 4
    assert calls == [
        ("https://example.com", "/api/health", 8),
        ("https://example.com", "/api/health", 8),
        ("https://example.com", "/api/health", 8),
        ("https://example.com", "/api/health", 8),
    ]


def test_report_files_are_written(tmp_path):
    load_gate = load_gate_module()
    endpoint = load_gate.Endpoint(path="/api/health", weight=100, label="health")
    stage = load_gate.StageSummary(
        stage_name="E1",
        concurrency=1,
        duration_sec=1,
        requests=1,
        http_2xx=1,
        http_4xx=0,
        http_5xx=0,
        timeouts=0,
        other_errors=0,
        p50_ms=100.0,
        p95_ms=100.0,
        p99_ms=100.0,
        max_ms=100.0,
        slow_samples=[],
        stop_reason=None,
    )

    report = load_gate.LoadGateReport(
        generated_at="2026-06-09T00:00:00Z",
        target="https://example.com",
        profile="public-read",
        dry_run=True,
        command="load_gate.py --dry-run",
        endpoints=[endpoint],
        stages=[stage],
        verdict="PASS",
        risks=[],
    )
    load_gate.write_report(report, tmp_path)

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert result["verdict"] == "PASS"
    assert "| E1 | 1 | 1s | 1 | 1 | 0 | 0 | 0 | 100.0ms | 100.0ms | 100.0ms | 100.0ms | PASS |" in markdown
