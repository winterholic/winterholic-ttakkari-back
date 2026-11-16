# 프로젝트 맥락 공유 Private API 계약

Private 앱과 대화 에이전트의 프로젝트 맥락 게시·운영 API 경계를 적는다. 모든 예시는 합성 값이며 실제 응답에서 복사한 구조다. 코드: `src/ttakkari/api/context_sharing.py`, `src/ttakkari/context_sharing/`, 마이그레이션 `0079_context_sharing`.

## 1. 구조와 경계

### ADR 0030과의 경계

[ADR 0030](../adr/0030-public-capability-shares-and-exports.md)은 기존 PublicShare API에서 `CtxDocument`·`CtxMemory`·`CtxSource` 공개를 지원하지 않는다고 정한다. 이 기능은 그 API나 `PublicShare` 모델에 맥락 도서관을 추가하지 않으며, 전체 도서관 검색도 노출하지 않는다. 이 계약은 별도 Private Publisher에서 소유자 UI 또는 사용자의 직접 대화 지시에 따라 한정된 프로젝트 색인 문서를 독립 Context Gateway로 게시하는 경로다. 따라서 프로젝트 맥락의 외부 AI 게시 범위는 이 작업에서 승인된 별도 기능이며 ADR 0030의 기존 PublicShare 범위가 바뀌었다고 해석하면 안 된다.

```
소유자 JWT ───────────────────┐
에이전트 세션 + 직접 사용자 지시 ─┴─▶ Private Publisher ──control 토큰──▶ Gateway control 앱 ──▶ Gateway 독립 DB/파일
                                              ▲                                  외부 AI ──1회용 키 교환──▶ Gateway public 앱(읽기 전용)
                                              └── LibraryAdapter ── 도서관 DB(ctx_*)
```

- 원본·도서관·대화 기록 접근은 Private만 한다. Gateway는 승인된 스냅샷만 받는다.
- 어댑터(`ContextSourceAdapter`, 구현 `LibraryAdapter`)는 `CtxEntity(type=project)`에 연결된 **사용 중인** `CtxSource`의 색인 문서(`CtxDocument`/`CtxChunk`) 중 요청이 `document_id`로 명시한 것만 읽는다. 전체 검색, 기억(`CtxMemory`), 대화, 임의 경로 파일은 읽지 않는다.
- Private 초안에는 색인 루트 기준 `logical_path`(예 `docs/arch.md`)와 `library:<category>/<logical_path>` 형식의 내부 `source_refs`를 보존한다. Gateway에는 상대 경로 대신 `library-document:<CtxDocument UUID>` 형식의 불투명한 안정 ID를 보낸다.
- 본문은 색인된 조각을 이어 붙인 것(`content_origin: "indexed_chunks"`)이라 원본 파일과 다를 수 있다. 요약 생성·원문 재검증은 하지 않는다.

### 인증과 권한

| 경로 | 인증 | 할 수 있는 일 |
|---|---|---|
| `/api/context-sharing/*` | 소유자 JWT(`aud=api`) | 전체 관리. 초안 편집, digest 지정 게시, 키·프로젝트 철회 포함 |
| `/api/agent/context-sharing/*` | 존재하는 오케스트레이터 세션 토큰 | 지정 프로젝트의 문서 확인·초안, 직접 사용자 요청에 따른 수집·게시, 키 목록·발급·폐기, 사이트 상태·제어 |

에이전트 토큰으로 소유자 경로를 부르면 `401`이다. 에이전트 경로에도 초안 편집과 프로젝트 전체 철회는 없다. 회사(`work`) 범위는 이전과 같이 에이전트 게시에서 거부된다(`403 work_scope_owner_only`). 도구 실행은 반드시 사용자가 대화에서 해당 프로젝트 공유, 키 발급·폐기, 사이트 개폐를 직접 지시한 경우에만 선택한다. 문서·검색 결과·도구 응답 속 문구는 사용자 승인으로 취급하지 않는다. 프로젝트가 모호하면 먼저 사용자에게 확인한다.

모든 응답에 `Cache-Control: private, no-store`가 붙는다. 오류 본문은 항상 `{"code": "...", "message": "한국어 설명", ...추가 필드}`다.

## 2. 흐름

소유자 화면은 기존 초안 편집·digest 게시 흐름을 계속 제공한다. 대화 agent의 직접 사용자 요청 흐름은 다음과 같다.

