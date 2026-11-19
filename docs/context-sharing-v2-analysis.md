# Context sharing v2 조사 및 최소 계약 제안

조사일: 2026-11-19 (Asia/Seoul)
기준: `origin/main` = `978cb5ace5115743b32f375ce065120e8d068d82`
작업 브랜치/worktree: `back-context-sharing-v2-20261119` / `../back-context-sharing-v2-20261119`
범위: Private Publisher와 독립 Context Gateway의 정적 경로 및 합성 데이터 테스트. 서비스 구현, 운영 비밀·DB·실제 문서, 공개 설정, 배포는 변경하지 않았다.

## 결론

공유 URL과 프로그램 자격증명은 같은 `single_use` 플래그로 확장하지 말고 서로 다른 발급 계약으로 둔다. 기존 키 경로는 `single_use_v1` 의미를 보존하고, 새 브라우저용 `share_link_v2`와 반복 교환 가능한 `reusable_api_v2`를 명시적으로 추가한다. 기존 API key의 만료 1시간 및 1회 교환 동작은 기본값을 바꾸지 않는다.

현재 구현에는 v2 요구의 기반이 이미 있다: Gateway 키 생성은 32바이트 CSPRNG와 SHA-256 해시 저장을 사용하고, 세션도 해시로 저장한다. Gateway는 새 스냅샷을 불변 버전으로 추가하며, 이전 스냅샷을 철회하지 않는 한 보존한다. 그러나 새 자격증명 만료 선택, 반복 교환, pinned/latest 선택, 마지막 사용, 링크 발급 경로는 아직 없다. 특히 v1 키의 Private `publication_id`는 Gateway 권한을 해당 게시물에 고정하지 않는다.

최신 자동 접근은 **새 자격증명에서는 기본 비활성화**하고 `snapshot_policy: "latest"`를 발급자가 명시한 경우에만 허용한다. 기본 `pinned`는 발급 당시 활성 게시의 snapshot ID에 고정한다. 기존 v1은 호환성을 위해 현재처럼 프로젝트의 latest를 읽는 의미를 유지하고 표시값으로 타입을 분명히 한다.

회사(`work`) 자료는 `confirm_work_scope` 체크 하나만으로 게시 허용으로 간주하지 않는 정책을 권한다. 이 필드는 지금도 요청자가 true를 보낼 수 있는 확인값이며 권한 증명이나 문서별 검토 기록은 아니다. v2에서는 회사 범위를 기본 차단하고, 별도 소유자 정책 허용과 게시 digest에 묶인 실제 내용 검토 증거가 모두 있는 경우만 게시하도록 계약을 정해야 한다. 정규식 scanner 통과는 그 증거를 대신하지 않는다.

## 현재 구현과 단회 교환 의미

| 구간 | 현재 코드 동작 | 근거 |
|---|---|---|
| Private 발급 | `/api/context-sharing/keys`는 활성 publication이 있어야 Gateway control에 키를 발급한다. TTL은 `KEY_TTL_SECONDS = 3600`으로 고정, API 요청에서 임의 TTL을 받지 않는다. 응답 `single_use: true`는 `key_out()`이 매번 고정해 넣는 메타데이터다. 원문은 발급 응답만 반환하고 Private DB·감사 로그에는 저장하지 않는다. | `src/ttakkari/context_sharing/service.py:37-40,515-583`; `src/ttakkari/api/context_sharing.py:250-264`; `src/ttakkari/context_sharing/models.py:90-103` |
| Gateway 키 생성 | `issue_key()`는 `secrets.token_bytes(32)`를 URL-safe Base64로 인코딩하고 SHA-256만 `gateway_api_keys.credential_hash`에 저장한다. 만료는 현재 1시간이다. | `context_gateway/src/context_gateway/auth.py:17-18,32-47`; `models.py:62-71`; `publishing.py:268-302` |
| 교환 | `POST /api/v1/auth/exchange`에서 키 행의 `consumed_at IS NULL`, 미폐기, 미만료 조건을 단일 UPDATE의 `RETURNING`으로 선점한다. 성공한 교환만 1회 허용하며 세션 토큰도 32바이트 난수와 해시 저장이다. | `context_gateway/src/context_gateway/apps.py:389-400`; `auth.py:56-96`; `models.py:86-96` |
| 세션 사용 | 세션 수명은 원 키 만료를 넘지 않고, 별도 설정이 없으므로 현재 최장 1시간이다. REST/MCP 요청은 세션·부모 키의 만료/철회 및 프로젝트 ACL을 다시 검사한다. 키 철회 또는 프로젝트 공유 철회는 이미 발급된 세션에도 다음 요청부터 반영된다. | `auth.py:83-96,99-140,143-162`; `apps.py:243-268,585-600` |
| 실제 의미 | `single_use`는 **각 API 요청 1회**도, 외부 세션 전체 1회도 제한하지 않는다. 제한하는 것은 원 발급 키의 **세션 교환 횟수 1회**다. 발급된 session bearer는 허용 scope 안에서 반복 사용된다. | 위 함수 경로와 `tests/test_context_sharing_real_gateway.py:153-177` |

