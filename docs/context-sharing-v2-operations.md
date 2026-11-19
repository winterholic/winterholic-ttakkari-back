# Context Sharing v2 운영 안내

이 문서는 현재 코드가 정의한 설정·배포·장애 대응 절차다. 실제 운영 Gateway에 v2를 연결하거나 공개 site를 켠 기록은 없다. 운영 적용은 별도 활성화 단계에서 승인된 설정·DB·배포 절차로 진행한다. 아래 ID와 키는 합성 예시이며 실운영 성공 증거가 아니다.

기준 코드와 배포 자료: `context_gateway/src/context_gateway/`, `context_gateway/Dockerfile`, `context_gateway/web/`, `deploy/context-gateway/compose.yaml`, `deploy/context-gateway/compose.homeserver.yaml`, `deploy/context-gateway/nginx.homeserver.conf`, Private migration `alembic/versions/0080_ctx_share_v2_keys.py`. 저장소 파일로 배포 경로와 라우팅 설정을 정적으로 대조했다. 현재 운영 Gateway의 배포 revision, TLS, 실제 DB role/grant, site 상태는 실행 조회하지 않았다. `context_gateway/web/README.md` 및 배포 설정은 읽기 전용으로 다뤘다.

## 연결 구성과 설정

Private Publisher는 게시·제어에만 Gateway control URL과 control token을 사용하고, 외부 AI가 사용할 public origin을 별도로 보유한다. Gateway public 앱은 독립 PostgreSQL과 snapshot 파일 경로를 사용한다. Gateway public API와 MCP는 같은 인증·ACL·snapshot resolver를 공유한다.

| 구성 요소 | 실제 설정 이름 | 코드 기본값/제약 | 역할 |
|---|---|---|---|
| Private | `TTAKKARI_CONTEXT_GATEWAY_CONTROL_URL` | 빈 문자열. 원격은 HTTPS, loopback만 HTTP 허용. URL userinfo 금지 | Private → Gateway control 요청 |
| Private | `TTAKKARI_CONTEXT_GATEWAY_CONTROL_TOKEN` | 빈 문자열, 로그/응답 비노출 | Gateway control Bearer. public 읽기 키와 별개 |
| Private | `TTAKKARI_CONTEXT_GATEWAY_PUBLIC_URL` | 빈 문자열 | 링크 origin과 Claude Code MCP `/mcp` 주소의 기준. 기존 설정값을 사용하며 URL을 추측해 채우지 않는다 |
| Private | `TTAKKARI_CONTEXT_GATEWAY_TIMEOUT_SECONDS` | `10.0`, 허용 `0 < x ≤ 60` | control HTTP timeout |
| Private | `TTAKKARI_PUBLIC_SHARE_ORIGIN` | 기존 PublicShare 설정. Context Gateway 공개 주소로 대체 사용하지 않는다 | 별도 기존 기능 |
| Gateway | `CONTEXT_GATEWAY_DATABASE_URL` | 필수, `postgresql+asyncpg://`만 허용 | Gateway 전용 PostgreSQL |
| Gateway | `CONTEXT_GATEWAY_SNAPSHOT_DIR` | `/var/lib/context-gateway/snapshots` | 불변 snapshot 파일 저장 경로/volume |
| Gateway | `CONTEXT_GATEWAY_CONTROL_TOKEN` | 빈 문자열이면 control 인증 불가 | control app의 Bearer 인증 |
| Gateway | `CONTEXT_GATEWAY_RATE_LIMIT` | 분당 `120` | 부모 키/링크 단위 한도. 원 자격증명과 모든 자식 세션이 같은 카운터를 사용 |
| Gateway | `CONTEXT_GATEWAY_INITIALIZE_SCHEMA` | 기본 `true` | public 앱 lifespan의 additive runtime DDL 및 role grant 적용 여부. control 앱 lifespan은 항상 schema initializer를 실행 |
| Gateway | `CONTEXT_GATEWAY_MCP_ALLOWED_HOSTS` | `localhost,127.0.0.1,localhost:*,127.0.0.1:*` | MCP Streamable HTTP Host 검증 목록. 실제 외부 host는 승인된 환경 값으로 설정 |

Private 설정의 실제 환경 변수 이름은 `TTAKKARI_` prefix가 붙는다. prod에서 세 Gateway 연결 값이 모두 비었을 때만 코드가 `publisher.json` credential 파일을 읽을 수 있다. 파일은 소유자 전용 권한·크기·정확한 필드·HTTPS origin·token 형식을 검증한다. 하나라도 명시되어 있으면 파일 값을 섞어 채우지 않으므로 불완전한 명시 설정은 fail closed다. 이 문서는 실제 파일 값이나 운영 주소를 복사하지 않는다.

