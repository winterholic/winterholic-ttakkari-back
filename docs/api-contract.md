# API 계약 (v0.1)

프론트(React PWA)와 백엔드가 공유하는 계약이다. 타입 원본은 `src/ttakkari/schemas.py`. 여기 바뀌면 프론트 `src/api/types.ts` 도 같이 바꾼다.

## 공통

- Base URL: 개발 `http://127.0.0.1:8787`, 운영은 HTTPS 터널 도메인.
- 인증: `Authorization: Bearer <access_token>`. 예외는 `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`, `/api/health`, `/api/dl/{token}`.
- 오류 본문은 항상 `{"code": string, "message": string}`. 검증 오류(422)만 `errors: [{loc, msg}]` 가 추가된다.
  - 주요 code: `unauthorized`, `forbidden`, `not_found`, `conflict`, `rate_limited`, `validation`, `outside_root`, `bad_path`, `sensitive`, `deny`, `gone`, `no_preview`.
- 시각은 ISO 8601 UTC. ID 는 UUID 문자열.

## 인증

| 메서드 | 경로 | 본문 / 응답 |
|---|---|---|
| POST | `/api/auth/login` | `{password}` → `TokenOut`. 리프레시 토큰은 HttpOnly 쿠키 `ttk_refresh`(path `/api/auth`)로 온다. 실패 5회/15분이면 429 + `Retry-After`. 비밀번호 미설정이면 503 |
| POST | `/api/auth/refresh` | 쿠키만 필요 → `TokenOut`. 쿠키를 회전한다. 이미 쓰인 쿠키가 다시 오면 그 계열 전체를 폐기하고 401 |
| POST | `/api/auth/logout` | 204 |
| POST | `/api/auth/logout-all` | Bearer 필요. 모든 기기 세션 폐기 |
| GET | `/api/auth/me` | `{authenticated, password_set}` |

`TokenOut = {access_token, token_type: "bearer", expires_in: 초}`. 프론트는 access token 을 메모리에만 두고, 401 이 오면 refresh 한 번 시도 후 재요청한다. 쿠키가 실리려면 `fetch(..., {credentials: "include"})`.

## 워크스페이스

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/workspaces?include_archived=` | `WorkspaceOut[]` |
| POST | `/api/workspaces` | `{name, root_path, default_engine?, codex_sandbox?, export_allowed?, extra_writable_roots?}` → 201. `allowed_roots` 밖이면 403 `outside_root`(추가 쓰기 경로도 같은 검증, 루트 안쪽·중복은 빠짐, 최대 20개), 중복 409 |
| GET | `/api/workspaces/default` | 기본 워크스페이스. 없으면 config 값으로 만들고, 보관 상태면 되살린다. 기본 루트가 없거나 허용 밖이면 404/403 |
| GET/PATCH | `/api/workspaces/{id}` | PATCH: `{name?, default_engine?, codex_sandbox?, export_allowed?, extra_writable_roots?, archived?}`. extra_writable_roots 는 통째로 교체 |
| GET | `/api/workspaces/{id}/files?path=.&show_hidden=false` | `DirListing {rel_path, entries: FileEntry[], truncated}` |
| GET | `/api/workspaces/{id}/search?q=` | 파일명 부분 일치 `FileEntry[]` (최대 200) |

`FileEntry = {name, rel_path, type: "file"|"dir"|"symlink"|"other", size, modified_at, sensitive}`. 경로는 항상 워크스페이스 기준 상대경로다. 절대경로를 보내지 않는다.

## 세션과 Run

세션 = 엔진 대화 하나(Chat 화면 하나). Run = 사용자 지시 1회.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/sessions?workspace_id=&include_archived=&limit=` | `SessionOut[]` (최근 갱신순). `active_run_id`, `last_run_status`, `pool` 포함. `&pool=work` 면 회사 업무 대화만 |
| POST | `/api/sessions` | `{workspace_id, title?, engine?, model?, effort?, kind?, pool?}` → 201. `pool`: `personal`(기본, 전체 계정 목록 순서) \| `work`(회사 그룹 계정만). 작업 세션은 오케스트레이터 값을 물려받는다 |
| GET/PATCH | `/api/sessions/{id}` | PATCH: `{title?, model?, effort?, archived?}` |
| GET | `/api/sessions/{id}/runs` | `RunOut[]` (오래된 순 = 대화 순서) |
| POST | `/api/sessions/{id}/runs` | `{prompt, context_artifact_ids?, model?, effort?}` → 202 `RunOut`. 실행 중 Run 이 있으면 `queued` 로 세션 대기열에 들어가 앞 Run 이 끝나면 순서대로 시작한다(여러 기기가 동시에 보내도 한 번에 하나) |
| GET | `/api/runs/{id}` | `RunOut` |
| POST | `/api/runs/{id}/cancel` | 취소. 아직 시작 안 한 대기 Run 은 바로 `cancelled`. 실행 중이면 최대 5초 기다렸다가 최신 `RunOut` |
| GET | `/api/runs/{id}/events?after=0&limit=500` | `RunEventOut[]` (폴링·초기 로드용) |
| GET | `/api/runs/{id}/stream?after=0` | SSE. 아래 참고 |
| GET | `/api/runs/{id}/diff` | text/plain unified diff (git 워크스페이스만) |
| POST | `/api/system/stop-all` | 비상 정지 |
| GET | `/api/system/info` | 엔진 설치 여부, 미리보기 변환기 여부, allowed_roots |

