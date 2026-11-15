"""저장소 가치 평가 API: 생성·캐시·이력·인증·재시작 정리(ADR 0073). 상류 GitHub 와 LLM 만 가짜."""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit_tests"))
from github_fakes import FakeGh, ok

from ttakkari.db import sessionmaker
from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.logs import LogFetcher
from ttakkari.main import app
from ttakkari.models import RepoAppraisal, utcnow
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.repo_appraisal import service as appraisal_svc

NOW = datetime.now(UTC)
FILES = {"README.md": "# widget\n" + "설명 " * 200,
         "pyproject.toml": '[project]\nname="widget"\n',
         "src/acme/core.py": "x = 1\n" * 100,
         "src/acme/main.py": "def main():\n    print(1)\n",
         "tests/test_core.py": "def test_x():\n    assert True\n",
         "AGENTS.md": "# 지침\n짧다\n"}


def _state(**over):
    state = {"license": "mit", "calls": [], "upstream_401": False}
    state.update(over)
    return state


def _handler(state):
    tree = [{"path": p, "size": len(t.encode()), "type": "blob"} for p, t in FILES.items()]

    def handle(req: httpx.Request) -> httpx.Response:
        state["calls"].append(req.url.path)
        if state["upstream_401"]:
            return httpx.Response(401, json={"message": "Bad credentials"})
        p = req.url.path
        if p == "/repos/acme/widget":
            return ok({"default_branch": "main", "license": {"key": state["license"]},
                       "open_issues_count": 1, "description": "위젯", "language": "Python",
                       "archived": False, "fork": False, "created_at": "2024-01-01T00:00:00Z"})
        if p == "/repos/acme/widget/git/trees/main":
            return ok({"tree": tree, "truncated": False})
        if p == "/repos/acme/widget/readme":
            return ok(FILES["README.md"].encode())
        if p == "/repos/acme/widget/commits":
            n = int(req.url.params.get("per_page", 100))
            rows = [{"sha": f"{i:040x}"[-40:],
                     "commit": {"author": {"date": (NOW - timedelta(days=i * 10)).isoformat()}},
                     "author": {"login": "alice"}} for i in range(min(n, 12))]
            return ok(rows)
        if p == "/repos/acme/widget/contributors":
            return ok([{"login": "alice", "contributions": 50},
                       {"login": "bob", "contributions": 30},
                       {"login": "cat", "contributions": 20}])
        if p == "/repos/acme/widget/releases":
            return ok([{"tag_name": "v1.0", "published_at": (NOW - timedelta(days=30)).isoformat(),
                        "draft": False, "prerelease": False}])
        if p == "/repos/acme/widget/issues":
            return ok([{"number": i, "state": "closed"} for i in range(1, 8)]
                      + [{"number": 8, "state": "open"}])
        if p.startswith("/repos/acme/widget/contents/"):
            name = p.split("/contents/", 1)[1]
            return ok(FILES[name].encode()) if name in FILES else ok({"message": "x"}, status=404)
        raise AssertionError(f"예상 밖 호출: {p}")
    return handle


@pytest.fixture
def fake_github():
    state = _state()
    gh = FakeGh([("alice", "success")])
    reg = AccountRegistry(runner=gh, gh_bin="gh")
    client = GithubClient(reg, transport=httpx.MockTransport(_handler(state)))
    svc = svc_mod.GithubService(registry=reg, client=client, logs=LogFetcher(client))
    app.dependency_overrides[svc_mod.get_service] = lambda: svc
    state["svc"] = svc
    yield state
    app.dependency_overrides.pop(svc_mod.get_service, None)


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    async def runner(system, prompt):
        calls.append(prompt)
        body = ('{"scores": ['
                '{"item": "code_quality.readability", "score": 3, "citations": ["src/acme/core.py"], "confidence": "medium"},'
                '{"item": "architecture.layering", "score": 2, "citations": ["src/acme/main.py"], "confidence": "low"},'
                '{"item": "ai_harness.coherence", "score": 4, "citations": ["AGENTS.md"], "confidence": "high"},'
                '{"item": "originality.verdict", "score": 2, "citations": ["evidence:readme"], "confidence": "low"}'
                '], "summary": "무난한 소규모 저장소."}')
        return body, None

    monkeypatch.setattr(appraisal_svc, "LLM_RUNNER", runner)
    return calls