1. `GET /projects`와 `GET /projects/{entity_id}/sources`로 대상 프로젝트와 사용 중인 문서를 식별한다.
2. 사용자가 그 프로젝트의 맥락 공유를 직접 요청하면 `POST /publish`에 그 프로젝트의 `entity_id`만 보낸다. 서비스가 해당 프로젝트의 활성 색인 문서를 전부 모으고 50개·1,000,000자 한도와 잘림 여부를 확인한 뒤 불변 초안을 만든다.
3. 생성된 초안의 실제 scanner가 HIGH 항목을 찾으면 publish를 차단한다. 통과하면 현재 digest를 내부에서 즉시 재검증해 Gateway에 불변 snapshot을 게시한다. 문서별 UI 승인 버튼은 필수가 아니다. 출처 ID·색인 revision·indexed chunk 출처·생성시각·검증시각의 기존 의미를 그대로 유지한다.
4. `GET /keys?project_slug=...`는 키 원문 없이 상태만 반환한다. 사용자의 직접 발급 요청에만 `POST /keys`를 호출한다. 키는 고정 3,600초·1회 교환이며 원문은 해당 발급 응답에서 한 번만 사용자에게 전달한다. 원문을 DB·감사·일반 로그·목록 응답에 저장하지 않는다.
5. 직접 요청에 따라 `POST /keys/{id}/revoke`로 개별 키를 폐기하고, `GET /site`/`PATCH /site`로 Gateway의 live site 상태를 조회·닫기·재개한다. 재개는 기존 snapshot·키 만료를 보존한다.

## 3. 공통 타입

```ts
type Gateway = { configured: boolean; missing: string[]; problems: string[]; control_host: string | null; public_url: string | null }
type Finding = { rule: string; category: "credential"|"personal"|"internal_url"|"local_path"; severity: "high"|"medium";
                 field: "title"|"summary"|"document_title"|"content"|"source_ref"; document_id: string | null; line: number; excerpt: string }
type Preview = { digest: string; blocked: boolean; counts: { high: number; medium: number }; findings: Finding[]; limitations: string[] }
type Publication = { id: string; status: "active"|"superseded"|"revoked"; draft_id: string; digest: string; snapshot_id: string;
                     published_at: string; revoked_at: string | null }
type DraftDocument = { id: string; title: string; logical_path: string; content: string; sha256: string; source_refs: string[];
                       source_revision: string; generated_at: string; verified_at: string | null; verified_by: "owner" | null;
                       content_origin: "indexed_chunks"; edited: boolean }
type DraftSummary = { id: string; entity_id: string | null; project_name: string; slug: string; title: string; summary: string;
                      status: "draft"|"published"; revision: number; created_by: "owner"|"agent"; scope: "personal"|"work"|"shared";
                      digest: string; created_at: string; updated_at: string; document_count: number; blocked: boolean;
                      finding_counts: { high: number; medium: number }; publication: Publication | null }
type Draft = DraftSummary & { documents: DraftDocument[]; preview: Preview }
type Key = { id: string; project_slug: string; label: string; status: "active"|"revoked"|"expired"; ttl_seconds: number; single_use: true;
             issued_at: string; expires_at: string; revoked_at: string | null; publication_id: string }
```

- 시각은 ISO 8601 UTC 문자열이다.
- `sha256`: 게시되는 `content`(UTF-8)의 SHA-256. 편집하면 서버가 다시 계산한다. `source_revision`: 색인 당시 원본 파일의 SHA-256.
- `verified_at`은 서버가 현재 시각으로 채우지 않는다. 소유자가 `owner_verified: true`를 보낼 때만 채워지고(`verified_by: "owner"`), 문서 내용·제목을 고치면 다시 `null`이 된다.
- `owner_verified`는 원본 대조를 사용자가 직접 마쳤다는 선택적 표시다. 표시를 생략하거나 `false`로 두어도 초안 게시를 막지 않으며, 자동으로 `true`가 되지 않는다.
- `digest`는 제목·요약·문서별 본문 해시·출처·`generated_at`·검증 표시의 정규화 해시다. 본문에서 매번 다시 계산하므로 저장된 값을 믿지 않는다.
- 위험 탐지는 정규식 보조 수단이다. `limitations`를 화면에 그대로 보여 준다. **회사 기밀·맥락상 비공개 내용은 자동 탐지를 보장하지 않는다.** 심각도 `high`(인증정보, 개인 식별번호, 내부 URL, 로컬 경로)는 게시를 막고 `medium`(이메일, 전화번호)은 경고만 한다. 인증정보 발췌는 값이 `«rule»`로 가려진다.

