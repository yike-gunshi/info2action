#!/usr/bin/env node
/**
 * Screenshot the detail panel by clicking a card.
 */
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const APP_URL = 'http://localhost:5173';
const DIR = 'qa_screenshots';
const label = process.argv[2] || 'detail';

if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(APP_URL, { waitUntil: 'networkidle', timeout: 15000 });
await page.waitForTimeout(1500);

// Click first card to open detail
const firstCard = await page.$('[data-masonry-id]');
if (firstCard) {
  await firstCard.click();
  await page.waitForTimeout(1000);

  // Screenshot the modal
  const modal = await page.$('.fixed.top-1\\/2');
  if (modal) {
    const box = await modal.boundingBox();
    if (box) {
      await page.screenshot({
        path: path.join(DIR, `${label}-modal.png`),
        clip: { x: box.x - 4, y: box.y - 4, width: box.width + 8, height: Math.min(box.height + 8, 880) },
      });
      console.log(`✅ Saved ${label}-modal.png`);
    }
  }

  // Full page with modal
  await page.screenshot({
    path: path.join(DIR, `${label}-full.png`),
    clip: { x: 0, y: 0, width: 1440, height: 900 },
  });
  console.log(`✅ Saved ${label}-full.png`);
}

await browser.close();