`context_artifact_ids` 는 사용자가 Workspace 뷰어에서 보고 있는 결과물이다. 백엔드가 원본 경로를 프롬프트 앞에 붙여 후속 지시의 맥락으로 쓴다.

`RunOut.status`: `queued` → `running` → `succeeded | failed | cancelled | interrupted`. `interrupted` 는 서버 재시작으로 프로세스를 잃은 경우이고, 같은 세션에서 그대로 이어서 지시하면 된다(엔진 대화는 보존).

`effort`: `low | medium | high | xhigh | max`. `model` 은 엔진별 문자열 그대로(claude: `opus`, `sonnet`, `haiku` 또는 전체 ID / codex: `gpt-6-luna` 등).

### SSE 스트림

- 이벤트 이름 `run_event`, `id` = seq, `data` = `RunEventOut` JSON. 스트림이 끝나면 `end` 이벤트 후 연결 종료.
- 재연결 시 `Last-Event-ID` 헤더(또는 `?after=`)로 그 이후부터 다시 받는다. 이벤트는 DB 에 남으므로 유실이 없다.
- EventSource 는 헤더를 못 싣는다. `fetch` 기반 SSE 클라이언트(예: `@microsoft/fetch-event-source`)로 Bearer 를 실어 보낸다.
- 15초마다 ping 주석이 온다.

### Run 이벤트 (`RunEventOut.type`)

| type | payload |
|---|---|
| `run.status` | `{status, ahead?}` (`ahead` = 접수 시점에 앞에 있던 Run 수) |
| `checkpoint` | `{ref}` git 체크포인트 커밋. 되돌릴 때 기준점 |
| `agent.session` | `{engine_session_id, model}` |
| `message` | `{text}` 에이전트 응답(마크다운) |
| `thinking` | `{text}` |
| `tool.call` | `{call_id, name, input}`. name 예: `Bash`, `Write`, `Edit`, `Read`, `shell`, `file_change`, `web_search`, `mcp:<server>/<tool>` |
| `tool.result` | `{call_id, is_error, output, exit_code?}` output 은 8000자에서 자름 |
| `guard.blocked` | `{call_id, reason}` 보험 규칙이 막은 도구 호출 |
| `plan` | `{items}` (codex todo) |
| `usage` | `{cost_usd, ...토큰 수}` |
| `artifact.created` | `{artifact: ArtifactOut}` |
| `run.diff` | `{files: [{path, added, removed}], new_files: string[]}` |
| `log` | `{stream: "stdout"|"system", text}` |
| `error` | `{message}` |
| `run.finished` | `{status, error, result_text, cost_usd, exit_code}` 항상 마지막 |

