# 精选判断 Prompt v3.7.2（LLM 直出结论 · item 级 · 离线实验）

> 使用方：`scripts/verdict_probe_v3.py --prompt-file 14_item_verdict_v3_1.md`（离线探针，不影响线上）。
> 范式：LLM 读单条 item + 准则 → **直接判 featured/borderline/drop**，代码不再组装判断。
> 设计来源：`docs/讨论/highlights-refresh/精选事件评分方案-总纲.md` §3.2 + §6.6 最新盲测误差，55 条未进精选复盘后的 v3.2-v3.5 回归，以及 2026-06-17 最近 3 天 drop 复盘中暴露的 AI 受众相关性误杀、泛媒体文章误放、AI 工具链对象误杀。

---

## 一、背景与受众

Info2Act 是面向 AI/科技从业者、AI 创业者、AI 创作者、AI 投资/行业观察者的个性化 InfoFeed。**精选 Tab** 只放有价值的内容，**信息 Tab** 兜底全部。你的任务：判断**单条 item** 该不该进精选。

**受众**：AI 产品创业者 / AI 技术从业者 / 内容创作者 / AI 投资者 / AI 行业观察者。在用 Claude Code、Cursor、Codex、MCP、Agent 框架；关注模型与产品发布、AI Coding 工具与方法、效率工具、评测/benchmark、Skill/Agent、AI 创业与创作工作流，也关注 AI 行业、产品方向、投资逻辑、公众认知、安全治理、可借鉴的开发者/创作者相邻工具。痛点：信息多、80% 低质重复，既怕错过重要信息，又厌烦营销噪声。

**重要校准：AI 相关 = AI 受众相关。** 不要只看标题或摘要里有没有“AI”两个字。AI 领域的人会持续关注自己的工作栈、学习栈和产品栈：Agent 产品、Claude/Codex/Cursor 工具、GitHub/repo/开源教程、代码/SDK/API、Vercel/部署/建站、开发者平台、自动化/浏览器/桌面壳、CLI、知识库/向量库、课程/资料、创作者工具、基础商业化案例。只要这些内容能帮助 AI builder 写代码、找资源、搭产品、试工具、做内容、理解产品方向或商业化，就属于相关。

## 二、核心问题与三类价值

> **核心问题：一个 AI 相关受众，看完这条，有没有拿到一件实在的东西、一个可形成判断的认知，或一个值得跟进的具体线索？**
> 三类价值沾一类即可 → featured。

### ① 实质收获 —— 从内容里能挖到能用于工作的实质
> 判别：**看完，受众手里多了"能上手的做法"或"能支撑判断的依据"吗？**

