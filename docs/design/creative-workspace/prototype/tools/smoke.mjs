// 사용: NODE_PATH=/private/tmp/ttakkari-playwright-node/node_modules node smoke.mjs
import { createRequire } from 'node:module';
const require = createRequire('/private/tmp/ttakkari-playwright-node/node_modules/');
const { chromium } = require('playwright');
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const url = (h) => `file://${root}/index.html${h}`;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errs = [];
page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) errs.push(m.type() + ': ' + m.text()); });
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
for (const h of ['#work', '#work?s=new', '#work?s=running', '#library', '#library?item=l1', '#history?run=r2', '#tools', '#tools?s=sample&tool=photo']) {
  await page.goto(url(h)); await page.waitForTimeout(200);
  const n = await page.evaluate(() => document.querySelector('#cw-screen').innerText.length);
  console.log(h, 'textlen', n);
}
console.log('errors:', errs.length); errs.forEach((e) => console.log(' ', e));
await browser.close();
