# API 계약 (v0.1)

프론트(React PWA)와 백엔드가 공유하는 계약이다. 타입 원본은 `src/ttakkari/schemas.py`. 여기 바뀌면 프론트 `src/api/types.ts` 도 같이 바꾼다.

## 공통

- Base URL: 개발 `http://127.0.0.1:8787`, 운영은 HTTPS 터널 도메인.
- 인증: owner API는 `Authorization: Bearer <access_token>`. 예외는 `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`, `/api/health`, `/api/dl/{token}` 및 별도 capability를 쓰는 `/api/public/shares/{share_id}` 계열이다.
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
| POST | `/api/sessions` | `{workspace_id, title?, engine?, model?, effort?, kind?, pool?}` → 201. `pool`: `personal`(기본, 모든 계정을 개인 모드 순서·켜기로) \| `work`(회사에 들어온 계정만 회사 모드 순서·켜기로). 작업 세션은 오케스트레이터 값을 물려받는다 |
| GET/PATCH | `/api/sessions/{id}` | PATCH: `{title?, model?, effort?, archived?}` |
| GET | `/api/sessions/{id}/runs` | `RunOut[]` (오래된 순 = 대화 순서) |
| POST | `/api/sessions/{id}/runs` | `{prompt, context_artifact_ids?, model?, effort?}` → 202 `RunOut`. 실행 중 Run 이 있으면 `queued` 로 세션 대기열에 들어가 앞 Run 이 끝나면 순서대로 시작한다(여러 기기가 동시에 보내도 한 번에 하나) |
| GET | `/api/runs/failed?limit=30&since=&before=&before_id=` | 실패한 모든 세션의 `Run`만 최신순으로 반환. `{items: FailedRunOut[], next_before, next_before_id}`. `limit` 1~100, `since` 는 하한 시각, 다음 페이지는 `before`와 `before_id`를 함께 전달. `FailedRunOut`은 `RunOut`에 `failed_at, session_title, session_kind, session_parent_id, pool, workspace_name`을 더한다 |
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
| POST | `/api/artifacts/resolve` | `{path, workspace_id?, session_id?}` → `ArtifactOut`. 메시지 본문 링크의 경로 문자열(절대경로, `~/`, `file://`, 상대경로, 끝의 `:줄`·`#L줄` 허용)을 결과물로 바꿔 뷰어로 열게 한다. 이미 등록된 경로(outbox·업로드·에이전트가 고친 파일)면 그 결과물, 아니면 워크스페이스 루트나 outbox 안의 파일만 `area=agent` 로 새로 등록한다. 상대경로는 `workspace_id`(또는 `session_id` 의 워크스페이스) 기준이다. 허용 범위 밖은 403 `outside_root`, 민감 파일 403 `sensitive`, `..` 400 `bad_path`, 디렉터리 400 `not_a_file`, 없으면 404 |
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

푸시는 필요한 순간에만 간다. Run의 정상 완료·취소·서버 재시작 중단에는 자동 알림이 없다.
- Run 이 오류로 끝났을 때(`failed`): `작업 실패`
- 에이전트가 MCP `notify_user`(`POST /api/agent/notify`)로 직접 보낼 때: 에이전트가 정한 title·body. 입력이나 승인이 필요해 멈춘 경우도 에이전트가 이 도구로 알린다(서버가 Run 을 멈춰 세우는 승인 상태는 없다)
- 사용량 한도 대기, 사용자가 켠 일정 알림, 체크리스트 리마인더, 회의록 분석 완료·실패, 배포 결과, 설정의 테스트 알림

발송 진입점은 `push.service.notify(PushKind, title, body, url=, tag=, target_id=)` 하나다. 새 기능이 알림을 붙일 때는 `PushKind` 에 항목을 더하고 이 함수를 한 줄 부른다. 구독 관리와 발송 인프라는 그대로다.

payload 는 `{title, body, url, tag}`:
- 일정 알림(`PushKind.calendar_reminder`)의 payload 는 아래 "일정" 절을 본다
- `title`: 자동 알림은 `작업 실패`
- `body`: 세션 제목. 80자 초과면 자른다. 푸시 서버를 거치므로 경로·에러 원문은 넣지 않는다
- `url`: `/chat/<session_id>`, `tag`: session_id (같은 세션 알림은 하나로 합쳐진다)

푸시 서비스가 404/410 을 돌려주면 그 구독을 지운다. 다른 오류는 로그만 남긴다. 발송 결과는 감사 로그 `push.run_finished` 에 집계(`sent|removed|failed`)로 남는다.

## 알림함

`notify()`는 푸시 전송 전에 `notifications`에 제목·본문·앱 내부 경로를 저장한다. 푸시 구독이 없어도, 전송이 실패해도 보관된다. 기존 감사 로그에는 본문이 없으므로 과거 알림은 복원하지 않으며 0031 적용 후 발생한 알림부터 보관한다. 단일 사용자 앱으로 읽음 상태는 모든 기기가 공유한다. 모든 API는 사용자 인증이 필요하다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/notifications` | 선택 `category`, `unread_only=false`, `before`(양수 ID), `limit=30`(1~100). ID 내림차순 `{items, next_before, unread_counts}` |
| PATCH | `/api/notifications/{id}/read` | 최초 `read_at` 저장. 재요청도 같은 시각 유지. 갱신한 알림 반환, 없으면 404 |
| POST | `/api/notifications/read-all` | `{through_id, category?}`. 해당 ID 이하·선택 유형의 미확인 알림만 읽음 처리. `{updated}`. 조회 이후 도착한 알림은 건드리지 않음 |

알림 항목: `{id, kind, category, title, body, url, created_at, read_at}`. `read_at=null`은 안 읽음이다. `unread_counts`는 필터·페이지와 관계없는 유형별 미확인 개수 및 합계 `all`을 반환한다. `next_before=null`이면 마지막 페이지다.

유형: `tasks`(작업 실패·사용량 대기), `assistant`(따까리가 보낸 소식), `planner`(일정·체크리스트), `meetings`(회의록), `system`(배포·테스트). 탭에는 전체·작업·따까리·일정·할 일·회의록·시스템으로 표시한다. 푸시의 `tag` 중복 합치기는 기기에서만 적용되며 알림함은 각각의 발생 기록을 남긴다. 외부 주소나 제어 문자가 포함된 목적지는 `/notifications`로 대체한다.

## 오케스트레이터(ADR 0006)

- `POST /api/sessions` 에 `kind`(`chat` 기본 | `orchestrator` | `desk`). 오케스트레이터·접수는 엔진이 `auto` 로 고정된다.
- `GET /api/sessions` 는 하위 세션(기획·작업)을 빼고, `?parent_id=<id>` 면 그 하위 세션만(접수 → 기획 세션, 기획·오케스트레이터 → 작업 세션).
- `SessionOut` 에 `kind`, `parent_id`. `RunOut` 에 `origin`(`user` | `report` | `orchestrator` | `desk`), `profile`(실제로 쓴 엔진 프로필).

## 접수 → 기획 → 작업(ADR 0013)

- 접수(`kind=desk`) 세션에 `POST /api/sessions/{id}/runs` 하면 대기열 없이 바로 돈다. 결과는 같은 Run 의 `result_text`·`message` 이벤트.
- Run 이벤트: `desk.route {action: answer|delegate, account, engine, ms, fallback?}`, `desk.retry {account, engine, ms, reason}`(한도면 `usage_limit`), `desk.delegated {lead_id, title}`.
- 넘기면 접수 아래 기획 세션(`kind=orchestrator`, `parent_id`=접수)에 `origin=desk` Run 이 생긴다. 기획 Run 이 끝날 때마다 접수 세션에 보고 Run(`origin=report`, `prompt` 가 `[기획 보고] 기획 세션 '<제목>' (lead_id=…) 완료|실패|취소|중단.`, `result_text`=기획의 마지막 답, 바로 succeeded)이 붙는다.
- `GET /api/system/info` 의 `max_concurrent_runs` → `max_concurrent: {desk, leads, workers}`.
- 접수 판정(실제 메시지로 접수 정확도 재기):
  - `POST /api/runs/{id}/desk-feedback` `{verdict: ok|should_delegate|should_answer, note?(≤2000), redo?(false)}` → `DeskFeedbackOut`. 접수 세션(`kind=desk`)의 `origin=user` Run 만(아니면 422, 없으면 404). 끝나지 않았거나 `desk.route` 가 없으면 409. 접수가 이미 한 쪽을 가리키는 판정(answer 했는데 `should_answer`)도 409. 같은 Run 에 다시 보내면 덮어쓴다(`note` 포함, 안 주면 지움).
  - `redo: true` 이고 `should_delegate`·접수가 answer 했던 Run 이면 그 메시지를 새 기획 세션에 넘기고(`origin=desk` Run) 그 Run 에 `desk.delegated {lead_id, title, redo: true}` 를 붙인다. 응답 `lead_id`. 이미 넘긴 Run 이면 다시 넘기지 않고 그 `lead_id`.
  - `DeskFeedbackOut`: `{run_id, session_id, message, verdict, decided, expected, note, created_at, updated_at, lead_id}`. `decided` 는 판정 시점 `desk.route.action`, `expected` 는 사용자가 맞다고 한 쪽.
  - `RunOut.desk_feedback`: `{verdict, decided, expected, note, updated_at} | null`. `GET /api/sessions/{id}/runs`·`GET /api/runs/{id}` 에서만 채운다.
  - `GET /api/desk/feedback?limit=50` 최근 판정(수정순). `GET /api/desk/eval-set?format=json|jsonl&limit=` → `[{message, expected}]`(최근순).
  - 재생 평가: `uv run ttakkari desk-eval [--limit N] [--engine codex|claude]`. eval-set 을 지금 접수 지침·모델로 다시 분류해 정확도·오답·지연 중앙값을 출력한다. 계정 사용량을 쓴다.
  - 고정 벤치 API/MCP: 개인 오케스트레이터만 `start_luna_common30_v2()`로 시작하고 `luna_common30_v2_status(job_id)`로 체크포인트·raw 행을 읽는다. 요청 본문·모델·프롬프트·경로는 받지 않는다. 고정 `common30-v2.jsonl` 30개 × 3회, 개인 Codex 프로필의 `gpt-6-luna/high`만 사용한다. `luna_common30_v2_cancel(job_id)`는 취소하고 부분 결과를 저장한다. 엔드포인트는 `POST /api/orchestrator/luna-common30-v2`, `GET|DELETE /api/orchestrator/luna-common30-v2/{job_id}`. 응답은 실제 모델 호출 완료 상태를 포함하며 result rows는 raw output을 담는다.
- 비서 지침: `GET|PUT|DELETE /api/assistant/instructions?role=common|desk|lead|worker`(기본 `common`) → `{text, role, is_default}`. DELETE 는 기본값으로 되돌린다(기본값은 접수만 있다).
- Run 이벤트(사용량 한도, ADR 0006 "한도 전환·대기"):
  - `account.limited` `{account, engine, reset_at|null, detail}` 한도 감지. 그 계정은 `reset_at`(모르면 `profile_cooldown_seconds` 뒤)까지 쉰다.
  - `engine.fallback` `{from, to, reason: usage_limit|missing, detail, reset_at, mode: resume|handoff}` 같은 Run 이 다음 계정으로 넘어감.
  - `run.waiting` `{until, accounts:[{name, engine, resets_at}], handoff}` 쓸 수 있는 계정이 없다. Run 은 `queued` 로 남고 `until`(가장 이른 초기화)에 같은 Run 이 이어진다. `handoff` 는 재개용 내부 인계문이다.
  - `run.resumed` `{account}` 대기 뒤 이어서 시작. 이어 받는 Run 은 `run.status`(`running`) 도 다시 남긴다.
  - 대기를 시작할 때 푸시(`push.run_waiting`) 한 번. 대기 중 취소는 일반 대기 Run 취소와 같다.

오케스트레이터 MCP 서버 전용(토큰 `aud=ttakkari-orch`, 세션 한정. 사용자 토큰은 401):

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/orchestrator/workers` | `{title, prompt, engine?(auto|claude|codex|antigravity|qwen-local), task_type?, model?, effort?, escalation_reason?}` → 201 WorkerOut. auto 작업자는 `general/routine/design/verification/reasoning` 별 코드 정책으로 Antigravity와 native를 선택한다(기본 general). 명시 `qwen-local`은 readiness 통과 때만 쓸 수 있으며 auto 후보가 아니다. reasoning 및 peer 모델은 `escalation_reason` 없으면 400. 동시 실행 20개 초과 429 |
| POST | `/api/orchestrator/workers/{id}/messages` | `{prompt, fresh_context?(false)}` → 202. `fresh_context` 면 엔진 대화를 새로 시작(실행 중이면 그 Run 이 끝난 뒤) |
| GET | `/api/orchestrator/workers` | 목록(짧은 결과). WorkerOut 에 `model, effort, context`(예: `72% (144k/200k) · 교체 기준 이상…`, 잰 값 없으면 `알 수 없음…`) |
| GET | `/api/orchestrator/context` | 메인 자신의 컨텍스트 `{context, context_tokens?, context_window?, context_percent?}` |
| POST | `/api/orchestrator/context/reset` | 메인 자신의 대화를 새로 시작. `{applies: "after this turn" \| "next turn"}` |
| GET | `/api/orchestrator/workers/{id}` | 상세(결과 4000자·오류·결과물 이름) |
| POST | `/api/orchestrator/workers/{id}/stop` | 대기·실행 Run 취소 |
| GET | `/api/orchestrator/accounts/usage` | 계정별 배정 판단 정보(MCP `account_usage`). `{measured_at, priority_override_until, note, accounts:[{name, engine, scope, enabled, priority, available, max_used_percent, remaining_to_limit_percent, limit:{waiting, until, reason(cooldown/usage_window/usage_cap)}, running_runs, models, orchestrator_model, usage:{source, plan, measured_at, age_seconds, stale, min_remaining_percent, five_hour, weekly, model_weekly, error} 또는 null}]}`. 상한 여유는 신선한 5시간·주간 창의 최솟값이며 stale/null 이면 null. 시각은 KST 오프셋 ISO, 윈도우는 `{used_percent, remaining_percent, resets_at, reset_since_measured, resets_in_seconds, burn_percent_per_hour, exhausts_in_seconds, exhausts_before_reset}`. 캐시된 값만 쓰고 새로 조회·인증 파일 읽기·폴더/이메일/토큰 노출이 없다. 회사 업무 오케스트레이터는 회사에 들어온 계정만, 회사 모드 순서·켜기 기준(`enabled`·`priority`·`available` 이 그 모드 값) |

