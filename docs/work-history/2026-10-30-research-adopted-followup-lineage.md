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
- 최초 통합·push·deploy 책임은 fcb55b7d 소유자에게 전달했으며, 같은 main 소유자의 후속 실행에서 통합·게시·예약을 완료했다. 아래 게시 기록의 최종 SHA와 예약 상태를 기준으로 한다.

## main 통합·게시 기록 (2026-10-30)

- 시작 기준 `origin/main`: `282ca335e0d739ddc780a2f02f096537cb4d6a1f`. 원 기능 commit `7fc3e6fe75baafd5851629915f516d642eae85ae`는 이 기준 위 단일 commit이었다. 정상 pre-push gate에서 Ruff가 같은 테스트 파일의 unused import/assignment 3건을 보고해 중단했다. 해당 import와 변수만 제거한 `10874f8c37f2e9ec90876e5a98968c3e8b582ab3`을 추가했다. 기존 기능 commit은 보존했다.
- 첫 정상 push 시 기본 uv cache `/Users/winterholic/.cache/uv`의 `.git` 초기화가 `Operation not permitted`로 실패했고 검증에 진입하지 못했다. 문서에 이미 기록된 허용 임시 cache를 사용해 정상 hook을 재실행했다:

  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-fcb55b7d git push origin HEAD:main
  ```

- 정상 hook 출력: Ruff 변경 Python 1개 통과; `pytest -m contract`: `85 passed, 6092 deselected in 15.73s`; 변경 테스트: `1 passed in 1.10s`. 결과 요약은 "사전 검증 통과: 계약 묶음과 바뀐 테스트 파일 범위만 확인했습니다. 전체 통과를 뜻하지 않습니다."였다.
- Push 결과: `282ca335..10874f8c HEAD -> main`. `git ls-remote origin refs/heads/main`과 local `HEAD` 모두 `10874f8c37f2e9ec90876e5a98968c3e8b582ab3`; 작업 트리는 clean이다.
- `request_backend_deploy(revision="10874f8c37f2e9ec90876e5a98968c3e8b582ab3")` 응답: 예약 `5d09e942-b4e5-4ab7-a4f0-ce09fd7ad27e`, `status=requested`, `skip_tests=false`, `strategy=generations`. 후속 `backend_deploy_status`도 `requested`를 반환했다. 이는 배포 수락이며 활성화/운영 성공 증거가 아니다.
- 프론트 main은 `f610d6652175fcd97b4f69b40fa9d2b52c4c7118`로 게시했다. GitHub Vercel status 1회 조회 시 `pending`, description `Vercel is deploying your app`; 이 check 시점에 한해 배포가 진행 중이었다. 인증된 운영 화면 및 실제 backend 활성 revision은 미검증이다.
