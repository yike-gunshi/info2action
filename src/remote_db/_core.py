"""Optional Supabase/Postgres adapter for the remote database migration.

SQLite remains the default for existing local development. New online
deployments can opt into ``INFO2ACTION_DATA_AUTHORITY=supabase`` to make the
remote database the production data authority and fail fast when a core read or
status-write surface is still pointing at SQLite.
"""
from __future__ import annotations

import json
import logging
import math
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
import highlight_score_v26
from media_assets import image_urls, media_kind, merge_media, normalize_media
from time_utils import highlights_published_before, parse_datetime, sort_key, to_utc_iso

BASE = Path(__file__).resolve().parents[2]

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
REMOTE_DB_POOL_MAX_IDLE_ENV = "INFO2ACTION_REMOTE_DB_POOL_MAX_IDLE_SEC"
FEED_EVENTS_TIMEOUT_MS_ENV = "INFO2ACTION_FEED_EVENTS_TIMEOUT_MS"

# Cloudflare drops the origin connection at ~100s (524). Any feed query allowed
# to run longer than that can never reach the user, so cap well below it.
_FEED_EVENTS_TIMEOUT_CEILING_MS = 90_000
_FEED_EVENTS_TIMEOUT_DEFAULT_MS = 30_000
REMOTE_DB_CONNECT_ATTEMPTS_ENV = "INFO2ACTION_REMOTE_DB_CONNECT_ATTEMPTS"
REMOTE_DB_FORCE_WRITABLE_ENV = "INFO2ACTION_REMOTE_DB_FORCE_WRITABLE_ON_CONNECT"
REMOTE_CACHE_TTL_ENV = "INFO2ACTION_REMOTE_CACHE_TTL_SEC"
FEED_RESULT_CACHE_TTL_ENV = "INFO2ACTION_FEED_RESULT_CACHE_TTL_SEC"
REMOTE_AUTH_CACHE_TTL_ENV = "INFO2ACTION_REMOTE_AUTH_CACHE_TTL_SEC"
REMOTE_SNAPSHOT_TTL_ENV = "INFO2ACTION_REMOTE_SNAPSHOT_TTL_SEC"
REMOTE_FEED_LIVE_TIMEOUT_MS_ENV = "INFO2ACTION_REMOTE_FEED_LIVE_TIMEOUT_MS"
SUBCATEGORY_LIVE_TIMEOUT_MS_ENV = "INFO2ACTION_SUBCATEGORY_LIVE_TIMEOUT_MS"
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
REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV = "INFO2ACTION_REMOTE_RUNNING_FETCH_MAX_AGE_MINUTES"
FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV = "INFO2ACTION_FETCH_RUN_HEARTBEAT_GRACE_SEC"
# 稳定性加固(2026-07-10 BF-0710-fetch-guards): 运行时(非重启)判活/回收孤儿用的更宽
# 心跳容忍窗口。基础 grace(600s)用于重启恢复(进程边界=确定性,可激进);运行时守卫
# 与运行时 stale 回收用这个更大的窗口,让一次瞬时 DB 承压(心跳走 2s checkout 的
# connect(),承压时最先失败)不会把仍在跑的 run 误判成孤儿→放行第二条 pipeline→
# 压力更大的正反馈。默认 1800s(30 个 60s 心跳周期)。
FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV = "INFO2ACTION_FETCH_RUN_RUNTIME_STALE_GRACE_SEC"
INFO_READ_MODEL_ENV = "INFO2ACTION_INFO_READ_MODEL"
INFO_READ_MODEL_REFRESH_ENV = "INFO2ACTION_INFO_READ_MODEL_REFRESH"
# BF-0706-4: 跨进程单飞锁 —— 防止一次重建跑过 min_interval 时新请求并发再起一次重建
# 造成叠加风暴(Supabase compute 被压崩)。pg advisory lock 会话级,连接关闭自动释放。
_INFO_READ_MODEL_BUILD_LOCK_KEY = 517070604
INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV = "INFO2ACTION_INFO_READ_MODEL_REFRESH_TIMEOUT_MS"
INFO_READ_MODEL_INCREMENTAL_ENV = "INFO2ACTION_INFO_READ_MODEL_INCREMENTAL"
INFO_READ_MODEL_PREWARM_SCOPES_ENV = "INFO2ACTION_INFO_READ_MODEL_PREWARM_SCOPES"
INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV = "INFO2ACTION_INFO_READ_MODEL_PREWARM_PAGE_LIMIT"
INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV = "INFO2ACTION_INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE"
INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV = "INFO2ACTION_INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS"
INFO_READ_MODEL_STATE_KEY = "feed_platforms_v1"
INFO_READ_MODEL_MIN_GITHUB_STARS = 50
INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT = 180000
INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT = 5000
# BF-0710-1: delta 刷新按时间窗分片,每轮只吃 (水位, min_start+窗口] 的内容并独立提交,
# 循环追赶;长积压不再变成一条撞 statement_timeout 的巨型 SQL。
INFO_READ_MODEL_DELTA_WINDOW_HOURS_ENV = "INFO2ACTION_INFO_READ_MODEL_DELTA_WINDOW_HOURS"
INFO_READ_MODEL_DELTA_WINDOW_HOURS_DEFAULT = 6
# BF-0710-1: 刷新失败指数退避封顶 2h —— 失败无退避的每 10min 原样重试
# 会把偶发拥塞滚成死循环(BF-0708 系列 P0 的库侧根因)。
INFO_READ_MODEL_REFRESH_BACKOFF_CAP_SEC = 7200
# 剩余墙钟预算低于此值时不再开启新一轮(避免注定超时的半轮)。
_INFO_READ_MODEL_DELTA_ROUND_FLOOR_MS = 30000
_INFO_READ_MODEL_DELTA_MAX_ROUNDS = 100
INFO_READ_MODEL_RETAIN_COMPLETE_VERSIONS = 1
INFO_READ_MODEL_PRUNE_TRANSIENT_AGE_HOURS = 6
INFO_READ_MODEL_SORT_POLICY = "published_at_desc_v1"
# perf-v27 P4: info 读模型收缩为「首屏预算」形态(目标架构定稿 §0-2/§5-5)。
# 只物化 section_category 维度(信息默认页 = 每模块「全部」pill 首屏),
# 热窗口 7 天、每 scope 封顶 TOP_N 行;其余维度(platform/source/category/
# group)与超出部分全部走 live 现场查(既有回退路径,索引已备)。
# 313k 行乘法式物化由此饿死:~14 scopes × 50 = ~700 行。
INFO_READ_MODEL_WINDOW_DAYS = 7
INFO_READ_MODEL_SCOPE_TOP_N = 50
# 版本形态指纹:delta 路径发现 active 版本不是本形态时,升级为全量重建换版
# (仿 sort_policy 自愈)。防止 delta 的窗口/封顶 prune 跑在旧 313k 行版本上
# 做巨型 DELETE。升版本形态(改维度/窗口/帽)时必须改这个串。
INFO_READ_MODEL_SCOPE_PROFILE = "section_category_top50_7d_v1"
INFO_READ_MODEL_PREWARM_SCOPES_DEFAULT = 2
INFO_READ_MODEL_PREWARM_PAGE_LIMIT_DEFAULT = 20
INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_DEFAULT = 1
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
HIGHLIGHTS_DISPLAY_THRESHOLD_ENV = "INFO2ACTION_HIGHLIGHTS_DISPLAY_THRESHOLD"
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
FEED_MORE_TIMEOUT_MS_ENV = "INFO2ACTION_FEED_MORE_TIMEOUT_MS"
FEED_MORE_TIMEOUT_MS_DEFAULT = 6000
HIGHLIGHTS_READ_MODEL_STATE_KEY = "highlights_events_v1"
HIGHLIGHTS_READ_MODEL_VERSION = "highlights_v1"
HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS = 50
# perf-v27 P4a: 精选热窗口 30→7 天(目标架构定稿 §0-1,用户拍板)。
# 精选 Tab 滑到 7 天即到底;>7 天的老事件从精选消失(含仍在发酵的长尾
# 事件——产品已认可)。翻旧精选走搜索。窗口收窄同时缩小物化/decisions
# 同步/新鲜度探针的扫描范围。
HIGHLIGHTS_READ_MODEL_WINDOW_DAYS = 7
# v25.0 F-B 双因子精选分：LLM 质量（max/avg 加权 + 薄证据收缩）× ln(1+独立源数)。
# 常量同时内插进 decisions SQL，Python 版是数值语义的参考实现（测试锚点）。
HIGHLIGHT_SCORE_W_MAX = 0.6
HIGHLIGHT_SCORE_W_AVG = 0.4
HIGHLIGHT_SCORE_SHRINK_K = 1.0
HIGHLIGHT_SCORE_PRIOR = 0.5
HIGHLIGHT_SCORE_EVIDENCE_NORM_SOURCES = 8
ACTION_BOARD_RESULT_CACHE_TTL_ENV = "INFO2ACTION_ACTIONS_BOARD_CACHE_TTL_SEC"
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
_INFO_READ_MODEL_REFRESH_LOCK = threading.Lock()
_INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
_INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES = 0
_HIGHLIGHTS_READ_MODEL_REFRESH_LOCK = threading.Lock()
_HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
_HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES = 0
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


