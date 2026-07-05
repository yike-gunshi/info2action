from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import highlight_score_calibration as calibration  # noqa: E402


def test_select_snapshot_rows_adds_duplicate_samples_with_same_key():
    clusters = [
        {
            "cluster_id": "c1",
            "category": "model",
            "source_count": 3,
            "last_updated_at": "2026-06-08T00:00:00Z",
        },
        {
            "cluster_id": "c2",
            "category": "tool",
            "source_count": 1,
            "last_updated_at": "2026-06-07T00:00:00Z",
        },
        {
            "cluster_id": "c3",
            "category": "tool",
            "source_count": 2,
            "last_updated_at": "2026-06-06T00:00:00Z",
        },
    ]

    selected = calibration.select_snapshot_rows(clusters, sample_limit=2, duplicate_count=1)

    assert len(selected) == 3
    assert selected[0]["sample_key"] == "c1"
    assert selected[-1]["sample_key"] == selected[0]["sample_key"]
    assert selected[-1]["sample_variant"] == "duplicate_1"


def test_build_cluster_payload_includes_cluster_not_percent_score():
    cluster = {
        "cluster_id": "c1",
        "title": "OpenAI 发布新模型",
        "summary": "官方发布了新的模型能力。",
        "source_count": 2,
        "platforms": ["rss", "twitter"],
        "sources": [
            {
                "title": "官方公告",
                "platform": "rss",
                "source": "OpenAI",
                "published_at": "2026-06-08T00:00:00Z",
                "url": "https://example.com",
                "summary": "官方说明",
            }
        ],
    }

    payload = calibration.build_cluster_payload(cluster)

    assert "cluster_id: c1" in payload
    assert "source_count: 2" in payload
    assert "官方公告" in payload
    assert "0-100" not in payload


def test_normalize_args_preserves_no_threshold_default():
    args = calibration.build_parser().parse_args(["analyze", "--classification-file", "/tmp/x.jsonl"])

    normalized = calibration.normalize_args(args)

    assert normalized.final_threshold is None
    assert normalized.duplicate_count == 20


def test_classify_offline_defaults_to_zero_temperature_for_calibration():
    args = calibration.build_parser().parse_args(["classify-offline"])

    normalized = calibration.normalize_args(args)

    assert normalized.temperature == 0.0


def test_build_review_rows_filters_duplicates_and_assigns_pairwise_groups():
    rows = [
        {
            "sample_key": "a",
            "sample_variant": "original",
            "cluster_id": "a",
            "title": "A",
            "category": "coding",
            "sources": [
                {
                    "id": "i1",
                    "title": "Source A",
                    "url": "https://example.com/a",
                    "source": "Example",
                    "ai_category": "products",
                    "ai_categories": ["coding", "models"],
                    "ai_summary": "source summary",
                }
            ],
            "content_type": "dynamic_news",
            "hard_gate": "pass",
            "bucket": "featured_candidate",
            "derived_score": 88,
            "confidence": 0.9,
            "reason_codes": ["official"],
            "dimension_scores": {"information_value": 5},
        },
        {
            "sample_key": "a",
            "sample_variant": "duplicate_1",
            "cluster_id": "a",
            "title": "A copy",
            "content_type": "dynamic_news",
            "derived_score": 40,
            "bucket": "manual_review",
        },
        {
            "sample_key": "b",
            "sample_variant": "original",
            "cluster_id": "b",
            "title": "B",
            "content_type": "dynamic_news",
            "hard_gate": "pass",
            "bucket": "candidate",
            "derived_score": 72,
            "confidence": 0.7,
            "dimension_scores": {"information_value": 4},
        },
    ]

    review_rows = calibration.build_review_rows(
        rows,
        pairwise_count=1,
        include_duplicates=False,
        app_base_url="https://remote.example",
    )

    assert len(review_rows) == 2
    assert {row["sample_key"] for row in review_rows} == {"a", "b"}
    assert {row["pairwise_group_id"] for row in review_rows} == {"dynamic_news_pair_1"}
    assert all(row["human_should_featured"] == "" for row in review_rows)
    assert json.loads(review_rows[0]["dimension_scores_json"])
    row_a = next(row for row in review_rows if row["sample_key"] == "a")
    assert row_a["cluster_url"] == "https://remote.example#cluster=a"
    assert json.loads(row_a["system_categories_json"]) == ["coding", "products", "models"]
    assert json.loads(row_a["sources_json"])[0]["url"] == "https://example.com/a"
    assert json.loads(row_a["human_categories_json"]) == []
    assert set(json.loads(row_a["human_dimension_feedback_json"]).values()) == {"unchecked"}
    assert row_a["human_evidence_feedback"] == "unchecked"
    assert row_a["human_score_feedback"] == "unchecked"
    assert json.loads(row_a["human_error_reasons_json"]) == []
    assert "suggested_should_featured" not in row_a
    assert "priority_reasons" not in row_a
    assert "pairwise_winner_sample_key" not in row_a