## Artifact

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/artifacts?workspace_id=&session_id=&run_id=&kind=&area=&changed=&q=&limit=&offset=` | Library 목록(최신순). `area=files` 는 사용자 업로드·직접 등록·넘겨받은 결과물(사용자와 직접 대화한 세션의 outbox, share_file), `area=agent` 는 작업 세션 outbox·작업이 고친 파일 같은 에이전트 중간 산출물이다. 보관함은 두 탭으로 나눠 부르고 세션 화면은 생략한다. `changed=true` 는 작업이 워크스페이스에서 바꾼 파일(`agent_modified`)만, `false` 는 그것을 뺀 결과물·업로드·등록 파일만, 생략하면 모두. 보관함 기본 목록은 `false`, 세션 화면은 생략 |
| POST | `/api/artifacts` | `{workspace_id, rel_path, session_id?}` 워크스페이스 파일을 결과물로 등록 |
| POST | `/api/uploads` | multipart `file`, `workspace_id`, `session_id?` → 201 `ArtifactOut`(`source: upload`). 기본 200MB 초과 413 `too_large`. 결과 ID 를 다음 Run 의 `context_artifact_ids` 에 넣으면 에이전트가 파일 경로를 받는다 |
| GET | `/api/artifacts/{id}` | `ArtifactOut` |
| PATCH | `/api/artifacts/{id}` | `{area: "files"\|"agent"}` 보관함 분류를 옮긴다(파일 내용은 그대로). `ArtifactOut` |
| DELETE | `/api/artifacts/{id}` | 204. 관리 영역 복사본도 지운다 |
| GET | `/api/artifacts/{id}/content?variant=original|preview` | 뷰어용 인라인(Bearer). Range 지원. 민감 파일은 403 |
| POST | `/api/artifacts/{id}/download-link?variant=&inline=` | `{url, expires_at}` 5분짜리 서명 링크. `url` 은 백엔드 기준 상대경로. `inline=true` 면 `/api/view/{token}`(인라인, CSP sandbox) |
| GET | `/api/view/{token}` | 인라인 열람(인증 헤더 불필요). HTML 미리보기 iframe 의 src 로만 쓴다 |
| GET | `/api/dl/{token}` | 첨부 다운로드(인증 헤더 불필요) |
| POST | `/api/artifacts/{id}/preview/retry` | 실패·불가 상태의 미리보기 변환 재시도 |

`ArtifactOut` 주요 필드: `kind` (`markdown|html|pdf|pptx|docx|sheet|image|code|other`) 로 뷰어를 고른다. `source` (`agent_output|agent_modified|user_registered|upload`), `preview_status` (`not_required|pending|processing|ready|failed|unavailable`), `export_policy` (`allow|deny|sensitive`), `downloadable`.

뷰어 규칙:
- `not_required` 인 형식은 `content?variant=original` 을 받아 프론트가 직접 렌더한다(markdown, pdf, sheet, image, code, html).
- `pptx`, `docx` 는 `preview_status == ready` 일 때 `content?variant=preview` (PDF) 를 PDF 뷰어로 연다. `unavailable|failed` 면 원본 다운로드만 제공한다.
- `html` 은 `download-link?inline=true` 로 받은 URL 을 `<iframe sandbox="" src>` 로 연다. 앱 출처에 `srcdoc` 으로 넣지 않는다(디자인 시스템 docs/19 §4). API 도메인이 프론트와 다른 출처라 격리되고, 응답에도 CSP sandbox 가 붙는다.

## Push (Web Push)

VAPID 키는 `~/.ttakkari/vapid.json`(0600)에 있다. `ttakkari gen-vapid` 로 만들고, 없으면 처음 쓸 때 자동 생성한다. 키를 바꾸면 기존 구독이 모두 무효가 되므로 이미 있으면 덮어쓰지 않는다. 푸시 서비스가 `sub` 를 검사하면 이 파일의 `subject` 를 실제 `mailto:` 주소로 바꾼다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/push/vapid-public-key` | `{public_key}` base64url. `pushManager.subscribe` 의 `applicationServerKey` |
| POST | `/api/push/subscriptions` | 본문은 브라우저 `PushSubscription.toJSON()` (`endpoint` https 필수, `keys.p256dh`, `keys.auth`) + 선택 `label`. endpoint 기준 upsert. 204 |
| DELETE | `/api/push/subscriptions` | `{endpoint}`. 없어도 204 |
| POST | `/api/push/test` | 모든 구독에 테스트 알림. `{sent, removed, failed}` |

푸시는 필요한 순간에만 간다. 정상 완료·취소·서버 재시작 중단에는 자동 알림이 없다. 보내는 경우는 둘이다.
- Run 이 오류로 끝났을 때(`failed`): `작업 실패`
- 에이전트가 MCP `notify_user`(`POST /api/agent/notify`)로 직접 보낼 때: 에이전트가 정한 title·body. 입력이나 승인이 필요해 멈춘 경우도 에이전트가 이 도구로 알린다(서버가 Run 을 멈춰 세우는 승인 상태는 없다)

발송 진입점은 `push.service.notify(PushKind, title, body, url=, tag=, target_id=)` 하나다. 새 기능이 알림을 붙일 때는 `PushKind` 에 항목을 더하고 이 함수를 한 줄 부른다. 구독 관리와 발송 인프라는 그대로다.

