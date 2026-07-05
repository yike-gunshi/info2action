# Self-hosting Info2Action

> 你已经跑通了 [README Quick Start](../README.md#-quick-start)（最小集 = 1 把 LLM key + RSS）？这份文档帮你把项目升级到「完整能力」并部署到自己的服务器。
>
> 凭证字段速查 → [docs/配置指南.md](配置指南.md)
> ECS 部署细节 → [docs/DEPLOY.md](DEPLOY.md)

## 1. 选择你的部署模式

按你想用什么能力，决定要配哪些凭证：

| 模式 | 你能用什么 | 你需要什么 | 推荐场景 |
|---|---|---|---|
| **Lite**（README 默认） | RSS / HN / GitHub Trending / 手动提交 + AI 摘要 + 个性化 | 1 把 LLM key | 5 分钟试用，看核心闭环 |
| **Plus** | Lite + Twitter + Reddit + B 站 + 用户登录 | + Twitter 私有 API headers + Resend | 个人日常用 |
| **Full** | Plus + 微信公众号 + 视频 ASR + Discord 行动派发 | + 语鲸 token + 豆包 ASR + OSS + Discord bot | 完整体验，等同生产环境 |

**建议路径**：先 Lite 跑通（已经在 README 完成）→ 想加什么再升级到 Plus / Full，按需配凭证。

## 2. Lite → Plus

### 2.1 加 Twitter 抓取

Twitter 用的是私有 API headers（不是官方 API）。**这部分依赖外网**，你需要：

- 一台能直连 Twitter 的服务器（或本地 + 代理）
- 一组从浏览器抓出来的 headers（`Authorization` / `x-csrf-token` / `cookie`）

获取方式：浏览器登录 twitter.com → DevTools Network → 抓任意一个 `https://x.com/i/api/...` 请求 → Copy as cURL → 用 [curlconverter.com](https://curlconverter.com/) 转成 Python headers dict → 填入 `.env`。

⚠️ **Twitter token 大约 1-2 周失效一次**。Headers 失效时所有请求会返回 401/10010，重新抓一遍即可。详见 [docs/配置指南.md §4](配置指南.md)。

### 2.2 加 Reddit / B 站

| 平台 | .env 字段 | 取值 |
|---|---|---|
| Reddit | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) 创建 script 类型 app |
| B 站 | `BILIBILI_SESSDATA` | 浏览器登录 → DevTools Application → Cookies → 复制 `SESSDATA` |

### 2.3 启用用户登录 + 邮箱验证

- **JWT_SECRET / ENCRYPTION_KEY**：`openssl rand -hex 32` 各生成一个
- **Resend**：[resend.com](https://resend.com) 注册 → 验证你的发件域名 → 创建 API key 填 `RESEND_API_KEY`，发件地址填 `RESEND_FROM_EMAIL`

完成后启动服务，注册流程会发出真实验证码邮件。

## 3. Plus → Full

### 3.1 微信公众号（语鲸）

通过 [语鲸](https://lingowhale.com) 第三方聚合服务抓取。需要语鲸账号 + auth token。详见 [docs/配置指南.md §5](配置指南.md)。

### 3.2 视频 ASR（YouTube + Twitter）

字幕优先 + 豆包 ASR 兜底，YouTube 字幕命中率 ~85% 走免费路径，剩下走 ASR：

```
.env 必填：
  DOUBAO_ASR_API_KEY        # 火山引擎控制台 → 智能语音 → seedasr
  ALIYUN_OSS_ACCESS_KEY_ID  # 阿里云 OSS（视频中转，1 天自动清）
  ALIYUN_OSS_ACCESS_KEY_SECRET
  ALIYUN_OSS_BUCKET         # 推荐 cn-beijing 区，带 1 天清理生命周期规则
```

**成本**：约 ¥300/月（Twitter + YouTube 混合，硬上限 10 小时/天 ≈ ¥18/天）。详见 [docs/配置指南.md §3](配置指南.md)。

### 3.3 Discord 行动派发

Info2Action 的「行动建议」可以派发到你私有 Discord 服务器的 Forum 频道，方便归类追踪：

```
.env 必填：
  DISCORD_BOT_TOKEN         # https://discord.com/developers → Bot
  DISCORD_FORUM_CHANNEL_ID  # 你的 Forum channel ID
  DISCORD_GUILD_ID          # 你的服务器 ID
```

**bot 权限**：`Send Messages` + `Create Public Threads` + `Manage Threads`。

### 3.4 外网代理（如果你在国内服务器）

部分源（Discord / Twitter / Reddit / GitHub）需要代理。Info2Action 默认走 `127.0.0.1:7890`（Clash 兼容）。

```
.env 可选：
  HTTP_PROXY=http://127.0.0.1:7890
  HTTPS_PROXY=http://127.0.0.1:7890
```

不需要代理就留空。

## 4. 部署到服务器

详细 ECS 部署、systemd unit、git pull 部署流程见 [docs/DEPLOY.md](DEPLOY.md)。

简要路径：

```bash
# 服务器侧
git clone https://github.com/yike-gunshi/info2action.git /opt/info2action
cd /opt/info2action
cp .env.example .env && vim .env       # 填凭证
uv pip install -r requirements.txt
cd frontend-react && npm install && npm run build && cd ..

# systemd
sudo cp ops/info-feed.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now info-feed
sudo systemctl status info-feed

# cron（自动抓取）
crontab -e
# 加：*/30 * * * * cd /opt/info2action && bash ops/fetch_all.sh >> logs/fetch.log 2>&1
```

**前端 build 在服务器跑**（不在本地），`frontend-react/dist/` 在本地 git-ignored。

**反向代理** 推荐 Caddy（自动 HTTPS）：

```caddy
your.domain.com {
  reverse_proxy 127.0.0.1:8080
}
```

## 5. 常见问题

### Q: 启动后 `/api/feed` 返回空？

A: 跑一次 `bash ops/fetch_all.sh`。第一次没数据是正常的，cron 还没跑。

### Q: AI 摘要为空 / 报错？

A: 检查：
- `.env` 里 LLM key 是否填了
- 启动 uvicorn 前是否 `set -a; source .env; set +a`（项目代码不用 `python-dotenv`，必须手动 source）
- 看后端日志 `journalctl -u info-feed -f`（systemd 部署）或终端输出

### Q: Twitter 突然一片空白？

A: 大概率 token 失效（1-2 周一次）。重抓 headers 填 `.env`，重启服务。

### Q: 想加新数据源？

A: 看 `src/fetch_*.py` 的现成 fetcher 当模板（推荐 `fetch_rss.py`，最简单），新平台的 fetcher 写在 `src/`，注册到 `ops/fetch_all.sh`，prompt 不需要改（`enrich_items` 是平台无关的）。

### Q: 想换 LLM provider？

A: `src/clustering/embedding_provider.py` 已抽象 provider 基类，加新 provider 实现 `EmbeddingProvider` ABC 即可。LLM 调用层在 `src/llm_client.py`，类似改法。

## 6. 下一步

- 改 prompt 调 AI 行为 → `prompts/*.md`，每个文件都有自己的 README 说明
- 调阈值/排序权重 → `config/config.json` + `src/routes/feed.py` 的 ranking 公式
- 看每个模块怎么实现的 → [docs/产品实现速查.md](产品实现速查.md)（PM cheat sheet）
- 调整 UX → [docs/DESIGN.md](DESIGN.md)
- 想贡献代码 → 看 [README #Contributing](../README.md#-contributing)
