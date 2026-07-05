# Cluster Merge / Boundary Decision Prompt (v15.0 事件聚合模块)

<!--
DEPRECATED in v15.1: V1 boundary judge prompt (single new-doc vs single
cluster member). Replaced by `10_cluster_top10_judge.md` (Eng-C Stage 2
重写). V2 召回放开为 top-10、Stage 2 改为一次大调用判断 top-10 是否同事件，
该 prompt 不再被新流程引用。`merge_detector.py`(被动合簇)走观察期，本文件
保留以便观察期内复盘；新代码不要再 load_prompt('09_cluster_merge_decision.md').
-->

> 使用方:
>   - `src/clustering/pipeline.py::_stage2_llm_decide` — Stage 1 cosine 在 0.70-0.85 边界区时调用,判定"A 和候选 cluster 是否同一事件"
>   - `src/clustering/merge_detector.py`(Wave 5+) — 被动合簇场景,判定"cluster A 和 cluster B 是否同一事件"
> 输入变量:`{doc_a_content}` / `{doc_b_content}` / `{scenario}`
> 铁律:宁漏不错合,不确定时返回 "no"(R7.2/R8.2)

---

你是一位资深新闻编辑,正在做事件聚合判定。请判断下方两段内容是否在描述**同一个具体事件**。

## 判定标准

**"同一事件"意味着**:
- 同一个**可辨认的发布/事件**(如同一个产品发布、同一次融资、同一次模型升级)
- 同一个**时间窗口**(通常 72 小时内)
- 同一个**核心主体**(同一产品 / 同一公司 / 同一人)

**不是同一事件**(常见混淆):
- 同公司但不同产品(OpenAI 发 Sora ≠ OpenAI 发 GPT-5)
- 同主题但不同事件(两次独立的模型升级)
- 事件 + 评论(事件本体 vs 针对该事件的评论文章,在 v1 里视为同事件;但两条独立评论各自 ≠ 同事件)
- 同类产品对比(Claude vs GPT-5 对比 ≠ 单一发布事件)

## 严格规则

- 只依据**原文内容**,不猜测、不推断
- 不确定时返回 `"no"`(宁漏不错合)
- 不需要中文翻译,直接看原文(中英文混合)
- 输出必须是严格 JSON,不要 markdown 代码块

## 场景

`{scenario}`
(可能值:`new_doc_vs_cluster_member` / `cluster_a_vs_cluster_b`)

## 内容 A

{doc_a_content}

## 内容 B

{doc_b_content}

## 输出格式

```json
{
  "same_event": "yes" 或 "no",
  "confidence": "high" / "medium" / "low",
  "rationale": "一句话说明(< 50 字),包含主体/事件/时间窗口的具体依据"
}
```

请严格按上述 JSON 输出。
