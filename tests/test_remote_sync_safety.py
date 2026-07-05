from __future__ import annotations

import json
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "scripts" / "sync_sqlite_to_supabase_poc.py"
SUPABASE_MIRROR_SCRIPT = ROOT / "scripts" / "sync_supabase_to_supabase.py"
VERIFY_STAGING_SCRIPT = ROOT / "scripts" / "verify_remote_only_staging.py"
sys.path.insert(0, str(ROOT / "scripts"))


def _make_minimal_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE items (
              id TEXT PRIMARY KEY,
              fetch_run_id INTEGER,
              fetched_at TEXT,
              content TEXT,
              detail_json TEXT,
              comments_json TEXT,
              embedding BLOB
            );
            CREATE TABLE clusters (
              id INTEGER PRIMARY KEY,
              is_visible_in_feed INTEGER,
              published_at TEXT,
              first_doc_at TEXT,
              last_updated_at TEXT,
              created_at TEXT,
              representative_vector BLOB
            );
            CREATE TABLE cluster_items (
              cluster_id INTEGER,
              item_id TEXT,
              rank_in_cluster INTEGER
            );
            CREATE TABLE item_status (
              user_id TEXT,
              item_id TEXT
            );
            CREATE TABLE cluster_status (
              user_id TEXT,
              cluster_id INTEGER
            );
            CREATE TABLE fetch_runs (
              id INTEGER PRIMARY KEY
            );
            CREATE TABLE cluster_judge_log (
              id INTEGER PRIMARY KEY,
              item_id TEXT
            );
            INSERT INTO items (
              id, fetch_run_id, fetched_at, content, detail_json, comments_json, embedding
            ) VALUES (
              'i1', 1, '2026-05-12T00:00:00Z',
              'heavy raw content', '{"raw": true}', '{"comments": []}', zeroblob(6144)
            );
            INSERT INTO clusters (
              id, is_visible_in_feed, published_at, first_doc_at, last_updated_at, created_at,
              representative_vector
            ) VALUES (
              1, 1, '2026-05-12T00:00:00Z', '2026-05-12T00:00:00Z',
              '2026-05-12T00:00:00Z', '2026-05-12T00:00:00Z', zeroblob(6144)
            );
            INSERT INTO cluster_items (cluster_id, item_id, rank_in_cluster) VALUES (1, 'i1', 1);
            INSERT INTO fetch_runs (id) VALUES (1);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_full_sync_requires_explicit_confirmation(tmp_path):
    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)

    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--db", str(db_path), "--all"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--confirm-full-sync" in result.stderr


def test_dry_run_reports_full_sync_scope_without_supabase(tmp_path):
    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)

    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--db", str(db_path), "--all", "--dry-run"],
        text=True,
        capture_output=True,
        check=True,
    )

    body = json.loads(result.stdout)
    assert body["dry_run"] is True
    assert body["local"]["mode"] == "all"
    assert body["local"]["tables"]["items"] == 1
    assert body["local"]["vectors"]["bad_item_embedding_dimensions"] == 0
    assert body["local"]["referential_checks"]["cluster_items_missing_item"] == 0
    assert body["local"]["estimated_payload_bytes"] > 0
    assert body["local"]["estimated_payload_mib"] > 0


