// 실플레이 재생: 재현 입력(JSON 스크립트)을 헤드리스 Chromium 에서 그대로 눌러 보고, 스크린샷·콘솔 오류·상태 단언을 기록한다.
// 브라우저를 못 띄우면 통과를 꾸미지 않고 status=unsupported 와 이유를 낸다(exit 3). 그 결과는 증거가 되지 못한다.
// 브라우저 탐색 방식은 플랫폼의 tests/game_smoke/ttk_driver.mjs(launchBrowser)를 따른다.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { analyzePng } from './png.mjs';
import { now, readJson, sha256Buf, writeJson } from './util.mjs';

export function loadPlaywright(root) {
  const dirs = [process.env.STUDIO_PLAYWRIGHT_DIR, process.env.TTAKKARI_PLAYWRIGHT_DIR, root, process.cwd()].filter(Boolean);
  const tried = [];
  for (const d of dirs) {
    const req = createRequire(path.join(path.resolve(d), 'noop.js'));
    for (const name of ['playwright', 'playwright-core']) {
      try {
        return { pw: req(name), from: `${name} @ ${d}` };
      } catch (e) {
        tried.push(`${name}@${d}`);
        void e;
      }
    }
  }
  return { pw: null, reason: `playwright 모듈을 찾지 못했다(시도: ${tried.join(', ')}). STUDIO_PLAYWRIGHT_DIR(node_modules 가 있는 폴더)을 지정한다` };
}

// 이 프로세스와 같은 샌드박스에서 읽기 전용 IPC 사전 검사를 한다. 실패 시 브라우저를 띄우지 않는다.
export function browserPreflight() {
  const script = fileURLToPath(new URL('./browser_preflight.py', import.meta.url));
  const check = spawnSync('python3', [script], { encoding: 'utf8', timeout: 10000 });
  if (check.error) throw new Error(`browser preflight: ${check.error.message}`);
  const report = JSON.parse(check.stdout);
  if (check.status !== 0) throw new Error(`browser preflight: ${report.status ?? 'unknown'}`);
  return report;
}

export async function launchStudioBrowser(chromium, preflight = browserPreflight) {
  const report = await preflight();
  if (!['permissions_allowed', 'not_applicable'].includes(report?.status)) {
    throw new Error(`browser preflight: ${report?.status ?? 'unknown'}`);
  }
  const executablePath = process.env.STUDIO_BROWSER_EXECUTABLE || process.env.TTAKKARI_BROWSER_EXECUTABLE;
  // 실행 오류 후 다른 바이너리·채널로 재시도하지 않는다.
  return chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.woff2': 'font/woff2' };

/** 정적 서빙 핸들러. dir 밖(경로 탈출)은 403, 없는 경로는 SPA 처럼 index.html 로 되돌린다. 소켓 없이 단위 테스트할 수 있게 분리했다. */
export function makeHandler(dir) {
  const base = path.resolve(dir);
  return (req, res) => {
    let u;
    try {
      u = decodeURIComponent((req.url ?? '/').split('?')[0]);
    } catch {
      res.writeHead(400).end();
      return;
    }
    let f = path.resolve(base, '.' + (u.endsWith('/') ? u + 'index.html' : u));
    if (!f.startsWith(base + path.sep) && f !== base) {
      res.writeHead(403).end();
      return;
    }
    if (!fs.existsSync(f) || !fs.statSync(f).isFile()) f = path.join(base, 'index.html');
    if (!fs.existsSync(f)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'content-type': MIME[path.extname(f)] ?? 'application/octet-stream' });
    fs.createReadStream(f).pipe(res);
  };
}

/** 정적 서버(빌드 산출물 확인용). 127.0.0.1 에만 바인딩한다. */
export function serveDir(dir) {
  const server = http.createServer(makeHandler(dir));
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, url: `http://127.0.0.1:${server.address().port}/` })));
}

const slug = (s) => String(s).replace(/[^\w.-]+/g, '_').slice(0, 60);