`WorkerOut`: `{worker_id, title, engine, status(queued|running|succeeded|failed|cancelled|interrupted|idle), active_run_id, last_result, last_error, artifacts}`.

### 컨텍스트 수명 관리 (2026-10-17)

- `GET /api/orchestrator/context`와 `WorkerOut.context_usage`는 마지막 완료 Run의 `{context_tokens, context_window, context_percent, measured_at, measured_run_id, measured_engine, measured_model, measurement_status: fresh|stale}`를 제공한다. 미측정 작업자는 `context_usage=null`이며 더 오래된 낮은 값을 찾아 여유로 간주하지 않는다. `context` 문자열도 유지한다.
- 측정은 해당 Run의 엔진·모델에 한정된다. 24시간을 넘거나 완료 시각이 없으면 `stale`이다. 실시간 값·계정 quota와 다르다. 오래된 높은 값도 대화가 비워졌다는 증거가 아니므로 70% 교대 대상이다. reset 이후는 **실행 시작 시각**으로 구분해 미리 대기 중이던 Run도 반영한다.
- Qwen·CLI 공통 접수 작업판에 후보별 사용률·시각·엔진/모델·fresh/stale·교대 정책을 넣는다. 50% 이상은 연속성·새 단계·기록·재로딩 비용 검토, 새 단계는 낮은 사용률에서도 명시 교대를 고려한다. task/step key를 추론해 저장하는 기능은 없다.
- 직렬 실행 소유권을 얻은 기획/작업 Run은 엔진 시작 전에 임계치를 검사한다. `context.routing` 이벤트와 `Run.usage.context_routing`에 `{action: unknown|reuse|review|fresh, measurement, reason, reset_at?}`를 저장한다. 기본 50% 이상은 검토 지시, 70% 이상은 엔진 대화 ID를 비운다. unknown은 0%가 아니다. 사용자 직접 요청·접수·작업 보고·작업자 메시지·미리 대기 중인 요청 모두 같은 경계를 거친다. 일반 채팅·접수 자체는 자동 교대 대상이 아니다.
- 자동 교대는 `Run.usage.context_handoff`에 `{source_run_ids, original_request, created_at, text}`를 reset과 같은 트랜잭션으로 저장한다. 최초 목표, 최근 완료 4턴의 요청·결과·오류, 체크포인트·outbox·기존 작업자 포인터를 원문으로 전달한다. 긴 필드는 중간 절단 대신 전체 생략과 원본 Run 포인터를 남긴다(본문 필드 예산 16,000자). 원문 발췌는 의미 요약·전체 도구 부작용 목록·검증 완료 보증이 아니다. 기존 기록·현재 코드·작업자를 확인하고 이미 한 일을 반복하지 않는다.
- 같은 Run의 한도 전환·복구에서는 저장한 인계를 사용하며 재교대하지 않는다. 활성 Run 취소·작업 복제·lease 변경은 없다. 긴 단일 Run 도중의 감시·자동 중단은 구현하지 않는다. 교대 뒤에도 화면의 세션·작업자 ID를 유지한다.
- 명시 `fresh_context`·`reset_my_context`는 기존 계약대로 사전 작성한 지시·기록을 사용한다. 자동 원문 인계는 임계치 교대에만 붙는다. 사람용 파일은 에이전트가 handoff 스킬로 작성하며 자동 파일 생성을 보장하지 않는다.

## 엔진 계정(ADR 0006)

