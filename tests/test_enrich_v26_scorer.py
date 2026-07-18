from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items  # noqa: E402
import highlight_score_v26  # noqa: E402
import remote_db  # noqa: E402


def _score_result(**overrides):
    result = {
        "reject": False,
        "reject_reason": "",
        "content_type": "tutorial_method",
        "content_type_confidence": 0.9,
        "dims": {
            "authority": 2,
            "substance": 3,
            "novelty": 2,
            "timeliness": 1,
            "audience_fit": 3,
        },
        "marketing": 0,
        "veto": "none",
        "uncertainty": "none",
        "value_path": "substantive",
        "reason": "完整可复用的教程",
        "confidence": 0.85,
    }
    result.update(overrides)
    return result


def _score_result_at(score10, **overrides):
    dims_by_score = {
        4.5: {
            "authority": 0,
            "substance": 0,
            "novelty": 3,
            "timeliness": 3,
            "audience_fit": 3,
        },
        5.5: {
            "authority": 1,
            "substance": 1,
            "novelty": 1,
            "timeliness": 2,
            "audience_fit": 3,
        },
        8.0: {
            "authority": 1,
            "substance": 3,
            "novelty": 1,
            "timeliness": 1,
            "audience_fit": 3,
        },
    }
    result = _score_result(dims=dims_by_score[score10])
    result.update(overrides)
    return result


def test_build_item_content_v26_includes_quoted_tweet_from_string_detail_json():
    item = {
        "id": "x-1",
        "platform": "twitter",
        "title": "原推标题",
        "content": "原推正文",
        "detail_json": json.dumps(
            {"quotedTweet": {"text": "被引用推文的完整信息"}},
            ensure_ascii=False,
        ),
    }

    content = enrich_items.build_item_content_v26(item)

    assert "原推正文" in content
    assert "quoted: 被引用推文的完整信息" in content


def test_build_item_content_v26_includes_dict_readme_even_when_description_exists():
    readme = "README_START\n" + ("细节" * 5000) + "\nREADME_END"
    item = {
        "id": "gh-1",
        "platform": "github",
        "title": "owner/repo",
        "content": "GitHub description is already non-empty",
        "detail_json": {"readme": readme},
    }

    content = enrich_items.build_item_content_v26(item)

    assert "GitHub description is already non-empty" in content
    assert "readme: README_START" in content
    readme_segment = content.split("readme: ", 1)[1]
    assert len(readme_segment) == 8000
    assert "【完整 README】" not in content


def test_enrich_highlight_score_v26_uses_prompt_loader_temp_zero_and_composition(monkeypatch):
    calls = {}
    raw_result = _score_result()

    monkeypatch.setattr(enrich_items, "load_prompt", lambda filename: calls.setdefault("prompt", filename) or "")

    def fake_call(*args, **kwargs):
        calls["call_args"] = args
        calls["call_kwargs"] = kwargs
        return json.dumps(raw_result, ensure_ascii=False)

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call)
    monkeypatch.setattr(
        enrich_items,
        "write_highlight_score_v26_current",
        lambda item_id, result, threshold: calls.update(
            written=(item_id, result, threshold)
        ),
    )

    result = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-1", "title": "教程", "content": "足够长的教程正文"},
        "key",
        "https://minimax.example",
        "MiniMax-M3",
        threshold=5.5,
        dry_run=False,
    )

    assert calls["prompt"] == "15_item_score_v26.md"
    assert calls["call_args"][3] == "15_item_score_v26.md"
    assert "足够长的教程正文" in calls["call_args"][4]
    assert calls["call_kwargs"]["temperature"] == 0.0
    assert result["score10"] == 8.8
    assert result["is_flag_bearer"] is True
    assert calls["written"] == ("item-1", result, 5.5)


def test_enrich_highlight_score_v26_records_parse_failure_without_writing(monkeypatch):
    calls = []
    monkeypatch.setattr(enrich_items, "load_prompt", lambda _filename: "system")
    monkeypatch.setattr(enrich_items, "call_minimax", lambda *args, **kwargs: "not-json")
    monkeypatch.setattr(
        enrich_items,
        "write_highlight_score_v26_current",
        lambda *_args, **_kwargs: calls.append("write"),
    )
    monkeypatch.setattr(
        enrich_items,
        "record_highlight_verdict_failure_current",
        lambda item_id, error: calls.append((item_id, error)),
    )

    result = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-bad", "title": "bad", "content": "bad response"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=False,
    )

    assert result is None
    assert "write" not in calls
    assert calls[0][0] == "item-bad"
    assert "json_parse_error" in calls[0][1]


