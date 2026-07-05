#!/usr/bin/env python3
"""Offline confirmed-edge event clustering experiment.

This script validates the "embedding recall -> concurrent LLM edge judge ->
deterministic reducer -> confirmed clusters" design.

By default it reads items from data/feed.db, uses existing item embeddings, and
writes only JSONL/Markdown artifacts under logs/. With --write-db it also writes
the confirmed experiment output into the local clusters tables for UI review.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clustering import vector_utils as vu  # noqa: E402
from clustering.summary_writer import _call_llm_chat  # noqa: E402
from utils.url_normalize import normalize_url  # noqa: E402


_GITHUB_RE = re.compile(r"github\.com/([^/\s?#]+)/([^/\s?#]+)", re.I)
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.I)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


@dataclass(frozen=True)
class Item:
    id: str
    platform: str
    source: str
    title: str
    content: str
    author_name: str
    url: str
    published_at: str
    fetched_at: str
    ai_summary: str
    ai_key_points: str
    ai_keywords: str
    ai_category: str
    content_type: str
    embedding: np.ndarray
    source_identity: str


class UnionFind:
    def __init__(self, ids: list[str]):
        self.parent = {x: x for x in ids}
        self.size = {x: 1 for x in ids}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            parent = self.parent[x]
            self.parent[x] = root
            x = parent
        return root

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def _load_config() -> dict[str, Any]:
    path = BASE / "config" / "config.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _source_identity(row: sqlite3.Row) -> str:
    url = (row["url"] or "").strip()
    item_id = row["id"]
    platform = (row["platform"] or "").strip()
    if url:
        arxiv = _ARXIV_RE.search(url)
        if arxiv:
            return f"arxiv:{arxiv.group(1).lower()}"
        github = _GITHUB_RE.search(url)
        if github:
            owner = github.group(1).strip().lower()
            repo = github.group(2).strip()
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            repo = repo.lower()
            return f"github:{owner}/{repo}"
        try:
            normalized = normalize_url(url)
            if normalized.platform in ("twitter", "youtube") and normalized.canonical_url:
                return normalized.canonical_url
        except Exception:
            pass
        return url
    if platform == "twitter" and str(item_id).isdigit():
        return f"https://x.com/i/status/{item_id}"
    return str(item_id)


def _load_items(limit: int | None = None) -> list[Item]:
    conn = sqlite3.connect(BASE / "data" / "feed.db")
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, platform, source, title, content, author_name, url,
               published_at, fetched_at, ai_summary, ai_key_points,
               ai_keywords, ai_category, content_type, embedding
          FROM items
         WHERE embedding IS NOT NULL
         ORDER BY COALESCE(published_at, fetched_at) ASC
    """
    if limit:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (int(limit),)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    items: list[Item] = []
    for r in rows:
        vec = vu.unpack_blob(r["embedding"])
        if vec is None:
            continue
        items.append(
            Item(
                id=r["id"],
                platform=r["platform"] or "",
                source=r["source"] or "",
                title=r["title"] or "",
                content=r["content"] or "",
                author_name=r["author_name"] or "",
                url=r["url"] or "",
                published_at=r["published_at"] or "",
                fetched_at=r["fetched_at"] or "",
                ai_summary=r["ai_summary"] or "",
                ai_key_points=r["ai_key_points"] or "",
                ai_keywords=r["ai_keywords"] or "",
                ai_category=r["ai_category"] or "",
                content_type=r["content_type"] or "",
                embedding=np.asarray(vec, dtype=np.float32),
                source_identity=_source_identity(r),
            )
        )
    conn.close()
    return items