async def _wait_done(client, auth, rid, timeout=20.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        r = await client.get(f"/api/repo-appraisals/{rid}", headers=auth)
        assert r.status_code == 200, r.text
        if r.json()["status"] in ("done", "failed"):
            return r.json()
        await asyncio.sleep(0.1)
    raise AssertionError("평가가 끝나지 않았다")


async def test_post_runs_to_done_with_scores(client, auth, fake_github, fake_llm):
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice", "mode": "quick"},
                          headers=auth)
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    done = await _wait_done(client, auth, rid)
    assert done["status"] == "done" and done["grade"] in ("A+", "A", "B+", "B", "C", "D")
    assert 0 <= done["weighted_score"] <= 100
    assert done["llm_used"] is True and done["llm_model"] == "codex:gpt-6-luna"
    assert done["evidence_chars"] > 0 and len(fake_llm) == 1  # LLM 1회
    result = done["result"]
    assert set(result["axes"]) == {"code_quality", "architecture", "maintainability",
                                   "ai_harness", "originality"}
    assert result["repo_type"] in ("library", "app", "docs", "experiment", "unknown")
    assert len(result["weaknesses"]) <= 3 and result["summary"]
    assert all(w["advice"] for w in result["weaknesses"])


async def test_cache_reuse_returns_same_row_without_llm(client, auth, fake_github, fake_llm):
    body = {"target": "https://github.com/acme/widget", "account": "alice", "mode": "quick"}
    first = (await client.post("/api/repo-appraisals", json=body, headers=auth)).json()
    await _wait_done(client, auth, first["id"])
    again = await client.post("/api/repo-appraisals", json=body, headers=auth)
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    assert len(fake_llm) == 1  # 재계산 없음


async def test_force_recalculates_and_mode_is_cache_key(client, auth, fake_github, fake_llm):
    body = {"target": "acme/widget", "account": "alice", "mode": "quick"}
    first = (await client.post("/api/repo-appraisals", json=body, headers=auth)).json()
    await _wait_done(client, auth, first["id"])
    forced = await client.post("/api/repo-appraisals", json={**body, "force": True}, headers=auth)
    assert forced.status_code == 202 and forced.json()["id"] != first["id"]
    await _wait_done(client, auth, forced.json()["id"])
    deep = await client.post("/api/repo-appraisals", json={**body, "mode": "deep"}, headers=auth)
    assert deep.status_code == 202
    await _wait_done(client, auth, deep.json()["id"])
    assert len(fake_llm) == 3


async def test_llm_failure_completes_with_deterministic_only(client, auth, fake_github, monkeypatch):
    async def failing(system, prompt):
        return None, "엔진 없음"

    monkeypatch.setattr(appraisal_svc, "LLM_RUNNER", failing)
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice"}, headers=auth)
    done = await _wait_done(client, auth, r.json()["id"])
    assert done["status"] == "done" and done["llm_used"] is False
    assert done["llm_model"] is None and done["result"]["axes"]["originality"] is None
    assert done["result"]["axes"]["maintainability"] is not None


async def test_llm_bad_citation_discarded(client, auth, fake_github, monkeypatch):
    async def bad(system, prompt):
        body = ('{"scores": [{"item": "code_quality.readability", "score": 5, '
                '"citations": ["src/없는.py"], "confidence": "high"}], "summary": "x"}')
        return body, None

    monkeypatch.setattr(appraisal_svc, "LLM_RUNNER", bad)
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice"}, headers=auth)
    done = await _wait_done(client, auth, r.json()["id"])
    items = {i["item"]: i for i in done["result"]["items"]}
    assert items["code_quality.readability"]["status"] == "unverified"
    assert done["llm_used"] is True  # 호출은 했으니 사용 표시


