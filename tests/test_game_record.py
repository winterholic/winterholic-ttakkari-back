"""게임 템플릿의 플레이 녹화(ADR 0060, G30): 프레임 녹화기의 상한과 bridge 의 요청 검증·응답을 node 로 직접 실행해 확인한다.

시계·캔버스·프레임 생성기를 주입해 시간에 의존하지 않는다. 실제 canvas·브라우저 경로는 관제탑 E2E(front)에서 확인한다.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from game_browser import free_port, require_browser, run_scenario, serve_games
from test_game_build_snapshots import requires_runtime
from test_game_pick import build_game

from ttakkari.config import get_settings

SRC = Path(__file__).parent.parent / "src/ttakkari/games/templates/web-phaser/src"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")

SCRIPT = """
import { FrameRecorder, RECORD_LIMITS, clampFps } from %(record)s;
import { acceptParentMessage, installBridge } from %(bridge)s;

function fake(limits = RECORD_LIMITS, frameBytes = 1000) {
  const clock = { t: 0, fn: null, ms: 0,
    now() { return this.t; },
    setInterval(fn, ms) { this.fn = fn; this.ms = ms; return 1; },
    clearInterval() { this.fn = null; } };
  let canvas = {};
  const grab = () => ({ dataUrl: 'data:image/jpeg;base64,' + 'A'.repeat(Math.ceil(frameBytes * 4 / 3)), width: 480, height: 270 });
  const rec = new FrameRecorder(() => canvas, limits, clock, grab);
  const advance = (ms) => { // 간격마다 한 번씩 tick 을 부른다. 멈췄으면 더 부르지 않는다.
    const end = clock.t + ms;
    while (clock.fn && clock.t + clock.ms <= end) { clock.t += clock.ms; clock.fn(); }
    clock.t = Math.max(clock.t, end);
  };
  return { rec, clock, advance, setCanvas: (c) => { canvas = c; } };
}
const summary = (r) => ({ n: r.frames.length, reason: r.reason, durationMs: r.durationMs, fps: r.fps, width: r.width, height: r.height,
  first: r.frames[0]?.tMs ?? null, last: r.frames.at(-1)?.tMs ?? null, increasing: r.frames.every((f, i) => i === 0 || f.tMs > r.frames[i - 1].tMs) });

const out = {};

// 정상 녹화: 4fps 로 2초
{ const f = fake(); f.rec.start(4); out.activeAfterStart = f.rec.active; f.advance(2000); out.normal = summary(f.rec.stop()); out.activeAfterStop = f.rec.active; }
// fps 힌트는 상한으로 자른다
out.fps = { high: clampFps(100), zero: clampFps(0), nan: clampFps(NaN), str: clampFps('x'), frac: clampFps(2.6) };
// 길이 한도: 30초를 넘기면 녹화를 멈추고 사유를 남긴다(오래된 프레임을 버리지 않는다)
{ const f = fake(); f.rec.start(6); f.advance(40000); out.duration = summary(f.rec.stop()); out.durationActiveAfterLimit = f.rec.active; }
// 바이트 한도: 프레임당 1.1MB 면 5장까지만(6장째가 6MB 를 넘는다)
{ const f = fake(RECORD_LIMITS, Math.round(1.1 * 1024 * 1024)); f.rec.start(6); f.advance(10000); const r = f.rec.stop(); out.bytes = summary(r);
  out.bytesTotal = r.frames.reduce((a, x) => a + Math.floor((x.dataUrl.length - 'data:image/jpeg;base64,'.length) * 3 / 4), 0); }
// 프레임 수 한도
{ const f = fake({ ...RECORD_LIMITS, maxFrames: 5 }); f.rec.start(6); f.advance(5000); out.frames = summary(f.rec.stop()); }
// 캔버스가 없으면 빈 결과와 사유
{ const f = fake(); f.setCanvas(null); f.rec.start(4); out.noCanvas = summary(f.rec.stop()); }
// 녹화 중이 아닐 때 stop 은 빈 결과
{ const f = fake(); out.idleStop = summary(f.rec.stop()); }
// 다시 시작하면 이전 프레임은 버린다
{ const f = fake(); f.rec.start(4); f.advance(1000); f.rec.start(4); f.advance(500); out.restart = summary(f.rec.stop()); }
// stop 뒤 다시 stop 하면 비어 있다(프레임은 한 번만 돌려준다)
{ const f = fake(); f.rec.start(4); f.advance(1000); f.rec.stop(); out.secondStop = summary(f.rec.stop()); }

