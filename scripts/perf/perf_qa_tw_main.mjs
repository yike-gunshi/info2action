import { chromium } from 'playwright';
import fs from 'fs';

const BASE = 'http://127.0.0.1:3567';
const OUT = '/tmp/qa_tw_main';

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const imgEvents = [];
page.on('response', (resp) => {
  const url = resp.url();
  const ct = resp.headers()['content-type'] || '';
  if (url.includes('/api/media/twitter') || url.includes('pbs.twimg.com') || ct.startsWith('image/')) {
    imgEvents.push({ url, status: resp.status(), ct, fromCache: resp.fromServiceWorker() });
  }
});

const consoleErrs = [];
page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrs.push(msg.text());
});

// Go to channels view with X subfilter
await page.goto(`${BASE}/#v=channels`, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForTimeout(6000);

// Look for X/Twitter channel pill/section
await page.screenshot({ path: `${OUT}/01_channels_initial.png`, fullPage: false });

// Find Twitter section by scanning for section header with "X" platform name
// /api/feed/platforms returns 'twitter' platform; channels page renders per-platform sections
const twitterSectionFound = await page.evaluate(() => {
  // Look for section anchors / pills containing X / Twitter
  const candidates = Array.from(document.querySelectorAll('[data-platform], button, a, h2, h3'));
  const xElems = candidates.filter(el => {
    const t = (el.textContent || '').trim();
    const dp = el.getAttribute('data-platform') || '';
    return dp.toLowerCase() === 'twitter' || dp.toLowerCase() === 'x' || t === 'X' || /^Twitter$/i.test(t);
  });
  if (xElems.length > 0) {
    xElems[0].scrollIntoView({ behavior: 'instant', block: 'center' });
    return { found: true, count: xElems.length, tag: xElems[0].tagName, text: (xElems[0].textContent || '').slice(0, 40) };
  }
  return { found: false };
});
console.log('twitter_section_probe:', JSON.stringify(twitterSectionFound));
await page.waitForTimeout(2000);
await page.screenshot({ path: `${OUT}/02_twitter_section.png`, fullPage: false });

// Wait for images to load (extended)
await page.waitForTimeout(20000);
await page.screenshot({ path: `${OUT}/03_after_wait.png`, fullPage: false });
await page.screenshot({ path: `${OUT}/03b_full.png`, fullPage: true });

// Probe actual <img> elements: check natural dimensions
const imgStats = await page.evaluate(() => {
  const imgs = Array.from(document.querySelectorAll('img'));
  const tw = imgs.filter(i => {
    const src = i.currentSrc || i.src || '';
    return src.includes('/api/media/twitter') || src.includes('pbs.twimg.com') || src.includes('twitter');
  });
  return {
    total_imgs: imgs.length,
    twitter_imgs: tw.length,
    twitter_loaded: tw.filter(i => i.naturalWidth > 0).length,
    twitter_zero: tw.filter(i => i.naturalWidth === 0).length,
    samples: tw.slice(0, 8).map(i => ({
      src: (i.currentSrc || i.src).slice(0, 140),
      naturalWidth: i.naturalWidth,
      naturalHeight: i.naturalHeight,
      complete: i.complete,
    })),
  };
});

const twReqs = imgEvents.filter(e => e.url.includes('/api/media/twitter'));
const directReqs = imgEvents.filter(e => e.url.includes('pbs.twimg.com'));
const twOk = twReqs.filter(e => e.status === 200).length;
const twBad = twReqs.filter(e => e.status >= 400).length;

const report = {
  url: page.url(),
  console_errors: consoleErrs.slice(0, 10),
  img_dom: imgStats,
  network: {
    proxy_requests: twReqs.length,
    proxy_200: twOk,
    proxy_4xx5xx: twBad,
    direct_pbs_requests: directReqs.length,
    direct_pbs_4xx5xx: directReqs.filter(e => e.status >= 400).length,
    sample_proxy: twReqs.slice(0, 5).map(e => ({ url: e.url.slice(-60), status: e.status })),
    sample_direct: directReqs.slice(0, 5).map(e => ({ url: e.url.slice(-60), status: e.status })),
  },
};
fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));

await browser.close();
