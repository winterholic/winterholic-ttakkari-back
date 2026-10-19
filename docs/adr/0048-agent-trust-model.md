# ADR 0048: 에이전트 신뢰 모델(같은 OS 사용자 문제)

- **상태**: Proposed (2026-10-19). 결정 대기. 최종 선택은 사용자가 한다.
- **WBS**: SEC2-DEC-01 (`docs/security/wbs.md`), 관련 SEC2-EXEC-01·02·03·04, 위협 S3·S8 (`docs/security/threat-model.md`)
- **범위**: 문서만 작성했다. 운영 코드, 계정, 키체인, 파일 권한, pg_hba는 바꾸지 않았다. 비밀 값은 읽지 않았다.

## 1. 문제

백엔드(launchd LaunchAgent `net.winterholic.ttakkari.deployer`, UserName 없음)와 모든 엔진 프로세스가 같은 uid 501(winterholic)로 돈다. 엔진은 승인 없이 셸을 실행한다(ADR 0003). 오염된 콘텐츠(T3)나 오작동 에이전트(T5)가 소유자 파일을 읽으면 JWT 서명키(A2)로 소유자 토큰을 위조하거나, 엔진 계정(A7)과 금융·Google 세션(A8)을 가져갈 수 있다.

`security/guard.py:3-6`은 guard가 우회 가능한 휴리스틱이고 경계가 아니라고 스스로 밝힌다. 이 ADR은 "무엇을 OS 경계로 삼을지"를 정한다.

## 2. 근거

### 2.1 SEC2-EXEC-01 실측 요약 (두 모델 독립 검증)

| 항목 | 결과 |
|---|---|
| claude·muse PreToolUse guard | `cat`, `base64`, Read 도구 직접 읽기는 차단 |
| 같은 guard의 우회 | `python3 -c` 삽입, 변수 은닉(`cat $D/$N`, `cat $TARGET`, `open(os.environ['T'])`, `cat $(echo $TARGET)`)은 허용 |
| OS 수준 | 같은 uid는 0600 canary를 차단 없이 읽음 |
| 미실측 | codex seatbelt(엔진 경유), antigravity `--sandbox`, qwen-local |

원 보고서(`outbox/ebc74324-…/sec2-exec-01-independent-verification-2026-10-19.md`)는 이 Run의 ttakkari guard가 `~/.ttakkari` 경로 접근으로 차단해 열지 못했다. 위 표는 작업 지시서의 요약을 옮긴 것이다. guard 차단을 우회하지 않았다.

`tests/test_guard.py`의 strict xfail 2건(105행 변수 확장 읽기, 124행 Bash 외부 쓰기)이 같은 한계를 회귀 테스트로 고정한다. 커밋 8d09fa9는 과차단을 줄이고 변수 경유 DB 삭제 등 4건을 막았지만 변수 확장 읽기는 여전히 xfail이다.

### 2.2 이 ADR 작성 중 추가 실측 (2026-10-19, 호스트 셸, 임시 canary)

**Codex permission profile 읽기 거부는 OS 수준에서 동작한다.** `codex-cli 0.162.1`, 임시 `CODEX_HOME`과 `/private/tmp/sec2dec.*` canary로 `codex sandbox -P <profile>`을 실행했다. 실제 `~/.codex` 설정은 건드리지 않았다.

```toml
[permissions.ttk]
extends = ":workspace"
[permissions.ttk.filesystem]
"<canary>/secret" = "none"
[permissions.ttk.network]
enabled = true
```

| 시도 | deny 없는 profile | deny profile |
|---|---|---|
| `cat <canary>` | `CANARY-OK` | `Operation not permitted` |
| `python3 -c "open(...)"` | 측정 안 함 | `PermissionError: [Errno 1] Operation not permitted` |
| `sh -c 'D=…; cat $D/c.txt; ls $D'` | 측정 안 함 | 둘 다 `Operation not permitted` |
| 작업공간 안 symlink로 읽기 | 측정 안 함 | `Operation not permitted` |
| 작업공간으로 hardlink 생성 | 측정 안 함 | `Operation not permitted` |
| 작업공간 쓰기 | 성공 | 성공(`hi`) |
| 네트워크 `curl https://example.com` | 측정 안 함 | network 미설정 `000`, `enabled = true` 후 `200` |

