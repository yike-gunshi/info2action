import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_parse_batch_response_matches_ids():
    import enrich_items

    raw = '[{"id":"a","summary":"A"},{"id":"b","summary":"B"}]'

    parsed = enrich_items.parse_batch_response(raw, expected_ids=["a", "b"])

    assert set(parsed) == {"a", "b"}
    assert parsed["a"]["summary"] == "A"


def test_batch_size_for_asr_item_is_one():
    import enrich_items

    item = {"id": "x", "asr_text": "long transcript"}

    assert enrich_items.batch_group_key(item) == "single"
