// G13 시나리오: 키보드·터치 입력, 콘솔 에러 0, 스크린샷, 계측 상태 변화, 일시정지. 결과는 @@RESULT@@ JSON 한 줄로 낸다.
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const out = (n) => path.join(cfg.out_dir, n);
const result = {};

async function run(mobile) {
  const s = await GameSession.open({
    pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile,
    viewport: mobile ? { width: 390, height: 844 } : { width: 800, height: 600 },
  });
  const r = {};
  const tag = mobile ? 'touch' : 'key';
  try {
    const s0 = await s.waitState((st) => st && st.ready && st.frames > 5, { label: 'boot' });
    r.initial = s0;
    r.captureBefore = await s.capture(out(tag + '-before.png'));
    if (!mobile) {
      await s.focusGame();
      // 키보드: 오른쪽으로 누르고 있으면 x 가 늘어난다.
      await s.page.keyboard.down('ArrowRight');
      const moved = await s.waitState((st) => st.x > s0.x + 40, { label: 'ArrowRight' });
      await s.page.keyboard.up('ArrowRight');
      r.keyMoved = { from: s0.x, to: moved.x };
      for (const key of ['ArrowDown', 'ArrowUp', 'ArrowLeft']) {
        await s.page.waitForTimeout(150);
        const before = await s.state();
        const moved = (st) => (key === 'ArrowDown' ? st.y > before.y + 20 : key === 'ArrowUp' ? st.y < before.y - 20 : st.x < before.x - 20);
        let after = null;
        for (let attempt = 0; attempt < 3 && !after; attempt += 1) {
          await s.page.keyboard.down(key);
          try { after = await s.waitState(moved, { label: key, timeoutMs: 3000 }); } catch { /* 키 이벤트가 프레임 사이에서 유실되면 한 번 더 누른다 */ }
          await s.page.keyboard.up(key);
          await s.page.waitForTimeout(100);
        }
        if (!after) throw new Error(key + ' 입력에 반응하지 않았다');
        r['key_' + key] = { before: [before.x, before.y], after: [after.x, after.y], attempts_note: 'up to 3' };
      }
      // 코인 쫓기: 상태를 읽고 코인 방향 키를 누르는 닫힌 루프.
      const startScore = (await s.state()).score;
      const deadline = Date.now() + 25000;
      let st = await s.state();
      while (st.score === startScore && Date.now() < deadline) {
        const want = { ArrowLeft: st.coin.x < st.x - 6, ArrowRight: st.coin.x > st.x + 6, ArrowUp: st.coin.y < st.y - 6, ArrowDown: st.coin.y > st.y + 6 };
        for (const [k, on] of Object.entries(want)) { if (on) await s.page.keyboard.down(k); else await s.page.keyboard.up(k); }
        await s.page.waitForTimeout(60);
        st = await s.state();
      }
      for (const k of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) await s.page.keyboard.up(k);
      r.score = { before: startScore, after: st.score };
      // 일시정지: frames 가 멈추고, 재개하면 다시 늘어난다.
      const p = await s.request('pause');
      await s.page.waitForTimeout(300);
      const f1 = (await s.state()).frames;
      await s.page.waitForTimeout(500);
      const f2 = (await s.state()).frames;
      const res = await s.request('resume');
      await s.page.waitForTimeout(500);
      const f3 = (await s.state()).frames;
      r.pause = { reply: p.paused, supported: p.supported, framesPaused: [f1, f2], resumeReply: res.paused, framesResumed: f3 };
      // 세이브 export/import
      const imp = await s.request('save-import', { data: JSON.stringify({ slot1: { score: 7 } }) });
      const exp = await s.request('save-export');
      r.save = { importOk: imp.ok, exported: exp.data };
    } else {
      // 터치: 코인 쪽을 탭하면 플레이어가 그쪽으로 이동한다.
      const st0 = await s.state();
      const b = await s.box();
      const fx = Math.min(0.9, st0.coin.x / st0.w);
      const fy = Math.min(0.9, st0.coin.y / st0.h);
      await s.tap(fx, fy);
      await s.page.waitForTimeout(80);
      const st1 = await s.waitState((st) => Math.abs(st.x - st0.x) > 15 || Math.abs(st.y - st0.y) > 15, { label: 'touch move' });
      r.touch = { tapFraction: [fx, fy], from: [st0.x, st0.y], to: [st1.x, st1.y], viewport: [Math.round(b.width), Math.round(b.height)] };
    }
    r.captureAfter = await s.capture(out(tag + '-after.png'));
    r.pageShot = await s.pageShot(out(tag + '-page.png'));
    r.final = await s.state();
  } catch (e) {
    r.error = String(e.stack ?? e);
  }
  r.console = s.collected;
  await s.close();
  return r;
}

result.desktop = await run(false);
result.mobile = await run(true);
emit(result);
