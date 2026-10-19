// 게임 브라우저 검증 공용 드라이버(TASK-G13·G14). 실제 관제탑과 같은 구조를 흉내 낸다:
// 부모 페이지(PWA 역할, 다른 origin) 안에 play URL 을 sandbox iframe 으로 열고, 모든 상태·캡처·일시정지는 postMessage bridge 로만 읽는다.
// Playwright 모듈 위치는 설정의 playwright_dir(기본 back/.browser-runtime)에서 찾는다.
import { createRequire } from 'node:module';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

export function loadPlaywright(dir) {
  const req = createRequire(path.join(dir, 'noop.js'));
  return req('playwright');
}

function cachedBrowsers() {
  const root = path.join(os.homedir(), 'Library/Caches/ms-playwright');
  const found = [];
  if (!fs.existsSync(root)) return found;
  for (const d of fs.readdirSync(root).sort().reverse()) {
    for (const rel of ['chrome-headless-shell-mac-arm64/chrome-headless-shell', 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing']) {
      const p = path.join(root, d, rel);
      if (d.startsWith('chromium') && fs.existsSync(p)) found.push(p);
    }
  }
  return found;
}

export async function launchBrowser(chromium) {
  const attempts = [];
  if (process.env.TTAKKARI_BROWSER_EXECUTABLE) attempts.push({ executablePath: process.env.TTAKKARI_BROWSER_EXECUTABLE });
  attempts.push({}, { channel: 'chrome' }, ...cachedBrowsers().map((executablePath) => ({ executablePath })));
  const errors = [];
  for (const opts of attempts) {
    try {
      return await chromium.launch({ headless: true, ...opts });
    } catch (e) {
      errors.push(String(e).split('\n')[0]);
    }
  }
  throw new Error('브라우저를 실행할 수 없다: ' + errors.join(' | '));
}

function hostHtml(src, sandbox, title) {
  return [
    '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
    '<title>' + title + '</title><style>html,body{margin:0;background:#0b0e14;height:100%}#wrap{width:100vw;height:100vh}iframe{border:0;width:100%;height:100%;display:block}</style></head>',
    '<body><div id="wrap"><iframe id="game" title="game" src="' + src + '" sandbox="' + sandbox + '" allow="fullscreen; autoplay; gamepad"></iframe></div>',
    '<script>',
    '// 관제탑 부모의 최소 구현: source 가 우리 iframe 이고 id 가 맞는 응답만 받는다.',
    'const frame = document.getElementById("game");',
    'window.ttk = {',
    '  request(type, extra = {}, timeoutMs = 8000) {',
    '    const id = Math.random().toString(16).slice(2) + Date.now().toString(16);',
    '    return new Promise((resolve, reject) => {',
    '      const onMsg = (ev) => {',
    '        if (ev.source !== frame.contentWindow) return;',
    '        const d = ev.data;',
    '        if (!d || d.id !== id) return;',
    '        cleanup(); resolve(d);',
    '      };',
    '      const send = () => frame.contentWindow && frame.contentWindow.postMessage({ type: "ttakkari:" + type, id, ...extra }, "*");',
    '      const cleanup = () => { window.removeEventListener("message", onMsg); clearInterval(t); clearTimeout(to); };',
    '      window.addEventListener("message", onMsg);',
    '      send();',
    '      const t = setInterval(send, 400);',
    '      const to = setTimeout(() => { cleanup(); reject(new Error("bridge timeout: " + type)); }, timeoutMs);',
    '    });',
    '  },',
    '};',
    '</script></body></html>',
  ].join('\n');
}

/** 부모 페이지를 서빙한다(게임 origin 과 다른 포트 = 다른 origin). */
export function startHost(port, src, sandbox, title = 'ttakkari host') {
  const server = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(hostHtml(src, sandbox, title));
  });
  return new Promise((resolve) => server.listen(port, '127.0.0.1', () => resolve(server)));
}

export class GameSession {
  constructor(page, iframeEl, collected) {
    this.page = page;
    this.iframeEl = iframeEl;
    this.collected = collected;
  }

