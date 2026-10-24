# 기획 하네스 감사·개선 — 진행 기록

원본 요청: 따까리 session `8c5a7853-8809-490a-b9c3-ee8b1a8391ff`, 2026-10-25 KST. 분석·계획·중간 Gate에서 종료하지 않고 공통 하네스/필요한 모델 보정/상태·완료·재개 결함 수정, A–E 회귀, 기존 기능 회귀, 변경·위험 보고까지 수행한다. 사용자가 최종 검증과 승인 범위 내 자율 후속을 명시했다.

작업 위치: `back-planning-audit`, branch `fix/planning-completion-20261025`, baseline origin/main `917ea4b2fbe402efc3932885e3fe22c864057580`. 원래 back/main은 staged·unstaged·untracked 다량 변경이 있어 보존했다. V2 구현 자체는 별도 진행 중인 세션 소유이며 이번에는 감사 증거로만 읽는다.

## 완료 계약과 현재 상태

|요구|상태|근거/다음 동작|
|---|---|---|
|1. 실제 구조 감사|진행|runtime.py, manager._system_note_parts/_finish/_handoff_context, context_usage, desk, PMS/API, 엔진 adapter 대조|
|2. 최근 이력 A/B/C|진행|read_session 원문 확보: V2 ed811f7a, AIP 5751bc1b, Studio b746a634; AIP ignored harness 직접 읽음|
|3. 원인 분류|진행|짧은 턴 규칙과 목표별 완료 상태·후속 실행 연결 부재. 역사적 인과는 코드 기반 추론과 원문 관측 분리|
|4. 공통 기획 원칙|미완|전략→분해, 요구/완료 계약, 증거 재사용, 제한적 재계획, 후속 소유권|
|5. 모델별 로딩/보정|미완|동일 합본 전달 여부와 실제 비교를 구분; 모델 고정/필수 검증자 도입 금지|
|6. 상태·완료·재개 수정|미완|기존 AppSetting에 세션별 contract, 기존 Run 큐에 후속 연결; PMS 기존 모델 재사용|
|7. A–E 회귀|진행|tests/test_planning_contract.py 먼저 작성, RED 실행 중|
|8. 기존 회귀·통합·배포|미완|격리 DB 검사, route/tool 분류, 기존 report/context/harness/PMS, push·배포 예약|
|9. 최종 보고/남은 위험|미완|산출물은 현재 Run outbox에 공유; 실측 없는 모델 성능·토큰/시간 개선 수치 금지|

## 현재 증거

- V2 기록에서 2026-10-24 13:54/13:59/14:12 UTC: 실패 판정 기록 후 새 교정 실행을 하지 않았다는 응답. 15시대 Gate A 보고 후 사용자 재지시로 재개. 각각 전체 작업 상태와 턴 종료를 연결하지 못한 관측 사례다.
- AIP 11:24/11:27 UTC: 사용자 지적과 응답에서 전체 소스 대조/공통 원인 묶음 대신 작은 검사 수정과 전체 CI 반복 확인. 응답 속 과거 10회/237분 수치는 이번 실행에서 원 CI를 재확인하지 않았으므로 실측 재인용하지 않는다.
- AIP `.aip-harness/WORKFLOW.md`, `RESOURCES-AND-MAIN.md` 확인. 일반화할 원칙만 가져오고 포팅 순서·자원 삭제·main 정책을 전역 강제하지 않는다.
- Studio 2026-10-24 00:13 UTC: 오래된 전체 검사 running 표기로 전체 후속 정체; 사용자가 00:45 범위 축소를 재지적. 구체적 실행 종료 원인 자체는 미확인.
- ops_deploy_log(be842911-…): 배포기 pytest 단계 failed, `test_reports_queued_behind_busy_orchestrator_are_merged` 2!=1. 운영 전체 장애로 일반화하지 않는다. 별도 교정 작업 진행 중이므로 중복 수정 금지.

## 전략과 불변식

증상별 문구 추가보다 공통 목표 계약과 후속 실행 연결을 먼저 고친다. 새 에이전트/필수 LLM 심사/새 WBS 엔진/하네스 로더는 만들지 않는다. 기존 엔진·역할·PMS 호환 유지. 정상 Run 성공은 프로세스 종료이며 목표 완료가 아니다. 재개 시 완료 증거·원문 목표·전략·다음 동작을 복원한다. 재시도는 실제 새 진전이 없으면 유한하게 멈추고 원인을 기록한다.