def test_enrich_highlight_score_v26_reruns_edge_band_and_keeps_lower_run_fields(
    monkeypatch,
    caplog,
):
    first = _score_result_at(5.5, reason="first run", value_path="substantive")
    second = _score_result_at(
        4.5,
        reason="lower second run",
        value_path="lead_value",
        uncertainty="thin_detail",
        veto="rumor_unverified",
    )
    responses = iter([first, second])
    calls = []
    monkeypatch.setattr(enrich_items, "load_prompt", lambda _filename: "system")

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(next(responses), ensure_ascii=False)

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call)
    caplog.set_level("INFO", logger="enrich_items")

    result = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-edge", "title": "edge", "content": "edge content"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=True,
    )

    assert len(calls) == 2
    assert calls[0][0][3:5] == calls[1][0][3:5]
    assert result["score10"] == 5.0
    assert result["runs"] == [5.5, 4.5]
    assert result["dims"] == second["dims"]
    assert result["reason"] == "lower second run"
    assert result["value_path"] == "lead_value"
    assert result["uncertainty"] == "thin_detail"
    assert result["veto"] == "rumor_unverified"
    assert result["is_flag_bearer"] is False
    assert "item_id=item-edge" in caplog.text
    assert "s1=5.5" in caplog.text
    assert "s2=4.5" in caplog.text


def test_enrich_highlight_score_v26_does_not_rerun_outside_edge_band(monkeypatch):
    calls = []
    monkeypatch.setattr(enrich_items, "load_prompt", lambda _filename: "system")

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(_score_result_at(8.0), ensure_ascii=False)

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call)

    result = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-outside", "title": "outside", "content": "outside content"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=True,
    )

    assert len(calls) == 1
    assert result["score10"] == 8.0
    assert result["runs"] == [8.0]


def test_enrich_highlight_score_v26_falls_back_when_pass2_fails(monkeypatch):
    responses = iter([
        json.dumps(_score_result_at(5.5), ensure_ascii=False),
        "not-json",
    ])
    written = []
    monkeypatch.setattr(enrich_items, "load_prompt", lambda _filename: "system")
    monkeypatch.setattr(enrich_items, "call_minimax", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        enrich_items,
        "write_highlight_score_v26_current",
        lambda item_id, result, threshold: written.append((item_id, result, threshold)),
    )

    result = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-pass2-fail", "title": "edge", "content": "edge content"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=False,
    )

    assert result["score10"] == 5.5
    assert result["runs"] == [5.5]
    assert "json_parse_error" in result["pass2_error"]
    assert written == [("item-pass2-fail", result, 5.5)]


def test_enrich_highlight_score_v26_stops_pass2_after_daily_cap(monkeypatch):
    calls = []
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHT_V26_PASS2_DAILY_CAP", "1")
    monkeypatch.setattr(
        enrich_items,
        "_HIGHLIGHT_V26_PASS2_USAGE",
        {"day": None, "count": 0},
        raising=False,
    )
    monkeypatch.setattr(enrich_items, "load_prompt", lambda _filename: "system")

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(_score_result_at(5.5), ensure_ascii=False)

    monkeypatch.setattr(enrich_items, "call_minimax", fake_call)

    first = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-cap-1", "title": "edge", "content": "edge content"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=True,
    )
    second = enrich_items.enrich_highlight_score_v26_for_item(
        {"id": "item-cap-2", "title": "edge", "content": "edge content"},
        "key",
        "base",
        "model",
        threshold=5.5,
        dry_run=True,
    )

    assert len(calls) == 3
    assert first["runs"] == [5.5, 5.5]
    assert second["runs"] == [5.5]


class _FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    def commit(self):
        self.calls.append(("commit", None))


