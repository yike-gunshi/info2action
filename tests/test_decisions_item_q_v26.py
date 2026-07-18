from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


class FakeConn:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _quality_sql() -> str:
    conn = FakeConn()
    remote_db._sync_highlight_cluster_decisions(
        conn,
        "remote_poc",
        window_days=30,
        min_github_stars=50,
    )
    return _normalize_sql(conn.sql.split("decisions AS (", 1)[0].split("quality AS (", 1)[1])


def test_item_q_prefers_v26_score10_and_falls_back_to_legacy_dimensions():
    quality_sql = _quality_sql()

    expected = _normalize_sql(
        """COALESCE(
               (highlight_scores->'v26'->>'score10')::numeric / 10.0,
               (
                 (highlight_scores->>'importance')::numeric
               + (highlight_scores->>'substance')::numeric
               + (highlight_scores->>'novelty')::numeric
               ) / 9.0
             ) AS item_q"""
    )
    assert expected in quality_sql


def test_scored_members_accept_complete_legacy_or_non_null_v26_score():
    quality_sql = _quality_sql()

    expected = _normalize_sql(
        """WHERE highlight_include_in_highlights IS TRUE
             AND (
                   (
                     highlight_scores ? 'importance'
                     AND highlight_scores ? 'substance'
                     AND highlight_scores ? 'novelty'
                   )
                   OR (
                     highlight_scores ? 'v26'
                     AND highlight_scores->'v26'->>'score10' IS NOT NULL
                   )
                 )"""
    )
    assert expected in quality_sql
