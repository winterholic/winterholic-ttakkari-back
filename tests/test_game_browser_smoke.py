"""실제 브라우저 플레이 smoke (TASK-G13): 격리 빌드한 실제 Phaser 게임을 별도 프로세스의 게임 origin 으로 서빙하고
Playwright(Chromium)로 열어 키·터치 입력, 콘솔 에러 0, 캡처, bridge 계측 상태 변화, 일시정지를 확인한다.

컨테이너 런타임(격리 빌드) 또는 브라우저 자동화가 없는 환경은 사유와 함께 skip 한다
(`TTAKKARI_REQUIRE_GAME_RUNTIME=1`, `TTAKKARI_REQUIRE_GAME_BROWSER=1` 이면 skip 대신 실패).
증거(스크린샷)는 `TTAKKARI_GAME_EVIDENCE_DIR` 이 있으면 거기에, 없으면 임시 폴더에 둔다.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from game_browser import SCENARIOS, free_port, require_browser, run_scenario, serve_games
from helpers import fresh_dir
from test_api_games import register, wait_build  # noqa: F401
from test_game_build_snapshots import _cleanup_snapshots, git, manifest, requires_runtime  # noqa: F401

from ttakkari.config import get_settings
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.contract import game_dir

PWA_PORT = 0


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    base = os.environ.get("TTAKKARI_GAME_EVIDENCE_DIR")
    d = Path(base) / "g13-smoke" if base else tmp_path / "g13-smoke"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def build_smoke_game(client, auth, slug: str) -> dict:
    root = fresh_dir(game_dir(slug))
    await PhaserAdapter().scaffold_project(root, manifest(slug))
    (root / "src" / "main.ts").write_text((SCENARIOS / "smoke_game_main.ts").read_text())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "smoke game: 방향키·터치 이동과 코인 점수")
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    started = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    done = await wait_build(client, auth, game["id"], started.json()["id"], timeout=600)
    assert done["status"] == "succeeded", done["error"]
    return {"game": game, "build": done}


@requires_runtime
async def test_real_browser_plays_isolated_build_with_keys_touch_and_bridge_state(client, auth, evidence, monkeypatch):
    require_browser()
    built = await build_smoke_game(client, auth, "smoke-play")
    game_port, host_port = free_port(), free_port()
    host_origin = f"http://127.0.0.1:{host_port}"
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", f"127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_origin_url", f"http://127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_frame_ancestors", [host_origin])
    play = (await client.post(f"/api/games/{built['game']['id']}/play", json={}, headers={**auth, "Origin": host_origin})).json()
    assert urlsplit(play["url"]).netloc == f"127.0.0.1:{game_port}" and "ttk-parent" in play["url"] and "allow-same-origin" not in play["sandbox"]

    with serve_games(game_port, [host_origin]):
        res = run_scenario("smoke_basic.mjs", {"play_url": play["url"], "sandbox": play["sandbox"], "host_port": host_port, "out_dir": str(evidence)})

    (evidence / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))  # 실패해도 원인을 볼 수 있게 먼저 남긴다
    for mode in ("desktop", "mobile"):
        r = res[mode]
        assert "error" not in r, (r.get("error"), r["console"])
        console = r["console"]
        assert console["consoleErrors"] == [] and console["pageErrors"] == [], console  # 콘솔 에러 0
        assert console["failedRequests"] == [] and console["badResponses"] == [], console
        assert r["initial"]["ready"] is True and r["initial"]["frames"] > 5  # 게임 루프가 돈다
        for key in ("captureBefore", "captureAfter"):
            cap = r[key]
            assert cap["png"] and cap["bytes"] > 800 and Path(cap["path"]).is_file(), (mode, key, cap)
        assert Path(r["pageShot"]).stat().st_size > 2000
        assert r["captureBefore"]["sha256"] != r["captureAfter"]["sha256"], "입력 뒤 화면이 바뀌지 않았다"  # 상태 변화가 화면에도 나타난다

    d = res["desktop"]
    assert d["keyMoved"]["to"] > d["keyMoved"]["from"] + 40  # 키보드: 오른쪽
    assert d["key_ArrowDown"]["after"][1] > d["key_ArrowDown"]["before"][1]
    assert d["key_ArrowUp"]["after"][1] < d["key_ArrowUp"]["before"][1]
    assert d["key_ArrowLeft"]["after"][0] < d["key_ArrowLeft"]["before"][0]
    assert d["score"]["after"] > d["score"]["before"]  # 규칙: 코인을 먹으면 점수
    p = d["pause"]
    assert p["supported"] is True and p["reply"] is True and p["resumeReply"] is False
    assert p["framesPaused"][0] == p["framesPaused"][1] and p["framesResumed"] > p["framesPaused"][1]  # 멈췄다가 다시 돈다
    assert d["save"]["importOk"] is True and '"slot1"' in d["save"]["exported"]  # 메모리 세이브 export/import (ADR 0052)

    m = res["mobile"]
    assert m["touch"]["viewport"][0] <= 390
    (fx, fy), (x0, y0), (x1, y1) = m["touch"]["tapFraction"], m["touch"]["from"], m["touch"]["to"]
    assert abs(x1 - x0) + abs(y1 - y0) > 15  # 터치: 탭한 쪽으로 이동 시작