@pytest.mark.parametrize(
    ("overrides", "score10", "is_flag_bearer", "expected_verdict"),
    [
        ({"reject": True}, None, False, "drop"),
        ({"veto": "marketing"}, 9.0, False, "drop"),
        ({}, 6.0, True, "featured"),
        ({}, 5.4, False, "borderline"),
    ],
)
def test_write_highlight_score_v26_remote_nests_scores_and_maps_verdict(
    monkeypatch,
    overrides,
    score10,
    is_flag_bearer,
    expected_verdict,
):
    monkeypatch.setattr(remote_db, "_maybe_jsonb", lambda value: value)
    monkeypatch.setattr(highlight_score_v26, "PROMPT_VERSION", "test-prompt-version")
    conn = _FakeConn()
    result = _score_result(**overrides)
    result.update(score10=score10, is_flag_bearer=is_flag_bearer)

    remote_db.write_highlight_score_v26_remote(
        conn,
        "item-1",
        result,
        threshold=5.5,
    )

    sql, params = conn.calls[0]
    nested_score = params[0]
    assert "COALESCE(highlight_scores, '{}'::jsonb)" in sql
    assert "jsonb_build_object('v26', %s::jsonb)" in sql
    assert "highlight_scores = %s" not in sql
    assert set(nested_score) == {
        "authority",
        "substance",
        "novelty",
        "timeliness",
        "audience_fit",
        "marketing",
        "score10",
        "content_type",
        "reject",
        "veto",
    }
    assert "importance" not in nested_score
    assert params[1] is is_flag_bearer
    assert params[2] == expected_verdict
    assert params[7] == highlight_score_v26.PROMPT_VERSION
    assert params[-1] == "item-1"
    assert conn.calls[-1] == ("commit", None)


def test_highlight_scorer_dispatch_defaults_to_v38(monkeypatch):
    calls = []
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHT_SCORER", raising=False)
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD", raising=False)
    monkeypatch.setattr(
        enrich_items,
        "enrich_highlight_verdict_for_item",
        lambda *args, **kwargs: calls.append(("v38", args, kwargs)),
    )
    monkeypatch.setattr(
        enrich_items,
        "enrich_highlight_score_v26_for_item",
        lambda *args, **kwargs: calls.append(("v26", args, kwargs)),
    )

    enrich_items.enrich_highlight_score_for_item(
        {"id": "item-1"}, "key", "base", "model", dry_run=False
    )

    assert [call[0] for call in calls] == ["v38"]


def test_highlight_scorer_dispatches_v26_with_threshold(monkeypatch):
    calls = []
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHT_SCORER", "v26")
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD", "5.5")
    monkeypatch.setattr(
        enrich_items,
        "enrich_highlight_verdict_for_item",
        lambda *args, **kwargs: calls.append(("v38", args, kwargs)),
    )
    monkeypatch.setattr(
        enrich_items,
        "enrich_highlight_score_v26_for_item",
        lambda *args, **kwargs: calls.append(("v26", args, kwargs)),
    )

    enrich_items.enrich_highlight_score_for_item(
        {"id": "item-1"}, "key", "base", "model", dry_run=False
    )

    assert [call[0] for call in calls] == ["v26"]
    assert calls[0][2]["threshold"] == 5.5


def test_highlight_scorer_config_reads_project_env_for_direct_cli(monkeypatch):
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHT_SCORER", raising=False)
    monkeypatch.delenv("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD", raising=False)
    monkeypatch.setattr(
        enrich_items,
        "load_project_env",
        lambda _base_dir: {
            "INFO2ACTION_HIGHLIGHT_SCORER": "v26",
            "INFO2ACTION_HIGHLIGHT_V26_THRESHOLD": "5.7",
        },
    )

    assert enrich_items.resolve_highlight_scorer_config() == ("v26", 5.7)


@pytest.mark.parametrize("threshold", [None, "", "not-a-number", "nan"])
def test_v26_threshold_missing_or_invalid_fails_fast(monkeypatch, threshold):
    monkeypatch.setenv("INFO2ACTION_HIGHLIGHT_SCORER", "v26")
    monkeypatch.setattr(enrich_items, "load_project_env", lambda _base_dir: {})
    if threshold is None:
        monkeypatch.delenv("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("INFO2ACTION_HIGHLIGHT_V26_THRESHOLD", threshold)

    with pytest.raises(ValueError, match="INFO2ACTION_HIGHLIGHT_V26_THRESHOLD"):
        enrich_items.resolve_highlight_scorer_config()
