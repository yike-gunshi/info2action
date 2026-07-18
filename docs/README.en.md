<div align="center">

# Info2Action

**Information should end in action.**

Cross-platform aggregation · Cross-source event dedup · AI curation · Info→Action loop

[中文（完整文档）](../README.md) · **English**

<br>

<img src="assets/readme/highlights.png" alt="Info2Action highlights — cross-source event timeline" width="880">

<sub>Highlights: reports of the same AI event from multiple platforms are automatically clustered into one timeline entry with an AI-written digest</sub>

</div>

---

Checking Twitter, WeChat, Reddit, Discord, RSS and YouTube every day means reading the same AI news 5–10 times across places — you fear missing out *and* you're exhausted by duplicates. That's not "too much information"; it's **FOMO + duplicate-reading fatigue**.

**Info2Action is a self-hostable, AI-driven personal information engine**: it aggregates your sources into one place, pre-reads, categorizes, scores and deduplicates them by *event*, then translates the signals worth acting on into executable actions — "open this link / write this reply / dispatch this task".

> It started as a morning-reading tool I built for myself — tired of fishing the same story out of a dozen apps, I let AI read the world first. It grew event clustering, curation and action dispatch; open-sourcing it means you can own a feed that belongs to you.

**For developers**: a complete full-stack pipeline — multi-source ingestion → LLM enrichment → embedding recall + LLM-judged clustering → curation scoring → action dispatch. FastAPI + React. Runs on a single machine with SQLite; scales to multi-user production on Supabase. **All prompts are open and editable** ([`prompts/`](../prompts/)).

## ✨ Features

- **Unified ingestion** — X/Twitter, Reddit, Hacker News, RSS, Bilibili, WeChat Official Accounts, GitHub (trending/releases/repo tracking), YouTube, Xiaohongshu, manual submit; runs on cron
- **AI enrichment** — one LLM pass produces summary, key points, category, content type, quality score and keywords
- **Cross-source event clustering** — embedding recall + LLM verdict on "same event"; event-level digests regenerated from original sources; fully auditable
- **AI curation** — per-item LLM verdicts (featured / borderline / drop) aggregated per cluster into a "what you should see now" timeline
- **Personalized ranking** — onboarding profile (role / interests / tools), pure-computation `quality × engagement × freshness × match` scoring
- **Info→Action loop** — signals worth acting on become executable action cards, dispatched to Discord Forum or opened in browser
- **Video understanding** — subtitles first, Doubao ASR fallback, segment-level bilingual captions
- **Multi-user** — open browsing without login; invite-based registration with email verification unlocks personalization, stars, history and actions
- **Dual storage** — local SQLite for single-machine use; Supabase Postgres + Storage (pgvector recall, remote read models) for multi-user production
- **Admin console** — curation-funnel panorama (per-cluster ledger of every item's score, veto reason and display verdict across the ingest→score→cluster→gate→display pipeline, with one-click manual override that feeds a golden replay set) plus per-run fetch reconciliation (ingest / AI-enrich / publish counts, stage timings, per-source drill-down)

## 🚀 Quick Start

Minimal setup: **one LLM API key + default RSS sources**, ~5 minutes to see the core loop.

Prerequisites: Python 3.11+, Node.js 20+, one LLM API key (MiniMax by default; Doubao / OpenAI / DeepSeek also supported).

```bash
git clone https://github.com/yike-gunshi/info2action.git
cd info2action

cp .env.example .env          # then set at least:
                              #   MINIMAX_API_KEY=xxx
                              #   APP_BASE_URL=http://localhost:3567

uv pip install -r requirements.txt
cd frontend-react && npm install && cd ..

npm run dev                   # starts backend :8080 + frontend :3567
open http://127.0.0.1:3567

bash ops/fetch_all.sh         # first fetch: RSS + AI enrichment (~2-5 min)
```

Full self-hosting (Twitter / WeChat / Discord / ASR / server deployment): see [SELF-HOST.md](SELF-HOST.md) (Chinese).

## 📄 License

[MIT](../LICENSE) © 2025-2026 yike-gunshi

> The product UI and most docs are currently in Chinese; i18n is on the roadmap. Questions and PRs in English are welcome in Issues / Discussions.
