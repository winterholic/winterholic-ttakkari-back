// G21 일반 smoke: 게임 규칙을 모른 채 어느 템플릿 게임에나 돌리는 시나리오(TASK-G21).
// 부팅(bridge 응답·그려진 화면), 입력 반응(계측 상태 또는 화면 변화), 일시정지, 세이브 내보내기, 콘솔 에러를 확인하고
// 확인하지 못한 것은 값을 지어내지 않고 'inconclusive'·'not_attempted' 로 낸다. 결과는 @@RESULT@@ JSON 한 줄이다.
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const out = (n) => path.join(cfg.out_dir, n);

const KEYS = ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Space', 'Enter', 'KeyD', 'KeyA', 'KeyW', 'KeyS'];
const TAPS = [[0.5, 0.5], [0.2, 0.8], [0.8, 0.8], [0.5, 0.2]];
const BLANK_SETTLE_MS = 8000; // 부팅 직후 단색 캡처를 "빈 화면"으로 확정하기 전에 기다리는 시간
const LIMITS = [
  '승리·종료 화면까지의 완주는 시도하지 않는다(규칙을 모르는 입력 탐색이라 도달 여부를 판정하지 않는다).',
  '화면이 계속 움직이는 게임은 화면 비교로 입력 반응을 판정할 수 없어 계측 상태(publishState)가 없으면 inconclusive 다.',
];


/** 두 계측 상태에서 값이 다른 최상위 키. */
function changedKeys(a, b) {
  const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  return [...keys].filter((k) => JSON.stringify((a ?? {})[k]) !== JSON.stringify((b ?? {})[k]));
}

async function pixelStats(s, dataUrl) {
  return await s.page.evaluate(async (url) => {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement('canvas');
    c.width = 64; c.height = 64;
    const x = c.getContext('2d');
    x.drawImage(img, 0, 0, 64, 64);
    const d = x.getImageData(0, 0, 64, 64).data;
    const counts = new Map();
    for (let i = 0; i < d.length; i += 4) {
      const k = (d[i] >> 4) * 256 + (d[i + 1] >> 4) * 16 + (d[i + 2] >> 4);
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    const top = Math.max(...counts.values());
    return { buckets: counts.size, dominantFraction: Math.round((top / 4096) * 1000) / 1000 };
  }, dataUrl);
}

async function snapshot(s, file) {
  const r = await s.request('capture');
  const png = Buffer.from(r.dataUrl.replace(/^data:image\/png;base64,/, ''), 'base64');
  const { createHash } = await import('node:crypto');
  const fs = await import('node:fs');
  if (file) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, png); }
  return { sha: createHash('sha256').update(png).digest('hex'), dataUrl: r.dataUrl, width: r.width, height: r.height, bytes: png.length };
}

async function safeState(s) {
  try { return await s.state(); } catch { return null; }
}