계정은 모두 개인 목록에 있고, 계정 추가·삭제도 개인 목록에서만 한다. 그중 회사 모드에도 쓸 계정을 고르면 `pool=work` 가 된다(회사에 들어온 계정). **개인 모드와 회사 모드는 켜기·기본 순서·임시 순서를 각자 따로 가진다.** 같은 계정을 개인 모드에선 끄고 회사 모드에선 켤 수 있다. 개인 대화·워커(`sessions.pool=personal`)는 모든 계정을 개인 순서(`priority`)·켜기(`enabled`)로 고르고 한도 폴백도 그 순서다. 회사 업무 대화(`sessions.pool=work`)는 회사에 들어온 계정만 회사 순서(`work_priority`)·켜기(`work_enabled`)로 고르고 폴백한다. 모드별 값은 `?pool=personal|work`(기본 personal)로 가른다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/accounts` | 전체 계정 목록, 개인 우선순위 순. `{id, name, engine, pool, config_dir, enabled, priority, max_used_percent, work_enabled, work_priority, login(ok/unknown/missing), login_label, cooldown_seconds}`. `pool`: `work` = 회사 모드에도 들어옴, `personal` = 개인 모드만. `enabled`·`priority` 는 개인 모드 값, `work_enabled`·`work_priority` 는 회사 모드 값(`pool=work` 인 계정에만 의미, 회사 목록은 `pool=work` 를 `work_priority` 순으로 본다) |
| POST | `/api/accounts` | `{name, engine, pool?, config_dir?, enabled?, max_used_percent?}` → 201(개인 목록 맨 뒤 순위, `pool=work` 면 회사 순서 맨 뒤에도 `enabled` 값으로 켠 채로). 상한 생략 시 기본 100, `내 클로드`·`Daddy codex` 이름의 보호 계정은 95. 이름 중복 409, 상대 경로·`~/.ttakkari` 안 422 |
| PATCH | `/api/accounts/{id}` | `{name?, pool?, config_dir?, enabled?, work_enabled?, max_used_percent?}`. 상한은 1~100. `enabled`=개인 모드 켜기, `work_enabled`=회사 모드 켜기(회사에 들어오지 않은 계정이면 400). `pool=work` 로 회사 모드에 넣으면 회사 순서 맨 뒤에 `work_enabled=true` 로 들어가고, `pool=personal` 로 빼도 개인 모드 순서·켜기는 그대로다. 다시 켜면(두 모드 어느 쪽이든) 한도 대기도 풀린다 |
| DELETE | `/api/accounts/{id}` | 204. 개인·회사 모드 모두에서 사라진다 |
| POST | `/api/accounts/reorder?pool=` | `{ids}` 제출한 계정을 앞에 두고 누락된 계정은 기존 상대 순서대로 뒤에 둠. 중복·그 모드에 없는 id 는 400. `pool=personal`(기본)은 모든 계정을 대상으로 `priority`, `pool=work` 는 회사에 들어온 계정만 대상으로 `work_priority` 를 바꾼다. 응답은 그 모드 목록 |
| GET/PUT/DELETE | `/api/accounts/override?pool=` | 임시 순서. 개인·회사 모드마다 따로 하나씩. PUT `{ids, until}`(`pool=work` 는 회사에 들어온 계정만 허용). 끝나면 그 모드만 기본 순서로 돌아간다 |
| GET | `/api/accounts/usage` | 개인 모드의 지금 적용 순서(임시 순서 반영). `max_used_percent`, 신선할 때만 `remaining_to_limit_percent`, `stale` 포함. `rank`·`enabled` 는 개인 모드, `work_rank`(회사 모드 순서 번호, 회사에 안 들어온 계정은 null)·`work_enabled` 는 회사 모드 값, `pool` 로 회사 표시 |
| GET | `/api/accounts/qwen-usage` | 로컬 Qwen 접수의 실제 llama.cpp HTTP 호출 토큰. `{model, timezone: Asia/Seoul, collection_started_at, server, today, week}`. 기간별 입력·출력·합계 토큰, 전체/계측/usage 누락/실패 호출 수, 마지막 사용 시각을 반환한다. 오늘은 KST 00:00부터, 주간은 월요일 KST 00:00부터 현재까지. 평가는 제외하며 서버 응답 `usage.prompt_tokens`, `completion_tokens`, `total_tokens`가 모두 있고 합이 맞는 호출만 계측한다. 누락·실패 토큰은 null로 보존하며 프롬프트·응답 본문은 저장하지 않는다. |
| GET | `/api/accounts/qwen-flash-usage` | 별도 로컬 Qwen3.8-Flash-Next 서버의 계측 및 일일/주간 영속 집계. `{model, timezone: Asia/Seoul, observed_at, collection_started_at, server: {status, detail}, metrics_status, input_tokens, cached_input_tokens, output_tokens, requests_processing, requests_deferred, today, week}`. `/props` 모델 alias를 확인한 뒤 `/metrics`만 GET한다. 입력은 캐시 제외 처리 토큰, 캐시 재사용은 별도 값. 관측 증가분(delta)을 DB(`qwen_flash_usage`)에 영속 기록하여 서버/백엔드 재시작에도 일간(KST 00:00~) 및 주간(월요일 KST 00:00~) 토큰 집계가 유지된다. 접수용 일간·주간 집계와 합산하지 않는다. 중지/기동/모델 불일치/계측 누락은 상태와 null로 반환하며 0으로 꾸미지 않는다. 인증 필요, Cache-Control: no-store. |
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
- `GET /api/agent/memos/tags?category=<이름 또는 none>` → `{tags:[{name, count}]}` (MCP `list_memo_tags`). `/memos/{id}` 보다 먼저 등록해야 한다.
- 보관함·서버(모든 Run, MCP `list_artifacts` `set_artifact_area` `server_resources`): `GET /api/agent/artifacts?area=files|agent`(항목에 `area`), `PATCH /api/agent/artifacts/{id}` `{area}` 로 분류 이동(되돌릴 수 있음, 감사 로그 `artifact.move` actor=agent, 사용자가 올리거나 등록한 파일 `upload`·`user_registered` 는 403 `user_owned`), `GET /api/agent/system/resources` 는 `GET /api/system/resources` 와 같은 값(프로세스 이름만).
- 에이전트는 카테고리를 이름으로 쓴다: 목록 `category=<이름 또는 none>`, 만들기·고치기 `category`(빈 문자열이면 미분류), 응답에 `category` 이름. 없는 이름은 만들지 않고 422 로 있는 이름을 알려 준다.
- `PATCH /api/agent/memos/{id}` 는 `append` 도 받는다. 사용자 메모의 `title`·`body` 를 바꾸려 하면 403 `user_owned`.
- `DELETE` 는 보관 처리 `{archived: true}`. 에이전트가 만든 항목만 되고, 사용자가 만든 것은 403 `user_owned`.
- MCP 도구: `list_memo_categories` `list_memos`(tags·category) `search_memos`(tags·category) `get_memo` `create_memo` `update_memo` `delete_memo` `list_events` `create_event` `update_event` `delete_event`.

### 서버 GPU 리소스

`GET /api/system/resources`(사용자 인증), `GET /api/agent/system/resources`(세션 인증)는 기존 CPU·메모리·디스크·네트워크에 `gpu` 객체를 추가한다. 2초 캐시를 공유한다.

- `status`: `available|unavailable|unsupported`; `reason`: `null|collection_failed|statistics_missing|unsupported_os`.
- `model`, `cores`: GPU 모델명과 코어 수, 알 수 없으면 `null`.
- `percent`: GPU **전체** 사용률(0~100), 읽지 못했으면 `null`. 정상 유휴 값 `0`과 구분한다. GPU 코어별 사용률은 제공하지 않는다.
- `memory_used_bytes`, `memory_allocated_bytes`: 드라이버가 보고하는 사용·할당 시스템 메모리 바이트, 누락·잘못된 값은 `null`. Apple Silicon 통합 메모리의 일부로, 전체 메모리에 더하거나 할당량을 VRAM 용량으로 해석하지 않는다.
- macOS의 `ioreg -r -d 1 -c IOAccelerator -l -w 0`을 고정 인자로 최대 2초 실행하고, 첫 유효 GPU 통계를 사용한다. 다른 어댑터끼리 값을 합치지 않는다. GPU 수집 실패가 나머지 리소스 응답을 막지 않는다.
- 프론트는 4초마다 갱신한다. `gpu`가 없는 이전 서버 응답도 허용하고 GPU 안내를 표시한다. `unavailable|unsupported`는 막대 없이 `—`와 설명을 표시한다.

### 서버 리소스 GPU 기록

`GET /api/system/resources/history?range=1h|24h|7d` (사용자 인증) → `{range, from, to, bucket_seconds, points}`. 포인트에는 UTC `timestamp`, GPU 전체 사용률·사용 메모리, CPU 전체 사용률·사용 메모리, 표본/결측 개수가 들어간다. GPU 또는 전체 수집 실패 값은 `null`; 성공한 유휴 GPU는 `0`이다. `1h`는 30초 원본, `24h`는 1분 평균, `7d`는 10분 평균을 반환한다. 모든 평균에는 `sample_count`, GPU 결측에는 `failed_count`가 함께 온다.

백엔드는 화면 접속 여부와 무관하게 30초마다 기존 `sysinfo.snapshot()`을 재사용해 적재한다(`TTAKKARI_RESOURCE_SAMPLE_SECONDS`, 기본 30, 0은 테스트/개발에서 끔). 원본은 90일 뒤 지우며 30초 간격 연속 수집 기준 상한은 약 259,200행이다. 90일간 원본 행은 향후 직접 분석에 쓸 수 있고 7일 화면 범위는 쿼리 시 집계하므로 추가 롤업 테이블은 두지 않는다. 집계는 PostgreSQL의 epoch 기반 버킷 연산을 사용한다.

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
| POST | `/api/transfers/simple` | 스크립트용 multipart `files`(여러 개), `dest_dir?`(기본 `TTAKKARI_TRANSFER_INBOX`). 사용자 토큰 또는 업로드 전용 토큰(`ttk_up_…`) |
| POST | `/api/transfers/raw?filename=` | 단축어용 파일 바이트 본문(한 요청에 한 파일). 이름은 확장자 포함 권장·URL 인코딩. `Content-Type: application/octet-stream`, 사용자 토큰 또는 업로드 전용 토큰. 선택 쿼리: `dest_dir`, `convert_heic=true`, `strip_location=true`, `keep_original=false`. → 201 `RawCompleteOut`; 양식·JSON 본문 415 `wrong_body_type`, 크기 초과 413 `too_large` |
| GET/POST/DELETE | `/api/transfer-tokens[/{id}]` | 업로드 전용 토큰. 원문은 만들 때 한 번만 |

아이폰 공유 시트 설정은 [단축어 파일 전송](iphone-shortcut.md)을 따른다. `URL 콘텐츠 가져오기`의 **요청 본문 전체를 파일**로 선택하고 **반복 항목**을 파일 값으로 넣는다. 양식 안에 `files` 필드를 만들지 않는다. 여러 입력은 `각 항목 반복`으로 하나씩 전송한다. 서버는 기존 사진 손질·권한 폴더 검사·보관함 등록을 적용한다.

`RawCompleteOut`은 기존 `files`, `notes`와 진단 필드 `success`, `receivedBytes`, `filename`, `savedPath`, `requestId`, `authPassed`, `contentLength`를 반환한다. `receivedBytes`는 손질 전 수신 크기이며 `files[].size`는 저장된 파일 크기다. `filename`은 요청 이름, `savedPath`는 첫 결과 파일의 실제 경로(원본 동시 저장 시 전체 경로는 `files`)다. 실제 파일 존재·크기와 DB 커밋을 확인한 뒤에만 `success=true`다. HTTP 오류·검증 오류·예상하지 못한 실패에는 `success=false`와 가능한 진단 값을 넣는다. 아직 본문을 읽지 못한 경우 `receivedBytes=null`, 읽은 빈 파일은 `receivedBytes=0`으로 구분한다. 예상하지 못한 실패는 500 `upload_failed`이며 requestId로 추적한다. 다른 API의 오류 응답 형식은 바꾸지 않는다.

파일 본문 요청은 `uvicorn.error.transfer` 로그에 `request_received`, `auth_passed`, `body_received`, `file_saved`, `request_finished`를 같은 requestId와 UTC 시각으로 남긴다. 오류는 `request_failed`에 예외·실패 단계가 기록된다. Content-Length·읽은 바이트 수·요청 이름·저장 경로·상태코드를 기록하며 Authorization 값·토큰·파일 내용은 기록하지 않는다. 보관함 등록 실패는 실제 저장 파일을 유지하고 artifact_id=null을 반환하며 경고 로그를 남긴다.

## 맥락 도서관(ADR 0009)

- `GET /api/assistant/library?scope=all|personal|work`: 활성 출처의 종류별 수와 프로젝트 이정표. 사용자 인증 필요.
- `POST /api/assistant/recall`: `{query, k?, scope?}`. 임베딩 모델 없는 전문·부분 일치 검색과 명시적 별칭·관계 확장.
- 맥락 도서관 화면(`/context-library`)용 읽기 전용 집계. 모두 사용자 인증, `scope=all|personal|work`(personal/work 는 shared 포함):
  - `GET /api/assistant/library/overview`: 종류×범위별 출처·문서·조각 수, 기억·엔터티 분포, 마지막 색인 시각, 색인 오류 출처.
  - `GET /api/assistant/library/tree`: 종류 → 프로젝트·모음(엔터티, 없으면 "연결된 항목 없음") → 출처. 문서는 아래로 지연 로딩한다.
  - `GET /api/assistant/library/sources/{id}/documents`, `GET /api/assistant/library/documents/{id}/chunks?limit=`: 문서 목록(조각 많은 순 500개)과 조각 미리보기. 범위 밖이면 404.
  - `GET /api/assistant/library/graph`: 노드(엔터티 + 기억·출처·문서·조각 수 + 품질 플래그 isolated/no_alias/no_summary/no_evidence/duplicate_suspect), 엣지(술어), 중복 의심 쌍.
  - `GET /api/assistant/library/entities/{id}`: 노드 상세(관계·연결된 기억 전문·묶인 출처).
  - `GET /api/assistant/library/quality`: 조각 길이 분포·짧은/긴/중복 조각 예시, 문서·출처 무결성, 엔터티·관계·기억 지표와 판정 문장(`findings`).
  - `POST /api/assistant/library/search-lab`: 기존 `{query, k?, scope?}` 요청을 유지하며, 화면은 `{query, limit?, offset?, scope?}`로 페이지를 요청한다. 응답에 `total, limit, offset`과 해당 페이지의 결과를 담는다. recall 과 같은 검색 결과에 걸린 단어(`matched_terms`)·걸린 곳(`match_in`)·경로(`via`: keyword/entity/fuzzy)를 붙인다. 쓰기 없음.
  - `GET /api/assistant/library/items/{memory|chunk|entity}/{id}?scope=`: 항목 원문·출처·분류·생성/수정 시각과 연결된 엔터티·관계·기억을 읽는다. 범위 밖이면 404.
- `GET /api/agent/library`: 에이전트 세션 범위의 목차. MCP `library_catalog`에 연결한다.
- 기존 `POST /api/agent/recall`은 회사 세션에서 work/shared 자료만 반환한다. 개인 세션은 전체 자료를 조회한다.
- 기억 생성은 `scope`(personal/work/shared), `source_ref`를 받는다. 엔터티 생성은 `scope`, `attributes`를 받는다.
- 출처 생성은 `scope`, `category`(projects/work-history/knowledge/health), `entity_id`를 받는다. 연결 엔터티는 같은 범위여야 한다.
- `/api/assistant/status`는 `embedder: "none"`, `retrieval: "lexical"`, `model: null`, `ready: true`를 반환한다.
- 기존 필드는 유지한다. 검색 점수는 확률이 아니며, 문서 결과에는 원문 경로·제목·절·프로젝트·범위·색인 시각을 포함한다.

## 회의록(ADR 0010)

사용자 인증. 오류는 `{code, message}`. 상태: `uploaded → transcribing → transcribed → analyzing → review | analyzed`, 실패는 `failed` + `failed_stage`(transcription | analysis) + `error`.

- `GET /api/meetings/providers`: 전사 Provider 상태 `{default_provider: "local_whisper", providers: [{id, label, state, message, is_default}]}`. 로컬 엔진만 등록하며 경로·인증 정보는 내보내지 않는다(ADR 0027).
- `GET /api/meetings?scope=all|personal|work&q=&status=&project_id=&limit=&offset=`: 요약 목록(프로젝트·녹음·`counts`{discussion, decision, action, open_issue, pending_proposals}). 진행 중인 건은 조회 시점에 끝났는지 확인해 반영한다.
- `POST /api/meetings` `{title, scope?, project_id?, held_at?, participants?, audio_artifact_id?, transcript?, transcript_source?: paste|file, auto_process?: bool, provider_id?: string}`: `auto_process` 면 녹음은 선택 Provider로 전사(기본 로컬 Whisper, 끝나면 분석), 전사 텍스트는 곧바로 분석. 막히면 회의는 만들고 응답에 `auto_error{code, message}`. 녹음(`POST /api/uploads` 로 먼저 올린 Artifact)이나 전사 텍스트 중 하나는 필요. 전사 텍스트가 있으면 `transcribed`, 녹음만 있으면 `uploaded`. 프로젝트는 회의 범위와 맞아야 한다(`scope_mismatch` 400). 전사는 `meeting_transcript_max_chars`(12만 자) 초과 시 413 `too_large`.
- `GET /api/meetings/{id}`: 상세(`transcript`, `items[]`, `proposals[]`, `analysis_context[]`, 목록과 같은 `vector{status: none|pending|ready|failed, chunks, error, embedded_at}`). `DELETE` 는 승인해 만든 기억과 보관함 녹음을 남긴다.
- `PATCH /api/meetings/{id}`: `{title?, scope?: personal|work, project_id?, held_at?, participants?}`. 범위 변경은 녹음·전사·분석·항목·제안을 보존한다. 전사·분석 중 변경은 409 `meeting_busy`. 명시한 프로젝트는 변경 후 범위로 검증하고, 프로젝트를 생략하면 범위가 맞지 않는 기존 연결만 해제한다(공통 프로젝트 유지). 기존 공개 링크는 해제하고 이전 범위 검색 조각은 같은 트랜잭션에서 지운 뒤 재색인한다. 승인한 도서관 기억은 재분류하지 않으며, 옛 범위 기억·액션을 바꾸려는 제안 승인은 409 `stale`. `scope: null`·잘못된 값은 422.
- `POST /api/meetings/{id}/transcript` `{text, source?}`: 전사 텍스트 입력·교체(`uploaded|transcribed|failed`).
- `POST /api/meetings/{id}/transcribe` `{auto_analyze?: true, provider_id?: string}`: 선택 Provider 전사 작업을 건다(202, `transcribing`). `local_whisper`만 지원하며 기본 경로에서 런타임·모델을 찾는다(ADR 0027). 진행은 STT job API로 조회한다. 쓸 수 없으면 409 `provider_unavailable` + 사유, 상태는 그대로.
- `GET /api/meetings/{id}/stt`: 로컬 시도 목록 `{jobs}`. 상태, `candidate_only`, 상위 시도, 원본 음성 식별값·실제 모델/시간/메모리 metadata. 파일 경로·토큰은 없음.
- `GET /api/meetings/{id}/stt/{job}/segments?offset=0&limit=100`: `{job, segments, offset}`. 구간은 초 단위 절대 start/end·원문·별도 corrected_text·모델 실제 지표·단어 시각·음성 Artifact ID·원시 출력. 미실행 화자/중첩은 null. 최대 500개씩.
- `POST /api/meetings/{id}/stt/{job}/reprocess` `{start, end, terms?: string[]}`: 202, 새 후보 시도. 성공한 원본의 시간 범위 내·기본 최대 120초. 원전사·분석 자동 교체 없음. 잘못된 범위는 422, 잘못 연결한 작업은 404. 사용자 인증 전용이며 에이전트 쓰기 API는 추가하지 않음.
- `POST /api/meetings/{id}/analyze`(202), `POST /api/meetings/{id}/analyze/cancel`: 분석 Run(기존 Run·엔진 체계)을 시작·취소. 재분석은 항목과 대기 제안만 교체한다.
- `PATCH /api/meetings/{id}/items/{item_id}` `{status?: open|done|cancelled, text?, owner?, due?}`: 액션·미해결 쟁점의 상태.
- `POST /api/meetings/{id}/proposals/{pid}/approve|reject`: 제안 처리. 승인만 맥락 도서관(`add_memory`, `supersede_memory`)이나 이전 회의의 액션(`update_action_item`)을 바꾼다. 대상이 이미 바뀌었으면 409 `stale`.
- 에이전트용(세션 한정 토큰, 읽기 전용, 회사 세션은 회사 회의만): `GET /api/agent/meetings?q=&project=&status=&limit=`, `GET /api/agent/meetings/{id}`, `GET /api/agent/meetings/search?q=&kind=&status=&project=&limit=`, `GET /api/agent/meetings/semantic?q=&project=&kind=&limit=`(조각 의미 검색, `results[{meeting_id, meeting_title, held_at, scope, kind, heading, text, score, similarity}]`). MCP `list_meetings`·`get_meeting`·`search_meeting_items`·`search_meetings`. 쓰기·승인 경로는 없다. `recall` 결과에도 `type: meeting` 이 섞인다.

## 프로젝트 관리 PMS · WBS(ADR 0012)

에이전트의 PMS 전체 관리는 `POST /api/agent/pms`에 세션 Bearer 토큰을 사용한다. 요청은
`{"operation":"list_tasks","project_id":"UUID","item_id":null,"data":{}}` 형식이다.
`operation`은 프로젝트 `list_projects/create_project/get_project/update_project/delete_project`·`dashboard`,
작업 `list_tasks/create_task/update_task/move_task/delete_task`,
마일스톤 `list_milestones/create_milestone/update_milestone/delete_milestone`,
이슈 `list_notes/create_note/update_note/delete_note`를 지원한다.
프로젝트 소속 작업에는 `project_id`, 기존 작업·마일스톤·이슈 수정/이동/삭제에는 `item_id`를 쓴다.
`list_tasks`와 작업 쓰기(`create_task/update_task/move_task/delete_task`)의 응답이 사용자 API와 다르다. 큰 프로젝트에서 응답이 에이전트 도구 결과 한도를 넘지 않게
기본이 compact·페이지 응답이다. `data`(모두 선택): `parent_id`(직계 자식만, 다른 프로젝트의 작업이면 404),
`depth`(정확히 그 깊이, 0=최상위), `max_depth`(그 깊이 이하), `status`(집계된 상태), `q`(제목 부분 일치, 대소문자 무시),
`max_chars`(500~16000, 기본 6000. 넘으면 뒤쪽 작업을 덜고 `truncated`·`next_offset`으로 이어 읽게 한다), `limit`(기본 50, 최대 200. `compact=false`면 기본·최대 25, 넘으면 422 `limit_too_large`), `offset`(기본 0),
`compact`(기본 true). compact 항목은 `{id, wbs, title, status, progress, parent_id, child_count}`뿐이고,
`compact=false`는 사용자 API와 같은 전체 필드(description 포함)다. 필터는 전체 트리에서 번호·집계를 계산한 뒤 걸러서
번호와 상태가 필터에 따라 달라지지 않는다. 응답 `{tasks, compact, total, project_total, offset, limit, returned, has_more, next_offset}`:
`total`은 필터 후 개수, `next_offset`은 마지막 쪽이면 null이다. 이전에는 `data: {}`가 전체 트리를 돌려줬다.
작업 쓰기는 전체 트리(`tasks`) 대신 `{task, ancestors, project, hint}`만 돌려준다. `task`는 저장된 작업 1건(description 대신
`description_length`), `ancestors`는 위에서 아래 순서의 compact 조상 경로(집계된 상태·진행률, move는 떠난 경로 포함, delete는 지운 작업의 부모 경로),
`project`는 `{id, progress, counts, task_total}`이다. delete는 `task` 없이 `removed`·`deleted_id`가 붙는다. 사용자 REST 응답은 그대로다.
나머지 `data` 필드는 아래 사용자 API의 해당 요청 스키마와 같다. 생략은 보존, nullable 필드의 null은 해제이며 알 수 없는 필드는 422다.
회사 세션은 회사 프로젝트만 관리한다. MCP의 `manage_pms`가 이 경로를 호출하며 PMS 변경은 사용자 위임에 따라 추가 승인 없이 실행된다.
계획을 등록하는 것은 해당 계획의 실제 개발 승인이 아니다. 읽기와 쓰기의 도구 annotation은 구분한다.

사용자 인증. 오류는 `{code, message}`. 날짜는 `YYYY-MM-DD`(시간대 없음), 오늘 기준은 한국 시간. 칸반 카드와 WBS 줄은 같은 작업이다.

- `GET /api/pms/projects?scope=all|personal|work&status=planning|active|on_hold|done&q=`: 요약 목록. 항목마다 `progress`(0-100), `counts`{total, todo, doing, review, done, overdue, due_soon, no_dates}(말단 작업 기준), `next_milestone`, `open_notes`, `members`.
- `POST /api/pms/projects` `{name, description?, scope?, status?, start_date?, end_date?, members?, phases?: string[]}`: `phases` 는 처음부터 만들 최상위 단계 이름. 종료일이 시작일보다 빠르면 422 `bad_dates`.
- `GET|PATCH|DELETE /api/pms/projects/{id}`: 삭제는 작업·마일스톤·이슈까지 지운다(204).
- `GET /api/pms/projects/{id}/dashboard?today=`: `progress`, `planned_progress`(기간 대비 흘러간 %, 기간 없으면 null), `days_left`, `counts`, `overdue[]`·`due_soon[]`(7일), `by_assignee[]`(name "" = 미배정), `phases[]`, `milestones[]`, `notes`{open, issues, risks, high, top[]}.
- `GET /api/pms/projects/{id}/tasks`: 트리 순서로 편 `tasks[]`. 항목: `id, parent_id, depth, wbs("1.1.2"), has_children, title, description, priority(low|normal|high|urgent), assignee, status(todo|doing|review|done), progress, start_date, end_date, dates_rollup, overdue, predecessors[]`. 하위 작업이 있으면 `status`·`progress`·(자식에 날짜가 있을 때) 기간은 자식에서 집계한 값이다.
- `POST /api/pms/projects/{id}/tasks` `{title, parent_id?, after_id?, description?, priority?, assignee?, start_date?, end_date?, status?, progress?, predecessors?}`: `after_id` 를 주면 그 작업 바로 뒤 형제로 들어간다(parent_id 무시). 8단계 초과 422 `too_deep`. 응답 `{task, tasks}`.
- `PATCH /api/pms/tasks/{id}`: 위 필드 일부. `assignee`·날짜는 null 로 비운다. 하위 작업이 있는 작업의 status/progress/집계된 기간은 422 `rollup`. 선행 작업 오류는 422 `bad_predecessor`·`predecessor_cycle`. 응답 `{task, tasks}`.
- `POST /api/pms/tasks/{id}/move` `{parent_id: id|null, index?: int}`: 들여쓰기·내어쓰기·순서 변경 하나로. 자기 자손 아래로는 422 `bad_move`. 응답 `{task, tasks}`.
- `DELETE /api/pms/tasks/{id}`: 하위 작업까지 지우고 다른 작업의 선행 참조에서 뺀다. 응답 `{removed, tasks}`.
- `GET|POST /api/pms/projects/{id}/milestones`, `PATCH|DELETE /api/pms/milestones/{id}`: `{title, due_date, done}`. 응답에 `days`(오늘까지 남은 일수)·`overdue`.
- `GET|POST /api/pms/projects/{id}/notes?status=open|resolved|all&kind=`, `PATCH|DELETE /api/pms/notes/{id}`: 이슈·리스크·메모 `{kind: issue|risk|note, title, body, severity: low|medium|high, owner, task_id, status: open|resolved}`. 열린 것·심각한 것 순.


## 체크리스트

화면 `/checklists`는 데스크톱 부가기능·모바일 더보기에서 연다. 날짜는 `YYYY-MM-DD`, 기본 오늘은 한국시간(Asia/Seoul)이다. 체크리스트는 날짜별 항목과 매일 반복할 고정 원본을 따로 보관한다.

- `GET /api/checklists?day=YYYY-MM-DD` → `{day, items, completed, total}`. 항목은 `{id, day, title, checked, template_id}`. 오늘의 고정 항목을 중복 없이 생성한다. 과거 날짜에 현재 고정 원본을 소급 생성하지 않고, 미래 날짜의 고정 항목은 해당 날짜가 되었을 때 생성한다.
- `POST /api/checklists/items` → 201. `{title, day?}`로 일일 항목 추가.
- `PATCH /api/checklists/items/{id}` → `{title?, checked?}`. 체크/해제와 이름 변경은 보낸 필드만 적용한다. 명시적 null, 빈 이름, 알 수 없는 필드는 거절한다.
- `DELETE /api/checklists/items/{id}` → 204. 해당 날짜에서 소프트 삭제한다. 고정 원본과 다른 날짜는 유지하며, 같은 날짜를 다시 조회해도 복원하지 않는다.
- `GET /api/checklists/templates` → `{templates: [{id, title}]}`.
- `POST /api/checklists/templates` → 201. `{title}`로 고정 항목을 만들고 오늘부터 미체크 상태로 추가한다.
- `PATCH /api/checklists/templates/{id}` → `{title}`. 앞으로 생성하는 날짜의 원본을 수정한다. 이미 생성한 날짜별 이름·완료 기록은 유지한다.
- `DELETE /api/checklists/templates/{id}` → 204. 앞으로 반복을 중단하며 기존 날짜별 기록은 유지한다.

같은 API가 `/api/agent/checklists`에도 있으며 기존 MCP 세션 토큰과 세션 존재를 검사한다. MCP 8종은 `list_checklist`, `create_checklist_item`, `update_checklist_item`, `delete_checklist_item`, `list_checklist_templates`, `create_checklist_template`, `update_checklist_template`, `delete_checklist_template`이다. 조회와 쓰기의 readOnlyHint를 구분하고, 사용자가 위임한 체크리스트 변경 도구는 Codex에서 사전 허용한다. 변경은 사용자/에이전트 구분과 함께 감사 기록에 남긴다.

알림은 기존 일정 스캔 루프와 푸시 서비스를 사용한다. 매일 KST **07:30에는 항목이 없거나 모두 완료해도 하루 정리 알림**을 보낸다. **12:00·16:00·20:00·22:00에는 미완료 항목이 있을 때만** 보낸다. 날짜·시간대별 DB 고유 키로 재시작과 동시 스캔에서 최대 한 번만 발송 시도한다. 발송 전에 시간대를 차지하므로 차지 직후 프로세스 종료나 발송 실패 때는 같은 시간대를 재시도하지 않는다. 실패는 로그·기존 푸시 감사 집계로 남긴다. 서버 중단 뒤에는 가장 최근 시간대만 1시간 미만 늦게 따라잡으며 이전 시간대 알림을 몰아 보내지 않는다. 알림을 열면 해당 날짜의 체크리스트로 이동한다. 실제 기기 수신에는 기존 PWA 푸시 구독·권한이 필요하다.

## Antigravity CLI 엔진 (ADR 0029)

- `EngineName`, `SessionEngine`, worker 및 계정의 `engine` 값에 `antigravity`를 추가한다. 계정 추가는 사용자가 이미 로그인한 단일 `agy` 프로필을 등록하며 `config_dir`는 받지 않는다. 계정 `login` 표시는 `unknown`으로 유지하지만, 사용량은 별도 정상 `agy` 조회에서 받은 quota·크레딧 실측을 반환한다.
- 2026-10-17 안전 보강(이후 아래 관리 프로필로 대체): 자동 생성한 폴더만으로 같은 기본 인증을 다른 계정처럼 등록하던 경로를 막았다. 기존의 비어 있지 않은 Antigravity `config_dir`는 전용 HOME 프로필로 검증되지 않으면 실행·사용량 조회에서 닫히고, 기본 계정 또는 캐시 값으로 대체하지 않는다. [조사 및 후속 설계](antigravity/multi-account-investigation.md)를 따른다.
- **2026-10-17 Antigravity 추가 계정(전용 HOME):** 첫 Antigravity 계정은 호스트 로그인(`config_dir=null`)을 그대로 쓰며 동작이 바뀌지 않는다. 두 번째 이후 계정 생성은 201이고 `config_dir`에 `accounts/(계정 id)/antigravity-home`(0700, 계정 id 매니페스트)을 배정한다. 로그인·OAuth 토큰은 이 HOME 안의 전용 macOS 키체인에만 저장되고 그 잠금 비밀번호만 호스트 키체인에 보관한다. 따까리는 토큰을 읽거나 응답·로그에 싣지 않는다. 계정 응답에 `auth_state`(`setup_required`, `auth_required`, `auth_expired`, `isolation_unverified`, `ready`, `keychain_*`, `invalid_profile`, `unsafe_path`), `auth_error`, `isolation_mode`(`managed_home_keychain`), `execution_enabled`가 추가된다. `ready`가 아니면 `login=missing`, `execution_enabled=false`이고 계정 배정과 사용량 조회에서 제외된다. `login_command`는 전용 HOME으로 agy 를 띄우는 `python -m ttakkari.antigravity_accounts login (HOME)` 명령이다. 실행 때마다 HOME·키체인 기본/검색 목록을 검증하고 유료 크레딧(`useG1Credits`, `useAiCredits`)을 끈 설정을 다시 쓴다. 실행 중 인증 실패는 해당 계정만 `auth_expired`로 바꾼다.
- `POST /api/accounts/(id)/antigravity/check` `{principal?, confirm?}` → AccountOut. 전용 HOME으로 `agy --print /usage`를 한 번 호출한다. `confirm=true`이면 사용자가 입력한 Google 계정(`principal` 필수)을 `user_confirmed_cli_login`으로 기록하고 `ready`로 올린다. `confirm`이 없으면 `isolation_unverified`로만 남는다. `/usage` 성공만으로 다른 계정이라고 판정하지 않는다. 호스트 계정이면 422, 실패는 409 `{code, message}`다.
- `POST /api/orchestrator/workers`와 MCP `spawn_worker`는 명시적 `engine="antigravity"`를 허용한다. 작업자 `auto`는 task_type별 Antigravity/native 후보를 사용하며 명시한 Antigravity 모델 ID도 허용한다. 메인·접수·레거시 auto와 회의록/날씨의 별도 경로는 기존 계약을 유지한다. 2026-10-16 `agy models`에서 확인한 Claude Sonnet/Opus 5.5, Gemini 4 Argon, Gemini 3.8 Flash의 low/medium/high 및 GPT-OSS 120B medium을 지원한다. 생각 수준은 모델 ID에 포함되며 Antigravity CLI에는 `--effort`를 전달하지 않는다. 입력된 effort가 있으면 모델 ID와 일치해야 하고, 내부 Run의 effort는 미지정으로 정규화한다. 기본 Antigravity worker 모델은 `claude-sonnet-5-5-high`다. Argon·Antigravity Opus는 구체적인 `escalation_reason`을 요구한다.
- 최신 모델 배정 방침은 일반 구현·조사·UI에 Antigravity Sonnet 우선, 단순 범용 작업에 Flash 적극 활용, 어려운 설계·추론·독립 대안에 Argon/Opus 활용이다. 메인 오케스트레이터의 native Opus medium/GPT Sol high는 유지한다. 새 방침은 역할 지침 뒤에 주입해 과거 Flash-only·Claude 제외 지침과의 충돌을 해소한다.
- quota는 선택 모델의 관측된 그룹으로 판단한다. Claude/GPT, Gemini Flash, 별도 Argon 창을 독립적으로 검사하고 실행 중 quota 거절의 cooldown도 그룹별로 공유한다. Argon 창이 없는 경우는 unknown이며 다른 그룹의 여유로 대체하지 않는다. 소진 측정은 stale이어도 reset까지 유지하고, 정확한 잔여 비율을 사용해 0.1% 잔여를 정수 반올림으로 소진 처리하지 않는다. AI 크레딧 0은 baseline quota 0을 뜻하지 않는다. 유료 overage 설정은 변경하지 않는다.
- 실제 Run 이벤트는 `agent.session`, `message`, `tool.call`, `tool.result`, `usage`, `error`로 정규화한다. `usage`의 token 필드는 CLI가 제공할 때만 기록하고 cost는 `null`이다. 대화 재개 키는 계정별 `engine_session_id`이며 CLI에는 `--conversation`으로 전달한다. 재개 요청 ID와 `init.conversation_id`가 다르면 새 대화로 교체하지 않고 Run을 오류로 종료한다.
- `GET /api/accounts/usage`와 MCP `account_usage`는 `agy --print /usage --output-format json`의 모델 그룹별 5시간·주간 quota와 `/credits`의 AI 크레딧 잔액을 반환한다. quota는 `windows[]`에 기존 사용률 `percent`와 원천 잔여 `remaining_percent`를 함께 싣고, 크레딧은 별도 `credits_balance` 정수다. 전체계정 상한 퍼센트나 절대 요청/토큰 수는 만들지 않는다. 크레딧 청구주기 소모량은 현재 CLI JSON에서 받지 못하므로 표시하지 않는다.
- 수집은 계정 사용량 공통 캐시·polling·stale 판정을 따른다. CLI 오류·timeout·미인증 또는 JSON schema 불일치는 `error`/미측정으로 반환하며 수치 0·100으로 대체하지 않는다. AI 크레딧 잔액 0은 `credits_balance: 0`, 미측정은 `null`로 구분한다. Antigravity 전체 계정 상한으로 합치지 않으며, 새 작업자 라우팅은 선택 모델의 해당 그룹 창만으로 headroom을 계산한다. 크레딧 잔액은 배정 점수에 쓰지 않는다.
- **운영 제한:** ADR 0029의 이전 CLI 1.2.10 파일 작성·workspace 밖 차단 실측은 유지한다. 2026-10-15 CLI 1.3.5는 MCP dispatcher `call_mcp_tool`과 workspace 설정을 인식했지만 안전 fixture의 `server/discover` 미지원으로 연결 성공은 확인하지 못했다. Ttakkari MCP 서버 2.3.0의 직접 stdio discovery·tools/list는 성공했으나 실제 API 호출은 실패하도록 격리한 fixture였다. 설치 스크립트는 launchd에서 backend를 직접 실행하며 OS sandbox wrapper가 없지만, 이것은 운영 agy의 앱 상태 저장 권한을 증명하지 않는다. 이 세션에는 인증된 기존 backend Run 호출 경로가 없어 운영 agy 실행·재개·MCP는 미검증이고, 도구 sandbox에서 본 EPERM을 backend 실패로 일반화하지 않는다. `--help`에 `--log-file`만 로그 경로 override로 있으며, project flag는 ID/name이다. 공식 문서상 settings/session cache는 `~/.gemini/antigravity-cli` 아래에 고정돼 있고, 공개 저장소 [AGY_HOME 요청 #155](https://github.com/google-antigravity/antigravity-cli/issues/155)는 열린 기능 요청이다. 따라서 현재 실행 경계 안에서 정식 저장 위치 대체 경로를 확인하지 못했다. CLI가 결과 stdout 없이 exit 0을 반환하는 경우 Run은 실패로 처리하며 stderr 진단을 보존한다. 첫 단발 worker→보고는 매니저가 직접 연결하므로 재개/MCP 호출이 구조상 필수는 아니지만, 현재 API에 재개 비활성 capability는 없고 MCP 의존 작업·후속 Run은 별도 검증 대상이다. 상세 증거·경계는 [ADR 0029](adr/0029-antigravity-cli-engine.md)의 배포 조건을 따른다.
- **통합 상태:** Antigravity 변경은 back `main`에 통합됐고 기준 커밋 `f89709a93c008a447d19c9410a7ee155ab182073`의 Qwen feedback export는 보존했다. 다른 세션의 미커밋 변경은 통합 커밋에 포함하지 않았다. API/worker 계약 구현과 단위 검증은 포함하지만, 실제 backend smoke와 디자인 worker 실행은 미완료다. 상세 범위는 [ADR 0029](adr/0029-antigravity-cli-engine.md)를 따른다.

## 운영 로그·오류 이슈 (ADR 0033)

사용자 UI는 `/api/ops-logs`, 세션 토큰을 쓰는 MCP는 `/api/agent/ops-logs`를 쓴다. 두 경로의 응답은 같고, 에이전트 경로는 회사(`pool=work`) 세션에 403이다. 모든 응답은 비밀값을 `[REDACTED]`로 가리고 `max_chars`(500~16000, 기본 6000)를 넘지 않는다. 넘으면 `truncated: true`와 `hint`가 붙는다.

- `GET /issues?status=unresolved|resolved|ignored|all&source=&level=&q=&release=&tag=key:value&since_hours=&sort=last_seen|first_seen|count&limit=(1~50)&cursor=` → `{total, items[{id:"LOG-n", title, source, level, status, count, first_seen, last_seen, release(8자), culprit, regressed?}], next_cursor}`
- `GET /issues/{LOG-n|n}?events=(0~10)` → `{issue, latest_event{message, stack, excerpt, breadcrumbs, tags}, earlier_events, releases, recent_hours}`
- `PATCH /issues/{id}` `{status?, note?, release?}` → 해결 시 `resolved_at`·`resolved_release`를 기록한다. 해결 뒤 새 이벤트가 오면 자동으로 `unresolved`로 돌아가고 `regressed`가 올라간다.
- `GET /events?q=&since_hours=72&until_hours=&level=&source=&release=&issue=&tag=&limit=&cursor=` → 일치 구간 주변 발췌 목록
- `GET /trend?hours=(1~720)&unit=hour|day&issue=` → 시간별 집계(샘플링과 무관)
- `GET /deploys?limit=&status=` → 최근 배포 job과 실패 단계·원인 한 줄·연결 이슈
- `GET /deploys/{job id(접두사 8자 이상)|SHA 접두사(7자 이상)|latest|latest_failed}/log?mode=summary|errors|tail|range&lines=(1~200)&offset=` → 배포 로그를 줄 번호와 함께 요약·구간으로. 경로는 입력으로 받지 않는다.
- `POST /sync` 증분 인입을 즉시 한 번 돌린다(사용자 경로).

## 회의록 외부 공유와 내보내기 (ADR 0030)

상태(2026-10-16): 문서형 전사·선택 내보내기·공유 관리·별도 공개 UI를 연결했다. `public_sharing_enabled` 코드 기본값은 `true`로 전환하며 기존 회의가 자동 공개되지는 않는다. 운영자는 명시적 `false` 설정으로 익명 전달을 차단할 수 있다. 꺼져 있으면 익명 조회·다운로드는 404이며 소유자 개인 export는 계속 동작한다. 운영 배포 결과는 작업기록을 따른다. 현재 지원 형식은 TXT/MD/JSON/CSV이며 SRT/VTT/DOCX/PDF와 PMS/WBS/Artifact는 사용할 수 없다.

| 메서드 | 경로 | 요청 / 응답 |
|---|---|---|
| POST | `/api/shares/preview` | owner Bearer, `{resource:{type:"meeting",id}, selection:{sections,include_people?,include_evidence?}, download_formats?, confirm_work_scope?}` → `{document, source_revision, format_options, warnings, public_sharing_enabled}`. `source_revision`은 이후 게시·export 요청에 그대로 돌려준다. 공개 전 화면에서 document와 warnings를 사용자가 확인한다. `public_sharing_enabled=false`이면 UI는 공개 버튼을 잠그고 개인 다운로드는 허용한다. |
| POST | `/api/shares` | owner Bearer, preview 입력 + `{expected_source_revision, expires_at?}` → 201 게시 정보와 일회성 capability URL. 링크 비밀은 fragment `#key=...`에 있으며 서버는 hash만 저장한다. `work` 범위는 `confirm_work_scope=true`가 필요하다. |
| GET | `/api/shares?resource_type=meeting&resource_id={uuid}&offset=0` | owner Bearer → 해당 원본의 공유 상태·publication/source revision·만료·선택 목록. 링크 비밀은 반환하지 않는다. |
| GET | `/api/shares/{share_id}` | owner Bearer → 단일 공유 상태와 회수 여부. |
| PUT | `/api/shares/{share_id}/publication` | owner Bearer, 새 선택/형식/만료 + `expected_source_revision`, `expected_publication_revision` → 활성 공유의 snapshot과 publication revision을 갱신한다. capability 비밀은 회전하지 않고 기존 URL을 계속 쓴다. 응답에는 capability URL이 없다. |
| DELETE | `/api/shares/{share_id}` | owner Bearer → 204. 비가역 회수. |
| POST | `/api/exports` | owner Bearer, `{resource:{type:"meeting",id}, selection, format:"txt"\|"md"\|"json"\|"csv", part?, expected_source_revision}` → 선택된 최신 projection을 담은 attachment bytes. Preview의 revision이 바뀌었으면 409 `source_changed`; 미분석 CSV part는 409 `analysis_not_generated`, 선택되지 않거나 맞지 않는 part는 409 `format_not_applicable`. 공유 생성·Artifact 등록은 하지 않으며 public sharing off에서도 가능하다. |
| GET/HEAD | `/api/public/shares/{share_id}` | 로그인 없이 `Authorization: Bearer ps1_…` capability 필요 → 게시된 선택 DTO JSON. URL query에 비밀을 넣지 않는다. |
| GET/HEAD | `/api/public/shares/{share_id}/download?format={txt\|md\|json\|csv}&part={document\|utterances\|decisions\|actions\|discussions\|open_issues}&revision={publication_revision}` | 같은 capability 필요 → 승인된 snapshot에서 생성한 attachment. 조회·다운로드 모두 매 요청 활성·만료·원본·선택 part를 확인하며 다운로드는 응답 직전 `final_gate`를 통과해야 한다. 회수/삭제/만료 시 이후 요청은 404, 생성 중 재게시로 revision이 바뀌면 409이며 생성 bytes는 반환하지 않는다. 이미 인가되어 시작한 전송은 취소하지 않는다. |

