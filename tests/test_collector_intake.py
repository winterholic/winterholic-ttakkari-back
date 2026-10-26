"""CP-S2-01: collector intake 의 멱등 등록·scope 결속을 실제 PostgreSQL 에서 확인한다.

DB 는 tests/conftest.py 가 프로세스마다 만드는 일회용 DB(ttakkari_test_<pid>_<난수>)이고 운영 DB 는 거부된다.
설계 기대값은 docs/design/collector-platform/contracts/fixtures/intake/ 10건을 그대로 쓴다.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from ttakkari.collector import intake
from ttakkari.collector.contracts import (
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    CollectorError,
    EnvelopeIdConflictError,
    ScopeBindingMismatchError,
)
from ttakkari.collector.intake import submit_envelope
from ttakkari.collector_models import CollectorJob, CollectorOutbox
from ttakkari.db import sessionmaker

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "docs/design/collector-platform/contracts/fixtures"
INTAKE_EXPECTATIONS = json.loads((FIXTURES_DIR / "intake" / "expectations.json").read_text(encoding="utf-8"))
REQUEST_KEY = "test-req-001"
AUTH = AuthContext(tenant_id="fixture-tenant", scope="personal", principal_id="fixture-owner")


@pytest.fixture
def envelope() -> dict:
    return json.loads((FIXTURES_DIR / "valid-accept.json").read_text(encoding="utf-8"))


def rebound(envelope: dict, tenant_id: str, scope: str, principal: str) -> tuple[AuthContext, dict]:
    """다른 tenant/scope 주체와 그에 맞게 결속된 envelope."""
    env = copy.deepcopy(envelope)
    env["access"]["tenant_id"] = tenant_id
    env["access"]["scope"] = scope
    env["access"]["principal_ids"] = [principal]
    return AuthContext(tenant_id=tenant_id, scope=scope, principal_id=principal), env


async def submit(auth: AuthContext, request_key: str, envelope: dict):
    async with sessionmaker()() as db:
        result = await submit_envelope(db, auth, request_key, envelope)
        await db.commit()
        return result


async def counts() -> tuple[int, int]:
    async with sessionmaker()() as db:
        return (
            await db.scalar(select(func.count(CollectorJob.id))),
            await db.scalar(select(func.count(CollectorOutbox.id))),
        )


async def test_single_submission_creates_job_and_outbox(envelope):
    result = await submit(AUTH, REQUEST_KEY, envelope)

    assert result.status == "created"
    assert len(result.payload_hash) == 64
    async with sessionmaker()() as db:
        job = (await db.execute(select(CollectorJob))).scalar_one()
        outbox = (await db.execute(select(CollectorOutbox))).scalar_one()
    assert job.id == result.job_id
    assert (job.status, job.stage, job.request_key) == ("queued", "intake", REQUEST_KEY)
    assert (job.tenant_id, job.scope) == ("fixture-tenant", "personal")
    assert (outbox.job_id, outbox.status, outbox.tenant_id) == (result.job_id, "pending", "fixture-tenant")


async def test_identical_replay_returns_same_job_without_new_rows(envelope):
    first = await submit(AUTH, REQUEST_KEY, envelope)
    second = await submit(AUTH, REQUEST_KEY, envelope)

    assert (first.status, second.status) == ("created", "replay")
    assert first.job_id == second.job_id
    assert await counts() == (1, 1)


async def test_replay_ignores_key_order_and_recorded_at(envelope):
    first = await submit(AUTH, REQUEST_KEY, envelope)

    reversed_order = {k: envelope[k] for k in reversed(list(envelope))}
    re_recorded = copy.deepcopy(envelope)
    re_recorded["recorded_at"] = "2026-10-20T16:00:00Z"
    for variant in (reversed_order, re_recorded):
        again = await submit(AUTH, REQUEST_KEY, variant)
        assert again.status == "replay"
        assert again.job_id == first.job_id
    assert await counts() == (1, 1)


async def test_conflicting_payload_for_same_key_is_409(envelope):
    await submit(AUTH, REQUEST_KEY, envelope)

    altered = copy.deepcopy(envelope)
    altered["decision"]["state"] = "hold"
    altered["decision"]["reason_codes"] = ["unmeasured_quality"]
    with pytest.raises(CollectorConflictError) as caught:
        await submit(AUTH, REQUEST_KEY, altered)
    assert caught.value.http_status == 409

    new_id = copy.deepcopy(envelope)
    new_id["envelope_id"] = "11111111-2222-3333-4444-555555555555"
    with pytest.raises(CollectorConflictError):
        await submit(AUTH, REQUEST_KEY, new_id)
    assert await counts() == (1, 1)


async def test_ten_parallel_requests_for_same_key_make_one_job(envelope):
    results = await asyncio.gather(*(submit(AUTH, REQUEST_KEY, envelope) for _ in range(10)))

    assert len({r.job_id for r in results}) == 1
    assert sorted(r.status for r in results) == ["created"] + ["replay"] * 9
    assert await counts() == (1, 1)


async def test_ten_parallel_requests_with_distinct_keys_and_same_envelope_id_make_one_job(envelope):
    """키만 다르고 envelope_id 가 같은 동시 요청은 outbox 유니크 경합을 탄다. 하나만 만들고 나머지는 409 다."""
    outcomes = await asyncio.gather(
        *(submit(AUTH, f"key-{i}", envelope) for i in range(10)), return_exceptions=True
    )

    created = [o for o in outcomes if not isinstance(o, BaseException)]
    failed = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(created) == 1 and created[0].status == "created"
    assert len(failed) == 9
    assert all(isinstance(o, EnvelopeIdConflictError) for o in failed), failed
    assert await counts() == (1, 1)


async def test_same_envelope_id_with_new_key_is_409_not_integrity_error(envelope):
    await submit(AUTH, REQUEST_KEY, envelope)

    with pytest.raises(EnvelopeIdConflictError) as caught:
        await submit(AUTH, "another-key", envelope)
    assert (caught.value.http_status, caught.value.error_code) == (409, "envelope_id_conflict")
    assert await counts() == (1, 1)


async def test_unique_violation_race_is_translated_when_precheck_misses(envelope, monkeypatch):
    """사전 조회가 놓친 경합(조회 뒤 다른 트랜잭션이 커밋)도 INSERT 의 IntegrityError 를 409 로 바꾼다."""
    await submit(AUTH, REQUEST_KEY, envelope)
    real = intake._envelope_id_registered
    calls = 0

    async def blind_first_call(db, tenant_id, scope, envelope_id):
        nonlocal calls
        calls += 1
        return False if calls == 1 else await real(db, tenant_id, scope, envelope_id)

    monkeypatch.setattr(intake, "_envelope_id_registered", blind_first_call)
    with pytest.raises(EnvelopeIdConflictError):
        await submit(AUTH, "another-key", envelope)
    assert calls == 1
    assert await counts() == (1, 1)


async def test_same_envelope_id_in_other_tenant_or_scope_is_independent(envelope):
    first = await submit(AUTH, REQUEST_KEY, envelope)
    other_tenant, tenant_env = rebound(envelope, "other-tenant", "personal", "other-owner")
    other_scope, scope_env = rebound(envelope, "fixture-tenant", "work", "fixture-owner")

    by_tenant = await submit(other_tenant, "new-key", tenant_env)
    assert by_tenant.status == "created"
    assert by_tenant.job_id != first.job_id

    # C2 단일 도서관: 같은 tenant 안에서는 scope 와 무관하게 단일 도서관에 등록되므로 동일 envelope_id 재제출 시 409 충돌이다.
    with pytest.raises(CollectorConflictError):
        await submit(other_scope, "new-key", scope_env)

    assert await counts() == (2, 2)


async def test_outbox_unique_key_includes_tenant(envelope):
    async with sessionmaker()() as db:
        cols = await db.scalar(text(
            "select array_agg(a.attname order by a.attname) from pg_constraint c "
            "join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey) "
            "where c.conname = 'uq_collector_outbox_tenant_sink_envelope'"))
    assert cols == ["envelope_id", "scope", "sink", "tenant_id"]


async def test_scope_tamper_is_403_and_leaves_no_rows(envelope):
    tampered = copy.deepcopy(envelope)
    tampered["access"]["scope"] = "work"

    with pytest.raises(ScopeBindingMismatchError) as caught:
        await submit(AUTH, REQUEST_KEY, tampered)
    assert (caught.value.http_status, caught.value.error_code) == (403, "scope_binding_mismatch")
    assert await counts() == (0, 0)


def test_exception_hierarchy_keeps_403_out_of_the_422_handler():
    assert not issubclass(ScopeBindingMismatchError, CollectorContractError)
    assert not issubclass(CollectorContractError, ScopeBindingMismatchError)
    assert not issubclass(CollectorConflictError, CollectorContractError)
    assert all(issubclass(e, CollectorError) for e in (
        CollectorContractError, ScopeBindingMismatchError, CollectorConflictError, EnvelopeIdConflictError))
    assert [e.http_status for e in (CollectorContractError, ScopeBindingMismatchError, CollectorConflictError)] == [
        422, 403, 409]


async def test_failed_transaction_leaves_zero_remnants(envelope):
    async with sessionmaker()() as db:
        await submit_envelope(db, AUTH, REQUEST_KEY, envelope)
        await db.rollback()
    assert await counts() == (0, 0)


@pytest.mark.parametrize("request_key", ["", "   ", "k" * 257, "bad\x00key"])
async def test_invalid_request_key_is_contract_error(envelope, request_key):
    with pytest.raises(CollectorContractError):
        await submit(AUTH, request_key, envelope)
    assert await counts() == (0, 0)


async def test_empty_envelope_is_contract_error():
    with pytest.raises(CollectorContractError):
        await submit(AUTH, REQUEST_KEY, {})
    assert await counts() == (0, 0)


def _extension(envelope: dict, value) -> dict:
    env = copy.deepcopy(envelope)
    env["extensions"]["x-test:value"] = value
    return env


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: _extension(e, "bad \ud800 surrogate"), id="lone-surrogate-value"),
        pytest.param(lambda e: _extension(e, {"\udc00": 1}), id="lone-surrogate-key"),
        pytest.param(lambda e: _extension(e, 2**53 + 1), id="int-above-2**53"),
        pytest.param(lambda e: _extension(e, -(2**60)), id="int-below--2**53"),
        pytest.param(lambda e: _extension(e, float("nan")), id="nan"),
        pytest.param(lambda e: _extension(e, float("inf")), id="infinity"),
        pytest.param(lambda e: _extension(e, "nul\x00byte"), id="nul-character"),
        pytest.param(lambda e: _extension(e, json.loads("[" * 70 + "]" * 70)), id="deep-nesting"),
    ],
)
async def test_unencodable_values_are_contract_errors_not_generic_exceptions(envelope, mutate):
    with pytest.raises(CollectorContractError):
        await submit(AUTH, REQUEST_KEY, mutate(envelope))
    assert await counts() == (0, 0)


async def test_overlong_source_key_is_contract_error(envelope):
    env = copy.deepcopy(envelope)
    env["source"]["key"] = "s" * 257
    with pytest.raises(CollectorContractError):
        await submit(AUTH, REQUEST_KEY, env)
    assert await counts() == (0, 0)


async def test_integer_at_2_pow_53_boundary_is_accepted_and_replays(envelope):
    env = _extension(envelope, 2**53)
    first = await submit(AUTH, REQUEST_KEY, env)
    assert (await submit(AUTH, REQUEST_KEY, env)).job_id == first.job_id


@pytest.mark.parametrize("fixture_name", sorted(INTAKE_EXPECTATIONS))
async def test_intake_fixture_matches_expectation_on_real_db(fixture_name):
    """계약 fixture 10건이 DB 경로에서도 같은 결과다. reject 는 job/outbox 를 만들지 않는다."""
    case = json.loads((FIXTURES_DIR / "intake" / fixture_name).read_text(encoding="utf-8"))
    auth = AuthContext(
        tenant_id=case["auth"]["tenant_id"], scope=case["auth"]["session_pool"],
        principal_id=case["auth"]["principal_id"])
    want_sequence = INTAKE_EXPECTATIONS[fixture_name]
    assert len(want_sequence) == len(case["submissions"])

    for index, (want, submission) in enumerate(zip(want_sequence, case["submissions"], strict=True)):
        label = f"{fixture_name}[{index}] -> {want}"
        if want == "reject:scope_binding_mismatch":
            with pytest.raises(ScopeBindingMismatchError):
                await submit(auth, case["request_key"], submission)
        elif want == "conflict:409":
            with pytest.raises(CollectorConflictError):
                await submit(auth, case["request_key"], submission)
        else:
            assert want in {"created", "replay"}, label
            assert (await submit(auth, case["request_key"], submission)).status == want, label

    created = sum(1 for want in want_sequence if want == "created")
    assert await counts() == (created, created), fixture_name


async def test_runs_on_throwaway_database_not_production():
    async with sessionmaker()() as db:
        name = await db.scalar(text("select current_database()"))
    print(f"COLLECTOR_TEST_DB_IDENTITY={name}")
    assert name != "ttakkari"
    # 배포 후보 검사는 ttakkari_gen_<hex> 일회용 DB 를 쓰므로 두 접두어 모두 일회용으로 본다.
    assert name.startswith(("ttakkari_test", "ttakkari_gen_"))
