# ADR 0073: 계정별 GitHub 저장소 작업

- 상태: 구현 결정 (2026-10-26)
- 목적: 에이전트 실행 sandbox의 Keychain 쓰기 실패 때문에 계정 전환이 필요한 저장소 작업이 멈추는 불편을 줄인다.

## 결정

`gh auth switch`는 에이전트 실행 경로에 노출하지 않는다. 이 명령은 github.com의 전역 활성 계정을 바꾸고 Keychain 구성을 다시 쓰므로, 동시에 다른 작업이 실행되면 선택 계정이 서로 덮어써질 수 있다. 앞선 실행에서 확인된 Keychain 오류의 정확한 macOS 거부 원인과 명령별 정책 차단은 미확정이다.

기존 `github.AccountRegistry`의 명시 계정 토큰 획득을 재사용해, 개인 세션 MCP 도구 `github_git`에 다음 작업만 추가한다.

- `clone`: `https://github.com/{owner}/{repo}.git`를 현재 세션 Workspace의 `.ttakkari-github/{owner}/{repo}`에 shallow clone
- `pull`: 동일 경로의 저장소에서 origin URL, clean working tree, 현재 브랜치와 `origin/<branch>` upstream을 확인한 뒤 `git pull --ff-only`

각 작업은 `account`를 필수로 받고, 토큰은 gh argv나 응답에 넣지 않는다. 고정 askpass helper와 프로세스 환경으로 Git credential 입력에만 전달하며, 시스템·전역 credential helper를 끈다. 원격 host는 `github.com`으로 고정하고 askpass도 해당 host에만 응답한다. 도구는 개인 세션에서만 허용하며 작업은 Workspace 안에서만 한다. 셸·임의 경로·임의 URL·push·submodule·대상 코드 실행은 허용하지 않는다. 성공한 작업은 저장소, 선택 계정, 세션과 상대 경로만 audit에 기록한다.

GitHub 탐색 API(ADR 0042)는 기존대로 읽기 전용이며, 이 결정은 그 API의 임의 MCP 노출을 열지 않는다. `github_git`은 별도의 제한된 저장소 작업 도구다.

## 검증 경계

테스트는 mock gh runner와 mock Git runner로 계정별 토큰 격리, 잘못된 owner/repo, 미등록 계정, 다른 host askpass 거부, existing destination 보존, 잘못된 origin과 dirty tree의 pull 거부, work-session 차단을 확인한다. 실제 Keychain/gh token refresh와 GitHub private repo 작업은 이 테스트로 검증되지 않는다. 운영 runtime 경로도 미확인이다.

## 1차 출처

- [GitHub CLI `gh auth token`](https://cli.github.com/manual/gh_auth_token): host/user별 토큰 출력 및 만료 토큰 자동 갱신 동작.
- [GitHub CLI environment](https://cli.github.com/manual/gh_help_environment): `GH_TOKEN`/`GITHUB_TOKEN` 우선순위. 이 구현은 이 변수를 Git CLI에 의존하지 않고 askpass를 쓴다.
- [Git credentials](https://git-scm.com/docs/gitcredentials): `GIT_ASKPASS`의 credential prompt 처리.