guard가 놓친 변수 은닉·`python3 -c` 경로를 seatbelt는 막는다. 다만 이 측정은 `codex sandbox` 명령 기준이다. 운영 어댑터는 `codex exec`에 `-c sandbox_mode="workspace-write"`와 `-p <profile>`을 넘긴다(`agents/codex.py:131-157`). `codex exec --help`에는 `-P`가 없으므로, 프로필 파일의 `default_permissions` 등으로 같은 정책을 `exec`에 적용할 수 있는지와 legacy `sandbox_mode`와 함께 쓸 수 있는지는 **확인 필요**다. MCP 서버 자식 프로세스도 sandbox 안에서 도는지 **확인 필요**하다.

**Claude Code에는 OS sandbox 설정 키가 있다.** `claude 2.1.285` 바이너리 문자열에서 `denyRead`, `allowRead`, `denyWrite`, `allowWrite`, `excludedCommands`, `allowUnsandboxedCommands`, `failIfUnavailable`을 찾았다. 엔진 경유 동작은 미실측이고 Bash 외 Read 도구에 적용되는지도 **확인 필요**하다. Muse(`@muse-code/sdk`)에 같은 기능이 있는지는 **확인 필요**하다.

**Antigravity `--sandbox`는 deny-default seatbelt를 쓰는 것으로 보인다.** `agy 1.4.2` 바이너리 문자열에 `; macOS Seatbelt sandbox profile`, `(deny default)`, `exebox.(*fileTree).allowRead`, `enableTerminalSandbox`가 있다. 이름대로라면 터미널 명령에만 적용되고 읽기는 허용 목록 방식이다. 기본 허용 목록에 `~/.ttakkari`가 들어가는지, `view_file` 같은 내장 파일 도구가 sandbox 밖에서 도는지는 **확인 필요**다.

### 2.3 보호 대상 인벤토리 (존재·권한·소유자만, 값 미열람)

| 자산 | 경로 | 권한·소유자 | 확인 방법 |
|---|---|---|---|
| A2 JWT 서명키 | `~/.ttakkari/config.env` (`config.py` `Settings.model_config.env_file`, 기동 시 읽음) | **미확인**. 코드상 `ensure_dirs`가 `~/.ttakkari`를 0700으로 생성(`config.py` `Settings.ensure_dirs`, 생성 시에만 적용) | 이 Run의 guard가 `stat ~/.ttakkari`도 차단 |
| A7 엔진 계정 | `~/.ttakkari/accounts/*` | 미확인(같은 이유) | 코드 |
| A7 | `~/.claude/.credentials.json` | `-rw------- winterholic:staff` | stat |
| A7 | `~/.claude.json` | `-rw------- winterholic:staff` | stat |
| A7 | `~/.codex/auth.json` | 파일 없음. 계정별 `CODEX_HOME`(ADR 0006, `~/.ttakkari/accounts` 아래로 추정)에 있을 가능성 **확인 필요** | stat |
| A7 | `~/.gemini/antigravity-cli/antigravity-oauth-token` | `-rw------- winterholic:staff`. 상위 디렉터리는 0755 | ls |
| A7 | Antigravity 프로필 키체인 잠금 해제 비밀 | login keychain, 서비스 `net.winterholic.ttakkari.antigravity.unlock.v1` (`security/antigravity_keychain.py:18`) | 코드 |
| A8 금융·Google 세션 | `~/.ttakkari/tossinvest-chrome`, `~/.ttakkari/google-accounts`(ADR 0051), 이전 위치 `~/.ttakkari/notebooklm-chrome`·`~/.ttakkari/notebooklm/accounts` (`config.py` `tossinvest_profile_dir`·`google_accounts_dir`·`notebooklm_dir`) | 미확인 | 코드 |
| A8 | 사용자 Chrome 프로필 `~/Library/Application Support/Google/Chrome` | `drwx------ winterholic:staff` | ls |
| DB 접속 | `postgresql+asyncpg://localhost/ttakkari` 기본값(`config.py` `database_url`), 운영값은 config.env | `pg_hba.conf`(0600 winterholic): **local·127.0.0.1·::1 모두 `trust`** | pg_hba 인증 방식 행만 확인 |
| 하네스 | `~/.claude`, `~/.codex` | 둘 다 `drwxr-xr-x winterholic:staff`. 쓰기 허용 루트에 포함(`config.py` `allowed_roots`·`default_writable_roots`, 사용자 결정 10-11) | stat |
| 홈 | `~` | `drwxr-x---+ winterholic:staff` (staff 그룹 읽기·탐색 가능, ACL 있음) | ls |
| 작업공간 | `~/development` | `drwx------ winterholic:staff` | ls |
| 키체인 | `~/Library/Keychains/login.keychain-db` | `-rw-r--r--` (파일 자체는 암호화) | stat |

