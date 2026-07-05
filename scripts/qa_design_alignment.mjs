#!/usr/bin/env node
/**
 * QA: Design alignment verification
 * Checks card typography, layout, detail panel against DESIGN.md specs.
 */
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const APP_URL = 'http://localhost:5173';
const DIR = 'qa_screenshots';
if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

const results = [];
function pass(id, desc) { results.push({ id, s: 'pass', desc }); console.log(`  ✅ ${id}: ${desc}`); }
function fail(id, desc, detail) { results.push({ id, s: 'fail', desc, detail }); console.log(`  ❌ ${id}: ${desc} → ${detail}`); }

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(APP_URL, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(2000);

// ═══ T1: Card title typography ═══
console.log('\n── T1: Card Title ──');
const titleEl = await page.$('[data-masonry-id] h3');
if (titleEl) {
  const styles = await titleEl.evaluate(el => {
    const cs = getComputedStyle(el);
    return { fontSize: cs.fontSize, fontWeight: cs.fontWeight, lineHeight: cs.lineHeight };
  });
  if (styles.fontSize === '15px') pass('T1a', `标题字号 ${styles.fontSize}`);
  else fail('T1a', '标题字号', `期望 15px, 实际 ${styles.fontSize}`);
  if (parseInt(styles.fontWeight) >= 600) pass('T1b', `标题字重 ${styles.fontWeight}`);
  else fail('T1b', '标题字重', `期望 ≥600, 实际 ${styles.fontWeight}`);
} else fail('T1', '找不到卡片标题元素', '');

// ═══ T2: Platform label typography ═══
console.log('\n── T2: Platform Label ──');
const platformEl = await page.$('[data-masonry-id] h3 span span');
if (platformEl) {
  const styles = await platformEl.evaluate(el => {
    const cs = getComputedStyle(el);
    return { fontSize: cs.fontSize, fontWeight: cs.fontWeight };
  });
  if (styles.fontSize === '10px') pass('T2a', `平台标签字号 ${styles.fontSize}`);
  else fail('T2a', '平台标签字号', `期望 10px, 实际 ${styles.fontSize}`);
  if (parseInt(styles.fontWeight) >= 700) pass('T2b', `平台标签字重 ${styles.fontWeight}`);
  else fail('T2b', '平台标签字重', `期望 ≥700, 实际 ${styles.fontWeight}`);
} else fail('T2', '找不到平台标签元素', '');

// ═══ T3: Metrics row ═══
console.log('\n── T3: Metrics Row ──');
const metricsRow = await page.$('[data-masonry-id] .border-t');
if (metricsRow) {
  const styles = await metricsRow.evaluate(el => {
    const cs = getComputedStyle(el);
    return { fontSize: cs.fontSize, fontFamily: cs.fontFamily, borderTop: cs.borderTopWidth };
  });
  if (styles.fontSize === '12px') pass('T3a', `指标字号 ${styles.fontSize}`);
  else fail('T3a', '指标字号', `期望 12px, 实际 ${styles.fontSize}`);
  if (styles.fontFamily.includes('JetBrains') || styles.fontFamily.includes('monospace')) pass('T3b', `指标字体 mono`);
  else fail('T3b', '指标字体', `期望 mono, 实际 ${styles.fontFamily.slice(0, 50)}`);
  if (parseFloat(styles.borderTop) >= 1) pass('T3c', `指标分隔线 ${styles.borderTop}`);
  else fail('T3c', '指标分隔线', `期望 ≥1px, 实际 ${styles.borderTop}`);
} else fail('T3', '找不到指标行(border-t)', '');

// ═══ T4: Masonry gap ═══
console.log('\n── T4: Masonry Gap ──');
const masonryContainer = await page.$('.flex.gap-\\[14px\\]');
if (masonryContainer) {
  const gap = await masonryContainer.evaluate(el => getComputedStyle(el).gap);
  if (gap === '14px' || gap.includes('14')) pass('T4', `列间距 ${gap}`);
  else fail('T4', '列间距', `期望 14px, 实际 ${gap}`);
} else fail('T4', '找不到 Masonry 容器(gap-[14px])', '');

// ═══ T5: Hover effect (translateY) ═══
console.log('\n── T5: Hover ──');
const card = await page.$('[data-masonry-id]');
if (card) {
  await card.hover();
  await page.waitForTimeout(300);
  const inner = await card.$(':first-child');
  if (inner) {
    const transform = await inner.evaluate(el => getComputedStyle(el).transform);
    // -2px = translate-y-0.5 → matrix has -2 in last position
    if (transform.includes('-2') || transform.includes('-0.5')) pass('T5', `hover 位移 OK`);
    else if (transform === 'none') fail('T5', 'hover 位移', `无 transform 生效`);
    else pass('T5', `hover transform: ${transform.slice(0, 40)}`);
  }
}

// ═══ T6: Detail Panel ═══
console.log('\n── T6: Detail Panel ──');
const clickCard = await page.$('[data-masonry-id]');
if (clickCard) {
  await clickCard.click();
  await page.waitForTimeout(1200);

  // Check modal exists
  const modal = await page.$('.fixed.z-\\[500\\]');
  if (modal) pass('T6a', '详情弹窗已打开');
  else fail('T6a', '详情弹窗', '未找到 z-[500] 弹窗');

  // Check avatar in header
  const avatar = await page.$('.fixed.z-\\[500\\] img.rounded-full, .fixed.z-\\[500\\] div.rounded-full');
  if (avatar) pass('T6b', '头像在 header 区域');
  else fail('T6b', '头像位置', '未在 header 找到头像');

  // Check only 1 divider
  const dividers = await page.$$('.fixed.z-\\[500\\] .h-px.bg-border');
  if (dividers.length <= 1) pass('T6c', `分割线 ${dividers.length} 条`);
  else fail('T6c', '分割线数量', `期望 ≤1, 实际 ${dividers.length}`);

  // Screenshot
  await page.screenshot({
    path: path.join(DIR, 'qa-detail.png'),
    clip: { x: 0, y: 0, width: 1440, height: 900 },
  });
  console.log(`  📸 qa-detail.png`);

  // Close modal
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
}

// ═══ T7: Masonry clip line ═══
console.log('\n── T7: Masonry Clip Line ──');
const gradientMask = await page.$('.bg-gradient-to-t');
if (gradientMask) pass('T7', '渐变蒙版存在');
else fail('T7', '渐变蒙版', '未找到 bg-gradient-to-t');

// ═══ T8: Section screenshots ═══
console.log('\n── T8: Page Screenshots ──');
await page.screenshot({ path: path.join(DIR, 'qa-feed-top.png'), clip: { x: 0, y: 0, width: 1440, height: 900 } });
console.log('  📸 qa-feed-top.png');

// Scroll to see more sections
await page.evaluate(() => window.scrollBy(0, 600));
await page.waitForTimeout(500);
await page.screenshot({ path: path.join(DIR, 'qa-feed-mid.png'), clip: { x: 0, y: 0, width: 1440, height: 900 } });
console.log('  📸 qa-feed-mid.png');

// ═══ Summary ═══
const total = results.length;
const passed = results.filter(r => r.s === 'pass').length;
const failed = results.filter(r => r.s === 'fail').length;
console.log(`\n═══ QA Summary: ${passed}/${total} passed, ${failed} failed ═══`);
if (failed > 0) {
  console.log('Failed:');
  results.filter(r => r.s === 'fail').forEach(r => console.log(`  ❌ ${r.id}: ${r.desc} → ${r.detail}`));
}

await browser.close();
process.exit(failed > 0 ? 1 : 0);
