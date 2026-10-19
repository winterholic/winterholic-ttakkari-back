"""게임별 시나리오를 따로 쓰지 않는 일반 smoke (TASK-G21): 서로 다른 게임 4종을 실제 격리 빌드·실제 브라우저로 돌려
정상 게임은 통과, 입력에 반응하지 않는 게임과 부팅 중 죽는 게임은 이유와 함께 실패로 가려내는지 확인한다.

컨테이너 런타임 또는 브라우저 자동화가 없으면 skip 한다(TTAKKARI_REQUIRE_GAME_RUNTIME/BROWSER=1 이면 실패).
증거는 TTAKKARI_GAME_EVIDENCE_DIR/g21-generic 에 남는다.
"""

import json
import os
import shutil
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

FIXTURES = Path(__file__).parent / "fixtures" / "e2e_dot_dungeon"


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    base = os.environ.get("TTAKKARI_GAME_EVIDENCE_DIR")
    d = Path(base) / "g21-generic" if base else tmp_path / "g21-generic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _apply_dot_dungeon(root: Path) -> None:
    src = FIXTURES / "v1"
    for f in src.rglob("*"):
        if f.is_file():
            dest = root / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)


async def build_game(client, auth, slug: str, source: str) -> dict:
    """source: 'coin' | 'dungeon' | 'static' | 'crash' | 'late'. 모두 같은 scaffold 위에 게임 소스만 바꾼다."""
    root = fresh_dir(game_dir(slug))
    await PhaserAdapter().scaffold_project(root, manifest(slug))
    if source == "dungeon":
        _apply_dot_dungeon(root)
    else:
        (root / "src" / "main.ts").write_text((SCENARIOS / {"coin": "smoke_game_main.ts", "static": "smoke_game_static.ts", "crash": "smoke_game_crash.ts",
                                                   "late": "smoke_game_late.ts"}[source]).read_text())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"generic smoke game: {source}")
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    started = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    done = await wait_build(client, auth, game["id"], started.json()["id"], timeout=600)
    assert done["status"] == "succeeded", done["error"]
    return game


async def smoke(client, auth, monkeypatch, game: dict, out: Path) -> dict:
    game_port, host_port = free_port(), free_port()
    host_origin = f"http://127.0.0.1:{host_port}"
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", f"127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_origin_url", f"http://127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_frame_ancestors", [host_origin])
    play = (await client.post(f"/api/games/{game['id']}/play", json={}, headers={**auth, "Origin": host_origin})).json()
    assert urlsplit(play["url"]).netloc == f"127.0.0.1:{game_port}"
    with serve_games(game_port, [host_origin]):
        res = run_scenario("smoke_generic.mjs", {"play_url": play["url"], "sandbox": play["sandbox"], "host_port": host_port, "out_dir": str(out / game["slug"])}, timeout=400)
    (out / f"{game['slug']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


@requires_runtime
@pytest.mark.timeout(1800)
async def test_generic_smoke_passes_working_games_and_flags_broken_ones(client, auth, monkeypatch, evidence):
    require_browser()
    results = {}
    for source in ("coin", "dungeon", "static", "crash", "late"):
        game = await build_game(client, auth, f"gsmoke-{source}", source)
        results[source] = await smoke(client, auth, monkeypatch, game, evidence)

    # 정상 게임 2종(구조가 전혀 다른 게임): 데스크톱·모바일 모두 통과, 입력이 계측 상태 또는 화면을 바꿨다
    for source in ("coin", "dungeon"):
        res = results[source]
        for mode in ("desktop", "mobile"):
            r = res[mode]
            assert r["verdict"]["ok"], (source, mode, r["verdict"], r.get("error"), r["console"])
            assert r["boot"]["bridge"] and r["renders"]["blank"] is False and r["renders"]["buckets"] >= 3, (source, mode, r["renders"])
            assert r["inputEffect"] in {"state", "pixels"}, (source, mode, r["inputEffect"], r["probes"])
            assert r["pause"]["supported"] is True and r["save"]["exportOk"] is True, (source, mode)
        assert res["ok"] is True
        assert res["limits"] and any("완주" in x for x in res["limits"])  # 하지 않은 일을 결과에 명시한다

    # 첫 프레임이 늦는 정상 게임: 부팅 직후 캡처는 단색이지만 기다리면 그려지므로 "빈 화면"으로 오판하지 않는다(실제 worker 게임에서 간헐 실패했던 경우)
    late = results["late"]
    for mode in ("desktop", "mobile"):
        r = late[mode]
        assert r["verdict"]["ok"], (mode, r["verdict"], r.get("error"), r["console"])
        assert r["renders"]["firstCaptureBlank"] is True and r["renders"]["blank"] is False and r["renders"]["buckets"] >= 3, (mode, r["renders"])
    assert late["ok"] is True

    # 입력에 반응하지 않는 게임: 부팅은 되지만 입력 반응이 없어 실패
    static = results["static"]
    assert static["ok"] is False
    assert static["desktop"]["inputEffect"] == "none" and static["mobile"]["inputEffect"] == "none"
    assert any("어떤 입력에도" in p for p in static["desktop"]["verdict"]["problems"])
    assert static["desktop"]["renders"]["blank"] is False  # 화면은 그려졌다. 실패 사유가 입력 반응뿐임을 구분한다

    # 부팅 중 죽는 게임: 에러를 잡아 실패
    crash = results["crash"]
    assert crash["ok"] is False
    problems = " ".join(crash["desktop"]["verdict"]["problems"])
    assert "콘솔·페이지 에러" in problems or "bridge 가 응답하지 않아" in problems or "비어 있다" in problems, crash["desktop"]["verdict"]