  static async open({ pw, hostPort, playUrl, sandbox, mobile = false, viewport = { width: 390, height: 844 }, hostOrigin = null }) {
    const browser = await launchBrowser(pw.chromium);
    const ctx = await browser.newContext({ viewport, hasTouch: mobile, isMobile: mobile, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const collected = { consoleErrors: [], pageErrors: [], failedRequests: [], badResponses: [] };
    // 게임은 sandbox iframe 이라 frame 의 콘솔도 page 이벤트로 올라온다.
    // 부모(front) 페이지가 스스로 내는 오류(service worker 등록 등)는 게임 검증 대상이 아니라 제외한다.
    page.on('console', (m) => { if (m.type() === 'error' && !(hostOrigin && (m.location().url.startsWith(hostOrigin) || /unsupported MIME type|status of 401/.test(m.text())))) collected.consoleErrors.push(m.text()); });
    page.on('pageerror', (e) => { if (!(hostOrigin && String(e).includes(hostOrigin))) collected.pageErrors.push(String(e)); });
    page.on('requestfailed', (r) => collected.failedRequests.push(r.method() + ' ' + r.url() + ' ' + (r.failure() ? r.failure().errorText : '')));
    page.on('response', (r) => { if (r.status() >= 400 && !(hostOrigin && (r.url().startsWith(hostOrigin) || r.url().includes('/api/auth/')))) collected.badResponses.push(r.status() + ' ' + r.url()); });
    let host = null;
    if (hostOrigin) {
      // 이미 떠 있는 서버(예: 격리 front)의 origin 을 부모 origin 으로 쓴다: 그 주소의 한 경로만 가로채 부모 페이지를 내준다.
      // (route 로 가로챈 응답은 Chrome 의 로컬 네트워크 접근 검사에서 공개 주소 취급이라 막히므로, 실제 loopback 응답 문서를 열고 내용만 바꾼다.)
      await page.goto(hostOrigin + '/favicon.svg', { waitUntil: 'load' }).catch(() => {});
      await page.evaluate((html) => { document.open(); document.write(html); document.close(); }, hostHtml(playUrl, sandbox, 'ttakkari host'));
      await page.waitForLoadState('load');
    } else {
      host = await startHost(hostPort, playUrl, sandbox);
      await page.goto('http://127.0.0.1:' + hostPort + '/', { waitUntil: 'load' });
    }
    const s = new GameSession(page, await page.$('#game'), collected);
    s.browser = browser;
    s.host = host;
    return s;
  }

  request(type, extra) { return this.page.evaluate(([t, e]) => window.ttk.request(t, e), [type, extra ?? {}]); }
  async state() { return (await this.request('state')).state; }

  async waitState(pred, { timeoutMs = 10000, label = 'state' } = {}) {
    const end = Date.now() + timeoutMs;
    let last = null;
    while (Date.now() < end) {
      try { last = await this.state(); if (pred(last)) return last; } catch { /* bridge 가 아직 없다 */ }
      await this.page.waitForTimeout(100);
    }
    throw new Error(label + ' 조건을 만족하지 못했다. 마지막 상태: ' + JSON.stringify(last));
  }

  async box() { return this.iframeEl.boundingBox(); }
  async focusGame() { const b = await this.box(); await this.page.mouse.click(b.x + b.width / 2, b.y + b.height - 4); }
  async tap(fx, fy) { const b = await this.box(); await this.page.touchscreen.tap(b.x + b.width * fx, b.y + b.height * fy); }

  /** 손가락을 누른 채 유지한다(CDP 터치 이벤트). tap 은 즉시 떼므로 '누르고 있는 방향으로 이동' 입력에는 이것을 쓴다. */
  async touchDown(fx, fy, id = 1) {
    const b = await this.box();
    this.cdp = this.cdp ?? (await this.page.context().newCDPSession(this.page));
    await this.cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: b.x + b.width * fx, y: b.y + b.height * fy, id }] });
  }

  async touchMove(fx, fy, id = 1) {
    const b = await this.box();
    await this.cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: b.x + b.width * fx, y: b.y + b.height * fy, id }] });
  }

  async touchUp() {
    if (this.cdp) await this.cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  }

  /** bridge 캡처(게임이 직접 만든 canvas PNG)를 파일로 저장하고 {path, bytes, sha256, width, height} 를 돌려준다. */
  async capture(file) {
    const r = await this.request('capture');
    const buf = Buffer.from(r.dataUrl.replace(/^data:image\/png;base64,/, ''), 'base64');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, buf);
    return { path: file, bytes: buf.length, png: buf.subarray(0, 8).toString('hex') === '89504e470d0a1a0a', sha256: crypto.createHash('sha256').update(buf).digest('hex'), width: r.width, height: r.height };
  }

  /** 페이지 화면 전체 스크린샷(부모 페이지 + iframe). */
  async pageShot(file) { fs.mkdirSync(path.dirname(file), { recursive: true }); await this.page.screenshot({ path: file }); return file; }

  async close() { await this.browser.close(); if (this.host) await new Promise((r) => this.host.close(r)); }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export function emit(result) { process.stdout.write('\n@@RESULT@@' + JSON.stringify(result) + '\n'); }