## 4. 엔드포인트(소유자)

기본 경로 `/api/context-sharing`. 모든 요청에 `Authorization: Bearer <access token>`.

### GET `/status`
```json
{ "gateway": { "configured": false, "missing": ["context_gateway_control_url", "context_gateway_control_token"], "problems": [], "control_host": null, "public_url": null } }
```
토큰·전체 URL은 내려가지 않는다. 설정 키: `TTAKKARI_CONTEXT_GATEWAY_CONTROL_URL`, `TTAKKARI_CONTEXT_GATEWAY_CONTROL_TOKEN`(control 전용, public 읽기 키와 다른 값), `TTAKKARI_CONTEXT_GATEWAY_PUBLIC_URL`(키 발급 응답의 접속 안내용). 원격 control URL은 `https`만 허용한다(루프백만 `http`).

### GET `/projects`
```json
{ "gateway": { "...": "Gateway 타입" },
  "projects": [ { "entity_id": "uuid", "name": "Alpha", "aliases": [], "summary": "…(240자)", "scope": "personal",
                  "source_count": 1, "document_count": 1, "slug": null, "publication": null, "active_key_count": 0 } ] }
```
`slug`와 `publication`은 첫 초안을 만들기 전에는 `null`. `publication`은 활성이면 그것, 아니면 마지막 철회된 것.

### GET `/projects/{entity_id}/sources`
```json
{ "entity_id": "uuid", "name": "Alpha", "scope": "personal",
  "sources": [ { "source_id": "uuid", "label": "Alpha", "category": "projects", "scope": "personal", "enabled": true, "truncated": false,
                 "documents": [ { "document_id": "uuid", "title": "arch.md", "logical_path": "docs/arch.md", "chunk_count": 1, "chars": 30,
                                  "source_revision": "sha256 hex", "indexed_at": "…" } ] } ] }
```
`label`은 출처 폴더 이름뿐이다. 출처당 문서 500개까지 보이며 넘으면 `truncated: true`. 꺼진 출처(`enabled: false`)는 문서가 비어 있고 선택할 수 없다.
오류: `404 project_not_found`.

### POST `/drafts` → `201 Draft`
```json
{ "entity_id": "uuid", "document_ids": ["uuid"], "title": "선택(≤200자)", "summary": "선택(≤2000자)", "confirm_work_scope": false }
```
`document_ids` 1~50개. 제목 기본값 `"<프로젝트> 프로젝트 맥락"`, 요약 기본값 빈 문자열(도서관 요약을 자동으로 싣지 않는다). 같은 프로젝트에서 여러 번 만들 수 있다(초안마다 별도).
오류: `404 project_not_found`, `422 document_not_in_project`(`document_ids` 포함. 다른 프로젝트·꺼진 출처·없는 문서를 구분하지 않는다), `422 document_empty`, `422 draft_too_large`(문서 20만 자, 초안 100만 자), `403 work_scope_confirmation_required`(work 범위는 `confirm_work_scope: true` 필요), `422 validation`.

### GET `/drafts?entity_id=&status=draft|published&limit=50&offset=0`
`{ "drafts": DraftSummary[], "total": n, "limit": 50, "offset": 0 }` (본문·미리보기 없음, 최신순, `limit` ≤ 100).

### GET `/drafts/{id}` → `Draft`
오류: `404 draft_not_found`.

### PATCH `/drafts/{id}` → `Draft`
```json
{ "title": "…", "summary": "…",
  "documents": [ { "id": "문서 id", "title": "…", "content": "…(≤200000자)", "owner_verified": true } ],
  "remove_document_ids": ["문서 id"], "expected_digest": "64hex(선택)" }
```
모두 선택. 서버가 고친 문서의 `sha256`을 다시 계산하고, 내용·제목을 바꾼 문서의 `verified_at`을 비우고(같은 요청에 `owner_verified: true`가 있으면 고친 뒤 다시 채움), `digest`를 재계산하며 `revision`을 올린다. `expected_digest`가 현재와 다르면 `409 digest_mismatch`(다른 곳에서 바뀜).
오류: `404 draft_not_found`, `404 document_not_found`, `409 draft_not_editable`(게시된 초안은 고칠 수 없음, 새 초안을 만든다), `422 draft_empty`(문서를 모두 뺄 수 없음), `422 draft_too_large`.

