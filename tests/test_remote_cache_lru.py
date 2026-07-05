"""B3(BE-3/BE-4)— remote_db 有界 LRU 缓存与倒排索引失效。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv(remote_db.REMOTE_CACHE_MAX_ENTRIES_ENV, raising=False)
    monkeypatch.delenv(remote_db.REMOTE_CACHE_MAX_MB_ENV, raising=False)
    remote_db._cache_clear_all()
    yield
    remote_db._cache_clear_all()


def test_entry_cap_evicts_least_recently_used(monkeypatch):
    monkeypatch.setenv(remote_db.REMOTE_CACHE_MAX_ENTRIES_ENV, '64')
    for i in range(64):
        remote_db._cache_set(('k', i), i)
    # 触碰 ('k', 0) 使其变为最近使用
    assert remote_db._cache_get(('k', 0)) == 0
    remote_db._cache_set(('k', 64), 64)  # 触发淘汰
    assert remote_db._cache_get(('k', 0)) == 0      # 被触碰过,保留
    assert remote_db._cache_get(('k', 1)) is None    # 最久未用,被淘汰
    assert len(remote_db._CACHE) == 64


def test_byte_cap_evicts(monkeypatch):
    monkeypatch.setenv(remote_db.REMOTE_CACHE_MAX_MB_ENV, '8')  # 下限 8MB
    big = 'x' * (3 * 1024 * 1024)  # 每条 ≈6MB(str 估算 ×2)
    remote_db._cache_set(('big', 1), big)
    remote_db._cache_set(('big', 2), big)  # 超 8MB → 淘汰第 1 条
    assert remote_db._cache_get(('big', 1)) is None
    assert remote_db._cache_get(('big', 2)) is not None
    assert remote_db._CACHE_TOTAL_BYTES <= 8 * 1024 * 1024


def test_user_invalidation_via_index_only_hits_that_user():
    remote_db._cache_set(('feed_item_detail', 'sch', 'item-1', 'user-a'), {'v': 1})
    remote_db._cache_set(('feed_item_detail', 'sch', 'item-1', 'user-b'), {'v': 2})
    remote_db._cache_set(('feed_sections_result', 'sch'), {'v': 3})

    removed = remote_db.clear_user_cache_keys('user-a')

    assert removed == 1
    assert remote_db._cache_get(('feed_item_detail', 'sch', 'item-1', 'user-a')) is None
    assert remote_db._cache_get(('feed_item_detail', 'sch', 'item-1', 'user-b')) == {'v': 2}
    assert remote_db._cache_get(('feed_sections_result', 'sch')) == {'v': 3}


def test_item_invalidation_handles_nested_tuple_keys():
    remote_db._cache_set(('feed_items_detail_batch', 'sch', ('item-1', 'item-2')), {'v': 1})
    remote_db._cache_set(('feed_item_detail', 'sch', 'item-9'), {'v': 9})

    removed = remote_db.clear_item_detail_cache_keys('item-2')

    assert removed == 1
    assert remote_db._cache_get(('feed_items_detail_batch', 'sch', ('item-1', 'item-2'))) is None
    assert remote_db._cache_get(('feed_item_detail', 'sch', 'item-9')) == {'v': 9}


def test_feed_prefix_invalidation_via_index(monkeypatch):
    monkeypatch.setattr(remote_db, 'clear_feed_local_read_cache_files', lambda: 0)
    remote_db._cache_set(('feed_sections_result', 'sch', 'x'), 1)
    remote_db._cache_set(('auth_session_user', 'sch', 'jti', 'user-a'), 2)

    remote_db.clear_feed_cache_keys()

    assert remote_db._cache_get(('feed_sections_result', 'sch', 'x')) is None
    assert remote_db._cache_get(('auth_session_user', 'sch', 'jti', 'user-a')) == 2  # auth 不受影响


def test_index_stays_consistent_after_overwrite_and_delete():
    key = ('feed_item_detail', 'sch', 'item-1', 'user-a')
    remote_db._cache_set(key, {'v': 1})
    remote_db._cache_set(key, {'v': 2})  # 覆盖
    assert remote_db._cache_get(key) == {'v': 2}
    remote_db._cache_delete(key)
    assert remote_db._cache_get(key) is None
    # 索引里不残留
    assert key not in remote_db._CACHE_TOKEN_INDEX.get('user-a', set())
