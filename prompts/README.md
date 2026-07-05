# AI Prompt 配置中心

本目录存放 info2action 系统中所有 AI 调用的 prompt 模板（**当前 17 个 .md 文件**，含 v15.1 新增 `10_cluster_top10_judge.md` + 标 DEPRECATED 的 `09_cluster_merge_decision.md`）。
修改后无需重启服务，下次调用自动生效。

> 调方案 / 看模块对应关系，先看 `docs/产品实现速查.md`。本文件只给文件清单和模板变量。

## 流程总览

```
数据抓取 (ops/fetch_all.sh) → 入库 (src/ingest.py)
    ↓
┌───────────────────────────────────────────────────────────────┐
│ AI 内容理解（两套路径并存，详见 产品实现速查.md §1 / §11 矛盾#1） │
│                                                                │
│  ① 统一路径（主链路 / ECS）                                    │
│      enrich_items.py → 03_enrich_item.md                       │
│      （一次性产出 摘要+要点+分类+评分+关键词）                  │
│                                                                │
│  ② 分散路径（仅 MBP cron，已 DEPRECATED）                      │
│      score_items.py    → 01_classify_and_score.md              │
│      backfill_categories.py → 01b_classify_backfill.md          │
│      generate_summaries.py → 02_summary_breakdown.md /          │
│                              02_summary_breakdown_asr.md (视频) │
└───────────────────────────────────────────────────────────────┘
    ↓
③ 兴趣匹配 (interest_engine.py)
   ├── 03_interest_matching.md   (item ↔ 兴趣方向相关性 0-10，>=5 才返回)
   ├── 03b_interest_keywords.md  (兴趣描述 → 5-10 检索关键词)
   └── 03c_interest_action.md    (基于最相关信息生成 1 条行动建议)
    ↓
④ 行动点生成 (generate_actions.py) → 04_action_analysis.md + directions.yaml
④ 行动点去重 (dedup_actions.py)    → 04b_action_dedup.md
    ↓
⑤ 每日简报 (generate_briefing.py) → 05_daily_briefing.md
    ↓
⑥ 关键词扩展 / 趋势过滤 (serve.py)
   ├── 06_keyword_extraction.md   (从标题摘要提取 2-3 个搜索关键词)
   └── 06b_trend_filtering.md     (过滤非 AI/科技趋势词)
    ↓
⑦ 事件聚类（v15.1 V2，clustering/pipeline.py + summary_writer.py）
   ├── 07_cluster_summary.md       (Stage 4 双段事件摘要)
   ├── 08_cluster_title.md         (合簇后标题重算)
   ├── 09_cluster_merge_decision.md ⚠️ DEPRECATED in v15.1
   │                              (V1 单对判定，仅 merge_detector 观察期还用)
   └── 10_cluster_top10_judge.md   (V2 Stage 2 一次性 top-K 判定)
```

## 模板变量

部分 prompt 支持运行时变量替换（`{variable}` 语法）：

| 文件 | 变量 | 说明 |
|------|------|------|
| `01_classify_and_score.md` | `{categories}`, `{feedback}` | 分类体系（来自 classification.json）+ 用户偏好信号 |
| `01b_classify_backfill.md` | `{categories}` | 分类体系 |
| `02_summary_breakdown.md` | `{category}` | 内容分类（products/ai_tools/models/tech/tutorials/industry/creator/investment） |
| `02_summary_breakdown_asr.md` | `{category}` | 同上，但视频 ASR 专用（增加 interview/podcast 类目） |
| `03_enrich_item.md` | `{categories}` | 分类体系（统一路径用） |
| `03_interest_matching.md` | `{keywords}` | 用户兴趣的关键词列表 |
| `03b_interest_keywords.md` | — | 无（输入是用户兴趣描述本身） |
| `03c_interest_action.md` | `{interest_name}`, `{keywords}` | 兴趣方向名称 + 关键词 |
| `04_action_analysis.md` | `{manifest_text}`, `{pulse_active_work}`, `{pulse_problems}`, `{pulse_learnings}`, `{pulse_content}`, `{user_guidance}`, `{directions_text}`, `{feedback_context}` | WORKSPACE-MANIFEST + WORKSPACE-PULSE + 用户偏好 + directions.yaml + 反馈信号 |
| `04b_action_dedup.md` | `{direction_label}`, `{direction_description}`, `{actions_in_direction}` | 方向内行动点去重 |
| `05_daily_briefing.md` | `{exclusions}` | 用户已配置的兴趣方向（避免重复，可为空） |
| `06_keyword_extraction.md` | `{topic_name}`, `{existing_keywords}` | 当前主题 + 已有关键词 |
| `06b_trend_filtering.md` | — | 无 |
| `07_cluster_summary.md` | — | 成员原文经 user message 传入（不再走 system prompt 占位符，V2.3 §13.2） |
| `08_cluster_title.md` | `{cluster_a_title}`, `{cluster_b_title}`, `{cluster_merged_docs}` | 合簇前两个 cluster 标题 + 合并后成员原文 |
| `09_cluster_merge_decision.md` | `{doc_a_content}`, `{doc_b_content}`, `{scenario}` | ⚠️ DEPRECATED；仅 `merge_detector.py` 仍调用 |
| `10_cluster_top10_judge.md` | `{new_doc}`, `{candidate_clusters}` | new doc 内容块 + top-K 候选 cluster 块 |
| `directions.yaml` | —（被 04 引用） | 行动方向框架（11 个方向 + 1 个 _uncategorized） |

## 加载方式

绝大多数 prompt 走 `prompt_loader.load_prompt(filename, **kwargs)`，自动找文件 + 替换 `{variable}`。
**唯一例外**：`04_action_analysis.md` 在 `src/generate_actions.py:40` 走 `os.path.join + open + str.replace` 直接读，新增变量需要同步改 `src/generate_actions.py:417` 附近的 replace 链。

## 编辑指南

- 直接编辑 `.md` 文件即可，不需要改代码（除非加新的 `{variable}`）
- `---` 分隔线之上为文件说明（不会被加载），之下为实际 prompt 内容
- 保持 `{variable}` 占位符不变，否则运行时替换会失败
- 删除某个 prompt 文件后，代码会回退到内置默认值
- 变更历史见 `CHANGELOG.md`
- 想知道某条 prompt 影响哪些产品效果、对应代码在哪、哪些参数硬编码，看 `docs/产品实现速查.md`
