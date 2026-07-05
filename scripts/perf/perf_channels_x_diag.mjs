// Forge-bugfix P3 reproduce: real user view of /#v=channels with X subfilter.
// Stay open 60s to capture the full image loading lifecycle.
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:3652';
const OUT = '/tmp/perf_qa_channels_x';
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowMs = () => Date.now();

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const network = [];
  page.on('request', r => {
    const u = r.url();
    if (u.includes('/api/') || u.includes('twimg') || u.includes('twitter')) {
      network.push({ url: u.replace(BASE, ''), start: nowMs(), method: r.method(), type: r.resourceType() });
    }
  });
  page.on('response', async r => {
    if (!r.url().includes('/api/') && !r.url().includes('twimg')) return;
    const u = r.url().replace(BASE, '');
    const e = network.find(x => x.url === u && !x.end);
    if (!e) return;
    e.end = nowMs(); e.status = r.status();
    try { e.bytes = (await r.body()).length; } catch { e.bytes = 0; }
  });
  page.on('requestfailed', r => {
    const u = r.url().replace(BASE, '');
    const e = network.find(x => x.url === u && !x.end);
    if (e) {
      e.end = nowMs(); e.status = 'FAILED';
      e.failure = r.failure()?.errorText;
    }
  });

  console.log('--- T1: navigate to /#v=channels ---');
  const tStart = nowMs();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
  await sleep(2000);

  console.log('--- T2: click X (twitter) sub-tab ---');
  // Find and click X tab — small button with X letter
  const beforeClickReqs = network.length;
  const xButton = page.locator('button:has-text("X"), [aria-label*="twitter"], button:text-is("X")').first();
  if (await xButton.count() > 0) {
    await xButton.click({ timeout: 3000 }).catch(e => console.log('  click err:', e.message.slice(0, 80)));
  } else {
    console.log('  X tab button not found — listing all visible buttons:');
    const btnTexts = await page.$$eval('button', btns => btns.slice(0, 20).map(b => (b.textContent||'').trim().slice(0, 20)));
    console.log('  ', btnTexts);
  }

  console.log('--- T3: wait 30s for images to load ---');
  await sleep(30000);
  await page.screenshot({ path: `${OUT}/channels_x.png`, fullPage: false });

  // Check actual <img> elements in DOM
  const imgState = await page.evaluate(() => {
    const imgs = Array.from(document.querySelectorAll('img'));
    return imgs.filter(i => {
      const r = i.getBoundingClientRect();
      return r.width > 100 && r.top < 1000;  // visible card images, not avatars/icons
    }).map(i => ({
      src: i.src,
      complete: i.complete,
      naturalWidth: i.naturalWidth,
      naturalHeight: i.naturalHeight,
      loaded: i.complete && i.naturalWidth > 0,
    }));
  });

  console.log('\n========= 频道页 X 子过滤实测 =========\n');
  console.log(`Total /api/media/twitter-* requests: ${network.filter(n => n.url.includes('/api/media/twitter')).length}`);
  console.log(`  twitter-photo: ${network.filter(n => n.url.includes('/api/media/twitter-photo/')).length}`);
  console.log(`  twitter-poster: ${network.filter(n => n.url.includes('/api/media/twitter-poster/')).length}`);
  console.log(`Direct pbs.twimg.com: ${network.filter(n => n.url.includes('pbs.twimg.com')).length}`);
  console.log(`Visible card images in DOM: ${imgState.length}`);
  console.log(`  loaded successfully: ${imgState.filter(i => i.loaded).length}`);
  console.log(`  failed/loading: ${imgState.filter(i => !i.loaded).length}`);
  if (imgState.filter(i => !i.loaded).length > 0) {
    console.log('\nFailed/loading image src samples:');
    for (const i of imgState.filter(j => !j.loaded).slice(0, 8)) {
      console.log(`  src=${i.src.slice(0, 100)}  complete=${i.complete}  natW=${i.naturalWidth}`);
    }
  }

  console.log('\n=== 媒体类请求耗时分布 ===');
  const mediaReqs = network.filter(n => n.url.includes('/api/media/twitter')).filter(n => n.end);
  const buckets = { '<500ms': 0, '500ms-2s': 0, '2-5s': 0, '5-10s': 0, '>10s': 0, 'failed': 0 };
  for (const r of mediaReqs) {
    if (r.status === 'FAILED' || r.status === -1) buckets.failed++;
    else {
      const ms = r.end - r.start;
      if (ms < 500) buckets['<500ms']++;
      else if (ms < 2000) buckets['500ms-2s']++;
      else if (ms < 5000) buckets['2-5s']++;
      else if (ms < 10000) buckets['5-10s']++;
      else buckets['>10s']++;
    }
  }
  for (const [k, v] of Object.entries(buckets)) console.log(`  ${k}: ${v}`);

  console.log('\n=== 最慢 10 个媒体请求 ===');
  const sorted = mediaReqs.filter(n => n.end).sort((a, b) => (b.end - b.start) - (a.end - a.start));
  for (const r of sorted.slice(0, 10)) {
    console.log(`  ${String(r.end - r.start).padStart(6)}ms  ${String(r.status).padStart(6)} ${r.url.slice(0, 100)}`);
  }

  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify({ network, imgState }, null, 2));
  console.log(`\nfiles: ${OUT}/report.json + channels_x.png`);
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
