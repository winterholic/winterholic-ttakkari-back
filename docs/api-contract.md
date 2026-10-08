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
| POST | `/api/workspaces` | `{name, root_path, default_engine?, codex_sandbox?, export_allowed?}` → 201. `allowed_roots` 밖이면 403 `outside_root`, 중복 409 |
| GET/PATCH | `/api/workspaces/{id}` | PATCH: `{name?, default_engine?, codex_sandbox?, export_allowed?, archived?}` |
| GET | `/api/workspaces/{id}/files?path=.&show_hidden=false` | `DirListing {rel_path, entries: FileEntry[], truncated}` |
| GET | `/api/workspaces/{id}/search?q=` | 파일명 부분 일치 `FileEntry[]` (최대 200) |

`FileEntry = {name, rel_path, type: "file"|"dir"|"symlink"|"other", size, modified_at, sensitive}`. 경로는 항상 워크스페이스 기준 상대경로다. 절대경로를 보내지 않는다.

## 세션과 Run

세션 = 엔진 대화 하나(Chat 화면 하나). Run = 사용자 지시 1회.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/sessions?workspace_id=&include_archived=&limit=` | `SessionOut[]` (최근 갱신순). `active_run_id`, `last_run_status` 포함 |
| POST | `/api/sessions` | `{workspace_id, title?, engine?, model?, effort?}` → 201 |
| GET/PATCH | `/api/sessions/{id}` | PATCH: `{title?, model?, effort?, archived?}` |
| GET | `/api/sessions/{id}/runs` | `RunOut[]` (오래된 순 = 대화 순서) |
| POST | `/api/sessions/{id}/runs` | `{prompt, context_artifact_ids?, model?, effort?}` → 202 `RunOut`. 같은 세션에 실행 중 Run 이 있으면 409 |
| GET | `/api/runs/{id}` | `RunOut` |
| POST | `/api/runs/{id}/cancel` | 취소. 최대 5초 기다렸다가 최신 `RunOut` |
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
| `run.status` | `{status}` |
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
| GET | `/api/artifacts?workspace_id=&session_id=&run_id=&kind=&q=&limit=&offset=` | Library 목록(최신순) |
| POST | `/api/artifacts` | `{workspace_id, rel_path, session_id?}` 워크스페이스 파일을 결과물로 등록 |
| GET | `/api/artifacts/{id}` | `ArtifactOut` |
| DELETE | `/api/artifacts/{id}` | 204. 관리 영역 복사본도 지운다 |
| GET | `/api/artifacts/{id}/content?variant=original|preview` | 뷰어용 인라인(Bearer). Range 지원. 민감 파일은 403 |
| POST | `/api/artifacts/{id}/download-link?variant=` | `{url, expires_at}` 5분짜리 서명 링크. `url` 은 백엔드 기준 상대경로 |
| GET | `/api/dl/{token}` | 첨부 다운로드(인증 헤더 불필요) |
| POST | `/api/artifacts/{id}/preview/retry` | 실패·불가 상태의 미리보기 변환 재시도 |

`ArtifactOut` 주요 필드: `kind` (`markdown|html|pdf|pptx|docx|sheet|image|code|other`) 로 뷰어를 고른다. `source` (`agent_output|agent_modified|user_registered`), `preview_status` (`not_required|pending|processing|ready|failed|unavailable`), `export_policy` (`allow|deny|sensitive`), `downloadable`.

뷰어 규칙:
- `not_required` 인 형식은 `content?variant=original` 을 받아 프론트가 직접 렌더한다(markdown, pdf, sheet, image, code, html).
- `pptx`, `docx` 는 `preview_status == ready` 일 때 `content?variant=preview` (PDF) 를 PDF 뷰어로 연다. `unavailable|failed` 면 원본 다운로드만 제공한다.
- `html` 은 받은 텍스트를 `<iframe sandbox="allow-scripts" srcdoc>` 로만 렌더한다(`allow-same-origin` 금지). 응답에도 CSP sandbox 헤더가 붙어 있다.