def _jsonl_write(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "\n[trimmed]"


def _parse_key_points(raw: str, max_lines: int = 5) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return _clip(raw, 400)
    if not isinstance(data, list):
        return _clip(raw, 400)
    lines: list[str] = []
    for item in data[:max_lines]:
        if isinstance(item, str):
            lines.append(f"- {item}")
        elif isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            points = item.get("points")
            if title:
                lines.append(f"- {title}")
            if isinstance(points, list):
                for p in points[:3]:
                    if p:
                        lines.append(f"  - {p}")
        elif item:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _doc_block(item: Item, *, max_content: int = 1200) -> str:
    parts = [
        f"item_id: {item.id}",
        f"platform: {item.platform}",
        f"source: {item.source}",
        f"author: {item.author_name}",
        f"published_at: {item.published_at or item.fetched_at}",
        f"category: {item.ai_category}",
        f"content_type: {item.content_type}",
        f"url: {item.url}",
        f"title: {item.title}",
        "",
        "summary:",
        item.ai_summary or "(none)",
    ]
    key_points = _parse_key_points(item.ai_key_points)
    if key_points:
        parts += ["", "key_points:", key_points]
    if item.ai_keywords:
        parts += ["", f"keywords: {item.ai_keywords}"]
    if item.content:
        parts += ["", "content:", _clip(item.content, max_content)]
    return "\n".join(parts)


def _job_task_id(job: dict[str, Any]) -> str:
    return f"task_{job['item_id']}"


def _cluster_job_task_id(job: dict[str, Any]) -> str:
    return f"cluster_task_{job['item_id']}"


def _candidate_docs_block(
    candidates: list[dict[str, Any]],
    by_id: dict[str, Item],
    *,
    candidate_doc_content_chars: int,
) -> str:
    candidate_blocks = []
    for c in candidates:
        item = by_id[c["candidate_item_id"]]
        block = _doc_block(item, max_content=candidate_doc_content_chars)
        candidate_blocks.append(
            f"candidate_item_id: {item.id}\n"
            f"cosine_recall: {c['cosine']:.4f}\n"
            f"recall_reason: {c['recall_reason']}\n"
            f"{block}"
        )
    return "\n\n---\n\n".join(candidate_blocks)


def _job_block(
    job: dict[str, Any],
    by_id: dict[str, Item],
    *,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> str:
    item = by_id[job["item_id"]]
    candidate_docs = _candidate_docs_block(
        job["candidates"],
        by_id,
        candidate_doc_content_chars=candidate_doc_content_chars,
    )
    return f"""task_id: {_job_task_id(job)}

New Doc:
{_doc_block(item, max_content=new_doc_content_chars)}

Candidate Docs:
{candidate_docs}
"""


def _batch_prompt(
    jobs: list[dict[str, Any]],
    by_id: dict[str, Item],
    *,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> str:
    task_blocks = "\n\n==========\n\n".join(
        _job_block(
            job,
            by_id,
            new_doc_content_chars=new_doc_content_chars,
            candidate_doc_content_chars=candidate_doc_content_chars,
        )
        for job in jobs
    )
    return f"""You judge whether candidate documents describe the same concrete event as each task's New Doc.

Rules:
- Same broad topic is not enough.
- Same company but different product/action is not enough.
- same_event=true requires a concrete shared entity: product/version, repo URL, paper id, company action, launch, incident, acquisition, funding, model release, or another named event subject.
- Short or vague texts without a concrete shared entity should be same_event=false.
- Be conservative: uncertain means false.
- Treat every task independently. Do not infer same_event from another task.
- Return strict JSON only. Include every candidate in each task's matches.

Tasks:
{task_blocks}

Output schema:
{{
  "tasks": [
    {{
      "task_id": "task_<new_item_id>",
      "new_doc_fingerprint": {{
        "subject": "...",
        "action": "...",
        "time": "...",
        "event_type": "product_launch|tool_release|model_update|industry_news|tutorial_case|technical_insight|opinion|resource_collection|other"
      }},
      "matches": [
        {{
          "candidate_item_id": "...",
          "same_event": true,
          "confidence": "high|medium|low",
          "relationship": "same_event|direct_commentary|follow_up_update|same_topic_only|unrelated",
          "shared_entity": "specific shared entity if true, else empty string",
          "rationale": "short reason"
        }}
      ]
    }}
  ]
}}
"""


def _parse_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("matches"), list):
        return None
    return obj


def _parse_batch_json(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    tasks = obj.get("tasks")
    if tasks is None:
        tasks = obj.get("results")
    if not isinstance(tasks, list):
        return None
    cleaned = [x for x in tasks if isinstance(x, dict)]
    return cleaned


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _clean_match(match: dict[str, Any]) -> dict[str, Any] | None:
    candidate_id = str(match.get("candidate_item_id") or "").strip()
    if not candidate_id:
        return None
    same_raw = match.get("same_event")
    if isinstance(same_raw, str):
        same_event = same_raw.strip().lower() in ("true", "yes", "1")
    else:
        same_event = bool(same_raw)
    confidence = str(match.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    relationship = str(match.get("relationship") or "unrelated").strip().lower()
    if relationship not in (
        "same_event",
        "direct_commentary",
        "follow_up_update",
        "same_topic_only",
        "unrelated",
    ):
        relationship = "unrelated"
    shared_entity = str(match.get("shared_entity") or "").strip()
    if same_event and relationship in ("same_topic_only", "unrelated"):
        same_event = False
    if same_event and not shared_entity:
        # Accuracy guard: do not confirm vague true verdicts.
        same_event = False
        confidence = "low"
        relationship = "unrelated"
    return {
        "candidate_item_id": candidate_id,
        "same_event": same_event,
        "confidence": confidence,
        "relationship": relationship,
        "shared_entity": shared_entity,
        "rationale": str(match.get("rationale") or "")[:500],
    }


def _clean_cluster_match(match: dict[str, Any]) -> dict[str, Any] | None:
    cluster_id = str(match.get("candidate_cluster_id") or "").strip()
    if not cluster_id:
        return None
    same_raw = match.get("same_event")
    if isinstance(same_raw, str):
        same_event = same_raw.strip().lower() in ("true", "yes", "1")
    else:
        same_event = bool(same_raw)
    confidence = str(match.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    relationship = str(match.get("relationship") or "unrelated").strip().lower()
    if relationship not in (
        "same_event",
        "direct_commentary",
        "follow_up_update",
        "same_topic_only",
        "unrelated",
    ):
        relationship = "unrelated"
    shared_entity = str(match.get("shared_entity") or "").strip()
    if same_event and relationship in ("same_topic_only", "unrelated"):
        same_event = False
    if same_event and not shared_entity:
        same_event = False
        confidence = "low"
        relationship = "unrelated"
    return {
        "candidate_cluster_id": cluster_id,
        "same_event": same_event,
        "confidence": confidence,
        "relationship": relationship,
        "shared_entity": shared_entity,
        "rationale": str(match.get("rationale") or "")[:700],
    }


def _build_jobs(
    items: list[Item],
    *,
    cosine_min: float,
    top_k: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ids = [x.id for x in items]
    matrix = np.stack([x.embedding for x in items]).astype(np.float32)
    norm = np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    matrix = matrix / norm
    sim = matrix @ matrix.T

    exact_edges: list[dict[str, Any]] = []
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if item.source_identity:
            groups[item.source_identity].append(item.id)
    for identity, group in groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group)
        anchor = group_sorted[0]
        for other in group_sorted[1:]:
            exact_edges.append({
                "item_id": other,
                "candidate_item_id": anchor,
                "cosine": None,
                "recall_reason": "exact_identity",
                "source_identity": identity,
            })

    jobs: list[dict[str, Any]] = []
    exact_pairs = {
        tuple(sorted((e["item_id"], e["candidate_item_id"])))
        for e in exact_edges
    }
    for i, item in enumerate(items):
        candidates = []
        for j in range(i):
            score = float(sim[i, j])
            if score < cosine_min:
                continue
            pair = tuple(sorted((item.id, ids[j])))
            if pair in exact_pairs:
                continue
            candidates.append({
                "candidate_item_id": ids[j],
                "cosine": score,
                "recall_reason": "embedding_doc_doc",
            })
        candidates.sort(key=lambda x: x["cosine"], reverse=True)
        candidates = candidates[:max(0, int(top_k))]
        if not candidates:
            continue
        jobs.append({
            "item_id": item.id,
            "candidate_count": len(candidates),
            "max_cosine": round(candidates[0]["cosine"], 4),
            "min_cosine": round(candidates[-1]["cosine"], 4),
            "candidates": [
                {
                    **c,
                    "cosine": round(c["cosine"], 6),
                }
                for c in candidates
            ],
        })
    return jobs, exact_edges


def _result_from_task(
    job: dict[str, Any],
    task: dict[str, Any],
    *,
    elapsed_sec: float,
) -> dict[str, Any]:
    matches = []
    valid_candidates = {c["candidate_item_id"] for c in job["candidates"]}
    for raw_match in task.get("matches", []):
        if not isinstance(raw_match, dict):
            continue
        match = _clean_match(raw_match)
        if not match:
            continue
        if match["candidate_item_id"] not in valid_candidates:
            continue
        matches.append(match)
    fingerprint = task.get("new_doc_fingerprint")
    return {
        "item_id": job["item_id"],
        "task_id": _job_task_id(job),
        "candidate_count": job["candidate_count"],
        "ok": True,
        "elapsed_sec": elapsed_sec,
        "fingerprint": fingerprint if isinstance(fingerprint, dict) else {},
        "matches": matches,
    }


def _failed_result(job: dict[str, Any], error: str, *, elapsed_sec: float,
                   raw_preview: str = "", batch_size: int = 1) -> dict[str, Any]:
    out = {
        "item_id": job["item_id"],
        "task_id": _job_task_id(job),
        "candidate_count": job["candidate_count"],
        "ok": False,
        "error": error,
        "elapsed_sec": elapsed_sec,
        "batch_size": batch_size,
    }
    if raw_preview:
        out["raw_preview"] = raw_preview[:400]
    return out


def _judge_batch(
    batch: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    api_key: str,
    api_base: str | None,
    model: str,
    timeout: int,
    llm_max_tokens: int,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> list[dict[str, Any]]:
    started = time.time()
    prompt = _batch_prompt(
        batch,
        by_id,
        new_doc_content_chars=new_doc_content_chars,
        candidate_doc_content_chars=candidate_doc_content_chars,
    )
    try:
        raw = _call_llm_chat(
            api_key=api_key,
            api_base=api_base,
            model=model,
            system_prompt=prompt,
            user_content="Return the JSON now.",
            max_tokens=llm_max_tokens,
            timeout=timeout,
        )
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        return [_failed_result(job, str(e), elapsed_sec=elapsed, batch_size=len(batch)) for job in batch]
    parsed = _parse_batch_json(raw)
    elapsed = round(time.time() - started, 2)
    if parsed is None:
        preview = (raw or "")[:400]
        return [
            _failed_result(job, "batch_parse_fail", elapsed_sec=elapsed,
                           raw_preview=preview, batch_size=len(batch))
            for job in batch
        ]

    tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in parsed:
        task_id = str(task.get("task_id") or "").strip()
        if task_id:
            tasks_by_id[task_id] = task

    results: list[dict[str, Any]] = []
    for job in batch:
        task = tasks_by_id.get(_job_task_id(job))
        if task is None:
            results.append(_failed_result(
                job, "missing_task_result", elapsed_sec=elapsed,
                batch_size=len(batch),
            ))
            continue
        results.append(_result_from_task(job, task, elapsed_sec=elapsed))
    return results


def _estimate_job_chars(
    job: dict[str, Any],
    by_id: dict[str, Item],
    *,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> int:
    return len(_job_block(
        job,
        by_id,
        new_doc_content_chars=new_doc_content_chars,
        candidate_doc_content_chars=candidate_doc_content_chars,
    )) + 400


def _pack_batches(
    jobs: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    jobs_per_request: int,
    max_prompt_chars: int,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    max_jobs = max(1, int(jobs_per_request))
    budget = max(5000, int(max_prompt_chars))
    for job in jobs:
        job_chars = _estimate_job_chars(
            job,
            by_id,
            new_doc_content_chars=new_doc_content_chars,
            candidate_doc_content_chars=candidate_doc_content_chars,
        )
        if current and (
            len(current) >= max_jobs or current_chars + job_chars > budget
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(job)
        current_chars += job_chars
    if current:
        batches.append(current)
    return batches


def _estimate_cluster_job_chars(
    job: dict[str, Any],
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    *,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> int:
    return len(_cluster_job_block(
        job,
        by_id,
        cluster_by_id,
        cluster_edges,
        new_doc_content_chars=new_doc_content_chars,
        cluster_doc_limit=cluster_doc_limit,
        cluster_doc_content_chars=cluster_doc_content_chars,
    )) + 500


def _pack_cluster_batches(
    jobs: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    jobs_per_request: int,
    max_prompt_chars: int,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    max_jobs = max(1, int(jobs_per_request))
    budget = max(5000, int(max_prompt_chars))
    for job in jobs:
        job_chars = _estimate_cluster_job_chars(
            job,
            by_id,
            cluster_by_id,
            cluster_edges,
            new_doc_content_chars=new_doc_content_chars,
            cluster_doc_limit=cluster_doc_limit,
            cluster_doc_content_chars=cluster_doc_content_chars,
        )
        if current and (
            len(current) >= max_jobs or current_chars + job_chars > budget
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(job)
        current_chars += job_chars
    if current:
        batches.append(current)
    return batches


def _run_judges(
    jobs: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    api_key: str,
    api_base: str | None,
    model: str,
    workers: int,
    timeout: int,
    llm_max_tokens: int,
    jobs_per_request: int,
    max_prompt_chars: int,
    new_doc_content_chars: int,
    candidate_doc_content_chars: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    batches = _pack_batches(
        jobs,
        by_id=by_id,
        jobs_per_request=jobs_per_request,
        max_prompt_chars=max_prompt_chars,
        new_doc_content_chars=new_doc_content_chars,
        candidate_doc_content_chars=candidate_doc_content_chars,
    )
    print(
        f"[judge] packed {len(jobs)} jobs into {len(batches)} LLM requests "
        f"(jobs_per_request<={jobs_per_request}, max_prompt_chars={max_prompt_chars})",
        flush=True,
    )
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [
            pool.submit(
                _judge_batch,
                batch,
                by_id=by_id,
                api_key=api_key,
                api_base=api_base,
                model=model,
                timeout=timeout,
                llm_max_tokens=llm_max_tokens,
                new_doc_content_chars=new_doc_content_chars,
                candidate_doc_content_chars=candidate_doc_content_chars,
            )
            for batch in batches
        ]
        for idx, fut in enumerate(cf.as_completed(futures), 1):
            batch_results = fut.result()
            results.extend(batch_results)
            if idx % 5 == 0 or idx == len(futures):
                ok = sum(1 for r in results if r.get("ok"))
                fail = len(results) - ok
                print(
                    f"[judge] requests {idx}/{len(futures)} done "
                    f"job_results={len(results)}/{len(jobs)} ok={ok} fail={fail}",
                    flush=True,
                )
    results.sort(key=lambda r: r["item_id"])
    return results


def _run_doc_cluster_judges(
    jobs: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    clusters: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
    api_key: str,
    api_base: str | None,
    model: str,
    workers: int,
    timeout: int,
    llm_max_tokens: int,
    jobs_per_request: int,
    max_prompt_chars: int,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    cluster_by_id = {c["cluster_id"]: c for c in clusters}
    cluster_edges = _cluster_edges_by_members(clusters, accepted_edges)
    batches = _pack_cluster_batches(
        jobs,
        by_id=by_id,
        cluster_by_id=cluster_by_id,
        cluster_edges=cluster_edges,
        jobs_per_request=jobs_per_request,
        max_prompt_chars=max_prompt_chars,
        new_doc_content_chars=new_doc_content_chars,
        cluster_doc_limit=cluster_doc_limit,
        cluster_doc_content_chars=cluster_doc_content_chars,
    )
    print(
        f"[doc-cluster] packed {len(jobs)} jobs into {len(batches)} LLM requests "
        f"(jobs_per_request<={jobs_per_request}, max_prompt_chars={max_prompt_chars})",
        flush=True,
    )
    results: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [
            pool.submit(
                _judge_cluster_batch,
                batch,
                by_id=by_id,
                cluster_by_id=cluster_by_id,
                cluster_edges=cluster_edges,
                api_key=api_key,
                api_base=api_base,
                model=model,
                timeout=timeout,
                llm_max_tokens=llm_max_tokens,
                new_doc_content_chars=new_doc_content_chars,
                cluster_doc_limit=cluster_doc_limit,
                cluster_doc_content_chars=cluster_doc_content_chars,
            )
            for batch in batches
        ]
        for idx, fut in enumerate(cf.as_completed(futures), 1):
            batch_results = fut.result()
            results.extend(batch_results)
            if idx % 5 == 0 or idx == len(futures):
                ok = sum(1 for r in results if r.get("ok"))
                fail = len(results) - ok
                print(
                    f"[doc-cluster] requests {idx}/{len(futures)} done "
                    f"job_results={len(results)}/{len(jobs)} ok={ok} fail={fail}",
                    flush=True,
                )
    results.sort(key=lambda r: r["item_id"])
    return results


def _confirmed_from_results(
    items: list[Item],
    exact_edges: list[dict[str, Any]],
    judge_results: list[dict[str, Any]],
    *,
    doc_cluster_results: list[dict[str, Any]] | None = None,
    doc_cluster_members: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter]:
    by_id = {x.id: x for x in items}
    uf = UnionFind([x.id for x in items])
    accepted_edges: list[dict[str, Any]] = []
    reasons: Counter = Counter()

    for edge in exact_edges:
        uf.union(edge["item_id"], edge["candidate_item_id"])
        accepted_edges.append({
            **edge,
            "decision_source": "exact_identity",
            "confidence": "high",
            "relationship": "same_event",
        })
        reasons["accepted_exact"] += 1

    for result in judge_results:
        if not result.get("ok"):
            reasons["judge_failed"] += 1
            continue
        item_id = result["item_id"]
        any_accept = False
        for match in result.get("matches", []):
            same = match.get("same_event") is True
            conf = match.get("confidence")
            rel = match.get("relationship")
            if same and conf in ("high", "medium") and rel in (
                "same_event",
                "direct_commentary",
                "follow_up_update",
            ):
                uf.union(item_id, match["candidate_item_id"])
                accepted_edges.append({
                    "item_id": item_id,
                    "candidate_item_id": match["candidate_item_id"],
                    "decision_source": "llm_edge_judge",
                    **match,
                })
                reasons["accepted_llm"] += 1
                any_accept = True
        if not any_accept:
            reasons["judge_no_confirmed_edge"] += 1

    doc_cluster_members = doc_cluster_members or {}
    for result in doc_cluster_results or []:
        if not result.get("ok"):
            reasons["doc_cluster_judge_failed"] += 1
            continue
        item_id = result["item_id"]
        any_accept = False
        for match in result.get("matches", []):
            same = match.get("same_event") is True
            conf = match.get("confidence")
            rel = match.get("relationship")
            candidate_cluster_id = match.get("candidate_cluster_id")
            member_ids = doc_cluster_members.get(candidate_cluster_id) or []
            if same and conf in ("high", "medium") and rel in (
                "same_event",
                "direct_commentary",
                "follow_up_update",
            ) and member_ids:
                anchor = member_ids[0]
                for member_id in member_ids:
                    uf.union(item_id, member_id)
                accepted_edges.append({
                    "item_id": item_id,
                    "candidate_item_id": anchor,
                    "candidate_cluster_id": candidate_cluster_id,
                    "candidate_member_count": len(member_ids),
                    "decision_source": "llm_doc_cluster_judge",
                    **match,
                })
                reasons["accepted_doc_cluster"] += 1
                any_accept = True
        if not any_accept:
            reasons["doc_cluster_no_confirmed_edge"] += 1

    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[uf.find(item.id)].append(item.id)

    clusters: list[dict[str, Any]] = []
    for root, group in groups.items():
        if len(group) < 2:
            continue
        members = [by_id[x] for x in group]
        members.sort(key=lambda x: (x.published_at or x.fetched_at, x.id))
        source_count = len({m.source_identity for m in members})
        platforms = sorted({m.platform for m in members if m.platform})
        clusters.append({
            "cluster_id": f"exp_{len(clusters) + 1}",
            "member_count": len(members),
            "confirmed_source_count": source_count,
            # Visibility in this experiment means "has at least one confirmed
            # edge". Do not hide exact/LLM-confirmed clusters only because the
            # source identity count is 1; the user wants LLM-confirmed events
            # to be displayable independent of doc/source count.
            "visible": True,
            "platforms": platforms,
            "first_doc_at": members[0].published_at or members[0].fetched_at,
            "last_doc_at": members[-1].published_at or members[-1].fetched_at,
            "members": [
                {
                    "id": m.id,
                    "platform": m.platform,
                    "source": m.source,
                    "author": m.author_name,
                    "title": m.title,
                    "url": m.url,
                    "source_identity": m.source_identity,
                }
                for m in members
            ],
        })
    clusters.sort(key=lambda c: (c["member_count"], c["confirmed_source_count"]), reverse=True)
    return clusters, accepted_edges, reasons


def _cluster_member_ids(clusters: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        c["cluster_id"]: [m["id"] for m in c.get("members", []) if m.get("id")]
        for c in clusters
    }


def _centroid(items: list[Item]) -> np.ndarray | None:
    vecs = [np.asarray(x.embedding, dtype=np.float32) for x in items if x.embedding is not None]
    if not vecs:
        return None
    matrix = np.stack(vecs).astype(np.float32)
    mean = matrix.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm > 1e-12:
        mean = mean / norm
    return mean.astype(np.float32)


def _cluster_block(
    cluster: dict[str, Any],
    by_id: dict[str, Item],
    edges: list[dict[str, Any]],
    *,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> str:
    members = [by_id[m["id"]] for m in cluster.get("members", []) if m["id"] in by_id]
    entities = _cluster_shared_entities(edges)
    title = _cluster_review_title(members, entities)
    doc_blocks = []
    for item in members[: max(1, int(cluster_doc_limit))]:
        doc_blocks.append(_doc_block(item, max_content=cluster_doc_content_chars))
    edge_lines = []
    for edge in edges[:12]:
        entity = edge.get("shared_entity") or edge.get("source_identity") or ""
        rationale = edge.get("rationale") or edge.get("decision_source") or ""
        edge_lines.append(
            f"- {edge.get('item_id')} <-> {edge.get('candidate_item_id')}: "
            f"{entity}; {rationale}"
        )
    return "\n".join([
        f"candidate_cluster_id: {cluster['cluster_id']}",
        f"title_hint: {title}",
        f"member_count: {cluster['member_count']}",
        f"platforms: {', '.join(cluster.get('platforms') or [])}",
        f"time_range: {cluster.get('first_doc_at')} -> {cluster.get('last_doc_at')}",
        f"shared_entities: {', '.join(entities) if entities else '(none)'}",
        "",
        "confirmed_edges:",
        "\n".join(edge_lines) if edge_lines else "(none)",
        "",
        "representative_member_docs:",
        "\n\n---\n\n".join(doc_blocks),
    ])


def _cluster_candidates_block(
    candidates: list[dict[str, Any]],
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    *,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> str:
    blocks = []
    for candidate in candidates:
        cluster_id = candidate["candidate_cluster_id"]
        cluster = cluster_by_id[cluster_id]
        block = _cluster_block(
            cluster,
            by_id,
            cluster_edges.get(cluster_id, []),
            cluster_doc_limit=cluster_doc_limit,
            cluster_doc_content_chars=cluster_doc_content_chars,
        )
        blocks.append(
            f"candidate_cluster_id: {cluster_id}\n"
            f"cosine_recall: {candidate['cosine']:.4f}\n"
            f"recall_reason: {candidate['recall_reason']}\n"
            f"{block}"
        )
    return "\n\n---CLUSTER---\n\n".join(blocks)


def _cluster_job_block(
    job: dict[str, Any],
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    *,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> str:
    item = by_id[job["item_id"]]
    candidate_clusters = _cluster_candidates_block(
        job["candidates"],
        by_id,
        cluster_by_id,
        cluster_edges,
        cluster_doc_limit=cluster_doc_limit,
        cluster_doc_content_chars=cluster_doc_content_chars,
    )
    return f"""task_id: {_cluster_job_task_id(job)}

New Doc:
{_doc_block(item, max_content=new_doc_content_chars)}

Candidate Existing Clusters:
{candidate_clusters}
"""


def _cluster_batch_prompt(
    jobs: list[dict[str, Any]],
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    *,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> str:
    task_blocks = "\n\n==========\n\n".join(
        _cluster_job_block(
            job,
            by_id,
            cluster_by_id,
            cluster_edges,
            new_doc_content_chars=new_doc_content_chars,
            cluster_doc_limit=cluster_doc_limit,
            cluster_doc_content_chars=cluster_doc_content_chars,
        )
        for job in jobs
    )
    return f"""You judge whether each New Doc belongs to each candidate existing cluster as the same concrete event.

Rules:
- Same broad topic is not enough.
- Same company/product family is not enough.
- same_event=true requires the New Doc and the candidate cluster to share a concrete event subject: release, incident, lawsuit, acquisition, funding, paper id, repo/project, named product version, or another named event.
- If the candidate cluster itself looks too broad, reject unless the New Doc matches the same concrete event represented by confirmed edges.
- Be conservative: uncertain means false.
- Treat every task independently.
- Return strict JSON only.

Tasks:
{task_blocks}

Output schema:
{{
  "tasks": [
    {{
      "task_id": "cluster_task_<new_item_id>",
      "new_doc_fingerprint": {{
        "subject": "...",
        "action": "...",
        "time": "...",
        "event_type": "product_launch|tool_release|model_update|industry_news|tutorial_case|technical_insight|opinion|resource_collection|other"
      }},
      "matches": [
        {{
          "candidate_cluster_id": "exp_...",
          "same_event": true,
          "confidence": "high|medium|low",
          "relationship": "same_event|direct_commentary|follow_up_update|same_topic_only|unrelated",
          "shared_entity": "specific shared entity if true, else empty string",
          "rationale": "short reason"
        }}
      ]
    }}
  ]
}}
"""


def _build_doc_cluster_jobs(
    items: list[Item],
    clusters: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
    *,
    cosine_min: float,
    top_k: int,
) -> list[dict[str, Any]]:
    by_id = {x.id: x for x in items}
    cluster_edges = _cluster_edges_by_members(clusters, accepted_edges)
    cluster_vecs: dict[str, np.ndarray] = {}
    cluster_members = _cluster_member_ids(clusters)
    for cluster in clusters:
        members = [by_id[x] for x in cluster_members.get(cluster["cluster_id"], []) if x in by_id]
        vec = _centroid(members)
        if vec is not None:
            cluster_vecs[cluster["cluster_id"]] = vec

    jobs: list[dict[str, Any]] = []
    for item in items:
        item_vec = np.asarray(item.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(item_vec))
        if norm <= 1e-12:
            continue
        item_vec = item_vec / norm
        candidates = []
        for cluster in clusters:
            cluster_id = cluster["cluster_id"]
            members = set(cluster_members.get(cluster_id, []))
            if item.id in members:
                continue
            cvec = cluster_vecs.get(cluster_id)
            if cvec is None:
                continue
            score = float(item_vec @ cvec)
            if score < cosine_min:
                continue
            candidates.append({
                "candidate_cluster_id": cluster_id,
                "cosine": score,
                "recall_reason": "embedding_doc_cluster",
            })
        candidates.sort(key=lambda x: x["cosine"], reverse=True)
        candidates = candidates[:max(0, int(top_k))]
        if not candidates:
            continue
        jobs.append({
            "item_id": item.id,
            "candidate_count": len(candidates),
            "max_cosine": round(candidates[0]["cosine"], 4),
            "min_cosine": round(candidates[-1]["cosine"], 4),
            "candidates": [
                {
                    **c,
                    "cosine": round(c["cosine"], 6),
                }
                for c in candidates
            ],
        })
    return jobs


def _result_from_cluster_task(
    job: dict[str, Any],
    task: dict[str, Any],
    *,
    elapsed_sec: float,
) -> dict[str, Any]:
    matches = []
    valid_candidates = {c["candidate_cluster_id"] for c in job["candidates"]}
    for raw_match in task.get("matches", []):
        if not isinstance(raw_match, dict):
            continue
        match = _clean_cluster_match(raw_match)
        if not match:
            continue
        if match["candidate_cluster_id"] not in valid_candidates:
            continue
        matches.append(match)
    fingerprint = task.get("new_doc_fingerprint")
    return {
        "item_id": job["item_id"],
        "task_id": _cluster_job_task_id(job),
        "candidate_count": job["candidate_count"],
        "ok": True,
        "elapsed_sec": elapsed_sec,
        "fingerprint": fingerprint if isinstance(fingerprint, dict) else {},
        "matches": matches,
    }


def _judge_cluster_batch(
    batch: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    cluster_by_id: dict[str, dict[str, Any]],
    cluster_edges: dict[str, list[dict[str, Any]]],
    api_key: str,
    api_base: str | None,
    model: str,
    timeout: int,
    llm_max_tokens: int,
    new_doc_content_chars: int,
    cluster_doc_limit: int,
    cluster_doc_content_chars: int,
) -> list[dict[str, Any]]:
    started = time.time()
    prompt = _cluster_batch_prompt(
        batch,
        by_id,
        cluster_by_id,
        cluster_edges,
        new_doc_content_chars=new_doc_content_chars,
        cluster_doc_limit=cluster_doc_limit,
        cluster_doc_content_chars=cluster_doc_content_chars,
    )
    try:
        raw = _call_llm_chat(
            api_key=api_key,
            api_base=api_base,
            model=model,
            system_prompt=prompt,
            user_content="Return the JSON now.",
            max_tokens=llm_max_tokens,
            timeout=timeout,
        )
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        return [_failed_result(job, str(e), elapsed_sec=elapsed, batch_size=len(batch)) for job in batch]
    parsed = _parse_batch_json(raw)
    elapsed = round(time.time() - started, 2)
    if parsed is None:
        preview = (raw or "")[:400]
        return [
            _failed_result(job, "cluster_batch_parse_fail", elapsed_sec=elapsed,
                           raw_preview=preview, batch_size=len(batch))
            for job in batch
        ]

    tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in parsed:
        task_id = str(task.get("task_id") or "").strip()
        if task_id:
            tasks_by_id[task_id] = task

    results: list[dict[str, Any]] = []
    for job in batch:
        task = tasks_by_id.get(_cluster_job_task_id(job))
        if task is None:
            results.append(_failed_result(
                job, "missing_cluster_task_result", elapsed_sec=elapsed,
                batch_size=len(batch),
            ))
            continue
        results.append(_result_from_cluster_task(job, task, elapsed_sec=elapsed))
    return results


def _write_report(
    out_dir: Path,
    *,
    items: list[Item],
    jobs: list[dict[str, Any]],
    exact_edges: list[dict[str, Any]],
    judge_results: list[dict[str, Any]],
    doc_cluster_jobs: list[dict[str, Any]] | None,
    doc_cluster_results: list[dict[str, Any]] | None,
    display_results: list[dict[str, Any]] | None,
    clusters: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
    reasons: Counter,
    args: argparse.Namespace,
) -> None:
    by_id = {x.id: x for x in items}
    cluster_edges = _cluster_edges_by_members(clusters, accepted_edges)
    ok = sum(1 for r in judge_results if r.get("ok"))
    visible = [c for c in clusters if c["visible"]]
    candidate_hist = Counter(j["candidate_count"] for j in jobs)
    result_reason = Counter()
    for r in judge_results:
        if not r.get("ok"):
            result_reason[f"fail:{r.get('error')}"] += 1
            continue
        confirmed = 0
        for m in r.get("matches", []):
            if m.get("same_event") is True and m.get("confidence") in ("high", "medium"):
                confirmed += 1
        result_reason["has_confirmed_match" if confirmed else "no_confirmed_match"] += 1
    doc_cluster_results = doc_cluster_results or []
    doc_cluster_jobs = doc_cluster_jobs or []
    display_results = display_results or []
    doc_cluster_ok = sum(1 for r in doc_cluster_results if r.get("ok"))
    display_ok = sum(1 for r in display_results if r.get("ok"))

    lines = [
        "# Confirmed Edge Pipeline 聚合审阅报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 已有 embedding 的 items：{len(items)}",
        f"- cosine_min：{args.cosine_min}",
        f"- top_k：{args.top_k}",
        f"- dry_run：{args.dry_run}",
        f"- LLM 并发 workers：{args.workers}",
        f"- 每次 LLM request 最多 task 数：{args.jobs_per_request}",
        f"- 单次 prompt 字符预算：{args.max_prompt_chars}",
        f"- 单次 prompt token 预算约：{args.max_prompt_tokens}",
        f"- New Doc 原文截断：{args.new_doc_content_chars} chars",
        f"- Candidate Doc 原文截断：{args.candidate_doc_content_chars} chars",
        f"- 本轮 LLM judge jobs：{len(judge_results)} / 候选 jobs：{len(jobs)}",
        f"- 本轮 doc-cluster judge jobs：{len(doc_cluster_results)} / 候选 jobs：{len(doc_cluster_jobs)}",
        f"- 本轮簇级展示判断：{len(display_results)}",
        "",
        "## 总览",
        "",
        f"- exact identity 边：{len(exact_edges)}",
        f"- 候选 judge jobs：{len(jobs)}",
        f"- judge 成功：{ok}",
        f"- judge 失败：{len(judge_results) - ok}",
        f"- doc-cluster judge 成功：{doc_cluster_ok}",
        f"- doc-cluster judge 失败：{len(doc_cluster_results) - doc_cluster_ok}",
        f"- 簇级展示判断成功：{display_ok}",
        f"- 确认合并边：{len(accepted_edges)}",
        f"- confirmed clusters：{len(clusters)}",
        f"- 可展示 confirmed clusters：{len(visible)}",
        "",
        "## 候选数量分布",
        "",
    ]
    for k in sorted(candidate_hist):
        lines.append(f"- 每个 task {k} 个候选：{candidate_hist[k]}")
    lines += ["", "## Reducer 原因分布", ""]
    for k, v in reasons.most_common():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Judge 结果分布", ""]
    for k, v in result_reason.most_common():
        lines.append(f"- {k}: {v}")
    lines += ["", "## 可展示聚合簇详情", ""]
    for c in visible:
        member_items = [by_id[m["id"]] for m in c["members"] if m["id"] in by_id]
        edges = cluster_edges.get(c["cluster_id"], [])
        entities = _cluster_shared_entities(edges)
        title = c.get("db_title") or _cluster_review_title(member_items, entities)
        keywords = _cluster_review_keywords(member_items, entities)
        judgment = c.get("display_judgment") or {}
        lines += [
            f"### {c['cluster_id']}｜{title}",
            "",
            f"- 成员数：{c['member_count']}",
            f"- 确认来源数：{c['confirmed_source_count']}",
            f"- 平台：{', '.join(c['platforms'])}",
            f"- 时间范围：{c['first_doc_at']} → {c['last_doc_at']}",
            f"- 展示判断：{judgment.get('event_type', 'unknown')} / {judgment.get('confidence', 'unknown')}",
            f"- 聚合关键词：{', '.join(keywords) if keywords else '(无)'}",
            "",
            "#### 中文事件摘要",
            "",
            c.get("db_summary", "").replace("【精华速览】", "").strip() or "(无)",
            "",
            "#### 确认边",
            "",
        ]
        for edge in edges[:20]:
            src = edge.get("decision_source")
            a = edge.get("item_id")
            b = edge.get("candidate_item_id")
            entity = edge.get("shared_entity") or edge.get("source_identity") or ""
            confidence = edge.get("confidence") or ""
            rationale = edge.get("rationale") or ""
            lines.append(f"- `{a}` ↔ `{b}`｜{src}｜{confidence}｜共享实体：{entity or '(无)'}")
            if rationale:
                lines.append(f"  - 理由：{_clip_inline(rationale, 260)}")
        lines += [
            "",
            "#### 成员明细",
            "",
        ]
        for idx, m in enumerate(c["members"], 1):
            item = by_id.get(m["id"])
            title_line = (m["title"] or m["id"]).replace("\n", " ")
            lines.append(f"**{idx}. [{m['platform']}/{m['source']}] {title_line}**")
            lines.append("")
            lines.append(f"- item_id：`{m['id']}`")
            if m.get("author"):
                lines.append(f"- 作者：{m['author']}")
            if m.get("url"):
                lines.append(f"- URL：{m['url']}")
            if item is not None:
                item_keywords = _split_keywords(item.ai_keywords)
                if item_keywords:
                    lines.append(f"- 关键词：{', '.join(item_keywords[:12])}")
                if item.ai_summary:
                    lines.append(f"- AI 摘要：{_clip_inline(item.ai_summary, 420)}")
                key_points = _parse_key_points(item.ai_key_points, max_lines=4)
                if key_points:
                    lines.append("- 要点：")
                    for kp in key_points.splitlines()[:6]:
                        lines.append(f"  {kp}")
                if item.content:
                    lines.append(f"- 正文摘录：{_clip_inline(item.content, 700)}")
            lines.append("")
        lines += [
            "#### 审阅提示",
            "",
            "- 需要人工确认：这些成员是否描述同一个具体事件，而不是同主题/同产品线/横向比较。",
            "- 如果发现过合，优先看 `accepted_edges.jsonl` 中对应边的 `shared_entity` 与 `rationale`。",
            "",
        ]
    lines += ["", "## 内部/不可展示聚合簇", ""]
    invisible = [c for c in clusters if not c["visible"]]
    if not invisible:
        lines.append("(无)")
    for c in invisible:
        member_items = [by_id[m["id"]] for m in c["members"] if m["id"] in by_id]
        title = c.get("db_title") or _cluster_review_title(member_items, [])
        warnings = c.get("display_warnings") or []
        lines += [
            f"### {c['cluster_id']}｜{title}",
            "",
            f"- 成员数：{c['member_count']}",
            f"- 确认来源数：{c['confirmed_source_count']}（簇级展示判断未通过）",
            f"- 平台：{', '.join(c['platforms'])}",
            f"- 不展示原因：{'; '.join(warnings) if warnings else '(无)'}",
            "",
        ]
        for m in c["members"][:8]:
            title_line = (m["title"] or m["id"]).replace("\n", " ")
            lines.append(f"- [{m['platform']}/{m['source']}] {title_line} (`{m['id']}`)")
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _clip_inline(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "..."


def _split_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    parts = re.split(r"[,，、;；|｜\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def _cluster_review_keywords(items: list[Item], entities: list[str], limit: int = 18) -> list[str]:
    counter: Counter = Counter()
    for ent in entities:
        if ent:
            counter[ent] += 10
    for item in items:
        for kw in _split_keywords(item.ai_keywords):
            counter[kw] += 2
        for text in (item.title, item.ai_summary):
            for token in re.findall(r"[A-Za-z][A-Za-z0-9._/-]{2,}", text or ""):
                if token.lower() in {"the", "and", "for", "with", "that", "this"}:
                    continue
                counter[token] += 1
    return [k for k, _ in counter.most_common(limit)]


def _cluster_review_title(items: list[Item], entities: list[str]) -> str:
    if entities:
        return " / ".join(entities[:4])
    if not items:
        return "未命名聚合簇"
    keywords = _cluster_review_keywords(items, [], limit=6)
    if keywords:
        return " / ".join(keywords[:4])
    for item in items:
        if item.title:
            return _clip_inline(item.title, 80)
    return items[0].id


def _cluster_edges_by_members(
    clusters: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in clusters:
        members = {m["id"] for m in c.get("members", [])}
        edges = []
        for edge in accepted_edges:
            if edge.get("item_id") in members and edge.get("candidate_item_id") in members:
                edges.append(edge)
        out[c["cluster_id"]] = edges
    return out


def _cluster_shared_entities(edges: list[dict[str, Any]], limit: int = 8) -> list[str]:
    counter: Counter = Counter()
    for edge in edges:
        for key in ("shared_entity", "source_identity"):
            val = str(edge.get(key) or "").strip()
            if not val:
                continue
            # Exact URL identities are useful for debugging but terrible as a
            # human-facing title. Keep repo/arxiv-style compact identities.
            if val.startswith("http") and len(val) > 80:
                continue
            counter[val] += 1
    return [k for k, _ in counter.most_common(limit)]


def _cluster_display_prompt(
    cluster: dict[str, Any],
    by_id: dict[str, Item],
    edges: list[dict[str, Any]],
    *,
    cluster_summary_docs: int,
    cluster_summary_content_chars: int,
) -> str:
    members = [by_id[m["id"]] for m in cluster.get("members", []) if m["id"] in by_id]
    entities = _cluster_shared_entities(edges)
    docs = "\n\n---DOC---\n\n".join(
        _doc_block(item, max_content=cluster_summary_content_chars)
        for item in members[: max(1, int(cluster_summary_docs))]
    )
    edge_lines = []
    for edge in edges[:25]:
        entity = edge.get("shared_entity") or edge.get("source_identity") or ""
        rationale = edge.get("rationale") or edge.get("decision_source") or ""
        edge_lines.append(
            f"- source={edge.get('decision_source')} item={edge.get('item_id')} "
            f"candidate_item={edge.get('candidate_item_id')} "
            f"candidate_cluster={edge.get('candidate_cluster_id') or ''} "
            f"entity={entity} rationale={rationale}"
        )
    return f"""你是事件聚合产品的最终展示审稿人。请判断这个 confirmed cluster 是否应该展示给用户，并生成中文标题/摘要。

展示规则：
- 只展示具体事件：产品/模型发布、公司动作、事故、诉讼、收购融资、论文/项目发布、明确版本更新、政策/行业新闻等。
- 不展示：教程系列、常青技术文章、同一作者历史文章合集、资源收藏、泛话题讨论、观点/吐槽、只是同产品线但非同一事件。
- 如果簇内有明显混入或主题过宽，is_display_event=false。
- 不要因为成员数或来源数不足而拒绝；只看是否是一个具体、准确的事件簇。
- 宁可不展示，也不要展示不准的聚合。
- 输出必须是严格 JSON。标题、摘要、key_points 用中文。

Cluster:
cluster_id: {cluster['cluster_id']}
member_count: {cluster['member_count']}
platforms: {', '.join(cluster.get('platforms') or [])}
time_range: {cluster.get('first_doc_at')} -> {cluster.get('last_doc_at')}
shared_entities: {', '.join(entities) if entities else '(none)'}

Confirmed edges:
{chr(10).join(edge_lines) if edge_lines else '(none)'}

Member docs:
{docs}

Output schema:
{{
  "is_display_event": true,
  "confidence": "high|medium|low",
  "event_type": "product_launch|tool_release|model_update|company_news|lawsuit|incident|research|project_release|tutorial_series|opinion|resource_collection|same_topic_only|other",
  "title": "中文标题，尽量包含核心实体和动作",
  "summary": "中文 2-4 句，说明事件本身、为什么聚合到一起、仍需注意的边界",
  "key_points": [
    {{"title": "核心事实", "points": ["...", "..."]}},
    {{"title": "聚合依据", "points": ["...", "..."]}}
  ],
  "warnings": ["如果拒绝展示或有混入风险，写具体原因；否则为空数组"]
}}
"""


def _normalize_display_judgment(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {
            "is_display_event": False,
            "confidence": "low",
            "title": "",
            "summary": "",
            "key_points": [],
            "warnings": ["cluster_display_parse_failed"],
        }
    confidence = str(raw.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    visible_raw = raw.get("is_display_event")
    if isinstance(visible_raw, str):
        is_display = visible_raw.strip().lower() in ("true", "yes", "1")
    else:
        is_display = bool(visible_raw)
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    kps_raw = raw.get("key_points")
    key_points: list[Any] = []
    if isinstance(kps_raw, list):
        for item in kps_raw:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    key_points.append(text)
            elif isinstance(item, dict):
                group_title = str(item.get("title") or "").strip()
                points = []
                raw_points = item.get("points")
                if isinstance(raw_points, list):
                    for p in raw_points:
                        text = str(p).strip() if p is not None else ""
                        if text:
                            points.append(text)
                if group_title or points:
                    key_points.append({"title": group_title, "points": points})
    warnings_raw = raw.get("warnings")
    warnings: list[str] = []
    if isinstance(warnings_raw, list):
        for w in warnings_raw:
            text = str(w).strip() if w is not None else ""
            if text:
                warnings.append(text)
    if is_display and confidence not in ("high", "medium"):
        is_display = False
        warnings.append("display_confidence_low")
    if is_display and (not title or not summary):
        is_display = False
        warnings.append("display_missing_title_or_summary")
    return {
        "is_display_event": is_display,
        "confidence": confidence,
        "event_type": str(raw.get("event_type") or "other").strip(),
        "title": title,
        "summary": summary,
        "key_points": key_points,
        "warnings": warnings,
    }


def _summarize_one_cluster(
    cluster: dict[str, Any],
    *,
    by_id: dict[str, Item],
    edges: list[dict[str, Any]],
    api_key: str,
    api_base: str | None,
    model: str,
    timeout: int,
    llm_max_tokens: int,
    cluster_summary_docs: int,
    cluster_summary_content_chars: int,
) -> dict[str, Any]:
    started = time.time()
    prompt = _cluster_display_prompt(
        cluster,
        by_id,
        edges,
        cluster_summary_docs=cluster_summary_docs,
        cluster_summary_content_chars=cluster_summary_content_chars,
    )
    try:
        raw = _call_llm_chat(
            api_key=api_key,
            api_base=api_base,
            model=model,
            system_prompt=prompt,
            user_content="返回 JSON。",
            max_tokens=llm_max_tokens,
            timeout=timeout,
        )
    except Exception as e:
        return {
            "cluster_id": cluster["cluster_id"],
            "ok": False,
            "elapsed_sec": round(time.time() - started, 2),
            "error": str(e),
            "judgment": _normalize_display_judgment(None),
        }
    judgment = _normalize_display_judgment(_parse_json_object(raw))
    return {
        "cluster_id": cluster["cluster_id"],
        "ok": bool(judgment["title"] or judgment["warnings"]),
        "elapsed_sec": round(time.time() - started, 2),
        "judgment": judgment,
        "raw_preview": (raw or "")[:500],
    }


def _run_cluster_display_judges(
    clusters: list[dict[str, Any]],
    *,
    by_id: dict[str, Item],
    accepted_edges: list[dict[str, Any]],
    api_key: str,
    api_base: str | None,
    model: str,
    workers: int,
    timeout: int,
    llm_max_tokens: int,
    cluster_summary_docs: int,
    cluster_summary_content_chars: int,
) -> list[dict[str, Any]]:
    if not clusters:
        return []
    cluster_edges = _cluster_edges_by_members(clusters, accepted_edges)
    results: list[dict[str, Any]] = []
    print(f"[display] judging {len(clusters)} clusters with workers={workers}", flush=True)
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [
            pool.submit(
                _summarize_one_cluster,
                cluster,
                by_id=by_id,
                edges=cluster_edges.get(cluster["cluster_id"], []),
                api_key=api_key,
                api_base=api_base,
                model=model,
                timeout=timeout,
                llm_max_tokens=llm_max_tokens,
                cluster_summary_docs=cluster_summary_docs,
                cluster_summary_content_chars=cluster_summary_content_chars,
            )
            for cluster in clusters
        ]
        for idx, fut in enumerate(cf.as_completed(futures), 1):
            result = fut.result()
            results.append(result)
            if idx % 5 == 0 or idx == len(futures):
                visible = sum(1 for r in results if r.get("judgment", {}).get("is_display_event"))
                print(f"[display] clusters {idx}/{len(futures)} done visible={visible}", flush=True)
    results.sort(key=lambda r: r["cluster_id"])
    judgments = {r["cluster_id"]: r for r in results}
    for cluster in clusters:
        result = judgments.get(cluster["cluster_id"])
        judgment = (result or {}).get("judgment") or _normalize_display_judgment(None)
        cluster["display_judgment"] = judgment
        cluster["visible"] = bool(judgment.get("is_display_event"))
        if judgment.get("title"):
            cluster["db_title"] = judgment["title"]
        if judgment.get("summary"):
            cluster["db_summary"] = "【精华速览】\n" + judgment["summary"]
        cluster["db_key_points"] = judgment.get("key_points") or []
        cluster["display_warnings"] = judgment.get("warnings") or []
    return results


def _cluster_representative_blob(items: list[Item]) -> bytes | None:
    vecs = [np.asarray(item.embedding, dtype=np.float32) for item in items if item.embedding is not None]
    if not vecs:
        return None
    matrix = np.stack(vecs).astype(np.float32)
    mean = matrix.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm > 1e-12:
        mean = mean / norm
    return vu.pack_blob(mean.astype(np.float32))


def _db_summary_for_cluster(
    cluster: dict[str, Any],
    members: list[Item],
    edges: list[dict[str, Any]],
    entities: list[str],
) -> tuple[str, str]:
    if cluster.get("db_summary"):
        return (
            str(cluster["db_summary"]),
            json.dumps(cluster.get("db_key_points") or [], ensure_ascii=False),
        )
    title = _cluster_review_title(members, entities)
    entity_text = "、".join(entities[:4]) if entities else title
    sample_titles = [m.title for m in members if m.title][:5]
    summary = (
        "【精华速览】\n"
        f"这是 confirmed-edge 实验聚合出的本地事件簇，核心实体/主题为 {entity_text}。"
        f"共有 {cluster['member_count']} 条成员，LLM 或精确身份规则确认它们存在同一事件关系。"
        "请重点检查下方来源是否真的是同一具体事件，而不是同主题或同产品线泛化。"
    )
    points = [
        {
            "title": "确认依据",
            "points": [
                _clip_inline(
                    f"{edge.get('shared_entity') or edge.get('source_identity') or '未命名实体'}："
                    f"{edge.get('rationale') or edge.get('decision_source') or 'confirmed edge'}",
                    180,
                )
                for edge in edges[:5]
            ] or ["由 confirmed-edge reducer 形成，缺少可读确认边说明。"],
        },
        {
            "title": "成员标题",
            "points": [_clip_inline(t, 160) for t in sample_titles] or ["无标题成员。"],
        },
    ]
    return summary, json.dumps(points, ensure_ascii=False)


def _write_clusters_to_db(
    out_dir: Path,
    *,
    items: list[Item],
    clusters: list[dict[str, Any]],
    accepted_edges: list[dict[str, Any]],
    backup: bool,
) -> None:
    db_path = BASE / "data" / "feed.db"
    if backup:
        backup_dir = BASE / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"feed-before-confirmed-edge-{stamp}.db"
        shutil.copy2(db_path, backup_path)
    else:
        backup_path = None

    by_id = {x.id: x for x in items}
    cluster_edges = _cluster_edges_by_members(clusters, accepted_edges)
    now = datetime.now().isoformat(timespec="seconds")
    written: list[dict[str, Any]] = []

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("DELETE FROM cluster_status")
            conn.execute("DELETE FROM cluster_items")
            try:
                conn.execute("DELETE FROM cluster_judge_log")
            except sqlite3.OperationalError:
                pass
            conn.execute("DELETE FROM clusters")
            conn.execute("UPDATE items SET cluster_id = NULL, cluster_locked = 0")

            for cluster in clusters:
                if not cluster.get("visible"):
                    continue
                members = [by_id[m["id"]] for m in cluster.get("members", []) if m["id"] in by_id]
                if len(members) < 2:
                    continue
                members.sort(key=lambda x: (x.published_at or x.fetched_at, x.id))
                edges = cluster_edges.get(cluster["cluster_id"], [])
                entities = _cluster_shared_entities(edges)
                title = cluster.get("db_title") or _cluster_review_title(members, entities)
                summary, key_points = _db_summary_for_cluster(cluster, members, edges, entities)
                platforms_json = json.dumps(sorted({m.platform for m in members if m.platform}), ensure_ascii=False)
                first_doc_at = members[0].published_at or members[0].fetched_at or now
                last_doc_at = members[-1].published_at or members[-1].fetched_at or first_doc_at
                source_count = len({m.source_identity for m in members if m.source_identity})
                rep_blob = _cluster_representative_blob(members)
                cur = conn.execute(
                    """INSERT INTO clusters
                         (ai_title, ai_summary, ai_key_points, live_version,
                          doc_count, platforms_json, first_doc_at, last_doc_at,
                          last_updated_at, is_visible_in_feed, prompt_version,
                          representative_vector, unique_source_count,
                          last_summary_warnings_json)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        title,
                        summary,
                        key_points,
                        len(members),
                        platforms_json,
                        first_doc_at,
                        last_doc_at,
                        now,
                        "experimental_confirmed_edge_v1",
                        rep_blob,
                        source_count,
                        json.dumps(cluster.get("display_warnings") or [], ensure_ascii=False),
                    ),
                )
                db_cluster_id = int(cur.lastrowid)
                for idx, member in enumerate(members):
                    conn.execute(
                        """INSERT INTO cluster_items
                             (cluster_id, item_id, rank_in_cluster,
                              is_primary_source, source_identity, join_decision_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            db_cluster_id,
                            member.id,
                            idx,
                            1 if idx == 0 else 0,
                            member.source_identity,
                            cluster["cluster_id"],
                        ),
                    )
                    conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (db_cluster_id, member.id))
                written.append({
                    "experiment_cluster_id": cluster["cluster_id"],
                    "db_cluster_id": db_cluster_id,
                    "member_count": len(members),
                    "unique_source_count": source_count,
                    "title": title,
                })
    finally:
        conn.close()

    summary_path = out_dir / "db_write_summary.json"
    summary_path.write_text(
        json.dumps({
            "db_path": str(db_path),
            "backup_path": str(backup_path) if backup_path else None,
            "written_clusters": len(written),
            "clusters": written,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[db] wrote {len(written)} clusters into {db_path}", flush=True)
    if backup_path:
        print(f"[db] backup={backup_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline confirmed-edge clustering experiment")
    parser.add_argument("--limit", type=int, default=0, help="Limit items with embeddings, 0 means all")
    parser.add_argument("--cosine-min", type=float, default=0.75)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--llm-max-tokens", type=int, default=8192,
                        help="Max output tokens for each batched LLM request")
    parser.add_argument("--jobs-per-request", type=int, default=6,
                        help="Pack multiple judge jobs into one LLM request")
    parser.add_argument("--cluster-jobs-per-request", type=int, default=3,
                        help="Pack multiple doc-cluster judge jobs into one LLM request")
    parser.add_argument("--max-prompt-tokens", type=int, default=100000,
                        help="Approximate soft prompt token budget for each batched LLM request")
    parser.add_argument("--max-prompt-chars", type=int, default=0,
                        help="Override prompt char budget; 0 means max_prompt_tokens * 4")
    parser.add_argument("--new-doc-content-chars", type=int, default=12000,
                        help="Raw content chars included for each new doc")
    parser.add_argument("--candidate-doc-content-chars", type=int, default=8000,
                        help="Raw content chars included for each candidate doc")
    parser.add_argument("--doc-cluster-cosine-min", type=float, default=0.0,
                        help="Doc-cluster cosine threshold; 0 means reuse --cosine-min")
    parser.add_argument("--doc-cluster-top-k", type=int, default=5)
    parser.add_argument("--cluster-doc-limit", type=int, default=4,
                        help="Representative docs included for each candidate cluster")
    parser.add_argument("--cluster-doc-content-chars", type=int, default=5000)
    parser.add_argument("--cluster-summary-docs", type=int, default=10,
                        help="Member docs included for final display/summary judge")
    parser.add_argument("--cluster-summary-content-chars", type=int, default=6000)
    parser.add_argument("--summary-workers", type=int, default=8)
    parser.add_argument("--max-llm-jobs", type=int, default=0, help="Cap LLM jobs for a smoke run")
    parser.add_argument("--max-doc-cluster-jobs", type=int, default=0,
                        help="Cap doc-cluster LLM jobs for a smoke run")
    parser.add_argument("--skip-doc-cluster", action="store_true")
    parser.add_argument("--skip-cluster-display", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Build candidates only, skip LLM")
    parser.add_argument("--write-db", action="store_true",
                        help="Write confirmed clusters into local feed.db for UI review")
    parser.add_argument("--no-db-backup", action="store_true",
                        help="Skip feed.db backup before --write-db")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    if not args.max_prompt_chars:
        args.max_prompt_chars = max(5000, int(args.max_prompt_tokens) * 4)
    if not args.doc_cluster_cosine_min:
        args.doc_cluster_cosine_min = args.cosine_min

    _load_dotenv(BASE / ".env")
    cfg = _load_config()
    ai = cfg.get("ai_summary", {})
    api_key = os.environ.get("MINIMAX_API_KEY") or ai.get("api_key") or ""
    api_base = ai.get("api_base") or "https://api.minimaxi.com/anthropic/v1"
    model = ai.get("model") or "MiniMax-M2.7"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else BASE / "logs" / "confirmed-edge-experiment" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    items = _load_items(limit=args.limit or None)
    if not items:
        raise SystemExit("no items with embedding found")
    by_id = {x.id: x for x in items}
    jobs, exact_edges = _build_jobs(items, cosine_min=args.cosine_min, top_k=args.top_k)
    if args.max_llm_jobs and args.max_llm_jobs > 0:
        jobs_for_judge = jobs[: args.max_llm_jobs]
    else:
        jobs_for_judge = jobs

    _jsonl_write(out_dir / "candidate_jobs.jsonl", jobs)
    _jsonl_write(out_dir / "exact_edges.jsonl", exact_edges)

    print(f"[setup] out_dir={out_dir}", flush=True)
    print(f"[setup] items={len(items)} candidate_jobs={len(jobs)} exact_edges={len(exact_edges)}", flush=True)
    print(f"[setup] judge_jobs={0 if args.dry_run else len(jobs_for_judge)} dry_run={args.dry_run}", flush=True)

    judge_results: list[dict[str, Any]] = []
    if not args.dry_run:
        if not api_key:
            raise SystemExit("missing MINIMAX_API_KEY")
        judge_results = _run_judges(
            jobs_for_judge,
            by_id=by_id,
            api_key=api_key,
            api_base=api_base,
            model=model,
            workers=args.workers,
            timeout=args.timeout,
            llm_max_tokens=args.llm_max_tokens,
            jobs_per_request=args.jobs_per_request,
            max_prompt_chars=args.max_prompt_chars,
            new_doc_content_chars=args.new_doc_content_chars,
            candidate_doc_content_chars=args.candidate_doc_content_chars,
        )
    _jsonl_write(out_dir / "judge_results.jsonl", judge_results)

    initial_clusters, initial_edges, initial_reasons = _confirmed_from_results(
        items, exact_edges, judge_results,
    )
    (out_dir / "initial_doc_doc_clusters.json").write_text(
        json.dumps(initial_clusters, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _jsonl_write(out_dir / "initial_accepted_edges.jsonl", initial_edges)
    print(
        f"[doc-doc] clusters={len(initial_clusters)} accepted_edges={len(initial_edges)} "
        f"reasons={dict(initial_reasons)}",
        flush=True,
    )

    doc_cluster_jobs: list[dict[str, Any]] = []
    doc_cluster_results: list[dict[str, Any]] = []
    if initial_clusters and not args.skip_doc_cluster:
        doc_cluster_jobs = _build_doc_cluster_jobs(
            items,
            initial_clusters,
            initial_edges,
            cosine_min=args.doc_cluster_cosine_min,
            top_k=args.doc_cluster_top_k,
        )
        if args.max_doc_cluster_jobs and args.max_doc_cluster_jobs > 0:
            doc_cluster_jobs_for_judge = doc_cluster_jobs[: args.max_doc_cluster_jobs]
        else:
            doc_cluster_jobs_for_judge = doc_cluster_jobs
        _jsonl_write(out_dir / "doc_cluster_candidate_jobs.jsonl", doc_cluster_jobs)
        print(
            f"[doc-cluster] candidate_jobs={len(doc_cluster_jobs)} "
            f"judge_jobs={0 if args.dry_run else len(doc_cluster_jobs_for_judge)}",
            flush=True,
        )
        if not args.dry_run and doc_cluster_jobs_for_judge:
            doc_cluster_results = _run_doc_cluster_judges(
                doc_cluster_jobs_for_judge,
                by_id=by_id,
                clusters=initial_clusters,
                accepted_edges=initial_edges,
                api_key=api_key,
                api_base=api_base,
                model=model,
                workers=args.workers,
                timeout=args.timeout,
                llm_max_tokens=args.llm_max_tokens,
                jobs_per_request=args.cluster_jobs_per_request,
                max_prompt_chars=args.max_prompt_chars,
                new_doc_content_chars=args.new_doc_content_chars,
                cluster_doc_limit=args.cluster_doc_limit,
                cluster_doc_content_chars=args.cluster_doc_content_chars,
            )
    _jsonl_write(out_dir / "doc_cluster_judge_results.jsonl", doc_cluster_results)

    clusters, accepted_edges, reasons = _confirmed_from_results(
        items,
        exact_edges,
        judge_results,
        doc_cluster_results=doc_cluster_results,
        doc_cluster_members=_cluster_member_ids(initial_clusters),
    )

    display_results: list[dict[str, Any]] = []
    if clusters and not args.dry_run and not args.skip_cluster_display:
        display_results = _run_cluster_display_judges(
            clusters,
            by_id=by_id,
            accepted_edges=accepted_edges,
            api_key=api_key,
            api_base=api_base,
            model=model,
            workers=args.summary_workers,
            timeout=args.timeout,
            llm_max_tokens=args.llm_max_tokens,
            cluster_summary_docs=args.cluster_summary_docs,
            cluster_summary_content_chars=args.cluster_summary_content_chars,
        )
    elif not args.skip_cluster_display:
        # In dry-run mode, keep the pre-display visibility marker for candidate review.
        for c in clusters:
            c["visible"] = True
    _jsonl_write(out_dir / "cluster_display_results.jsonl", display_results)

    (out_dir / "confirmed_clusters.json").write_text(
        json.dumps(clusters, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _jsonl_write(out_dir / "accepted_edges.jsonl", accepted_edges)
    _write_report(
        out_dir,
        items=items,
        jobs=jobs,
        exact_edges=exact_edges,
        judge_results=judge_results,
        doc_cluster_jobs=doc_cluster_jobs,
        doc_cluster_results=doc_cluster_results,
        display_results=display_results,
        clusters=clusters,
        accepted_edges=accepted_edges,
        reasons=reasons,
        args=args,
    )
    if args.write_db:
        _write_clusters_to_db(
            out_dir,
            items=items,
            clusters=clusters,
            accepted_edges=accepted_edges,
            backup=not args.no_db_backup,
        )
    print(f"[done] report={out_dir / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