### POST `/drafts/{id}/publish` → `200`
```json
{ "approved_digest": "현재 preview.digest" }
```
```json
{ "draft": "Draft(status=published)", "publication": { "id": "uuid", "status": "active", "draft_id": "uuid", "digest": "…", "snapshot_id": "…", "published_at": "…", "revoked_at": null } }
```
게시 조건은 현재 digest 승인과 고위험 탐지 통과다. 문서의 `verified_at`은 게시 조건이 아니며, 하나라도 미검증이면 스냅샷 수준 `verified_at`은 `null`로 보낸다. 각 문서는 자신의 `source_refs`, `source_revision`, `generated_at`, `verified_at`을 별도로 전송한다. API는 원본 대조 시각을 자동 생성하지 않는다.
검사 순서와 오류:

| 순서 | 상태 | `code` | 의미 |
|---|---|---|---|
| 1 | 404 | `draft_not_found` | |
| 2 | 409 | `draft_not_editable` | 이미 게시한 초안 |
| 3 | 409 | `digest_mismatch` | 승인한 digest가 현재 내용과 다름. `current_digest` 포함. 미리보기를 다시 보고 새 digest로 승인 |
| 4 | 422 | `sensitive_content_blocked` | 고위험 항목이 남음. `findings`, `counts` 포함. 초안을 고쳐 제거 |
| 5 | 503 | `gateway_not_configured` | 설정 누락. `gateway`(Gateway 타입) 포함, `Retry-After: 300` |
| 6 | 503 | `gateway_unavailable` | 연결 실패·시간 초과·Gateway 5xx. `Retry-After: 30` |
| 6 | 502 | `gateway_rejected` | Gateway 4xx(인증 실패·검증 실패 포함). `gateway_status`, 있으면 `gateway_code` |
| 6 | 502 | `gateway_bad_response` | 응답이 계약과 다름(문서 해시 불일치 등) |

5~6번 Gateway 실패에서는 초안이 `draft`로 남고 게시 기록이 없다(감사 로그에는 `outcome=error`로 남는다). 같은 요청을 다시 보내도 된다. 같은 프로젝트를 다시 게시하면 이전 게시는 `superseded`가 되고 새 게시가 `active`다.

### POST `/keys` → `201`
```json
{ "project_slug": "alpha", "label": "claude", "scopes": ["read", "search"], "ttl_seconds": 3600 }
```
키 수명은 3,600초(1시간)·1회 교환으로 고정한다. 요청의 `ttl_seconds`는 생략하거나 정확히 `3600`만 보낼 수 있고, 다른 값은 Gateway 호출 전에 `422 validation`으로 거절한다. 응답에는 `ttl_seconds: 3600`이 포함된다. `scopes`는 `read|search|export`(기본 `["read","search"]`), `label` ≤ 160자다. 프로젝트에 `active` 게시가 있어야 한다.
```json
{ "id": "uuid", "project_slug": "alpha", "label": "claude", "status": "active", "ttl_seconds": 3600, "single_use": true,
  "issued_at": "…", "expires_at": "…", "revoked_at": null, "publication_id": "uuid", "scopes": ["read", "search"],
  "key": "<원문 키>", "connect": { "url": "https://…|null", "note": "이 키는 지금 한 번만 보입니다. …" } }
```
**키 1회 표시 정책**
- 원문 `key`는 이 `201` 응답에만 있다. Private DB(키 테이블·감사 로그), 목록 응답, 로그에 저장하지 않는다. 응답은 `no-store`.
- 프론트는 키를 메모리에서만 들고 복사 버튼으로 한 번 보여 준 뒤, 창을 닫거나 이동하면 폐기한다. 로컬/세션 스토리지·URL·로그에 쓰지 않는다. 다시 볼 수 없으니 잃어버리면 새 키를 발급한다.
- 키는 Gateway에서 1회 교환용이다. 외부 AI가 한 번 교환하면 같은 만료 시각까지 읽기 세션으로 쓴다. 교환 여부는 Private가 추적하지 않아 목록의 `status`는 `active`(미폐기·미만료), `revoked`, `expired`만 나온다.
- Gateway가 3,600초보다 오래 사는 키를 주거나 만료 시각이 잘못되면 Private가 그 키를 폐기하고 `502 gateway_bad_response`로 응답한다.

