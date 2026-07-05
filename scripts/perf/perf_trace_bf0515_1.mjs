// BF-0515-1 verification: full Playwright QA against worktree 3742
// (transaction pooler) vs documented baseline (session pooler).
//
// Tests:
//   T1  cold load /#v=channels         — FCP, LCP, time to first card visible, API timings
//   T2  warm load /#v=channels         — refresh in same context, measure improvement
//   T3  tab switching highlights<->channels<->recommend — transition latency
//   T4  login UI smoke + wrong-pwd round-trip
//   T5  10 concurrent contexts (simulating multi-user) — success rate + p99
//
// Outputs: /tmp/perf_qa_bf0515_1/ — JSON report + screenshots + summary

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = 'http://127.0.0.1:3742';
const OUT = '/tmp/perf_qa_bf0515_1';
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- helpers ----

function nowMs() { return Date.now(); }

async function captureMetrics(page) {
  return await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0] || {};
    const paints = Object.fromEntries(
      performance.getEntriesByType('paint').map(p => [p.name, Math.round(p.startTime)])
    );
    const lcp = performance.getEntriesByType('largest-contentful-paint').slice(-1)[0]?.startTime;
    return {
      domContentLoaded: Math.round((nav.domContentLoadedEventEnd || 0) - (nav.startTime || 0)),
      loadEvent: Math.round((nav.loadEventEnd || 0) - (nav.startTime || 0)),
      fp: paints['first-paint'] || null,
      fcp: paints['first-contentful-paint'] || null,
      lcp: lcp ? Math.round(lcp) : null,
    };
  }).catch(() => ({}));
}

function attachNetwork(page, log) {
  page.on('request', r => log.push({ url: r.url(), start: nowMs(), type: r.resourceType(), method: r.method() }));
  page.on('response', async r => {
    const entry = log.find(e => e.url === r.url() && !e.end);
    if (!entry) return;
    entry.end = nowMs();
    entry.status = r.status();
    try { entry.bytes = (await r.body()).length; } catch { entry.bytes = 0; }
  });
  page.on('requestfailed', r => {
    const entry = log.find(e => e.url === r.url() && !e.end);
    if (entry) {
      entry.end = nowMs();
      entry.status = -1;
      entry.failure = r.failure()?.errorText;
    }
  });
}

function summarizeApi(log, pathPrefix = '/api/') {
  const apis = log
    .filter(e => e.end && e.url.includes(pathPrefix))
    .map(e => ({
      url: e.url.replace(BASE, ''),
      status: e.status,
      ms: e.end - e.start,
      method: e.method,
    }));
  apis.sort((a, b) => b.ms - a.ms);
  return {
    count: apis.length,
    sum_ms: apis.reduce((s, e) => s + e.ms, 0),
    failed: apis.filter(e => e.status < 200 || e.status >= 400).length,
    top: apis.slice(0, 8),
  };
}

async function waitForCardOrTimeout(page, timeoutMs = 30000) {
  // Card appears as <article> / [data-item-id] / class infused. Use simplest reliable selector.
  // A skeleton box has no actual text; a real card has visible title text.
  const start = nowMs();
  try {
    await page.waitForFunction(() => {
      const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
      // require at least 1 card with non-empty visible text
      for (const c of cards) {
        const text = (c.textContent || '').trim();
        if (text.length > 30) return true;  // some real content
      }
      return false;
    }, { timeout: timeoutMs });
    return { ok: true, ms: nowMs() - start };
  } catch {
    return { ok: false, ms: nowMs() - start, reason: 'no card after timeout' };
  }
}

// ---- tests ----