payload 는 `{title, body, url, tag}`:
- 일정 알림(`PushKind.calendar_reminder`)의 payload 는 아래 "일정" 절을 본다
- `title`: 자동 알림은 `작업 실패`
- `body`: 세션 제목. 80자 초과면 자른다. 푸시 서버를 거치므로 경로·에러 원문은 넣지 않는다
- `url`: `/chat/<session_id>`, `tag`: session_id (같은 세션 알림은 하나로 합쳐진다)

푸시 서비스가 404/410 을 돌려주면 그 구독을 지운다. 다른 오류는 로그만 남긴다. 발송 결과는 감사 로그 `push.run_finished` 에 집계(`sent|removed|failed`)로 남는다.

## 오케스트레이터(ADR 0006)

- `POST /api/sessions` 에 `kind`(`chat` 기본 | `orchestrator`). 오케스트레이터는 엔진이 `auto` 로 고정된다.
- `GET /api/sessions` 는 작업 세션을 빼고, `?parent_id=<오케스트레이터 id>` 면 그 작업 세션만.
- `SessionOut` 에 `kind`, `parent_id`. `RunOut` 에 `origin`(`user` | `report` | `orchestrator`), `profile`(실제로 쓴 엔진 프로필).
- Run 이벤트 `engine.fallback` `{from, to, reason: usage_limit|missing, detail}`.

오케스트레이터 MCP 서버 전용(토큰 `aud=ttakkari-orch`, 세션 한정. 사용자 토큰은 401):

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/orchestrator/workers` | `{title, prompt, engine?(auto), model?, effort?}` → 201 WorkerOut. 동시 실행 20개 초과 429 |
| POST | `/api/orchestrator/workers/{id}/messages` | `{prompt}` → 202 |
| GET | `/api/orchestrator/workers` | 목록(짧은 결과) |
| GET | `/api/orchestrator/workers/{id}` | 상세(결과 4000자·오류·결과물 이름) |
| POST | `/api/orchestrator/workers/{id}/stop` | 대기·실행 Run 취소 |

`WorkerOut`: `{worker_id, title, engine, status(queued|running|succeeded|failed|cancelled|interrupted|idle), active_run_id, last_result, last_error, artifacts}`.

## 엔진 계정(ADR 0006)

개인 계정 목록이 전체이고, 그 안의 일부가 「회사」 그룹이다(`pool=work` 태그). 개인 대화·워커는 회사 그룹까지 전체 순서로 계정을 고르고 한도 폴백도 그 순서다. 회사 업무 대화(`sessions.pool=work`)는 같은 순서에서 회사 그룹 계정만 쓴다. 켜기·임시 순서도 하나뿐이다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/accounts` | 전체 계정 목록, 우선순위 순(회사 그룹 포함). `{id, name, engine, pool, config_dir, enabled, priority, login(ok/unknown/missing), login_label, cooldown_seconds}`. `pool` 은 그룹 태그: `work` = 회사 그룹, `personal` = 태그 없음 |
| POST | `/api/accounts` | `{name, engine, pool?, config_dir?, enabled?}` → 201(전체 목록 맨 뒤 순위). 이름 중복 409, 상대 경로·`~/.ttakkari` 안 422 |
| PATCH | `/api/accounts/{id}` | `{name?, pool?, config_dir?, enabled?}`. `pool` 로 회사 그룹에 넣기/빼기(순서는 그대로). 다시 켜면 한도 대기도 풀린다 |
| DELETE | `/api/accounts/{id}` | 204 |
| POST | `/api/accounts/reorder` | `{ids}` 제출한 계정을 앞에 두고 누락된 계정은 기존 상대 순서대로 뒤에 둠. 중복·없는 id 는 400 → 전역 전체 목록 |
| GET/PUT/DELETE | `/api/accounts/override` | 임시 순서 하나(전체 목록 기준). PUT `{ids, until}`. 회사 업무 탭은 이 순서에서 회사 그룹만 따른다 |
| GET | `/api/accounts/usage` | 전체 목록의 지금 적용 순서. `rank` 는 그 순서의 번호, `pool` 로 회사 그룹 표시 |
| POST | `/api/accounts/{id}/cooldown/clear` | 한도 대기 해제 |

## 메모·캘린더(ADR 0008)

