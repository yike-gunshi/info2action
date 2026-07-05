// 验证用户「刷新还是慢」的根因
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:3652';
const OUT = '/tmp/perf_refresh_diag';
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowMs = () => Date.now();

async function captureLoad(page, label) {
  const events = [];
  const reqStart = new Map();
  page.on('request', r => {
    if (r.url().includes('/api/media/')) reqStart.set(r.url(), nowMs());
  });
  page.on('response', r => {
    if (r.url().includes('/api/media/')) {
      const start = reqStart.get(r.url());
      events.push({
        url: r.url().replace(BASE, ''),
        ms: start ? nowMs() - start : 0,
        status: r.status(),
        from_cache: r.fromServiceWorker() || (r.request().headers()['cache-control'] || '').includes('only-if-cached'),
      });
    }
  });
  return events;
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // ---- Run 1: cold first visit ----
  console.log('--- Run 1: 冷首次打开 /#v=channels ---');
  let events1 = await captureLoad(page, 'r1');
  page.on('response', r => {
    if (r.url().includes('/api/media/')) events1.push(r);
  });
  let net1 = [];
  page.on('request', r => {
    if (r.url().includes('/api/media/')) net1.push({ url: r.url().replace(BASE, ''), start: nowMs(), method: r.method() });
  });
  page.on('response', async r => {
    if (!r.url().includes('/api/media/')) return;
    const e = net1.find(x => BASE + x.url === r.url() && !x.end);
    if (!e) return;
    e.end = nowMs(); e.status = r.status();
    e.cache_status = r.headers()['x-cache'] || 'none';
    e.from_disk_cache = false;
    try {
      const sec = await r.serverAddr();
      e.from_network = !!sec;
    } catch {}
  });
  const t0 = nowMs();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'networkidle', timeout: 60000 });
  await sleep(3000);
  await page.screenshot({ path: `${OUT}/run1_cold.png` });

  const r1Stats = net1.filter(n => n.end);
  console.log(`  Total media requests: ${r1Stats.length}`);
  console.log(`  Avg time: ${Math.round(r1Stats.reduce((s, e) => s + (e.end - e.start), 0) / r1Stats.length)}ms`);
  console.log(`  Page total wall: ${nowMs() - t0}ms`);

  // ---- Run 2: soft refresh (Cmd+R simulation) ----
  console.log('\n--- Run 2: 软刷新（Cmd+R）模拟 ---');
  let net2 = [];
  page.on('request', r => {
    if (r.url().includes('/api/media/')) net2.push({ url: r.url().replace(BASE, ''), start: nowMs(), method: r.method() });
  });
  page.on('response', async r => {
    if (!r.url().includes('/api/media/')) return;
    const e = net2.find(x => BASE + x.url === r.url() && !x.end && x.start > t0 + 5000);
    if (!e) return;
    e.end = nowMs(); e.status = r.status();
  });
  const t1 = nowMs();
  await page.reload({ waitUntil: 'networkidle' });
  await sleep(3000);
  await page.screenshot({ path: `${OUT}/run2_softrefresh.png` });
  const r2Stats = net2.filter(n => n.end && n.start > t1);
  console.log(`  Total media requests: ${r2Stats.length}`);
  if (r2Stats.length) console.log(`  Avg time: ${Math.round(r2Stats.reduce((s, e) => s + (e.end - e.start), 0) / r2Stats.length)}ms`);
  console.log(`  Wall: ${nowMs() - t1}ms`);

  // ---- Inspect network panel browser-side ----
  const cacheStats = await page.evaluate(() => {
    const entries = performance.getEntriesByType('resource').filter(e => e.name.includes('/api/media/'));
    return entries.map(e => ({
      url: e.name.split(BASE.replace(':', '\\:')).pop().slice(0, 80),
      duration: Math.round(e.duration),
      transferSize: e.transferSize,  // 0 = from cache
      decodedBodySize: e.decodedBodySize,
      from_cache: e.transferSize === 0 && e.decodedBodySize > 0,
    }));
  });
  console.log('\n=== Browser perfomance.getEntriesByType("resource") ===');
  console.log(`Total media entries: ${cacheStats.length}`);
  console.log(`From browser cache (transferSize=0): ${cacheStats.filter(c => c.from_cache).length}`);
  console.log(`From network (transferSize>0): ${cacheStats.filter(c => !c.from_cache).length}`);

  console.log('\nSample (first 8):');
  for (const c of cacheStats.slice(0, 8)) {
    console.log(`  duration=${c.duration}ms transferSize=${c.transferSize} from_cache=${c.from_cache} ${c.url.slice(-60)}`);
  }

  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify({ run1: net1, run2: net2, browserCache: cacheStats }, null, 2));
  console.log(`\nfiles: ${OUT}/`);
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
