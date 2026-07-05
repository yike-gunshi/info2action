# Cluster Top-10 Judge Prompt (v15.1 事件聚合 V2 Stage 2)

> 使用方：`src/clustering/pipeline.py::_judge_top_k`
> 输入变量：`{new_doc}` / `{candidate_clusters}`
> 替换 v15.0 的 `09_cluster_merge_decision.md`（单对单 boundary judge）
> 铁律：宁漏不错合；LLM 只输出 `new_doc_fingerprint + matches[]`，代码层做选择
---
# Event Cluster Candidate Judge

你正在为 info2action 的"最新事件"模块做事件归并判断。

info2action 是一个面向 AI/科技从业者和内容创作者的信息雷达。系统会从 Twitter、RSS、Hacker News、Reddit、GitHub、B 站、公众号等来源抓取信息，先对每条单 doc 做 AI 理解，生成摘要、要点、分类、内容类型和关键词。

当前步骤发生在"事件聚合"流程中：
1. 上一步已经用 embedding 做了粗召回，找到了与 New Doc 语义相近的候选 clusters。
2. embedding 只能说明"可能相似"，不能说明"同一事件"。
3. 你的任务是做精筛：判断 New Doc 是否应该加入某一个候选 cluster。

"最新事件"模块希望展示的是最近发生的、值得用户快速掌握的具体信息变化，例如：
- 新产品或官方功能发布
- 新工具、新插件、新框架、新开源项目发布
- 模型发布、benchmark、价格、能力变化
- 重要技术机制、架构方案或高价值教程案例
- 行业动态、融资、并购、政策、算力、芯片

但这不是硬过滤。你的核心任务不是分类，而是判断 New Doc 和候选 cluster 是否描述同一个具体事件。

重要原则：
- embedding 召回只代表"语义可能相似"，不能代表同一事件。
- 你必须按"同一个具体事件"判断，而不是按主题相似判断。
- 不确定时不要合并。
- 宁可漏合，不要错合。
- 单 doc 不能因为自身重要就变成可见事件；如果没有同事件候选，应创建内部 singleton。

## 什么是同一事件

同一事件通常同时满足：
1. 同一个核心主体：同一产品、公司、项目、模型、人物或组织。
2. 同一个核心动作：发布、更新、融资、关闭、事故、开源、研究发现、政策变化等。
3. 合理时间窗口：通常 72 小时内；如果超过，需要能从内容看出它仍是同一事件的持续报道。
4. 信息关系直接：评论、实测、教程、后续分析可以加入，但必须明确围绕同一个主事件。

**BF-0428-3 硬必要条件（任一缺失即判 same_event=false）：**

- New Doc 与候选 cluster 必须**共享至少一个具体可命名的实体**。可命名 = 在二者文本中都能写出同一个名称。允许的具体实体类型（任选其一即满足）：
  - 同一**产品/版本号**（HappyHorse 1.0 == HappyHorse 1.0；不同版本不算）
  - 同一**公司动作**（"阿里发布 HappyHorse" == "阿里发布 HappyHorse"；"阿里发布 X" ≠ "阿里发布 Y"）
  - 同一**事件主语**（"SpaceX Falcon Heavy 发射 Viasat-3 F3" ≠ "Emirates A380 Starlink Wi-Fi"，前者主语是 SpaceX 火箭发射任务，后者主语是航空公司机舱升级）
  - 同一**人物 + 行为**（马斯克在某诉讼中的具体动作）
  - 同一**项目/repo**（同一 GitHub URL 或同一 paper 标题）

- **仅以下情况一律判 same_event=false**（即便 cosine 召回近）：
  - 同行业 / 同主题（"都是 AI 产品发布" / "都是浏览器产品" / "都是 RSS feed 文章"）
  - 同公司不同产品（"阿里 HappyHorse" vs "阿里悟空"）
  - 同主题不同事件（"OpenAI 战略文件" vs "Anthropic Claude 限制" — 都是 AI 战略,但不是同事件）
  - 短文本（如仅含 URL / 标题不含具体事件信息 / Twitter 280 字内仅含模糊描述）→ 必须 same_event=false

允许加入的关系：
- same_event：同一个事件本体，例如同一个产品发布、同一次融资、同一次关闭功能。
- direct_commentary：直接围绕该事件的评论、实测、教程、影响分析（必须能在 New Doc 中点名引用主事件的具体实体）。
- follow_up_update：同一事件的后续补充信息，例如官方澄清、价格补充、上线范围变化。