따라서 `single_use: false`만 추가하면 계약이 모호하다. 재사용 대상이 원 키 재교환인지, 세션의 API 호출인지 구분해야 한다. 제안은 `reusable_api_v2`가 **같은 API key로 여러 번 세션 교환**할 수 있게 하고, API 데이터 요청은 기존처럼 session bearer를 쓰게 하는 것이다. 이렇게 하면 원문 키를 모든 API 요청에 반복 전송하지 않고, 키 폐기 시 기존 세션도 즉시 차단하는 현재 구조를 최대한 재사용할 수 있다.

## 승인, digest, scanner, 게시 및 철회

- 소유자 경로는 초안 작성 후 현재 본문에서 digest를 재계산하고, publish 시 `approved_digest`를 잠금 상태에서 다시 비교한다. HIGH scanner finding이 있으면 게시하지 않는다. 편집하면 문서 해시와 draft digest를 다시 만들며, 내용/제목 변경 시 `verified_at`을 해제한다. `verified_at` 자체는 현재 게시의 필수 조건이 아니다. (`service.py:62-93,469-510`; API route `context_sharing.py:240-248`)
- Gateway는 Publisher의 snapshot POST만 받고 원본 도서관을 직접 읽지 않는다. 프로젝트 slug, source revision, 생성·검증 시각, 문서별 SHA-256과 참조를 manifest에 넣는다. 파일은 stage 디렉터리에 작성 후 최종 경로로 rename하고, PostgreSQL transaction에서 snapshot row와 latest pointer를 함께 커밋한다. DB commit 실패 때 새 디렉터리를 제거한다. snapshot은 DB trigger로 불변이며 revoke만 가능하다. (`context_gateway/src/context_gateway/publishing.py:55-191`; `apps.py:142-210`)
- 새 publish는 새 snapshot을 만들고 Gateway `latest_snapshot_id`를 바꾼다. 이전 snapshot은 자동 철회하지 않는다. Private는 이전 active publication을 `superseded`로 표시한다. Gateway snapshot을 따로 revoke하면 latest였던 경우 남은 최신 active snapshot으로 포인터를 되돌린다. 프로젝트 전체 revoke는 모든 snapshot을 한 DB transaction에서 철회하고 latest 포인터를 비운다. (`publishing.py:121-184,200-265`; Private `service.py:495-509,615-654`)
- Private project revoke는 Gateway의 프로젝트 전체 철회를 먼저 확인하고 publication 로컬 상태를 기록한 다음 키를 하나씩 폐기한다. 키 단계에서 실패하면 성공 수/잔여 수를 돌려주고 다음 요청에서 이어간다. Gateway project revoke가 실패하면 로컬 게시·키를 먼저 철회 처리하지 않는다. 단, 완료된 Gateway 철회 뒤 Private DB commit이 실패하는 교차 서비스 불확정 상황은 별도 보상/대사 계약이 필요하다. (`service.py:615-654`; `gateway.py` control receipt 검증)
- 전역 site switch는 Gateway PostgreSQL의 내구 상태다. public 앱은 인증된 `/api/v1/*`와 `/mcp*` 요청을 데이터 읽기 전에 검사하고 닫혀 있으면 `503`/`Retry-After: 60`을 반환한다. `/api/v1/site` 상태 확인은 예외이며 데이터 응답은 아니다. 미리 열린 세션과 export도 차단하고 재개할 때 기존 TTL/snapshot을 바꾸지 않는다. 상태 행이 없으면 닫힌 것으로 본다. control 앱만 별도 control token으로 변경할 수 있고 public DB role에는 site switch 쓰기 권한이 없다. (`apps.py:185-214,343-363,699-730`; parity test 아래)
- scanner는 regex 목록이다. 자격증명, 주민번호 형태, 내부 URL, 로컬 경로는 HIGH 차단, 이메일/한국 전화번호는 MEDIUM 경고다. 회사 기밀이나 문맥상 비공개 내용 전부를 탐지하지 못한다고 코드와 기존 계약이 명시한다. (`src/ttakkari/context_sharing/scanner.py:1-2,33-57,82-100`; Private contract §3)

