# Antigravity 다중 계정 조사·후속 설계

상태: **조사 및 단일 인증 중복 등록 방지 보강. 다중 계정 기능 미구현·운영 승인 전.**
조사일: 2026-10-17 KST. 코드 기준: origin/main 41e007d. 조사 당시 배포 포인터: c1d706274cb4d3fc2d25f138280184ba1653dfb0.

## 판정

현재 mgy·ParaGravity·Multigravity를 설치하거나 런타임 의존성으로 추가하지 않는다. 기존 공식 `agy` 어댑터와 공통 Run/계정/라우팅 구조를 유지한다. mgy는 전체 대화 DB·MCP·메모리를 공유하므로 요청의 세션·개인정보 독립 조건에 맞지 않고, ParaGravity·Multigravity는 Electron/IDE 프로필 런처다. 공식 CLI 공개 저장소에는 인증·실행 엔진 소스가 없다. README·CHANGELOG·예제와 실제 설치 바이너리를 구분한다.

최종 인증 격리 방식은 **선정 보류**다. 다음 검증 1순위는 별도 HOME + 프로필 전용 암호화된 native Keychain이며, 해당 Keychain의 검색 목록·기본값·잠금/재부팅 수명을 검증해야 한다. HOME 변경만으로 완전한 인증 경계가 증명되지는 않는다. 별도 macOS 사용자/VM은 더 강한 대안이지만 사용자 생성·권한·공유 작업 디렉터리·MCP 전송 경계가 추가되고 현재 적용하지 않았다. SSH 환경변수 가장, 빈 비밀번호 Keychain, 토큰 복사, 바이너리 패치는 채택하지 않는다.

## 조사 소스 고정

| 대상 | 커밋 | 구분 | 라이선스 확인 | 판정 |
|---|---|---|---|---|
| wafardev/multigravity-cli | 0966e8e52f4ee649ce6898fe0ce1e75f7276b91a | agy CLI용 Bash/PowerShell | MIT LICENSE와 package.json | 도입하지 않음 |
| edison-land/paragravity | d553e1815587c636ac9dae1bbf3ae274ea49b0ea | Electron 앱/IDE 런처, Python | MIT LICENSE | 도입하지 않음 |
| sujitagarwal/multigravity-cli | 0e82b29145a13d4eac8fe89d00eecf968403593d | IDE 프로필 런처, Bash/PowerShell | 파일·GitHub API 모두 라이선스 미확인 | 코드 복사·의존성 도입하지 않음 |
| google-antigravity/antigravity-cli | e8a400403c82b7eb27a333784dc3c37985aa622e | 공식 제품 문서/예제/이슈 | 저장소 LICENSE 없음; 제품은 서비스 약관 적용 | 공식 설치 CLI 경계 유지 |

설치 바이너리: `/opt/homebrew/bin/agy`, `--version` = 1.4.0, SHA256 = a4502aaf310b2fd6eb657fcfec2fcc31a690fbbd0c2d14c8eefbb556409024ce.
이는 조사 대상 식별자이며 운영 바이너리를 고정하거나 배포한 것이 아니다. 신규 외부 패키지/설치 스크립트/업데이터는 실행하지 않았다.

### mgy: 직접 활용/래핑 모두 부적합

`bin/mgy:setup_profile_environment`는 프로필의 `.gemini/antigravity-cli`를 호스트의 같은 디렉터리로 symlink한다. 기존 프로필 디렉터리를 제거하고 연결하는 경로가 있어 운영 인증/상태에 직접 실행하지 않았다. 전체 conversations·summaries·설정·MCP가 공유되고 `.gitconfig`·`.ssh`도 연결한다. 계정별 실행은 HOME/USERPROFILE을 바꾼 `exec agy`다. macOS에서는 프로필 내 빈 비밀번호 Keychain을 만들고 그 HOME의 default/search list를 설정한다. 이때 오류를 `|| true`로 무시하므로 설정 성공만으로 독립 인증을 보장하지 않는다. 호스트 Keychain을 실제로 바꾼다는 주장이나 동시 안전성 주장은 이번 조사로 확정하지 않는다.