async def test_running_detail_shows_stage(client, auth, fake_github, monkeypatch):
    gate = asyncio.Event()

    async def slow(system, prompt):
        await gate.wait()
        return ('{"scores": [], "summary": "늦음"}', None)

    monkeypatch.setattr(appraisal_svc, "LLM_RUNNER", slow)
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice"}, headers=auth)
    rid = r.json()["id"]
    stages = set()
    for _ in range(200):
        detail = (await client.get(f"/api/repo-appraisals/{rid}", headers=auth)).json()
        if detail["status"] in ("done", "failed"):
            break
        if detail.get("stage"):
            stages.add(detail["stage"])
        await asyncio.sleep(0.02)
    gate.set()
    done = await _wait_done(client, auth, rid)
    assert done["status"] == "done" and stages <= {"collect", "score", "llm"}


async def test_history_lists_latest_first_with_delta(client, auth, fake_github, fake_llm):
    body = {"target": "acme/widget", "account": "alice"}
    first = (await client.post("/api/repo-appraisals", json=body, headers=auth)).json()
    d1 = await _wait_done(client, auth, first["id"])
    fake_github["license"] = None  # 두 번째는 라이선스 없음 → 점수 하락
    fake_github["svc"].client._cache.clear()
    second = (await client.post("/api/repo-appraisals", json={**body, "force": True},
                                headers=auth)).json()
    d2 = await _wait_done(client, auth, second["id"])
    r = await client.get("/api/repo-appraisals",
                         params={"owner": "acme", "repo": "widget", "limit": 10}, headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [second["id"], first["id"]]
    assert items[0]["delta"] == pytest.approx(d2["weighted_score"] - d1["weighted_score"])
    assert items[0]["delta"] < 0 and items[1]["delta"] is None
    assert all(set(i) <= {"id", "owner", "repo", "head_sha", "mode", "repo_type", "status",
                          "weighted_score", "grade", "llm_used", "delta", "created_at",
                          "finished_at"} for i in items)  # 화이트리스트만


@pytest.mark.parametrize("path", ["/api/repo-appraisals", "/api/repo-appraisals/x",
                                  "/api/repo-appraisals?owner=a&repo=b"])
async def test_owner_login_required(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"Authorization": "Bearer bad"})).status_code == 401


@pytest.mark.parametrize("path", ["/api/repo-appraisals", "/api/repo-appraisals/x",
                                  "/api/repo-appraisals?owner=a&repo=b"])
async def test_agent_session_token_rejected_with_401(client, path):
    agent = {"Authorization": f"Bearer {issue_token(uuid.uuid4())}"}
    assert (await client.get(path, headers=agent)).status_code == 401


async def test_post_rejects_bad_input(client, auth, fake_github):
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme", "account": "alice"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "zed"}, headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "account_unavailable"
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice", "mode": "huge"},
                          headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "validation"
    r = await client.get("/api/repo-appraisals", headers=auth)
    assert r.status_code == 422  # owner·repo 필수


async def test_upstream_401_becomes_502_not_app_logout(client, auth, fake_github):
    fake_github["upstream_401"] = True
    r = await client.post("/api/repo-appraisals",
                          json={"target": "acme/widget", "account": "alice"}, headers=auth)
    assert r.status_code == 502 and r.json()["code"] == "gh_auth_expired"


async def test_unknown_id_404(client, auth):
    r = await client.get(f"/api/repo-appraisals/{uuid.uuid4()}", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_restart_recovery_fails_inflight_only():
    async with sessionmaker()() as db:
        from sqlalchemy import select
        running = RepoAppraisal(owner="a", repo="r", mode="quick", rubric_version="1",
                               status="running", result={"stage": "llm"})
        queued = RepoAppraisal(owner="a", repo="r", mode="quick", rubric_version="1",
                              status="queued")
        done = RepoAppraisal(owner="a", repo="r", mode="quick", rubric_version="1",
                            status="done", weighted_score=80.0, finished_at=utcnow())
        db.add_all([running, queued, done])
        await db.commit()
        n = await appraisal_svc.recover_running()
        assert n == 2
        db.expire_all()  # recover 는 별도 세션이라 다시 읽는다
        rows = {r.id: r for r in await db.scalars(
            select(RepoAppraisal).where(RepoAppraisal.owner == "a"))}
        assert rows[running.id].status == "failed" and rows[running.id].error == "서버 재시작으로 중단"
        assert rows[queued.id].status == "failed" and rows[queued.id].finished_at is not None
        assert rows[done.id].status == "done"
        await db.rollback()
