"""Disk-backed LRU cache for remote (Supabase Storage) assets.

P0-4 (C 端放量, docs/plans/2026-07-03-C端放量问题清单.md): /images 和 /lw
之前每次请求都从 Supabase Storage 下载字节,且在 async 路由里同步调用
urllib(阻塞整个事件循环)。本模块给这两条路径提供:

- data/asset_cache 下的磁盘缓存,按容量上限做 mtime-LRU 淘汰,重启后仍命中
  (部署重启是进程内缓存全冷的主因,见 P1-2);
- 条带锁 singleflight:并发 miss 同一对象时只有一个线程去下载;
- 短 TTL 负缓存:不存在的对象不会被反复打到 Storage。

调用方必须在线程池里执行 get_or_fetch(例如 starlette run_in_threadpool),
本模块所有 IO 都是同步的。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable

ASSET_CACHE_DIR_ENV = "INFO2ACTION_ASSET_CACHE_DIR"
ASSET_CACHE_MAX_MB_ENV = "INFO2ACTION_ASSET_CACHE_MAX_MB"
ASSET_CACHE_NEGATIVE_TTL_ENV = "INFO2ACTION_ASSET_CACHE_NEGATIVE_TTL_SEC"
ASSET_CACHE_DISABLED_ENV = "INFO2ACTION_ASSET_CACHE_DISABLED"

_DEFAULT_MAX_MB = 512
_DEFAULT_NEGATIVE_TTL_SEC = 60

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 条带锁:固定数量,零增长;同 key 必然落在同一条带上,保证 singleflight。
_STRIPE_COUNT = 64
_stripe_locks = [threading.Lock() for _ in range(_STRIPE_COUNT)]

_negative_lock = threading.Lock()
_negative: dict[str, float] = {}
_NEGATIVE_MAX_ENTRIES = 4096

_sweep_lock = threading.Lock()
_bytes_since_sweep = 0
_bytes_since_sweep_lock = threading.Lock()


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _disabled() -> bool:
    return _truthy(os.environ.get(ASSET_CACHE_DISABLED_ENV))


def _cache_dir() -> str:
    raw = (os.environ.get(ASSET_CACHE_DIR_ENV) or "").strip()
    return raw or os.path.join(_BASE, "data", "asset_cache")


def _max_bytes() -> int:
    raw = (os.environ.get(ASSET_CACHE_MAX_MB_ENV) or "").strip()
    try:
        mb = int(raw) if raw else _DEFAULT_MAX_MB
    except (ValueError, TypeError):
        mb = _DEFAULT_MAX_MB
    return max(1, mb) * 1024 * 1024


def _negative_ttl_sec() -> float:
    raw = (os.environ.get(ASSET_CACHE_NEGATIVE_TTL_ENV) or "").strip()
    try:
        ttl = float(raw) if raw else _DEFAULT_NEGATIVE_TTL_SEC
    except (ValueError, TypeError):
        ttl = _DEFAULT_NEGATIVE_TTL_SEC
    return max(0.0, ttl)


def _entry_path(object_path: str) -> str:
    digest = hashlib.sha256(object_path.encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), digest[:2], digest)


def _stripe_for(object_path: str) -> threading.Lock:
    digest = hashlib.sha256(object_path.encode("utf-8")).digest()
    return _stripe_locks[digest[0] % _STRIPE_COUNT]


def _read_entry(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        # mtime 即 LRU recency;失败不影响命中。
        os.utime(path, None)
    except OSError:
        pass
    return data


def _negative_hit(object_path: str) -> bool:
    ttl = _negative_ttl_sec()
    if ttl <= 0:
        return False
    now = time.monotonic()
    with _negative_lock:
        expires_at = _negative.get(object_path)
        if expires_at is None:
            return False
        if expires_at <= now:
            _negative.pop(object_path, None)
            return False
        return True


def _negative_set(object_path: str) -> None:
    ttl = _negative_ttl_sec()
    if ttl <= 0:
        return
    now = time.monotonic()
    with _negative_lock:
        if len(_negative) >= _NEGATIVE_MAX_ENTRIES:
            expired = [k for k, exp in _negative.items() if exp <= now]
            for k in expired:
                _negative.pop(k, None)
            if len(_negative) >= _NEGATIVE_MAX_ENTRIES:
                _negative.clear()
        _negative[object_path] = now + ttl


def _write_entry(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _iter_entries(root: str):
    try:
        subdirs = os.scandir(root)
    except OSError:
        return
    with subdirs:
        for sub in subdirs:
            if not sub.is_dir(follow_symlinks=False):
                continue
            try:
                files = os.scandir(sub.path)
            except OSError:
                continue
            with files:
                for entry in files:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield entry.path, stat.st_size, stat.st_mtime


def _sweep_if_needed(written: int) -> None:
    """按写入量节流的 LRU 淘汰:超容量时按 mtime 从旧到新删到 90% 以下。"""
    global _bytes_since_sweep
    max_bytes = _max_bytes()
    threshold = max(32 * 1024 * 1024, max_bytes // 16)
    with _bytes_since_sweep_lock:
        _bytes_since_sweep += written
        if _bytes_since_sweep < threshold:
            return
        _bytes_since_sweep = 0

    if not _sweep_lock.acquire(blocking=False):
        return  # 已有线程在清理
    try:
        entries = list(_iter_entries(_cache_dir()))
        total = sum(size for _, size, _ in entries)
        if total <= max_bytes:
            return
        target = int(max_bytes * 0.9)
        entries.sort(key=lambda e: e[2])  # oldest mtime first
        for path, size, _ in entries:
            if total <= target:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue
    finally:
        _sweep_lock.release()


def get_or_fetch(object_path: str, fetch: Callable[[], bytes | None]) -> bytes | None:
    """Return cached bytes for object_path, fetching (and caching) on miss.

    fetch() 返回 None 表示对象不存在(进入负缓存);fetch() 抛出的异常原样
    冒泡且不缓存。必须在线程池中调用。
    """
    if _disabled():
        return fetch()

    path = _entry_path(object_path)
    data = _read_entry(path)
    if data is not None:
        return data
    if _negative_hit(object_path):
        return None

    with _stripe_for(object_path):
        # singleflight:拿到锁后重查,前一个 leader 可能已经写入。
        data = _read_entry(path)
        if data is not None:
            return data
        if _negative_hit(object_path):
            return None
        data = fetch()
        if data is None:
            _negative_set(object_path)
            return None
        try:
            _write_entry(path, data)
        except OSError:
            return data  # 磁盘异常时退化为直通,不影响响应

    _sweep_if_needed(len(data))
    return data


def put(object_path: str, data: bytes) -> None:
    """B1: 写入/刷新缓存条目并清除对应负缓存。

    供"服务端生成后回填"场景(如 ffmpeg 抽帧、CDN 冷取回)——若只依赖
    get_or_fetch,生成前的 miss 会留下 60s 负缓存,导致刚生成的资产在
    窗口期内被误判不存在。
    """
    if _disabled() or data is None:
        return
    with _negative_lock:
        _negative.pop(object_path, None)
    try:
        _write_entry(_entry_path(object_path), data)
    except OSError:
        return
    _sweep_if_needed(len(data))


def clear() -> None:
    """Drop negative cache and all disk entries (tests / manual ops)."""
    with _negative_lock:
        _negative.clear()
    for path, _, _ in list(_iter_entries(_cache_dir())):
        try:
            os.remove(path)
        except OSError:
            pass