def test_supabase_mirror_refuses_non_staging_target(tmp_path):
    source_env = tmp_path / "source.env"
    target_env = tmp_path / "target.env"
    source_env.write_text("SUPABASE_DB_URL=postgresql://user:pass@example.com:5432/source\n")
    target_env.write_text(
        "SUPABASE_REMOTE_DB_ENV=dev\n"
        "SUPABASE_DB_URL=postgresql://user:pass@example.com:5432/target\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SUPABASE_MIRROR_SCRIPT),
            "--source-env",
            str(source_env),
            "--target-env",
            str(target_env),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "SUPABASE_REMOTE_DB_ENV=staging" in result.stderr


def test_supabase_mirror_refuses_same_source_and_target(tmp_path):
    source_env = tmp_path / "source.env"
    target_env = tmp_path / "target.env"
    same_url = "postgresql://user:pass@example.com:5432/postgres"
    source_env.write_text(f"SUPABASE_DB_URL={same_url}\n")
    target_env.write_text(f"SUPABASE_REMOTE_DB_ENV=staging\nSUPABASE_DB_URL={same_url}\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SUPABASE_MIRROR_SCRIPT),
            "--source-env",
            str(source_env),
            "--target-env",
            str(target_env),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "source and target database URLs are identical" in result.stderr


def test_verify_staging_refuses_non_staging_env_before_network(tmp_path):
    env_file = tmp_path / "production.env"
    env_file.write_text(
        "SUPABASE_REMOTE_DB_ENV=production\n"
        "SUPABASE_DB_URL=postgresql://user:pass@example.com:5432/postgres\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY_STAGING_SCRIPT),
            "--env-file",
            str(env_file),
            "--skip-write-probe",
            "--skip-storage-probe",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "SUPABASE_REMOTE_DB_ENV=staging" in result.stderr


def test_full_sync_plan_estimates_payload_bytes(tmp_path):
    from sync_sqlite_to_supabase_poc import local_sync_plan, sqlite_connect

    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)
    conn = sqlite_connect(db_path)
    try:
        plan = local_sync_plan(conn, db_path, full=True)
    finally:
        conn.close()

    assert plan["mode"] == "all"
    assert plan["estimated_payload_bytes"] >= 6144 * 2
    assert plan["estimated_payload_mib"] > 0


def test_capacity_check_prefers_estimated_payload_bytes():
    from preflight_supabase_remote_poc import capacity_check

    report = {
        "local": {
            "db_size_bytes": 1_000,
            "estimated_payload_bytes": 10,
        },
        "remote": {"db_size_bytes": 5},
    }

    capacity = capacity_check(report, max_db_mib=None)

    assert capacity["local_bytes_basis"] == "estimated_payload_bytes"
    assert capacity["rough_after_sync_bytes"] == 15


def test_sync_capacity_budget_prefers_estimated_payload_bytes():
    from sync_sqlite_to_supabase_poc import sync_capacity_budget

    budget = sync_capacity_budget(
        {"db_size_bytes": 1_000, "estimated_payload_bytes": 10},
        remote_db_size_bytes=5,
        max_db_mib=None,
        headroom_mib=0,
    )

    assert budget["local_bytes_basis"] == "estimated_payload_bytes"
    assert budget["rough_after_sync_bytes"] == 15
    assert budget["would_exceed_max"] is None


def test_sync_capacity_budget_rejects_when_estimate_exceeds_max():
    from sync_sqlite_to_supabase_poc import assert_capacity_budget

    with pytest.raises(SystemExit, match="capacity check failed"):
        assert_capacity_budget(
            {"estimated_payload_bytes": 20},
            remote_db_size_bytes=90,
            max_db_mib=100 / 1024 / 1024,
            headroom_mib=0,
        )


def test_all_items_select_sql_supports_deterministic_resume_offset():
    from sync_sqlite_to_supabase_poc import all_items_select_sql

    assert all_items_select_sql(0) == "SELECT * FROM items ORDER BY fetched_at DESC, id DESC"
    assert all_items_select_sql(1500).endswith("LIMIT -1 OFFSET 1500")


def test_all_item_ids_reads_ordered_ids_without_selecting_blob_rows(tmp_path):
    from sync_sqlite_to_supabase_poc import all_item_ids

    db_path = tmp_path / "feed.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE items (
              id TEXT PRIMARY KEY,
              fetched_at TEXT,
              embedding BLOB
            );
            INSERT INTO items (id, fetched_at, embedding) VALUES
              ('old', '2026-05-10T00:00:00Z', zeroblob(6144)),
              ('new-a', '2026-05-12T00:00:00Z', zeroblob(6144)),
              ('new-b', '2026-05-12T00:00:00Z', zeroblob(6144));
            """
        )
        assert all_item_ids(conn, items_offset=0) == ["new-b", "new-a", "old"]
        assert all_item_ids(conn, items_offset=2) == ["old"]
        assert all_item_ids(conn, items_offset=1, items_limit=1) == ["new-a"]
    finally:
        conn.close()


def test_slim_item_payload_strips_heavy_fields_but_keeps_embedding():
    from sync_sqlite_to_supabase_poc import item_payload

    payload = item_payload(
        {
            "id": "i1",
            "user_id": None,
            "platform": "rss",
            "source": "feed",
            "fetch_run_id": 1,
            "title": "Title",
            "content": "heavy raw content",
            "author_name": None,
            "author_id": None,
            "author_avatar": None,
            "url": "https://example.com",
            "cover_url": None,
            "description": "short description",
            "media_json": None,
            "metrics_json": None,
            "tags_json": None,
            "lang": "zh",
            "detail_json": '{"raw": true}',
            "comments_json": '{"comments": []}',
            "ai_summary": "summary",
            "ai_key_points": "points",
            "ai_category": "models",
            "ai_keywords": "keyword",
            "ai_categories": '["models"]',
            "ai_subcategories": None,
            "multi_l1_reason": None,
            "ai_extracted": None,
            "content_type": "news",
            "ai_quality_score": 0.9,
            "visible": 1,
            "relevance_score": 0.8,
            "embedding": b"\x00" * 6144,
            "embedding_provider": "minimax",
            "embedding_model": "embo-01",
            "embedding_input_variant": "default",
            "embedding_generated_at": "2026-05-12T00:00:00Z",
            "canonical_url": "https://example.com",
            "cluster_id": 1,
            "fetched_at": "2026-05-12T00:00:00Z",
            "published_at": "2026-05-12T00:00:00Z",
            "created_at": "2026-05-12T00:00:00Z",
        },
        slim=True,
    )

    assert payload["content"] is None
    assert payload["detail_json"] is None
    assert payload["comments_json"] is None
    assert payload["description"] == "short description"
    assert payload["embedding"].startswith("[")


def test_item_payload_normalizes_epoch_timestamp_strings():
    from sync_sqlite_to_supabase_poc import item_payload

    payload = item_payload(
        {
            "id": "i1",
            "platform": "rss",
            "source": "feed",
            "embedding": None,
            "published_at": "1772093328",
            "fetched_at": "2026-05-12T00:00:00Z",
        },
        slim=True,
    )

    assert payload["published_at"] == "2026-02-26T08:08:48+00:00"


def test_jsonb_strips_nul_escape_before_postgres_jsonb():
    from sync_sqlite_to_supabase_poc import jsonb

    payload = jsonb('{"text": "before\\u0000after", "nested": ["x\\u0000y"]}')

    assert "\\u0000" not in payload
    assert json.loads(payload) == {"text": "beforeafter", "nested": ["xy"]}


def test_item_payload_strips_nul_characters_from_text_and_json():
    from sync_sqlite_to_supabase_poc import item_payload

    payload = item_payload(
        {
            "id": "i1",
            "platform": "twitter",
            "source": "following",
            "title": "hello\x00world",
            "content": "body\x00text",
            "detail_json": '{"text": "before\\u0000after"}',
            "comments_json": None,
            "embedding": None,
            "fetched_at": "2026-05-12T00:00:00Z",
        },
        slim=False,
    )

    assert payload["title"] == "helloworld"
    assert payload["content"] == "bodytext"
    assert "\\u0000" not in payload["detail_json"]
    assert json.loads(payload["detail_json"]) == {"text": "beforeafter"}


def test_item_upsert_keeps_existing_heavy_fields_when_slim_payload_is_null():
    from sync_sqlite_to_supabase_poc import upsert_items

    source = inspect.getsource(upsert_items)
    assert "content = COALESCE(excluded.content, items.content)" in source
    assert "detail_json = COALESCE(excluded.detail_json, items.detail_json)" in source
    assert "comments_json = COALESCE(excluded.comments_json, items.comments_json)" in source


def test_item_upsert_does_not_rewrite_existing_embeddings_on_full_resume():
    from sync_sqlite_to_supabase_poc import upsert_items

    source = inspect.getsource(upsert_items)
    assert "embedding = COALESCE(items.embedding, excluded.embedding)" in source


def test_remote_item_upsert_only_advances_fetched_at_on_card_relevant_change():
    import remote_db

    sql = remote_db._item_upsert_sql("remote_poc")
    refresh_condition = remote_db._item_upsert_read_model_refresh_condition()

    assert "excluded.fetch_run_id IS NOT NULL" in sql
    assert "IS DISTINCT FROM target.metrics_json" in refresh_condition
    assert "IS DISTINCT FROM target.detail_json" in refresh_condition
    assert "IS DISTINCT FROM target.cover_url" in refresh_condition
    assert "IS DISTINCT FROM target.asr_text_cn" not in refresh_condition
    assert "THEN excluded.fetched_at" in sql


def test_remote_item_upsert_skips_noop_conflict_updates():
    import remote_db

    sql = " ".join(remote_db._item_upsert_sql("remote_poc").split())

    assert "DO UPDATE SET" in sql
    assert "WHERE (" in sql
    assert "COALESCE(excluded.asr_text_cn, target.asr_text_cn) IS DISTINCT FROM target.asr_text_cn" in sql
    assert "COALESCE(excluded.ai_key_points, target.ai_key_points) IS DISTINCT FROM target.ai_key_points" in sql
    assert "COALESCE(NULLIF(excluded.source, ''), target.source) IS DISTINCT FROM target.source" in sql
    assert "OR COALESCE(excluded.fetch_run_id, target.fetch_run_id) IS DISTINCT FROM target.fetch_run_id" not in sql


def test_remote_fetch_run_item_upsert_skips_noop_conflict_updates():
    import remote_db

    sql = " ".join(remote_db._fetch_run_item_upsert_sql("remote_poc").split())

    assert "recorded_at = now()" not in sql
    assert "ON CONFLICT (run_id, item_id) DO UPDATE SET" in sql
    assert "WHERE remote_poc.fetch_run_items.platform IS DISTINCT FROM excluded.platform" in sql
    assert "remote_poc.fetch_run_items.was_inserted IS DISTINCT FROM CASE" in sql
    assert "remote_poc.fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1" in sql


def test_info_card_items_upsert_skips_noop_updates():
    import remote_db

    source = inspect.getsource(remote_db.refresh_info_read_model_delta_in_place)

    assert "ON CONFLICT (version_id, item_id) DO UPDATE SET" in source
    assert "info_card_items.card_json IS DISTINCT FROM excluded.card_json" in source
    assert "info_card_items.relevance_score IS DISTINCT FROM excluded.relevance_score" in source


def test_info_scope_items_delta_refresh_does_not_replace_whole_scopes():
    import remote_db

    source = inspect.getsource(remote_db.refresh_info_read_model_delta_in_place)

    assert "CREATE TEMP TABLE info_read_model_existing_delta_scope_rows" in source
    assert "delete_obsolete_scope_items" in source
    assert "update_existing_scope_items" in source
    assert "insert_missing_scope_items" in source
    assert "scope_max_rank" in source
    assert "DELETE FROM {schema}.info_scope_items si\n                      USING pg_temp.info_read_model_affected_scopes" not in source
    assert "CREATE TEMP TABLE info_read_model_affected_scope_rows" not in source


def test_info_scope_items_reads_order_by_sort_keys():
    import remote_db

    source = inspect.getsource(remote_db._info_scope_item_order_sql)

    assert "sort_at DESC NULLS LAST" in source
    assert "fetched_at DESC NULLS LAST" in source
    assert "relevance_score DESC NULLS LAST" in source
    assert "item_id DESC" in source


def test_item_payload_can_skip_existing_embedding_conversion():
    from sync_sqlite_to_supabase_poc import item_payload

    payload = item_payload(
        {
            "id": "i1",
            "platform": "twitter",
            "source": "following",
            "embedding": b"not-a-1536-vector",
        },
        skip_embedding=True,
    )

    assert payload["embedding"] is None


def test_non_1536_vectors_are_preserved_by_complete_sync_side_tables():
    from sync_sqlite_to_supabase_complete import (
        is_doubao_embedding,
        vector_dim,
        vector_literal as any_dim_vector_literal,
    )
    from sync_sqlite_to_supabase_poc import vector_literal

    blob_2048 = b"\x00" * (2048 * 4)

    assert vector_literal(blob_2048) is None
    assert vector_dim(blob_2048) == 2048
    assert is_doubao_embedding("doubao-embedding-text", None, blob_2048) is True
    assert any_dim_vector_literal(blob_2048, 2048).startswith("[")
    assert any_dim_vector_literal(blob_2048, 1536) is None


def test_cluster_vector_upsert_does_not_null_existing_vectors_for_unsupported_dims():
    from sync_sqlite_to_supabase_poc import bulk_merge_sql, upsert_clusters

    bulk_sql = bulk_merge_sql("remote_poc", "clusters", "stage_clusters")
    upsert_source = inspect.getsource(upsert_clusters)

    assert "representative_vector = COALESCE(excluded.representative_vector, target.representative_vector)" in bulk_sql
    assert "event_embedding = COALESCE(excluded.event_embedding, target.event_embedding)" in bulk_sql
    assert "representative_vector = COALESCE(excluded.representative_vector, clusters.representative_vector)" in upsert_source
    assert "event_embedding = COALESCE(excluded.event_embedding, clusters.event_embedding)" in upsert_source


def test_bulk_item_merge_preserves_existing_embeddings_and_restores_heavy_fields():
    from sync_sqlite_to_supabase_poc import bulk_merge_sql

    sql = bulk_merge_sql("remote_poc", "items", "stage_items")

    assert "insert into remote_poc.items as target" in sql
    assert "from stage_items" in sql
    assert "content = COALESCE(excluded.content, target.content)" in sql
    assert "detail_json = COALESCE(excluded.detail_json, target.detail_json)" in sql
    assert "comments_json = COALESCE(excluded.comments_json, target.comments_json)" in sql
    assert "embedding = COALESCE(target.embedding, excluded.embedding)" in sql


def test_bulk_copy_command_quotes_columns_for_copy():
    from sync_sqlite_to_supabase_poc import ITEM_COLUMNS, copy_command_sql

    sql = copy_command_sql("stage_items", ITEM_COLUMNS[:3])

    assert sql == 'COPY stage_items ("id", "user_id", "platform") FROM STDIN'


def test_bulk_copy_row_values_use_existing_payload_cleaning():
    from sync_sqlite_to_supabase_poc import ITEM_COLUMNS, copy_row_values, item_payload

    values = copy_row_values(
        {
            "id": "i1",
            "platform": "twitter",
            "source": "following",
            "title": "hello\x00world",
            "content": "body\x00text",
            "detail_json": '{"text": "before\\u0000after"}',
            "embedding": None,
        },
        columns=ITEM_COLUMNS,
        payload_fn=item_payload,
        slim=False,
    )

    by_column = dict(zip(ITEM_COLUMNS, values))
    assert by_column["title"] == "helloworld"
    assert by_column["content"] == "bodytext"
    assert "\\u0000" not in by_column["detail_json"]


def test_bulk_all_items_queries_remote_embedding_presence_before_copying():
    from sync_sqlite_to_supabase_poc import bulk_copy_all_items_by_id_batches

    source = inspect.getsource(bulk_copy_all_items_by_id_batches)
    assert "remote_item_ids_with_embedding" in source


def test_full_sync_status_queries_skip_orphan_rows_before_remote_fk_insert():
    from sync_sqlite_to_supabase_poc import all_table_select_sql

    assert "join items i on i.id = s.item_id" in all_table_select_sql("item_status")
    assert "join clusters c on c.id = s.cluster_id" in all_table_select_sql("cluster_status")


def test_slim_dry_run_reports_selected_scope_without_supabase(tmp_path):
    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--db",
            str(db_path),
            "--slim",
            "--slim-days",
            "3650",
            "--slim-cluster-days",
            "3650",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    body = json.loads(result.stdout)
    assert body["dry_run"] is True
    assert body["local"]["mode"] == "slim"
    assert body["local"]["slim"]["strip_heavy_fields"] is True
    assert body["local"]["tables"]["items"] == 1
    assert body["local"]["tables"]["clusters"] == 1


def test_incremental_sync_requires_explicit_confirmation(tmp_path):
    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)

    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--db", str(db_path), "--incremental"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--confirm-incremental-sync" in result.stderr


def test_incremental_dry_run_reports_selected_scope_without_supabase(tmp_path):
    db_path = tmp_path / "feed.db"
    _make_minimal_db(db_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SYNC_SCRIPT),
            "--db",
            str(db_path),
            "--incremental",
            "--incremental-hours",
            "100000",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    body = json.loads(result.stdout)
    assert body["dry_run"] is True
    assert body["local"]["mode"] == "incremental"
    assert body["local"]["tables"]["items"] == 1
    assert body["local"]["tables"]["clusters"] == 1
    assert body["local"]["incremental"]["hours"] == 100000
    assert body["local"]["incremental"]["strip_heavy_fields"] is False


def test_remote_sync_after_pipeline_wrapper_is_opt_in_and_bulk_incremental():
    wrapper = ROOT / "ops" / "remote_sync_after_pipeline.sh"

    text = wrapper.read_text()

    assert "INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE" in text
    assert "--incremental" in text
    assert "--confirm-incremental-sync" in text
    assert "--bulk-copy" in text
    assert "not enabled" in text


def test_hourly_pipeline_invokes_remote_sync_after_pipeline_wrapper():
    text = (ROOT / "ops" / "cron_hourly_pipeline_light.sh").read_text()

    assert "remote_sync_after_pipeline.sh" in text


def test_remote_only_full_schema_migration_covers_production_tables():
    migration = ROOT / "supabase" / "migrations" / "0002_remote_only_full_schema.sql"
    text = migration.read_text()

    required_tables = [
        "actions",
        "action_logs",
        "action_feedback",
        "feedback",
        "briefings",
        "interests",
        "interest_matches",
        "health_log",
        "users",
        "invite_codes",
        "sessions",
        "user_profiles",
        "asr_usage",
        "settings",
        "search_keywords",
        "clusters_v2",
        "cluster_items_v2",
        "cluster_p_log",
    ]
    for table in required_tables:
        assert f"remote_poc.{table}" in text

    required_item_columns = [
        "ai_error_count",
        "ai_last_error",
        "ai_dimensions",
        "asr_text",
        "asr_segments",
        "cluster_locked",
        "stage_a_state",
        "visible",
    ]
    for column in required_item_columns:
        assert column in text