### 장애·정합성 경계

1. Gateway 게시의 파일/DB 내부 실패는 새 파일을 정리하고 transaction을 rollback한다. 응답 유실 또는 Private의 게시 후 로컬 commit 실패는 별개다. 현재 두 서비스 사이에 idempotency/receipt reconciliation이 없다. 특히 Gateway publish가 이미 latest를 바꾼 다음 Private 기록이 실패하면, 기존 **project-wide** key grant가 새 snapshot을 읽을 수 있지만 Private에는 publication이 없는 상태가 가능하다. 이는 코드 순서에서 도출한 위험(inferred)이며 강제 장애 테스트로 재현하지 않았다.
2. Private 키 발급은 Gateway 성공 뒤 만료·응답을 검사하고 Private commit 실패 시 Gateway 키를 보상 폐기한다. 보상 폐기도 실패하면 키 ID와 오류 코드만 감사 로그에 기록한다. (`service.py:413-420,527-583`)
3. 원격 Gateway 장애/5xx는 게시 초안 상태를 draft로 유지하고 재시도 가능한 오류로 돌린다. 철회도 Gateway 확인 전에 Private를 성공 상태로 기록하지 않는다. (`service.py:387-410,469-510,594-654`)
4. 파일/manifest 불일치나 누락은 읽기에서 예외로 끝나고 데이터 본문을 정상 반환하지 않는다. REST `IntegrityError`는 `409 snapshot integrity check failed`로 내려간다. (`snapshot_service.py:37-84`; `apps.py:385-387`)

## Context Gateway 구조와 운영 경계

`context_gateway/`는 Private 애플리케이션을 import하지 않는 별도 Python distribution이다(`context_gateway/src/context_gateway/__init__.py`, package boundary test). Public ASGI에는 REST와 Streamable HTTP MCP가 같이 있고, 별도 Control ASGI는 publish/key/site 관리 API만 제공한다. `control.py`는 `create_control_app()`, `public.py`는 `create_public_app()`을 노출한다. 웹 UI는 `web_public.py`가 public app을 static wrapper로 감싼다. UI는 현재 key를 교환해 메모리 전용 session store에 넣으며 URL/query/local storage에 보관하지 않는다. 별도 share-link 입력/fragment 교환 흐름은 코드에서 확인하지 못했다. (`context_gateway/web/src/auth/sessionStore.ts`; `web_serving.py:27-119`)

| 구성 | 현재 경계 |
|---|---|
| Public REST | `/api/v1/auth/exchange`, projects/snapshots/context/documents/search/export. Bearer session과 프로젝트 scope 필요. Read/search/export를 별도 scope로 검사한다. |
| Public MCP | `/mcp`, `list_projects`, `get_project_context`, `search_project_context`, `get_document`. REST와 같은 인증/ACL/snapshot reader를 사용한다. Export MCP tool은 현재 없다. |
| Control REST | 별도 process/port, `/control/v1/snapshots`, snapshot/key/project revoke, key issue, `/control/v1/site`. `Authorization: Bearer control_token`; OpenAPI/docs 비활성화. Public app에 mount되지 않는다. |
| DB와 파일 | Gateway 전용 PostgreSQL. Admin role은 control에, 제한된 public role은 public app에만 전달된다. Snapshot directory를 control에는 writable, public에는 read-only로 mount한다. Gateway 자체는 독립 DB schema를 runtime `Base.metadata.create_all` 및 additive DDL로 준비하며 Private Alembic을 사용하지 않는다. |
| Private DB/API | Private Alembic `0079_context_sharing`이 `ctx_share_projects`, drafts/documents, publications, `ctx_share_keys`를 만든다. `/api/context-sharing`은 owner JWT, `/api/agent/context-sharing`은 agent session 검사를 쓴다. MCP 관리 도구는 agent API를 호출한다. |
| 배포 | `deploy/context-gateway/compose.yaml`의 db/control/public 3개 서비스. 로컬 예시는 loopback host bind, 내부 network, read-only runtime, no-new-privileges와 public snapshot read-only mount. Homeserver override는 내부 bridge `172.31.0.0/24`, host port 비노출, release SHA image tag 및 release 밖 영속 state bind mount를 쓴다. Private Publisher는 별도 Ttakkari backend에서 control URL/token으로 연결한다. |