`bin/mgy-quota:get_agy_oauth_client`는 설치 바이너리에서 OAuth client 자료를 추출하고 후보를 조합해 refresh를 시도한다. 같은 파일의 credential 로딩은 Keychain 또는 토큰 파일을 직접 읽고 별도 API를 호출한다. 이 경로는 따까리의 공식 CLI `/usage`·`/credits` 수집으로 대체 가능하므로 채택하지 않는다. 계정간 `fork`는 SQLite 대화 식별자/메타데이터를 재작성한다. 토큰 갱신·로그아웃 격리·다중 프로세스 세션 잠금의 실제 성공은 미검증이다. Bash 문법 검사만 통과했으며 실제 로그인·실행은 하지 않았다. 조사 시 GitHub API의 열린 이슈 수는 0이었다. 보안 검증의 증거로 쓰지 않는다.

### ParaGravity: 복구 아이디어 참고, 인증 방식 제외

`bin/paragravity:cmd_launch`는 앱 실행 파일에 `--user-data-dir=<profile>/data`를 전달하고 HOME을 `<profile>/home`으로 바꾼다. 동시에 SSH_CONNECTION/SSH_CLIENT를 강제로 넣어 `jetski-standalone-oauth-token` 파일 저장 경로를 유도한다. `.gemini/antigravity-cli`의 공식 CLI와 동일한 인증 계약으로 볼 근거가 없다. `extract_account_info`는 토큰 내용을 읽어 JWT payload의 이메일/만료를 해석한다. 토큰 파일 존재·exp는 refresh 성공이나 서버측 자격을 증명하지 않는다. 파일 모드 0600/디렉터리 0700 보강이 있지만 파일 암호화와 동등하지 않다.

기본 full symlink 정책은 `.ssh`, `.config`, 셸 설정, 개발 디렉터리를 노출하며 macOS Keychain 디렉터리도 연결한다. `none`으로 줄이는 옵션은 있으나 Keychain bridge는 별도 처리다. 상속/복제는 허용 목록과 인증 필드 필터링으로 conversations/OAuth 복사를 피하려고 한다. `info --json`, profile 상태, PID·프로필 확인, 그룹 종료, stale lock/cache 정리, 업데이트 격리·doctor는 참고할 가치가 있다. 이슈 #12는 Electron 업데이트 뒤 SingletonLock/V8 캐시/프로필 인자 손실 문제를 다룬다. 닫힌 이슈라는 사실을 현재 CLI 1.4.0의 검증으로 대체하지 않는다.

로컬 표준 unittest 43개 중 38개 성공, 프로세스 탐지/종료/병렬 상태 관련 5개 실패. 실행 환경은 nested Codex 명령 sandbox이며 실제 Antigravity 앱 대신 가짜 셸 프로세스를 쓴다. 테스트가 ps/pgrep의 stderr를 버려 실패 원인을 확정할 수 없다. 코드상 프로세스 조회 제한과 연관될 가능성은 추론이다. 운영 Mac의 앱 실패나 제품 결함으로 일반화하지 않으며 같은 실패를 우회 재실행하지 않았다. 가짜 앱에는 sleep 300 자식이 있어 실패 뒤 일부가 최대 수 분 남을 수 있으나 운영 Antigravity 프로세스를 생성한 검사는 아니다.

### Multigravity: 이름의 CLI는 관리 도구 UI를 뜻함

`multigravity:launch_profile`는 macOS에서 `HOME=<profile> open -n <Antigravity.app> --args --user-data-dir ... --extensions-dir ...`를 호출한다. agy의 headless stream-json 어댑터가 아니다. 공유 설정·확장 템플릿이 있고 `clone_profile`는 프로필 전체를 `cp -R`한다. 별도 계정에 인증 상태가 복사되지 않는다고 보증할 수 없어 인증용 복제는 채택하지 않는다. export/import/rename/doctor/status 기능이 있지만 백엔드 Run heartbeat·소유권·재개 프로토콜이 없다. updater는 원격 스크립트를 받아 교체하므로 unpinned 운영 의존성으로 부적합하다. 라이선스가 확인되지 않아 코드 재사용을 허용된 것으로 취급하지 않는다.

