# scripts/perf/ — 性能验证 / 负载测试脚本

BF-0515 系列性能加固期间产出的回归脚本。后续要做性能改动时复用，验证本机或 worktree 上的 backend 行为。

## 脚本清单

| 脚本 | 用途 |
|---|---|
| `load_gate.py` | production-safe 公开只读压测门禁；支持 base URL、profile、爬坡档位、hard stop 和 JSON/Markdown 证据输出 |
| `load_test.sh` | 历史 bash 并发负载测试；写死本地端口，仅作 BF-0515 旧基线参考，不直接用于 production |
| `perf_trace.mjs` | Playwright 跑用户路径，捕获 API timing + console errors |
| `perf_trace_bf0515_1.mjs` | T1-T5 验收 (cold load / warm refresh / pill switch / login / concurrent) |
| `perf_trace_bf0515_full.mjs` | 全栈验收（含 ETag 后） |
| `perf_trace_real_user.mjs` | 真用户视角（视频录制 + 帧截图 + 网络瀑布） |
| `perf_trace_v2.mjs` | 用 data-testid 找真 pill，抓真实 DOM 更新时机 |
| `perf_refresh_diag.mjs` | 软刷新 vs 冷加载对比，验 ETag 304 是否生效 |
| `perf_image_diag.mjs` | 图片加载诊断（哪些图 cert error / 哪些走 backend proxy） |
| `perf_channels_image_diag.mjs` | 频道页媒体请求耗时分布 |
| `perf_channels_x_diag.mjs` | 频道 X 子标签下的 video poster + photo 实测 |
| `perf_image_diag_new.mjs` | 同 perf_image_diag.mjs，对不同 worktree 端口 |

## 用法

production 或 staging 压测优先使用 `load_gate.py`：

```bash
python3 scripts/perf/load_gate.py \
  --base-url https://info2act.com \
  --profile public-read \
  --steps 1,5,10,20,50 \
  --duration-sec 60 \
  --timeout-sec 8 \
  --allow-production
```

仅查看将要执行的公开只读 profile，不发真实请求：

```bash
python3 scripts/perf/load_gate.py --base-url https://info2act.com --dry-run
```

`load_gate.py` 默认把证据写入 `docs/qa/load-test/<timestamp>-<profile>/`，包含 `result.json` 和 `report.md`。production 域名必须显式传 `--allow-production` 才会发真实请求，避免误操作。

其他旧 Playwright 脚本顶部通常写死了 BASE URL（默认 `http://127.0.0.1:3567` 或某个 worktree 端口）。换 worktree 或 ECS 验证时 sed 改一下 BASE 再跑：

```bash
sed -i.bak 's|http://127.0.0.1:3567|http://your-target|' scripts/perf/perf_trace.mjs
node scripts/perf/perf_trace.mjs
```

输出默认在 `/tmp/perf_qa_*/`（每个脚本不同子目录）。

## 依赖

`playwright` 已在 `package.json`。运行前确保 `npm install` 跑过。
