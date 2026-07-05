// Honest QA v2: real selectors, real waits, no false positives.
// - Wait for /api/feed/events response on pill click (not poll DOM)
// - Use data-testid="highlights-pill-bar" for actual pills
// - Block Twitter image URLs (they're a separate problem, not BF-0515 scope)
// - Compare cold vs warm visually

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = 'http://127.0.0.1:3876';
const OUT = '/tmp/perf_qa_v2';
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowMs = () => Date.now();

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // BLOCK Twitter image proxy (it's a separate ~80s problem we're not measuring here)
  await page.route(/\/api\/media\/twitter-poster\//, route => route.abort());
  await page.route(/\/api\/media\/twitter-mp4\//, route => route.abort());

  const network = [];
  page.on('request', r => {
    if (r.url().includes('/api/')) {
      network.push({ url: r.url().replace(BASE, ''), start: nowMs(), method: r.method() });
    }
  });
  page.on('response', async r => {
    if (!r.url().includes('/api/')) return;
    const e = network.find(x => BASE + x.url === r.url() && !x.end);
    if (!e) return;
    e.end = nowMs(); e.status = r.status();
  });

  const events = [];
  const sessionStart = nowMs();
  const log = (label, extra = {}) => events.push({ ms_since_start: nowMs() - sessionStart, label, ...extra });
  log('session_start');

  // ─── Test A: 频道页 ────────────────────────────────────────
  log('A_navigate_channels');
  const tA = nowMs();

  // Wait for /api/feed/platforms response specifically
  const apiP = page.waitForResponse(r => r.url().includes('/api/feed/platforms'), { timeout: 30000 }).catch(() => null);

  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 });
  log('A_load_event', { ms: nowMs() - tA });

  const apiResp = await apiP;
  log('A_api_platforms_responded', { ms: nowMs() - tA, status: apiResp?.status() });

  // wait for cards with REAL author/title content (not skeleton)
  const realCardA = await page.evaluate(() => {
    return new Promise(resolve => {
      const start = performance.now();
      const check = () => {
        const cards = document.querySelectorAll('article, [data-item-id]');
        for (const c of cards) {
          const text = (c.textContent || '').trim();
          if (text.length > 80) {  // require substantial content
            resolve({ ms: Math.round(performance.now() - start), sample: text.slice(0, 80) });
            return;
          }
        }
        if (performance.now() - start > 30000) {
          resolve({ ms: Math.round(performance.now() - start), timeout: true });
          return;
        }
        requestAnimationFrame(check);
      };
      check();
    });
  });
  log('A_first_real_card', realCardA);
  await page.screenshot({ path: path.join(OUT, '01_channels.png') }).catch(() => {});

  // ─── Test B: 切到 highlights (精选) ──────────────────────────
  await sleep(1500);
  log('B_switch_to_highlights');
  const tB = nowMs();
  const apiE = page.waitForResponse(r => r.url().includes('/api/feed/events'), { timeout: 30000 }).catch(() => null);
  await page.evaluate(() => { window.location.hash = 'v=highlights'; });
  const apiERespB = await apiE;
  log('B_events_api_responded', { ms: nowMs() - tB, status: apiERespB?.status() });

  // wait for pill bar to mount
  await page.waitForSelector('[data-testid="highlights-pill-bar"]', { timeout: 15000 }).catch(() => {});
  log('B_pill_bar_mounted', { ms: nowMs() - tB });
  await page.screenshot({ path: path.join(OUT, '02_highlights.png') }).catch(() => {});

  // List actual pills
  const pillTexts = await page.$$eval('[data-testid="highlights-pill-bar"] button', btns =>
    btns.map(b => b.textContent.trim())
  );
  log('B_pill_list', { pills: pillTexts });

  // ─── Test C: pill click → 等真 /api/feed/events?categories= ───
  for (const pillText of pillTexts.slice(1, 5)) {  // skip first pill (全部)
    await sleep(1200);
    log(`C_click_${pillText}_attempt`);
    const tC = nowMs();
    // wait for the categorized events API response
    const apiPill = page.waitForResponse(
      r => r.url().includes('/api/feed/events') && r.url().includes('categories='),
      { timeout: 20000 }
    ).catch(() => null);

    await page.locator(`[data-testid="highlights-pill-bar"] button:has-text("${pillText}")`).first().click().catch(e => {
      log(`C_click_${pillText}_click_error`, { error: String(e).slice(0, 100) });
    });

    const apiResp = await apiPill;
    if (!apiResp) {
      log(`C_click_${pillText}_NO_API_CALL`, { wall_ms: nowMs() - tC });
      continue;
    }
    log(`C_click_${pillText}_api_responded`, {
      api_ms: nowMs() - tC,
      status: apiResp.status(),
      url: apiResp.url().replace(BASE, ''),
    });

    // wait for content to actually update in the DOM
    await sleep(200);
    const newContent = await page.evaluate(() => {
      const cards = document.querySelectorAll('article, [data-item-id]');
      const titles = [];
      for (const c of cards) {
        const t = (c.textContent || '').slice(0, 80).trim();
        if (t) titles.push(t);
        if (titles.length >= 2) break;
      }
      return titles;
    });
    log(`C_click_${pillText}_dom_update`, { wall_ms: nowMs() - tC, titles_sample: newContent });
    await page.screenshot({ path: path.join(OUT, `03_pill_${pillText}.png`) }).catch(() => {});
  }

  // ─── Test D: 切回精选首页（应该 cache 命中） ─────────────────
  await sleep(1000);
  log('D_back_to_highlights_root');
  const tD = nowMs();
  await page.locator('[data-testid="highlights-pill-bar"] button').first().click().catch(() => {}); // 全部
  await sleep(500);
  log('D_done', { ms: nowMs() - tD });

  await ctx.close();
  fs.writeFileSync(path.join(OUT, 'timeline.json'), JSON.stringify({ events, network }, null, 2));

  // ─── Print honest summary ────────────────────────────────────
  console.log('\n========= HONEST QA v2 (Twitter images blocked) =========\n');
  console.log(`Target: ${BASE}\n`);

  const A_load = events.find(e => e.label === 'A_load_event');
  const A_api = events.find(e => e.label === 'A_api_platforms_responded');
  const A_card = events.find(e => e.label === 'A_first_real_card');
  console.log(`[A] 频道页冷加载:`);
  console.log(`    DOM load:                  ${A_load?.ms}ms`);
  console.log(`    /api/feed/platforms 响应:  ${A_api?.ms}ms (HTTP ${A_api?.status})`);
  console.log(`    第一张真卡片渲染:           ${A_card?.ms}ms`);
  console.log(`    卡片首字内容:               "${A_card?.sample?.slice(0, 60) || 'N/A'}"`);

  const B_api = events.find(e => e.label === 'B_events_api_responded');
  const B_pills = events.find(e => e.label === 'B_pill_list');
  console.log(`\n[B] 切到精选:`);
  console.log(`    /api/feed/events 响应:     ${B_api?.ms}ms (HTTP ${B_api?.status})`);
  console.log(`    Pills 找到:                ${B_pills?.pills?.join(', ')}`);

  console.log(`\n[C] Pill 切换 (api 响应时间 → DOM 更新时间):`);
  for (const ev of events.filter(e => e.label.startsWith('C_click_') && e.label.endsWith('_api_responded'))) {
    const cat = ev.label.replace('C_click_', '').replace('_api_responded', '');
    const dom = events.find(e => e.label === `C_click_${cat}_dom_update`);
    console.log(`    ${cat}:  api ${ev.api_ms}ms → DOM ${dom?.wall_ms}ms`);
    if (dom?.titles_sample?.length) console.log(`            new title: "${dom.titles_sample[0].slice(0, 60)}"`);
  }
  for (const ev of events.filter(e => e.label.endsWith('_NO_API_CALL'))) {
    console.log(`    ${ev.label}:  ⚠️ 没触发 API 调用`);
  }

  console.log(`\n[D] 切回 全部 :  ${events.find(e => e.label === 'D_done')?.ms}ms`);

  console.log(`\n=== Network: top 10 slow API ===`);
  const apis = network.filter(n => n.end).sort((a, b) => (b.end - b.start) - (a.end - a.start));
  for (const a of apis.slice(0, 10)) {
    console.log(`    ${String(a.end - a.start).padStart(6)}ms ${a.status} ${a.url.slice(0, 100)}`);
  }

  console.log(`\nfiles: ${OUT}/timeline.json + screenshots`);
  await browser.close();
}

main().catch(e => { console.error(e); process.exit(1); });
