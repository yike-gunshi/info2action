import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_highlight_verdicts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_highlight_verdicts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_module()


def _stub_runtime(monkeypatch):
    monkeypatch.setattr(backfill.enrich_items, "load_config", lambda: {"ai_summary": {}})
    monkeypatch.setattr(
        backfill.enrich_items,
        "resolve_minimax_runtime_config",
        lambda _config: ("api-key", "https://api.example.com", "test-model"),
    )


def test_default_scorer_uses_v38_path(monkeypatch):
    _stub_runtime(monkeypatch)
    load_prompt_calls = []
    monkeypatch.setattr(
        backfill.highlight_verdict,
        "load_system_prompt",
        lambda: load_prompt_calls.append(True) or "system prompt",
    )
    monkeypatch.setattr(
        backfill.highlight_verdict,
        "build_item_content",
        lambda _item: "item content",
    )
    monkeypatch.setattr(
        backfill.highlight_verdict,
        "normalize_verdict_result",
        lambda _raw: {"cluster_verdict": "featured"},
    )
    monkeypatch.setattr(backfill.enrich_items, "call_minimax", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        backfill.enrich_items,
        "enrich_highlight_score_v26_for_item",
        lambda *_args, **_kwargs: pytest.fail("v26 scorer must not run by default"),
    )
    monkeypatch.setattr(
        backfill.remote_db,
        "query_pending_highlight_verdict_items_remote",
        lambda **_kwargs: [{"id": "item-1"}],
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--dry-run"])

    assert backfill.main() == 0
    assert load_prompt_calls == [True]


def test_v26_scorer_processes_each_item_and_uses_v26_prompt_version(monkeypatch, capsys):
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(
        backfill.enrich_items,
        "resolve_highlight_scorer_config",
        lambda: ("ignored-scorer", 4.75),
    )
    query_calls = []
    monkeypatch.setattr(
        backfill.remote_db,
        "query_pending_highlight_verdict_items_remote",
        lambda **kwargs: query_calls.append(kwargs) or [{"id": "item-1"}, {"id": "item-2"}],
    )
    monkeypatch.setattr(
        backfill.highlight_verdict,
        "load_system_prompt",
        lambda: pytest.fail("v26 scorer must not load the v38 prompt"),
    )
    scorer_calls = []

    def fake_v26(item, api_key, api_base, model, **kwargs):
        scorer_calls.append((item, api_key, api_base, model, kwargs))
        if item["id"] == "item-1":
            return {"cluster_verdict": "featured"}
        return None

    monkeypatch.setattr(
        backfill.enrich_items,
        "enrich_highlight_score_v26_for_item",
        fake_v26,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--scorer", "v26", "--rescore-version-mismatch", "--dry-run"],
    )

    assert backfill.main() == 0
    assert len(scorer_calls) == 2
    assert all(call[4]["threshold"] == 4.75 for call in scorer_calls)
    assert query_calls[0]["rescore_prompt_version"] == backfill.highlight_score_v26.PROMPT_VERSION
    output = capsys.readouterr().out
    assert "'featured': 1" in output
    assert "'scored': 1" in output


def test_v26_scorer_requires_configured_threshold(monkeypatch, capsys):
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(
        backfill.enrich_items,
        "resolve_highlight_scorer_config",
        lambda: ("ignored-scorer", None),
    )
    monkeypatch.setattr(
        backfill.remote_db,
        "query_pending_highlight_verdict_items_remote",
        lambda **_kwargs: pytest.fail("items must not be queried without a threshold"),
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--scorer", "v26"])

    assert backfill.main() != 0
    assert "INFO2ACTION_HIGHLIGHT_V26_THRESHOLD" in capsys.readouterr().out


def test_v26_scorer_counts_errors_without_recording_a_second_failure(monkeypatch, capsys):
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(
        backfill.enrich_items,
        "resolve_highlight_scorer_config",
        lambda: ("ignored-scorer", 4.75),
    )
    monkeypatch.setattr(
        backfill.remote_db,
        "query_pending_highlight_verdict_items_remote",
        lambda **_kwargs: [{"id": "item-1"}],
    )
    monkeypatch.setattr(
        backfill.enrich_items,
        "enrich_highlight_score_v26_for_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scoring failed")),
    )
    monkeypatch.setattr(
        backfill.remote_db,
        "record_highlight_verdict_failure_remote",
        lambda *_args, **_kwargs: pytest.fail("v26 handles its own failure recording"),
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--scorer", "v26"])

    assert backfill.main() == 0
    assert "'error': 1" in capsys.readouterr().out