이슈 #9는 로그인 재요청 사례이며 GitHub API의 최신 상태는 closed다(검색 캐시의 open 표기와 다름). #10은 인증 없는 대화 export/import 기능 요청이며 open, #12는 실행명 변경 이슈이며 open이었다. Bash 문법 검사만 실행했다. 실제 GUI/계정 로그인은 미실행이다.

### 공식 CLI: 외부 하네스 유지 가능, 인증 소스는 비공개

공식 headless 문서는 `-p`, json/stream-json, `--conversation` 및 NDJSON stdin 드라이버를 설명한다. 따까리는 현재 Run마다 단일 print 프로세스를 실행하고 계정별 엔진 대화 키로 재개한다. 단순 API 역프록시나 토큰 추출로 자체 에이전트 하네스를 대체할 필요가 없다.

인증 문서는 로컬 플랫폼 간 공유 sign-in/native keyring과 실제 SSH의 수동 OAuth를 설명한다. 저장 위치 변경 요청 #155는 open이다. 반면 macOS #1080의 collaborator는 별도 HOME·깨끗한 cwd를 제안했다. 이는 유용한 공식 저장소 안내이나 현재 1.4.0에서 두 계정 인증까지 검증한 증거는 아니다. 그 답변의 `agy login` 예시는 현재 `agy help login`에서 unknown subcommand였다. Windows #1261은 USERPROFILE을 바꿔도 Credential Manager 항목은 공유된다고 명시하므로 macOS에 그대로 일반화하지 않는다.

1.4.0 실제 `--help`에는 `--user-data-dir`가 없고, `/usage`와 함께 전달하면 exit 2 `flags provided but not defined: -user-data-dir`다. `--version`과만 조합하면 exit 0이므로 버전 출력 성공은 플래그 적용 검증이 아니다. CHANGELOG와 /usage JSON 구조는 업데이트 시 회귀 대상이다.

## 기존 따까리 영향과 확장 지점

| 경계 | 현재 코드 | 다중 계정에서 필요한 변경(미구현) |
|---|---|---|
| 인증 선택 | runs/profiles.Profile.env: Claude=CLAUDE_CONFIG_DIR, Codex=CODEX_HOME, Antigravity=기본 인증 | CLI와 usage에 같은 검증된 AccountContext 전달 |
| 실행 하네스 | agents/{claude,codex,antigravity}.py, LaunchSpec → argv | 공식 agy·NDJSON·sandbox 유지, 실행 전 격리 증빙 확인 |
| 계정/범위 | EngineAccount, 개인/회사 enabled·priority | account_id 명시 지정, 소유자 승인 범위, 격리 모드/검증상태 |
| 라우팅 | runs/worker_routing, profiles.choose, 모델 그룹 cooldown | 인증 실패 계정 격리, 상태 불명은 인증됨과 구분 |
| 대화 | ChatSession.engine_sessions의 계정 UUID/엔진·모델별 키 | 계정 변경 시 다른 계정 conversation ID 재사용 금지 |
| 프로세스 | RunManager._attempt, 새 process session, timeout/cancel, heartbeat, generation lease | 인증 계정과 PID/작업 결합, 종료 확인 불가를 성공으로 닫지 않기 |
| MCP | 세션별 private tmp 프로젝트 .agents/mcp_config.json, 0700/0600, 종료 cleanup | 다른 OS 사용자 방식이면 설정 파일·scope token 전송 경계 재설계 필요 |
| 상태/이벤트 | Run/RunEvent, EngineAccount, account_usage, usage_store | auth/cli_probe/last_success/last_error/reauth 관측시각과 출처 분리 |
| 작업 파일 | Workspace root를 cwd로 전달; 계정과 Git worktree는 별개 | 같은 저장소 동시 쓰기마다 별도 worktree 강제/소유권 및 정리 정책 |
| 장애 재개 | persisted cooldown, interrupted Run 재개/backoff, generation ownership | 부작용 이후 자동 계정 재시도 금지 또는 검증된 멱등 절차 |

