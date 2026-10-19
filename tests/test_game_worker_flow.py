"""게임 개발·수정 요청이 기존 세션·Run·worker routing 으로 넘어가고, 잠금 결정 위반은 빌드가 거부되는지 (TASK-G08)."""

import json
import uuid
from pathlib import Path

import pytest
from conftest import agent_calls
from helpers import wait_run
from sqlalchemy import func, select
from test_api_games import fake_adapter, make_repo, register, wait_build  # noqa: F401
from test_game_build_snapshots import _cleanup_snapshots, manifest  # noqa: F401

from ttakkari.db import sessionmaker
from ttakkari.games.contract import game_dir
from ttakkari.models import ChatSession, GameBuild, Run
from ttakkari.orchestrator.runtime import issue_token


async def new_game(client, auth, slug: str, *, approve: bool = True, build: bool = False) -> dict:
    make_repo(slug)
    game = (await register(client, auth, slug)).json()
    if approve:
        assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    if build:
        b = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
        game["build"] = await wait_build(client, auth, game["id"], b["id"])
        assert game["build"]["status"] == "succeeded"
    return game


def drift_decisions(slug: str, decisions=("전부 3D",)) -> None:
    """에이전트가 저장소 파일의 잠금 결정을 바꾼 상황."""
    (game_dir(slug) / "ttakkari-game.json").write_text(json.dumps(manifest(slug, locked_decisions=decisions).to_json(), ensure_ascii=False))


async def ask(client, auth, game_id: str, **body):
    return await client.post(f"/api/games/{game_id}/requests", json={"instruction": "점프를 20% 가볍게", **body}, headers=auth)


# ---- 사용자 요청 경로 -----------------------------------------------------------------------------


async def test_request_endpoints_require_auth_and_validate(client, auth):
    gid = uuid.uuid4()
    for method, url in (("POST", f"/api/games/{gid}/requests"), ("GET", f"/api/games/{gid}/requests")):
        r = await client.request(method, url, json={"instruction": "x"} if method == "POST" else None)
        assert r.status_code == 401
    assert (await ask(client, auth, str(gid))).status_code == 404
    game = await new_game(client, auth, "wk-valid")
    for body in ({"instruction": ""}, {"instruction": "x" * 20_001}, {"kind": "deploy"}, {"instruction": "x", "path": "/etc"}, {}):
        r = await client.post(f"/api/games/{game['id']}/requests", json=body, headers=auth)
        assert r.status_code == 422, body
    assert (await ask(client, auth, game["id"], build_id=str(uuid.uuid4()))).status_code == 404
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(Run)) == 0  # 거부된 요청은 Run 을 만들지 않는다


