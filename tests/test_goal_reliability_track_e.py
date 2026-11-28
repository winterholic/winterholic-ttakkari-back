"""Track E: 계약 접수 휴리스틱(E-1), 긴 프롬프트 완료 조건 절 우선 추출(E-1 추가), safe_intake 오류 보존 및 알림(E-2), 경로별 소유권 매트릭스(E-3) 시험."""
from __future__ import annotations

from uuid import UUID

import pytest
from goal_helpers import (
    contract,
    get,
    lead,
    put,
)
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    Run,
    RunOrigin,
    RunStatus,
)
from ttakkari.orchestrator import goal_signals, work


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "dispatch", _noop)


# --------------------------------------------------------------------------------------------------
# E-1: 계약 접수 휴리스틱 테스트
# --------------------------------------------------------------------------------------------------

def test_e1_intake_heuristics_complex_and_casual_inputs():
    """E-1: 번호 목록, 다단계, 복합 요청은 등록(is_complex=True), 단순 대화/질문은 미등록(is_complex=False)."""
    # 1. 번호 목록 및 다단계 요청 -> True
    req1 = "다음 작업을 순서대로 진행해줘:\n1. DB 스키마 마이그레이션 작성\n2. API 엔드포인트 구현\n3. 통합 테스트 작성"
    assert goal_signals.is_complex(req1) is True

    # 2. 다단계 마크다운 요청 -> True
    req2 = "### 1단계: 버그 원인 분석\n- 로그 확인\n### 2단계: 패치 적용\n### 3단계: 배포"
    assert goal_signals.is_complex(req2) is True

    # 3. 짧은 복합 요청 -> True
    req3 = "로그인 API 구현하고 단위 테스트 작성해서 통과시켜줘"
    assert goal_signals.is_complex(req3) is True

    # 4. 복합 분석 및 구현 요청 -> True
    req4 = "현재 캐시 구조를 분석하고 redis 기반 분산 락으로 전환하는 계획을 세워 구현해줘"
    assert goal_signals.is_complex(req4) is True

    # 5. 일반 대화 -> False
    assert goal_signals.is_complex("안녕하세요! 좋은 아침입니다.") is False
    assert goal_signals.is_complex("수고하셨습니다. 감사합니다.") is False
    assert goal_signals.is_complex("ㅇㅋ 알겠어") is False

    # 6. 단순 단답형 질문 -> False
    assert goal_signals.is_complex("이 함수 파라미터 타입이 뭐야?") is False
    assert goal_signals.is_complex("pytest 실행할 때 특정 파일만 지정하는 옵션 뭐였지?") is False
    assert goal_signals.is_complex("지금 서버 포트 몇 번으로 떠 있어?") is False


# --------------------------------------------------------------------------------------------------
# E-1 추가: 긴 프롬프트에서 중간 사례 목록 대신 최종 완료 조건 절 우선 추출 테스트
# --------------------------------------------------------------------------------------------------

def test_e1_additional_long_prompt_completion_section_prioritization():
    """E-1 추가: 중간에 번호 목록(예: §11 사례 1~4)이 있고 뒤에 '## 15. 최종 완료 조건'이 있는 긴 프롬프트에서
    완료 조건 절의 항목을 우선 추출한다.
    """
    prompt = """
# 대규모 안정화 작업 지시서

## 배경 설명
현재 시스템의 불안정 요소와 과거 장애 사례를 검토한다.

### 11. 과거 장애 사례
1. 사례 A: DB 커넥션 풀 고갈로 인한 타임아웃
2. 사례 B: Redis 네트워크 분할로 세션 유실
3. 사례 C: 외부 API 대기 중 무진전 후속 반복으로 인한 8회 차단
4. 사례 D: 워커 종료 이벤트 누락으로 인한 행 걸림

## 설계 요구사항
위 장애 사례를 방지하기 위한 시스템 설계를 도출한다.

## 15. 최종 완료 조건
1. D-2 외부 의존성 대기 모델 구현 및 summary API 연결
2. D-3 이벤트 기반 자동 재개 및 중복 방지 구현
3. D-4 독립 요구사항 동시 진행 보장
4. E-1 명시적 완료 조건 절 우선 추출 휴리스틱 구현
5. E-2 safe_intake 오류 보존 및 warning 알림 구현

## 검증 방법
pytest 실행하여 모든 테스트를 통과시킨다.
"""
    items = goal_signals.extract_items(prompt)
    assert len(items) == 5, f"Expected 5 items from completion section, got {len(items)}: {items}"
    [it["label"] for it in items]
    texts = [it["text"] for it in items]

    # 중간 사례(사례 A~D)가 아니라 최종 완료 조건의 1~5 항목이 잡혀야 함
    assert any("외부 의존성 대기" in t for t in texts), "D-2 외부 의존성 대기 항목이 포함되어야 함"
    assert any("이벤트 기반 자동 재개" in t for t in texts), "D-3 항목이 포함되어야 함"
    assert not any("사례 A" in t for t in texts), "중간 배경 사례 A는 요구사항으로 잡히지 않아야 함"