공통 구조를 유지할 수 있으므로 별도 스케줄러/계정 DB/로그 서비스는 만들지 않는다. 이번 수정은 데이터베이스 스키마나 기존 계정 인증을 바꾸지 않는다. RunManager의 다른 엔진·계정 자동 한도 fallback은 기존 정책 그대로이며 요청의 중복 방지 요건을 모두 충족했다고 볼 수 없다.

## 이번 코드 보강의 정확한 범위

1. 두 번째 Antigravity 계정 POST는 409 `antigravity_isolation_unsupported`. DB transaction advisory lock은 동시 POST·복수 backend generation 사이의 검사 경쟁도 막는다. 계정 생성 시 자동 폴더를 만들기 전에 거부한다.
2. 기존 DB 또는 설정에서 들어온 Antigravity 비기본 config_dir는 Profile.env에서 ValueError로 닫는다. 무시한 채 기본 Google 계정을 실행하지 않는다.
3. usage_for는 config_dir가 지정되면 cache/subprocess보다 먼저 오류를 반환한다. 다른 identity에 기본 계정 quota를 표시하지 않는다.
4. 첫 기본 Antigravity 계정 및 Claude/Codex 등록·라우팅·인증 환경은 유지한다. 기존 계정을 삭제/재로그인/로그아웃시키지 않았다.

이것은 **잘못된 다중 계정 표시를 막는 선행 수정**이며 요구된 다중 계정 실행 기능 구현이 아니다. 기존 비기본 경로 계정은 앞으로 오류를 표시하게 된다. 잘못된 행/과거 usage sample을 자동 삭제하지 않았으므로 관리자가 실제 계정 증빙과 비교해 별도로 정리해야 한다.

## 인증 격리 후보와 최종 선정 게이트

- 독립 user-data-dir: IDE 전용 플래그. 현재 agy에서는 거부 → CLI 후보 제외.
- 별도 HOME만: 설정/대화 root 격리 후보. 빈 HOME의 native default keychain 부재 및 headless auth 실패 관측. 새 Google 계정 인증을 얻은 뒤 logout/refresh 교차검증 필요 → 단독 최종 채택하지 않음.
- 별도 HOME + 전용 암호화 Keychain: 검증 1순위. password·ACL·검색 목록·락/재부팅·정상 로그인 수명 관리가 추가된다. 빈 password·호스트 Keychain 공유 금지. 현재 생성/기본값 변경은 미실행.
- 별도 macOS 사용자 또는 VM: OS 자격 증명 경계 후보. 작업 파일·git 권한·stdio/MCP 경계 비용이 크고 시스템 변경 필요 → 미적용. 사용자 승인 없는 OS 사용자/권한/Remote Login 변경 금지.
- 실제 SSH OAuth 또는 Enterprise ADC: 공식 지원되는 서로 다른 환경/과금 경로. 동생의 개인 구독 OAuth와 동등한 대안이라고 가정하지 않음. SSH 환경 가장·API 키 과금 전환 금지.

## 후속 AccountContext 계약 (설계안, 구현 아님)

Account Manager는 account_id → {isolation_mode, state_root, expected_principal, cli_version, verified_at}를 검증하고 LaunchSpec와 usage probe에 동일한 env/cwd를 전달한다. expected_principal은 토큰을 파싱해 얻지 않고 소유자가 공식 로그인에서 확인한 식별자와 별도 검사 증빙을 결합한다. 현재 공식 비밀 없는 identity 조회 인터페이스의 충분성은 미확인이다. quota가 다르거나 새 conversation ID가 나왔다는 것만으로 다른 Google 계정임을 입증하지 않는다.

상태는 `unprovisioned → auth_required → ready → running`이며 `auth_expired`, `limited`, `cli_unavailable`, `isolation_unverified`를 독립 원인으로 기록한다. 인증 상태·CLI 가용·quota 관측에는 각각 시간·출처를 저장한다. 실행 중 작업·마지막 오류/정상은 기존 Run에서 도출하고 last_success는 실제 result SUCCESS에만 갱신한다. 모호한 timeout/네트워크 오류를 expired로 오판하지 않는다.

