#!/usr/bin/env node
/**
 * Quick screenshot utility for card design verification.
 * Usage: cd /path/to/info2action && node .worktrees/react-rewrite/scripts/screenshot_card.mjs [label]
 */
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const APP_URL = 'http://localhost:5173';
const DIR = 'qa_screenshots';
const label = process.argv[2] || 'card';

if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(APP_URL, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(1500);

// Screenshot first section cards area
const firstSection = await page.$('[id^="s-"]');
if (firstSection) {
  const box = await firstSection.boundingBox();
  if (box) {
    await page.screenshot({
      path: path.join(DIR, `${label}-section.png`),
      clip: { x: 0, y: box.y, width: 1440, height: Math.min(box.height, 900) },
    });
    console.log(`✅ Saved ${label}-section.png`);
  }
}

// Screenshot individual card (first one)
const firstCard = await page.$('[data-masonry-id]');
if (firstCard) {
  const box = await firstCard.boundingBox();
  if (box) {
    await page.screenshot({
      path: path.join(DIR, `${label}-single-card.png`),
      clip: { x: box.x - 4, y: box.y - 4, width: box.width + 8, height: box.height + 8 },
    });
    console.log(`✅ Saved ${label}-single-card.png`);
  }
}

// Screenshot full page top area
await page.screenshot({
  path: path.join(DIR, `${label}-full.png`),
  clip: { x: 0, y: 0, width: 1440, height: 900 },
});
console.log(`✅ Saved ${label}-full.png`);

await browser.close();
