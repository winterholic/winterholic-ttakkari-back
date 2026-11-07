# ArtCraft 영문 조회 누락 파일 오류 release (2026-11-07)

## 결과

독립 검증된 source 55c43479f7dfd67aaab52f76c46c3f760b31c463를 당시 최신 origin/main 4b40e470344e6f3ab0c885a2e9a41d48ba17c9a0 위의 새 격리 worktree에서 통합했다. 충돌은 없었고 source commit의 두 코드 파일과 통합본은 동일하다. 통합 코드 commit 00fff558c6096ca0a05d45d6bc2e26e723f3079b를 일반 push로 main에 반영했고, 이 SHA로 backend 배포 예약 52628c7c-e226-432e-a381-0c3866eb3cc6이 requested로 접수됐다. 접수는 배포 완료를 뜻하지 않는다.

## 실행 증거

- Run 첫 셸 명령 /Users/winterholic/bin/ttk get "/api/runs/$TTAKKARI_RUN_ID" → exit 0; 프로필 회사 코덱스 ai2 (ai2@hyperez.io), Run ID 38bb9ca9-7f2d-48cd-8efc-fe824822ee7f, engine/model/effort codex/gpt-6-luna/high.
- 공유 dirty checkout은 읽기만 했다. git status --short --branch는 main...origin/main [ahead 5, behind 796]와 다수 변경을 표시했다. 작업은 새 worktree /private/tmp/back-artcraft-lookup-release-20261107에서 수행했다.
- git fetch origin main → exit 0, origin/main=4b40e470344e6f3ab0c885a2e9a41d48ba17c9a0.
- 새 worktree 추가와 source cherry-pick 모두 exit 0. 충돌 없음, 2 files changed (35 insertions, 7 deletions). 통합 commit 00fff558c6096ca0a05d45d6bc2e26e723f3079b.
- source와 통합본의 두 파일 diff → exit 0, 출력 없음(동일).
- 지정 db_owner_proof.py를 저장소 venv로 실행 → exit 0, DB_OWNER_PROOF=PASS. SQL identity: ttakkari_test/ttk_test, 127.0.0.1/32, port 64345, readonly=on. marker Run ID·폴더·포트·UID 및 data child 검증 모두 true.
- 초회 일반 push는 exit 1. 설치된 pre-push hook의 uv sync가 기본 ~/.cache/uv에서 Operation not permitted로 실패했다. hook 우회 없이 허용된 /private/tmp 캐시로 경로만 바꾸어 정상 push를 재실행했다.
- 통과한 일반 push: UV_CACHE_DIR=/private/tmp/ttk-uv-cache-artcraft-lookup-20261107 git push origin HEAD:refs/heads/main → exit 0. Hook: Ruff 변경 Python 2개 통과; 계약 pytest 87 passed, 6976 deselected in 16.86s; 변경 테스트 83 passed in 7.92s; 총 41초.
- git ls-remote origin refs/heads/main과 git rev-parse HEAD 모두 00fff558c6096ca0a05d45d6bc2e26e723f3079b. git status clean, git diff --check exit 0.
- request_backend_deploy revision=00fff558c6096ca0a05d45d6bc2e26e723f3079b 응답: reservation ID 52628c7c-e226-432e-a381-0c3866eb3cc6, skip_tests=false, status=requested, strategy=generations, error=null. 배포 polling·장기 대기와 실제 운영 전환 확인은 하지 않았다.

## 변경 및 검증 범위

코드 변경은 src/ttakkari/artcraft_tasks.py, tests/test_artcraft_tasks.py 두 파일뿐이다. 독립 검증의 45개 API/분류 결과와 source classifier 결과는 동일 코드 범위 근거로 재사용했다. 현재 Run DB proof 이후 hook을 실행했고 계약 및 변경 테스트를 통과했다. 전체 pytest와 배포 이후 운영 smoke는 실행하지 않았다. 최초 잘못된 상대 venv 경로 실행은 exit 127이었고 절대 저장소 venv 경로로 proof를 통과했다.

세부 명령 출력과 원문 독립 근거는 back-artcraft-audit-20261107/docs/artcraft/evidence/2026-11-07-lookup-error-release/RESULT.md에 보존했다.
