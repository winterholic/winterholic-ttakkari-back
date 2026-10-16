# 회의 분석 개인 계정·모델 순서 (2026-10-16)

사용자가 회사 회의 분석 실패 화면(켜진 회사 엔진 계정 없음)을 보내며 회사 계정 필수 조건을 없애고 기본 개인 계정의 Luna → Antigravity Flash → Antigravity Sonnet → native Sonnet 순서를 요청했다. 전체 오케스트레이터/작업자 정책은 변경하지 않는다.

- 기존 `test_project_scope_rules_and_work_pool`을 바꿔 개인 Claude만 켜진 회사 회의에서 기존 오류를 재현했다(1 failed, 기대 원인: 켜진 회사 엔진 계정 없음).
- `meetings/routing.py`에서 켜진 `pool=personal` 계정만 고르고 단계별 한도와 cooldown을 확인한다. personal 모드의 일반 동작(회사 계정도 허용)과 달리 이 분석 경로에서는 회사 계정을 제외한다.
- 모델 순서는 고정하되 같은 단계 계정끼리는 기존 잔여 한도/동시 실행 분산을 쓴다. Antigravity 두 모델의 quota·대화 ID를 각각 유지한다. 실행 중 한도/실행 파일 부재는 남은 단계로 넘어가며 모두 소진되면 기존 한도 초기화 대기를 사용한다.
- 세션 pool은 회의 scope 그대로 둬서 기존 MCP 조회·맥락·프로젝트·검색·승인 범위를 유지한다. 분석 Run 연결을 기준으로 별도 선택을 적용하며, 일반 Run의 계정 선택은 유지한다. 기존 meeting_analysis_model 힌트보다 사용자 지정 고정 체인이 우선한다.
- 가짜 Codex에도 기존 가짜 Claude와 동일한 회의 JSON 응답을 제공해 Luna 우선 전환 후 기존 파이프라인 검사를 유지했다.

## 검증

분리 테스트 DB `ttakkari_test_meeting_routing_1016` 사용. 최초 기본 테스트 DB 실행은 과거 clovanote 테이블 FK가 남아 schema setup에서 실패했으며, 운영 DB 문제는 아니다. 별도 DB 생성 후 원래 오류를 재현했다. 전체 소진 검사의 첫 실행은 대기 Run 정리가 없어 teardown을 기다렸다. 그 테스트 DB의 queued Run만 cancelled로 바꿔 종료했고 테스트 자체에도 cancel API 정리를 추가했다. 운영 데이터 변경은 하지 않았다.

실행 명령:

```sh
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_test_meeting_routing_1016 .venv/bin/pytest tests/test_meeting_routing.py tests/test_meetings.py tests/test_meeting_scope.py tests/test_meeting_pms.py tests/test_meeting_exports.py tests/test_account_selection.py tests/test_accounts.py tests/test_api_runs.py tests/test_antigravity_model_routing.py tests/test_generations.py -q --tb=short --junitxml=/Users/winterholic/.ttakkari/data/outbox/1cf87380-1a32-4b32-aec6-9c140e51c801/meeting-routing-tests.xml
```

결과: **204 passed in 57.53s**. 변경 Python 파일 Ruff `All checks passed!`, `git diff --check` 통과. 합성 엔진으로 네 단계 런타임 한도 전환·같은 Antigravity 계정의 모델별 대화 분리·Luna 5시간/주간 상한·Antigravity 그룹별 소진·크레딧0·전체 소진 대기를 검사했다. 실계정 LLM 응답 검증과 구분한다.

운영 DB에는 READ ONLY 트랜잭션으로 계정 메타데이터·저장된 usage/cooldown만 읽었다. 새 소스 기준 후보는 개인 Codex 2개(Luna), 개인 Antigravity(Flash/Sonnet), 개인 Claude(Sonnet)이며 차단 후보 없음. 실제 모델·도구 호출이나 운영 회의 재분석은 실행하지 않았다.

수정 전 운영 `/api/ready` 직접 조회: active revision `110cbe857f21bac2849b76d0ed7d4ffb6f0b9964`, ready=true. 새 소스의 운영 반영·실제 회의 분석 성공은 배포 후 별도 확인이 필요하다. 검증한 clean commit을 push하고 generation 배포 예약을 요청한다. 예약 접수는 배포 성공이 아니며 폴링하지 않는다.