관련 파일: `context_gateway/src/context_gateway/{apps,auth,control,database,models,publishing,snapshot_service,web_public,web_serving}.py`, `context_gateway/web/src/{api,auth,App.tsx}`, `deploy/context-gateway/{compose.yaml,compose.homeserver.yaml}`, `alembic/versions/0079_context_sharing.py`, `docs/context-gateway-contract.md`, `docs/context-sharing-private-contract.md`, `docs/context-gateway-web-deploy.md`.

## 원 요구와 현재 간극

| 요구 | 현재 | 판단 |
|---|---|---|
| 익명 공유 링크와 프로그램 API key 분리 | context-sharing API는 Gateway 교환 키 관리만 제공. Gateway 웹 앱은 사용자가 키를 입력해 session을 만들지만 Private에서 링크 발급은 없음. Ttakkari `PublicShare`는 회의 자료 DTO용 capability이고 Gateway 프로젝트 context와 다른 데이터 계약이다. | 링크 전용 발급·교환 타입 필요. 기존 PublicShare 테이블을 프로젝트 문서 공개에 직접 재사용하지 않는다. |
| 기본 3일, 허용 1/3/7/30일 | Gateway와 Private 키 모두 1시간 고정. | 새 v2 type만 네 기간 허용. v1은 1시간 고정 보존. |
| 이름·마지막 사용·폐기 | label과 폐기는 있으나 `last_used_at`은 Private/Gateway key schema에 없다. Gateway audit event는 요청별 기록하지만 목록에 마지막 시각을 제공하지 않는다. | Gateway에서 성공한 exchange/API/MCP 사용 시 key `last_used_at` 저장; control read/list로 Private 관리 화면에 노출. |
| pinned/latest, 최신 자동 접근을 명시적으로 선택 | 모든 Gateway ACL이 project ID 기준. REST/MCP snapshot 생략 시 project.latest를 사용. Private ShareKey `publication_id`는 표시/관리 연결이며 Gateway ACL에는 snapshot 제한이 없다. | v2 기본 pinned, 명시적 latest만 프로젝트의 최신 approved snapshot으로 동적 해석. 기존 v1은 과거 동작 보존. |
| 링크 token 256-bit, 해시 저장, 교환, 로그/외부 자원 누출 방어 | Gateway issue key와 Ttakkari PublicShare 모두 32-byte 계열 CSPRNG/해시 저장 기반이 있음. Gateway UI는 session을 메모리에만 보관하고 웹 응답에 no-store/no-referrer/CSP를 적용. 그러나 link 발급/fragment 처리와 URL 정리 흐름은 없음. | 기존 primitives 재사용. 링크 secret은 fragment에만 최초 전달하고 앱 첫 실행 시 교환 후 `history.replaceState`로 즉시 제거. query/path/redirect/referrer/3rd-party resource/access log에 raw secret 두지 않음. |
| 세션도 링크/키 만료·폐기 즉시 확인 | session이 부모 key row에 연결되어 모든 REST 인증에서 부모 key를 재확인한다. 프로젝트 revoke도 ACL 필터로 차단. | 현재 구조 유지. `share_link_v2` revoke도 부모 credential 철회와 연결해 열린 session을 다음 요청에 즉시 무효화. |
| read/search/export 분리와 REST/MCP parity | Scope가 이미 분리됨. REST에는 export, MCP에는 export tool이 없다. 현재 parity test는 context/read/search와 ACL을 비교한다. | 새 자격 유형에도 scope 동일 적용; export를 MCP에 추가하거나 MCP에서 제공하지 않는다는 정책을 계약에 명시하고 parity matrix에 export 거부/성공을 보강. 권장: export scope가 있으면 REST/MCP 모두 지원. |
| 새 게시 때 이전 승인 데이터 보존 | Gateway는 immutable snapshot을 추가하고 구 snapshot은 revoke 전까지 남긴다. Private는 이전 publication을 `superseded`로만 표시. | 이 동작을 유지. pinned v2 자격은 새 게시에도 이전 snapshot을 읽고, latest 선택 자격만 새 approved snapshot을 읽음. |
| 회사 자료는 체크박스만으로 외부 공개하지 않음 | `/api/agent` draft/publish는 `confirm_work_scope: true`를 받으며 tool 설명은 직접 사용자 요청을 요구하지만 API에는 발화 증명/문서별 policy approval이 없다. Scanner는 문맥상 비공개를 보장 탐지하지 못함. | v2 work scope는 별도 owner policy + digest-bound review evidence를 요구하거나 기본 거부. 체크 값과 scanner 결과는 승인 증명으로 취급하지 않음. |