async function T1_coldLoad(browser) {
  console.log('--- T1: cold load /#v=channels ---');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const net = [];
  attachNetwork(page, net);
  const consoleErrs = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrs.push(m.text().slice(0, 200)); });
  const t0 = nowMs();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 }).catch(() => {});
  const cardWait = await waitForCardOrTimeout(page);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  const wallMs = nowMs() - t0;
  const metrics = await captureMetrics(page);
  await page.screenshot({ path: path.join(OUT, 'T1_cold_channels.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return {
    test: 'T1_cold_channels',
    wall_ms: wallMs,
    time_to_first_card_ms: cardWait.ms,
    card_appeared: cardWait.ok,
    metrics,
    api: summarizeApi(net),
    console_errors: consoleErrs,
  };
}

async function T2_warmLoad(browser) {
  console.log('--- T2: warm load (re-open same channels) ---');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  // first hit (warmup)
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await waitForCardOrTimeout(page);
  await sleep(500);
  // measured: refresh
  const net = [];
  attachNetwork(page, net);
  const t0 = nowMs();
  await page.reload({ waitUntil: 'load' }).catch(() => {});
  const cardWait = await waitForCardOrTimeout(page);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  const wallMs = nowMs() - t0;
  await page.screenshot({ path: path.join(OUT, 'T2_warm_channels.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return {
    test: 'T2_warm_channels_refresh',
    wall_ms: wallMs,
    time_to_first_card_ms: cardWait.ms,
    card_appeared: cardWait.ok,
    api: summarizeApi(net),
  };
}

async function T3_tabSwitching(browser) {
  console.log('--- T3: tab switching highlights<->channels<->recommend ---');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/#v=highlights`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await waitForCardOrTimeout(page);
  await sleep(500);

  const transitions = [];
  for (const target of ['channels', 'recommend', 'highlights', 'channels']) {
    const net = [];
    attachNetwork(page, net);
    const t0 = nowMs();
    await page.evaluate((t) => { window.location.hash = `v=${t}`; }, target);
    // wait for either: new card appears OR networkidle
    const cardWait = await waitForCardOrTimeout(page, 15000);
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    const wallMs = nowMs() - t0;
    transitions.push({
      to: target,
      wall_ms: wallMs,
      time_to_first_card_ms: cardWait.ms,
      card_appeared: cardWait.ok,
      api: summarizeApi(net),
    });
    await sleep(300);
  }
  await ctx.close();
  return { test: 'T3_tab_switching', transitions };
}

async function T4_loginSmoke(browser) {
  console.log('--- T4: login page render + wrong-pwd round-trip ---');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const net = [];
  attachNetwork(page, net);
  const t0 = nowMs();
  await page.goto(`${BASE}/#login`, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {});
  const loginPageMs = nowMs() - t0;

  // try wrong credentials
  const emailInput = page.locator('input[type="text"], input[type="email"], input[name="login"]').first();
  const pwInput = page.locator('input[type="password"]').first();
  const loginBtn = page.locator('button:has-text("登录"), button:has-text("Login")').first();

  const hasForm = await emailInput.count() > 0 && await pwInput.count() > 0 && await loginBtn.count() > 0;

  let loginAttempt = null;
  if (hasForm) {
    await emailInput.fill('verify-bf-0515-1@info2action.test').catch(() => {});
    await pwInput.fill('wrong-pwd-bf-test').catch(() => {});
    const t1 = nowMs();
    await loginBtn.click().catch(() => {});
    // wait for error message or response
    await page.waitForResponse(r => r.url().includes('/api/auth/login'), { timeout: 15000 }).catch(() => {});
    loginAttempt = {
      ms: nowMs() - t1,
      api: summarizeApi(net.filter(e => e.url.includes('/api/auth/login'))),
    };
  }

  await page.screenshot({ path: path.join(OUT, 'T4_login.png'), fullPage: false }).catch(() => {});
  await ctx.close();
  return {
    test: 'T4_login_smoke',
    login_page_render_ms: loginPageMs,
    login_form_present: hasForm,
    wrong_pwd_attempt: loginAttempt,
  };
}

async function T5_concurrentTabs(browser, n = 10) {
  console.log(`--- T5: ${n} concurrent contexts simulating multi-user ---`);
  const tasks = [];
  for (let i = 0; i < n; i++) {
    tasks.push((async () => {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      const net = [];
      attachNetwork(page, net);
      let crashed = false;
      page.on('pageerror', () => { crashed = true; });
      const t0 = nowMs();
      try {
        await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 });
        const cardWait = await waitForCardOrTimeout(page, 30000);
        await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
        const wallMs = nowMs() - t0;
        const apiSum = summarizeApi(net);
        await ctx.close();
        return {
          tab: i + 1,
          wall_ms: wallMs,
          time_to_first_card_ms: cardWait.ms,
          card_appeared: cardWait.ok,
          api_failed: apiSum.failed,
          api_total_ms: apiSum.sum_ms,
          crashed,
        };
      } catch (e) {
        await ctx.close();
        return { tab: i + 1, error: String(e), crashed: true };
      }
    })());
  }
  const results = await Promise.all(tasks);
  const successCount = results.filter(r => r.card_appeared && !r.crashed && !r.error).length;
  const walls = results.filter(r => r.wall_ms).map(r => r.wall_ms).sort((a, b) => a - b);
  const cardWaits = results.filter(r => r.time_to_first_card_ms).map(r => r.time_to_first_card_ms).sort((a, b) => a - b);
  const p = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))] || null;
  return {
    test: 'T5_concurrent_tabs',
    n,
    success_count: successCount,
    success_rate: `${successCount}/${n}`,
    wall_ms: { min: walls[0] || null, p50: p(walls, 0.5), p95: p(walls, 0.95), max: walls[walls.length - 1] || null },
    time_to_card_ms: { min: cardWaits[0] || null, p50: p(cardWaits, 0.5), p95: p(cardWaits, 0.95), max: cardWaits[cardWaits.length - 1] || null },
    api_failures_total: results.reduce((s, r) => s + (r.api_failed || 0), 0),
    per_tab: results,
  };
}

