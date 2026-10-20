// G31: 실제 bridge record-start/stop 으로 녹화해 JPEG 프레임 전체를 파일로 저장한다.
// cfg: playwright_dir, host_port, play_url, sandbox, out_dir, fps(기본 2), ms(기본 6000), tag(기본 'rec'). 결과는 @@RESULT@@ JSON 한 줄이다.
import fs from 'node:fs';
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const call = (s, type, extra, timeoutMs = 20000) => s.page.evaluate(([t, e, ms]) => window.ttk.request(t, e, ms), [type, extra, timeoutMs]);

const R = {};
const s = await GameSession.open({
  pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile: false, viewport: { width: 800, height: 600 },
});
try {
  await s.waitState((x) => x && x.ready && x.frames > 3, { label: 'boot' });
  await s.page.waitForTimeout(400);
  const started = await call(s, 'record-start', { fps: cfg.fps ?? 2 });
  R.started = { supported: started.supported, active: started.active };
  await s.page.waitForTimeout(cfg.ms ?? 6000);
  const r = await call(s, 'record-stop', {});
  R.result = { supported: r.supported, reason: r.reason, fps: r.fps, width: r.width, height: r.height, durationMs: r.durationMs, n: r.frames.length };
  const bytes = r.frames.map((f) => Buffer.from(f.dataUrl.slice(f.dataUrl.indexOf(',') + 1), 'base64'));
  R.jpeg = bytes.every((b) => b[0] === 0xff && b[1] === 0xd8 && b[b.length - 2] === 0xff && b[b.length - 1] === 0xd9);
  R.totalBytes = bytes.reduce((a, b) => a + b.length, 0);
  R.distinct = new Set(r.frames.map((f) => f.dataUrl)).size;
  fs.mkdirSync(cfg.out_dir, { recursive: true });
  const tag = cfg.tag ?? 'rec';
  R.files = r.frames.map((f, i) => {
    const name = `${tag}-${String(i).padStart(2, '0')}.jpg`;
    fs.writeFileSync(path.join(cfg.out_dir, name), bytes[i]);
    return { file: name, t_ms: f.tMs };
  });
} catch (e) {
  R.error = String(e.stack ?? e);
}
R.console = s.collected;
await s.close();
emit(R);
process.exit(0);