def test_write_review_csv_roundtrip(tmp_path):
    rows = calibration.build_review_rows(
        [
            {
                "sample_key": "a",
                "sample_variant": "original",
                "cluster_id": "a",
                "title": "A",
                "content_type": "product_tool",
                "derived_score": 70,
            }
        ],
        pairwise_count=0,
        include_duplicates=False,
    )
    out = tmp_path / "review.csv"

    calibration.write_review_csv(out, rows)

    with out.open("r", encoding="utf-8") as handle:
        loaded = list(csv.DictReader(handle))
    assert loaded[0]["sample_key"] == "a"
    assert "human_categories_json" in loaded[0]
    assert "human_dimension_feedback_json" in loaded[0]
    assert "human_evidence_feedback" in loaded[0]
    assert "human_score_feedback" in loaded[0]
    assert "human_error_reasons_json" in loaded[0]
    assert "human_error_reason" not in loaded[0]
    assert "priority_reasons" not in loaded[0]
    assert "suggested_should_featured" not in loaded[0]


def test_system_categories_collects_cluster_and_source_tags():
    row = {
        "category": "products",
        "ai_category": "products[legacy]",
        "ai_categories": ["coding", "other", "models"],
        "sources": [
            {"ai_category": "eval", "ai_categories": ["coding", "tutorials"]},
            {"ai_category": "other", "ai_categories": ["industry"]},
        ],
    }

    assert calibration.system_categories(row) == [
        "products",
        "coding",
        "models",
        "eval",
        "tutorials",
        "industry",
    ]


def test_load_review_category_options_uses_visible_classification_config():
    categories = calibration.load_review_category_options()
    ids = [category["id"] for category in categories]

    assert ids[:3] == ["products", "efficiency_tools", "coding"]
    assert "events" in ids
    assert "other" not in ids


def test_summarize_classification_rows_counts_distribution():
    summary = calibration.summarize_classification_rows(
        [
            {"content_type": "dynamic_news", "hard_gate": "pass", "bucket": "candidate", "derived_score": 60},
            {"content_type": "product_tool", "hard_gate": "reject", "bucket": "reject", "derived_score": 0},
            {"content_type": "dynamic_news", "hard_gate": "pass", "bucket": "featured_candidate", "derived_score": 90},
        ]
    )

    assert summary["content_type_counts"] == {"dynamic_news": 2, "product_tool": 1}
    assert summary["hard_gate_counts"] == {"pass": 2, "reject": 1}
    assert summary["score_summary"]["min"] == 0.0
    assert summary["score_summary"]["max"] == 90.0