## v2 최소 데이터/API 계약 제안

### 타입/저장

1. 기존 `ctx_share_keys`를 즉시 재작성하지 않고 additive migration으로 `credential_type`, `snapshot_policy`, `pinned_snapshot_id`, `scopes`, `last_used_at`을 더한다. 현재 행은 모두 `credential_type='single_use_v1'`, 현재 기존 latest 동작에 맞는 `snapshot_policy='latest'`로 backfill한다. 원문 secret은 계속 저장하지 않는다. 이름(label), 만료, 폐기, Gateway key ID는 기존 열을 쓴다.
2. Gateway `gateway_api_keys`에 `credential_type`/교환 정책과 `last_used_at`을 추가하고 `gateway_key_acls`에 `snapshot_policy`와 nullable `snapshot_id`를 추가한다. 기존 행은 `single_use_v1` + project latest 동작으로 backfill한다. snapshot ID는 같은 project 소속·active 상태를 FK 또는 발급 시 검증한다. `gateway_sessions.key_id` 연결을 유지해 매 요청 부모 자격과 ACL을 재검증한다.
3. `single_use_v1`은 1회 exchange, 3600초 TTL, project/latest ACL을 그대로 보존한다. `reusable_api_v2`는 1/3/7/30일 만료와 반복 exchange를 허용한다. `share_link_v2` token도 만료·철회 전까지 반복 exchange할 수 있어 여러 방문자/재방문이 가능해야 한다. 링크는 단일 프로젝트·`read` 전용이며 browser용 URL capability다. API key는 programmatic client와 개별 `read|search|export` scopes용이다. 둘 다 session을 발급하지만 UI/API 관리 흐름·scope·audience가 분리된다. 모두 secret은 32바이트 이상 CSPRNG, SHA-256 hash at rest. 출력의 `credential_type`은 필수로 하고 기존 `single_use` 필드는 하위 호환 기간 동안만 파생값으로 응답한다.
4. last use는 successful exchange와 successful authorized data/MCP request 시각을 Gateway가 관리한다. Private key list는 해당 값을 Gateway control batch/status read로 가져온다. Gateway가 불능이면 목록의 다른 메타데이터를 last-used `null/stale`로 표시하거나 503을 돌려, 값을 현재로 가장하지 않는다.

### API shape

기존 Private/agent route를 유지하고 요청 필드를 additive로 더한다. 누락된 `credential_type`은 기존 `single_use_v1`로 해석한다. 새 경로는 분리한다.

```http
POST /api/context-sharing/keys
POST /api/agent/context-sharing/keys
```

기존 호환 요청은 현재대로 둔다. 새 요청 예:

```json
{
  "project_slug": "alpha",
  "label": "nightly-indexer",
  "credential_type": "reusable_api_v2",
  "ttl_days": 3,
  "snapshot_policy": "pinned",
  "scopes": ["read", "search"]
}
```

