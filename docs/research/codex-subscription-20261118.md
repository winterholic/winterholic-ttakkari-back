# Codex 구독 사용량 조회 · 최소 비대화형 활성화 · app-server 인증/초기화 조사

조사일: 2026-11-18. 설치 CLI: `codex-cli 0.166.0` (`/opt/homebrew/bin/codex`).
성격: 읽기 전용 조사 기록(11-18 시점). 코드 변경 없음. 11절은 2026-11-21 운영 관측 추가 기록. 실제 인증 파일(`~/.codex/auth.json`)·키체인 직접 읽기 없음, 실제 모델 요청 없음.
실험은 전부 빈 임시 `CODEX_HOME` 에서만 했고 자식 프로세스 env 에서 `CODEX_API_KEY`/`OPENAI_API_KEY` 를 제거했다.

이 문서는 두 번의 조사(스키마·주간 가설·exec, 그리고 app-server 인증 갱신·초기화)를 합쳐 한 파일에 보존한다. 표기 규칙: **확정**(공식 원문 또는 실행 출력으로 확인), **가설**(근거 있는 추정), **미확인/미검증**(확인하지 못함).

## 0. 핵심 결론

| 항목 | 판정 | 근거 |
|---|---|---|
| `account/rateLimits/read` 와 `account/read` 존재·스키마 | 확정 | 공식 문서(원문) + 설치 CLI 생성 JSON Schema |
| `usedPercent`/`windowDurationMins`/`resetsAt` 의미 | 확정 (문구 수준) | 공식 Field notes |
| `RateLimitWindow` 에서 필수는 `usedPercent` 뿐 | 확정 | 0.166.0 생성 스키마 `required: ["usedPercent"]` |
| 주간 창이 "첫 사용 시점"에 시작 | **공식 보장 없음 (미확인)** | 해당 의미의 문장을 어떤 공식 페이지에서도 찾지 못함 |
| 조회(`rateLimits/read`) 자체가 창을 시작/비시작 | **공식 보장 없음 (미확인)** | 읽기 전용이라는 명시 없음 |
| 주간 초기화 도래 후 응답 변화 | **부분 확정** (2026-11-21 운영 관측, 11절) | 11-18 시점에는 초기화가 오지 않아 미검증. 11-21 세 계정의 reset+10초 관측으로 즉시 새 창(0%, `resetsAt`=관측+7일)을 보고함을 확인 |
| `codex exec` 최소 플래그 | 확정 | `codex exec --help` + 공식 non-interactive 문서 |
| exec 의 ChatGPT 로그인 강제(`-c forced_login_method`) | 미검증 | 설정 키는 공식. exec 호출 단위 적용과 불일치 시 동작은 실행하지 않음 |
| app-server 는 쓰기 가능한 `CODEX_HOME` 이 필요 | 확정 (실측) | 읽기 전용/없는 홈에서 initialize 전 exit 1 재현 |
| 중첩 샌드박스 `process_exited` 의 실제 원인 | **미확정** | 그 환경의 stderr/exit code 를 보지 못함. 위 항목은 후보일 뿐 |
| 구독 로그인 있는 홈에서의 quota 실응답 | **미검증** | 인증 사용이 필요해 하지 않음 |
| `refreshToken:false` 가 자동 갱신을 막는지 | **미확인** | 문서에 보장 없음 |
| 구독 자동화 | 공식: API 키가 기본, ChatGPT 관리 인증은 "신뢰 가능한 비공개 러너 + 직렬 사용" 조건의 고급 경로 | 5절 |

## 1. 버전 근거

```
$ which codex ; codex --version
/opt/homebrew/bin/codex
codex-cli 0.166.0
```

- `codex app-server --help` 의 첫 줄: `[experimental] Run the app server or related tooling`. 프로토콜은 실험적이며 버전 종속.
- 공식 문서: 생성된 스키마 산출물은 "실행한 Codex 버전에 정확히 일치". 따라서 0.166.0 이외 버전에서는 재생성해 대조해야 한다.
- 스키마 생성(로컬, 인증·모델 호출 불필요):

```
$ codex app-server generate-json-schema --out /tmp/cxr/schema
(39개 파일; v2/GetAccountRateLimitsResponse.json, v2/GetAccountResponse.json, v2/AccountRateLimitsUpdatedNotification.json 등)
$ python3 -c "...GetAccountRateLimitsResponse.json..."
['accountId', 'ordinaryUsageAllowed', 'rateLimitResetCredits', 'rateLimitUpsell', 'rateLimits', 'rateLimitsByLimitId']
required: ['rateLimits']
```

## 2. App Server 스키마

### 2.1 공식 URL (실제 열람)