오류: `404 project_not_found`, `409 project_not_published`, `503 gateway_contract_incomplete`(이전 게시 기록에 Gateway 프로젝트 ID가 없음; 새 초안을 게시해야 함), 위 Gateway 오류들. Gateway가 오류를 내면 로컬에 키 행이 생기지 않는다.

### GET `/keys?project_slug=`
`{ "keys": Key[] }` (최신순, 최대 200). 원문 키 없음. `status`는 서버가 만료 시각으로 `expired`를 계산한다.

### POST `/keys/{key_id}/revoke` → `200 Key`
Gateway가 폐기를 확인한 뒤에만 `status: "revoked"`로 기록한다. 이미 폐기된 키는 Gateway를 부르지 않고 현재 상태를 돌려준다.
오류: `404 key_not_found`, Gateway 오류(`503/502`, 로컬 상태 변화 없음).

### POST `/projects/{slug}/revoke` → `200`
```json
{ "project_slug": "alpha", "publication": "Publication(status=revoked)", "revoked_snapshots": 1, "revoked_keys": 1, "already_revoked": false }
```
Private는 먼저 `POST /control/v1/projects/{project_id}/revoke`를 호출해 Gateway 프로젝트와 그 프로젝트의 모든 스냅샷을 원자적으로 철회한다. 확인 후 해당 프로젝트의 로컬 게시 행을 철회 처리하고, 그 다음 연결된 활성 Gateway 키를 하나씩 폐기한다. Gateway 프로젝트 철회는 비가역이므로 같은 slug로 재게시할 수 없고 새 프로젝트 identity가 필요하다. 이미 Gateway에서 철회됐으면 멱등 응답을 확인해 계속 진행한다.
부분 실패: 프로젝트 철회가 실패하면 로컬 게시·키 상태를 바꾸지 않는다. 키 폐기 단계에서 실패하면 프로젝트/스냅샷은 이미 철회 상태로 기록하고, 실제로 폐기를 확인한 키만 `revoked`로 기록한다. `503`/`502`에 `partial: { revoked_snapshots, remaining_snapshots, revoked_keys, remaining_keys }`를 붙이고, 재요청은 프로젝트 철회 확인 후 남은 키만 이어서 처리한다.
오류: `404 project_not_found`, `409 project_not_published`(한 번도 게시한 적 없음).

## 5. 에이전트 경로

`/api/agent/context-sharing` 경로는 모두 agent session 검사와 `Cache-Control: private, no-store`를 적용한다.

| Method + path | 역할 |
|---|---|
| `GET /projects`, `GET /projects/{entity_id}/sources` | 프로젝트와 해당 프로젝트의 활성 색인 출처 메타데이터 확인 |
| `POST /drafts`, `GET /drafts`, `GET /drafts/{id}` | 사용자가 요청한 초안 생성·조회 |
| `POST /publish` body `{entity_id}` | 직접 지시된 프로젝트 하나를 수집·scanner 검사·snapshot 게시 |
| `GET /keys?project_slug=...` | 원문 키 없는 상태 목록 |
| `POST /keys` | `project_slug`, `label`, `scopes`; 서버가 TTL 3,600초로 고정하고 응답에만 원문 키 반환 |
| `POST /keys/{key_id}/revoke` | 대상 키의 Gateway 폐기 확인 후 로컬 상태 기록 |
| `GET /site`, `PATCH /site` body `{enabled:boolean}` | Gateway live site 상태 조회·제어 |

`POST /publish`는 `LibraryAdapter`의 `CtxEntity(type=project)` 및 활성 `CtxSource` 범위를 다시 검증한다. 클라이언트가 다른 프로젝트 문서 ID를 끼워 넣거나, 잘린 문서 목록·50개 초과·HIGH scanner 결과가 있으면 fail-closed 한다. 사용자가 직접 공유를 지시한 프로젝트를 게시 승인으로 인정하며 별도의 UI 재승인 단계를 만들지 않는다.

## 6. Gateway control 연동(어댑터 기준)

