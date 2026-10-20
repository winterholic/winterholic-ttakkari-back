"""버전 이력 탐색·비교 API: 요청·피드백 연결, 기준 버전, 소스 변경 요약, 거부·변조 경로 (TASK-G32, ADR 0061).

게임 저장소는 임시 git 저장소이고 worker Run 은 표식만 직접 만든 합성이다(실제 worker 실행 없음).
"""

import uuid

import pytest
from test_api_games import fake_adapter, make_repo  # noqa: F401
from test_game_build_snapshots import _cleanup_snapshots, git  # noqa: F401
from test_game_worker_flow import new_game

from ttakkari.db import sessionmaker
from ttakkari.games import autobuild, versions
from ttakkari.games.contract import game_dir
from ttakkari.models import ChatSession, GameBuild, Run, RunStatus


async def make_request_run(game: dict, instruction: str, *, base_build_id: str | None = None, feedback: dict | None = None, kind="modify",
                           prompt_head: str = "[작업 지침]\n수정한다\n\n") -> uuid.UUID:
    async with sessionmaker()() as db:
        sess = ChatSession(workspace_id=uuid.UUID(game["workspace_id"]), title="t", engine="auto")
        db.add(sess)
        await db.flush()
        tag = {"game_id": game["id"], "kind": kind, "build_id": base_build_id, "lock": "verified", "auto_build": True,
               **({"feedback": feedback} if feedback else {})}
        run = Run(session_id=sess.id, workspace_id=sess.workspace_id, prompt=f"{prompt_head}[사용자 요청]\n{instruction}\n", engine="auto",
                  status=RunStatus.succeeded, usage={"game": tag})
        db.add(run)
        await db.commit()
        return run.id


async def commit_change(slug: str, text: str, msg: str) -> None:
    root = game_dir(slug)
    (root / "a.txt").write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


