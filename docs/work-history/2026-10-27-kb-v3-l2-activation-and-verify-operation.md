# 2026-10-27 KB v3 L2 기본 활성화 및 실검증 호스트 오퍼레이션(`kb_v3_verify`)

- **작업 브랜치**: `feat/kb-v3-l2-activation-20261027` (Base: `c07ea7cd0440363abc6cacb909b3f0cd338ef43b`)
- **작업 영역**: Sibling worktree `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027`
- **커밋**: `90d9a6ce2408a6ad6cbba9c312d42a18134520a3`

## 1. 작업 목적 및 원칙
- KB v3 전면 리팩토링 검증(R1, R7, R8) 구현:
  - R1/R7: 제품 기본 L2 어댑터를 `subscription_cli` (`gpt-6-luna`)로 활성화하고 테스트 격리 및 disabled optout 보존.
  - R8: 고정 kind 호스트 오퍼레이션 `kb_v3_verify`를 등록하여 공개 합성 3문서(PDF 1건 + 30일/90일 정책 2건)의 제품 수집 파이프라인 통과, L2 추출 및 상충(`value_contradiction`) 감지 검증.
- 리드 소유 원칙 준수: `main`, `git push`, `request_backend_deploy` 미수행, 브랜치 격리 커밋 보존.

## 2. 주요 변경 사항
1. `src/ttakkari/config.py`:
   - `kb_l2_adapter = "subscription_cli"`
   - `kb_l2_subscription_model = "gpt-6-luna"`
2. `tests/conftest.py`:
   - 테스트 격리용 `TTAKKARI_KB_L2_ADAPTER: "disabled"` 명시.
3. `src/ttakkari/operations/plans.py`, `src/ttakkari/api/operations.py`, `src/ttakkari/orchestrator/mcp_server.py`, `src/ttakkari/operations/inline.py`:
   - 승인된 고정 kind `kb_v3_verify` 등록 (extra='forbid'로 generic 입력 차단).
4. `src/ttakkari/operations/kb_v3_verify.py`:
   - 공개 합성 PDF 1건 + 30일/90일 정책 문서 2건 등록.
   - worker pump 기반 finite 대기 및 터미널 상태 조기 탈출.
   - DB 조회 기반 집계값 반환 및 민감정보 비노출.
5. `src/ttakkari/kb_pipeline/daily_budget.py`:
   - SQLite/DB 방언 간 naive-aware datetime 비교 예외 방어 `_to_aware()` 추가.
6. `tests/test_kb_v3_default_activation.py`, `tests/test_kb_v3_verify_operation.py`:
   - 27개 관련 단위/통합 테스트 전체 통과 (0.95s).

## 3. 검증 근거
- 단위 테스트: `pytest tests/test_kb_v3_default_activation.py tests/test_kb_v3_verify_operation.py tests/test_kb_v3_visibility_and_l2_adapters.py` (27 passed in 1.06s)
- 정적 검증: `ruff check` All checks passed!
- 산출물 공유: outbox에 `kb_v3_l2_activation_report.md` 생성 및 MCP `share_file` 등록 완료.
- 미검증 항목: 실제 호스트 LLM 및 운영 PostgreSQL E2E는 배포 후 리드 검증 영역.