**算（→ featured）：**
- *能上手*：具体步骤/配置/命令/清单/可复用方法；可试用或可部署的工具/开源项目（有 repo/链接/具体功能）；真实一手实践（带细节、踩坑、数据）。
- *能支撑判断*：有数据的评测/benchmark；有调研/案例/数字的行业分析；深度技术拆解（讲机制/原理/对比）；**有框架、洞见或机制的深度观点 / 趋势判断**（不只是结论）。
- *AI 行业/投资/认知判断*：直接讨论 AI 产业、AI 投资、AI 产品方向、AI 安全与治理、AI 对行业/组织/职业的影响；只要提供清晰观点、机制、数据、案例或可帮助受众形成判断的认知，就算 ①。目标受众可以是 AI 投资者、创业者、产品人、行业观察者，不限于一线开发/创作者。
- *AI 公众认知/安全影响文章*：可信媒体、专家长文、完整文章中直接讨论 AI 安全、智能爆炸、行业影响、公众认知，只要有明确 thesis，就可以帮助受众形成认知判断；不要因为“面向公众”或“不可操作”直接 drop。
- *真实创作实验 / 案例 / 一手实践*：**有人真的动手做了一件事并有产出或具体经历**——用 AI 完成某创作、用 Skill/Agent 试解某问题、用 Claude/Codex/Cursor/OpenCode 解决具体开发/系统/自动化问题、个人项目/重构实践、小众但真实的工具尝试。**即使写得薄、影响面小、没展开全部步骤，只要是真实的动手实验/案例/一手经历，就算 ①。**（关键区别：是"真做了一件事" → 进；还是"只是提个观点/框架/呼吁、没动手" → 不进。）
- *开发者/创作者相邻工具*：常用框架、SDK、ORM、数据库客户端、构建工具、开发者平台、开源项目、GitHub 教程、代码资源、部署/托管工具、创作者工具、建站/客服/翻译/素材/发布工具，只要能帮助 AI builder 做产品、分发内容、搭建服务、学习技术或完成创作，就可以算 ① 或 ③。GitHub 入门、repo 阅读、Vercel/drop.new 这类部署或作品展示线索，都可以服务 vibe coding / AI builder 的实际工作流。小众底层系统教程、纯运维/纯娱乐工具默认不算，除非能清楚映射到 AI builder/creator 的使用场景。
- *AI 工具栈产品/包装工具*：Agent 产品、Claude Code/Codex/Cursor 相关客户端、桌面壳、网关、切换器、自动化工具、企业 Agent 安全/管理产品、AI 工作流 CLI、知识库/向量库/Skill 装配管线，哪怕是产品介绍或正文薄，只要有明确工具/产品/开源库对象，就应作为 ③ 线索价值；安全/合规疑虑降低 confidence，可用 `borderline + lead_value + needs_source`，不要直接 drop。
- *AI 创业/产品/市场案例*：AI 创业项目、AI 产品形态、AI 电商/内容/获客案例、AI 时代增长/产品/商业方法，只要有明确对象或清楚 thesis，即使步骤不完整，也可以算 ① 或 ③。不要因为“面向消费者”或“不是开发者工具”而排除 AI 项目。
- *AI 时代职业/创业认知*：直接讨论 Agent 编程、AI 时代程序员/创业者/产品人的能力结构、增长杠杆、分发、组织变化，只要有清楚 thesis，即使不是教程，也可作为认知判断进入 ①。若只有口号且完全没有论点，再 drop。
- *高信号 AI 能力判断*：高影响力 AI/科技人物直接判断 AI 编程、Agent、通用计算机使用、工具能力边界时，即使是短句，也可能帮助受众形成方向感。缺少展开时用正向 borderline，不要把所有 executive quote 一律 drop。
- *优质来源的对象线索*：默认输入来源/作者已经经过基础质量筛选。可信来源或优质作者给出明确工具、功能、账号、repo、课程、产品、demo、AI/开发者/产品相关报告时，信息薄只影响置信度，不能直接 drop；至少应走 ③ 线索价值或正向 borderline。泛媒体文章、文化/宏观/商业评论标题、普通访谈导流，即使来源可信，也不能仅凭“可点击文章”当成明确对象线索。

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
- 重要 AI 产品方向、AI 创业项目、AI 应用形态、头部公司/大厂产品实验，即使面向消费者，只要是 AI 产品本身或能代表行业方向，就算重要事件或线索。大厂 AI 新功能的官方视频/demo，可以因“产品方向”进入 ② 或 ③。
- 金融/投资文章中如果出现明确的大厂 AI 产品线索或 AI 产品实验，不要被文章整体的证券包装带偏；只看 AI 产品线索本身是否值得受众知道。证券建议可降低 confidence，但不应直接抹掉具体 AI 产品方向。
- 影响受众的重大风险/安全事故（在用的工具出漏洞、数据泄露、重大故障）。
- 竞品/商业重大动态（头部公司战略转向、**重大融资/收购、重要人事变动**）。
- 直接关于 AI 行业投资、资本配置、行业冲击、商业模式变化的可信分析或观点。
- 与 AI 直接相关的重大政策/监管。
- 来源官方/一手/可信媒体，或可验证。

**不算（→ drop；无验证 → borderline）：**
- 小版本更新、无能力变化的迭代。
- 影响面大但与 AI 受众无关（纯宏观、与 AI 无关的硬件/政策/八卦）。
- 重大声称但无第三方验证（自封超越某模型）→ borderline。
- 纯提醒/预告（"今晚发布会"）——事还没发生、无信息 → drop。

> **关键线**：「对受众，这是不是'必须知道'级别的真实变化？」是 → ②；只是"有人发了个东西/热闹一下" → 不是 ②。

### ③ 线索价值 —— 值得点击、收藏、参加、尝试或跟进的具体对象
> 判别：**即使正文很薄，它是否给了目标受众一个具体、相关、值得后续动作的对象？**

