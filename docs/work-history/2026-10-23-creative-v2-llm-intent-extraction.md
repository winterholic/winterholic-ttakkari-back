# Creative Studio v2 — Run-backed intent review (2026-10-23)

범위: 첫 수직 슬라이스 이후 필수 누락을 해소하는 다음 기능 단위. main 병합·배포·영상 렌더는 하지 않았다. 독립 검수 대기 중인 first-slice 기준 SHA는 backend `99a9e084a4716bce21adbc310d0e25f9694872b5`, frontend `02dd278e39b6cea5851283e00a3166de48bf6fe1`로 고정했다. 모든 후속 변경은 해당 브랜치 위의 별도 커밋이다.

## 구현

- 이전 구현의 직접 desk-Qwen 호출을 제거하고 기존 Ttakkari Run lifecycle로 변경했다. `/api/creative/v2/intent/runs`는 기존 Qwen local worker를 personal worker session으로 큐잉하고, 기존 activation/readiness·model allowlist·Run 저장·취소·resume 경로를 사용한다. 결과는 `/intent/runs/{id}`에서 저장된 Run 결과를 파싱한다.
- worker task type `creative_intent`는 MCP 서버 없이 실행하며 Run manager에서 쓰기 경로를 해당 Run의 임시 outbox로 제한한다. 프롬프트는 브리프를 JSON 데이터로 구분하고 지시문·도구 요청으로 따르지 않게 한다.
- 모델 출력은 strict JSON IntentSpec으로 파싱한다. 모델이 주장한 `user_explicit`, 필수(hard), essential 값은 각각 `llm_inferred`, soft, false로 강제한다. 선택·제약·전달 항목의 인용은 브리프와 대조하고, 누락·환각된 인용 및 schema mismatch는 초안으로 내보내지 않는다. LLM 출처는 질문이 비어 있어도 검토가 필요하다.
- UI는 기존 단순 확인 checkbox를 구조화 편집기로 교체했다. 카탈로그 선택, 선호/필수, 근거, 제약 유형·내용, 전달 항목·중요도·필수 여부, 미결 질문, 음악 길이를 편집한다. 직접 구조화 입력도 가능하다. 검증 후 사용자가 확인한 항목만 `user_structured`로 계획 생성 요청에 포함된다. 길이·화면 비율은 기존 선택 UI에서 적용하고 기존 계획 편집·재계산·승인 흐름을 쓴다.
- Run 시작·재개 요청 키를 URL에 보존해 응답이 유실된 경우 중복 Run 대신 동일 키로 기존 요청을 확인한다. 상태 조회 오류, 취소, 실패, 취소·실패 후 같은 session 재개 및 schema 불일치 표시를 연결했다.
- Style DNA 귀속 정정은 별도 docs-only commit `2073ec26a85f620f7269003caedea8248bad477f`에 남겼다: 사용자 원문은 15개 필드이고 `version` 추가는 구현 설계 선택이다.

## 검증과 실행 증거

- Backend command: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_creative_v2_api.py tests/test_creative_v2_extraction.py tests/test_creative_v2_run_extraction.py tests/test_route_inventory.py unit_tests/test_creative_v2_duration.py unit_tests/test_creative_v2_matching.py unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_registry.py unit_tests/test_creative_v2_run_scope.py unit_tests/test_creative_v2_style_dna.py` → `118 passed in 3.77s`. Raw output: `outbox/logs/backend-pytest-intent-review.log`.
- Backend Ruff command covered changed API, extraction, intent, run service, manager, route and tests → `All checks passed!`; `git diff --check` 통과.
- Frontend `pnpm test -- --run` → `137 passed | 2 skipped` test files; `1305 passed | 10 skipped` tests. Raw output: `outbox/logs/frontend-vitest-intent-review.log`.
- Frontend `pnpm exec tsc --noEmit`, `pnpm lint` (ESLint + UI lint; 기준선 138, 신규 위반 0), and `pnpm build` 통과. Vite는 기존 500 kB 이상 chunk 안내를 출력했다.
- Backend integration used the existing Run service with a temporary fake Qwen worker. It verifies queue → persisted Run → parsed review-only IntentSpec, disabled-engine failure, idempotent retry and personal worker task tagging. This is Run lifecycle integration evidence, **not** a real model response.
- Configured Ttakkari browser opened local Vite successfully, but the isolated browser profile redirected `/creative/plan` to `/login?next=%2Fchat`; refresh returned 401 and service worker loaded HTML with an unsupported MIME type. Authenticated feature page could not be rendered there. Browser was closed; no shell browser fallback was attempted.

## 미검증·미완료

- 실제 설치 모델 응답은 실행하지 않았다. live backend `/api/version`은 `75a12d690ad2458a56ea10675588539afb82791f`로 이 후속 라우트가 없는 revision이다. Qwen status lookup은 기존에 확인한 대로 한 경로는 404, worker 상태는 인증 없이 401이었으므로 운영 activation과 readiness는 미확인이다. worker binary 존재나 host llama-server 관측은 이 intent 경로의 실제 응답을 증명하지 않는다.
- 실제 MCP tool 실행 0회, 실제 model conversation resume 0회, 모델 usage metering은 미검증이다. 테스트 fake worker의 결과를 실제 모델 결과로 세지 않는다.
- first-slice exact SHA에 대한 독립 검수는 별도 worker에서 queued 상태이며 통과로 간주하지 않는다. 새 구현 커밋은 그 exact-SHA 검수 대상과 구분한다.
- WBS 8.3/8.6은 아직 done 처리하지 않았다. 실제 Qwen 응답·UI 브라우저 검증 및 잔여 범위가 남아 있다. 전문 레시피 실행 가능성을 새로 주장하지 않았고, 실제 영상 생성은 범위 밖이다.

## Review branch commits

- Backend implementation: `22486d2efb2bc53bad107f3cf31663b29c0e2840` (`feat(creative): run-backed intent review and editing gate`).
- Backend feature history: `d7b594e` (`docs(work-history): record Run-backed creative intent review`).
- Frontend implementation: `efbf1520e96d6b0da948f9f9bd0215fb71e08b90` (`feat(creative): add reviewed intent editing and run recovery UI`).
- These commits were pushed to `feat/creative-multidimensional-v2` and `feat/creative-intent-duration`. The separate Style DNA correction commit `2073ec26a85f620f7269003caedea8248bad477f` was pushed before them.
