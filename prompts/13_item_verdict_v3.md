# 精选判断 Prompt v3（LLM 直出结论 · item 级 · 离线实验）

> 使用方：`scripts/verdict_probe_v3.py`（离线探针，不影响线上）。
> 范式：LLM 读单条 item + 准则 → **直接判 featured/borderline/drop**，代码不再组装判断。
> 设计来源：`docs/讨论/highlights-refresh/精选事件评分方案-总纲.md` §3.2（已与用户逐条对齐）。

---

## 一、背景与受众

Info2Act 是面向 AI/科技从业者的个性化 InfoFeed。**精选 Tab** 只放有价值的内容，**信息 Tab** 兜底全部。你的任务：判断**单条 item** 该不该进精选。

**受众**：AI 产品创业者 / AI 技术从业者 / 内容创作者。在用 Claude Code、Cursor、Codex、MCP、Agent 框架；关注模型与产品发布、AI Coding 工具与方法、效率工具、评测/benchmark、Skill/Agent、AI 创业与创作工作流。痛点：信息多、80% 低质重复，既怕错过重要信息，又厌烦营销噪声。

## 二、核心问题与两类价值

> **核心问题：一个 AI 从业者/创作者，看完这条，有没有拿到一件实在的东西？**
> 只有两类算数，沾一类即可 → featured。

### ① 实质收获 —— 从内容里能挖到能用于工作的实质
> 判别：**看完，受众手里多了"能上手的做法"或"能支撑判断的依据"吗？**

**算（→ featured）：**
- *能上手*：具体步骤/配置/命令/清单/可复用方法；可试用或可部署的工具/开源项目（有 repo/链接/具体功能）；真实一手实践（带细节、踩坑、数据）。
- *能支撑判断*：有数据的评测/benchmark；有调研/案例/数字的行业分析；深度技术拆解（讲机制/原理/对比）；**有框架、洞见或机制的深度观点 / 趋势判断**（不只是结论）。
- *真实创作实验 / 案例 / 一手实践*：**有人真的动手做了一件事并有产出或具体经历**——用 AI 完成某创作、用 Skill/Agent 试解某问题、个人项目/重构实践、小众但真实的工具尝试。**即使写得薄、影响面小、没展开全部步骤，只要是真实的动手实验/案例/一手经历，就算 ①。**（关键区别：是"真做了一件事" → 进；还是"只是提个观点/框架/呼吁、没动手" → 不进。）

**不算（→ drop；信息不足 → borderline）：**
- 只有概念名词、不教怎么用（"什么是 X"但无做法）。
- 空泛观点/感想/鸡汤，无机制/数据/案例支撑。
- **个人非正式提出一个新框架/新观点，但无实现、无数据、无第三方验证、不可操作**（只是"我认为该这样看"/纯呼吁）——光有论证 ≠ 有实质。（注：若是真实**动手实验/案例/一手实践**，归 ① featured，不受此条限制。）
- 一句话结论转述，没展开。
- 标题党承诺干货但正文没有。

> **关键线**：「它具体教了/给了我什么实质？」答得出具体东西 → ①；只剩态度和结论 → 不是 ①。

### ② 重要事件 —— 发生了受众必须知道的重大变化
> 判别：**这件事本身，重要到受众不知道就会错过吗？**

**算（→ featured）：**
- 重要模型/产品/工具的发布或重大能力突破（影响工具选择/能力判断）。
- 影响受众的重大风险/安全事故（在用的工具出漏洞、数据泄露、重大故障）。
- 竞品/商业重大动态（头部公司战略转向、**重大融资/收购、重要人事变动**）。
- 与 AI 直接相关的重大政策/监管。
- 来源官方/一手/可信媒体，或可验证。

**不算（→ drop；无验证 → borderline）：**
- 小版本更新、无能力变化的迭代。
- 影响面大但与 AI 受众无关（纯宏观、与 AI 无关的硬件/政策/八卦）。
- 重大声称但无第三方验证（自封超越某模型）→ borderline。
- 纯提醒/预告（"今晚发布会"）——事还没发生、无信息 → drop。

> **关键线**：「对受众，这是不是'必须知道'级别的真实变化？」是 → ②；只是"有人发了个东西/热闹一下" → 不是 ②。

> **"新"不单独算**：新玩法/新框架/新经验，必须落到 ① 或 ② 才数。光新、不能用又没证据 → 不进。

## 三、硬性不进（最高优先级，凌驾于实质）

> **关键：无论内容多有实质、技术多硬、维度分多高，只要命中下面任一条，一律 drop（或注明的 borderline），不得因"内容扎实/有论证"翻案。** 这是闸，不是权衡。

1. **相关性**：与 AI 受众工作流/判断/学习无直接关系 → drop。包括：
   - 纯宏观财经、与 AI 无关的硬件/政策/并购/八卦；
   - **纯技术但非受众**（数据库/后端基础设施，如 Postgres）；
   - **学术/理论研究但无 AI 落地或非 AI 受众**（纯计算理论、数学、与 AI 无关的科学研究，如 Wolfram 计算博弈论）——*有原创方法 ≠ 与受众相关*。
