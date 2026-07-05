import { chromium } from 'playwright';
import fs from 'fs';
const OUT = '/tmp/qa_tw_main';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const reqs = []; const errs = [];
page.on('response', r => {
  const u = r.url();
  if (u.includes('/api/media/twitter') || u.includes('pbs.twimg.com')) reqs.push({u, s: r.status()});
});
page.on('console', m => { if (m.type()==='error') errs.push(m.text().slice(0,140)); });
await page.goto('http://127.0.0.1:3567/#v=channels', { waitUntil: 'domcontentloaded', timeout: 30000 });
// Wait long enough for prewarm + image load
await page.waitForTimeout(30000);
await page.screenshot({ path: `${OUT}/04_stable_top.png`, fullPage: false });
// Scroll down to find Twitter-heavy section
await page.evaluate(() => window.scrollTo(0, 1200));
await page.waitForTimeout(3000);
await page.screenshot({ path: `${OUT}/05_scrolled.png`, fullPage: false });
const stats = await page.evaluate(() => {
  const imgs = Array.from(document.querySelectorAll('img'));
  const tw = imgs.filter(i => (i.currentSrc||i.src||'').includes('/api/media/twitter'));
  return { total: imgs.length, tw_total: tw.length, tw_loaded: tw.filter(i=>i.naturalWidth>0).length };
});
console.log('FINAL_STATS:', JSON.stringify({ ...stats, proxy_reqs: reqs.length, proxy_200: reqs.filter(r=>r.s===200).length, proxy_err: reqs.filter(r=>r.s>=400).length, pbs_direct: reqs.filter(r=>r.u.includes('pbs.twimg.com')).length, console_errors_first3: errs.slice(0,3) }));
await browser.close();
