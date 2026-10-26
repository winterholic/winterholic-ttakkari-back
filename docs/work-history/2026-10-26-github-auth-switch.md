# 계정별 GitHub clone/pull 경로 (2026-10-26)

## 범위와 원인 증거

- 기준은 `origin/main` `1083a0cee98879da6869a6b224f5a11282ec290d`, 격리 worktree `/Users/winterholic/development/ttakkari-gh-auth-switch-20261026`, 브랜치 `fix/gh-auth-switch-20261026`이다. 작업 디렉터리 `projects/winterholic-ttakkari/back`의 기존 staged/unstaged/untracked 변경은 건드리지 않았다.
- 이전 회사 기획 대화 원문(`642edb99-b9f3-402d-8ece-c9b92141e975`)에서 관측된 사실: Codex nested workspace-write/approval-never 실행의 `gh auth switch --hostname github.com --user daeseon-dev`가 Keychain 쓰기 단계에서 exit status 161로 실패했고, 사용자가 일반 터미널에서 전환한 뒤 저장소 clone/pull은 성공했다. 두 계정 인증 메타데이터도 당시 확인됐다. 이 트랙에서는 이 명령을 재실행하지 않았다.
- 정확한 macOS Keychain 거부 원인과 gh 개별 명령 차단 정책은 미확정이다. `src/ttakkari/github/accounts.py`는 이미 검증 목록의 login별 `gh auth token --hostname github.com --user <login>`을 사용하고, 별도 토큰 캐시를 login 키로 보관한다. ADR 0042는 전역 활성 계정 변경 금지와 에이전트/MCP 노출 제외를 정했다. `runs/manager.py`의 Codex subprocess는 Run 작업 디렉터리·sandbox 설정에서 실행한다. 따라서 명령별 차단 규칙을 확인하지 못했고, 이전 오류는 sandbox 안에서 Keychain 쓰기가 실패한 경로로만 분류한다.

## 변경

- `github_git(operation, account, owner, repo)` MCP 도구와 `/api/agent/github/git`을 추가했다. 개인 세션에서만 동작하며 현재 세션 Workspace의 `.ttakkari-github/<owner>/<repo>`에 `clone` 또는 동일 origin·clean tree·현재 브랜치 upstream 검증 후 `pull --ff-only`만 실행한다.
- 인증은 기존 AccountRegistry의 login별 토큰을 사용한다. Git은 고정 askpass helper로 github.com 인증 prompt에만 token을 전달한다. 임의 명령, URL, 경로, push, submodule 수집, 대상 코드 실행은 제공하지 않는다. 선택 계정은 audit에 남기고 토큰은 argv·응답·로그에 넣지 않는다.
- 전역 `gh auth switch` 직접 지원은 하지 않는다. 동시에 실행되는 작업 사이의 global active account race를 피하기 어렵고 Keychain 쓰기가 필요하기 때문이다. 상세 결정은 `docs/adr/0073-account-scoped-github-git-operations.md`.

## 검증

```text
$ PYTHONPATH=src pytest -q tests/test_github_git_ops.py tests/test_github_integration.py unit_tests/test_github*.py
313 passed, 2 warnings in 6.69s
```

두 경고는 기존 `api/public_shares.py` OpenAPI duplicate operation ID다. 검증은 mock gh/Git runner이며 실제 GitHub clone/pull 결과가 아니다.

```text
$ ruff check <변경 Python 파일 목록>
All checks passed!
$ git diff --check
(exit 0, 출력 없음)
```

TDD 보정 중 공식 Git 문서 대조에서 `git clone --no-recurse-submodules`가 지원 옵션 목록에 없음을 확인해 해당 플래그를 넣은 테스트를 먼저 실패시킨 뒤 clone 인자에서 제거했다. clone은 기본 동작상 submodule을 초기화하지 않는다. pull에는 문서화된 `--no-recurse-submodules`를 명시한다.

## 미검증 및 재개 지점

- 실제 비공개 저장소 clone/pull 및 backend LaunchAgent runtime은 미검증이다. 아래 별도 push 시도는 feature의 Git clone/pull 검증과 별개다. mock 테스트를 실환경 성공으로 간주하지 않는다.
- 운영 revision/runtime은 확인하지 않았다. 이 회사 세션의 `ops_list_deploys` 조회가 403으로 거부되어 있으며, 보호된 로그를 다른 경로로 확인하지 않는다.
- push 기록: 기본 자격으로 `git push -u origin fix/gh-auth-switch-20261026`가 GitHub에서 `Permission to winterholic/winterholic-ttakkari-back.git denied to daeseon-dev` (HTTP 403)로 거부됐다. `winterholic` 계정별 토큰을 값 출력 없이 askpass에 전달한 한 번의 시도는 exit 128이었다. stderr를 숨겨 실제 prompt는 관측하지 못했다. 후속 synthetic test에서 Git password prompt의 `https://x-access-token@github.com` 형태를 helper가 거부함을 재현해 보완했다. 따라서 두 번째 시도의 직접 원인이 그 prompt였다는 설명은 추론이다. mock에서 해당 prompt 형식을 재현한 뒤 helper를 보완했고, `gh auth token --hostname github.com --user winterholic` 결과를 값 출력 없이 askpass 환경변수로 전달한 push가 성공했다.

```text
$ git push --porcelain -u origin fix/gh-auth-switch-20261026
push exit=0; origin accepted fix/gh-auth-switch-20261026
[new branch] refs/heads/fix/gh-auth-switch-20261026
```

첫 기본 인증 push의 403은 `daeseon-dev`가 Ttakkari 저장소에 권한이 없어 발생했다. 계정별 인증으로는 전역 활성 계정 전환 없이 push할 수 있음을 확인했다.
- 최신 main 통합·배포는 lead가 충돌·소유권과 회사 범위를 판단한 뒤 재개한다. 이 작업에서는 deploy 도구나 runtime restart를 하지 않는다.
