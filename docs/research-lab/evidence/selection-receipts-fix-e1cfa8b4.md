# 실행 영수증이 실패 적용 조건으로 읽히던 교정

2026-10-26 리드 Codex가 `e1cfa8b4`에서 직접 재현·수정했다. 실제 모델 RSS 감시
인수 도중 runtime_report가 선행838ae3e6/a2c594f6와 d1204079/60bb4e24의 실패를
새로 보여줬다. ops_deploy_log(summary)에서 두 controller가 동일한
`test_real_failure_reappears_in_followup_selection_without_global_exclusion[True]`
의 unknown!=applicable로 실패한 원문을 확인했다. d120은1failed/2909passed·907.53s였다.
원문은 `selection-deploy-failure-d1204079.json`이며 로그 끝의 Pydantic 경고를
실패 원인으로 취급하지 않는다. 2a8/e1cfa8b4는 당시 testing이며 실패 확정 아님.

## 원인과 변경

콜스택: 실패 Run→service.selection_failures가 원본 environment_snapshot을 전달→
rank_methods가 전체 observed_environment를 조건으로 해석→applicability가 다음 연구에
같은 research_execution_owner 토큰이 없음을 missing으로 반환했다. 3e38ecf3의
영속 소유권부터 실행 영수증이 모든 Run에 붙었으며, 그 이전 규칙은 이를 구분하지 않았다.
현재 RunPG/ASGI에서도 단일 원래 검사1failed0.85s로 같은 현상을 재현했다.
모델·GPU·명령 sandbox 실패가 원인이 아니다.

rules.py에서 서버 소유의 정확히5개 실행 관리 필드(소유권·외부자원·모델자식·RSS감시·
복구 영수증)만 observed 조건 투영에서 제외한다. 원본 observed_environment는 그대로
반환·저장하며 execution_metadata_fields로 분리된 필드 이름을 공개한다.
research_* 접두사 전체를 무시하지 않는다. 다른 관측 조건과 명시적 declared 조건,
선언/관측 충돌, unknown·scope·bounded-history·원본 실패 ID/오류/인과미평가를 보존한다.
미측정 실패 원인이나 방법 무효성을 추정하지 않는다. schema/protocol 변경 없음.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest 'tests/test_research_selection_failures.py::test_real_failure_reappears_in_followup_selection_without_global_exclusion[True]' -q --timeout=20 --tb=short
1 failed in0.85s: unknown!=applicable; 의도적 invalid corpus KeyError 뒤 원본 실패행은 저장됨.
$ ... pytest tests/test_research_selection_failures.py -k 'execution_receipts or unrecognized_observation' -q --timeout=20 --tb=short
17 failed in0.84s: 구현 전 metadata와 조건 분리 반례. 앞선 fixture title 누락17error0.87s는 검사 입력을 고쳐 분리했다.
$ RESEARCH_FAILURE_RECEIPT=<이번Run outbox>/research-selection-receipt.json PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_selection_failures.py tests/test_research_selection_context.py tests/test_research_selection_evidence.py unit_tests/test_research_rules.py -q --timeout=20 --tb=short
최종50 passed in1.47s (중간48pass와 합산하지 않음).
```

50에는 legacy/v2 실제 실패→후속A/B/미지정 환경의 eligibility·fresh resume timeline,
5필드×3환경·원본불변, 관측 충돌(영수증 유/무), 미등록 research_custom_condition,
영수증만 있는 불명조건·명시 선언조건 보존이 포함된다. v1/v2 원시API 응답은
`selection-receipts-e1cfa8b4.json`이다. controller 전체 재실행이나 운영 인수가 아니다.
관련 메모리 제품7소스는 변경되지 않아 실제 모델 결과를 재실행하지 않았다.

원목표/PMS7.2·7.3·6.2·8.6은 미완으로 유지한다. calibration·Registry 추천 UI,
전체 메모리 예산·native checkpoint/GPU 기록틈·운영 Metal/별lease·인증UI·전체gate는
별도 원조건이다. 필수 push hook/원격 SHA/누적 배포 예약은 영속 계약의 후속 영수증에 연결한다.