Frontend는 preview → 사용자가 선택·검토 → 게시 → capability fragment를 브라우저에서 Authorization 헤더로 옮김 → 공개 조회/다운로드 → owner 목록·회수 순으로 연결한다. URL fragment는 서버 요청에 자동 전송하지 않는다. `format_options`의 `enabled`, `applicable`, `parts`, `reason`을 그대로 UI 상태에 사용한다. preview/download와 owner export 모두 서버가 선택된 section/part만 직렬화하며 frontend가 원본 회의 상세 API를 공개 페이지 데이터원으로 대신 호출하지 않는다. 정식 활성화 전에는 UI 캐시/CSP·외부 proxy 로그와 공개 전달 환경을 별도 확인해야 한다.


### 작업자 task routing 및 고정 엔진 한도 대체 (2026-10-16)

정책 단일 원본은 `runs/worker_routing.py`다. `account_usage.worker_routing`, 각 계정의 `model_capacity`, 역할 지침의 단계 목록은 이 원본에서 생성한다.

| task_type | 모델 단계(같은 단계 안 계정은 부하 분산) |
|---|---|
| general | Antigravity Sonnet → Flash → native Sonnet/Luna |
| routine | Flash → Luna → Antigravity Sonnet → native Sonnet |
| design | Antigravity Sonnet → native Sonnet |
| verification | Luna → Antigravity Sonnet → native Sonnet (Flash 제외) |
| reasoning | Argon → Antigravity Opus medium → native Sol/Opus; 사유 필수 |