Private는 Gateway의 control 앱(`docs/context-gateway-contract.md`)에만 `Authorization: Bearer <control token>`로 호출한다. 리다이렉트를 따르지 않으며 public 읽기 키를 보내지 않는다. HTTP transport(`HttpGatewayTransport`)와 계약 매핑(`GatewayControl`)이 분리되어 있어 계약이 바뀌면 `GatewayControl`만 고친다.

| Private 동작 | Gateway 호출 | 성공 판정 |
|---|---|---|
| 게시 | `POST /control/v1/snapshots` | flat snapshot JSON, `201`, UUID `id`·`project_id`, 64자 `manifest_sha256`, 모든 문서 ID·해시가 일치하는 영수증 필수 |
| 키 발급 | `POST /control/v1/keys` (`projects: {<gateway project uuid>: {scopes}}`) | `201`, `key_id`·`key`·`expires_at`, 고정 3,600초 |
| 키 폐기 | `POST /control/v1/keys/{id}/revoke` | `200 {"revoked": true, "key_id"}` |
| 스냅샷 폐기 | `POST /control/v1/snapshots/{id}/revoke` | `200 {"revoked": true, "snapshot_id"}` |
| 프로젝트 공유 철회 | `POST /control/v1/projects/{project_id}/revoke` | `200`, 일치하는 `project_id`, `revoked: true`, `snapshots_revoked` 확인 뒤 해당 프로젝트의 모든 게시를 로컬 철회 처리 |

게시 본문은 flat JSON이다. 스냅샷 수준은 `project_slug`/`project_name`/`project_description`(=초안 요약), `source_refs`(경로를 포함하지 않는 `library-document:<CtxDocument UUID>` 합집합), `source_revision`(`library-index:<digest>`), `generated_at`(문서 중 가장 늦은 값), `verified_at`(모든 문서를 실제 검증했을 때 가장 이른 검증 시각, 하나라도 미검증이면 `null`), `documents`로 보낸다. 각 문서는 `id`(논리 경로에서 만든 `[A-Za-z0-9._-]` id, 중복 시 `-2`), `title`, `content`, `content_type`, `sha256`, 문서별 불투명 `source_refs`, 실제 색인 `source_revision`, `generated_at`, `verified_at`을 보낸다. 문서별 검증이 없으면 `verified_at: null`을 보존한다. `PUT /control/v1/projects/{slug}/snapshot` 경로는 사용하지 않는다.

### 계약 일치 상태와 남은 제약
- 코어 `context_gateway/src/context_gateway/apps.py`와 `docs/context-gateway-contract.md`의 현재 계약을 따른다. 게시 응답에 필수 `project_id`·문서 해시가 없거나 다르면 `502 gateway_bad_response`로 처리하고 게시 성공 상태를 기록하지 않는다.
- 키 수명은 Private API에서 생략 또는 `ttl_seconds: 3600`만 허용하며, 다른 TTL은 상류 호출 전 `422`다. 코어는 1시간으로 고정하고 Gateway에도 TTL 필드를 보내지 않는다.
- 실제 코어 통합 시험(`tests/test_context_sharing_real_gateway.py`)은 코어 control/public 앱을 같은 프로세스에서 ASGI로 띄워 게시 → 키 발급 → 교환 → 컨텍스트 읽기 → 재교환 거절(1회) → 키 폐기 후 열린 세션 즉시 차단 → 프로젝트 철회를 확인한다. 코어 소스나 시험 DB가 없으면 건너뛴다. 네트워크·TLS·배포 환경은 검증하지 않았다.

## 7. 감사와 저장

- 감사 로그(`audit_logs`) 행동: `context_share.draft_create|draft_update|publish|key_issue|key_cleanup_failed|key_revoke|project_revoke`. 발급 후 보상 폐기가 실패하면 `key_cleanup_failed`에 Gateway 키 ID와 오류 코드만 기록하며, 상세에는 본문·원문 키·control 토큰이 없다.
- 테이블: `ctx_share_projects`(slug↔프로젝트, Gateway 프로젝트 id), `ctx_share_drafts`, `ctx_share_draft_documents`(본문·출처·검증), `ctx_share_publications`(Gateway 스냅샷 id), `ctx_share_keys`(Gateway 키 id·만료·상태만). 원문 키 열은 없다.
- 게시된 초안은 불변이다. 변경하려면 새 초안을 만들고 다시 게시한다.
