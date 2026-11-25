# Context Sharing v2 구현 계약

2026-11-19. 기준 분석: `docs/context-sharing-v2-analysis.md` (backend `978cb5ac`), front `docs/context-sharing-v2-ui-analysis.md` (`f6dfe5b`). 사용자 요구 CG-R1~R8을 구현하는 작업 계약이며 운영 공개 활성화 승인이 아니다.

## 불변 조건과 호환성

- 기존 Gateway 서비스·전용 DB·snapshot volume·Private publisher/control 경계를 확장한다. 신규 서비스·내부 DB/RAG 직접 접근을 도입하지 않는다.
- 기존 `single_use_v1`은 TTL 3600초, 원 키 1회 교환, 부모 만료 이내 세션 반복 사용 및 기존 프로젝트/snapshot 조회 의미를 보존한다. 누락된 타입은 v1이다.
- 신규 타입은 `reusable_api_v2`, `share_link_v2`다. TTL `ttl_days`는 1/3/7/30, 기본 3. 새 타입의 무제한 키는 없다.
- 새 grant의 `snapshot_policy`는 `pinned` 또는 `latest`. 기본 pinned이며 latest 문자열을 발급자가 명시한 경우만 새 승인 게시물로 이동한다. pinned는 발급 당시 활성 publication의 Gateway snapshot에 고정한다. 새 타입은 다른 snapshot을 URL/인자로 지정해 우회할 수 없다. latest도 현재 latest 이외 버전을 요청하면 404다. 기존 v1의 과거 조회 의미는 바꾸지 않는다.
- 정책 제한은 문서 본문뿐 아니라 프로젝트 요약·이름·latest_snapshot_id·snapshot 목록에도 적용한다. pinned 자격으로 새 게시의 요약/metadata나 다른 snapshot ID를 노출하지 않는다. 새 manifest에 게시 당시 project_name/project_description을 포함하고 기존 manifest에 없는 pinned metadata는 발급 당시 값을 grant에 보존하는 등 불변/발급 스냅샷 자료로 응답한다. 현재 Project.description을 무조건 재사용하면 pinned 범위 밖 새 요약이 노출될 수 있다.
- Gateway 승인 snapshot의 불변성, digest/scanner, 부모 key·session·project·snapshot 만료/철회 재검사, 전체 중단을 유지한다.

## 데이터와 발급

기존 Private `ShareKey` 및 Gateway `APIKey/KeyACL/Session`을 additive하게 확장한다. 기존 행은 v1이며 `publication_id`는 발급 당시 감사 연결이다. 신규 v2의 실제 snapshot 제한은 Gateway ACL에서 강제한다.

- Private metadata: `credential_type`, `snapshot_policy`, nullable `pinned_snapshot_id`, `scopes`, nullable `last_used_at`. 기존 label/issued/expires/revoked 및 Gateway ID를 재사용한다.
- Gateway APIKey: 타입과 마지막 사용. KeyACL: 정책과 nullable snapshot UUID. 기존 세션→부모 키 연결을 재사용한다. 직접 API Key 요청의 rate limit도 교환 세션 생성으로 우회할 수 없게 자격증명 단위로 제한한다.
- 32바이트 CSPRNG, 원문은 발급 응답 1회만 반환. DB·감사·목록·오류에는 해시/ID/metadata만 보존한다.
- 링크와 API Key 목록을 분리하며, 링크 원문 URL도 재조회하지 않는다. v1+v2 API Key 목록에는 타입으로 구분하고 링크를 섞지 않는다.
- 사용 시각은 Gateway가 성공한 인증 사용에서 기록한다. Private는 control metadata로 대조하며 불능일 때 `usage_status: unavailable`로 표시한다. 알 수 없는 값을 사용 없음으로 단정하지 않는다.

## Control API (Gateway 소유)

기존 `/control/v1/*` 계약은 유지한다. 신규 발급은 다음 경로를 쓴다.

`POST /control/v2/credentials`:

```json
{
  "credential_type": "reusable_api_v2",
  "label": "동료 에이전트",
  "ttl_days": 3,
  "projects": {
    "GATEWAY_PROJECT_UUID": {
      "scopes": ["read", "search"],
      "snapshot_policy": "pinned",
      "snapshot_id": "GATEWAY_SNAPSHOT_UUID"
    }
  }
}
```

v2는 프로젝트 하나만 허용한다. pinned에는 동일 프로젝트의 미철회 snapshot ID가 필요하다. latest에는 snapshot_id를 넣지 않는다. share_link_v2 scope는 read 필수, export 선택이며 search는 제공하지 않는다. API Key는 read/search/export를 독립 선택한다.

