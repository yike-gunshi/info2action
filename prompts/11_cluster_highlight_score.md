# Cluster Highlight Scoring Prompt (Calibration v1)

> 使用方：`scripts/highlight_score_calibration.py`
> 目标：离线验证 Cluster 精选评分体系，不直接决定线上精选展示。
> 重要边界：不要输出 0-100 分；只输出 1-5 档锚点评分和证据。
---
你正在为 info2action 的「精选 Tab」做离线评分校准。

info2action 面向 AI/科技从业者和内容创作者。系统会从 Twitter、RSS、Hacker News、Reddit、GitHub、B 站、公众号等来源抓取内容，并聚合成 cluster。精选 Tab 的目标不是只展示重大新闻，而是展示「有价值的内容」，过滤掉没有实质价值、纯营销、重复搬运、不可验证、明显过时或领域不匹配的内容。

当前输入是一个 cluster，不是单条 item。你需要判断这个 cluster 是否值得进入精选候选，并输出结构化 JSON。最终是否真正进入精选由后续人工校准和代码阈值决定。

## 硬门槛

如命中以下任一情况，`hard_gate` 输出 `reject`：

- 非 AI/科技/产品/创业/效率/内容创作相关领域。
- 纯营销、广告、带货、招商、优惠券、软文推广，且缺少实质信息。
- 重复搬运或同质转述，没有新的信息增量。
- 关键信息无法验证，来源不明，事实主张缺乏依据。
- 没有实质信息，只有标题党、情绪表达、空泛观点。
- 明显过时，且没有历史参考价值。

如果不确定但可能有价值，输出 `review`，不要直接 reject。

## 内容类型

`content_type` 只能从以下值选择：

- `dynamic_news`：动态新闻/发布事件。例：模型发布、产品更新、融资、政策、事故、行业变化。
- `product_tool`：产品工具/资源发现。例：新工具、开源项目、插件、资源合集。
- `tutorial_method`：教程方法/实践指南。例：操作流程、工程实践、方法论、可复用指南。
- `evaluation_report`：评测报告/数据分析。例：benchmark、横向对比、数据报告、实测结论。
- `opinion_case`：观点案例/经验复盘。例：有证据和场景的经验、案例、洞察。
- `general`：通用兜底。类型不明确但仍可评价。
- `reject`：拒绝类。

## 1-5 档评分锚点

只允许 1、2、3、4、5，不允许小数，不允许百分制。

### information_value 信息价值
- 1：几乎没有新信息或只是情绪/标题。
- 2：有少量信息，但大多常识化或可替代。
- 3：有明确事实或观点，对部分用户有参考价值。
- 4：信息明确且重要，能帮助用户快速理解变化或机会。
- 5：高价值信息，包含关键变化、影响、机会或风险，值得优先展示。

### usefulness 有用性
- 1：用户看完难以采取任何行动。
- 2：有启发但缺少可执行线索。
- 3：能帮助理解或做轻量判断。
- 4：包含可复用方法、选择建议、操作路径或决策依据。
- 5：高度可执行，能直接支持用户学习、判断、采用或规避。

### timeliness 时效性
- 1：明显过时且无历史价值。
- 2：不是最新内容，参考价值有限。
- 3：时间一般，但类型允许不新，例如教程或案例。
- 4：近期内容，对当前判断仍重要。
- 5：非常及时，正在发生或刚发布。

### authority_trust 权威可信
- 1：来源不明、事实无法验证。
- 2：可信度弱，只有单方说法或二手转述。
- 3：来源基本可信，但证据有限。
- 4：来源可信，有明确出处、作者、数据或实测。
- 5：官方/一手/高可信来源，证据充分，可验证性强。

### content_depth 内容深度
- 1：极浅，只是标题或一句话。
- 2：有展开但缺少细节。
- 3：有一定背景、要点或解释。
- 4：包含机制、上下文、对比、影响或细节。
- 5：结构完整，覆盖背景、事实、机制、影响和限制。

### domain_fit 领域匹配
- 1：基本不相关。
- 2：弱相关，放进精选会显得跑题。
- 3：相关但不核心。
- 4：与 AI/科技/产品/效率/创作用户高度相关。
- 5：高度命中核心用户的长期兴趣或当前工作流。

### cluster_incremental_value 新信息增量/去重价值
- 1：cluster 只是重复搬运，合并后没有额外价值。
- 2：多来源但内容高度重复。
- 3：有一定互补信息或来源确认。
- 4：多来源提供不同角度、证据、评论或实测。
- 5：聚合显著提升理解，单条 item 无法替代。

### marketing_noise 营销噪声
- 1：几乎无营销噪声。
- 2：轻微推广，但不影响信息判断。
- 3：推广和信息混杂，需要谨慎。
- 4：营销色彩强，价值需要打折。
- 5：主要是营销/广告/引流。

## 推荐桶

`bucket` 只能从以下值选择：

- `featured_candidate`：明显有价值，建议进入精选候选。
- `candidate`：有价值，但需要等待阈值和人工校准。
- `manual_review`：边界样本，需要人工复核。
- `reject`：不应进入精选。

## 输出要求

只输出严格 JSON，不要 markdown 代码块，不要解释。所有维度都必须给出 1-5 档分，并给出简短证据。不要输出任何 0-100 分。

{
  "hard_gate": "pass / reject / review",
  "hard_gate_reason": "如 reject 或 review，说明原因；pass 可为空",
  "content_type": "dynamic_news / product_tool / tutorial_method / evaluation_report / opinion_case / general / reject",
  "content_type_confidence": 0.0,
  "dimension_scores": {
    "information_value": 1,
    "usefulness": 1,
    "timeliness": 1,
    "authority_trust": 1,
    "content_depth": 1,
    "domain_fit": 1,
    "cluster_incremental_value": 1
  },
  "dimension_evidence": {
    "information_value": ["证据1"],
    "usefulness": ["证据1"],
    "timeliness": ["证据1"],
    "authority_trust": ["证据1"],
    "content_depth": ["证据1"],
    "domain_fit": ["证据1"],
    "cluster_incremental_value": ["证据1"]
  },
  "marketing_noise": 1,
  "bucket": "featured_candidate / candidate / manual_review / reject",
  "confidence": 0.0,
  "reason_codes": ["official_source", "multi_source_confirmation"],
  "factual_claims_or_data": false,
  "notes": "简短说明主要判断"
}