async def test_modify_request_becomes_worker_session_and_run_with_game_context(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-modify", build=True)
    r = await ask(client, auth, game["id"], kind="modify")
    assert r.status_code == 202, r.text
    req = r.json()
    assert req["kind"] == "modify" and req["lock"] == "verified" and req["build_id"] == game["build"]["id"]
    sess = (await client.get(f"/api/sessions/{req['session_id']}", headers=auth)).json()
    assert sess["kind"] == "worker" and sess["workspace_id"] == game["workspace_id"] and sess["engine"] == "auto"
    assert sess["worker_task_type"] == "general" and sess["parent_id"] is None
    run = (await client.get(f"/api/runs/{req['run_id']}", headers=auth)).json()  # 상태는 기존 Run API 로 읽는다
    assert run["origin"] == "user" and run["workspace_id"] == game["workspace_id"]
    for needle in ("wk-modify", "web-phaser", game["id"], "2D", "터치 우선", "사용자가 승인한 잠금 결정", game["build"]["id"],
                   (game["build"]["bundle_sha256"])[:16], "점프를 20% 가볍게", "locked_decisions 를 바꾸지 않는다", "격리 러너"):
        assert needle in run["prompt"], needle
    assert run["usage"]["game"] == {"game_id": game["id"], "kind": "modify", "build_id": game["build"]["id"], "lock": "verified"}
    done = await wait_run(client, auth, req["run_id"])
    assert done["status"] in {"succeeded", "failed"}
    assert any("점프를 20% 가볍게" in json.dumps(c["argv"], ensure_ascii=False) for c in agent_calls())  # 엔진까지 전달됐다
    listed = (await client.get(f"/api/games/{game['id']}/requests", headers=auth)).json()
    assert [x["run_id"] for x in listed] == [req["run_id"]] and listed[0]["lock"] == "verified"
    other = await new_game(client, auth, "wk-other")
    assert (await client.get(f"/api/games/{other['id']}/requests", headers=auth)).json() == []  # 다른 게임의 요청은 섞이지 않는다


async def test_verify_request_routes_as_verification_and_forbids_edits(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-verify", build=True)
    req = (await ask(client, auth, game["id"], kind="verify", instruction="잠금 결정이 지켜졌는지 확인")).json()
    sess = (await client.get(f"/api/sessions/{req['session_id']}", headers=auth)).json()
    assert sess["worker_task_type"] == "verification"
    run = (await client.get(f"/api/runs/{req['run_id']}", headers=auth)).json()
    assert "독립 검증만 수행한다" in run["prompt"] and "소스는 고치지 않는다" in run["prompt"]
    assert "게임을 개발한다" not in run["prompt"]
    await wait_run(client, auth, req["run_id"])


async def test_request_reports_unapproved_and_tampered_lock_without_starting_a_second_queue(client, auth):
    draft = await new_game(client, auth, "wk-draft", approve=False)
    r = (await ask(client, auth, draft["id"], kind="develop")).json()
    assert r["lock"] == "unapproved" and r["build_id"] is None
    run = (await client.get(f"/api/runs/{r['run_id']}", headers=auth)).json()
    assert "아직 사용자가 승인하지 않은 초안" in run["prompt"] and "아직 성공한 빌드가 없다" in run["prompt"]
    game = await new_game(client, auth, "wk-tamper")
    drift_decisions("wk-tamper")
    t = (await ask(client, auth, game["id"])).json()
    assert t["lock"] == "tampered"
    tprompt = (await client.get(f"/api/runs/{t['run_id']}", headers=auth)).json()["prompt"]
    assert "경고" in tprompt and "승인된 내용으로 되돌려라" in tprompt and "전부 3D" in tprompt and "사용자가 승인한 잠금 결정(리비전" not in tprompt
    async with sessionmaker()() as db:  # 새 큐 없음: 세션과 Run 만 늘었다
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.workspace_id == uuid.UUID(game["workspace_id"]))) == 1
    await wait_run(client, auth, r["run_id"])
    await wait_run(client, auth, t["run_id"])


# ---- 잠금 위반 거부 ---------------------------------------------------------------------------------