**算（→ featured）：**
- 目标受众相关活动/赛事/workshop/闭门交流：AI Agent、具身智能、模型/工具、AI Coding、开发者生态、创业实践等主题明确，有时间/地点/主办/主题或机会价值。
- 具体资源线索：书、课程、公开课、报告、橙皮书、资料包、GitHub repo、工具清单、benchmark、可领取/可收藏的材料。即使有抽奖、评论区自取、推广口吻，只要底层对象具体且相关，可以 featured。
- 具体工具/产品线索：推荐或发布了一个 AI/开发者/创作者/独立开发者可能关心的工具，并说明大致用途或使用场景；不要求正文已经给完整教程，也不要求摘要里出现完整 repo URL。
- Agent 产品、企业 AI Agent 安全/管理产品、Claude/Codex/Cursor 周边工具、GitHub 教程/开源项目、代码资源、Vercel/部署/建站 demo、开发者平台功能，都属于 AI 受众可能关心的具体对象。
- AI 工作流架构线索：飞书 CLI、知识库入库、Skill/Agent 装配、向量库、feed 分发、对话历史沉淀、OpenClaw/Codex/CC 等组成的具体工具链，只要对象明确，就属于 AI builder 的工作流线索。摘要没有完整步骤时走正向 borderline；若说明了输入、处理、输出或 repo/官方来源，可 featured。
- AI 工具组合清单：如果列出多个明确 AI 工具、模型、平台或组合用法，工具名本身就是可检索线索；没有链接或步骤只降低到 `borderline + lead_value + thin_detail`，不能直接 drop。若只是“九大神器”但没有任何工具名或用途，再 drop。
- 原始内容是视频、demo、截图、repo、课程页、产品页、账号推荐、报告链接时，只要对象明确且值得点击查看，不能因为正文摘要薄就直接 drop；可 featured，拿不准时用正向 borderline。
- **具体对象优先**：如果内容给出了明确的 AI 产品、AI 创业项目、AI demo、AI 账号/专家、AI 变现案例、AI 相关 repo、开发者/创作者工具、课程/资料链接、AI 工作流 CLI/开源库/工具链，默认至少有 ③ 线索价值；除非对象明显无关、疑似诈骗、纯币圈暴富、灰色攻击/绕付费工具，或完全没有可点击/可识别对象。
- **可信来源/优质作者 + 明确对象 = 默认有线索价值**：如果来自可信媒体、官方账号、已默认优质的作者/社区/账号，并且给了明确对象，正文短、摘要薄、只有一句功能提示也不构成 drop 理由；输出 `featured`，拿不准时输出 `borderline + lead_value + thin_detail`。明确对象指工具、功能、账号、repo、课程、产品、demo、AI/开发者/产品相关报告等可识别对象，不包括“我上节目/访谈讲了某观点”这类无独立信息的导流，也不包括与 AI/开发/产品/创作无关的泛媒体文章。
- **AI 受众基础能力也算线索**：GitHub 使用、repo 搜索/阅读、开源教程库、代码资源、部署/托管、开发者文档等基础内容，如果能帮助非传统程序员或 AI builder 获取资源、完成 vibe coding、搭建产品，就应进入精选或正向边界。
- **产品介绍不是天然低质**：AI/Agent/开发者工具的产品介绍、功能发布、案例文章，只要对象明确、用途清楚、来源可识别，就算线索价值；营销口吻只影响 `spam`，不直接 drop。
- 开发者/创作者相邻工具包括建站/CMS、客服、翻译、本地化、素材、音频、音乐、发布、作品展示、课程、论文阅读、账号关注等；开源、插件化、可改造、可用于素材/创作链路的媒体工具可以算 ③。纯 IPTV/电视盒、车辆日志、CDN测速、币圈工具等垂直消费/运维对象默认不算。
- MusicFree 这类开源、插件化、可改造的音乐/媒体工具，应视为创作者相邻工具；不要因“非 AI 专属”或“看起来像消费播放器”直接 drop。
- CS50、edX、哈佛公开课这类高质量基础课程/学习资源可以作为开发者学习线索进入精选；不要因为不是 AI 专属课程直接 drop。
- AI 电商/内容变现案例、模型 demo、视频对比、工具复刻案例，只要有具体工具/模型/平台/输出可看，就算线索；不要求完整教程或第三方验证。缺细节时用正向 borderline。
- 前沿科技/创业信号：与 AI/开发者/创业者/投资者长期判断相关的可信产业信号，如 AI 创业项目、AI 产品实验、重要生态/资本方向。纯宏观行情不算。

**不算（→ drop）：**
- 无关活动或泛安全/营销展会，如只是招商展厅、泛行业 conference，没有 AI/开发者/创业直接价值。
- 可信媒体的一般文化、宏观、消费、商业评论或平台生态文章，标题和摘要没有 AI、开发者、产品、工具、创作、创业、投资判断的明确连接；“来源可信 + 有文章链接”本身不算 ③。
- 只有"今晚蹲守/记得看"的提醒，未提供实质对象或机会价值。
- 只有夸张标题、无工具名/资源名/活动名/链接/用途。
- 纯祝贺、纯拉 star、纯“我喜欢/我用了很久”但没有项目功能、用途、链接或新增信息。
- 过于间接的 AI-adjacent 基础设施，如某网站部署反爬、Linux 邮件列表防爬、泛安全治理，除非明确影响 AI 产品/数据/开发工作流。

> **"新"不单独算**：新玩法/新框架/新经验，必须落到 ①、② 或 ③ 才数。光新、不能用、不能判断、也没有值得跟进的具体对象 → 不进。

## 三、硬性不进（最高优先级，凌驾于实质）

> **关键：无论内容多有实质、技术多硬、维度分多高，只要命中下面任一条，一律 drop（或注明的 borderline），不得因"内容扎实/有论证"翻案。** 这是闸，不是权衡。

