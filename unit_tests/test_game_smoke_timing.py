"""Exercise the actual generic smoke with a deterministic, throttled game frame clock.

No browser is started. This tests input sampling, not Playwright or Phaser integration.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCENARIO = Path(__file__).resolve().parents[1] / "tests/game_smoke/smoke_generic.mjs"

DRIVER = r"""
let clock = 0;
Date.now = () => clock;
export function loadPlaywright() { return {}; }
export function emit(result) { console.log('@@RESULT@@' + JSON.stringify(result)); }
export class GameSession {
  static async open() { return new GameSession(); }
  constructor() {
    this.x = 0;
    this.frames = 0;
    this.nextFrame = clock + 200;
    this.held = new Set();
    this.collected = { consoleErrors: [], pageErrors: [], failedRequests: [], badResponses: [] };
    this.page = {
      keyboard: {
        down: async key => { this.held.add(key); },
        up: async key => { this.held.delete(key); },
      },
      evaluate: async () => ({ buckets: 4, dominantFraction: 0.4 }),
      waitForTimeout: async ms => {
        const end = clock + ms;
        while (this.nextFrame <= end) {
          this.frames++;
          if (process.env.SMOKE_RESPONDS === '1' && this.held.size) this.x++;
          this.nextFrame += 500;
        }
        clock = end;
      },
    };
  }
  async state() {
    const state = { ready: true, frames: this.frames };
    if (process.env.SMOKE_PUBLISH_POSITION === '1') state.x = this.x;
    return state;
  }
  async request(type) {
    if (type === 'capture') return {
      dataUrl: 'data:image/png;base64,' + Buffer.from('frame-' + this.x).toString('base64'),
      width: 800, height: 600,
    };
    if (type === 'save-export') return { data: '{}' };
    return { supported: true, paused: type === 'pause' };
  }
  async focusGame() {}
  async tap() { if (process.env.SMOKE_RESPONDS === '1') this.x++; }
  async touchDown() { await this.tap(); }
  async touchMove() { await this.tap(); }
  async touchUp() {}
  async pageShot() {}
  async close() {
    if (this.held.size) throw new Error('keyboard remained held after probing');
  }
}
"""


@pytest.mark.parametrize("responds", [True, False], ids=["slow-responsive", "static-negative-control"])
@pytest.mark.parametrize("publish_position", [True, False], ids=["state", "pixels"])
def test_generic_smoke_observes_input_on_a_throttled_frame_clock(
        tmp_path, monkeypatch, responds, publish_position):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the smoke sampling regression")
    shutil.copyfile(SCENARIO, tmp_path / SCENARIO.name)
    (tmp_path / "ttk_driver.mjs").write_text(DRIVER, encoding="utf-8")
    monkeypatch.setenv("SMOKE_RESPONDS", "1" if responds else "0")
    monkeypatch.setenv("SMOKE_PUBLISH_POSITION", "1" if publish_position else "0")
    result = subprocess.run(
        [node, str(tmp_path / SCENARIO.name), json.dumps({"out_dir": str(tmp_path / "evidence")})],
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    body = json.loads(next(line.removeprefix("@@RESULT@@") for line in result.stdout.splitlines()
                           if line.startswith("@@RESULT@@")))
    for mode in ("desktop", "mobile"):
        observed = body[mode]
        assert observed["verdict"]["ok"] is responds, observed
        effect = ("state" if publish_position else "pixels") if responds else "none"
        assert observed["inputEffect"] == effect, observed
        assert "frames" in observed["baseline"]["noisyKeys"]
    assert body["ok"] is responds
