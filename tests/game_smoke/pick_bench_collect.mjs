// G29b: 벤치 게임의 오브젝트마다 실제 bridge pick 응답을 모은다. cfg: playwright_dir, host_port, play_url, sandbox, targets{키:[fx,fy]}.
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const s = await GameSession.open({ pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile: false, viewport: { width: 800, height: 900 } });
const out = {};
try {
  await s.waitState((st) => st && st.ready, { label: 'boot' });
  await s.page.waitForTimeout(1500);
  for (const [key, [x, y]] of Object.entries(cfg.targets)) {
    out[key] = await s.page.evaluate(([px, py]) => window.ttk.request('pick', { x: px, y: py }, 8000), [x, y]);
  }
} catch (e) {
  out.error = String(e.stack ?? e);
}
await s.close();
emit(out);
process.exit(0);
