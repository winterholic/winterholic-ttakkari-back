# 2026-10-27 KB v3 실검증 호스트 오퍼레이션 교정 및 라이프사이클 계약 복원

- **작업 브랜치**: `fix/kb-v3-operation-fixes-20261027` (Base: `a873a2a650e4ae3e627346056a82a67826e41e97`)
- **작업 영역**: Sibling worktree `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-operation-fixes-20261027`
- **커밋**: `658d2db2ea7953b45ec6cbe000bbc97741fdac7f`

## 1. 교정 배경 및 원인 분석
독립 검증(`review/kb-v3/l2-independent-20261027/report.md`) 결과 exact `f774928b`의 4대 결함과 `1e5d7bb2`의 제품 코드 오염 확인:
1. **False positive success**: `held` 3건, 0 L2 호출, 0 assertion/evidence/conflict 상태에서도 `succeeded` 반환.
2. **글로벌 백로그 침범**: `get_default_worker().pump()` 및 전역 `_tasks` gather로 운영 큐의 무관한 사용자 일감을 선점 및 블로킹.
3. **INLINE 라이프사이클 계약 위반**: 무거운 작업임에도 INLINE에 등록되어 취소 요청 후에도 요청 코루틴이 계속 돌아 `succeeded`로 덮어쓰고, 동시성 제어(단일 heavy, dedup) 부재.
4. **1e5d의 잘못된 하드코딩**: 개발 작업자 모델 정책(`verification_required`, `recommended_verifier: Claude 4.6`)이 제품 응답에 삽입됨.

## 2. 교정 내역
1. **Success Gate 계약 엄격화 (`src/ttakkari/operations/kb_v3_verify.py`)**:
   - 정확히 3개 문서 / 3개 ready 잡 일치 확인.
   - 각 잡의 `extracting` 단계에서 실제 L2 호출 성공 증거(`calls_made > 0`, `adapter != 'disabled'`, `available is True`, `status == 'succeeded'`) 필수.
   - 현재 3개 리비전 source-backed exact quoted claim/evidence 검증 (`KbEvidence`의 `span_start:span_end`가 `KbPassage.body` 내 유효 구간).
   - 30일 vs 90일 정책 간 동일 주체(`subject_entity_id`) 및 동일 predicate, 상이한 값의 충돌(`KbConflict`) 필수.
   - failed/held/degraded/partial/disabled 정직 보고 및 `counts.documents` 실제 DB 쿼리 결과 반환.
   - 1e5 하드코딩(`verification_required`, `recommended_verifier`) 완전 배제.
2. **글로벌 worker.pump 및 공유 _tasks gather 제거**:
   - 제품 ingest/enqueue 후 트랜잭션 commit으로 일반 큐 소유권 명시.
   - 런타임에서는 순수 관측(observation) 루프로 동작.
   - 테스트 등 명시적 worker 전달 시에도 대상 3개 ID만 안전하게 claim하고 단일 task handle만 finite await.
   - 루프마다 잔여 상한 시간(`deadline - time.monotonic()`) 적용.
3. **Host Operation 라이프사이클 계약 복원 (`plans.py`, `runner.py`, `inline.py`)**:
   - `plans.INLINE`에서 제외, `plans.HEAVY`에 `kb_v3_verify` 등록 (180초 타임아웃).
   - `plans.make_plan`: `python -m ttakkari.operations.kb_v3_verify <job_id>` 러너 계획 수립.
   - `runner._summarize`: stdout의 JSON summary 파싱 및 검증 상태 판정.
   - 동시 실행 시 동일 kind dedup, 다른 heavy 충돌 가드(`jobs.Conflict`), 취소 시 cancelled 보존.
4. **재실행 멱등성 및 증거 격리**:
   - 멱등 3고정 key 유지 및 stale assertion/unrelated conflict 배제.

## 3. TDD 검증 결과
- **음성 RED**: 6 failed, 5 passed (Exit Code: 1, `review/kb-v3/operation-fixes-20261027/red-output.txt`).
- **최소 교정 GREEN**: 14 passed in 1.77s (Exit Code: 0, `review/kb-v3/operation-fixes-20261027/green-output.txt`).
- **회귀 검증**: `test_kb_v3_default_activation.py` 6 passed (0.03s).

## 4. 범위 및 미실행 항목
- `미실행: PostgreSQL 서버 및 prod 호스트 오퍼레이션 실기동`: 샌드박스 제한 및 안전 지침에 따라 SQLite 메모리 검증 수행.
- `미실행: 실제 클라우드 LLM 유료 호출`: subscription_cli / gpt-6-luna 원칙 준수.
- `미실행: git push 및 backend deploy`: root 저장소 소유 정책 준수.
