// G14 단계 6·8·11·12: 실제 관제탑(front dev 서버)을 Playwright 로 열어 로그인하고, 게임 화면 확인·일시정지·캡처·영역 표시·수정 요청 제출을
// 사람이 하는 대로 수행한다. cfg: playwright_dir, front_url, password, game_id, out_dir, mode(observe|feedback|builds), comment
import fs from 'node:fs';
import path from 'node:path';
import { launchBrowser, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const out = (n) => path.join(cfg.out_dir, n);
fs.mkdirSync(cfg.out_dir, { recursive: true });
const R = { mode: cfg.mode, checks: {}, shots: {} };

const browser = await launchBrowser(pw.chromium);
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
await ctx.addInitScript(() => { try { localStorage.setItem('tk-onboarded', '1'); } catch { /* 저장소 없음 */ } }); // 첫 실행 소개 대화상자를 건너뛴다
const page = await ctx.newPage();
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const posted = [];
page.on('request', (r) => { if (r.method() === 'POST' && /\/api\/games\/[^/]+\/(requests|rollback|builds)$/.test(r.url())) posted.push({ url: r.url(), body: r.postData() }); });

try {
  await page.goto(cfg.front_url + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#password', cfg.password);
  await page.click('button[type=submit]');
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 20000 });
  await page.goto(cfg.front_url + '/games/' + cfg.game_id, { waitUntil: 'domcontentloaded' });
  const frame = page.locator('iframe[title^="게임 화면"]');
  await frame.waitFor({ timeout: 30000 });
  await page.waitForTimeout(2500); // 게임 부팅
  R.chips = await page.locator('.game-chips').innerText();
  R.checks.genre_chip = /장르\s*\S+/.test(R.chips);
  R.shots.cockpit = out(cfg.prefix + 'cockpit.png');
  await page.screenshot({ path: R.shots.cockpit });

  if (cfg.mode === 'observe') {
    await page.getByRole('button', { name: '일시정지' }).click();
    R.checks.pause_toggles = await page.getByRole('button', { name: '계속하기' }).waitFor({ timeout: 8000 }).then(() => true, () => false);
    await page.getByRole('button', { name: '계속하기' }).click().catch(() => {});
    R.checks.resume_toggles = await page.getByRole('button', { name: '일시정지' }).waitFor({ timeout: 8000 }).then(() => true, () => false);
    await page.getByRole('tab', { name: '잠긴 결정' }).click();
    R.lockedDecisions = await page.locator('[role=tabpanel]').innerText();
  }

  if (cfg.mode === 'feedback') {
    await page.getByRole('button', { name: '현재 화면 캡처' }).click();
    const img = page.locator('img[alt="캡처한 게임 화면"]');
    await img.waitFor({ timeout: 20000 });
    R.checks.captured = true;
    await img.scrollIntoViewIfNeeded();
    const b = await img.boundingBox();
    // 주인공 쪽(화면 가운데 아래)을 박스로 표시한다. cfg.box 는 이미지 비율 좌표 [x1,y1,x2,y2].
    const [x1, y1, x2, y2] = cfg.box ?? [0.3, 0.35, 0.7, 0.65];
    await page.mouse.move(b.x + b.width * x1, b.y + b.height * y1);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width * ((x1 + x2) / 2), b.y + b.height * ((y1 + y2) / 2), { steps: 6 });
    await page.mouse.move(b.x + b.width * x2, b.y + b.height * y2, { steps: 6 });
    await page.mouse.up();
    R.shots.annotated = out(cfg.prefix + 'annotated-ui.png');
    await page.screenshot({ path: R.shots.annotated, fullPage: true });
    await page.fill('#game-feedback-text', cfg.comment);
    const wait = page.waitForRequest((r) => r.method() === 'POST' && /\/api\/games\/[^/]+\/requests$/.test(r.url()), { timeout: 60000 });
    await page.getByRole('button', { name: 'AI 수정 요청' }).click();
    const req = await wait;
    R.requestBody = JSON.parse(req.postData() ?? 'null');
    await page.getByRole('tab', { name: 'AI 작업' }).waitFor();
    await page.waitForTimeout(1500);
    R.shots.afterSubmit = out(cfg.prefix + 'after-submit.png');
    await page.screenshot({ path: R.shots.afterSubmit, fullPage: true });
  }

  if (cfg.mode === 'builds') {
    await page.getByRole('tab', { name: '버전' }).click();
    await page.waitForTimeout(1200);
    R.buildsText = await page.locator('[role=tabpanel]').innerText();
    R.shots.builds = out(cfg.prefix + 'builds-tab.png');
    await page.screenshot({ path: R.shots.builds, fullPage: true });
  }
} catch (e) {
  R.error = String(e.stack ?? e);
  await page.screenshot({ path: out(cfg.prefix + 'error.png'), fullPage: true }).catch(() => {});
}
R.posted = posted;
R.consoleErrors = consoleErrors;
await browser.close();
emit(R);
