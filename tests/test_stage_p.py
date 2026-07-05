import json

from clustering import stage_p


def _raw_payload(*, kept_ids, removed=None):
    return json.dumps({
        "cluster_l1": "products",
        "cluster_l2": ["ai_image"],
        "event_summary": "GPT Image 2 usage and examples",
        "event_certainty": "high",
        "removed": removed or [],
        "kept_ids": kept_ids,
    }, ensure_ascii=False)


def _raw_other_payload():
    return json.dumps({
        "cluster_l1": "other",
        "cluster_l2": ["other"],
        "event_summary": "assorted low-signal content",
        "event_certainty": "low",
        "removed": [],
        "kept_ids": ["a"],
    }, ensure_ascii=False)


def test_parse_response_repairs_unambiguous_numeric_id_typo():
    expected = {
        "2049317447556378717",
        "2049308981554987356",
    }
    raw = _raw_payload(
        kept_ids=["2049317447556371717"],
        removed=[{
            "id": "2049308981554987356",
            "reason": "marketing post",
        }],
    )

    parsed = stage_p.parse_response(raw, expected)

    assert parsed["kept_ids"] == ["2049317447556378717"]
    assert parsed["removed"][0]["id"] == "2049308981554987356"


def test_parse_response_repairs_unambiguous_prefix_id_typo():
    expected = {
        "lw_69ec6776c31052094870279e",
        "reddit_1syt37w",
    }
    raw = _raw_payload(
        kept_ids=["lwin_69ec6776c31052094870279e", "reddit_1syt37w"],
    )

    parsed = stage_p.parse_response(raw, expected)

    assert parsed["kept_ids"] == [
        "lw_69ec6776c31052094870279e",
        "reddit_1syt37w",
    ]


def test_parse_response_prefers_removed_when_id_also_kept():
    raw = _raw_payload(
        kept_ids=["keep_me", "dup_id"],
        removed=[{
            "id": "dup_id",
            "reason": "different event",
        }],
    )

    parsed = stage_p.parse_response(raw, {"keep_me", "dup_id"})

    assert parsed["kept_ids"] == ["keep_me"]
    assert parsed["removed"] == [{
        "id": "dup_id",
        "reason": "different event",
    }]


def test_parse_response_allows_other_without_taxonomy_l2():
    parsed = stage_p.parse_response(_raw_other_payload(), {"a"})

    assert parsed["cluster_l1"] == "other"
    assert parsed["cluster_l2"] == []


def test_parse_response_accepts_eval_l1():
    raw = json.dumps({
        "cluster_l1": "eval",
        "cluster_l2": ["coding_eval", "eval_benchmarks"],
        "event_summary": "SWE Atlas 发布并评测 AI 编程真实工程能力",
        "event_certainty": "high",
        "removed": [],
        "kept_ids": ["a", "b"],
    }, ensure_ascii=False)

    parsed = stage_p.parse_response(raw, {"a", "b"})

    assert parsed["cluster_l1"] == "eval"
    assert parsed["cluster_l2"] == ["coding_eval", "eval_benchmarks"]


def test_parse_response_keeps_ids_that_model_omits_without_extra_ids():
    raw = _raw_payload(kept_ids=["a"])

    parsed = stage_p.parse_response(raw, {"a", "b"})

    assert parsed["kept_ids"] == ["a", "b"]


def test_parse_response_falls_invalid_l2_back_to_other_when_available():
    raw = json.dumps({
        "cluster_l1": "products",
        "cluster_l2": ["coding_tool"],
        "event_summary": "Claude creative tool launch",
        "event_certainty": "high",
        "removed": [],
        "kept_ids": ["a"],
    }, ensure_ascii=False)

    parsed = stage_p.parse_response(raw, {"a"})

    assert parsed["cluster_l2"] == ["other"]