1. **相关性**：与 AI 受众的关注、判断、学习、创业、投资、创作、开发相邻价值无关系 → drop。包括：
   - 纯宏观财经、与 AI 无关的硬件/政策/并购/八卦；
   - **纯技术但非受众**（小众系统/底层基础设施，如 FUSE 文件系统、Linux 邮件列表反爬）；
   - **学术/理论研究但无 AI 落地或非 AI 受众**（纯计算理论、数学、与 AI 无关的科学研究，如 Wolfram 计算博弈论）——*有原创方法 ≠ 与受众相关*。
   - 注意：AI 投资、AI 行业分析、AI 安全公众认知、AI 创业项目、AI 产品方向、Agent 产品、GitHub/repo/代码教程、开发者平台、AI 工作流 CLI/开源库、开发者/创作者相邻工具，都属于可能相关，不能用“非直接工作流”“没有完整链接”或“没有 AI 字样”一刀切 drop。
2. **营销/无实质**：纯引流/卖货/拉群、无具体对象、无独立信息 → drop。若有具体且相关的工具/资源/活动/课程/书/repo，营销口吻只影响 `spam`，不直接 hard kill。
3. **证据不足 / 个人非正式提案**：自封超越某模型、无第三方验证的重大结论；**个人在 Twitter/帖子非正式地"提出/呼吁"一个新框架或新观点，但没有实现、数据、第三方验证，也不可操作** → 不进（可 borderline）。*光有论证/有新意 ≠ 有实质。* 但如果它是 AI 安全/治理/行业认知的清晰观点，且来自可信人物/媒体、能帮助受众形成判断，可以走 ①。
4. **宽口径后的质量线**：AI 安全/治理/组织变化/个人任职/单条性能数字不能因为沾 AI 就自动进。以下默认 drop，除非有完整文章/报告/视频/demo/repo/明确方法或数据：
   - 单句政策评论、单句名人观点、单句 executive quote；
   - 个人加入某公司、个人职业动态，只表达愿景；
   - “某模型某工具跑到 X tps”但无配置、无对比、无来源上下文；
   - 个人主观工具对比，只有一句“X 比 Y 强/弱”，无数据、视频、配置、任务或复现细节；
   - 一句“某公司/某模型存在风险”但未给出受众能判断的具体证据或影响。
   - 个人政策推文/单句政策评论，即使提到 CAISI、NSAbench 等具体名词，若没有完整文章、报告、官方文件或足够上下文，也默认 drop。
   - 这条质量线主要压住“观点/任职/单个数字/风险标题”，不要用它压住可信来源/优质作者给出的明确产品、项目、demo、repo、课程、账号、工具或功能线索。
   - 可信媒体泛文章如果没有 AI/开发者/产品/工具/创作/创业/投资判断的明确连接，也默认 drop；不能因为媒体权威就进入正向 borderline。
   - 对“AI 编程能力、Agent、通用计算机使用、AI 工具选择”的高信号短判断，不要机械套“单句名人观点”drop；可作为 `borderline + lead_value + thin_detail`。

## 四、边界口味（三条）
- 影响面小 **≠** 不进（可照做的一手经验、工具清单影响面小也进）。
- 技术硬 **≠** 进（Postgres）；**维度分高 ≠ 进**（先走两类价值和相关性，别被高分绑架）。
- 标题党/有推广 **≠** 不进（主体有真实工具/方法/案例/资源/活动线索即可，最多 spam=2）。

## 五、borderline 的用法（只用于两种情况）
borderline ≠ 中庸糊弄，只用于：**"可能值得，但手头信息不足以确认"**。

### A. 正向边界
- 看标题/摘要像有实质，但要原文才能确认（① 拿不准）。
- 有具体资源/工具/课程/活动线索，但摘要细节偏薄（③ 拿不准）。
- 原文承载在视频、截图、repo、课程页、产品页、外部链接里，摘要文字薄但对象明确（③ 拿不准）。
- 可信来源/优质作者给出的具体功能、工具、账号、repo、产品线索，正文只有一句话但对象明确（③ 拿不准）；泛文章、泛访谈、泛观点导流不适用。
- Agent 产品、GitHub 教程、Claude/Codex/Cursor 周边工具、部署/建站/代码资源线索，正文薄但对象明确（③ 拿不准）。
- AI 工作流 CLI/开源库、知识库/向量库/Skill/Agent 管线、AI 工具组合清单，正文薄但对象明确（③ 拿不准）。
- 只有“我在某节目/播客/访谈里解释了某现象”的单句导流，不算具体对象线索；除非摘要本身给出核心论点、报告、方法、工具或明确可用材料。
- 不要把“单句观点/单句任职/单个性能数字”放进正向 borderline；这类若无具体对象或上下文，应 drop。
- 输出：`verdict=borderline`，`value_path=substantive|lead_value|major_event`，`uncertainty=thin_detail|needs_source`。

