# Research 채택 결과 재진입·후속 lineage 검증 (2026-10-30)

## 범위

- 정본 요구 §5·§6·§12 C·§13의 실행 결과 채택/보존 → 저장 상태 재진입 → 이전 설정을 후속 연구에서 재사용하는 한 흐름을 선택했다.
- frontend 기록 화면에 저장된 채택 결과의 후속 실험 진입 버튼을 추가했다. backend API/schema/DB 기능은 이미 존재하므로 중복 구현하지 않았다.
- frontend topic의 구현 및 mock UI 회귀는 frontend 저장소의 `docs/work-history/2026-10-30-research-retry-reentry-integration.md`에 기록했다.

## 저장 계보 회귀

- `tests/test_research_v2_e2e.py::test_vertical_e2e_rag_research_flow`에 채택 결과로 후속 실험을 만드는 단계를 추가했다.
- 응답의 평면·`experiment` 래퍼 source 4개 ID, 저장된 config의 clone+override, timeline `experiment_planned` payload를 확인했다. 원본 Configuration 파라미터와 succeeded Run 상태가 바뀌지 않는 것도 확인했다.
- RED 환경 진입 첫 시도: 기본 `uv run`이 uv 캐시 `/Users/winterholic/.cache/uv`의 `.git` 파일 접근 `Operation not permitted`로 uv 초기화에서 실패했다. 테스트는 실행되지 않았다.
- 허용 임시 캐시 `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache`로 다시 실행:

  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache uv run pytest -q tests/test_research_v2_e2e.py::test_vertical_e2e_rag_research_flow
  .                                                                        [100%]
  1 passed in 1.01s
  ```

- 이 테스트는 backend API test fixture/SQLite에서 기존 preset 기반 `retrieval_eval`을 사용했다. 실제 PostgreSQL, 배포 API, 인증 UI, 호스트 ML 실행 증거가 아니다. 외부 운영 side effect/DB 변경은 수행하지 않았다.

## 남은 범위

- UX10·PMS 7.x 등 기존 미완 항목, native checkpoint, OS 합산 메모리, legacy 92는 이 작업으로 닫지 않았다.
- 통합·push·deploy는 fcb55b7d 소유자에게 이관한다. 이 topic은 구현·검증·커밋까지만이다.
