# Context Gateway site-control 운영 배포 (2026-11-16)

## 결과

- 배포 대상 소스: `032ace3b91c64fff1d0d555af39981f6aedcdd54`.
- 홈서버 `current`와 control/public 실행 revision을 위 SHA로 전환했다. Private 백엔드와 frontend는 배포하지 않았다.
- control/public은 running, control healthy다. public에는 Docker healthcheck가 없어 Compose `ps`의 health 값은 비어 있다. 외부 HTTPS `/healthz`가 200인 것을 별도로 확인했다.
- 현재 사이트는 `enabled=true`다. PATCH, 키 발급, snapshot 게시/삭제, DB 초기화/삭제는 하지 않았다.
- 기존 행 수는 배포 전후 동일하다: projects 6, snapshots 6, API keys 4, sessions 3.
- 롤백 기준은 직전 운영 revision `515b565dc652e67e806daff921fa26e86695d581`이며, 해당 release와 이미지 tag가 남아 있는 것을 확인했다.

## 배포 전 관측

환경은 홈서버의 기존 `hs-ops` SSH와 Docker Compose 5.0.0이다. 자격값은 기존 `/opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env`를 Compose `--env-file`로만 참조했다.

```text
readlink -f /opt/homeserver/apps/context-gateway/current
/opt/homeserver/apps/context-gateway/releases/515b565dc652e67e806daff921fa26e86695d581

docker compose --project-name context-gateway --env-file .../runtime/secrets/deployment.env -f .../current/deploy/context-gateway/compose.homeserver.yaml config --quiet
exit 0

authenticated GET http://127.0.0.1:8788/control/v1/site (기존 control 컨테이너 내부)
HTTP 404
실행 중 모듈의 라우트 목록에도 /control/v1/site가 없었고, DB to_regclass('public.gateway_site_control')은 absent였다.
따라서 저장된 토글 값은 배포 전에 존재하지 않았다. 대상 revision의 _site_enabled 및 초기화 로직에 따라 기존 설치는 enabled=true로 시작한다는 것은 코드에 근거한 판단이다.

기존 DB 행 수:
gateway_projects|6
gateway_snapshots|6
gateway_api_keys|4
gateway_sessions|3
```

첫 행 수 조회는 잘못된 테이블명 `projects` 때문에 실패했다(`relation "projects" does not exist`). `\\dt public.*`로 실제 이름을 확인하고 읽기 전용 count 조회를 다시 실행했다. 테이블이나 데이터를 변경하지 않았다.

## 소스 전달과 배포

정확한 Git archive 범위는 `context_gateway/`와 `deploy/context-gateway/`였다. archive 목록에는 부모 디렉터리 표식 `deploy/`만 추가로 나타났다. Private 코드, frontend 저장소, 원본 문서는 전송하지 않았다.

```bash
git archive --format=tar 032ace3b91c64fff1d0d555af39981f6aedcdd54 context_gateway deploy/context-gateway \
  | ssh hs-ops 'mkdir -p /opt/homeserver/apps/context-gateway/releases/032ace3b91c64fff1d0d555af39981f6aedcdd54 && tar -xf - -C /opt/homeserver/apps/context-gateway/releases/032ace3b91c64fff1d0d555af39981f6aedcdd54'
```

원격에서 compose 파일과 `context_gateway/pyproject.toml` 존재를 확인했다. release는 상태 경로 밖이다. 실제 영속 경로는 기존 `/opt/homeserver/apps/context-gateway/runtime/` 아래 DB·snapshot·secret 경로를 유지했다.

기존 env 파일은 `CONTEXT_GATEWAY_IMAGE_TAG=515b565d…`를 우선해, 첫 config 확인이 이전 image tag로 해석됐다. 자격 파일은 수정하지 않았다. 새 tag를 해당 명령 환경에서만 덮어써 resolved Compose 값이 요청 SHA인지 확인한 뒤 실행했다.

```bash
export CONTEXT_GATEWAY_IMAGE_TAG=032ace3b91c64fff1d0d555af39981f6aedcdd54
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f /opt/homeserver/apps/context-gateway/releases/032ace3b91c64fff1d0d555af39981f6aedcdd54/deploy/context-gateway/compose.homeserver.yaml config --quiet
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f /opt/homeserver/apps/context-gateway/releases/032ace3b91c64fff1d0d555af39981f6aedcdd54/deploy/context-gateway/compose.homeserver.yaml build control
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f /opt/homeserver/apps/context-gateway/releases/032ace3b91c64fff1d0d555af39981f6aedcdd54/deploy/context-gateway/compose.homeserver.yaml up -d --wait control public
```

실행 출력 요약:

```text
Compose config: ok
resolved control/public image: context-gateway:032ace3b91c64fff1d0d555af39981f6aedcdd54
up --wait: control healthy, public started; DB remained running/healthy
```