명시 account_id는 해당 허용 계정만 선택한다. 자동 분배는 ready·scope 허용·해당 모델 quota를 만족하는 계정 중 기존 load 점수를 사용한다. 인증 오류 후 자동 다른 계정 재실행은 기본 금지다. 부작용을 수행한 Run은 파일 diff/단계 기록·멱등성 판정 없이는 replay하지 않는다. 한도 전환은 계정 약관과 소유자 승인 범위 확인을 전제로 하며 한도 우회를 위한 계정 순환을 구현하지 않는다.

## 계정 추가/인증 절차 (현 단계에서 실행 가능한 범위)

현재 추가 계정은 등록되지 않는다. 기존 기본 계정은 현재 상태 그대로 사용한다. 두 번째 계정의 소유자가 허용 범위와 본인 로그인을 준비한 뒤 다음 절차를 진행해야 한다. **아래는 승인된 운영 절차가 아닌 후속 구현의 수용 기준**이다.

1. 새 프로필 위치·권한 0700·자격증명 저장소를 만든다. 기존 HOME/.gemini 또는 Keychain을 복사하지 않는다. 저장소 분리와 암호화·잠금 복구를 비밀 없는 테스트 항목으로 먼저 확인한다.
2. 검증된 프로필 실행 환경에서 공식 agy의 상호작용 로그인 절차를 소유자가 수행한다. 현재 버전의 login 하위명령을 추정하지 않는다. OAuth URL/code를 로그·Git·보고서에 저장하지 않는다.
3. 공식 표면에서 계정 A/B 식별을 각각 확인하고 증빙의 최소 비밀 없는 metadata만 기록한다. /usage·모델 응답·conversation resume·MCP 도구 호출을 각각 별도로 검증한다.
4. 각각 다른 worktree에서 nonce 파일 생성과 읽기를 수행하고 결과·PID·Run event·conversation ID를 회수한다. A가 B의 nonce/대화를 볼 수 없는지 검사한다.
5. 테스트 전용 새 계정에서 logout/refresh 실패를 만들고 기존 A에 영향이 없는지 확인한다. 실제 기존 A를 logout시키는 파괴적 검사는 별도 승인 전에는 하지 않는다.
6. 강제 종료·네트워크 격리·백엔드 재시작은 격리 시험환경에서 재현한다. macOS 재부팅·CLI 업데이트 시험은 운영 영향을 합의한 별도 창에서 수행한다. 계정별 ready 판정 이후에만 배정 대상에 넣는다.

## 장애 복구/유지보수 (후속 구현 기준)

| 상황 | 필요한 처리 | 현재 실측 |
|---|---|---|
| CLI 강제 종료 | Run interrupted/failed, 그룹·자식 종료 확인, workspace lease 회수 | 이번 두 계정 검사 미실행 |
| 인증 만료/취소 | 해당 계정 auth_expired·배정 제외, 공식 재인증; 다른 계정 자동 replay 금지 | 빈 HOME auth 실패만 관측, 실제 만료 미실행 |
| 네트워크 단절 | transport error와 auth error 분리, 부작용 checkpoint 재검토 | 미실행 |
| backend 재시작 | 기존 lease·cooldown·engine session 유지, 중복 부작용 없이 resume | 두 계정 미실행 |
| macOS 재부팅 | 암호화 Keychain unlock·프로필 권한·소유권 재확인 | 미실행, 재부팅하지 않음 |
| agy 업데이트 | 별도 새 바이너리 checksum/버전·JSON/인증/재개/MCP 회귀 후 교체 | 1.4.0 조회만, 업데이트하지 않음 |

롤백은 이번 코드 commit만 되돌린 후 clean SHA로 generation 배포한다. 계정 프로필/인증 데이터 삭제는 롤백에 포함하지 않는다. 프로필별 settings/MCP는 비밀 없는 허용 목록만 공유 후보이며 대화/brain/cache/history는 공유하지 않는다. 토큰을 로그·Git·일반 백업에 넣지 않는다. 프로필 저장소 0700/개별 설정 0600은 같은 OS UID의 악성 프로세스를 완전히 격리하는 보안 경계가 아님을 전제로 한다.

## 서비스 약관과 승인 범위

