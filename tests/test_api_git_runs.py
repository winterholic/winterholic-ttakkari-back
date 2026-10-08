from __future__ import annotations

import re

from conftest import git
from helpers import run_events, run_to_end


def _ref_sha(root, run_id: str) -> str:
    return git(root, "rev-parse", "--verify", f"refs/ttakkari/runs/{run_id}")


async def test_clean_git_run_records_checkpoint_at_head(client, auth, new_session, make_git_dir):
    root = make_git_dir()
    ws, sess = await new_session(root)
    assert ws["is_git"] is True
    run = await run_to_end(client, auth, sess["id"], "hello")
    assert run["status"] == "succeeded"
    assert re.fullmatch(r"[0-9a-f]{40}", run["checkpoint_ref"])
    assert run["checkpoint_ref"] == git(root, "rev-parse", "HEAD")  # 깨끗한 트리는 HEAD 가 체크포인트
    assert _ref_sha(root, run["id"]) == run["checkpoint_ref"]
    evs = await run_events(client, auth, run["id"])
    cp = [e for e in evs if e["type"] == "checkpoint"]
    assert len(cp) == 1 and cp[0]["payload"]["ref"] == run["checkpoint_ref"]
    # 아무것도 안 바꿨으면 diff 이벤트가 없고 diff API 는 빈 본문이다.
    assert not [e for e in evs if e["type"] == "run.diff"]
    r = await client.get(f"/api/runs/{run['id']}/diff", headers=auth)
    assert r.status_code == 200 and r.text == ""
    # 체크포인트는 작업 트리/브랜치를 건드리지 않는다.
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "stash", "list") == ""


async def test_agent_edits_show_in_run_diff_event_and_endpoint(client, auth, new_session, make_git_dir):
    root = make_git_dir()
    _ws, sess = await new_session(root)
    run = await run_to_end(client, auth, sess["id"], "MODIFY the readme")
    assert run["status"] == "succeeded"

    assert (root / "README.md").read_text() == "hello\nmodified by fake agent\n"
    evs = await run_events(client, auth, run["id"])
    diffs = [e["payload"] for e in evs if e["type"] == "run.diff"]
    assert len(diffs) == 1
    assert diffs[0]["files"] == [{"path": "README.md", "added": 1, "removed": 0}]
    assert diffs[0]["new_files"] == ["new_file.txt"]
    types = [e["type"] for e in evs]
    assert types.index("run.diff") < types.index("run.finished")

    r = await client.get(f"/api/runs/{run['id']}/diff", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "--- a/README.md" in r.text and "+modified by fake agent" in r.text
    assert "new_file.txt" not in r.text  # 미추적 신규 파일은 git diff 에 안 나오고 이벤트의 new_files 로만 전달된다

    # 사용자가 이후에 더 고쳐도 같은 체크포인트 기준으로 반영된다.
    (root / "README.md").write_text("hello\nmodified by fake agent\nuser edit\n")
    r2 = await client.get(f"/api/runs/{run['id']}/diff", headers=auth)
    assert "+user edit" in r2.text


async def test_modified_files_registered_as_agent_modified_artifacts(client, auth, new_session, make_git_dir):
    root = make_git_dir()
    _ws, sess = await new_session(root)
    run = await run_to_end(client, auth, sess["id"], "MODIFY")
    arts = (await client.get("/api/artifacts", params={"run_id": run["id"]}, headers=auth)).json()
    by = {a["filename"]: a for a in arts}
    assert set(by) == {"report.md", "README.md", "new_file.txt"}
    assert by["README.md"]["source"] == "agent_modified" and by["README.md"]["rel_path"] == "README.md"
    assert by["new_file.txt"]["source"] == "agent_modified"
    assert by["report.md"]["source"] == "agent_output"


async def test_diff_is_relative_to_dirty_checkpoint(client, auth, new_session, make_git_dir):
    root = make_git_dir()
    (root / "README.md").write_text("hello\nuncommitted before run\n")
    (root / "untracked-before.txt").write_text("u")
    _ws, sess = await new_session(root)
    run = await run_to_end(client, auth, sess["id"], "MODIFY")
    assert run["checkpoint_ref"] != git(root, "rev-parse", "HEAD")  # stash create 가 만든 커밋
    assert _ref_sha(root, run["id"]) == run["checkpoint_ref"]
    d = next(e["payload"] for e in await run_events(client, auth, run["id"]) if e["type"] == "run.diff")
    assert d["files"] == [{"path": "README.md", "added": 1, "removed": 0}]
    assert d["new_files"] == ["new_file.txt"]  # 실행 전부터 있던 미추적 파일은 제외
    text = (await client.get(f"/api/runs/{run['id']}/diff", headers=auth)).text
    assert "+modified by fake agent" in text
    assert "+uncommitted before run" not in text
    # 작업 트리 변경은 사라지지 않았다.
    assert (root / "README.md").read_text() == "hello\nuncommitted before run\nmodified by fake agent\n"


async def test_separate_refs_per_run(client, auth, new_session, make_git_dir):
    root = make_git_dir()
    _ws, sess = await new_session(root)
    r1 = await run_to_end(client, auth, sess["id"], "MODIFY first")
    r2 = await run_to_end(client, auth, sess["id"], "second")
    assert r1["checkpoint_ref"] != r2["checkpoint_ref"]  # 두 번째는 첫 Run 이 남긴 변경을 포함한 상태
    assert _ref_sha(root, r1["id"]) == r1["checkpoint_ref"] and _ref_sha(root, r2["id"]) == r2["checkpoint_ref"]
    d2 = await client.get(f"/api/runs/{r2['id']}/diff", headers=auth)
    assert d2.status_code == 200 and d2.text == ""
    d1 = await client.get(f"/api/runs/{r1['id']}/diff", headers=auth)
    assert "+modified by fake agent" in d1.text


async def test_non_git_workspace_has_no_checkpoint_and_diff_404(client, auth, new_session):
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "hi")
    assert run["checkpoint_ref"] is None
    r = await client.get(f"/api/runs/{run['id']}/diff", headers=auth)
    assert r.status_code == 404


async def test_git_repo_without_commits_runs_without_checkpoint(client, auth, new_session, make_dir):
    root = make_dir()
    git(root, "init", "-q", "-b", "main")
    _ws, sess = await new_session(root)
    run = await run_to_end(client, auth, sess["id"], "hi")
    assert run["status"] == "succeeded" and run["checkpoint_ref"] is None
    assert (await client.get(f"/api/runs/{run['id']}/diff", headers=auth)).status_code == 404


async def test_diff_unknown_run_404_and_requires_auth(client, auth):
    import uuid
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/diff", headers=auth)).status_code == 404
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/diff")).status_code == 401
