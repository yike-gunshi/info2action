// BF-0515 full-stack verification (A0+A1+A2+A3 combined).
// Targets: A3 worktree frontend at 3876, backend at 8476.
// Compares against earlier BF-0515-1-only baseline (perf_trace_bf0515_1.mjs results).

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = 'http://127.0.0.1:3876';
const OUT = '/tmp/perf_qa_bf0515_full';
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowMs = () => Date.now();

function attachNet(page, log) {
  page.on('request', r => log.push({ url: r.url(), start: nowMs(), method: r.method(), type: r.resourceType() }));
  page.on('response', async r => {
    const e = log.find(x => x.url === r.url() && !x.end);
    if (!e) return;
    e.end = nowMs(); e.status = r.status();
    try { e.bytes = (await r.body()).length; } catch { e.bytes = 0; }
  });
}
function summarizeApi(log) {
  const apis = log.filter(e => e.end && e.url.includes('/api/'))
    .map(e => ({ url: e.url.replace(BASE, ''), status: e.status, ms: e.end - e.start, method: e.method }));
  apis.sort((a, b) => b.ms - a.ms);
  return {
    count: apis.length,
    sum_ms: apis.reduce((s, e) => s + e.ms, 0),
    failed: apis.filter(e => e.status < 200 || e.status >= 400).length,
    top: apis.slice(0, 8),
  };
}
async function captureMetrics(page) {
  return await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0] || {};
    const paints = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, Math.round(p.startTime)]));
    const lcp = performance.getEntriesByType('largest-contentful-paint').slice(-1)[0]?.startTime;
    return {
      domContentLoaded: Math.round((nav.domContentLoadedEventEnd || 0) - (nav.startTime || 0)),
      fcp: paints['first-contentful-paint'] || null,
      lcp: lcp ? Math.round(lcp) : null,
    };
  }).catch(() => ({}));
}
async function waitForCard(page, timeoutMs = 30000) {
  const start = nowMs();
  try {
    await page.waitForFunction(() => {
      const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
      for (const c of cards) {
        const t = (c.textContent || '').trim();
        if (t.length > 30) return true;
      }
      return false;
    }, { timeout: timeoutMs });
    return { ok: true, ms: nowMs() - start };
  } catch {
    return { ok: false, ms: nowMs() - start };
  }
}