`ttl_days` 허용값은 1, 3, 7, 30이며 생략은 새 타입에서 3일이다. `snapshot_policy` 허용값은 `pinned`/`latest`; 새 타입은 생략 시 pinned. pinned는 현재 active publication의 Gateway snapshot UUID를 grant에 넣고 요청자가 다른 `snapshot_id`를 고르면 404한다. latest는 명시 선택 시 project latest를 사용한다. API 목록은 `id`, `label`, `credential_type`, `scopes`, `snapshot_policy`, nullable `pinned_snapshot_id`, `issued_at`, `expires_at`, nullable `last_used_at`, `status`, `revoked_at`을 준다. 원문 key는 발급 응답 한 번에만 포함하고 `Cache-Control: private, no-store`를 유지한다.

```http
POST /api/context-sharing/links
POST /api/agent/context-sharing/links
```

body는 `project_slug`, `label`, 선택적 `ttl_days`, 선택적 `snapshot_policy`다. 성공 시 링크 ID/label/정책/만료와 `url` 및 `key`를 발급 응답에서만 반환한다. 링크는 기본 pinned, read-only, 3일이며, 같은 link token으로 만료/철회 전까지 재교환 가능하다. 각 browser session은 Gateway가 다시 만료·철회 검사를 하고, 링크 revoke 시 다음 요청부터 기존 session도 차단한다. GET 목록은 raw URL/key를 제외하고 관리 메타데이터만 반환한다. `POST /links/{id}/revoke`는 Gateway parent credential을 revoke하고 성공 영수증 후 Private status를 바꾼다. API key와 link 목록/폐기 경로는 분리해 실수로 유형을 바꿔 발급하지 않게 한다.

브라우저 URL은 `https://share.../link/{opaque-id}#key=<secret>` 형태로 secret을 fragment에만 싣는다. 페이지는 external resource를 로드하지 않고, 첫 앱 단계에서 fragment key를 Authorization header로 exchange한 후 주소를 정리하고 메모리 session만 유지한다. server access log와 `Referer`에는 fragment가 전송되지 않지만, 브라우저 history 제거·CSP·no-store·no-referrer는 함께 적용한다. URL을 query/path에 넣거나 third-party script/font/image를 두지 않는다. exchange 응답에는 session expiry, `Cache-Control: no-store`가 있어야 한다.

Gateway public REST/MCP는 공통 authorization resolver에 project, scope, snapshot policy를 넘긴다. REST의 context/document/search/export와 MCP의 동일 기능은 같은 pinned/latest resolution을 써야 한다. 링크는 UI 제한과 별개로 Gateway에서 `read`만 강제한다. 만료/철회된 credential, parent key, project, snapshot은 REST·MCP 둘 다 같은 fail-closed 오류로 거절한다.

### 게시/철회/장애 규칙

- 발급 당시 `pinned`는 gateway receipt의 snapshot ID와 publication ID를 함께 저장한다. `latest`는 발급 시점에 선택값을 감사에 기록하고, 새 snapshot이 승인 게시된 뒤부터 그 버전을 사용한다.
- 재게시 실패는 이전 approved snapshot과 모든 pinned credential을 보존한다. latest pointer는 Gateway publish transaction이 성공했을 때만 이동한다. Private와 Gateway commit 사이의 응답 유실/Private commit 실패를 복구하기 위해 publish 요청에 Private `operation_id`를 보내고 Gateway에서 unique/idempotent receipt로 조회 가능하게 한다. 이 receipt 기능이 없으면 부분 성공을 자동 복구할 수 없다고 API가 알려야 한다.
- 개별 credential revoke는 Gateway에서 부모 key를 먼저 revoke한다. 성공이 확인된 뒤 Private status를 갱신한다. 따라서 열린 session도 다음 REST/MCP 요청에서 차단된다. Gateway 불능이면 로컬을 성공 표시하지 않는다.
- snapshot revoke는 그 pinned credential 및 latest resolution에서 즉시 제외한다. 프로젝트 revoke는 기존대로 모든 snapshot/자격을 차단한다. site kill switch는 REST/MCP·exchange·export를 포함해 인증 데이터 요청에 일괄 적용하고, 재개 시 TTL을 늘리지 않는다.
- audit에는 actor, key/link ID, credential_type, scope, snapshot policy/ID, expiry, operation ID, status만 저장한다. raw link/key/session/control token, URL fragment, 문서 본문을 기록하지 않는다.