모두 사용자 토큰. 시각은 Asia/Seoul 벽시계 문자열이다: 시간 일정 `YYYY-MM-DDTHH:MM`(오프셋이 있으면 KST 로 환산), 종일 `YYYY-MM-DD`(**끝 날짜 포함**). 오류는 공통 형식 `{code, message}`; 검증 실패 422 `validation`.

### 메모
`MemoOut`: `{id, title, tags[], category_id, pinned, source(user|agent), rev, excerpt, created_at, updated_at, body}`. 목록에는 `body` 가 없다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/memos?q=&tag=&category=&pinned=&limit=&offset=` | `{memos}`. 고정 먼저, 최근 수정 순. `q` 는 제목·본문·태그 부분 일치. `tag` 는 여러 번 줄 수 있고 모두 가진 메모만(AND). `category` 는 카테고리 id 또는 `none`(미분류) |
| GET | `/api/memos/tags?category=` | `{tags: [{name, count}]}`. `category` 를 주면 그 카테고리 메모의 태그만 |
| POST | `/api/memos` | `{title?, body?, tags?, category_id?, pinned?}` → 201. 태그는 앞의 `#`·중복을 정리, 최대 20개·30자 |
| GET | `/api/memos/{id}` | 본문 포함 |
| PATCH | `/api/memos/{id}` | `{title?, body?, tags?, category_id?, pinned?, rev?}`. `category_id: null` 은 미분류로, 빼면 그대로. `rev` 가 현재와 다르면 409 `conflict`(자동 저장 충돌). 성공하면 `rev` +1 |
| DELETE | `/api/memos/{id}` | 204. 완전 삭제 |
| GET | `/api/memo-categories` | `{categories: [{id, name, color, position, count}], total, uncategorized}`. 순서대로 |
| POST | `/api/memo-categories` | `{name, color?}` → 201. 이름 1~30자, 대소문자 무시 중복은 409 `duplicate`. `color` 는 `blue·green·amber·violet·red·gray`(토큰 `--tk-color-chart-categorical-1..6`) |
| PATCH | `/api/memo-categories/{id}` | `{name?, color?}`. `color: null` 은 색 없음 |
| PUT | `/api/memo-categories/order` | `{ids}`. 그 순서로 다시 매긴다 |
| DELETE | `/api/memo-categories/{id}` | 204. 속한 메모는 미분류로 옮기고 `rev` +1(다른 기기 편집기가 지운 카테고리로 되돌려 쓰지 않게) |

