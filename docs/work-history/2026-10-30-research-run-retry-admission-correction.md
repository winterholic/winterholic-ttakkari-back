# Research Run retry admission correction

- 날짜: 2026-10-30
- 기준 backend topic: `topic/research-lab-v2-execution-20261030`, 시작 HEAD `165e80147d79ec42c1259b14e8bcd10b38abd949`; 부모 제품 구현 `add75ebda42759055a6b9fe0146fb5d1c8fb4ecd`, base `8deece0d14dfd2e0bc07d8d594e1c939d767f944`.
- 관련 frontend: `topic/research-run-retry-lineage-20261030` at `ff129e8f7458225d3a50158d5b96f0cb4e3c2877`; 수정 없음.
- 독립검증: verifier `951`, Run `2d7ba429`, 전체 근거는 `research-run-retry-independent-20261030.md`. 해당 독립 보고서는 편집하지 않음.
- 원인: retry만 Experiment `FOR UPDATE`와 queued/running 검사를 했고 normal start는 plain read라 두 경로가 같은 실행 슬롯을 직렬화하지 않았다. 또한 retry는 normal start의 현재 실행 계획 validator를 건너뛰었다.
- 수정: 두 경로가 같은 Experiment row lock → active/queued 검사 → 공통 현재 plan validation → owner/run/event reservation → commit 순서를 사용한다. 잘못된 plan은 owner/Run/Event 생성 전에 422로 끝난다. Retry의 config ID, repeat index, seed, fresh raw output, 계보, actor, 원본 불변 조건은 유지한다.
- 검증: 신규 `tests/test_research_retry_admission_unit.py` 5개 통과, Ruff 통과. exact commands/output과 실행환경 한계는 [evidence](../research-lab/evidence/research-run-retry-admission-correction-20261030.md)에 기록.
- 시험범위 주의: 테스트 함수는 fake DB로 retry↔normal, normal↔normal check/insert 경합과 invalid planner 결과를 확인했다. 공통 pytest conftest가 격리 temporary PostgreSQL cluster를 자동 준비했지만, real PostgreSQL 동시 트랜잭션 반례는 실행하지 않았다. 따라서 PG 동시성은 미검증이다. 기존 uv cache permission denial과 import-root 설정 실패를 기록했고, 알려진 PG 경합 probe를 재실행하지 않았다.
- 기존 execution owner 취소/스케줄 실패/복구 코드는 수정하지 않음. 과거 owner lifecycle evidence는 원 revision 근거로 유지하며 이번에 전체 suite를 반복하지 않음.
- 원 Research v2, R1–16, native checkpoint, 합산 OS memory, legacy 92, PMS 7.x/8.3–8.6, UX10은 미완 유지. backend main 통합·게시·배포 실행 owner는 `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`이며 root는 기획·감독이다.
