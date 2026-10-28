from types import SimpleNamespace

import pytest

from ttakkari.collector.contracts import CollectorContractError


class _Result:
    def all(self):
        return []


class _Db:
    def __init__(self, mission, sources=None):
        self.mission = mission
        self.sources = sources or []
        self.scalar_calls = 0

    async def scalar(self, _query):
        self.scalar_calls += 1
        return self.mission if self.scalar_calls == 1 else None

    async def scalars(self, _query):
        return self.sources

    async def execute(self, _query):
        return _Result()

    async def flush(self):
        return None

    def begin_nested(self):
        return _Nested()


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _mission(urls=None, *, goal="디자인 시스템·UI/UX 지식 백과사전 구축"):
    from ttakkari.collector import missions
    plan = missions.plan(goal, urls=urls)
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000123",
        tenant_id="00000000-0000-0000-0000-000000000456",
        current_run_seq=1,
        status="running",
        goal=goal,
        gaps=[],
        plan=plan,
    )


@pytest.mark.asyncio
async def test_design_system_goal_advances_to_arxiv_when_web_search_challenged(monkeypatch):
    """When DDG challenges 3 consecutive queries, search_phase must not drop remaining alternate-provider queries."""
    from ttakkari.collector import research_rounds

    mission = _mission()
    queries = mission.plan["queries"]
    assert len(queries) >= 4
    # Query 3 is the papers topic with latin letters: "UI UX research paper arxiv"
    assert (mission.plan.get("query_topics") or {}).get(queries[3]) == "papers"

    executed_calls = []

    async def fake_run_providers(query, topic):
        executed_calls.append((query, topic))
        if topic == "papers":
            return {
                "candidates": [{"url": "https://arxiv.org/abs/2601.00001", "adapter": "paper", "query": query, "provenance": "arxiv_api", "title": "Design System Spec"}],
                "providers": [{"name": "duckduckgo_html", "status": "failed", "code": "search_provider_challenge", "retryable": False},
                              {"name": "arxiv_api", "status": "ok", "candidates": 1}],
                "status": "ok",
                "error": None,
                "retryable": False,
            }
        # DDG anti-bot challenge
        return {
            "candidates": [],
            "providers": [{"name": "duckduckgo_html", "status": "failed", "code": "search_provider_challenge", "retryable": False}],
            "status": "failed",
            "error": "search_provider_challenge",
            "retryable": False,
        }

    job = SimpleNamespace(
        id="job-123",
        attempt_token="token-123",
        cursor={"prepared": [], "search_cursor": 0, "no_new": 0},
        cancel_requested=False,
    )

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, *args, **kwargs):
            return mission

    monkeypatch.setattr("ttakkari.db.sessionmaker", lambda: FakeSession)
    monkeypatch.setattr(research_rounds, "_run_providers", fake_run_providers)

    async def fake_heartbeat(): pass
    ctx = SimpleNamespace(heartbeat=fake_heartbeat)
    async def fake_commit_work(cb, stage=None):
        class DbMock:
            async def flush(self): pass
            async def get(self, *args, **kwargs): return mission
        await cb(DbMock())
        return "continue"
    ctx.commit_work = fake_commit_work

    async def fake_fenced_job(db, jid, tok):
        return SimpleNamespace(cancel_requested=False, cursor=job.cursor)
    monkeypatch.setattr("ttakkari.collector.jobs._fenced_job", fake_fenced_job)
    async def fake_launch(db, m): pass
    monkeypatch.setattr("ttakkari.collector.missions.launch", fake_launch)

    done = []
    result = await research_rounds.search_phase(job, ctx, mission.id, done)
    assert result is None  # Ended gracefully without early abort

    # Verify that the alternate arxiv query (query 3) was executed despite 3 prior consecutive failures!
    topics_executed = [t for q, t in executed_calls]
    assert "papers" in topics_executed
    # And candidate was preserved
    assert any("arxiv.org" in s["url"] for s in mission.plan["sources"])


@pytest.mark.asyncio
async def test_reconcile_mission_isolates_register_source_contract_error(monkeypatch):
    """A contract error during control.register_source must be isolated to that source gap and not crash reconcile."""
    from ttakkari.collector import missions

    approved_url = "https://docs.approved.test/component"
    candidate = SimpleNamespace(
        source_key="license:approved-document",
        request={"seeds": [approved_url]},
        tenant_id="00000000-0000-0000-0000-000000000456",
        scope="library",
        pool="personal",
    )
    class P:
        max_pages_per_job = 10
        max_depth = 1
        def allows(self, p): return True
        def check_url(self, u): return None
        def to_dict(self): return {"allowed_hosts": ["docs.approved.test"]}

    mission = _mission([approved_url])
    db = _Db(mission, [candidate])

    async def get_run(*_a):
        return SimpleNamespace(run_seq=1, page_budget=10, extra_pages=0)
    async def active_policy(*_a):
        return P(), 1
    async def fail_register(*_a, **_kw):
        raise CollectorContractError("invalid source seed parameters")
    async def fake_lock(*_a):
        return None
    async def fake_mqr(*_a, **_kw):
        return None

    monkeypatch.setattr(missions.store, "_lock", fake_lock)
    monkeypatch.setattr(missions.store, "active_policy", active_policy)
    monkeypatch.setattr(missions, "get_run", get_run)
    monkeypatch.setattr(missions.control, "register_source", fail_register)
    monkeypatch.setattr(missions.research_rounds, "maybe_queue_round", fake_mqr)

    # Should not raise CollectorContractError
    await missions.reconcile_mission(db, mission)

    # The failing source was recorded as source_unavailable gap
    assert any(g.get("reason") == "source_unavailable" and "출처 등록에 실패" in g.get("message", "") for g in mission.gaps)