class RemoteDBTimeoutError(RemoteDBError):
    """A query exceeded its statement_timeout.

    Distinct from other RemoteDBError subclasses because callers may degrade
    gracefully (serve stale data / empty state) instead of surfacing an error:
    the database is reachable, this particular query was just too slow.
    See BF-0708-3.
    """


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


def event_read_backend() -> str:
    return _backend_for(BACKEND_ENV)


def feed_read_backend() -> str:
    return _backend_for(FEED_BACKEND_ENV)


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


def remote_schema() -> str:
    schema = (_runtime_env().get(REMOTE_SCHEMA_ENV) or DEFAULT_REMOTE_SCHEMA).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RemoteDBConfigError(f"Invalid {REMOTE_SCHEMA_ENV}: {schema!r}")
    return schema


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


def _cache_max_entries() -> int:
    return _env_int(_runtime_env(), REMOTE_CACHE_MAX_ENTRIES_ENV, 4096, min_value=64)


def _cache_max_bytes() -> int:
    return _env_int(_runtime_env(), REMOTE_CACHE_MAX_MB_ENV, 192, min_value=8) * 1024 * 1024


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


def clear_actions_board_cache_keys() -> int:
    removed = 0
    with _CACHE_LOCK:
        for key in list(_CACHE_TOKEN_INDEX.get("actions_board_result", ())):
            if isinstance(key, tuple) and key and key[0] == "actions_board_result":
                if _cache_remove_locked(key):
                    removed += 1
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
        max_idle = _env_int(env, REMOTE_DB_POOL_MAX_IDLE_ENV, 120, min_value=10)
        _POOL = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=float(timeout),
            open=True,
            # check runs a liveness probe before handing a connection to the caller.
            # The Supabase transaction pooler (port 6543) silently recycles idle
            # server-side connections; without this probe the pool hands out a dead
            # connection and the first query fails with EDBHANDLEREXITED. Only the
            # 30-minute /api/auth/refresh path stayed idle long enough to hit it,
            # which is why it alone returned 500. See BF-0708-1.
            check=ConnectionPool.check_connection,
            # Retire connections above min_size before the pooler's idle window
            # closes them. min_size connections stay resident, so `check` above
            # remains the load-bearing guard, not this.
            max_idle=float(max_idle),
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


def _wrap_remote_db_exception(exc: Exception) -> RemoteDBError:
    """Classify a raw driver exception into the right RemoteDBError subclass.

    A statement_timeout cancellation means the database is healthy but this
    query was too slow — callers can degrade to stale/empty data. Everything
    else is a genuine failure and must stay loud. See BF-0708-3.
    """
    try:
        import psycopg
    except ImportError:
        return RemoteDBError(f"Remote DB connection/query failed: {exc}")

    if isinstance(exc, psycopg.errors.QueryCanceled):
        return RemoteDBTimeoutError(f"Remote DB query timed out: {exc}")
    return RemoteDBError(f"Remote DB connection/query failed: {exc}")


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
            raise _wrap_remote_db_exception(exc) from exc

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
        raise _wrap_remote_db_exception(exc) from exc
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


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _timestamp_value(value: Any) -> str | None:
    return to_utc_iso(value) if value else None