부수 관측: `security list-keychains` 검색 목록에 `/private/tmp/agy-smoke.fLDeCI/b/Library/Keychains/login.keychain-db`가 남아 있다. 과거 Antigravity 스모크 테스트의 잔여물로 보이며 이 ADR 범위 밖이라 건드리지 않았다(정리 여부 **확인 필요**).

### 2.4 서명키보다 먼저 보이는 구멍: PostgreSQL trust

pg_hba가 루프백 TCP와 유닉스 소켓 모두 `trust`다. 같은 호스트의 어떤 프로세스든, uid와 무관하게 비밀번호 없이 어떤 DB 역할로든 접속할 수 있다. 에이전트는 네트워크가 열려 있어(`sandbox_workspace_write.network_access=true`, `codex.py:136`) `psql -h 127.0.0.1 ttakkari`로 refresh 토큰 해시 삽입, Credential 교체, 기억·대화 열람이 가능하다(**추론**, 악용은 미실행). 서명키를 숨겨도 DB 경로로 소유자 등가 권한이 남는다. 선택지 A·B 모두 이 항목을 함께 고쳐야 효과가 있다.

## 3. 선택지

- **A. 엔진 전용 macOS 사용자.** 예: `ttk-agent`. 백엔드는 winterholic, 엔진은 별도 uid로 띄우고 홈 권한을 분리한다.
- **B. 엔진별 sandbox 읽기 거부.** Codex permission profile, Claude Code sandbox, Antigravity sandbox로 보호 경로 읽기를 OS 수준에서 막는다. 원안의 "서명키를 백엔드 전용 키체인 ACL로 이동"도 여기서 평가한다.
- **C. 위험 수용과 문서화.**
- **D. 조합(권고).** 지금은 DB 인증 + B를 하고, 고위험 콘텐츠 Run만 장기적으로 A 또는 VM으로 격리한다.

### 3.1 막는 공격과 남는 공격

| | 막는 공격 | 남는 공격 |
|---|---|---|
| A | 엔진 uid로 소유자 0600·0700 파일 읽기(서명키, 계정, Chrome 세션). 소유자 프로세스에 signal 보내기. 하네스(`~/.claude`) 무단 수정, 단 쓰기를 주지 않을 때만 | **pg trust 그대로면 DB 경유 전권**. staff 그룹·world 읽기 파일 노출(홈이 `drwxr-x---`라 새 사용자가 staff에 들어가면 읽힘, **확인 필요**). 쓰기를 열어 준 `~/development`·outbox 안 자료 변조. 루프백 관리 포트(caddy admin, llama-server, S16). MCP 도구 남용(S7) |
| B | sandbox가 걸린 엔진의 셸 명령이 보호 경로를 읽는 것. guard가 놓친 `python3 -c`·변수 은닉도 포함(Codex 실측) | sandbox 없는 엔진이나 경로: Muse(미확인), Claude Read 도구(guard만, 미확인), Antigravity 내장 파일 도구(미확인), qwen-local(미확인). 엔진 본체와 MCP 서버는 sandbox 밖. pg trust. 하네스 쓰기 지속성(S8). sandbox 프로필을 하네스 설정이 덮을 위험 |
| B의 키체인 이동 | 평문 config.env 유출 사고(백업·로그로 새는 경우) | **같은 uid 공격은 거의 못 막는다.** 키체인 ACL은 uid가 아니라 코드 서명 기준으로 앱을 구분한다. 백엔드가 쓰는 venv python을 허용하면 엔진도 같은 python으로 읽을 수 있다(**확인 필요**: Homebrew python ad-hoc 서명의 ACL 식별 방식). 다른 바이너리로 읽으면 GUI 승인창이 떠 무인 백엔드가 멈출 수 있다 |
| C | 없음 | 전부. S3·S8 그대로 |
| D | A·B의 합. DB 인증 수정으로 DB 경로 차단 | 고위험 Run 분류 밖의 일반 Run에는 B의 잔여 위험이 남음 |

