// Honest QA: open browser + actually click things + record what user sees,
// not just metric numbers. Captures video + every network call + frame
// screenshots showing exactly when content changes.

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = 'http://127.0.0.1:3876';
const OUT = '/tmp/perf_qa_real_user';
fs.mkdirSync(OUT, { recursive: true });
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowMs = () => Date.now();

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: OUT, size: { width: 1440, height: 900 } },
  });
  const page = await ctx.newPage();

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
    e.end = nowMs();
    e.status = r.status();
  });

  const events = [];  // timeline of significant events
  const log = (label, extra = {}) => events.push({ ms_since_start: nowMs() - sessionStart, label, ...extra });

  const sessionStart = nowMs();
  log('session_start');

  // ─── Test A: cold load 频道 page ────────────────────────────────
  log('Test_A_navigate_channels');
  const t0 = nowMs();
  await page.goto(`${BASE}/#v=channels`, { waitUntil: 'load', timeout: 60000 });
  log('channels_load_event', { ms: nowMs() - t0 });

  // wait for cards to appear with REAL content (not skeleton)
  const cardSeenAt = await page.evaluate(() => {
    return new Promise(resolve => {
      const start = performance.now();
      const check = () => {
        const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
        for (const c of cards) {
          const text = (c.textContent || '').trim();
          // require: real text, not "skeleton" / "loading" / placeholder
          if (text.length > 50 && !text.toLowerCase().includes('loading') && !text.toLowerCase().includes('skeleton')) {
            resolve({ ms: Math.round(performance.now() - start), text_sample: text.slice(0, 80) });
            return;
          }
        }
        if (performance.now() - start > 30000) {
          resolve({ ms: Math.round(performance.now() - start), text_sample: null, timeout: true });
          return;
        }
        requestAnimationFrame(check);
      };
      check();
    });
  }).catch(() => ({ error: 'eval failed' }));
  log('channels_first_real_card', cardSeenAt);
  await page.screenshot({ path: path.join(OUT, '01_channels_after_card.png') }).catch(() => {});

  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  log('channels_network_idle', { ms: nowMs() - t0 });

  // ─── Test B: 切到推荐 ────────────────────────────────────────────
  await sleep(1500);
  log('Test_B_switch_to_recommend');
  const tB = nowMs();
  await page.evaluate(() => { window.location.hash = 'v=recommend'; });
  const cardB = await page.evaluate(() => {
    return new Promise(resolve => {
      const start = performance.now();
      const check = () => {
        const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
        for (const c of cards) {
          const t = (c.textContent || '').trim();
          if (t.length > 50 && !t.toLowerCase().includes('loading')) {
            resolve({ ms: Math.round(performance.now() - start), text_sample: t.slice(0, 80) });
            return;
          }
        }
        if (performance.now() - start > 15000) {
          resolve({ ms: Math.round(performance.now() - start), timeout: true });
          return;
        }
        requestAnimationFrame(check);
      };
      check();
    });
  }).catch(() => null);
  log('recommend_first_real_card', { ms: nowMs() - tB, ...cardB });
  await page.screenshot({ path: path.join(OUT, '02_recommend.png') }).catch(() => {});

  // ─── Test C: 切到精选, 然后依次点 pill ────────────────────────
  await sleep(1500);
  log('Test_C_switch_to_highlights');
  const tC = nowMs();
  await page.evaluate(() => { window.location.hash = 'v=highlights'; });
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  log('highlights_network_idle', { ms: nowMs() - tC });
  await page.screenshot({ path: path.join(OUT, '03_highlights.png') }).catch(() => {});

  // List all pill-like elements
  const pillSurvey = await page.evaluate(() => {
    const candidates = document.querySelectorAll('button, [role="tab"], [class*="pill"], [class*="chip"], [class*="filter"]');
    const items = [];
    for (const el of candidates) {
      const text = (el.textContent || '').trim();
      if (text.length > 0 && text.length < 30) {
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 && rect.top < 800) {
          items.push({ text, x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), tag: el.tagName, cls: (el.className || '').slice(0, 50) });
        }
      }
    }
    return items.slice(0, 40);
  }).catch(() => []);
  log('pill_candidates', { count: pillSurvey.length });
  fs.writeFileSync(path.join(OUT, 'pill_survey.json'), JSON.stringify(pillSurvey, null, 2));

  // try clicking each suspected pill (产品, 工具, Coding, 模型)
  for (const cat of ['产品', '工具', 'Coding', '模型']) {
    await sleep(1000);
    const startBefore = await page.evaluate(() => {
      const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
      const titles = [];
      for (const c of cards) {
        const t = (c.textContent || '').slice(0, 50).trim();
        if (t) titles.push(t);
        if (titles.length >= 3) break;
      }
      return titles;
    });
    const tStart = nowMs();
    log(`pill_click_${cat}_attempt`);
    // try multiple selectors
    let clicked = false;
    for (const sel of [`button:text-is("${cat}")`, `text="${cat}"`, `[role="tab"]:text-is("${cat}")`]) {
      try {
        await page.locator(sel).first().click({ timeout: 1500 });
        clicked = true;
        break;
      } catch {}
    }
    if (!clicked) {
      log(`pill_click_${cat}_FAILED`);
      continue;
    }
    log(`pill_click_${cat}_clicked`);

    // poll: when do the visible card titles change?
    const change = await page.evaluate((before) => {
      return new Promise(resolve => {
        const start = performance.now();
        const check = () => {
          const cards = document.querySelectorAll('article, [data-item-id], [class*="InfoCard"], [class*="card"]');
          const titles = [];
          for (const c of cards) {
            const t = (c.textContent || '').slice(0, 50).trim();
            if (t) titles.push(t);
            if (titles.length >= 3) break;
          }
          const changed = titles.length > 0 && before.length > 0 && titles[0] !== before[0];
          if (changed) {
            resolve({ ms: Math.round(performance.now() - start), new_titles: titles });
            return;
          }
          if (performance.now() - start > 15000) {
            resolve({ ms: Math.round(performance.now() - start), timeout: true, new_titles: titles });
            return;
          }
          requestAnimationFrame(check);
        };
        check();
      });
    }, startBefore);
    log(`pill_click_${cat}_content_changed`, { ms: nowMs() - tStart, ...change });
    await page.screenshot({ path: path.join(OUT, `04_pill_${cat}.png`) }).catch(() => {});
  }

  // ─── close + dump ─────────────────────────────────────────────
  await sleep(500);
  await ctx.close();

  // resolve video file path
  const videoFiles = fs.readdirSync(OUT).filter(f => f.endsWith('.webm'));
  console.log('video:', videoFiles);

  fs.writeFileSync(path.join(OUT, 'timeline.json'), JSON.stringify({
    started: new Date(sessionStart).toISOString(),
    events,
    network,
  }, null, 2));

  // Print summary
  console.log('\n========= REAL USER QA REPORT =========\n');
  console.log('Test A: 冷加载 /#v=channels');
  const A_load = events.find(e => e.label === 'channels_load_event');
  const A_card = events.find(e => e.label === 'channels_first_real_card');
  const A_idle = events.find(e => e.label === 'channels_network_idle');
  console.log(`  load event:           ${A_load?.ms}ms`);
  console.log(`  first REAL card text: ${A_card?.ms}ms — "${A_card?.text_sample?.slice(0, 60) || 'N/A'}"`);
  console.log(`  network idle:         ${A_idle?.ms}ms`);

  console.log('\nTest B: 切到推荐');
  const B_card = events.find(e => e.label === 'recommend_first_real_card');
  console.log(`  first REAL card text: ${B_card?.ms}ms — "${B_card?.text_sample?.slice(0, 60) || 'N/A'}"`);

  console.log('\nTest C: 精选 pill 切换');
  const pillEvents = events.filter(e => e.label.startsWith('pill_click_'));
  for (const cat of ['产品', '工具', 'Coding', '模型']) {
    const attempt = events.find(e => e.label === `pill_click_${cat}_attempt`);
    const clicked = events.find(e => e.label === `pill_click_${cat}_clicked`);
    const changed = events.find(e => e.label === `pill_click_${cat}_content_changed`);
    const failed = events.find(e => e.label === `pill_click_${cat}_FAILED`);
    if (failed) {
      console.log(`  ${cat}: 点击失败（pill 选择器没找到）`);
    } else if (changed) {
      console.log(`  ${cat}: 点击 → 内容变化用了 ${changed.ms}ms ${changed.timeout ? '(TIMEOUT, 内容没变)' : ''}`);
      if (changed.new_titles) console.log(`        新标题: ${changed.new_titles[0]?.slice(0, 50)}`);
    } else {
      console.log(`  ${cat}: ?`);
    }
  }

  console.log('\nTop API calls during whole session:');
  const apis = network.filter(n => n.end).sort((a, b) => (b.end - b.start) - (a.end - a.start));
  for (const a of apis.slice(0, 10)) {
    console.log(`  ${String(a.end - a.start).padStart(6)}ms  ${a.status} ${a.method} ${a.url.slice(0, 90)}`);
  }

  console.log(`\nFiles: ${OUT}/timeline.json + screenshots + ${videoFiles[0] || 'no video'}`);
  await browser.close();
}

main().catch(e => { console.error(e); process.exit(1); });
