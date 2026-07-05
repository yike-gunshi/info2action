"""人工验证 — 把指定 cluster 的全过程信息导出成 markdown。

包含 4 段对比信息（按 pipeline 顺序）：
  1. 簇元信息（id / dominant_category / event_summary / certainty / size）
  2. 簇成员列表 + 每条 doc 的 LLM 决策（kept / removed + reason）
  3. Stage A 喂给 BGE-M3 的完整 aikw 文本（每条 doc 一个块）
  4. Stage P 喂给 LLM 的完整 system + user prompt（实际调用的同款）
  5. LLM 输出原文（cluster_p_log raw_response）

用法：
  python scripts/inspect_cluster.py 102
  python scripts/inspect_cluster.py 102 --db /tmp/info2action-cluster-v2-eval/feed.db
  python scripts/inspect_cluster.py --all-clean       # 导出所有 clean 簇
  python scripts/inspect_cluster.py --top-n 10        # 导出最大 N 个簇

落档：默认 /tmp/info2action-cluster-v2-eval/inspections/cluster-{id}.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

DEFAULT_DB = Path("/tmp/info2action-cluster-v2-eval/feed.db")
INSPECT_DIR = (
    REPO_ROOT
    / "docs" / "讨论" / "clustering" / "实验" / "simplified"
    / "2026-04-29-stage-zp-acceptance" / "inspections"
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _build_aikw_text(item: dict) -> str:
    """复用 src/clustering/stage_a.py 的 _aikw_text 逻辑（这里独立实现避免 import 循环）。"""
    title = (item.get("title") or "").strip() if isinstance(item.get("title"), str) else ""
    ai_summary = (item.get("ai_summary") or "").strip() if isinstance(item.get("ai_summary"), str) else ""
    ai_keywords = (item.get("ai_keywords") or "").strip() if isinstance(item.get("ai_keywords"), str) else ""
    parts = []
    if title:
        parts.append(title)
    if ai_summary:
        parts.append(ai_summary)
    if ai_keywords:
        parts.append(f"关键词: {ai_keywords}")
    if not parts:
        return f"(empty item {item.get('id') or 'unknown'})"
    return "\n\n".join(parts)


def _read_event_def(category: str | None) -> str:
    """读 event_definitions/{category}.md 文件内容（独立 IO 避免 import）。"""
    if not category:
        return "（无对应 event_definition 文件 — 弱事件性分类，跳过 LLM）"
    cat = category.strip().lower()
    if cat == "tools":
        cat = "ai_tools"
    path = SRC_DIR / "clustering" / "event_definitions" / f"{cat}.md"
    if not path.exists():
        return f"（{cat}.md 不存在）"
    return path.read_text(encoding="utf-8")


def inspect_cluster(conn: sqlite3.Connection, cluster_id: int) -> str:
    """生成一个簇的 inspect markdown。返回 markdown 字符串。"""
    cluster = conn.execute(
        """SELECT id, dominant_category, event_summary, event_certainty,
                  member_count, stage_p_state, stage_p_run_at, stage_p_failed_reason,
                  created_at, last_member_added_at
           FROM clusters_v2 WHERE id = ?""",
        (cluster_id,),
    ).fetchone()
    if not cluster:
        return f"# Cluster #{cluster_id} 不存在\n"

    members = conn.execute(
        """SELECT i.id, i.platform, i.source, i.title, i.ai_summary, i.ai_keywords,
                  i.ai_category, i.fetched_at, i.canonical_url, i.url,
                  ci.added_at, ci.joined_cosine, ci.removed_at, ci.removed_reason
           FROM cluster_items_v2 ci
           JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id = ?
           ORDER BY ci.removed_at IS NULL DESC, ci.added_at""",
        (cluster_id,),
    ).fetchall()
    members_dicts = [dict(m) for m in members]

    p_log = conn.execute(
        """SELECT id, item_id, action, reason, llm_model, raw_response, created_at
           FROM cluster_p_log WHERE cluster_id = ? ORDER BY id""",
        (cluster_id,),
    ).fetchall()

    kept = [m for m in members_dicts if m["removed_at"] is None]
    removed = [m for m in members_dicts if m["removed_at"] is not None]

    out: list[str] = []
    out.append(f"# Cluster #{cluster['id']} — {cluster['dominant_category']} / {cluster['stage_p_state']}")
    out.append("")

    # ==== Section 1: 元信息 ====
    out.extend([
        "## 1. 簇元信息",
        "",
        f"- **cluster_id**: {cluster['id']}",
        f"- **dominant_category**: {cluster['dominant_category']}",
        f"- **stage_p_state**: {cluster['stage_p_state']}",
        f"- **event_certainty**: {cluster['event_certainty']}",
        f"- **member_count**（visible）: {cluster['member_count']}",
        f"- **总成员（含剔除）**: {len(members_dicts)}（保留 {len(kept)} / 剔除 {len(removed)}）",
        f"- **created_at**: {cluster['created_at']}",
        f"- **stage_p_run_at**: {cluster['stage_p_run_at']}",
        "",
        "**event_summary**:",
        "",
        f"> {cluster['event_summary'] or '(空)'}",
        "",
    ])
    if cluster["stage_p_failed_reason"]:
        out.extend([
            "**stage_p_failed_reason**:",
            "",
            f"> {cluster['stage_p_failed_reason']}",
            "",
        ])

    # ==== Section 2: 成员列表 + LLM 决策 ====
    out.extend([
        "## 2. 成员列表 + LLM 决策",
        "",
        f"### 保留 {len(kept)} 条",
        "",
    ])
    if kept:
        out.append("| seq | id | platform | title | fetched_at | cosine（加入时） |")
        out.append("|---|---|---|---|---|---|")
        for i, m in enumerate(kept, 1):
            title = (m["title"] or "").replace("\n", " ").replace("|", "\\|")[:80]
            cos = f"{m['joined_cosine']:.4f}" if m["joined_cosine"] is not None else "-"
            out.append(f"| {i} | `{m['id']}` | {m['platform']} | {title} | {m['fetched_at']} | {cos} |")
        out.append("")
    else:
        out.append("（无）")
        out.append("")

    out.extend([
        f"### 剔除 {len(removed)} 条（LLM 判定不属于本簇主事件）",
        "",
    ])
    if removed:
        out.append("| seq | id | platform | title | LLM 剔除理由 |")
        out.append("|---|---|---|---|---|")
        for i, m in enumerate(removed, 1):
            title = (m["title"] or "").replace("\n", " ").replace("|", "\\|")[:60]
            reason = (m["removed_reason"] or "").replace("\n", " ").replace("|", "\\|")
            out.append(f"| {i} | `{m['id']}` | {m['platform']} | {title} | {reason} |")
        out.append("")
    else:
        out.append("（无）")
        out.append("")

    # ==== Section 3: Stage A — BGE-M3 input（aikw 文本） ====
    out.extend([
        "## 3. Stage A 喂给 BGE-M3 的 aikw 文本",
        "",
        "> 这就是每条 doc 算 embedding 时的实际输入文本（拼接 = title + ai_summary + 关键词 双换行分隔）",
        "",
    ])
    for i, m in enumerate(members_dicts, 1):
        marker = "（剔除）" if m["removed_at"] else "（保留）"
        out.extend([
            f"### {i}. [id={m['id']}] {marker}",
            "",
            f"- **platform**: {m['platform']} / {m['source']}",
            f"- **fetched_at**: {m['fetched_at']}",
            f"- **canonical_url**: `{m['canonical_url'] or '(NULL)'}`",
            f"- **原始 url**: `{m['url'] or '(NULL)'}`",
            f"- **ai_category**: {m['ai_category'] or '(NULL)'}",
            "",
            "**aikw 文本（喂给 BGE-M3）**:",
            "",
            "```",
            _build_aikw_text(m),
            "```",
            "",
        ])

    # ==== Section 4: Stage P — LLM input（system + user） ====
    out.extend([
        "## 4. Stage P 喂给 LLM 的完整 prompt",
        "",
        "### 4.1 system prompt",
        "",
        "> 通用框架（角色/产品背景/判断标准/工作步骤）+ 按 dominant_category 注入的事件颗粒度片段",
        "",
        "**注入的事件颗粒度片段**（来自 `src/clustering/event_definitions/`）：",
        "",
        "```markdown",
        _read_event_def(cluster["dominant_category"]),
        "```",
        "",
        "### 4.2 user prompt（Stage P 输入）",
        "",
        "> 实际喂给 LLM 的 user content。每条 doc 一行：`[id=xxx] 标题 | 完整 ai_summary`",
        "",
        f"以下是 {len(members_dicts)} 个被聚类算法合并到同一簇的内容卡片：",
        "",
    ])
    for m in members_dicts:
        title = (m["title"] or "").strip().replace("\n", " ")
        summary = (m["ai_summary"] or "").strip().replace("\n", " ")
        out.append(f"- [id={m['id']}] {title} | {summary}")
    out.extend([
        "",
        "（输出 JSON schema 要求略，详见 prompt-snapshot.md §4.2）",
        "",
    ])

    # ==== Section 5: cluster_p_log 审计 ====
    out.extend([
        "## 5. cluster_p_log 审计原始数据",
        "",
        f"共 {len(p_log)} 条 log。",
        "",
    ])
    summary_log = [r for r in p_log if r["action"] == "summary"]
    failed_log = [r for r in p_log if r["action"] == "failed"]
    unsupported_log = [r for r in p_log if r["action"] == "unsupported"]

    if summary_log:
        out.extend(["### LLM summary 决策（含原始响应前 8K 字）", ""])
        for log_row in summary_log:
            try:
                summary_obj = json.loads(log_row["reason"]) if log_row["reason"] else {}
            except (json.JSONDecodeError, TypeError):
                summary_obj = {}
            out.extend([
                f"- **created_at**: {log_row['created_at']}",
                f"- **llm_model**: {log_row['llm_model']}",
                f"- **summary 字段**：",
                "",
                "  ```json",
                f"  {json.dumps(summary_obj, ensure_ascii=False, indent=2)}",
                "  ```",
                "",
            ])

    if failed_log:
        out.extend(["### LLM failed 记录", ""])
        for log_row in failed_log:
            out.extend([
                f"- **created_at**: {log_row['created_at']}",
                f"- **reason**: {log_row['reason']}",
                "",
            ])
            if log_row["raw_response"]:
                out.extend([
                    "  原始响应（前 1500 字）：",
                    "",
                    "  ```",
                    (log_row["raw_response"] or "")[:1500],
                    "  ```",
                    "",
                ])

    if unsupported_log:
        out.extend(["### unsupported（弱事件性分类，未调 LLM）", ""])
        for log_row in unsupported_log:
            out.extend([f"- {log_row['reason']}", ""])

    # 最后附 raw_response（首条 remove）
    raw_logs = [r for r in p_log if r["action"] == "remove" and r["raw_response"]]
    if raw_logs:
        out.extend([
            "### LLM 完整原始响应（首条 remove 处保存的全文）",
            "",
            "```",
            raw_logs[0]["raw_response"][:8000],
            "```",
            "",
        ])

    out.extend([
        "---",
        "",
        "_此文件由 `scripts/inspect_cluster.py` 生成，可重跑覆盖_",
    ])
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cluster_id", nargs="?", type=int, help="要 inspect 的 cluster_id")
    ap.add_argument("--all-clean", action="store_true", help="导出所有 stage_p_state='clean' 的簇")
    ap.add_argument("--top-n", type=int, help="导出最大 N 个簇（按 member_count）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out-dir", type=Path, default=INSPECT_DIR)
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB 不存在：{args.db}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    conn = _connect(args.db)

    target_ids: list[int]
    if args.cluster_id is not None:
        target_ids = [args.cluster_id]
    elif args.all_clean:
        rows = conn.execute(
            "SELECT id FROM clusters_v2 WHERE stage_p_state IN ('clean','failed') ORDER BY id"
        ).fetchall()
        target_ids = [r["id"] for r in rows]
    elif args.top_n:
        rows = conn.execute(
            """SELECT id FROM clusters_v2
               WHERE stage_p_state IN ('clean','failed')
               ORDER BY member_count DESC LIMIT ?""",
            (args.top_n,),
        ).fetchall()
        target_ids = [r["id"] for r in rows]
    else:
        raise SystemExit("请指定 cluster_id / --all-clean / --top-n")

    print(f"[inspect] 导出 {len(target_ids)} 个簇 → {args.out_dir}")
    for cid in target_ids:
        md = inspect_cluster(conn, cid)
        out_path = args.out_dir / f"cluster-{cid}.md"
        out_path.write_text(md, encoding="utf-8")
        print(f"  ✓ {out_path}")
    print(f"[inspect] 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