### 3.2 엔진별 적용 가능성과 난이도

| 엔진 | A (별도 uid) | B (sandbox 읽기 거부) |
|---|---|---|
| claude | 중. 계정 로그인을 새 사용자 홈에서 다시 해야 함. `CLAUDE_CONFIG_DIR` 다계정(ADR 0006) 재구성 | 중. sandbox 설정 키 존재(바이너리 문자열). Bash 적용·Read 도구 범위·MCP 영향 실측 필요 |
| codex | 중. `CODEX_HOME` 계정 이동 | **하. OS 수준 실측 통과**(`codex sandbox`). `exec` 경로 적용 방법만 확인 필요 |
| antigravity | **상**. 로그인·OAuth가 login keychain에 기대는데 GUI 세션 없는 사용자는 keychain이 잠기거나 없을 수 있음(**확인 필요**). ADR 0029의 상태 경로 override 부재와 겹침 | 중. `--sandbox` 이미 켜짐. 기본 허용 목록과 내장 파일 도구 범위 확인 필요 |
| muse | 중. 프로필·runtime root 이동 | 미정. SDK에 OS sandbox가 없으면 hook(휴리스틱)만 남음 |
| qwen-local | 하. MCP·자격증명을 받지 않음(`agents/qwen_local.py:1-4`). 자체 하네스만 옮기면 됨 | 하네스 소관. 자체 sandbox 범위 **확인 필요** |

### 3.3 정상 사용 영향

WBS 불변 조건: `~/development` 쓰기, 셸·테스트, 네트워크는 결정과 무관하게 유지한다.

| 항목 | A | B | C |
|---|---|---|---|
| `~/development` 쓰기 | 현재 `drwx------`라 그대로는 못 씀. 그룹 ACL 상속(`chmod +a`) 필요. 에이전트가 만든 파일 소유자가 달라져 git `safe.directory`(dubious ownership) 경고, 사용자 도구와 권한 충돌 가능 | 영향 없음. 실측에서 작업공간 쓰기 성공 |
| 셸·테스트 | 사용자 전역 도구(Homebrew, uv, pnpm 캐시, `~/.cargo` 등) 경로·캐시를 새 사용자용으로 다시 구성해야 함 | 테스트가 보호 경로를 읽으면 실패. 운영 테스트는 `TTAKKARI_NO_ENV_FILE`로 config.env를 안 읽음(`config.py` `get_settings`) |
| 네트워크 | 영향 없음 | Codex는 profile에 `network.enabled = true` 필요(실측). 다른 엔진 확인 필요 |
| 엔진 로그인 갱신 | 새 사용자로 각 엔진 재로그인. Antigravity는 keychain 문제로 불확실. 소유자 대화형 세션과 계정 공유가 끊김 | 엔진 본체는 sandbox 밖이라 토큰 갱신 영향 없음(Codex 구조상 추론) |
| MCP | HTTP로 백엔드에 붙으므로 동작. outbox(`~/.ttakkari/data/outbox`) 쓰기 권한을 따로 줘야 함 | MCP 서버가 sandbox 안이면 `get_settings()`가 config.env를 읽다 실패할 수 있음(**확인 필요**). sandbox 밖이면 영향 없음 |
| 하네스 수정(사용자 결정 10-11) | 소유자 `~/.claude`를 계속 고치게 하려면 쓰기를 줘야 하고 그러면 S8이 남음 | 영향 없음(쓰기 루트 유지). S8은 EXEC-04로 감지 |

### 3.4 운영·배포 영향과 작업량