async def test_agent_changing_locked_decisions_makes_build_rejected_until_restored(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-lock", build=True)
    original = (game_dir("wk-lock") / "ttakkari-game.json").read_text()
    drift_decisions("wk-lock", ("전부 3D", "터치 우선"))  # 에이전트가 결정을 바꾸고 커밋까지 했다고 가정
    r = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "lock_violation", r.text
    shown = (await client.get(f"/api/games/{game['id']}", headers=auth)).json()
    assert shown["lock_approved"] is True and shown["lock_matches_repo"] is False
    builds = (await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()
    assert [b["status"] for b in builds] == ["succeeded"]  # 실패 행도 새로 만들지 않았다
    # 결정을 지우는 변경, 순서만 바꾼 변경도 같은 결과
    for decisions in ((), ("터치 우선", "2D")):
        drift_decisions("wk-lock", decisions)
        assert (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).status_code == 409
    (game_dir("wk-lock") / "ttakkari-game.json").write_text(original)  # 승인본으로 되돌리면 다시 빌드된다
    b = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert b.status_code == 202
    done = await wait_build(client, auth, game["id"], b.json()["id"])
    assert done["status"] == "succeeded" and done["lock_verified"] is True and done["seq"] == 2
    # 사용자가 새 결정을 직접 승인하면 그 결정이 기준이 된다
    drift_decisions("wk-lock", ("전부 3D",))
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).json()["locked_revision"] == 2
    assert (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).status_code == 202


async def test_lock_changed_between_prepare_and_execute_fails_the_build(client, auth, fake_adapter, monkeypatch):  # noqa: F811
    from ttakkari.games import builds as game_builds
    from ttakkari.games.contract import GameContractError
    game = await new_game(client, auth, "wk-race")
    async with sessionmaker()() as db:
        from ttakkari.models import GameProject
        g = await db.get(GameProject, uuid.UUID(game["id"]))
        _, plan = await game_builds.prepare_build(db, g)
        drift_decisions("wk-race")
        with pytest.raises(GameContractError) as err:
            await game_builds.execute_build(db, plan)
        assert err.value.code == "lock_violation"
        b = (await db.scalars(select(GameBuild).where(GameBuild.game_id == g.id))).one()
        assert b.status == "failed" and "잠금 결정" in (b.error or "") and b.bundle_rel_path is None


# ---- 오케스트레이터(MCP spawn_worker) 경로 -----------------------------------------------------------


async def orchestrator(client, auth, make_dir) -> tuple[dict, dict]:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    orch = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    return orch, {"Authorization": f"Bearer {issue_token(orch['id'])}"}


async def test_spawn_worker_with_game_id_runs_in_game_workspace_with_locked_decisions(client, auth, make_dir, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-mcp", build=True)
    orch, h = await orchestrator(client, auth, make_dir)
    r = await client.post("/api/orchestrator/workers", headers=h, json={
        "title": "점프 수정", "prompt": "점프 높이를 낮춰라", "engine": "claude", "game_id": game["id"]})
    assert r.status_code == 201, r.text
    wid = r.json()["worker_id"]
    sess = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    assert sess["parent_id"] == orch["id"] and sess["workspace_id"] == game["workspace_id"] != orch["workspace_id"]
    run = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    assert run["origin"] == "orchestrator" and "점프 높이를 낮춰라" in run["prompt"] and "터치 우선" in run["prompt"] and game["build"]["id"] in run["prompt"]
    assert run["usage"]["game"]["kind"] == "modify" and run["usage"]["game"]["lock"] == "verified"
    assert (await wait_run(client, auth, run["id"]))["status"] == "succeeded"
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()  # 기존 도구로 상태를 읽는다
    assert detail["status"] == "succeeded"
    listed = (await client.get(f"/api/games/{game['id']}/requests", headers=auth)).json()
    assert [x["run_id"] for x in listed] == [run["id"]]
    v = await client.post("/api/orchestrator/workers", headers=h, json={
        "title": "검증", "prompt": "독립 검토", "task_type": "verification", "game_id": game["id"], "game_build_id": game["build"]["id"]})
    assert v.status_code == 201, v.text
    vrun = (await client.get(f"/api/sessions/{v.json()['worker_id']}/runs", headers=auth)).json()[0]
    assert vrun["usage"]["game"]["kind"] == "verify" and "독립 검증만 수행한다" in vrun["prompt"]
    await wait_run(client, auth, vrun["id"])


async def test_spawn_worker_game_errors_leave_no_session(client, auth, make_dir, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "wk-err", build=True)
    other = await new_game(client, auth, "wk-err2", build=True)
    orch, h = await orchestrator(client, auth, make_dir)

    async def spawn(**extra):
        return await client.post("/api/orchestrator/workers", headers=h, json={"title": "t", "prompt": "p", "engine": "claude", **extra})
    assert (await spawn(game_id=str(uuid.uuid4()))).status_code == 404
    assert (await spawn(game_id=game["id"], game_build_id=other["build"]["id"])).status_code == 404  # 다른 게임의 빌드
    assert (await spawn(game_id="not-a-uuid")).status_code == 422
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.parent_id == uuid.UUID(orch["id"]))) == 0
    work = (await client.post("/api/sessions", json={"workspace_id": orch["workspace_id"], "kind": "orchestrator", "pool": "work"}, headers=auth)).json()
    wh = {"Authorization": f"Bearer {issue_token(work['id'])}"}
    r = await client.post("/api/orchestrator/workers", headers=wh, json={"title": "t", "prompt": "p", "engine": "claude", "game_id": game["id"]})
    assert r.status_code == 403  # 회사 범위 오케스트레이터는 개인 게임 저장소를 다루지 않는다


def test_mcp_spawn_worker_exposes_game_arguments():
    import inspect
    import re
    src = Path(__file__).parent.parent / "src/ttakkari/orchestrator/mcp_server.py"
    text = src.read_text()
    sig = re.search(r"async def spawn_worker\((.*?)\) -> str:", text, re.DOTALL).group(1)
    assert "game_id: str | None = None" in sig and "game_build_id: str | None = None" in sig
    assert inspect.isfunction(test_mcp_spawn_worker_exposes_game_arguments)