// ---- main ----

async function main() {
  const browser = await chromium.launch({ headless: true });
  const startedAt = new Date().toISOString();

  const results = {
    started_at: startedAt,
    target: BASE,
    notes: 'BF-0515-1 worktree backend on transaction pooler :6543',
  };

  results.T1 = await T1_coldLoad(browser);
  results.T2 = await T2_warmLoad(browser);
  results.T3 = await T3_tabSwitching(browser);
  results.T4 = await T4_loginSmoke(browser);
  results.T5 = await T5_concurrentTabs(browser, 10);

  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(results, null, 2));

  console.log('\n========= BF-0515-1 VERIFICATION REPORT =========\n');
  console.log(`Target: ${BASE}\n`);

  console.log(`【T1】冷加载 /#v=channels`);
  console.log(`  wall: ${results.T1.wall_ms}ms`);
  console.log(`  time-to-first-card: ${results.T1.time_to_first_card_ms}ms (appeared: ${results.T1.card_appeared})`);
  console.log(`  FCP: ${results.T1.metrics.fcp || 'n/a'}ms  LCP: ${results.T1.metrics.lcp || 'n/a'}ms`);
  console.log(`  API: ${results.T1.api.count} calls / ${results.T1.api.sum_ms}ms total / ${results.T1.api.failed} failed`);
  for (const a of results.T1.api.top.slice(0, 5)) console.log(`    ${String(a.ms).padStart(5)}ms  ${a.status} ${a.url.slice(0, 80)}`);
  if (results.T1.console_errors.length) {
    console.log(`  console errors: ${results.T1.console_errors.length}`);
    for (const e of results.T1.console_errors.slice(0, 3)) console.log(`    ${e.slice(0, 100)}`);
  }
  console.log();

  console.log(`【T2】暖加载 (refresh /#v=channels)`);
  console.log(`  wall: ${results.T2.wall_ms}ms  time-to-first-card: ${results.T2.time_to_first_card_ms}ms`);
  console.log(`  API: ${results.T2.api.count} calls / ${results.T2.api.sum_ms}ms`);
  for (const a of results.T2.api.top.slice(0, 3)) console.log(`    ${String(a.ms).padStart(5)}ms  ${a.status} ${a.url.slice(0, 80)}`);
  console.log();

  console.log(`【T3】Tab 切换`);
  for (const t of results.T3.transitions) {
    console.log(`  → ${t.to.padEnd(10)}  wall: ${t.wall_ms}ms  card: ${t.time_to_first_card_ms}ms (${t.card_appeared ? 'OK' : 'TIMEOUT'})  API: ${t.api.count}/${t.api.sum_ms}ms`);
  }
  console.log();

  console.log(`【T4】登录页 + 错误密码`);
  console.log(`  login page render: ${results.T4.login_page_render_ms}ms`);
  console.log(`  form found: ${results.T4.login_form_present}`);
  if (results.T4.wrong_pwd_attempt) {
    console.log(`  wrong-pwd attempt: ${results.T4.wrong_pwd_attempt.ms}ms`);
    for (const a of results.T4.wrong_pwd_attempt.api.top) console.log(`    ${a.ms}ms  ${a.status} ${a.url}`);
  }
  console.log();

  console.log(`【T5】${results.T5.n} 并发 tab (多用户红线)`);
  console.log(`  ✅ 成功率: ${results.T5.success_rate}`);
  console.log(`  wall: min=${results.T5.wall_ms.min}ms p50=${results.T5.wall_ms.p50}ms p95=${results.T5.wall_ms.p95}ms max=${results.T5.wall_ms.max}ms`);
  console.log(`  card-visible: min=${results.T5.time_to_card_ms.min}ms p50=${results.T5.time_to_card_ms.p50}ms max=${results.T5.time_to_card_ms.max}ms`);
  console.log(`  API failures total: ${results.T5.api_failures_total}`);
  console.log();

  console.log(`Report: ${path.join(OUT, 'report.json')}`);
  console.log(`Screenshots: ${OUT}/T1_cold_channels.png, T2_warm_channels.png, T4_login.png`);

  await browser.close();
}

main().catch(e => { console.error(e); process.exit(1); });