배포 뒤 `current`를 atomic symlink 교체로 새 release에 연결했다. 이후 current 경로에서 Compose `config --quiet`를 다시 실행해 통과했다. 기존 deployment.env의 image tag는 변경하지 않았으므로 향후 Compose 작업도 명령 환경에서 `CONTEXT_GATEWAY_IMAGE_TAG`를 명시해야 한다.

## 실행 revision과 격리

안전한 inspect 필드만 추출했다.

```text
control: context-gateway:032ace3b91c64fff1d0d555af39981f6aedcdd54
         container revision label=032ace3b91c64fff1d0d555af39981f6aedcdd54
         running, healthy, readonly_root=true
public:  같은 image tag / container revision label
         running, readonly_root=true
         /opt/homeserver/apps/context-gateway/runtime/snapshots -> /var/lib/context-gateway/snapshots, rw=false
control snapshot mount: 같은 host source, rw=true
ports: container target 8787은 있으나 PublishedPort=0 / docker inspect port bindings null
network: context-gateway_gateway-internal, internal=true, subnet=172.31.0.0/24
         control=.2, public=.3, DB=.4
DB: 같은 runtime PostgreSQL bind mount, running/healthy
```

실제 image tag와 실행 컨테이너 label은 SHA가 일치했다. `docker image inspect`의 image-level `org.opencontainers.image.revision` label은 null이다. 이 Compose는 해당 label을 image build metadata가 아니라 컨테이너 label로 설정한다. image ID는 `sha256:a96dae0af5e56aec47f37c18688870aa22ad3c3345c08709adc21f6e6ac350c2`다.

## 외부 경계와 API 계약 대조

외부 HTTPS에서 변경된 경로만 확인했다.

```text
GET https://share.winterholic.net/healthz             HTTP 200
GET https://share.winterholic.net/                   HTTP 200
GET https://share.winterholic.net/api/v1/site         HTTP 200 {"enabled":true}
GET https://share.winterholic.net/api/v1/projects     HTTP 401 (무자격 읽기 거부)
GET https://share.winterholic.net/control/v1/site     HTTP 404 (public에서 control 차단)
GET https://share.winterholic.net/docs                HTTP 200
GET https://share.winterholic.net/openapi.json        HTTP 200, OpenAPI 3.1.0
```

기존 control 컨테이너의 환경을 내부 probe에 사용했다. 자격 원문은 출력하지 않았다.

```text
GET http://127.0.0.1:8788/control/v1/site, Bearer from control container environment
HTTP 200 {"enabled":true}
같은 GET, Authorization 없음
HTTP 401
```

공개 OpenAPI에는 `/api/v1/site`를 포함한 공개 경로 9개가 있으며 `/control/*` 경로는 없다. 현재 `docs/context-gateway-contract.md`의 공개 API 경로 표에는 `/api/v1/site`가 없다. 즉 live schema와 문서 표 사이에 해당 공개 상태 endpoint 누락이 확인됐다. 범위의 소유 work-history만 커밋하며 계약 문서는 수정하지 않았다.

Ttakkari 브라우저에서도 UI를 렌더했다. 페이지 제목은 `Context Gateway`, 화면에는 접속 안내와 1회용 키 입력 폼이 표시됐고 키는 입력하지 않았다. console 오류는 Cloudflare Insights 스크립트가 `script-src 'self'` CSP에 의해 차단된 항목 2개와 `/favicon.ico` 404 1개였다. 기존 CSP를 완화하거나 favicon을 추가하지 않았다.

## 롤백 기준과 한계

직전 `515b565dc652e67e806daff921fa26e86695d581` release 디렉터리와 `context-gateway:515b565dc652e67e806daff921fa26e86695d581` image tag가 남아 있음을 확인했다. 롤백은 해당 이전 release의 Compose 파일과 기존 자격 파일을 사용하고 image tag를 프로세스 환경에서 지정한다.

```bash
export CONTEXT_GATEWAY_IMAGE_TAG=515b565dc652e67e806daff921fa26e86695d581
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f /opt/homeserver/apps/context-gateway/releases/515b565dc652e67e806daff921fa26e86695d581/deploy/context-gateway/compose.homeserver.yaml up -d --wait control public
```

롤백 명령은 실행하지 않았다. 기존 DB와 snapshot 데이터는 유지된다. 이번 작업에서 Private/frontend 배포, nginx/Tunnel/방화벽 변경, 사이트 PATCH, key 발급·삭제, snapshot 변경, 전체 HTTP38 재실행, 백업/복원 검증은 하지 않았다. Public 무자격 읽기 거부는 대표 projects 목록 1개로 확인했고, 전체 API route 전수를 반복하지 않았다.
