# Research Run 재실행 계보 — 2026-10-30

## 요구와 실제 차이

원요구 정본 `refactor-v2-requirements.md` §9–11은 반복 관측을 기능 완성으로 바꾸거나 이미 끝난 모듈을 재감사하지 말고 실행 가능한 제품 경로를 계속 닫도록 한다. 기존 실험 모니터의 “다시 실행”은 experiment/config 단위 실행 요청을 호출해 새 Run의 기본 seed와 repeat index를 만들었다. 따라서 실패·취소·성공 Run을 재현할 때 원 조건을 보존하고 원본과 연결하는 사용자 여정이 없었다.

이번 범위는 terminal Run의 동일 조건 재실행과 계보 보존이다. 기존 R1–16, UX10 및 A–D 생성·설정·실행·비교·결과물·재진입 증거는 출발 맥락으로 재사용했으며, 이번 변경에서 전체 요구의 재검증을 주장하지 않는다.

## 구현

- `POST /api/research/runs/{run_id}/retry`는 succeeded/failed/cancelled의 `local_measured` 또는 `migrated_v1` Run만 받는다. 외부 보고서, 활성/대기 Run, 중복 실행 및 설정이 없는 Run은 거부한다.
- 새 Run은 새 ID를 갖고 configuration, repeat index, seed를 원 Run에서 복사한다. 새 raw output으로 시작하며 원 Run을 수정하지 않는다. 실험 행 잠금과 기존 실행 소유권 경계를 이용해 중복 재실행을 막는다.
- 원본 Run ID를 새 Run의 `environment_snapshot.research_retry`에 기록하고 실행 중 adapter snapshot이 이 계보를 덮어쓰지 않도록 보존한다. `run_retry_created` 이벤트에 actor와 조건을 남긴다.
- 모니터와 상세 화면은 Run ID로 이 경로를 호출하고, terminal Run에 “원 조건 다시 실행” 동작을 제공한다.
- 이는 새 독립 실험 실행이다. 모델 학습 상태를 이어가는 native checkpoint resume가 아니다. 합성 checkpoint를 학습 resume 증거로 취급하지 않는다.

## 검증 근거

Backend implementation commit: `add75ebda42759055a6b9fe0146fb5d1c8fb4ecd` (topic branch `topic/research-lab-v2-execution-20261030`). Frontend topic commit: `ff129e8f7458225d3a50158d5b96f0cb4e3c2877` (branch `topic/research-run-retry-lineage-20261030`). Both are based on the recorded backend/frontend bases below and remain topic-only.

기존 integration worktree의 잠금 의존성/테스트 환경을 사용해 새 backend topic의 코드를 검증했다.

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:../back-research-v2-integration/src UV_CACHE_DIR=/private/tmp/ttakkari-uv-research-v2-execution uv run --locked --project ../back-research-v2-integration pytest tests/test_research_runner_lifecycle.py -q --timeout=25 --tb=short
45 passed in 2.71s

UV_CACHE_DIR=/private/tmp/ttakkari-uv-research-v2-execution uv run --locked --project ../back-research-v2-integration ruff check src/ttakkari/research/runner.py src/ttakkari/api/research.py tests/test_research_runner_lifecycle.py
All checks passed!
```

추가 신규 검증은 failed terminal Run에서 seed/repeat/configuration 유지, 원본 raw 보존, 새 Run ID 및 계보 유지, 실제 단일 adapter 재실행, 중복 재시도 409; 활성 Run 재시도 409를 lifecycle 통합 테스트로 확인했다.

프런트 topic에서:

```text
pnpm exec vitest run src/pages/research/research.test.tsx -t "retries one terminal Run"
1 passed, 67 skipped

pnpm exec vitest run src/pages/research
8 test files passed, 96 tests passed

pnpm exec eslint src/pages/research/api.ts src/pages/research/components/RunDetailDialog.tsx src/pages/research/views/ExperimentMonitorView.tsx src/pages/research/research.test.tsx
exit 0

pnpm build
exit 0; production build and PWA generation succeeded (existing large-chunk warning)
```

## 범위 밖 / 미실행

- 전체 R1–16 및 UX10 대표 여정의 통합 인수, legacy 92, 전체 PMS 7.x, 8.3–8.6는 완료로 판정하지 않는다. 상세/목록 소유권 delta의 독립 PASS는 해당 경계만 뒷받침한다.
- native ML 학습 checkpoint resume, 전체 aggregate OS 메모리 상한 및 모든 엔진별 자원 계약은 별도 잔여 요구다. 이번 재실행은 실제 지원 inference/RAG/TTS/STT/external adapter 중 허용된 기존 Run에 대한 독립 재실행이지 학습 복구 구현이 아니다.
- 브라우저 기반 인증 UI, 활성 운영 runtime/supervisor, 운영 checkpoint, 배포 동작은 관측/검증하지 않았다. 테스트는 API/lifecycle과 mock UI 계약 및 프런트 빌드 범위다.
- 제품상 다음 단계는 원요구 전체의 미구현 사용자 여정을 evidence 기준으로 좁혀 닫고, 그 뒤 독립 root 통합 검증을 받는 것이다. 본 topic은 통합/배포하지 않았다.