@pytest.mark.asyncio
async def test_e1_additional_long_prompt_auto_contract_creation(client, auth, make_dir):
    """E-1 추가: 긴 복합 요청으로 Run 생성 시 자동 계약이 최종 완료 조건 항목들로 구성된다."""
    sess, h = await lead(client, auth, make_dir)
    long_prompt = """
# 신규 기능 구현 요청서
시스템 점검 사항:
1. 기존 모듈 점검 A
2. 기존 모듈 점검 B

## 세부 지침
## 15. 최종 완료 조건
1. 첫번째 핵심 기능 구현
2. 두번째 핵심 기능 검증
3. 문서화 작성
"""
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": long_prompt})
    assert r.status_code == 202, r.text

    view = await get(client, h)
    assert view["state"] is not None
    reqs = view["state"]["requirements"]
    assert len(reqs) == 3
    texts = [req["text"] for req in reqs]
    assert "첫번째 핵심 기능 구현" in texts[0]
    assert "두번째 핵심 기능 검증" in texts[1]
    assert "문서화 작성" in texts[2]


# --------------------------------------------------------------------------------------------------
# E-2: safe_intake 오류 보존 및 프롬프트 경고 알림 테스트
# --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2_safe_intake_error_captured_on_run_and_alerted_in_prompt_note(client, auth, make_dir, monkeypatch):
    """E-2: safe_intake 중 오류 발생 시 Run 저장은 성공하고, run.usage['contract_intake_error']에 원인이 보존되며,
    다음 턴 prompt_note 에서 경고 문구로 기획에 알린다.
    """
    sess, _h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    # intake_request 에 인위적인 예외 주입
    async def broken_intake(*args, **kwargs):
        raise ValueError("simulated parsing failure during intake")

    monkeypatch.setattr(work, "intake_request", broken_intake)

    complex_prompt = "1. 첫 작업\n2. 둘째 작업 수행해줘"
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": complex_prompt})
    assert r.status_code == 202, "계약 등록 실패가 요청 저장을 막아서는 안 됨"

    # Run.usage 에 contract_intake_error 가 보존되었는지 검증
    async with sessionmaker()() as db:
        runs = (await db.scalars(select(Run).where(Run.session_id == sid))).all()
        assert len(runs) == 1
        err_info = (runs[0].usage or {}).get("contract_intake_error")
        assert err_info is not None
        assert "simulated parsing failure" in err_info["error"]
        assert err_info["origin"] == "user"

        # prompt_note 가 다음 턴에 경고를 출력하는지 검증
        note = await work.prompt_note(db, await db.get(work.ChatSession, sid))
        assert "주의: 직전 복합 요청의 목표 계약 자동 등록이 실패했습니다" in note
        assert "simulated parsing failure" in note


@pytest.mark.asyncio
async def test_e2_simple_request_does_not_generate_intake_error_noise(client, auth, make_dir):
    """E-2: 단순 대화는 복합 요청이 아니므로 자동 계약 등록을 시도하지 않고 오류도 남기지 않는다."""
    sess, _h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "안녕하세요 반갑습니다"})
    assert r.status_code == 202

    async with sessionmaker()() as db:
        run = (await db.scalars(select(Run).where(Run.session_id == sid))).one()
        assert (run.usage or {}).get("contract_intake_error") is None

        note = await work.prompt_note(db, await db.get(work.ChatSession, sid))
        assert note == ""


# --------------------------------------------------------------------------------------------------
# E-3: 경로별 소유권 매트릭스 테스트
# --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [RunOrigin.report, RunOrigin.orchestrator, RunOrigin.system])
async def test_e3_non_user_desk_origins_never_create_contracts(client, auth, make_dir, origin):
    """E-3: report, orchestrator, system 등 비사용자/비접수 origin 은 복합 프롬프트가 들어와도 계약을 만들지 않는다."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    complex_prompt = "1. 작업 A\n2. 작업 B 진행해줘"
    async with sessionmaker()() as db:
        s = await db.get(work.ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="claude", prompt=complex_prompt,
                  origin=origin, status=RunStatus.queued)
        db.add(run)
        await db.flush()
        # safe_intake 호출
        res = await work.safe_intake(db, s, run.id, complex_prompt, origin.value)
        assert res is None
        await db.commit()

    view = await get(client, h)
    assert view["state"] is None, f"Origin {origin} must not create a contract"


@pytest.mark.asyncio
async def test_e3_explicit_resume_activates_paused_and_blocked_contracts(client, auth, make_dir):
    """E-3: user/desk 가 '재개해줘' 같은 명시적 재개 지시를 보내면 paused/blocked 계약이 active 로 복귀한다."""
    sess, h = await lead(client, auth, make_dir)
    UUID(sess["id"])

    # 계약 등록 후 일시중지
    c = contract(status="paused", blocker="사용자 일시중지 요청")
    assert (await put(client, h, c)).status_code == 200
    assert (await get(client, h))["state"]["status"] == "paused"

    # 명시적 재개 요청 전송
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "이전 작업 다시 재개해줘"})
    assert r.status_code == 202

    view = await get(client, h)
    assert view["state"]["status"] == "active"
    assert view["state"]["blocker"] == ""

    # blocked 계약도 동일하게 재개되는지 검증
    cur = await get(client, h)
    blocked_state = {**cur["state"], "status": "blocked", "blocker": "오류로 인한 차단"}
    assert (await put(client, h, blocked_state, revision=cur["revision"])).status_code == 200
    assert (await get(client, h))["state"]["status"] == "blocked"

    r2 = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "계속 진행해"})
    assert r2.status_code == 202
    assert (await get(client, h))["state"]["status"] == "active"
