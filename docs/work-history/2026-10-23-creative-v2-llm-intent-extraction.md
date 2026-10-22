# Creative Studio v2 — 서버 의도 추출 연결 (2026-10-23)

범위: 첫 수직 슬라이스의 후속. 기존 카탈로그·Style DNA·길이 정책, 그리고 worker의 `director.py`·`pipeline.py`·`managed_comfy.py`·`records` 작업은 건드리지 않았다.

## 구현

- `/api/creative/v2/intent/extract` 인증 라우트를 추가하고 기존 managed desk Qwen 서비스의 readiness·endpoint·constrained JSON 호출 경로를 재사용했다. 독립 모델 프로세스나 유료 모델 호출을 추가하지 않았다.
- 모델 출력을 `IntentSpec`으로 파싱하고 선택 ID·근거·제약을 검증한다. 브리프에 없는 인용을 가진 출력은 거부한다.
- 모델이 `user_explicit` 또는 hard로 주장한 내용도 API에서는 `llm_inferred` soft로 강제한다. 항상 검토 질문을 넣어 사용자 확인 전의 추론으로 남긴다.
- 제작 계획 화면에서 AI 분석을 실행하고 선택 항목 이름·근거·검토 질문을 보여준다. 브리프가 바뀌면 초안과 확인 상태를 지우고, 사용자가 검토 checkbox를 켠 뒤에만 `POST /plans`에 intent를 포함한다.
- API 라우트 인벤토리, 한국어 표시 이름, 프런트 API 타입을 갱신했다.

## 검증

- RED: 새 추출 계약 테스트가 모듈 미구현으로 수집 실패하는 것을 확인했다.
- Backend: `PYTHONPATH=src .../pytest -q tests/test_creative_v2_extraction.py tests/test_creative_v2_api.py tests/test_route_inventory.py unit_tests/test_creative_v2_duration.py unit_tests/test_creative_v2_matching.py unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_registry.py unit_tests/test_creative_v2_style_dna.py` → 106 passed. Ruff → `All checks passed!`; `git diff --check` 통과.
- Frontend: `pnpm lint`(ESLint + ui-lint, 신규 위반 없음), `pnpm build`(TypeScript + Vite 빌드), `pnpm vitest run src/pages/creative/CreativePlanPage.test.tsx` → 11 passed. 빌드는 성공했고 대형 청크 용량 안내를 출력했다.
- API와 추출 로직은 모델을 mock해 확인했다. 실제 managed Qwen 호출, 브라우저 렌더, 실제 영상 제작은 실행하지 않았다.

## 남은 일

- 화면에서 분석된 IntentSpec을 직접 수정하는 UX가 없다.
- 실제 Qwen 출력 품질·레이턴시·usage metering을 운영 설정에서 확인해야 한다.
- 누락된 전문 능력과 R01~R20 실제 실행 연결, 장면 렌더·복구·QA, 실제 검증 영상과 독립 검수는 계속 미완료다.
