// G16 실제 브라우저 E2E: 관제탑 '아이디어 한 줄 → 게임 생성' 흐름을 사람이 하는 대로 수행한다.
// 폼 입력 → 생성 → /games/:id 이동 → 'AI가 게임을 만드는 중' 안내 → (실제 worker Run 완료 → 자동 빌드) → 게임 iframe 열림.
// cfg: playwright_dir, front_url, password, out_dir, idea, name, genre, decision, max_wait_ms. 결과는 @@RESULT@@ JSON 한 줄이다.
import fs from 'node:fs';
import path from 'node:path';
import { launchBrowser, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const out = (n) => path.join(cfg.out_dir, n);
fs.mkdirSync(cfg.out_dir, { recursive: true });
const R = { checks: {}, shots: {}, timeline: {} };
const t0 = Date.now();
const mark = (k) => { R.timeline[k] = Math.round((Date.now() - t0) / 1000); };

const browser = await launchBrowser(pw.chromium);
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
await ctx.addInitScript(() => { try { localStorage.setItem('tk-onboarded', '1'); } catch { /* 저장소 없음 */ } });
const page = await ctx.newPage();
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const posted = [];
page.on('request', (r) => { if (r.method() === 'POST' && /\/api\/games\/create$/.test(r.url())) posted.push({ url: r.url(), body: r.postData() }); });

try {
  await page.goto(cfg.front_url + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#password', cfg.password);
  await page.click('button[type=submit]');
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 20000 });
  await page.goto(cfg.front_url + '/games', { waitUntil: 'domcontentloaded' });
  const form = page.locator('form[aria-label="새 게임 만들기"]');
  await form.waitFor({ timeout: 20000 });
  R.checks.form_visible = true;
  await page.fill('#game-idea', cfg.idea);
  await page.locator('.game-create__more summary').click();
  await page.fill('#game-name', cfg.name);
  await page.fill('#game-genre', cfg.genre);
  await page.fill('#game-decision', cfg.decision);
  await page.press('#game-decision', 'Enter');
  R.checks.decision_listed = await page.locator('ul[aria-label="꼭 지킬 결정 목록"] li').count() === 1;
  R.shots.form = out('1-form.png');
  await page.screenshot({ path: R.shots.form });

  await page.getByRole('button', { name: '게임 만들기' }).click();
  await page.waitForURL(/\/games\/[0-9a-f-]{36}$/, { timeout: 60000 });
  mark('created_navigated_s');
  R.gameId = page.url().split('/').pop();
  R.checks.navigated_to_cockpit = true;

  const working = page.getByText('AI가 게임을 만드는 중이에요');
  R.checks.working_notice = await working.waitFor({ timeout: 30000 }).then(() => true, () => false);
  await page.waitForTimeout(1500);
  R.shots.working = out('2-working.png');
  await page.screenshot({ path: R.shots.working });

  const frame = page.locator('iframe[title^="게임 화면"]');
  R.checks.iframe_opened = await frame.waitFor({ timeout: cfg.max_wait_ms }).then(() => true, () => false);
  mark('iframe_opened_s');
  R.checks.working_notice_gone = R.checks.iframe_opened ? await working.isHidden() : false;
  if (R.checks.iframe_opened) {
    await page.waitForTimeout(4000); // 게임 부팅
    R.chips = await page.locator('.game-chips').innerText();
    R.checks.build_chip = /빌드 v1/.test(R.chips);
    R.shots.playing = out('3-playing.png');
    await page.screenshot({ path: R.shots.playing });
  }
} catch (e) {
  R.error = String(e.stack ?? e);
  try { await page.screenshot({ path: out('error.png') }); } catch { /* 무시 */ }
}
R.posted = posted;
R.consoleErrors = consoleErrors;
await browser.close();
emit(R);
