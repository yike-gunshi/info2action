"""BF-0804-1: 聚类跨轮补救预算不得静默置零。

生产曾把 INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT 设为 0,`if retry_limit > 0`
整段补救路径被跳过,已判 featured 的条目拿不到 embedding→永久不进簇→精选
静默丢件(7 天 649 条)。该变量当时既不在 .env.example 里,置零也不打日志。

这里守三件事:置零必须留痕、所有补救预算读取都走会留痕的入口、两个变量
必须在 .env.example 有记录且注明置零后果。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from clustering import pipeline  # noqa: E402

_ENV_EXAMPLE = os.path.join(os.path.dirname(__file__), "..", ".env.example")
_PIPELINE_SRC = os.path.join(
    os.path.dirname(__file__), "..", "src", "clustering", "pipeline.py"
)

_ITEM_VAR = "INFO2ACTION_CLUSTER_ITEM_RETRY_LIMIT"
_SUMMARY_VAR = "INFO2ACTION_CLUSTER_SUMMARY_RETRY_LIMIT"


@pytest.fixture(autouse=True)
def _reset_warned():
    pipeline._RETRY_DISABLED_WARNED.clear()
    yield
    pipeline._RETRY_DISABLED_WARNED.clear()


def test_unset_falls_back_to_default_without_warning(monkeypatch):
    monkeypatch.delenv(_ITEM_VAR, raising=False)
    events = []
    monkeypatch.setattr(pipeline, "_log_event", lambda e, **f: events.append((e, f)))

    assert pipeline._retry_limit_env(_ITEM_VAR, 200) == 200
    assert events == []


def test_zero_budget_emits_disabled_event(monkeypatch):
    monkeypatch.setenv(_ITEM_VAR, "0")
    events = []
    monkeypatch.setattr(pipeline, "_log_event", lambda e, **f: events.append((e, f)))

    assert pipeline._retry_limit_env(_ITEM_VAR, 200) == 0
    assert [e for e, _ in events] == ["cluster_retry_disabled"]
    assert events[0][1]["setting"] == _ITEM_VAR


def test_zero_budget_warns_once_per_setting(monkeypatch):
    monkeypatch.setenv(_ITEM_VAR, "0")
    monkeypatch.setenv(_SUMMARY_VAR, "0")
    events = []
    monkeypatch.setattr(pipeline, "_log_event", lambda e, **f: events.append((e, f)))

    for _ in range(3):
        pipeline._retry_limit_env(_ITEM_VAR, 200)
        pipeline._retry_limit_env(_SUMMARY_VAR, 100)

    assert sorted(f["setting"] for _, f in events) == [_ITEM_VAR, _SUMMARY_VAR]


def test_every_retry_budget_read_goes_through_the_warning_entrypoint():
    """新增补救预算读取点时别绕过 _retry_limit_env,否则又会静默。"""
    with open(_PIPELINE_SRC, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    found = 0
    for idx, line in enumerate(lines):
        if _ITEM_VAR not in line and _SUMMARY_VAR not in line:
            continue
        if line.lstrip().startswith("#"):
            continue
        found += 1
        caller = f"{lines[idx - 1]}{line}"
        assert "_retry_limit_env(" in caller, (
            f"pipeline.py:{idx + 1} 读补救预算没走 _retry_limit_env: {line.strip()}"
        )
    assert found == 6, f"补救预算读取点数量变了({found}),确认新增的那处也留痕"


def test_env_example_documents_both_retry_budgets():
    with open(_ENV_EXAMPLE, encoding="utf-8") as fh:
        text = fh.read()

    for var in (_ITEM_VAR, _SUMMARY_VAR):
        assert f"{var}=" in text, f"{var} 未写入 .env.example(隐藏开关是本 bug 的根因)"

    # 必须说清置零后果,对齐隔壁富化那对的写法
    assert "设为 0" in text and "补救" in text