[Google Antigravity 약관](https://antigravity.google/terms)의 6항은 타사 도구의 서비스 접근과 OAuth 재사용을 제한한다. [공식 headless 문서](https://antigravity.google/docs/cli/headless)는 공식 agy를 프로그램/CI에서 실행하는 용도를 설명한다. 둘을 함께 읽어 공식 CLI wrapper와 토큰 추출 기반 직접 서비스 호출을 구분해야 하지만, 이 조사만으로 가족 계정 공유/자동 한도 대체가 허용된다는 법적 결론을 내리지 않는다. 동생 계정 소유자의 허용 범위는 아직 확인하지 않았으며 사용 권한이 있다고 가정해 실제 호출하지 않았다. 필요한 경우 공급자 확인 후 적용한다.

## 실행 증거와 미검증

- 코드 회귀: 전용 PostgreSQL DB `ttakkari_agy_accounts_20261017`, 최초 143 passed in 28.21s. f98075a 위 rebase 후 Qwen adapter/run도 포함한 최종 160 passed in 31.03s. 기존 accounts/Antigravity adapter·usage·run·model routing·worker routing과 새 격리 검사 포함. 인증/모델 실계정 테스트가 아니다.
- TDD: 새 단위 검사 3건 실패(경로 무시·기본 quota 호출·기본 캐시 재사용) 후 수정. 새 API 검사 2건 실패(직렬/동시 생성 둘 다 201) 후 수정. 첫 green 검사에서는 제품의 {code,message} 응답 계약 대신 detail을 검사한 테스트 오류 1건을 고쳤다.
- 실제 CLI: 1.4.0 /usage 기본 HOME SUCCESS/exit 0, 빈 HOME ERROR/exit 1. 후자는 authentication failed or timed out. 이 결과를 운영 backend의 장애로 일반화하지 않는다.
- macOS 메타데이터: default-keychain 기본 HOME에서 기존 login keychain, 빈 HOME에서는 default keychain 부재. 읽기만 했으며 Keychain 생성/기본값 변경/비밀 조회를 하지 않았다.
- 배포 경로 확인: `.ttakkari/deploy/current`의 c1d7062 release에 Antigravity adapter/API/profile 모듈이 실제 존재함을 확인. 실행 중 backend 모델 응답·MCP·재개는 별도 미검증.
- ParaGravity: pinned source에서 `python3 -m unittest discover -s tests -v`: 43 tests, failures=5. source의 fake process 테스트이며 운영 앱·인증 검사 아님.
- 브라우저 직접 실행 preflight는 nested sandbox의 mach-register/mach-lookup denied로 blocked. 이를 macOS 운영 브라우저 장애라고 하지 않는다. configured browser MCP 검사는 보고서 파일에 대해 별도로 수행한다.
- 서로 다른 두 Google 계정의 실제 코드 실행·동시 실행·logout/refresh 격리·재부팅·업데이트: **미실행**. 이번 요청 전체를 완료 처리하지 않는다.

## 1차 출처

- [mgy 고정 소스](https://github.com/wafardev/multigravity-cli/tree/0966e8e52f4ee649ce6898fe0ce1e75f7276b91a)
- [ParaGravity 고정 소스](https://github.com/edison-land/paragravity/tree/d553e1815587c636ac9dae1bbf3ae274ea49b0ea), [업데이트 복구 이슈 #12](https://github.com/edison-land/paragravity/issues/12)
- [Multigravity 고정 소스](https://github.com/sujitagarwal/multigravity-cli/tree/0e82b29145a13d4eac8fe89d00eecf968403593d), [로그인 이슈 #9](https://github.com/sujitagarwal/multigravity-cli/issues/9)
- [공식 저장소 고정 시점](https://github.com/google-antigravity/antigravity-cli/tree/e8a400403c82b7eb27a333784dc3c37985aa622e), [인증 문서](https://antigravity.google/docs/install?tab=cli), [headless 문서](https://antigravity.google/docs/cli/headless)
- [macOS HOME 안내 #1080](https://github.com/google-antigravity/antigravity-cli/issues/1080), [Windows keyring 범위 #1261](https://github.com/google-antigravity/antigravity-cli/issues/1261), [root override #155](https://github.com/google-antigravity/antigravity-cli/issues/155)