async function T1_coldChannels(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const net = []; attachNet(page, net);
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text().slice(0, 150)); });
  const t0 = nowMs();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 }).catch(() => {});
  const card = await waitForCard(page);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  const wall = nowMs() - t0;
  const m = await captureMetrics(page);
  await page.screenshot({ path: path.join(OUT, 'T1_channels.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return { test: 'T1_cold_channels', wall_ms: wall, time_to_card_ms: card.ms, card_appeared: card.ok, fcp: m.fcp, lcp: m.lcp, api: summarizeApi(net), console_errs_count: errs.length };
}

async function T2_warmChannels(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await waitForCard(page);
  await sleep(800);
  const net = []; attachNet(page, net);
  const t0 = nowMs();
  await page.reload({ waitUntil: 'load' }).catch(() => {});
  const card = await waitForCard(page);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  const wall = nowMs() - t0;
  await page.screenshot({ path: path.join(OUT, 'T2_warm.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return { test: 'T2_warm_channels_refresh', wall_ms: wall, time_to_card_ms: card.ms, card_appeared: card.ok, api: summarizeApi(net) };
}

async function T3_pillSwitching(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/#v=highlights`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await waitForCard(page);
  await sleep(500);

  // Try to find and click pills (categories like products, tools, coding)
  const transitions = [];
  for (const cat of ['产品', '工具', 'Coding', '模型', 'Skill', '行业']) {
    const net = []; attachNet(page, net);
    const t0 = nowMs();
    const pill = page.locator(`button:has-text("${cat}"), [role="tab"]:has-text("${cat}")`).first();
    const count = await pill.count();
    if (count) {
      try { await pill.click({ timeout: 2000 }); }
      catch { transitions.push({ to: cat, error: 'click failed' }); continue; }
      await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
      const wall = nowMs() - t0;
      transitions.push({ to: cat, wall_ms: wall, api: summarizeApi(net) });
    } else {
      transitions.push({ to: cat, missing: true });
    }
    await sleep(400);
  }
  await page.screenshot({ path: path.join(OUT, 'T3_pills.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return { test: 'T3_pill_switching', transitions };
}

async function T4_login(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const net = []; attachNet(page, net);
  const t0 = nowMs();
  await page.goto(`${BASE}/#login`, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {});
  const renderMs = nowMs() - t0;
  const emailInput = page.locator('input[type="text"], input[type="email"], input[name="login"]').first();
  const pwInput = page.locator('input[type="password"]').first();
  const btn = page.locator('button:has-text("登录"), button:has-text("Login")').first();
  let attempt = null;
  if (await emailInput.count() && await pwInput.count() && await btn.count()) {
    await emailInput.fill('verify-bf-0515-full@info2action.test').catch(() => {});
    await pwInput.fill('wrong-pwd-test').catch(() => {});
    const t1 = nowMs();
    await btn.click().catch(() => {});
    await page.waitForResponse(r => r.url().includes('/api/auth/login'), { timeout: 15000 }).catch(() => {});
    attempt = { ms: nowMs() - t1, api: summarizeApi(net.filter(e => e.url.includes('/api/auth/login'))) };
  }
  await page.screenshot({ path: path.join(OUT, 'T4_login.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return { test: 'T4_login_smoke', login_page_ms: renderMs, wrong_pwd_attempt: attempt };
}

async function T5_concurrentTabs(browser, n = 10) {
  const tasks = [];
  for (let i = 0; i < n; i++) {
    tasks.push((async () => {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      const net = []; attachNet(page, net);
      let crashed = false;
      page.on('pageerror', () => { crashed = true; });
      const t0 = nowMs();
      try {
        await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 });
        const card = await waitForCard(page, 30000);
        await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
        const apiSum = summarizeApi(net);
        await ctx.close();
        return { tab: i + 1, wall_ms: nowMs() - t0, time_to_card_ms: card.ms, card_appeared: card.ok, api_failed: apiSum.failed, crashed };
      } catch (e) {
        await ctx.close();
        return { tab: i + 1, error: String(e), crashed: true };
      }
    })());
  }
  const results = await Promise.all(tasks);
  const success = results.filter(r => r.card_appeared && !r.crashed && !r.error).length;
  const walls = results.filter(r => r.wall_ms).map(r => r.wall_ms).sort((a, b) => a - b);
  const cards = results.filter(r => r.time_to_card_ms).map(r => r.time_to_card_ms).sort((a, b) => a - b);
  const p = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))] || null;
  return {
    test: 'T5_concurrent_tabs', n,
    success_count: success, success_rate: `${success}/${n}`,
    wall_ms: { min: walls[0], p50: p(walls, 0.5), p95: p(walls, 0.95), max: walls[walls.length - 1] },
    time_to_card_ms: { min: cards[0], p50: p(cards, 0.5), p95: p(cards, 0.95), max: cards[cards.length - 1] },
  };
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const results = { started: new Date().toISOString(), target: BASE };
  console.log('--- T1: cold channels ---');
  results.T1 = await T1_coldChannels(browser);
  console.log('--- T2: warm refresh ---');
  results.T2 = await T2_warmChannels(browser);
  console.log('--- T3: pill switching ---');
  results.T3 = await T3_pillSwitching(browser);
  console.log('--- T4: login smoke ---');
  results.T4 = await T4_login(browser);
  console.log('--- T5: 10 concurrent tabs ---');
  results.T5 = await T5_concurrentTabs(browser, 10);

  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(results, null, 2));

  console.log('\n========= BF-0515 FULL-STACK VERIFICATION =========\n');
  console.log(`Target: ${BASE} (worktree A3 = A0+A1+A2+A3 combined)\n`);

  const c = (n) => `\x1b[1m${n}\x1b[0m`;

  console.log(`【T1 冷加载频道】wall ${c(results.T1.wall_ms + 'ms')}, time-to-card ${c(results.T1.time_to_card_ms + 'ms')}, card OK=${results.T1.card_appeared}`);
  console.log(`  FCP ${results.T1.fcp}ms / LCP ${results.T1.lcp}ms / API: ${results.T1.api.count} calls / failed ${results.T1.api.failed}`);
  console.log(`  Top API:`);
  for (const a of results.T1.api.top.slice(0, 5)) console.log(`    ${String(a.ms).padStart(5)}ms ${a.status} ${a.url.slice(0, 80)}`);
  if (results.T1.console_errs_count) console.log(`  console errs: ${results.T1.console_errs_count}`);
  console.log();

  console.log(`【T2 暖刷新】wall ${c(results.T2.wall_ms + 'ms')}, time-to-card ${c(results.T2.time_to_card_ms + 'ms')}`);
  console.log(`  Top API:`);
  for (const a of results.T2.api.top.slice(0, 3)) console.log(`    ${String(a.ms).padStart(5)}ms ${a.status} ${a.url.slice(0, 80)}`);
  console.log();

  console.log(`【T3 Pill 切换】`);
  for (const t of results.T3.transitions) {
    if (t.error) console.log(`  → ${t.to}: ERROR ${t.error}`);
    else if (t.missing) console.log(`  → ${t.to}: pill not found`);
    else console.log(`  → ${t.to}: wall ${c(t.wall_ms + 'ms')} (API ${t.api.count}/${t.api.sum_ms}ms)`);
  }
  console.log();

  console.log(`【T4 登录】page render ${c(results.T4.login_page_ms + 'ms')}`);
  if (results.T4.wrong_pwd_attempt) {
    console.log(`  wrong-pwd: ${c(results.T4.wrong_pwd_attempt.ms + 'ms')}, top API:`);
    for (const a of results.T4.wrong_pwd_attempt.api.top) console.log(`    ${a.ms}ms ${a.status} ${a.url}`);
  }
  console.log();

  console.log(`【T5 10 并发 tabs】`);
  console.log(`  ✅ 成功: ${c(results.T5.success_rate)}`);
  console.log(`  wall: min=${results.T5.wall_ms.min}ms p50=${results.T5.wall_ms.p50}ms p95=${results.T5.wall_ms.p95}ms max=${results.T5.wall_ms.max}ms`);
  console.log(`  card: min=${results.T5.time_to_card_ms.min}ms p50=${results.T5.time_to_card_ms.p50}ms max=${results.T5.time_to_card_ms.max}ms`);
  console.log();

  console.log(`Report: ${OUT}/report.json`);
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
