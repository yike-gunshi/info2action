#!/usr/bin/env python3
"""Production-safe public read load gate for info2action.

The default profile only sends public GET requests. Production targets require
an explicit --allow-production flag unless --dry-run is used.
"""
import argparse
import concurrent.futures as futures
import json
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_ROOT = ROOT / "docs" / "qa" / "load-test"
PRODUCTION_HOSTS = {"info2act.com", "www.info2act.com"}


@dataclass(frozen=True)
class Endpoint:
    path: str
    weight: int
    label: str


@dataclass(frozen=True)
class RequestResult:
    endpoint: Endpoint
    status: int | None
    elapsed_ms: float
    error: str | None = None


@dataclass(frozen=True)
class Thresholds:
    max_timeout_rate: float = 0.01
    max_p99_ms: float = 8000.0
    max_consecutive_timeouts: int = 3
    warn_p95_ms: float = 2000.0


@dataclass
class StageSummary:
    stage_name: str
    concurrency: int
    duration_sec: int
    requests: int
    http_2xx: int
    http_4xx: int
    http_5xx: int
    timeouts: int
    other_errors: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    slow_samples: list[dict[str, object]]
    stop_reason: str | None


@dataclass
class LoadGateReport:
    generated_at: str
    target: str
    profile: str
    dry_run: bool
    command: str
    endpoints: list[Endpoint]
    stages: list[StageSummary]
    verdict: str
    risks: list[str]


def join_url(base_url: str, endpoint_path: str) -> str:
    base = urllib.parse.urlsplit(base_url)
    endpoint = endpoint_path.lstrip("/")
    base_path = base.path.rstrip("/")
    if endpoint_path == "/":
        joined_path = f"{base_path}/" if base_path else "/"
    else:
        joined_path = f"{base_path}/{endpoint}" if base_path else f"/{endpoint}"
    return urllib.parse.urlunsplit((base.scheme, base.netloc, joined_path, "", ""))


def expand_profile(profile: str) -> list[Endpoint]:
    if profile != "public-read":
        raise SystemExit(f"unknown profile: {profile}")
    return [
        Endpoint(path="/api/feed/events?page=1&limit=20", weight=30, label="events"),
        Endpoint(path="/api/feed/platforms?per_platform=20", weight=20, label="platforms"),
        Endpoint(path="/api/feed/sections?per_category=20", weight=20, label="sections"),
        Endpoint(path="/api/health", weight=10, label="health"),
        Endpoint(path="/api/search?q=AI&context=recommend&limit=20&events_only=1", weight=10, label="search"),
        Endpoint(path="/", weight=10, label="home"),
    ]


def parse_steps(value: str) -> list[int]:
    try:
        steps = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--steps must be comma-separated integers") from exc
    if not steps or any(step <= 0 for step in steps):
        raise argparse.ArgumentTypeError("--steps must contain positive integers")
    return steps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--profile", default="public-read", choices=("public-read",))
    parser.add_argument("--steps", type=parse_steps, default=parse_steps("1,5,10,20,50"))
    parser.add_argument("--duration-sec", type=int, default=60)
    parser.add_argument("--timeout-sec", type=float, default=8.0)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args(argv)


def is_production_url(base_url: str) -> bool:
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return host in PRODUCTION_HOSTS


def validate_args(args: argparse.Namespace) -> None:
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--base-url must be an http(s) URL")
    if args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be positive")
    if args.timeout_sec <= 0:
        raise SystemExit("--timeout-sec must be positive")
    if is_production_url(args.base_url) and not args.allow_production and not args.dry_run:
        raise SystemExit("production target requires --allow-production")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[index], 1)