## v1과 v2 동작 차이

| 항목 | 기존 `single_use_v1` | `reusable_api_v2` | `share_link_v2` |
|---|---|---|---|
| 발급 기간 | 고정 3,600초 | `ttl_days`: 1/3/7/30, 기본 3일 | `ttl_days`: 1/3/7/30, 기본 3일 |
| 원 자격증명 | 1회 exchange 후 재사용 불가 | 만료 전 반복 직접 Bearer 사용 및 반복 exchange 가능 | `/s#token=…` 방문용. API/MCP Bearer로는 거부 |
| 세션 | exchange 세션은 최대 1시간, 부모 만료를 넘지 않음 | 동일. 부모 자격증명 폐기/만료 시 기존 세션도 다음 요청에서 거부 | 전용 HttpOnly/Secure/SameSite=Strict cookie 세션. 링크 만료/폐기에 종속 |
| scope | 기존 scope 동작 보존 | `read`, `search`, `export` 독립 선택 | `read` 필수, `export` 선택, `search` 미지원 |
| snapshot | 기존 조회 호환성: 프로젝트의 공개 snapshot 조회 의미를 유지 | `pinned` 또는 `latest`. 기본 pinned | `pinned` 또는 `latest`. 기본 pinned |

v2는 프로젝트 하나에만 grant한다. `pinned`는 발급 순간의 활성 snapshot UUID에 고정하며 요청의 snapshot ID가 다르면 숨김 처리(404)한다. `latest`는 매 요청에서 현재 latest를 선택하지만, caller가 다른/과거 snapshot UUID를 지정하면 역시 404다. 둘 다 ACL 범위 밖 snapshot을 지정해 우회할 수 없다. pinned metadata도 발급 시 고정한 값에서 응답하며 새 게시물의 요약이나 snapshot 목록을 통한 정보 노출을 허용하지 않는다. v1의 기존 과거 snapshot 조회 의미는 v2 정책으로 재해석하지 않는다.

발급 응답에서 원 키는 한 번만 전달된다. v2 API key 원문은 `Authorization` header로 직접 사용할 수 있고, 선택적으로 `/api/v1/auth/exchange`로 세션을 발급할 수도 있다. 원 키는 URL/query, 로그, DB, 목록에 넣지 않는다. 세션은 원 키와 별도 32바이트 무작위 credential이며 DB에는 hash만 저장된다. 폐기는 부모 키/링크와 자식 세션을 요청 때마다 재검사하므로 이미 열린 세션에도 적용된다. 키를 분실하면 재조회하지 말고 새 키를 발급한 뒤 기존 키를 폐기한다.

## REST 연결 예시

아래 흐름은 Private `POST /api/context-sharing/keys`의 v2 발급 `201` 응답에서 `key`와 **`connect.project_id`**를 받은 직후 사용할 수 있는 형태다. `project_id`는 Gateway UUID이며 Private 관리 UI의 `entity_id`, `project_slug`, 문서 검색 ID와 다르다. 특히 `read` scope가 없는 search/export 전용 키도 발급 응답의 `connect.project_id`를 사용한다. 다음 합성 UUID는 문서 예시일 뿐, 실제 발급 후에는 그 응답의 값을 대입한다.

```sh
# 키를 URL이나 shell history에 직접 적지 않는다. 아래 -H는 실행 시 헤더 값을 인자로 전달한다.
# 프로세스 인자 수집이 있는 환경에서는 이를 사용하지 말고 승인된 secret 주입 방식을 쓴다.
read -s CONTEXT_GATEWAY_API_KEY
export CONTEXT_GATEWAY_API_KEY
export PROJECT_ID='7f1e25a0-82c4-4a61-98bd-30d404bb1510' # 합성값; connect.project_id로 교체
export CONTEXT_GATEWAY_PUBLIC_URL='https://<기존 설정의 Gateway public origin>'

curl --fail-with-body -sS \
  -H "Authorization: Bearer ${CONTEXT_GATEWAY_API_KEY}" \
  -H 'Accept: application/json' \
  "${CONTEXT_GATEWAY_PUBLIC_URL}/api/v1/projects/${PROJECT_ID}/context"

curl --fail-with-body -sS --get \
  -H "Authorization: Bearer ${CONTEXT_GATEWAY_API_KEY}" \
  --data-urlencode 'q=deployment' \
  "${CONTEXT_GATEWAY_PUBLIC_URL}/api/v1/projects/${PROJECT_ID}/search"

curl --fail-with-body -sS \
  -H "Authorization: Bearer ${CONTEXT_GATEWAY_API_KEY}" \
  "${CONTEXT_GATEWAY_PUBLIC_URL}/api/v1/projects/${PROJECT_ID}/export?format=markdown"
```