- https://developers.openai.com/codex/app-server → HTTP 308 → https://learn.chatgpt.com/docs/app-server (서버 응답 리다이렉트).
- 정본 원문: `curl https://developers.openai.com/codex/app-server.md` → HTTP 200, 109,524 bytes. learn.chatgpt.com 쪽 `.md` 도 같은 크기.
- GitHub `openai/codex` 의 `codex-rs/app-server/README.md` → 200, 61,621 bytes 이지만 `rateLimits` 설명이 **없음**. `docs/app-server.md` 는 404.
- 주의: WebFetch(요약기)는 처음에 "rateLimits 문서 없음"이라고 오답했다. `curl` 로 받은 원문 grep 이 정본이다.

### 2.2 핸드셰이크 (공식 "Initialization")

연결당 `initialize` 요청 1회 + `initialized` 알림. 그 전 요청은 `Not initialized`, 중복 `initialize` 는 `Already initialized`. 기본 전송 `stdio://` (줄 단위 JSON, `"jsonrpc"` 헤더 생략).

```json
{ "method": "initialize", "id": 0, "params": { "clientInfo": { "name": "my_client", "title": "My Client", "version": "0.1.0" } } }
```

`clientInfo.name` 은 OpenAI Compliance Logs Platform 식별용.

### 2.3 `account/read`

요청(공식 예시): `{ "method": "account/read", "id": 1, "params": { "refreshToken": false } }`

응답(공식 예시):

```json
{ "id": 1, "result": { "account": { "type": "chatgpt", "email": "user@example.com", "planType": "pro" }, "requiresOpenaiAuth": true } }
{ "id": 1, "result": { "account": { "type": "apiKey" }, "requiresOpenaiAuth": true } }
{ "id": 1, "result": { "account": null, "requiresOpenaiAuth": true } }
```

0.166.0 생성 스키마(`GetAccountResponse.json`):
- 필수 `requiresOpenaiAuth: boolean`. `account` 는 null 가능.
- `account` oneOf: `apiKey` | `chatgpt`(`email` nullable, `planType` 필수, `workspaceName` nullable) | `amazonBedrock` | `gcp`.
- `planType` enum: free, go, plus, pro, prolite, promax, team, self_serve_business_prolite, self_serve_business_usage_based, business, ent26, enterprise_cbp_automation, enterprise_cbp_usage_based, enterprise, edu, edu_plus, edu_pro, sci, sci_plus, unknown. 모르는 값은 unknown 취급.
- 실측 응답에는 스키마 필수가 아닌 `workspaceRouting`(null) 이 함께 왔다(3.1절).
- `refreshToken: true` 는 관리형 ChatGPT 모드에서 토큰 갱신 강제. 외부 토큰 모드에서는 무시.
- `userId` 는 nullable 이며 구버전 서버에는 없을 수 있다.

### 2.4 `account/rateLimits/read`

공식 예시(문서 "6) Rate limits (ChatGPT)"):

```json
{ "method": "account/rateLimits/read", "id": 6 }
{ "id": 6, "result": {
  "rateLimits": { "limitId": "codex", "limitName": null,
    "primary": { "usedPercent": 25, "windowDurationMins": 15, "resetsAt": 1730947200 },
    "secondary": null, "rateLimitReachedType": null },
  "rateLimitsByLimitId": { "codex": { "...": "동일 형태" }, "codex_other": { "...": "..." } },
  "rateLimitResetCredits": { "availableCount": 2, "credits": [ { "id": "RateLimitResetCredit_1", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1781654400, "expiresAt": 1784246400 } ] }
} }
{ "method": "account/rateLimits/updated", "params": { "rateLimits": { "limitId": "codex", "primary": { "usedPercent": 31, "windowDurationMins": 15, "resetsAt": 1730948100 } } } }
```

공식 Field notes 원문 요지:
- `rateLimits`: 하위 호환 단일 버킷 뷰. `rateLimitsByLimitId`(있을 때만): `limit_id`(예: `codex`)로 묶은 다중 버킷 뷰.
- `usedPercent`: "current usage within the quota window".
- `windowDurationMins`: "the quota window length".
- `resetsAt`: "a Unix timestamp (seconds) for the next reset".
- `planType`, `credits`, `rateLimitReachedType` 은 서버가 주면 포함. `rateLimitResetCredits.availableCount` 가 권위값.

