// 사용: node shot.mjs <hash> <width> <height> <theme> <out.png> [full]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire('/private/tmp/ttakkari-playwright-node/node_modules/');
const { chromium } = require('playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const [hash, w, h, theme, out, full] = process.argv.slice(2);
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({ viewport: { width: +w, height: +h }, deviceScaleFactor: 1, colorScheme: theme });
const page = await ctx.newPage();
await page.addInitScript((t) => localStorage.setItem('tk-theme', t), theme);
await page.goto('file://' + root + '/index.html' + hash);
await page.waitForTimeout(400);
await page.screenshot({ path: out, fullPage: full === 'full' });
await browser.close();