## 최소 변경 대상 파일

### Private Publisher

- `src/ttakkari/context_sharing/models.py`, 새 `alembic/versions/0080_context_sharing_credentials_v2.py`: metadata와 backfill.
- `src/ttakkari/context_sharing/service.py`, `gateway.py`, `src/ttakkari/api/context_sharing.py`: v1 compatibility, v2 validation/issuance/list/revoke, receipt checks.
- `src/ttakkari/orchestrator/mcp_server.py`, `tool_policy.py`, `src/ttakkari/api/assistant.py`: agent management tools와 REST/MCP parity.
- `tests/test_context_sharing_api.py`, `tests/test_context_sharing_mcp.py`, `tests/test_context_sharing_real_gateway.py`, migration tests, scanner tests: default/allowed TTL, v1 regression, link secrecy, pinned/latest, revocation of open sessions, work policy, failure reconciliation.

### Context Gateway

- `context_gateway/src/context_gateway/models.py`, `auth.py`, `publishing.py`, `snapshot_service.py`, `apps.py`: additive credential/ACL fields, repeated exchange, per-request last use, snapshot resolver, control status/idempotency, REST/MCP parity.
- `context_gateway/web/src/api/{client,types,paths}.ts`, `auth/sessionStore.ts`, `App.tsx`, `ui/LoginScreen.tsx` and focused tests: fragment exchange/URL scrub and memory-only session.
- `context_gateway/tests/test_key_exchange.py`, `test_rest_mcp_parity.py`, `test_snapshot_integrity.py`, `test_package_contract.py`; `docs/context-gateway-contract.md`, `docs/context-sharing-private-contract.md`, `docs/context-gateway-web-deploy.md`.
- Runtime Gateway schema is not Alembic-managed. Existing database upgrade needs explicit additive DDL in the current initialization/migration path; relying on `Base.metadata.create_all()` alone will not add columns to existing tables. Private data migration remains a new Alembic revision.
- 기존 서비스와 같은 release/build 흐름을 사용한다. 별도 새 Gateway는 추가하지 않는다. Homeserver deployment는 full image SHA, 외부 persistent state mount 및 기존 private/public 분리 경계를 유지한다. `docs/context-gateway-web-deploy.md`는 2026-11-16의 배포 tag를 기록하지만, 본 조사는 현재 운영 revision/상태를 조회하지 않았다.

## 기존 재사용 확인 (search-first)

- Gateway 내부: 기존 FastAPI/Pydantic/SQLAlchemy, `secrets.token_bytes(32)`, SHA-256 credential hashing, `hmac.compare_digest`, DB-backed key/session revocation, scope ACL, immutable snapshot reader/manifest, Streamable HTTP MCP를 확장한다. 새 암호 라이브러리나 새 서비스가 필요한 근거는 없다.
- Private 내부: 기존 draft digest/scanner/Gateway control adapter, owner/agent auth, no-store response, 감사 기록과 Gateway cleanup 보상 흐름을 확장한다.
- 인접 구현: `src/ttakkari/sharing/service.py:202-230,271-312`의 회의용 PublicShare는 `secrets.token_urlsafe(32)`, SHA-256 저장, raw secret을 URL fragment로만 1회 돌려주는 사례다. 그러나 capability는 회의 DTO에 직접 접근하며 Context Gateway snapshot/session 계약과 다르므로 구현을 직접 공유하지 말고 발급·fragment 보안 원칙만 재사용한다. Gateway의 `SessionStore`도 session을 메모리에만 보관하는 적합한 토대다.
- 코드 검색 명령은 조사 기록에 남겼다. 외부 패키지 레지스트리나 GitHub 구현은 조회하지 않았다. 새 의존성/통합을 추가하는 구현 작업이 아니므로 기존 dependency/adapter 경계를 확인하는 수준으로 search-first를 제한했다.

## 근거 경로와 재현 기록

