from pathlib import Path


def test_reduce_remote_db_pressure_migration_disables_legacy_mv_cron():
    sql = Path("supabase/migrations/0027_reduce_remote_db_pressure.sql").read_text()

    assert "cron.unschedule('refresh-mv-items-top-per-platform')" in sql


def test_reduce_remote_db_pressure_migration_adds_fetch_run_items_lookup_index():
    sql = Path("supabase/migrations/0027_reduce_remote_db_pressure.sql").read_text()

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_fetch_run_items_run_inserted_item_idx" in sql
    assert "ON remote_poc.fetch_run_items(run_id, was_inserted, item_id)" in sql


def test_reduce_remote_db_pressure_migration_adds_publish_partial_index():
    sql = Path("supabase/migrations/0027_reduce_remote_db_pressure.sql").read_text()

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_clusters_touched_publish_pending_idx" in sql
    assert "ON remote_poc.clusters(last_touched_run_id, id)" in sql
    assert "WHERE last_touched_run_id IS NOT NULL" in sql
    assert "ai_title_draft IS NOT NULL" in sql
    assert "pending_is_visible_in_feed IS NOT NULL" in sql