def max_consecutive_timeouts(results: list[RequestResult]) -> int:
    best = 0
    current = 0
    for result in results:
        if result.status is None and result.error == "timed out":
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def summarize_stage(
    *,
    stage_name: str,
    concurrency: int,
    duration_sec: int,
    results: list[RequestResult],
    thresholds: Thresholds,
) -> StageSummary:
    elapsed = [result.elapsed_ms for result in results]
    http_2xx = sum(1 for result in results if result.status is not None and 200 <= result.status <= 299)
    http_4xx = sum(1 for result in results if result.status is not None and 400 <= result.status <= 499)
    http_5xx = sum(1 for result in results if result.status is not None and result.status >= 500)
    timeouts = sum(1 for result in results if result.status is None and result.error == "timed out")
    other_errors = sum(1 for result in results if result.status is None and result.error != "timed out")
    p50 = percentile(elapsed, 50)
    p95 = percentile(elapsed, 95)
    p99 = percentile(elapsed, 99)
    max_ms = round(max(elapsed), 1) if elapsed else None

    stop_reason = None
    if http_5xx:
        stop_reason = "5xx_detected"
    elif results and timeouts / len(results) >= thresholds.max_timeout_rate:
        stop_reason = "timeout_rate_exceeded"
    elif max_consecutive_timeouts(results) >= thresholds.max_consecutive_timeouts:
        stop_reason = "consecutive_timeouts"
    elif p99 is not None and p99 > thresholds.max_p99_ms:
        stop_reason = "p99_exceeded"
    # 连接拒绝/DNS 失败等计入 other_errors:目标彻底挂掉时没有 5xx 也没有
    # timeout,若不判定会对着死服务继续加压并给出 PASS(2026-07-04 校准发现)。
    # 公网路径(CF/TLS)存在零星连接毛刺,阈值取「≥5% 且 ≥5 个」避免小样本误停。
    elif results and other_errors >= 5 and other_errors / len(results) >= 0.05:
        stop_reason = "connection_error_rate_exceeded"
    elif not http_2xx:
        stop_reason = "no_successful_response"

    # 错误请求(连接失败/超时)通常耗时短,进不了最慢 top8;强制收录前 4 个
    # 供诊断,再补最慢请求。
    error_results = [r for r in results if r.error is not None][:4]
    slowest = sorted(results, key=lambda item: item.elapsed_ms, reverse=True)
    sample_pool = error_results + [r for r in slowest if r not in error_results]
    slow_samples = [
        {
            "endpoint": result.endpoint.label,
            "path": result.endpoint.path,
            "status": result.status,
            "elapsed_ms": round(result.elapsed_ms, 1),
            "error": result.error,
        }
        for result in sample_pool[:10]
    ]
    return StageSummary(
        stage_name=stage_name,
        concurrency=concurrency,
        duration_sec=duration_sec,
        requests=len(results),
        http_2xx=http_2xx,
        http_4xx=http_4xx,
        http_5xx=http_5xx,
        timeouts=timeouts,
        other_errors=other_errors,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        max_ms=max_ms,
        slow_samples=slow_samples,
        stop_reason=stop_reason,
    )


def should_stop(summary: StageSummary) -> bool:
    return summary.stop_reason is not None


def weighted_endpoint_picker(endpoints: list[Endpoint]) -> Callable[[], Endpoint]:
    weighted: list[Endpoint] = []
    for endpoint in endpoints:
        weighted.extend([endpoint] * endpoint.weight)
    if not weighted:
        raise SystemExit("profile has no endpoints")
    return lambda: random.choice(weighted)


def request_once(base_url: str, endpoint: Endpoint, timeout_sec: float) -> RequestResult:
    url = join_url(base_url, endpoint.path)
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "info2action-load-gate/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            response.read(1024)
            status = int(response.status)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(endpoint=endpoint, status=status, elapsed_ms=round(elapsed_ms, 1))
    except TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(endpoint=endpoint, status=None, elapsed_ms=round(elapsed_ms, 1), error="timed out")
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        try:
            exc.read(1024)
        except Exception:
            pass
        return RequestResult(endpoint=endpoint, status=int(exc.code), elapsed_ms=round(elapsed_ms, 1))
    except urllib.error.URLError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        reason = getattr(exc, "reason", exc)
        error = "timed out" if "timed out" in str(reason).lower() else type(reason).__name__
        return RequestResult(endpoint=endpoint, status=None, elapsed_ms=round(elapsed_ms, 1), error=error)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(endpoint=endpoint, status=None, elapsed_ms=round(elapsed_ms, 1), error=type(exc).__name__)


def run_stage(
    *,
    base_url: str,
    endpoints: list[Endpoint],
    concurrency: int,
    duration_sec: int,
    timeout_sec: float,
    request_func: Callable[[str, Endpoint, float], RequestResult] = request_once,
    max_requests_per_worker: int | None = None,
) -> list[RequestResult]:
    picker = weighted_endpoint_picker(endpoints)
    deadline = time.monotonic() + duration_sec
    results: list[RequestResult] = []

    def worker() -> list[RequestResult]:
        local_results: list[RequestResult] = []
        sent = 0
        while time.monotonic() < deadline:
            if max_requests_per_worker is not None and sent >= max_requests_per_worker:
                break
            endpoint = picker()
            local_results.append(request_func(base_url, endpoint, timeout_sec))
            sent += 1
        return local_results

    with futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        for future in futures.as_completed([executor.submit(worker) for _ in range(concurrency)]):
            results.extend(future.result())
    return results


