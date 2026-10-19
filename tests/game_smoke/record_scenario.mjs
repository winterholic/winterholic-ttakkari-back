// G30 시나리오: 실제 브라우저의 sandbox iframe 안 Phaser 게임에 bridge record-start/stop 을 보내 프레임이 실제로 모이는지 확인한다.
// cfg: playwright_dir, host_port, play_url, sandbox, out_dir, mobile(선택), ms(녹화 시간, 기본 2000). 결과는 @@RESULT@@ JSON 한 줄이다.
import fs from 'node:fs';
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);

const call = (s, type, extra, timeoutMs = 20000) => s.page.evaluate(([t, e, ms]) => window.ttk.request(t, e, ms), [type, extra, timeoutMs]);

const R = {};
const s = await GameSession.open({
  pw, hostPort: cfg.host_port, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile: !!cfg.mobile,
  viewport: cfg.mobile ? { width: 390, height: 844 } : { width: 800, height: 600 },
});
try {
  await s.waitState((x) => x && x.ready && x.frames > 3, { label: 'boot' });
  const started = await call(s, 'record-start', { fps: 6 });
  R.started = { supported: started.supported, active: started.active, limits: started.limits };
  // 녹화 중 플레이어를 움직여 프레임이 서로 달라지게 한다(smoke_game_main: 방향키 이동)
  await s.focusGame();
  await s.page.keyboard.down('ArrowRight');
  await s.page.waitForTimeout(cfg.ms ?? 2000);
  await s.page.keyboard.up('ArrowRight');
  const r = await call(s, 'record-stop', {});
  R.result = { supported: r.supported, reason: r.reason, fps: r.fps, width: r.width, height: r.height, durationMs: r.durationMs, n: r.frames.length };
  R.times = r.frames.map((f) => f.tMs);
  const bytes = r.frames.map((f) => Buffer.from(f.dataUrl.slice(f.dataUrl.indexOf(',') + 1), 'base64'));
  R.jpeg = bytes.every((b) => b[0] === 0xff && b[1] === 0xd8 && b[b.length - 2] === 0xff && b[b.length - 1] === 0xd9);
  R.prefixOk = r.frames.every((f) => f.dataUrl.startsWith('data:image/jpeg;base64,'));
  R.totalBytes = bytes.reduce((a, b) => a + b.length, 0);
  R.distinct = new Set(r.frames.map((f) => f.dataUrl)).size; // 게임이 움직이면 프레임이 서로 달라야 한다(빈 화면 반복이 아님)
  fs.mkdirSync(cfg.out_dir, { recursive: true });
  if (bytes.length) {
    const tag = cfg.mobile ? 'mobile' : 'desktop';
    fs.writeFileSync(path.join(cfg.out_dir, `record-${tag}-first.jpg`), bytes[0]);
    fs.writeFileSync(path.join(cfg.out_dir, `record-${tag}-last.jpg`), bytes[bytes.length - 1]);
  }
  R.second = await call(s, 'record-stop', {}).then((x) => ({ n: x.frames.length, reason: x.reason }));
} catch (e) {
  R.error = String(e.stack ?? e);
}
R.console = s.collected;
await s.close();
emit(R);