- auto의 `model`은 모델군 선호이며 엔진 잠금이 아니다. 생략을 권장한다. task_type 생략은 general이며 Luna/Flash 모델 선호를 작업 종류로 오인하지 않는다. peer 모델만 reasoning으로 분류해 사유를 요구한다. Antigravity 모델군의 수준을 지정하면 해당 군의 후보에 적용한다(예: 단순 검색·추출·형식 변환은 `routine`에서 `gemini-3.8-flash-low` 또는 `gemini-3.8-flash-medium` 지정 가능; `Gemini 3.7 Flash`는 현재 설치된 `agy models` 카탈로그에 없어 도입하지 않음). 실제 실행 모델은 Run에 기록한다.
- 외부 제공사 발표·후기 기반의 모델 특성 자료(확인 필요: 코딩 벤치마크 대비 장기 에이전트 완수·컴퓨터 조작 편차 및 추론 토큰 증가 가능성)는 작업 성격별 배정·검증 정책 근거로만 참고하며, 검증된 자체 성능 순위로 단정하지 않는다.
- **Muse auto 편입 (2026-10-18, 코드 수락):** 운영 `6d5f580`에서 작은 코딩·MCP·동일 세션 재개를 각각 확인하고 GPT 독립 검증을 기록한 뒤 코드 수락 래치를 열었다([근거·미검증 범위](work-history/2026-10-18-muse-auto-acceptance.md)). `TTAKKARI_MUSE_ENABLED`, `TTAKKARI_MUSE_AUTO_ROUTING` 기본값은 true이며 둘 중 하나를 false로 지정하면 auto에서 제외된다. 확인된 구독 모델 `muse-spark-1.3`만 general(Sonnet과 같은 첫 단계)·routine(Flash와 같은 첫 단계) 후보가 된다. 전체 계정 모델 목록과 다른 모델 ID의 접근 권한은 보증하지 않는다. 기존 규칙(측정된 후보 > Run당 상한 여유 > 초기화 시각)이 적용되고 미관측·stale Muse는 측정된 대안보다 앞서지 않는다. design·verification·reasoning에는 넣지 않는다(Muse는 검증자가 되지 않는다). Muse의 general/routine 결과는 Flash와 같이 `verification_required=true`이며 영수증의 `recommended_verifier.excluded_family`는 `muse`다. 완료 보고에는 실제 engine/model을 표시한다. 개인 계정만 대상이고 회사(work) 범위 후보는 늘지 않는다. 게이트가 닫혀 있으면 `account_usage.worker_routing`·역할 지침 단계표·`accepts_model`에서 Muse가 빠지고 `account_usage.muse_auto_routing=false`다. 명시 `engine="muse"` 선택은 auto 수락 래치와 별개이며 Muse 실행 자체는 `TTAKKARI_MUSE_ENABLED`를 따른다. 코드 수락은 배포 후 실제 auto 선택·벤치마크·긴 작업·취소·복구 검증을 대신하지 않는다.
- **Flash 구현 작업의 독립 검증:** `general`·`routine` 작업이 `gemini-3.8-flash-*`로 실행되면 배정 영수증(`worker.routing`), `WorkerOut`, 완료 보고(`[작업 보고]`)에 `verification_required=true`와 `verification_status="pending"`을 표시한다. 최종 완료 판정 전에 `task_type="verification"`(GPT-6 Luna 우선, 불가 시 Claude Sonnet; Flash 배제)으로 교차 검증하고, `POST /api/orchestrator/workers/{id}/verification` 또는 `message_worker(verification_outcome="passed"|"failed", verification_note=..., verifier_worker_id=...)`로 결과를 기록한다. 검증 실패(`failed`)는 `false_completion=true`와 `worker.verification` Run 이벤트로 남기며 `GET /api/orchestrator/workers/metrics`에서 모델·작업유형별 재작업률(False Completion)을 집계 조회할 수 있다.
- **고정 엔진 세션의 한도 대체(`strict_engine`):** `claude`·`codex`·`antigravity`로 고정한 작업·일반 대화 세션은 기본적으로 평상시 해당 엔진 계정을 먼저 쓰고, 해당 엔진의 켜진 계정이 **모두 사용량 한도**일 때 대기하기 전에 (1) 같은 모델 계열의 다른 경로(예: native Claude `sonnet`/`opus` ↔ Antigravity `claude-sonnet-5-5-high`/`claude-opus-5-5-medium`, Antigravity `gpt-oss-120b-medium` → native Codex `gpt-6-luna`), (2) 작업 성격에 맞는 다른 엔진·풀의 같은 등급 모델 순으로 넘어가며, 모든 경로가 한도일 때만 `_wait_for_reset`으로 대기한다. 전환 시 `_attempt_handoff`와 엔진·모델별 대화 키를 유지하고 `engine.fallback` 이벤트 및 `run.usage.fallback`(`from/to`, `from_engine/from_model`, `to_engine/to_model`, `reason`, `fallback_tier`)에 전환 경로와 사유를 남긴다. 세션별로 `strict_engine=true`(`SessionIn`/`SessionPatch`/`WorkerIn`/`MessageIn`)를 지정하면 기존처럼 해당 엔진만 고수하고 한도 시 대기한다. 메인 오케스트레이터(Opus medium / Sol high), 접수(desk), 회의 분석, 날씨 시스템 Run 및 `work` 계정 범위 분리는 변경하지 않는다.
- 기존 queued quota wait도 active coordinator가 현재 후보로 재평가한다. lease 부재와 control/Run 잠금 안에서만 reset 예약을 해제하며, 취소·실행 중 소유권·restart backoff는 유지한다. 같은 Run의 시작·checkpoint·인계문·provider 대화를 보존한다. 원래 요청 모델은 `run.usage.routing_request.model`에 유지하고 실제 fallback 모델은 `Run.model`로 기록한다.
- Antigravity 모델 그룹 풀(`agy_claude_and_gpt_models`, `agy_gemini_models`, `agy_gemini_4_argon`)은 별도 `limit_key`와 창으로 판단해 한 풀 소진이 다른 풀을 막지 않는다.
- 명시 선택형 `engine="qwen-local"`은 같은 worker API/MCP `spawn_worker`에서만 사용할 수 있고 `engine="auto"` 후보에는 포함하지 않는다. 모델은 `Qwen3.8-Flash-Next-Q2_K`로 고정한다. 활성화 기본값은 false이며, 명시 선택도 disabled/readiness 실패면 `Qwen local blocked: <reason>`으로 실패하고 다른 제공자로 전환하지 않는다. readiness는 고정 `qwen-worker doctor` 응답이 contract `qwen-local-readiness-v1` 및 Seatbelt·native host monitor·bounded fixture 세 검사를 모두 true로 증명해야 통과한다.
- 소유자 전용 `GET /api/system/qwen-worker`는 `{enabled, config_locked, ready, reason}`을 반환한다. `PUT`은 엄격한 `{enabled: boolean}`만 받으며, 켜기는 doctor가 통과한 뒤 `app_settings.qwen_worker_activation`에 저장하고 감사 기록을 남긴다. 설정이 없으면 꺼짐이며 에이전트/오케스트레이터 토큰은 이 API를 사용할 수 없다. `TTAKKARI_QWEN_LOCAL_ENABLED`가 명시되어 있으면 그 true/false를 우선하고 PUT은 `409 qwen_config_locked`로 거부한다. 환경 설정이 생략된 경우에만 앱에서 바꿀 수 있다. 준비 검사 실패는 `409 qwen_not_ready`이며 활성화 저장을 하지 않는다. 켜짐/꺼짐은 다음 배정·실행 시도에서 DB로 읽으며 실행 중인 시도는 변경하지 않는다. 개인 scope, 모델 허용 목록, 평시 auto 제외, 비상 진입 조건은 계속 적용한다.
- 로컬 작업자는 Ttakkari MCP와 계정 인증 환경을 받지 않는다. `worker.routing`/`run.usage.routing_request`에는 요청 엔진·작업 유형·실제 provider engine/model 또는 blocked 상태, 개인/회사 계정 scope와 workspace를 기록한다. 로컬 엔진의 `quota`는 null이며 cloud 계정 사용량으로 집계하지 않는다. 기존 `/api/accounts` 계정 엔진 목록에는 포함하지 않는다.
- 오케스트레이터 위임 지침은 Mac 전체에서 Qwen 미종료 작업(queued/running)을 하나만 허용한다. 새 Qwen 작업이나 후속/재개 요청 전에 작업 상태와 실제 라우팅(비상 Qwen 포함)을 확인하고, 앞 작업의 완료·실패·취소 확정 보고 뒤 다음 작업을 제출한다. 유휴 대화는 슬롯을 차지하지 않으며 클라우드 작업은 병행할 수 있다. 이는 오케스트레이터의 위임 규칙이며, 별도 서버 대기열이나 API 수준의 전역 동시성 제한을 추가한 것은 아니다.
- `0037_worker_routing`(`down_revision = "0036_system_resource_samples"`)은 `sessions.worker_task_type`(nullable)과 `sessions.strict_engine`(`server_default=false`)만 추가하는 additive migration이며 `0034`·`0035`·`0036_system_resource_samples`에서 온라인 업그레이드할 수 있다. GPU 이력의 0036 뒤에 직렬 연결하며 generation protocol/소유권·readiness·drain 계약은 유지한다.
- `0038_qwen_flash_usage`(`down_revision = "0037_worker_routing"`)은 Flash Next 로컬 모델의 일일·주간 토큰 영속 집계를 위한 `qwen_flash_usage` 테이블을 추가하는 additive migration이다. generation protocol 1을 유지하며 이전 리비전들에서 온라인 업그레이드할 수 있다.


