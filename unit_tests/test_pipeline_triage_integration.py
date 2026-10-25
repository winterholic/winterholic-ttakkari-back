from datetime import UTC
from types import SimpleNamespace

from ttakkari.collector import pipeline, textproc
from ttakkari.collector.contracts import validate_envelope
from ttakkari.collector.policy import CollectRequest, L1Policy


def test_decide_integrates_triage_with_goal():
    norm = textproc.normalize_text(
        "파이썬 asyncio 비동기 코루틴 이벤트 루프 프로그래밍 가이드입니다. "
        "async def와 await 구문을 사용하여 단일 스레드에서도 수천 개의 동시 연결을 논블로킹으로 처리할 수 있습니다. "
        "Task와 Future를 생성하여 백그라운드 작업을 조율하고 예외 처리를 수행합니다."
    )
    extracted = SimpleNamespace(title="Asyncio 가이드", truncated=False)
    profile_result = SimpleNamespace(hold_reasons=[], notes=[])
    l1 = SimpleNamespace(hold_reasons=[], notes=[], error=None)
    l0 = SimpleNamespace(flags=(), signals={})
    analysis = SimpleNamespace(
        text=norm.text,
        extracted=extracted,
        profile_result=profile_result,
        l1=l1,
        l0=l0,
        truncated=False,
    )
    p = SimpleNamespace(
        analysis=analysis,
        signals={},
        reasons=[],
        kind="document",
    )
    req = CollectRequest(
        seeds=("https://docs.test/",),
        profile="general",
        profile_params={},
        max_pages=10,
        max_depth=2,
        l1=L1Policy(mode="off"),
    )

    # Test with relevant goal
    state, reasons, _errors = pipeline._decide(
        p,
        exact=None,
        near=[],
        req=req,
        goal="Python asyncio 비동기 프로그래밍",
    )
    assert state == "accept"
    assert "triage:accepted" in reasons
    assert p.signals["triage"]["action"] == "accept"
    assert p.signals["triage"]["triage_status"] == "auto_accepted"
    assert p.signals["triage"]["scores"]["relevance"] >= 0.30

    # Test with completely irrelevant goal
    p_unrel = SimpleNamespace(
        analysis=analysis,
        signals={},
        reasons=[],
        kind="document",
    )
    state_unrel, reasons_unrel, _ = pipeline._decide(
        p_unrel,
        exact=None,
        near=[],
        req=req,
        goal="프로야구 홈런 타자 타격 순위",
    )
    assert state_unrel == "hold"
    assert "triage:irrelevant_to_goal" in reasons_unrel
    assert p_unrel.signals["triage"]["action"] == "hold"
    assert p_unrel.signals["triage"]["triage_status"] == "auto_held"


def test_triage_mode_reports_active_when_legacy_l1_scorer_is_shadowed():
    norm = textproc.normalize_text(
        "Python asyncio event loop and coroutine guide. The event loop schedules async tasks."
    )
    analysis = SimpleNamespace(
        text=norm.text,
        extracted=SimpleNamespace(title="Python asyncio guide", truncated=False),
        profile_result=SimpleNamespace(hold_reasons=[], notes=[]),
        l1=SimpleNamespace(hold_reasons=[], notes=[], error=None),
        l0=SimpleNamespace(flags=(), signals={}),
        truncated=False,
    )
    prepared = SimpleNamespace(
        analysis=analysis, signals={}, reasons=[], kind="document"
    )
    request = CollectRequest(
        seeds=("https://docs.test/",),
        profile="general",
        profile_params={},
        max_pages=1,
        max_depth=0,
        l1=L1Policy(mode="shadow", scorer="legacy-shadow"),
    )

    pipeline._decide(
        prepared,
        exact=None,
        near=[],
        req=request,
        goal="Python asyncio coroutine",
    )

    assert prepared.signals["triage"]["mode"] == "active"


