"""게임 오브젝트 픽킹 bridge (TASK-G27, ADR 0059): 부모가 화면 비율 좌표로 `ttakkari:pick` 을 보내면 게임이 그 지점의
오브젝트 정보(이름·종류·텍스처 키·화면 경계)를 돌려준다. 요청 검증은 node 로 순수 함수를, 동작은 실제 Phaser·격리 빌드·Chromium 으로 확인한다.

컨테이너 런타임 또는 브라우저 자동화가 없으면 skip 한다(TTAKKARI_REQUIRE_GAME_RUNTIME/BROWSER=1 이면 실패).
증거는 TTAKKARI_GAME_EVIDENCE_DIR/g27-pick 에 남는다.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from game_browser import (
    SCENARIOS,
    free_port,
    require_browser,
    run_scenario,
    serve_games,
)
from helpers import fresh_dir
from test_api_games import register, wait_build
from test_game_build_snapshots import (  # noqa: F401
    _cleanup_snapshots,
    git,
    manifest,
    requires_runtime,
)

from ttakkari.config import get_settings
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.contract import game_dir

BRIDGE = PhaserAdapter.TEMPLATE / "src" / "bridge.ts"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")

SCRIPT = """
import { acceptParentMessage } from %s;
const parent = {};
const msg = (data) => acceptParentMessage({ origin: 'https://pwa.example', source: parent, data }, parent, 'https://pwa.example');
console.log(JSON.stringify({
  ok: msg({ type: 'ttakkari:pick', id: 'p1', x: 0.25, y: 0.75 }),
  edges: msg({ type: 'ttakkari:pick', id: 'p2', x: 0, y: 1 }),
  extra_ignored: msg({ type: 'ttakkari:pick', id: 'p3', x: 0.5, y: 0.5, eval: 'x' }),
  no_xy: msg({ type: 'ttakkari:pick', id: 'p4' }),
  too_big: msg({ type: 'ttakkari:pick', id: 'p5', x: 1.01, y: 0.5 }),
  negative: msg({ type: 'ttakkari:pick', id: 'p6', x: -0.01, y: 0.5 }),
  string_x: msg({ type: 'ttakkari:pick', id: 'p7', x: '0.5', y: 0.5 }),
  nan: msg({ type: 'ttakkari:pick', id: 'p8', x: Number.NaN, y: 0.5 }),
  infinity: msg({ type: 'ttakkari:pick', id: 'p9', x: Number.POSITIVE_INFINITY, y: 0.5 }),
  capture_unchanged: msg({ type: 'ttakkari:capture', id: 'c1', x: 0.5, y: 0.5 }),
}));
"""


@requires_node
def test_bridge_pick_request_accepts_only_normalized_finite_coordinates():
    out = subprocess.run(["node", "--input-type=module", "-e", SCRIPT % json.dumps(BRIDGE.as_uri())], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr[-800:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["ok"] == {"type": "ttakkari:pick", "id": "p1", "x": 0.25, "y": 0.75}
    assert got["edges"] == {"type": "ttakkari:pick", "id": "p2", "x": 0, "y": 1}
    assert got["extra_ignored"] == {"type": "ttakkari:pick", "id": "p3", "x": 0.5, "y": 0.5}  # 알려진 필드만 통과시킨다
    for rejected in ("no_xy", "too_big", "negative", "string_x", "nan", "infinity"):
        assert got[rejected] is None, rejected
    assert got["capture_unchanged"] == {"type": "ttakkari:capture", "id": "c1"}  # 다른 요청에 좌표가 섞여 들어가지 않는다


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    base = os.environ.get("TTAKKARI_GAME_EVIDENCE_DIR")
    d = Path(base) / "g27-pick" if base else tmp_path / "g27-pick"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def build_game(client, auth, slug: str, fixture: str) -> dict:
    root = fresh_dir(game_dir(slug))
    await PhaserAdapter().scaffold_project(root, manifest(slug))
    (root / "src" / "main.ts").write_text((SCENARIOS / fixture).read_text())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"pick fixture: {fixture}")
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    started = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    done = await wait_build(client, auth, game["id"], started.json()["id"], timeout=600)
    assert done["status"] == "succeeded", done["error"]  # 템플릿 pick.ts·bridge.ts 가 tsc 와 vite 를 통과한다
    return game


async def run_pick_scenario(client, auth, monkeypatch, game: dict, out: Path, **extra) -> dict:
    game_port, host_port = free_port(), free_port()
    host_origin = f"http://127.0.0.1:{host_port}"
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", f"127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_origin_url", f"http://127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_frame_ancestors", [host_origin])
    play = (await client.post(f"/api/games/{game['id']}/play", json={}, headers={**auth, "Origin": host_origin})).json()
    assert urlsplit(play["url"]).netloc == f"127.0.0.1:{game_port}" and "allow-same-origin" not in play["sandbox"]
    with serve_games(game_port, [host_origin]):
        return run_scenario("pick_scenario.mjs", {"play_url": play["url"], "sandbox": play["sandbox"], "host_port": host_port, "out_dir": str(out), **extra})


def contains(bounds: dict, x: float, y: float) -> bool:
    return bounds["x"] <= x <= bounds["x"] + bounds["width"] and bounds["y"] <= y <= bounds["y"] + bounds["height"]


@requires_runtime
async def test_real_phaser_game_answers_pick_with_object_info(client, auth, evidence, monkeypatch):
    require_browser()
    game = await build_game(client, auth, "pick-real", "smoke_game_pick.ts")
    res = await run_pick_scenario(client, auth, monkeypatch, game, evidence)
    (evidence / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    for mode in ("desktop", "mobile"):
        r = res[mode]
        assert "error" not in r, (mode, r.get("error"), r["console"])
        assert r["console"]["consoleErrors"] == [] and r["console"]["pageErrors"] == [], r["console"]
        w, h = r["size"]
        for name in ("player", "shadowEdge", "coin", "label", "crate", "ghost", "heart", "empty"):
            assert r[name]["supported"] is True and r[name]["canvas"]["width"] > 0, (mode, name, r[name])

        player = r["player"]["hits"]  # 겹친 오브젝트는 위에 그려진 것부터
        assert [x["name"] for x in player] == ["player", "shadow"], player
        assert player[0]["type"] == "Rectangle" and player[0]["scene"] == "pickdemo" and player[0]["container"] is None
        assert player[0]["depth"] > player[1]["depth"]
        for hit in player:
            assert contains(hit["bounds"], r["player"]["point"]["x"], r["player"]["point"]["y"]), hit  # 좌표를 포함하는 화면 비율 경계
            assert all(0 <= hit["bounds"][k] <= 1 for k in ("x", "y")) and hit["bounds"]["width"] > 0
        assert [x["name"] for x in r["shadowEdge"]["hits"]] == ["shadow"]  # 가장자리는 아래 오브젝트만
        coin = r["coin"]["hits"]
        assert [x["name"] for x in coin] == ["coin"] and coin[0]["type"] == "Arc"
        label = r["label"]["hits"]
        assert [x["name"] for x in label] == ["score-label"] and label[0]["type"] == "Text" and label[0]["text"] == "score 0" and label[0]["texture"] is None
        crate = r["crate"]["hits"]
        assert [x["name"] for x in crate] == ["crate"] and crate[0]["texture"] == "crate-tex" and crate[0]["type"] == "Image"
        heart = r["heart"]["hits"]  # 컨테이너 자식: 자식이 먼저, 어느 컨테이너 안인지 함께
        assert heart[0]["name"] == "heart" and heart[0]["container"] == "hud", heart
        assert r["ghost"]["hits"] == [] and r["empty"]["hits"] == []  # 숨긴 오브젝트와 빈 곳은 비어 있다
        assert r["rejected"] == {"out_of_range": "ignored", "negative": "ignored", "nan": "ignored"}  # 규약 밖 요청에는 답하지 않는다
        assert r["capture"]["png"] and r["capture"]["bytes"] > 800
        assert w > 0 and h > 0


@requires_runtime
async def test_game_without_pick_wiring_reports_unsupported(client, auth, evidence, monkeypatch):
    """pick 을 연결하지 않은 게임(이전 템플릿·worker 가 새로 쓴 main.ts)은 조용히 멈추지 않고 사유와 함께 미지원으로 답한다."""
    require_browser()
    game = await build_game(client, auth, "pick-unsupported", "smoke_game_static.ts")
    res = await run_pick_scenario(client, auth, monkeypatch, game, evidence, expect_supported=False)
    for mode in ("desktop", "mobile"):
        r = res[mode]
        assert "error" not in r, (mode, r.get("error"))
        assert r["unsupported"]["supported"] is False and r["unsupported"]["hits"] == [] and "pickAt" in r["unsupported"]["reason"]