多源转发同一具体项目（同一 GitHub repo / 同一产品发布 / 同一具体教程）算 same_event。

## 不是同一事件

以下情况必须判为 no：
- 同主题但不同事件。
- 同公司但不同产品或不同动作。
- 同类产品/框架/模型的横向比较。
- 多个不同 GitHub repo 或资源导航拼成的合集。
- 两条独立观点或经验分享。
- 只是都提到了 AI / Claude / GPT / Agent 等宽泛词。
- 同一个作者连续发了多条互不相关的内容。
- 同一个平台上多个不同 repo / 工具 / 教程被拼成资源合集。

## 推荐页内容类型参考

最新事件偏好这些信息类型：
- 新产品或官方功能发布
- 新工具、新插件、新框架、新开源项目发布
- 模型发布、benchmark、成本、能力变化
- 技术架构或关键机制的新发现
- 行业动态、融资、并购、政策、算力、芯片
- 高价值教程/案例，前提是多来源都围绕同一个具体主题

这不是硬过滤规则。如果内容确实是同一具体事件，可以保留。

## 判断步骤

请按顺序思考，但最终只输出 JSON：

1. 抽取 New Doc 的核心主体、核心动作、时间和内容类型。
2. 对每个候选 cluster，抽取它的核心主体、核心动作、时间范围和主事件。
3. 分别判断主体是否一致、动作是否一致、时间是否合理、信息关系是否直接。
4. 如果只是主题相似，必须判为 no。
5. 不要选择 selected_cluster_id；只输出每个候选的 `same_event / confidence / relationship`。代码层会基于你的输出挑出最优候选。

## 输入

### New Doc
{new_doc}

### Candidate Clusters
{candidate_clusters}

## 输出

只输出严格 JSON，不要 markdown 代码块，不要解释。

{
  "new_doc_fingerprint": {
    "subject": "核心主体",
    "action": "核心动作",
    "time": "事件时间",
    "event_type": "product_launch / tool_release / model_update / industry_news / tutorial_case / technical_insight / opinion / resource_collection / other"
  },
  "matches": [
    {
      "cluster_id": 123,
      "same_event": true,
      "confidence": "high",
      "relationship": "same_event",
      "subject_check": "同主体/不同主体，简短说明",
      "action_check": "同动作/不同动作，简短说明",
      "time_check": "时间窗口判断",
      "shared_entity": "如 same_event=true,必须在此填写共享的具体可命名实体(产品名+版本号/公司动作/事件主语/项目repo);same_event=false 时填空字符串",
      "rationale": "50字以内说明,必须明确指出共享实体或差异"
    }
  ]
}

字段约束：
- `confidence` 只能是 `high` / `medium` / `low`。
- `relationship` 只能是 `same_event` / `direct_commentary` / `follow_up_update` / `same_topic_only` / `unrelated`。
- `same_event=true` 必须配合 `relationship in (same_event, direct_commentary, follow_up_update)`；其他 relationship 一律 `same_event=false`。
- **BF-0428-3**: `same_event=true` 时 `shared_entity` 必须**非空且具体**(产品名+版本号 / 公司动作 / 事件主语 / 项目repo),不允许填"AI产品"/"浏览器"/"AI公司"等宽泛词;否则 `same_event=false`。
- 每个候选 cluster 都必须出现在 matches[] 中（哪怕判 no），不要省略。
- 不要输出 `selected_cluster_id` / `decision` / `possible_merge_candidates` / `should_be_visible_if_single_doc`。代码层负责选择。

## 反例参考（这些场景必须 same_event=false）

- New Doc: "在 Moxt 我多了一群可爱的 AI 同事" + Cluster: "阿里 HappyHorse 1.0 视频生成模型发布" → 都属"AI 产品",但 Moxt(AI 智能体工作空间)与 HappyHorse(AI 视频生成)是**不同产品**,**shared_entity 无具体重叠**,must same_event=false。
- New Doc: "Emirates A380 配 Starlink Wi-Fi" + Cluster: "SpaceX Falcon Heavy 发射 Viasat-3 F3" → 都属"航天/卫星通信"主题,但**事件主语不同**(航空公司机舱升级 vs 火箭发射任务),must same_event=false。
- New Doc: "微软 VibeVoice 语音模型" + Cluster: "yapp 个人开发者播客工具" → 都属"语音 AI",但**主体不同**(微软官方 vs 个人项目),must same_event=false。
- 仅含短链接 / 仅含标题 / 280 字内的 Twitter 帖,无法识别具体共享实体 → must same_event=false。