def test_build_envelope_includes_triage_extension_and_measured_relevance():
    import uuid
    from datetime import datetime

    from ttakkari.collector import parse, quality

    norm = textproc.normalize_text(
        "PostgreSQL MVCC 다중 버전 동시성 제어 아키텍처 가이드입니다. "
        "트랜잭션 격리 수준에 따라 스냅샷을 생성하여 락 충돌 없이 고성능 읽기 작업을 지원합니다."
    )
    extracted = SimpleNamespace(
        title="PostgreSQL MVCC 가이드",
        canonical_url="https://docs.test/mvcc",
        published=None,
        meta={"parser_version": parse.PARSER_VERSION},
        truncated=False,
    )
    l0 = quality.l0_signals(norm.text)
    l1 = quality.L1Outcome(quality.unmeasured_quality(textproc.NORMALIZER_VERSION))
    profile_result = SimpleNamespace(
        hold_reasons=[], notes=[], published_at=None, signals={}
    )
    lang = textproc.identify_language(norm.text)

    analysis = SimpleNamespace(
        text=norm.text,
        normalized_steps=norm.steps,
        truncated=False,
        extracted_sha256=textproc.sha256_text(norm.text),
        language=lang,
        minhash=None,
        l0=l0,
        profile_result=profile_result,
        extracted=extracted,
        l1=l1,
    )

    triage_sig = {
        "version": "lightweight-triage-v1",
        "action": "accept",
        "triage_status": "auto_accepted",
        "scores": {
            "relevance": 0.65,
            "quality": 0.85,
            "novelty": 1.0,
            "trust": 1.0,
            "composite": 0.82,
        },
        "reasons": ["triage:accepted", "triage:high_relevance"],
        "thresholds": {"min_accept_relevance": 0.25},
        "mode": "active",
        "verified": False,
    }

    p = SimpleNamespace(
        analysis=analysis,
        signals={"triage": triage_sig},
        reasons=["triage:accepted"],
        errors=[],
        raw_unpreserved=False,
        url="https://docs.test/mvcc",
        final_url="https://docs.test/mvcc",
        http_status=200,
        redirects=(),
        etag=None,
        last_modified=None,
        robots="allowed",
        media_type="text/html",
        raw_sha256="a" * 64,
        raw_size=len(norm.text.encode()),
        raw_handle="raw/123",
        fetched_at=datetime.now(UTC),
        entry={"pu": None},
        item_id=uuid.uuid4(),
    )

    job = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        envelope={
            "source": {"key": "test-src"},
            "access": {"principal_ids": ["owner"], "sensitivity": "normal"},
            "extensions": {
                "collector:mission": {
                    "id": str(uuid.uuid4()),
                    "goal": "PostgreSQL MVCC 트랜잭션 동시성 제어",
                }
            },
        },
    )

    from ttakkari.collector.policy import SourcePolicySpec

    spec = SourcePolicySpec(
        allowed_hosts=("docs.test",),
        schemes=("https",),
        ports=(443,),
        permissions={"store": "allowed", "knowledge": "allowed", "export": "allowed"},
        license_id="MIT",
        policy_url="https://docs.test/policy",
        checked_at=datetime.now(UTC),
        basis="Official docs license",
        max_pages_per_job=10,
        max_depth=1,
    )

    req = CollectRequest(
        seeds=("https://docs.test/mvcc",),
        profile="general",
        profile_params={},
        max_pages=1,
        max_depth=0,
        l1=L1Policy(mode="off"),
    )

    now = datetime.now(UTC)
    env = pipeline._build_envelope(
        job,
        spec,
        "policy-v1",
        req,
        p,
        "accept",
        ["triage:accepted"],
        [],
        None,
        [],
        None,
        now,
    )

    # Validate against strict contracts
    validated = validate_envelope(env)
    assert (
        validated["extensions"]["collector:triage"]["triage_status"] == "auto_accepted"
    )
    assert validated["quality"]["relevance"]["status"] == "measured"
    assert validated["quality"]["relevance"]["value"] == 0.65
    assert validated["quality"]["relevance"]["scale"] == "similarity"
    assert validated["quality"]["relevance"]["query_id"] is not None