def ms(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}ms"


def report_to_dict(report: LoadGateReport) -> dict[str, object]:
    return asdict(report)


def stage_verdict(stage: StageSummary) -> str:
    return stage.stop_reason or "PASS"


def render_markdown(report: LoadGateReport) -> str:
    lines = [
        "# Load Gate Report",
        "",
        "## Run Info",
        "",
        f"- time: {report.generated_at}",
        f"- target: {report.target}",
        f"- profile: {report.profile}",
        f"- dry_run: {report.dry_run}",
        f"- tool command: `{report.command}`",
        "",
        "## Preflight",
        "",
        "Preflight should be filled from the runbook checks before executing production load.",
        "",
        "## Ramp Results",
        "",
        "| Stage | Concurrency | Duration | Requests | 2xx | 4xx | 5xx | Timeout | p50 | p95 | p99 | Max | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for stage in report.stages:
        lines.append(
            f"| {stage.stage_name} | {stage.concurrency} | {stage.duration_sec}s | {stage.requests} | "
            f"{stage.http_2xx} | {stage.http_4xx} | {stage.http_5xx} | {stage.timeouts} | "
            f"{ms(stage.p50_ms)} | {ms(stage.p95_ms)} | {ms(stage.p99_ms)} | {ms(stage.max_ms)} | "
            f"{stage_verdict(stage)} |"
        )
    lines.extend(
        [
            "",
            "## Postflight",
            "",
            "Postflight should verify `/api/health`, browser homepage, core tabs, journal logs, and Supabase activity.",
        "",
        "## Decision",
        "",
        f"- verdict: {report.verdict}",
        "- can promote/publicize: only after production preflight, ramp, and postflight evidence",
        f"- risks: {', '.join(report.risks) if report.risks else 'none recorded'}",
        "- follow-up: ",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: LoadGateReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def default_out_dir(profile: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUT_ROOT / f"{stamp}-{profile}"


def build_dry_run_report(args: argparse.Namespace, endpoints: list[Endpoint]) -> LoadGateReport:
    return LoadGateReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        target=args.base_url,
        profile=args.profile,
        dry_run=True,
        command=" ".join(sys.argv),
        endpoints=endpoints,
        stages=[],
        verdict="DRY_RUN",
        risks=[],
    )


def run_load_gate(args: argparse.Namespace) -> LoadGateReport:
    endpoints = expand_profile(args.profile)
    if args.dry_run:
        return build_dry_run_report(args, endpoints)

    thresholds = Thresholds()
    stage_summaries: list[StageSummary] = []
    risks: list[str] = []
    verdict = "PASS"

    for index, concurrency in enumerate(args.steps, start=1):
        stage_name = f"E{index}"
        results = run_stage(
            base_url=args.base_url,
            endpoints=endpoints,
            concurrency=concurrency,
            duration_sec=args.duration_sec,
            timeout_sec=args.timeout_sec,
        )
        summary = summarize_stage(
            stage_name=stage_name,
            concurrency=concurrency,
            duration_sec=args.duration_sec,
            results=results,
            thresholds=thresholds,
        )
        stage_summaries.append(summary)
        print(
            f"{stage_name} c={concurrency} requests={summary.requests} "
            f"2xx={summary.http_2xx} 5xx={summary.http_5xx} timeout={summary.timeouts} "
            f"p95={ms(summary.p95_ms)} p99={ms(summary.p99_ms)} verdict={stage_verdict(summary)}",
            flush=True,
        )
        if should_stop(summary):
            verdict = "FAIL"
            risks.append(f"stopped at {stage_name}: {summary.stop_reason}")
            break

    return LoadGateReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        target=args.base_url,
        profile=args.profile,
        dry_run=False,
        command=" ".join(sys.argv),
        endpoints=endpoints,
        stages=stage_summaries,
        verdict=verdict,
        risks=risks,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    endpoints = expand_profile(args.profile)
    out_dir = args.out_dir or default_out_dir(args.profile)
    if args.dry_run:
        report = build_dry_run_report(args, endpoints)
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        report = run_load_gate(args)
    write_report(report, out_dir)
    print(f"report: {out_dir}")
    return 0 if report.verdict in {"PASS", "DRY_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
