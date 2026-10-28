from types import SimpleNamespace

import pytest


class _Result:
    def all(self):
        return []


class _Db:
    def __init__(self, mission, sources):
        self.mission = mission
        self.sources = sources
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


class _Policy:
    def __init__(self, host):
        self.host = host
        self.max_pages_per_job = 10
        self.max_depth = 1

    def allows(self, purpose):
        return purpose == "store"

    def check_url(self, parsed):
        return None if parsed.host == self.host else "host_not_allowed"

    def to_dict(self):
        return {"allowed_hosts": [self.host], "permissions": {"store": "allowed"}}


def _mission(urls, *, goal="디자인 시스템·UI/UX 지식 백과사전 구축"):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000123",
        tenant_id="00000000-0000-0000-0000-000000000456",
        status="running",
        goal=goal,
        gaps=[{"url": "search", "reason": "search_provider_challenge"}],
        plan={
            "title": "디자인 시스템",
            "mode": "manual",
            "sources": [{"url": url, "adapter": "website"} for url in urls],
            "budget": {"pages": 10, "depth": 1},
            "schedule": {},
        },
    )


@pytest.mark.asyncio
async def test_design_system_goal_keeps_unknown_source_blocked_and_preserves_challenge_gap(monkeypatch):
    from ttakkari.collector import missions

    mission = _mission(["https://designsystem.digital.gov/about/"])
    db = _Db(mission, [])
    grants = []

    async def register_source(*args, **kwargs):
        grants.append(kwargs)
        return SimpleNamespace(source_key=kwargs["source_key"]), None

    async def active_policy(*_args):
        return None

    async def get_run(*_args):
        return SimpleNamespace(run_seq=3, page_budget=10, extra_pages=0)

    async def maybe_queue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(missions.store, "_lock", lambda *_args: _noop())
    monkeypatch.setattr(missions.store, "active_policy", active_policy)
    monkeypatch.setattr(missions, "get_run", get_run)
    monkeypatch.setattr(missions.control, "register_source", register_source)
    monkeypatch.setattr(missions.research_rounds, "maybe_queue_round", maybe_queue)

    await missions.reconcile_mission(db, mission)

    assert not grants
    assert mission.status == "waiting_sources"
    assert {gap["reason"] for gap in mission.gaps} == {"search_provider_challenge", "source_policy_required"}


async def _noop():
    return None


@pytest.mark.asyncio
async def test_reconcile_queues_existing_resolver_policy_without_host_wide_grant(monkeypatch):
    from ttakkari.collector import missions

    approved_url = "https://docs.approved.test/reference"
    blocked_url = "https://design-tokens.github.io/community-group/format/"
    candidate = SimpleNamespace(
        source_key="license:approved-document",
        request={"seeds": [approved_url]},
        tenant_id="00000000-0000-0000-0000-000000000456",
        scope="library",
        pool="personal",
    )
    policy = _Policy("docs.approved.test")
    mission = _mission([approved_url, blocked_url])
    db = _Db(mission, [candidate])
    registered = []
    enqueued = []

    async def active_policy(*_args):
        return policy, 1

    async def get_run(*_args):
        return SimpleNamespace(run_seq=3, page_budget=10, extra_pages=0)

    async def register_source(*_args, **kwargs):
        registered.append(kwargs)
        return SimpleNamespace(source_key=kwargs["source_key"], tenant_id=candidate.tenant_id,
                               scope="library", pool="personal"), None

    async def enqueue(*_args, **kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(id="new-job")

    async def maybe_queue(*_args, **_kwargs):
        return None

    monkeypatch.setattr(missions.store, "_lock", lambda *_args: _noop())
    monkeypatch.setattr(missions.store, "active_policy", active_policy)
    monkeypatch.setattr(missions, "get_run", get_run)
    monkeypatch.setattr(missions.control, "register_source", register_source)
    monkeypatch.setattr(missions.control, "enqueue", enqueue)
    monkeypatch.setattr(missions.research_rounds, "maybe_queue_round", maybe_queue)

    await missions.reconcile_mission(db, mission)

    assert len(registered) == len(enqueued) == 1
    assert registered[0]["policy"]["allowed_hosts"] == ["docs.approved.test"]
    assert mission.status == "running"
    assert {gap["reason"] for gap in mission.gaps} == {"search_provider_challenge", "source_policy_required"}