/** 스크립트 한 편을 실행한다. 단계별 pass/fail 과 증거 파일을 result 에 모은다. */
export async function runPlay({ root, script, url, outDir }) {
  const spec = typeof script === 'string' ? readJson(script) : script;
  const result = { tool: 'studio-play', name: spec.name ?? 'play', at: now(), url, ok: false, status: 'failed', steps: [], shots: [], console: { errors: [], pageErrors: [], failedRequests: [] }, vars: {} };
  const loaded = loadPlaywright(root);
  if (!loaded.pw) return { ...result, status: 'unsupported', reason: loaded.reason };
  let browser;
  try {
    browser = await launchStudioBrowser(loaded.pw.chromium);
  } catch (e) {
    return { ...result, status: 'unsupported', reason: String(e.message) };
  }
  fs.mkdirSync(outDir, { recursive: true });
  const vp = spec.viewport ?? { width: 960, height: 540 };
  const ctx = await browser.newContext({ viewport: vp, hasTouch: !!spec.touch, isMobile: !!spec.touch, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.setDefaultTimeout(spec.timeoutMs ?? 10000);
  page.on('console', (m) => m.type() === 'error' && result.console.errors.push(m.text()));
  page.on('pageerror', (e) => result.console.pageErrors.push(String(e)));
  page.on('requestfailed', (r) => result.console.failedRequests.push(`${r.method()} ${r.url()}`));
  const box = async () => (await page.$('canvas'))?.boundingBox() ?? { x: 0, y: 0, ...vp, width: vp.width, height: vp.height };
  const fail = (step, msg) => result.steps.push({ ...step, ok: false, msg });
  try {
    await page.goto(spec.url ? new URL(spec.url, url).href : url, { waitUntil: 'load' });
    for (const [i, step] of (spec.steps ?? []).entries()) {
      const rec = { i, op: step.op, label: step.label ?? null };
      try {
        if (step.op === 'wait') await page.waitForTimeout(step.ms ?? 500);
        else if (step.op === 'focus') {
          const b = await box();
          await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
        } else if (step.op === 'key') {
          await page.keyboard.down(step.code);
          await page.waitForTimeout(step.holdMs ?? 150);
          await page.keyboard.up(step.code);
        } else if (step.op === 'keys') {
          for (const c of step.codes) await page.keyboard.down(c);
          await page.waitForTimeout(step.holdMs ?? 300);
          for (const c of [...step.codes].reverse()) await page.keyboard.up(c);
        } else if (step.op === 'tap') {
          const b = await box();
          await page.mouse.click(b.x + b.width * (step.x ?? 0.5), b.y + b.height * (step.y ?? 0.5));
        } else if (step.op === 'eval') {
          result.vars[step.as ?? `v${i}`] = await page.evaluate(step.expr);
        } else if (step.op === 'waitFor') {
          await page.waitForFunction(step.expr, null, { timeout: step.timeoutMs ?? 8000 });
        } else if (step.op === 'dump') {
          const v = await page.evaluate(step.expr);
          const f = path.join(outDir, slug(step.to ?? 'dump.json'));
          writeJson(f, v);
          rec.file = path.relative(root, f);
        } else if (step.op === 'shot') {
          const buf = await page.screenshot();
          const f = path.join(outDir, `${String(result.shots.length + 1).padStart(2, '0')}-${slug(step.name ?? 'shot')}.png`);
          fs.writeFileSync(f, buf);
          const st = analyzePng(buf);
          result.shots.push({ file: path.relative(root, f), sha256: sha256Buf(buf), pixels: st });
          rec.file = path.relative(root, f);
          if (st?.blank) {
            fail(rec, '스크린샷이 빈 화면이다');
            continue;
          }
        } else if (step.op === 'expect') {
          const v = await page.evaluate(step.expr);
          if (!v) {
            fail(rec, `단언 실패: ${step.expr} → ${JSON.stringify(v)}`);
            continue;
          }
        } else if (step.op === 'expectNoConsoleErrors') {
          const n = result.console.errors.length + result.console.pageErrors.length;
          if (n) {
            fail(rec, `콘솔·페이지 오류 ${n}건: ${[...result.console.errors, ...result.console.pageErrors][0].slice(0, 160)}`);
            continue;
          }
        } else {
          fail(rec, `알 수 없는 op: ${step.op}`);
          continue;
        }
        result.steps.push({ ...rec, ok: true });
      } catch (e) {
        fail(rec, String(e.message ?? e).split('\n')[0]);
      }
    }
  } catch (e) {
    result.error = String(e.message ?? e);
  } finally {
    await browser.close();
  }
  result.ok = !result.error && result.steps.length > 0 && result.steps.every((s) => s.ok);
  result.status = result.ok ? 'passed' : 'failed';
  result.limits = ['헤드리스 Chromium 에서 입력을 눌러 본 결과다. 소리·체감 타격감·모바일 실기기는 확인하지 못한다.', '단언(expect)은 스크립트 작성자가 쓴 조건만 확인한다.'];
  return result;
}
