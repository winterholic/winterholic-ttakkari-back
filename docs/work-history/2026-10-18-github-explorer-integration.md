# GitHub 탐색 백엔드: main 통합 (2026-10-18)

`feat/github-explorer-20261018`(b61057f, 기준 6be9f56)의 6개 커밋을 최신 `origin/main`(8cf86ca) 위에 clean worktree(`/private/tmp/ttakkari-gh-integrate-back`)에서 cherry-pick했다. 충돌 없음. `git diff 6be9f56 b61057f`와 `git diff origin/main HEAD`가 같았다(80개 파일). main 쪽 변경 9개 파일(리버스·host_gpu)과 겹치는 파일은 없다. alembic·deploy 계약 변경 없음(head `0044_reverse_reconstruction` 그대로). 독립 검증자 3faa는 b61057f를 통합 가능(P0/P1/P2 없음)으로 판정했다.

## 이번 통합에서 바꾼 것(문서·주석만)
- ADR 0042 §13-1에 실측 정정: matching-refs "쪽당 100개"는 잘못된 전제였다. 검증자 9c0c 보고로는 `per_page=5`에도 205개 전체가 오고 `Link`가 없었다. 현재 코드는 보수적으로 정확하지만 불필요한 정확 조회가 종류별 최대 6번 나갈 수 있다.
- ADR 0042 §13 미검증 문단에 짧은 SHA·ref 실측 반영: `git/ref/heads/fix`(접두사) 404, 슬래시 브랜치 `fix/14692-issue-view-comments-help` 200 단일 객체, `commits/<7자>` + `application/vnd.github.sha` 200/40자, 없는 SHA와 blob 7자는 상류 422 → 앱 404 `ref_not_found`. 모호한 짧은 SHA는 미관측.
- ADR 0042 §14에 잔여 P3 D1~D4 기록. 코드 기능은 수정하지 않았다. `refs.py` 모듈 docstring의 "쪽당 100개" 문구만 정정했다.
- 실측 값은 검증자 9c0c의 보고/테스트에서 온 것이고 이 Run에서 GitHub를 다시 호출하지 않았다.

## 검사 (커밋 3747567, 전용 DB `ttakkari_verify_gh_int_f3a1`, `PYTHONPATH=<worktree>/src`로 worktree 소스 임포트 확인)
- `pytest -q unit_tests/test_github_*.py`: 296 passed
- `pytest -q tests/test_github_*.py`: 3 passed
- `pytest -q`(전체): 2005 passed, 6 skipped, 2 xfailed, 6 subtests passed (370s)
- `ruff check src/ttakkari/github src/ttakkari/api/github.py tests/test_github_integration.py unit_tests/test_github*.py`: All checks passed. `ruff check src tests unit_tests` 전체는 main 기존 파일 3건(`api/artcraft.py:226`, `artcraft_tasks.py:7`, `deploy_gate.py:30`) 때문에 실패한다. 이 통합이 건드린 파일이 아니다.
- 금지 패턴 grep: `gh auth switch|shell=True|follow_redirects=True` 코드 사용 0건(주석 1건뿐), httpx 클라이언트 두 곳은 모두 timeout 명시, `follow_redirects=False`.
- 운영 DB·인증 설정·secret은 읽지 않았다.

## 미검증·후속
- 운영 LaunchAgent의 gh 키체인·PATH·SSO·실기기 검증은 하지 않았다(ADR §12·§14).
- 잔여 P3 D1~D4는 ADR §14. 후속 WBS로 분리한다.