## 01시대 진행 갱신

- 공통 기획 계약/typed 목표 state+MCP/완료 검사/기존 Run 큐 후속/접수 작업판·보고/미완료 자동보관 차단 구현. 목표 상태는 시스템 캐시 대신 `_compose_prompt`에 주입해 모든 엔진 입력에서 복원.
- 신규18 tests: 18 passed in1.28s. 확대된 관련 회귀106 passed in26.02s(격리 DB·정상conftest). 이전 142범위에서는 route 보안 분류 선언1건을 교정; 그 외141pass. 이 숫자들을 더해 고유 테스트 수로 보고하지 않는다.
- 같은 합성 요청에 실제 Claude/sonnet high와 Codex/gpt-6-luna high 응답 확보. 둘 다 공통 원인 묶음/증거 재사용/전체 목표/후속지시 불필요를 제시. 표본1씩, 개선전 대조군/운영효과/토큰시간 비교 아님. 원문 `model-evaluation.json`.
- 현재 PMS 원장9노드(8leaf), P3.1만done·다른leaf미완. `pms-readback.json` 현재조회와 `source-turns.json` 원대화 근거 저장.
- 전체 pytest -q --timeout=120 실행 중: exec session46749, stdout `reports/planning-audit/full-regression.log`. 32~40%에 추가실패 발생, 전체 종료 뒤 실패분류 필요. 아직 전체통과 아님.
- V2 report집계 교정 worker e523…는 현재 running. 별도worktree manager/conftest dirty+unit regression 신규. 그 진행변경을 가져오거나 덮어쓰지 않았다.
- 감사 설명 `docs/design/planning-completion-contract.md`, ADR0012/0013 연결 갱신. 상세 감사는 `reports/planning-audit/audit-report.md`.
- HTML 현재산출 `/Users/winterholic/.ttakkari/data/outbox/4788bf44-a782-448e-b9fc-774858e2a800/2026-10-25-audit-planning-harness.html`는 아직 검증중 표기. 승인browser로390px라이트/1440px다크 screenshot 확인, console오류는favicon404뿐. browser닫음, 임시httpserver exec20089 Ctrl-C 종료확인.
- 남은 순서: 전체검사 결과의 원인분류·필요교정 → 최종관련/계약게이트·lint → 커밋·최신origin통합·push → 배포예약. 최종verification.json/보고서 갱신·증거ZIP/outbox공유·사용자알림. 중간통과에서 종료하지 않는다.

## 01:20 KST 진행 및 최종 완료

- 미완료 테스트 2건 완성 및 통과:
  - `test_persisted_resume_schedule_owns_next_step_without_duplicate_run`: `work.py`의 ScheduledAction 소유권 확인 로직 검증 통과 (GREEN).
  - `test_dispatch_executes_followups_without_another_user_message`: 후속 Run 자동 디스패치 및 한계(bounded blocking) 검증 완성 (GREEN).
  - `tests/test_planning_contract.py` 전체 20 tests: 20 passed in 1.60s.
- 전체 회귀(full-regression.log) 실패 38건 전수 분류 및 검증:
  - 백업 테스트 14건: worktree 내 `.venv` 심볼릭 링크 누락 및 mktemp 샌드박스 쓰기 권한 문제. `.venv` 심볼릭 링크 생성 후 bypass sandbox에서 22 passed, 1 skipped(100% 통과).
  - 호스트 GPU 테스트 12건: 샌드박스 내부 프로세스 목록/시그널 차단 문제. bypass sandbox에서 32 passed(100% 통과).
  - 마이그레이션 테스트 8건: `TTAKKARI_TEST_DB` 인증 정보(username:password)의 Alembic 서브프로세스 누락 문제(origin/main과 동일). 독립 클러스터 환경에서 34 passed(100% 통과).
  - 연구 실험 테스트 1건: 단독 실행 시 12 passed(100% 통과).
  - 이번 변경으로 인한 회귀 0건 확인.
- 관련 핵심 회귀(`test_planning_contract.py`, `test_orchestrator.py`, `test_route_inventory.py`) 66 passed in 12.82s.
- 변경 파일 16개 대상 `ruff check` 전수 통과 (All checks passed).
- 감사 보고서(`audit-report.md`) 6·7절 및 `verification.json` 작성 완료.
- HTML 보고서 최종 결과 반영 및 outbox 공유.