// bridge 요청 검증
const parent = {}, other = {}; const pwa = 'https://pwa.example';
const msg = (data, o = {}) => acceptParentMessage({ origin: pwa, source: parent, data, ...o }, parent, pwa);
out.accept = {
  start: msg({ type: 'ttakkari:record-start', id: 'r1' }),
  startFps: msg({ type: 'ttakkari:record-start', id: 'r2', fps: 5 }),
  startBadFps: msg({ type: 'ttakkari:record-start', id: 'r3', fps: '5; drop' }),
  startNaN: msg({ type: 'ttakkari:record-start', id: 'r4', fps: null }),
  stop: msg({ type: 'ttakkari:record-stop', id: 'r5' }),
  wrongOrigin: msg({ type: 'ttakkari:record-start', id: 'r6' }, { origin: 'https://evil.example' }),
  wrongSource: msg({ type: 'ttakkari:record-stop', id: 'r7' }, { source: other }),
};

// installBridge: 응답은 부모 origin 으로만, 캔버스가 없는 게임은 사유와 함께 빈 결과
{
  const sent = []; const listeners = [];
  const p = { postMessage: (m, o) => sent.push({ m, o }) };
  globalThis.window = { location: { hash: '#ttk-parent=' + encodeURIComponent(pwa) }, parent: p, addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); } };
  installBridge(() => null, {});
  listeners[0]({ origin: 'https://evil.example', source: p, data: { type: 'ttakkari:record-start', id: 'x1' } });
  listeners[0]({ origin: pwa, source: p, data: { type: 'ttakkari:record-start', id: 'x2' } });
  listeners[0]({ origin: pwa, source: p, data: { type: 'ttakkari:record-stop', id: 'x3' } });
  out.bridge = sent.filter((s) => s.m.type && s.m.type.startsWith('ttakkari:record')).map((s) => ({ type: s.m.type, id: s.m.id, o: s.o, supported: s.m.supported, reason: s.m.reason, n: s.m.frames?.length }));
  out.limits = sent.find((s) => s.m.type === 'ttakkari:record:started')?.m.limits;
}
console.log(JSON.stringify(out));
process.exit(0);
"""


def run() -> dict:
    script = SCRIPT % {"record": json.dumps((SRC / "record.ts").as_uri()), "bridge": json.dumps((SRC / "bridge.ts").as_uri())}
    proc = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-1200:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def got() -> dict:
    return run()


@requires_node
def test_recorder_collects_frames_at_the_requested_rate_and_returns_them_once(got):
    n = got["normal"]
    assert n["reason"] == "stopped" and n["fps"] == 4 and n["increasing"]
    assert 8 <= n["n"] <= 10 and n["first"] == 0 and 1750 <= n["last"] <= 2000
    assert n["durationMs"] == 2000 and (n["width"], n["height"]) == (480, 270)
    assert got["activeAfterStart"] is True and got["activeAfterStop"] is False
    assert got["secondStop"]["n"] == 0  # 프레임은 한 번만 돌려준다
    assert got["restart"]["n"] <= 6 and got["restart"]["first"] == 0  # 다시 시작하면 이전 프레임은 버린다


@requires_node
def test_recorder_fps_hint_is_clamped_to_the_limit(got):
    assert got["fps"] == {"high": 6, "zero": 1, "nan": 4, "str": 4, "frac": 3}


@requires_node
def test_recorder_stops_with_a_reason_instead_of_silently_truncating(got):
    d = got["duration"]
    assert d["reason"] == "limit_duration" and d["durationMs"] <= 31000 and d["n"] > 0 and d["last"] <= 30000
    assert got["durationActiveAfterLimit"] is False
    b = got["bytes"]
    assert b["reason"] == "limit_bytes" and b["n"] == 5 and got["bytesTotal"] <= 6 * 1024 * 1024
    assert got["frames"]["reason"] == "limit_frames" and got["frames"]["n"] == 5


@requires_node
def test_recorder_without_canvas_or_recording_returns_an_empty_result(got):
    assert got["noCanvas"]["reason"] == "canvas_unavailable" and got["noCanvas"]["n"] == 0
    assert got["idleStop"]["n"] == 0 and got["idleStop"]["durationMs"] == 0


@requires_node
def test_bridge_accepts_record_requests_only_from_the_declared_parent(got):
    a = got["accept"]
    assert a["start"] == {"type": "ttakkari:record-start", "id": "r1"}
    assert a["startFps"] == {"type": "ttakkari:record-start", "id": "r2", "fps": 5}
    assert a["startBadFps"] == {"type": "ttakkari:record-start", "id": "r3"} and a["startNaN"] == {"type": "ttakkari:record-start", "id": "r4"}
    assert a["stop"] == {"type": "ttakkari:record-stop", "id": "r5"}
    assert a["wrongOrigin"] is None and a["wrongSource"] is None


@requires_node
def test_bridge_replies_only_to_the_parent_origin(got):
    assert got["bridge"] == [
        {"type": "ttakkari:record:started", "id": "x2", "o": "https://pwa.example", "supported": True},
        {"type": "ttakkari:record:result", "id": "x3", "o": "https://pwa.example", "supported": True, "reason": "canvas_unavailable", "n": 0},
    ]  # 위조 origin 의 x1 에는 답하지 않는다
    assert got["limits"] == {"maxMs": 30000, "maxFps": 6, "maxWidth": 480, "maxBytes": 6 * 1024 * 1024}


# 실제 Phaser 게임 + Chromium: 격리 빌드(템플릿 tsc·vite 통과 포함)한 게임에서 프레임이 실제로 모이는지 확인한다.
@requires_runtime
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile390x844"])
async def test_real_game_records_jpeg_frames_within_limits(client, auth, monkeypatch, tmp_path, mobile):
    require_browser()
    base = os.environ.get("TTAKKARI_GAME_EVIDENCE_DIR")
    out = (Path(base) / "g30-record") if base else tmp_path / "g30-record"
    out.mkdir(parents=True, exist_ok=True)
    game = await build_game(client, auth, "record-real-" + ("m" if mobile else "d"), "smoke_game_main.ts")
    game_port, host_port = free_port(), free_port()
    host_origin = f"http://127.0.0.1:{host_port}"
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", f"127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_origin_url", f"http://127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_frame_ancestors", [host_origin])
    play = (await client.post(f"/api/games/{game['id']}/play", json={}, headers={**auth, "Origin": host_origin})).json()
    with serve_games(game_port, [host_origin]):
        res = run_scenario("record_scenario.mjs", {"play_url": play["url"], "sandbox": play["sandbox"], "host_port": host_port, "out_dir": str(out), "mobile": mobile, "ms": 2000})
    (out / f"result-{'mobile' if mobile else 'desktop'}.json").write_text(json.dumps({k: v for k, v in res.items() if k != "times"}, ensure_ascii=False, indent=1))
    assert res.get("error") is None, res.get("error")
    assert res["console"]["consoleErrors"] == [] and res["console"]["pageErrors"] == [], res["console"]
    assert res["started"]["supported"] is True and res["started"]["active"] is True
    r = res["result"]
    assert r["reason"] == "stopped" and r["supported"] is True and r["fps"] == 6
    assert 8 <= r["n"] <= 14 and 1800 <= r["durationMs"] <= 3500, r  # 6fps x 2초 안팎
    assert r["width"] <= 480 and r["height"] > 0 and r["n"] <= 180
    assert res["times"] == sorted(res["times"]) and res["times"][0] == 0
    assert res["jpeg"] is True and res["prefixOk"] is True  # 실제 JPEG 바이트(SOI/EOI)
    assert 0 < res["totalBytes"] <= 6 * 1024 * 1024
    assert res["distinct"] >= 2  # 움직이는 게임이라 프레임이 서로 다르다
    assert res["second"] == {"n": 0, "reason": "stopped"}  # 프레임은 한 번만 돌려준다
