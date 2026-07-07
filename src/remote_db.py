"""Optional Supabase/Postgres adapter for the remote database migration.

SQLite remains the default for existing local development. New online
deployments can opt into ``INFO2ACTION_DATA_AUTHORITY=supabase`` to make the
remote database the production data authority and fail fast when a core read or
status-write surface is still pointing at SQLite.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
import uuid
import time
import threading
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import action_detail_read_model
from category_taxonomy import ACTIVE_CATEGORY_IDS, canonicalize_category, expand_query_categories
from env_utils import load_project_env
from health_freshness import classify_platform_freshness
from time_utils import parse_datetime, sort_key, to_utc_iso

BASE = Path(__file__).resolve().parents[1]

GLOBAL_BACKEND_ENV = "INFO2ACTION_READ_BACKEND"
BACKEND_ENV = "INFO2ACTION_EVENT_READ_BACKEND"
FEED_BACKEND_ENV = "INFO2ACTION_FEED_READ_BACKEND"
STATUS_BACKEND_ENV = "INFO2ACTION_STATUS_BACKEND"
DATA_AUTHORITY_ENV = "INFO2ACTION_DATA_AUTHORITY"
STORAGE_MODE_ENV = "INFO2ACTION_STORAGE_MODE"
PIPELINE_WRITE_MODE_ENV = "INFO2ACTION_PIPELINE_WRITE_MODE"
FETCH_WRITE_BACKEND_ENV = "INFO2ACTION_FETCH_WRITE_BACKEND"
ENRICH_BACKEND_ENV = "INFO2ACTION_ENRICH_BACKEND"
EMBEDDING_BACKEND_ENV = "INFO2ACTION_EMBEDDING_BACKEND"
CLUSTER_BACKEND_ENV = "INFO2ACTION_CLUSTER_BACKEND"
APP_STATE_BACKEND_ENV = "INFO2ACTION_APP_STATE_BACKEND"
ASSET_BACKEND_ENV = "INFO2ACTION_ASSET_BACKEND"
REMOTE_SYNC_AFTER_PIPELINE_ENV = "INFO2ACTION_REMOTE_SYNC_AFTER_PIPELINE"
REMOTE_SCHEMA_ENV = "SUPABASE_REMOTE_DB_SCHEMA"
SUPABASE_URL_ENV = "SUPABASE_URL"
SUPABASE_SERVICE_KEY_ENV = "SUPABASE_SERVICE_ROLE_KEY"
SUPABASE_STORAGE_BUCKET_ENV = "SUPABASE_STORAGE_BUCKET"
DEFAULT_REMOTE_SCHEMA = "remote_poc"
DEFAULT_STORAGE_BUCKET = "info2action-assets"
SQLITE_BACKEND = "sqlite"
REMOTE_BACKENDS = {"supabase", "supabase_poc", "postgres", "postgres_poc"}
LOCAL_AUTHORITIES = {"", "local", "sqlite"}
REMOTE_AUTHORITIES = {"remote", "supabase", "supabase_poc", "postgres", "postgres_poc"}
PIPELINE_SQLITE_THEN_SYNC = "sqlite_then_sync"
PIPELINE_UNSUPPORTED_DIRECT = {"supabase_direct", "direct_supabase", "direct"}
PIPELINE_UNSUPPORTED_DUAL = {"dual_write", "dual"}
STORAGE_LOCAL = "local"
STORAGE_SQLITE_THEN_SYNC = "sqlite_then_sync"
STORAGE_REMOTE_ONLY = "remote_only"
UNCATEGORIZED_SENTINEL = "__uncategorized__"
STATUS_COLUMNS = {
    "clicked": "clicked_at",
    "starred": "starred_at",
    "hidden": "hidden_at",
    "read": "read_at",
}
ASR_DAILY_QUOTA_HOURS_DEFAULT = 10.0
ASR_ITEM_UPDATE_COLUMNS = {
    "ai_summary",
    "asr_text",
    "asr_status",
    "asr_duration_sec",
    "asr_cost_yuan",
    "asr_attempted_at",
    "asr_failed_reason",
    "asr_provider",
    "asr_segments",
    "asr_text_cn",
    "asr_segments_cn",
}
_DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES = -480
ASR_JSON_COLUMNS = {"asr_segments", "asr_segments_cn"}
ASR_TIMESTAMP_COLUMNS = {"asr_attempted_at"}
REMOTE_ITEM_WRITE_COLUMNS = (
    "id",
    "user_id",
    "platform",
    "source",
    "source_id",
    "fetch_run_id",
    "title",
    "content",
    "author_name",
    "author_id",
    "author_avatar",
    "url",
    "cover_url",
    "description",
    "media_json",
    "metrics_json",
    "tags_json",
    "lang",
    "detail_json",
    "comments_json",
    "asr_text",
    "asr_status",
    "asr_duration_sec",
    "asr_cost_yuan",
    "asr_attempted_at",
    "asr_failed_reason",
    "asr_provider",
    "asr_segments",
    "asr_text_cn",
    "asr_segments_cn",
    "ai_summary",
    "ai_key_points",
    "ai_category",
    "ai_keywords",
    "ai_categories",
    "ai_subcategories",
    "multi_l1_reason",
    "ai_extracted",
    "content_type",
    "ai_quality_score",
    "visible",
    "relevance_score",
    "embedding",
    "embedding_provider",
    "embedding_model",
    "embedding_input_variant",
    "embedding_generated_at",
    "canonical_url",
    "cluster_id",
    "fetched_at",
    "published_at",
    "created_at",
)
REMOTE_ITEM_JSONB_COLUMNS = {
    "media_json",
    "metrics_json",
    "tags_json",
    "detail_json",
    "comments_json",
    "asr_segments",
    "asr_segments_cn",
    "ai_categories",
    "ai_subcategories",
    "ai_extracted",
}
REMOTE_ITEM_TIMESTAMP_COLUMNS = {
    "embedding_generated_at",
    "asr_attempted_at",
    "fetched_at",
    "published_at",
    "created_at",
}
REMOTE_ITEM_MULTIROW_UPSERT_CHUNK_SIZE = 200

REMOTE_DB_POOL_DISABLED_ENV = "INFO2ACTION_REMOTE_DB_POOL_DISABLED"
REMOTE_DB_POOL_MIN_ENV = "INFO2ACTION_REMOTE_DB_POOL_MIN"
REMOTE_DB_POOL_MAX_ENV = "INFO2ACTION_REMOTE_DB_POOL_MAX"
REMOTE_DB_POOL_TIMEOUT_ENV = "INFO2ACTION_REMOTE_DB_POOL_TIMEOUT_SEC"
REMOTE_DB_CONNECT_TIMEOUT_ENV = "INFO2ACTION_REMOTE_DB_CONNECT_TIMEOUT_SEC"
REMOTE_DB_CONNECT_ATTEMPTS_ENV = "INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS"
REMOTE_DB_FORCE_WRITABLE_ENV = "INFO2ACTION_REMOTE_DB_FORCE_WRITABLE_ON_CONNECT"
REMOTE_CACHE_TTL_ENV = "INFO2ACTION_REMOTE_CACHE_TTL_SEC"
REMOTE_AUTH_CACHE_TTL_ENV = "INFO2ACTION_REMOTE_AUTH_CACHE_TTL_SEC"
REMOTE_SNAPSHOT_TTL_ENV = "INFO2ACTION_REMOTE_SNAPSHOT_TTL_SEC"
REMOTE_FEED_LIVE_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_FEED_LIVE_TIMEOUT_MS"
REMOTE_ACTIONS_BOARD_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_ACTIONS_BOARD_TIMEOUT_MS"
REMOTE_ACTIONS_BOARD_DETAIL_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_ACTIONS_BOARD_DETAIL_TIMEOUT_MS"
REMOTE_PENDING_SCAN_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_PENDING_SCAN_TIMEOUT_MS"
REMOTE_DB_PRESSURE_TIMEOUT_MIN_ENV = "INFO2ACTION_REMOTE_DB_PRESSURE_TIMEOUT_MINUTES"
REMOTE_DB_PRESSURE_AUTOVACUUM_AGE_SEC_ENV = "INFO2ACTION_REMOTE_DB_PRESSURE_AUTOVACUUM_AGE_SEC"
REMOTE_DB_PRESSURE_PROBE_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_DB_PRESSURE_PROBE_TIMEOUT_MS"
REMOTE_CLUSTER_WRITE_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_CLUSTER_WRITE_TIMEOUT_MS"
CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_ENV = "INFO2ACTION_CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS"
CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_ENV = "INFO2ACTION_CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS"
REMOTE_FEED_LIVE_DISABLED_ENV = "INFO2ACTION_REMOTE_FEED_LIVE_DISABLED"
REMOTE_FEED_LIVE_CIRCUIT_SEC_ENV = "INFO2ACTION_REMOTE_FEED_LIVE_CIRCUIT_SEC"
ALLOW_BLOCKING_MV_REFRESH_ENV = "INFO2ACTION_ALLOW_BLOCKING_MV_REFRESH"
ALLOW_PLATFORM_MV_REFRESH_ENV = "INFO2ACTION_ALLOW_PLATFORM_MV_REFRESH"
PREWARM_REFRESH_PLATFORMS_MV_ENV = "INFO2ACTION_PREWARM_REFRESH_PLATFORMS_MV"
REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV = "INFO2ACTION_REMOTE_RUNNING_FETCH_MAX_AGE_MINUTES"
FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV = "INFO2ACTION_FETCH_RUN_HEARTBEAT_GRACE_SEC"
INFO_READ_MODEL_ENV = "INFO2ACTION_INFO_READ_MODEL"
INFO_READ_MODEL_REFRESH_ENV = "INFO2ACTION_INFO_READ_MODEL_REFRESH"
# BF-0706-4: 跨进程单飞锁 —— 防止一次重建跑过 min_interval 时新请求并发再起一次重建
# 造成叠加风暴(Supabase compute 被压崩)。pg advisory lock 会话级,连接关闭自动释放。
_INFO_READ_MODEL_BUILD_LOCK_KEY = 517070604
INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV = "INFO2ACTION_INFO_READ_MODEL_REFRESH_TIMEOUT_MS"
INFO_READ_MODEL_INCREMENTAL_ENV = "INFO2ACTION_INFO_READ_MODEL_INCREMENTAL"
INFO_READ_MODEL_PREWARM_SCOPES_ENV = "INFO2ACTION_INFO_READ_MODEL_PREWARM_SCOPES"
INFO_READ_MODEL_LIVE_OVERLAY_ENV = "INFO2ACTION_INFO_READ_MODEL_LIVE_OVERLAY"
INFO_READ_MODEL_LIVE_OVERLAY_LIMIT_ENV = "INFO2ACTION_INFO_READ_MODEL_LIVE_OVERLAY_LIMIT"
INFO_READ_MODEL_LIVE_OVERLAY_PER_SCOPE_LIMIT_ENV = "INFO2ACTION_INFO_READ_MODEL_LIVE_OVERLAY_PER_SCOPE_LIMIT"
INFO_READ_MODEL_LIVE_OVERLAY_TIMEOUT_MS_ENV = "INFO2ACTION_INFO_READ_MODEL_LIVE_OVERLAY_TIMEOUT_MS"
INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV = "INFO2ACTION_INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS"
INFO_READ_MODEL_LIVE_OVERLAY_RESULT_CACHE_TTL_ENV = "INFO2ACTION_INFO_READ_MODEL_LIVE_OVERLAY_RESULT_CACHE_TTL_SEC"
INFO_READ_MODEL_STATE_KEY = "feed_platforms_v1"
INFO_READ_MODEL_MIN_GITHUB_STARS = 50
INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT = 180000
INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT = 5000
INFO_READ_MODEL_RETAIN_COMPLETE_VERSIONS = 1
INFO_READ_MODEL_PRUNE_TRANSIENT_AGE_HOURS = 6
INFO_READ_MODEL_SORT_POLICY = "published_at_desc_v1"
INFO_READ_MODEL_PREWARM_SCOPES_DEFAULT = 2
HIGHLIGHTS_READ_MODEL_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL"
HIGHLIGHTS_READ_MODEL_REFRESH_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH"
HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS"
HIGHLIGHTS_READ_MODEL_INCREMENTAL_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_INCREMENTAL"
HIGHLIGHTS_READ_MODEL_STALE_FALLBACK_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_STALE_FALLBACK"
HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS"
HIGHLIGHTS_READ_MODEL_SELF_HEAL_ENV = "INFO2ACTION_HIGHLIGHTS_READ_MODEL_SELF_HEAL"
HIGHLIGHTS_REFRESH_SKIP_DURING_FETCH_ENV = "INFO2ACTION_HIGHLIGHTS_REFRESH_SKIP_DURING_FETCH"
HIGHLIGHTS_VERDICT_FILTER_ENV = "INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_ENABLED"
HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS_ENV = "INFO2ACTION_HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS"
HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT = 180000
EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_ENV = "INFO2ACTION_EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS"
EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV = "INFO2ACTION_EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS"
EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_DEFAULT = 4500
EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT = 15000
CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_DEFAULT = 4500
CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_DEFAULT = 15000
# BF-0704-6: 1500ms 在冷缓存 bitmap 回表下必超时导致公开搜索常态降级;
# PGroonga 索引落地后 4000ms 覆盖冷缓存首查,且仍受通用预算 min() 约束。
CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_ENV = "INFO2ACTION_CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS"
CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_DEFAULT = 4000
CONTEXT_SEARCH_EVENTS_DEGRADED_TTL_SEC = 30
# 搜索 total 封顶:全量 count(*) 需回表全部匹配行,冷缓存下是主要耗时来源
CONTEXT_SEARCH_EVENTS_TOTAL_CAP = 1001
REMOTE_FEED_SEARCH_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_FEED_SEARCH_TIMEOUT_MS"
REMOTE_FEED_SEARCH_TIMEOUT_MS_DEFAULT = 6000
HIGHLIGHTS_READ_MODEL_STATE_KEY = "highlights_events_v1"
HIGHLIGHTS_READ_MODEL_VERSION = "highlights_v1"
HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS = 50
HIGHLIGHTS_READ_MODEL_WINDOW_DAYS = 30
ACTION_BOARD_READ_MODEL_ENV = "INFO2ACTION_ACTION_BOARD_READ_MODEL"
ACTION_BOARD_READ_MODEL_REFRESH_ENV = "INFO2ACTION_ACTION_BOARD_READ_MODEL_REFRESH"
ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_ENV = "INFO2ACTION_ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS"
ACTION_BOARD_READ_MODEL_VERSION = 1
ACTION_BOARD_READ_MODEL_NAME = "action_board_v1"
ACTION_BOARD_READ_MODEL_STATE_PREFIX = "action_board_v1"
ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT = 60000

_POOL: Any | None = None
_POOL_DSN: str | None = None
_POOL_LOCK = threading.Lock()

# BE-4(B3): 进程内缓存改为有界 LRU(条目数+近似字节双上限)。
# 原实现是无界 dict——每用户×每 item 的 detail(单条可达数百 KB)、batch
# 组合键、搜索词长尾会让 2GB 单机数周内缓慢走向 OOM。
# _CACHE_TOKEN_INDEX 是 key 内字符串元素的倒排索引,把 clear_user/item/
# prefix 三类失效从 O(全缓存) 降为 O(命中数)——原 O(N) 全扫在全局锁下
# 进行,点击越密缓存越大,所有线程的缓存读都在锁上排队(BE-3)。
from collections import OrderedDict as _OrderedDict

_CACHE: "_OrderedDict[tuple[Any, ...], dict[str, Any]]" = _OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_TOKEN_INDEX: dict[str, set] = {}
_CACHE_TOTAL_BYTES = 0
REMOTE_CACHE_MAX_ENTRIES_ENV = "INFO2ACTION_REMOTE_CACHE_MAX_ENTRIES"
REMOTE_CACHE_MAX_MB_ENV = "INFO2ACTION_REMOTE_CACHE_MAX_MB"
_SNAPSHOT_WRITE_LOCK = threading.Lock()
_SNAPSHOT_WRITES_IN_FLIGHT: set[str] = set()
_MV_REFRESH_LOCK = threading.Lock()
_MV_REFRESH_LAST_ATTEMPT_AT = 0.0
_INFO_READ_MODEL_REFRESH_LOCK = threading.Lock()
_INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
_HIGHLIGHTS_READ_MODEL_REFRESH_LOCK = threading.Lock()
_HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
_HIGHLIGHTS_READ_MODEL_SELF_HEAL_LOCK = threading.Lock()
_HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT = False
_REMOTE_FEED_LIVE_CIRCUIT_LOCK = threading.Lock()
_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL = 0.0
_LOCAL_READ_CACHE_DIR = BASE / "data" / "remote_read_cache"
_LOCAL_READ_CACHE_MAX_AGE_SEC = 24 * 60 * 60
_LOCAL_READ_CACHE_FRESH_SEC = 180
_REMOTE_STATUS_TIMEOUT_MS = 1500


class RemoteDBError(RuntimeError):
    """Base class for expected remote DB adapter failures."""


class RemoteDBConfigError(RemoteDBError):
    """Remote DB was requested but required config/dependencies are missing."""


def _runtime_env() -> dict[str, str]:
    """Merge project `.env` with process env; process env wins."""
    values = load_project_env(BASE)
    values.update({k: v for k, v in os.environ.items() if isinstance(v, str)})
    return values


def _normalized(value: str | None) -> str:
    return (value or "").strip().lower()


def _truthy(value: str | None) -> bool:
    return _normalized(value) in {"1", "true", "yes", "on"}


def _env_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    raw = (env.get(key) or "").strip()
    if raw == "":
        return default
    return _truthy(raw)


def _force_writable_on_connect(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(REMOTE_DB_FORCE_WRITABLE_ENV))


def _remote_authority_global_backend(env: dict[str, str]) -> str | None:
    authority = _normalized(env.get(DATA_AUTHORITY_ENV))
    backend = _normalized(env.get(GLOBAL_BACKEND_ENV))
    if authority in REMOTE_AUTHORITIES and backend in REMOTE_BACKENDS:
        return backend
    return None


def _backend_for(surface_env: str) -> str:
    """Return the configured backend for a read surface.

    Empty/missing values intentionally resolve to SQLite so local development and
    existing deployments keep their current behavior unless explicitly opted in.
    """
    env = _runtime_env()
    backend = _normalized(env.get(surface_env))
    global_remote = _remote_authority_global_backend(env)
    if global_remote and backend in {"", SQLITE_BACKEND}:
        backend = global_remote
    else:
        backend = backend or _normalized(env.get(GLOBAL_BACKEND_ENV)) or SQLITE_BACKEND
    return backend or SQLITE_BACKEND


def data_authority() -> str:
    """Return the configured production data authority.

    The value is intentionally separate from read backend switches: a developer
    can test a single remote surface, while a production deployment can declare
    that the remote DB is authoritative and must satisfy all core surfaces.
    """
    raw = _normalized(_runtime_env().get(DATA_AUTHORITY_ENV) or "local")
    if raw in LOCAL_AUTHORITIES:
        return "local"
    if raw in REMOTE_AUTHORITIES:
        return "supabase" if raw in {"remote", "supabase", "supabase_poc"} else "postgres"
    raise RemoteDBConfigError(
        f"Invalid {DATA_AUTHORITY_ENV}: {raw!r}. Use 'local' or 'supabase'."
    )


def remote_authority_enabled() -> bool:
    return data_authority() != "local"


def storage_mode() -> str:
    raw = _normalized(_runtime_env().get(STORAGE_MODE_ENV))
    if raw in {"", STORAGE_LOCAL, "sqlite"}:
        return STORAGE_LOCAL
    mode = raw.replace("-", "_")
    if mode in {"remote", "remoteonly", STORAGE_REMOTE_ONLY}:
        return STORAGE_REMOTE_ONLY
    if mode in {"sync", "sqlite_sync", "sqlite_then_sync", "local_then_sync"}:
        return STORAGE_SQLITE_THEN_SYNC
    raise RemoteDBConfigError(
        f"Invalid {STORAGE_MODE_ENV}: {raw!r}. "
        f"Use '{STORAGE_LOCAL}', '{STORAGE_SQLITE_THEN_SYNC}', or '{STORAGE_REMOTE_ONLY}'."
    )


def remote_only_blockers() -> list[str]:
    """Return known blockers before the project can run without local storage."""
    blockers: list[str] = []
    if not remote_authority_enabled():
        blockers.append(f"{DATA_AUTHORITY_ENV}=supabase is required.")
    if not asset_storage_to_remote():
        blockers.append(f"{ASSET_BACKEND_ENV}=supabase is required for images/audio/html assets.")
    try:
        assert_pipeline_write_mode_ready()
    except RemoteDBError as exc:
        blockers.append(str(exc))
    try:
        assert_asset_storage_ready()
    except RemoteDBError as exc:
        blockers.append(str(exc))
    return blockers


def assert_storage_contract_ready() -> dict[str, Any]:
    """Validate the high-level storage contract for deployments.

    ``remote_only`` is the final target: a cloned repo plus Supabase credentials
    can access and mutate all production data without a local ``data/feed.db``.
    Until every persistent table and pipeline writer is migrated, this mode must
    fail loudly rather than falling back to SQLite.
    """
    mode = storage_mode()
    if mode == STORAGE_LOCAL:
        return {"mode": mode, "remote_only": False, "blockers": []}
    if mode == STORAGE_SQLITE_THEN_SYNC:
        return {"mode": mode, "remote_only": False, "blockers": []}

    blockers = remote_only_blockers()
    if blockers:
        raise RemoteDBConfigError(
            f"{STORAGE_MODE_ENV}={STORAGE_REMOTE_ONLY} remote-only target is not ready. "
            f"Known blockers: {'; '.join(blockers)}"
        )
    return {
        "mode": mode,
        "remote_only": True,
        "blockers": [],
        "asset_storage": assert_asset_storage_ready(),
    }


def event_read_backend() -> str:
    return _backend_for(BACKEND_ENV)


def events_read_from_remote() -> bool:
    return event_read_backend() in REMOTE_BACKENDS


def feed_read_backend() -> str:
    return _backend_for(FEED_BACKEND_ENV)


def feed_read_from_remote() -> bool:
    return feed_read_backend() in REMOTE_BACKENDS


def status_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(STATUS_BACKEND_ENV))
    global_remote = _remote_authority_global_backend(env)
    if global_remote and backend in {"", SQLITE_BACKEND}:
        feed_backend = _backend_for(FEED_BACKEND_ENV)
        backend = feed_backend if feed_backend in REMOTE_BACKENDS else global_remote
    else:
        backend = (
            backend
            or _normalized(env.get(FEED_BACKEND_ENV))
            or _normalized(env.get(GLOBAL_BACKEND_ENV))
            or SQLITE_BACKEND
        )
    return backend or SQLITE_BACKEND


def status_write_to_remote() -> bool:
    return status_backend() in REMOTE_BACKENDS


def fetch_write_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(FETCH_WRITE_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        if mode == STORAGE_REMOTE_ONLY:
            backend = "supabase"
        else:
            backend = SQLITE_BACKEND
    if backend in {"postgres", "postgres_poc", "supabase_poc"}:
        return "supabase"
    if backend in {"", SQLITE_BACKEND, "local"}:
        return SQLITE_BACKEND
    if backend == "supabase":
        return backend
    raise RemoteDBConfigError(
        f"Invalid {FETCH_WRITE_BACKEND_ENV}: {backend!r}. Use 'sqlite' or 'supabase'."
    )


def fetch_write_to_remote() -> bool:
    return fetch_write_backend() == "supabase"


def enrich_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(ENRICH_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        backend = "supabase" if mode == STORAGE_REMOTE_ONLY else SQLITE_BACKEND
    if backend in {"postgres", "postgres_poc", "supabase_poc"}:
        return "supabase"
    if backend in {"", SQLITE_BACKEND, "local"}:
        return SQLITE_BACKEND
    if backend == "supabase":
        return backend
    raise RemoteDBConfigError(
        f"Invalid {ENRICH_BACKEND_ENV}: {backend!r}. Use 'sqlite' or 'supabase'."
    )


def enrich_to_remote() -> bool:
    return enrich_backend() == "supabase"


def embedding_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(EMBEDDING_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        backend = "supabase" if mode == STORAGE_REMOTE_ONLY else SQLITE_BACKEND
    if backend in {"postgres", "postgres_poc", "supabase_poc"}:
        return "supabase"
    if backend in {"", SQLITE_BACKEND, "local"}:
        return SQLITE_BACKEND
    if backend == "supabase":
        return backend
    raise RemoteDBConfigError(
        f"Invalid {EMBEDDING_BACKEND_ENV}: {backend!r}. Use 'sqlite' or 'supabase'."
    )


def embedding_to_remote() -> bool:
    return embedding_backend() == "supabase"


def cluster_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(CLUSTER_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        backend = "supabase" if mode == STORAGE_REMOTE_ONLY else SQLITE_BACKEND
    if backend in {"postgres", "postgres_poc", "supabase_poc"}:
        return "supabase"
    if backend in {"", SQLITE_BACKEND, "local"}:
        return SQLITE_BACKEND
    if backend == "supabase":
        return backend
    raise RemoteDBConfigError(
        f"Invalid {CLUSTER_BACKEND_ENV}: {backend!r}. Use 'sqlite' or 'supabase'."
    )


def cluster_to_remote() -> bool:
    return cluster_backend() == "supabase"


def app_state_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(APP_STATE_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        backend = "supabase" if (mode == STORAGE_REMOTE_ONLY or remote_authority_enabled()) else SQLITE_BACKEND
    if backend in {"postgres", "postgres_poc", "supabase_poc"}:
        return "supabase"
    if backend in {"", SQLITE_BACKEND, "local"}:
        return SQLITE_BACKEND
    if backend == "supabase":
        return backend
    raise RemoteDBConfigError(
        f"Invalid {APP_STATE_BACKEND_ENV}: {backend!r}. Use 'sqlite' or 'supabase'."
    )


def app_state_to_remote() -> bool:
    return app_state_backend() == "supabase"


def asset_backend() -> str:
    env = _runtime_env()
    backend = _normalized(env.get(ASSET_BACKEND_ENV))
    if not backend:
        mode = storage_mode()
        backend = "supabase" if mode == STORAGE_REMOTE_ONLY else STORAGE_LOCAL
    if backend in {"", STORAGE_LOCAL, "file", "filesystem"}:
        return STORAGE_LOCAL
    if backend in {"remote", "supabase", "supabase_storage", "storage"}:
        return "supabase"
    raise RemoteDBConfigError(
        f"Invalid {ASSET_BACKEND_ENV}: {backend!r}. Use 'local' or 'supabase'."
    )


def asset_storage_to_remote() -> bool:
    return asset_backend() == "supabase"


def any_remote_backend_enabled() -> bool:
    return events_read_from_remote() or feed_read_from_remote() or status_write_to_remote()


def assert_remote_authority_ready() -> dict[str, Any]:
    """Validate the server deploy contract for remote-authoritative data.

    This is a configuration gate, not a connectivity probe. Call ``status()``
    afterwards when startup should also prove the remote database is reachable.
    """
    authority = data_authority()
    if authority == "local":
        return {"authority": authority, "backends": {}, "schema": remote_schema()}

    backends = {
        "feed": feed_read_backend(),
        "event": event_read_backend(),
        "status": status_backend(),
    }
    required_envs = {
        "feed": FEED_BACKEND_ENV,
        "event": BACKEND_ENV,
        "status": STATUS_BACKEND_ENV,
    }
    local_surfaces = [
        f"{required_envs[name]}={backend}"
        for name, backend in backends.items()
        if backend not in REMOTE_BACKENDS
    ]
    if local_surfaces:
        raise RemoteDBConfigError(
            f"{DATA_AUTHORITY_ENV}={authority} requires remote backends for "
            f"feed/event/status surfaces; found {', '.join(local_surfaces)}. "
            f"Set {GLOBAL_BACKEND_ENV}=supabase_poc, or set each surface backend explicitly."
        )
    # Validate presence without leaking the actual connection string.
    database_url()
    schema = remote_schema()
    return {"authority": authority, "backends": backends, "schema": schema}


def pipeline_write_mode() -> str:
    """Return the configured pipeline write mode.

    Phase 2A intentionally keeps the production write path as local SQLite
    followed by an explicit Supabase incremental sync. Full direct Supabase
    writes require a larger per-stage migration of fetch/enrich/cluster/publish
    transactions, so unsupported modes fail fast instead of silently falling
    back to local writes.
    """
    raw = _normalized(_runtime_env().get(PIPELINE_WRITE_MODE_ENV))
    if not raw:
        raw = "supabase_direct" if storage_mode() == STORAGE_REMOTE_ONLY else PIPELINE_SQLITE_THEN_SYNC
    mode = raw.replace("-", "_")
    if mode in {"sqlite", "local", "local_then_sync", PIPELINE_SQLITE_THEN_SYNC}:
        return PIPELINE_SQLITE_THEN_SYNC
    if mode in PIPELINE_UNSUPPORTED_DIRECT:
        return "supabase_direct"
    if mode in PIPELINE_UNSUPPORTED_DUAL:
        return "dual_write"
    raise RemoteDBConfigError(
        f"Invalid {PIPELINE_WRITE_MODE_ENV}: {raw!r}. "
        f"Use '{PIPELINE_SQLITE_THEN_SYNC}' until direct Supabase writes are implemented."
    )


def remote_sync_after_pipeline_enabled() -> bool:
    return _truthy(_runtime_env().get(REMOTE_SYNC_AFTER_PIPELINE_ENV))


def assert_pipeline_write_mode_ready() -> dict[str, Any]:
    mode = pipeline_write_mode()
    if mode == "supabase_direct":
        writers = {
            "fetch": fetch_write_backend(),
            "enrich": enrich_backend(),
            "embedding": embedding_backend(),
            "cluster": cluster_backend(),
            "app_state": app_state_backend(),
        }
        missing = [name for name, backend in writers.items() if backend != "supabase"]
        if missing:
            raise RemoteDBConfigError(
                f"{PIPELINE_WRITE_MODE_ENV}=supabase_direct requires direct Supabase writers "
                f"for {', '.join(missing)}. Set {FETCH_WRITE_BACKEND_ENV}=supabase, "
                f"{ENRICH_BACKEND_ENV}=supabase, {EMBEDDING_BACKEND_ENV}=supabase, "
                f"{CLUSTER_BACKEND_ENV}=supabase, and {APP_STATE_BACKEND_ENV}=supabase."
            )
        return {
            "mode": mode,
            "remote_sync_after_pipeline": False,
            "direct_writers": writers,
        }
    if mode != PIPELINE_SQLITE_THEN_SYNC:
        raise RemoteDBConfigError(
            f"{PIPELINE_WRITE_MODE_ENV}={mode} is not implemented yet. "
            f"Use {PIPELINE_WRITE_MODE_ENV}={PIPELINE_SQLITE_THEN_SYNC} together with "
            f"{REMOTE_SYNC_AFTER_PIPELINE_ENV}=1 for the current production pipeline."
        )
    return {
        "mode": mode,
        "remote_sync_after_pipeline": remote_sync_after_pipeline_enabled(),
    }


def remote_schema() -> str:
    schema = (_runtime_env().get(REMOTE_SCHEMA_ENV) or DEFAULT_REMOTE_SCHEMA).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RemoteDBConfigError(f"Invalid {REMOTE_SCHEMA_ENV}: {schema!r}")
    return schema


def load_source_index_remote(pg_conn: Any | None = None) -> dict[str, Any] | None:
    """Load the remote sources registry index. Fail open for fetch/ingest paths."""
    try:
        if pg_conn is None:
            with connect() as conn:
                return load_source_index_remote(conn)
        import db

        rows = pg_conn.execute(
            f"SELECT id, platform, source_key, status, config_json FROM {remote_schema()}.sources"
        ).fetchall()
        return db.build_source_index_from_rows(rows)
    except Exception:
        return None


def list_active_sources_remote(platform: str, pg_conn: Any | None = None) -> list[dict[str, Any]]:
    """Return active remote sources for one platform. Fail open to config fallback."""
    try:
        if pg_conn is None:
            with connect() as conn:
                return list_active_sources_remote(platform, conn)
        import db

        rows = pg_conn.execute(
            f"""SELECT id, source_key, display_name, config_json
                FROM {remote_schema()}.sources
                WHERE platform=%s AND status='active'
                ORDER BY id""",
            (platform,),
        ).fetchall()
        return [db.normalize_active_source_row(row) for row in rows]
    except Exception:
        return []


def record_source_fetch_result_remote(
    source_id: int | None,
    ok: bool,
    error: Any = None,
    broken_after: int = 5,
    pg_conn: Any | None = None,
) -> None:
    """Record one remote source fetch result without interrupting the fetch pipeline."""
    try:
        if source_id is None:
            return
        if pg_conn is None:
            with connect() as conn:
                record_source_fetch_result_remote(
                    source_id,
                    ok,
                    error=error,
                    broken_after=broken_after,
                    pg_conn=conn,
                )
                return

        schema = remote_schema()
        row = pg_conn.execute(
            f"SELECT status, consecutive_failures FROM {schema}.sources WHERE id = %s",
            (source_id,),
        ).fetchone()
        if row is None:
            return
        status = row["status"]
        if status not in {"active", "broken"}:
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ok:
            new_status = "active" if status == "broken" else status
            pg_conn.execute(
                f"""UPDATE {schema}.sources
                      SET status = %s,
                          consecutive_failures = 0,
                          last_success_at = %s,
                          last_error = NULL,
                          updated_at = %s
                    WHERE id = %s""",
                (new_status, now, now, source_id),
            )
            commit = getattr(pg_conn, "commit", None)
            if commit:
                commit()
            return

        try:
            threshold = int(broken_after)
        except (TypeError, ValueError):
            threshold = 5
        if threshold <= 0:
            threshold = 5
        failures = int(row["consecutive_failures"] or 0) + 1
        new_status = "broken" if status == "active" and failures >= threshold else status
        last_error = None if error is None else str(error)[:500]
        pg_conn.execute(
            f"""UPDATE {schema}.sources
                  SET status = %s,
                      consecutive_failures = %s,
                      last_error = %s,
                      updated_at = %s
                WHERE id = %s""",
            (new_status, failures, last_error, now, source_id),
        )
        commit = getattr(pg_conn, "commit", None)
        if commit:
            commit()
    except Exception:
        return


def database_url() -> str:
    env = _runtime_env()
    url = env.get("SUPABASE_DB_URL") or env.get("DATABASE_URL") or ""
    if not url.strip():
        raise RemoteDBConfigError(
            "SUPABASE_DB_URL is missing; add it to .env before using the remote read backend."
        )
    return url.strip()


def _env_int(env: dict[str, str], key: str, default: int, *, min_value: int = 0) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return max(min_value, int(raw))
    except (TypeError, ValueError):
        return default


def _remote_cache_ttl(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    return _env_int(values, REMOTE_CACHE_TTL_ENV, 180, min_value=0)


def _remote_snapshot_ttl(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    return _env_int(values, REMOTE_SNAPSHOT_TTL_ENV, 1800, min_value=0)


def _platform_mv_refresh_allowed(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(ALLOW_PLATFORM_MV_REFRESH_ENV))


def _cache_max_entries() -> int:
    return _env_int(_runtime_env(), REMOTE_CACHE_MAX_ENTRIES_ENV, 4096, min_value=64)


def _cache_max_bytes() -> int:
    return _env_int(_runtime_env(), REMOTE_CACHE_MAX_MB_ENV, 192, min_value=8) * 1024 * 1024


def _key_tokens(key: tuple[Any, ...]):
    """key 中的可索引 token(含一层嵌套),供倒排索引/定向失效。"""
    for elem in key:
        if isinstance(elem, (tuple, list, set, frozenset)):
            for sub in elem:
                yield str(sub)
        else:
            yield str(elem)


def _estimate_size(value: Any, _cap: int = 4 * 1024 * 1024) -> int:
    """近似字节数(递归,4MB 早停)——成本与既有的 set 侧 deepcopy 同阶。"""
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return 24
        if isinstance(value, str):
            return 50 + len(value) * 2
        if isinstance(value, (bytes, bytearray)):
            return 50 + len(value)
        if isinstance(value, dict):
            total = 64
            for k, v in value.items():
                total += _estimate_size(k, _cap) + _estimate_size(v, _cap)
                if total >= _cap:
                    return _cap
            return total
        if isinstance(value, (list, tuple, set, frozenset)):
            total = 64
            for v in value:
                total += _estimate_size(v, _cap)
                if total >= _cap:
                    return _cap
            return total
    except Exception:
        pass
    return 256


def _cache_remove_locked(key: tuple[Any, ...]) -> bool:
    """必须持 _CACHE_LOCK 调用;对索引/字节漂移容错。"""
    global _CACHE_TOTAL_BYTES
    entry = _CACHE.pop(key, None)
    if entry is not None:
        _CACHE_TOTAL_BYTES = max(0, _CACHE_TOTAL_BYTES - int(entry.get("size") or 0))
    # 索引清理不依赖 entry 是否存在(容忍外部直接 _CACHE.clear() 造成的漂移)
    for token in set(_key_tokens(key)):
        bucket = _CACHE_TOKEN_INDEX.get(token)
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                _CACHE_TOKEN_INDEX.pop(token, None)
    return entry is not None


def _cache_evict_locked() -> None:
    max_entries = _cache_max_entries()
    max_bytes = _cache_max_bytes()
    while _CACHE and (len(_CACHE) > max_entries or _CACHE_TOTAL_BYTES > max_bytes):
        oldest_key = next(iter(_CACHE))
        _cache_remove_locked(oldest_key)


def _cache_clear_all() -> None:
    """清空缓存与全部记账(测试/运维用;不要绕过它直接 _CACHE.clear())。"""
    global _CACHE_TOTAL_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_TOKEN_INDEX.clear()
        _CACHE_TOTAL_BYTES = 0


def _cache_get(key: tuple[Any, ...]) -> Any | None:
    return _cache_get_with_ttl(key, _remote_cache_ttl())


def _cache_get_with_ttl(key: tuple[Any, ...], ttl: int) -> Any | None:
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not cached:
            return None
        if now - float(cached.get("ts", 0)) > ttl:
            _cache_remove_locked(key)
            return None
        _CACHE.move_to_end(key)  # LRU touch
        return cached.get("value")


def _cache_set(key: tuple[Any, ...], value: Any) -> Any:
    return _cache_set_with_ttl(key, value, _remote_cache_ttl())


def _cache_set_with_ttl(key: tuple[Any, ...], value: Any, ttl: int) -> Any:
    global _CACHE_TOTAL_BYTES
    if ttl <= 0:
        return value
    size = _estimate_size(value)
    with _CACHE_LOCK:
        _cache_remove_locked(key)
        _CACHE[key] = {"ts": time.monotonic(), "value": value, "size": size}
        _CACHE_TOTAL_BYTES += size
        for token in set(_key_tokens(key)):
            _CACHE_TOKEN_INDEX.setdefault(token, set()).add(key)
        _cache_evict_locked()
    return value


def _cache_get_copy(key: tuple[Any, ...]) -> Any | None:
    cached = _cache_get(key)
    return copy.deepcopy(cached) if cached is not None else None


def _cache_get_copy_with_ttl(key: tuple[Any, ...], ttl: int) -> Any | None:
    cached = _cache_get_with_ttl(key, ttl)
    return copy.deepcopy(cached) if cached is not None else None


def _cache_set_copy(key: tuple[Any, ...], value: Any) -> Any:
    _cache_set(key, copy.deepcopy(value))
    return value


def _cache_set_copy_with_ttl(key: tuple[Any, ...], value: Any, ttl: int) -> Any:
    _cache_set_with_ttl(key, copy.deepcopy(value), ttl)
    return value


def _cache_delete(key: tuple[Any, ...]) -> None:
    with _CACHE_LOCK:
        _cache_remove_locked(key)


def _auth_cache_ttl() -> int:
    # Keep revoked-session exposure bounded while still collapsing multi-endpoint
    # admin page loads behind the same access token.
    return min(_remote_cache_ttl(), _env_int(_runtime_env(), REMOTE_AUTH_CACHE_TTL_ENV, 60, min_value=0))


def clear_remote_query_cache() -> None:
    """DEPRECATED (BF-0515-cache-scoped-invalidation): use the targeted
    helpers `clear_user_cache_keys(user_id)` or `clear_feed_cache_keys()`
    instead.

    This function used to wipe ALL process caches (including other users'
    feed/sections/platforms snapshots), making cache hit rate ~0% in
    multi-user scenarios. Kept as alias to clear_feed_cache_keys() for any
    legacy caller that hasn't been migrated."""
    clear_feed_cache_keys()


# Cache key prefixes that hold feed-content data. Cleared together when items
# table changes (fetch_run, item visibility flips, etc.).
_FEED_CACHE_PREFIXES = frozenset({
    "admin_fetch_runs_result",
    "admin_overview_result",
    "events_result_30d_v3",
    "events_result_30d_v4",
    "events_result_30d_v5",  # P0-2: 内容缓存去 user 化后的当前版本
    "events_total_30d",
    "events_date_counts_30d",
    "events_highlights_date_counts_v1",
    "highlights_read_model_events",
    "feed_sections_result",
    "feed_sections_counts",
    "feed_platforms_result",
    "feed_platform_page_count",
    "info_read_model_platform_page",
    "info_read_model_section_category_page",
    "feed_total",
    "lingowhale_group_counts",
    "platform_counts",
    "platform_category_counts",
    "feed_category_count",
    "context_search_events_degraded",
    "context_search_events_total",
})
_FEED_LOCAL_READ_CACHE_PREFIXES = (
    "feed_events_",
    "feed_sections_",
    "feed_platforms_",
    "feed_items_",
)
_FEED_SNAPSHOT_KEY_PREFIXES = ("events:", "sections:")

_DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES = -480
_MAX_TIMEZONE_OFFSET_MINUTES = 14 * 60


def _timezone_offset_minutes(value: int | None) -> int:
    try:
        offset = int(value if value is not None else _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES)
    except (TypeError, ValueError):
        offset = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES
    return max(-_MAX_TIMEZONE_OFFSET_MINUTES, min(_MAX_TIMEZONE_OFFSET_MINUTES, offset))


def clear_feed_local_read_cache_files() -> int:
    """Remove feed-content disk read caches after visible feed data changes."""
    removed = 0
    try:
        for path in _LOCAL_READ_CACHE_DIR.glob("*.json"):
            if not any(path.name.startswith(prefix) for prefix in _FEED_LOCAL_READ_CACHE_PREFIXES):
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                pass
    except OSError:
        return removed
    return removed


def clear_feed_snapshot_rows(prefixes: tuple[str, ...] = _FEED_SNAPSHOT_KEY_PREFIXES) -> int:
    """Remove Supabase feed read-model snapshots after visible feed data changes."""
    if not prefixes:
        return 0
    conditions = " OR ".join(["snapshot_key LIKE %s"] * len(prefixes))
    params = tuple(f"{prefix}%" for prefix in prefixes)
    try:
        with connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {remote_schema()}.feed_snapshots WHERE {conditions}",
                params,
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        return 0


def clear_feed_cache_keys(*, clear_remote_snapshots: bool = False) -> int:
    """BF-0515-cache-scoped-invalidation: clear only feed-content cache
    entries. Called after fetch_run / new items / visibility changes —
    anything that changes what users SEE in their feed list. Does NOT touch
    auth or user-profile caches.

    Returns count removed (for logging / metrics)."""
    removed = 0
    with _CACHE_LOCK:
        # B3: 走倒排索引(prefix 即 key[0] token),O(命中数) 而非 O(全缓存)
        for prefix in _FEED_CACHE_PREFIXES:
            for key in list(_CACHE_TOKEN_INDEX.get(prefix, ())):
                if isinstance(key, tuple) and key and key[0] == prefix:
                    if _cache_remove_locked(key):
                        removed += 1
    removed += clear_feed_local_read_cache_files()
    if clear_remote_snapshots:
        removed += clear_feed_snapshot_rows()
    return removed


def _cache_key_mentions_item_id(key: tuple[Any, ...], item_id: str) -> bool:
    for elem in key:
        if str(elem) == item_id:
            return True
        if isinstance(elem, (tuple, list, set)) and any(str(value) == item_id for value in elem):
            return True
    return False


def clear_item_detail_cache_keys(item_id: str | int | None) -> int:
    """Clear item-detail caches for one item after ASR/detail fields change."""
    if item_id is None or item_id == "":
        return 0
    item_id_str = str(item_id)
    removed = 0
    with _CACHE_LOCK:
        # B3: item_id token 直查索引
        for key in list(_CACHE_TOKEN_INDEX.get(item_id_str, ())):
            if not isinstance(key, tuple) or not key:
                continue
            if key[0] not in {"feed_item_detail", "feed_items_detail_batch"}:
                continue
            if _cache_remove_locked(key):
                removed += 1
    return removed


def clear_user_cache_keys(user_id: str | int | None) -> int:
    """BF-0515-cache-scoped-invalidation: clear cache entries that mention
    this user_id (auth sessions, personalized feeds, profile, item status).
    Called after login / logout / profile update / status mutation.

    Other users' caches are NOT touched — multi-user safe.

    Returns count removed."""
    if user_id is None or user_id == "":
        return 0
    user_id_str = str(user_id)
    removed = 0
    with _CACHE_LOCK:
        # B3: user_id token 直查索引——原 O(全缓存) 扫描在全局锁下进行,
        # 每次点击/收藏都会触发,是缓存变大后的隐性卡点(BE-3)
        for key in list(_CACHE_TOKEN_INDEX.get(user_id_str, ())):
            if isinstance(key, tuple) and _cache_remove_locked(key):
                removed += 1
    return removed


# BF-0515-singleflight: in-process request coalescing.
# When N concurrent requests for the same cache_key all hit a cache miss,
# only the first one runs compute_fn; the other N-1 wait on a threading.Event
# and share the result. Prevents "thundering herd" against Supabase.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[tuple[Any, ...], dict[str, Any]] = {}
_SINGLEFLIGHT_TIMEOUT_SEC = 30  # match longest expected query (cold platforms ~15s)


def _singleflight_sync(key: tuple[Any, ...], compute_fn):
    """Run compute_fn at most once per key across concurrent threads.

    Caller is responsible for cache-get BEFORE invoking this (singleflight does
    not consult cache itself). Caller is also responsible for caching the result
    after this returns. Singleflight only deduplicates the COMPUTE path.

    On exception, all waiters get the same exception.
    """
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is not None:
            existing['waiters'] += 1
            holder = existing
            should_compute = False
        else:
            holder = {
                'event': threading.Event(),
                'result': None,
                'error': None,
                'waiters': 1,
            }
            _INFLIGHT[key] = holder
            should_compute = True

    if should_compute:
        try:
            holder['result'] = compute_fn()
        except BaseException as exc:
            holder['error'] = exc
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(key, None)
            holder['event'].set()
    else:
        if not holder['event'].wait(timeout=_SINGLEFLIGHT_TIMEOUT_SEC):
            # leader hung; do not block forever, fall back to fresh compute
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(key, None)
            return compute_fn()

    if holder['error'] is not None:
        raise holder['error']
    return holder['result']


def _rollback_safely(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _commit_safely(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if not callable(commit):
        return
    try:
        commit()
    except Exception:
        _rollback_safely(conn)


def _local_read_cache_path(name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.=-]+", "_", name).strip("._") or "snapshot"
    return _LOCAL_READ_CACHE_DIR / f"{safe}.json"


def _read_local_read_cache(name: str, *, max_age_sec: int | None = None) -> Any | None:
    path = _local_read_cache_path(name)
    try:
        stat = path.stat()
        max_age = _LOCAL_READ_CACHE_MAX_AGE_SEC if max_age_sec is None else max_age_sec
        if time.time() - stat.st_mtime > max_age:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("degraded") is True:
            return None
        return payload
    except Exception:
        return None


def _write_local_read_cache_async(name: str, payload: Any) -> None:
    def _write() -> None:
        try:
            _LOCAL_READ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = _local_read_cache_path(name)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


def _feed_snapshots_available(conn: Any, schema: str) -> bool:
    cache_key = ("feed_snapshots_available", schema)
    cached = _cache_get_with_ttl(cache_key, 300)
    if cached is not None:
        return bool(cached)
    try:
        row = conn.execute("select to_regclass(%s) as name", (f"{schema}.feed_snapshots",)).fetchone()
        available = bool(row and row.get("name"))
    except Exception:
        _rollback_safely(conn)
        available = False
    _cache_set_with_ttl(cache_key, available, 300)
    return available


def prewarm_events_categories() -> dict[str, Any]:
    """BF-0515-prewarm-events: warm fetch_events cache for default + each L1 category.

    Each pill the user clicks corresponds to a `categories=[X]` cache key.
    Without prewarm, first user to click each category pays the full cold
    cost (~1-4s). After prewarm, the cache is hot.

    Sequential to avoid overloading the connection pool. Total time ~10-25s
    for 14 calls (13 categories + default).
    """
    from category_taxonomy import ACTIVE_CATEGORY_IDS  # local import to avoid circular

    timings: list[dict[str, Any]] = []
    t_total = time.time()
    # 1 default (no categories filter)
    targets: list[list[str] | None] = [None]
    # + each single L1 category (matches what pill click sends)
    for cat in ACTIVE_CATEGORY_IDS:
        targets.append([cat])

    success = 0
    failed = 0
    for cats in targets:
        t0 = time.time()
        try:
            fetch_events(
                page=1,
                limit=20,
                user_id=None,
                public_only=True,
                min_github_stars=50,
                enabled=True,
                categories=cats,
            )
            timings.append({"cats": cats or "default", "ms": int((time.time() - t0) * 1000), "ok": True})
            success += 1
        except Exception as exc:
            timings.append({"cats": cats or "default", "ms": int((time.time() - t0) * 1000), "ok": False, "err": str(exc)[:80]})
            failed += 1
    return {
        "ok": failed == 0,
        "total_ms": int((time.time() - t_total) * 1000),
        "success": success,
        "failed": failed,
        "per_target": timings,
    }


def refresh_platforms_mv_if_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    """Refresh the platform MV at most once per process interval.

    Refreshing this MV is one of the most expensive remote-db operations. The
    platform/read prewarm path must not refresh it every cache cycle.
    """
    global _MV_REFRESH_LAST_ATTEMPT_AT
    min_interval = max(0, int(min_interval_sec))
    now = time.monotonic()
    with _MV_REFRESH_LOCK:
        age = now - _MV_REFRESH_LAST_ATTEMPT_AT if _MV_REFRESH_LAST_ATTEMPT_AT else None
        if age is not None and age < min_interval:
            return {
                "ok": True,
                "skipped": "recent_attempt",
                "age_sec": round(age, 1),
                "min_interval_sec": min_interval,
            }
        _MV_REFRESH_LAST_ATTEMPT_AT = now
    return refresh_platforms_mv()


def prewarm_platforms(
    *,
    refresh_mv: bool | None = None,
    refresh_min_interval_sec: int = 600,
    refresh_read_model: bool | None = None,
    refresh_read_model_min_interval_sec: int = 600,
    refresh_highlights_read_model: bool | None = None,
    refresh_highlights_read_model_min_interval_sec: int = 600,
) -> dict[str, Any]:
    """BF-0515-mv-pgcron: warm up the in-process result cache for /api/feed/platforms.

    Sequence:
      1. Optionally refresh the materialized view when explicitly requested.
      2. Call query_feed_sections / query_feed_platforms with anonymous params
         → populates result_cache for the 信息 tab default views.
      3. Result cache TTL is _remote_cache_ttl() (default 180s) — long enough
         that backend startup + next fetch_run cover the gap.

    Called from:
      - lifespan startup (background thread)
      - fetch.py finally block (background thread, after every fetch_run)
    """
    timings = {}
    t0 = time.time()
    if refresh_mv is None:
        refresh_mv = _truthy(_runtime_env().get(PREWARM_REFRESH_PLATFORMS_MV_ENV))
    if refresh_mv and _platform_mv_refresh_allowed():
        refresh_result = refresh_platforms_mv_if_stale(min_interval_sec=refresh_min_interval_sec)
        timings['mv_refresh_ms'] = int((time.time() - t0) * 1000)
        timings['mv_refresh_ok'] = refresh_result.get('ok', False)
        if refresh_result.get("skipped"):
            timings['mv_refresh_skipped_reason'] = refresh_result.get("skipped")
    elif refresh_mv:
        timings['mv_refresh_ms'] = 0
        timings['mv_refresh_ok'] = False
        timings['mv_refresh_skipped'] = True
        timings['mv_refresh_skipped_reason'] = "platform_mv_refresh_not_allowed"
    else:
        timings['mv_refresh_ms'] = 0
        timings['mv_refresh_ok'] = False
        timings['mv_refresh_skipped'] = True

    t1 = time.time()
    if refresh_read_model is None:
        env = _runtime_env()
        refresh_read_model = _info_read_model_enabled(env) and _truthy(env.get(INFO_READ_MODEL_REFRESH_ENV, "1"))
    if refresh_read_model:
        try:
            read_model_result = refresh_info_read_model_if_stale(
                min_interval_sec=refresh_read_model_min_interval_sec
            )
            timings['read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['read_model_refresh_ok'] = read_model_result.get('ok', False)
            if read_model_result.get("skipped"):
                timings['read_model_refresh_skipped_reason'] = read_model_result.get("skipped")
            if read_model_result.get("scope_items") is not None:
                timings['read_model_scope_items'] = read_model_result.get("scope_items")
        except Exception as exc:
            timings['read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['read_model_refresh_ok'] = False
            timings['read_model_refresh_error'] = str(exc)[:200]
    else:
        timings['read_model_refresh_ms'] = 0
        timings['read_model_refresh_ok'] = False
        timings['read_model_refresh_skipped'] = True

    t1 = time.time()
    if refresh_highlights_read_model is None:
        env = _runtime_env()
        refresh_highlights_read_model = (
            _highlights_read_model_enabled(env)
            and _truthy(env.get(HIGHLIGHTS_READ_MODEL_REFRESH_ENV, "1"))
        )
    if refresh_highlights_read_model:
        try:
            highlights_result = refresh_highlights_read_model_if_stale(
                min_interval_sec=refresh_highlights_read_model_min_interval_sec
            )
            timings['highlights_read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['highlights_read_model_refresh_ok'] = highlights_result.get('ok', False)
            if highlights_result.get("skipped"):
                timings['highlights_read_model_refresh_skipped_reason'] = highlights_result.get("skipped")
            if highlights_result.get("scope_items") is not None:
                timings['highlights_read_model_scope_items'] = highlights_result.get("scope_items")
        except Exception as exc:
            timings['highlights_read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['highlights_read_model_refresh_ok'] = False
            timings['highlights_read_model_refresh_error'] = str(exc)[:200]
    else:
        timings['highlights_read_model_refresh_ms'] = 0
        timings['highlights_read_model_refresh_ok'] = False
        timings['highlights_read_model_refresh_skipped'] = True

    t1 = time.time()
    try:
        query_feed_sections(
            per_category=50,
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=50,
        )
        timings['sections_query_ms'] = int((time.time() - t1) * 1000)
        timings['sections_query_ok'] = True
    except Exception as exc:
        timings['sections_query_ms'] = int((time.time() - t1) * 1000)
        timings['sections_query_ok'] = False
        timings['sections_query_error'] = str(exc)[:200]

    t1 = time.time()
    try:
        query_feed_platforms(
            per_platform=50,
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=50,
        )
        timings['query_ms'] = int((time.time() - t1) * 1000)
        timings['query_ok'] = True
    except Exception as exc:
        timings['query_ms'] = int((time.time() - t1) * 1000)
        timings['query_ok'] = False
        timings['query_error'] = str(exc)[:200]
    t2 = time.time()
    if _info_read_model_enabled():
        try:
            env = _runtime_env()
            page_result = prewarm_info_read_model_pages(
                max_scopes=_env_int(
                    env,
                    INFO_READ_MODEL_PREWARM_SCOPES_ENV,
                    INFO_READ_MODEL_PREWARM_SCOPES_DEFAULT,
                    min_value=1,
                ),
            )
            timings['read_model_page_prewarm_ms'] = int((time.time() - t2) * 1000)
            timings['read_model_page_prewarm_ok'] = page_result.get('ok', False)
            if page_result.get("pages") is not None:
                timings['read_model_page_prewarm_pages'] = page_result.get("pages")
            if page_result.get("items") is not None:
                timings['read_model_page_prewarm_items'] = page_result.get("items")
        except Exception as exc:
            timings['read_model_page_prewarm_ms'] = int((time.time() - t2) * 1000)
            timings['read_model_page_prewarm_ok'] = False
            timings['read_model_page_prewarm_error'] = str(exc)[:200]
    else:
        timings['read_model_page_prewarm_ms'] = 0
        timings['read_model_page_prewarm_skipped'] = True
    timings['total_ms'] = int((time.time() - t0) * 1000)
    return timings


def refresh_platforms_mv() -> dict[str, Any]:
    """BF-0515-mv-pgcron: refresh mv_items_top_per_platform.

    Called from fetch.py finally block (after every fetch_run completes).
    Tries CONCURRENTLY first (non-blocking, requires unique index — we have one).
    By default it never falls back to plain REFRESH, because plain refresh takes
    an AccessExclusive lock on the materialized view and can blank the feed while
    readers wait. Set INFO2ACTION_ALLOW_BLOCKING_MV_REFRESH=1 only for manual
    first-build/admin maintenance windows.
    Returns timing + row count for logging.
    """
    schema = remote_schema()
    t0 = time.time()
    with connect() as conn:
        old_autocommit = getattr(conn, "autocommit", None)
        try:
            try:
                conn.autocommit = True
            except Exception:
                pass
            try:
                conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {schema}.mv_items_top_per_platform")
                mode = "concurrent"
            except Exception as exc:
                _rollback_safely(conn)
                if not _truthy(_runtime_env().get(ALLOW_BLOCKING_MV_REFRESH_ENV)):
                    return {
                        "ok": False,
                        "mode": "concurrent",
                        "blocking_skipped": True,
                        "error": str(exc)[:200],
                        "elapsed_ms": int((time.time() - t0) * 1000),
                    }
                try:
                    conn.execute(f"REFRESH MATERIALIZED VIEW {schema}.mv_items_top_per_platform")
                    mode = "blocking"
                except Exception as exc:
                    _rollback_safely(conn)
                    return {"ok": False, "error": str(exc)[:200]}
            row = conn.execute(f"SELECT count(*) AS n FROM {schema}.mv_items_top_per_platform").fetchone()
        finally:
            try:
                if old_autocommit is not None:
                    conn.autocommit = old_autocommit
            except Exception:
                pass
    # invalidate the cached "platforms response" so next request re-aggregates
    with _CACHE_LOCK:
        for prefix in (
            "feed_platforms_result",
            "feed_sections_result",
            "feed_sections_counts",
            "platform_counts",
            "platform_category_counts",
        ):
            for k in list(_CACHE_TOKEN_INDEX.get(prefix, ())):
                if isinstance(k, tuple) and k and k[0] == prefix:
                    _cache_remove_locked(k)
    return {"ok": True, "mode": mode, "rows": int(row.get("n") or 0), "elapsed_ms": int((time.time() - t0) * 1000)}


def _info_read_model_freshness(
    conn: Any,
    schema: str,
    *,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    active = _info_read_model_active_version(conn, schema)
    active_meta = _json_value(active.get("meta_json")) if active else {}
    if not isinstance(active_meta, dict):
        active_meta = {}
    active_sort_policy = active_meta.get("sort_policy") if active else None
    sort_policy_stale = active_sort_policy != INFO_READ_MODEL_SORT_POLICY
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=int(min_github_stars),
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    latest = conn.execute(
        f"""SELECT i.fetched_at AS latest_fetched_at
              FROM {schema}.items i
              {_where_sql(where)}
             ORDER BY i.fetched_at DESC NULLS LAST
             LIMIT 1""",
        params,
    ).fetchone()
    active_max = active.get("max_fetched_at") if active else None
    latest_max = (latest or {}).get("latest_fetched_at")
    result = {
        "active_version_id": str(active.get("version_id")) if active and active.get("version_id") else None,
        "active_generated_at": _timestamp_value(active.get("generated_at")) if active else None,
        "active_max_fetched_at": _timestamp_value(active_max),
        "latest_max_fetched_at": _timestamp_value(latest_max),
        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
        "active_sort_policy": active_sort_policy,
        "sort_policy_stale": bool(sort_policy_stale),
        "data_stale": bool(
            latest_max
            and (
                not active_max
                or sort_key(latest_max) > sort_key(active_max)
            )
        ),
    }
    result["stale"] = bool(result["sort_policy_stale"] or result["data_stale"])
    return result


def info_read_model_freshness_remote(
    *,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Read-only freshness probe for the Info tab read model."""
    enabled = _info_read_model_enabled()
    result: dict[str, Any] = {
        "enabled": enabled,
        "read_model": "info_platforms_v1",
        "state_key": INFO_READ_MODEL_STATE_KEY,
        "data_backend": feed_read_backend(),
        "incremental_enabled": _info_read_model_incremental_enabled(),
        "live_overlay_enabled": _info_live_overlay_enabled(),
    }
    if not enabled:
        return result
    schema = remote_schema()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            result.update(
                _info_read_model_freshness(
                    conn,
                    schema=schema,
                    min_github_stars=min_github_stars,
                )
            )
    except Exception as exc:
        raise RemoteDBError(f"info read model freshness probe failed: {exc}") from exc
    return result


def _info_read_model_incremental_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), INFO_READ_MODEL_INCREMENTAL_ENV, default=True)


def _prune_info_read_model_versions(
    conn: Any,
    *,
    schema: str,
    retain_complete_versions: int = INFO_READ_MODEL_RETAIN_COMPLETE_VERSIONS,
) -> None:
    """Prune stale Info read model versions without deleting the active version."""
    safe_retain = max(1, int(retain_complete_versions or 1))
    conn.execute(
        f"""WITH protected_versions AS (
               SELECT active_version_id AS version_id
                 FROM {schema}.info_read_model_state
                WHERE active_version_id IS NOT NULL
               UNION
               SELECT version_id
                 FROM (
                   SELECT version_id
                     FROM {schema}.info_read_model_versions
                    WHERE status = 'complete'
                    ORDER BY completed_at DESC NULLS LAST,
                             generated_at DESC NULLS LAST
                    LIMIT %(retain_complete_versions)s
                 ) recent_complete
             )
             DELETE FROM {schema}.info_read_model_versions v
              WHERE NOT EXISTS (
                    SELECT 1
                      FROM protected_versions p
                     WHERE p.version_id = v.version_id
                )
                AND (
                     v.status = 'complete'
                  OR v.generated_at < now() - interval '{INFO_READ_MODEL_PRUNE_TRANSIENT_AGE_HOURS} hours'
                )""",
        {"retain_complete_versions": safe_retain},
    )


def _info_read_model_scope_rows_select(source_table: str) -> str:
    return f"""WITH raw_scope_rows AS (
                      SELECT i.platform, 'all'::text AS dimension, ''::text AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                      UNION ALL
                      SELECT i.platform, 'source'::text AS dimension, COALESCE(i.source, '') AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                       WHERE COALESCE(i.source, '') != ''
                      UNION ALL
                      SELECT i.platform, 'group'::text AS dimension,
                             CASE
                               WHEN COALESCE(i.detail_json ->> 'group', '') IN ('', '未分组', '独立频道')
                               THEN '未分组'
                               ELSE i.detail_json ->> 'group'
                             END AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                       WHERE i.platform = 'lingowhale'
                      UNION ALL
                      SELECT i.platform, 'group_source'::text AS dimension,
                             (CASE
                                WHEN COALESCE(i.detail_json ->> 'group', '') IN ('', '未分组', '独立频道')
                                THEN '未分组'
                                ELSE i.detail_json ->> 'group'
                              END) || %(compound_separator)s || COALESCE(i.source, '') AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                       WHERE i.platform = 'lingowhale'
                         AND COALESCE(i.source, '') != ''
                      UNION ALL
                      SELECT i.platform, 'category'::text AS dimension, cat.value AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                        CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
                       WHERE i.ai_categories IS NOT NULL
                      UNION ALL
                      SELECT i.platform, 'category'::text AS dimension,
                             CASE
                               WHEN i.ai_category IS NOT NULL AND i.ai_category != 'other'
                               THEN i.ai_category
                               ELSE %(uncategorized)s
                             END AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                       WHERE i.ai_categories IS NULL
                      UNION ALL
                      SELECT '_all'::text AS platform, 'section_category'::text AS dimension,
                             i.section_category AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                      UNION ALL
                      SELECT '_all'::text AS platform, 'section_subcategory'::text AS dimension,
                             i.section_category || %(compound_separator)s || subcat.value AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                        CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
                       WHERE i.ai_subcategories IS NOT NULL
                   )
            SELECT 'platform=' || platform || '|dimension=' || dimension || '|value=' || value AS scope_key,
                   platform, dimension, value, item_id, sort_at, fetched_at, relevance_score,
                   sort_at AS rank_at
              FROM raw_scope_rows"""


def _info_scope_item_order_sql(alias: str = "si") -> str:
    return (
        f"{alias}.sort_at DESC NULLS LAST, "
        f"{alias}.fetched_at DESC NULLS LAST, "
        f"{alias}.relevance_score DESC NULLS LAST, "
        f"{alias}.item_id DESC"
    )


def migrate_info_read_model_sort_policy() -> dict[str, Any]:
    """Rerank the active info read model when only the sort policy changed.

    This avoids a full card_json rebuild for legacy versions whose cards are
    still usable but whose scope ranks were generated with an older policy.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "read_active_version"
            step_t0 = time.time()
            active = _info_read_model_active_version(conn, schema)
            if not active or not active.get("version_id"):
                return {"ok": False, "skipped": "no_active_version"}
            active_meta = _json_value(active.get("meta_json"))
            if isinstance(active_meta, dict) and active_meta.get("sort_policy") == INFO_READ_MODEL_SORT_POLICY:
                return {
                    "ok": True,
                    "skipped": "sort_policy_current",
                    "version_id": str(active["version_id"]),
                }
            active_version_id = str(active["version_id"])
            _record_step(current_step, step_t0)

            current_step = "normalize_card_sort_at"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_card_items
                       SET sort_at = COALESCE(published_at, fetched_at, sort_at)
                     WHERE version_id = %(active_version_id)s::uuid
                       AND sort_at IS DISTINCT FROM COALESCE(published_at, fetched_at, sort_at)""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_reranked_scope_items"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_reranked_scope_items")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_reranked_scope_items ON COMMIT DROP AS
                    WITH source_rows AS (
                      SELECT si.scope_key,
                             si.item_id,
                             COALESCE(ci.sort_at, ci.published_at, ci.fetched_at, si.sort_at, si.fetched_at) AS sort_at,
                             COALESCE(ci.fetched_at, si.fetched_at) AS fetched_at,
                             COALESCE(ci.relevance_score, si.relevance_score) AS relevance_score
                        FROM {schema}.info_scope_items si
                        JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                         AND ci.item_id = si.item_id
                       WHERE si.version_id = %(active_version_id)s::uuid
                    )
                    SELECT scope_key,
                           row_number() OVER (
                             PARTITION BY scope_key
                             ORDER BY sort_at DESC NULLS LAST,
                                      fetched_at DESC NULLS LAST,
                                      relevance_score DESC NULLS LAST,
                                      item_id DESC
                           )::integer AS rank,
                           item_id,
                           sort_at,
                           fetched_at,
                           relevance_score
                      FROM source_rows""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_reranked_scope_items")
            reranked_row = conn.execute(
                "SELECT count(*) AS n FROM pg_temp.info_read_model_reranked_scope_items"
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "update_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""WITH agg AS (
                       SELECT scope_key,
                              count(*)::integer AS total_count,
                              max(sort_at) AS max_sort_at
                         FROM pg_temp.info_read_model_reranked_scope_items
                        GROUP BY scope_key
                     )
                     UPDATE {schema}.info_scopes sc
                        SET total_count = agg.total_count,
                            max_sort_at = agg.max_sort_at,
                            generated_at = now()
                       FROM agg
                      WHERE sc.version_id = %(active_version_id)s::uuid
                        AND sc.scope_key = agg.scope_key""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "replace_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"DELETE FROM {schema}.info_scope_items WHERE version_id = %(active_version_id)s::uuid",
                {"active_version_id": active_version_id},
            )
            conn.execute(
                f"""INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid, scope_key, rank, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM pg_temp.info_read_model_reranked_scope_items""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "mark_version_policy"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'sort_policy', %(sort_policy)s::text,
                             'sort_policy_migration', 'rerank_active',
                             'sort_policy_migrated_at', now()
                           ),
                           completed_at = COALESCE(completed_at, now())
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    "active_version_id": active_version_id,
                    "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                },
            )
            conn.execute(
                f"""UPDATE {schema}.info_read_model_state
                       SET updated_at = now()
                     WHERE key = %(state_key)s
                       AND active_version_id = %(active_version_id)s::uuid""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "active_version_id": active_version_id,
                },
            )
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"info read model sort policy migration failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "sort_policy_migration",
        "version_id": active_version_id,
        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
        "scope_items": int((reranked_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_info_read_model_delta_in_place(
    *,
    sample_limit: int = 200,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Apply new info items to the active read model without cloning all rows."""
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=safe_min_github_stars,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where.append("i.fetched_at > %(active_max_fetched_at)s::timestamptz")
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    delta_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    current_step = "read_active_version"
    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _info_read_model_active_version(conn, schema)
    if not active or not active.get("version_id") or not active.get("max_fetched_at"):
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_meta = _json_value(active.get("meta_json"))
    if not isinstance(active_meta, dict) or active_meta.get("sort_policy") != INFO_READ_MODEL_SORT_POLICY:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    active_max_fetched_at = active["max_fetched_at"]
    _record_step(current_step, step_t0)

    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "materialize_delta"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta ON COMMIT DROP AS
                    {delta_select_sql}""",
                {**params, "active_max_fetched_at": active_max_fetched_at},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta")
            delta_row = conn.execute(
                "SELECT count(*) AS n, max(fetched_at) AS max_fetched_at FROM pg_temp.info_read_model_delta"
            ).fetchone()
            delta_count = int((delta_row or {}).get("n") or 0)
            if delta_count <= 0:
                conn.commit()
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": _timestamp_value(active_max_fetched_at),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            delta_max_fetched_at = (delta_row or {}).get("max_fetched_at")
            _record_step(current_step, step_t0)

            current_step = "upsert_delta_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', i.ai_summary,
                              'ai_category', i.ai_category,
                              'ai_keywords', i.ai_keywords,
                              'ai_categories', i.ai_categories,
                              'ai_subcategories', i.ai_subcategories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_delta i
                     ON CONFLICT (version_id, item_id) DO UPDATE SET
                       card_json = excluded.card_json,
                       platform = excluded.platform,
                       source = excluded.source,
                       sort_at = excluded.sort_at,
                       fetched_at = excluded.fetched_at,
                       published_at = excluded.published_at,
                       relevance_score = excluded.relevance_score
                     WHERE {schema}.info_card_items.card_json IS DISTINCT FROM excluded.card_json
                        OR {schema}.info_card_items.platform IS DISTINCT FROM excluded.platform
                        OR {schema}.info_card_items.source IS DISTINCT FROM excluded.source
                        OR {schema}.info_card_items.sort_at IS DISTINCT FROM excluded.sort_at
                        OR {schema}.info_card_items.fetched_at IS DISTINCT FROM excluded.fetched_at
                        OR {schema}.info_card_items.published_at IS DISTINCT FROM excluded.published_at
                        OR {schema}.info_card_items.relevance_score IS DISTINCT FROM excluded.relevance_score""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta_scope_rows ON COMMIT DROP AS
                    {_info_read_model_scope_rows_select("pg_temp.info_read_model_delta")}""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_existing_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_existing_delta_scope_rows ON COMMIT DROP AS
                    SELECT sc.scope_key, sc.platform, sc.dimension, sc.value,
                           si.item_id, si.sort_at, si.fetched_at, si.relevance_score,
                           si.sort_at AS rank_at
                      FROM {schema}.info_scope_items si
                      JOIN {schema}.info_scopes sc
                        ON sc.version_id = si.version_id
                       AND sc.scope_key = si.scope_key
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta d
                              WHERE d.id::text = si.item_id
                           )""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_existing_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scopes")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_delta_scope_rows
                    UNION
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_existing_delta_scope_rows"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "delete_obsolete_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""DELETE FROM {schema}.info_scope_items si
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta d
                              WHERE d.id::text = si.item_id
                           )
                       AND NOT EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta_scope_rows dsr
                              WHERE dsr.scope_key = si.scope_key
                                AND dsr.item_id = si.item_id
                           )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "update_existing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_scope_items si
                       SET sort_at = dsr.sort_at,
                           fetched_at = dsr.fetched_at,
                           relevance_score = dsr.relevance_score
                      FROM pg_temp.info_read_model_delta_scope_rows dsr
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND si.scope_key = dsr.scope_key
                       AND si.item_id = dsr.item_id
                       AND (
                            si.sort_at IS DISTINCT FROM dsr.sort_at
                         OR si.fetched_at IS DISTINCT FROM dsr.fetched_at
                         OR si.relevance_score IS DISTINCT FROM dsr.relevance_score
                       )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_missing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH missing AS (
                       SELECT dsr.scope_key, dsr.item_id, dsr.sort_at, dsr.fetched_at,
                              dsr.relevance_score, dsr.rank_at
                         FROM pg_temp.info_read_model_delta_scope_rows dsr
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM {schema}.info_scope_items si
                                 WHERE si.version_id = %(active_version_id)s::uuid
                                   AND si.scope_key = dsr.scope_key
                                   AND si.item_id = dsr.item_id
                              )
                     ),
                     scope_max_rank AS (
                       SELECT si.scope_key, max(si.rank) AS max_rank
                         FROM {schema}.info_scope_items si
                        WHERE si.version_id = %(active_version_id)s::uuid
                          AND EXISTS (
                                SELECT 1
                                  FROM pg_temp.info_read_model_affected_scopes a
                                 WHERE a.scope_key = si.scope_key
                              )
                        GROUP BY si.scope_key
                     ),
                     ranked AS (
                       SELECT m.scope_key, m.item_id, m.sort_at, m.fetched_at,
                              m.relevance_score,
                              COALESCE(smr.max_rank, 0) + row_number() OVER (
                                PARTITION BY m.scope_key
                                ORDER BY m.rank_at DESC NULLS LAST,
                                         m.fetched_at DESC NULLS LAST,
                                         m.relevance_score DESC NULLS LAST,
                                         m.item_id DESC
                              ) AS append_rank
                         FROM missing m
                         LEFT JOIN scope_max_rank smr
                           ON smr.scope_key = m.scope_key
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid, scope_key, append_rank::integer,
                            item_id, sort_at, fetched_at, relevance_score
                       FROM ranked""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "upsert_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_meta")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_delta_scope_meta ON COMMIT DROP AS
                   SELECT scope_key, max(platform) AS platform, max(dimension) AS dimension,
                          max(value) AS value
                     FROM pg_temp.info_read_model_delta_scope_rows
                    GROUP BY scope_key"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_meta")
            conn.execute(
                f"""WITH affected_scope_stats AS (
                       SELECT a.scope_key,
                              COALESCE(max(dsm.platform), max(sc.platform)) AS platform,
                              COALESCE(max(dsm.dimension), max(sc.dimension)) AS dimension,
                              COALESCE(max(dsm.value), max(sc.value), '') AS value,
                              count(si.item_id)::integer AS total_count,
                              max(si.sort_at) AS max_sort_at
                         FROM pg_temp.info_read_model_affected_scopes a
                         LEFT JOIN {schema}.info_scopes sc
                           ON sc.version_id = %(active_version_id)s::uuid
                          AND sc.scope_key = a.scope_key
                         LEFT JOIN pg_temp.info_read_model_delta_scope_meta dsm
                           ON dsm.scope_key = a.scope_key
                         LEFT JOIN {schema}.info_scope_items si
                           ON si.version_id = %(active_version_id)s::uuid
                          AND si.scope_key = a.scope_key
                        GROUP BY a.scope_key
                     )
                     INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            platform,
                            dimension,
                            value,
                            total_count,
                            max_sort_at,
                            now()
                       FROM affected_scope_stats
                      WHERE total_count > 0
                     ON CONFLICT (version_id, scope_key) DO UPDATE SET
                       platform = excluded.platform,
                       dimension = excluded.dimension,
                       value = excluded.value,
                       total_count = excluded.total_count,
                       max_sort_at = excluded.max_sort_at,
                       generated_at = excluded.generated_at""",
                {"active_version_id": active_version_id},
            )
            conn.execute(
                f"""DELETE FROM {schema}.info_scopes sc
                      USING pg_temp.info_read_model_affected_scopes a
                     WHERE sc.version_id = %(active_version_id)s::uuid
                       AND sc.scope_key = a.scope_key
                       AND NOT EXISTS (
                             SELECT 1
                               FROM {schema}.info_scope_items si
                              WHERE si.version_id = sc.version_id
                                AND si.scope_key = sc.scope_key
                           )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "update_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET max_fetched_at = GREATEST(
                             COALESCE(max_fetched_at, '-infinity'::timestamptz),
                             %(delta_max_fetched_at)s::timestamptz
                           ),
                           completed_at = now(),
                           meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'sort_policy', %(sort_policy)s::text,
                             'last_delta_mode', 'in_place',
                             'last_delta_at', now()
                           )
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    "active_version_id": active_version_id,
                    "delta_max_fetched_at": delta_max_fetched_at,
                    "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                },
            )
            conn.execute(
                f"""UPDATE {schema}.info_read_model_state
                       SET updated_at = now()
                     WHERE key = %(state_key)s
                       AND active_version_id = %(active_version_id)s::uuid""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "active_version_id": active_version_id,
                },
            )
            _record_step(current_step, step_t0)

            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"info read model in-place delta refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "delta_in_place",
        "version_id": active_version_id,
        "delta_items": delta_count,
        "active_max_fetched_at": _timestamp_value(delta_max_fetched_at),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_info_read_model_if_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    global _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    min_interval = max(0, int(min_interval_sec))
    now = time.monotonic()
    with _INFO_READ_MODEL_REFRESH_LOCK:
        age = now - _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT if _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT else None
        if age is not None and age < min_interval:
            return {
                "ok": True,
                "skipped": "recent_attempt",
                "age_sec": round(age, 1),
                "min_interval_sec": min_interval,
            }
        _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = now
    schema = remote_schema()
    with connect() as conn:
        _set_short_statement_timeout(conn, 15000)
        freshness = _info_read_model_freshness(conn, schema=schema)
    if not freshness.get("stale"):
        return {
            "ok": True,
            "skipped": "data_fresh",
            **freshness,
        }
    if freshness.get("sort_policy_stale"):
        migration_result = migrate_info_read_model_sort_policy()
        if freshness.get("data_stale") and _info_read_model_incremental_enabled():
            incremental_result = refresh_info_read_model_delta_in_place()
            incremental_result["sort_policy_migration"] = migration_result
            return incremental_result
        return migration_result
    if _info_read_model_incremental_enabled():
        return refresh_info_read_model_delta_in_place()
    return refresh_info_read_model()


def refresh_info_read_model_incremental(
    *,
    sample_limit: int = 200,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Create a new complete info read-model version by re-ranking only delta scopes.

    The reader contract stays simple: every served page still points at a complete
    version. The builder avoids re-scanning historical items; it clones the active
    projection, materializes items newer than the active max, and re-ranks only the
    scopes touched by that delta.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=safe_min_github_stars,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where.append("i.fetched_at > %(active_max_fetched_at)s::timestamptz")
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    delta_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    active_version_id: str | None = None
    active_max_fetched_at: Any = None
    delta_count = 0
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    current_step = "read_active_version"
    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _info_read_model_active_version(conn, schema)
    if not active or not active.get("version_id") or not active.get("max_fetched_at"):
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_meta = _json_value(active.get("meta_json"))
    if not isinstance(active_meta, dict) or active_meta.get("sort_policy") != INFO_READ_MODEL_SORT_POLICY:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    active_max_fetched_at = active["max_fetched_at"]
    _record_step(current_step, step_t0)

    with connect() as conn:
        # BF-0706-4: 单飞锁 —— 已有重建在跑就跳过,避免并发叠加压崩 compute。
        got_lock = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (_INFO_READ_MODEL_BUILD_LOCK_KEY,)
        ).fetchone()["locked"]
        if not got_lock:
            return {"ok": True, "skipped": "build_in_progress"}
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "create_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_versions (
                       version_id, status, generated_at, sample_limit, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(sample_limit)s,
                       jsonb_build_object(
                         'min_github_stars', %(min_github_stars)s::integer,
                         'mode', 'incremental',
                         'parent_version_id', %(active_version_id)s::text,
                         'from_fetched_at', %(active_max_fetched_at)s::text,
                         'sort_policy', %(sort_policy)s::text
                       )
                     )""",
                {
                    "version_id": version_id,
                    "sample_limit": safe_sample_limit,
                    "min_github_stars": safe_min_github_stars,
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": active_max_fetched_at,
                    "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                },
            )
            conn.commit()
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            _record_step(current_step, step_t0)

            current_step = "materialize_delta"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta ON COMMIT DROP AS
                    {delta_select_sql}""",
                {**params, "active_max_fetched_at": active_max_fetched_at},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta")
            delta_row = conn.execute(
                "SELECT count(*) AS n, max(fetched_at) AS max_fetched_at FROM pg_temp.info_read_model_delta"
            ).fetchone()
            delta_count = int((delta_row or {}).get("n") or 0)
            if delta_count <= 0:
                conn.execute(
                    f"DELETE FROM {schema}.info_read_model_versions WHERE version_id = %(version_id)s::uuid",
                    {"version_id": version_id},
                )
                conn.commit()
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": _timestamp_value(active_max_fetched_at),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            delta_max_fetched_at = (delta_row or {}).get("max_fetched_at")
            _record_step(current_step, step_t0)

            current_step = "clone_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, ci.item_id, ci.card_json,
                            ci.platform, ci.source, ci.sort_at, ci.fetched_at,
                            ci.published_at, ci.relevance_score
                       FROM {schema}.info_card_items ci
                      WHERE ci.version_id = %(active_version_id)s::uuid""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_delta_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', i.ai_summary,
                              'ai_category', i.ai_category,
                              'ai_keywords', i.ai_keywords,
                              'ai_categories', i.ai_categories,
                              'ai_subcategories', i.ai_subcategories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_delta i
                     ON CONFLICT (version_id, item_id) DO UPDATE SET
                       card_json = excluded.card_json,
                       platform = excluded.platform,
                       source = excluded.source,
                       sort_at = excluded.sort_at,
                       fetched_at = excluded.fetched_at,
                       published_at = excluded.published_at,
                       relevance_score = excluded.relevance_score""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta_scope_rows ON COMMIT DROP AS
                    {_info_read_model_scope_rows_select("pg_temp.info_read_model_delta")}""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scopes")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_delta_scope_rows"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "copy_unaffected_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid, sc.scope_key, sc.platform,
                            sc.dimension, sc.value, sc.total_count,
                            sc.max_sort_at, now()
                       FROM {schema}.info_scopes sc
                      WHERE sc.version_id = %(active_version_id)s::uuid
                        AND NOT EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                         WHERE a.scope_key = sc.scope_key)""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            conn.execute(
                f"""INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id,
                       sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, si.scope_key, si.rank,
                            si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                       FROM {schema}.info_scope_items si
                      WHERE si.version_id = %(active_version_id)s::uuid
                        AND NOT EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                         WHERE a.scope_key = si.scope_key)""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_affected_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_affected_scope_rows ON COMMIT DROP AS
                    SELECT sc.scope_key, sc.platform, sc.dimension, sc.value,
                           si.item_id, si.sort_at, si.fetched_at, si.relevance_score,
                           si.sort_at AS rank_at
                      FROM {schema}.info_scope_items si
                      JOIN {schema}.info_scopes sc
                        ON sc.version_id = si.version_id
                       AND sc.scope_key = si.scope_key
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                    WHERE a.scope_key = si.scope_key)
                    UNION ALL
                    SELECT scope_key, platform, dimension, value, item_id,
                           sort_at, fetched_at, relevance_score, rank_at
                      FROM pg_temp.info_read_model_delta_scope_rows""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scope_rows")
            _record_step(current_step, step_t0)

            current_step = "insert_affected_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""WITH deduped AS (
                       SELECT *,
                              row_number() OVER (
                                PARTITION BY scope_key, item_id
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS item_rn
                         FROM pg_temp.info_read_model_affected_scope_rows
                     )
                     INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            max(platform) AS platform,
                            max(dimension) AS dimension,
                            max(value) AS value,
                            count(*)::integer,
                            max(rank_at),
                            now()
                       FROM deduped
                      WHERE item_rn = 1
                      GROUP BY scope_key""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_affected_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH deduped AS (
                       SELECT *,
                              row_number() OVER (
                                PARTITION BY scope_key, item_id
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS item_rn
                         FROM pg_temp.info_read_model_affected_scope_rows
                     ),
                     ranked AS (
                       SELECT scope_key, item_id, sort_at, fetched_at, relevance_score,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS rn
                         FROM deduped
                        WHERE item_rn = 1
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, scope_key, rn::integer, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM ranked""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "complete_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_fetched_at = GREATEST(
                             %(active_max_fetched_at)s::timestamptz,
                             %(delta_max_fetched_at)s::timestamptz
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {
                    "version_id": version_id,
                    "active_max_fetched_at": active_max_fetched_at,
                    "delta_max_fetched_at": delta_max_fetched_at,
                },
            )
            _record_step(current_step, step_t0)

            current_step = "swap_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                {"state_key": INFO_READ_MODEL_STATE_KEY, "version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "count_rows"
            step_t0 = time.time()
            card_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_card_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            scope_item_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_scope_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            if current_step != "read_active_version":
                error_message = f"{current_step}: {exc}"
                try:
                    conn.execute(
                        f"""UPDATE {schema}.info_read_model_versions
                               SET status = 'error',
                                   error_message = %(error_message)s,
                                   completed_at = now()
                             WHERE version_id = %(version_id)s::uuid""",
                        {"version_id": version_id, "error_message": error_message[:500]},
                    )
                    conn.commit()
                except Exception:
                    _rollback_safely(conn)
            raise RemoteDBError(f"info read model incremental refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "incremental",
        "version_id": version_id,
        "parent_version_id": active_version_id,
        "delta_items": delta_count,
        "card_items": int((card_row or {}).get("n") or 0),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_info_read_model(*, sample_limit: int = 200, min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS) -> dict[str, Any]:
    """Build a versioned server read model for the 信息 tab platform view.

    The read model is intentionally version-swapped: readers keep using the
    previous complete version while this function builds a new one.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=safe_min_github_stars,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    eligible_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    with connect() as conn:
        # BF-0706-4: 单飞锁 —— 已有重建在跑就跳过,避免并发叠加压崩 compute。
        got_lock = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (_INFO_READ_MODEL_BUILD_LOCK_KEY,)
        ).fetchone()["locked"]
        if not got_lock:
            return {"ok": True, "skipped": "build_in_progress"}
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "create_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_versions (
                       version_id, status, generated_at, sample_limit, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(sample_limit)s,
                       %(meta_json)s::jsonb
                     )""",
                {
                    "version_id": version_id,
                    "sample_limit": safe_sample_limit,
                    "meta_json": json.dumps({
                        "min_github_stars": safe_min_github_stars,
                        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                    }),
                },
            )
            conn.commit()
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            _record_step(current_step, step_t0)
            current_step = "materialize_eligible"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_eligible")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_eligible ON COMMIT DROP AS
                    {eligible_select_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.info_read_model_eligible")
            _record_step(current_step, step_t0)
            current_step = "insert_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', i.ai_summary,
                              'ai_category', i.ai_category,
                              'ai_keywords', i.ai_keywords,
                              'ai_categories', i.ai_categories,
                              'ai_subcategories', i.ai_subcategories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_eligible i""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "materialize_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_scope_rows ON COMMIT DROP AS
                    WITH raw_scope_rows AS (
                              SELECT i.platform, 'all'::text AS dimension, ''::text AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                              UNION ALL
                              SELECT i.platform, 'source'::text AS dimension, COALESCE(i.source, '') AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                               WHERE COALESCE(i.source, '') != ''
                              UNION ALL
                              SELECT i.platform, 'group'::text AS dimension,
                                     CASE
                                       WHEN COALESCE(i.detail_json ->> 'group', '') IN ('', '未分组', '独立频道')
                                       THEN '未分组'
                                       ELSE i.detail_json ->> 'group'
                                     END AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                               WHERE i.platform = 'lingowhale'
                              UNION ALL
                              SELECT i.platform, 'group_source'::text AS dimension,
                                     (CASE
                                        WHEN COALESCE(i.detail_json ->> 'group', '') IN ('', '未分组', '独立频道')
                                        THEN '未分组'
                                        ELSE i.detail_json ->> 'group'
                                      END) || %(compound_separator)s || COALESCE(i.source, '') AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                               WHERE i.platform = 'lingowhale'
                                 AND COALESCE(i.source, '') != ''
                              UNION ALL
                              SELECT i.platform, 'category'::text AS dimension, cat.value AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                                CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
                               WHERE i.ai_categories IS NOT NULL
                              UNION ALL
                              SELECT i.platform, 'category'::text AS dimension,
                                     CASE
                                       WHEN i.ai_category IS NOT NULL AND i.ai_category != 'other'
                                       THEN i.ai_category
                                       ELSE %(uncategorized)s
                                     END AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                               WHERE i.ai_categories IS NULL
                              UNION ALL
                              SELECT '_all'::text AS platform, 'section_category'::text AS dimension,
                                     i.section_category AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                              UNION ALL
                              SELECT '_all'::text AS platform, 'section_subcategory'::text AS dimension,
                                     i.section_category || %(compound_separator)s || subcat.value AS value,
                                     i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                                FROM pg_temp.info_read_model_eligible i
                                CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
                               WHERE i.ai_subcategories IS NOT NULL
                            )
                    SELECT 'platform=' || platform || '|dimension=' || dimension || '|value=' || value AS scope_key,
                           platform, dimension, value, item_id, sort_at, fetched_at, relevance_score,
                           sort_at AS rank_at
                      FROM raw_scope_rows""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_scope_rows")
            _record_step(current_step, step_t0)
            current_step = "insert_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            platform,
                            dimension,
                            value,
                            count(*)::integer,
                            max(rank_at),
                            now()
                       FROM pg_temp.info_read_model_scope_rows
                      GROUP BY scope_key, platform, dimension, value""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "insert_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH ranked AS (
                       SELECT scope_key, item_id, sort_at, fetched_at, relevance_score,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS rn
                         FROM pg_temp.info_read_model_scope_rows
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, scope_key, rn::integer, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM ranked""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "complete_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_fetched_at = (
                             SELECT max(fetched_at)
                               FROM {schema}.info_card_items
                              WHERE version_id = %(version_id)s::uuid
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "swap_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                {"state_key": INFO_READ_MODEL_STATE_KEY, "version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "count_rows"
            step_t0 = time.time()
            card_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_card_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            scope_item_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_scope_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            _record_step(current_step, step_t0)
            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)
            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            error_message = f"{current_step}: {exc}"
            try:
                conn.execute(
                    f"""UPDATE {schema}.info_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": error_message[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError(f"info read model refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "version_id": version_id,
        "card_items": int((card_row or {}).get("n") or 0),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def _highlights_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(HIGHLIGHTS_READ_MODEL_ENV))


def _highlights_stale_fallback_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_STALE_FALLBACK_ENV, default=True)


def _highlights_request_freshness_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS_ENV, default=False)


def _highlights_self_heal_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_SELF_HEAL_ENV, default=True)


def _highlights_refresh_skip_during_fetch_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_REFRESH_SKIP_DURING_FETCH_ENV, default=True)


def _highlights_read_model_incremental_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_INCREMENTAL_ENV, default=True)


def _highlights_verdict_filter_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_VERDICT_FILTER_ENV, default=False)


def _highlights_verdict_filter_recent_days(env: dict[str, str] | None = None) -> int:
    return _env_int(env or _runtime_env(), HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS_ENV, 0, min_value=0)


def _highlights_verdict_cluster_filter(
    schema: str,
    cluster_alias: str,
    env: dict[str, str] | None = None,
) -> str:
    if not _highlights_verdict_filter_enabled(env):
        return ""
    recent_days = _highlights_verdict_filter_recent_days(env)
    include_filter = f"""EXISTS (
        SELECT 1
          FROM {schema}.cluster_items hci
          JOIN {schema}.items hi ON hi.id = hci.item_id
         WHERE hci.cluster_id = {cluster_alias}.id
           AND hi.highlight_include_in_highlights IS TRUE
      )"""
    if recent_days > 0:
        cluster_sort_expr = (
            f"COALESCE({cluster_alias}.last_doc_at, {cluster_alias}.first_doc_at, "
            f"{cluster_alias}.last_updated_at, now())"
        )
        return f"""
      AND (
        {cluster_sort_expr} < now() - ({recent_days}::int * interval '1 day')
        OR {include_filter}
      )
    """
    return f"""
      AND {include_filter}
    """


def _events_read_model_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_ENV,
        EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _events_read_model_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV,
        EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _context_search_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_ENV,
        CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _context_search_events_only_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    env = env or _runtime_env()
    return min(
        _context_search_statement_timeout_ms(env),
        _env_int(
            env,
            CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_ENV,
            CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_DEFAULT,
            min_value=500,
        ),
    )


def _context_search_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_ENV,
        CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _info_read_model_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _highlights_read_model_active_version(conn: Any, schema: str) -> dict[str, Any] | None:
    active = conn.execute(
        f"""SELECT v.version_id::text AS version_id,
                   v.generated_at,
                   v.completed_at,
                   v.max_cluster_updated_at,
                   v.window_days,
                   v.min_github_stars,
                   v.meta_json,
                   sc.max_sort_at
              FROM {schema}.highlights_read_model_state st
              JOIN {schema}.highlights_read_model_versions v
                ON v.version_id = st.active_version_id
              LEFT JOIN {schema}.highlights_scopes sc
                ON sc.version_id = st.active_version_id
               AND sc.scope_key = %(scope_key)s
             WHERE st.key = %(state_key)s
               AND v.status = 'complete'""",
        {
            "scope_key": _highlights_scope_key(dimension="all"),
            "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
        },
    ).fetchone()
    return dict(active) if active else None


def _highlights_read_model_delta_checkpoint(active: dict[str, Any]) -> Any:
    meta = _json_value(active.get("meta_json"))
    checkpoint = None
    if isinstance(meta, dict):
        checkpoint = meta.get("last_delta_checkpoint_at")
    return (
        checkpoint
        or active.get("completed_at")
        or active.get("generated_at")
        or active.get("max_cluster_updated_at")
        or active.get("max_sort_at")
    )


def _trigger_highlights_read_model_self_heal(*, reason: str, min_interval_sec: int = 60) -> dict[str, Any]:
    global _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT
    if not _highlights_self_heal_enabled():
        return {"triggered": False, "skipped": "disabled"}
    with _HIGHLIGHTS_READ_MODEL_SELF_HEAL_LOCK:
        if _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT:
            return {"triggered": False, "skipped": "in_flight"}
        _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT = True

    def _worker() -> None:
        global _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT
        try:
            result = refresh_highlights_read_model_if_stale(min_interval_sec=min_interval_sec)
            print(
                f"[highlights-read-model] self-heal reason={reason}: {result}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[highlights-read-model] self-heal reason={reason} failed: {exc!r}",
                flush=True,
            )
        finally:
            with _HIGHLIGHTS_READ_MODEL_SELF_HEAL_LOCK:
                _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT = False

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"highlights-read-model-self-heal:{reason}",
    ).start()
    return {"triggered": True, "min_interval_sec": int(min_interval_sec)}


def _highlights_category_sql(item_alias: str) -> str:
    raw = f"split_part(coalesce({item_alias}.ai_category, ''), '[', 1)"
    return f"""CASE {raw}
                 WHEN 'ai_tools' THEN 'efficiency_tools'
                 WHEN 'tools' THEN 'efficiency_tools'
                 WHEN 'insights' THEN 'tech'
                 ELSE {raw}
               END"""


def _highlights_category_priority_sql(category_expr: str) -> str:
    cases = "\n".join(
        f"WHEN '{category_id}' THEN {idx}"
        for idx, category_id in enumerate(ACTIVE_CATEGORY_IDS)
        if category_id != "other"
    )
    return f"CASE {category_expr} {cases} ELSE 999 END"


def _highlights_scope_item_order_sql(alias: str = "si") -> str:
    return f"{alias}.sort_at DESC NULLS LAST, {alias}.cluster_id DESC"


def _sync_highlight_cluster_decisions(
    conn: Any,
    schema: str,
    *,
    window_days: int,
    min_github_stars: int,
    checkpoint_at: Any | None = None,
    delta_cluster_table: str | None = None,
) -> None:
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, int(min_github_stars), "c")
    delta_join = ""
    checkpoint_filter = ""
    params: dict[str, Any] = {
        "window_days": max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365)),
    }
    if delta_cluster_table:
        delta_join = f"JOIN {delta_cluster_table} decision_delta ON decision_delta.cluster_id = c.id"
    elif checkpoint_at:
        checkpoint_filter = f"""AND (
                    c.last_updated_at > %(checkpoint_at)s::timestamptz
                 OR EXISTS (
                       SELECT 1
                         FROM {schema}.cluster_items ci_delta
                         JOIN {schema}.items i_delta ON i_delta.id = ci_delta.item_id
                        WHERE ci_delta.cluster_id = c.id
                          AND i_delta.highlight_scored_at > %(checkpoint_at)s::timestamptz
                    )
                  )"""
        params["checkpoint_at"] = checkpoint_at
    conn.execute(
        f"""WITH visible_clusters AS (
               SELECT c.id AS cluster_id,
                      c.ai_title,
                      c.ai_summary,
                      c.doc_count,
                      c.unique_source_count,
                      c.first_doc_at,
                      c.last_doc_at,
                      c.last_updated_at,
                      COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                 FROM {schema}.clusters c
                 {delta_join}
                WHERE c.is_visible_in_feed = true
                  AND c.published_at IS NOT NULL
                  AND coalesce(c.archived, false) = false
                  AND c.merged_into IS NULL
                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                  {checkpoint_filter}
                  {public_filter}
                  {github_filter}
             ),
             members AS (
               SELECT vc.cluster_id,
                      ci.item_id,
                      COALESCE(ci.is_primary_source, false) AS is_primary_source,
                      ci.rank_in_cluster,
                      i.highlight_verdict,
                      i.highlight_value_path,
                      i.highlight_uncertainty,
                      i.highlight_include_in_highlights,
                      i.highlight_reason,
                      i.highlight_prompt_version,
                      i.highlight_model,
                      i.highlight_last_error,
                      CASE
                        WHEN i.highlight_verdict = 'featured'
                         AND i.highlight_include_in_highlights IS TRUE THEN 'featured'
                        WHEN i.highlight_verdict = 'borderline'
                         AND i.highlight_include_in_highlights IS TRUE THEN 'positive_borderline'
                        WHEN i.highlight_verdict = 'borderline' THEN 'risk_borderline'
                        WHEN i.highlight_verdict = 'drop' THEN 'drop'
                        ELSE 'pending'
                      END AS item_cluster_verdict
                 FROM visible_clusters vc
                 JOIN {schema}.cluster_items ci ON ci.cluster_id = vc.cluster_id
                 JOIN {schema}.items i ON i.id = ci.item_id
             ),
             counts AS (
               SELECT cluster_id,
                      bool_or(highlight_include_in_highlights IS TRUE) AS has_include,
                      bool_or(item_cluster_verdict = 'featured') AS has_featured,
                      bool_or(item_cluster_verdict = 'positive_borderline') AS has_positive_borderline,
                      bool_or(item_cluster_verdict = 'risk_borderline') AS has_risk_borderline,
                      bool_or(item_cluster_verdict = 'pending') AS has_pending,
                      jsonb_build_object(
                        'featured', count(*) FILTER (WHERE item_cluster_verdict = 'featured'),
                        'positive_borderline', count(*) FILTER (WHERE item_cluster_verdict = 'positive_borderline'),
                        'risk_borderline', count(*) FILTER (WHERE item_cluster_verdict = 'risk_borderline'),
                        'drop', count(*) FILTER (WHERE item_cluster_verdict = 'drop'),
                        'pending', count(*) FILTER (WHERE item_cluster_verdict = 'pending')
                      ) AS verdict_counts_json
                 FROM members
                GROUP BY cluster_id
             ),
             best_member AS (
               SELECT *,
                      row_number() OVER (
                        PARTITION BY cluster_id
                        ORDER BY CASE item_cluster_verdict
                                   WHEN 'featured' THEN 1
                                   WHEN 'positive_borderline' THEN 2
                                   WHEN 'risk_borderline' THEN 3
                                   WHEN 'pending' THEN 4
                                   ELSE 5
                                 END,
                                 is_primary_source DESC,
                                 rank_in_cluster ASC NULLS LAST,
                                 item_id DESC
                      ) AS rn
                 FROM members
             ),
             decisions AS (
               SELECT vc.cluster_id,
                      CASE
                        WHEN COALESCE(c.has_include, false) THEN 'included'
                        WHEN COALESCE(c.has_pending, true) THEN 'pending'
                        ELSE 'excluded'
                      END AS decision,
                      CASE
                        WHEN COALESCE(c.has_featured, false) THEN 'featured'
                        WHEN COALESCE(c.has_positive_borderline, false) THEN 'positive_borderline'
                        WHEN COALESCE(c.has_pending, true) THEN 'pending'
                        WHEN COALESCE(c.has_risk_borderline, false) THEN 'risk_borderline'
                        ELSE 'drop'
                      END AS cluster_verdict,
                      bm.item_id AS deciding_item_id,
                      COALESCE(NULLIF(bm.highlight_reason, ''), bm.highlight_last_error, '') AS reason,
                      COALESCE(c.verdict_counts_json, '{{}}'::jsonb) AS verdict_counts_json,
                      bm.highlight_prompt_version AS prompt_version,
                      bm.highlight_model AS model,
                      jsonb_strip_nulls(jsonb_build_object(
                        'cluster_id', vc.cluster_id,
                        'ai_title', vc.ai_title,
                        'ai_summary', vc.ai_summary,
                        'doc_count', vc.doc_count,
                        'unique_source_count', vc.unique_source_count,
                        'first_doc_at', vc.first_doc_at,
                        'last_doc_at', vc.last_doc_at,
                        'last_updated_at', vc.last_updated_at,
                        'sort_at', vc.sort_at
                      )) AS snapshot_json
                 FROM visible_clusters vc
                 LEFT JOIN counts c ON c.cluster_id = vc.cluster_id
                 LEFT JOIN best_member bm ON bm.cluster_id = vc.cluster_id AND bm.rn = 1
             )
             INSERT INTO {schema}.highlight_cluster_decisions AS target (
               cluster_id, decision, cluster_verdict, deciding_item_id,
               reason, verdict_counts_json, prompt_version, model,
               decided_at, updated_at, snapshot_json
             )
             SELECT cluster_id, decision, cluster_verdict, deciding_item_id,
                    reason, verdict_counts_json, prompt_version, model,
                    now(), now(), snapshot_json
               FROM decisions
             ON CONFLICT (cluster_id) DO UPDATE SET
               decision = excluded.decision,
               cluster_verdict = excluded.cluster_verdict,
               deciding_item_id = excluded.deciding_item_id,
               reason = excluded.reason,
               verdict_counts_json = excluded.verdict_counts_json,
               prompt_version = excluded.prompt_version,
               model = excluded.model,
               decided_at = excluded.decided_at,
               updated_at = excluded.updated_at,
               snapshot_json = excluded.snapshot_json
             WHERE target.decision IS DISTINCT FROM excluded.decision
                OR target.cluster_verdict IS DISTINCT FROM excluded.cluster_verdict
                OR target.deciding_item_id IS DISTINCT FROM excluded.deciding_item_id
                OR target.reason IS DISTINCT FROM excluded.reason
                OR target.verdict_counts_json IS DISTINCT FROM excluded.verdict_counts_json
                OR target.prompt_version IS DISTINCT FROM excluded.prompt_version
                OR target.model IS DISTINCT FROM excluded.model
                OR target.snapshot_json IS DISTINCT FROM excluded.snapshot_json""",
        params,
    )


def refresh_highlights_read_model_delta_in_place(
    *,
    window_days: int = HIGHLIGHTS_READ_MODEL_WINDOW_DAYS,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Apply changed highlight clusters to the active read model in place."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_window_days = max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365))
    safe_min_github_stars = int(min_github_stars)
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, safe_min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    category_expr = _highlights_category_sql("i")
    category_priority = _highlights_category_priority_sql("category")
    active_categories = [category_id for category_id in ACTIVE_CATEGORY_IDS if category_id != "other"]
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    current_step = "read_active_version"
    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _highlights_read_model_active_version(conn, schema)
    if not active or not active.get("version_id"):
        return refresh_highlights_read_model(
            window_days=safe_window_days,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    checkpoint_at = _highlights_read_model_delta_checkpoint(active)
    if not checkpoint_at:
        return refresh_highlights_read_model(
            window_days=safe_window_days,
            min_github_stars=safe_min_github_stars,
        )
    _record_step(current_step, step_t0)

    delta_clusters_sql = f"""WITH candidate_delta_clusters AS (
                               SELECT c.id AS cluster_id,
                                      c.last_updated_at AS delta_checkpoint_at
                                 FROM {schema}.clusters c
                                WHERE c.is_visible_in_feed = true
                                  AND c.published_at IS NOT NULL
                                  AND coalesce(c.archived, false) = false
                                  AND c.merged_into IS NULL
                                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                                  AND c.last_updated_at > %(checkpoint_at)s::timestamptz
                                  {public_filter}
                                  {github_filter}
                               UNION ALL
                               SELECT c.id AS cluster_id,
                                      max(i_delta.highlight_scored_at) AS delta_checkpoint_at
                                 FROM {schema}.items i_delta
                                 JOIN {schema}.cluster_items ci_delta
                                   ON ci_delta.item_id = i_delta.id
                                 JOIN {schema}.clusters c
                                   ON c.id = ci_delta.cluster_id
                                WHERE i_delta.highlight_scored_at > %(checkpoint_at)s::timestamptz
                                  AND c.is_visible_in_feed = true
                                  AND c.published_at IS NOT NULL
                                  AND coalesce(c.archived, false) = false
                                  AND c.merged_into IS NULL
                                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                                  {public_filter}
                                  {github_filter}
                                GROUP BY c.id
                             )
                             SELECT cluster_id,
                                    max(delta_checkpoint_at) AS delta_checkpoint_at
                               FROM candidate_delta_clusters
                              GROUP BY cluster_id"""

    delta_scope_cte = f"""WITH base_clusters AS (
                       SELECT c.id AS cluster_id,
                              c.ai_title,
                              c.ai_summary,
                              c.doc_count,
                              c.unique_source_count,
                              c.first_doc_at,
                              c.last_doc_at,
                              c.platforms_json,
                              COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                              c.live_version,
                              c.last_updated_at,
                              COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                         FROM {schema}.clusters c
                         JOIN pg_temp.highlights_read_model_delta_clusters dc
                           ON dc.cluster_id = c.id
                         LEFT JOIN LATERAL (
                           SELECT i_cover.cover_url
                             FROM {schema}.cluster_items ci_cover
                             JOIN {schema}.items i_cover ON i_cover.id = ci_cover.item_id
                            WHERE ci_cover.cluster_id = c.id
                              AND NULLIF(i_cover.cover_url, '') IS NOT NULL
                              AND i_cover.platform <> 'manual'
                              AND i_cover.user_id IS NULL
                            ORDER BY COALESCE(ci_cover.is_primary_source, false) DESC,
                                     ci_cover.rank_in_cluster ASC NULLS LAST
                            LIMIT 1
                         ) event_cover ON true
                        WHERE c.is_visible_in_feed = true
                          AND c.published_at IS NOT NULL
                          AND coalesce(c.archived, false) = false
                          AND c.merged_into IS NULL
                          AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                          {public_filter}
                          {github_filter}
                          {verdict_filter}
                     ),
                     source_members AS (
                       SELECT ci.cluster_id,
                              ci.source_identity,
                              ci.rank_in_cluster,
                              COALESCE(ci.is_primary_source, false) AS is_primary_source,
                              i.id AS item_id,
                              i.platform,
                              i.author_name,
                              i.source,
                              i.url,
                              i.ai_category,
                              i.published_at,
                              i.fetched_at,
                              {category_expr} AS category
                         FROM {schema}.cluster_items ci
                         JOIN {schema}.items i ON i.id = ci.item_id
                         JOIN base_clusters b ON b.cluster_id = ci.cluster_id
                     ),
                     category_counts AS (
                       SELECT cluster_id, category, count(*) AS n
                         FROM source_members
                        WHERE category = ANY(%(active_categories)s::text[])
                        GROUP BY cluster_id, category
                     ),
                     category_ranked AS (
                       SELECT cluster_id,
                              category,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY n DESC,
                                         {category_priority},
                                         category ASC
                              ) AS rn
                         FROM category_counts
                     ),
                     source_dedup AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              is_primary_source,
                              rank_in_cluster,
                              published_at,
                              fetched_at,
                              row_number() OVER (
                                PARTITION BY cluster_id,
                                             COALESCE(
                                               source_identity,
                                               url,
                                               platform || ':' || COALESCE(author_name, source, item_id::text)
                                             )
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                         item_id DESC
                              ) AS identity_rn
                         FROM source_members
                     ),
                     source_ranked AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST
                              ) AS preview_rn
                         FROM source_dedup
                        WHERE identity_rn = 1
                     ),
                     source_preview AS (
                       SELECT cluster_id,
                              jsonb_agg(
                                jsonb_strip_nulls(jsonb_build_object(
                                  'platform', platform,
                                  'author', author_name,
                                  'source', source
                                ))
                                ORDER BY preview_rn
                              ) FILTER (WHERE preview_rn <= 3) AS source_preview
                         FROM source_ranked
                        GROUP BY cluster_id
                     ),
                     cluster_cards AS (
                       SELECT b.cluster_id,
                              b.sort_at,
                              b.last_updated_at,
                              cr.category,
                              jsonb_strip_nulls(jsonb_build_object(
                                'id', b.cluster_id,
                                'ai_title', b.ai_title,
                                'ai_summary', b.ai_summary,
                                'doc_count', b.doc_count,
                                'unique_source_count', b.unique_source_count,
                                'category', cr.category,
                                'source_preview', COALESCE(sp.source_preview, '[]'::jsonb),
                                'first_doc_at', b.first_doc_at,
                                'last_doc_at', b.last_doc_at,
                                'platforms', COALESCE(b.platforms_json, '[]'::jsonb),
                                'cover_url', b.cover_url,
                                'live_version', b.live_version
                              )) AS card_json
                         FROM base_clusters b
                         LEFT JOIN category_ranked cr
                                ON cr.cluster_id = b.cluster_id
                               AND cr.rn = 1
                         LEFT JOIN source_preview sp ON sp.cluster_id = b.cluster_id
                     ),
                     scope_rows AS (
                       SELECT %(scope_key_all)s::text AS scope_key,
                              'all'::text AS dimension,
                              ''::text AS value,
                              cluster_id,
                              sort_at,
                              last_updated_at,
                              card_json
                         FROM cluster_cards
                       UNION ALL
                       SELECT 'category:' || category AS scope_key,
                              'category'::text AS dimension,
                              category AS value,
                              cluster_id,
                              sort_at,
                              last_updated_at,
                              card_json
                         FROM cluster_cards
                        WHERE category IS NOT NULL
                          AND category != ''
                     )"""
    params = {
        "active_version_id": active_version_id,
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "checkpoint_at": checkpoint_at,
        "active_categories": active_categories,
        "scope_key_all": "all",
        "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
    }
    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "materialize_delta_clusters"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_clusters")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_delta_clusters ON COMMIT DROP AS
                    {delta_clusters_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_clusters")
            delta_cluster_row = conn.execute(
                """SELECT count(*) AS clusters,
                          max(delta_checkpoint_at) AS max_delta_checkpoint_at
                     FROM pg_temp.highlights_read_model_delta_clusters"""
            ).fetchone()
            delta_clusters = int((delta_cluster_row or {}).get("clusters") or 0)
            if delta_clusters <= 0:
                conn.commit()
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "mode": "delta_in_place",
                    "active_version_id": active_version_id,
                    "checkpoint_at": _timestamp_value(checkpoint_at),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            delta_max_checkpoint_at = (delta_cluster_row or {}).get("max_delta_checkpoint_at")
            _record_step(current_step, step_t0)

            current_step = "sync_cluster_decisions"
            step_t0 = time.time()
            _sync_highlight_cluster_decisions(
                conn,
                schema,
                window_days=safe_window_days,
                min_github_stars=safe_min_github_stars,
                delta_cluster_table="pg_temp.highlights_read_model_delta_clusters",
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_delta_scope_rows ON COMMIT DROP AS
                    {delta_scope_cte}
                    SELECT scope_key, dimension, value, cluster_id, sort_at,
                           last_updated_at, card_json
                      FROM scope_rows""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_scope_rows")
            delta_row = conn.execute(
                """SELECT count(*) AS scope_rows
                     FROM pg_temp.highlights_read_model_delta_scope_rows"""
            ).fetchone()
            delta_scope_rows = int((delta_row or {}).get("scope_rows") or 0)
            _record_step(current_step, step_t0)

            current_step = "materialize_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_affected_scopes")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.highlights_read_model_delta_scope_rows
                   UNION
                   SELECT DISTINCT si.scope_key
                     FROM {schema}.highlights_scope_items si
                     JOIN pg_temp.highlights_read_model_delta_clusters dc
                       ON dc.cluster_id = si.cluster_id
                    WHERE si.version_id = %(active_version_id)s::uuid""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "delete_obsolete_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""DELETE FROM {schema}.highlights_scope_items si
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.highlights_read_model_delta_clusters dc
                              WHERE dc.cluster_id = si.cluster_id
                           )
                       AND NOT EXISTS (
                             SELECT 1
                               FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                              WHERE dsr.scope_key = si.scope_key
                                AND dsr.cluster_id = si.cluster_id
                           )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "update_existing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.highlights_scope_items si
                       SET sort_at = dsr.sort_at,
                           card_json = dsr.card_json
                      FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND si.scope_key = dsr.scope_key
                       AND si.cluster_id = dsr.cluster_id
                       AND (
                            si.sort_at IS DISTINCT FROM dsr.sort_at
                         OR si.card_json IS DISTINCT FROM dsr.card_json
                       )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "insert_missing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH missing AS (
                       SELECT dsr.scope_key, dsr.cluster_id, dsr.sort_at, dsr.card_json
                         FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM {schema}.highlights_scope_items si
                                 WHERE si.version_id = %(active_version_id)s::uuid
                                   AND si.scope_key = dsr.scope_key
                                   AND si.cluster_id = dsr.cluster_id
                              )
                     ),
                     scope_max_rank AS (
                       SELECT si.scope_key, max(si.rank) AS max_rank
                         FROM {schema}.highlights_scope_items si
                        WHERE si.version_id = %(active_version_id)s::uuid
                          AND EXISTS (
                                SELECT 1
                                  FROM pg_temp.highlights_read_model_affected_scopes a
                                 WHERE a.scope_key = si.scope_key
                              )
                        GROUP BY si.scope_key
                     ),
                     ranked AS (
                       SELECT m.scope_key, m.cluster_id, m.sort_at, m.card_json,
                              COALESCE(smr.max_rank, 0) + row_number() OVER (
                                PARTITION BY m.scope_key
                                ORDER BY m.sort_at DESC NULLS LAST,
                                         m.cluster_id DESC
                              ) AS append_rank
                         FROM missing m
                         LEFT JOIN scope_max_rank smr
                           ON smr.scope_key = m.scope_key
                     )
                     INSERT INTO {schema}.highlights_scope_items (
                       version_id, scope_key, rank, cluster_id, sort_at, card_json
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            append_rank::integer,
                            cluster_id,
                            sort_at,
                            card_json
                       FROM ranked""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "upsert_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_scope_meta")
            conn.execute(
                """CREATE TEMP TABLE highlights_read_model_delta_scope_meta ON COMMIT DROP AS
                   SELECT scope_key, max(dimension) AS dimension, max(value) AS value
                     FROM pg_temp.highlights_read_model_delta_scope_rows
                    GROUP BY scope_key"""
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_scope_meta")
            conn.execute(
                f"""WITH affected_scope_stats AS (
                       SELECT a.scope_key,
                              COALESCE(max(dsm.dimension), max(sc.dimension)) AS dimension,
                              COALESCE(max(dsm.value), max(sc.value), '') AS value,
                              count(si.cluster_id)::integer AS total_count,
                              max(si.sort_at) AS max_sort_at
                         FROM pg_temp.highlights_read_model_affected_scopes a
                         LEFT JOIN {schema}.highlights_scopes sc
                           ON sc.version_id = %(active_version_id)s::uuid
                          AND sc.scope_key = a.scope_key
                         LEFT JOIN pg_temp.highlights_read_model_delta_scope_meta dsm
                           ON dsm.scope_key = a.scope_key
                         LEFT JOIN {schema}.highlights_scope_items si
                           ON si.version_id = %(active_version_id)s::uuid
                          AND si.scope_key = a.scope_key
                        GROUP BY a.scope_key
                     )
                     INSERT INTO {schema}.highlights_scopes (
                       version_id, scope_key, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            dimension,
                            value,
                            total_count,
                            max_sort_at,
                            now()
                       FROM affected_scope_stats
                      WHERE total_count > 0
                     ON CONFLICT (version_id, scope_key) DO UPDATE SET
                       dimension = excluded.dimension,
                       value = excluded.value,
                       total_count = excluded.total_count,
                       max_sort_at = excluded.max_sort_at,
                       generated_at = excluded.generated_at""",
                params,
            )
            conn.execute(
                f"""DELETE FROM {schema}.highlights_scopes sc
                      USING pg_temp.highlights_read_model_affected_scopes a
                     WHERE sc.version_id = %(active_version_id)s::uuid
                       AND sc.scope_key = a.scope_key
                       AND NOT EXISTS (
                             SELECT 1
                               FROM {schema}.highlights_scope_items si
                              WHERE si.version_id = sc.version_id
                                AND si.scope_key = sc.scope_key
                           )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "update_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_versions
                       SET completed_at = now(),
                           max_cluster_updated_at = (
                             SELECT max(max_sort_at)
                               FROM {schema}.highlights_scopes
                              WHERE version_id = %(active_version_id)s::uuid
                           ),
                           meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'read_model', %(read_model)s::text,
                             'last_delta_mode', 'in_place',
                             'last_delta_at', now(),
                             'last_delta_checkpoint_at', %(delta_max_checkpoint_at)s::timestamptz
                           )
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    **params,
                    "read_model": HIGHLIGHTS_READ_MODEL_VERSION,
                    "delta_max_checkpoint_at": delta_max_checkpoint_at,
                },
            )
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_state
                       SET updated_at = now()
                     WHERE key = %(state_key)s
                       AND active_version_id = %(active_version_id)s::uuid""",
                params,
            )

            current_step = "count_scope_items"
            step_t0 = time.time()
            scope_item_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = %(active_version_id)s::uuid""",
                params,
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"highlights read model in-place delta refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "delta_in_place",
        "version_id": active_version_id,
        "delta_clusters": delta_clusters,
        "delta_scope_rows": delta_scope_rows,
        "active_checkpoint_at": _timestamp_value(delta_max_checkpoint_at),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_highlights_read_model_if_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    global _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    # P-C insurance: never refresh the highlights read model while a fetch run is
    # still in flight. During a run, clusters are published in one batch only at
    # publish_run() (end of run); a mid-run refresh (e.g. request-path self-heal)
    # can advance the delta checkpoint past clusters that are visible but not yet
    # fully scored, and the single scalar checkpoint cannot recover them. The
    # post-fetch refresh is triggered *after* finish_fetch_run marks the run done,
    # so this guard does not block that legitimate path. Fail open (proceed) if the
    # running-run probe itself errors — never let a transient DB error stall refresh.
    if _highlights_refresh_skip_during_fetch_enabled():
        try:
            fetch_running = has_recent_running_fetch_remote()
        except Exception:
            fetch_running = False
        if fetch_running:
            return {"ok": True, "skipped": "fetch_running"}
    min_interval = max(0, int(min_interval_sec))
    now = time.monotonic()
    with _HIGHLIGHTS_READ_MODEL_REFRESH_LOCK:
        age = (
            now - _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
            if _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
            else None
        )
        if age is not None and age < min_interval:
            return {
                "ok": True,
                "skipped": "recent_attempt",
                "age_sec": round(age, 1),
                "min_interval_sec": min_interval,
            }
        _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = now
    if _highlights_read_model_incremental_enabled():
        return refresh_highlights_read_model_delta_in_place()
    return refresh_highlights_read_model()


def _highlights_sort_tuple(row: dict[str, Any] | None) -> tuple[float, int]:
    if not row:
        return (float("-inf"), 0)
    raw_id = row.get("id", row.get("cluster_id", 0))
    try:
        cluster_id = int(raw_id or 0)
    except (TypeError, ValueError):
        cluster_id = 0
    return (sort_key(row.get("sort_at")), cluster_id)


def highlights_read_model_freshness(
    *,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Compare the active highlights read model with the live visible top row."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "enabled": False, "stale": False, "skipped": "disabled"}
    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, int(min_github_stars), "c")
    with connect() as conn:
        _set_short_statement_timeout(conn, 2500)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
        except Exception:
            _rollback_safely(conn)
        active = conn.execute(
            f"""SELECT st.active_version_id::text AS active_version_id,
                       sc.max_sort_at,
                       top.cluster_id,
                       top.sort_at
                  FROM {schema}.highlights_read_model_state st
                  JOIN {schema}.highlights_read_model_versions v
                    ON v.version_id = st.active_version_id
                  LEFT JOIN {schema}.highlights_scopes sc
                    ON sc.version_id = st.active_version_id
                   AND sc.scope_key = %(scope_key)s
                  LEFT JOIN LATERAL (
                    SELECT cluster_id, sort_at
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = st.active_version_id
                       AND scope_key = %(scope_key)s
                     ORDER BY {_highlights_scope_item_order_sql("highlights_scope_items")}
                     LIMIT 1
                  ) top ON true
                 WHERE st.key = %(state_key)s
                   AND v.status = 'complete'""",
            {
                "scope_key": _highlights_scope_key(dimension="all"),
                "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
            },
        ).fetchone()
        latest = conn.execute(
            f"""SELECT c.id,
                       COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                  FROM {schema}.clusters c
                 WHERE c.is_visible_in_feed = true
                   AND c.published_at IS NOT NULL
                   AND coalesce(c.archived, false) = false
                   AND c.merged_into IS NULL
                   AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                   {public_filter}
                   {github_filter}
                 ORDER BY sort_at DESC NULLS LAST,
                          c.id DESC
                 LIMIT 1""",
            {"window_days": HIGHLIGHTS_READ_MODEL_WINDOW_DAYS},
        ).fetchone()
    active_dict = dict(active) if active else None
    latest_dict = dict(latest) if latest else None
    stale = False
    reason = "data_fresh"
    if latest_dict and not active_dict:
        stale = True
        reason = "missing_active_version"
    elif latest_dict and _highlights_sort_tuple(latest_dict) > _highlights_sort_tuple(active_dict):
        stale = True
        reason = "live_top_newer"
    return {
        "ok": True,
        "enabled": True,
        "stale": stale,
        "reason": reason,
        "active_version_id": (active_dict or {}).get("active_version_id"),
        "active_top_cluster_id": (active_dict or {}).get("cluster_id"),
        "active_top_sort_at": to_utc_iso((active_dict or {}).get("sort_at")),
        "latest_cluster_id": (latest_dict or {}).get("id"),
        "latest_sort_at": to_utc_iso((latest_dict or {}).get("sort_at")),
    }


def refresh_highlights_read_model_if_data_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    freshness = highlights_read_model_freshness()
    if not freshness.get("stale"):
        return {**freshness, "skipped": freshness.get("reason") or "data_fresh"}
    refreshed = refresh_highlights_read_model_if_stale(min_interval_sec=min_interval_sec)
    return {**freshness, "refresh": refreshed}


def refresh_highlights_read_model(
    *,
    window_days: int = HIGHLIGHTS_READ_MODEL_WINDOW_DAYS,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Build a version-swapped read model for the 精选 tab event timeline."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_window_days = max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365))
    safe_min_github_stars = int(min_github_stars)
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, safe_min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    category_expr = _highlights_category_sql("i")
    category_priority = _highlights_category_priority_sql("category")
    active_categories = [category_id for category_id in ACTIVE_CATEGORY_IDS if category_id != "other"]
    scope_cte = f"""WITH base_clusters AS (
                       SELECT c.id AS cluster_id,
                              c.ai_title,
                              c.ai_summary,
                              c.doc_count,
                              c.unique_source_count,
                              c.first_doc_at,
                              c.last_doc_at,
                              c.platforms_json,
                              COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                              c.live_version,
                              c.last_updated_at,
                              COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                         FROM {schema}.clusters c
                         LEFT JOIN LATERAL (
                           SELECT i_cover.cover_url
                             FROM {schema}.cluster_items ci_cover
                             JOIN {schema}.items i_cover ON i_cover.id = ci_cover.item_id
                            WHERE ci_cover.cluster_id = c.id
                              AND NULLIF(i_cover.cover_url, '') IS NOT NULL
                              AND i_cover.platform <> 'manual'
                              AND i_cover.user_id IS NULL
                            ORDER BY COALESCE(ci_cover.is_primary_source, false) DESC,
                                     ci_cover.rank_in_cluster ASC NULLS LAST
                            LIMIT 1
                         ) event_cover ON true
                        WHERE c.is_visible_in_feed = true
                          AND c.published_at IS NOT NULL
                          AND coalesce(c.archived, false) = false
                          AND c.merged_into IS NULL
                          AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                          {public_filter}
                          {github_filter}
                          {verdict_filter}
                     ),
                     source_members AS (
                       SELECT ci.cluster_id,
                              ci.source_identity,
                              ci.rank_in_cluster,
                              COALESCE(ci.is_primary_source, false) AS is_primary_source,
                              i.id AS item_id,
                              i.platform,
                              i.author_name,
                              i.source,
                              i.url,
                              i.ai_category,
                              i.published_at,
                              i.fetched_at,
                              {category_expr} AS category
                         FROM {schema}.cluster_items ci
                         JOIN {schema}.items i ON i.id = ci.item_id
                         JOIN base_clusters b ON b.cluster_id = ci.cluster_id
                     ),
                     category_counts AS (
                       SELECT cluster_id, category, count(*) AS n
                         FROM source_members
                        WHERE category = ANY(%(active_categories)s::text[])
                        GROUP BY cluster_id, category
                     ),
                     category_ranked AS (
                       SELECT cluster_id,
                              category,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY n DESC,
                                         {category_priority},
                                         category ASC
                              ) AS rn
                         FROM category_counts
                     ),
                     source_dedup AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              is_primary_source,
                              rank_in_cluster,
                              published_at,
                              fetched_at,
                              row_number() OVER (
                                PARTITION BY cluster_id,
                                             COALESCE(
                                               source_identity,
                                               url,
                                               platform || ':' || COALESCE(author_name, source, item_id::text)
                                             )
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                         item_id DESC
                              ) AS identity_rn
                         FROM source_members
                     ),
                     source_ranked AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST
                              ) AS preview_rn
                         FROM source_dedup
                        WHERE identity_rn = 1
                     ),
                     source_preview AS (
                       SELECT cluster_id,
                              jsonb_agg(
                                jsonb_strip_nulls(jsonb_build_object(
                                  'platform', platform,
                                  'author', author_name,
                                  'source', source
                                ))
                                ORDER BY preview_rn
                              ) FILTER (WHERE preview_rn <= 3) AS source_preview
                         FROM source_ranked
                        GROUP BY cluster_id
                     ),
                     cluster_cards AS (
                       SELECT b.cluster_id,
                              b.sort_at,
                              cr.category,
                              jsonb_strip_nulls(jsonb_build_object(
                                'id', b.cluster_id,
                                'ai_title', b.ai_title,
                                'ai_summary', b.ai_summary,
                                'doc_count', b.doc_count,
                                'unique_source_count', b.unique_source_count,
                                'category', cr.category,
                                'source_preview', COALESCE(sp.source_preview, '[]'::jsonb),
                                'first_doc_at', b.first_doc_at,
                                'last_doc_at', b.last_doc_at,
                                'platforms', COALESCE(b.platforms_json, '[]'::jsonb),
                                'cover_url', b.cover_url,
                                'live_version', b.live_version
                              )) AS card_json
                         FROM base_clusters b
                         LEFT JOIN category_ranked cr
                                ON cr.cluster_id = b.cluster_id
                               AND cr.rn = 1
                         LEFT JOIN source_preview sp ON sp.cluster_id = b.cluster_id
                     ),
                     scope_rows AS (
                       SELECT %(scope_key_all)s::text AS scope_key,
                              'all'::text AS dimension,
                              ''::text AS value,
                              cluster_id,
                              sort_at,
                              card_json
                         FROM cluster_cards
                       UNION ALL
                       SELECT 'category:' || category AS scope_key,
                              'category'::text AS dimension,
                              category AS value,
                              cluster_id,
                              sort_at,
                              card_json
                         FROM cluster_cards
                        WHERE category IS NOT NULL
                          AND category != ''
                     )"""
    params = {
        "version_id": version_id,
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "active_categories": active_categories,
        "scope_key_all": "all",
        "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
        "meta_json": json.dumps({"read_model": HIGHLIGHTS_READ_MODEL_VERSION}),
    }
    t0 = time.time()
    with connect() as conn:
        try:
            _set_short_statement_timeout(
                conn,
                _env_int(
                    _runtime_env(),
                    HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
                    HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
                    min_value=60000,
                ),
            )
            conn.execute(
                f"""INSERT INTO {schema}.highlights_read_model_versions (
                       version_id, status, generated_at, window_days,
                       min_github_stars, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(window_days)s,
                       %(min_github_stars)s, %(meta_json)s::jsonb
                     )""",
                params,
            )
            conn.execute(
                f"""{scope_cte}
                     INSERT INTO {schema}.highlights_scopes (
                       version_id, scope_key, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            dimension,
                            value,
                            count(*)::integer,
                            max(sort_at),
                            now()
                       FROM scope_rows
                      GROUP BY scope_key, dimension, value""",
                params,
            )
            conn.execute(
                f"""{scope_cte},
                     ranked AS (
                       SELECT scope_key,
                              cluster_id,
                              sort_at,
                              card_json,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY sort_at DESC NULLS LAST,
                                         cluster_id DESC
                              ) AS rn
                         FROM scope_rows
                     )
                     INSERT INTO {schema}.highlights_scope_items (
                       version_id, scope_key, rank, cluster_id, sort_at, card_json
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            rn::integer,
                            cluster_id,
                            sort_at,
                            card_json
                       FROM ranked""",
                params,
            )
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_cluster_updated_at = (
                             SELECT max(sort_at)
                               FROM {schema}.highlights_scope_items
                              WHERE version_id = %(version_id)s::uuid
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            )
            conn.execute(
                f"""INSERT INTO {schema}.highlights_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                params,
            )
            _sync_highlight_cluster_decisions(
                conn,
                schema,
                window_days=safe_window_days,
                min_github_stars=safe_min_github_stars,
            )
            scope_item_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            scope_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scopes
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            conn.execute(
                f"""DELETE FROM {schema}.highlights_read_model_versions
                     WHERE version_id NOT IN (
                       SELECT version_id
                         FROM {schema}.highlights_read_model_versions
                        ORDER BY generated_at DESC
                        LIMIT 3
                     )"""
            )
            conn.commit()
        except Exception as exc:
            _rollback_safely(conn)
            try:
                conn.execute(
                    f"""UPDATE {schema}.highlights_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": str(exc)[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError("highlights read model refresh failed") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "version_id": version_id,
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "scopes": int((scope_row or {}).get("n") or 0),
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _set_short_statement_timeout(conn: Any, timeout_ms: int = _REMOTE_STATUS_TIMEOUT_MS) -> None:
    try:
        conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'")
    except Exception:
        _rollback_safely(conn)


def _set_local_statement_and_idle_tx_timeouts(
    conn: Any,
    *,
    statement_timeout_ms: int,
    idle_tx_timeout_ms: int,
) -> bool:
    try:
        conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_ms)}ms'")
        conn.execute(
            "SET LOCAL idle_in_transaction_session_timeout = "
            f"'{int(idle_tx_timeout_ms)}ms'"
        )
        return True
    except Exception:
        _rollback_safely(conn)
        return False


def _set_events_read_model_timeouts(conn: Any) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=_events_read_model_statement_timeout_ms(),
        idle_tx_timeout_ms=_events_read_model_idle_tx_timeout_ms(),
    )


def _set_context_search_timeouts(
    conn: Any,
    *,
    statement_timeout_ms: int | None = None,
) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=statement_timeout_ms or _context_search_statement_timeout_ms(),
        idle_tx_timeout_ms=_context_search_idle_tx_timeout_ms(),
    )


def _set_info_read_model_timeouts(conn: Any, *, statement_timeout_ms: int = 2500) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=statement_timeout_ms,
        idle_tx_timeout_ms=_info_read_model_idle_tx_timeout_ms(),
    )


def _remote_feed_live_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_FEED_LIVE_TIMEOUT_MS_ENV, 2500, min_value=500)


def _remote_feed_search_timeout_ms() -> int:
    # 搜索比常规 feed 读昂贵(索引命中后仍需回表匹配行),独立预算
    return _env_int(
        _runtime_env(),
        REMOTE_FEED_SEARCH_TIMEOUT_MS_ENV,
        REMOTE_FEED_SEARCH_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _remote_actions_board_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_ACTIONS_BOARD_TIMEOUT_MS_ENV, 4500, min_value=500)


def _remote_actions_board_detail_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_ACTIONS_BOARD_DETAIL_TIMEOUT_MS_ENV, 1200, min_value=300)


def _remote_pending_scan_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_PENDING_SCAN_TIMEOUT_MS_ENV, 30000, min_value=5000)


def set_pending_scan_statement_timeout(conn: Any) -> None:
    _set_short_statement_timeout(conn, _remote_pending_scan_timeout_ms())


def _remote_cluster_write_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_CLUSTER_WRITE_TIMEOUT_MS_ENV, 300000, min_value=30000)


def set_cluster_write_statement_timeout(conn: Any) -> None:
    _set_short_statement_timeout(conn, _remote_cluster_write_timeout_ms())


def _remote_feed_live_circuit_open() -> bool:
    env = _runtime_env()
    if _truthy(env.get(REMOTE_FEED_LIVE_DISABLED_ENV)):
        return True
    with _REMOTE_FEED_LIVE_CIRCUIT_LOCK:
        return time.monotonic() < _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL


def _mark_remote_feed_live_circuit_open() -> None:
    global _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL
    hold_sec = _env_int(_runtime_env(), REMOTE_FEED_LIVE_CIRCUIT_SEC_ENV, 60, min_value=1)
    with _REMOTE_FEED_LIVE_CIRCUIT_LOCK:
        _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL = max(
            _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL,
            time.monotonic() + hold_sec,
        )


def _platforms_mv_available(conn: Any, schema: str) -> bool:
    """BF-0515-mv-pgcron: detect whether mv_items_top_per_platform exists.
    Cached 5 min so we don't query pg_catalog on every request."""
    cache_key = ("platforms_mv_available", schema)
    cached = _cache_get_with_ttl(cache_key, 300)
    if cached is not None:
        return bool(cached)
    try:
        row = conn.execute(
            "select to_regclass(%s) as name",
            (f"{schema}.mv_items_top_per_platform",),
        ).fetchone()
        available = bool(row and row.get("name"))
    except Exception:
        _rollback_safely(conn)
        available = False
    _cache_set_with_ttl(cache_key, available, 300)
    return available


def _mark_stale_payload(payload: Any, *, source: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    result = copy.deepcopy(payload)
    result["degraded"] = True
    result["stale"] = True
    result["stale_source"] = source
    return result


def _read_feed_snapshot(
    conn: Any,
    schema: str,
    snapshot_key: str,
    *,
    allow_expired: bool = False,
) -> Any | None:
    availability_key = ("feed_snapshots_available", schema)
    if _cache_get_with_ttl(availability_key, 300) is False:
        return None
    try:
        expires_filter = "" if allow_expired else "AND (expires_at IS NULL OR expires_at > now())"
        row = conn.execute(
            f"""SELECT payload_json
                  FROM {schema}.feed_snapshots
                 WHERE snapshot_key = %s
                   {expires_filter}
                 ORDER BY generated_at DESC
                 LIMIT 1""",
            (snapshot_key,),
        ).fetchone()
    except Exception:
        _rollback_safely(conn)
        _cache_set_with_ttl(availability_key, False, 300)
        return None
    _cache_set_with_ttl(availability_key, True, 300)
    if not row:
        return None
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if allow_expired:
        return _mark_stale_payload(payload, source="feed_snapshots")
    return payload


def _write_feed_snapshot(conn: Any, schema: str, snapshot_key: str, payload: Any) -> None:
    ttl = _remote_snapshot_ttl()
    if ttl <= 0 or not _feed_snapshots_available(conn, schema):
        return
    try:
        conn.execute(
            f"""INSERT INTO {schema}.feed_snapshots
                  (snapshot_key, payload_json, generated_at, expires_at)
                VALUES (%s, %s, now(), now() + (%s * interval '1 second'))
                ON CONFLICT (snapshot_key) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  generated_at = excluded.generated_at,
                  expires_at = excluded.expires_at""",
            (snapshot_key, _maybe_jsonb(payload), int(ttl)),
        )
        conn.commit()
    except Exception:
        _rollback_safely(conn)


def _write_feed_snapshot_async(schema: str, snapshot_key: str, payload: Any) -> None:
    if _remote_snapshot_ttl() <= 0:
        return
    with _SNAPSHOT_WRITE_LOCK:
        if snapshot_key in _SNAPSHOT_WRITES_IN_FLIGHT:
            return
        _SNAPSHOT_WRITES_IN_FLIGHT.add(snapshot_key)

    payload_copy = copy.deepcopy(payload)

    def _worker() -> None:
        try:
            try:
                with connect() as conn:
                    _write_feed_snapshot(conn, schema, snapshot_key, payload_copy)
            except Exception:
                pass
        finally:
            with _SNAPSHOT_WRITE_LOCK:
                _SNAPSHOT_WRITES_IN_FLIGHT.discard(snapshot_key)

    threading.Thread(target=_worker, name=f"feed-snapshot:{snapshot_key[:32]}", daemon=True).start()


def _events_snapshot_key(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
) -> str:
    cats = ",".join(sorted(categories or []))
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    return (
        "events:v4:"
        f"limit={int(limit)}:"
        f"public={int(bool(public_only))}:"
        f"stars={int(min_github_stars)}:"
        f"enabled={int(bool(enabled))}:"
        f"tz={tz_offset}:"
        f"cats={cats}"
    )


def _feed_events_local_cache_name(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
) -> str:
    cats = ",".join(sorted(categories or []))
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    return (
        f"feed_events_limit={int(limit)}_"
        f"public={int(bool(public_only))}_"
        f"stars={int(min_github_stars)}_"
        f"enabled={int(bool(enabled))}_"
        f"tz={tz_offset}_"
        f"cats={cats}"
    )


def _feed_items_local_cache_name(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return (
        f"feed_items_limit={int(limit)}_"
        f"public={int(bool(public_only))}_"
        f"stars={int(min_github_stars)}"
    )


def _sections_snapshot_key(
    *,
    per_category: int | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> str:
    return (
        "sections:v1:"
        f"per={per_category if per_category is not None else 'all'}:"
        f"public={int(bool(public_only))}:"
        f"owner={manual_owner_user_id or ''}:"
        f"stars={int(min_github_stars)}"
    )


def _get_pool(psycopg_module: Any, dict_row: Any) -> Any | None:
    env = _runtime_env()
    if _truthy(env.get(REMOTE_DB_POOL_DISABLED_ENV)):
        return None
    try:
        from psycopg_pool import ConnectionPool
    except Exception:
        return None

    dsn = database_url()
    global _POOL, _POOL_DSN
    with _POOL_LOCK:
        if _POOL is not None and _POOL_DSN == dsn:
            return _POOL
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
        min_size = _env_int(env, REMOTE_DB_POOL_MIN_ENV, 1, min_value=0)
        max_size = _env_int(env, REMOTE_DB_POOL_MAX_ENV, 8, min_value=max(1, min_size))
        timeout = _env_int(env, REMOTE_DB_POOL_TIMEOUT_ENV, 2, min_value=1)
        connect_timeout = _env_int(env, REMOTE_DB_CONNECT_TIMEOUT_ENV, 2, min_value=1)
        _POOL = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=float(timeout),
            open=True,
            # prepare_threshold=None disables psycopg's prepared-statement cache,
            # required by Supabase transaction-mode pooler (port 6543) which does
            # not preserve session state between checkouts. See BF-0515-1.
            kwargs={"row_factory": dict_row, "connect_timeout": connect_timeout, "prepare_threshold": None},
        )
        _POOL_DSN = dsn
        return _POOL


def supabase_project_url() -> str:
    env = _runtime_env()
    url = (
        env.get(SUPABASE_URL_ENV)
        or env.get("SUPABASE_PROJECT_URL")
        or env.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip().rstrip("/")
    if not url:
        raise RemoteDBConfigError(
            f"{SUPABASE_URL_ENV} is missing; add your Supabase project URL before using remote asset storage."
        )
    return url


def supabase_service_role_key() -> str:
    env = _runtime_env()
    key = (env.get(SUPABASE_SERVICE_KEY_ENV) or env.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not key:
        raise RemoteDBConfigError(
            f"{SUPABASE_SERVICE_KEY_ENV} is missing; remote asset storage needs a service-role key."
        )
    return key


def supabase_storage_bucket() -> str:
    return (_runtime_env().get(SUPABASE_STORAGE_BUCKET_ENV) or DEFAULT_STORAGE_BUCKET).strip()


def assert_asset_storage_ready() -> dict[str, Any]:
    if not asset_storage_to_remote():
        return {"backend": STORAGE_LOCAL, "remote_assets": False}
    supabase_project_url()
    supabase_service_role_key()
    bucket = supabase_storage_bucket()
    return {"backend": "supabase", "bucket": bucket, "remote_assets": True}


def _strip_nul(value: Any) -> Any:
    """BF-0704-1: Postgres jsonb 不接受 \u0000——外链抓取的正文里偶发 NUL,
    写库报 unsupported Unicode escape sequence(生产 26 次/10h,富化丢失)。
    入库前对字符串深度剥离。"""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {(_strip_nul(k) if isinstance(k, str) else k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul(v) for v in value)
    return value


def _maybe_jsonb(value: Any) -> Any:
    if value is None:
        return None
    try:
        from psycopg.types.json import Jsonb
    except Exception:
        return _strip_nul(_json_value(value))
    return Jsonb(_strip_nul(_json_value(value)))


def _item_write_value(column: str, item: dict[str, Any]) -> Any:
    value = item.get(column)
    if column in REMOTE_ITEM_JSONB_COLUMNS:
        return _maybe_jsonb(value)
    if column in REMOTE_ITEM_TIMESTAMP_COLUMNS:
        return _timestamp_value(value)
    if column == "visible" and value is None:
        return 1
    return value


REMOTE_ITEM_LIGHT_UPDATE_COLUMNS = {
    "title",
    "content",
    "author_name",
    "cover_url",
    "media_json",
    "metrics_json",
    "detail_json",
    "comments_json",
    "published_at",
}


def update_item_light_fields_remote(
    pg_conn: Any | None,
    item_id: str,
    updates: dict[str, Any],
) -> None:
    """Update mutable item fields without attempting an insert.

    ``upsert_item_remote`` must provide required insert columns such as platform
    and source. Submit-url refreshes for existing items only need to enrich a few
    display fields, so an UPDATE avoids tripping insert-time NOT NULL checks.
    """
    if pg_conn is None:
        with connect() as conn:
            update_item_light_fields_remote(conn, item_id, updates)
            return

    clean_updates: dict[str, Any] = {}
    for column in REMOTE_ITEM_LIGHT_UPDATE_COLUMNS:
        if column not in updates:
            continue
        value = updates.get(column)
        if value is None:
            continue
        clean_updates[column] = _item_write_value(column, updates)

    if not clean_updates:
        return

    columns = sorted(clean_updates)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    values = [clean_updates[column] for column in columns]
    pg_conn.execute(
        f"UPDATE {remote_schema()}.items SET {assignments} WHERE id = %s",
        values + [item_id],
    )


def _commit_if_supported(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _executemany(pg_conn: Any, sql: str, rows: list[tuple] | list[list]) -> None:
    cursor_factory = getattr(pg_conn, "cursor", None)
    if callable(cursor_factory):
        with pg_conn.cursor() as cur:
            cur.executemany(sql, rows)
        return
    for row in rows:
        pg_conn.execute(sql, row)


REMOTE_ID_SEQUENCES = {
    "fetch_runs": "fetch_runs_id_seq",
    "clusters": "clusters_id_seq",
    "cluster_judge_log": "cluster_judge_log_id_seq",
}


def _ensure_remote_id_sequence(pg_conn: Any, table: str) -> None:
    """Keep Postgres id sequences ahead of rows imported with SQLite ids."""
    sequence = REMOTE_ID_SEQUENCES[table]
    schema = remote_schema()
    pg_conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
        (f"{schema}.{table}.id_sequence",),
    )
    pg_conn.execute(
        f"""SELECT setval(
              '{schema}.{sequence}'::regclass,
              greatest(coalesce((select max(id) from {schema}.{table}), 0), 1),
              true
            )"""
    )


def start_fetch_run_remote(pg_conn: Any | None = None) -> int:
    """Create a fetch run in Supabase and return its id."""
    if pg_conn is not None:
        _ensure_remote_id_sequence(pg_conn, "fetch_runs")
        row = pg_conn.execute(
            f"INSERT INTO {remote_schema()}.fetch_runs (started_at, status) "
            "VALUES (%s, %s) RETURNING id",
            (datetime.now(timezone.utc), "running"),
        ).fetchone()
        _commit_if_supported(pg_conn)
        return int(row["id"] if isinstance(row, dict) else row[0])

    with connect() as conn:
        return start_fetch_run_remote(conn)


def fetch_run_heartbeat_grace_seconds() -> int:
    return _env_int(
        _runtime_env(),
        FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV,
        600,
        min_value=60,
    )


def touch_fetch_run_heartbeat_remote(
    pg_conn: Any | None = None,
    *,
    run_id: int,
    owner: str,
    touched_at: datetime | None = None,
) -> None:
    """Refresh the remote fetch-run lease for a live backend process."""
    if pg_conn is None:
        with connect() as conn:
            touch_fetch_run_heartbeat_remote(
                conn,
                run_id=run_id,
                owner=owner,
                touched_at=touched_at,
            )
            return

    heartbeat_at = touched_at or datetime.now(timezone.utc)
    payload = {
        "_heartbeat_at": heartbeat_at.isoformat(),
        "_heartbeat_owner": str(owner or "")[:160],
    }
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET stats_json = COALESCE(stats_json, '{{}}'::jsonb) || %s
             WHERE id = %s
               AND status = 'running'""",
        (_maybe_jsonb(payload), int(run_id)),
    )
    _commit_if_supported(pg_conn)


def finish_fetch_run_remote(
    pg_conn: Any | None,
    run_id: int,
    stats: dict[str, Any] | Any,
    error: str | None = None,
) -> None:
    """Mark a Supabase fetch run complete."""
    if pg_conn is None:
        with connect() as conn:
            finish_fetch_run_remote(conn, run_id, stats, error)
            return

    stats_payload = dict(stats or {}) if isinstance(stats, dict) else {"value": stats}
    finished_at = datetime.now(timezone.utc)
    try:
        stats_payload["_audit"] = build_fetch_run_audit_summary_remote(
            pg_conn,
            run_id,
            stats_payload,
            finished_at=finished_at,
        )
    except Exception as exc:
        _rollback_safely(pg_conn)
        stats_payload["_audit_error"] = str(exc)[:200]
        print(
            f"[remote-db] failed to build fetch-run audit snapshot for run {run_id}: {exc}",
            flush=True,
        )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET finished_at = %s,
                   status = %s,
                   stats_json = %s,
                   error_msg = %s
             WHERE id = %s""",
        (
            finished_at,
            "error" if error else "done",
            _maybe_jsonb(stats_payload),
            error,
            run_id,
        ),
    )
    _commit_if_supported(pg_conn)


def mark_orphaned_fetch_runs_remote(
    pg_conn: Any | None = None,
    *,
    started_before: datetime,
    heartbeat_stale_before: datetime | None = None,
    reason: str,
    limit: int = 20,
) -> list[int]:
    """Mark running remote fetch runs from a previous backend process as interrupted."""
    if pg_conn is None:
        with connect() as conn:
            return mark_orphaned_fetch_runs_remote(
                conn,
                started_before=started_before,
                heartbeat_stale_before=heartbeat_stale_before,
                reason=reason,
                limit=limit,
            )

    finished_at = datetime.now(timezone.utc)
    stale_before = heartbeat_stale_before or (
        finished_at - timedelta(seconds=fetch_run_heartbeat_grace_seconds())
    )
    stats_payload = {
        "_result_status": "interrupted",
        "_interrupted_at": finished_at.isoformat(),
        "_interrupted_reason": reason,
        "_orphaned_fetch_recovery": True,
    }
    rows = pg_conn.execute(
        f"""WITH orphaned AS (
                SELECT id
                  FROM {remote_schema()}.fetch_runs
                 WHERE status = 'running'
                   AND started_at < %s
                   AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at) < %s
                 ORDER BY started_at ASC
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE {remote_schema()}.fetch_runs fr
               SET finished_at = %s,
                   status = %s,
                   error_msg = %s,
                   stats_json = COALESCE(fr.stats_json, '{{}}'::jsonb) || %s
              FROM orphaned
             WHERE fr.id = orphaned.id
             RETURNING fr.id""",
        (
            started_before,
            stale_before,
            max(1, int(limit or 20)),
            finished_at,
            "error",
            reason,
            _maybe_jsonb(stats_payload),
        ),
    ).fetchall()
    _commit_if_supported(pg_conn)
    return [int(row["id"] if isinstance(row, dict) else row[0]) for row in rows]


def mark_fetch_runs_interrupted_remote(
    pg_conn: Any | None = None,
    *,
    run_ids: list[int] | tuple[int, ...],
    reason: str,
) -> list[int]:
    """Mark known in-process remote fetch runs as interrupted."""
    normalized_run_ids = sorted({int(run_id) for run_id in run_ids if run_id is not None})
    if not normalized_run_ids:
        return []
    if pg_conn is None:
        with connect() as conn:
            return mark_fetch_runs_interrupted_remote(
                conn,
                run_ids=normalized_run_ids,
                reason=reason,
            )

    finished_at = datetime.now(timezone.utc)
    stats_payload = {
        "_result_status": "interrupted",
        "_interrupted_at": finished_at.isoformat(),
        "_interrupted_reason": reason,
        "_shutdown_interruption": True,
    }
    rows = pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET finished_at = %s,
                   status = %s,
                   error_msg = %s,
                   stats_json = COALESCE(stats_json, '{{}}'::jsonb) || %s
             WHERE status = 'running'
               AND id = ANY(%s)
             RETURNING id""",
        (
            finished_at,
            "error",
            reason,
            _maybe_jsonb(stats_payload),
            normalized_run_ids,
        ),
    ).fetchall()
    _commit_if_supported(pg_conn)
    return [int(row["id"] if isinstance(row, dict) else row[0]) for row in rows]


def _parse_remote_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            return None


def _elapsed_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = _parse_remote_datetime(started_at)
    end = _parse_remote_datetime(finished_at)
    if not start or not end:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    return max(0.0, round((end - start).total_seconds(), 2))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_has_item_records_remote(conn: Any, run_id: int) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {remote_schema()}.fetch_run_items WHERE run_id = %s LIMIT 1",
        (run_id,),
    ).fetchone()
    return row is not None


def _new_run_items_sql_remote(conn: Any, run_id: int) -> tuple[str, dict[str, Any], str]:
    schema = remote_schema()
    if _run_has_item_records_remote(conn, run_id):
        return (
            f"""SELECT i.*
                  FROM {schema}.fetch_run_items fri
                  JOIN {schema}.items i ON i.id = fri.item_id
                 WHERE fri.run_id = %(run_id)s
                   AND fri.was_inserted = 1""",
            {"run_id": run_id},
            "fetch_run_items",
        )
    return (
        f"""SELECT i.*
              FROM {schema}.items i
              JOIN {schema}.fetch_runs r ON r.id = %(run_id)s
             WHERE i.fetch_run_id = r.id
               AND i.created_at >= r.started_at
               AND i.created_at <= coalesce(r.finished_at, now())""",
        {"run_id": run_id},
        "created_at_fallback",
    )


def _remote_pill_from_item(row: dict[str, Any]) -> str:
    cats = _json_value(row.get("ai_categories"))
    if isinstance(cats, list) and cats:
        return str(cats[0] or "_uncategorized")
    return canonicalize_category(row.get("ai_category")) or "_uncategorized"


def _extract_fetch_errors(stats: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(stats, dict):
        return errors
    for key, value in stats.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            for err in value.get("errors") or []:
                errors.append({"scope": str(key), "message": str(err)})
        elif isinstance(value, list):
            for err in value:
                errors.append({"scope": str(key), "message": str(err)})
    return errors[:20]


def build_fetch_run_audit_summary_remote(
    conn: Any,
    run_id: int,
    raw_stats: dict[str, Any] | None = None,
    finished_at: Any = None,
) -> dict[str, Any]:
    schema = remote_schema()
    run = conn.execute(
        f"SELECT * FROM {schema}.fetch_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run:
        return {}
    run_data = dict(run)
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    if _run_has_item_records_remote(conn, run_id):
        source = "fetch_run_items"
        item_rows = conn.execute(
            f"""SELECT i.platform, i.source, i.ai_summary, i.ai_error_count, i.ai_last_error,
                       i.cluster_id, i.ai_categories, i.ai_category
                  FROM {schema}.fetch_run_items fri
                  JOIN {schema}.items i ON i.id = fri.item_id
                 WHERE fri.run_id = %s
                   AND fri.was_inserted = 1""",
            (run_id,),
        ).fetchall()
    else:
        source = "created_at_fallback"
        item_rows = conn.execute(
            f"""SELECT i.platform, i.source, i.ai_summary, i.ai_error_count, i.ai_last_error,
                       i.cluster_id, i.ai_categories, i.ai_category
                  FROM {schema}.items i
                  JOIN {schema}.fetch_runs r ON r.id = %s
                 WHERE i.fetch_run_id = r.id
                   AND i.created_at >= r.started_at
                   AND i.created_at <= coalesce(r.finished_at, now())""",
            (run_id,),
        ).fetchall()
    item_rows = [dict(r) for r in item_rows]

    platform_source_counts: dict[tuple[str, str], int] = {}
    platform_counts: dict[str, int] = {}
    pill_count_map: dict[str, int] = {}
    summarized = 0
    ai_failed = 0
    clustered_items = 0
    touched_cluster_ids: set[Any] = set()

    for row in item_rows:
        platform = row.get("platform") or "unknown"
        source_name = row.get("source") or "unknown"
        platform_source_counts[(platform, source_name)] = platform_source_counts.get((platform, source_name), 0) + 1
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

        if row.get("ai_summary"):
            summarized += 1
        if int(row.get("ai_error_count") or 0) > 0 or row.get("ai_last_error") is not None:
            ai_failed += 1
        cluster_id = row.get("cluster_id")
        if cluster_id is not None:
            clustered_items += 1
            touched_cluster_ids.add(cluster_id)
        pill = _remote_pill_from_item(row)
        pill_count_map[pill] = pill_count_map.get(pill, 0) + 1

    platform_source = [
        {"platform": platform, "source": source_name, "count": count}
        for (platform, source_name), count in sorted(
            platform_source_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]
    pill_counts = [
        {"pill": pill, "count": count}
        for pill, count in sorted(pill_count_map.items(), key=lambda item: (-item[1], item[0]))
    ]

    ended_at = finished_at or run_data.get("finished_at")
    stage_durations = raw_stats.get("_stage_durations_sec") or raw_stats.get("stage_durations_sec") or {}
    result_status = raw_stats.get("_result_status")
    total_new = len(item_rows)
    touched_clusters = len(touched_cluster_ids)
    published_clusters = _optional_int(raw_stats.get("_published_clusters_count"))
    if published_clusters is None:
        published_row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {schema}.clusters WHERE published_run_id = %s",
            (run_id,),
        ).fetchone()
        published_clusters = int((published_row or {}).get("count") or 0)
    return {
        "version": "v15.2",
        "run_id": run_id,
        "source": source,
        "duration_sec": _elapsed_seconds(run_data.get("started_at"), ended_at),
        "stage_durations_sec": stage_durations if isinstance(stage_durations, dict) else {},
        "result_status": result_status,
        "new_items_count": total_new,
        "platform_counts": [
            {"platform": key, "count": value}
            for key, value in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "platform_source_counts": platform_source,
        "pill_counts": pill_counts,
        "ai_summary": {
            "summarized": summarized,
            "failed": ai_failed,
            "pending": max(0, total_new - summarized - ai_failed),
        },
        "event_cluster": {
            "clustered_items": clustered_items,
            "touched_clusters": touched_clusters,
            "published_clusters": published_clusters,
        },
        "errors": _extract_fetch_errors(raw_stats),
    }


def _fetch_run_to_audit_remote(conn: Any, row: Any, *, build_missing_audit: bool = True) -> dict[str, Any]:
    data = dict(row)
    stats = _json_value(data.get("stats_json"))
    stats = stats if isinstance(stats, dict) else {}
    audit = stats.get("_audit") if isinstance(stats, dict) else None
    if not isinstance(audit, dict):
        stage_durations = stats.get("_stage_durations_sec") or stats.get("stage_durations_sec") or {}
        total_new_items = _optional_int(data.get("total_new_items"))
        if total_new_items is None:
            total_new_items = _optional_int(stats.get("_new_items_count"))
        ai_summarized = _optional_int(data.get("ai_summarized"))
        if ai_summarized is None:
            ai_summarized = _optional_int(stats.get("_ai_summarized"))
        ai_failed = _optional_int(data.get("ai_failed"))
        if ai_failed is None:
            ai_failed = _optional_int(stats.get("_ai_failed"))
        clustered_items = _optional_int(data.get("clustered_items"))
        if clustered_items is None:
            clustered_items = _optional_int(stats.get("_clustered_items"))
        touched_clusters = _optional_int(data.get("touched_clusters"))
        if touched_clusters is None:
            touched_clusters = _optional_int(stats.get("_touched_clusters"))
        published_clusters = _optional_int(data.get("published_clusters"))
        if published_clusters is None:
            published_clusters = _optional_int(stats.get("_published_clusters_count"))
        audit = (
            build_fetch_run_audit_summary_remote(conn, int(data["id"]), stats)
            if build_missing_audit
            else {
                "version": "v15.2",
                "run_id": int(data["id"]),
                "new_items_count": total_new_items,
                "stage_durations_sec": stage_durations if isinstance(stage_durations, dict) else {},
                "result_status": stats.get("_result_status"),
                "platform_counts": [],
                "platform_source_counts": [],
                "pill_counts": [],
                "ai_summary": {
                    "summarized": ai_summarized,
                    "failed": ai_failed,
                    "pending": (
                        max(0, total_new_items - (ai_summarized or 0) - (ai_failed or 0))
                        if (
                            total_new_items is not None
                            and (ai_summarized is not None or ai_failed is not None)
                        )
                        else None
                    ),
                },
                "event_cluster": {
                    "clustered_items": clustered_items,
                    "touched_clusters": touched_clusters,
                    "published_clusters": published_clusters,
                },
                "errors": _extract_fetch_errors(stats),
            }
        )
    data["started_at"] = _timestamp_value(data.get("started_at"))
    data["finished_at"] = _timestamp_value(data.get("finished_at"))
    data["stats"] = stats
    data["audit"] = audit
    data["duration_sec"] = audit.get("duration_sec") or _elapsed_seconds(
        data.get("started_at"),
        data.get("finished_at"),
    )
    data["total_new_items"] = audit.get("new_items_count")
    data.pop("stats_json", None)
    return data


def list_fetch_run_audits_remote(
    limit: int = 50,
    offset: int = 0,
    *,
    pg_conn: Any | None = None,
    build_missing_audit: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    cache_key = (
        "admin_fetch_runs_result",
        remote_schema(),
        limit,
        offset,
        bool(build_missing_audit),
    )
    if pg_conn is None:
        cached = _cache_get_copy(cache_key)
        if cached is not None:
            return cached

        def _compute() -> list[dict[str, Any]]:
            cached_inside = _cache_get_copy(cache_key)
            if cached_inside is not None:
                return cached_inside
            with connect() as conn:
                result = _list_fetch_run_audits_remote_uncached(
                    conn,
                    limit=limit,
                    offset=offset,
                    build_missing_audit=build_missing_audit,
                )
            return _cache_set_copy(cache_key, result)

        return _singleflight_sync(cache_key, _compute)

    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        return _list_fetch_run_audits_remote_uncached(
            conn,
            limit=limit,
            offset=offset,
            build_missing_audit=build_missing_audit,
        )
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def _list_fetch_run_audits_remote_uncached(
    conn: Any,
    *,
    limit: int,
    offset: int,
    build_missing_audit: bool = False,
) -> list[dict[str, Any]]:
    schema = remote_schema()
    rows = conn.execute(
        f"""SELECT id, started_at, finished_at, status, stats_json, error_msg
              FROM {schema}.fetch_runs
             ORDER BY id DESC
             LIMIT %s OFFSET %s""",
        (limit, offset),
    ).fetchall()
    if not rows:
        return []

    return [
        _fetch_run_to_audit_remote(conn, row, build_missing_audit=build_missing_audit)
        for row in rows
    ]


def get_fetch_run_audit_remote(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.fetch_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        return _fetch_run_to_audit_remote(conn, row) if row else None


def query_fetch_run_audit_items_remote(
    run_id: int,
    *,
    platform: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or 50), 100))
    bounded_offset = max(0, int(offset or 0))
    with connect() as conn:
        if not conn.execute(
            f"SELECT 1 FROM {remote_schema()}.fetch_runs WHERE id = %s",
            (run_id,),
        ).fetchone():
            return {"missing_run": True}
        items_sql, item_params, source_kind = _new_run_items_sql_remote(conn, run_id)
        where = []
        params = dict(item_params)
        if platform:
            where.append("ni.platform = %(platform)s")
            params["platform"] = platform
        if source:
            where.append("ni.source = %(source)s")
            params["source"] = source
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM ({items_sql}) ni {where_sql}",
            params,
        ).fetchone()
        rows = conn.execute(
            f"""SELECT ni.id, ni.title, ni.platform, ni.source, ni.url,
                       ni.ai_summary, ni.ai_error_count, ni.ai_last_error,
                       ni.ai_category, ni.ai_categories, ni.cluster_id,
                       ni.created_at, ni.fetched_at
                  FROM ({items_sql}) ni
                  {where_sql}
                 ORDER BY ni.created_at DESC NULLS LAST, ni.id DESC
                 LIMIT %(limit)s OFFSET %(offset)s""",
            {**params, "limit": bounded_limit, "offset": bounded_offset},
        ).fetchall()
    items = []
    for raw in rows:
        item = dict(raw)
        item["created_at"] = _timestamp_value(item.get("created_at"))
        item["fetched_at"] = _timestamp_value(item.get("fetched_at"))
        item["pill"] = _remote_pill_from_item(item)
        item["ai_status"] = "failed" if (item.get("ai_error_count") or 0) > 0 or item.get("ai_last_error") else (
            "summarized" if item.get("ai_summary") else "pending"
        )
        item["cluster_status"] = "clustered" if item.get("cluster_id") is not None else "pending"
        items.append(item)
    return {
        "items": items,
        "total": int(total["count"] if total else 0),
        "source": source_kind,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


def _embedding_usage_where_remote(hours: float | None = 24, run_id: int | None = None) -> tuple[str, dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {}
    if hours is not None:
        try:
            hours_float = float(hours)
        except (TypeError, ValueError):
            hours_float = 24.0
        if hours_float > 0:
            params["since"] = datetime.now(timezone.utc) - timedelta(hours=hours_float)
            clauses.append("created_at >= %(since)s")
    if run_id is not None:
        params["run_id"] = int(run_id)
        clauses.append("run_id = %(run_id)s")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _normalize_embedding_usage_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    if "created_at" in data:
        data["created_at"] = _timestamp_value(data.get("created_at"))
    if "item_ids_json" in data:
        data["item_ids_json"] = _json_value(data.get("item_ids_json"))
    return data


def record_embedding_usage_remote(log: dict[str, Any], pg_conn: Any | None = None) -> int | None:
    """Persist one embedding provider call in the remote audit table."""
    if not isinstance(log, dict):
        return None

    payload = dict(log)
    payload.setdefault("created_at", datetime.now(timezone.utc))
    item_ids = payload.get("item_ids_json")
    if isinstance(item_ids, tuple):
        item_ids = list(item_ids)
    if isinstance(item_ids, str):
        item_ids = _json_value(item_ids)
    payload["item_ids_json"] = _maybe_jsonb(item_ids) if item_ids is not None else None

    columns = (
        "created_at",
        "provider",
        "model",
        "mode",
        "source",
        "stage",
        "run_id",
        "caller_file",
        "caller_func",
        "input_count",
        "input_chars",
        "input_bytes",
        "estimated_tokens",
        "token_estimator",
        "output_count",
        "output_dim",
        "status",
        "error",
        "latency_ms",
        "price_yuan_per_1k_tokens",
        "estimated_cost_yuan",
        "item_ids_json",
    )
    values = [payload.get(col) for col in columns]

    def _insert(conn: Any) -> int | None:
        row = conn.execute(
            f"""INSERT INTO {remote_schema()}.embedding_usage_logs ({','.join(columns)})
                VALUES ({','.join(['%s'] * len(columns))})
                RETURNING id""",
            values,
        ).fetchone()
        conn.commit()
        if row is None:
            return None
        return int(row["id"] if hasattr(row, "keys") else row[0])

    if pg_conn is not None:
        return _insert(pg_conn)
    try:
        with connect() as conn:
            return _insert(conn)
    except Exception:
        rest_id = _record_embedding_usage_remote_rest(payload)
        if rest_id is not None:
            return rest_id
        raise


def _record_embedding_usage_remote_rest(payload: dict[str, Any]) -> int | None:
    """Best-effort Supabase REST fallback when the Postgres pooler is saturated."""
    try:
        url = f"{supabase_project_url()}/rest/v1/embedding_usage_logs"
        key = supabase_service_role_key()
        body = {}
        for key_name, value in payload.items():
            if value is None:
                body[key_name] = None
            elif key_name == "item_ids_json":
                body[key_name] = _json_value(getattr(value, "obj", value))
            elif isinstance(value, datetime):
                body[key_name] = value.isoformat()
            else:
                body[key_name] = value
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Profile": remote_schema(),
                "Prefer": "return=representation",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        rows = json.loads(raw) if raw else []
        if isinstance(rows, list) and rows:
            row = rows[0]
            if isinstance(row, dict) and row.get("id") is not None:
                return int(row["id"])
    except Exception:
        return None
    return None


def get_embedding_usage_audit_remote(
    *,
    hours: float = 24,
    run_id: int | None = None,
    limit: int = 100,
    pg_conn: Any | None = None,
) -> dict[str, Any]:
    schema = remote_schema()
    where, params = _embedding_usage_where_remote(hours=hours, run_id=run_id)
    bounded_limit = max(1, min(int(limit or 100), 500))
    cache_key = (
        "admin_embedding_usage_result",
        schema,
        float(hours or 24),
        run_id,
        bounded_limit,
    )
    if pg_conn is None:
        cached = _cache_get_copy(cache_key)
        if cached is not None:
            return cached

        def _compute() -> dict[str, Any]:
            cached_inside = _cache_get_copy(cache_key)
            if cached_inside is not None:
                return cached_inside
            result = get_embedding_usage_audit_remote(
                hours=hours,
                run_id=run_id,
                limit=bounded_limit,
                pg_conn=False,
            )
            return _cache_set_copy(cache_key, result)

        return _singleflight_sync(cache_key, _compute)

    conn_cm = None
    if pg_conn is None or pg_conn is False:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        summary = conn.execute(
            f"""SELECT COUNT(*) AS total_calls,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                       COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failed_calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(input_chars), 0) AS input_chars,
                       COALESCE(SUM(input_bytes), 0) AS input_bytes,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens_attempted,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_tokens ELSE 0 END), 0) AS estimated_tokens_success,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_cost_yuan ELSE 0 END), 0.0) AS estimated_cost_yuan_success,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan_all
                  FROM {schema}.embedding_usage_logs
                  {where}""",
            params,
        ).fetchone()
        by_source = conn.execute(
            f"""SELECT COALESCE(source, 'unknown') AS source,
                       COALESCE(stage, '') AS stage,
                       provider,
                       model,
                       status,
                       COUNT(*) AS calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(input_chars), 0) AS input_chars,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
                  FROM {schema}.embedding_usage_logs
                  {where}
                 GROUP BY source, stage, provider, model, status
                 ORDER BY estimated_tokens DESC, calls DESC
                 LIMIT 50""",
            params,
        ).fetchall()
        by_run = conn.execute(
            f"""SELECT run_id,
                       COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
                  FROM {schema}.embedding_usage_logs
                  {where}
                 GROUP BY run_id
                 ORDER BY MAX(created_at) DESC
                 LIMIT 50""",
            params,
        ).fetchall()
        rows = conn.execute(
            f"""SELECT *
                  FROM {schema}.embedding_usage_logs
                  {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT %(limit)s""",
            {**params, "limit": bounded_limit},
        ).fetchall()
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)
    return {
        "hours": hours,
        "run_id": run_id,
        "summary": dict(summary) if summary else {},
        "by_source": [dict(r) for r in by_source],
        "by_run": [dict(r) for r in by_run],
        "logs": [_normalize_embedding_usage_row(r) for r in rows],
        "limit": bounded_limit,
    }


ADMIN_CONSOLE_TZ = ZoneInfo("Asia/Shanghai")


def _admin_console_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(ADMIN_CONSOLE_TZ).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ADMIN_CONSOLE_TZ).replace(microsecond=0)


def _admin_console_to_shanghai_iso(value: Any) -> str | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else parse_datetime(str(value))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ADMIN_CONSOLE_TZ).replace(microsecond=0).isoformat()


def _admin_console_age_hours(value: Any, now_utc: datetime) -> float | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else parse_datetime(str(value))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 3600)


def _admin_console_age_text(value: Any, now_utc: datetime) -> str:
    age = _admin_console_age_hours(value, now_utc)
    if age is None:
        return "时间未知"
    if age < 1:
        return f"{max(0, int(age * 60))}m 前"
    return f"{int(age)}h 前"


def _admin_console_percent(value: float) -> str:
    percent = value * 100
    if abs(percent - round(percent)) < 0.05:
        return f"{int(round(percent))}%"
    return f"{percent:.1f}%"


def _admin_console_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _admin_console_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _admin_console_table_has_columns(
    conn: Any,
    schema: str,
    table_name: str,
    column_names: set[str],
) -> bool:
    rows = conn.execute(
        """SELECT column_name
             FROM information_schema.columns
            WHERE table_schema = %(schema_name)s
              AND table_name = %(table_name)s
              AND column_name = ANY(%(column_names)s)""",
        {
            "schema_name": schema,
            "table_name": table_name,
            "column_names": sorted(column_names),
        },
    ).fetchall()
    found = {str(row["column_name"]) for row in rows}
    return column_names.issubset(found)


def _admin_console_date_points(now_shanghai: datetime, days: int, value: Any) -> list[dict[str, Any]]:
    start = now_shanghai.date() - timedelta(days=days - 1)
    return [
        {"date": (start + timedelta(days=idx)).isoformat(), "value": value}
        for idx in range(days)
    ]


def _admin_console_trend(rows: list[Any], now_shanghai: datetime, days: int, default: Any) -> list[dict[str, Any]]:
    values = {str(row["date"]): row.get("value") for row in rows}
    points = _admin_console_date_points(now_shanghai, days, default)
    for point in points:
        if point["date"] in values:
            value = values[point["date"]]
            point["value"] = None if value is None else value
    return points


def _admin_console_embedding_signal(total_calls: int | None, failed_calls: int | None) -> dict[str, Any]:
    signal = {
        "key": "embedding",
        "level": "unknown",
        "label": "Embedding",
        "detail": "24h 无 embedding 调用",
        "link": "runs",
    }
    if total_calls is None:
        signal["detail"] = "embedding 数据不可用"
        return signal
    if total_calls <= 0:
        return signal
    failed = int(failed_calls or 0)
    failure_rate = failed / total_calls
    if failure_rate == 0:
        level = "ok"
    elif failure_rate < 0.10:
        level = "warn"
    else:
        level = "crit"
    signal.update({
        "level": level,
        "detail": f"24h 失败率 {_admin_console_percent(failure_rate)}（{total_calls} 次）",
    })
    return signal


def _admin_console_disk_signal(used_percent: float | None, db_size: str | None = None) -> dict[str, Any]:
    signal = {
        "key": "disk",
        "level": "unknown",
        "label": "磁盘",
        "detail": "磁盘信息不可用",
        "link": None,
    }
    if used_percent is None:
        return signal
    if used_percent > 90:
        level = "crit"
    elif used_percent >= 80:
        level = "warn"
    else:
        level = "ok"
    db_text = db_size or "DB 未知"
    signal.update({
        "level": level,
        "detail": f"已用 {int(round(used_percent))}% · DB {db_text}",
    })
    return signal


def _admin_console_pipeline_signal(
    latest_run: dict[str, Any] | None,
    counts: dict[str, Any] | None,
    now_utc: datetime,
) -> dict[str, Any]:
    signal = {
        "key": "pipeline",
        "level": "unknown",
        "label": "抓取 Pipeline",
        "detail": "无任何 run 记录",
        "link": "runs",
    }
    if not latest_run:
        return signal

    status_text = str(latest_run.get("status") or "unknown").lower()
    success_24h = int((counts or {}).get("success_runs_24h") or 0)
    success_48h = int((counts or {}).get("success_runs_48h") or 0)
    total_24h = int((counts or {}).get("total_runs_24h") or 0)
    run_time = latest_run.get("finished_at") or latest_run.get("started_at")
    detail = f"run #{latest_run.get('id')} {status_text} · {_admin_console_age_text(run_time, now_utc)}"

    if status_text in {"failed", "error"} or status_text.startswith("failed") or success_48h == 0:
        level = "crit"
    elif "partial" in status_text or total_24h == 0 or success_24h == 0:
        level = "warn"
    elif status_text == "success" and success_24h > 0:
        level = "ok"
    else:
        level = "warn"
    signal.update({"level": level, "detail": detail})
    return signal


def _admin_console_freshness_signal(rows: list[Any], now_utc: datetime) -> dict[str, Any]:
    signal = {
        "key": "freshness",
        "level": "unknown",
        "label": "平台新鲜度",
        "detail": "无平台抓取记录",
        "link": "runs",
    }
    platform_ages: list[tuple[str, float, Any]] = []
    for row in rows:
        platform = str(row.get("platform") or "unknown")
        last_fetched_at = row.get("last_fetched_at")
        age = _admin_console_age_hours(last_fetched_at, now_utc)
        if age is not None:
            platform_ages.append((platform, age, last_fetched_at))
    if not platform_ages:
        return signal

    worst = max(platform_ages, key=lambda item: item[1])
    level = classify_platform_freshness(worst[1])
    signal.update({
        "level": level,
        "detail": f"{worst[0]} 最近抓取 {int(worst[1])}h 前",
    })
    return signal


def _admin_console_remote_db_signal(started_at: float) -> dict[str, Any]:
    status()
    elapsed_ms = max(0, int(round((time.monotonic() - started_at) * 1000)))
    return {
        "key": "remote_db",
        "level": "ok",
        "label": "远程 DB",
        "detail": f"可达 · {elapsed_ms}ms",
        "link": None,
    }


def _admin_console_incidents(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incidents = [
        {
            "severity": signal["level"],
            "text": f"{signal['label']}：{signal['detail']}",
            "link": signal.get("link"),
        }
        for signal in signals
        if signal.get("level") in {"warn", "crit"}
    ]
    incidents.sort(key=lambda item: 0 if item["severity"] == "crit" else 1)
    return incidents[:5]


def _admin_console_db_size(conn: Any) -> str | None:
    row = conn.execute(
        "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size"
    ).fetchone()
    return (row or {}).get("db_size")


def _admin_console_disk_usage_percent() -> float | None:
    try:
        usage = shutil.disk_usage("/")
    except Exception:
        return None
    total = getattr(usage, "total", 0) or 0
    if total <= 0:
        return None
    return (float(getattr(usage, "used", 0) or 0) / float(total)) * 100


def admin_console_summary_remote(*, now: datetime | None = None) -> dict[str, Any]:
    now_shanghai = _admin_console_now(now)
    now_utc = now_shanghai.astimezone(timezone.utc)
    today = now_shanghai.date().isoformat()
    start_14d = (now_shanghai.date() - timedelta(days=13)).isoformat()
    start_7d = (now_shanghai.date() - timedelta(days=6)).isoformat()
    since_1d = now_utc - timedelta(days=1)
    since_7d = now_utc - timedelta(days=7)
    since_24h = now_utc - timedelta(hours=24)
    since_48h = now_utc - timedelta(hours=48)

    c_metrics = {
        "total_users": None,
        "new_users_today": None,
        "new_users_7d": None,
        "active_users_1d": None,
        "active_users_7d": None,
        "info_click_users_7d": None,
        "info_click_items_7d": None,
        "info_click_items_total": None,
        "highlight_click_users_7d": None,
        "highlight_click_events_7d": None,
        "highlight_click_events_total": None,
    }
    interactions_detail = {
        "starred_users": None,
        "starred_total": None,
        "read_users_7d": None,
        "read_items_7d": None,
        "latest_signup": None,
    }
    cost = {
        "embedding_cost_yuan_24h": None,
        "embedding_calls_24h": None,
    }
    embedding_calls = None
    embedding_failed = None
    schema = remote_schema()
    remote_started_at = time.monotonic()

    with connect() as conn:
        users_ok = _admin_console_table_has_columns(conn, schema, "users", {"id", "username", "created_at"})
        item_status_ok = _admin_console_table_has_columns(
            conn, schema, "item_status", {"user_id", "item_id", "read_at", "clicked_at", "starred_at"}
        )
        cluster_status_ok = _admin_console_table_has_columns(
            conn, schema, "cluster_status", {"user_id", "cluster_id", "clicked_at", "starred_at"}
        )
        fetch_runs_ok = _admin_console_table_has_columns(
            conn, schema, "fetch_runs", {"id", "started_at", "finished_at", "status", "error_msg"}
        )
        items_ok = _admin_console_table_has_columns(conn, schema, "items", {"platform", "fetched_at"})
        embedding_ok = _admin_console_table_has_columns(
            conn, schema, "embedding_usage_logs", {"created_at", "status", "estimated_cost_yuan"}
        )

        if users_ok:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total_users,
                           COUNT(*) FILTER (
                             WHERE timezone('Asia/Shanghai', created_at)::date = %(today)s::date
                           ) AS new_users_today,
                           COUNT(*) FILTER (
                             WHERE created_at >= %(since_7d)s
                           ) AS new_users_7d
                      FROM {schema}.users""",
                {"today": today, "since_7d": since_7d},
            ).fetchone()
            c_metrics["total_users"] = _admin_console_int((row or {}).get("total_users"))
            c_metrics["new_users_today"] = _admin_console_int((row or {}).get("new_users_today"))
            c_metrics["new_users_7d"] = _admin_console_int((row or {}).get("new_users_7d"))

            latest = conn.execute(
                f"""SELECT username, created_at
                      FROM {schema}.users
                     ORDER BY created_at DESC
                     LIMIT 1"""
            ).fetchone()
            if latest:
                interactions_detail["latest_signup"] = {
                    "username": latest.get("username"),
                    "created_at": _admin_console_to_shanghai_iso(latest.get("created_at")),
                }

        if item_status_ok and cluster_status_ok:
            for key, since in (("active_users_1d", since_1d), ("active_users_7d", since_7d)):
                row = conn.execute(
                    f"""SELECT COUNT(DISTINCT user_id) AS active_users
                          FROM (
                            SELECT user_id
                              FROM {schema}.item_status
                             WHERE read_at >= %(since)s
                                OR clicked_at >= %(since)s
                                OR starred_at >= %(since)s
                            UNION
                            SELECT user_id
                              FROM {schema}.cluster_status
                             WHERE clicked_at >= %(since)s
                                OR starred_at >= %(since)s
                          ) active_users""",
                    {"since": since},
                ).fetchone()
                c_metrics[key] = _admin_console_int((row or {}).get("active_users"))

        if item_status_ok:
            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS info_click_users_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS info_click_items_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE clicked_at IS NOT NULL
                           ) AS info_click_items_total
                      FROM {schema}.item_status""",
                {"since_7d": since_7d},
            ).fetchone()
            c_metrics["info_click_users_7d"] = _admin_console_int((row or {}).get("info_click_users_7d"))
            c_metrics["info_click_items_7d"] = _admin_console_int((row or {}).get("info_click_items_7d"))
            c_metrics["info_click_items_total"] = _admin_console_int((row or {}).get("info_click_items_total"))

            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE starred_at IS NOT NULL
                           ) AS starred_users,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE starred_at IS NOT NULL
                           ) AS starred_total,
                           COUNT(DISTINCT user_id) FILTER (
                             WHERE read_at >= %(since_7d)s
                           ) AS read_users_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE read_at >= %(since_7d)s
                           ) AS read_items_7d
                      FROM {schema}.item_status""",
                {"since_7d": since_7d},
            ).fetchone()
            interactions_detail["starred_users"] = _admin_console_int((row or {}).get("starred_users"))
            interactions_detail["starred_total"] = _admin_console_int((row or {}).get("starred_total"))
            interactions_detail["read_users_7d"] = _admin_console_int((row or {}).get("read_users_7d"))
            interactions_detail["read_items_7d"] = _admin_console_int((row or {}).get("read_items_7d"))

        if cluster_status_ok:
            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS highlight_click_users_7d,
                           COUNT(DISTINCT cluster_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS highlight_click_events_7d,
                           COUNT(DISTINCT cluster_id) FILTER (
                             WHERE clicked_at IS NOT NULL
                           ) AS highlight_click_events_total
                      FROM {schema}.cluster_status""",
                {"since_7d": since_7d},
            ).fetchone()
            c_metrics["highlight_click_users_7d"] = _admin_console_int((row or {}).get("highlight_click_users_7d"))
            c_metrics["highlight_click_events_7d"] = _admin_console_int((row or {}).get("highlight_click_events_7d"))
            c_metrics["highlight_click_events_total"] = _admin_console_int((row or {}).get("highlight_click_events_total"))

        if embedding_ok:
            row = conn.execute(
                f"""SELECT COUNT(*) AS embedding_calls_24h,
                           COUNT(*) FILTER (
                             WHERE status != 'success'
                           ) AS embedding_failed_24h,
                           COALESCE(SUM(estimated_cost_yuan), 0.0) AS embedding_cost_yuan_24h
                      FROM {schema}.embedding_usage_logs
                     WHERE created_at >= %(since_24h)s""",
                {"since_24h": since_24h},
            ).fetchone()
            embedding_calls = _admin_console_int((row or {}).get("embedding_calls_24h"))
            embedding_failed = _admin_console_int((row or {}).get("embedding_failed_24h"))
            cost["embedding_calls_24h"] = embedding_calls
            cost["embedding_cost_yuan_24h"] = _admin_console_float((row or {}).get("embedding_cost_yuan_24h"))

        latest_run = None
        pipeline_counts = None
        if fetch_runs_ok:
            latest_run = conn.execute(
                f"""SELECT id, started_at, finished_at, status, error_msg
                      FROM {schema}.fetch_runs
                     ORDER BY id DESC
                     LIMIT 1"""
            ).fetchone()
            pipeline_counts = conn.execute(
                f"""SELECT COUNT(*) FILTER (
                             WHERE COALESCE(finished_at, started_at) >= %(since_24h)s
                           ) AS total_runs_24h,
                           COUNT(*) FILTER (
                             WHERE status = 'success'
                               AND COALESCE(finished_at, started_at) >= %(since_24h)s
                           ) AS success_runs_24h,
                           COUNT(*) FILTER (
                             WHERE status = 'success'
                               AND COALESCE(finished_at, started_at) >= %(since_48h)s
                           ) AS success_runs_48h
                      FROM {schema}.fetch_runs""",
                {"since_24h": since_24h, "since_48h": since_48h},
            ).fetchone()

        freshness_rows = []
        if items_ok:
            freshness_rows = conn.execute(
                f"""SELECT platform, MAX(fetched_at) AS last_fetched_at
                      FROM {schema}.items
                     WHERE platform IS NOT NULL
                     GROUP BY platform"""
            ).fetchall()

        db_size = _admin_console_db_size(conn)

        if users_ok:
            user_trend_rows = conn.execute(
                f"""SELECT to_char(days.day, 'YYYY-MM-DD') AS date,
                           COALESCE(counts.value, 0)::int AS value
                      FROM generate_series(%(start_date)s::date, %(end_date)s::date, interval '1 day') AS days(day)
                 LEFT JOIN (
                           SELECT timezone('Asia/Shanghai', created_at)::date AS day,
                                  COUNT(*)::int AS value
                             FROM {schema}.users
                            WHERE timezone('Asia/Shanghai', created_at)::date >= %(start_date)s::date
                         GROUP BY day
                      ) counts ON counts.day = days.day
                  ORDER BY days.day""",
                {"start_date": start_14d, "end_date": today},
            ).fetchall()
            new_users_14d = _admin_console_trend(user_trend_rows, now_shanghai, 14, 0)
        else:
            new_users_14d = _admin_console_date_points(now_shanghai, 14, None)

        if fetch_runs_ok:
            fetch_trend_rows = conn.execute(
                f"""SELECT to_char(days.day, 'YYYY-MM-DD') AS date,
                           CASE
                             WHEN counts.total_runs IS NULL OR counts.total_runs = 0 THEN NULL
                             ELSE counts.success_runs::float / counts.total_runs
                           END AS value
                      FROM generate_series(%(start_date)s::date, %(end_date)s::date, interval '1 day') AS days(day)
                 LEFT JOIN (
                           SELECT timezone('Asia/Shanghai', COALESCE(finished_at, started_at))::date AS day,
                                  COUNT(*)::int AS total_runs,
                                  COUNT(*) FILTER (WHERE status = 'success')::int AS success_runs
                             FROM {schema}.fetch_runs
                            WHERE COALESCE(finished_at, started_at) IS NOT NULL
                              AND timezone('Asia/Shanghai', COALESCE(finished_at, started_at))::date >= %(start_date)s::date
                         GROUP BY day
                      ) counts ON counts.day = days.day
                  ORDER BY days.day""",
                {"start_date": start_7d, "end_date": today},
            ).fetchall()
            fetch_success_rate_7d = _admin_console_trend(fetch_trend_rows, now_shanghai, 7, None)
        else:
            fetch_success_rate_7d = _admin_console_date_points(now_shanghai, 7, None)

    signals = [
        _admin_console_pipeline_signal(dict(latest_run) if latest_run else None, dict(pipeline_counts) if pipeline_counts else None, now_utc),
        _admin_console_freshness_signal(freshness_rows, now_utc),
        _admin_console_embedding_signal(embedding_calls, embedding_failed),
        _admin_console_remote_db_signal(remote_started_at),
        _admin_console_disk_signal(_admin_console_disk_usage_percent(), db_size),
    ]

    return {
        "available": True,
        "generated_at": now_shanghai.isoformat(),
        "c_metrics": c_metrics,
        "interactions_detail": interactions_detail,
        "cost": cost,
        "health": {
            "signals": signals,
            "incidents": _admin_console_incidents(signals),
        },
        "trends": {
            "new_users_14d": new_users_14d,
            "fetch_success_rate_7d": fetch_success_rate_7d,
        },
    }


def admin_overview_remote(
    *,
    fetch_run_limit: int = 20,
    fetch_run_offset: int = 0,
    embedding_hours: float = 24,
    embedding_limit: int = 50,
    include_embedding: bool = False,
) -> dict[str, Any]:
    """Load initial admin dashboard data with one auth pass and one DB checkout."""
    limit = max(1, min(int(fetch_run_limit or 20), 100))
    offset = max(0, int(fetch_run_offset or 0))
    cache_key = (
        "admin_overview_result",
        remote_schema(),
        limit,
        offset,
        float(embedding_hours),
        max(1, min(int(embedding_limit or 50), 500)),
        bool(include_embedding),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached

    def _compute() -> dict[str, Any]:
        cached_inside = _cache_get_copy(cache_key)
        if cached_inside is not None:
            return cached_inside
        result = _admin_overview_remote_uncached(
            fetch_run_limit=limit,
            fetch_run_offset=offset,
            embedding_hours=embedding_hours,
            embedding_limit=embedding_limit,
            include_embedding=include_embedding,
        )
        return _cache_set_copy(cache_key, result)

    return _singleflight_sync(cache_key, _compute)


def _admin_overview_remote_uncached(
    *,
    fetch_run_limit: int = 20,
    fetch_run_offset: int = 0,
    embedding_hours: float = 24,
    embedding_limit: int = 50,
    include_embedding: bool = False,
) -> dict[str, Any]:
    """Load initial admin dashboard data with one auth pass and one DB checkout."""
    with connect() as conn:
        return {
            "codes": list_invite_codes_remote(pg_conn=conn),
            "users": list_users_remote(pg_conn=conn),
            "fetch_runs": {
                "runs": list_fetch_run_audits_remote(
                    limit=fetch_run_limit,
                    offset=fetch_run_offset,
                    pg_conn=conn,
                ),
                "limit": max(1, min(int(fetch_run_limit or 20), 100)),
                "offset": max(0, int(fetch_run_offset or 0)),
            },
            "embedding_usage": (
                get_embedding_usage_audit_remote(
                    hours=embedding_hours,
                    limit=embedding_limit,
                    pg_conn=conn,
                )
                if include_embedding
                else _empty_embedding_usage(embedding_hours, embedding_limit)
            ),
        }


def _empty_embedding_usage(hours: float = 24, limit: int = 50) -> dict[str, Any]:
    return {
        "hours": hours,
        "run_id": None,
        "summary": {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "input_count": 0,
            "input_chars": 0,
            "input_bytes": 0,
            "estimated_tokens_attempted": 0,
            "estimated_tokens_success": 0,
            "output_count": 0,
            "estimated_cost_yuan_success": 0.0,
            "estimated_cost_yuan_all": 0.0,
        },
        "by_source": [],
        "by_run": [],
        "logs": [],
        "limit": max(1, min(int(limit or 50), 500)),
    }


def get_last_fetch_remote() -> dict[str, Any] | None:
    """Return the most recent remote fetch run in the same shape as db.get_last_fetch."""
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT id, started_at, finished_at, status, stats_json, error_msg
                  FROM {remote_schema()}.fetch_runs
                 ORDER BY id DESC
                 LIMIT 1"""
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["started_at"] = _timestamp_value(item.get("started_at"))
    item["finished_at"] = _timestamp_value(item.get("finished_at"))
    item["stats_json"] = _json_value(item.get("stats_json"))
    return item


def has_recent_running_fetch_remote(max_age_minutes: int | None = None) -> bool:
    """Return whether Supabase has a recent in-flight fetch run.

    Old failed finish paths can leave zombie rows with status=running, so this
    guard only treats rows with a fresh heartbeat as active. Scheduler callers
    should fail closed when this query cannot be answered.
    """
    age_minutes = max_age_minutes
    if age_minutes is None:
        age_minutes = _env_int(
            _runtime_env(),
            REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV,
            180,
            min_value=1,
        )
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT EXISTS (
                    SELECT 1
                      FROM {remote_schema()}.fetch_runs
                     WHERE status = 'running'
                       AND started_at >= now() - (%s::int * interval '1 minute')
                       AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at)
                           >= now() - (%s::int * interval '1 second')
                     LIMIT 1
                 ) AS has_running""",
            (int(age_minutes), fetch_run_heartbeat_grace_seconds()),
        ).fetchone()
    if isinstance(row, dict):
        return bool(row.get("has_running"))
    try:
        return bool(row[0])
    except Exception:
        return False


def has_recent_finished_fetch_remote(*, minutes: int = 30) -> bool:
    safe_minutes = max(1, int(minutes or 30))
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT EXISTS (
                    SELECT 1
                      FROM {remote_schema()}.fetch_runs
                     WHERE finished_at IS NOT NULL
                       AND finished_at >= now() - (%s::int * interval '1 minute')
                     LIMIT 1
                 ) AS has_finished""",
            (safe_minutes,),
        ).fetchone()
    return bool(_row_get(row, "has_finished", False))


def remote_db_pressure(
    *,
    timeout_minutes: int | None = None,
    autovacuum_age_sec: int | None = None,
    probe_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Read-only pressure probe used to skip optional DB-heavy work."""
    env = _runtime_env()
    safe_timeout_minutes = (
        int(timeout_minutes)
        if timeout_minutes is not None
        else _env_int(env, REMOTE_DB_PRESSURE_TIMEOUT_MIN_ENV, 15, min_value=1)
    )
    safe_autovacuum_age_sec = (
        int(autovacuum_age_sec)
        if autovacuum_age_sec is not None
        else _env_int(env, REMOTE_DB_PRESSURE_AUTOVACUUM_AGE_SEC_ENV, 1800, min_value=1)
    )
    safe_probe_timeout_ms = (
        int(probe_timeout_ms)
        if probe_timeout_ms is not None
        else _env_int(env, REMOTE_DB_PRESSURE_PROBE_TIMEOUT_MS_ENV, 1500, min_value=100)
    )
    t0 = time.time()
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    try:
        schema = remote_schema()
        with connect() as conn:
            _set_short_statement_timeout(conn, safe_probe_timeout_ms)
            running_row = conn.execute(
                f"""SELECT EXISTS (
                        SELECT 1
                          FROM {schema}.fetch_runs
                         WHERE status = 'running'
                           AND started_at >= now() - interval '3 hours'
                           AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at)
                               >= now() - (%s::int * interval '1 second')
                         LIMIT 1
                     ) AS has_running""",
                (fetch_run_heartbeat_grace_seconds(),),
            ).fetchone()
            if bool(_row_get(running_row, "has_running", False)):
                reasons.append("remote_fetch_running")

            timeout_row = conn.execute(
                f"""SELECT EXISTS (
                        SELECT 1
                          FROM {schema}.fetch_runs
                         WHERE started_at >= now() - (%s::int * interval '1 minute')
                           AND (
                             COALESCE(error_msg, '') ILIKE '%%statement timeout%%'
                             OR COALESCE(stats_json::text, '') ILIKE '%%statement timeout%%'
                           )
                         LIMIT 1
                     ) AS has_recent_timeout""",
                (safe_timeout_minutes,),
            ).fetchone()
            if bool(_row_get(timeout_row, "has_recent_timeout", False)):
                reasons.append("recent_statement_timeout")

            vacuum_row = conn.execute(
                """SELECT COUNT(*)::int AS active_vacuums,
                          COALESCE(MAX(EXTRACT(EPOCH FROM (now() - a.query_start))), 0)::float
                            AS max_autovacuum_age_sec
                     FROM pg_stat_progress_vacuum v
                     LEFT JOIN pg_stat_activity a ON a.pid = v.pid
                    WHERE COALESCE(a.backend_type, '') = 'autovacuum worker'"""
            ).fetchone()
            active_vacuums = int(_row_get(vacuum_row, "active_vacuums", 0) or 0)
            max_age = float(_row_get(vacuum_row, "max_autovacuum_age_sec", 0) or 0)
            detail["active_vacuums"] = active_vacuums
            detail["max_autovacuum_age_sec"] = round(max_age, 1)
            if active_vacuums > 0 and max_age >= safe_autovacuum_age_sec:
                reasons.append("long_autovacuum")
    except Exception as exc:
        return {
            "ok": False,
            "pressure": True,
            "reasons": ["pressure_probe_failed"],
            "error": str(exc)[:200],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    return {
        "ok": True,
        "pressure": bool(reasons),
        "reasons": reasons,
        "detail": detail,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _multirow_values_placeholder(column_count: int, row_count: int) -> str:
    count = max(1, int(row_count or 1))
    row_sql = "(" + ", ".join(["%s"] * column_count) + ")"
    return ", ".join([row_sql] * count)


def _flatten_rows(rows: list[tuple] | list[list]) -> list[Any]:
    return [value for row in rows for value in row]


def _execute_multirow_upsert(pg_conn: Any, sql_factory, rows: list[tuple] | list[list]) -> None:
    for start in range(0, len(rows), REMOTE_ITEM_MULTIROW_UPSERT_CHUNK_SIZE):
        chunk = rows[start : start + REMOTE_ITEM_MULTIROW_UPSERT_CHUNK_SIZE]
        pg_conn.execute(sql_factory(len(chunk)), _flatten_rows(chunk))


def _has_duplicate_item_ids(batch: list[dict[str, Any]]) -> bool:
    seen: set[str] = set()
    for item in batch:
        item_id = item.get("id")
        if item_id is None:
            continue
        key = str(item_id)
        if key in seen:
            return True
        seen.add(key)
    return False


def _item_upsert_sql(schema: str, *, row_count: int = 1) -> str:
    columns = REMOTE_ITEM_WRITE_COLUMNS
    col_sql = ", ".join(columns)
    placeholders = _multirow_values_placeholder(len(columns), row_count)
    refresh_change_condition = _item_upsert_read_model_refresh_condition()
    noop_guard = _item_upsert_noop_guard(refresh_change_condition)
    return f"""INSERT INTO {schema}.items AS target ({col_sql})
            VALUES {placeholders}
            ON CONFLICT (id) DO UPDATE SET
              content = CASE
                WHEN length(COALESCE(excluded.content, '')) > length(COALESCE(target.content, ''))
                THEN excluded.content ELSE target.content END,
              url = COALESCE(NULLIF(excluded.url, ''), target.url),
              ai_summary = COALESCE(excluded.ai_summary, target.ai_summary),
              ai_key_points = COALESCE(excluded.ai_key_points, target.ai_key_points),
              metrics_json = excluded.metrics_json,
              detail_json = COALESCE(excluded.detail_json, target.detail_json),
              comments_json = excluded.comments_json,
              asr_text = COALESCE(excluded.asr_text, target.asr_text),
              asr_status = COALESCE(excluded.asr_status, target.asr_status),
              asr_duration_sec = COALESCE(excluded.asr_duration_sec, target.asr_duration_sec),
              asr_cost_yuan = COALESCE(excluded.asr_cost_yuan, target.asr_cost_yuan),
              asr_attempted_at = COALESCE(excluded.asr_attempted_at, target.asr_attempted_at),
              asr_failed_reason = COALESCE(excluded.asr_failed_reason, target.asr_failed_reason),
              asr_provider = COALESCE(excluded.asr_provider, target.asr_provider),
              asr_segments = COALESCE(excluded.asr_segments, target.asr_segments),
              asr_text_cn = COALESCE(excluded.asr_text_cn, target.asr_text_cn),
              asr_segments_cn = COALESCE(excluded.asr_segments_cn, target.asr_segments_cn),
              cover_url = COALESCE(excluded.cover_url, target.cover_url),
              author_name = COALESCE(NULLIF(excluded.author_name, ''), target.author_name),
              source = COALESCE(NULLIF(excluded.source, ''), target.source),
              source_id = COALESCE(excluded.source_id, target.source_id),
              fetch_run_id = COALESCE(excluded.fetch_run_id, target.fetch_run_id),
              fetched_at = CASE
                WHEN excluded.fetch_run_id IS NOT NULL
                     AND {refresh_change_condition}
                THEN excluded.fetched_at
                ELSE target.fetched_at END
            WHERE {noop_guard}"""


def _item_upsert_read_model_refresh_condition() -> str:
    """Return true when an existing item update changes info read-model content."""
    return """(
                COALESCE(NULLIF(excluded.url, ''), target.url) IS DISTINCT FROM target.url
                OR COALESCE(excluded.ai_summary, target.ai_summary) IS DISTINCT FROM target.ai_summary
                OR excluded.metrics_json IS DISTINCT FROM target.metrics_json
                OR COALESCE(excluded.detail_json, target.detail_json) IS DISTINCT FROM target.detail_json
                OR COALESCE(excluded.cover_url, target.cover_url) IS DISTINCT FROM target.cover_url
                OR COALESCE(NULLIF(excluded.author_name, ''), target.author_name) IS DISTINCT FROM target.author_name
                OR COALESCE(NULLIF(excluded.source, ''), target.source) IS DISTINCT FROM target.source
              )"""


def _item_upsert_noop_guard(refresh_change_condition: str) -> str:
    return f"""(
                CASE
                  WHEN length(COALESCE(excluded.content, '')) > length(COALESCE(target.content, ''))
                  THEN excluded.content ELSE target.content END IS DISTINCT FROM target.content
                OR COALESCE(NULLIF(excluded.url, ''), target.url) IS DISTINCT FROM target.url
                OR COALESCE(excluded.ai_summary, target.ai_summary) IS DISTINCT FROM target.ai_summary
                OR COALESCE(excluded.ai_key_points, target.ai_key_points) IS DISTINCT FROM target.ai_key_points
                OR excluded.metrics_json IS DISTINCT FROM target.metrics_json
                OR COALESCE(excluded.detail_json, target.detail_json) IS DISTINCT FROM target.detail_json
                OR excluded.comments_json IS DISTINCT FROM target.comments_json
                OR COALESCE(excluded.asr_text, target.asr_text) IS DISTINCT FROM target.asr_text
                OR COALESCE(excluded.asr_status, target.asr_status) IS DISTINCT FROM target.asr_status
                OR COALESCE(excluded.asr_duration_sec, target.asr_duration_sec) IS DISTINCT FROM target.asr_duration_sec
                OR COALESCE(excluded.asr_cost_yuan, target.asr_cost_yuan) IS DISTINCT FROM target.asr_cost_yuan
                OR COALESCE(excluded.asr_attempted_at, target.asr_attempted_at) IS DISTINCT FROM target.asr_attempted_at
                OR COALESCE(excluded.asr_failed_reason, target.asr_failed_reason) IS DISTINCT FROM target.asr_failed_reason
                OR COALESCE(excluded.asr_provider, target.asr_provider) IS DISTINCT FROM target.asr_provider
                OR COALESCE(excluded.asr_segments, target.asr_segments) IS DISTINCT FROM target.asr_segments
                OR COALESCE(excluded.asr_text_cn, target.asr_text_cn) IS DISTINCT FROM target.asr_text_cn
                OR COALESCE(excluded.asr_segments_cn, target.asr_segments_cn) IS DISTINCT FROM target.asr_segments_cn
                OR COALESCE(excluded.cover_url, target.cover_url) IS DISTINCT FROM target.cover_url
                OR COALESCE(NULLIF(excluded.author_name, ''), target.author_name) IS DISTINCT FROM target.author_name
                OR COALESCE(NULLIF(excluded.source, ''), target.source) IS DISTINCT FROM target.source
                OR COALESCE(excluded.source_id, target.source_id) IS DISTINCT FROM target.source_id
                OR (
                  excluded.fetch_run_id IS NOT NULL
                  AND {refresh_change_condition}
                  AND excluded.fetched_at IS DISTINCT FROM target.fetched_at
                )
              )"""


def _fetch_run_item_upsert_sql(schema: str, *, row_count: int = 1) -> str:
    placeholders = _multirow_values_placeholder(5, row_count)
    return f"""INSERT INTO {schema}.fetch_run_items
                  (run_id, item_id, platform, source, was_inserted)
                VALUES {placeholders}
                ON CONFLICT (run_id, item_id) DO UPDATE SET
                  platform = excluded.platform,
                  source = excluded.source,
                  was_inserted = CASE
                    WHEN {schema}.fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1
                    THEN 1 ELSE 0 END
                WHERE {schema}.fetch_run_items.platform IS DISTINCT FROM excluded.platform
                   OR {schema}.fetch_run_items.source IS DISTINCT FROM excluded.source
                   OR {schema}.fetch_run_items.was_inserted IS DISTINCT FROM CASE
                        WHEN {schema}.fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1
                        THEN 1 ELSE 0 END"""


def upsert_item_remote(
    pg_conn: Any,
    item_dict: dict[str, Any],
    *,
    fetch_run_id: int | None = None,
) -> None:
    """Insert/update an item in Supabase and track fetch_run_items."""
    if pg_conn is None:
        with connect() as conn:
            upsert_item_remote(conn, item_dict, fetch_run_id=fetch_run_id)
            return

    item = dict(item_dict)
    if fetch_run_id is not None:
        item["fetch_run_id"] = fetch_run_id

    schema = remote_schema()
    item_id = item.get("id")
    run_id = item.get("fetch_run_id")
    run_exists = False
    existed_before = None
    if run_id is not None:
        run_exists = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.fetch_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            is not None
        )
        existed_before = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.items WHERE id = %s",
                (item_id,),
            ).fetchone()
            is not None
        )

    columns = REMOTE_ITEM_WRITE_COLUMNS
    values = [_item_write_value(col, item) for col in columns]
    pg_conn.execute(_item_upsert_sql(schema), values)

    if run_id is not None and run_exists:
        was_inserted = 0 if existed_before else 1
        pg_conn.execute(
            _fetch_run_item_upsert_sql(schema),
            (run_id, item_id, item.get("platform"), item.get("source"), was_inserted),
        )


def batch_upsert_items_remote(
    pg_conn: Any | None,
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    fetch_run_id: int | None = None,
) -> int:
    """Upsert a batch of items into Supabase in one transaction."""
    if pg_conn is None:
        with connect() as conn:
            return batch_upsert_items_remote(conn, items, fetch_run_id=fetch_run_id)

    batch = [dict(item) for item in items]
    if not batch:
        return 0
    if fetch_run_id is not None:
        for item in batch:
            item["fetch_run_id"] = fetch_run_id
    has_duplicate_ids = _has_duplicate_item_ids(batch)

    schema = remote_schema()
    run_exists = False
    existing_ids: set[str] = set()
    if fetch_run_id is not None:
        run_exists = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.fetch_runs WHERE id = %s",
                (fetch_run_id,),
            ).fetchone()
            is not None
        )
        ids = [str(item.get("id")) for item in batch if item.get("id")]
        if ids:
            rows = pg_conn.execute(
                f"SELECT id FROM {schema}.items WHERE id = ANY(%s)",
                (ids,),
            ).fetchall()
            existing_ids = {str(row["id"] if isinstance(row, dict) else row[0]) for row in rows}

    values = [[_item_write_value(col, item) for col in REMOTE_ITEM_WRITE_COLUMNS] for item in batch]
    if has_duplicate_ids:
        _executemany(pg_conn, _item_upsert_sql(schema), values)
    else:
        _execute_multirow_upsert(
            pg_conn,
            lambda row_count: _item_upsert_sql(schema, row_count=row_count),
            values,
        )

    if fetch_run_id is not None and run_exists:
        run_rows = [
            (
                fetch_run_id,
                item.get("id"),
                item.get("platform"),
                item.get("source"),
                0 if str(item.get("id")) in existing_ids else 1,
            )
            for item in batch
            if item.get("id")
        ]
        if run_rows:
            if has_duplicate_ids:
                _executemany(pg_conn, _fetch_run_item_upsert_sql(schema), run_rows)
            else:
                _execute_multirow_upsert(
                    pg_conn,
                    lambda row_count: _fetch_run_item_upsert_sql(schema, row_count=row_count),
                    run_rows,
                )
    _commit_if_supported(pg_conn)
    return len(batch)


def query_pending_enrichment_items_remote(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    run_id: int | None = None,
    run_items_scope: str = "tagged",
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> list[dict[str, Any]]:
    """Return items needing enrichment from Supabase."""
    schema = remote_schema()
    item_alias = "items"
    from_clause = f"{schema}.items"
    select_cols = f"""{item_alias}.id, {item_alias}.platform, {item_alias}.source,
                     {item_alias}.author_name, {item_alias}.metrics_json,
                     {item_alias}.url, {item_alias}.title, {item_alias}.content,
                     {item_alias}.ai_summary, {item_alias}.ai_category as category,
                     {item_alias}.detail_json, {item_alias}.asr_text"""
    params: list[Any] = []
    clauses = [f"{item_alias}.platform <> 'bilibili'"]

    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        clauses = [f"{item_alias}.id IN ({placeholders})"]
        params.extend(ids)
    else:
        if run_id is not None:
            if run_items_scope == "inserted":
                item_alias = "i"
                from_clause = (
                    f"{schema}.fetch_run_items fri "
                    f"JOIN {schema}.items {item_alias} ON {item_alias}.id = fri.item_id"
                )
                select_cols = f"""{item_alias}.id, {item_alias}.platform, {item_alias}.source,
                                 {item_alias}.author_name, {item_alias}.metrics_json,
                                 {item_alias}.url, {item_alias}.title, {item_alias}.content,
                                 {item_alias}.ai_summary, {item_alias}.ai_category as category,
                                 {item_alias}.detail_json, {item_alias}.asr_text"""
                clauses = [
                    f"{item_alias}.platform <> 'bilibili'",
                    "fri.run_id = %s",
                    "fri.was_inserted = 1",
                ]
                params.append(run_id)
            elif run_items_scope == "tagged":
                clauses.append(f"{item_alias}.fetch_run_id = %s")
                params.append(run_id)
            else:
                raise RemoteDBConfigError(f"Unsupported run_items_scope={run_items_scope!r}")
        time_expr = (
            f"{item_alias}.published_at"
            if require_published_at
            else f"COALESCE({item_alias}.published_at, {item_alias}.fetched_at)"
        )
        if require_published_at:
            clauses.append(f"{item_alias}.published_at IS NOT NULL")
        if window_start:
            clauses.append(f"{time_expr} >= %s")
            params.append(_timestamp_value(window_start))
        if window_end:
            clauses.append(f"{time_expr} < %s")
            params.append(_timestamp_value(window_end))
        clauses.extend(
            [
                f"({item_alias}.ai_retry_after IS NULL OR {item_alias}.ai_retry_after <= now())",
                f"""(
                    {item_alias}.ai_summary IS NULL OR {item_alias}.ai_summary = ''
                    OR {item_alias}.ai_quality_score IS NULL
                    OR {item_alias}.ai_category IS NULL OR {item_alias}.ai_category = ''
                    OR {item_alias}.ai_categories IS NULL
                )""",
            ]
        )

    order_expr = (
        f"COALESCE({item_alias}.published_at, {item_alias}.fetched_at)"
        if (window_start or window_end)
        else f"{item_alias}.fetched_at"
    )
    limit_clause = ""
    if limit:
        limit_clause = " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        set_pending_scan_statement_timeout(conn)
        rows = conn.execute(
            f"""SELECT {select_cols}
                  FROM {from_clause}
                 WHERE {' AND '.join(clauses)}
                 ORDER BY {order_expr} DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def query_pending_highlight_verdict_items_remote(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
    rescore_prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    """Return Supabase items that still need item-level Highlights verdicts."""
    schema = remote_schema()
    select_cols = """id, platform, source, author_name, metrics_json, url, title, content,
                     ai_summary, ai_category as category, detail_json, asr_text"""
    params: list[Any] = []
    clauses = ["platform <> 'bilibili'"]
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        clauses = [f"id IN ({placeholders})"]
        params.extend(ids)
    else:
        time_expr = (
            "published_at"
            if require_published_at
            else "COALESCE(published_at, fetched_at)"
        )
        if require_published_at:
            clauses.append("published_at IS NOT NULL")
        if window_start:
            clauses.append(f"{time_expr} >= %s")
            params.append(_timestamp_value(window_start))
        if window_end:
            clauses.append(f"{time_expr} < %s")
            params.append(_timestamp_value(window_end))
        clauses.extend(
            [
                "ai_summary IS NOT NULL",
                "(highlight_retry_after IS NULL OR highlight_retry_after <= now())",
            ]
        )
        if rescore_prompt_version:
            clauses.append("(highlight_verdict IS NULL OR highlight_prompt_version IS DISTINCT FROM %s)")
            params.append(str(rescore_prompt_version))
        else:
            clauses.append("(highlight_verdict IS NULL)")
    order_expr = "COALESCE(published_at, fetched_at)" if (window_start or window_end) else "fetched_at"
    limit_clause = ""
    if limit:
        limit_clause = " LIMIT %s"
        params.append(limit)
    with connect() as conn:
        set_pending_scan_statement_timeout(conn)
        rows = conn.execute(
            f"""SELECT {select_cols}
                  FROM {schema}.items
                 WHERE {' AND '.join(clauses)}
                 ORDER BY {order_expr} DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def write_enrichment_remote(pg_conn: Any | None, item_id: str, parsed: dict[str, Any]) -> None:
    """Write enrichment output to a Supabase item row."""
    if pg_conn is None:
        with connect() as conn:
            write_enrichment_remote(conn, item_id, parsed)
            return

    key_points = parsed.get("key_points")
    keywords = parsed.get("keywords")
    dimensions = parsed.get("dimensions")
    categories = parsed.get("categories")
    subcategories = parsed.get("subcategories")
    ai_extracted = parsed.get("ai_extracted") or {}
    has_extracted = bool(
        ai_extracted.get("skills")
        or ai_extracted.get("models")
        or ai_extracted.get("event_card")
    )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET ai_summary = %s,
                   ai_key_points = %s,
                   ai_category = COALESCE(%s, ai_category),
                   content_type = %s,
                   ai_dimensions = %s,
                   ai_quality_score = %s,
                   relevance_score = COALESCE(%s, relevance_score),
                   ai_keywords = %s,
                   ai_categories = %s,
                   ai_subcategories = %s,
                   multi_l1_reason = %s,
                   ai_extracted = %s,
                   visible = %s,
                   ai_error_count = 0,
                   ai_last_error = NULL,
                   ai_last_error_at = NULL,
                   ai_retry_after = NULL
             WHERE id = %s""",
        (
            parsed.get("summary"),
            json.dumps(key_points, ensure_ascii=False) if key_points else None,
            parsed.get("category"),
            parsed.get("content_type"),
            _maybe_jsonb(dimensions) if dimensions else None,
            parsed.get("quality_score"),
            parsed.get("relevance_score"),
            json.dumps(keywords, ensure_ascii=False) if keywords else None,
            _maybe_jsonb(categories) if categories else None,
            _maybe_jsonb(subcategories) if subcategories else None,
            parsed.get("multi_l1_reason"),
            _maybe_jsonb(ai_extracted) if has_extracted else None,
            1 if parsed.get("visible", True) else 0,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def write_highlight_verdict_remote(pg_conn: Any | None, item_id: str, result: dict[str, Any]) -> None:
    """Write item-level Highlights verdict metadata to Supabase."""
    if pg_conn is None:
        with connect() as conn:
            write_highlight_verdict_remote(conn, item_id, result)
            return

    pending = result.get("cluster_verdict") == "pending" or not result.get("highlight_verdict")
    retry_after_value = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
        if pending
        else None
    )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET highlight_verdict = %s,
                   highlight_value_path = %s,
                   highlight_uncertainty = %s,
                   highlight_include_in_highlights = %s,
                   highlight_reason = %s,
                   highlight_scores = %s,
                   highlight_ai_relevant = %s,
                   highlight_spam = %s,
                   highlight_confidence = %s,
                   highlight_prompt_version = %s,
                   highlight_model = %s,
                   highlight_scored_at = COALESCE(%s::timestamptz, now()),
                   highlight_error_count = CASE
                     WHEN %s THEN COALESCE(highlight_error_count, 0) + 1
                     ELSE 0
                   END,
                   highlight_last_error = %s,
                   highlight_retry_after = %s
             WHERE id = %s""",
        (
            result.get("highlight_verdict"),
            result.get("highlight_value_path"),
            result.get("highlight_uncertainty"),
            bool(result.get("highlight_include_in_highlights")),
            result.get("highlight_reason"),
            _maybe_jsonb(result.get("highlight_scores") or {}),
            result.get("highlight_ai_relevant"),
            result.get("highlight_spam"),
            result.get("highlight_confidence"),
            result.get("highlight_prompt_version"),
            result.get("highlight_model"),
            _timestamp_value(result.get("highlight_scored_at")),
            pending,
            result.get("highlight_last_error"),
            retry_after_value,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def record_highlight_verdict_failure_remote(
    pg_conn: Any | None,
    item_id: str,
    error: str,
    *,
    retry_after: Any = None,
) -> None:
    """Record highlight-verdict failure metadata without touching enrichment fields."""
    if pg_conn is None:
        with connect() as conn:
            record_highlight_verdict_failure_remote(
                conn,
                item_id,
                error,
                retry_after=retry_after,
            )
            return

    retry_after_value = None
    if isinstance(retry_after, (int, float)):
        retry_after_value = datetime.now(timezone.utc).timestamp() + float(retry_after)
        retry_after_value = datetime.fromtimestamp(retry_after_value, tz=timezone.utc)
    elif retry_after:
        retry_after_value = _timestamp_value(retry_after)
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET highlight_error_count = COALESCE(highlight_error_count, 0) + 1,
                   highlight_last_error = %s,
                   highlight_retry_after = %s,
                   highlight_scored_at = now()
             WHERE id = %s""",
        (
            str(error or "")[:1000],
            retry_after_value,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def query_highlight_cluster_decisions_remote(
    *,
    decision: str = "excluded",
    cluster_verdict: str | None = None,
    recent_days: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return machine highlight decisions for review tooling."""
    safe_decision = decision if decision in {"included", "excluded", "pending"} else "excluded"
    safe_cluster_verdict = (
        cluster_verdict
        if cluster_verdict in {"featured", "positive_borderline", "risk_borderline", "drop", "pending"}
        else None
    )
    try:
        safe_recent_days = max(0, min(int(recent_days or 0), 365))
    except (TypeError, ValueError):
        safe_recent_days = 0
    safe_limit = max(1, min(int(limit or 100), 500))
    schema = remote_schema()
    where = ["d.decision = %s"]
    params: list[Any] = [safe_decision]
    if safe_cluster_verdict:
        where.append("d.cluster_verdict = %s")
        params.append(safe_cluster_verdict)
    if safe_recent_days:
        where.append(
            "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at, d.decided_at) "
            ">= now() - (%s::int * interval '1 day')"
        )
        params.append(safe_recent_days)
    params.append(safe_limit)
    with connect() as conn:
        rows = conn.execute(
            f"""WITH filtered_decisions AS (
                   SELECT d.cluster_id,
                          d.decision,
                          d.cluster_verdict,
                          d.deciding_item_id,
                          d.reason,
                          d.verdict_counts_json,
                          d.prompt_version,
                          d.model,
                          d.decided_at,
                          d.updated_at,
                          d.snapshot_json,
                          c.ai_title,
                          c.ai_summary,
                          c.doc_count,
                          c.unique_source_count,
                          c.first_doc_at,
                          c.last_doc_at
                     FROM {schema}.highlight_cluster_decisions d
                     LEFT JOIN {schema}.clusters c ON c.id = d.cluster_id
                    WHERE {" AND ".join(where)}
                    ORDER BY d.decided_at DESC, d.cluster_id DESC
                    LIMIT %s
                 )
                SELECT fd.cluster_id,
                       fd.decision,
                       fd.cluster_verdict,
                       fd.deciding_item_id,
                       fd.reason,
                       fd.verdict_counts_json,
                       fd.prompt_version,
                       fd.model,
                       fd.decided_at,
                       fd.updated_at,
                       fd.snapshot_json,
                       fd.ai_title,
                       fd.ai_summary,
                       fd.doc_count,
                       fd.unique_source_count,
                       fd.first_doc_at,
                       fd.last_doc_at,
                       lr.human_verdict AS latest_human_verdict,
                       lr.error_kind AS latest_error_kind,
                       lr.notes AS latest_notes,
                       lr.reviewer AS latest_reviewer,
                       lr.reviewed_at AS latest_reviewed_at
                  FROM filtered_decisions fd
                  LEFT JOIN LATERAL (
                       SELECT r.human_verdict,
                              r.error_kind,
                              r.notes,
                              r.reviewer,
                              r.reviewed_at
                         FROM {schema}.highlight_exclusion_reviews r
                        WHERE r.cluster_id = fd.cluster_id
                        ORDER BY r.reviewed_at DESC, r.id DESC
                        LIMIT 1
                  ) lr ON true
                 ORDER BY fd.decided_at DESC, fd.cluster_id DESC""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def query_highlight_review_docs_remote(cluster_id: int) -> list[dict[str, Any]]:
    """Return source docs/items for a cluster highlight review page."""
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.id,
                       i.title,
                       i.url,
                       i.platform,
                       i.source,
                       i.author_name,
                       i.ai_summary,
                       i.content,
                       i.published_at,
                       i.fetched_at,
                       ci.rank_in_cluster,
                       COALESCE(ci.is_primary_source, false) AS is_primary_source,
                       i.highlight_verdict,
                       i.highlight_value_path,
                       i.highlight_uncertainty,
                       i.highlight_include_in_highlights,
                       i.highlight_reason
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %s
                 ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                          ci.rank_in_cluster ASC NULLS LAST,
                          COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                          i.id DESC""",
            (int(cluster_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def write_highlight_exclusion_review_remote(
    pg_conn: Any | None,
    *,
    cluster_id: int,
    human_verdict: str,
    machine_decision_at: Any = None,
    error_kind: str | None = None,
    notes: str | None = None,
    reviewer: str | None = None,
) -> None:
    """Append one human review entry for an excluded/pending highlight cluster."""
    if human_verdict not in {"should_feature", "confirmed_drop", "unsure"}:
        raise RemoteDBConfigError(f"Unsupported human_verdict={human_verdict!r}")
    if pg_conn is None:
        with connect() as conn:
            write_highlight_exclusion_review_remote(
                conn,
                cluster_id=cluster_id,
                human_verdict=human_verdict,
                machine_decision_at=machine_decision_at,
                error_kind=error_kind,
                notes=notes,
                reviewer=reviewer,
            )
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.highlight_exclusion_reviews (
               cluster_id, machine_decision_at, human_verdict,
               error_kind, notes, reviewer, reviewed_at
             )
             VALUES (%s, %s, %s, %s, %s, %s, now())""",
        (
            int(cluster_id),
            _timestamp_value(machine_decision_at),
            human_verdict,
            str(error_kind or "").strip()[:120] or None,
            str(notes or "").strip()[:1000] or None,
            str(reviewer or "").strip()[:120] or None,
        ),
    )
    _commit_if_supported(pg_conn)


def record_ai_failure_remote(
    pg_conn: Any | None,
    item_id: str,
    error: str,
    *,
    retry_after: Any = None,
    increment: bool = True,
) -> None:
    """Record item-level AI failure metadata in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            record_ai_failure_remote(
                conn,
                item_id,
                error,
                retry_after=retry_after,
                increment=increment,
            )
            return

    retry_after_value = None
    if isinstance(retry_after, (int, float)):
        retry_after_value = datetime.now(timezone.utc).timestamp() + float(retry_after)
        retry_after_value = datetime.fromtimestamp(retry_after_value, tz=timezone.utc)
    elif retry_after:
        retry_after_value = _timestamp_value(retry_after)
    count_expr = "COALESCE(ai_error_count, 0) + 1" if increment else "COALESCE(ai_error_count, 0)"
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET ai_error_count = {count_expr},
                   ai_last_error = %s,
                   ai_last_error_at = %s,
                   ai_retry_after = %s
             WHERE id = %s""",
        (str(error)[:500], datetime.now(timezone.utc), retry_after_value, item_id),
    )
    _commit_if_supported(pg_conn)


def vector_to_pg(value: Any) -> str | None:
    if value is None:
        return None
    return "[" + ",".join(f"{float(x):.9g}" for x in value) + "]"


def pg_vector_to_list(value: Any) -> list[float] | None:
    """Parse a pgvector value returned by psycopg into a Python float list."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    return [float(part.strip()) for part in text.split(",") if part.strip()]


def update_item_embedding_remote(
    pg_conn: Any | None,
    item_id: str,
    vector: Any,
    provider_name: str,
    *,
    model: str | None = None,
    input_variant: str | None = None,
) -> None:
    """Write an item embedding to Supabase pgvector."""
    if pg_conn is None:
        with connect() as conn:
            update_item_embedding_remote(
                conn,
                item_id,
                vector,
                provider_name,
                model=model,
                input_variant=input_variant,
            )
            return

    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET embedding = %s::extensions.vector,
                   embedding_provider = %s,
                   embedding_model = COALESCE(%s, embedding_model),
                   embedding_input_variant = COALESCE(%s, embedding_input_variant),
                   embedding_generated_at = now()
             WHERE id = %s""",
        (
            vector_to_pg(vector),
            provider_name,
            model,
            input_variant,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _row_id(row: Any) -> int:
    if isinstance(row, dict):
        return int(row["id"])
    return int(row[0])


def _source_identity_from_row(row: Any) -> str | None:
    raw_url = (_row_get(row, "url") or "").strip()
    item_id = _row_get(row, "id")
    if raw_url:
        try:
            from utils.url_normalize import normalize_url
            normalized = normalize_url(raw_url)
            if normalized.platform in ("twitter", "youtube") and normalized.canonical_url:
                return normalized.canonical_url
        except Exception:
            pass
        return raw_url
    return item_id


def add_item_to_cluster_remote(
    pg_conn: Any | None,
    cluster_id: int,
    item_id: str,
    *,
    rank_in_cluster: int = 9999,
    is_primary_source: int | bool = 0,
    source_identity: str | None = None,
    join_decision_id: int | None = None,
) -> None:
    """Write cluster membership and item.cluster_id directly to Supabase."""
    if pg_conn is None:
        with connect() as conn:
            add_item_to_cluster_remote(
                conn,
                cluster_id,
                item_id,
                rank_in_cluster=rank_in_cluster,
                is_primary_source=is_primary_source,
                source_identity=source_identity,
                join_decision_id=join_decision_id,
            )
            return

    schema = remote_schema()
    pg_conn.execute(
        f"""INSERT INTO {schema}.cluster_items
              (cluster_id, item_id, rank_in_cluster, added_at, is_primary_source,
               source_identity, join_decision_id)
            VALUES (%s, %s, %s, now(), %s, %s, %s)
            ON CONFLICT (cluster_id, item_id) DO NOTHING""",
        (
            cluster_id,
            item_id,
            rank_in_cluster,
            bool(is_primary_source),
            source_identity,
            str(join_decision_id) if join_decision_id is not None else None,
        ),
    )
    pg_conn.execute(
        f"UPDATE {schema}.items SET cluster_id = %s WHERE id = %s",
        (cluster_id, item_id),
    )
    _commit_if_supported(pg_conn)


def mark_cluster_touched_by_run_remote(
    pg_conn: Any | None,
    cluster_id: int,
    run_id: int | None,
) -> None:
    if run_id is None:
        return
    if pg_conn is None:
        with connect() as conn:
            mark_cluster_touched_by_run_remote(conn, cluster_id, run_id)
            return
    pg_conn.execute(
        f"UPDATE {remote_schema()}.clusters SET last_touched_run_id = %s WHERE id = %s",
        (run_id, cluster_id),
    )
    _commit_if_supported(pg_conn)


def finalize_cluster_state_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    tau_hours: float,
) -> dict[str, Any]:
    """Recompute derived cluster fields in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return finalize_cluster_state_remote(conn, cluster_id, tau_hours=tau_hours)

    schema = remote_schema()
    set_cluster_write_statement_timeout(pg_conn)
    counts = pg_conn.execute(
        f"""SELECT
              COUNT(DISTINCT (i.platform, COALESCE(i.author_name, i.id))) AS doc_count,
              COUNT(DISTINCT ci.source_identity)
                FILTER (WHERE ci.source_identity IS NOT NULL) AS unique_source_count
            FROM {schema}.cluster_items ci
            JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchone()
    doc_count = int(_row_get(counts, "doc_count", 0) or 0)
    unique_source_count = int(_row_get(counts, "unique_source_count", 0) or 0)

    platform_rows = pg_conn.execute(
        f"""SELECT DISTINCT i.platform
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchall()
    platforms = sorted({
        _row_get(row, "platform")
        for row in platform_rows
        if _row_get(row, "platform")
    })

    bounds = pg_conn.execute(
        f"""SELECT MIN(COALESCE(i.published_at, i.fetched_at)) AS first_doc_at,
                  MAX(COALESCE(i.published_at, i.fetched_at)) AS last_doc_at
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    first_doc_at = _timestamp_value(_row_get(bounds, "first_doc_at")) or to_utc_iso(now)
    last_doc_at = _timestamp_value(_row_get(bounds, "last_doc_at")) or first_doc_at

    vector_rows = pg_conn.execute(
        f"""SELECT i.embedding::text AS embedding_text,
                  COALESCE(i.published_at, i.fetched_at) AS ts
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s
              AND i.embedding IS NOT NULL""",
        (cluster_id,),
    ).fetchall()
    representative_vector = None
    vecs = []
    timestamps = []
    if vector_rows:
        try:
            import numpy as np
            from clustering import vector_utils as vu
            from time_utils import parse_datetime

            for row in vector_rows:
                parsed_vector = pg_vector_to_list(_row_get(row, "embedding_text"))
                if parsed_vector is None:
                    continue
                vecs.append(np.asarray(parsed_vector, dtype=np.float32))
                timestamps.append(parse_datetime(_row_get(row, "ts")) or now)
            representative_vector = vu.weighted_mean_with_decay(
                vecs,
                timestamps,
                now=now,
                tau_hours=tau_hours,
            ) if vecs else None
        except Exception:
            representative_vector = None

    pg_conn.execute(
        f"""UPDATE {schema}.clusters
               SET doc_count = %s,
                   unique_source_count = %s,
                   platforms_json = %s,
                   first_doc_at = %s,
                   last_doc_at = %s,
                   last_updated_at = %s,
                   representative_vector = COALESCE(%s::extensions.vector, representative_vector)
             WHERE id = %s""",
        (
            doc_count,
            unique_source_count,
            _maybe_jsonb(platforms),
            first_doc_at,
            last_doc_at,
            now,
            vector_to_pg(representative_vector),
            cluster_id,
        ),
    )
    _commit_if_supported(pg_conn)
    return {
        "cluster_id": cluster_id,
        "doc_count": doc_count,
        "unique_source_count": unique_source_count,
        "platforms": platforms,
        "first_doc_at": first_doc_at,
        "last_doc_at": last_doc_at,
    }


def create_singleton_cluster_remote(
    pg_conn: Any | None,
    item_id: str,
    vector: Any,
    first_doc_at: Any,
    *,
    source_identity: str | None = None,
    run_id: int | None = None,
    tau_hours: float = 24.0,
) -> int:
    """Create a singleton event cluster in Supabase and attach the seed item."""
    if pg_conn is None:
        with connect() as conn:
            return create_singleton_cluster_remote(
                conn,
                item_id,
                vector,
                first_doc_at,
                source_identity=source_identity,
                run_id=run_id,
                tau_hours=tau_hours,
            )

    schema = remote_schema()
    event_time = _timestamp_value(first_doc_at) or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    _ensure_remote_id_sequence(pg_conn, "clusters")
    set_cluster_write_statement_timeout(pg_conn)
    row = pg_conn.execute(
        f"""INSERT INTO {schema}.clusters
              (first_doc_at, last_doc_at, last_updated_at,
               doc_count, unique_source_count,
               platforms_json, is_visible_in_feed,
               created_run_id, last_touched_run_id, created_at)
            VALUES (%s, %s, %s, 1, 1, '[]'::jsonb,
                    false, %s, %s, %s)
            RETURNING id""",
        (
            event_time,
            event_time,
            now,
            run_id,
            run_id,
            now,
        ),
    ).fetchone()
    cluster_id = _row_id(row)
    if source_identity is None:
        seed = pg_conn.execute(
            f"SELECT id, url FROM {schema}.items WHERE id = %s",
            (item_id,),
        ).fetchone()
        source_identity = _source_identity_from_row(seed) if seed is not None else item_id
    add_item_to_cluster_remote(
        pg_conn,
        cluster_id,
        item_id,
        rank_in_cluster=0,
        is_primary_source=True,
        source_identity=source_identity,
    )
    finalize_cluster_state_remote(pg_conn, cluster_id, tau_hours=tau_hours)
    mark_cluster_touched_by_run_remote(pg_conn, cluster_id, run_id)
    _commit_if_supported(pg_conn)
    return cluster_id


def write_judge_log_remote(
    pg_conn: Any | None,
    *,
    item_id: str,
    candidate_cluster_ids: list[int],
    estimated_input_tokens: int | None,
    matches: list[dict] | None,
    selected_cluster_id: int | None,
    selection_reason: str,
    possible_merge_candidates: list[int],
    decision_model: str,
) -> int | None:
    """Insert a cluster judge decision row in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return write_judge_log_remote(
                conn,
                item_id=item_id,
                candidate_cluster_ids=candidate_cluster_ids,
                estimated_input_tokens=estimated_input_tokens,
                matches=matches,
                selected_cluster_id=selected_cluster_id,
                selection_reason=selection_reason,
                possible_merge_candidates=possible_merge_candidates,
                decision_model=decision_model,
            )
    _ensure_remote_id_sequence(pg_conn, "cluster_judge_log")
    row = pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.cluster_judge_log
              (item_id, candidate_cluster_ids, llm_input_tokens,
               llm_output_tokens, matches_json, selected_cluster_id,
               selection_reason, possible_merge_candidates, decision_model,
               created_at)
            VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, now())
            RETURNING id""",
        (
            item_id,
            _maybe_jsonb(candidate_cluster_ids),
            estimated_input_tokens,
            _maybe_jsonb(matches) if matches is not None else None,
            selected_cluster_id,
            selection_reason,
            _maybe_jsonb(possible_merge_candidates),
            decision_model,
        ),
    ).fetchone()
    _commit_if_supported(pg_conn)
    return _row_id(row) if row is not None else None


def recall_top_k_clusters_remote(
    pg_conn: Any | None,
    vector: Any,
    *,
    k: int = 10,
    window_days: int = 30,
    cosine_min: float = 0.0,
    item_time: str | datetime | None = None,
    temporal_adjacency_days: float | None = None,
    max_merged_span_days: float | None = None,
) -> list[dict[str, Any]]:
    """Return top-K cluster recall candidates from Supabase pgvector."""
    if pg_conn is None:
        with connect() as conn:
            return recall_top_k_clusters_remote(
                conn,
                vector,
                k=k,
                window_days=window_days,
                cosine_min=cosine_min,
                item_time=item_time,
                temporal_adjacency_days=temporal_adjacency_days,
                max_merged_span_days=max_merged_span_days,
            )

    schema = remote_schema()
    query_vector = vector_to_pg(vector)
    item_dt = parse_datetime(item_time)
    where = [
        "c.representative_vector IS NOT NULL",
        "COALESCE(c.archived, false) = false",
        "c.merged_into IS NULL",
    ]
    params: list[Any] = [query_vector]
    if item_dt is not None:
        cluster_first = "COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)"
        cluster_last = "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at)"
        adjacency_days = 3.0 if temporal_adjacency_days is None else max(0.0, float(temporal_adjacency_days))
        where.append(
            f"""{cluster_first} <= %s::timestamptz + (%s::double precision * interval '1 day')
                AND {cluster_last} >= %s::timestamptz - (%s::double precision * interval '1 day')"""
        )
        params.extend([item_dt, adjacency_days, item_dt, adjacency_days])
        if max_merged_span_days is not None:
            max_span_days = max(0.0, float(max_merged_span_days))
            where.append(
                f"""EXTRACT(EPOCH FROM (
                       GREATEST({cluster_last}, %s::timestamptz)
                       - LEAST({cluster_first}, %s::timestamptz)
                    )) / 86400.0 <= %s"""
            )
            params.extend([item_dt, item_dt, max_span_days])
    elif window_days:
        where.append("c.last_updated_at > now() - (%s::int * interval '1 day')")
        params.append(int(window_days))
    if cosine_min:
        where.append("1 - (c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector) >= %s")
        params.extend([query_vector, float(cosine_min)])
    params.extend([query_vector, max(0, int(k))])
    rows = pg_conn.execute(
        f"""SELECT c.id AS cluster_id,
                  c.representative_vector::text AS representative_vector,
                  1 - (c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector) AS cosine,
                  c.doc_count,
                  c.live_version,
                  c.first_doc_at,
                  c.last_doc_at,
                  c.last_updated_at,
                  c.ai_title,
                  c.ai_summary,
                  c.ai_key_points
             FROM {schema}.clusters c
            WHERE {' AND '.join(where)}
            ORDER BY c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector
            LIMIT %s""",
        tuple(params),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["representative_vector"] = pg_vector_to_list(item.get("representative_vector"))
        for key in ("first_doc_at", "last_doc_at", "last_updated_at"):
            if key in item:
                item[key] = _timestamp_value(item.get(key))
        out.append(item)
    return out


def bump_cluster_version_and_stale_actions_remote(
    pg_conn: Any | None,
    cluster_id: int,
    new_version: int,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            bump_cluster_version_and_stale_actions_remote(conn, cluster_id, new_version)
            return
    schema = remote_schema()
    pg_conn.execute(
        f"UPDATE {schema}.clusters SET live_version = %s WHERE id = %s",
        (new_version, cluster_id),
    )
    pg_conn.execute(
        f"""UPDATE {schema}.actions
               SET is_stale = 1
             WHERE source_type = 'cluster'
               AND source_id = %s
               AND (cluster_version IS NULL OR cluster_version < %s)
               AND is_stale = 0""",
        (str(cluster_id), new_version),
    )
    _commit_if_supported(pg_conn)


def mark_cluster_hidden_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    warning: str,
    publish_immediately: bool,
    run_id: int | None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            mark_cluster_hidden_remote(
                conn,
                cluster_id,
                warning=warning,
                publish_immediately=publish_immediately,
                run_id=run_id,
            )
            return
    schema = remote_schema()
    warnings_json = [warning]
    if not publish_immediately:
        pg_conn.execute(
            f"""UPDATE {schema}.clusters
                   SET pending_is_visible_in_feed = 0,
                       pending_summary_warnings_json = %s,
                       last_touched_run_id = COALESCE(%s, last_touched_run_id)
                 WHERE id = %s""",
            (_maybe_jsonb(warnings_json), run_id, cluster_id),
        )
    else:
        now = datetime.now(timezone.utc)
        pg_conn.execute(
            f"""UPDATE {schema}.clusters
                   SET is_visible_in_feed = false,
                       last_summary_warnings_json = %s,
                       last_updated_at = %s,
                       published_at = %s,
                       published_run_id = COALESCE(%s, published_run_id)
                 WHERE id = %s""",
            (_maybe_jsonb(warnings_json), now, now, run_id, cluster_id),
        )
    _commit_if_supported(pg_conn)


def get_cluster_summary_context_remote(pg_conn: Any | None, cluster_id: int) -> dict[str, Any] | None:
    if pg_conn is None:
        with connect() as conn:
            return get_cluster_summary_context_remote(conn, cluster_id)
    row = pg_conn.execute(
        f"""SELECT id, live_version, doc_count, unique_source_count
              FROM {remote_schema()}.clusters
             WHERE id = %s""",
        (cluster_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def collect_cluster_member_rows_remote(
    pg_conn: Any | None,
    cluster_id: int,
) -> list[dict[str, Any]]:
    if pg_conn is None:
        with connect() as conn:
            return collect_cluster_member_rows_remote(conn, cluster_id)
    rows = pg_conn.execute(
        f"""SELECT i.id, i.title, i.content, i.author_name, i.platform, i.url,
                  i.detail_json,
                  i.ai_summary, i.ai_key_points, i.ai_category,
                  i.published_at, i.fetched_at,
                  ci.is_primary_source, ci.rank_in_cluster
             FROM {remote_schema()}.items i
             JOIN {remote_schema()}.cluster_items ci ON ci.item_id = i.id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def cluster_dominant_category_remote(pg_conn: Any | None, cluster_id: int) -> str | None:
    rows = collect_cluster_member_rows_remote(pg_conn, cluster_id)
    from clustering import visibility_policy

    return visibility_policy.dominant_category(row.get("ai_category") for row in rows)


def write_cluster_summary_draft_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    title: str,
    summary: str,
    key_points: Any,
    is_visible: bool,
    warnings: list[str],
    run_id: int | None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            write_cluster_summary_draft_remote(
                conn,
                cluster_id,
                title=title,
                summary=summary,
                key_points=key_points,
                is_visible=is_visible,
                warnings=warnings,
                run_id=run_id,
            )
            return
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.clusters
               SET ai_title_draft = %s,
                   ai_summary_draft = %s,
                   ai_key_points_draft = %s,
                   pending_is_visible_in_feed = %s,
                   pending_summary_warnings_json = %s,
                   last_touched_run_id = COALESCE(%s, last_touched_run_id)
             WHERE id = %s""",
        (
            title,
            summary,
            json.dumps(key_points, ensure_ascii=False),
            1 if is_visible else 0,
            _maybe_jsonb(warnings),
            run_id,
            cluster_id,
        ),
    )
    _commit_if_supported(pg_conn)


def publish_cluster_summary_live_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    is_visible: bool,
    warnings: list[str],
    run_id: int | None,
    new_version: int,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            publish_cluster_summary_live_remote(
                conn,
                cluster_id,
                is_visible=is_visible,
                warnings=warnings,
                run_id=run_id,
                new_version=new_version,
            )
            return
    now = datetime.now(timezone.utc)
    schema = remote_schema()
    pg_conn.execute(
        f"""UPDATE {schema}.clusters
               SET ai_title = ai_title_draft,
                   ai_summary = ai_summary_draft,
                   ai_key_points = ai_key_points_draft,
                   ai_title_draft = NULL,
                   ai_summary_draft = NULL,
                   ai_key_points_draft = NULL,
                   is_visible_in_feed = %s,
                   last_summary_warnings_json = %s,
                   pending_is_visible_in_feed = NULL,
                   pending_summary_warnings_json = NULL,
                   last_updated_at = %s,
                   published_at = %s,
                   published_run_id = COALESCE(%s, published_run_id)
             WHERE id = %s""",
        (
            bool(is_visible),
            _maybe_jsonb(warnings),
            now,
            now,
            run_id,
            cluster_id,
        ),
    )
    bump_cluster_version_and_stale_actions_remote(pg_conn, cluster_id, new_version)
    _commit_if_supported(pg_conn)


def publish_run_remote(pg_conn: Any | None, run_id: int) -> int:
    if pg_conn is None:
        with connect() as conn:
            return publish_run_remote(conn, run_id)
    schema = remote_schema()
    batch_size = _env_int(
        _runtime_env(),
        "INFO2ACTION_PUBLISH_RUN_BATCH_SIZE",
        25,
        min_value=1,
    )
    published = 0
    while True:
        rows = pg_conn.execute(
            f"""SELECT id, COALESCE(live_version, 0) + 1 AS new_version
                  FROM {schema}.clusters
                 WHERE last_touched_run_id = %s
                   AND (
                     ai_title_draft IS NOT NULL
                     OR ai_summary_draft IS NOT NULL
                     OR ai_key_points_draft IS NOT NULL
                     OR pending_is_visible_in_feed IS NOT NULL
                   )
                 ORDER BY id ASC
                 LIMIT %s""",
            (run_id, batch_size),
        ).fetchall()
        if not rows:
            break

        values: list[tuple[int, int]] = [
            (int(_row_get(row, "id")), int(_row_get(row, "new_version")))
            for row in rows
        ]
        placeholders = ", ".join(["(%s, %s)"] * len(values))
        value_params: list[int] = [item for pair in values for item in pair]
        now = datetime.now(timezone.utc)
        updated = pg_conn.execute(
            f"""WITH publish_values(id, new_version) AS (
                    VALUES {placeholders}
                 )
                UPDATE {schema}.clusters c
                   SET ai_title = COALESCE(c.ai_title_draft, c.ai_title),
                       ai_summary = COALESCE(c.ai_summary_draft, c.ai_summary),
                       ai_key_points = COALESCE(c.ai_key_points_draft, c.ai_key_points),
                       ai_title_draft = NULL,
                       ai_summary_draft = NULL,
                       ai_key_points_draft = NULL,
                       is_visible_in_feed = COALESCE((c.pending_is_visible_in_feed <> 0), c.is_visible_in_feed),
                       last_summary_warnings_json = COALESCE(c.pending_summary_warnings_json, c.last_summary_warnings_json),
                       pending_is_visible_in_feed = NULL,
                       pending_summary_warnings_json = NULL,
                       live_version = v.new_version,
                       last_updated_at = %s,
                       published_at = %s,
                       published_run_id = %s
                  FROM publish_values v
                 WHERE c.id = v.id
             RETURNING c.id, v.new_version""",
            (*value_params, now, now, run_id),
        ).fetchall()
        if not updated:
            _commit_if_supported(pg_conn)
            break

        action_values = [
            (int(_row_get(row, "id")), int(_row_get(row, "new_version")))
            for row in updated
        ]
        action_placeholders = ", ".join(["(%s, %s)"] * len(action_values))
        action_params: list[int] = [item for pair in action_values for item in pair]
        pg_conn.execute(
            f"""UPDATE {schema}.actions a
                   SET is_stale = 1
                  FROM (VALUES {action_placeholders}) AS v(id, new_version)
                 WHERE a.source_type = 'cluster'
                   AND a.source_id = v.id::text
                   AND (a.cluster_version IS NULL OR a.cluster_version < v.new_version)
                   AND a.is_stale = 0""",
            tuple(action_params),
        )
        published += len(updated)
        _commit_if_supported(pg_conn)
    return published


def _normalize_action_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    if "source_item_ids" in data:
        data["source_item_ids"] = _json_value(data.get("source_item_ids")) or []
    for col in (
        "created_at",
        "confirmed_at",
        "executed_at",
        "completed_at",
        "dismissed_at",
        "dispatched_at",
        "project_context_updated_at",
    ):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def log_action_event_remote(
    pg_conn: Any | None,
    action_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            log_action_event_remote(conn, action_id, event_type, detail)
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.action_logs
              (action_id, event_type, detail_json)
            VALUES (%s, %s, %s)""",
        (action_id, event_type, _maybe_jsonb(detail) if detail else None),
    )
    _commit_if_supported(pg_conn)


def create_action_remote(
    pg_conn: Any | None = None,
    *,
    source_type: str,
    title: str,
    action_type: str,
    prompt: str,
    source_item_ids: list[str] | None = None,
    reason: str | None = None,
    priority: str = "medium",
    related_project: str | None = None,
    status: str = "pending",
    direction: str = "_uncategorized",
    direction_label: str = "待归类",
    user_id: str | None = None,
    source_id: str | None = None,
    cluster_version: int | None = None,
    steps: list[str] | None = None,
) -> str:
    if pg_conn is None:
        with connect() as conn:
            return create_action_remote(
                conn,
                source_type=source_type,
                title=title,
                action_type=action_type,
                prompt=prompt,
                source_item_ids=source_item_ids,
                reason=reason,
                priority=priority,
                related_project=related_project,
                status=status,
                direction=direction,
                direction_label=direction_label,
                user_id=user_id,
                source_id=source_id,
                cluster_version=cluster_version,
                steps=steps,
            )
    action_id = str(uuid.uuid4())
    steps_text = json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) and steps else None
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.actions
              (id, user_id, source_type, source_item_ids, source_id,
               cluster_version, original_title, original_prompt,
               original_reason, original_priority, title, action_type,
               related_project, prompt, steps, reason, priority, status,
               direction, direction_label)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            action_id,
            user_id,
            source_type,
            _maybe_jsonb(source_item_ids or []),
            source_id,
            cluster_version,
            title,
            prompt,
            reason,
            priority,
            title,
            action_type,
            related_project,
            prompt,
            steps_text,
            reason,
            priority,
            status,
            direction,
            direction_label,
        ),
    )
    log_action_event_remote(
        pg_conn,
        action_id,
        "created",
        {"source_item_ids": source_item_ids or [], "source_type": source_type},
    )
    _commit_if_supported(pg_conn)
    invalidate_action_board_read_model_remote(pg_conn)
    return action_id


def _action_where(
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    if status == "in_progress":
        where.append("status = ANY(%(statuses)s)")
        params["statuses"] = ["confirmed", "executing", "dispatched"]
    elif status:
        where.append("status = %(status)s")
        params["status"] = status
    if priority:
        where.append("priority = %(priority)s")
        params["priority"] = priority
    if action_type:
        where.append("action_type = %(action_type)s")
        params["action_type"] = action_type
    if direction:
        where.append("direction = %(direction)s")
        params["direction"] = direction
    if source_filter == "with-source":
        where.append("jsonb_array_length(COALESCE(source_item_ids, '[]'::jsonb)) > 0")
    elif source_filter == "no-source":
        where.append("jsonb_array_length(COALESCE(source_item_ids, '[]'::jsonb)) = 0")
    return where, params


_ACTION_BOARD_LANES = (
    {
        "slug": "pending",
        "label": "待处理",
        "statuses": ["pending"],
    },
    {
        "slug": "in_progress",
        "label": "执行中",
        "statuses": ["confirmed", "executing", "dispatched"],
    },
    {
        "slug": "done",
        "label": "已完成",
        "statuses": ["done"],
    },
)
_ACTION_BOARD_VISIBLE_STATUSES = [
    status
    for lane in _ACTION_BOARD_LANES
    for status in lane["statuses"]
]


def _action_board_lane_for_status(status: str | None) -> str | None:
    value = str(status or "")
    for lane in _ACTION_BOARD_LANES:
        if value in lane["statuses"]:
            return str(lane["slug"])
    return None


def _action_board_lanes_for_status(status: str | None) -> list[dict[str, Any]]:
    if not status:
        return [dict(lane) for lane in _ACTION_BOARD_LANES]
    if status == "in_progress":
        return [dict(lane) for lane in _ACTION_BOARD_LANES if lane["slug"] == "in_progress"]
    lane_slug = _action_board_lane_for_status(status)
    if not lane_slug:
        return []
    return [dict(lane) for lane in _ACTION_BOARD_LANES if lane["slug"] == lane_slug]


def _action_date_filter_sql(date_filter: str | None) -> str | None:
    if date_filter == "today":
        return "created_at >= date_trunc('day', now())"
    if date_filter == "week":
        return "created_at >= date_trunc('day', now()) - interval '6 days'"
    return None


def _action_board_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), ACTION_BOARD_READ_MODEL_ENV, default=True)


def _action_board_read_model_refresh_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), ACTION_BOARD_READ_MODEL_REFRESH_ENV, default=True)


def _action_board_read_model_refresh_timeout_ms() -> int:
    return _env_int(
        _runtime_env(),
        ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=5000,
    )


def _action_board_viewer_scope(can_view_all: bool) -> str:
    return "admin" if can_view_all else "owner"


def _action_board_read_model_state_key(viewer_scope: str, owner_user_id: str | None) -> str:
    owner_key = owner_user_id if viewer_scope != "admin" and owner_user_id else "_all"
    return f"{ACTION_BOARD_READ_MODEL_STATE_PREFIX}:{viewer_scope}:{owner_key}"


def _action_board_scope_key(date_filter: str | None, priority: str | None) -> str:
    return f"date:{date_filter or 'all'}|priority:{priority or 'all'}"


def _action_board_read_model_supported(
    *,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    include_detail_payloads: bool,
) -> bool:
    return not any((action_type, direction, source_filter, include_detail_payloads))


def _action_board_read_model_active_version(
    conn: Any,
    schema: str,
    state_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""SELECT v.version_id,
                   v.viewer_scope,
                   v.owner_user_id,
                   v.payload_version,
                   v.generated_at,
                   v.completed_at,
                   v.max_action_updated_at,
                   v.total_actions,
                   v.meta_json,
                   (v.generated_at < date_trunc('day', now())) AS generated_before_today
              FROM {schema}.action_board_read_model_state st
              JOIN {schema}.action_board_read_model_versions v
                ON v.version_id = st.active_version_id
             WHERE st.key = %(state_key)s
               AND v.status = 'complete'
               AND v.payload_version = %(payload_version)s
             LIMIT 1""",
        {"state_key": state_key, "payload_version": ACTION_BOARD_READ_MODEL_VERSION},
    ).fetchone()
    return dict(row) if row else None


def _action_board_read_model_is_stale(active: dict[str, Any] | None, date_filter: str | None) -> bool:
    if not active:
        return True
    if int(active.get("payload_version") or 0) != ACTION_BOARD_READ_MODEL_VERSION:
        return True
    # The projection prebuilds "today" and "week" scopes. Rebuild once per day
    # even when no actions changed so those relative scopes do not drift at midnight.
    if date_filter in {"today", "week"} and bool(active.get("generated_before_today")):
        return True
    return False


def invalidate_action_board_read_model_remote(pg_conn: Any | None = None) -> None:
    """Invalidate active board projections after an action write.

    Action writes are low-volume, and the board has only a handful of active
    scope keys, so clearing all active state is simpler and safer than trying to
    infer which viewer/date/priority scopes changed.
    """
    if pg_conn is None:
        with connect() as conn:
            invalidate_action_board_read_model_remote(conn)
            return
    try:
        pg_conn.execute(
            f"DELETE FROM {remote_schema()}.action_board_read_model_state WHERE key LIKE %s",
            (f"{ACTION_BOARD_READ_MODEL_STATE_PREFIX}:%",),
        )
        _commit_if_supported(pg_conn)
    except Exception:
        _rollback_safely(pg_conn)


def refresh_action_board_read_model_remote(
    *,
    owner_user_id: str | None = None,
    can_view_all: bool = False,
) -> dict[str, Any]:
    """Build a complete action board projection for one viewer scope."""
    if not _action_board_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    viewer_scope = _action_board_viewer_scope(can_view_all)
    state_key = _action_board_read_model_state_key(viewer_scope, owner_user_id)
    version_id = str(uuid.uuid4())
    scope_defs = [
        {
            "scope_key": _action_board_scope_key(date_filter, priority),
            "date_filter": date_filter or "all",
            "priority_filter": priority,
            "priority_key": priority or "all",
        }
        for date_filter in (None, "today", "week")
        for priority in (None, "high", "medium", "low", "bug")
    ]
    lane_defs = [dict(lane) for lane in _ACTION_BOARD_LANES]
    where = ["status = ANY(%(visible_statuses)s)"]
    params: dict[str, Any] = {
        "version_id": version_id,
        "state_key": state_key,
        "viewer_scope": viewer_scope,
        "owner_user_id": owner_user_id,
        "payload_version": ACTION_BOARD_READ_MODEL_VERSION,
        "read_model_name": ACTION_BOARD_READ_MODEL_NAME,
        "visible_statuses": _ACTION_BOARD_VISIBLE_STATUSES,
        "scope_defs": json.dumps(scope_defs, ensure_ascii=False),
        "lane_defs": json.dumps(lane_defs, ensure_ascii=False),
    }
    if owner_user_id and viewer_scope != "admin":
        where.append("user_id = %(owner_user_id)s")
    where_sql = _where_sql(where)
    t0 = time.time()
    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, _action_board_read_model_refresh_timeout_ms())
            conn.execute(
                f"""INSERT INTO {schema}.action_board_read_model_versions (
                       version_id, viewer_scope, owner_user_id, payload_version,
                       status, generated_at, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, %(viewer_scope)s, %(owner_user_id)s,
                       %(payload_version)s, 'building', now(),
                       jsonb_build_object('read_model', %(read_model_name)s::text)
                     )""",
                params,
            )
            conn.commit()
            _set_short_statement_timeout(conn, _action_board_read_model_refresh_timeout_ms())
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_base")
            conn.execute(
                f"""CREATE TEMP TABLE action_board_rm_base ON COMMIT DROP AS
                    SELECT id::text AS action_id,
                           source_item_ids,
                           title,
                           action_type,
                           prompt,
                           priority,
                           status,
                           direction,
                           direction_label,
                           created_at,
                           GREATEST(
                             COALESCE(created_at, '-infinity'::timestamptz),
                             COALESCE(confirmed_at, '-infinity'::timestamptz),
                             COALESCE(executed_at, '-infinity'::timestamptz),
                             COALESCE(completed_at, '-infinity'::timestamptz),
                             COALESCE(dismissed_at, '-infinity'::timestamptz),
                             COALESCE(dispatched_at, '-infinity'::timestamptz),
                             COALESCE(project_context_updated_at, '-infinity'::timestamptz)
                           ) AS action_updated_at,
                           CASE
                             WHEN status = 'pending' THEN 'pending'
                             WHEN status IN ('confirmed', 'executing', 'dispatched') THEN 'in_progress'
                             WHEN status = 'done' THEN 'done'
                             ELSE NULL
                           END AS lane_slug,
                           jsonb_strip_nulls(jsonb_build_object(
                             'id', id::text,
                             'source_item_ids', COALESCE(source_item_ids, '[]'::jsonb),
                             'title', title,
                             'action_type', action_type,
                             'prompt', prompt,
                             'priority', priority,
                             'status', status,
                             'direction', direction,
                             'direction_label', direction_label,
                             'created_at', created_at
                           )) AS card_json
                      FROM {schema}.actions
                      {where_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.action_board_rm_base")
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_scope_defs")
            conn.execute(
                """CREATE TEMP TABLE action_board_rm_scope_defs ON COMMIT DROP AS
                   SELECT scope_key, date_filter, priority_filter, priority_key
                     FROM jsonb_to_recordset(%(scope_defs)s::jsonb)
                          AS scope(scope_key text, date_filter text,
                                   priority_filter text, priority_key text)""",
                params,
            )
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_scope_rows")
            conn.execute(
                """CREATE TEMP TABLE action_board_rm_scope_rows ON COMMIT DROP AS
                   SELECT sd.scope_key,
                          sd.date_filter,
                          sd.priority_filter,
                          sd.priority_key,
                          b.lane_slug,
                          b.status,
                          b.action_id,
                          b.created_at,
                          b.card_json
                     FROM pg_temp.action_board_rm_scope_defs sd
                     JOIN pg_temp.action_board_rm_base b
                       ON (sd.date_filter = 'all'
                           OR (sd.date_filter = 'today'
                               AND b.created_at >= date_trunc('day', now()))
                           OR (sd.date_filter = 'week'
                               AND b.created_at >= date_trunc('day', now()) - interval '6 days'))
                      AND (sd.priority_filter IS NULL OR b.priority = sd.priority_filter)
                    WHERE b.lane_slug IS NOT NULL"""
            )
            conn.execute("ANALYZE pg_temp.action_board_rm_scope_rows")
            conn.execute(
                f"""WITH scope_totals AS (
                       SELECT scope_key, count(*)::integer AS total_count
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key
                     ),
                     status_counts AS (
                       SELECT scope_key, status, count(*)::integer AS cnt
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key, status
                     ),
                     status_json AS (
                       SELECT scope_key, jsonb_object_agg(status, cnt) AS status_counts_json
                         FROM status_counts
                        GROUP BY scope_key
                     )
                     INSERT INTO {schema}.action_board_scopes (
                       version_id, scope_key, date_filter, priority_filter,
                       total_count, status_counts_json, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            sd.scope_key,
                            sd.date_filter,
                            sd.priority_key,
                            COALESCE(st.total_count, 0),
                            COALESCE(sj.status_counts_json, '{{}}'::jsonb),
                            now()
                       FROM pg_temp.action_board_rm_scope_defs sd
                       LEFT JOIN scope_totals st ON st.scope_key = sd.scope_key
                       LEFT JOIN status_json sj ON sj.scope_key = sd.scope_key""",
                params,
            )
            conn.execute(
                f"""WITH lane_defs AS (
                       SELECT slug, label
                         FROM jsonb_to_recordset(%(lane_defs)s::jsonb)
                              AS lane(slug text, label text, statuses jsonb)
                     ),
                     lane_counts AS (
                       SELECT scope_key, lane_slug, count(*)::integer AS total_count
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key, lane_slug
                     )
                     INSERT INTO {schema}.action_board_scope_lanes (
                       version_id, scope_key, lane_slug, lane_label,
                       total_count, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            sd.scope_key,
                            lane_defs.slug,
                            lane_defs.label,
                            COALESCE(lc.total_count, 0),
                            now()
                       FROM pg_temp.action_board_rm_scope_defs sd
                       CROSS JOIN lane_defs
                       LEFT JOIN lane_counts lc
                         ON lc.scope_key = sd.scope_key
                        AND lc.lane_slug = lane_defs.slug""",
                params,
            )
            conn.execute(
                f"""WITH ranked AS (
                       SELECT scope_key,
                              lane_slug,
                              action_id,
                              created_at,
                              card_json,
                              row_number() OVER (
                                PARTITION BY scope_key, lane_slug
                                ORDER BY created_at DESC NULLS LAST, action_id DESC
                              ) AS rn
                         FROM pg_temp.action_board_rm_scope_rows
                     )
                     INSERT INTO {schema}.action_board_scope_items (
                       version_id, scope_key, lane_slug, rank,
                       action_id, created_at, card_json
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            lane_slug,
                            rn::integer,
                            action_id,
                            created_at,
                            card_json
                       FROM ranked""",
                params,
            )
            version_stats = conn.execute(
                """SELECT count(*)::integer AS total_actions,
                          max(action_updated_at) AS max_action_updated_at
                     FROM pg_temp.action_board_rm_base"""
            ).fetchone()
            conn.execute(
                f"""UPDATE {schema}.action_board_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_action_updated_at = %(max_action_updated_at)s,
                           total_actions = %(total_actions)s,
                           meta_json = meta_json || jsonb_build_object(
                             'elapsed_ms', %(elapsed_ms)s::integer,
                             'scope_count', %(scope_count)s::integer
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {
                    **params,
                    "max_action_updated_at": (version_stats or {}).get("max_action_updated_at"),
                    "total_actions": int((version_stats or {}).get("total_actions") or 0),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "scope_count": len(scope_defs),
                },
            )
            conn.execute(
                f"""INSERT INTO {schema}.action_board_read_model_state (
                       key, active_version_id, updated_at
                     )
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                params,
            )
            scope_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.action_board_scope_items
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            conn.execute(
                f"""DELETE FROM {schema}.action_board_read_model_versions v
                     WHERE v.viewer_scope = %(viewer_scope)s
                       AND v.owner_user_id IS NOT DISTINCT FROM %(owner_user_id)s
                       AND NOT EXISTS (
                         SELECT 1 FROM {schema}.action_board_read_model_state st
                          WHERE st.active_version_id = v.version_id
                       )
                       AND v.version_id NOT IN (
                         SELECT version_id
                           FROM {schema}.action_board_read_model_versions
                          WHERE viewer_scope = %(viewer_scope)s
                            AND owner_user_id IS NOT DISTINCT FROM %(owner_user_id)s
                          ORDER BY generated_at DESC
                          LIMIT 3
                       )""",
                params,
            )
            conn.commit()
        except Exception as exc:
            _rollback_safely(conn)
            try:
                conn.execute(
                    f"""UPDATE {schema}.action_board_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": str(exc)[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError("action board read model refresh failed") from exc
    return {
        "ok": True,
        "version_id": version_id,
        "viewer_scope": viewer_scope,
        "owner_user_id": owner_user_id,
        "scope_items": int((scope_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _query_actions_board_read_model_remote(
    *,
    status: str | None,
    priority: str | None,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    date_filter: str | None,
    user_id: str | None,
    can_view_all: bool,
    limit_per_direction: int,
    offset: int,
    include_detail_payloads: bool,
) -> dict[str, Any] | None:
    if not _action_board_read_model_enabled():
        return None
    if not _action_board_read_model_supported(
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        include_detail_payloads=include_detail_payloads,
    ):
        return None
    lane_defs = _action_board_lanes_for_status(status)
    if not lane_defs:
        return None
    limit = max(1, min(int(limit_per_direction or 20), 50))
    start = max(0, int(offset or 0))
    schema = remote_schema()
    viewer_scope = _action_board_viewer_scope(can_view_all)
    state_key = _action_board_read_model_state_key(viewer_scope, user_id)
    scope_key = _action_board_scope_key(date_filter, priority)
    lane_slugs = [str(lane["slug"]) for lane in lane_defs]

    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
            active = _action_board_read_model_active_version(conn, schema, state_key)
    except Exception as exc:
        print(f"[warn] action board read model unavailable: {exc}")
        return None

    if _action_board_read_model_is_stale(active, date_filter):
        if not _action_board_read_model_refresh_enabled():
            return None
        try:
            refresh_action_board_read_model_remote(
                owner_user_id=user_id,
                can_view_all=can_view_all,
            )
            with connect() as conn:
                _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
                active = _action_board_read_model_active_version(conn, schema, state_key)
        except Exception as exc:
            print(f"[warn] action board read model refresh degraded to live query: {exc}")
            return None

    if not active or not active.get("version_id"):
        return None

    version_id = str(active["version_id"])
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
            scope_row = conn.execute(
                f"""SELECT scope_key, total_count, status_counts_json
                      FROM {schema}.action_board_scopes
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                     LIMIT 1""",
                {"version_id": version_id, "scope_key": scope_key},
            ).fetchone()
            if not scope_row:
                return None
            lane_rows = conn.execute(
                f"""SELECT lane_slug, lane_label, total_count
                      FROM {schema}.action_board_scope_lanes
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                       AND lane_slug = ANY(%(lane_slugs)s)
                     ORDER BY CASE lane_slug
                                WHEN 'pending' THEN 0
                                WHEN 'in_progress' THEN 1
                                WHEN 'done' THEN 2
                                ELSE 3
                              END""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "lane_slugs": lane_slugs,
                },
            ).fetchall()
            item_rows = conn.execute(
                f"""SELECT lane_slug, rank, action_id, created_at, card_json
                      FROM {schema}.action_board_scope_items
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                       AND lane_slug = ANY(%(lane_slugs)s)
                       AND rank > %(start)s
                       AND rank <= %(end)s
                     ORDER BY CASE lane_slug
                                WHEN 'pending' THEN 0
                                WHEN 'in_progress' THEN 1
                                WHEN 'done' THEN 2
                                ELSE 3
                              END,
                              rank ASC""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "lane_slugs": lane_slugs,
                    "start": start,
                    "end": start + limit,
                },
            ).fetchall()
    except Exception as exc:
        print(f"[warn] action board read model query degraded to live query: {exc}")
        return None

    items_by_lane: dict[str, list[dict[str, Any]]] = {slug: [] for slug in lane_slugs}
    for row in item_rows:
        card = _json_value(row.get("card_json"))
        if not isinstance(card, dict):
            continue
        action = _normalize_action_row(card)
        items_by_lane.setdefault(str(row.get("lane_slug")), []).append(action)

    lane_counts = {str(row.get("lane_slug")): int(row.get("total_count") or 0) for row in lane_rows}
    lane_labels = {str(row.get("lane_slug")): str(row.get("lane_label") or row.get("lane_slug")) for row in lane_rows}
    directions: list[dict[str, Any]] = []
    for lane in lane_defs:
        slug = str(lane["slug"])
        items = items_by_lane.get(slug, [])
        total = int(lane_counts.get(slug, 0) or 0)
        loaded_until = start + len(items)
        has_more = total > loaded_until
        directions.append({
            "slug": slug,
            "label": lane_labels.get(slug) or str(lane["label"]),
            "count": total,
            "items": items,
            "has_more": has_more,
            "next_offset": loaded_until if has_more else None,
        })

    raw_counts = _json_value(scope_row.get("status_counts_json"))
    counts = {status_value: 0 for status_value in _ACTION_BOARD_VISIBLE_STATUSES}
    if isinstance(raw_counts, dict):
        counts.update({str(key): int(value or 0) for key, value in raw_counts.items()})
    counts["in_progress"] = sum(
        int(counts.get(status_value, 0) or 0)
        for status_value in ("confirmed", "executing", "dispatched")
    )
    counts["total"] = int(scope_row.get("total_count") or 0)
    return {
        "counts": counts,
        "directions": directions,
        "meta": {
            "limit_per_direction": limit,
            "offset": start,
            "degraded": False,
            "detail_degraded": False,
            "detail_included": False,
            "read_model": ACTION_BOARD_READ_MODEL_NAME,
            "read_model_version_id": version_id,
            "scope_key": scope_key,
            "query_strategy": "action_board_read_model",
        },
    }


def get_actions_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    where_sql = _where_sql(where)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT *
                  FROM {remote_schema()}.actions
                  {where_sql}
                 ORDER BY
                   CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                               WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                               WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                               WHEN 'dismissed' THEN 4 ELSE 5 END,
                   CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                   created_at DESC""",
            params,
        ).fetchall()
    return [_normalize_action_row(row) for row in rows]


def _action_source_ids(value: Any) -> list[str]:
    data = _json_value(value)
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


_ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS = (
    "created_at",
    "confirmed_at",
    "executed_at",
    "completed_at",
    "dismissed_at",
    "dispatched_at",
    "project_context_updated_at",
)


def _action_source_updated_at(action: dict[str, Any] | None) -> str | None:
    if not action:
        return None
    values = [
        action.get(field)
        for field in _ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS
        if action.get(field)
    ]
    if not values:
        return None
    return _timestamp_value(max(values, key=sort_key))


def _action_detail_read_model_fresh(row: dict[str, Any]) -> bool:
    source_updated_at = _action_source_updated_at(row)
    if not source_updated_at:
        return True
    cached_source_updated_at = row.get("source_updated_at")
    if not cached_source_updated_at:
        return False
    return sort_key(cached_source_updated_at) >= sort_key(source_updated_at)


def _source_item_by_id(rows: list[Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: dict(row) for row in rows}


def _action_source_items_from_map(
    by_id: dict[str, dict[str, Any]],
    source_ids: list[str],
    *,
    request_user_id: str | None,
    can_view_all: bool,
) -> list[dict[str, Any]]:
    out = []
    for sid in source_ids:
        item = copy.deepcopy(by_id.get(sid))
        if not item:
            continue
        if (
            item.get("platform") == "manual"
            and not can_view_all
            and item.get("user_id") != request_user_id
        ):
            continue
        detail = _json_value(item.get("detail_json")) or {}
        item["referenced_urls"] = detail.get("referenced_urls", []) if isinstance(detail, dict) else []
        item.pop("detail_json", None)
        item.pop("user_id", None)
        out.append(item)
    return out


def get_actions_payload_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
    request_user_id: str | None = None,
    can_view_all: bool = False,
    include_source_items: bool = False,
    include_detail_payloads: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit = max(1, min(int(limit or 100), 200))
    page_offset = max(0, int(offset or 0))
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    where_sql = _where_sql(where)
    page_params = {**params, "limit": page_limit, "offset": page_offset}
    scope_where = []
    scope_params = {}
    if user_id:
        scope_where.append("user_id = %(user_id)s")
        scope_params["user_id"] = user_id
    direction_where = ["status IN ('pending','confirmed','executing','dispatched')", *scope_where]
    with connect() as conn:
        _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
        action_rows = conn.execute(
            f"""SELECT id, source_item_ids, title, action_type, prompt,
                      priority, status, direction, direction_label, created_at
                  FROM {remote_schema()}.actions
                  {where_sql}
                 ORDER BY
                   CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                               WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                               WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                               WHEN 'dismissed' THEN 4 ELSE 5 END,
                   CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                   created_at DESC
                 LIMIT %(limit)s OFFSET %(offset)s""",
            page_params,
        ).fetchall()
        count_rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(scope_where)}
                 GROUP BY status""",
            scope_params,
        ).fetchall()
        direction_rows = conn.execute(
            f"""SELECT direction, direction_label, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(direction_where)}
                 GROUP BY direction, direction_label
                 ORDER BY cnt DESC""",
            scope_params,
        ).fetchall()
        actions = [_normalize_action_row(row) for row in action_rows]
        if include_source_items:
            source_ids = list(dict.fromkeys(
                sid
                for action in actions
                for sid in _action_source_ids(action.get("source_item_ids"))
            ))
            source_by_id: dict[str, dict[str, Any]] = {}
            if source_ids:
                placeholders = ", ".join(["%s"] * len(source_ids))
                source_rows = conn.execute(
                    f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                          FROM {remote_schema()}.items
                         WHERE id IN ({placeholders})""",
                    tuple(source_ids),
                ).fetchall()
                source_by_id = _source_item_by_id(source_rows)
            for action in actions:
                action_ids = _action_source_ids(action.get("source_item_ids"))
                source_items = _action_source_items_from_map(
                    source_by_id,
                    action_ids,
                    request_user_id=request_user_id,
                    can_view_all=can_view_all,
                )
                action["source_item_ids"] = action_ids
                action["source_items"] = source_items
                action["source_item_count"] = len(source_items)
    if include_detail_payloads:
        viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
        detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(actions)
        detail_payloads = get_action_detail_read_models_remote(
            detail_action_ids,
            viewer_scope=viewer_scope,
            owner_user_id=user_id,
        )
        actions = [
            action_detail_read_model.merge_action_with_detail_payload(
                action,
                detail_payloads.get(str(action.get("id"))),
            )
            for action in actions
        ]
    return {
        "actions": actions,
        "counts": {row["status"]: row["cnt"] for row in count_rows},
        "directions": [
            {"slug": row["direction"], "label": row["direction_label"], "count": row["cnt"]}
            for row in direction_rows
        ],
        "meta": {
            "limit": page_limit,
            "offset": page_offset,
            "degraded": False,
            "query_strategy": "legacy_actions_paginated",
        },
    }


def get_actions_board_payload_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    date_filter: str | None = None,
    user_id: str | None = None,
    request_user_id: str | None = None,
    can_view_all: bool = False,
    limit_per_direction: int = 20,
    offset: int = 0,
    include_detail_payloads: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit_per_direction or 20), 50))
    start = max(0, int(offset or 0))
    read_model_payload = _query_actions_board_read_model_remote(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        date_filter=date_filter,
        user_id=user_id,
        can_view_all=can_view_all,
        limit_per_direction=limit,
        offset=start,
        include_detail_payloads=include_detail_payloads,
    )
    if read_model_payload is not None:
        return read_model_payload
    schema = remote_schema()
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    lane_defs = _action_board_lanes_for_status(status)
    if not lane_defs:
        return {
            "counts": {"total": 0, "in_progress": 0},
            "directions": [],
            "meta": {
                "limit_per_direction": limit,
                "offset": start,
                "degraded": False,
                "detail_degraded": False,
                "detail_included": False,
                "read_model": False,
                "query_strategy": "status_lanes_lateral",
            },
        }
    if status is None:
        where.append("status = ANY(%(board_statuses)s)")
        params["board_statuses"] = _ACTION_BOARD_VISIBLE_STATUSES
    date_sql = _action_date_filter_sql(date_filter)
    if date_sql:
        where.append(date_sql)
    where_sql = _where_sql(where)

    detail_degraded = False
    with connect() as conn:
        _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
        count_rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {schema}.actions
                  {where_sql}
                 GROUP BY status""",
            params,
        ).fetchall()

        raw_counts = {str(row.get("status")): int(row.get("cnt") or 0) for row in count_rows}
        lane_summaries = [
            {
                "slug": lane["slug"],
                "label": lane["label"],
                "statuses": lane["statuses"],
                "cnt": sum(raw_counts.get(status, 0) for status in lane["statuses"]),
            }
            for lane in lane_defs
        ]

        item_where_sql = _where_sql([
            *where,
            "status IN (SELECT jsonb_array_elements_text(lane_summary.statuses))",
        ])
        board_rows = []
        if lane_summaries:
            board_rows = conn.execute(
                f"""WITH lane_summary AS (
                       SELECT slug, label, statuses, cnt
                         FROM jsonb_to_recordset(%(lane_summaries)s::jsonb)
                              AS lane(slug text, label text, statuses jsonb, cnt integer)
                     )
                     SELECT lane_summary.slug AS board_lane,
                            lane_summary.label AS board_lane_label,
                            lane_summary.cnt AS lane_total,
                            a.id, a.source_item_ids, a.title, a.action_type, a.prompt,
                            a.priority, a.status, a.direction, a.direction_label, a.created_at
                       FROM lane_summary
                       LEFT JOIN LATERAL (
                         SELECT id, source_item_ids, title, action_type, prompt,
                                priority, status, direction, direction_label, created_at
                           FROM {schema}.actions
                           {item_where_sql}
                          ORDER BY created_at DESC
                          LIMIT %(limit)s OFFSET %(offset)s
                       ) a ON true
                      ORDER BY CASE lane_summary.slug
                                 WHEN 'pending' THEN 0
                                 WHEN 'in_progress' THEN 1
                                 WHEN 'done' THEN 2
                                 ELSE 3
                               END,
                               a.created_at DESC""",
                {
                    **params,
                    "lane_summaries": json.dumps(lane_summaries, ensure_ascii=False),
                    "limit": limit,
                    "offset": start,
                },
            ).fetchall()

        directions: list[dict[str, Any]] = []
        visible_actions: list[dict[str, Any]] = []
        directions_by_slug: dict[str, dict[str, Any]] = {}
        action_cols = (
            "id",
            "source_item_ids",
            "title",
            "action_type",
            "prompt",
            "priority",
            "status",
            "direction",
            "direction_label",
            "created_at",
        )
        for row in board_rows:
            slug = str(row.get("board_lane") or "")
            label = str(row.get("board_lane_label") or slug)
            total = int(row.get("lane_total") or 0)
            entry = directions_by_slug.setdefault(
                slug,
                {
                    "slug": slug,
                    "label": label,
                    "count": total,
                    "items": [],
                },
            )
            if row.get("id") is None:
                continue
            action = _normalize_action_row({col: row.get(col) for col in action_cols})
            entry["items"].append(action)
            visible_actions.append(action)

        directions = list(directions_by_slug.values())
        for entry in directions:
            loaded_until = start + len(entry["items"])
            entry["has_more"] = int(entry["count"] or 0) > loaded_until
            entry["next_offset"] = loaded_until if entry["has_more"] else None

        if include_detail_payloads and visible_actions:
            viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
            detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(visible_actions)
            try:
                detail_payloads = _get_action_list_detail_payloads_remote(
                    conn,
                    detail_action_ids,
                    viewer_scope=viewer_scope,
                    owner_user_id=user_id,
                    statement_timeout_ms=_remote_actions_board_detail_timeout_ms(),
                )
                merged_by_id = {
                    str(action.get("id")): action_detail_read_model.merge_action_with_detail_payload(
                        action,
                        detail_payloads.get(str(action.get("id"))),
                    )
                    for action in visible_actions
                }
                for entry in directions:
                    entry["items"] = [
                        merged_by_id.get(str(action.get("id")), action)
                        for action in entry["items"]
                    ]
            except Exception as exc:
                detail_degraded = True
                _rollback_safely(conn)
                print(f"[warn] actions board detail payload degraded: {exc}")

    counts = {status: 0 for status in _ACTION_BOARD_VISIBLE_STATUSES}
    counts.update({str(row["status"]): int(row["cnt"] or 0) for row in count_rows})
    counts["in_progress"] = sum(
        int(counts.get(status, 0) or 0)
        for status in ("confirmed", "executing", "dispatched")
    )
    counts["total"] = sum(int(v or 0) for v in counts.values())
    counts["total"] -= int(counts.get("in_progress", 0) or 0)
    return {
        "counts": counts,
        "directions": directions,
        "meta": {
            "limit_per_direction": limit,
            "offset": start,
            "degraded": False,
            "detail_degraded": detail_degraded,
            "detail_included": bool(include_detail_payloads and not detail_degraded),
            "read_model": False,
            "query_strategy": "status_lanes_lateral",
        },
    }


def _get_action_list_detail_payloads_remote(
    conn: Any,
    action_ids: list[str],
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    statement_timeout_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(action_id) for action_id in action_ids if action_id))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    where = [
        f"action_id IN ({placeholders})",
        "viewer_scope = %s",
        "payload_version = %s",
    ]
    params: list[Any] = [
        *ids,
        viewer_scope,
        action_detail_read_model.READ_MODEL_VERSION,
    ]
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %s")
        params.append(owner_user_id)
    if statement_timeout_ms is not None:
        _set_short_statement_timeout(conn, statement_timeout_ms)
    rows = conn.execute(
        f"""SELECT action_id,
                   jsonb_strip_nulls(jsonb_build_object(
                     'steps', payload->'steps',
                     'source_items', payload->'source_items',
                     'source_item_count', payload->'source_item_count',
                     'execution_status', payload->'execution_status',
                     '_list_payload', true
                   )) AS payload
              FROM {remote_schema()}.action_detail_read_models
             WHERE {" AND ".join(where)}""",
        tuple(params),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _json_value(row.get("payload"))
        if isinstance(payload, dict):
            out[str(row.get("action_id"))] = payload
    return out


def get_action_remote(action_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    where = ["id = %(action_id)s"]
    params = {"action_id": action_id}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.actions {_where_sql(where)}",
            params,
        ).fetchone()
    return _normalize_action_row(row) if row else None


def update_action_remote(
    action_id: str,
    *,
    owner_user_id: str | None = None,
    pg_conn: Any | None = None,
    **fields: Any,
) -> bool:
    allowed = {
        "title", "prompt", "reason", "priority", "status", "action_type",
        "related_project", "source_item_ids", "direction", "direction_label",
        "execution_tool", "execution_result", "execution_exit_code",
        "execution_model", "execution_duration_seconds", "session_id",
        "project_context", "project_context_updated_at", "confirmed_at",
        "executed_at", "completed_at", "dismissed_at", "discord_thread_id",
        "discord_thread_url", "dispatched_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if pg_conn is None:
        with connect() as conn:
            return update_action_remote(
                action_id,
                owner_user_id=owner_user_id,
                pg_conn=conn,
                **updates,
            )
    sets = []
    params: dict[str, Any] = {"action_id": action_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        params[pname] = _maybe_jsonb(value) if key == "source_item_ids" else value
    where = "id = %(action_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    cur = pg_conn.execute(
        f"UPDATE {remote_schema()}.actions SET {', '.join(sets)} WHERE {where}",
        params,
    )
    _commit_if_supported(pg_conn)
    invalidate_action_board_read_model_remote(pg_conn)
    return (getattr(cur, "rowcount", 0) or 0) > 0


def delete_action_remote(action_id: str, *, owner_user_id: str | None = None) -> bool:
    if owner_user_id and not get_action_remote(action_id, user_id=owner_user_id):
        return False
    with connect() as conn:
        params = {"action_id": action_id}
        conn.execute(
            f"DELETE FROM {remote_schema()}.action_feedback WHERE action_id = %(action_id)s",
            params,
        )
        conn.execute(
            f"DELETE FROM {remote_schema()}.action_logs WHERE action_id = %(action_id)s",
            params,
        )
        where = "id = %(action_id)s"
        if owner_user_id:
            where += " AND user_id = %(owner_user_id)s"
            params["owner_user_id"] = owner_user_id
        cur = conn.execute(f"DELETE FROM {remote_schema()}.actions WHERE {where}", params)
        conn.commit()
        invalidate_action_board_read_model_remote(conn)
        return (getattr(cur, "rowcount", 0) or 0) > 0


def get_action_counts_remote(*, user_id: str | None = None) -> dict[str, int]:
    where = []
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(where)}
                 GROUP BY status""",
            params,
        ).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


def get_action_directions_remote(*, user_id: str | None = None) -> list[dict[str, Any]]:
    where = ["status IN ('pending','confirmed','executing','dispatched')"]
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT direction, direction_label, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(where)}
                 GROUP BY direction, direction_label
                 ORDER BY cnt DESC""",
            params,
        ).fetchall()
    return [
        {"slug": row["direction"], "label": row["direction_label"], "count": row["cnt"]}
        for row in rows
    ]


def get_actions_by_item_remote(item_id: str, *, user_id: str | None = None) -> list[dict[str, Any]]:
    where = ["source_item_ids @> %(source_item_ids)s"]
    params: dict[str, Any] = {"source_item_ids": _maybe_jsonb([item_id])}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, action_type, priority, status, reason, steps,
                      created_at, source_item_ids
                 FROM {remote_schema()}.actions
                 {_where_sql(where)}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()
    result = []
    for row in rows:
        d = _normalize_action_row(row)
        # v2 §14.3(T7): steps 供信息弹窗列表原位展示
        raw_steps = d.get('steps')
        if isinstance(raw_steps, str):
            try:
                d['steps'] = json.loads(raw_steps)
            except (json.JSONDecodeError, TypeError):
                d['steps'] = None
        result.append(d)
    return result


def get_cluster_actions_remote(cluster_id: int, *, user_id: str) -> list[dict[str, Any]]:
    """BF-0706-3: 事件弹窗行动列表的 remote 分支(镜像 clusters.cluster_actions_list 本地查询)。

    Why: cluster_actions_list 之前只读本地 sqlite,生产走 Supabase 时事件下已生成行动
    点一个都不显示。按 source_type='cluster' AND source_id AND user_id 查。
    """
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, action_type, prompt, priority, status,
                      cluster_version, is_stale, created_at
                 FROM {remote_schema()}.actions
                WHERE source_type = 'cluster'
                  AND source_id = %(source_id)s
                  AND user_id = %(user_id)s
                ORDER BY created_at DESC""",
            {"source_id": str(cluster_id), "user_id": user_id},
        ).fetchall()
    return [_normalize_action_row(row) for row in rows]


def add_action_feedback_remote(action_id: str, phase: str, rating: str, comment: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.action_feedback
                  (action_id, phase, rating, comment)
                VALUES (%s, %s, %s, %s)""",
            (action_id, phase, rating, comment),
        )
        log_action_event_remote(
            conn,
            action_id,
            "feedback",
            {"phase": phase, "rating": rating, "comment": comment},
        )
        conn.commit()


def get_item_action_context_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, platform, title, content, ai_summary, ai_key_points,
                      ai_category, detail_json
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["detail_json"] = _json_value(item.get("detail_json"))
    return item


def record_keywords_remote(keywords: list[str], platform: str) -> None:
    if not keywords:
        return
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for keyword in keywords:
            kw = str(keyword or "").strip()
            if not kw:
                continue
            conn.execute(
                f"""INSERT INTO {remote_schema()}.search_keywords
                      (keyword, platform, last_used_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (keyword, platform) DO UPDATE SET
                      last_used_at = excluded.last_used_at""",
                (kw, platform, now),
            )
        conn.commit()


def query_link_enrichment_items_remote(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, content, detail_json, url
                  FROM {remote_schema()}.items
                 WHERE fetched_at > now() - interval '2 days'
                   AND (detail_json IS NULL OR NOT (detail_json ? 'referenced_urls'))
                 ORDER BY fetched_at DESC
                 LIMIT %s""",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["detail_json"] = _json_value(item.get("detail_json"))
        out.append(item)
    return out


def update_item_detail_json_remote(item_id: str, detail_json: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.items SET detail_json = %s WHERE id = %s",
            (_maybe_jsonb(detail_json), item_id),
        )
        conn.commit()


def get_action_source_items_remote(
    source_ids: list[str],
    *,
    request_user_id: str | None,
    can_view_all: bool,
) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    placeholders = ", ".join(["%s"] * len(source_ids))
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                  FROM {remote_schema()}.items
                 WHERE id IN ({placeholders})""",
            tuple(source_ids),
        ).fetchall()
    return _action_source_items_from_map(
        _source_item_by_id(rows),
        source_ids,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
    )


def get_action_detail_read_model_remote(
    action_id: str,
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
) -> dict[str, Any] | None:
    where = [
        "action_id = %(action_id)s",
        "viewer_scope = %(viewer_scope)s",
        "payload_version = %(payload_version)s",
    ]
    params: dict[str, Any] = {
        "action_id": action_id,
        "viewer_scope": viewer_scope,
        "payload_version": action_detail_read_model.READ_MODEL_VERSION,
    }
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %(owner_user_id)s")
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        row = conn.execute(
            f"""SELECT rm.payload, rm.source_updated_at,
                       a.created_at, a.confirmed_at, a.executed_at,
                       a.completed_at, a.dismissed_at, a.dispatched_at,
                       a.project_context_updated_at
                  FROM {remote_schema()}.action_detail_read_models rm
                  JOIN {remote_schema()}.actions a
                    ON a.id = rm.action_id
                 {_where_sql([clause.replace("action_id", "rm.action_id", 1) for clause in where])}
                 LIMIT 1""",
            params,
        ).fetchone()
    if not row:
        return None
    row_data = dict(row)
    if not _action_detail_read_model_fresh(row_data):
        return None
    payload = _json_value(row.get("payload"))
    return payload if isinstance(payload, dict) else None


def get_action_detail_read_models_remote(
    action_ids: list[str],
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    statement_timeout_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(action_id) for action_id in action_ids if action_id))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    where = [
        f"action_id IN ({placeholders})",
        "viewer_scope = %s",
        "payload_version = %s",
    ]
    params: list[Any] = [
        *ids,
        viewer_scope,
        action_detail_read_model.READ_MODEL_VERSION,
    ]
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %s")
        params.append(owner_user_id)
    with connect() as conn:
        if statement_timeout_ms is not None:
            _set_short_statement_timeout(conn, statement_timeout_ms)
        rows = conn.execute(
            f"""SELECT rm.action_id, rm.payload, rm.source_updated_at,
                       a.created_at, a.confirmed_at, a.executed_at,
                       a.completed_at, a.dismissed_at, a.dispatched_at,
                       a.project_context_updated_at
                  FROM {remote_schema()}.action_detail_read_models rm
                  JOIN {remote_schema()}.actions a
                    ON a.id = rm.action_id
                 WHERE {" AND ".join(clause.replace("action_id", "rm.action_id", 1) for clause in where)}""",
            tuple(params),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_data = dict(row)
        if not _action_detail_read_model_fresh(row_data):
            continue
        payload = _json_value(row.get("payload"))
        if isinstance(payload, dict):
            out[str(row.get("action_id"))] = payload
    return out


def action_detail_read_model_freshness_remote(
    *,
    viewer_scope: str = "admin",
    limit: int | None = None,
) -> dict[str, Any]:
    """Read-only health probe for action detail read-model payloads used by Action Tab."""
    safe_limit = max(1, min(int(limit or action_detail_read_model.LIST_PREFETCH_TOTAL), 100))
    schema = remote_schema()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            latest_row = conn.execute(
                f"""SELECT created_at
                      FROM {schema}.actions
                     ORDER BY created_at DESC
                     LIMIT 1"""
            ).fetchone()
            rows = conn.execute(
                f"""WITH top_actions AS (
                       SELECT id, created_at, confirmed_at, executed_at,
                              completed_at, dismissed_at, dispatched_at,
                              project_context_updated_at
                         FROM {schema}.actions
                        ORDER BY
                          CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                                      WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                                      WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                                      WHEN 'dismissed' THEN 4 ELSE 5 END,
                          CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                        WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                          created_at DESC
                        LIMIT %(limit)s
                     )
                     SELECT a.id, a.created_at, a.confirmed_at, a.executed_at,
                            a.completed_at, a.dismissed_at, a.dispatched_at,
                            a.project_context_updated_at, rm.source_updated_at
                       FROM top_actions a
                       LEFT JOIN {schema}.action_detail_read_models rm
                         ON rm.action_id = a.id
                        AND rm.viewer_scope = %(viewer_scope)s
                        AND rm.payload_version = %(payload_version)s
                      ORDER BY
                        CASE WHEN rm.action_id IS NULL THEN 1 ELSE 0 END DESC,
                        a.created_at DESC""",
                {
                    "limit": safe_limit,
                    "viewer_scope": viewer_scope,
                    "payload_version": action_detail_read_model.READ_MODEL_VERSION,
                },
            ).fetchall()
    except Exception as exc:
        raise RemoteDBError(f"action detail read model freshness probe failed: {exc}") from exc

    top_count = len(rows)
    missing = 0
    stale = 0
    stale_action_ids: list[str] = []
    for row in rows:
        data = dict(row)
        if not data.get("source_updated_at"):
            missing += 1
            stale_action_ids.append(str(data.get("id")))
            continue
        if not _action_detail_read_model_fresh(data):
            stale += 1
            stale_action_ids.append(str(data.get("id")))
    return {
        "enabled": True,
        "read_model": "action_detail_v1",
        "payload_version": action_detail_read_model.READ_MODEL_VERSION,
        "viewer_scope": viewer_scope,
        "data_backend": feed_read_backend(),
        "latest_action_created_at": _timestamp_value((latest_row or {}).get("created_at")),
        "sample_limit": safe_limit,
        "sampled_actions": top_count,
        "prefetch_missing_count": missing,
        "prefetch_stale_count": stale,
        "prefetch_unfresh_count": missing + stale,
        "stale_action_ids_sample": stale_action_ids[:10],
        "stale": bool(missing or stale),
    }


def upsert_action_detail_read_model_remote(
    *,
    action_id: str,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    payload: dict[str, Any],
    source_item_ids: list[str] | None = None,
    source_updated_at: str | None = None,
    pg_conn: Any | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            upsert_action_detail_read_model_remote(
                action_id=action_id,
                viewer_scope=viewer_scope,
                owner_user_id=owner_user_id,
                payload=payload,
                source_item_ids=source_item_ids,
                source_updated_at=source_updated_at,
                pg_conn=conn,
            )
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.action_detail_read_models
              (action_id, viewer_scope, owner_user_id, payload, source_item_ids,
               payload_version, built_at, source_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (action_id, viewer_scope) DO UPDATE SET
              owner_user_id = excluded.owner_user_id,
              payload = excluded.payload,
              source_item_ids = excluded.source_item_ids,
              payload_version = excluded.payload_version,
              built_at = excluded.built_at,
              source_updated_at = excluded.source_updated_at""",
        (
            action_id,
            viewer_scope,
            owner_user_id,
            _maybe_jsonb(payload),
            _maybe_jsonb(source_item_ids or []),
            action_detail_read_model.READ_MODEL_VERSION,
            source_updated_at,
        ),
    )
    _commit_if_supported(pg_conn)


def delete_action_detail_read_model_remote(
    action_id: str,
    *,
    viewer_scope: str | None = None,
    pg_conn: Any | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            delete_action_detail_read_model_remote(action_id, viewer_scope=viewer_scope, pg_conn=conn)
            return
    params: dict[str, Any] = {"action_id": action_id}
    where = "action_id = %(action_id)s"
    if viewer_scope:
        where += " AND viewer_scope = %(viewer_scope)s"
        params["viewer_scope"] = viewer_scope
    pg_conn.execute(f"DELETE FROM {remote_schema()}.action_detail_read_models WHERE {where}", params)
    _commit_if_supported(pg_conn)


def build_action_detail_read_model_remote(
    action_id: str,
    *,
    request_user_id: str | None,
    can_view_all: bool,
    owner_user_id: str | None = None,
    execution_status: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any] | None:
    action = get_action_remote(action_id, user_id=None if can_view_all else owner_user_id)
    if not action:
        return None
    source_ids = action_detail_read_model.parse_source_item_ids(action.get("source_item_ids"))
    source_items = get_action_source_items_remote(
        source_ids,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
    )
    payload = action_detail_read_model.build_action_detail_payload(
        action,
        source_items=source_items,
        execution_status=execution_status,
    )
    if persist:
        upsert_action_detail_read_model_remote(
            action_id=action_id,
            viewer_scope=action_detail_read_model.viewer_scope_for(can_view_all=can_view_all),
            owner_user_id=action.get("user_id"),
            payload=payload,
            source_item_ids=source_ids,
            source_updated_at=_action_source_updated_at(action),
        )
    return payload


def _normalize_interest_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for field in ("keywords", "suggestion"):
        data[field] = _json_value(data.get(field))
        if field == "keywords" and data[field] is None:
            data[field] = []
    for col in ("created_at", "last_scan_at"):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def create_interest_remote(
    *,
    name: str,
    description: str | None = None,
    keywords: list[str] | None = None,
    sort: str = "relevance",
    item_limit: int = 30,
    scope: str = "all",
    user_id: str | None = None,
) -> int:
    with connect() as conn:
        row = conn.execute(
            f"""INSERT INTO {remote_schema()}.interests
                  (user_id, name, description, keywords, sort, item_limit, scope)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
            (user_id, name, description, _maybe_jsonb(keywords or []), sort, item_limit, scope),
        ).fetchone()
        conn.commit()
        return _row_id(row)


def list_interests_remote(*, user_id: str | None = None) -> list[dict[str, Any]]:
    where = []
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {remote_schema()}.interests {_where_sql(where)} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return [_normalize_interest_row(row) for row in rows]


def get_interest_remote(interest_id: int, *, user_id: str | None = None) -> dict[str, Any] | None:
    where = ["id = %(interest_id)s"]
    params = {"interest_id": interest_id}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.interests {_where_sql(where)}",
            params,
        ).fetchone()
    return _normalize_interest_row(row) if row else None


def update_interest_remote(
    interest_id: int,
    *,
    owner_user_id: str | None = None,
    **fields: Any,
) -> bool:
    allowed = {
        "name", "description", "keywords", "sort", "item_limit", "scope",
        "enabled", "scan_status", "last_scan_at", "suggestion",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    sets = []
    params: dict[str, Any] = {"interest_id": interest_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        if key == "keywords":
            params[pname] = _maybe_jsonb(value)
        elif key == "suggestion" and isinstance(value, (dict, list)):
            params[pname] = json.dumps(value, ensure_ascii=False)
        else:
            params[pname] = value
    where = "id = %(interest_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE {remote_schema()}.interests SET {', '.join(sets)} WHERE {where}",
            params,
        )
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def delete_interest_remote(interest_id: int, *, owner_user_id: str | None = None) -> bool:
    if owner_user_id and not get_interest_remote(interest_id, user_id=owner_user_id):
        return False
    params = {"interest_id": interest_id}
    where = "id = %(interest_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        conn.execute(
            f"DELETE FROM {remote_schema()}.interest_matches WHERE interest_id = %(interest_id)s",
            {"interest_id": interest_id},
        )
        cur = conn.execute(f"DELETE FROM {remote_schema()}.interests WHERE {where}", params)
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def get_interest_match_stats_remote(interest_id: int) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                      SUM(CASE WHEN is_new = 1 THEN 1 ELSE 0 END) AS new_count
                 FROM {remote_schema()}.interest_matches
                WHERE interest_id = %s""",
            (interest_id,),
        ).fetchone()
    return {"total": row["total"] or 0, "new_count": row["new_count"] or 0}


def get_interest_matches_remote(
    interest_id: int,
    sort: str = "relevance",
    limit: int = 30,
    offset: int = 0,
) -> list[dict[str, Any]]:
    order = "m.relevance_score DESC" if sort == "relevance" else "i.fetched_at DESC"
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT m.interest_id, m.item_id, m.relevance_score, m.is_new, m.matched_at,
                      i.platform, i.source, i.title, i.content, i.author_name, i.url,
                      i.cover_url, i.ai_summary, i.ai_key_points, i.ai_category,
                      i.relevance_score AS item_score, i.fetched_at, i.published_at,
                      NULL::timestamptz AS clicked_at, NULL::timestamptz AS starred_at
                 FROM {schema}.interest_matches m
                 JOIN {schema}.items i ON m.item_id = i.id
                WHERE m.interest_id = %s
                ORDER BY {order}
                LIMIT %s OFFSET %s""",
            (interest_id, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_interest_matches_read_remote(interest_id: int) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.interest_matches SET is_new = 0 WHERE interest_id = %s",
            (interest_id,),
        )
        conn.commit()


def upsert_interest_matches_remote(interest_id: int, matches: list[dict[str, Any]]) -> None:
    with connect() as conn:
        for match in matches:
            conn.execute(
                f"""INSERT INTO {remote_schema()}.interest_matches
                      (interest_id, item_id, relevance_score, is_new, matched_at)
                    VALUES (%s, %s, %s, 1, now())
                    ON CONFLICT (interest_id, item_id) DO UPDATE SET
                      relevance_score = excluded.relevance_score,
                      matched_at = excluded.matched_at""",
                (interest_id, match["item_id"], match["relevance_score"]),
            )
        conn.commit()


def fetch_items_for_interest_scan_remote(
    *,
    scope: str = "all",
    since: Any = None,
) -> list[dict[str, Any]]:
    where = ["ai_summary IS NOT NULL", "ai_summary != ''"]
    params: dict[str, Any] = {}
    if since:
        where.append("fetched_at > %(since)s")
        params["since"] = since
    elif scope == "7d":
        where.append("fetched_at > now() - interval '7 days'")
    elif scope == "30d":
        where.append("fetched_at > now() - interval '30 days'")
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, ai_summary, ai_key_points
                  FROM {remote_schema()}.items
                 {_where_sql(where)}
                 ORDER BY fetched_at DESC""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_interest_top_items_remote(interest_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.id, i.title, i.ai_summary
                  FROM {schema}.interest_matches m
                  JOIN {schema}.items i ON i.id = m.item_id
                 WHERE m.interest_id = %s
                 ORDER BY m.relevance_score DESC
                 LIMIT %s""",
            (interest_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _normalize_user_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    for col in (
        "created_at",
        "last_login_at",
        "verification_code_expires",
        "reset_token_expires",
    ):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def create_user_remote(user_id: str, username: str, email: str, password_hash: str, role: str = "user") -> dict[str, Any] | None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.users
                  (id, username, email, password_hash, role)
                VALUES (%s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return get_user_remote(user_id)


def create_user_with_invite_remote(
    user_id: str,
    username: str,
    email: str,
    password_hash: str,
    invite_code: str,
    verification_code: str,
    verification_code_expires: Any,
    role: str = "user",
) -> bool:
    """Create a user and consume an invite in one transaction."""
    schema = remote_schema()
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {schema}.users
                  (id, username, email, password_hash, role)
                VALUES (%s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role),
        )
        cur = conn.execute(
            f"""UPDATE {schema}.invite_codes
                   SET used_count = used_count + 1,
                       used_by = %s
                 WHERE code = %s
                   AND used_count < max_uses
                   AND (expires_at IS NULL OR expires_at > now())""",
            (user_id, invite_code),
        )
        if (getattr(cur, "rowcount", 0) or 0) <= 0:
            conn.rollback()
            return False
        conn.execute(
            f"""UPDATE {schema}.users
                   SET verification_code = %s,
                       verification_code_expires = %s
                 WHERE id = %s""",
            (verification_code, verification_code_expires, user_id),
        )
    clear_user_cache_keys(user_id)
    return True


def create_user_open_remote(
    user_id: str,
    username: str,
    email: str,
    password_hash: str,
    verification_code: str,
    verification_code_expires: Any,
    role: str = "user",
) -> bool:
    """P1-4 开放注册:创建用户并写入验证码,不消耗邀请码。"""
    schema = remote_schema()
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {schema}.users
                  (id, username, email, password_hash, role,
                   verification_code, verification_code_expires)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role,
             verification_code, verification_code_expires),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return True


def get_user_remote(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE id = %s",
            (user_id,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_login_remote(login: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE email = %s OR username = %s",
            (login, login),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_username_remote(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE username = %s",
            (username,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_email_remote(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE email = %s",
            (email,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_reset_token_remote(token: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, username, reset_token_expires
                  FROM {remote_schema()}.users
                 WHERE reset_token = %s""",
            (token,),
        ).fetchone()
    return _normalize_user_row(row)


def update_user_remote(user_id: str, **fields: Any) -> None:
    allowed = {
        "username", "email", "password_hash", "role", "discord_bot_token_enc",
        "last_login_at", "email_verified", "verification_code",
        "verification_code_expires", "reset_token", "reset_token_expires",
        "discord_channel_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = []
    params: dict[str, Any] = {"user_id": user_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        params[pname] = value
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.users SET {', '.join(sets)} WHERE id = %(user_id)s",
            params,
        )
        conn.commit()
    clear_user_cache_keys(user_id)


def list_users_remote(*, pg_conn: Any | None = None) -> list[dict[str, Any]]:
    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        rows = conn.execute(
            f"""SELECT id, username, email, role, created_at, last_login_at
                  FROM {remote_schema()}.users
                 ORDER BY created_at"""
        ).fetchall()
        return [_normalize_user_row(row) for row in rows]
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def get_invite_code_remote(code: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.invite_codes WHERE code = %s",
            (code,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["expires_at"] = _timestamp_value(data.get("expires_at"))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def use_invite_code_remote(code: str, user_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            f"""UPDATE {remote_schema()}.invite_codes
                   SET used_count = used_count + 1,
                       used_by = %s
                 WHERE code = %s
                   AND used_count < max_uses
                   AND (expires_at IS NULL OR expires_at > now())""",
            (user_id, code),
        )
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def create_invite_code_remote(code: str, created_by: str | None, max_uses: int = 1, expires_at: Any = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.invite_codes
                  (code, created_by, max_uses, expires_at)
                VALUES (%s, %s, %s, %s)""",
            (code, created_by, max_uses, expires_at),
        )
        conn.commit()


def list_invite_codes_remote(*, pg_conn: Any | None = None) -> list[dict[str, Any]]:
    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        rows = conn.execute(
            f"SELECT * FROM {remote_schema()}.invite_codes ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["expires_at"] = _timestamp_value(data.get("expires_at"))
            data["created_at"] = _timestamp_value(data.get("created_at"))
            out.append(data)
        return out
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def delete_invite_code_remote(code: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.invite_codes WHERE code = %s", (code,))
        conn.commit()


def create_session_remote(session_id: str, user_id: str, token_type: str, expires_at: Any) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.sessions
                  (id, user_id, token_type, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  user_id = excluded.user_id,
                  token_type = excluded.token_type,
                  expires_at = excluded.expires_at""",
            (session_id, user_id, token_type, expires_at),
        )
        conn.commit()
    clear_user_cache_keys(user_id)


def create_sessions_remote(sessions: list[tuple[str, str, str, Any]]) -> None:
    if not sessions:
        return
    schema = remote_schema()
    sql = f"""INSERT INTO {schema}.sessions
                (id, user_id, token_type, expires_at)
              VALUES (%s, %s, %s, %s)
              ON CONFLICT (id) DO UPDATE SET
                user_id = excluded.user_id,
                token_type = excluded.token_type,
                expires_at = excluded.expires_at"""
    with connect() as conn:
        _executemany(conn, sql, sessions)
        conn.commit()
    # Each session tuple is (id, user_id, token_type, expires_at) — clear per-user.
    for sess in sessions:
        if len(sess) >= 2 and sess[1]:
            clear_user_cache_keys(sess[1])


def get_session_remote(session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["expires_at"] = _timestamp_value(data.get("expires_at"))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def get_user_for_session_remote(session_id: str, user_id: str) -> dict[str, Any] | None:
    """Return a user only when the JWT session is still present and valid."""
    schema = remote_schema()
    cache_key = ("auth_session_user", schema, session_id, user_id)
    cached = _cache_get_with_ttl(cache_key, _auth_cache_ttl())
    if cached is not None:
        return cached
    with connect() as conn:
        row = conn.execute(
            f"""SELECT u.*, p.onboarding_completed AS profile_onboarding_completed
                  FROM {schema}.sessions s
                  JOIN {schema}.users u ON u.id = s.user_id
             LEFT JOIN {schema}.user_profiles p ON p.user_id = u.id
                 WHERE s.id = %s
                   AND s.user_id = %s
                   AND s.expires_at >= now()
                 LIMIT 1""",
            (session_id, user_id),
        ).fetchone()
    user = _normalize_user_row(row)
    if user and "profile_onboarding_completed" in user:
        user["_onboarding_completed"] = True if user["profile_onboarding_completed"] is None else bool(user["profile_onboarding_completed"])
    return _cache_set_with_ttl(cache_key, user, _auth_cache_ttl())


def finish_login_remote(
    user_id: str,
    *,
    access_jti: str,
    access_expires_at: Any,
    refresh_jti: str,
    refresh_expires_at: Any,
    last_login_at: Any,
) -> dict[str, Any] | None:
    """Persist login side effects in one remote round-trip and return profile."""
    schema = remote_schema()
    with connect() as conn:
        profile = conn.execute(
            f"""
            WITH upd_user AS (
                UPDATE {schema}.users
                   SET last_login_at = %(last_login_at)s
                 WHERE id = %(user_id)s
             RETURNING id
            ),
            upsert_sessions AS (
                INSERT INTO {schema}.sessions (id, user_id, token_type, expires_at)
                VALUES
                    (%(access_jti)s,  %(user_id)s, 'access',  %(access_expires_at)s),
                    (%(refresh_jti)s, %(user_id)s, 'refresh', %(refresh_expires_at)s)
                ON CONFLICT (id) DO UPDATE SET
                    user_id = excluded.user_id,
                    token_type = excluded.token_type,
                    expires_at = excluded.expires_at
                RETURNING id
            )
            SELECT p.*
              FROM {schema}.user_profiles p
             WHERE p.user_id = %(user_id)s
            """,
            {
                "user_id": user_id,
                "last_login_at": last_login_at,
                "access_jti": access_jti,
                "access_expires_at": access_expires_at,
                "refresh_jti": refresh_jti,
                "refresh_expires_at": refresh_expires_at,
            },
        ).fetchone()
        conn.commit()
    clear_user_cache_keys(user_id)
    if not profile:
        return None
    data = dict(profile)
    for field in ("interests", "tools", "manifest"):
        data[field] = _json_value(data.get(field))
    return data


def refresh_access_session_remote(
    *,
    refresh_jti: str,
    user_id: str,
    access_jti: str,
    access_expires_at: Any,
) -> dict[str, Any] | None:
    """Validate refresh session and insert a new access session in one trip."""
    schema = remote_schema()
    with connect() as conn:
        user = conn.execute(
            f"""SELECT u.*
                  FROM {schema}.sessions s
                  JOIN {schema}.users u ON u.id = s.user_id
                 WHERE s.id = %s
                   AND s.user_id = %s
                   AND s.token_type = 'refresh'
                   AND s.expires_at >= now()
                 LIMIT 1""",
            (refresh_jti, user_id),
        ).fetchone()
        if not user:
            return None
        conn.execute(
            f"""INSERT INTO {schema}.sessions
                  (id, user_id, token_type, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  user_id = excluded.user_id,
                  token_type = excluded.token_type,
                  expires_at = excluded.expires_at""",
            (access_jti, user_id, "access", access_expires_at),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return _normalize_user_row(user)


def delete_user_sessions_remote(user_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE user_id = %s", (user_id,))
        conn.commit()
    clear_user_cache_keys(user_id)


def delete_session_remote(session_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE id = %s", (session_id,))
        conn.commit()
    # Don't know user_id from session_id alone. Auth cache TTL=10s catches stale.


def cleanup_expired_sessions_remote() -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE expires_at < now()")
        conn.commit()
    # Bulk cleanup; user_ids unknown. Auth cache TTL=10s catches stale.


def get_user_profile_remote(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.user_profiles WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    for field in ("interests", "tools", "manifest"):
        data[field] = _json_value(data.get(field))
    return data


_PROFILE_SENTINEL = object()


def upsert_user_profile_remote(
    user_id: str,
    *,
    role: Any = _PROFILE_SENTINEL,
    interests: Any = _PROFILE_SENTINEL,
    tools: Any = _PROFILE_SENTINEL,
    manifest: Any = _PROFILE_SENTINEL,
    onboarding_completed: Any = _PROFILE_SENTINEL,
) -> dict[str, Any] | None:
    fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if role is not _PROFILE_SENTINEL:
        fields["role"] = role
    if interests is not _PROFILE_SENTINEL:
        fields["interests"] = _maybe_jsonb(interests)
    if tools is not _PROFILE_SENTINEL:
        fields["tools"] = _maybe_jsonb(tools)
    if manifest is not _PROFILE_SENTINEL:
        fields["manifest"] = _maybe_jsonb(manifest)
    if onboarding_completed is not _PROFILE_SENTINEL:
        fields["onboarding_completed"] = 1 if onboarding_completed else 0

    columns = ["user_id", *fields.keys()]
    values = [user_id, *fields.values()]
    placeholders = ", ".join(["%s"] * len(values))
    updates = ", ".join(f"{col} = excluded.{col}" for col in fields)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.user_profiles ({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (user_id) DO UPDATE SET {updates}""",
            tuple(values),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return get_user_profile_remote(user_id)


def _normalize_briefing_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    for col in ("insights", "suggestions"):
        data[col] = _json_value(data.get(col))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def upsert_briefing_remote(
    briefing_id: str,
    date: str,
    insights: list[dict[str, Any]] | None,
    suggestions: list[dict[str, Any]] | None,
    input_count: int,
    model: str,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.briefings
                  (id, date, insights, suggestions, input_count, model, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                  date = excluded.date,
                  insights = excluded.insights,
                  suggestions = excluded.suggestions,
                  input_count = excluded.input_count,
                  model = excluded.model,
                  created_at = excluded.created_at""",
            (
                briefing_id,
                date,
                json.dumps(insights or [], ensure_ascii=False),
                json.dumps(suggestions or [], ensure_ascii=False),
                input_count,
                model,
            ),
        )
        conn.commit()


def get_briefing_remote(date: str | None = None) -> dict[str, Any] | None:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            f"""SELECT *
                  FROM {remote_schema()}.briefings
                 WHERE date = %s
                 ORDER BY created_at DESC
                 LIMIT 1""",
            (date,),
        ).fetchone()
    return _normalize_briefing_row(row)


def list_briefing_dates_remote(limit: int = 30) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT date
                  FROM {remote_schema()}.briefings
                 ORDER BY date DESC
                 LIMIT %s""",
            (limit,),
        ).fetchall()
    return [row["date"] for row in rows]


def fetch_high_score_items_remote(min_score: int = 6, hours: int = 24) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, platform, source, title, ai_summary, ai_category, ai_keywords,
                      relevance_score, author_name, url, fetched_at
                 FROM {remote_schema()}.items
                WHERE fetched_at > now() - (%s::int * interval '1 hour')
                  AND relevance_score >= %s
                ORDER BY relevance_score DESC""",
            (int(hours), min_score),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["ai_keywords"] = _json_value(item.get("ai_keywords"))
        item["fetched_at"] = _timestamp_value(item.get("fetched_at"))
        out.append(item)
    return out


def get_all_interest_keywords_remote() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT keywords FROM {remote_schema()}.interests WHERE enabled = 1"
        ).fetchall()
    out: list[str] = []
    seen = set()
    for row in rows:
        keywords = _json_value(row.get("keywords")) or []
        if not isinstance(keywords, list):
            continue
        for kw in keywords:
            text = str(kw or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def add_feedback_remote(item_id: str, fb_type: str, topic: str | None = None, text: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.feedback (item_id, type, topic, text)
                VALUES (%s, %s, %s, %s)""",
            (item_id, fb_type, topic, text),
        )
        conn.commit()


def record_item_feedback_remote(
    *,
    item_id: str,
    action: str,
    platform: str | None = None,
    title: str | None = None,
    author: str | None = None,
    url: str | None = None,
    reason: str | None = None,
    topic: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.item_feedback
                  (item_id, platform, item_title, item_author, item_url,
                   action, reason, topic_at_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (item_id, platform, title, author, url, action, reason, topic),
        )
        conn.commit()


def get_feedback_item_context_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, title, author_name, url, ai_summary
                  FROM {remote_schema()}.items
                 WHERE id = %s""",
            (item_id,),
        ).fetchone()
    return dict(row) if row else None


def get_feedback_scores_remote() -> dict[str, Any]:
    schema = remote_schema()
    with connect() as conn:
        author_rows = conn.execute(
            f"""SELECT i.author_name, f.type, COUNT(*) AS cnt
                  FROM {schema}.feedback f
                  JOIN {schema}.items i ON f.item_id = i.id
                 WHERE f.type IN ('positive', 'low_quality')
                   AND COALESCE(i.author_name, '') != ''
                 GROUP BY i.author_name, f.type"""
        ).fetchall()
        item_rows = conn.execute(
            f"SELECT item_id, type FROM {schema}.feedback"
        ).fetchall()
        text_rows = conn.execute(
            f"""SELECT item_id, text, created_at
                  FROM {schema}.feedback
                 WHERE type = 'text'
                   AND text IS NOT NULL
                   AND text != ''
                 ORDER BY created_at DESC"""
        ).fetchall()

    author_scores: dict[str, int] = {}
    for row in author_rows:
        name = row["author_name"]
        author_scores.setdefault(name, 0)
        author_scores[name] += int(row["cnt"] or 0) if row["type"] == "positive" else -int(row["cnt"] or 0)
    item_feedback: dict[str, list[str]] = {}
    for row in item_rows:
        item_feedback.setdefault(row["item_id"], []).append(row["type"])
    text_feedback: dict[str, list[dict[str, Any]]] = {}
    for row in text_rows:
        text_feedback.setdefault(row["item_id"], []).append({
            "text": row["text"],
            "created_at": _timestamp_value(row.get("created_at")),
        })
    return {
        "author_scores": author_scores,
        "item_feedback": item_feedback,
        "text_feedback": text_feedback,
    }


def lingowhale_group_counts_remote() -> dict[str, int]:
    cache_key = ("lingowhale_group_counts", remote_schema())
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(detail_json ->> 'group', '') AS group_name,
                      COUNT(*) AS cnt
                 FROM {remote_schema()}.items
                WHERE platform = 'lingowhale'
                GROUP BY COALESCE(detail_json ->> 'group', '')"""
        ).fetchall()
    counts = {row["group_name"]: int(row["cnt"] or 0) for row in rows}
    return _cache_set_copy(cache_key, counts)


LINGOWHALE_GROUPS_SETTING_KEY = "lingowhale_groups"


def get_lingowhale_groups_metadata_remote() -> list[dict[str, Any]] | None:
    value = get_setting_remote(LINGOWHALE_GROUPS_SETTING_KEY)
    if isinstance(value, list):
        return value
    return None


def set_lingowhale_groups_metadata_remote(groups: list[dict[str, Any]]) -> None:
    set_setting_remote(LINGOWHALE_GROUPS_SETTING_KEY, groups)
    _cache_delete(("setting", LINGOWHALE_GROUPS_SETTING_KEY))


def get_setting_remote(key: str) -> Any:
    cache_key = ("setting", key)
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    with connect() as conn:
        row = conn.execute(
            f"SELECT value FROM {remote_schema()}.settings WHERE key = %s",
            (key,),
        ).fetchone()
    value = _json_value(row["value"]) if row else None
    return _cache_set_copy(cache_key, value)


def set_setting_remote(key: str, value: Any) -> None:
    value_text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.settings (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at""",
            (key, value_text),
        )
        conn.commit()
    _cache_delete(("setting", key))


def get_submit_existing_item_remote(item_id: str, url: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, title, content, ai_summary, url
                  FROM {remote_schema()}.items
                 WHERE id = %s OR url = %s
                 ORDER BY CASE WHEN id = %s THEN 0 ELSE 1 END
                 LIMIT 1""",
            (item_id, url, item_id),
        ).fetchone()
    return dict(row) if row else None


def _asr_today_cst() -> str:
    """Return the Beijing-date bucket used by ASR quota accounting."""
    cst = datetime.now(timezone.utc) + timedelta(hours=8)
    return cst.strftime("%Y-%m-%d")


def _asr_daily_quota_sec() -> int:
    hours = float(_runtime_env().get("ASR_DAILY_QUOTA_HOURS") or ASR_DAILY_QUOTA_HOURS_DEFAULT)
    return int(hours * 3600)


def _asr_usage_snapshot(date_cst: str, seconds_used: int) -> dict[str, Any]:
    daily_sec = _asr_daily_quota_sec()
    try:
        next_day = (datetime.strptime(date_cst, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    return {
        "date_cst": date_cst,
        "seconds_used": int(seconds_used or 0),
        "used_hours": round(int(seconds_used or 0) / 3600, 1),
        "daily_quota_sec": daily_sec,
        "remaining_hours": round((daily_sec - int(seconds_used or 0)) / 3600, 1),
        "over_limit": int(seconds_used or 0) >= daily_sec,
        "reset_at": reset_at,
    }


def get_asr_usage_today_remote(pg_conn: Any | None = None, user_id: str | int = "0") -> dict[str, Any]:
    """Return today's ASR quota usage from Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return get_asr_usage_today_remote(conn, user_id=user_id)
    today = _asr_today_cst()
    row = pg_conn.execute(
        f"""SELECT seconds_used
              FROM {remote_schema()}.asr_usage
             WHERE user_id = %s AND date_cst = %s""",
        (str(user_id), today),
    ).fetchone()
    used = int(_row_get(row, "seconds_used", 0) or 0) if row else 0
    return _asr_usage_snapshot(today, used)


def check_asr_quota_remote(
    pg_conn: Any | None,
    duration_sec: int,
    *,
    user_id: str | int = "0",
) -> tuple[bool, dict[str, Any]]:
    """Check ASR quota against the remote usage table."""
    usage = get_asr_usage_today_remote(pg_conn, user_id=user_id)
    allowed = usage["seconds_used"] + max(0, int(duration_sec or 0)) <= usage["daily_quota_sec"]
    return allowed, usage


def consume_asr_quota_remote(
    pg_conn: Any | None,
    duration_sec: int,
    *,
    user_id: str | int = "0",
) -> dict[str, Any]:
    """Increment ASR quota usage in Supabase and return the updated snapshot."""
    if duration_sec is None or duration_sec <= 0:
        return get_asr_usage_today_remote(pg_conn, user_id=user_id)
    if pg_conn is None:
        with connect() as conn:
            return consume_asr_quota_remote(conn, duration_sec, user_id=user_id)
    today = _asr_today_cst()
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.asr_usage
              (user_id, date_cst, seconds_used, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, date_cst) DO UPDATE SET
              seconds_used = {remote_schema()}.asr_usage.seconds_used + excluded.seconds_used,
              updated_at = excluded.updated_at""",
        (str(user_id), today, int(duration_sec)),
    )
    _commit_if_supported(pg_conn)
    return get_asr_usage_today_remote(pg_conn, user_id=user_id)


# ── v21.0 action-revival: 行动点生成每日配额(remote 镜像 db.* 同名函数)──

def _generation_usage_snapshot(day_cst: str, used: int, limit: int) -> dict[str, Any]:
    from datetime import datetime, timedelta
    try:
        next_day = (datetime.strptime(day_cst, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    used = max(0, int(used))
    return {
        'day_cst': day_cst,
        'used': used,
        'limit': int(limit),
        'remaining': max(0, int(limit) - used),
        'over_limit': used >= int(limit),
        'reset_at': reset_at,
    }


def get_generation_usage_today_remote(
    pg_conn: Any | None = None,
    *,
    user_id: str,
    limit: int,
) -> dict[str, Any]:
    """Return today's generation quota snapshot from Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return get_generation_usage_today_remote(conn, user_id=user_id, limit=limit)
    today = _asr_today_cst()
    row = pg_conn.execute(
        f"""SELECT count
              FROM {remote_schema()}.user_daily_generation
             WHERE user_id = %s AND day_cst = %s""",
        (str(user_id), today),
    ).fetchone()
    used = int(_row_get(row, "count", 0) or 0) if row else 0
    return _generation_usage_snapshot(today, used, limit)


def try_consume_generation_quota_remote(
    pg_conn: Any | None,
    *,
    user_id: str,
    limit: int,
) -> tuple[bool, dict[str, Any]]:
    """Atomically consume one generation credit if under `limit`.

    Uses an upsert whose DO UPDATE is guarded by a WHERE on the existing count,
    so a row at the limit yields no RETURNING row (not consumed). New rows insert
    at count=1. Returns (allowed, snapshot).
    """
    if pg_conn is None:
        with connect() as conn:
            return try_consume_generation_quota_remote(conn, user_id=user_id, limit=limit)
    today = _asr_today_cst()
    schema = remote_schema()
    row = pg_conn.execute(
        f"""INSERT INTO {schema}.user_daily_generation
              (user_id, day_cst, count, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (user_id, day_cst) DO UPDATE SET
              count = {schema}.user_daily_generation.count + 1,
              updated_at = now()
            WHERE {schema}.user_daily_generation.count < %s
            RETURNING count""",
        (str(user_id), today, int(limit)),
    ).fetchone()
    _commit_if_supported(pg_conn)
    if row is not None:
        return True, _generation_usage_snapshot(today, int(_row_get(row, "count", 1) or 1), limit)
    return False, get_generation_usage_today_remote(pg_conn, user_id=user_id, limit=limit)


def get_item_asr_state_remote(item_id: str) -> dict[str, Any] | None:
    """Fetch the ASR status payload used by `/api/items/{id}/asr`."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, asr_text, asr_status, asr_duration_sec,
                      asr_cost_yuan, asr_attempted_at, asr_failed_reason, asr_provider,
                      ai_summary, asr_segments, asr_text_cn, asr_segments_cn
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    for col in ASR_JSON_COLUMNS:
        data[col] = _json_value(data.get(col))
    data["asr_attempted_at"] = _timestamp_value(data.get("asr_attempted_at"))
    return data


def get_item_media_json_remote(item_id: str) -> Any:
    """Return `items.media_json` from Supabase for media/ASR helpers."""
    with connect() as conn:
        row = conn.execute(
            f"SELECT media_json FROM {remote_schema()}.items WHERE id = %s",
            (item_id,),
        ).fetchone()
    return _json_value(_row_get(row, "media_json")) if row else None


def get_media_item_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, media_json
                  FROM {remote_schema()}.items
                 WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["media_json"] = _json_value(data.get("media_json"))
    return data


def get_twitter_mp4_url_remote(item_id: str) -> str | None:
    item = get_media_item_remote(item_id)
    if not item or item.get("platform") != "twitter":
        return None
    media = item.get("media_json") or []
    if isinstance(media, str):
        media = _json_value(media)
    if not isinstance(media, list):
        return None
    for entry in media:
        if isinstance(entry, dict) and entry.get("type") == "video" and entry.get("url"):
            return entry["url"]
    return None


def get_asr_worker_item_remote(item_id: str) -> dict[str, Any] | None:
    """Fetch the item fields needed by the ASR worker."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, title, content, ai_summary, media_json, url,
                      asr_text, asr_duration_sec
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["media_json"] = _json_value(data.get("media_json"))
    return data


def update_item_asr_fields_remote(item_id: str, **fields: Any) -> None:
    """Update ASR-related item fields in Supabase."""
    updates = {key: value for key, value in fields.items() if key in ASR_ITEM_UPDATE_COLUMNS}
    if not updates:
        return
    sets = []
    params: dict[str, Any] = {"item_id": item_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        if key in ASR_JSON_COLUMNS:
            params[pname] = _maybe_jsonb(value)
        elif key in ASR_TIMESTAMP_COLUMNS:
            params[pname] = _timestamp_value(value)
        else:
            params[pname] = value
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.items SET {', '.join(sets)} WHERE id = %(item_id)s",
            params,
        )
        conn.commit()
    clear_item_detail_cache_keys(item_id)
    if "ai_summary" in updates:
        clear_feed_cache_keys()


def _storage_object_url(object_path: str) -> str:
    from urllib.parse import quote

    clean_path = object_path.strip().lstrip("/")
    if not clean_path or ".." in clean_path.split("/"):
        raise RemoteDBConfigError(f"Invalid storage object path: {object_path!r}")
    bucket = quote(supabase_storage_bucket(), safe="")
    encoded_path = "/".join(quote(part, safe="") for part in clean_path.split("/"))
    return f"{supabase_project_url()}/storage/v1/object/{bucket}/{encoded_path}"


def _storage_headers(content_type: str | None = None, *, upsert: bool = False) -> dict[str, str]:
    key = supabase_service_role_key()
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if content_type:
        headers["Content-Type"] = content_type
    if upsert:
        headers["x-upsert"] = "true"
    return headers


def upload_asset_bytes_remote(
    object_path: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    upsert: bool = True,
    source_item_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Upload binary data to Supabase Storage and record lightweight metadata."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        data=data,
        method="POST" if upsert else "PUT",
        headers=_storage_headers(content_type, upsert=upsert),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RemoteDBError(f"Supabase Storage upload failed: HTTP {exc.code} {body}") from exc
    upsert_asset_metadata_remote(
        object_path=object_path,
        content_type=content_type,
        size_bytes=len(data),
        source_item_id=source_item_id,
        kind=kind,
    )
    return {
        "bucket": supabase_storage_bucket(),
        "object_path": object_path,
        "content_type": content_type,
        "size_bytes": len(data),
    }


def download_asset_bytes_remote(object_path: str) -> bytes | None:
    """Download binary data from Supabase Storage. Return None when missing."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        method="GET",
        headers=_storage_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        missing = (
            exc.code == 404
            or "not_found" in body.lower()
            or "object not found" in body.lower()
        )
        if missing:
            return None
        raise RemoteDBError(f"Supabase Storage download failed: HTTP {exc.code} {body}") from exc


def delete_asset_remote(object_path: str) -> None:
    """Delete a remote binary asset and its metadata."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        method="DELETE",
        headers=_storage_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code != 404 and "not_found" not in body.lower() and "object not found" not in body.lower():
            raise RemoteDBError(f"Supabase Storage delete failed: HTTP {exc.code} {body}") from exc
    with connect() as conn:
        conn.execute(
            f"DELETE FROM {remote_schema()}.remote_assets WHERE object_path = %s",
            (object_path,),
        )
        conn.commit()


def upsert_asset_metadata_remote(
    *,
    object_path: str,
    content_type: str | None = None,
    size_bytes: int | None = None,
    source_item_id: str | None = None,
    kind: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.remote_assets
                  (object_path, bucket, content_type, size_bytes, source_item_id, kind, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (object_path) DO UPDATE SET
                  bucket = excluded.bucket,
                  content_type = excluded.content_type,
                  size_bytes = excluded.size_bytes,
                  source_item_id = COALESCE(excluded.source_item_id, {remote_schema()}.remote_assets.source_item_id),
                  kind = COALESCE(excluded.kind, {remote_schema()}.remote_assets.kind),
                  updated_at = excluded.updated_at""",
            (
                object_path,
                supabase_storage_bucket(),
                content_type,
                size_bytes,
                source_item_id,
                kind,
            ),
        )
        conn.commit()


@contextmanager
def connect() -> Iterator[Any]:
    """Yield a psycopg connection with dict rows, imported lazily."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RemoteDBConfigError(
            "psycopg is not installed; run `pip install -r requirements.txt` "
            "or use `uv run --with 'psycopg[binary]>=3.2' ...`."
        ) from exc

    pool = _get_pool(psycopg, dict_row)
    if pool is not None:
        try:
            with pool.connection() as conn:
                if _force_writable_on_connect():
                    conn.execute("set default_transaction_read_only=off")
                    conn.commit()
                # pgvector lives in `extensions` schema on Supabase; the `<=>`
                # operator is resolved via search_path at parse time. Transaction
                # pooler recycles backends with default search_path, so we set
                # it on every checkout to guarantee cluster pipeline SQL works.
                conn.execute(f"SET search_path TO {remote_schema()}, extensions, public")
                conn.commit()
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    _rollback_safely(conn)
                    raise
                return
        except RemoteDBError:
            raise
        except Exception as exc:
            raise RemoteDBError(f"Remote DB connection/query failed: {exc}") from exc

    env = _runtime_env()
    connect_timeout = _env_int(env, REMOTE_DB_CONNECT_TIMEOUT_ENV, 2, min_value=1)
    connect_attempts = _env_int(env, REMOTE_DB_CONNECT_ATTEMPTS_ENV, 1, min_value=1)
    conn = None
    last_exc: Exception | None = None
    for attempt in range(1, connect_attempts + 1):
        try:
            conn = psycopg.connect(
                database_url(),
                row_factory=dict_row,
                connect_timeout=connect_timeout,
                prepare_threshold=None,
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= connect_attempts:
                break
            time.sleep(min(5, attempt * 1.5))
    if conn is None:
        raise RemoteDBError(f"Remote DB connection failed: {last_exc}") from last_exc

    try:
        if _force_writable_on_connect():
            conn.execute("set default_transaction_read_only=off")
            conn.commit()
        conn.execute(f"SET search_path TO {remote_schema()}, extensions, public")
        conn.commit()
        with conn:
            yield conn
    except RemoteDBError:
        raise
    except Exception as exc:
        raise RemoteDBError(f"Remote DB connection/query failed: {exc}") from exc
    finally:
        conn.close()


def _json_array(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _media_urls_from_item(cover_url: Any, media_json: Any) -> list[str]:
    urls: list[str] = []

    def add_url(value: Any):
        if not value:
            return
        url = str(value).strip()
        if url and url not in urls:
            urls.append(url)

    add_url(cover_url)
    media = _json_value(media_json)
    if not isinstance(media, list):
        return urls

    for entry in media:
        if isinstance(entry, str):
            add_url(entry)
            continue
        if not isinstance(entry, dict):
            continue
        media_type = str(entry.get("type") or "").lower()
        if media_type in ("video", "animated_gif"):
            continue
        add_url(entry.get("url") or entry.get("preview_image_url") or entry.get("src"))
    return urls


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _timestamp_value(value: Any) -> str | None:
    return to_utc_iso(value) if value else None


def _feed_cols(
    status_alias: str | None = None,
    *,
    include_content: bool = False,
    include_heavy_json: bool = True,
) -> str:
    status_cols = (
        f"""{status_alias}.read_at, {status_alias}.clicked_at,
            {status_alias}.starred_at, {status_alias}.hidden_at"""
        if status_alias
        else """NULL::timestamptz AS read_at, NULL::timestamptz AS clicked_at,
                NULL::timestamptz AS starred_at, NULL::timestamptz AS hidden_at"""
    )
    content_col = "i.content," if include_content else ""
    # List/card endpoints do not render these detail-style JSON/text blobs.
    # Returning NULL aliases keeps the row shape stable while avoiding TOASTing
    # large fields such as ai_key_points for every card.
    tags_col = "i.tags_json" if include_heavy_json else "NULL::jsonb AS tags_json"
    heavy_json_cols = (
        "i.detail_json, i.comments_json,"
        if include_heavy_json
        else "NULL::jsonb AS detail_json, NULL::jsonb AS comments_json,"
    )
    ai_key_points_col = "i.ai_key_points" if include_heavy_json else "NULL::text AS ai_key_points"
    ai_keywords_col = "i.ai_keywords" if include_heavy_json else "NULL::text AS ai_keywords"
    ai_subcategories_col = "i.ai_subcategories" if include_heavy_json else "NULL::jsonb AS ai_subcategories"
    multi_l1_reason_col = "i.multi_l1_reason" if include_heavy_json else "NULL::text AS multi_l1_reason"
    ai_extracted_col = "i.ai_extracted" if include_heavy_json else "NULL::jsonb AS ai_extracted"
    asr_cols = (
        """i.asr_text, i.asr_status, i.asr_duration_sec, i.asr_cost_yuan,
           i.asr_attempted_at, i.asr_failed_reason, i.asr_provider,
           i.asr_segments, i.asr_text_cn, i.asr_segments_cn,"""
        if include_content
        else ""
    )
    return f"""
      i.id, i.user_id, i.platform, i.source, i.title, i.author_name, i.author_id,
      {content_col}
      i.author_avatar, i.url, i.cover_url, i.media_json, i.metrics_json,
      {tags_col}, i.lang, {heavy_json_cols} i.description,
      {asr_cols}
      i.ai_summary, {ai_key_points_col}, i.ai_category, {ai_keywords_col},
      i.ai_categories, {ai_subcategories_col}, {multi_l1_reason_col}, {ai_extracted_col},
      i.content_type, i.visible, i.relevance_score, i.fetched_at, i.published_at,
      i.created_at, {status_cols}
    """


def _normalize_item(raw: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    item = dict(raw)
    item.pop("embedding", None)
    item.pop("rn", None)
    item.pop("section_category", None)
    category = canonicalize_category(item.get("ai_category"))
    if category != item.get("ai_category"):
        item["ai_category"] = category
    for col in (
        "media_json",
        "metrics_json",
        "tags_json",
        "detail_json",
        "comments_json",
        "ai_categories",
        "ai_subcategories",
        "ai_extracted",
        "ai_key_points",
        "asr_segments",
        "asr_segments_cn",
    ):
        if col in item:
            item[col] = _json_value(item.get(col))
    for col in (
        "fetched_at",
        "published_at",
        "created_at",
        "read_at",
        "clicked_at",
        "starred_at",
        "hidden_at",
        "asr_attempted_at",
    ):
        if col in item:
            item[col] = _timestamp_value(item.get(col))
    if not detail:
        item.pop("detail_json", None)
        item.pop("comments_json", None)
        for col in (
            "tags_json",
            "ai_key_points",
            "ai_keywords",
            "ai_subcategories",
            "multi_l1_reason",
            "ai_extracted",
        ):
            item.pop(col, None)
    return item


def _manual_item_filter(
    alias: str,
    *,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if public_only:
        where.append(f"{alias}.platform != 'manual'")
    elif manual_owner_user_id:
        where.append(f"({alias}.platform != 'manual' OR {alias}.user_id = %(manual_owner_user_id)s)")
        params["manual_owner_user_id"] = manual_owner_user_id
    return where, params


def _info_display_source_filter(alias: str) -> str:
    return f"({alias}.platform != 'twitter' OR COALESCE({alias}.source, '') != 'bookmarks')"


def _item_display_filter(alias: str, min_github_stars: int = 50) -> list[str]:
    where = [
        f"({alias}.source IS NULL OR {alias}.source NOT LIKE 'search:%%')",
        _info_display_source_filter(alias),
    ]
    if min_github_stars > 0:
        where.append(
            f"""({alias}.platform != 'github' OR (
              {alias}.metrics_json IS NOT NULL
              AND ({alias}.metrics_json ->> 'stars') ~ '^[0-9]+$'
              AND ({alias}.metrics_json ->> 'stars')::integer >= {int(min_github_stars)}
            ))"""
        )
    return where


def _base_item_where(
    *,
    alias: str = "i",
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> tuple[list[str], dict[str, Any]]:
    where, params = _manual_item_filter(
        alias,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    where.extend(_item_display_filter(alias, min_github_stars=min_github_stars))
    return where, params


def _add_ai_relevance_filter(where: list[str], *, alias: str = "i") -> None:
    """v18.0 nav-merge: 强制 AI 相关性过滤（信息 tab 复用 query_feed_platforms）。

    PRD §Spec-2 锁定口径（D3）：
        (ai_category IS NOT NULL AND ai_category != 'other')
     OR (ai_categories IS NOT NULL AND ai_categories::text NOT IN ('[]','null','"null"'))

    Postgres 注：ai_categories 是 jsonb，与字面量字符串比较需 ::text cast。
    """
    where.append(
        f"((({alias}.ai_category IS NOT NULL AND {alias}.ai_category != 'other')"
        f" OR ({alias}.ai_categories IS NOT NULL"
        f" AND {alias}.ai_categories::text NOT IN ('[]', 'null', '\"null\"'))))"
    )


def _item_status_join(schema: str, user_id: str | None) -> tuple[str, dict[str, Any], str | None]:
    if not user_id:
        return "", {}, None
    return (
        f"LEFT JOIN {schema}.item_status s ON s.item_id = i.id AND s.user_id = %(status_user_id)s",
        {"status_user_id": user_id},
        "s",
    )


def _where_sql(where: list[str]) -> str:
    return "WHERE " + " AND ".join(where) if where else ""


def _add_search_filter(
    where: list[str],
    params: dict[str, Any],
    search: str | None,
    *,
    param_key: str = "search_like",
):
    if not search:
        return
    params[param_key] = f"%{search}%"
    where.append(
        "(coalesce(i.title, '') || ' ' || coalesce(i.author_name, '') || ' ' || "
        f"coalesce(i.ai_summary, '') || ' ' || coalesce(i.ai_keywords::text, '')) ILIKE %({param_key})s"
    )


def _add_category_filter(where: list[str], params: dict[str, Any], category: str | None):
    if not category:
        return
    if category == UNCATEGORIZED_SENTINEL:
        where.append("i.ai_categories IS NULL")
        return
    params["category"] = category
    where.append(
        """EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(i.ai_categories) AS cat(value)
          WHERE cat.value = %(category)s
        )"""
    )


def _fetch_items(conn: Any, schema: str, where: list[str], params: dict[str, Any],
                 *, order_sql: str, limit: int | None = None, offset: int = 0,
                 detail: bool = False, status_user_id: str | None = None) -> list[dict[str, Any]]:
    qparams = dict(params)
    status_join, status_params, status_alias = _item_status_join(schema, status_user_id)
    qparams.update(status_params)
    limit_sql = ""
    if limit is not None and limit > 0:
        qparams["limit"] = limit
        qparams["offset"] = max(0, offset)
        limit_sql = "LIMIT %(limit)s OFFSET %(offset)s"
    elif offset > 0:
        qparams["offset"] = offset
        limit_sql = "OFFSET %(offset)s"
    rows = conn.execute(
        f"""SELECT {_feed_cols(status_alias, include_content=detail, include_heavy_json=detail)}
              FROM {schema}.items i
              {status_join}
              {_where_sql(where)}
              {order_sql}
              {limit_sql}""",
        qparams,
    ).fetchall()
    return [_normalize_item(dict(r), detail=detail) for r in rows]


_CATEGORY_PRIORITY = {cid: idx for idx, cid in enumerate(ACTIVE_CATEGORY_IDS)}
_EVENT_SOURCE_PREVIEW_LIMIT = 3


def _category_l1(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if "[" in raw:
        raw = raw.split("[", 1)[0]
    category = canonicalize_category(raw)
    if not category or category == "other" or category not in ACTIVE_CATEGORY_IDS:
        return None
    return category


def _build_event_source_metadata(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        cluster_id = int(row.get("cluster_id"))
        data = grouped.setdefault(
            cluster_id,
            {"source_preview": [], "_seen_sources": set(), "_category_counts": {}},
        )
        category = _category_l1(row.get("ai_category"))
        if category:
            counts = data["_category_counts"]
            counts[category] = counts.get(category, 0) + 1

        platform = row.get("platform") or ""
        identity = (
            row.get("source_identity")
            or row.get("url")
            or f"{platform}:{row.get('author_name') or row.get('source') or row.get('item_id')}"
        )
        if identity in data["_seen_sources"]:
            continue
        data["_seen_sources"].add(identity)
        if len(data["source_preview"]) >= _EVENT_SOURCE_PREVIEW_LIMIT:
            continue
        data["source_preview"].append({
            "platform": platform,
            "author": row.get("author_name"),
            "source": row.get("source"),
        })

    result: dict[int, dict[str, Any]] = {}
    for cluster_id, data in grouped.items():
        category = None
        counts = data["_category_counts"]
        if counts:
            category = sorted(
                counts.items(),
                key=lambda item: (-item[1], _CATEGORY_PRIORITY.get(item[0], 999), item[0]),
            )[0][0]
        result[cluster_id] = {
            "category": category,
            "source_preview": data["source_preview"],
        }
    return result


def _fetch_event_source_metadata(conn: Any, schema: str, cluster_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not cluster_ids:
        return {}
    rows = conn.execute(
        f"""SELECT ci.cluster_id, ci.source_identity, ci.rank_in_cluster,
                  ci.is_primary_source,
                  i.id AS item_id, i.platform, i.author_name, i.source,
                  i.url, i.ai_category, i.published_at, i.fetched_at
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = ANY(%(cluster_ids)s)
            ORDER BY ci.cluster_id ASC,
                     COALESCE(ci.is_primary_source, false) DESC,
                     ci.rank_in_cluster ASC NULLS LAST""",
        {"cluster_ids": cluster_ids},
    ).fetchall()
    normalized = [dict(r) for r in rows]
    normalized.sort(
        key=lambda r: (
            int(r.get("cluster_id") or 0),
            -int(bool(r.get("is_primary_source"))),
            int(r.get("rank_in_cluster") if r.get("rank_in_cluster") is not None else 999999),
            -sort_key(r.get("published_at") or r.get("fetched_at")),
        )
    )
    return _build_event_source_metadata(normalized)


def _row_to_event(
    row: dict[str, Any],
    *,
    user_last_seen: dict[int, int | None] | None = None,
    source_metadata: dict[int, dict[str, Any]] | None = None,
) -> dict:
    seen_map = user_last_seen or {}
    cid = int(row["id"])
    live_version = int(row.get("live_version") or 0)
    seen = seen_map.get(cid)
    metadata = (source_metadata or {}).get(cid, {})
    return {
        "id": cid,
        "ai_title": row.get("ai_title"),
        "ai_summary": row.get("ai_summary"),
        "doc_count": int(row.get("doc_count") or 0),
        "unique_source_count": int(row.get("unique_source_count") or 0),
        "category": metadata.get("category"),
        "source_preview": metadata.get("source_preview", []),
        "first_doc_at": to_utc_iso(row.get("first_doc_at")) or row.get("first_doc_at"),
        "last_doc_at": to_utc_iso(row.get("last_doc_at")) if row.get("last_doc_at") else None,
        "platforms": _json_array(row.get("platforms_json")),
        "cover_url": row.get("cover_url"),
        "has_update": bool(seen is not None and live_version > seen),
        "live_version": live_version,
        "last_seen_version": seen,
    }


def _public_cluster_filter(schema: str, cluster_alias: str = "c") -> str:
    return f"""
      AND NOT EXISTS (
        SELECT 1
        FROM {schema}.cluster_items ci_priv
        JOIN {schema}.items i_priv ON i_priv.id = ci_priv.item_id
        WHERE ci_priv.cluster_id = {cluster_alias}.id
          AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
      )
    """


def _github_display_filter(schema: str, min_stars: int, cluster_alias: str = "c") -> str:
    if min_stars <= 0:
        return ""
    return f"""
      AND (
        NOT EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          WHERE ci_disp.cluster_id = {cluster_alias}.id
        )
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          JOIN {schema}.items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform != 'github'
        )
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          JOIN {schema}.items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform = 'github'
            AND CASE
              WHEN i_disp.metrics_json ? 'stars'
                   AND (i_disp.metrics_json ->> 'stars') ~ '^[0-9]+$'
                THEN (i_disp.metrics_json ->> 'stars')::integer
              ELSE 0
            END >= {int(min_stars)}
        )
      )
    """


def _highlights_scope_key(*, dimension: str, value: str | None = None) -> str:
    if dimension == "all":
        return "all"
    return f"{dimension}:{value or ''}"


def _highlights_scope_for_categories(categories: list[str] | None) -> str | None:
    if not categories:
        return _highlights_scope_key(dimension="all")
    normalized: set[str] = set()
    for category in categories:
        cid = _category_l1(category)
        if not cid:
            return None
        normalized.add(cid)
    if len(normalized) != 1:
        return None
    return _highlights_scope_key(dimension="category", value=next(iter(normalized)))


def _normalize_highlights_read_model_cursor(
    cursor: Any,
    *,
    expected_scope_key: str,
    expected_version_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(cursor, dict):
        return None
    version_id = str(cursor.get("version_id") or "").strip()
    scope_key = str(cursor.get("scope_key") or "").strip()
    try:
        normalized_version_id = str(uuid.UUID(version_id))
    except (TypeError, ValueError):
        return None
    if expected_version_id and normalized_version_id != expected_version_id:
        return None
    if scope_key != expected_scope_key:
        return None
    try:
        rank_after = int(cursor.get("rank_after"))
    except (TypeError, ValueError):
        return None
    if rank_after < 0:
        return None
    return {
        "version_id": normalized_version_id,
        "scope_key": scope_key,
        "rank_after": rank_after,
    }


def _event_from_highlights_card(
    value: Any,
    *,
    user_last_seen: dict[int, int | None] | None = None,
) -> dict[str, Any] | None:
    card = _json_value(value)
    if not isinstance(card, dict):
        return None
    try:
        cluster_id = int(card.get("id"))
    except (TypeError, ValueError):
        return None
    seen_map = user_last_seen or {}
    live_version = int(card.get("live_version") or 0)
    seen = seen_map.get(cluster_id)
    return {
        "id": cluster_id,
        "ai_title": card.get("ai_title"),
        "ai_summary": card.get("ai_summary"),
        "doc_count": int(card.get("doc_count") or 0),
        "unique_source_count": int(card.get("unique_source_count") or 0),
        "category": card.get("category"),
        "source_preview": _json_array(card.get("source_preview")),
        "first_doc_at": to_utc_iso(card.get("first_doc_at")) or card.get("first_doc_at"),
        "last_doc_at": to_utc_iso(card.get("last_doc_at")) if card.get("last_doc_at") else None,
        "platforms": _json_array(card.get("platforms")),
        "cover_url": card.get("cover_url"),
        "has_update": bool(seen is not None and live_version > seen),
        "live_version": live_version,
        "last_seen_version": seen,
    }


def _query_highlights_read_model_events(
    *,
    conn: Any,
    schema: str,
    page: int,
    limit: int,
    cursor: dict[str, Any] | None,
    since_version_snapshot: int | None,
    fetched_since: str | None,
    user_id: str | None,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int,
) -> dict[str, Any] | None:
    if not _highlights_read_model_enabled():
        return None
    if fetched_since or since_version_snapshot is not None:
        return None
    if not public_only and not user_id:
        return None
    if int(min_github_stars) != HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS:
        return None
    scope_key = _highlights_scope_for_categories(categories)
    if not scope_key:
        return None
    safe_limit = max(1, min(int(limit), 100))
    safe_page = max(1, int(page or 1))
    try:
        if not _set_events_read_model_timeouts(conn):
            return None
        cursor_state = _normalize_highlights_read_model_cursor(
            cursor,
            expected_scope_key=scope_key,
        )
        active_data = None
        if cursor_state:
            pinned = conn.execute(
                f"""SELECT v.version_id::text AS version_id,
                           sc.scope_key,
                           sc.total_count,
                           sc.max_sort_at,
                           sc.generated_at
                      FROM {schema}.highlights_read_model_versions v
                      JOIN {schema}.highlights_scopes sc
                        ON sc.version_id = v.version_id
                     WHERE v.version_id = %(version_id)s::uuid
                       AND v.status = 'complete'
                       AND sc.scope_key = %(scope_key)s""",
                {
                    "version_id": cursor_state["version_id"],
                    "scope_key": scope_key,
                },
            ).fetchone()
            if pinned:
                active_data = dict(pinned)
        if active_data is None:
            active = conn.execute(
                f"""SELECT v.version_id::text AS version_id,
                           sc.scope_key,
                           sc.total_count,
                           sc.max_sort_at,
                           sc.generated_at
                      FROM {schema}.highlights_read_model_state st
                      JOIN {schema}.highlights_read_model_versions v
                        ON v.version_id = st.active_version_id
                      JOIN {schema}.highlights_scopes sc
                        ON sc.version_id = v.version_id
                     WHERE st.key = %(state_key)s
                       AND v.status = 'complete'
                       AND sc.scope_key = %(scope_key)s""",
                {
                    "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                },
            ).fetchone()
            if not active:
                return None
            active_data = dict(active)
            cursor_state = None
        version_id = str(active_data.get("version_id") or "")
        rank_after = int(cursor_state["rank_after"]) if cursor_state else (safe_page - 1) * safe_limit
        rows = conn.execute(
            f"""SELECT rank, cluster_id, sort_at, card_json
                  FROM {schema}.highlights_scope_items
                 WHERE version_id = %(version_id)s::uuid
                   AND scope_key = %(scope_key)s
                 ORDER BY {_highlights_scope_item_order_sql("highlights_scope_items")}
                 OFFSET %(rank_after)s
                 LIMIT %(limit_plus_one)s""",
            {
                "version_id": version_id,
                "scope_key": scope_key,
                "rank_after": rank_after,
                "limit_plus_one": safe_limit + 1,
            },
        ).fetchall()
        date_counts_cache_key = (
            "events_highlights_date_counts_v1",
            schema,
            version_id,
            scope_key,
            int(timezone_offset_minutes),
            int(active_data.get("total_count") or 0),
            _timestamp_value(active_data.get("max_sort_at")),
            _timestamp_value(active_data.get("generated_at")),
        )
        date_counts = _cache_get_copy(date_counts_cache_key)
        if date_counts is None:
            date_count_rows = conn.execute(
                f"""SELECT COALESCE(
                              to_char((
                                sort_at - (%(timezone_offset_minutes)s::int * interval '1 minute')
                              )::date, 'YYYY-MM-DD'),
                              'unknown'
                            ) AS day,
                           count(*) AS n
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                     GROUP BY day""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "timezone_offset_minutes": timezone_offset_minutes,
                },
            ).fetchall()
            date_counts = {
                str(row["day"] or "unknown"): int(row["n"] or 0)
                for row in date_count_rows
            }
            _cache_set_copy(date_counts_cache_key, date_counts)
        has_more = len(rows) > safe_limit
        page_rows = [dict(row) for row in rows[:safe_limit]]
        # P0-2: seen 状态不在内容层查询,由 fetch_events 的 overlay 覆盖,
        # read model 结果因此可被所有登录用户共享缓存。
        seen_map: dict[int, int | None] = {}
        _commit_safely(conn)
        events: list[dict[str, Any]] = []
        for row in page_rows:
            event = _event_from_highlights_card(row.get("card_json"), user_last_seen=seen_map)
            if event:
                events.append(event)
        next_cursor = None
        if has_more and page_rows:
            next_cursor = {
                "version_id": version_id,
                "scope_key": scope_key,
                "rank_after": rank_after + len(page_rows),
            }
        return {
            "enabled": enabled,
            "events": events,
            "next_cursor": next_cursor,
            "new_since_last_fetch": 0,
            "total_available_within_30d": int(active_data.get("total_count") or 0),
            "date_counts": date_counts,
            "data_backend": event_read_backend(),
            "read_model": HIGHLIGHTS_READ_MODEL_VERSION,
            "read_model_version_id": version_id,
            "scope_key": scope_key,
        }
    except Exception:
        _rollback_safely(conn)
        return None


def fetch_events(
    *,
    page: int = 1,
    limit: int = 20,
    cursor: dict[str, Any] | None = None,
    since_version_snapshot: int | None = None,
    fetched_since: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    min_github_stars: int = 50,
    enabled: bool = False,
    categories: list[str] | None = None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
) -> dict:
    """P0-2(C 端放量):内容与用户状态分离。

    内容(clusters timeline、total、date_counts)与用户无关,由
    ``_fetch_events_content`` 计算并按「匿名/登录」两个 scope 共享缓存——
    N 个登录用户共用一份内容缓存和 singleflight,Supabase 压力与用户数
    解耦。逐用户的 seen 状态(has_update/last_seen_version)在返回前用
    一条索引查询薄覆盖;覆盖失败只降级状态,不影响内容。
    """
    result = _fetch_events_content(
        page=page,
        limit=limit,
        cursor=cursor,
        since_version_snapshot=since_version_snapshot,
        fetched_since=fetched_since,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
        enabled=enabled,
        categories=categories,
        timezone_offset_minutes=timezone_offset_minutes,
    )
    if user_id:
        _overlay_user_cluster_seen(result, user_id)
    return result


def _overlay_user_cluster_seen(result: dict, user_id: str) -> None:
    """In-place 覆盖逐用户 seen 字段;调用方持有的是缓存的深拷贝,可安全改。"""
    events = result.get("events") or []
    ids = [int(ev["id"]) for ev in events if ev.get("id") is not None]
    if not ids:
        return
    schema = remote_schema()
    try:
        with connect() as conn:
            seen_rows = conn.execute(
                f"""SELECT cluster_id, last_seen_version
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = ANY(%(cluster_ids)s)""",
                {"user_id": user_id, "cluster_ids": ids},
            ).fetchall()
    except Exception:
        return  # 状态覆盖失败:内容照常返回,seen 字段保持匿名默认值
    seen_map = {int(r["cluster_id"]): int(r["last_seen_version"] or 0) for r in seen_rows}
    for ev in events:
        cid = ev.get("id")
        if cid is None:
            continue
        seen = seen_map.get(int(cid))
        live_version = int(ev.get("live_version") or 0)
        ev["last_seen_version"] = seen
        ev["has_update"] = bool(seen is not None and live_version > seen)


def _fetch_events_content(
    *,
    page: int = 1,
    limit: int = 20,
    cursor: dict[str, Any] | None = None,
    since_version_snapshot: int | None = None,
    fetched_since: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    min_github_stars: int = 50,
    enabled: bool = False,
    categories: list[str] | None = None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
) -> dict:
    """Fetch visible events from Supabase/Postgres in the API response shape.

    输出必须与用户无关(seen 字段为匿名默认值,由 fetch_events 覆盖);
    ``user_id`` 仅参与 read model 资格与快照条件判断,缓存 key 只取
    ``bool(user_id)`` 区分「匿名公共 / 登录」两种内容口径(public_only
    过滤不同)。

    v17.0: categories OR 多选 — 精选 tab L1 chip 筛选。空列表 / None = 不筛选。
    """
    schema = remote_schema()
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    # v17.0: categories filter（Postgres split_part 提取 L1 段）
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    where_sql = f"""
      c.is_visible_in_feed = true
      AND c.published_at IS NOT NULL
      AND coalesce(c.archived, false) = false
      AND c.merged_into IS NULL
      AND c.last_updated_at > now() - interval '30 days'
      AND (
        %(fetched_since)s::timestamptz IS NULL
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci
          JOIN {schema}.items i ON i.id = ci.item_id
          WHERE ci.cluster_id = c.id
            AND i.fetched_at >= %(fetched_since)s::timestamptz
        )
      )
      {public_filter}
      {github_filter}
      {verdict_filter}
      {categories_filter}
    """
    params = {
        "fetched_since": fetched_since,
        "limit_plus_one": limit + 1,
        "offset": offset,
        "snapshot": since_version_snapshot,
        "categories": categories or [],
        "timezone_offset_minutes": tz_offset,
    }
    cursor_cache_key = json.dumps(cursor, sort_keys=True, default=str) if cursor else ""
    result_cache_key = (
        "events_result_30d_v5",  # v5: 内容缓存去 user 化(P0-2),只按登录与否分桶
        schema,
        int(page),
        int(limit),
        cursor_cache_key,
        since_version_snapshot,
        fetched_since or "",
        bool(user_id),
        bool(public_only),
        int(min_github_stars),
        bool(enabled),
        tz_offset,
        tuple(categories or []),
    )
    prefer_highlights_read_model = (
        _highlights_read_model_enabled()
        and fetched_since is None
        and since_version_snapshot is None
        and (public_only or bool(user_id))
        and int(min_github_stars) == HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS
        and _highlights_scope_for_categories(categories) is not None
    )
    highlights_stale_freshness: dict[str, Any] | None = None
    highlights_self_heal: dict[str, Any] | None = None
    skip_stale_snapshot_fallback = False
    if (
        prefer_highlights_read_model
        and page == 1
        and cursor is None
        and _highlights_stale_fallback_enabled()
        and _highlights_request_freshness_enabled()
    ):
        try:
            freshness = highlights_read_model_freshness(min_github_stars=min_github_stars)
            if freshness.get("stale"):
                highlights_stale_freshness = freshness
                highlights_self_heal = _trigger_highlights_read_model_self_heal(
                    reason=str(freshness.get("reason") or "stale"),
                    min_interval_sec=60,
                )
                prefer_highlights_read_model = False
                skip_stale_snapshot_fallback = True
        except Exception as exc:
            highlights_stale_freshness = {
                "ok": False,
                "stale": None,
                "error": str(exc)[:200],
            }
    if not skip_stale_snapshot_fallback:
        cached_result = _cache_get_copy(result_cache_key)
        if cached_result is not None:
            return cached_result
    snapshot_key = None
    local_cache_name = None
    expired_snapshot_fallback = None
    if (
        page == 1
        and limit == 20
        and since_version_snapshot is None
        and fetched_since is None
        and not user_id
    ):
        snapshot_key = _events_snapshot_key(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
            enabled=enabled,
            categories=categories,
            timezone_offset_minutes=tz_offset,
        )
        local_cache_name = _feed_events_local_cache_name(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
            enabled=enabled,
            categories=categories,
            timezone_offset_minutes=tz_offset,
        )
        fresh_fallback = _read_local_read_cache(
            local_cache_name,
            max_age_sec=_LOCAL_READ_CACHE_FRESH_SEC,
        )
        if fresh_fallback is not None and not prefer_highlights_read_model:
            return _cache_set_copy(result_cache_key, fresh_fallback)
    if prefer_highlights_read_model:
        def _compute_highlights_result() -> dict[str, Any] | None:
            cached_inside = _cache_get_copy(result_cache_key)
            if cached_inside is not None:
                return cached_inside
            with connect() as conn:
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                )
            if read_model_result is not None:
                if snapshot_key:
                    _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                    if local_cache_name:
                        _write_local_read_cache_async(local_cache_name, read_model_result)
                return _cache_set_copy(result_cache_key, read_model_result)
            return None

        try:
            read_model_result = _singleflight_sync(
                ("events_highlights_read_model", *result_cache_key),
                _compute_highlights_result,
            )
        except RemoteDBError:
            if page == 1 and not user_id and since_version_snapshot is None and fetched_since is None:
                if local_cache_name:
                    stale_fallback = _read_local_read_cache(local_cache_name)
                    if stale_fallback is not None:
                        return _cache_set_copy(
                            result_cache_key,
                            _mark_stale_payload(stale_fallback, source="local_read_cache"),
                        )
                return {
                    "enabled": enabled,
                    "events": [],
                    "next_cursor": None,
                    "new_since_last_fetch": 0,
                    "total_available_within_30d": 0,
                    "date_counts": {},
                    "data_backend": event_read_backend(),
                    "degraded": True,
                }
            raise
        if read_model_result is not None:
            return read_model_result
        prefer_highlights_read_model = False
    total_cache_key = (
        "events_total_30d",
        schema,
        bool(public_only),
        bool(user_id),
        int(min_github_stars),
        fetched_since or "",
        tuple(categories or []),
    )
    date_counts_cache_key = (
        "events_date_counts_30d",
        schema,
        bool(public_only),
        bool(user_id),
        int(min_github_stars),
        fetched_since or "",
        tz_offset,
        tuple(categories or []),
    )
    try:
        with connect() as conn:
            if prefer_highlights_read_model:
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                )
                if read_model_result is not None:
                    if snapshot_key:
                        _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                        if local_cache_name:
                            _write_local_read_cache_async(local_cache_name, read_model_result)
                    return _cache_set_copy(result_cache_key, read_model_result)
            if snapshot_key and not skip_stale_snapshot_fallback:
                snapshot = _read_feed_snapshot(conn, schema, snapshot_key)
                if snapshot is not None:
                    return _cache_set_copy(result_cache_key, snapshot)
                expired_snapshot_fallback = _read_feed_snapshot(
                    conn,
                    schema,
                    snapshot_key,
                    allow_expired=True,
                )
            if not prefer_highlights_read_model:
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                )
                if read_model_result is not None:
                    if snapshot_key:
                        _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                        if local_cache_name:
                            _write_local_read_cache_async(local_cache_name, read_model_result)
                    return _cache_set_copy(result_cache_key, read_model_result)
            rows = conn.execute(
                f"""SELECT c.id, c.ai_title, c.ai_summary, c.doc_count,
                           c.unique_source_count, c.first_doc_at, c.last_doc_at,
                           c.platforms_json,
                           COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                           c.live_version,
                           c.last_updated_at
                      FROM {schema}.clusters c
                      LEFT JOIN LATERAL (
                        SELECT i.cover_url
                          FROM {schema}.cluster_items ci
                          JOIN {schema}.items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND NULLIF(i.cover_url, '') IS NOT NULL
                           AND i.platform <> 'manual'
                           AND i.user_id IS NULL
                         ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                                  ci.rank_in_cluster ASC NULLS LAST
                         LIMIT 1
                      ) event_cover ON true
                     WHERE {where_sql}
                     ORDER BY c.first_doc_at DESC NULLS LAST,
                              c.last_updated_at DESC NULLS LAST,
                              c.id DESC
                     LIMIT %(limit_plus_one)s OFFSET %(offset)s""",
                params,
            ).fetchall()
            total_count = _cache_get(total_cache_key)
            if total_count is None:
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM {schema}.clusters c WHERE {where_sql}",
                    params,
                ).fetchone()
                total_count = int(total_row["n"] if total_row else 0)
                _cache_set(total_cache_key, total_count)
            date_counts = _cache_get_copy(date_counts_cache_key)
            if date_counts is None:
                date_count_rows = conn.execute(
                    f"""SELECT COALESCE(
                                  to_char((
                                    COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)
                                    - (%(timezone_offset_minutes)s::int * interval '1 minute')
                                  )::date, 'YYYY-MM-DD'),
                                  'unknown'
                                ) AS day,
                               count(*) AS n
                          FROM {schema}.clusters c
                         WHERE {where_sql}
                         GROUP BY day""",
                    params,
                ).fetchall()
                date_counts = {
                    str(row["day"] or "unknown"): int(row["n"] or 0)
                    for row in date_count_rows
                }
                _cache_set_copy(date_counts_cache_key, date_counts)
            new_since = 0
            if since_version_snapshot is not None:
                row = conn.execute(
                    f"""SELECT count(*) AS n
                          FROM {schema}.clusters c
                         WHERE {where_sql}
                           AND c.id > %(snapshot)s""",
                    params,
                ).fetchone()
                new_since = int(row["n"] if row else 0)
            # P0-2: seen 状态不在内容层查询,由 fetch_events 的 overlay 覆盖。
            seen_map: dict[int, int | None] = {}
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            source_metadata = _fetch_event_source_metadata(conn, schema, [int(r["id"]) for r in page_rows])
            result = {
                "enabled": enabled,
                "events": [
                    _row_to_event(dict(r), user_last_seen=seen_map, source_metadata=source_metadata)
                    for r in page_rows
                ],
                "next_cursor": (page + 1) if has_more else None,
                "new_since_last_fetch": new_since,
                "total_available_within_30d": int(total_count),
                "date_counts": date_counts,
                "data_backend": event_read_backend(),
            }
            if highlights_stale_freshness is not None:
                result["read_model_stale"] = True
                result["fallback_reason"] = "highlights_read_model_stale"
                result["read_model_freshness"] = highlights_stale_freshness
                if highlights_self_heal is not None:
                    result["read_model_self_heal"] = highlights_self_heal
            if snapshot_key:
                _write_feed_snapshot_async(schema, snapshot_key, result)
                if local_cache_name:
                    _write_local_read_cache_async(local_cache_name, result)
        if _feed_result_cacheable(result):
            return _cache_set_copy(result_cache_key, result)
        return result
    except RemoteDBError:
        if page == 1 and not user_id and since_version_snapshot is None and fetched_since is None:
            if expired_snapshot_fallback is not None:
                return _cache_set_copy(result_cache_key, expired_snapshot_fallback)
            if local_cache_name:
                stale_fallback = _read_local_read_cache(local_cache_name)
                if stale_fallback is not None:
                    return _cache_set_copy(
                        result_cache_key,
                        _mark_stale_payload(stale_fallback, source="local_read_cache"),
                    )
            result = {
                "enabled": enabled,
                "events": [],
                "next_cursor": None,
                "new_since_last_fetch": 0,
                "total_available_within_30d": 0,
                "date_counts": {},
                "data_backend": event_read_backend(),
                "degraded": True,
            }
            return result
        raise


def search_recommend_remote(
    *,
    q: str,
    limit: int = 30,
    public_only: bool = False,
    min_github_stars: int = 50,
    categories: list[str] | None = None,
) -> dict:
    """v17.0: Supabase 路径搜索 — recommend context 返回 docs + events 双区。

    docs: items 表 ILIKE (title/content/ai_summary/author_name/ai_keywords)
    events: clusters 表 ILIKE (ai_title/ai_summary), 可选 categories OR 筛选
            与 /api/feed/events 保持一致的 visibility 门槛 (is_visible_in_feed + unique_source_count>=2)
    """
    schema = remote_schema()
    pattern = f"%{q}%"
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    params: dict[str, Any] = {
        "pattern": pattern,
        "limit": limit,
        "categories": categories or [],
    }
    with connect() as conn:
        # docs 区 (items 表) — recommend context 与 channel 等共享 doc 维度搜索
        # 性能优化 (v17.0): 仅搜 title + ai_summary 短字段。content 是长文本字段,
        # ILIKE 全表扫描 63k 行约耗时 47s; 去掉后约 1-2s。如需 content 全文搜索,
        # 后续应建 tsvector + GIN 索引专项支持。
        doc_rows = conn.execute(
            f"""SELECT id, platform, title, author_name, published_at, ai_summary
                  FROM {schema}.items
                 WHERE title ILIKE %(pattern)s
                    OR ai_summary ILIKE %(pattern)s
                 ORDER BY coalesce(published_at, fetched_at) DESC
                 LIMIT %(limit)s""",
            params,
        ).fetchall()
        docs_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.items
                 WHERE title ILIKE %(pattern)s
                    OR ai_summary ILIKE %(pattern)s""",
            params,
        ).fetchone()
        # events 区 (clusters 表) — categories 叠加可选
        ev_where = f"""
          c.is_visible_in_feed = true
          AND coalesce(c.unique_source_count, 0) >= 2
          AND c.published_at IS NOT NULL
          AND coalesce(c.archived, false) = false
          AND c.merged_into IS NULL
          AND (c.ai_title ILIKE %(pattern)s OR c.ai_summary ILIKE %(pattern)s)
          {public_filter}
          {github_filter}
          {categories_filter}
        """
        ev_rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.doc_count,
                       c.unique_source_count, c.first_doc_at, c.last_doc_at,
                       c.platforms_json, c.cover_url, c.live_version
                  FROM {schema}.clusters c
                 WHERE {ev_where}
                 ORDER BY c.first_doc_at DESC
                 LIMIT %(limit)s""",
            params,
        ).fetchall()
        ev_total_row = conn.execute(
            f"SELECT count(*) AS n FROM {schema}.clusters c WHERE {ev_where}",
            params,
        ).fetchone()
        source_metadata = _fetch_event_source_metadata(conn, schema, [int(r["id"]) for r in ev_rows])
    return {
        "docs": [dict(r) for r in doc_rows],
        "docs_total": int(docs_total_row["n"]) if docs_total_row else 0,
        # v17.0 fix: events 区必须走 _row_to_event transformer
        # 否则前端收到 platforms_json (JSON 字符串) 而非 platforms (数组),
        # 来源图标渲染 fallback 灰色 "s"; unique_source_count 也缺失,
        # EventCard 来源徽章 (BF-0428-1) 失效
        "events": [_row_to_event(dict(r), source_metadata=source_metadata) for r in ev_rows],
        "events_total": int(ev_total_row["n"]) if ev_total_row else 0,
        "data_backend": event_read_backend(),
    }


def cluster_detail(*, cluster_id: int, public_only: bool = False,
                   user_id: str | None = None) -> dict | None:
    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.ai_key_points, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version, c.merged_into,
                       c.is_visible_in_feed
                  FROM {schema}.clusters c
                  LEFT JOIN LATERAL (
                    SELECT i.cover_url
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.cover_url, '') IS NOT NULL
                       AND i.platform <> 'manual'
                       AND i.user_id IS NULL
                     ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                              ci.rank_in_cluster ASC NULLS LAST
                     LIMIT 1
                  ) detail_cover ON true
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        metadata = (
            _fetch_event_source_metadata(conn, schema, [cluster_id]).get(cluster_id, {})
            if row else {}
        )
    if not row:
        return None
    data = dict(row)
    user_last_seen = None
    viewer_status = {"clicked_at": None, "starred_at": None, "last_seen_version": None}
    if user_id:
        with connect() as conn:
            seen = conn.execute(
                f"""SELECT clicked_at, starred_at, last_seen_version
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
        if seen:
            user_last_seen = int(seen["last_seen_version"]) if seen["last_seen_version"] is not None else None
            viewer_status = {
                "clicked_at": to_utc_iso(seen.get("clicked_at")) if seen.get("clicked_at") else None,
                "starred_at": to_utc_iso(seen.get("starred_at")) if seen.get("starred_at") else None,
                "last_seen_version": user_last_seen,
            }
    body = {
        "id": int(data["id"]),
        "ai_title": data.get("ai_title"),
        "ai_summary": data.get("ai_summary"),
        "ai_key_points": _json_array(data.get("ai_key_points")),
        "doc_count": int(data.get("doc_count") or 0),
        "unique_source_count": int(data.get("unique_source_count") or 0),
        "platforms": _json_array(data.get("platforms_json")),
        "category": metadata.get("category"),
        "first_doc_at": to_utc_iso(data.get("first_doc_at")) or data.get("first_doc_at"),
        "last_doc_at": to_utc_iso(data.get("last_doc_at")) if data.get("last_doc_at") else None,
        "cover_url": data.get("cover_url"),
        "media_urls": _media_urls_from_item(data.get("cover_url"), None),
        "live_version": int(data.get("live_version") or 0),
        "user_last_seen_version": user_last_seen,
        "viewer_status": viewer_status,
        "is_visible_in_feed": bool(data.get("is_visible_in_feed")),
        "data_backend": event_read_backend(),
    }
    if data.get("merged_into"):
        body["redirect_to"] = int(data["merged_into"])
    return body


def cluster_sources(
    *,
    cluster_id: int,
    page: int = 1,
    limit: int = 20,
    public_only: bool = False,
) -> dict | None:
    schema = remote_schema()
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    source_where, source_params = _manual_item_filter("i", public_only=public_only)
    source_filter = (" AND " + " AND ".join(source_where)) if source_where else ""
    with connect() as conn:
        exists = conn.execute(
            f"""SELECT 1
                  FROM {schema}.clusters c
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        if not exists:
            return None
        rows = conn.execute(
            f"""SELECT i.id AS item_id, i.title, i.author_name, i.platform,
                       i.published_at, i.fetched_at, i.url, ci.is_primary_source,
                       i.cover_url, i.media_json,
                       left(coalesce(i.ai_summary, i.content, ''), 200) AS snippet
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %(cluster_id)s
                 {source_filter}
                 ORDER BY coalesce(i.published_at, i.fetched_at) DESC,
                          ci.is_primary_source DESC
                 LIMIT %(limit_plus_one)s OFFSET %(offset)s""",
            {
                "cluster_id": cluster_id,
                "limit_plus_one": limit + 1,
                "offset": offset,
                **source_params,
            },
        ).fetchall()
    has_more = len(rows) > limit
    sources = []
    for raw in rows[:limit]:
        r = dict(raw)
        platform = r.get("platform") or ""
        badge = None
        if platform in ("openai", "anthropic", "official"):
            badge = "official"
        elif platform in ("hackernews",):
            badge = "community"
        sources.append({
            "item_id": r.get("item_id"),
            "title": r.get("title"),
            "author": r.get("author_name"),
            "platform": platform,
            "published_at": to_utc_iso(r.get("published_at") or r.get("fetched_at")),
            "url": r.get("url"),
            "cover_url": r.get("cover_url"),
            "media_urls": _media_urls_from_item(r.get("cover_url"), r.get("media_json")),
            "is_primary_source": int(bool(r.get("is_primary_source"))),
            "authority_badge": badge,
            "snippet": (r.get("snippet") or "").strip(),
        })
    return {
        "sources": sources,
        "next_cursor": (page + 1) if has_more else None,
        "data_backend": event_read_backend(),
    }


def cluster_bundle(
    *,
    cluster_id: int,
    page: int = 1,
    limit: int = 20,
    public_only: bool = False,
    user_id: str | None = None,
) -> dict | None:
    """Return cluster detail and first-page sources in a single DB checkout."""
    schema = remote_schema()
    cache_key = (
        "cluster_bundle",
        schema,
        int(cluster_id),
        int(page),
        int(limit),
        bool(public_only),
        user_id or "",
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    source_where, source_params = _manual_item_filter("i", public_only=public_only)
    source_filter = (" AND " + " AND ".join(source_where)) if source_where else ""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.ai_key_points, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version, c.merged_into,
                       c.is_visible_in_feed
                  FROM {schema}.clusters c
                  LEFT JOIN LATERAL (
                    SELECT i.cover_url
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.cover_url, '') IS NOT NULL
                       AND i.platform <> 'manual'
                       AND i.user_id IS NULL
                     ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                              ci.rank_in_cluster ASC NULLS LAST
                     LIMIT 1
                  ) detail_cover ON true
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        metadata = _fetch_event_source_metadata(conn, schema, [cluster_id]).get(cluster_id, {})
        user_last_seen = None
        viewer_status = {"clicked_at": None, "starred_at": None, "last_seen_version": None}
        if user_id:
            seen = conn.execute(
                f"""SELECT clicked_at, starred_at, last_seen_version
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
            if seen:
                user_last_seen = int(seen["last_seen_version"]) if seen["last_seen_version"] is not None else None
                viewer_status = {
                    "clicked_at": to_utc_iso(seen.get("clicked_at")) if seen.get("clicked_at") else None,
                    "starred_at": to_utc_iso(seen.get("starred_at")) if seen.get("starred_at") else None,
                    "last_seen_version": user_last_seen,
                }
        source_rows = conn.execute(
            f"""SELECT i.id AS item_id, i.title, i.author_name, i.platform,
                       i.published_at, i.fetched_at, i.url, ci.is_primary_source,
                       i.cover_url, i.media_json,
                       left(coalesce(i.ai_summary, i.content, ''), 200) AS snippet
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %(cluster_id)s
                 {source_filter}
                 ORDER BY coalesce(i.published_at, i.fetched_at) DESC,
                          ci.is_primary_source DESC
                 LIMIT %(limit_plus_one)s OFFSET %(offset)s""",
            {
                "cluster_id": cluster_id,
                "limit_plus_one": limit + 1,
                "offset": offset,
                **source_params,
            },
        ).fetchall()

    detail = {
        "id": int(data["id"]),
        "ai_title": data.get("ai_title"),
        "ai_summary": data.get("ai_summary"),
        "ai_key_points": _json_array(data.get("ai_key_points")),
        "doc_count": int(data.get("doc_count") or 0),
        "unique_source_count": int(data.get("unique_source_count") or 0),
        "platforms": _json_array(data.get("platforms_json")),
        "category": metadata.get("category"),
        "first_doc_at": to_utc_iso(data.get("first_doc_at")) or data.get("first_doc_at"),
        "last_doc_at": to_utc_iso(data.get("last_doc_at")) if data.get("last_doc_at") else None,
        "cover_url": data.get("cover_url"),
        "media_urls": _media_urls_from_item(data.get("cover_url"), None),
        "live_version": int(data.get("live_version") or 0),
        "user_last_seen_version": user_last_seen,
        "viewer_status": viewer_status,
        "is_visible_in_feed": bool(data.get("is_visible_in_feed")),
        "data_backend": event_read_backend(),
    }
    if data.get("merged_into"):
        detail["redirect_to"] = int(data["merged_into"])

    has_more = len(source_rows) > limit
    sources = []
    for raw in source_rows[:limit]:
        r = dict(raw)
        platform = r.get("platform") or ""
        badge = None
        if platform in ("openai", "anthropic", "official"):
            badge = "official"
        elif platform in ("hackernews",):
            badge = "community"
        sources.append({
            "item_id": r.get("item_id"),
            "title": r.get("title"),
            "author": r.get("author_name"),
            "platform": platform,
            "published_at": to_utc_iso(r.get("published_at") or r.get("fetched_at")),
            "url": r.get("url"),
            "cover_url": r.get("cover_url"),
            "media_urls": _media_urls_from_item(r.get("cover_url"), r.get("media_json")),
            "is_primary_source": int(bool(r.get("is_primary_source"))),
            "authority_badge": badge,
            "snippet": (r.get("snippet") or "").strip(),
        })
    result = {
        "cluster": detail,
        "sources": sources,
        "sources_next_cursor": (page + 1) if has_more else None,
        "data_backend": event_read_backend(),
    }
    return _cache_set_copy(cache_key, result)


def mark_cluster_clicked(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT live_version FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            live_version = int(row["live_version"] or 0)
            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, clicked_at, last_seen_version
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, now(), %(live_version)s)
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         clicked_at = excluded.clicked_at,
                         last_seen_version = excluded.last_seen_version""",
                {"user_id": user_id, "cluster_id": cluster_id, "live_version": live_version},
            )
        conn.commit()
    return {"ok": True, "last_seen_version": live_version, "data_backend": status_backend()}


def mark_cluster_seen(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT live_version FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            live_version = int(row["live_version"] or 0)
            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, last_seen_version
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, %(live_version)s)
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         last_seen_version = excluded.last_seen_version""",
                {"user_id": user_id, "cluster_id": cluster_id, "live_version": live_version},
            )
        conn.commit()
    return {
        "cluster_id": cluster_id,
        "last_seen_version": live_version,
        "data_backend": status_backend(),
    }


def set_cluster_star(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT 1 FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            status = cur.execute(
                f"""SELECT starred_at
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
            if status and status.get("starred_at"):
                cur.execute(
                    f"""UPDATE {schema}.cluster_status
                           SET starred_at = NULL
                         WHERE user_id = %(user_id)s
                           AND cluster_id = %(cluster_id)s""",
                    {"user_id": user_id, "cluster_id": cluster_id},
                )
                conn.commit()
                clear_user_cache_keys(user_id)
                return {"ok": True, "starred_at": None, "data_backend": status_backend()}

            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, starred_at
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, now())
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         starred_at = excluded.starred_at""",
                {"user_id": user_id, "cluster_id": cluster_id},
            )
            starred = cur.execute(
                f"""SELECT starred_at
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
        conn.commit()
    clear_user_cache_keys(user_id)
    return {
        "ok": True,
        "starred_at": to_utc_iso(starred.get("starred_at")) if starred and starred.get("starred_at") else None,
        "data_backend": status_backend(),
    }


def _library_item_entry(item: dict[str, Any], *, status_field: str) -> dict[str, Any]:
    return {
        "id": f"item:{item.get('id')}",
        "type": "item",
        "occurred_at": item.get(status_field) or item.get("fetched_at"),
        "item": item,
    }


def _library_cluster_entry(row: dict[str, Any], *, status_field: str) -> dict[str, Any]:
    viewer_status = {
        "clicked_at": to_utc_iso(row.get("clicked_at")) if row.get("clicked_at") else None,
        "starred_at": to_utc_iso(row.get("starred_at")) if row.get("starred_at") else None,
        "last_seen_version": int(row.get("last_seen_version") or 0),
    }
    cover_url = row.get("cover_url")
    cluster = {
        "id": int(row["id"]),
        "ai_title": row.get("ai_title"),
        "ai_summary": row.get("ai_summary"),
        "doc_count": int(row.get("doc_count") or 0),
        "unique_source_count": int(row.get("unique_source_count") or 0),
        "platforms": _json_array(row.get("platforms_json")),
        "category": row.get("category"),
        "first_doc_at": to_utc_iso(row.get("first_doc_at")) or row.get("first_doc_at"),
        "last_doc_at": to_utc_iso(row.get("last_doc_at")) if row.get("last_doc_at") else None,
        "cover_url": cover_url,
        "media_urls": [cover_url] if cover_url else [],
        "live_version": int(row.get("live_version") or 0),
        "user_last_seen_version": viewer_status["last_seen_version"],
        "is_visible_in_feed": bool(row.get("is_visible_in_feed")),
        "viewer_status": viewer_status,
        "data_backend": event_read_backend(),
    }
    return {
        "id": f"cluster:{int(row['id'])}",
        "type": "cluster",
        "occurred_at": to_utc_iso(row.get(status_field)) or row.get(status_field),
        "cluster": cluster,
    }


def query_library(
    *,
    view: str,
    limit: int = 100,
    offset: int = 0,
    user_id: str,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict[str, Any]:
    if view not in ("history", "starred"):
        raise ValueError("view must be history or starred")
    schema = remote_schema()
    status_field = "clicked_at" if view == "history" else "starred_at"
    fetch_limit = max(1, int(limit) + int(offset))

    item_where, item_params = _base_item_where(
        public_only=False,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    item_where.append(f"s.{status_field} IS NOT NULL")
    item_order = f"ORDER BY s.{status_field} DESC NULLS LAST"

    cluster_params = {
        "user_id": user_id,
        "limit": fetch_limit,
    }
    cluster_status_predicate = f"s.{status_field} IS NOT NULL"
    cluster_privacy_filter = """
      AND NOT EXISTS (
        SELECT 1
          FROM {schema}.cluster_items ci_priv
          JOIN {schema}.items i_priv ON i_priv.id = ci_priv.item_id
         WHERE ci_priv.cluster_id = c.id
           AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
           AND COALESCE(i_priv.user_id, '') <> %(user_id)s
      )
    """.format(schema=schema)

    with connect() as conn:
        # BE-6: 收藏/历史是登录核心路径;cluster 页带两个 LATERAL 子查询,
        # 数据倾斜时可拖到 pooler 全局 120s 并占死池连接。与 feed live 同口径止损。
        _set_short_statement_timeout(conn, 4500)
        item_count_params = dict(item_params)
        item_count_join, item_status_params, _ = _item_status_join(schema, user_id)
        item_count_params.update(item_status_params)
        item_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.items i
                  {item_count_join}
                  {_where_sql(item_where)}""",
            item_count_params,
        ).fetchone()
        item_rows = _fetch_items(
            conn,
            schema,
            item_where,
            item_params,
            order_sql=item_order,
            limit=fetch_limit,
            offset=0,
            status_user_id=user_id,
        )

        cluster_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.cluster_status s
                  JOIN {schema}.clusters c ON c.id = s.cluster_id
                 WHERE s.user_id = %(user_id)s
                   AND {cluster_status_predicate}
                   AND COALESCE(c.archived, false) = false
                   AND c.merged_into IS NULL
                   {cluster_privacy_filter}""",
            cluster_params,
        ).fetchone()
        cluster_rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version,
                       c.is_visible_in_feed, cat.category,
                       s.clicked_at, s.starred_at, s.last_seen_version
                  FROM {schema}.cluster_status s
                  JOIN {schema}.clusters c ON c.id = s.cluster_id
                  LEFT JOIN LATERAL (
                    SELECT i.cover_url
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.cover_url, '') IS NOT NULL
                     ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                              ci.rank_in_cluster ASC NULLS LAST
                     LIMIT 1
                  ) detail_cover ON true
                  LEFT JOIN LATERAL (
                    SELECT i.ai_category AS category
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.ai_category, '') IS NOT NULL
                     GROUP BY i.ai_category
                     ORDER BY count(*) DESC
                     LIMIT 1
                  ) cat ON true
                 WHERE s.user_id = %(user_id)s
                   AND {cluster_status_predicate}
                   AND COALESCE(c.archived, false) = false
                   AND c.merged_into IS NULL
                   {cluster_privacy_filter}
                 ORDER BY s.{status_field} DESC NULLS LAST
                 LIMIT %(limit)s""",
            cluster_params,
        ).fetchall()

    entries = (
        [_library_item_entry(item, status_field=status_field) for item in item_rows]
        + [_library_cluster_entry(dict(row), status_field=status_field) for row in cluster_rows]
    )
    entries.sort(key=lambda entry: entry.get("occurred_at") or "", reverse=True)
    total = int(item_total_row["n"] if item_total_row else 0) + int(cluster_total_row["n"] if cluster_total_row else 0)
    return {
        "entries": entries[offset:offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "view": view,
        "data_backend": feed_read_backend(),
    }


def query_feed(
    *,
    platform: str | None = None,
    source: str | None = None,
    unread: bool = False,
    starred: bool = False,
    clicked: bool = False,
    search: str | None = None,
    limit: int = 0,
    offset: int = 0,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    """Return `/api/feed` data from the remote DB.

    Anonymous requests do not have a remote item_status scope. Starred/clicked
    filters therefore return empty results, while unread behaves like "all".
    """
    if not user_id and (starred or clicked):
        return {"items": [], "total": 0, "offset": offset, "limit": limit, "data_backend": feed_read_backend()}
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    _add_search_filter(where, params, search)
    if platform:
        where.append("i.platform = %(platform)s")
        params["platform"] = platform
    if source:
        where.append("i.source = %(source)s")
        params["source"] = source
    order_sql = "ORDER BY i.fetched_at DESC NULLS LAST, i.published_at DESC NULLS LAST"
    if user_id:
        if unread:
            where.append("(s.item_id IS NULL OR (s.clicked_at IS NULL AND s.hidden_at IS NULL))")
        if starred:
            where.append("s.starred_at IS NOT NULL")
        if clicked:
            where.append("s.clicked_at IS NOT NULL")
            order_sql = "ORDER BY s.clicked_at DESC NULLS LAST"
    count_cache_key = (
        "feed_total",
        schema,
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        platform or "",
        source or "",
        search or "",
        bool(unread),
        bool(starred),
        bool(clicked),
    )
    local_cache_name = None
    if (
        not user_id
        and not platform
        and not source
        and not search
        and not unread
        and not starred
        and not clicked
        and offset == 0
        and 0 < int(limit or 0) <= 50
        and manual_owner_user_id is None
    ):
        local_cache_name = _feed_items_local_cache_name(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
        )
        fresh_fallback = _read_local_read_cache(
            local_cache_name,
            max_age_sec=_LOCAL_READ_CACHE_FRESH_SEC,
        )
        if fresh_fallback is not None:
            return fresh_fallback

    if _remote_feed_live_circuit_open():
        if local_cache_name:
            stale_fallback = _read_local_read_cache(local_cache_name)
            if stale_fallback is not None:
                return _mark_stale_payload(stale_fallback, source="local_read_cache")
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "data_backend": feed_read_backend(),
            "degraded": True,
        }

    try:
        with connect() as conn:
            _set_short_statement_timeout(
                conn,
                _remote_feed_search_timeout_ms() if search else _remote_feed_live_timeout_ms(),
            )
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=order_sql,
                limit=limit if limit > 0 else None,
                offset=offset,
                status_user_id=user_id,
            )
    except Exception as exc:
        if search:
            # BF-0704-6: 搜索失败只降级搜索本身,不熔断整个 feed live 读
            print(f"[warn] feed search query failed (search={search!r}): {exc}")
        else:
            _mark_remote_feed_live_circuit_open()
        if local_cache_name:
            stale_fallback = _read_local_read_cache(local_cache_name)
            if stale_fallback is not None:
                return _mark_stale_payload(stale_fallback, source="local_read_cache")
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "data_backend": feed_read_backend(),
            "degraded": True,
        }

    total = _cache_get(count_cache_key)
    total_is_estimate = False
    if total is None:
        try:
            with connect() as conn:
                _set_short_statement_timeout(
                    conn,
                    _remote_feed_search_timeout_ms() if search else _remote_feed_live_timeout_ms(),
                )
                count_params = dict(params)
                count_join, count_status_params, _ = _item_status_join(schema, user_id)
                count_params.update(count_status_params)
                if search:
                    # 搜索 total 封顶,避免全量匹配行回表 count
                    count_params["count_cap"] = CONTEXT_SEARCH_EVENTS_TOTAL_CAP
                    count_sql = (
                        f"SELECT count(*) AS n FROM (SELECT 1 FROM {schema}.items i "
                        f"{count_join} {_where_sql(where)} LIMIT %(count_cap)s) capped"
                    )
                else:
                    count_sql = f"SELECT count(*) AS n FROM {schema}.items i {count_join} {_where_sql(where)}"
                total_row = conn.execute(count_sql, count_params).fetchone()
                total = int(total_row["n"] if total_row else 0)
                _cache_set(count_cache_key, total)
        except Exception as exc:
            if search:
                print(f"[warn] feed search count failed (search={search!r}): {exc}")
            else:
                _mark_remote_feed_live_circuit_open()
            total = max(0, offset) + len(items)
            total_is_estimate = True
    result = {
        "items": items,
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "data_backend": feed_read_backend(),
    }
    if total_is_estimate:
        result["degraded"] = True
        result["degraded_reason"] = "feed_total_unavailable"
        result["total_is_estimate"] = True
    elif local_cache_name:
        _write_local_read_cache_async(local_cache_name, result)
    return result


def context_search(
    *,
    q: str,
    context: str = "recommend",
    limit: int = 30,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
    categories: list[str] | None = None,
    events_only: bool = False,
) -> dict:
    """Remote-only implementation for `/api/search`.

    Search remains full-DB semantics: the DB computes totals, and the API only
    returns the first page of docs/events for rendering.
    """
    keyword = (q or "").strip()
    if not keyword:
        base: dict[str, Any] = {"docs": [], "docs_total": 0}
        if context == "recommend":
            base.update({"events": [], "events_total": 0})
        return base

    events_only_recommend = events_only and context == "recommend"
    if events_only_recommend:
        out: dict[str, Any] = {"docs": [], "docs_total": 0}
    else:
        docs_body = query_feed(
            search=keyword,
            limit=limit,
            offset=0,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        out = {
            "docs": docs_body["items"],
            "docs_total": docs_body["total"],
        }
    if context != "recommend":
        return out

    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    params = {"search_like": f"%{keyword}%", "limit": limit}
    base_filters_sql = f"""
      c.is_visible_in_feed = true
      AND c.published_at IS NOT NULL
      AND coalesce(c.archived, false) = false
      AND c.merged_into IS NULL
      {public_filter}
      {github_filter}
      {categories_filter}
    """
    # BF-0704-6 rev2: 标题优先。concat(title+summary) 的 bitmap recheck 要对每个
    # 匹配行 detoast ai_summary(高频词 2k+ 行,冷缓存 >15s);ai_title 是行内列,
    # recheck 零 TOAST 回表。标题命中不足一页时才用全文摘要补充(稀有词匹配行少)。
    title_where_sql = f"{base_filters_sql} AND c.ai_title ILIKE %(search_like)s"
    supplement_where_sql = (
        f"{base_filters_sql}"
        " AND (coalesce(c.ai_title, '') || ' ' || coalesce(c.ai_summary, '')) ILIKE %(search_like)s"
        " AND NOT (c.ai_title ILIKE %(search_like)s)"
    )
    params["categories"] = categories or []
    cache_key = (
        "context_search_events_total",
        schema,
        keyword,
        bool(public_only),
        user_id or "",
        int(min_github_stars),
        tuple(categories or []),
    )
    degraded_cache_key = (
        "context_search_events_degraded",
        schema,
        keyword,
        bool(public_only),
        user_id or "",
        int(min_github_stars),
        tuple(categories or []),
    )
    if events_only_recommend and _cache_get_with_ttl(
        degraded_cache_key,
        CONTEXT_SEARCH_EVENTS_DEGRADED_TTL_SEC,
    ):
        out["events"] = []
        out["events_total"] = 0
        out["data_backend"] = event_read_backend()
        out["degraded"] = True
        out["degraded_reason"] = "context_search_events_unavailable"
        return out
    def _events_search_sql(where_clause: str) -> str:
        return f"""SELECT c.id, c.ai_title, c.ai_summary, c.doc_count,
                           c.unique_source_count, c.first_doc_at, c.last_doc_at,
                           c.platforms_json,
                           COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                           c.live_version
                      FROM {schema}.clusters c
                      LEFT JOIN LATERAL (
                        SELECT i.cover_url
                          FROM {schema}.cluster_items ci
                          JOIN {schema}.items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND NULLIF(i.cover_url, '') IS NOT NULL
                           AND i.platform <> 'manual'
                           AND i.user_id IS NULL
                         ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                                  ci.rank_in_cluster ASC NULLS LAST
                         LIMIT 1
                      ) event_cover ON true
                     WHERE {where_clause}
                     ORDER BY c.first_doc_at DESC NULLS LAST,
                              c.last_updated_at DESC NULLS LAST,
                              c.id DESC
                     LIMIT %(limit)s"""

    try:
        with connect() as conn:
            statement_timeout_ms = (
                _context_search_events_only_statement_timeout_ms()
                if events_only_recommend
                else None
            )
            if not _set_context_search_timeouts(conn, statement_timeout_ms=statement_timeout_ms):
                raise RemoteDBError("context search timeout setup failed")
            rows = list(conn.execute(_events_search_sql(title_where_sql), params).fetchall())
            supplement_rows: list = []
            if len(rows) < limit:
                # 标题命中不足一页 → 稀有词,全文摘要补充便宜;补充失败只丢补充,不整体降级
                try:
                    supplement_params = dict(params)
                    supplement_params["limit"] = limit - len(rows)
                    supplement_rows = list(
                        conn.execute(
                            _events_search_sql(supplement_where_sql), supplement_params
                        ).fetchall()
                    )
                except Exception as supp_exc:
                    print(
                        f"[warn] context search summary supplement failed (q={keyword!r}): {supp_exc}"
                    )
                    conn.rollback()
                    if not _set_context_search_timeouts(
                        conn, statement_timeout_ms=statement_timeout_ms
                    ):
                        raise RemoteDBError("context search timeout setup failed")
            if supplement_rows:
                rows = rows + supplement_rows
                # 两段结果各自有序,合并后按同一时间线键重排(ISO 字符串可比,规避 naive/aware 混排)
                rows.sort(
                    key=lambda r: (to_utc_iso(r["first_doc_at"]) or "", int(r["id"])),
                    reverse=True,
                )
                rows = rows[:limit]
            total = _cache_get(cache_key)
            if total is None:
                # BF-0704-6: total 只做展示。封顶 count 且只按标题算(行内列,零 TOAST),
                # 补充命中数直接累加;全量 concat count 冷缓存要回表数千行,不再使用。
                count_params = dict(params)
                count_params["count_cap"] = CONTEXT_SEARCH_EVENTS_TOTAL_CAP
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM (SELECT 1 FROM {schema}.clusters c "
                    f"WHERE {title_where_sql} LIMIT %(count_cap)s) capped",
                    count_params,
                ).fetchone()
                total = int(total_row["n"] if total_row else 0)
                if total < CONTEXT_SEARCH_EVENTS_TOTAL_CAP:
                    total += len(supplement_rows)
                _cache_set(cache_key, total)
            source_metadata = _fetch_event_source_metadata(conn, schema, [int(row["id"]) for row in rows])
    except Exception as exc:
        print(f"[warn] context search events query failed (q={keyword!r}): {exc}")
        if events_only_recommend:
            _cache_set_with_ttl(
                degraded_cache_key,
                True,
                CONTEXT_SEARCH_EVENTS_DEGRADED_TTL_SEC,
            )
        out["events"] = []
        out["events_total"] = 0
        out["data_backend"] = event_read_backend()
        out["degraded"] = True
        out["degraded_reason"] = "context_search_events_unavailable"
        return out
    out["events"] = [_row_to_event(dict(row), source_metadata=source_metadata) for row in rows]
    out["events_total"] = int(total)
    return out




def _can_use_sections_mv_fast_path(
    *,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
) -> bool:
    if user_id is not None or search or manual_owner_user_id:
        return False
    if per_category is None:
        return False
    try:
        return int(per_category) <= 50
    except (TypeError, ValueError):
        return False


def _feed_sections_local_cache_name(
    *,
    per_category: int | None,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return f"feed_sections_per={per_category or 'all'}_public={int(public_only)}_stars={int(min_github_stars)}"


def _section_category_from_row(row: dict[str, Any]) -> str:
    categories = [
        str(cat).strip()
        for cat in _json_array(row.get("ai_categories"))
        if str(cat).strip()
    ]
    if categories:
        return categories[0]
    single = canonicalize_category(row.get("ai_category"))
    if single and single != "other":
        return single
    return "_uncategorized"


def _sections_from_mv_rows(
    rows: list[Any],
    *,
    per_category: int | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    sections: dict[str, list[dict[str, Any]]] = {}
    cat_counts: dict[str, int] = {}
    limit = max(1, int(per_category)) if per_category is not None else None

    for row in rows:
        raw = dict(row)
        category = _section_category_from_row(raw)
        cat_counts[category] = cat_counts.get(category, 0) + 1
        bucket = sections.setdefault(category, [])
        if limit is None or len(bucket) < limit:
            raw["section_category"] = category
            bucket.append(_normalize_item(raw))
    return sections, cat_counts


def _section_counts_from_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
    category_expr: str,
) -> dict[str, int]:
    rows = conn.execute(
        f"""SELECT {category_expr} AS section_category, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY 1
             ORDER BY cnt DESC""",
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        data = dict(row)
        category = data.get("section_category") or data.get("category") or "_uncategorized"
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            counts[category] = cnt
    return counts


def query_feed_sections(
    *,
    per_category: int | None = 50,
    search: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 Spec-2.5（rev1, 2026-05-15）: 与 query_feed_platforms 同一份双字段
    # OR AI 过滤口径（D3 决策），保证「按频道」/「按分类」两个视角看到同一批
    # 数据。原 `ai_categories IS NOT NULL` 单字段严格过滤会丢长尾 multi-tag
    # 数据 + 与 platforms 入口口径不一致；改成 OR 后分组键 fallback 到
    # ai_category 单字段（COALESCE 表达式扩展处理）。
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    # v18.0 Spec-2.5: 分组键优先 multi-tag ai_categories[0]，缺失时 fallback
    # 到单字段 ai_category（OR 过滤后允许此分支），仍空兜底 _uncategorized。
    category_expr = _section_category_expr("i")
    live_overlay_enabled = bool(_info_live_overlay_enabled())
    result_cache_ttl = _feed_result_cache_lookup_ttl(live_overlay_enabled=live_overlay_enabled)
    result_cache_key = (
        "feed_sections_result",
        schema,
        per_category,
        search or "",
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        live_overlay_enabled,
        int(_info_live_overlay_limit()),
        int(_info_live_overlay_per_scope_limit()),
    )
    cached_result = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
    if cached_result is not None:
        return cached_result
    def _compute_read_model() -> dict[str, Any] | None:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        if search:
            return _query_feed_sections_search_read_model(
                schema=schema,
                per_category=per_category,
                search=search,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
        return _query_feed_sections_read_model(
            schema=schema,
            per_category=per_category,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )

    read_model_result = _singleflight_sync(
        ("feed_sections_read_model", *result_cache_key),
        _compute_read_model,
    )
    if read_model_result is not None:
        cache_ttl = _feed_result_cache_ttl(read_model_result)
        if cache_ttl > 0:
            return _cache_set_copy_with_ttl(result_cache_key, read_model_result, cache_ttl)
        return read_model_result
    if search and _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return _degraded_feed_sections_result("info_search_read_model_unavailable")
    use_sections_fast_path = _can_use_sections_mv_fast_path(
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    )
    if _remote_feed_live_circuit_open():
        if use_sections_fast_path:
            fallback = _read_local_read_cache(
                _feed_sections_local_cache_name(
                    per_category=per_category,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
        return _degraded_feed_sections_result()
    # BF-0515-singleflight: dedupe concurrent cache-miss → only 1 thread queries Supabase.
    # Other concurrent callers wait on threading.Event and share the same result.
    # Re-check cache inside compute_fn in case singleflight wait yielded a winner.
    def _compute() -> dict:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        snapshot_key = None
        if search is None and not user_id:
            snapshot_key = _sections_snapshot_key(
                per_category=per_category,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
        live_conn = None
        try:
            with connect() as conn:
                live_conn = conn
                _set_short_statement_timeout(conn, _remote_feed_live_timeout_ms())
                item_params = dict(params)
                status_join, status_params, status_alias = _item_status_join(schema, user_id)
                item_params.update(status_params)
                limit_sql = ""
                if per_category is not None:
                    item_params["per_category"] = max(1, int(per_category))
                    limit_sql = "WHERE rn <= %(per_category)s"
                rows = conn.execute(
                    f"""WITH ranked AS (
                           SELECT {_feed_cols(status_alias)},
                                  {category_expr} AS section_category,
                                  row_number() OVER (
                                    PARTITION BY {category_expr}
                                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                             i.fetched_at DESC NULLS LAST,
                                             i.relevance_score DESC NULLS LAST
                                  ) AS rn
                             FROM {schema}.items i
                             {status_join}
                             {_where_sql(where)}
                         )
                         SELECT * FROM ranked
                         {limit_sql}
                         ORDER BY section_category,
                                  COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                  fetched_at DESC NULLS LAST,
                                  relevance_score DESC NULLS LAST""",
                    item_params,
                ).fetchall()
                sections = {}
                for row in rows:
                    raw = dict(row)
                    category = raw.get("section_category") or "_uncategorized"
                    sections.setdefault(category, []).append(_normalize_item(raw))
                cat_counts = _section_counts_from_items(conn, schema, where, params, category_expr)
                result = {
                    "sections": sections,
                    "total": sum(cat_counts.values()),
                    "cat_counts": cat_counts,
                    "personalized": False,
                    "data_backend": feed_read_backend(),
                    "overview_generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sample_limit": int(per_category) if per_category is not None else None,
                }
                if snapshot_key:
                    _write_local_read_cache_async(
                        _feed_sections_local_cache_name(
                            per_category=per_category,
                            public_only=public_only,
                            min_github_stars=min_github_stars,
                        ),
                        result,
                    )
        except Exception:
            if live_conn is not None:
                _rollback_safely(live_conn)
            raise RemoteDBError("sections live path failed")
        return _cache_set_copy(result_cache_key, result)

    try:
        return _singleflight_sync(result_cache_key, _compute)
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        if use_sections_fast_path:
            fallback = _read_local_read_cache(
                _feed_sections_local_cache_name(
                    per_category=per_category,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
            return _degraded_feed_sections_result()
        return _degraded_feed_sections_result()


def query_feed_by_category(
    *,
    category: str,
    keyword: str | None = None,
    search: str | None = None,
    subcategory: str | None = None,
    offset: int = 0,
    limit: int = 50,
    cursor: dict[str, Any] | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    if not keyword and not search and manual_owner_user_id:
        union_page = _query_feed_by_category_private_manual_union_page(
            schema=schema,
            category=category,
            subcategory=subcategory,
            offset=safe_offset,
            limit=safe_limit,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if union_page is not None:
            return union_page
    if search:
        search_read_model_result = _query_feed_by_category_search_read_model(
            schema=schema,
            category=category,
            keyword=keyword,
            search=search,
            subcategory=subcategory,
            offset=safe_offset,
            limit=safe_limit,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if search_read_model_result is not None:
            return search_read_model_result
        if (
            not keyword
            and not manual_owner_user_id
            and _can_use_info_search_read_model(
                search=search,
                user_id=user_id,
                public_only=public_only,
                min_github_stars=min_github_stars,
            )
        ):
            return _degraded_feed_category_result(
                category,
                "info_search_read_model_unavailable",
            )
    read_model_result = _query_feed_by_category_read_model(
        schema=schema,
        category=category,
        keyword=keyword,
        search=search,
        subcategory=subcategory,
        offset=safe_offset,
        limit=safe_limit,
        cursor=cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    if read_model_result is not None:
        return read_model_result
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    if category == UNCATEGORIZED_SENTINEL:
        params["category"] = UNCATEGORIZED_SENTINEL
        where.append(f"{category_expr} = %(category)s")
    else:
        category_ids = expand_query_categories(category)
        params["category_ids"] = category_ids
        where.append(f"{category_expr} = ANY(%(category_ids)s)")
    _add_search_filter(where, params, search, param_key="global_search_like")
    _add_search_filter(where, params, keyword, param_key="keyword_search_like")
    if subcategory:
        params["subcategory"] = subcategory
        where.append(
            """EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
              WHERE subcat.value = %(subcategory)s
            )"""
        )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    count_cache_key = (
        "feed_category_count",
        schema,
        category,
        keyword or "",
        search or "",
        subcategory or "",
        bool(public_only),
        manual_owner_user_id or "",
        user_id or "",
        int(min_github_stars),
    )
    if _remote_feed_live_circuit_open():
        return _degraded_feed_category_result(category)
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _remote_feed_live_timeout_ms())
            count = _cache_get(count_cache_key)
            if count is None:
                count_params = dict(params)
                count_join, count_status_params, _ = _item_status_join(schema, user_id)
                count_params.update(count_status_params)
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM {schema}.items i {count_join} {_where_sql(where)}",
                    count_params,
                ).fetchone()
                count = int(total_row["n"] if total_row else 0)
                _cache_set(count_cache_key, count)
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=safe_offset,
                status_user_id=user_id,
            )
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        return _degraded_feed_category_result(category)
    return {
        "items": items,
        "category": category,
        "total": int(count),
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": safe_offset + len(items) < int(count),
        "next_offset": (
            safe_offset + len(items)
            if safe_offset + len(items) < int(count)
            else None
        ),
        "data_backend": feed_read_backend(),
    }


def _category_counts_for_all_platforms(
    conn: Any,
    schema: str,
    *,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    search: str | None = None,
) -> dict[str, dict[str, int]]:
    cache_key = (
        "platform_category_counts",
        schema,
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        search or "",
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 PRD §Spec-2: category counts 必须与 query_feed_platforms 同口径
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    cat_rows = conn.execute(
        f"""SELECT i.platform, cat.value AS category, count(DISTINCT i.id) AS cnt
              FROM {schema}.items i
              CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
              {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
             GROUP BY i.platform, cat.value
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    null_rows = conn.execute(
        f"""SELECT i.platform, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where + ["i.ai_categories IS NULL"])}
             GROUP BY i.platform""",
        params,
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in cat_rows:
        platform = row["platform"] or "_unknown"
        counts.setdefault(platform, {})[row["category"]] = int(row["cnt"] or 0)
    for row in null_rows:
        platform = row["platform"] or "_unknown"
        cnt = int(row["cnt"] or 0)
        if cnt > 0:
            counts.setdefault(platform, {})[UNCATEGORIZED_SENTINEL] = cnt
    return _cache_set(cache_key, counts)


def _can_use_platforms_mv_fast_path(
    *,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
) -> bool:
    """Return whether /api/feed/platforms can use the MV-only first paint path."""
    if user_id is not None or search or manual_owner_user_id:
        return False
    if per_platform is None:
        return False
    try:
        return int(per_platform) <= 50
    except (TypeError, ValueError):
        return False


def _feed_platforms_local_cache_name(
    *,
    per_platform: int | None,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return f"feed_platforms_per={per_platform or 'all'}_public={int(public_only)}_stars={int(min_github_stars)}"


def _with_degraded_reason(result: dict[str, Any], reason: str | None) -> dict[str, Any]:
    if reason:
        result["degraded_reason"] = reason
    return result


def _degraded_feed_platforms_result(reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "sections": {},
        "platform_counts": {},
        "source_counts": {},
        "category_counts": {},
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_sections_result(reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "sections": {},
        "total": 0,
        "cat_counts": {},
        "personalized": False,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_category_result(category: str, reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "items": [],
        "category": category,
        "total": 0,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_platform_page_result(platform: str, *, category: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "items": [],
        "platform": platform,
        "category": category,
        "total": 0,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _platform_overview_counts_from_rows(
    rows: list[Any],
) -> tuple[
    dict[str, int],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """Derive lightweight overview counts from MV rows.

    This intentionally counts only the precomputed top rows. It keeps the
    anonymous information page usable even when full-table remote aggregations
    are slow or stuck.
    """
    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

        source = data.get("source") or ""
        platform_source_counts = source_counts.setdefault(platform, {})
        platform_source_counts[source] = platform_source_counts.get(source, 0) + 1

        categories = [
            str(cat).strip()
            for cat in _json_array(data.get("ai_categories"))
            if str(cat).strip()
        ]
        if not categories:
            single = canonicalize_category(data.get("ai_category"))
            if single and single != "other":
                categories = [single]
            else:
                categories = [UNCATEGORIZED_SENTINEL]
        for category in categories:
            platform_category_counts = category_counts.setdefault(platform, {})
            platform_category_counts[category] = platform_category_counts.get(category, 0) + 1
    return platform_counts, source_counts, category_counts


def _count_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
) -> int:
    row = conn.execute(
        f"SELECT count(*) AS n FROM {schema}.items i {_where_sql(where)}",
        params,
    ).fetchone()
    return int((row or {}).get("n") or 0)


def _platform_page_count_cache_key(
    *,
    schema: str,
    platform: str,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
    search: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> tuple[Any, ...]:
    return (
        "feed_platform_page_count",
        schema,
        platform,
        source or "",
        group or "",
        category or "",
        search or "",
        bool(public_only),
        manual_owner_user_id or "",
        user_id or "",
        int(min_github_stars),
    )


def _warm_platform_page_count_cache(
    *,
    schema: str,
    platform_counts: dict[str, int],
    source_counts: dict[str, dict[str, int]],
    category_counts: dict[str, dict[str, int]],
    search: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    user_id: str | None,
    min_github_stars: int,
) -> None:
    for platform, total in platform_counts.items():
        _cache_set(
            _platform_page_count_cache_key(
                schema=schema,
                platform=platform,
                search=search,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                user_id=user_id,
                min_github_stars=min_github_stars,
            ),
            int(total),
        )
    for platform, per_source in source_counts.items():
        for source, total in per_source.items():
            _cache_set(
                _platform_page_count_cache_key(
                    schema=schema,
                    platform=platform,
                    source=source,
                    search=search,
                    public_only=public_only,
                    manual_owner_user_id=manual_owner_user_id,
                    user_id=user_id,
                    min_github_stars=min_github_stars,
                ),
                int(total),
            )
    for platform, per_category in category_counts.items():
        for category, total in per_category.items():
            _cache_set(
                _platform_page_count_cache_key(
                    schema=schema,
                    platform=platform,
                    category=category,
                    search=search,
                    public_only=public_only,
                    manual_owner_user_id=manual_owner_user_id,
                    user_id=user_id,
                    min_github_stars=min_github_stars,
                ),
                int(total),
            )


def _estimate_platform_page_total(*, offset: int, limit: int, item_count: int) -> int:
    loaded_until = offset + item_count
    if item_count >= limit:
        return loaded_until + 1
    return loaded_until


def _platform_overview_counts_from_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
) -> tuple[
    dict[str, int],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """Aggregate full overview counts from the live items table.

    The cards query can stay capped at 50 per platform, but these counts are
    user-facing totals and must not be derived from that capped card sample.
    """
    platform_rows = conn.execute(
        f"""SELECT i.platform, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY i.platform
             ORDER BY cnt DESC""",
        params,
    ).fetchall()
    source_rows = conn.execute(
        f"""SELECT i.platform, i.source, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY i.platform, i.source
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    category_rows = conn.execute(
        f"""SELECT i.platform, cat.value AS category, count(DISTINCT i.id) AS cnt
              FROM {schema}.items i
              CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
              {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
             GROUP BY i.platform, cat.value
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    null_category_rows = conn.execute(
        f"""SELECT i.platform,
                   CASE
                     WHEN i.ai_category IS NOT NULL AND i.ai_category != 'other'
                     THEN i.ai_category
                     ELSE %(uncategorized)s
                   END AS category,
                   count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where + ["i.ai_categories IS NULL"])}
             GROUP BY i.platform, category
             ORDER BY i.platform, cnt DESC""",
        {**params, "uncategorized": UNCATEGORIZED_SENTINEL},
    ).fetchall()

    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}

    for row in platform_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            platform_counts[platform] = cnt

    for row in source_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        source = data.get("source") or ""
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            source_counts.setdefault(platform, {})[source] = cnt

    for row in list(category_rows) + list(null_category_rows):
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            category_counts.setdefault(platform, {})[category] = (
                category_counts.setdefault(platform, {}).get(category, 0) + cnt
            )

    return platform_counts, source_counts, category_counts


def _section_category_expr(item_alias: str = "i") -> str:
    """Primary category expression for the 信息 tab type sections."""
    alias = item_alias.strip() or "i"
    raw = (
        "COALESCE("
        f"{alias}.ai_categories ->> 0,"
        f" CASE WHEN {alias}.ai_category IS NOT NULL AND {alias}.ai_category != 'other'"
        f" THEN {alias}.ai_category ELSE NULL END,"
        f" '{UNCATEGORIZED_SENTINEL}'"
        ")"
    )
    return (
        "CASE "
        f"WHEN {raw} IN ('ai_tools', 'tools') THEN 'efficiency_tools' "
        f"WHEN {raw} = 'insights' THEN 'tech' "
        f"ELSE {raw} "
        "END"
    )


def _max_fetched_at(items: list[dict[str, Any]]) -> str | None:
    timestamps = [item.get("fetched_at") for item in items if item.get("fetched_at")]
    if not timestamps:
        return None
    return max(timestamps, key=sort_key)


def _info_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(INFO_READ_MODEL_ENV))


def _info_scope_key(*, platform: str, dimension: str, value: str | None = None) -> str:
    return f"platform={platform}|dimension={dimension}|value={value or ''}"


INFO_SCOPE_COMPOUND_SEPARATOR = "::"


def _info_section_subcategory_value(category: str, subcategory: str) -> str:
    return f"{category}{INFO_SCOPE_COMPOUND_SEPARATOR}{subcategory}"


def _info_group_source_value(group: str, source: str) -> str:
    return f"{group}{INFO_SCOPE_COMPOUND_SEPARATOR}{source}"


def _split_info_compound_value(value: str) -> tuple[str, str]:
    left, sep, right = str(value or "").partition(INFO_SCOPE_COMPOUND_SEPARATOR)
    return left, right if sep else ""


def _info_read_model_page_cache_key(
    *,
    schema: str,
    platform: str,
    source: str | None,
    group: str | None,
    category: str | None,
    offset: int,
    limit: int,
    exclude_ids: list[str] | None = None,
    version_id: str | None = None,
) -> tuple[Any, ...]:
    return (
        "info_read_model_platform_page",
        schema,
        version_id or "",
        platform,
        source or "",
        group or "",
        category or "",
        int(offset),
        int(limit),
        tuple(exclude_ids or []),
    )


def _info_read_model_section_category_page_cache_key(
    *,
    schema: str,
    category: str,
    subcategory: str | None = None,
    offset: int,
    limit: int,
    version_id: str | None = None,
) -> tuple[Any, ...]:
    return (
        "info_read_model_section_category_page",
        schema,
        version_id or "",
        category,
        subcategory or "",
        int(offset),
        int(limit),
    )


def _normalize_info_read_model_cursor(
    cursor: Any,
    *,
    expected_scope_key: str,
) -> dict[str, Any] | None:
    if not isinstance(cursor, dict):
        return None
    version_id = str(cursor.get("version_id") or "").strip()
    scope_key = str(cursor.get("scope_key") or "").strip()
    if not version_id or scope_key != expected_scope_key:
        return None
    try:
        rank_after = int(cursor.get("rank_after"))
    except (TypeError, ValueError):
        return None
    if rank_after < 0:
        return None
    clean_exclude_ids: list[str] = []
    for raw_item_id in cursor.get("exclude_ids") or []:
        item_id = str(raw_item_id or "").strip()
        if item_id and item_id not in clean_exclude_ids:
            clean_exclude_ids.append(item_id)
        if len(clean_exclude_ids) >= 200:
            break
    normalized = {
        "version_id": version_id,
        "scope_key": scope_key,
        "rank_after": rank_after,
    }
    if clean_exclude_ids:
        normalized["exclude_ids"] = clean_exclude_ids
    return normalized


def _info_read_model_next_cursor(
    *,
    version_id: Any,
    scope_key: str,
    rank_after: int,
    total_count: int,
    exclude_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    if not version_id or not scope_key:
        return None
    try:
        rank_value = max(0, int(rank_after))
        total_value = max(0, int(total_count))
    except (TypeError, ValueError):
        return None
    if rank_value >= total_value:
        return None
    clean_exclude_ids: list[str] = []
    for raw_item_id in exclude_ids or []:
        item_id = str(raw_item_id or "").strip()
        if item_id and item_id not in clean_exclude_ids:
            clean_exclude_ids.append(item_id)
        if len(clean_exclude_ids) >= 200:
            break
    cursor = {
        "version_id": str(version_id),
        "scope_key": scope_key,
        "rank_after": rank_value,
    }
    if clean_exclude_ids:
        cursor["exclude_ids"] = clean_exclude_ids
    return cursor


def _item_ids_for_cursor(items: list[dict[str, Any]]) -> list[str]:
    item_ids: list[str] = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id not in item_ids:
            item_ids.append(item_id)
        if len(item_ids) >= 200:
            break
    return item_ids


def _info_platform_scope(
    *,
    platform: str,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
) -> tuple[str, str, str] | None:
    if source and group and not category:
        value = _info_group_source_value(str(group), str(source))
        return "group_source", value, _info_scope_key(platform=platform, dimension="group_source", value=value)
    active_filters = [
        ("source", source),
        ("group", group),
        ("category", category),
    ]
    chosen = [(dimension, value) for dimension, value in active_filters if value]
    if len(chosen) > 1:
        return None
    if not chosen:
        dimension, value = "all", ""
    else:
        dimension, value = chosen[0]
    return dimension, str(value or ""), _info_scope_key(platform=platform, dimension=dimension, value=value)


def _can_use_info_read_model(
    *,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> bool:
    if not _info_read_model_enabled():
        return False
    if search:
        return False
    if not public_only and not user_id:
        return False
    return int(min_github_stars) == INFO_READ_MODEL_MIN_GITHUB_STARS


def _can_use_info_search_read_model(
    *,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    min_github_stars: int,
) -> bool:
    if not search:
        return False
    if not _info_read_model_enabled():
        return False
    if not public_only and not user_id:
        return False
    return int(min_github_stars) == INFO_READ_MODEL_MIN_GITHUB_STARS


def _read_model_card_search_sql(
    *,
    card_expr: str = "ci.card_json",
    param_key: str = "search_like",
    search_text_expr: str | None = None,
) -> str:
    if search_text_expr:
        return f"{search_text_expr} ILIKE %({param_key})s"
    return (
        f"(coalesce({card_expr} ->> 'title', '') || ' ' || "
        f"coalesce({card_expr} ->> 'author_name', '') || ' ' || "
        f"coalesce({card_expr} ->> 'description', '') || ' ' || "
        f"coalesce({card_expr} ->> 'ai_summary', '') || ' ' || "
        f"coalesce({card_expr} ->> 'ai_keywords', '')) ILIKE %({param_key})s"
    )


def _apply_user_status_overlay(
    *,
    schema: str,
    items: list[dict[str, Any]],
    user_id: str | None,
) -> list[dict[str, Any]]:
    if not user_id or not items:
        return items
    item_ids = [str(item.get("id") or "").strip() for item in items if str(item.get("id") or "").strip()]
    if not item_ids:
        return items
    try:
        with connect() as conn:
            rows = conn.execute(
                f"""SELECT item_id, read_at, clicked_at, starred_at, hidden_at
                      FROM {schema}.item_status
                     WHERE user_id = %(status_user_id)s
                       AND item_id = ANY(%(item_ids)s)""",
                {"status_user_id": user_id, "item_ids": item_ids},
            ).fetchall()
    except Exception:
        return items
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        item_id = str(data.get("item_id") or "")
        if item_id:
            by_id[item_id] = data
    if not by_id:
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        status = by_id.get(str(item.get("id") or ""))
        if not status:
            out.append(item)
            continue
        next_item = dict(item)
        for key in ("read_at", "clicked_at", "starred_at", "hidden_at"):
            if key in status:
                next_item[key] = _timestamp_value(status.get(key))
        out.append(next_item)
    return out


def _apply_user_status_overlay_to_sections(
    *,
    schema: str,
    sections: dict[str, list[dict[str, Any]]],
    user_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    if not user_id or not sections:
        return sections
    flat_items: list[dict[str, Any]] = []
    positions: list[tuple[str, int]] = []
    for section_key, items in sections.items():
        for index, item in enumerate(items):
            flat_items.append(item)
            positions.append((section_key, index))
    overlaid = _apply_user_status_overlay(schema=schema, items=flat_items, user_id=user_id)
    next_sections = {key: list(items) for key, items in sections.items()}
    for (section_key, index), item in zip(positions, overlaid, strict=False):
        next_sections[section_key][index] = item
    return next_sections


def _private_manual_base_where(
    *,
    manual_owner_user_id: str,
    search: str | None = None,
    min_github_stars: int = 50,
) -> tuple[list[str], dict[str, Any]]:
    where = [
        "i.platform = 'manual'",
        "i.user_id = %(manual_owner_user_id)s",
        "i.visible = 1",
    ]
    params: dict[str, Any] = {"manual_owner_user_id": manual_owner_user_id}
    where.extend(_item_display_filter("i", min_github_stars=min_github_stars))
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    return where, params


def _feed_item_sort_key(item: dict[str, Any]) -> tuple[str, str, float, str]:
    sort_at = str(item.get("published_at") or item.get("fetched_at") or item.get("created_at") or "")
    fetched_at = str(item.get("fetched_at") or "")
    try:
        score = float(item.get("relevance_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return sort_at, fetched_at, score, str(item.get("id") or "")


def _merge_overlay_items(
    base_items: list[dict[str, Any]],
    overlay_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not overlay_items:
        return base_items
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    # Overlay rows may be a fresher copy of the same item already present in the
    # read-model card JSON. Keep the live row so fetched_at/order can advance.
    for item in [*overlay_items, *base_items]:
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        merged.append(item)
    merged.sort(key=_feed_item_sort_key, reverse=True)
    return merged[:limit]


def _private_manual_category_where(
    *,
    category: str,
    subcategory: str | None,
    manual_owner_user_id: str,
    search: str | None,
    min_github_stars: int,
) -> tuple[list[str], dict[str, Any]]:
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    if category == UNCATEGORIZED_SENTINEL:
        params["category"] = UNCATEGORIZED_SENTINEL
        where.append(f"{category_expr} = %(category)s")
    else:
        params["category_ids"] = expand_query_categories(category)
        where.append(f"{category_expr} = ANY(%(category_ids)s)")
    clean_subcategory = str(subcategory or "").strip()
    if clean_subcategory:
        params["subcategory"] = clean_subcategory
        where.append(
            """EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(i.ai_subcategories) AS subcat(value)
              WHERE subcat.value = %(subcategory)s
            )"""
        )
    return where, params


def _query_feed_by_category_private_manual_union_page(
    *,
    schema: str,
    category: str,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id:
        return None
    raw_category = (category or "").strip()
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    window_limit = safe_offset + safe_limit
    where, params = _private_manual_category_where(
        category=cache_category,
        subcategory=clean_subcategory,
        manual_owner_user_id=manual_owner_user_id,
        search=None,
        min_github_stars=min_github_stars,
    )
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            total_row = conn.execute(
                f"SELECT count(*) AS cnt FROM {schema}.items i {_where_sql(where)}",
                params,
            ).fetchone()
            private_total = int(dict(total_row).get("cnt") or 0) if total_row else 0
            if private_total <= 0:
                return None
            private_items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=window_limit,
                offset=0,
                status_user_id=user_id,
            )
    except Exception:
        return None

    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    base_cursor = (
        {**cursor_state, "rank_after": 0}
        if cursor_state
        else None
    )
    public_page = _query_feed_by_category_read_model(
        schema=schema,
        category=category,
        keyword=None,
        search=None,
        subcategory=clean_subcategory,
        offset=0,
        limit=window_limit,
        cursor=base_cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
        max_limit=window_limit,
    )
    if public_page is None:
        return None
    public_items = list(public_page.get("items") or [])
    merged = _merge_overlay_items(public_items, private_items, limit=window_limit)
    page_items = merged[safe_offset:safe_offset + safe_limit]
    total = int(public_page.get("total") or 0) + private_total
    next_offset = safe_offset + len(page_items)
    result = dict(public_page)
    result.update({
        "items": page_items,
        "category": category,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "next_cursor": None,
        "private_manual_overlay": True,
        "private_manual_overlay_page": True,
    })
    return result


def _query_private_manual_sections_overlay(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id or per_category is None:
        return None
    safe_limit = max(1, min(int(per_category), 200))
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            count_rows = conn.execute(
                f"""SELECT {category_expr} AS category, count(*) AS cnt
                      FROM {schema}.items i
                      {_where_sql(where)}
                     GROUP BY 1""",
                params,
            ).fetchall()
            status_join, status_params, status_alias = _item_status_join(schema, user_id)
            item_params = dict(params)
            item_params.update(status_params)
            item_params["limit"] = safe_limit
            item_rows = conn.execute(
                f"""WITH ranked AS (
                       SELECT {category_expr} AS section_category,
                              {_feed_cols(status_alias, include_heavy_json=False)},
                              row_number() OVER (
                                PARTITION BY {category_expr}
                                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                         i.fetched_at DESC NULLS LAST,
                                         i.relevance_score DESC NULLS LAST,
                                         i.id DESC
                              ) AS rn
                         FROM {schema}.items i
                         {status_join}
                         {_where_sql(where)}
                     )
                     SELECT *
                       FROM ranked
                      WHERE rn <= %(limit)s
                      ORDER BY section_category, rn""",
                item_params,
            ).fetchall()
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    for row in count_rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            cat_counts[category] = cnt
    if not cat_counts:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    for row in item_rows:
        raw = dict(row)
        category = raw.get("section_category") or UNCATEGORIZED_SENTINEL
        sections.setdefault(category, []).append(_normalize_item(raw))
    return {"sections": sections, "cat_counts": cat_counts}


def _merge_private_manual_sections_overlay(
    result: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    if not overlay:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    cat_counts = dict(result.get("cat_counts") or {})
    for category, count in (overlay.get("cat_counts") or {}).items():
        cat_counts[category] = int(cat_counts.get(category) or 0) + int(count or 0)
    for category, items in (overlay.get("sections") or {}).items():
        sections[category] = _merge_overlay_items(sections.get(category, []), list(items), limit=limit)
    out["sections"] = sections
    out["cat_counts"] = cat_counts
    out["total"] = sum(int(value or 0) for value in cat_counts.values())
    out["private_manual_overlay"] = True
    return out


def _query_private_manual_platforms_overlay(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id or per_platform is None:
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            total_row = conn.execute(
                f"SELECT count(*) AS cnt FROM {schema}.items i {_where_sql(where)}",
                params,
            ).fetchone()
            source_rows = conn.execute(
                f"""SELECT COALESCE(i.source, '') AS source, count(*) AS cnt
                      FROM {schema}.items i
                      {_where_sql(where)}
                     GROUP BY 1""",
                params,
            ).fetchall()
            category_rows = conn.execute(
                f"""SELECT cat.value AS category, count(DISTINCT i.id) AS cnt
                      FROM {schema}.items i
                      CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
                      {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
                     GROUP BY cat.value""",
                params,
            ).fetchall()
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=0,
                status_user_id=user_id,
            )
    except Exception:
        return None

    total = int(dict(total_row).get("cnt") or 0) if total_row else 0
    if total <= 0:
        return None
    source_counts = {
        str(dict(row).get("source") or "user-submit"): int(dict(row).get("cnt") or 0)
        for row in source_rows
        if int(dict(row).get("cnt") or 0) > 0
    }
    category_counts = {
        str(dict(row).get("category") or UNCATEGORIZED_SENTINEL): int(dict(row).get("cnt") or 0)
        for row in category_rows
        if int(dict(row).get("cnt") or 0) > 0
    }
    return {
        "sections": {"manual": items},
        "platform_counts": {"manual": total},
        "source_counts": {"manual": source_counts},
        "category_counts": {"manual": category_counts},
    }


def _merge_private_manual_platforms_overlay(
    result: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    if not overlay:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    platform_counts = dict(result.get("platform_counts") or {})
    source_counts = {
        key: dict(value)
        for key, value in (result.get("source_counts") or {}).items()
    }
    category_counts = {
        key: dict(value)
        for key, value in (result.get("category_counts") or {}).items()
    }
    for platform, count in (overlay.get("platform_counts") or {}).items():
        platform_counts[platform] = int(platform_counts.get(platform) or 0) + int(count or 0)
    for platform, items in (overlay.get("sections") or {}).items():
        sections[platform] = _merge_overlay_items(sections.get(platform, []), list(items), limit=limit)
    for platform, counts in (overlay.get("source_counts") or {}).items():
        bucket = source_counts.setdefault(platform, {})
        for key, count in counts.items():
            bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
    for platform, counts in (overlay.get("category_counts") or {}).items():
        bucket = category_counts.setdefault(platform, {})
        for key, count in counts.items():
            bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
    out["sections"] = sections
    out["platform_counts"] = platform_counts
    out["source_counts"] = source_counts
    out["category_counts"] = category_counts
    out["private_manual_overlay"] = True
    return out


def _info_read_model_active_version(conn: Any, schema: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""SELECT v.version_id, v.generated_at, v.max_fetched_at, v.meta_json
              FROM {schema}.info_read_model_state s
              JOIN {schema}.info_read_model_versions v
                ON v.version_id = s.active_version_id
             WHERE s.key = %(state_key)s
               AND v.status = 'complete'""",
        {"state_key": INFO_READ_MODEL_STATE_KEY},
    ).fetchone()
    return dict(row) if row else None


def _item_from_read_model_card(value: Any) -> dict[str, Any] | None:
    data = _json_value(value)
    if not isinstance(data, dict):
        return None
    return _normalize_item(data)


def _info_live_overlay_enabled(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(INFO_READ_MODEL_LIVE_OVERLAY_ENV))


def _info_live_overlay_limit(env: dict[str, str] | None = None) -> int:
    return _env_int(env or _runtime_env(), INFO_READ_MODEL_LIVE_OVERLAY_LIMIT_ENV, 120, min_value=0)


def _info_live_overlay_per_scope_limit(env: dict[str, str] | None = None) -> int:
    return _env_int(env or _runtime_env(), INFO_READ_MODEL_LIVE_OVERLAY_PER_SCOPE_LIMIT_ENV, 20, min_value=1)


def _info_live_overlay_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(env or _runtime_env(), INFO_READ_MODEL_LIVE_OVERLAY_TIMEOUT_MS_ENV, 1500, min_value=250)


def _info_live_overlay_result_cache_ttl(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    ttl = _env_int(values, INFO_READ_MODEL_LIVE_OVERLAY_RESULT_CACHE_TTL_ENV, 30, min_value=0)
    remote_ttl = _remote_cache_ttl(values)
    if ttl <= 0 or remote_ttl <= 0:
        return 0
    return min(ttl, remote_ttl)


def _feed_result_cache_lookup_ttl(*, live_overlay_enabled: bool) -> int:
    if live_overlay_enabled:
        return _info_live_overlay_result_cache_ttl()
    return _remote_cache_ttl()


def _feed_result_cache_ttl(result: dict[str, Any] | None) -> int:
    if not isinstance(result, dict):
        return 0
    if result.get("degraded"):
        return 0
    if result.get("read_model_stale"):
        return 0
    if result.get("live_overlay_enabled"):
        if result.get("live_overlay_error"):
            return 0
        return _info_live_overlay_result_cache_ttl()
    return _remote_cache_ttl()


def _feed_result_cacheable(result: dict[str, Any] | None) -> bool:
    return _feed_result_cache_ttl(result) > 0


def _item_info_categories(item: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for raw in _json_array(item.get("ai_categories")):
        category = canonicalize_category(raw)
        if category and category != "other" and category not in categories:
            categories.append(category)
    if categories:
        return categories
    single = canonicalize_category(item.get("ai_category"))
    if single and single != "other":
        return [single]
    return [UNCATEGORIZED_SENTINEL]


def _max_item_fetched_at(*groups: list[dict[str, Any]]) -> str | None:
    timestamps: list[str] = []
    for items in groups:
        timestamps.extend(str(item.get("fetched_at")) for item in items if item.get("fetched_at"))
    if not timestamps:
        return None
    return max(timestamps, key=sort_key)


def _query_info_live_overlay_items(
    conn: Any,
    schema: str,
    *,
    active_version_id: Any,
    active_max_fetched_at: Any,
    scope_dimension: str,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any]:
    env = _runtime_env()
    enabled = _info_live_overlay_enabled(env)
    limit = _info_live_overlay_limit(env)
    per_scope_limit = min(max(limit, 0), _info_live_overlay_per_scope_limit(env)) if limit > 0 else 0
    meta: dict[str, Any] = {
        "enabled": enabled,
        "attempted": False,
        "limit": limit,
        "per_scope_limit": per_scope_limit,
        "timeout_ms": _info_live_overlay_timeout_ms(env),
        "after": _timestamp_value(active_max_fetched_at),
    }
    if not enabled:
        return {"items": [], "meta": meta, "cacheable": True}
    if limit <= 0 or not active_version_id or not active_max_fetched_at:
        meta["skipped"] = "missing_active_version_or_limit"
        return {"items": [], "meta": meta, "cacheable": False}

    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where.append("i.fetched_at > %(overlay_after)s::timestamptz")
    params["version_id"] = str(active_version_id)
    params["overlay_after"] = _timestamp_value(active_max_fetched_at) or str(active_max_fetched_at)
    params["overlay_limit"] = limit
    params["overlay_per_scope_limit"] = per_scope_limit
    status_join, status_params, status_alias = _item_status_join(schema, user_id)
    params.update(status_params)
    section_expr = _section_category_expr("i")
    if scope_dimension == "platform":
        partition_expr = "recent.platform"
    elif scope_dimension == "section_category":
        partition_expr = "recent.section_category"
    else:
        meta["skipped"] = "unsupported_scope_dimension"
        return {"items": [], "meta": meta, "cacheable": False}

    try:
        meta["attempted"] = True
        if not _set_info_read_model_timeouts(
            conn,
            statement_timeout_ms=_info_live_overlay_timeout_ms(env),
        ):
            meta["fallback_reason"] = "info_live_overlay_timeout_setup_failed"
            return {"items": [], "meta": meta, "cacheable": False}
        rows = conn.execute(
            f"""WITH recent AS MATERIALIZED (
                    SELECT {_feed_cols(status_alias, include_heavy_json=False)},
                           {section_expr} AS section_category
                      FROM {schema}.items i
                      {status_join}
                     {_where_sql(where)}
                     ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                              i.fetched_at DESC NULLS LAST,
                              i.relevance_score DESC NULLS LAST,
                              i.id DESC
                     LIMIT %(overlay_limit)s
                ),
                ranked AS (
                    SELECT recent.*,
                           row_number() OVER (
                               PARTITION BY {partition_expr}
                               ORDER BY COALESCE(recent.published_at, recent.fetched_at) DESC NULLS LAST,
                                        recent.fetched_at DESC NULLS LAST,
                                        recent.relevance_score DESC NULLS LAST,
                                        recent.id DESC
                           ) AS overlay_rn
                      FROM recent
                )
                SELECT *
                 FROM ranked
                 WHERE overlay_rn <= %(overlay_per_scope_limit)s
                 ORDER BY COALESCE(published_at, fetched_at) DESC NULLS LAST,
                          fetched_at DESC NULLS LAST,
                          relevance_score DESC NULLS LAST,
                          id DESC""",
            params,
        ).fetchall()
        # End the read-only transaction before Python-side normalization and
        # response merging. Under client timeouts, keeping the transaction open
        # here can strand Supavisor sessions as idle-in-transaction.
        _commit_safely(conn)
    except Exception as exc:
        _rollback_safely(conn)
        meta["error"] = str(exc)[:200]
        meta["fallback_reason"] = "info_live_overlay_failed"
        return {"items": [], "meta": meta, "cacheable": False}

    items: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data.pop("overlay_rn", None)
        category = data.get("section_category") or _section_category_from_row(data)
        item = _normalize_item(data)
        item["section_category"] = category
        items.append(item)
    meta["count"] = len(items)
    meta["latest_fetched_at"] = _max_item_fetched_at(items)
    return {"items": items, "meta": meta, "cacheable": False}


def _attach_info_live_overlay_meta(
    result: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    meta = overlay.get("meta") if isinstance(overlay, dict) else None
    if not isinstance(meta, dict) or not meta.get("enabled"):
        return result
    out = dict(result)
    out["live_overlay_enabled"] = True
    out["live_overlay"] = bool(out.get("live_overlay"))
    out["live_overlay_count"] = int(out.get("live_overlay_count") or meta.get("count") or 0)
    out["live_overlay_after"] = meta.get("after")
    out["live_overlay_limit"] = meta.get("limit")
    out["live_overlay_per_scope_limit"] = meta.get("per_scope_limit")
    out["live_overlay_timeout_ms"] = meta.get("timeout_ms")
    out["live_overlay_attempted"] = bool(meta.get("attempted"))
    if meta.get("latest_fetched_at"):
        out["live_overlay_latest_fetched_at"] = meta.get("latest_fetched_at")
    if meta.get("skipped"):
        out["live_overlay_skipped"] = meta.get("skipped")
    if meta.get("error"):
        out["live_overlay_error"] = meta.get("error")
        out["fallback_reason"] = meta.get("fallback_reason")
    return out


def _merge_info_live_overlay_platforms(
    result: dict[str, Any],
    overlay_items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not overlay_items:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    platform_counts = dict(result.get("platform_counts") or {})
    source_counts = {
        key: dict(value)
        for key, value in (result.get("source_counts") or {}).items()
    }
    category_counts = {
        key: dict(value)
        for key, value in (result.get("category_counts") or {}).items()
    }
    platform_next_cursors = dict(result.get("platform_next_cursors") or {})
    base_ids_by_platform = {
        key: {str(item.get("id") or "") for item in items if item.get("id")}
        for key, items in sections.items()
    }
    existing_ids = {
        str(item.get("id") or "")
        for items in sections.values()
        for item in items
        if item.get("id")
    }
    applied: list[dict[str, Any]] = []
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for item in overlay_items:
        item_id = str(item.get("id") or "")
        is_duplicate = bool(item_id and item_id in existing_ids)
        if item_id and not is_duplicate:
            existing_ids.add(item_id)
        platform = item.get("platform") or "_unknown"
        source = item.get("source") or ""
        by_platform.setdefault(platform, []).append(item)
        if not is_duplicate:
            platform_counts[platform] = int(platform_counts.get(platform) or 0) + 1
            source_bucket = source_counts.setdefault(platform, {})
            source_bucket[source] = int(source_bucket.get(source) or 0) + 1
            category_bucket = category_counts.setdefault(platform, {})
            for category in _item_info_categories(item):
                category_bucket[category] = int(category_bucket.get(category) or 0) + 1
        applied.append(item)

    if not applied:
        return result
    for platform, items in by_platform.items():
        sections[platform] = _merge_overlay_items(sections.get(platform, []), items, limit=limit)
    for platform, cursor in list(platform_next_cursors.items()):
        if not cursor:
            continue
        base_ids = base_ids_by_platform.get(platform, set())
        retained = sum(
            1
            for item in sections.get(platform, [])
            if str(item.get("id") or "") in base_ids
        )
        platform_next_cursors[platform] = _info_read_model_next_cursor(
            version_id=cursor.get("version_id"),
            scope_key=cursor.get("scope_key"),
            rank_after=retained,
            total_count=int(platform_counts.get(platform) or 0),
            exclude_ids=_item_ids_for_cursor(sections.get(platform, [])),
        )

    out["sections"] = sections
    out["platform_counts"] = platform_counts
    out["source_counts"] = source_counts
    out["category_counts"] = category_counts
    if platform_next_cursors:
        out["platform_next_cursors"] = platform_next_cursors
    out["overview_max_fetched_at"] = _max_item_fetched_at(
        [{"fetched_at": result.get("overview_max_fetched_at")}],
        applied,
    )
    out["live_overlay"] = True
    out["live_overlay_count"] = len(applied)
    return out


def _merge_info_live_overlay_sections(
    result: dict[str, Any],
    overlay_items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not overlay_items:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    cat_counts = dict(result.get("cat_counts") or {})
    section_next_cursors = dict(result.get("section_next_cursors") or {})
    base_ids_by_category = {
        key: {str(item.get("id") or "") for item in items if item.get("id")}
        for key, items in sections.items()
    }
    existing_ids = {
        str(item.get("id") or "")
        for items in sections.values()
        for item in items
        if item.get("id")
    }
    applied: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in overlay_items:
        item_id = str(item.get("id") or "")
        is_duplicate = bool(item_id and item_id in existing_ids)
        if item_id and not is_duplicate:
            existing_ids.add(item_id)
        category = item.get("section_category") or _section_category_from_row(item)
        by_category.setdefault(category, []).append(item)
        if not is_duplicate:
            cat_counts[category] = int(cat_counts.get(category) or 0) + 1
        applied.append(item)

    if not applied:
        return result
    for category, items in by_category.items():
        sections[category] = _merge_overlay_items(sections.get(category, []), items, limit=limit)
    for category, cursor in list(section_next_cursors.items()):
        if not cursor:
            continue
        base_ids = base_ids_by_category.get(category, set())
        retained = sum(
            1
            for item in sections.get(category, [])
            if str(item.get("id") or "") in base_ids
        )
        section_next_cursors[category] = _info_read_model_next_cursor(
            version_id=cursor.get("version_id"),
            scope_key=cursor.get("scope_key"),
            rank_after=retained,
            total_count=int(cat_counts.get(category) or 0),
            exclude_ids=_item_ids_for_cursor(sections.get(category, [])),
        )

    out["sections"] = sections
    out["cat_counts"] = cat_counts
    out["total"] = sum(int(value or 0) for value in cat_counts.values())
    if section_next_cursors:
        out["section_next_cursors"] = section_next_cursors
    out["overview_max_fetched_at"] = _max_item_fetched_at(
        [{"fetched_at": result.get("overview_max_fetched_at")}],
        applied,
    )
    out["live_overlay"] = True
    out["live_overlay_count"] = len(applied)
    return out


def _query_feed_sections_search_read_model(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_category is None:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_category), 200))
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 8000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id,
                              ci.card_json,
                              ci.sort_at,
                              ci.fetched_at,
                              ci.relevance_score,
                              COALESCE(
                                NULLIF(NULLIF(ci.card_json #>> '{{ai_categories,0}}', ''), 'other'),
                                NULLIF(NULLIF(ci.card_json ->> 'ai_category', ''), 'other'),
                                %(uncategorized)s
                              ) AS category
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     counts AS (
                       SELECT category,
                              count(item_id)::integer AS total_count
                         FROM matched_cards
                        GROUP BY category
                     ),
                     page_rows AS (
                       SELECT category,
                              total_count,
                              page_rank AS rn,
                              sort_at,
                              fetched_at,
                              relevance_score,
                              item_id,
                              card_json
                         FROM (
                           SELECT mc.*,
                                  c.total_count,
                                  row_number() OVER (
                                    PARTITION BY mc.category
                                    ORDER BY {_info_scope_item_order_sql("mc")}
                                  ) AS page_rank
                             FROM matched_cards mc
                             JOIN counts c
                               ON c.category = mc.category
                         ) ranked
                        WHERE page_rank <= %(limit)s
                     )
                     SELECT pr.category, pr.total_count, pr.rn, pr.card_json
                       FROM page_rows pr
                      ORDER BY pr.category,
                               pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC""",
                {
                    "version_id": str(active.get("version_id") or ""),
                    "limit": safe_limit,
                    "search_like": f"%{search}%",
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                },
            ).fetchall()
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        total = int(data.get("total_count") or 0)
        if total > 0:
            cat_counts[category] = total
        item = _item_from_read_model_card(data.get("card_json"))
        if item:
            sections.setdefault(category, []).append(item)
            all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    section_next_cursors = {
        category: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform="_all", dimension="section_category", value=category),
            rank_after=len(sections.get(category, [])),
            total_count=int(cat_counts.get(category) or 0),
        )
        for category in cat_counts
    }
    result = {
        "sections": sections,
        "total": sum(cat_counts.values()),
        "cat_counts": cat_counts,
        "personalized": False,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_search_v1",
        "read_model_version_id": version_id_str,
        "section_next_cursors": section_next_cursors,
    }
    overlay = _query_private_manual_sections_overlay(
        schema=schema,
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_sections_overlay(result, overlay)


def _query_feed_platforms_search_read_model(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_platform is None:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 8000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            common_params = {
                "version_id": str(active.get("version_id") or ""),
                "search_like": f"%{search}%",
            }
            card_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id,
                              ci.card_json,
                              ci.platform,
                              ci.source,
                              ci.sort_at,
                              ci.fetched_at,
                              ci.relevance_score
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     counts AS (
                       SELECT platform,
                              count(item_id)::integer AS total_count
                         FROM matched_cards
                        GROUP BY platform
                     ),
                     page_rows AS (
                       SELECT platform,
                              total_count,
                              page_rank AS rn,
                              sort_at,
                              fetched_at,
                              relevance_score,
                              item_id,
                              card_json
                         FROM (
                           SELECT mc.*,
                                  c.total_count,
                                  row_number() OVER (
                                    PARTITION BY mc.platform
                                    ORDER BY {_info_scope_item_order_sql("mc")}
                                  ) AS page_rank
                             FROM matched_cards mc
                             JOIN counts c
                               ON c.platform = mc.platform
                         ) ranked
                        WHERE page_rank <= %(limit)s
                     )
                     SELECT pr.platform, pr.total_count, pr.rn, pr.card_json
                       FROM page_rows pr
                      ORDER BY pr.platform,
                               pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC""",
                {**common_params, "limit": safe_limit},
            ).fetchall()
            source_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id, ci.platform, ci.source
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     )
                    SELECT platform, source, count(DISTINCT item_id)::integer AS cnt
                      FROM matched_cards
                     WHERE COALESCE(source, '') != ''
                     GROUP BY platform, source
                     ORDER BY platform, cnt DESC""",
                common_params,
            ).fetchall()
            category_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id, ci.platform, ci.card_json
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     matched_categories AS (
                       SELECT mc.platform,
                              cat.value AS category,
                              mc.item_id
                         FROM matched_cards mc
                        CROSS JOIN LATERAL jsonb_array_elements_text(
                              CASE
                                WHEN jsonb_typeof(mc.card_json -> 'ai_categories') = 'array'
                                THEN mc.card_json -> 'ai_categories'
                                ELSE '[]'::jsonb
                              END
                            ) AS cat(value)
                       UNION ALL
                       SELECT mc.platform,
                              COALESCE(
                                NULLIF(NULLIF(mc.card_json ->> 'ai_category', ''), 'other'),
                                %(uncategorized)s
                              ) AS category,
                              mc.item_id
                         FROM matched_cards mc
                        WHERE jsonb_array_length(
                              CASE
                                WHEN jsonb_typeof(mc.card_json -> 'ai_categories') = 'array'
                                THEN mc.card_json -> 'ai_categories'
                                ELSE '[]'::jsonb
                              END
                            ) = 0
                     )
                    SELECT platform, category, count(DISTINCT item_id)::integer AS cnt
                      FROM matched_categories
                     WHERE COALESCE(category, '') != ''
                     GROUP BY platform, category
                     ORDER BY platform, cnt DESC""",
                {**common_params, "uncategorized": UNCATEGORIZED_SENTINEL},
            ).fetchall()
    except Exception:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    platform_counts: dict[str, int] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        total = int(data.get("total_count") or 0)
        if total > 0:
            platform_counts[platform] = total
        item = _item_from_read_model_card(data.get("card_json"))
        if item:
            sections.setdefault(platform, []).append(item)
            all_section_items.append(item)
    source_counts: dict[str, dict[str, int]] = {}
    for row in source_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        source = data.get("source") or ""
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            source_counts.setdefault(platform, {})[source] = cnt
    category_counts: dict[str, dict[str, int]] = {}
    for row in category_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            category_counts.setdefault(platform, {})[category] = cnt
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    platform_next_cursors = {
        platform: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform=platform, dimension="all", value=""),
            rank_after=len(sections.get(platform, [])),
            total_count=int(platform_counts.get(platform) or 0),
        )
        for platform in platform_counts
    }
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_search_v1",
        "read_model_version_id": version_id_str,
        "platform_next_cursors": platform_next_cursors,
    }
    overlay = _query_private_manual_platforms_overlay(
        schema=schema,
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_platforms_overlay(result, overlay)


def _query_feed_platforms_read_model(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_platform is None:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    live_overlay: dict[str, Any] = {"items": [], "meta": {"enabled": False}, "cacheable": True}
    try:
        with connect() as conn:
            if not _set_info_read_model_timeouts(conn):
                return None
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            version_id = active["version_id"]
            scope_rows = conn.execute(
                f"""SELECT sc.platform, sc.dimension, sc.value,
                           sc.total_count,
                           sc.max_sort_at
                      FROM {schema}.info_scopes sc
                     WHERE sc.version_id = %(version_id)s
                       AND sc.dimension IN ('all', 'source', 'category')
                     ORDER BY sc.platform, sc.dimension, sc.total_count DESC""",
                {"version_id": version_id},
            ).fetchall()
            card_rows = conn.execute(
                f"""WITH all_scope_items AS MATERIALIZED (
                       SELECT sc.platform, page.rank, page.item_id,
                              page.sort_at, page.fetched_at, page.relevance_score
                         FROM (
                               SELECT platform, scope_key
                                 FROM {schema}.info_scopes
                                WHERE version_id = %(version_id)s
                                  AND dimension = 'all'
                              ) sc
                         CROSS JOIN LATERAL (
                               SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                                 FROM {schema}.info_scope_items si
                                 JOIN {schema}.info_card_items ci
                                   ON ci.version_id = si.version_id
                                  AND ci.item_id = si.item_id
                                WHERE si.version_id = %(version_id)s
                                  AND si.scope_key = sc.scope_key
                                  AND {_info_display_source_filter("ci")}
                                ORDER BY {_info_scope_item_order_sql("si")}
                                LIMIT %(limit)s
                              ) page
                     )
                     SELECT page.platform, page.rank, page.sort_at,
                            page.fetched_at, page.relevance_score, page.item_id,
                            card.card_json
                       FROM all_scope_items page
                       CROSS JOIN LATERAL (
                             SELECT ci.card_json
                               FROM {schema}.info_card_items ci
                              WHERE ci.version_id = %(version_id)s
                                AND ci.item_id = page.item_id
                              OFFSET 0
                       ) card
                      ORDER BY page.platform,
                               page.sort_at DESC NULLS LAST,
                               page.fetched_at DESC NULLS LAST,
                               page.relevance_score DESC NULLS LAST,
                               page.item_id DESC""",
                {"version_id": version_id, "limit": safe_limit},
            ).fetchall()
            _commit_safely(conn)
            live_overlay = _query_info_live_overlay_items(
                conn,
                schema,
                active_version_id=active.get("version_id"),
                active_max_fetched_at=active.get("max_fetched_at"),
                scope_dimension="platform",
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
    except Exception:
        return None

    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    bookmark_counts: dict[str, int] = {}
    for row in scope_rows:
        data = dict(row)
        if (
            (data.get("platform") or "") == "twitter"
            and (data.get("dimension") or "") == "source"
            and (data.get("value") or "") == "bookmarks"
        ):
            bookmark_counts["twitter"] = int(data.get("total_count") or 0)
    for row in scope_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        dimension = data.get("dimension") or ""
        value = data.get("value") or ""
        total = int(data.get("total_count") or 0)
        if total <= 0:
            continue
        if dimension == "all":
            platform_counts[platform] = max(0, total - int(bookmark_counts.get(platform) or 0))
        elif dimension == "source" and value:
            if platform == "twitter" and value == "bookmarks":
                continue
            source_counts.setdefault(platform, {})[value] = total
        elif dimension == "category" and value:
            category_counts.setdefault(platform, {})[value] = total

    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        item = _item_from_read_model_card(data.get("card_json"))
        if not item:
            continue
        platform = data.get("platform") or item.get("platform") or "_unknown"
        sections.setdefault(platform, []).append(item)
        all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    platform_next_cursors = {
        platform: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform=platform, dimension="all", value=""),
            rank_after=len(sections.get(platform, [])),
            total_count=int(platform_counts.get(platform) or 0),
        )
        for platform in platform_counts
    }
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id_str,
        "platform_next_cursors": platform_next_cursors,
    }
    result = _merge_info_live_overlay_platforms(result, list(live_overlay.get("items") or []))
    result = _attach_info_live_overlay_meta(result, live_overlay)
    overlay = _query_private_manual_platforms_overlay(
        schema=schema,
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_platforms_overlay(result, overlay)


def _query_feed_sections_read_model(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_category is None:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_category), 200))
    live_overlay: dict[str, Any] = {"items": [], "meta": {"enabled": False}, "cacheable": True}
    try:
        with connect() as conn:
            if not _set_info_read_model_timeouts(conn):
                return None
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            version_id = active["version_id"]
            count_rows = conn.execute(
                f"""SELECT sc.value AS category,
                           sum(sc.total_count)::integer AS total_count,
                           max(sc.max_sort_at) AS max_sort_at
                      FROM {schema}.info_scopes sc
                     WHERE sc.version_id = %(version_id)s
                       AND sc.dimension = 'section_category'
                       AND sc.value != ''
                     GROUP BY sc.value
                     ORDER BY total_count DESC""",
                {"version_id": version_id},
            ).fetchall()
            card_rows = conn.execute(
                f"""WITH scope_rows AS (
                       SELECT sc.value AS category,
                              sc.scope_key
                         FROM {schema}.info_scopes sc
                        WHERE sc.version_id = %(version_id)s
                          AND sc.dimension = 'section_category'
                          AND sc.value != ''
                     )
                     SELECT sr.category,
                            page.rank,
                            page.fetched_at,
                            page.sort_at,
                            page.relevance_score,
                            page.item_id,
                            ci.card_json
                       FROM scope_rows sr
                      CROSS JOIN LATERAL (
                            SELECT si.rank, si.sort_at, si.fetched_at, si.relevance_score, si.item_id
                              FROM {schema}.info_scope_items si
                              JOIN {schema}.info_card_items ci
                                ON ci.version_id = si.version_id
                               AND ci.item_id = si.item_id
                             WHERE si.version_id = %(version_id)s
                               AND si.scope_key = sr.scope_key
                               AND {_info_display_source_filter("ci")}
                             ORDER BY {_info_scope_item_order_sql("si")}
                             LIMIT %(limit)s
                           ) AS page
                       JOIN {schema}.info_card_items ci
                         ON ci.version_id = %(version_id)s
                        AND ci.item_id = page.item_id
                      ORDER BY sr.category,
                               page.sort_at DESC NULLS LAST,
                               page.fetched_at DESC NULLS LAST,
                               page.relevance_score DESC NULLS LAST,
                               page.item_id DESC""",
                {"version_id": version_id, "limit": safe_limit},
            ).fetchall()
            _commit_safely(conn)
            live_overlay = _query_info_live_overlay_items(
                conn,
                schema,
                active_version_id=active.get("version_id"),
                active_max_fetched_at=active.get("max_fetched_at"),
                scope_dimension="section_category",
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    for row in count_rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        total = int(data.get("total_count") or 0)
        if total > 0:
            cat_counts[category] = total
    if not cat_counts:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        item = _item_from_read_model_card(data.get("card_json"))
        if not item:
            continue
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        sections.setdefault(category, []).append(item)
        all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    overview_generated_at = _timestamp_value(active.get("generated_at"))
    overview_max_fetched_at = _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items)
    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    section_next_cursors: dict[str, dict[str, Any] | None] = {}
    for category, items in sections.items():
        total = int(cat_counts.get(category) or 0)
        next_offset = len(items)
        scope_key = _info_scope_key(platform="_all", dimension="section_category", value=category)
        next_cursor = _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=scope_key,
            rank_after=next_offset,
            total_count=total,
        )
        section_next_cursors[category] = next_cursor
        page = {
            "items": items,
            "category": category,
            "total": total,
            "offset": 0,
            "limit": safe_limit,
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "data_backend": feed_read_backend(),
            "read_model": "info_platforms_v1",
            "read_model_version_id": version_id_str,
            "scope_key": scope_key,
            "scope_dimension": "section_category",
            "scope_value": category,
            "overview_generated_at": overview_generated_at,
            "overview_max_fetched_at": overview_max_fetched_at,
        }
        if next_cursor:
            page["next_cursor"] = next_cursor
        if not user_id and bool(live_overlay.get("cacheable", True)):
            _cache_set_copy(
                _info_read_model_section_category_page_cache_key(
                    schema=schema,
                    category=category,
                    offset=0,
                    limit=safe_limit,
                ),
                page,
            )

    result = {
        "sections": sections,
        "total": sum(cat_counts.values()),
        "cat_counts": cat_counts,
        "personalized": False,
        "data_backend": feed_read_backend(),
        "overview_generated_at": overview_generated_at,
        "overview_max_fetched_at": overview_max_fetched_at,
        "sample_limit": safe_limit,
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id_str,
        "section_next_cursors": section_next_cursors,
    }
    result = _merge_info_live_overlay_sections(result, list(live_overlay.get("items") or []))
    result = _attach_info_live_overlay_meta(result, live_overlay)
    overlay = _query_private_manual_sections_overlay(
        schema=schema,
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_sections_overlay(result, overlay)


def _query_feed_by_category_read_model(
    *,
    schema: str,
    category: str,
    keyword: str | None,
    search: str | None,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    max_limit: int = 200,
) -> dict[str, Any] | None:
    if keyword:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    raw_category = (category or "").strip()
    if not raw_category:
        return None
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), max(1, int(max_limit or 200))))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    cursor_exclude_ids = list(cursor_state.get("exclude_ids") or []) if cursor_state else []
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    page_cache_key = _info_read_model_section_category_page_cache_key(
        schema=schema,
        category=cache_category,
        subcategory=clean_subcategory,
        offset=effective_offset,
        limit=safe_limit,
        version_id=cursor_version_id,
    )
    cached_page = None if cursor_exclude_ids else _cache_get_copy(page_cache_key)
    if cached_page is not None:
        if user_id:
            cached_page = dict(cached_page)
            cached_page["items"] = _apply_user_status_overlay(
                schema=schema,
                items=list(cached_page.get("items") or []),
                user_id=user_id,
            )
        return cached_page
    exclude_predicate_sql = "NOT (si.item_id = ANY(%(exclude_ids)s))" if cursor_exclude_ids else "TRUE"
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_rows AS (
                       SELECT sc.version_id, sc.scope_key
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                          AND sc.dimension = %(scope_dimension)s
                     ),
                     summary AS (
                       SELECT count(*)::integer AS scope_count,
                              (
                                SELECT count(*)::integer
                                  FROM scope_rows sr
                                  JOIN {schema}.info_scope_items si
                                    ON si.version_id = sr.version_id
                                   AND si.scope_key = sr.scope_key
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE {_info_display_source_filter("ci")}
                              ) AS total_count
                     ),
                     page_rows AS (
                       SELECT si.rank, si.fetched_at, si.relevance_score, si.item_id,
                              si.sort_at, ci.card_json
                         FROM scope_rows sr
                         JOIN {schema}.info_scope_items si
                           ON si.version_id = sr.version_id
                          AND si.scope_key = sr.scope_key
                         JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                          AND ci.item_id = si.item_id
                        WHERE {exclude_predicate_sql}
                          AND {_info_display_source_filter("ci")}
                        ORDER BY {_info_scope_item_order_sql("si")}
                        LIMIT %(limit)s OFFSET %(offset)s
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.fetched_at, pr.relevance_score, pr.item_id,
                            pr.sort_at, pr.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "scope_dimension": scope_dimension,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "end_rank": effective_offset + safe_limit,
                    "exclude_ids": cursor_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": scope_dimension,
        "scope_value": scope_value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
        if cursor_exclude_ids:
            result["next_cursor"]["exclude_ids"] = cursor_exclude_ids
    if user_id:
        return result
    if cursor_exclude_ids:
        return result
    return _cache_set_copy(page_cache_key, result)


def _query_feed_by_category_search_read_model(
    *,
    schema: str,
    category: str,
    keyword: str | None,
    search: str | None,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if keyword:
        return None
    if manual_owner_user_id:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    raw_category = (category or "").strip()
    if not raw_category:
        return None
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_rows AS (
                       SELECT sc.version_id, sc.scope_key
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                          AND sc.dimension = %(scope_dimension)s
                     ),
                     summary AS (
                       SELECT (SELECT count(*)::integer FROM scope_rows) AS scope_count,
                              (
                                SELECT count(*)::integer
                                  FROM scope_rows sr
                                  JOIN {schema}.info_scope_items si
                                    ON si.version_id = sr.version_id
                                   AND si.scope_key = sr.scope_key
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                                   AND {_info_display_source_filter("ci")}
                              ) AS total_count
                     ),
                     page_rows AS (
                       SELECT rank, item_id, sort_at, fetched_at, relevance_score
                         FROM (
                           SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                             FROM scope_rows sr
                             JOIN {schema}.info_scope_items si
                               ON si.version_id = sr.version_id
                              AND si.scope_key = sr.scope_key
                             JOIN {schema}.info_card_items ci
                               ON ci.version_id = si.version_id
                              AND ci.item_id = si.item_id
                            WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                              AND {_info_display_source_filter("ci")}
                            ORDER BY {_info_scope_item_order_sql("si")}
                            LIMIT %(limit)s OFFSET %(offset)s
                         ) AS page_match
                        ORDER BY sort_at DESC NULLS LAST,
                                 fetched_at DESC NULLS LAST,
                                 relevance_score DESC NULLS LAST,
                                 item_id DESC
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, page_ci.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                       LEFT JOIN {schema}.info_card_items page_ci
                         ON page_ci.version_id = av.version_id
                        AND page_ci.item_id = pr.item_id
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "scope_dimension": scope_dimension,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "cursor_version_id": cursor_version_id or "",
                    "search_like": f"%{search}%",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_search_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": scope_dimension,
        "scope_value": scope_value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
    return result


def _query_feed_by_platform_read_model(
    *,
    schema: str,
    platform: str,
    offset: int,
    limit: int,
    source: str | None,
    group: str | None,
    category: str | None,
    search: str | None,
    exclude_ids: list[str] | None,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    scope = _info_platform_scope(platform=platform, source=source, group=group, category=category)
    if not scope:
        return None
    dimension, value, scope_key = scope
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    cursor_exclude_ids = list(cursor_state.get("exclude_ids") or []) if cursor_state else []
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    effective_offset = (
        int(cursor_state["rank_after"])
        if cursor_state
        else max(safe_offset, len(clean_exclude_ids)) if clean_exclude_ids else safe_offset
    )
    # Keep the normal read-model page cache hot. Only live-overlay cursors need a
    # SQL anti-filter because overlay rows can be fresher copies of later ranks.
    effective_exclude_ids: list[str] = cursor_exclude_ids
    page_cache_key = _info_read_model_page_cache_key(
        schema=schema,
        platform=platform,
        source=source,
        group=group,
        category=category,
        offset=effective_offset,
        limit=safe_limit,
        exclude_ids=effective_exclude_ids,
        version_id=cursor_version_id,
    )
    cached_page = _cache_get_copy(page_cache_key)
    if cached_page is not None:
        if user_id:
            cached_page = dict(cached_page)
            cached_page["items"] = _apply_user_status_overlay(
                schema=schema,
                items=list(cached_page.get("items") or []),
                user_id=user_id,
            )
        return cached_page
    exclude_sql = "AND NOT (si.item_id = ANY(%(exclude_ids)s))" if effective_exclude_ids else ""
    try:
        with connect() as conn:
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_row AS (
                       SELECT (
                                SELECT count(*)::integer
                                  FROM {schema}.info_scope_items si
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE si.version_id = sc.version_id
                                   AND si.scope_key = sc.scope_key
                                   AND {_info_display_source_filter("ci")}
                              ) AS total_count
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                     ),
                     page_rows AS (
                       SELECT si.rank, si.item_id, si.sort_at, si.fetched_at,
                              si.relevance_score, ci.card_json
                         FROM {schema}.info_scope_items si
                         JOIN active_version av
                           ON av.version_id = si.version_id
                         JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                         AND ci.item_id = si.item_id
                        WHERE si.scope_key = %(scope_key)s
                          {exclude_sql}
                          AND {_info_display_source_filter("ci")}
                        ORDER BY {_info_scope_item_order_sql("si")}
                        LIMIT %(limit)s OFFSET %(offset)s
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at, sr.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, pr.card_json
                       FROM active_version av
                       JOIN scope_row sr ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "exclude_ids": effective_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": dimension,
        "scope_value": value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
        if effective_exclude_ids:
            result["next_cursor"]["exclude_ids"] = effective_exclude_ids
    if user_id:
        return result
    return _cache_set_copy(page_cache_key, result)


def _query_feed_by_platform_search_read_model(
    *,
    schema: str,
    platform: str,
    offset: int,
    limit: int,
    source: str | None,
    group: str | None,
    category: str | None,
    search: str | None,
    exclude_ids: list[str] | None,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if platform == "manual" and manual_owner_user_id:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    scope = _info_platform_scope(platform=platform, source=source, group=group, category=category)
    if not scope:
        return None
    dimension, value, scope_key = scope
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    exclude_sql = "AND NOT (si.item_id = ANY(%(exclude_ids)s))" if clean_exclude_ids else ""
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_row AS (
                       SELECT sc.version_id, sc.scope_key
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                     ),
                     summary AS (
                       SELECT (SELECT count(*)::integer FROM scope_row) AS scope_count,
                              (
                                SELECT count(*)::integer
                                  FROM scope_row sr
                                  JOIN {schema}.info_scope_items si
                                    ON si.version_id = sr.version_id
                                   AND si.scope_key = sr.scope_key
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                                   AND {_info_display_source_filter("ci")}
                              ) AS total_count
                     ),
                     page_rows AS (
                       SELECT rank, item_id, sort_at, fetched_at, relevance_score
                         FROM (
                           SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                             FROM scope_row sr
                             JOIN {schema}.info_scope_items si
                               ON si.version_id = sr.version_id
                              AND si.scope_key = sr.scope_key
                             JOIN {schema}.info_card_items ci
                               ON ci.version_id = si.version_id
                              AND ci.item_id = si.item_id
                            WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                              AND {_info_display_source_filter("ci")}
                              {exclude_sql}
                            ORDER BY {_info_scope_item_order_sql("si")}
                            LIMIT %(limit)s OFFSET %(offset)s
                         ) AS page_match
                        ORDER BY sort_at DESC NULLS LAST,
                                 fetched_at DESC NULLS LAST,
                                 relevance_score DESC NULLS LAST,
                                 item_id DESC
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, page_ci.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                       LEFT JOIN {schema}.info_card_items page_ci
                         ON page_ci.version_id = av.version_id
                        AND page_ci.item_id = pr.item_id
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "exclude_ids": clean_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                    "search_like": f"%{search}%",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_search_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": dimension,
        "scope_value": value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
    return result


def prewarm_info_read_model_pages(*, max_scopes: int = 5, page_limit: int = 50, pages_per_scope: int = 2) -> dict[str, Any]:
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_max_scopes = max(1, min(int(max_scopes or 80), 300))
    safe_limit = max(1, min(int(page_limit or 50), 200))
    safe_pages_per_scope = max(1, min(int(pages_per_scope or 1), 5))
    safe_item_limit = safe_limit * safe_pages_per_scope
    t0 = time.time()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 30000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return {"ok": False, "skipped": "no_active_version"}
            version_id = active["version_id"]
            rows = conn.execute(
                f"""WITH ranked_scopes AS (
                       SELECT scope_key, platform, dimension, value, total_count,
                              row_number() OVER (
                                PARTITION BY CASE
                                  WHEN dimension IN ('section_category', 'section_subcategory') THEN dimension
                                  ELSE 'platform_scope'
                                END
                                ORDER BY total_count DESC, scope_key
                              ) AS scope_rank
                         FROM {schema}.info_scopes
                        WHERE version_id = %(version_id)s
                          AND dimension IN ('source', 'group', 'group_source', 'category', 'section_category', 'section_subcategory')
                          AND total_count > 0
                     ),
                     hot_scopes AS (
                       SELECT scope_key, platform, dimension, value, total_count
                         FROM ranked_scopes
                        WHERE scope_rank <= %(max_scopes)s
                     )
                     SELECT hs.scope_key, hs.platform, hs.dimension, hs.value,
                            hs.total_count, page.rank, page.card_json
                       FROM hot_scopes hs
                       CROSS JOIN LATERAL (
                             SELECT ordered.order_rank AS rank, ordered.card_json
                               FROM (
                                     SELECT row_number() OVER (
                                              ORDER BY {_info_scope_item_order_sql("si")}
                                            ) AS order_rank,
                                            ci.card_json,
                                            si.sort_at,
                                            si.fetched_at,
                                            si.relevance_score,
                                            si.item_id
                                       FROM {schema}.info_scope_items si
                                       JOIN {schema}.info_card_items ci
                                         ON ci.version_id = si.version_id
                                        AND ci.item_id = si.item_id
                                      WHERE si.version_id = %(version_id)s
                                        AND si.scope_key = hs.scope_key
                                        AND {_info_display_source_filter("ci")}
                                    ) ordered
                              ORDER BY ordered.order_rank
                              LIMIT %(item_limit)s
                           ) page
                      ORDER BY hs.total_count DESC, hs.scope_key, page.rank""",
                {
                    "version_id": version_id,
                    "max_scopes": safe_max_scopes,
                    "item_limit": safe_item_limit,
                },
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "elapsed_ms": int((time.time() - t0) * 1000)}

    pages: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        scope_key = row.get("scope_key")
        if not scope_key:
            continue
        rank = max(1, int(row.get("rank") or 1))
        page_offset = ((rank - 1) // safe_limit) * safe_limit
        page = pages.setdefault(
            (str(scope_key), page_offset),
            {
                "items": [],
                "platform": row.get("platform"),
                "category": (
                    _split_info_compound_value(str(row.get("value") or ""))[0]
                    if row.get("dimension") == "section_subcategory"
                    else row.get("value") if row.get("dimension") in ("category", "section_category") else None
                ),
                "total": int(row.get("total_count") or 0),
                "offset": page_offset,
                "limit": safe_limit,
                "has_more": False,
                "next_offset": None,
                "data_backend": feed_read_backend(),
                "read_model": "info_platforms_v1",
                "scope_dimension": row.get("dimension"),
                "scope_value": row.get("value") or "",
                "overview_generated_at": _timestamp_value(active.get("generated_at")),
                "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")),
            },
        )
        item = _item_from_read_model_card(row.get("card_json"))
        if item:
            page["items"].append(item)

    item_count = 0
    for page in pages.values():
        item_count += len(page["items"])
        page_offset = int(page.get("offset") or 0)
        next_offset = page_offset + len(page["items"])
        page["has_more"] = next_offset < int(page.get("total") or 0)
        page["next_offset"] = next_offset if page["has_more"] else None
        dimension = page.get("scope_dimension")
        value = page.get("scope_value") or ""
        if dimension == "section_category":
            cache_key = _info_read_model_section_category_page_cache_key(
                schema=schema,
                category=value,
                offset=page_offset,
                limit=safe_limit,
            )
        elif dimension == "section_subcategory":
            category_value, subcategory_value = _split_info_compound_value(value)
            cache_key = _info_read_model_section_category_page_cache_key(
                schema=schema,
                category=category_value,
                subcategory=subcategory_value,
                offset=page_offset,
                limit=safe_limit,
            )
        else:
            group_value, source_value = _split_info_compound_value(value) if dimension == "group_source" else ("", "")
            cache_key = _info_read_model_page_cache_key(
                schema=schema,
                platform=str(page.get("platform") or ""),
                source=source_value if dimension == "group_source" else value if dimension == "source" else None,
                group=group_value if dimension == "group_source" else value if dimension == "group" else None,
                category=value if dimension == "category" else None,
                offset=page_offset,
                limit=safe_limit,
                exclude_ids=[],
            )
        _cache_set_copy(cache_key, page)
    return {
        "ok": True,
        "pages": len(pages),
        "items": item_count,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def query_feed_platforms(
    *,
    per_platform: int | None = 50,
    search: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    """v18.0 nav-merge: 信息 tab 复用本函数；强制 AI 相关性过滤（D3）。

    sections 是首屏样本；platform_counts/source_counts/category_counts 是全量
    聚合口径，不能再从首屏样本推导。
    """
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 PRD §Spec-2: 强制 AI 相关性过滤（OR 双字段口径）
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    # BF-0515-singleflight: cache full /api/feed/platforms response (no result
    # cache existed before — every call ran 5 SQLs). Singleflight prevents N
    # cold-start callers from each running the same SQLs.
    live_overlay_enabled = bool(_info_live_overlay_enabled())
    result_cache_ttl = _feed_result_cache_lookup_ttl(live_overlay_enabled=live_overlay_enabled)
    result_cache_key = (
        "feed_platforms_result",
        schema,
        per_platform,
        search or "",
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        live_overlay_enabled,
        int(_info_live_overlay_limit()),
        int(_info_live_overlay_per_scope_limit()),
    )
    cached_result = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
    if cached_result is not None:
        return cached_result
    if search:
        read_model_result = _query_feed_platforms_search_read_model(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
    else:
        read_model_result = _query_feed_platforms_read_model(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
    if read_model_result is not None:
        cache_ttl = _feed_result_cache_ttl(read_model_result)
        if cache_ttl > 0:
            return _cache_set_copy_with_ttl(result_cache_key, read_model_result, cache_ttl)
        return read_model_result
    if search and _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return _degraded_feed_platforms_result("info_search_read_model_unavailable")
    use_platforms_fast_path = _can_use_platforms_mv_fast_path(
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    )
    if _remote_feed_live_circuit_open():
        if use_platforms_fast_path:
            fallback = _read_local_read_cache(
                _feed_platforms_local_cache_name(
                    per_platform=per_platform,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
        return _degraded_feed_platforms_result()

    def _compute() -> dict:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        return _query_feed_platforms_uncached(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
            where=where,
            params=params,
            result_cache_key=result_cache_key,
        )

    try:
        return _singleflight_sync(result_cache_key, _compute)
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        if use_platforms_fast_path:
            fallback = _read_local_read_cache(
                _feed_platforms_local_cache_name(
                    per_platform=per_platform,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
            return _degraded_feed_platforms_result()
        return _degraded_feed_platforms_result()


def _query_feed_platforms_uncached(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    where: list[str],
    params: dict[str, Any],
    result_cache_key: tuple[Any, ...],
) -> dict:
    with connect() as conn:
        item_params = dict(params)
        item_limit_sql = ""
        if per_platform is not None:
            item_params["per_platform"] = per_platform
            item_limit_sql = "WHERE rn <= %(per_platform)s"
        _set_short_statement_timeout(conn, _remote_feed_live_timeout_ms())
        try:
            status_join, status_params, status_alias = _item_status_join(schema, user_id)
            item_params.update(status_params)
            rows = conn.execute(
                f"""WITH ranked AS (
                       SELECT {_feed_cols(status_alias, include_heavy_json=False)},
                              row_number() OVER (
                                PARTITION BY i.platform
                                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                         i.fetched_at DESC NULLS LAST,
                                         i.relevance_score DESC NULLS LAST,
                                         i.id DESC
                              ) AS rn
                         FROM {schema}.items i
                         {status_join}
                         {_where_sql(where)}
                     )
                     SELECT * FROM ranked
                     {item_limit_sql}
                     ORDER BY platform,
                              COALESCE(published_at, fetched_at) DESC NULLS LAST,
                              fetched_at DESC NULLS LAST,
                              relevance_score DESC NULLS LAST,
                              id DESC""",
                item_params,
            ).fetchall()
            platform_counts, source_counts, category_counts = _platform_overview_counts_from_items(
                conn,
                schema,
                where,
                params,
            )
            _warm_platform_page_count_cache(
                schema=schema,
                platform_counts=platform_counts,
                source_counts=source_counts,
                category_counts=category_counts,
                search=search,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                user_id=user_id,
                min_github_stars=min_github_stars,
            )
        except Exception:
            _rollback_safely(conn)
            raise RemoteDBError("platforms live path failed")
    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in rows:
        item = _normalize_item(dict(row))
        sections.setdefault(item.get("platform") or "_unknown", []).append(item)
        all_section_items.append(item)
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overview_max_fetched_at": _max_fetched_at(all_section_items),
        "sample_limit": int(per_platform) if per_platform is not None else None,
    }
    if _can_use_platforms_mv_fast_path(
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    ):
        _write_local_read_cache_async(
            _feed_platforms_local_cache_name(
                per_platform=per_platform,
                public_only=public_only,
                min_github_stars=min_github_stars,
            ),
            result,
        )
    return _cache_set_copy(result_cache_key, result)


def query_feed_by_platform(
    *,
    platform: str,
    offset: int = 0,
    limit: int = 50,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
    search: str | None = None,
    exclude_ids: list[str] | None = None,
    cursor: dict[str, Any] | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.platform = %(platform)s")
    params["platform"] = platform
    _add_search_filter(where, params, search)
    where.append("i.visible = 1")
    # 2026-05-22 read model: /platforms/more must page through the same
    # AI-relevant item universe used by /platforms counts and first-page cards.
    _add_ai_relevance_filter(where)
    if source:
        where.append("i.source = %(source)s")
        params["source"] = source
    if group:
        if group == "未分组":
            where.append(
                "((i.detail_json ->> 'group') IN ('未分组','独立频道') OR (i.detail_json ->> 'group') IS NULL)"
            )
        else:
            where.append("(i.detail_json ->> 'group') = %(group)s")
            params["group"] = group
    if category and category != UNCATEGORIZED_SENTINEL:
        where.append("i.ai_categories IS NOT NULL")
    _add_category_filter(where, params, category)
    count_where = list(where)
    count_params = dict(params)
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    if clean_exclude_ids:
        where.append("NOT (i.id::text = ANY(%(exclude_ids)s))")
        params["exclude_ids"] = clean_exclude_ids
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    if search:
        search_read_model_result = _query_feed_by_platform_search_read_model(
            schema=schema,
            platform=platform,
            offset=safe_offset,
            limit=safe_limit,
            source=source,
            group=group,
            category=category,
            search=search,
            exclude_ids=clean_exclude_ids,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if search_read_model_result is not None:
            return search_read_model_result
        if (
            not (platform == "manual" and manual_owner_user_id)
            and _info_platform_scope(platform=platform, source=source, group=group, category=category)
            and _can_use_info_search_read_model(
                search=search,
                user_id=user_id,
                public_only=public_only,
                min_github_stars=min_github_stars,
            )
        ):
            return _degraded_feed_platform_page_result(
                platform,
                category=category,
                reason="info_search_read_model_unavailable",
            )
    read_model_result = _query_feed_by_platform_read_model(
        schema=schema,
        platform=platform,
        offset=safe_offset,
        limit=safe_limit,
        source=source,
        group=group,
        category=category,
        search=search,
        exclude_ids=clean_exclude_ids,
        cursor=cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    if read_model_result is not None:
        return read_model_result
    count_cache_key = _platform_page_count_cache_key(
        schema=schema,
        platform=platform,
        source=source,
        group=group,
        category=category,
        search=search,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        user_id=user_id,
        min_github_stars=min_github_stars,
    )
    cached_total = _cache_get(count_cache_key)
    total = int(cached_total) if cached_total is not None else None
    total_is_estimate = False
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 4500)
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=safe_offset,
                status_user_id=user_id,
            )
            if total is None:
                try:
                    total = _count_items(conn, schema, count_where, count_params)
                    _cache_set(count_cache_key, total)
                except Exception:
                    _rollback_safely(conn)
                    total = _estimate_platform_page_total(
                        offset=safe_offset,
                        limit=safe_limit,
                        item_count=len(items),
                    )
                    total_is_estimate = True
    except Exception:
        raise RemoteDBError("platform page query failed")
    next_offset = safe_offset + len(items)
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
    }
    if total_is_estimate:
        result.update({
            "degraded": True,
            "degraded_reason": "platform_page_total_unavailable",
            "total_is_estimate": True,
        })
    return result


def get_feed_item(
    *,
    item_id: str,
    public_only: bool = False,
    can_access_all: bool = False,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict | None:
    schema = remote_schema()
    cache_key = (
        "feed_item_detail",
        schema,
        item_id,
        bool(public_only),
        bool(can_access_all),
        user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=None if can_access_all else user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.id = %(item_id)s")
    params["item_id"] = item_id
    with connect() as conn:
        rows = _fetch_items(
            conn,
            schema,
            where,
            params,
            order_sql="",
            limit=1,
            detail=True,
            status_user_id=user_id,
        )
    if not rows:
        return None
    item = rows[0]
    item["data_backend"] = feed_read_backend()
    return _cache_set_copy(cache_key, item)


def get_feed_items(
    *,
    item_ids: list[str],
    public_only: bool = False,
    can_access_all: bool = False,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> list[dict]:
    ids = []
    seen: set[str] = set()
    for raw in item_ids:
        item_id = str(raw).strip()
        if not item_id or item_id in seen:
            continue
        ids.append(item_id)
        seen.add(item_id)
    if not ids:
        return []
    schema = remote_schema()
    cache_key = (
        "feed_items_detail_batch",
        schema,
        tuple(ids),
        bool(public_only),
        bool(can_access_all),
        user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=None if can_access_all else user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.id = ANY(%(item_ids)s)")
    params["item_ids"] = ids
    with connect() as conn:
        rows = _fetch_items(
            conn,
            schema,
            where,
            params,
            order_sql="",
            detail=True,
            status_user_id=user_id,
        )
    by_id = {str(item.get("id")): item for item in rows}
    ordered = []
    for item_id in ids:
        item = by_id.get(item_id)
        if not item:
            continue
        item["data_backend"] = feed_read_backend()
        ordered.append(item)
    return _cache_set_copy(cache_key, ordered)


def set_status(
    *,
    item_id: str,
    action: str,
    force: bool = False,
    user_id: str | None = None,
    can_access_all: bool = False,
) -> dict[str, Any]:
    col = STATUS_COLUMNS.get(action)
    if col is None:
        raise RemoteDBConfigError(f"Invalid status action: {action!r}")
    if not user_id:
        return {"ok": True, "skipped": "anonymous", "data_backend": status_backend()}

    schema = remote_schema()
    # BE-3(B4): 权限检查合并进同一个连接——原实现独立 connect() 做检查,
    # 最高频写路径每次占 2 个池连接并双付 checkout 开销。
    with connect() as conn:
        with conn.cursor() as cur:
            item_row = cur.execute(
                f"SELECT platform, user_id FROM {schema}.items WHERE id = %(item_id)s",
                {"item_id": item_id},
            ).fetchone()
            if not item_row:
                return {"ok": False, "not_found": True, "data_backend": status_backend()}
            if item_row["platform"] == "manual" and not can_access_all and item_row["user_id"] != user_id:
                return {"ok": False, "not_found": True, "data_backend": status_backend()}
            current = cur.execute(
                f"""SELECT {col}
                      FROM {schema}.item_status
                     WHERE user_id = %(user_id)s AND item_id = %(item_id)s""",
                {"user_id": user_id, "item_id": item_id},
            ).fetchone()
            if action in ("starred", "hidden") and not force and current and current[col]:
                cur.execute(
                    f"""UPDATE {schema}.item_status
                           SET {col} = NULL
                         WHERE user_id = %(user_id)s AND item_id = %(item_id)s""",
                    {"user_id": user_id, "item_id": item_id},
                )
                value = None
            else:
                cur.execute(
                    f"""INSERT INTO {schema}.item_status (user_id, item_id, {col})
                        VALUES (%(user_id)s, %(item_id)s, now())
                        ON CONFLICT (user_id, item_id) DO UPDATE SET {col} = excluded.{col}
                        RETURNING {col}""",
                    {"user_id": user_id, "item_id": item_id},
                )
                row = cur.fetchone()
                value = _timestamp_value(row[col]) if row else None
        conn.commit()
    # Item-status mutation only affects this user's view of feed (read/click/star/hide).
    # Other users' feed caches are unaffected.
    clear_user_cache_keys(user_id)
    return {"ok": True, "item_id": item_id, "action": action, col: value, "data_backend": status_backend()}


def get_stats(
    *,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    cache_key = (
        "stats_per_platform",
        schema,
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    if user_id is None and public_only and not manual_owner_user_id:
        platform_result = query_feed_platforms(
            per_platform=50,
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=min_github_stars,
        )
        result = {
            (platform or "_unknown"): {
                "total": int(total or 0),
                "unread": int(total or 0),
            }
            for platform, total in (platform_result.get("platform_counts") or {}).items()
        }
        return _cache_set_copy(cache_key, result)
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    with connect() as conn:
        _set_short_statement_timeout(conn, 1500)
        status_join, status_params, _ = _item_status_join(schema, user_id)
        params = {**params, **status_params}
        unread_sql = (
            "sum(case when s.clicked_at is null and s.hidden_at is null then 1 else 0 end)"
            if user_id
            else "count(*)"
        )
        rows = conn.execute(
            f"""SELECT i.platform, count(*) AS total,
                       {unread_sql} AS unread
                  FROM {schema}.items i
                  {status_join}
                  {_where_sql(where)}
                 GROUP BY i.platform""",
            params,
        ).fetchall()
    result = {
        (r["platform"] or "_unknown"): {
            "total": int(r["total"] or 0),
            "unread": int(r["unread"] or 0),
        }
        for r in rows
    }
    _cache_set_copy(cache_key, result)
    return result


def status() -> dict:
    schema = remote_schema()
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            """SELECT
                 COALESCE(MAX(CASE WHEN relname = 'items' THEN reltuples END), 0)::bigint AS items,
                 COALESCE(MAX(CASE WHEN relname = 'clusters' THEN reltuples END), 0)::bigint AS clusters,
                 COALESCE(MAX(CASE WHEN relname = 'cluster_items' THEN reltuples END), 0)::bigint AS cluster_items,
                 COALESCE(MAX(CASE WHEN relname = 'fetch_runs' THEN reltuples END), 0)::bigint AS fetch_runs
               FROM pg_class
              WHERE oid IN (
                to_regclass(%s),
                to_regclass(%s),
                to_regclass(%s),
                to_regclass(%s)
              )""",
            (
                f"{schema}.items",
                f"{schema}.clusters",
                f"{schema}.cluster_items",
                f"{schema}.fetch_runs",
            ),
        ).fetchone()
        version = conn.execute("SELECT version() AS version").fetchone()
    return {
        "backend": event_read_backend(),
        "event_backend": event_read_backend(),
        "feed_backend": feed_read_backend(),
        "status_backend": status_backend(),
        "schema": schema,
        "counts": dict(row or {}),
        "postgres_version": (version or {}).get("version"),
    }