발급 응답 필드: `key_id`, `key`, `credential_type`, `label`, `ttl_days`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`, `projects` (동일 grant shape). Private는 전체 정책·ID·만료 영수증을 검증한다.

`POST /control/v2/credentials/status`에 `{ "key_ids": ["UUID"] }` (최대 200)를 보내면 `{ "credentials": [...] }` metadata를 반환한다. key 원문은 없다. 모든 타입의 폐기는 기존 `POST /control/v1/keys/{id}/revoke`를 재사용한다.

## Private owner/agent API (Private 소유)

기존 `/api/context-sharing`와 `/api/agent/context-sharing` base 아래의 `/keys`를 호환 확장한다.

- `POST /keys`: 기존 body는 v1로 유지. 새 body는 `credential_type: reusable_api_v2`, `ttl_days` (기본 3), `snapshot_policy` (기본 pinned), 기존 project_slug/label/scopes.
- 신규 키 발급 응답의 `connect`는 기존 url/note와 `project_id` (Gateway UUID)를 제공한다. 관리 front의 entity_id를 공개 REST project_id와 혼동하지 않는다. read 권한 없는 search/export 전용 키도 발급 직후 올바른 공개 프로젝트 UUID로 연결 예시를 만들 수 있어야 한다.
- `GET /keys`: v1/v2 프로그램 키 목록, 링크 제외. 응답 기존 필드에 타입·정책·pinned_snapshot_id·scopes·last_used_at·usage_status 추가. `single_use`는 v1에서만 true다.
- `POST /keys/{id}/revoke`: 링크 ID를 혼용하면 404. 모든 부모 세션까지 차단하는 Gateway 영수증을 확인한다.
- `POST /links`: project_slug, label, ttl_days 기본3, snapshot_policy 기본pinned, `allow_export` 기본 false. type은 서버가 share_link_v2로 정한다.
- `GET /links?project_slug=...`: `{links:[...]}`. 공통 metadata 형태, 원문/URL 없음.
- `POST /links/{id}/revoke`: API Key ID 혼용 404.
- 링크 발급 응답은 metadata와 `url`만 포함한다. raw `key`를 별도 중복 노출하지 않는다. URL은 설정된 public origin의 `/s#token=SECRET`이다.
- `/projects`는 현재 publication과 active_key_count를 보존하고 active_link_count 및 last_published_at을 추가한다. active 수는 만료/철회 중/철회됨을 제외한다. 조회만으로 게시/발급하지 않는다.
- 원문 응답은 no-store이며 Query/Mutation 영속 캐시에 넣지 않는다. 불명확한 원격 결과를 발급 성공/폐기 완료로 표시하지 않는다.

## 공개 REST/MCP 인증 및 브라우저 계약 (Gateway 소유)

`reusable_api_v2`는 기존 공개 REST `/api/v1/*` 및 `/mcp`의 `Authorization: Bearer API_KEY`로 직접 여러 번 호출 가능하다. 기존 session bearer와 공존한다. `/api/v1/auth/exchange`에서 v2 API Key는 반복 교환 가능하며 각 세션은 최대 1시간 및 부모 키 만료를 넘지 않는다. v1은 종전 동작을 유지한다. 링크 원문/링크 세션을 일반 REST/MCP bearer로 쓰는 것은 거절한다.

REST와 MCP는 같은 인가·snapshot resolver를 쓴다. MCP에 export scope를 확인하는 export 도구를 추가한다. 매 요청 credential/parent/session/project/snapshot 및 site switch를 재검사한다. 새 자격의 인증 실패·만료·철회 오류를 코드로 구분하되 기존 v1 오류 호환성은 보존한다. URL·로그에는 인증정보를 넣지 않는다.

사람용 링크는 `/s#token=...`로 진입하며 서버에 fragment가 전달되지 않는다. `/s`는 기존 공개 앱의 정적 shell을 제공한다. 앱은 어떤 인증 UI도 표시하기 전에 token을 메모리로 옮기고 history.replaceState로 fragment를 제거한다.

- `POST /api/v1/share/exchange`, JSON `{token: SECRET}`: share_link_v2만 수락한다. 짧은 세션을 DB 해시로 만들고 Secure/HttpOnly/SameSite=Strict, Path=/api/v1/share 쿠키로 반환한다. secret·session 원문을 응답 본문에 넣지 않는다. Origin/Fetch-Metadata/JSON content-type 검증으로 cross-site exchange를 차단한다. 같은 링크 재교환·새 방문 반복을 허용한다.
- `GET /api/v1/share/context`: 쿠키와 부모 grant를 재검사한다. `{project: ProjectOut, snapshot: SnapshotOut, documents: [...본문 포함...], link: {label, expires_at, snapshot_policy, scopes}, session_expires_at}`. 해당 링크가 허용한 프로젝트와 snapshot만 반환한다. 전체 프로젝트 목록/다른 버전 탐색 API를 노출하지 않는다.
- `GET /api/v1/share/export?format=markdown`: 부모 grant export scope를 추가 검사한 뒤 Markdown을 반환한다. read만으로 복사는 가능하지만 export 다운로드는 scope가 있어야 한다.
- `POST /api/v1/share/logout`: 현재 브라우저 세션을 폐기하고 쿠키를 지운다. 부모 링크 자체는 폐기하지 않는다.
- no-store/no-referrer/CSP/noindex, 외부 자원/이미지/HTML 실행 차단을 모든 공개 응답에 적용한다. 브라우저 history·storage·오류·analytics에 원문이 남지 않게 한다. cookie 세션 만료 후에는 원 링크 재열기를 안내한다.

