// G27 시나리오: 실제 브라우저의 sandbox iframe 안 Phaser 게임에 bridge pick 요청을 보내 오브젝트 정보를 확인한다.
// cfg: playwright_dir, host_port, play_url, sandbox, out_dir, expect_supported(기본 true). 결과는 @@RESULT@@ JSON 한 줄이다.
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const out = (n) => path.join(cfg.out_dir, n);

async function pick(s, x, y, timeoutMs = 8000) {
  return s.page.evaluate(([px, py, t]) => window.ttk.request('pick', { x: px, y: py }, t), [x, y, timeoutMs]);
}

async function run(mobile) {
  const s = await GameSession.open({
    pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile,
    viewport: mobile ? { width: 390, height: 844 } : { width: 800, height: 600 },
  });
  const r = {};
  try {
    if (cfg.expect_supported === false) {
      await s.waitState((st) => st && st.ready, { label: 'boot' });
      r.unsupported = await pick(s, 0.5, 0.5);
    } else {
      const st = await s.waitState((x) => x && x.ready && x.frames > 3 && x.at, { label: 'boot' });
      const frac = (name) => { const [ax, ay] = st.at[name]; return [ax / st.w, ay / st.h]; };
      r.size = [st.w, st.h];
      for (const name of ['player', 'shadowEdge', 'coin', 'label', 'crate', 'ghost', 'heart', 'empty']) {
        const [fx, fy] = frac(name);
        r[name] = await pick(s, fx, fy);
      }
      // 규약 위반 입력은 게임이 조용히 무시한다(응답이 오지 않아 시간 초과).
      r.rejected = {};
      for (const [k, x, y] of [['out_of_range', 2, 0.5], ['negative', -0.1, 0.5], ['nan', 'a', 0.5]]) {
        r.rejected[k] = await pick(s, x, y, 1500).then(() => 'replied', () => 'ignored');
      }
      r.capture = await s.capture(out((mobile ? 'mobile' : 'desktop') + '-pick.png'));
      r.state = await s.state();
    }
  } catch (e) {
    r.error = String(e.stack ?? e);
  }
  r.console = s.collected;
  await s.close();
  return r;
}

const result = { desktop: await run(false), mobile: await run(true) };
emit(result);