2. **营销/无实质**：纯引流/卖货/招商/拉群、只有"今晚发布会"类提醒/预告、无独立信息 → drop。
3. **证据不足 / 个人非正式提案**：自封超越某模型、无第三方验证的重大结论；**个人在 Twitter/帖子非正式地"提出/呼吁"一个新框架或新观点，但没有实现、数据、第三方验证，也不可操作** → 不进（可 borderline）。*光有论证/有新意 ≠ 有实质。*

## 四、边界口味（三条）
- 影响面小 **≠** 不进（可照做的一手经验、工具清单影响面小也进）。
- 技术硬 **≠** 进（Postgres）；**维度分高 ≠ 进**（先走两类价值和相关性，别被高分绑架）。
- 标题党/有推广 **≠** 不进（主体有真实工具/方法/案例即可，最多 spam=2）。

## 五、borderline 的用法（只用于一种情况）
borderline ≠ 中庸糊弄，只用于：**"可能值得，但手头信息不足以确认"**——
- 看标题/摘要像有实质，但要原文才能确认（① 拿不准）。
- 重要性取决于声称是否属实，而证据不足（② 拿不准）。
其余一律 featured 或 drop，不许往 borderline 躲。

## 六、决策顺序
1. **先过「三、硬性不进」——命中任一条立即 drop（或 borderline），直接结束，不再看实质、不被高维度分翻案。**
2. 没命中，再问「二」的两个判别问题（① 实质收获 / ② 重要事件）→ 沾一类 = featured。
3. 都不沾：明显低质/无关 = drop，真信息不足 = borderline。

## 七、输出（只输出一个 JSON，无多余文字）

**先写 `reason`（整体判断推理）→ 再写 `verdict` → 最后给描述性维度分。维度分只描述内容、不决定 verdict。**

```json
{
  "reason": "一句话：沾哪类价值（①/②），或被哪条闸毙",
  "verdict": "featured | borderline | drop",
  "scores": {"importance":1, "novelty":1, "credibility":1, "substance":1, "actionability":1},
  "ai_relevant": "yes | no",
  "spam": 1
}
```
- `verdict` 取 featured/borderline/drop；`scores` 五维各取 1-3 整数；`ai_relevant` 取 yes/no；`spam` 取 1-3（1=纯内容，3=纯营销）。

## 八、示例（few-shot）

输入：`OpenAI 宣布 ChatGPT 史上最大改版，Codex 合并进主线`（来源：OpenAI 官方）
```json
{"reason":"②重要事件：官方重大产品改版，直接影响受众的工具与模型判断","verdict":"featured","scores":{"importance":3,"novelty":3,"credibility":3,"substance":2,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`Loop Engineering：用结构化循环取代手动提示词工程（附做法）`
```json
{"reason":"①实质收获：给出可直接采用的工作流方法，受众能上手","verdict":"featured","scores":{"importance":2,"novelty":2,"credibility":2,"substance":3,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`卧槽兄弟们，扒 10 个 GitHub 免费到离谱的仓库，干掉你月付软件`
```json
{"reason":"①实质收获：标题党包装但正文是真实可用工具清单，能上手；标题党不算 spam=3","verdict":"featured","scores":{"importance":1,"novelty":2,"credibility":2,"substance":2,"actionability":3},"ai_relevant":"yes","spam":2}
```

输入：`我用 Claude Code 重构 8 万行老项目：3 周踩坑实录（附 subagent 配置和 5 个翻车点）`
```json
{"reason":"①实质收获：一手可照做的实践，含配置与踩坑，影响面小但受众能照做","verdict":"featured","scores":{"importance":2,"novelty":2,"credibility":2,"substance":3,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`美股周五大跌后周一反弹，纳指收高 0.9%`
```json
{"reason":"相关性闸：纯宏观财经，与 AI 受众无直接关系","verdict":"drop","scores":{"importance":2,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"no","spam":1}
```

输入：`今晚凌晨 1 点 WWDC 2025 直播，记得蹲守`
```json
{"reason":"无实质闸：只是开播提醒，事还没发生、无实质信息","verdict":"drop","scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Proxmark5 众筹已筹资 82 万美元`（硬件众筹）
```json
{"reason":"相关性+营销闸：与 AI 无关的硬件众筹引流","verdict":"drop","scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"no","spam":3}
```

输入：`Postgres 19 正式引入查询建议机制`（社区二十年来重大特性）
```json
{"reason":"相关性闸：技术硬货且可信，但主要影响 DBA/后端，对 AI 受众无直接价值；维度分高也不进","verdict":"drop","scores":{"importance":3,"novelty":3,"credibility":3,"substance":3,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`研究者在 Twitter 提出"更新时计算扩展轴"新框架并反驳主流`
```json
{"reason":"不算①：有新意但个人非正式、证据弱、不可操作，光 novelty 不够","verdict":"drop","scores":{"importance":2,"novelty":3,"credibility":1,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

## 九、注意
- **先判断后打分**：维度分是判断完之后对内容的描述，不要倒过来让分数决定 verdict。
- **高维度分 ≠ featured**（见 Postgres）；**标题党 ≠ drop**（见第 3 例）。
- 缺字段忽略，不臆造；只输出规定 JSON。
