"""Stage A — 入库 + canonical_url（极简版 v0）

设计稿 docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5

每条 enriched item（ai_summary + ai_keywords 都有）由本模块加两样信号：
  1. embedding：BGE-M3 × aikw 1024 维 float32（L2 归一化）
  2. canonical_url：去 utm_*/fbclid/gclid/ref/source/from + fragment 后的 URL

写回 items 表的 7 个 Stage A 列，幂等，失败 retry 3 次。

不做：实体抽取 / content 清洗 / 短链解析 / 媒体 pHash。
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np

from .vector_utils import pack_blob

LOGGER = logging.getLogger(__name__)

EMBEDDING_MODEL_ID = "BAAI/bge-m3"
EMBEDDING_MODEL_NAME = "bge-m3"
EMBEDDING_INPUT_VARIANT = "aikw"
EMBEDDING_DIM = 1024
EMBEDDING_MAX_SEQ_LENGTH = 1024
EMBEDDING_BATCH_SIZE = 8

CANONICAL_URL_DROP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_referrer",
    "fbclid", "gclid", "msclkid", "yclid", "dclid",
    "ref", "ref_src", "ref_url", "source", "from", "spm",
})

RETRY_LIMIT = 3


def _aikw_text(item: Mapping[str, object]) -> str:
    title = (item.get("title") or "").strip() if isinstance(item.get("title"), str) else ""
    ai_summary = (item.get("ai_summary") or "").strip() if isinstance(item.get("ai_summary"), str) else ""
    ai_keywords = (item.get("ai_keywords") or "").strip() if isinstance(item.get("ai_keywords"), str) else ""
    parts: list[str] = []
    if title:
        parts.append(title)
    if ai_summary:
        parts.append(ai_summary)
    if ai_keywords:
        parts.append(f"关键词: {ai_keywords}")
    if not parts:
        return f"(empty item {item.get('id') or 'unknown'})"
    return "\n\n".join(parts)


def extract_canonical_url(url: object) -> str | None:
    """归一化 URL：去 utm_*/fbclid/gclid/ref/source/from + fragment + lowercase host。

    不做短链解析；外部重定向不动。返回 None 当输入为空或不可解析。
    """
    if not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    kept_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in CANONICAL_URL_DROP_PARAMS
    ]
    new_query = urlencode(kept_pairs, doseq=True)
    new = parts._replace(
        netloc=parts.netloc.lower(),
        query=new_query,
        fragment="",
    )
    return urlunsplit(new)


_ENCODER = None


def _get_encoder():
    """Lazy-load SentenceTransformer(BGE-M3) once per process。

    首次加载约 35-40s（本地 cache 命中时 1-3s）。后续 encode() 复用。
    """
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:  # pragma: no cover - install error
        raise RuntimeError(
            "sentence-transformers 未安装。请用 `uv run --with sentence-transformers …` "
            "或 `pip install sentence-transformers` 后重试。"
        ) from e
    started = time.time()
    LOGGER.info("loading %s (this may download model on first run)…", EMBEDDING_MODEL_ID)
    model = SentenceTransformer(EMBEDDING_MODEL_ID)
    if hasattr(model, "max_seq_length"):
        model.max_seq_length = EMBEDDING_MAX_SEQ_LENGTH
    LOGGER.info("loaded %s in %.1fs", EMBEDDING_MODEL_ID, time.time() - started)
    _ENCODER = model
    return model


def encode_aikw(items: list[Mapping[str, object]]) -> np.ndarray:
    """对一批 item 跑 BGE-M3 × aikw embedding。返回 N×1024 float32（L2 归一化）。"""
    if not items:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    model = _get_encoder()
    texts = [_aikw_text(it) for it in items]
    arr = model.encode(
        texts,
        batch_size=EMBEDDING_BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] != len(items) or arr.shape[1] != EMBEDDING_DIM:
        raise RuntimeError(
            f"BGE-M3 encode 输出形状异常 expected ({len(items)},{EMBEDDING_DIM}) got {arr.shape}"
        )
    return arr


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_pending_item_ids(conn, *, limit: int | None = None) -> list[str]:
    """选出待跑 Stage A 的 item id：ai_summary 非空 且 (stage_a_state IS NULL OR 'failed')。"""
    sql = (
        "SELECT id FROM items "
        "WHERE ai_summary IS NOT NULL AND TRIM(ai_summary) != '' "
        "  AND (stage_a_state IS NULL OR stage_a_state = 'failed') "
        "ORDER BY fetched_at DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return [row["id"] for row in conn.execute(sql, params).fetchall()]


def _load_items(conn, item_ids: list[str]) -> list[dict]:
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT id, title, ai_summary, ai_keywords, url FROM items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _write_done(conn, item_id: str, embedding: np.ndarray, canonical_url: str | None,
                generated_at: str) -> None:
    conn.execute(
        """UPDATE items SET
             embedding = ?,
             embedding_model = ?,
             embedding_input_variant = ?,
             embedding_generated_at = ?,
             canonical_url = ?,
             stage_a_state = 'done',
             stage_a_failed_at = NULL
           WHERE id = ?""",
        (
            pack_blob(embedding),
            EMBEDDING_MODEL_NAME,
            EMBEDDING_INPUT_VARIANT,
            generated_at,
            canonical_url,
            item_id,
        ),
    )


def _write_failed(conn, item_ids: Iterable[str]) -> None:
    failed_at = _utc_now_iso()
    for iid in item_ids:
        conn.execute(
            "UPDATE items SET stage_a_state = 'failed', stage_a_failed_at = ? WHERE id = ?",
            (failed_at, iid),
        )


def stage_a_run(conn, *, item_ids: list[str] | None = None, limit: int | None = None,
                batch_size: int = 64) -> dict:
    """Stage A 主入口。

    Args:
        conn: sqlite3.Connection
        item_ids: 指定 id 列表；None 则自动选 pending
        limit: 自动选 pending 时的上限
        batch_size: 每批送入 BGE-M3 的 item 数（控制内存）

    Returns:
        统计 dict：{processed, succeeded, failed, skipped_no_summary, took_seconds}
    """
    started = time.time()
    if item_ids is None:
        target_ids = query_pending_item_ids(conn, limit=limit)
    else:
        target_ids = list(item_ids)

    stats = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped_no_summary": 0,
        "took_seconds": 0.0,
    }
    if not target_ids:
        stats["took_seconds"] = round(time.time() - started, 2)
        return stats

    for batch_start in range(0, len(target_ids), batch_size):
        batch_ids = target_ids[batch_start:batch_start + batch_size]
        items = _load_items(conn, batch_ids)
        # 过滤掉 ai_summary 空的（保护：query_pending_item_ids 已过滤一次）
        usable = [it for it in items if (it.get("ai_summary") or "").strip()]
        skipped = len(items) - len(usable)
        stats["skipped_no_summary"] += skipped

        if not usable:
            continue

        last_err: Exception | None = None
        embeddings: np.ndarray | None = None
        for attempt in range(1, RETRY_LIMIT + 1):
            try:
                embeddings = encode_aikw(usable)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                LOGGER.warning("BGE-M3 encode 第 %d 次失败 batch_size=%d err=%s",
                               attempt, len(usable), e)
                if attempt < RETRY_LIMIT:
                    time.sleep(2 ** (attempt - 1))

        if embeddings is None or last_err is not None:
            _write_failed(conn, [it["id"] for it in usable])
            conn.commit()
            stats["failed"] += len(usable)
            stats["processed"] += len(usable)
            continue

        generated_at = _utc_now_iso()
        for item, vec in zip(usable, embeddings):
            canonical = extract_canonical_url(item.get("url"))
            try:
                _write_done(conn, item["id"], vec, canonical, generated_at)
                stats["succeeded"] += 1
            except Exception as e:  # noqa: BLE001
                LOGGER.warning("写 Stage A 字段失败 id=%s err=%s", item.get("id"), e)
                _write_failed(conn, [item["id"]])
                stats["failed"] += 1
            stats["processed"] += 1
        conn.commit()

    stats["took_seconds"] = round(time.time() - started, 2)
    return stats