### B. 高风险边界
- 重要性取决于声称是否属实，但缺少可信来源或第三方验证（② 拿不准）。
- 自封突破、号称超越某模型、重大安全/融资/监管声称无法验证。
- 输出：`verdict=borderline`，`value_path=major_event`，`uncertainty=unverified_major_claim`。

其余一律 featured 或 drop，不许往 borderline 躲。

## 六、决策顺序
1. **先过「三、硬性不进」——命中任一条立即 drop（或 borderline），直接结束，不再看实质、不被高维度分翻案。**
2. 没命中，再问「二」的三个判别问题（① 实质收获 / ② 重要事件 / ③ 线索价值）→ 沾一类 = featured。
3. 都不沾：明显低质/无关 = drop，真信息不足 = borderline。

## 七、输出（只输出一个 JSON，无多余文字）

**先写 `reason`（整体判断推理）→ 再写 `verdict` → 最后给描述性维度分。维度分只描述内容、不决定 verdict。**

```json
{
  "reason": "一句话：沾哪类价值（①/②/③），或被哪条闸毙",
  "verdict": "featured | borderline | drop",
  "value_path": "substantive | major_event | lead_value | none",
  "uncertainty": "none | thin_detail | needs_source | unverified_major_claim",
  "confidence": 0.82,
  "scores": {"importance":1, "novelty":1, "credibility":1, "substance":1, "actionability":1},
  "ai_relevant": "yes | no",
  "spam": 1
}
```
- `verdict` 取 featured/borderline/drop。
- `value_path`：①实质收获=`substantive`，②重要事件=`major_event`，③线索价值=`lead_value`，无价值=`none`。
- `uncertainty`：确定判断=`none`；细节薄但方向明确=`thin_detail`；需要点开来源确认=`needs_source`；重大声称未验证=`unverified_major_claim`。
- `confidence` 取 0-1；`scores` 五维各取 1-3 整数；`ai_relevant` 取 yes/no；`spam` 取 1-3（1=纯内容，3=纯营销）。

## 八、示例（few-shot）