async function run(mobile) {
  const s = await GameSession.open({
    pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile,
    viewport: mobile ? { width: 390, height: 844 } : { width: 800, height: 600 },
  });
  const tag = mobile ? 'touch' : 'key';
  const r = { probes: [] };
  try {
    // 1. 부팅: bridge 가 응답하고 캡처가 나온다
    const bootEnd = Date.now() + 20000;
    let first = null;
    while (Date.now() < bootEnd && !first) {
      try { first = await snapshot(s, out(tag + '-boot.png')); } catch { await s.page.waitForTimeout(300); }
    }
    r.boot = { bridge: first !== null };
    if (!first) { r.console = s.collected; await s.close(); return r; }
    r.boot.capture = { bytes: first.bytes, width: first.width, height: first.height };
    // bridge 는 게임의 첫 프레임보다 먼저 응답할 수 있다(실제 worker 가 만든 게임에서 3회 중 2회 부팅 직후 단색 캡처를 확인).
    // 그 순간의 단색을 곧바로 "빈 화면"으로 판정하면 정상 게임이 간헐 실패하므로, 화면이 그려질 때까지 기다린 뒤에도 단색이면 실패로 본다.
    const isBlank = (st) => st.dominantFraction > 0.995 || st.buckets < 2;
    const firstStats = await pixelStats(s, first.dataUrl);
    r.renders = firstStats;
    r.renders.blank = isBlank(firstStats);
    r.renders.firstCaptureBlank = r.renders.blank;
    const settleStart = Date.now();
    while (r.renders.blank && Date.now() - settleStart < BLANK_SETTLE_MS) {
      await s.page.waitForTimeout(300);
      try {
        const again = await snapshot(s);
        r.renders = { ...(await pixelStats(s, again.dataUrl)), firstCaptureBlank: true };
        r.renders.blank = isBlank(r.renders);
        r.renders.settledMs = Date.now() - settleStart;
      } catch { /* 다음 반복에서 다시 시도한다 */ }
    }
    await s.page.waitForTimeout(800);
    const state0 = await safeState(s);
    r.boot.published = state0 !== null && typeof state0 === 'object' && Object.keys(state0).length > 0;
    r.boot.state = state0;

    // 2. 아무 입력이 없을 때의 기준선: 계속 바뀌는 값(프레임 수 등)과 계속 움직이는 화면을 입력 반응으로 오인하지 않기 위해
    const idleA = await snapshot(s);
    const stateA = await safeState(s);
    await s.page.waitForTimeout(500);
    const idleB = await snapshot(s);
    const stateB = await safeState(s);
    const noisyKeys = new Set(changedKeys(stateA, stateB));
    const idleAnimated = idleA.sha !== idleB.sha;
    r.baseline = { noisyKeys: [...noisyKeys], idleAnimated };

    // 3. 입력 탐색: 키(데스크톱) 또는 탭·스와이프(모바일)
    const inputs = mobile
      ? [...TAPS.map(([x, y]) => ({ name: 'tap@' + x + ',' + y, run: async () => { await s.tap(x, y); await s.page.waitForTimeout(250); } })),
         { name: 'swipe-right', run: async () => { await s.touchDown(0.3, 0.5); await s.touchMove(0.7, 0.5); await s.page.waitForTimeout(200); await s.touchUp(); } }]
      : KEYS.map((k) => ({ name: k, run: async () => { await s.page.keyboard.down(k); await s.page.waitForTimeout(350); await s.page.keyboard.up(k); await s.page.waitForTimeout(150); } }));
    if (!mobile) await s.focusGame();
    for (const input of inputs) {
      const beforeState = await safeState(s);
      const beforeShot = await snapshot(s);
      await input.run();
      const afterState = await safeState(s);
      const afterShot = await snapshot(s);
      const keys = changedKeys(beforeState, afterState).filter((k) => !noisyKeys.has(k));
      r.probes.push({ input: input.name, stateKeysChanged: keys, pixelsChanged: beforeShot.sha !== afterShot.sha });
    }
    const anyState = r.probes.some((p) => p.stateKeysChanged.length > 0);
    const anyPixels = r.probes.some((p) => p.pixelsChanged);
    r.inputEffect = anyState ? 'state' : !idleAnimated ? (anyPixels ? 'pixels' : 'none') : 'inconclusive';

    // 4. 일시정지·세이브
    try {
      const p = await s.request('pause');
      const q = await s.request('resume');
      r.pause = { supported: p.supported, paused: p.paused, resumedPaused: q.paused };
    } catch (e) { r.pause = { error: String(e).slice(0, 120) }; }
    try {
      const ex = await s.request('save-export');
      r.save = { exportOk: typeof ex.data === 'string' && typeof JSON.parse(ex.data) === 'object' };
    } catch (e) { r.save = { error: String(e).slice(0, 120) }; }
    await s.pageShot(out(tag + '-page.png'));
  } catch (e) {
    r.error = String(e.stack ?? e);
  }
  r.console = s.collected;
  await s.close();
  return r;
}

function verdict(r) {
  const problems = [];
  if (r.error) problems.push('실행 오류: ' + r.error.split('\n')[0]);
  if (!r.boot?.bridge) problems.push('bridge 가 응답하지 않아 부팅하지 못했다');
  if (r.renders?.blank) problems.push('부팅 후 화면이 비어 있다(단색)');
  const c = r.console ?? { consoleErrors: [], pageErrors: [], failedRequests: [], badResponses: [] };
  if (c.consoleErrors.length || c.pageErrors.length) problems.push('콘솔·페이지 에러 ' + (c.consoleErrors.length + c.pageErrors.length) + '건');
  if (c.failedRequests.length || c.badResponses.length) problems.push('실패한 요청 ' + (c.failedRequests.length + c.badResponses.length) + '건');
  if (r.inputEffect === 'none') problems.push('어떤 입력에도 상태·화면이 바뀌지 않았다');
  return { ok: problems.length === 0, problems, inputEffect: r.inputEffect ?? null };
}

const result = { limits: LIMITS };
result.desktop = await run(false);
result.mobile = await run(true);
result.desktop.verdict = verdict(result.desktop);
result.mobile.verdict = verdict(result.mobile);
result.ok = result.desktop.verdict.ok && result.mobile.verdict.ok;
emit(result);