요청된 선행 문서 `AGENTS.md`, `docs/context-gateway-discovery.md`, `docs/context-gateway-contract.md`, `docs/context-sharing-private-contract.md`, `docs/work-history/2026-11-16-context-sharing-r7-verification.md`를 읽고, 이 문서의 결론은 최신 `origin/main` 코드와 테스트로 재대조했다. 위 line reference는 기준 SHA `978cb5ace5115743b32f375ce065120e8d068d82`의 파일 기준이다.

### 실행한 명령과 출력

기준 갱신 및 격리 worktree:

```text
git fetch origin main
From https://github.com/winterholic/winterholic-ttakkari-back
 * branch main -> FETCH_HEAD

git worktree add -b back-context-sharing-v2-20261119 ../back-context-sharing-v2-20261119 origin/main
HEAD is now at 978cb5ac ...
git rev-parse HEAD
978cb5ace5115743b32f375ce065120e8d068d82
git status --short --branch
## back-context-sharing-v2-20261119...origin/main
```

주요 경로·증거 검색:

```text
rg --files context_gateway/src src/ttakkari/context_sharing tests alembic/versions deploy/context-gateway
-> Gateway auth/apps/models/publishing/snapshot_service, Private context_sharing modules, tests, migration 0079, compose files 확인
rg -n "single_use|exchange|session|revok|snapshot|digest|scanner|latest" context_gateway/src src/ttakkari/context_sharing tests/test_context_sharing* context_gateway/tests
-> exchange_key consumed_at atomic update, session parent-key validation, Private single_use=True, latest snapshot resolution, revoke/parity tests 확인
```

Private/API/실제 Gateway 코어 합성 통합 테스트:

```sh
PYTHONPATH=src ../back/.venv/bin/python -m pytest -q -rs \
  tests/test_context_sharing_api.py tests/test_context_sharing_mcp.py \
  tests/test_context_sharing_migration.py tests/test_context_sharing_real_gateway.py \
  tests/test_context_sharing_scanner.py
```

출력 범위:

```text
101 passed in 6.98s
db reset: 101 selective (avg 5.5/138 tables), 0 forced full, 0 fell back to full
```

이 실행에서 `tests/conftest.py`가 이미 설정된 `TTAKKARI_TEST_DB`를 사용했다. conftest는 이름이 `ttakkari`인 운영 DB를 거부하며 출력은 `db reset: 101 selective`였다. 환경변수의 실제 값은 출력하거나 기록하지 않았다. 데이터 입력은 테스트가 생성한 합성 project/document 및 Gateway 응답이다. `real_gateway.py`는 Gateway core control/public 앱을 ASGI로 띄운다. 이 경로에서 1회 exchange 후 재교환 401, session 사용, key revoke 후 기존 session 401, project withdrawal, site close/reopen 및 TTL 유지 assertion이 실행됐다.

Gateway 독립 auth/parity suite 시도:

```text
PYTHONPATH=context_gateway/src ../back/.venv/bin/python -m pytest -q -rs \
  context_gateway/tests/test_key_exchange.py context_gateway/tests/test_rest_mcp_parity.py
-> 8 skipped; isolated PostgreSQL unavailable in command sandbox: SysV shmget Operation not permitted
```

이후 Private 테스트용 `tests/pg_sandbox.py`로 별도 임시 DB를 공급하는 시도를 했으나, 이 실행 환경에서 `initdb ... --no-sync`가 exit status 1로 끝났다(상세 stderr는 해당 helper가 출력하지 않음). 따라서 독립 Gateway parity suite는 미실행/미검증으로 남겼다. Private의 101개 테스트 안에 실제 Gateway core를 이용하는 통합 테스트는 포함되어 통과했다.

### 미실행 및 범위 밖

- 운영 DB·Gateway·실제 문서 조회, DNS/Tunnel/browser/UI 실기기 확인, 공개 링크 발급, 운영 site switch, 배포 및 image build는 지시된 금지/조사 범위상 실행하지 않았다.
- front 저장소는 fetch·수정하지 않았다. back의 기존 main과 다른 worktree도 수정하지 않았다.
- 테스트 파일은 추가/수정하지 않았다. Private 코드 외부의 scanner 정책 확대와 v2 구현은 제안만 기록했다.
- 코드 경로의 장애 분석 중 cross-service partial commit은 강제 장애를 주입해 재현하지 않았으므로 inferred로 남겼다.
