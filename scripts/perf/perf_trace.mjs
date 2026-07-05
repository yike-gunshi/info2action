// User-journey perf trace v2: hash-based tab nav for reliability.
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = 'http://127.0.0.1:3567';
const OUT = '/tmp/perf_qa';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function navHash(page, hash, settleMs = 800) {
  await page.evaluate((h) => { window.location.hash = h; }, hash);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  await sleep(settleMs);
}

const journeys = [
  {
    name: '01_cold_load_highlights',
    description: '冷加载（默认 v=highlights 精选 tab）',
    run: async (page) => {
      await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 });
      await sleep(800);
    },
  },
  {
    name: '02_switch_recommend',
    description: '切到「推荐」tab',
    run: async (page) => navHash(page, 'v=recommend'),
  },
  {
    name: '03_switch_channels',
    description: '切到「频道」tab（最常用列表页）',
    run: async (page) => navHash(page, 'v=channels'),
  },
  {
    name: '04_switch_back_highlights',
    description: '回到「精选」tab（命中缓存观察）',
    run: async (page) => navHash(page, 'v=highlights'),
  },
  {
    name: '05_open_first_card_in_channels',
    description: '在频道页点击第一张卡片',
    run: async (page) => {
      await navHash(page, 'v=channels', 1500);
      const card = page.locator('article, [role="article"], [data-item-id], [class*="InfoCard"], [class*="info-card"]').first();
      const count = await card.count();
      if (count) {
        try { await card.click({ timeout: 3000 }); }
        catch (e) { console.log('  click failed:', e.message); }
      } else {
        console.log('  no card found');
      }
      await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
      await sleep(1000);
    },
  },
  {
    name: '06_back_to_channels',
    description: '关闭详情回到频道列表',
    run: async (page) => navHash(page, 'v=channels'),
  },
];

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const reqs = new Map();
  page.on('request', r => {
    reqs.set(r.url() + '@' + Date.now() + '@' + Math.random(), {
      url: r.url(),
      start: Date.now(),
      end: 0,
      status: 0,
      type: r.resourceType(),
      method: r.method(),
    });
  });
  page.on('response', async r => {
    let last = null;
    for (const v of reqs.values()) {
      if (v.url === r.url() && v.end === 0) { last = v; }
    }
    if (!last) return;
    last.end = Date.now();
    last.status = r.status();
    try { last.bytes = (await r.body()).length; } catch { last.bytes = 0; }
  });
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE-ERROR:', msg.text().slice(0, 200));
  });

  const journeyReports = [];

  for (const j of journeys) {
    const before = new Set(reqs.keys());
    const t0 = Date.now();
    let error = null;
    try { await j.run(page); }
    catch (e) { error = String(e); }
    const t1 = Date.now();

    const apiCalls = [];
    for (const [k, info] of reqs.entries()) {
      if (before.has(k)) continue;
      if (!info.end) continue;
      if (!info.url.includes('/api/')) continue;
      apiCalls.push({
        url: info.url.replace(BASE, ''),
        method: info.method,
        status: info.status,
        ms: info.end - info.start,
        bytes: info.bytes || 0,
      });
    }
    apiCalls.sort((a, b) => b.ms - a.ms);

    journeyReports.push({
      name: j.name,
      description: j.description,
      total_wall_ms: t1 - t0,
      api_call_count: apiCalls.length,
      api_total_ms: apiCalls.reduce((s, c) => s + c.ms, 0),
      api_top10: apiCalls.slice(0, 10),
      api_all: apiCalls,
      error,
    });

    await page.screenshot({ path: path.join(OUT, `${j.name}.png`), fullPage: false }).catch(() => {});
  }

  fs.writeFileSync(path.join(OUT, 'perf_report.json'), JSON.stringify(journeyReports, null, 2));

  console.log('\n========= PERF REPORT =========\n');
  for (const r of journeyReports) {
    console.log(`### ${r.name} — ${r.description}`);
    console.log(`  wall: ${r.total_wall_ms}ms | API calls: ${r.api_call_count} | sum API ms: ${r.api_total_ms}ms`);
    if (r.error) console.log(`  ERROR: ${r.error}`);
    if (r.api_top10.length) {
      console.log('  Top API calls (by ms):');
      for (const c of r.api_top10) {
        console.log(`    ${String(c.ms).padStart(5)}ms  ${String(c.status).padStart(3)} ${c.method.padEnd(5)} ${c.url.slice(0, 130)}`);
      }
    }
    console.log();
  }

  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
