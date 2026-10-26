"""게임 스튜디오 12단계 E2E 중 재현 가능한 부분 (TASK-G14).

실제 worker 단계(4·9)는 자동화할 수 없다. 로컬 격리 따까리에서 실제 Run(Claude)이 쓴 소스를 `tests/fixtures/e2e_dot_dungeon/`
에 기록해 두었고, 이 테스트는 그 소스를 **재생**한다(worker 를 흉내 내거나 mock 하지 않는다. 소스를 저장소에 커밋하는 일만 테스트가 한다).
재생한 소스로 실제 격리 빌드, 실제 Chromium 조작(이동·공격·처치·체력·게임오버·재시작), 구조화 피드백 요청, 롤백, 잠금 거부를 검증한다.
실제 worker 가 한 일의 증거(Run ID·커밋·빌드 ID·해시·스크린샷)는 docs/design/game-studio.md §12 에 있다.

컨테이너 런타임 또는 브라우저 자동화가 없으면 사유와 함께 skip 한다. 증거는 `TTAKKARI_GAME_EVIDENCE_DIR/g14-replay` 에 남는다.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from game_browser import free_port, require_browser, run_scenario, serve_games
from helpers import fresh_dir
from test_api_games import register, wait_build
from test_game_build_snapshots import (  # noqa: F401
    _cleanup_snapshots,
    git,
    requires_runtime,
)
from test_game_feedback_request import upload

from ttakkari.config import get_settings
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.contract import ProjectManifest, game_dir

FIXTURES = Path(__file__).parent / "fixtures" / "e2e_dot_dungeon"
DECISIONS = ("아트 스타일: 도트(픽셀 아트). 외부 에셋 없이 코드로 만든 픽셀 스프라이트만 사용",
             "2D 탑뷰 로그라이크 한 판짜리(방 3개), 모바일 터치 우선 + 키보드 지원",
             "게임 엔진: Phaser 3 (web-phaser 템플릿)")
CORE_RULES = ("state_shape", "move", "attack_kills_enemy", "hp_decreases", "game_over", "restart")


def apply_fixture(root: Path, version: str, message: str) -> str:
    """기록된 worker 소스를 저장소에 놓고 커밋한다. 돌려주는 값은 커밋 SHA."""
    src = FIXTURES / version
    for f in src.rglob("*"):
        if f.is_file():
            dest = root / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@requires_runtime
@pytest.mark.timeout(1500)
async def test_game_studio_flow_with_recorded_worker_output(client, auth, monkeypatch, tmp_path):
    require_browser()
    base = os.environ.get("TTAKKARI_GAME_EVIDENCE_DIR")
    out = Path(base) / "g14-replay" if base else tmp_path / "g14-replay"
    out.mkdir(parents=True, exist_ok=True)
    slug = "dot-dungeon-replay"
    manifest = ProjectManifest(id=slug, name="도트 던전", genre="로그라이크", engine="web-phaser", version="0.1.0", entrypoint="index.html",
                               locked_decisions=DECISIONS)
    root = fresh_dir(game_dir(slug))

    # 3) 프로젝트 생성·잠금 결정 승인
    await PhaserAdapter().scaffold_project(root, manifest)
    game = (await register(client, auth, slug)).json()
    gid = game["id"]
    assert game["genre"] == "로그라이크" and game["engine_id"] == "web-phaser"
    approved = (await client.post(f"/api/games/{gid}/lock/approve", headers=auth)).json()
    assert approved["lock_approved"] and approved["locked_decisions"] == list(DECISIONS)

    # 4) (재생) 기록된 worker 소스 v1, 5) 격리 빌드
    v1_commit = apply_fixture(root, "v1", "도트 던전 구현(기록된 worker 출력 재생)")

    async def build() -> dict:
        started = await client.post(f"/api/games/{gid}/builds", json={}, headers=auth)
        assert started.status_code == 202, started.text
        done = await wait_build(client, auth, gid, started.json()["id"], timeout=600)
        assert done["status"] == "succeeded", (done["error"], done["log_tail"])
        assert done["lock_verified"] is True
        return done

    b1 = await build()
    assert b1["source_commit"] == v1_commit

    game_port, host_port = free_port(), free_port()
    host_origin = f"http://127.0.0.1:{host_port}"
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", f"127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_origin_url", f"http://127.0.0.1:{game_port}")
    monkeypatch.setattr(s, "game_frame_ancestors", [host_origin])

    async def play_rules(tag: str, build_id: str | None, *, quick: bool = False, clear_first: bool = False) -> dict:
        play = await client.post(f"/api/games/{gid}/play", json={"build_id": build_id} if build_id else {}, headers={**auth, "Origin": host_origin})
        assert play.status_code == 200, play.text
        pl = play.json()
        assert "allow-same-origin" not in pl["sandbox"]
        res = run_scenario("e2e_roguelike.mjs", {"play_url": pl["url"], "sandbox": pl["sandbox"], "host_port": host_port, "out_dir": str(out), "tag": tag,
                                                 "quick": quick, "clear_first": clear_first}, timeout=300)
        assert res.get("error") is None, res.get("error")
        assert res["console"]["consoleErrors"] == [] and res["console"]["pageErrors"] == [], res["console"]  # 콘솔 에러 0
        failed = [k for k in CORE_RULES if not res["checks"].get(k)]
        assert not failed, (tag, failed, {k: res.get(k) for k in ("combat", "hpSeen", "gameover", "restart")})
        return res

    # 6~7) 별도 프로세스 게임 origin + 실제 브라우저 조작으로 규칙 확인
    with serve_games(game_port, [host_origin]):
        r1 = await play_rules("v1", b1["id"])
        assert r1["initial"]["hp"] == 3 and r1["initial"]["room"] == 1

        # 8) 스크린샷 + 영역 주석을 구조화 요청으로 제출 (worker 에게 열 수 있는 파일 경로가 전달되는지)
        shot = Path(r1["shots"]["initial"]["path"])
        art = await upload(client, auth, game["workspace_id"], "v1-initial.png", shot.read_bytes())
        req = await client.post(f"/api/games/{gid}/requests", headers=auth, json={
            "kind": "modify", "instruction": "캐릭터가 배경에 묻혀 보여. 더 선명하게 하고 공격 이펙트 추가",
            "feedback": {"build_id": b1["id"], "artifact_id": art["id"], "width": r1["shots"]["initial"]["width"], "height": r1["shots"]["initial"]["height"],
                         "annotations": [{"type": "box", "x": 0.02, "y": 0.36, "w": 0.18, "h": 0.21}]}})
        assert req.status_code == 202, req.text
        run = (await client.get(f"/api/runs/{req.json()['run_id']}", headers=auth)).json()
        staged = [Path(ln.split(": ", 1)[1]) for ln in run["prompt"].splitlines() if ln.startswith("- 원본 스크린샷: ")]
        assert staged and staged[0].is_file() and root in staged[0].parents
        assert staged[0].read_bytes() == shot.read_bytes() and "사각형: 왼쪽 위 (0.020, 0.360)" in run["prompt"]

        # 9~10) (재생) 기록된 worker 수정 v2 -> 재빌드 -> 회귀 규칙 확인
        v2_commit = apply_fixture(root, "v2", "캐릭터 가시성 개선과 공격 이펙트(기록된 worker 출력 재생)")
        b2 = await build()
        assert b2["source_commit"] == v2_commit and b2["bundle_sha256"] != b1["bundle_sha256"]
        r2 = await play_rules("v2", b2["id"], quick=True)

        # 11) 전후 비교: 같은 위치의 캡처가 다르고, 기존 빌드는 그대로 남는다(불변 스냅샷)
        assert r1["shots"]["initial"]["sha256"] != r2["shots"]["initial"]["sha256"]
        again = (await client.get(f"/api/games/{gid}/builds/{b1['id']}", params={"verify": "true"}, headers=auth)).json()
        assert again["snapshot_verified"] is True and again["bundle_sha256"] == b1["bundle_sha256"]

        # 12) 이전 정상 버전(v1)으로 소스 복구 롤백: 새 복구 빌드가 만들어지고 번들이 v1 과 같다
        rb = await client.post(f"/api/games/{gid}/rollback", json={"build_id": b1["id"], "restore_source": True, "confirm": True}, headers=auth)
        assert rb.status_code == 200, rb.text
        restored = await wait_build(client, auth, gid, rb.json()["restore_build"]["id"], timeout=600)
        assert restored["status"] == "succeeded", (restored["error"], restored["log_tail"])
        assert restored["restored_from_id"] == b1["id"]
        assert restored["bundle_sha256"] == b1["bundle_sha256"]  # 소스 트리가 v1 과 같아 번들도 같다
        assert git(root, "diff", "--stat", v1_commit, "HEAD") == ""
        assert (await client.get(f"/api/games/{gid}", headers=auth)).json()["active_build_id"] == restored["id"]  # 복구 빌드가 활성이 된다
        # 기본 대상은 활성 빌드(복구 빌드). 방1 을 먼저 비우려는 변형으로, 처치 단계가 방을 비우면 게임오버를 못 기다리던 간헐 실패 경로를 지난다.
        # 비우거나 방2 로 이동하는 도중 봇이 죽으면 이동 검사는 실패해도 게임오버는 이미 확인된 것이다.
        r3 = await play_rules("v3-restored", None, quick=True, clear_first=True)
        if r3.get("roomClearedInKillPhase"):
            assert r3["checks"].get("moved_on_after_clear") or r3["checks"].get("game_over"), {
                k: r3.get(k) for k in ("notes", "hpSeen", "gameover", "checks")
            }
        builds = (await client.get(f"/api/games/{gid}/builds", headers=auth)).json()
        assert len(builds) == 3 and {b["status"] for b in builds} == {"succeeded"}  # 원래·수정·복구 빌드가 모두 남았다

    # 잠금 결정을 깨는 변경(에이전트가 매니페스트를 고쳐 커밋한 상태)은 빌드가 거부되고, 요청에는 경고가 붙는다
    m = json.loads((root / "ttakkari-game.json").read_text())
    m["locked_decisions"][0] = "아트 스타일: 3D 폴리곤"
    (root / "ttakkari-game.json").write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
    git(root, "commit", "-qam", "잠금 결정 위반 시뮬레이션")
    rejected = await client.post(f"/api/games/{gid}/builds", json={}, headers=auth)
    assert rejected.status_code == 409 and rejected.json()["code"] == "lock_violation"
    warned = await client.post(f"/api/games/{gid}/requests", json={"kind": "modify", "instruction": "배경을 바꿔 줘"}, headers=auth)
    assert warned.json()["lock"] == "tampered"
    assert len((await client.get(f"/api/games/{gid}/builds", headers=auth)).json()) == 3  # 거부된 빌드는 행도 만들지 않는다

    (out / "result.json").write_text(json.dumps({
        "v1": {"commit": v1_commit, "build": b1["id"], "bundle_sha256": b1["bundle_sha256"], "checks": r1["checks"]},
        "v2": {"commit": v2_commit, "build": b2["id"], "bundle_sha256": b2["bundle_sha256"], "checks": r2["checks"]},
        "restored": {"build": restored["id"], "bundle_sha256": restored["bundle_sha256"], "checks": r3["checks"]},
    }, ensure_ascii=False, indent=1))
