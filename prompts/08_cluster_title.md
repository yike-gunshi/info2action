# Cluster Title Prompt (v15.0 事件聚合模块 · 仅标题生成场景)

> 使用方:`src/clustering/summary_writer.py` 当只需重算标题(不改摘要)时使用。
> 正常场景 07_cluster_summary.md 已返回 title,本文件用于合簇后**标题兼容命名**(§8.P0.2 答 C 锁死规则无视)。
> 输入变量:`{cluster_a_title}` / `{cluster_b_title}` / `{cluster_merged_docs}`

---

你正在为**合簇后的新标题**做选择。两个事件 A 和 B 已被 LLM 确认是同一事件,现在需要给合并后的 cluster 一个最优标题。

## 规则

- 如果 A 或 B 中有一个标题明显覆盖另一个(例如 B="OpenAI 发布 GPT-5" 已包含 A="GPT-5 传闻"),直接用覆盖面更广的那个
- 如果两者都不完整,基于下方**成员报道原文**生成一个新的综合标题
- 标题 ≤ 26 字(英文专有名词按实际长度计入),客观事实,不标题党;超长必须精简,长仓库名/长英文名只保留主名
- 中文,英文专有名词保留原文

## 输入

- Cluster A title: `{cluster_a_title}`
- Cluster B title: `{cluster_b_title}`
- 合并后成员原文摘要(按时间):

{cluster_merged_docs}

## 输出(严格 JSON)

```json
{
  "title": "最终标题",
  "rationale": "一句话说明为什么选这个(<30字)"
}
```

请严格按上述 JSON 输出,不要 markdown 代码块包裹。
