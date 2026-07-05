// Honest diag of /#v=channels: what image endpoints are slow + why?
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:3652';
const OUT = '/tmp/perf_qa_channels_diag';
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
  });
  page.on('requestfailed', r => {
    const u = r.url().replace(BASE, '');
    const e = network.find(x => x.url === u && !x.end);
    if (e) {
      e.end = nowMs();
      e.status = 'FAILED';
      e.failure = r.failure()?.errorText;
    }
  });
  const consoleErrs = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrs.push(m.text().slice(0, 200)); });

  console.log('Navigating to channels page...');
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 });
  // wait long enough for ffmpeg poster gen to potentially complete
  await sleep(20000);
  await page.screenshot({ path: `${OUT}/channels.png`, fullPage: false });

  // Categorize requests
  const photoReqs = network.filter(n => n.url.includes('/api/media/twitter-photo/'));
  const posterReqs = network.filter(n => n.url.includes('/api/media/twitter-poster/'));
  const mp4Reqs = network.filter(n => n.url.includes('/api/media/twitter-mp4/'));
  const directTwimg = network.filter(n => n.url.includes('twimg.com'));
  const otherApi = network.filter(n => n.url.includes('/api/') && !n.url.includes('/api/media/'));

  console.log('\n========= CHANNELS PAGE IMAGE DIAG =========\n');
  console.log(`Total tracked requests: ${network.length}`);
  console.log(`twitter-photo (BF-0515 new):  ${photoReqs.length}, slow >2s: ${photoReqs.filter(r => r.end && r.end - r.start > 2000).length}`);
  console.log(`twitter-poster (ffmpeg video): ${posterReqs.length}, slow >2s: ${posterReqs.filter(r => r.end && r.end - r.start > 2000).length}`);
  console.log(`twitter-mp4 (raw video):       ${mp4Reqs.length}, slow >2s: ${mp4Reqs.filter(r => r.end && r.end - r.start > 2000).length}`);
  console.log(`direct pbs.twimg.com (uncovered): ${directTwimg.length}`);
  console.log(`other /api/* endpoints:        ${otherApi.length}`);

  console.log('\nSlowest requests:');
  const allTimed = network.filter(n => n.end).sort((a, b) => (b.end - b.start) - (a.end - a.start));
  for (const r of allTimed.slice(0, 15)) {
    console.log(`  ${String(r.end - r.start).padStart(6)}ms  ${String(r.status).padStart(6)} ${r.url.slice(0, 100)}`);
  }

  console.log('\nFailed requests:');
  for (const r of network.filter(n => n.status === 'FAILED' || n.status === -1).slice(0, 10)) {
    console.log(`  FAIL  ${r.url.slice(0, 100)}  ${r.failure || ''}`);
  }

  console.log('\nConsole errors (sample):');
  for (const e of consoleErrs.slice(0, 8)) console.log(`  ${e.slice(0, 150)}`);

  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify({ network, consoleErrs }, null, 2));
  console.log(`\nfiles: ${OUT}/report.json + channels.png`);
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