검색에는 `search`, 내보내기에는 `export` scope가 필요하다. 컨텍스트 응답은 JSON이며 `Accept: text/markdown`을 보내면 Markdown 응답을 협상할 수 있다. 예제 origin은 실제 운영 URL이 아니다. Gateway public origin은 기존 `TTAKKARI_CONTEXT_GATEWAY_PUBLIC_URL` 설정에서 확인한다.

### Claude Code Streamable HTTP MCP

Gateway MCP endpoint는 public origin 아래 `/mcp`다. Claude Code 공식 문서는 remote HTTP MCP transport, `Authorization: Bearer …` header, 그리고 `.mcp.json`의 `url`/`headers`에서 `${VAR}` 환경 변수 치환을 지원한다고 명시한다. 아래 구성은 운영 URL/키를 저장소 파일에 넣지 않고, 배포 환경에서 이미 제공된 origin과 별도 credential 환경 변수만 참조한다.

```json
{
  "mcpServers": {
    "context-gateway": {
      "type": "http",
      "url": "${TTAKKARI_CONTEXT_GATEWAY_PUBLIC_URL}/mcp",
      "headers": {
        "Authorization": "Bearer ${CONTEXT_GATEWAY_API_KEY}"
      }
    }
  }
}
```

`CONTEXT_GATEWAY_API_KEY`는 Claude Code의 credential 환경 변수 보호 목록에 포함되지 않는 별도 이름으로 운영 환경에서 제공한다. `.mcp.json`의 environment expansion은 설정 공유 기능이며 secret manager를 대신하지 않는다. 이 예시의 변수가 unset이면 missing-variable 경고가 표시되고 `${VAR}` 문자열이 그대로 남는다. 보호 목록에 속한 credential 변수는 별도로 빈 값으로 처리되므로 Gateway 전용 변수 이름을 사용한다. 연결 확인은 Claude Code에서 `claude mcp get context-gateway` 또는 `/mcp`로 한다. 지원 근거: [Claude Code MCP 공식 문서](https://code.claude.com/docs/en/mcp), “Add a remote HTTP server” 및 “Environment variable expansion in `.mcp.json`” 절, 확인일 2026-11-19.

MCP 도구는 `list_projects`, `get_project_context`, `search_project_context`, `get_document`, `export_project_context`다. 각 도구가 필요한 scope를 개별 검사한다. 링크 자격증명은 MCP audience와 맞지 않아 거절된다.

## 데이터베이스 배포와 호환성

Private Alembic과 Gateway runtime DDL은 서로 다른 DB와 적용기를 사용한다. 한쪽 성공을 다른 쪽 migration 성공으로 간주하지 않는다.

### 1. Private DB: Alembic `0080_ctx_share_v2_keys`

`0080`은 Private `ctx_share_projects`/`ctx_share_keys`에 revoke state/error, credential type, snapshot policy/id, scopes, last-used metadata를 추가한다. 기존 키에는 `single_use_v1`, `latest`와 기본 metadata를 채우고 기존 publication 연결은 유지한다. v1 요청·응답과 1시간/1회 교환 의미를 보존하는 확장 migration이다.

운영 적용 전 Private DB 백업과 적용 대상 확인 후, Private backend 환경에서 migration CLI를 실행한다. 아래는 명령 형식 예시이며 이 작업에서 운영 DB에는 실행하지 않았다.

```sh
cd <Private backend checkout>
TTAKKARI_NO_ENV_FILE=1 TTAKKARI_ENV=prod \
  ../back/.venv/bin/alembic upgrade head
TTAKKARI_NO_ENV_FILE=1 TTAKKARI_ENV=prod \
  ../back/.venv/bin/alembic current
```

코드에서는 `TTAKKARI_DATABASE_URL`을 `Settings`가 읽는다. 연결 대상은 배포 담당자가 확인한 Private DB여야 한다. 확인 예시:

```sql
SELECT version_num FROM alembic_version;
SELECT credential_type, count(*) FROM ctx_share_keys GROUP BY credential_type ORDER BY credential_type;
SELECT revoke_state, count(*) FROM ctx_share_projects GROUP BY revoke_state ORDER BY revoke_state;
```

현재 head는 `0080_ctx_share_v2_keys`여야 한다. 기존 행은 `single_use_v1`로 분류되고, 미해결 폐기 intent는 임의로 active/revoked로 고치지 않는다.

### 2. Gateway DB: additive runtime DDL과 public role 권한

Gateway에는 Alembic migration CLI가 코드 트리에 없다. `create_control_app` lifespan은 PostgreSQL advisory transaction lock 아래 항상 schema initializer를 호출한다. `create_public_app`도 `CONTEXT_GATEWAY_INITIALIZE_SCHEMA=true`(기본값)일 때 같은 initializer를 실행한다. initializer는 `Base.metadata.create_all`, 여러 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, snapshot 불변 trigger 및 site-control seed를 적용한다. `gateway_api_keys.credential_type` 기본값은 `single_use_v1`, ACL `snapshot_policy` 기본값은 `latest`라 기존 키/ACL의 v1 의미와 호환된다. 새 v2 행을 담기 전에 새 Gateway 코드의 DDL을 완료한다.

배포 순서는 Gateway DB/snapshot 백업 및 상태 경로 확인 → Gateway release로 `control` additive DDL·role grant 완료 → DB/role과 public/control 경로 확인 → Private DB 백업 후 Alembic `0080_ctx_share_v2_keys` 적용 → Private API/MCP backend 별도 배포 → 승인된 v2 활성화다. 아래 manifest가 실제 Gateway service·mount·port 정의를 제공한다. `compose.homeserver.yaml`에서는 public의 `CONTEXT_GATEWAY_INITIALIZE_SCHEMA=false`이므로 `control`의 health가 DDL 완료 뒤에야 healthy가 된다. 실제 실행 상태는 별도 확인해야 한다.

### 저장소 기준 Gateway release 배포 절차

실제 파일은 `deploy/context-gateway/compose.yaml`, 홈서버 override `deploy/context-gateway/compose.homeserver.yaml`, 홈서버 nginx `deploy/context-gateway/nginx.homeserver.conf`, `deploy/context-gateway/Dockerfile.postgres`, 그리고 `context_gateway/Dockerfile`이다. 운영 기록 `docs/work-history/2026-11-16-context-gateway-site-deployment.md`는 release를 `/opt/homeserver/apps/context-gateway/releases/<full SHA>/`, 상태를 `/opt/homeserver/apps/context-gateway/runtime/`에 둔 기존 홈서버 절차를 기록한다. 상태 경로는 release 밖이며 `CONTEXT_GATEWAY_STATE_DIR`로 지정한다. 홈서버 override는 이에 더해 `CONTEXT_GATEWAY_RUNTIME_UID`, `CONTEXT_GATEWAY_RUNTIME_GID`, `CONTEXT_GATEWAY_IMAGE_TAG`를 요구한다. 당시에는 배포 뒤 `current` 심볼릭 링크를 새 release로 원자적으로 바꾸고, current 경로에서도 Compose 구성을 확인했다. 이 경로와 당시 revision은 저장소 기록이지 현재 운영 상태 확인이 아니다.

Gateway와 배포 디렉터리만 full revision으로 release에 전송한다. Private backend 코드·DB·사용자 문서는 이 이미지/build context에 포함하지 않는다.

```bash
REVISION=REPLACE_WITH_FULL_GIT_SHA
RELEASE=/opt/homeserver/apps/context-gateway/releases/$REVISION
git archive --format=tar "$REVISION" context_gateway deploy/context-gateway \
  | ssh hs-ops "mkdir -p '$RELEASE' && tar -xf - -C '$RELEASE'"
```

`context_gateway/Dockerfile`은 named build context `web`에서 `package.json`/lockfile을 가져와 Node 22에서 `npm ci`, `npm test`, `npm run build`로 `/web/dist`를 만든다. Python 3.13 image에는 Python virtualenv와 `/app/web-dist`만 복사하고 non-root UID 10001로 실행한다. Compose의 control/public은 같은 `context-gateway:<full SHA>` image를 사용한다. 따라서 이 이미지 빌드에 정적 web dist 배포가 포함되며, 별도 frontend 배포 단계는 없다.

운영자가 승인된 호스트 절차로 수행할 때의 명령 형식이다. `deployment.env` 값을 출력하지 말고 `config --quiet`만 사용한다. 이전 env tag가 남아 있을 수 있으므로 각 Compose 호출 전에 의도한 full SHA를 지정한다.

```bash
export CONTEXT_GATEWAY_IMAGE_TAG="$REVISION"
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" config --quiet
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" build control
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" up -d --wait control public
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" ps
readlink -f /opt/homeserver/apps/context-gateway/current
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f /opt/homeserver/apps/context-gateway/current/deploy/context-gateway/compose.homeserver.yaml config --quiet
```

기동 순서와 저장소: DB는 별도 PostgreSQL 17 서비스다. `Dockerfile.postgres`는 `01-create-public-role.sh`를 `/docker-entrypoint-initdb.d/`에 넣고, 이 스크립트는 빈 DB volume을 처음 초기화할 때 `context_gateway_public` role을 만든다. 기존 DB volume에서는 init script가 다시 실행되지 않는다. `control`은 `context_gateway_admin`으로 연결해 시작 시 schema initializer를 실행하고 snapshot 디렉터리를 read/write로 mount한다. `public`은 healthy `control`에 의존하며 `context_gateway_public`으로 연결하고 schema initializer를 끈 채 같은 snapshot 디렉터리를 read-only로 mount한다. 홈서버 override는 PostgreSQL과 snapshot을 각각 `<CONTEXT_GATEWAY_STATE_DIR>/postgres`, `<CONTEXT_GATEWAY_STATE_DIR>/snapshots`에 bind mount하고 host port publish를 제거해 내부 bridge 고정 주소를 쓴다. 기본 compose의 loopback 포트는 standalone용이며 홈서버 override에는 적용되지 않는다.

기동 후 확인 명령 예시다. 승인된 운영 시간에 읽기 전용으로 실행한다.

```bash
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" exec -T db \
  psql -U context_gateway_admin -d context_gateway -c \
  "SELECT rolname FROM pg_roles WHERE rolname = 'context_gateway_public';"
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" exec -T db \
  psql -U context_gateway_admin -d context_gateway -c \
  "SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges WHERE grantee = 'context_gateway_public' ORDER BY table_name, privilege_type;"
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f "$RELEASE/deploy/context-gateway/compose.homeserver.yaml" exec -T db \
  psql -U context_gateway_admin -d context_gateway -c \
  "SELECT grantee, table_name, column_name, privilege_type FROM information_schema.column_privileges WHERE grantee = 'context_gateway_public' ORDER BY table_name, column_name, privilege_type;"
curl --fail-with-body -sS https://share.winterholic.net/healthz
curl --fail-with-body -sS -o /dev/null -w '%{http_code}\n' https://share.winterholic.net/s
curl --fail-with-body -sS https://share.winterholic.net/api/v1/site
curl -sS -o /dev/null -w '%{http_code}\n' -X OPTIONS \
  https://share-control.winterholic.net/control/v2/credentials
```

기존 설정의 public URL과 control token을 사용하는 확인 요청은 운영자가 승인된 환경에서 token을 출력하지 않게 수행한다. 마지막 OPTIONS 경로 확인은 인증·body 없이 router 전달 여부만 보는 검사로, `/control/v2/credentials`가 전달되면 405, 전달되지 않으면 404가 예상된다. ingress를 승인된 방식으로 바꾸기 전 host에서 `nginx -t`와 설정된 `edgectl` 검사를 별도로 수행한다. nginx 파일 주석상 TLS는 Cloudflare Tunnel에서 종료되고 host nginx가 내부 bridge에 proxy한다. 이번에는 운영 TLS 상태를 확인하지 않았다. 저장소 nginx는 access log를 끄고 `share.winterholic.net`을 public으로 전달한다. 이전 control 설정은 `/control/v1/`만 전달했으나, 현재 worktree의 `deploy/context-gateway/nginx.homeserver.conf`에는 신규 `/control/v2/`도 같은 control upstream으로 전달하는 location이 있다. 이 경로는 별도 control hostname에만 있으며 fallback은 404다. Private v2 발급/상태 조회는 `/control/v2/credentials`, `/control/v2/credentials/status`를 사용한다. 저장소 설정에 추가한 v2 경로는 아직 운영에 적용되지 않았다. 운영 적용과 `nginx -t`·`edgectl` 검증 전에는 v2 제어 프록시가 live 상태라고 간주하지 않는다. 이번 교정에서는 운영 nginx/Tunnel을 변경하거나 실운영 경로를 확인하지 않았다.

### Private backend 배포는 별도 절차

Gateway 배포는 Gateway 전용 DB/schema/role/image/snapshot 경로를 다룬다. Private Alembic `0080_ctx_share_v2_keys`는 별도 Private DB migration이며, Private Python API/MCP 코드도 별도 backend release로 배포한다. Gateway release 명령은 Private 코드를 배포하지 않고, Private 배포도 Gateway DDL·control/public services를 대체하지 않는다. 순차 활성화 전에 두 배포가 각각 준비됐는지 확인하며, 이 안내 교정에서는 어느 쪽도 실행하지 않았다.

Rollback은 v2 행이 남은 상태에서 Private `alembic downgrade 0079_context_sharing`을 금지한다. 배포 전/후 `ctx_share_keys`에 `reusable_api_v2` 또는 `share_link_v2` 행이 하나라도 있으면 v2 열을 삭제하는 downgrade는 하지 않는다. v2 credential/session 및 pending revoke 상태를 다루는 승인된 rollback 절차가 선행되어야 하며, v2 행이 0건인 조건부 downgrade도 백업·복구 리허설과 별도 운영 승인이 필요하다. Gateway runtime DDL에는 자동 downgrade가 없다. 회사(`work`) 프로젝트 신규 외부 게시·키/링크 발급은 조직 승인 정책 증명 전까지 차단하며, 운영 공개 활성화는 이 문서나 구현 수락만으로 승인되지 않는다.

### 공개 share 경로와 로그·분석 경계

`context_gateway/web_serving.py`는 `/s`에 `/index.html`을 제공하고 `/api/v1/*`, `/mcp`를 public FastAPI 앱으로 전달한다. 공개 앱에는 control endpoint가 없다. `/api/v1/share/*`는 공개 앱에서 link-session 전용 cookie 인증을 수행한다. `/s#token=...` fragment는 HTTP 요청 경로로 전송되지 않으며 UI는 token을 메모리에 옮긴 뒤 history에서 제거한다. `POST /api/v1/share/exchange`는 JSON body로 token을 받고, 응답 body에는 만료 시각만 반환하며 share session은 `Secure; HttpOnly; SameSite=Strict; Path=/api/v1/share` cookie로 설정한다. context/export/logout 요청은 이 cookie로 인증하고 부모 grant를 다시 확인한다. WebServingApp 응답에는 `Cache-Control: no-store`, CSP, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-Robots-Tag: noindex, nofollow, noarchive`가 적용된다. share API는 no-store/no-referrer/noindex와 더 제한적인 CSP를 추가.

저장소 설정에서 nginx 두 server는 `access_log off`이고 Compose logging driver는 `local`이다. Gateway/Private 소스를 검색했을 때 token·request body·cookie를 출력하는 애플리케이션 logger나 analytics 연동은 찾지 못했다. 이는 소스 정적 검토이며 운영 stdout 수집기나 Cloudflare 분석 설정을 확인한 것은 아니다. Uvicorn 명령에서 access log를 끄지 않았으므로 요청 경로가 로그에 남을 수 있다고 보고 token을 query/path에 넣지 않는다. Browser history 처리와 CSP는 UI/응답 경로 보호이지 upstream 로그 보존 정책을 증명하지 않는다. `share_link` token은 fragment/JSON body에서만 사용하고 URL/query나 MCP/API Bearer로 보내지 않는다. 운영 분석·로그 도구의 body/header/cookie 수집과 보존 설정은 별도 확인 사항이다.

initializer는 role `context_gateway_public`이 이미 존재할 때만 아래 권한을 반영한다. role 생성과 비밀번호 관리는 이 코드의 책임이 아니다. 해당 role의 정확한 권한 적용은 DBA가 운영 DB에서 확인한다. initializer를 끈 환경에서는 배포자가 동일 스키마 DDL과 권한을 사전에 적용해야 하며, 이 저장소에는 별도의 수동 migration CLI가 없다.

- `SELECT` on `gateway_projects`, `gateway_snapshots`, `gateway_api_keys`, `gateway_key_acls`, `gateway_sessions`
- `UPDATE (consumed_at, last_used_at, window_started_at, window_count)` on `gateway_api_keys`
- `INSERT, UPDATE` on `gateway_sessions`; `INSERT` on `gateway_audit_events`
- `SELECT` on `gateway_site_control`; `gateway_site_control_audit` 전체 권한 회수
- `gateway_site_control`의 public role `INSERT/UPDATE/DELETE` 권한 회수

확인 예시(조회 전용):

```sql
SELECT column_name FROM information_schema.columns
 WHERE table_name IN ('gateway_api_keys', 'gateway_key_acls', 'gateway_publish_operations', 'gateway_projects')
 ORDER BY table_name, ordinal_position;
SELECT rolname FROM pg_roles WHERE rolname = 'context_gateway_public';
SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges
 WHERE grantee = 'context_gateway_public' ORDER BY table_name, privilege_type;
SELECT enabled FROM gateway_site_control WHERE id = 1;
```

### 호환성 창과 rollback

먼저 확장 schema를 적용하고, v1 동작을 보존하는 Gateway/Private 호환 코드를 배포한 다음 별도 활성화에서 v2 issuance를 연다. 기존 v1 클라이언트는 새 Gateway에서도 기존 1회 교환/기존 snapshot 의미를 유지한다. 새 v2 자격증명이 생긴 뒤에는 v1 전용 코드로 바로 rollback하지 않는다. Gateway v2 ACL·credential을 v1 코드가 올바르게 해석한다는 보장이 없기 때문이다. 우선 신규 v2 발급을 중단하고, Gateway/Private에서 v2 자격증명과 해당 세션을 만료 또는 폐기하고, 미해결 `revoking`을 대사한 뒤 보존된 additive schema 위에서 호환되는 코드로 rollback한다.

Private `alembic downgrade 0079_context_sharing`은 v2 metadata 열을 실제로 삭제한다. v2 행은 이후 기본값 `single_use_v1`로 오분류될 수 있으므로 단순 downgrade는 금지한다. 조건부 rollback이 반드시 필요한 경우에만 백업과 복구 리허설 뒤, **`ctx_share_keys`에 v2 row가 0건이고(`reusable_api_v2`, `share_link_v2` 모두 0), revoking 상태/미해결 폐기 intent도 없고, 새 기능 세션이 없는 상태**를 확인한 경우에 한해 별도 운영 승인 절차로 downgrade를 검토한다. v2 row가 하나라도 남으면 downgrade하지 말고 schema를 유지한 채 애플리케이션을 호환 release로 돌린다. Gateway runtime DDL도 기존 v1 열을 포함해 additive 적용되며 자동 downgrade 절차는 제공하지 않는다. DB·snapshot volume 복원은 상호 시점 일치 백업으로 수행하고, snapshot 불변성을 깨는 수동 수정은 하지 않는다.

## 장애·복구

### Private ↔ Gateway control 통신 단절

Private는 키/링크/프로젝트 폐기 의도를 먼저 DB에 `revoking`으로 commit한 뒤 Gateway에 전달한다. 통신 실패 시 API는 retryable 오류와 `status: revoking`을 반환하며 Gateway 폐기 완료로 표시하지 않는다. Private `generation_service.maintenance_jobs`는 `context-sharing-revocations`를 `max(10, min(60, runtime_poll_seconds))`초 주기로, 한 번에 최대 50개씩 재시도한다. 1시간 retention 작업과 분리되어 있다. 재시도는 Gateway revoke의 멱등성을 이용한다. 운영에서 generation runtime/maintenance가 돌지 않는 상태라면 자동 재시도를 기대하지 말고 해당 Private 운영 상태를 복구한다.

통신 단절 전에 Gateway에 도달하지 못한 키/링크는 여전히 유효할 수 있다. 연결 복구 전에는 강제 차단이라고 주장할 수 없다. 그러나 Private의 프로젝트 revoke intent가 `revoking`이면 Private API에서 신규 publish·credential 발급을 막는다. 원격 폐기 완료 후 Private commit이 실패해도 pending을 유지하고 재시도로 대사한다. 상태를 임의로 `revoked` 또는 `active`로 바꾸지 않는다.

복구 후 Private가 maintenance 재시도를 수행하게 두고, owner API에서 project/key/link status가 `revoked`인지 확인한다. Gateway control에서 프로젝트의 revoked 상태, 키 status 응답의 `revoked_at`, 그리고 필요한 경우 public `401 credential_revoked`를 확인한다. 대기 건이 남으면 Private `revoke_error`, Gateway 연결 상태와 감사 이력을 확인한 뒤 재시도 상태를 추적한다. 원문 키/토큰은 로그나 지원 티켓에 복사하지 않는다.

### Public site 비상 중단과 재개

Private `GET /api/context-sharing/site`로 현재 Gateway 전역 site 상태를 읽고, 직접 승인된 운영 절차에서만 `PATCH /api/context-sharing/site`의 `{"enabled":false}`를 요청한다. 실제 Gateway control은 `GET/PATCH /control/v1/site`이며 `gateway_site_control`의 durable global switch를 사용한다. 닫히면 인증이 필요한 `/api/v1/*` 및 `/mcp` 요청은 `503` `{"detail":"site_temporarily_unavailable"}`와 `Retry-After: 60`을 받는다. `/healthz`와 public site-status 경로는 상태 확인용으로 남는다. 이 스위치는 Private ↔ Gateway control 연결이 살아 있어야 변경 가능하다.

재개는 원인 해결·폐기 상태 대사·운영 확인 뒤 `enabled=true`로 명시 적용한다. 재개는 새로운 snapshot이나 자격증명을 만들지 않고 이전 snapshot을 수정/삭제하지 않는다. 기존 key 만료시각도 연장하지 않는다. 재개 후 승인된 합성/격리 자격증명으로 site 상태와 제한된 읽기를 확인한 뒤 일반 경로를 연다. 운영 공개 연결을 이 문서 작업에서 수행하거나 확인하지 않았다. 저장소 nginx에는 control v2 경로가 추가돼 있다. 운영 설정 반영과 실제 route 전달 검증은 아직 수행하지 않았으므로 운영 v2 연결을 보장하지 않는다.

### 회사 범위와 조직 정책

조직 공유 정책 증명이 없는 현재 구현은 회사(`work`) 프로젝트의 신규 외부 게시, 신규 v1/v2 API key 및 신규 share link 발급을 `403 work_sharing_policy_required`로 차단한다. 요청 타입을 v1로 바꾸거나 `confirm_work_scope`를 보내도 우회할 수 없다. 기존에 발급된 v1 행은 재해석/자동 폐기되지 않는다. 회사 자료 외부 공유를 운영에서 허용하려면 조직 승인 정책과 증명·감사·운영 소유권을 별도 설계·승인해야 한다. 현재 임의 allow flag나 정책 우회 설정은 없다.

## HTTP detail 및 Private 오류 분류

Gateway REST의 FastAPI 오류는 통상 `{"detail":"…"}`다. Private control 오류는 `{"code":"…","message":"…"}`로 별도 전달한다.

| HTTP | Gateway `detail` / 응답 | 해석 |
|---|---|---|
| `401` | `unauthorized` | 없는/잘못된 Bearer, 또는 v1 키 사용·만료·폐기 시의 호환 오류 |
| `401` | `credential_expired` | v2 원 자격증명 또는 세션이 만료됨. 새 발급 필요 |
| `401` | `credential_revoked` | v2 부모 credential 또는 세션이 폐기됨 |
| `401` | `audience_mismatch` | `share_link_v2`를 API/MCP에 사용하거나 API credential을 link cookie 경로에 사용함 |
| `404` | `not found` | 없는 프로젝트/문서, grant가 없는 프로젝트/scope, pinned 밖 또는 latest 외 snapshot. 존재 여부를 노출하지 않음 |
| `429` | `rate limit exceeded`; `Retry-After: 60` | 부모 키/링크와 자식 세션이 공유하는 분당 한도 초과. 60초 뒤 재시도 |
| `503` | `site_temporarily_unavailable`; `Retry-After: 60` | Gateway global site switch가 닫힘 |
| `409` | `snapshot integrity check failed` | 저장된 snapshot 무결성 검사 실패. 해당 응답 재시도보다 운영 조사/복구 우선 |

Private의 Gateway control 연결 실패는 코드 기준 `gateway_unavailable`(503, `Retry-After: 30`), 설정 누락은 `gateway_not_configured`(503, `Retry-After: 300`), Gateway 4xx 거부는 `gateway_rejected`(502)로 분류된다. 이는 Private 관리 API의 오류이며 공개 REST의 `detail` 코드와 혼동하지 않는다. HTTP 401/404/429/503을 성공 빈 결과로 처리하지 말고, 오류 body의 credential이나 요청 URL을 로그에 남기지 않는다.

## 검증 범위와 활성화 상태

Private work-history에는 Private 선택 회귀 최종 `197 passed in 21.64s`와 합성 Gateway control/ASGI 통합 결과가 기록돼 있다. 이는 격리 fixture의 코드 경로 검증이다. DB migration CLI도 전용 합성 시험 DB에서 확인됐으며 Gateway 런타임 DDL은 격리 fixture 테스트로 확인됐다. 어느 것도 운영 Gateway에 접속해 키를 발급하거나 문서를 게시한 결과가 아니다. 저장소 manifest/nginx의 정적 검토는 실제 설정 내용을 확인한 것이고, 현재 운영 Gateway revision·TLS/DNS·DB role/grant·site 상태는 미검증이다.

이 문서에 기록된 Claude Code 지원 근거 URL은 위 공식 문서 링크이며 조회일은 2026-11-19다. 조회된 공식 문서는 `--transport http`, Authorization Bearer header, `.mcp.json` 변수 확장을 지원한다. 이 근거는 Claude Code 설정 형식을 뒷받침할 뿐 Gateway 운영 연결을 증명하지 않는다.