| | 운영·배포 영향 | 대략 작업량 |
|---|---|---|
| A | 사용자 생성·sudoers(NOPASSWD 래퍼) 또는 엔진용 LaunchDaemon이 필요하고 관리자 권한 작업이다. 백엔드 `RunManager`의 spawn 경로, 세대 배포의 drain·killpg(다른 uid 프로세스 종료 권한), warm pool 수정. 원복은 가능하지만 침습적 | 5~10일. 엔진 5종 재로그인과 회귀 포함, **확인 필요** |
| B | 어댑터 인자·프로필 파일만 바뀌고 세대 배포 절차는 그대로. 엔진 CLI 업데이트 때 설정 키가 바뀔 위험이 있어 회귀 테스트가 필요 | Codex 0.5~1일, Claude·Muse 1~2일(실측 포함), Antigravity 1일, DB 인증 0.5~1일 |
| B 키체인 | launchd LaunchAgent는 GUI 세션에서 돌아 login keychain 접근은 가능할 것으로 보이나, 재부팅 후 로그인 전·잠금 상태의 기동 실패 가능성 **확인 필요**. ACL 승인창은 무인 기동을 막을 수 있음 | 1일 + 기동 회귀 |
| C | 없음 | 0.5일(문서) |
| D | B와 같음. 장기 격리는 별도 결정 | 단기 3~5일, 장기는 A 수준 |

## 4. 권고안 (D, 단계적)

**권고 요지.** 지금 별도 macOS 사용자(A)로 가는 것은 비용 대비 효과가 낮다. pg trust를 그대로 두면 uid를 나눠도 DB로 전권이 넘어가고, Antigravity 로그인과 `~/development` 소유권 문제가 크기 때문이다. 키체인 이동도 같은 uid 공격을 거의 막지 못하므로 보류한다. 대신 (1) DB 인증을 먼저 고치고, (2) 실측으로 동작이 확인된 엔진 sandbox 읽기 거부를 엔진별로 붙이고, (3) sandbox로 막을 수 없는 엔진은 실측 결과에 따라 "그 엔진에 고위험 콘텐츠 작업을 배정하지 않음"으로 수용을 기록한다. 장기적으로는 외부 콘텐츠를 다루는 고위험 Run(리버스 엔지니어링, GitHub 탐색, 웹·업로드 문서 처리)만 별도 사용자나 VM에서 돌리는 방안을 설계한다.

### 4.1 단기 (저비용, Phase 1~2)

1. **DB 인증**: 루프백 `trust`를 `scram-sha-256`으로 바꾸고 비밀번호는 config.env에 둔다. 유닉스 소켓을 `peer`로만 바꾸면 같은 uid는 그대로 통과하므로 TCP·소켓 모두 비밀번호를 요구해야 한다. 비밀번호 파일이 B로 보호될 때 비로소 같은 uid에도 효과가 난다.
2. **Codex**: 어댑터가 쓰는 run별 profile 파일에 `filesystem` 거부 목록을 넣는다. 대상은 `~/.ttakkari`(단 inbox·outbox·Run 작업 영역은 허용), `~/.claude/.credentials.json`, `~/.claude.json`, `~/.gemini/antigravity-cli/antigravity-oauth-token`, `~/Library/Keychains`, Chrome 프로필이다.
3. **Claude·Muse**: Claude Code sandbox(`denyRead`)를 격리 환경에서 실측한 뒤 적용한다. Muse는 SDK 기능을 확인하고 없으면 hook 유지 + 수용 기록.
4. **Antigravity·qwen-local**: EXEC-01의 남은 칸을 canary로 실측한다. 기본 sandbox가 이미 막으면 기록만 하고, 아니면 허용 목록 조정 또는 고위험 작업 배정 제외.
5. guard는 "보험"으로 유지한다. EXEC-02의 xfail 해소는 sandbox가 있는 엔진에서는 우선순위를 낮춘다.

### 4.2 장기

- 고위험 콘텐츠 Run 격리(별도 uid 또는 경량 VM). 대상 작업 종류, 자료 반입·반출 경로, 엔진 계정 분리를 설계한다.
- 하네스 쓰기 권한(사용자 결정 10-11)을 "작업 요청 시에만 쓰기 루트에 추가"로 좁히는 안을 EXEC-04에서 함께 검토한다.
- 백엔드가 서명된 단일 바이너리가 되면 키체인 ACL을 재평가한다.

## 5. 후속 WBS 제안

결정에 따라 아래 ID를 `docs/security/wbs.md`에 확정한다. 이 ADR은 wbs.md를 수정하지 않는다.