def test_summarize_review_file_counts_human_labels(tmp_path):
    review_file = tmp_path / "review.csv"
    with review_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=calibration.REVIEW_FIELDS)
        writer.writeheader()
        writer.writerow({
            "sample_key": "a",
            "human_should_featured": "yes",
            "human_error_reasons_json": "[]",
            "pairwise_group_id": "dynamic_news_pair_1",
        })
        writer.writerow({
            "sample_key": "b",
            "human_should_featured": "no",
            "human_error_reasons_json": '["marketing_noise"]',
            "pairwise_group_id": "dynamic_news_pair_1",
        })

    summary = calibration.summarize_review_file(review_file)

    assert summary["present"] is True
    assert summary["labeled_count"] == 2
    assert summary["human_should_featured_counts"] == {"yes": 1, "no": 1}
    assert summary["human_error_reasons_counts"] == {"marketing_noise": 1}
    assert summary["pairwise_labeled_count"] == 0


def test_load_env_file_sets_missing_values_without_overriding(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("A_VALUE=from_file\nB_VALUE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("A_VALUE", "from_process")
    monkeypatch.delenv("B_VALUE", raising=False)

    loaded = calibration.load_env_file_into_process(str(env_file))

    assert loaded == {"A_VALUE": "existing", "B_VALUE": "loaded"}
    assert calibration.os.environ["A_VALUE"] == "from_process"
    assert calibration.os.environ["B_VALUE"] == "from_file"


def test_pending_rows_with_retry_errors_only_reruns_failed_rows():
    snapshot_rows = [
        {"sample_key": "a", "sample_variant": "original"},
        {"sample_key": "b", "sample_variant": "original"},
    ]
    existing_rows = [
        {"sample_key": "a", "sample_variant": "original", "error": "classify_error"},
        {"sample_key": "b", "sample_variant": "original", "derived_score": 70},
    ]

    pending, kept = calibration.pending_classification_rows(
        snapshot_rows,
        existing_rows,
        resume=True,
        retry_errors=True,
        offline_limit=0,
    )

    assert pending == [{"sample_key": "a", "sample_variant": "original"}]
    assert kept == [{"sample_key": "b", "sample_variant": "original", "derived_score": 70}]


def test_rederive_classification_rows_preserves_previous_score():
    rows = [
        {
            "sample_key": "a",
            "sample_variant": "original",
            "derived_score": 99,
            "raw_result": {
                "hard_gate": "pass",
                "content_type": "dynamic_news",
                "content_type_confidence": 0.9,
                "dimension_scores": {
                    "information_value": 5,
                    "usefulness": 5,
                    "timeliness": 5,
                    "authority_trust": 2,
                    "content_depth": 5,
                    "domain_fit": 5,
                    "cluster_incremental_value": 5,
                },
                "marketing_noise": 1,
                "bucket": "featured_candidate",
            },
        }
    ]

    rederived = calibration.rederive_classification_rows(rows)

    assert rederived[0]["previous_derived_score"] == 99
    assert rederived[0]["derived_score"] == 64.0
    assert rederived[0]["diagnostic_band"] == "score_50_64"


def test_calibrate_thresholds_recommends_global_threshold_from_labels():
    classified_rows = [
        {"sample_key": "a", "sample_variant": "original", "derived_score": 90, "content_type": "dynamic_news"},
        {"sample_key": "b", "sample_variant": "original", "derived_score": 72, "content_type": "dynamic_news"},
        {"sample_key": "c", "sample_variant": "original", "derived_score": 60, "content_type": "dynamic_news"},
        {"sample_key": "d", "sample_variant": "original", "derived_score": 70, "content_type": "product_tool"},
        {"sample_key": "e", "sample_variant": "original", "derived_score": 45, "content_type": "product_tool"},
        {"sample_key": "f", "sample_variant": "original", "derived_score": 30, "content_type": "product_tool"},
    ]
    review_rows = [
        {"sample_key": "a", "human_should_featured": "yes"},
        {"sample_key": "b", "human_should_featured": "yes"},
        {"sample_key": "c", "human_should_featured": "no"},
        {"sample_key": "d", "human_should_featured": "yes"},
        {"sample_key": "e", "human_should_featured": "no"},
        {"sample_key": "f", "human_should_featured": "no"},
    ]

    calibration_result = calibration.calibrate_thresholds(
        classified_rows,
        review_rows,
        min_labeled=4,
        min_positive=2,
        min_negative=2,
        min_type_labeled=3,
    )

    assert calibration_result["ready_for_threshold_decision"] is True
    assert calibration_result["global"]["threshold"] == 61
    assert calibration_result["global"]["false_positive_count"] == 0
    assert calibration_result["global"]["false_negative_count"] == 0
    assert calibration_result["type_thresholds"]["dynamic_news"]["threshold"] == 61
    assert calibration_result["type_thresholds"]["product_tool"]["threshold"] == 46


def test_calibrate_thresholds_reports_missing_human_labels():
    calibration_result = calibration.calibrate_thresholds(
        [{"sample_key": "a", "sample_variant": "original", "derived_score": 90}],
        [{"sample_key": "a", "human_should_featured": ""}],
        min_labeled=1,
    )

    assert calibration_result["ready_for_threshold_decision"] is False
    assert calibration_result["blocking_reasons"] == ["not_enough_labeled_rows", "not_enough_positive_labels", "not_enough_negative_labels"]


def test_pairwise_calibration_counts_score_agreement():
    classified_rows = [
        {"sample_key": "a", "sample_variant": "original", "derived_score": 90},
        {"sample_key": "b", "sample_variant": "original", "derived_score": 70},
    ]
    review_rows = [
        {"sample_key": "a", "pairwise_group_id": "g1", "pairwise_winner_sample_key": "a"},
        {"sample_key": "b", "pairwise_group_id": "g1", "pairwise_winner_sample_key": ""},
    ]

    summary = calibration.calibrate_pairwise_preferences(classified_rows, review_rows)

    assert summary["pairwise_group_count"] == 1
    assert summary["score_agrees_with_winner_count"] == 1
    assert summary["score_agreement_rate"] == 1.0


def test_apply_review_updates_writes_labels_and_notes(tmp_path):
    review_file = tmp_path / "review.csv"
    rows = calibration.build_review_rows(
        [
            {
                "sample_key": "a",
                "sample_variant": "original",
                "cluster_id": "a",
                "title": "A",
                "content_type": "dynamic_news",
                "derived_score": 80,
            }
        ],
        pairwise_count=0,
        include_duplicates=False,
    )
    calibration.write_review_csv(review_file, rows)

    result = calibration.apply_review_updates(
        review_file,
        [
            {
                "sample_key": "a",
                "human_should_featured": "yes",
                "human_categories_json": ["products", "products", "", "coding"],
                "human_dimension_feedback_json": {
                    "information_value": "high",
                    "usefulness": "bad",
                    "marketing_noise": "low",
                },
                "human_evidence_feedback": "wrong",
                "human_score_feedback": "high",
                "human_error_reasons_json": ["marketing_noise", "unknown", "score_wrong", "score_wrong"],
                "human_notes": "keep",
            }
        ],
    )

    loaded = list(csv.DictReader(review_file.open(encoding="utf-8")))
    assert result == {"updated": 1, "missing": []}
    assert loaded[0]["human_should_featured"] == "yes"
    assert json.loads(loaded[0]["human_categories_json"]) == ["products", "coding"]
    dimension_feedback = json.loads(loaded[0]["human_dimension_feedback_json"])
    assert dimension_feedback["information_value"] == "high"
    assert dimension_feedback["usefulness"] == "unchecked"
    assert dimension_feedback["marketing_noise"] == "low"
    assert loaded[0]["human_evidence_feedback"] == "wrong"
    assert loaded[0]["human_score_feedback"] == "high"
    assert json.loads(loaded[0]["human_error_reasons_json"]) == ["marketing_noise", "score_wrong"]
    assert loaded[0]["human_notes"] == "keep"


def test_apply_review_updates_reports_missing_sample(tmp_path):
    review_file = tmp_path / "review.csv"
    calibration.write_review_csv(review_file, [])

    result = calibration.apply_review_updates(
        review_file,
        [{"sample_key": "missing", "human_should_featured": "no"}],
    )

    assert result == {"updated": 0, "missing": ["missing"]}