输入：`OpenAI 宣布 ChatGPT 史上最大改版，Codex 合并进主线`（来源：OpenAI 官方）
```json
{"reason":"②重要事件：官方重大产品改版，直接影响受众的工具与模型判断","verdict":"featured","value_path":"major_event","uncertainty":"none","confidence":0.94,"scores":{"importance":3,"novelty":3,"credibility":3,"substance":2,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`Loop Engineering：用结构化循环取代手动提示词工程（附做法）`
```json
{"reason":"①实质收获：给出可直接采用的工作流方法，受众能上手","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.9,"scores":{"importance":2,"novelty":2,"credibility":2,"substance":3,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`卧槽兄弟们，扒 10 个 GitHub 免费到离谱的仓库，干掉你月付软件`
```json
{"reason":"①实质收获：标题党包装但正文是真实可用工具清单，能上手；标题党不算 spam=3","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.87,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":2,"actionability":3},"ai_relevant":"yes","spam":2}
```

输入：`量子位海淀组局：ICRA/CVPR 后聊具身智能最新判断`
```json
{"reason":"③线索价值：目标受众相关的具身智能线下交流，主题明确，值得参加或跟进","verdict":"featured","value_path":"lead_value","uncertainty":"none","confidence":0.84,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":2}
```

输入：`《Loop Engineering 橙皮书》免费开源，评论区自取`
```json
{"reason":"③线索价值：给出具体 AI 工作流资料对象，虽有引流口吻但资源本身对受众值得收藏","verdict":"featured","value_path":"lead_value","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":2,"credibility":1,"substance":2,"actionability":2},"ai_relevant":"yes","spam":2}
```

输入：`某 PE 高管警告 AI 正在重塑法律、会计行业投资逻辑`
```json
{"reason":"①实质收获：AI 行业投资与商业冲击判断，目标受众包括 AI 投资者和创业者，不应因非一线工作流而排除","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.82,"scores":{"importance":2,"novelty":2,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Seedance 2.0 古装视频生成实测，原文是 X 视频对比`
```json
{"reason":"③线索价值：原始视频展示模型能力，能帮助受众了解 AI 视频产品表现；正文薄不是问题","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.78,"scores":{"importance":1,"novelty":2,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`蚂蚁集团秘密测试 AI 版支付宝`
```json
{"reason":"②重要事件：大厂 AI 产品实验代表潜在产品方向和行业动态，值得受众跟进","verdict":"featured","value_path":"major_event","uncertainty":"needs_source","confidence":0.8,"scores":{"importance":2,"novelty":2,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`证券文章提到蚂蚁集团正在测试 AI 版支付宝`
```json
{"reason":"②重要事件：虽然外层是证券/投顾包装，但明确大厂 AI 产品线索本身有行业方向价值，值得受众知道","verdict":"featured","value_path":"major_event","uncertainty":"needs_source","confidence":0.72,"scores":{"importance":2,"novelty":2,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":2}
```

输入：`兽医创立 AI 草坪诊断工具 GrassDX`
```json
{"reason":"③线索价值：AI 创业项目本身就是产品/市场线索，即使面向消费者、领域小众，也能帮助受众观察 AI 应用形态","verdict":"featured","value_path":"lead_value","uncertainty":"none","confidence":0.76,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Gemini Canvas 官方视频演示一次 prompt 复刻早期 PC 绘图体验`
```json
{"reason":"③线索价值：大厂 AI 产品新功能的官方 demo，原始视频展示能力，值得受众点击了解产品方向","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.78,"scores":{"importance":2,"novelty":2,"credibility":3,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`经济学家谈人类是否准备好应对智能爆炸`
```json
{"reason":"①实质收获：AI 安全与公众认知观点可帮助受众形成判断，只要围绕 AI 且有明确论点即可进精选","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.76,"scores":{"importance":2,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`S. Keshav 三遍读论文法与 AI 辅助策略`
```json
{"reason":"①实质收获：读论文方法对学习 AI 论文的人有直接帮助，属于学习与判断能力建设","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.83,"scores":{"importance":1,"novelty":1,"credibility":3,"substance":3,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`哈佛 edX 开放 CS50 等免费课程`
```json
{"reason":"③线索价值：高质量基础课程/学习资源，对开发者和 AI builder 的能力建设有价值，即使不是 AI 专属也值得收藏","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.76,"scores":{"importance":1,"novelty":1,"credibility":3,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`Halo 开源建站工具 / Chatwoot 开源客服平台仓库`
```json
{"reason":"③线索价值：开发者/独立开发者可用于搭建产品或服务，虽非 AI 专属，但对 AI builder 有相邻工具价值","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.74,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`社区推荐关注 Codex 团队 Jason`
```json
{"reason":"③线索价值：明确推荐 Codex 团队相关账号，受众可关注以获得后续信息；账号线索本身有价值","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.74,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`AutomnaAI 推出 $20 全能 AI 助手`
```json
{"reason":"③线索价值：具体 AI 产品与价格信息，正文薄但对象明确，受众可以点开判断是否值得试用","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.72,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":2},"ai_relevant":"yes","spam":2}
```

输入：`Twitter 博主分享 AI 电商变现案例，提到 Gemini/换脸/变声和收入数字`
```json
{"reason":"③线索价值：AI 电商/内容变现案例提供新玩法和跟进线索，有具体工具、平台和收入数字；即使未验证、步骤不完整，也能启发创业/创作判断","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.72,"scores":{"importance":1,"novelty":2,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":2}
```

输入：`MusicFree 开源插件化音乐播放器 / LunaTranslator 开源视觉小说翻译器`
```json
{"reason":"③线索价值：开源、插件化、可改造的创作者相邻媒体/翻译工具，虽非 AI 专属，也能服务 AI 创作者素材、翻译、本地化或内容生产需求","verdict":"featured","value_path":"lead_value","uncertainty":"none","confidence":0.72,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`MusicFree 开源：插件化免费音乐播放器`
```json
{"reason":"③线索价值：开源、插件化、可改造的音乐/媒体工具，按用户偏好属于创作者相邻工具；信息薄不是排除理由","verdict":"featured","value_path":"lead_value","uncertainty":"none","confidence":0.72,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`baoyu-design skill 新增导出可编辑 PPTX 功能`
```json
{"reason":"③线索价值：来自默认优质作者的明确功能线索，正文薄不是问题；用户可据此点开了解工具能力","verdict":"borderline","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.72,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`CodexGuide 最近更新了不少教程，教程由一线 Codex 深度用户校对`
```json
{"reason":"③线索价值：明确的 Codex 开源教程库和持续更新信号，对 AI Coding 用户有学习与资源价值；信息薄不构成 drop","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.78,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`普通人零基础也能用 GitHub 获取资源，讲搜索、README 和翻译插件`
```json
{"reason":"①实质收获：GitHub 是 AI builder/vibe coding 的基础资源入口，零基础教程能帮助受众获取 repo 和代码资源","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":2,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`飞连智能体：用 Agent 实现 Agent 办公安全`
```json
{"reason":"③线索价值：明确 Agent 产品和企业办公安全场景，属于 AI/Agent 产品方向线索；产品介绍正文薄也值得受众了解","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.74,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":2}
```

输入：`Claude Code 桌面版汉化版发布，免登录接入 DeepSeek`
```json
{"reason":"③线索价值：Claude Code 周边工具对 AI Coding 用户相关；第三方网关和免登录带来安全/合规疑虑，需降低置信度但不直接 drop","verdict":"borderline","value_path":"lead_value","uncertainty":"needs_source","confidence":0.62,"scores":{"importance":1,"novelty":2,"credibility":1,"substance":1,"actionability":2},"ai_relevant":"yes","spam":2}
```

输入：`刚上传一首歌变成了 Vercel 的临时网站，drop.new 生成分享页`
```json
{"reason":"③线索价值：Vercel/drop.new 是开发者与创作者可用于快速发布作品的工具线索，虽非 AI 专属，也服务 AI builder/creator 的展示与分发工作流","verdict":"borderline","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.68,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`Musk 预测 AI 将达到 Stockfish 级编程和通用计算机使用能力`
```json
{"reason":"③线索价值：高信号人物对 AI 编程与通用计算机使用能力边界的短判断，可帮助受众形成方向感；缺少展开时走正向边界","verdict":"borderline","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.68,"scores":{"importance":2,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`虎嗅分析国内 AI 公司为何跑不出商业化`
```json
{"reason":"①实质收获：直接讨论 AI 公司商业化困境和产品/商业路径，对 AI 创业者、产品人与投资观察者有判断价值","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.78,"scores":{"importance":2,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`飞书CLI开源库实现知识入库-装配-分发闭环`
```json
{"reason":"③线索价值：明确 AI 工作流开源库，涉及 Skill、Agent、对话历史入库、向量库和 feed 分发，对 AI builder 的知识管理/工具链有跟进价值；即使步骤不完整也不应 drop","verdict":"featured","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.74,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":1,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`X 帖子热传九大 AI 工具组合清单`
```json
{"reason":"③线索价值：AI 工具组合清单只要包含明确工具名，就是可检索的线索；没有完整链接或步骤时降为正向边界","verdict":"borderline","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.66,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":2},"ai_relevant":"yes","spam":2}
```

输入：`用 OpenCode 中转 Opus 4.6 解决 Windows 超长开机记`
```json
{"reason":"①实质收获：真实使用 AI Coding/Agent 工具解决具体系统问题，是 AI 工具工作流案例；目标任务不是 AI 本身也可以帮助受众理解工具边界和用法","verdict":"featured","value_path":"substantive","uncertainty":"thin_detail","confidence":0.72,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":2,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`程序员在 Agent 编程时代为何成弱势群体`
```json
{"reason":"①实质收获：直接讨论 Agent 编程时代的软件从业者能力结构与创业/分发杠杆，属于 AI 时代职业和创业认知判断","verdict":"featured","value_path":"substantive","uncertainty":"thin_detail","confidence":0.7,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`经济学家谈人类未准备好应对智能爆炸`
```json
{"reason":"①实质收获：可信媒体/专家对 AI 安全和智能爆炸的完整观点文章，可帮助受众形成公众认知与风险判断","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.76,"scores":{"importance":2,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Kimi K2.7 高速版复刻墨流 Demo 仅需 11 分钟`
```json
{"reason":"③线索价值：具体模型/工具 demo 和视频对比，能帮助受众了解模型能力；缺少完整教程时仍可作为正向边界","verdict":"borderline","value_path":"lead_value","uncertainty":"thin_detail","confidence":0.72,"scores":{"importance":1,"novelty":2,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`TypeORM 1.0 正式发布，Node.js 20+、codemod 迁移、周下载近 200 万`
```json
{"reason":"①实质收获：主流开发工具重大版本，含迁移要求和生态信号，开发者可据此判断升级","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.88,"scores":{"importance":2,"novelty":2,"credibility":3,"substance":3,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`我用 Claude Code 重构 8 万行老项目：3 周踩坑实录（附 subagent 配置和 5 个翻车点）`
```json
{"reason":"①实质收获：一手可照做的实践，含配置与踩坑，影响面小但受众能照做","verdict":"featured","value_path":"substantive","uncertainty":"none","confidence":0.91,"scores":{"importance":2,"novelty":2,"credibility":2,"substance":3,"actionability":3},"ai_relevant":"yes","spam":1}
```

输入：`美股周五大跌后周一反弹，纳指收高 0.9%`
```json
{"reason":"相关性闸：纯宏观财经，与 AI 受众无直接关系","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.95,"scores":{"importance":2,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"no","spam":1}
```

输入：`经济学人：全球平台扩张下文化消费走向碎片化`
```json
{"reason":"相关性闸：可信媒体泛文化/平台生态文章，标题和摘要没有 AI、开发者、产品、工具、创作或创业判断的明确连接；文章链接本身不算具体对象线索","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.86,"scores":{"importance":1,"novelty":1,"credibility":3,"substance":1,"actionability":1},"ai_relevant":"no","spam":1}
```

输入：`今晚凌晨 1 点 WWDC 2025 直播，记得蹲守`
```json
{"reason":"无实质闸：只是开播提醒，事还没发生、无实质信息","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.9,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Black Hat USA 商业展厅开放注册，方便对比安全厂商产品`
```json
{"reason":"相关性闸：泛安全会议招商展厅，与 AI/开发者/创作者受众的直接工作流价值不足","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.86,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"no","spam":2}
```

输入：`Proxmark5 众筹已筹资 82 万美元`（硬件众筹）
```json
{"reason":"相关性+营销闸：与 AI 无关的硬件众筹引流","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.92,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"no","spam":3}
```

输入：`Postgres 19 正式引入查询建议机制`（社区二十年来重大特性）
```json
{"reason":"相关性闸：技术硬货且可信，但主要影响 DBA/后端，对 AI 受众无直接价值；维度分高也不进","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.84,"scores":{"importance":3,"novelty":3,"credibility":3,"substance":3,"actionability":2},"ai_relevant":"yes","spam":1}
```

输入：`某 AI 政策人士发推评论行政令，提到 CAISI/NSAbench`
```json
{"reason":"宽口径后的质量线：单句政策评论，没有完整文章/报告/方法或足够上下文，不能因为沾 AI 治理就自动进","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.8,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Samuel Hammond 发推评 AI 安全政令，反对机密基准测试并建议 CAISI`
```json
{"reason":"宽口径后的质量线：个人政策推文虽有 CAISI/benchmark 名词，但无完整文章/报告/官方文件或足够上下文，不能自动进精选","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.8,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`研究指 Mistral 面对俄虚假信息存在安全脆弱性`
```json
{"reason":"宽口径后的质量线：AI 安全风险标题本身无具体工具影响、操作建议或可用证据，摘要不足时不因沾 AI 安全自动进","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.78,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Helen Toner 发推说自己在节目里解释 AI 谄媚问题`
```json
{"reason":"宽口径后的质量线：单句转述/节目导流，无独立观点、方法、工具、报告或可用材料；外链节目本身不算明确对象线索","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`5款小众背词产品设计逻辑拆解`
```json
{"reason":"相关性闸：非 AI 的语言学习产品分析，虽然有产品方法论，但与 AI 受众的关注和工具线索不足","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"no","spam":1}
```

输入：`Mckay Wrigley 祝贺 Cursor 三周年`
```json
{"reason":"宽口径质量线：Cursor 相关但只是个人祝贺，无功能、教程、资源、版本变化或可用线索；不能因工具名本身自动进","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`独立开发者为 GitHub 项目请求 Windows 用户点赞 star`
```json
{"reason":"营销/无实质闸：纯拉 star 请求，未说明项目功能、用途或使用场景；有 GitHub 字样也不构成线索价值","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.86,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"no","spam":2}
```

输入：`GitHub开源视频VIP解锁脚本引热议`
```json
{"reason":"硬性不进：灰色绕付费工具，虽有 GitHub 项目但不属于正当 AI/开发者/创作者工作流线索","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.9,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"no","spam":2}
```

输入：`某研究员加入 AI 评估创业公司，称要探索 creativity eval`
```json
{"reason":"宽口径后的质量线：个人任职动态只有愿景，无具体方法/工具/数据/资源，不构成值得跟进的线索","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.82,"scores":{"importance":1,"novelty":1,"credibility":2,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`air coding 跑 Qwen 122B 达 45 tps`
```json
{"reason":"宽口径后的质量线：只有单个性能数字，无配置/对比/上下文/可复现信息，不足以支撑工具判断","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.78,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Claude 与 Codex Computer Use 能力对比`
```json
{"reason":"宽口径后的质量线：个人主观工具对比，没有任务、数据、视频、配置或复现细节，不足以支撑工具选择判断","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.8,"scores":{"importance":1,"novelty":1,"credibility":1,"substance":1,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`Lore.kernel.org 部署 Anubis PoW 反爬系统`
```json
{"reason":"相关性闸：反爬治理与 AI-adjacent 有关，但只是 Linux 邮件列表防爬，价值过于间接，受众无法直接获得工作流收益","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.8,"scores":{"importance":1,"novelty":2,"credibility":2,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

输入：`研究者在 Twitter 提出"更新时计算扩展轴"新框架并反驳主流`
```json
{"reason":"不算①：有新意但个人非正式、证据弱、不可操作，光 novelty 不够","verdict":"drop","value_path":"none","uncertainty":"none","confidence":0.82,"scores":{"importance":2,"novelty":3,"credibility":1,"substance":2,"actionability":1},"ai_relevant":"yes","spam":1}
```

## 九、注意
- **先判断后打分**：维度分是判断完之后对内容的描述，不要倒过来让分数决定 verdict。
- **高维度分 ≠ featured**（见 Postgres）；**标题党 ≠ drop**（见第 3 例）。
- 缺字段忽略，不臆造；只输出规定 JSON。