| 제안 ID | 범위 | 선택지 | 우선순위 제안 | 작업량 |
|---|---|---|---|---|
| SEC2-DB-01 | pg_hba `trust` 제거, scram 비밀번호를 config.env로, 백엔드·마이그레이션·백업(OPS-01)·세대 배포 회귀 | A·B·D 공통 | P0 | 0.5~1일 |
| SEC2-EXEC-05 | Codex run profile에 보호 경로 `none`과 `network.enabled`. `exec` 적용 방식 확인, canary RED/GREEN, MCP·브라우저 MCP 대조 | B·D | P0 | 0.5~1일 |
| SEC2-EXEC-06 | Claude Code sandbox `denyRead` 실측·적용, Read 도구 범위, Muse SDK 대응 | B·D | P1 | 1~2일 |
| SEC2-EXEC-03 (조정) | 기존 엔진별 guard 동등성에 "Antigravity 기본 seatbelt 허용 목록과 내장 파일 도구 범위", "qwen-local sandbox" 실측 추가. EXEC-01 미실측 칸 종결 | 전부 | P1 유지 | +0.5일 |
| SEC2-EXEC-02 (조정) | sandbox 적용 엔진에서는 보험으로 강등(P3). sandbox 없는 엔진이 남으면 P2 유지 | B·D | P2→P3 조건부 | 변화 없음 |
| SEC2-EXEC-04 (조정) | A면 하네스 분리로 대체, B·D면 감지·알림 유지 + 하네스 쓰기 루트 축소 검토 | 전부 | P1 유지 | +0.5일 |
| SEC2-ISO-01 | 고위험 콘텐츠 Run 격리 설계(별도 uid 또는 VM) ADR | D 장기, A | P2 | 1일(설계) |
| SEC2-KEY-01 | 서명키 키체인 이동. 서명된 백엔드 바이너리 전에는 보류 | B 원안 | 보류 | 1일 |

C를 고르면 위 항목 대신 threat-model S3·S8 상태를 "수용(사유·날짜·재검토 조건)"으로 바꾸고, SEC2-DB-01만은 별도로 판단하기를 권한다. uid와 무관한 구멍이기 때문이다.

## 6. 사용자 결정 질문

1. 신뢰 모델: (a) D 단계적 권고 (b) A 지금 별도 사용자 (c) B만 (d) C 수용
2. DB 인증(SEC2-DB-01)을 신뢰 모델 결정과 별개로 바로 진행할까: (a) 바로 진행 (b) 결정과 함께 (c) 보류
3. sandbox로 막을 수 없는 엔진이 남으면: (a) 그 엔진은 고위험 콘텐츠 작업에서 제외 (b) 위험 수용 후 계속 배정 (c) 그 엔진만 A로 격리
4. 하네스(`~/.claude`·`~/.codex`) 상시 쓰기 허용(사용자 결정 10-11)을: (a) 유지하고 감지만 (b) 요청 시에만 열기 (c) 지금 결정 보류

## 7. 확인 필요 목록

- `codex exec`에 permission profile을 적용하는 방법과 legacy `sandbox_mode` 병용 가능 여부. MCP 서버 자식의 sandbox 적용 여부.
- Claude Code sandbox의 엔진 경유 동작, Read 도구 적용 범위, MCP 영향.
- Muse SDK의 OS sandbox 지원 여부.
- Antigravity 기본 seatbelt의 읽기 허용 목록에 `~/.ttakkari` 포함 여부, 내장 파일 도구의 sandbox 적용 여부.
- qwen-local 하네스 sandbox 범위.
- 키체인 ACL이 Homebrew·venv python을 어떻게 식별하는지, launchd LaunchAgent 기동 시점의 login keychain 잠금 상태.
- 별도 사용자를 staff 그룹 밖에 둘 수 있는지와 홈 `drwxr-x---+` ACL 내용.
- `~/.ttakkari` 하위 실제 권한(이 Run의 guard가 stat을 차단). 소유자가 직접 `stat -f '%Sp %Su %N' ~/.ttakkari ~/.ttakkari/config.env ~/.ttakkari/accounts`를 실행해 채운다.
- 계정별 `CODEX_HOME` 위치와 `auth.json` 권한.
