// Diagnose: which images on /#v=recommend fail to load and why?
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:3683';
const OUT = '/tmp/perf_qa_image_diag';
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const imageEvents = [];
  page.on('response', async r => {
    const url = r.url();
    if (url.includes('pbs.twimg.com') || url.includes('/api/media/')) {
      imageEvents.push({
        url,
        status: r.status(),
        type: r.headers()['content-type'],
        len: r.headers()['content-length'],
      });
    }
  });
  page.on('requestfailed', r => {
    const url = r.url();
    if (url.includes('pbs.twimg.com') || url.includes('/api/media/')) {
      imageEvents.push({ url, status: 'FAILED', failure: r.failure()?.errorText });
    }
  });
  const consoleErrs = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrs.push(m.text().slice(0, 200)); });

  await page.goto(`${BASE}/#v=recommend`, { waitUntil: 'networkidle', timeout: 60000 });
  await sleep(3000);

  // Click 产品 pill if needed
  await page.locator('button:has-text("产品")').first().click({ timeout: 2000 }).catch(() => {});
  await sleep(3000);

  await page.screenshot({ path: `${OUT}/recommend_products.png`, fullPage: false });

  // Inspect every <img> tag: did it load?
  const imageStatus = await page.evaluate(() => {
    const imgs = Array.from(document.querySelectorAll('img'));
    return imgs.map(img => ({
      src: img.src,
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      complete: img.complete,
      loaded: img.complete && img.naturalWidth > 0,
      visible: img.getBoundingClientRect().top < 800 && img.getBoundingClientRect().width > 0,
    }));
  });

  // Filter to ACTUAL cards (not icons) — images > 50px wide
  const cardImages = imageStatus.filter(i => {
    return i.src && (i.src.includes('pbs.twimg.com') || i.src.includes('/api/media/') || i.src.startsWith('http'));
  });

  console.log('\n========= IMAGE LOAD DIAGNOSIS =========\n');
  console.log(`Total <img> elements: ${imageStatus.length}`);
  console.log(`Card-related images (twimg.com / /api/media): ${cardImages.length}`);
  console.log(`Loaded successfully: ${cardImages.filter(i => i.loaded).length}`);
  console.log(`Failed to load: ${cardImages.filter(i => !i.loaded && i.complete).length}`);
  console.log(`Still loading: ${cardImages.filter(i => !i.complete).length}`);

  console.log('\nFailed image samples (first 10):');
  for (const img of cardImages.filter(i => !i.loaded && i.complete).slice(0, 10)) {
    console.log(`  ${img.src.slice(0, 100)}`);
  }

  console.log('\n=== Network response status for image URLs ===');
  for (const ev of imageEvents.slice(0, 20)) {
    console.log(`  ${ev.status}  ${ev.url.slice(0, 100)}`);
  }

  console.log('\n=== Console errors ===');
  for (const e of consoleErrs.slice(0, 10)) {
    console.log(`  ${e.slice(0, 150)}`);
  }

  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify({ imageStatus, imageEvents, consoleErrs }, null, 2));
  console.log(`\nFull report: ${OUT}/report.json`);
  console.log(`Screenshot: ${OUT}/recommend_products.png`);
  await browser.close();
}
main().catch(e => { console.error(e); process.exit(1); });
