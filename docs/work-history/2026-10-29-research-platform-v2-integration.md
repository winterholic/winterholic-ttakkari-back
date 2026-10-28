# Research Lab v2 및 플랫폼 V2 실제 미게시 차이 통합 및 검증

- **날짜**: 2026-10-29
- **작업 브랜치**: `integ/research-platform-v2-20261029`
- **기준 `origin/main`**: `2e75876d9db9329cb525a835e062c2e9520261cf` (`fix(runtime): raise the file descriptor soft limit at startup`)

---

## 1. 개요 및 정정 사실

1. **Research Lab v2 기반 커밋**:
   - `5a0b4ed151cfe588e0e6f03eca0d50ed975d40b1`은 운영 `12347b3f` 및 후속 FD `2e75876d`의 공통 ancestor로 이미 확인됨 (`git merge-base --is-ancestor 5a0b4ed1 origin/main` -> 참).
   - 따라서 "Research 전체 미통합" 가정은 취소되었으며, 실제 남은 기능 차이만 분리해 통합함.
2. **소유권 및 배포 원칙**:
   - `INFRA 441ae34d`가 유일한 ttakkari-back `main` 통합 및 배포 소유자임.
   - 본 작업자는 공유 `back`(`607f8bda`, dirty)을 수정하지 않고 격리 worktree(`back-research-v2-integration`)에서 검증된 통합 브랜치와 full SHA를 준비하여 기획 세션(`af14105d`)에 인계함.
   - 권한(`c8be9694`), 고아 취소(`439bc9af`), 좀비 Run(`2ea0d05a`)은 INFRA 소유로 분류되어 중복 변경하지 않음.

---

## 2. 백엔드 통합 내역 및 커밋 체인

기준 `origin/main`(`2e75876d`) 위에 실제 남은 5개 커밋을 충돌 없이 직렬 체리픽/이식 완료:

1. `48d80f80`: `feat(research): include registry methods and conditioned failures in review` (원본 `dac9737b`)
   - `src/ttakkari/api/research.py`: 카탈로그 리뷰 API(`get_catalog_review`)
   - `src/ttakkari/research/service.py`: `review_catalog_selection` 로직 및 조건부 실패 보존
   - `src/ttakkari/orchestrator/mcp_server.py`: MCP 도구 정렬
   - `tests/test_research_catalog_selection.py`, `tests/research_smoke/catalog_selection_live.py`, 증거 문서
2. `4a50d814`: `docs(research): retain controller mount failure receipt` (원본 `3c3b1640`)
3. `daa288b7`: `docs(research): record publish boundary and isolated build observations` (원본 `9548e0ce`)
4. `5bf88bdd`: `test(schedules): use runtime profile contract in persisted quota wait` (원본 `beea9a89`)
5. `80bb4169`: `fix(deploy): pin API executor mode from the release contract` (원본 `ba14ee63`)
   - `src/ttakkari/generation_deployment.py`: `Slots.api_executor_mode`에서 릴리스 계약을 읽어 plist env(`TTAKKARI_RUN_EXECUTOR_MODE`)에 주입
   - `unit_tests/test_executor_safety.py`: 파라미터화 테스트 4건

---

## 3. 검증 실행 결과

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest unit_tests/test_research_rules.py unit_tests/test_executor_safety.py
```
- **결과**: `40 passed, 1 warning in 0.78s`
  - `unit_tests/test_research_rules.py`: **19 passed**
  - `unit_tests/test_executor_safety.py`: **21 passed**
- **환경 한정 미실행**:
  - `tests/test_research_catalog_selection.py` (4건): antigravity 샌드박스 내 loopback socket(127.0.0.1:61445) `connect` 거부(`PermissionError: [Errno 1] Operation not permitted`). 코드 결함 아님.

---

## 4. 프론트엔드 상태

- **저장소**: `/Users/winterholic/development/projects/winterholic-ttakkari/front-research-v2`
- **기준 `origin/main`**: `b2b36009b6e42124efd552dcd5e33db78d51cf8f`
- **브랜치/HEAD**: `research-v2` (`57730bdf9e8b986f75f33c597f20e523497b3e79`)
- **차이**: `origin/main` 대비 정확히 1커밋 앞섬 (`feat(research): review catalog methods against study context`)
  - 변경 파일 4개: `src/pages/research/api.ts`, `src/pages/research/components/CatalogReview.tsx`, `src/pages/research/views/MethodLibraryView.tsx`, `src/pages/research/catalog-selection.test.tsx` (+156 lines)
- **검증**: 직전 세션 `vitest` 4 passed (756ms) 유효 증거 보존. (현재 antigravity 샌드박스 내 `/tmp` 및 `/var/folders` mkdir EPERM 제한 확인)

---

## 5. GitHub 자격 충돌 조사 및 인계 환경

- **현재 활성 계정**: `daeseon-dev` (회사 계정)
- **제약 및 원칙 준수**:
  - 전역 gh CLI 계정 변경 금지 및 회사 자격으로 개인 repo push 금지 규칙 준수.
  - 토큰 직접 노출/조회 없이 안전한 브랜치 생성 및 격리 유지.
  - 백엔드 main 병합 및 배포 단일 소유자인 INFRA(`441ae34d`)에게 본 통합 브랜치 및 full SHA를 인계하여 일괄 반영하도록 함.