### ArtCraft 관제 (ADR 0035)

소유자 로그인 전용이며 디자인시스템 API와 무관하다. 오류 형식은 공통 `{code, message}`.

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/artcraft/apps?refresh=` | `{generated_at, stale, error, apps[], remote{checked_at,attempted_at,error,checking}, jobs[]}`. 앱: `installed{release,cli_ok,cli_version,source_build}`, `latest{tag,update_available,checked_at}`, `smoke{passed,total,failed[],mcp,gui,ts,source}`, `mcp{ok,tool_count}`, `gui{running,listening,port,transport}`, `auth{mode,label,protected}`, `known_bugs[]`. 15초 캐시, `refresh=true`면 다시 읽는다. 조회 실패 시 이전 값과 `stale=true`, 처음부터 실패면 502 |
| POST | `/api/artcraft/remote-check` | GitHub 최신 릴리스 확인 Job(202). 이미 도는 중이면 그 Job, 120초 안 재요청은 429 |
| POST | `/api/artcraft/smoke` | `{apps?: string[], mode: "cli"\|"full", confirm?}` → 202 Job. `full`은 `confirm=true` 필요(428), 실행 중 앱이 있으면 409 `app_running`. 다른 스모크·GUI 작업이 돌면 409 `busy`. 모르는 앱은 404 |
| POST | `/api/artcraft/apps/{app}/gui` | `{action: "start"\|"stop", confirm?}` → 202 Job. `stop`은 `confirm=true` 필요(428). 이미 실행 중 409 `already_running`/`port_in_use`, 실행 중 아님 409 `not_running` |
| GET | `/api/artcraft/jobs`, `/jobs/{id}` | Job `{id,kind,action,apps,mode,state(queued\|running\|succeeded\|failed\|timeout),summary,output}`. `output`은 마지막 3000자이며 token/key 모양 값은 가린다 |
| GET | `/api/artcraft/bugs` | BUGS.md의 `## BUG-nnn` 절(고정 형식, ADR 0035)을 매 요청 읽는다. `{source_modified_at, counts{상태별}, apps[], problems, bugs[]}`. 항목: `id,title,repos[],apps[],severity(S1-3),kind(issue\|pr\|security),difficulty(easy\|medium\|hard),upstream{state(unchecked\|none\|related\|duplicate),note},found,verified,sections{summary,impact,repro,evidence,suggestion,unverified},truncated[],problems[]`(형식 오류는 숨기지 않고 사유를 싣는다) + 따까리가 저장한 `status,note,url,updated_at,history[]` + `handoff`(에이전트에게 넘길 요청 문구) |
| PUT | `/api/artcraft/bugs/{id}` | `{status, note?(≤500), url?(https://github.com/... 만, ≤300)}`. 상태: `candidate`(미검토)→`selected`(기여 선택)→`in_progress`→`reported`(보고됨·PR 올림)→`resolved`, 또는 `deferred`·`excluded`. 어느 상태로든 옮길 수 있고 변경은 `history`에 남는다. 예전 값 `unreported`는 읽을 때만 `candidate`로 본다. 없는 ID 404. **외부 GitHub로 올리는 기능은 없다** |
| GET | `/api/artcraft/agent-config` | 앱별 MCP 설정 `snippets{json,claude,codex}`와 전체 JSON |