### 일정
`EventOut`: `{id, title, description, location, all_day, start, end, rrule, recurring, reminder, remind_at, reminder_sent, source, created_at, updated_at}`. 목록의 `start`/`end` 는 그 **발생분**의 구간이고, `GET /{id}` 는 시리즈 자체의 구간이다.
반복은 보류했고 재설계 예정이다(ADR 0008). 입력은 받지 않고, 보류 전에 저장된 `rrule` 만 읽어 펼친다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/calendar/events?from=YYYY-MM-DD&to=YYYY-MM-DD&q=` | `{events}` 시작 순. 반복 일정은 구간 안 발생분으로 펼친다. 구간 최대 400일(넘으면 422) |
| POST | `/api/calendar/events` | `{title, start, end?, all_day?, description?, location?, reminder?}` → 201. `end` 생략: 종일은 하루, 시간은 1시간. 모르는 필드(`rrule` 포함)는 422 |
| GET | `/api/calendar/events/{id}` | 시리즈 |
| PATCH | `/api/calendar/events/{id}` | 보낸 필드만. `all_day` 를 바꾸면 `start`·`end` 도 같이(아니면 422). `reminder: null` 은 알림 끄기. 시각·알림이 바뀌면 알림을 다시 예약한다 |
| DELETE | `/api/calendar/events/{id}` | 204. 완전 삭제 |
| GET | `/api/calendar/holidays?from=&to=` | `{holidays: [{date, name, substitute}], coverage: {from_year, to_year}}`. 한국 공휴일(대체공휴일은 `substitute: true`). 2000~2050년, 구간 최대 400일 |

`reminder`(기본 `null` = 꺼짐): 시간 일정은 `at_start|10m|30m|1h|1d`(시작 기준 전), 종일 일정은 `day_9am|prev_day_9am`(당일·전날 09:00 KST). 종류에 맞지 않거나 반복 일정이면 422. `remind_at` 은 보낼 시각(KST), `reminder_sent` 는 보냈거나 늦어서 건너뛰었는지다. 발송은 푸시 `PushKind.calendar_reminder`(감사 로그 `push.calendar_reminder`), `url` 은 `/calendar?event=<id>&date=YYYY-MM-DD`.

### 에이전트용(MCP 서버가 부름, 세션 한정 토큰)
`GET /api/agent/memo-categories`, `GET|POST /api/agent/memos`, `GET|PATCH|DELETE /api/agent/memos/{id}`, `GET|POST /api/agent/events`, `PATCH|DELETE /api/agent/events/{id}`.
- `GET /api/agent/events?from=&to=&q=` → `{events, holidays}`.
- 에이전트는 카테고리를 이름으로 쓴다: 목록 `category=<이름 또는 none>`, 만들기·고치기 `category`(빈 문자열이면 미분류), 응답에 `category` 이름. 없는 이름은 만들지 않고 422 로 있는 이름을 알려 준다.
- `PATCH /api/agent/memos/{id}` 는 `append` 도 받는다. 사용자 메모의 `title`·`body` 를 바꾸려 하면 403 `user_owned`.
- `DELETE` 는 보관 처리 `{archived: true}`. 에이전트가 만든 항목만 되고, 사용자가 만든 것은 403 `user_owned`.
- MCP 도구: `list_memo_categories` `list_memos`(tags·category) `search_memos`(tags·category) `get_memo` `create_memo` `update_memo` `delete_memo` `list_events` `create_event` `update_event` `delete_event`.

## 폰 → 컴퓨터 파일 보내기

목적지는 기본 워크스페이스의 실행 위치와 권한 폴더 안(`~/.ttakkari` 제외). 사진은 서버에서 손질한다: HEIC → JPG(`convert_heic`, 기본 켬), EXIF 방향대로 회전, 위치 정보 삭제(`strip_location`, 기본 켬), `image.jpg` 같은 뻔한 이름은 촬영 시각 `IMG_YYYYMMDD_HHMMSS`, 같은 이름은 `이름 (2)`.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/transfers/folders?path=` | path 없으면 권한 폴더 목록과 기본 폴더, 있으면 그 아래 폴더 |
| POST | `/api/transfers/folders` | `{path}` 폴더 만들기 |
| POST | `/api/transfers` | `{filename, size, dest_dir, convert_heic?, strip_location?, keep_original?}` → `{id, received, size, chunk_size}`. 최대 20GB |
| PUT | `/api/transfers/{id}?offset=N` | 본문 = 조각 바이트. offset 이 받은 크기와 다르면 409 `offset_mismatch`(`message: received=N`) |
| GET | `/api/transfers/{id}` | 받은 크기(이어 올리기용) |
| POST | `/api/transfers/{id}/complete` | → `{files: [{path, name, size, artifact_id}], notes}`. 덜 받았으면 409 |
| DELETE | `/api/transfers/{id}` | 취소 |
| POST | `/api/transfers/simple` | multipart `files`(여러 개), `dest_dir?`(기본 `TTAKKARI_TRANSFER_INBOX`). 사용자 토큰 또는 업로드 전용 토큰(`ttk_up_…`) |
| GET/POST/DELETE | `/api/transfer-tokens[/{id}]` | 업로드 전용 토큰. 원문은 만들 때 한 번만 |

## 맥락 도서관(ADR 0009)

- `GET /api/assistant/library?scope=all|personal|work`: 활성 출처의 종류별 수와 프로젝트 이정표. 사용자 인증 필요.
- `POST /api/assistant/recall`: `{query, k?, scope?}`. 임베딩 모델 없는 전문·부분 일치 검색과 명시적 별칭·관계 확장.
- `GET /api/agent/library`: 에이전트 세션 범위의 목차. MCP `library_catalog`에 연결한다.
- 기존 `POST /api/agent/recall`은 회사 세션에서 work/shared 자료만 반환한다. 개인 세션은 전체 자료를 조회한다.
- 기억 생성은 `scope`(personal/work/shared), `source_ref`를 받는다. 엔터티 생성은 `scope`, `attributes`를 받는다.
- 출처 생성은 `scope`, `category`(projects/work-history/knowledge/health), `entity_id`를 받는다. 연결 엔터티는 같은 범위여야 한다.
- `/api/assistant/status`는 `embedder: "none"`, `retrieval: "lexical"`, `model: null`, `ready: true`를 반환한다.
- 기존 필드는 유지한다. 검색 점수는 확률이 아니며, 문서 결과에는 원문 경로·제목·절·프로젝트·범위·색인 시각을 포함한다.