0.166.0 생성 스키마(`GetAccountRateLimitsResponse.json`):
- 최상위 필수는 `rateLimits` 하나. 선택: `rateLimitsByLimitId`, `rateLimitResetCredits`, `accountId`, `ordinaryUsageAllowed`("Null means unavailable; clients must not infer recovery from percentages or reset times"), `rateLimitUpsell`.
- `RateLimitSnapshot`: `limitId`, `limitName`, `planType`, `primary`, `secondary`, `credits`, `rateLimitReachedType`, `individualLimit`, `spendControlReached`, `normalModelSlug` 전부 nullable/선택.
- `RateLimitWindow`: `usedPercent`(int32) 만 필수. `resetsAt`(int64|null), `windowDurationMins`(int64|null).
- `CreditsSnapshot`: `hasCredits`, `unlimited` 필수, `balance` nullable.
- `RateLimitReachedType`: rate_limit_reached, workspace_owner_credits_depleted, workspace_member_credits_depleted, workspace_owner_usage_limit_reached, workspace_member_usage_limit_reached.
- 요청 params(`NullableGetAccountRateLimitsParams`)는 null 허용. `excludeResetCreditDetails`("Skip the separate reset-credit detail lookup for background usage polls") 등 클라이언트 capability 필드가 있다.
- 알림 `account/rateLimits/updated` 스키마 설명: "Sparse rolling rate-limit update. Clients should merge available values into the most recent `account/rateLimits/read` response or refetch that snapshot. Nullable account metadata ... does not clear a previously observed value." 알림만으로 스냅샷을 대체하면 안 된다.

### 2.5 어떤 창이 주간인지

공식 예시는 15분/60분 창(예시 값)이다. 실제 계정의 `primary`/`secondary` 가 5시간/주간인지는 문서 보장이 없고 **미검증**. 구현은 `windowDurationMins` 로 분류하되 null/예상 밖 값은 원본 노출.

## 3. 주간 갱신 가설

### 3.1 공식 문서로 확인된 것

- https://developers.openai.com/codex/pricing.md (200): "Local messages and cloud chats share your plan's usage allowance. **Weekly limits may also apply.**" / "check your usage dashboard for current limits and reset times." / Plus·Business 는 5시간 단위 추정치 표, "Pro plans currently have no five-hour limit."
- 사용량 확인 위치: https://chatgpt.com/codex/settings/usage, CLI 세션 `/status`. pricing FAQ: "Check the dashboard every week or two".
- app-server 는 `resetsAt` 를 "다음 초기화 시각"으로만 정의.

### 3.2 공식 보장이 없는 것 (미확인)

`grep -i "weekly|first use|starts when|window start|rolling"` 를 app-server, pricing, non-interactive, automations 문서에 실행했으나 해당 의미의 문장이 없었다(pricing 의 "Weekly limits may also apply" 외).

1. 주간 창이 "첫 사용 시점"에 시작한다: **미확인**. `resetsAt` 가 사용 시작 기준인지 계정 고정 주기인지 문서가 정하지 않는다.
2. `account/rateLimits/read` 호출이 창을 시작시키는지 여부: **미확인**. 읽기 전용이라는 명시가 없다. 메서드 이름과 "Sparse ... update" 설명은 읽기 성격을 시사할 뿐 보장은 아니다.
3. 주간 초기화 도래 후 응답 변화: **미검증**(11-18 시점). 초기화가 도래하지 않아 관측하지 못했고, 검증됐다고 주장하지 않는다. → 11-21 관측은 11절.

### 3.3 구현 가설 (실측 필요)

- H1: 창 만료 후 첫 모델 호출이 새 창을 시작한다. 미확인.
- H2: 만료 후 read 는 `resetsAt` 가 과거인 채로 오거나 `usedPercent` 가 0이 된다. 미확인. → 11-21 운영 관측: `resetsAt` 은 과거로 남지 않고 곧바로 미래 시각(관측+7일)으로 오며 0%였다(11절). `resetsAt <= now` 는 "만료됨, 상태 불명"으로 두고 재조회하는 방어를 권장.
- 검증안(미실행): 만료 직전·직후에 read 만 반복해 `resetsAt` 변화를 기록하고, 이후 최소 `codex exec` 1회 뒤 read 와 비교. 실제 모델 호출이 필요해 하지 않았다.

## 4. `codex exec` 최소 비대화형 호출

### 4.1 설치 CLI help (0.166.0 `codex exec --help` 발췌)

- `--skip-git-repo-check`: "Allow running Codex outside a Git repository"
- `--ephemeral`: "Run without persisting session files to disk"
- `-s, --sandbox <read-only|workspace-write|danger-full-access>`
- `--ignore-user-config`: "Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`"
- `--ignore-rules`: "Do not load user or project execpolicy `.rules` files"
- `--json`, `-o/--output-last-message <FILE>`, `-C/--cd <DIR>`, `-m/--model`, `-c key=value`, `--output-schema`. 프롬프트는 인자 또는 stdin(`-`).