async def versions_of(client, auth, gid: str) -> list[dict]:
    r = await client.get(f"/api/games/{gid}/versions", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def test_versions_link_builds_to_requests_feedback_and_base(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ver-link")
    gid = game["id"]
    first = await autobuild.after_run(await make_request_run(game, "첫 개발", kind="develop"))
    assert first is not None and first.seq == 1
    await commit_change("ver-link", "v2", "점프를 가볍게")
    fb = {"dir": "x", "artifact_id": str(uuid.uuid4()), "annotated_artifact_id": None, "annotations": 2, "picked": 1, "clip_frames": 0}
    run2 = await make_request_run(game, "점프가 너무 무거워요\n20% 가볍게", base_build_id=str(first.id), feedback=fb)
    second = await autobuild.after_run(run2)
    assert second is not None and second.seq == 2

    vs = await versions_of(client, auth, gid)
    assert [v["seq"] for v in vs] == [2, 1]  # 최신 순
    v2, v1 = vs
    assert v2["is_active"] is True and v1["is_active"] is False
    assert v2["produced_by"]["instruction"] == "점프가 너무 무거워요 20% 가볍게"  # 공백을 접는다
    assert v2["produced_by"]["base_build_seq"] == 1 and v2["produced_by"]["kind"] == "modify"
    assert v2["produced_by"]["feedback"] == {"annotations": 2, "picked": 1, "clip_frames": 0, "has_screenshot": True}
    assert v1["produced_by"]["kind"] == "develop" and v1["produced_by"]["feedback"] is None
    assert v1["requests_on"] == 1 and v1["feedback_requests_on"] == 1  # v1 을 기준으로 피드백 요청이 하나 있었다
    assert v2["requests_on"] == 0


async def test_versions_without_run_and_unknown_game(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ver-manual")
    b = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
    from test_api_games import wait_build

    await wait_build(client, auth, game["id"], b["id"])
    [v] = await versions_of(client, auth, game["id"])
    assert v["produced_by"] is None and v["requests_on"] == 0 and v["is_active"] is True
    assert (await client.get(f"/api/games/{uuid.uuid4()}/versions", headers=auth)).status_code == 404
    assert (await client.get(f"/api/games/{game['id']}/versions", params={"limit": 0}, headers=auth)).status_code == 422


async def test_instruction_is_cut_and_marker_missing_is_empty():
    assert versions.instruction_of("앞\n[사용자 요청]\n" + "가" * 500 + "\n") == "가" * 199 + "…"
    assert versions.instruction_of("머리말 없음") == ""
    assert versions.instruction_of("[사용자 요청]\n제어\x07문자\x1b가 있어도\n") == "제어문자가 있어도"


async def test_compare_reports_files_and_commits_between_builds(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ver-diff")
    gid = game["id"]
    b1 = await autobuild.after_run(await make_request_run(game, "시작", kind="develop"))
    root = game_dir("ver-diff")
    (root / "b.txt").write_text("새 파일\n")
    await commit_change("ver-diff", "v2 변경", "두 번째 변경")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "세 번째")
    b2 = await autobuild.after_run(await make_request_run(game, "수정"))
    r = await client.get(f"/api/games/{gid}/compare", params={"base": str(b1.id), "head": str(b2.id)}, headers=auth)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is True and d["same_commit"] is False
    assert {f["path"] for f in d["files"]} == {"a.txt", "b.txt"} and d["files_total"] == 2
    assert d["insertions"] >= 2 and d["deletions"] == 1  # a.txt: v1 → v2 변경(1줄 삭제·1줄 추가) + b.txt 1줄
    assert [c["subject"] for c in d["commits"]] == ["세 번째", "두 번째 변경"]  # 새 것부터
    assert d["base_dirty"] is False and d["head_dirty"] is False


async def test_compare_same_commit_missing_commit_and_cross_game(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ver-same")
    gid = game["id"]
    b1 = await autobuild.after_run(await make_request_run(game, "하나"))
    async with sessionmaker()() as db:  # 같은 소스를 다시 빌드한 수동 빌드와, 커밋 기록이 없는 빌드
        same = GameBuild(game_id=uuid.UUID(gid), seq=2, status="succeeded", source_commit=b1.source_commit)
        none = GameBuild(game_id=uuid.UUID(gid), seq=3, status="succeeded", source_commit=None)
        gone = GameBuild(game_id=uuid.UUID(gid), seq=4, status="succeeded", source_commit="a" * 40)
        db.add_all([same, none, gone])
        await db.commit()
        same_id, none_id, gone_id = same.id, none.id, gone.id

    async def cmp(base, head):
        r = await client.get(f"/api/games/{gid}/compare", params={"base": str(base), "head": str(head)}, headers=auth)
        assert r.status_code == 200, r.text
        return r.json()

    assert (await cmp(b1.id, same_id))["same_commit"] is True
    no_commit = await cmp(b1.id, none_id)
    assert no_commit["available"] is False and "커밋 기록" in no_commit["reason"]
    missing = await cmp(b1.id, gone_id)
    assert missing["available"] is False and "읽지 못했어요" in missing["reason"]

    other = await new_game(client, auth, "ver-other")
    ob = await autobuild.after_run(await make_request_run(other, "다른 게임"))
    r = await client.get(f"/api/games/{gid}/compare", params={"base": str(b1.id), "head": str(ob.id)}, headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "no_build"


async def test_compare_never_writes_repo_and_ignores_hostile_config(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ver-ro")
    gid = game["id"]
    b1 = await autobuild.after_run(await make_request_run(game, "시작"))
    root = game_dir("ver-ro")
    await commit_change("ver-ro", "v2", "변경")
    marker = root.parent / "ver-ro-hook-ran"
    # 저장소 안 설정이 diff·textconv 외부 명령을 걸어도 실행되면 안 된다
    git(root, "config", "diff.evil.textconv", f"touch {marker}")
    git(root, "config", "diff.external", f"touch {marker}")
    (root / ".gitattributes").write_text("*.txt diff=evil\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "attrs")
    b2 = await autobuild.after_run(await make_request_run(game, "수정"))
    head_before, status_before = git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain")
    r = await client.get(f"/api/games/{gid}/compare", params={"base": str(b1.id), "head": str(b2.id)}, headers=auth)
    assert r.status_code == 200 and r.json()["available"] is True
    assert not marker.exists()
    assert (git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain")) == (head_before, status_before)


async def test_new_endpoints_require_authentication(client):
    gid, bid = uuid.uuid4(), uuid.uuid4()
    for url in (f"/api/games/{gid}/versions", f"/api/games/{gid}/compare?base={bid}&head={bid}"):
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={"Authorization": "Bearer nope"})).status_code == 401


@pytest.mark.parametrize("raw,expected", [
    ("1\t2\ta.txt\x00-\t-\timg.png\x00", [("a.txt", 1, 2), ("img.png", None, None)]),
    ("3\t0\tdir/제목\ttab.txt\x00", [("dir/제목\ttab.txt".replace("\t", " "), 3, 0)]),
])
def test_numstat_parser_handles_binary_and_odd_paths(raw, expected):
    assert versions._parse_numstat(raw) == expected

