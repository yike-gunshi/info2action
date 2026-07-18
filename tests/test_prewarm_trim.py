from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import remote_db


def test_info_read_model_page_prewarm_env_defaults_override_and_clamp(monkeypatch):
    monkeypatch.delenv(remote_db.INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV, raising=False)
    monkeypatch.delenv(remote_db.INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV, raising=False)
    assert remote_db._info_read_model_prewarm_page_limit() == 20
    assert remote_db._info_read_model_prewarm_pages_per_scope() == 1

    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV, "30")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV, "3")
    assert remote_db._info_read_model_prewarm_page_limit() == 30
    assert remote_db._info_read_model_prewarm_pages_per_scope() == 3

    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV, "0")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV, "0")
    assert remote_db._info_read_model_prewarm_page_limit() == 1
    assert remote_db._info_read_model_prewarm_pages_per_scope() == 1

    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV, "999")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV, "999")
    assert remote_db._info_read_model_prewarm_page_limit() == 200
    assert remote_db._info_read_model_prewarm_pages_per_scope() == 5


def test_prewarm_platforms_passes_trimmed_page_prewarm_env(monkeypatch):
    page_prewarm_calls = []

    monkeypatch.setenv(remote_db.INFO_READ_MODEL_ENV, "1")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_REFRESH_ENV, "0")
    monkeypatch.setenv(remote_db.HIGHLIGHTS_READ_MODEL_REFRESH_ENV, "0")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_SCOPES_ENV, "4")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV, "25")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV, "2")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "query_feed_sections",
        lambda **_kwargs: {"sections": {}, "cat_counts": {}},
    )
    monkeypatch.setattr(
        remote_db,
        "query_feed_platforms",
        lambda **_kwargs: {"sections": {}, "platform_counts": {}, "source_counts": {}},
    )
    monkeypatch.setattr(
        remote_db,
        "prewarm_info_read_model_pages",
        lambda **kwargs: page_prewarm_calls.append(kwargs) or {"ok": True, "pages": 4, "items": 100},
    )

    result = remote_db.prewarm_platforms()

    assert page_prewarm_calls == [
        {"max_scopes": 4, "page_limit": 25, "pages_per_scope": 2}
    ]
    assert result["read_model_page_prewarm_ok"] is True
