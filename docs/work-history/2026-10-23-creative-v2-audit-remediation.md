# Creative Studio v2 — 감사 결과 기반 수정 (2026-10-23)

범위: 구현 실태 전수 감사(A: 코드·카탈로그·파이프라인, B: 도구 연결·실행 테스트) 이후 별도 구현 세션이 수행한 수정. 기준 backend `4f14f380c1aa3af435384bed53f762a11e6e2831`, frontend `e4de26f519cf329ad04f9e86fa7aec68b994eff8`.

## 입력과 한계

- 감사 A/B 원문(`studio-audit-part-a.md`, `studio-audit-part-b.md`)은 이 구현 Run에서 ttakkari 보호 영역 가드(PreToolUse)에 막혀 **직접 읽지 못했다**. 이는 이 Run 환경의 제한이며 운영 불능을 뜻하지 않는다. 대신 감사 세션 A/B의 `read_session` 요약 결과(핵심 판정)와 현재 코드·실제 실행으로 지적을 재현해 판정했다. 감사 최종 통합표는 받지 못했고, 도착하면 추가 항목을 반영한다.
- 감사 요약에서 확인한 지적: v2 계획에서 실제 렌더 실행으로 이어지는 경로 미구현, 15분 영상은 정책상 허용만 되고 분할 렌더·체크포인트가 계획에 없음(D 시나리오 `deviated`, 분할 없음), 시나리오 A~C 의도 추출은 `lexical_hint`뿐.

## 지적별 판정

| 지적 | 판정 | 조치 |
|---|---|---|
| 긴 영상에 분할 렌더·재개 체크포인트가 계획에 없음 | 유효(코드 재현: 계획에 구간 필드 없음, v1 렌더러는 프로젝트 240초·장면 60초 한도) | 수정: `plans.render_plan` |
| 어휘 힌트가 영문 부분 문자열을 장르로 읽음 | 새로 재현(`bank transfers`→`C02.space_sf`, `build`·`quick`→`C03.ui_animation`) | 수정: 영문 끝 단어 경계 |
| 힌트가 "설명 영상"·"개념 설명"을 못 잡음 | 유효(`Kafka 개념 설명 영상` → 선택 0개) | 수정: `C01.tech_explainer` 힌트 추가 |
| 시나리오 A~C가 LLM 없이 `lexical_hint` | 사실이나 설계상 폴백. Run 기반 LLM 경로는 코드에 있고 배포 revision에서 미검증 | 변경 없음, 미검증 유지 |
| v2 계획 → 실제 렌더 실행 미연결 | 유효. `assert_renderable`을 호출하는 렌더러가 없고 v1은 사용자 업로드 자산(장면별 이미지·음악)을 요구해 자산 제작 엔진(G34 `director.py`/`managed_comfy.py` 작업)과 맞물린다 | **미수정**(범위·동시 작업 충돌). 새 기능으로 확대하지 않음 |
| 레시피 20개 실행 검증 | 현재 코드 재현: blocked 10, unverified 8, capabilities_executed 1, recipe_demonstrated 1. 숫자 그대로 정직하게 표시 중 | 오탐 아님·수정 대상 아님(데이터가 현실을 반영) |
| 15분 지원 | 계획·길이 정책은 900초까지 가능(`target_met` 재현), 렌더는 미연결 | 계획 단계 분할만 수정, 렌더는 미완 |

## 변경

- `src/ttakkari/creative/v2/plans.py`: `render_plan(plan)` 추가. 장면 경계에서 60초 목표로 구간을 나누고, 구간마다 위치 비의존 `input_sha256`(장면 내용·구간 내 프레임·화면비·fps·Style DNA)을 둔다. 조회 때 장면에서 다시 계산하며 저장하지 않고 승인 해시에서도 제외한다(기존 승인 계획의 호환 유지). `executed: false`. 장면이 렌더러 장면 한도 60초를 넘으면 경고.
- `src/ttakkari/creative/v2/intent.py`: `_hint_pattern`으로 영문·숫자로 끝나는 힌트는 단어 경계에서만 맞춤. 한글 조사(`UI를`)는 그대로 허용.
- `src/ttakkari/creative/v2/catalog_data.py`: `C01.tech_explainer` 힌트에 `개념 설명`, `설명 영상` 추가.
- 테스트: `unit_tests/test_creative_v2_plans.py`(분할 4건), `unit_tests/test_creative_v2_matching.py`(힌트 경계·조사·신규 구문), `tests/test_creative_v2_api.py`(API 응답에 `render_plan`).
- frontend: `src/api/creativeV2.ts` 타입, `CreativePlanPage.tsx` 승인 패널에 분할 렌더 계획(미실행 명시, 구간 키, 렌더러 한도 경고), 테스트 2건. 구서버 응답(필드 없음)도 허용.

## 검증 (작업 checkout `ttakkari-studio-v2-back`/`-front`, 테스트 환경 `uv run --frozen`)

- `pytest tests/test_creative_v2_*.py unit_tests/test_creative_v2_*.py tests/test_creative_*.py` → `163 passed`.
- `pytest unit_tests` 전체 → `1 failed, 1585 passed`. 실패 `test_generation_hardening.py::test_db_create_schema_record_failures_carry_their_stage`는 수정 전 HEAD(`4f14f38`) 새 worktree에서도 같은 단언(`'test-db' == 'schema_contract'`)으로 실패하는 기존 문제이며 이번 변경과 무관. 이번 Run에서 원인은 조사하지 않았다.
- `ruff check src/ttakkari/creative unit_tests/test_creative_v2_*.py` 통과. `ruff format --check`는 수정 전에도 5개 파일이 기준 미달이라 일괄 포맷하지 않았다.
- frontend `pnpm exec tsc --noEmit` 0, `pnpm lint`(ui-lint 138건, 증가 없음), `pnpm exec vitest run` → `1312 passed | 10 skipped`, `pnpm build` 성공.
- 실제 계획 생성 실행: 60개 정보 항목 + 희망 900초 → `target_met`, 900초, 60장면, 15구간, `requires_split: true`, `executed: false`. 오탐 재현 입력 `I build a quiz about bank transfers` → 선택 0개. 로그: Run outbox `work/evidence_run.out`.

## 미완·미검증

- 계획 → 실제 렌더 실행 연결(자산 제작 포함)은 하지 않았다. `render_plan`은 계획 데이터이며 렌더 증거가 아니다.
- 브라우저 실측은 하지 않았다(이전 기록과 같이 인증 세션이 필요). UI는 jsdom 테스트까지만 검증.
- 실제 Qwen intent 응답, 배포 revision의 Run 경로, MCP 실행, resume은 계속 미검증.
- 감사 최종 통합표 및 원문 직접 대조는 미완.