### ArtCraft 작업 플랫폼(ADR 0039, 관제와 같은 `/api/artcraft`)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/artcraft/catalog` | `{sections[{id,title,categories[]}], categories[{id,section,title,tagline,icon,maturity{level(ready\|limited\|experimental),label,summary},apps[{app,title,role,installed}],can[],cannot[],accepts[],outputs[],templates[{id,title,prompt,example,min_files,apps[],options[{id,label,default,choices[]}]}]}], available, unavailable_reason, workspace_id, workspace_error, limits{files,prompt}}`. 느린 호출 없음. `workspace_id`는 파일 업로드(`POST /api/uploads`)에 쓰는 ArtCraft 작업 워크스페이스 |
| POST | `/api/artcraft/tasks` | `{category, prompt(1-8000), template_id?, input_artifact_ids[≤10], apps?, options?{옵션ID:값}, speed?(fast\|normal\|careful)}` → 201 `TaskDetail`. 세션을 만들고 첫 Run을 대기열에 넣는다. 오류: 404 `unknown_category`, 422 `unknown_app`·`bad_input`(받지 않는 확장자, 파일 부족, 옵션 값, 없는 파일, 민감 파일), 503 `workspace_unavailable` |
| GET | `/api/artcraft/tasks?category=&state=active\|all&session_id=&limit=` | 작업 요약 `{id,session_id,category,title,apps,template_id,state(queued\|running\|done\|failed\|cancelled\|interrupted),round_count,output_count,last_run_id,error,progress,waiting_reason,created_at,updated_at,archived_at}`. 보관한 작업은 `session_id` 조회에만 나온다 |
| GET | `/api/artcraft/tasks/{id}` | 요약 + `options` + `rounds[{id,index,status,state,prompt,result_text,error,options,use_previous,inputs[ArtifactOut],outputs[ArtifactOut],progress,waiting_reason,created_at,started_at,finished_at}]`. 결과는 그 세션의 `source=agent_output` Artifact |
| POST | `/api/artcraft/tasks/{id}/runs` | `{prompt, input_artifact_ids[], use_previous(기본 true: 직전 결과가 있는 라운드의 결과를 prev/ 로 복사), options?, speed?}` → 202 `TaskDetail`. 실행 중이면 세션 대기열에 쌓인다. 보관한 작업은 409 `archived` |
| PATCH | `/api/artcraft/tasks/{id}` | `{title?, archived?}` → `TaskDetail` |
| GET | `/api/artcraft/outputs?category=&kind=&q=&limit=&offset=` | 작업물 갤러리 `{items[{artifact, task_id, task_title, category, run_id}], has_more}` |

취소는 `POST /api/runs/{id}/cancel`, 업로드는 `POST /api/uploads`(workspace_id = catalog.workspace_id), 이벤트는 `/api/runs/{id}/events`를 그대로 쓴다. Artifact 종류에 `audio`, `video`가 추가됐다. ArtCraft 작업 세션은 `GET /api/sessions` 목록에서 제외된다.

설정: `TTAKKARI_ARTCRAFT_WORKSPACE_ROOT`(기본 `~/development/projects/artcraft-workspace`, 작업 폴더 루트. `allowed_roots` 안이어야 한다).

설정: `TTAKKARI_ARTCRAFT_ROOT`(기본 `~/development/projects/artcraft`), `TTAKKARI_ARTCRAFT_AUTO_REMOTE_CHECK`(기본 true, 최신 릴리스가 6시간 넘게 오래되면 조회 시 백그라운드로 갱신).

### Muse Code 구독 사용량

`GET /api/accounts/usage`는 `engine=muse`를 지원한다. `login`은 공식 SDK account/read에서 관측한 상태를 사용하고 `execution_enabled`는 개발 작업의 활성화 여부다. 사용량 조회는 개발 실행 활성화와 독립적이며 모델 프롬프트·개발 도구를 실행하지 않는다. MSP usage/read는 호스트의 마지막 관측값이다. 인증된 호스트의 응답이 비면 별도 단일 스레드 helper가 공식 대화형 CLI의 `/usage`를 호출한다. 임시 빈 작업공간·PTY에서 `/usage`와 `/exit`만 보내고, 인증·결제·권한 질문에는 답하지 않는다. API 키·인증 파일·브라우저 쿠키를 추출하지 않는다.

이 호환 경로는 Muse 1.4.10에서 실계정 검증했다. CLI 카드의 정수 퍼센트와 분 단위 초기화·관측 시각을 UTC로 변환한다. UI 텍스트는 안정적인 SDK 계약이 아니므로 형식이 다르면 수집 실패로 처리한다. 앱 시작 시 계정 로딩이 늦는 경우에만 읽기 전용 명령을 최대 3번 보내며 전체 18초 제한이 있다. 권한 거절은 즉시 중단하고 사용량 API·모델 호출로 우회하지 않는다. CLI가 수치를 제공하지 않으면 0%나 무제한을 만들지 않는다.

실행 중 account-wide `usage/changed`와 종료 후 `muse/quota`도 Run의 토큰 합계와 분리해 같은 저장소에 기록한다. 관측값은 source=muse_cli이며 원래 관측 시각·플랜을 유지한다. 이후 인증된 조회가 비면 마지막 관측값과 오류를 함께 반환하고 수집 성공이나 새 측정으로 취급하지 않는다. 로그아웃·프로필 경로 변경은 캐시를 폐기한다. 등록 모델 목록과 실제 구독 모델 호출 권한은 별개다.

`GET /api/accounts`도 Muse의 공식 로그인 관측값을 사용한다. 첫 수집 전에는 동일한 읽기 전용 조회로 상태를 확인하며 이후에는 수집기 TTL 캐시를 재사용한다. 사용량 창이 비어 있어도 `accountLogin`이면 `login=ok`, `login_command=null`이다. Muse의 `config_dir` 변경 시 이전 프로필의 관측 캐시는 폐기한다. 로그인 여부와 사용량 수치 제공 여부를 혼동하지 않는다.


### 디자인시스템 조회와 시각 미리보기 (ADR 0034)

소유자 JWT: `GET /api/design-systems`, `/{system}/items`, `/{system}/item`. MCP는 같은 계약의 `/api/agent/design-systems` 아래 개인 세션에서만 허용하며 회사 세션은 403이다. 검색·상세는 원본 전체 40자리 `revision` SHA로 고정한다. 검색 kind는 token/document/component/example이며 토큰 요약에 `values:{light,dark}`가 포함된다.

시각 미리보기는 owner-only `GET /api/design-systems/{system}/preview?item=&theme=light|dark&revision=`. 기본 item은 `file:{system}/examples/preview.html`이며 example만 허용한다. 응답 `{revision,system,source,html,sections:[{id,title}],warnings:string[]}`. html은 같은 커밋의 허용 CSS·JS·이미지를 data URL로 묶은 문서이며 프론트는 opaque sandbox iframe에만 넣는다. 별도 대용량 계약(최대 20 MiB)으로 MCP의 16k 텍스트 도구에는 실리지 않는다. 누락 404, 잘못된 조건 422, 저장소/크기 오류 503.


### 주식 투자·토스증권 연결 (ADR 0044, 소유자 전용·읽기 전용)

`GET /api/stocks/overview` → `[{id, name, state, checked_at, detail}]` (지금은 토스증권 하나).
`GET /api/stocks/tossinvest/status` → `{state, message, logged_in, checked_at, browser_available, profile_exists, check_interval_seconds, last_run}`. 브라우저를 띄우지 않고 마지막 기록만 본다. `state`: `never_checked | running | ready | login_required | login_window | unknown | disabled | error | failed`.
`GET /api/stocks/tossinvest/runs?limit=`(1~100) → `RunOut[]` (최신순). `RunOut = {id, kind: check|sync, status, message, logged_in, page_title, api_paths: string[], duration_ms, request_source, created_at, updated_at}`.
`GET /api/stocks/tossinvest/login-help` → `{profile_dir, profile_exists, cookie_hosts(호스트 이름만), steps, open_command, note}`. 비밀값 없음.
`POST /api/stocks/tossinvest/login-window` → 202 `{state, message}`. 고정 인자로 일반 Chrome을 열어 사람이 로그인하게 한다. 요청 본문을 받지 않으며, 같은 프로필로 로그인 창이나 headless 확인이 실행 중이면 409(`login_window_open`·`check_running`)로 거절한다.
`POST /api/stocks/tossinvest/check`·`/sync` → 202 `RunOut`. DB advisory transaction lock 안에서 마지막 기록을 검사하고 `running` 행을 먼저 커밋한 뒤 Chrome을 연다. 마지막 시작/갱신 후 `tossinvest_check_interval_seconds`(기본·최소 300초) 안이면 동시 요청·재시작 후 요청도 429 `rate_limited` + `Retry-After`. 실행은 180초로 제한하고, 중단으로 `running`만 남은 경우 간격이 지난 후 재시도할 수 있다. 탐색 전 CDP Fetch로 허용된 HTTPS GET만 계속하며 나머지 페이지 요청은 차단한다. 금액·수량·쿠키는 저장·반환하지 않는다. 실제 로그인 연결과 Chrome 전체의 백그라운드 트래픽 통제는 미검증이다.
에이전트 읽기: `GET /api/agent/stocks/tossinvest/status|runs|login-help`, MCP `tossinvest_status`(READ_ONLY).