## 게시·철회·회사 범위·장애

- Private publish는 현재 승인 digest 및 scanner를 유지한다. 새 게시 중 파일·DB transaction 실패가 이전 승인 snapshot을 변경하지 않는다.
- Gateway snapshot publish에 선택적 `operation_id`를 추가한다. Private는 draft UUID를 재시도 안정 ID로 전송한다. Gateway는 동일 operation/payload에 동일 receipt를 반환하고, 변경된 payload의 ID 재사용은 409한다. 기존 클라이언트의 ID 없는 v1 게시도 지원한다. 새 receipt 기록은 snapshot/latest와 같은 transaction에 둔다.
- 폐기 요청은 Private에 내구 의도로 먼저 기록하고 Gateway에 전달한다. 성공 미확인 시 `revoking`/pending 상태와 재시도 오류를 반환하며 완료를 주장하지 않는다. 기존 app maintenance 패턴으로 pending 의도를 자동 재시도한다. 프로젝트 단위 요청은 새 발급/게시를 차단한다. 제어 통신 단절 중 Gateway에 아직 전달되지 않은 폐기를 즉시 반영했다고 주장하지 않는다. 이 분산 시스템 경계를 운영 문서와 UI에서 명시한다.
- Gateway 폐기 성공 뒤 Private commit 실패는 보수적으로 pending을 유지하고 idempotent 재시도로 대사한다. 상태를 임의 복구하거나 만료를 연장하지 않는다.
- 회사(work) 범위는 초안 생성·에이전트 게시 시 `confirm_work_scope=true`(소유자 화면의 외부 공유 의도 확인)를 요구한다. 그 뒤 게시·키·링크 발급은 개인 범위와 같다(2026-11-25 사용자 결정: 회사 업무에 쓰려고 만든 기능이라 조직 정책 증명 차단을 없앴다). 고위험 scanner와 문서 한도는 그대로다. 이미 발급된 v1 행을 재해석/자동 폐기하지 않으며 기존 1시간·단회 교환 의미도 보존한다.

## 파일 소유권과 검증

- Gateway core 구현자: `context_gateway/src/`, `context_gateway/tests/`, `deploy/context-gateway/`, `docs/context-gateway*`. `web/`는 공개 UI 구현자 소유다.
- Private 구현자: `src/ttakkari/context_sharing/`, `src/ttakkari/api/context_sharing.py`, 새 Alembic revision, 관련 private tests와 관리 MCP 배선 및 private 문서. 기존 공유 test/DB를 타 실행과 동시에 reset하지 않도록 단독 test DB를 쓴다.
- front 구현자: 별도 front worktree의 페이지/features/API/types/tests. Gateway 공개 UI는 `context_gateway/web/`만 별도 단위로 구현한다.
- 새 behavior는 실패 테스트→구현→재검증한다. 기존 테스트 삭제/완화 금지. 3일 기본·다른 TTL·반복 접근·pinned/latest·scope·프로젝트 격리·기존 세션 만료/철회·site 중단/재개·XSS/원문 누출·부분 실패·v1/migration을 검증한다.
- Gateway 독립 fixture의 SysV shmget/initdb sandbox 실패는 해당 환경에 한정한다. 알려진 실패를 재시도하지 않고 기존 PostgreSQL의 새 명시적 test DB를 사용한다. 운영 DB를 사용하거나 시험 DB를 공유 reset하지 않는다. 브라우저는 configured ttakkari_browser를 사용한다.
- 실제 운영 데이터 게시, 외부 공개 활성화, 비밀정보 조작 및 Gateway 운영 배포는 이 계약만으로 실행하지 않는다. 검증한 코드와 운영 활성화 상태를 분리한다.

클라이언트 공식 참고: <https://code.claude.com/docs/en/mcp> (2026-11-19 조회: HTTP transport 및 Authorization header, .mcp.json 환경변수 지원). 설정 예시는 실제 Gateway 경로와 설치 SDK를 확인한 뒤 제공한다. 최신 프로토콜 전체로의 불필요한 업그레이드는 하지 않는다.