공식 non-interactive 문서(https://developers.openai.com/codex/noninteractive.md, 200):
- "By default, `codex exec` runs in a read-only sandbox."
- "`codex exec` reuses saved CLI authentication by default."
- Git 저장소 필요, `--skip-git-repo-check` 로 우회. `--ephemeral` 은 세션 rollout 파일 비저장이며 프로젝트 trust 는 유지.
- `--ignore-user-config` 는 `$CODEX_HOME/config.toml` 비로딩, `--ignore-rules` 는 사용자·프로젝트 execpolicy `.rules` 비로딩.

### 4.2 최소 호출 후보 (미실행: 모델 호출이므로)

```
codex exec --ephemeral --skip-git-repo-check -s read-only --ignore-rules --json -C <빈 임시 디렉터리> "ok"
```

- `--ignore-user-config` 는 설정만 무시하고 인증은 `CODEX_HOME` 을 그대로 쓴다. 단 `config.toml` 의 `forced_login_method` 도 같이 무시될 수 있어 인증 강제는 별도 수단이 필요하다(추정).
- 가장 작은 모델 이름은 계정 의존이라 미확인. 최소 프롬프트의 실제 한도 소모량은 미측정.

### 4.3 인증 강제

- 공식(auth 문서 "Enforce a login method or workspace"): `forced_login_method = "chatgpt" # or "api"`. "If the active credentials don't match the configured restrictions, Codex logs the user out and exits." 설정 레퍼런스에도 `forced_login_method: chatgpt | api` 등재.
- 가설(미검증): `-c forced_login_method="chatgpt"` 로 호출 단위 강제가 되는지, 불일치 시 저장 자격이 지워지는지 실행하지 않았다. 시험은 별도 `CODEX_HOME` 에서만 해야 한다.
- `CODEX_API_KEY` 는 `codex exec` 에서 API 키 인증으로 쓰인다(공식). 예약 실행 env 에서 `CODEX_API_KEY`, `OPENAI_API_KEY` 를 제거해야 한다. `OPENAI_API_KEY` 가 exec 인증으로 쓰이는지는 문서가 직접 말하지 않아 미확인(경고 문구만 있음).

## 5. 예약 자동화 정책과 과금 경계 (공식 문서)

1. 로그인 두 가지(https://developers.openai.com/codex/auth.md): ChatGPT 로그인 = 구독 접근, API 키 = 사용량 기반. "OpenAI bills API key usage through your OpenAI Platform account at standard API rates", "When you sign in with an API key, Codex uses standard API pricing instead of included ChatGPT plan credits." 과금 경계의 핵심은 exec 가 어떤 자격으로 도는가다.
2. 공식 자동화 기본값은 API 키: "API keys are the right default for automation because they are simpler to provision and rotate. Use this path only if you specifically need to run as your Codex account."
3. ChatGPT 관리 인증의 CI/예약 실행은 공식으로 존재하나 조건부(https://learn.chatgpt.com/docs/auth/ci-cd-auth.md, 200):
   - 신뢰 가능한 비공개 러너만. "Do not use this workflow for public or open-source repositories."
   - `~/.codex/auth.json` 은 비밀번호처럼 취급. "only one machine or serialized job stream will use a given `auth.json` copy". 갱신된 파일을 다음 실행에 보존. 직접 refresh API 를 부르지 말 것.
   - 엔터프라이즈 access token 은 "trusted scripts, schedulers, and private CI runners" 용(auth 문서).
4. 앱의 "Scheduled tasks"(https://learn.chatgpt.com/docs/automations.md, 200)는 ChatGPT 웹/데스크톱 기능이며 "Codex CLI doesn't provide the Scheduled management interface". 자체 로컬 스케줄러가 구독 인증을 쓰는 것을 명시적으로 금지/허용하는 문구는 찾지 못했다. 서비스 약관은 조사 범위 밖이라 미확인.
5. app-server 문서 경고: "App-server authentication has never been permitted for commercial or hosted services." 사용자 본인 Mac 로컬·단일 계정에 한정해야 한다.
6. `forced_login_method`, `forced_chatgpt_workspace_id` 는 공식 정책 도구.

### 과금 방지 체크리스트 (제안)

- 예약 프로세스 env 에서 `CODEX_API_KEY`, `OPENAI_API_KEY` 제거 및 미설정 단언.
- `account/read` 로 `account.type == "chatgpt"` 확인, `apiKey` 면 중단.
- 같은 `CODEX_HOME` 에서 직렬 실행, `auth.json` 을 여러 기기/프로세스와 공유하지 않기.

## 6. app-server 인증 갱신 정책 (공식 문서)

출처: app-server.md "Auth endpoints", auth.md, auth/ci-cd-auth.md.

- 관리형 ChatGPT(`chatgpt`): "Codex owns the ChatGPT OAuth flow, persists tokens, and refreshes them automatically."
- 외부 토큰(`chatgptAuthTokens`, experimental): 호스트가 토큰을 공급하고 401 시 서버가 `account/chatgptAuthTokens/refresh` 요청을 보낸다(약 10초 타임아웃). `initialize.capabilities.experimentalApi = true` 필요.
- 자동 갱신 규칙(CI 가이드, "현재 오픈소스 클라이언트 기준"): `auth.json` 로드, `last_refresh` 가 약 8일보다 오래되면 실행 전에 토큰 번들 갱신, 성공 시 새 토큰과 `last_refresh` 를 `auth.json` 에 기록, 401 이면 내장 refresh-and-retry. "the supported CI/CD strategy is not 'call the refresh API yourself.' It is 'run Codex and persist the updated `auth.json`.'"
- 동시 사용: 한 `auth.json` 사본은 한 머신 또는 직렬 잡 스트림만. 사용자의 대화형 Codex/IDE 와 예약 수집기가 같은 `CODEX_HOME` 인증을 동시에 갱신하는 것은 공식 권장 범위 밖이다. refresh token 회전으로 한쪽이 무효화될 수 있다는 구체 메커니즘은 문서에 없고 **추정**이다.
- `refreshToken:false` 는 "강제하지 않음"일 뿐 자동 갱신을 끈다는 문구는 없다. 읽기 RPC 가 자동 갱신으로 `auth.json` 을 쓸 가능성은 문서상 배제되지 않는다(**미확인**).
- 키링 저장이면 CI 가이드는 적용 대상이 아니다("switch to file-backed storage first"). `cli_auth_credentials_store = file | keyring | auto | ephemeral`. 이 Mac 의 실제 저장 방식은 인증 접근 금지 범위라 확인하지 않았다.
- 기능 플래그 `mcp_oauth_refresh_coordination` (stable, true) 는 존재하나 MCP OAuth 용으로 보이며 ChatGPT 로그인 갱신과의 관계는 문서에 없다(미확인).
- workspace routing: 관리형 로그인 후 routing 준비가 끝나면 `account/updated` 가 다시 오고 클라이언트는 `account/read` 를 다시 읽는다. 선택 워크스페이스 discovery 실패는 `account/read` 에러가 될 수 있고 "A later read retries failed discovery". 읽기 클라이언트는 짧은 재시도가 필요할 수 있다.

### 위험 플래그

- `codex app-server --startup-sign-out`: "Sign out using the managed auth path before loading fresh startup requirements" (`codex app-server --help`). **기동 시 로그아웃**이므로 수집기에서 쓰면 안 된다.
- `account/logout`, `account/login/*` 호출 금지. 읽기 전용 클라이언트는 `account/read`, `account/rateLimits/read` 만 허용하는 화이트리스트가 안전하다.
- `--print-startup-config` 는 인증 로딩 여부를 몰라 실행하지 않았다.

## 7. app-server 초기화 실측 (빈 `CODEX_HOME`, 인증 없음, 모델 호출 없음)

방법: python `subprocess` 로 `codex app-server --listen stdio://` 를 띄워 JSONL 을 주고받는 프로브(`/tmp/cxr/probe.py`, 이 문서 부록 A 에 전문). env 에서 `CODEX_API_KEY`, `OPENAI_API_KEY` 제거, `CODEX_HOME` 만 지정.

### 7.1 쓰기 가능한 빈 홈 (정상 경로)

```
initialize 응답:
{"id":0,"result":{"userAgent":"research_probe/0.166.0 (Mac OS 27.0.0; arm64) unknown (research_probe; 0.0.1)","codexHome":"/private/tmp/cxr/emptyhome.gyT0","platformFamily":"unix","platformOs":"macos"}}
알림: {"method":"remoteControl/status/changed","params":{"status":"disabled",...}}
account/read:  {"id":1,"result":{"account":null,"requiresOpenaiAuth":true,"workspaceRouting":null}}
account/rateLimits/read: {"error":{"code":-32600,"message":"codex account authentication required to read rate limits"},"id":2}
stdin 닫기 → exit 0, stderr 비어 있음
```

기동 후 홈에 생긴 것: `installation_id`, `state_5.sqlite*`, `logs_2.sqlite`, `goals_1.sqlite*`, `memories_1.sqlite*`, `queue_1.sqlite*`, `skills/`, `tmp/`, `.tmp/`. app-server 는 인증을 읽기만 하는 프로세스가 아니라 `CODEX_HOME` 에 상태 DB 를 만들고 쓴다.

### 7.2 `CODEX_HOME` 쓰기 불가 (process_exited 후보 재현)

| 조건 | 결과 (initialize 응답 전, stdout 비어 있음) |
|---|---|
| A. 존재하지만 읽기 전용(`chmod 555`) 빈 홈 | exit 1. stderr: `WARNING: proceeding, even though we could not create PATH aliases: Permission denied (os error 13)` / `Error: failed to initialize sqlite state runtime under /private/tmp/cxr/rohome.cWsG: failed to initialize state runtime at /private/tmp/cxr/rohome.cWsG` |
| B. 존재하지 않는 홈 | exit 1. stderr: `Error: CODEX_HOME points to "/tmp/cxr/does-not-exist-home", but that path does not exist` |
| C. 생성 불가 경로 `/System/nope-home` | exit 1, B 와 같은 메시지 |
| D. 읽기 전용 홈 + `-c sqlite_home=<쓰기 가능>` + `-c log_dir=<쓰기 가능>` | sqlite·log 는 지정 경로에 생겼으나 exit 1, stderr `Error: Permission denied (os error 13)`. 홈 안의 다른 쓰기가 막힌 것으로 추정(파일은 미확인) |

정리:
- 클라이언트에는 "initialize 응답 없이 프로세스 종료"로 보이고 원인은 **stderr 에만** 있다. `process_exited` 에러에 exit code 와 stderr 마지막 N줄을 붙이는 것을 권장.
- 이것은 재현 가능한 후보일 뿐이다. 중첩 샌드박스의 실제 원인(쓰기 불가 홈 / 소켓·네트워크 제한 / 기타)은 **미확정**이며 호스트나 운영 환경 실패로 확대하지 않는다.

### 7.3 후보 원인별 다음 확인 (미실행)

1. 문제의 중첩 샌드박스에서 부록 A 의 프로브를 실행해 returncode 와 stderr 전문을 기록.
2. 샌드박스 쓰기 가능 루트에 `CODEX_HOME` 이 포함되는지 확인.
3. 호스트(샌드박스 밖)에서 같은 프로브를 실제 홈이 아닌 빈 홈으로 실행해 환경 의존성을 분리. 호스트 실패는 관측하지 않았다.

## 8. 구현 시사점

- 읽기 전용 RPC 호출 순서: `initialize` → `initialized` → `account/read(refreshToken:false)` 로 `account.type == "chatgpt"` 확인 → `account/rateLimits/read`. `apiKey` 면 중단.
- 오류 매핑: `-32600 "codex account authentication required..."` 와 `requiresOpenaiAuth:true && account:null` 은 로그인 필요 상태.
- 프로세스 수명: 요청당 spawn 후 stdin 닫기로 exit 0 정상 종료 확인(빈 홈 기준). 장수 프로세스로 쓰면 `account/rateLimits/updated` 병합이 필요하고 자동 갱신 쓰기가 계속 가능.
- `CODEX_HOME` 이 원본이면 app-server 가 거기에 상태 DB 를 만든다(사용자 홈에 파일 추가 부작용). `sqlite_home`, `log_dir` 로 상태 DB·로그 분리는 가능(D 에서 확인)하지만 홈 자체가 쓰기 불가이면 기동 실패. 인증을 사본 홈에 복사하는 방식은 중복 갱신 위험과 인증 파일 접근 범위 문제로 권장하지 않는다(선택은 메인이 한다).
- exec 모델 호출로 사용량을 조회하는 방식을 읽기 RPC 로 바꾸는 이유(조회가 모델 요청을 쓰는 점)는 타당하다. 다만 읽기 RPC 도 자동 갱신으로 `auth.json` 을 쓸 수 있다는 점은 배제되지 않는다.

## 9. 실행 기록

| 명령/동작 | 결과 |
|---|---|
| Read `.../skills/.system/openai-docs/SKILL.md` | 차단: `[ttakkari-guard] ttakkari 보호 영역 접근 차단`. 재시도 안 함. 해당 스킬 지침은 미반영하고 공식 URL 을 직접 열람 |
| Write 최초 지정 outbox (다른 Run) | 차단: `워크스페이스 밖 쓰기 차단`. 재시도 안 함. 이후 허용 저장소에 작성(이 파일) |
| `codex --version` | `codex-cli 0.166.0` |
| `codex exec --help`, `codex app-server --help`, `codex app-server daemon --help`, `codex login --help`, `codex --help` | 정상 출력, 4·6절 근거 |
| `codex features list` (grep) | `mcp_oauth_refresh_coordination stable true`, `secret_auth_storage stable false`, `sqlite removed true` |
| `codex app-server generate-json-schema --out /tmp/cxr/schema` | 정상, 39개 파일 |
| WebFetch `developers.openai.com/codex/app-server`, `/codex/noninteractive` | HTTP 308 → learn.chatgpt.com. app-server 요약은 rateLimits 를 못 찾음(오탐) |
| `curl https://developers.openai.com/codex/app-server.md` | 200, 109,524 bytes, rateLimits 확인 |
| `curl https://raw.githubusercontent.com/openai/codex/main/docs/app-server.md` | 404 |
| `curl .../codex-rs/app-server/README.md` | 200, rateLimits 설명 없음 |
| `curl developers.openai.com/codex/{auth,pricing,noninteractive}.md`, `learn.chatgpt.com/docs/{automations,auth/ci-cd-auth,config-file/config-reference,pricing}.md`, `learn.chatgpt.com/llms.txt` | 모두 200 |
| `timeout 20 codex app-server ...` | 이 Mac 에 `timeout` 명령이 없어 `command not found` (exit 127). python subprocess 프로브로 대체 |
| `python3 probe.py <home>` 케이스 7.1, A~D | 7절 결과 |

URL:
- https://developers.openai.com/codex/app-server.md (= https://learn.chatgpt.com/docs/app-server.md)
- https://developers.openai.com/codex/auth.md
- https://developers.openai.com/codex/pricing.md
- https://developers.openai.com/codex/noninteractive.md (= https://learn.chatgpt.com/docs/non-interactive-mode.md)
- https://learn.chatgpt.com/docs/auth/ci-cd-auth.md
- https://learn.chatgpt.com/docs/automations.md
- https://learn.chatgpt.com/docs/config-file/config-reference.md (`sqlite_home`, `log_dir`, `forced_login_method`, `cli_auth_credentials_store`)
- https://learn.chatgpt.com/docs/pricing.md
- https://learn.chatgpt.com/llms.txt (문서 색인)
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server/README.md

## 10. 미검증 범위

- 주간 창 시작 시점, 조회의 창 시작 여부(3.2). 주간 초기화 도래 후 동작은 11-18 시점에 관측하지 못했다(11-21 관측은 11절. 활성화 요청 뒤 창이 시작 시각에 고정되는지는 여전히 미검증).
- 실제 계정의 `primary/secondary` `windowDurationMins` 값과 현재 `planType`.
- 구독 로그인이 있는 홈에서 `codex app-server` 로 `account/read`/`rateLimits/read` 실응답 받기, `refreshToken:false` 가 자동 갱신을 막는지: 저장 인증 사용이 필요해 하지 않음.
- 중첩 샌드박스 `process_exited` 의 실제 원인(7.3 의 확인 3건). D 케이스의 두 번째 `Permission denied` 가 어떤 파일 쓰기인지.
- `codex exec -c forced_login_method=...` 동작, `OPENAI_API_KEY` 의 exec 인증 사용 여부, 최소 호출의 실제 소모량.
- 상태 DB 생성이 인증 로드/갱신보다 먼저인지 후인지(빈 홈에서는 관측 불가). 사용자의 실제 인증 저장 방식과 동시 사용 시 refresh token 회전 충돌.
- 자체 스케줄러에서 구독 사용에 대한 약관 판단.
- 문구 인용은 WebFetch 요약이 아니라 `curl` 로 받은 원문 발췌 기준. 프로토콜은 0.166.0 기준 experimental 이라 버전 변경 시 재확인 필요.

## 부록 A. 프로브 스크립트 (`/tmp/cxr/probe.py`, 인증·모델 호출 없음)

```python
import subprocess, json, os, time, threading, sys
home=sys.argv[1]; extra=sys.argv[2:]
env=dict(os.environ); env["CODEX_HOME"]=home
env.pop("CODEX_API_KEY",None); env.pop("OPENAI_API_KEY",None)
p=subprocess.Popen(["codex","app-server","--listen","stdio://"]+extra,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True)
out=[];err=[]
def rd(s,l):
    for line in s: l.append(line.rstrip())
threading.Thread(target=rd,args=(p.stdout,out),daemon=True).start()
threading.Thread(target=rd,args=(p.stderr,err),daemon=True).start()
def send(o):
    try: p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
    except Exception as e: print("send failed:",e)
send({"method":"initialize","id":0,"params":{"clientInfo":{"name":"research_probe","version":"0.0.1"}}})
time.sleep(3)
print("returncode after 3s:",p.poll())
send({"method":"initialized"}); send({"method":"account/read","id":1,"params":{"refreshToken":False}}); time.sleep(2)
print("returncode:",p.poll())
print("STDOUT:"); [print(" ",l[:300]) for l in out]
print("STDERR:"); [print(" ",l[:500]) for l in err[:10]]
try: p.stdin.close()
except Exception: pass
try: p.wait(5)
except Exception: p.kill()
print("final rc:",p.returncode)
```

사용: 반드시 **빈 임시 디렉터리**를 `CODEX_HOME` 으로 준다. 실제 홈(`~/.codex`)을 넘기면 저장된 인증을 사용·갱신할 수 있으므로 넘기지 않는다. 먼저 `export CODEX_HOME=$(mktemp -d)` 후 `python3 probe.py "$CODEX_HOME"`.

## 11. 2026-11-21 운영 관측: 주간 초기화 직후 응답과 자동 활성화 수정 (추가 기록)

**관측 (확정, 운영 기록).** 세 Codex 계정 모두 reset+10초 확인이 1~3초 지연으로 정상 실행됐다. 그러나 자동 활성화 요청은 한 번도 나가지 않았다. 개인 두 계정은 `already_active_or_unverifiable`, 회사 ai2는 따까리 Run 선사용으로 `already_used`였다. 관측 값(KST, 주간 창):

| 계정 | 관측 시각 | 주간 | `resetsAt` | `resetsAt` − (관측+7일) |
|---|---|---|---|---|
| 개인 | 22:30:14.485 | 0% | 11-28 22:30:15 | +0.52초 |
| Daddy | 22:31:33.074 | 0% (단기 창도 0%, 관측+5시간) | 11-28 22:31:34 | +0.93초 |
| 회사 ai2 | 18:24:59.749 | 기록 없음 | 11-28 18:25:00 | +0.25초 |

- 초기화 직후 API는 옛 `resetsAt` 을 되풀이하지 않고 바로 새 주간 창을 보고했다. 미사용 창의 `resetsAt` 은 관측 시각+7일에 1초 안쪽(+0.25~+0.93초)으로 붙은 정수 초였다(표본 3건, 올림 여부는 추정).
- 개인 계정의 옛 초기화 시각은 1795267804(22:30:04), 새 `resetsAt` 은 1795872615다. 이 값이 회귀 테스트(`unit_tests/test_subscription_policy.py`)의 고정값이다. Daddy·ai2의 옛 초기화 시각은 이 기록에 없어 테스트에서 관측−10초로 둔다(`resetsAt`−관측만 실측).

**원인.** `subscription_policy.plan()` 은 초기화 후에도 `resetsAt` 이 옛 값(`new_reset == old_reset`)일 때만 활성화했다. 실제 API는 새 창을 바로 보고하므로 그 분기는 사실상 실행되지 않았다. 새 창이 미래로 이동한 경우는 모두 0% 여부와 무관하게 생략했다.

**수정 (2026-11-21).** 초기화 후 관측이 새 창을 보여 주고 주간 0%, `ordinaryUsageAllowed` true, `|resetsAt − (관측+7일)| < 120초`이며 사용자 선사용·기존 dispatch가 아니면 `reset_confirmed`(옛 초기화 시각 주기)로 활성화한다. 자동 설정이 꺼져 있으면 `auto_disabled`다. 사용률이 0보다 크면 `already_used`, `resetsAt` 이 그보다 이르면(이미 시작된 창으로 추정) `already_active_or_unverifiable` 을 유지한다. 사전 영속 dispatch 표식, 결과 불명 시 재전송 금지, 이름 변경 별칭은 그대로다.

**남는 한계와 미검증.**
- 0% 는 서비스가 반올림한 값이라 극소량 외부 사용이 가려질 수 있다. 그때는 최소 요청이 한 번 더 나간다. 비용이 작고 생략하면 기능 목적이 사라져 수용한 결정이다.
- "시작된 창은 `resetsAt` 이 시작 시각+7일에 고정된다"는 **가설**이다. 11-21 관측 3건은 모두 `resetsAt` 이 관측+7일에 붙어 있었고 고정된 창을 관측한 기록은 없다. 반대로 "관측+7일 = 미사용"도 보장은 아니다. 회사 ai2는 따까리 Run이 있었는데도 `resetsAt` 이 관측+7일이었다(그 Run의 사용이 이미 계수됐는지는 기록 없음). 따까리 Run 선사용은 `user_used` 가 먼저 막지만 외부 CLI/앱 사용은 이 규칙으로 구분하지 못한다.
- 미사용 창에는 초기화 경계가 오지 않는다. 12시간 조회마다 `reset_at` 이 관측+7일로 밀리므로(정책 시뮬레이션으로 10일간 활성화 0회 확인) 이번 수정은 사용으로 시작된 주간 창이 끝나는 실제 경계에서 작동한다. 11-21에 생략돼 아직 미사용인 창은 사용되기 전에는 11-28 22:30 에도 경계가 오지 않는다. 그 계정의 첫 활성화를 앞당길지는 별도 결정이다.
- 수정 뒤 실제 활성화 요청의 결과(모델 접근, 토큰 소모, 창 시작과 `resetsAt` 고정)는 배포 뒤 처음 오는 실제 경계의 관측 전까지 **미검증**이다. 이 수정은 코드·테스트 변경이며 실제 Codex 호출이나 운영 DB 쓰기를 하지 않았다.
- 새 창 형태에서는 판정과 동시에 `reset_at` 이 미래 창으로 이동한다. 그래서 `auto_disabled` 로 생략한 주기를 나중에 자동 켜기·수동 활성화로 다시 열어도 `observed` 만 나오고 요청이 나가지 않는다(임시 탐침으로 확인, 이번 범위 밖).