### Google 계정 (ADR 0051, 소유자 전용)

Google 로그인·세션은 NotebookLM 같은 기능이 아니라 계정 단위다. 한 번 로그인한 계정의 세션을 등록된 모든 기능이 공유한다. 계정 키 `account_id`(`^[a-z0-9][a-z0-9_-]{0,31}$`)는 지금 `primary` 하나다. 응답에 쿠키·토큰은 없고 쿠키 이름만 있다. 오류 형식은 NotebookLM 절과 같다(`{code, message[, retry_after]}`).

- `GET /api/google-accounts` → `{accounts: [Account]}`. `GET /api/google-accounts/{account_id}` → `Account`. 브라우저·Google 요청 없이 마지막으로 알려진 상태만 본다.
  `Account` = `{account_id, label, state, message, logged_in, reauth_required, last_login_at, last_ok_at, last_checked_at, last_error_code, session{stored, cookie_names, permissions_ok, encrypted:false, protection}, login{state, message, active, feature, started_at, finished_at}, browser_available, features[Feature], metrics}`.
  `state`(계정 수준): `login_required | session_saved | ready | reauth_required | disabled`. 쿨다운·blocked 는 계정 상태가 아니다.
  `Feature` = `{id, label, description, path, enabled, covered, can_verify, can_resume, state, message, cooldown_until, retry_after, last_ok_at, ...}`. 기능 자신이 정한 상태다(`state` 는 계정 상태가 막으면 그 값, 아니면 기능 값: `ready | session_saved | cooldown | blocked | ...`). `covered` 는 저장된 세션에 이 기능의 쿠키 도메인이 있는가(세션이 없으면 `null`). false 이면 그 기능의 화면에서 로그인을 한 번 더 한다.
- `POST /api/google-accounts/{id}/login {feature?}` → 202 `login`. Mac 화면에 그 계정 전용 프로필 Chrome 창을 연다(원격 디버깅·자동화 플래그 없음). `feature` 가 연 앱 화면이 로그인 완료 판정 기준이다(비우면 첫 등록 기능). `login.state`: `starting|waiting|protection|saving|done|timeout|cancelled|failed`. 같은 계정에 창이 열려 있으면 409 `busy`, 꺼진 기능이면 503 `disabled`, 모르는 기능이면 422 `invalid`.
- `POST /api/google-accounts/{id}/login/complete` → `login`(창을 닫고 2단계로 쿠키 수집·저장) · `POST /login/cancel` → `login`.
- `DELETE /api/google-accounts/{id}/session` → `Account`. 저장된 세션 파일을 지운다(Google 로그아웃 아님, Chrome 프로필은 둔다).
- `POST /api/google-accounts/{id}/features/{feature}/verify` → 그 기능의 상태. 기능이 정한 확인 1회(Google 요청, 기능별 간격 제한). 지원하지 않으면 501 `unsupported`.
- `POST /api/google-accounts/{id}/features/{feature}/resume` → 그 기능의 상태. 그 기능의 `blocked` 를 사람이 확인한 뒤 푼다. 다른 기능에는 영향이 없다.
- 세션 만료는 어느 기능이 감지하든 계정 `reauth_required` 가 되고 알림(`push.google_account`, 링크 `/settings#google-accounts`)은 계정당 한 번이다. 다시 로그인하면 풀린다.
- 에이전트(개인 세션 전용, 업무 세션은 403): `GET /api/agent/google-accounts[/{account_id}]` 와 MCP `google_account_status`(READ_ONLY)뿐이다. 로그인 창·세션 삭제·재개·확인은 노출하지 않는다.

### NotebookLM 연동 (ADR 0045, 0051, 소유자 전용)

기존 Google 계정의 NotebookLM 권한·할당량만 쓴다(유료 API 키 경로 없음). 지금은 계정 `primary` 하나이며 DB 행·잠금·job은 모두 `account_id`를 키로 가진다. 상류 `notebooklm-py==0.8.5` 공개 API를 `NotebookLMBackend` Protocol 뒤에 둔다. 응답에 쿠키·토큰·상류 응답 원문은 없다.

**오류 형식** `{code, message[, retry_after, outcome_unknown]}`. `Retry-After` 헤더는 `retry_after`가 있을 때 같이 간다.
| status | code | 의미 |
|---|---|---|
| 409 | `auth_required` | 로그인 없음/만료. 로그인 창에서 다시 로그인 |
| 429 | `rate_limited` · `cooldown` | Google 사용 제한(429). `retry_after`초 동안 Google에 요청하지 않는다. 확인 간격 제한에도 쓴다 |
| 423 | `protection_signal` | CAPTCHA·보안 확인 신호. 사람이 확인하고 `POST /auth/resume` 할 때까지 요청하지 않는다 |
| 409 | `busy` · `duplicate` | 로그인 창/작업 중복 · 같은 소스 중복 또는 결과 불명 |
| 404 / 422 / 501 | `not_found` / `invalid` / `unsupported` | |
| 502 / 503 / 504 | `upstream_error`·`upstream_rejected` / `transient` / `timeout` | `outcome_unknown: true`이면 쓰기가 Google에 닿았는지 모른다. 같은 쓰기를 다시 보내지 말고 목록을 먼저 확인 |

**인증·상태**
- 아래 `/auth/*` 는 Google 계정 공통 모듈(위 절)로 위임하는 호환 경로다. 경로와 응답 키는 그대로이고 `account{id, state}` 가 추가됐다. 로그인 창은 NotebookLM 앱 화면 기준으로 열린다. 로그인·재인증은 계정 상태이고 `cooldown`·`blocked` 만 NotebookLM 고유 상태다.
- `GET /api/notebooklm/auth/status` → `{account_id, state, message, logged_in, reauth_required, cooldown_until, retry_after, last_ok_at, last_checked_at, last_error_code, session{stored, cookie_names(이름만), permissions_ok, encrypted:false, protection}, login{state, message, active, started_at, finished_at}, browser_available, backend, capabilities[{name, available, reason}], limits, metrics}`. Google 요청·브라우저 실행 없음. `state`: `login_required | session_saved | ready | reauth_required | cooldown | blocked | disabled`. `capabilities`에서 `available:false`인 항목(audio_overview, video_overview, research, drive_sources, sharing)은 지원하지 않는 기능이다.
- `POST /auth/login-window` → 202 `login`. Mac 화면에 전용 프로필 Chrome 창을 연다(원격 디버깅·자동화 플래그 없음). 사람이 그 창에서 Google 로그인을 한 번 한다. `login.state`: `starting|waiting|protection|saving|done|timeout|cancelled|failed`. 같은 계정에 창이 열려 있거나 이전 창이 남아 있으면 409 `busy`. 화면은 `GET /auth/status`의 `login`을 폴링한다.
- `POST /auth/login-window/complete` → `login`. 로그인 창을 닫고 2단계로 headless Chrome을 잠깐 띄워 세션 쿠키를 수집·저장한다.
- `POST /auth/login-window/cancel` → `login`. 창을 닫고 취소한다.
- `POST /auth/verify` → status. 저장된 세션으로 NotebookLM에 접속해 본다(Google 요청 1회, 30초 간격 제한 → 429).
- `POST /auth/resume` → status. `blocked`를 사람이 확인한 뒤 푼다(blocked가 아니면 409).
- `DELETE /auth/session` → status. 저장된 세션 파일을 지운다(Google 로그아웃 아님).

**노트북·소스**
- `GET /notebooks` → `[{id, title, source_count, created_at, updated_at, is_owner}]` · `POST /notebooks {title}` → 201 · `GET /notebooks/{id}` → 위 필드 + `sources[{id, title, kind, url, status, created_at, word_count}]` · `PATCH /notebooks/{id} {title}` · `DELETE /notebooks/{id}` → 204.
- `GET /notebooks/{id}/sources` → `SourceInfo[]`. `kind`: `web_page|youtube|pasted_text|pdf|document|media|other`, `status`: `processing|ready|error|unknown`.
- `POST /notebooks/{id}/sources/url {url, title?, force?}` · `.../sources/text {title, text, force?}` · `.../sources/upload`(multipart `file`, `title?`, `force?`; 상한 `notebooklm_upload_max_bytes`, 초과 413) → **202 `{duplicate:false, job, source:null}`**, 이미 등록된 소스면 **200 `{duplicate:true, job:null, source}`**. 중복 키는 (계정, 노트북, 정규화 URL | 텍스트 해시 | 파일 해시). 같은 소스가 등록 중이면 409 `duplicate`. 이전 등록 결과가 불명(`unknown`)이면 URL은 원격 목록과 대조해 확정하고, 못 찾으면 409 `duplicate` → 목록 확인 후에만 `force:true`.
- `DELETE /notebooks/{id}/sources/{sourceId}` → 204.
- `POST /notebooks/{id}/query {question, conversation_id?, source_ids?, export_for_ledger?}` → `{notebook_id, query, answer, conversation_id, turn_number, citations[{number, source_id, quoted_text, source_span{start,end}, answer_span{start,end}, chunk_id, score}], sources[{id, title, url, kind, citation_numbers}], created_at[, ledger_export]}`. `answer`는 NotebookLM이 만든 분석, `citations`는 연결, `sources`는 출처다. `source_span`은 원문 문서 좌표(상류 문서화 기준 UTF-16 code unit), `answer_span`은 답변에서 그 인용이 받치는 범위(상류 좌표, 단위 미검증). 값이 없으면 `null`이며 꾸며내지 않는다. `ledger_export`는 지식 원장용 구조(`source_units`·`analysis`·`citations`·`assertions: []`, `promotion.auto_promoted:false`)이고 적재는 하지 않는다.

**작업(job)**: `GET /jobs?limit=` · `GET /jobs/{id}` → `{id, kind, notebook_id, status, params, result, error_code, error, cancel_requested, created_at, started_at, finished_at}` · `POST /jobs/{id}/cancel`(끝난 작업은 409). `status`: `queued|running|succeeded|failed|cancelled|unknown`. 소스 등록 job의 `result`는 `{source, ready}`(처리 대기 시간이 지나면 `ready:false` + `note`). 등록 요청 중 취소·재시작은 `unknown`/`cancelled` + `result.outcome_unknown`. 서버가 재시작되면 실행 중이던 job은 `unknown`, 시작 전이던 job은 `failed`(`error_code: shutdown`)가 된다.

**에이전트(개인 세션 전용, 업무 세션은 403)**: `/api/agent/notebooklm/*` — 위 경로 중 `auth/status`·`auth/verify`, 노트북·소스(upload 제외)·query·jobs. 로그인 창·취소·resume·세션 삭제·업로드는 노출하지 않는다. MCP 도구: `notebooklm_status|list|get|create|rename|delete|add_source|delete_source|query|job|job_cancel` (삭제는 `confirm=true` 필요, 읽기 4개만 readOnly).

### GitHub 탐색 (ADR 0042, 소유자 전용·읽기 전용)

`/api/github/*` 전부 GET이며 소유자 JWT가 필요하다(에이전트/MCP 라우터 없음). `account=<gh login>` 쿼리가 필수이고(누락은 422), 계정 목록은 `GET /api/github/accounts`, 입력 해석은 `GET /api/github/resolve?q=&account=`. 공통 봉투 `{data,page,rate,fetched_at,cached,stale,account}`와 엔드포인트·오류 코드 표는 `tests/fixtures/github/README.md`, 타입 정본은 `src/ttakkari/github/schemas.py`. 상류 401은 앱 로그아웃을 막으려고 502 `gh_auth_expired`로 바꾼다. 토큰은 응답·로그에 없다. 요청 하나의 전체 시간 예산(15초, 로그 20초)은 계정 확인·모든 중첩 상류 호출·재시도를 합친 한 마감이고(초과 504 `gh_timeout`), `stale:true`는 마지막 확인 후 10분까지만 나온다. `/resolve`는 `HEAD`·7~39자 약식 SHA를 지원하고, 같은 접두사 ref가 매우 많아 확인하지 못하면 422 `gh_unprocessable`(`reason: ref_resolution_limit`)이며 `ref_not_found`로 가리지 않는다. gh 상태 조회 일시 실패는 503 `no_account`(`reason: gh_status_failed`)로 로그아웃과 구분한다(ADR 0042 §13).
