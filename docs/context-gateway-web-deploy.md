# Context Gateway 외부 웹 UI 배포

## 공개 진입점

Compose의 `public` 서비스는 `uvicorn context_gateway.web_public:app`으로 시작한다. 이 모듈은 기존 `create_public_app()`을 변경 없이 생성해 `WebServingApp`으로 감싼다. `/api/v1`, `/mcp`, `/healthz`, `/openapi.json`은 기존 공개 앱으로 전달하고 UI는 `web-dist/index.html`과 `web-dist/assets/`의 빌드 파일만 제공한다. UI는 `/`에서 열리며 SPA fallback은 없다. `/control`, 소스맵, 소스 파일, 설정 파일은 정적 응답 대상이 아니다.

`/openapi.json`은 기존 public FastAPI 앱이 생성하는 OpenAPI 3.1 문서를 반환한다. 문서 경로 `/docs`는 CSP에 허용되지 않는 CDN 스크립트나 인라인 코드를 사용하지 않도록, 공개 스키마를 HTML `<pre>`로 직접 읽을 수 있게 렌더링한다. 페이지에는 JSON 문서 링크도 있다. 이 schema는 공개 REST API만 포함하며 control 앱은 감싸거나 schema 생성에 사용하지 않는다. `/docs`와 schema 응답에도 동일한 보안 헤더가 붙는다.

모든 응답에는 `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`와 `frame-ancestors 'none'`을 포함한 CSP를 적용한다. 정적 자산은 GET/HEAD만 허용하고 CSS·JS·이미지·폰트 확장자를 `assets/` 바로 아래에서만 서빙한다.

## 포트와 격리

| 서비스 | 컨테이너 포트 | 기본 호스트 포트 | 노출 |
|---|---:|---:|---|
| public | 8787 | 8796 | `127.0.0.1` |
| control | 8788 | 8788 | `127.0.0.1` |
| private DB | 5432 | 없음 | 내부 네트워크 전용 |

public 기본 호스트 포트 `8796`은 기존 따까리 Caddy의 `127.0.0.1:8787`과 분리했다. `.env.example`에서 `CONTEXT_GATEWAY_PUBLIC_PORT`와 `CONTEXT_GATEWAY_CONTROL_PORT`를 바꿀 수 있다. 두 컨테이너 포트는 고정이며 호스트 바인딩만 조정한다. Gateway 내부 네트워크와 public의 snapshot 볼륨 읽기 전용 mount는 유지한다.

## 빌드와 실행

Python 빌드 컨텍스트는 `context_gateway/`로 제한하고, Compose named build context `web`으로 UI 디렉터리만 별도 전달한다. package 컨텍스트의 `.dockerignore`는 `web/`을 제외하므로 named context를 사용해 잠금 파일과 소스에 접근한다. 이 구성은 Compose 2.17 이상이 필요하다. Dockerfile은 `web/package-lock.json`으로 `npm ci`를 수행하고 `npm test`, `npm run build`를 실행한다. Python 런타임에는 Python 가상환경과 `/app/web-dist`만 복사하며 Node 런타임, UI 소스와 테스트는 들어가지 않는다.

```bash
cp deploy/context-gateway/.env.example deploy/context-gateway/.env
# .env의 예시 자격값을 별도 무작위 값으로 교체한다.
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml config
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml build
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml up -d
```

## 검증

```bash
cd context_gateway
UV_CACHE_DIR=/tmp/context-gateway-uv-cache uv run --frozen --extra test python -m pytest tests/test_web_serving.py -q
cd web && npm run build
```

`tests/test_web_serving.py`는 실제 `create_public_app()`에 ASGI 요청을 보내지만 lifespan을 시작하지 않는다. 따라서 DB에 연결하지 않고 무자격 API·MCP 인증 경계에서 401을 확인한다. 공개 OpenAPI 3.1 schema가 wrapper `/openapi.json`으로 전달되고 `/api/v1/` 경로만 포함하는지, CSP 호환 `/docs`에 CDN과 스크립트가 없는지 확인한다. 임시 빌드 디렉터리의 index와 JS/CSS는 200, control 경로와 source map·소스·설정·경로 탈출은 404인지 확인한다.

## OpenAPI 및 문서 접근 검증

```bash
cd context_gateway
UV_CACHE_DIR=/tmp/context-gateway-uv-cache uv run --frozen --extra test python -m pytest tests/test_web_serving.py -q
UV_CACHE_DIR=/tmp/context-gateway-uv-cache uv run --frozen --extra test ruff check src/context_gateway/web_serving.py tests/test_web_serving.py
```

ASGI 검증에서 `/openapi.json`은 실제 public 앱 라우트로 전달된다. schema version이 `3.1.x`이고 모든 경로가 `/api/v1/` 아래이며 control 경로가 없는지 assertion한다. `/docs`는 OpenAPI schema를 정적 HTML 본문으로 보여 주므로 현재 CSP에서 CDN 허용이나 inline script 예외가 필요 없다. 요청은 ASGI transport로 수행되며 운영 DB, DNS, Tunnel은 사용하지 않는다.

Compose 구성 검사는 실제 자격값 대신 합성 환경변수로 수행한다.

```bash
CONTEXT_GATEWAY_DB_ADMIN_PASSWORD=synthetic-admin \
CONTEXT_GATEWAY_DB_PUBLIC_PASSWORD=synthetic-public \
CONTEXT_GATEWAY_CONTROL_TOKEN=synthetic-control \
CONTEXT_GATEWAY_PUBLIC_PORT=18796 \
CONTEXT_GATEWAY_CONTROL_PORT=18798 \
docker compose -f deploy/context-gateway/compose.yaml config --quiet
```

## 검증 상태와 한계

- 완료: static/진입점 테스트 3개, production frontend build, 합성 환경 `docker compose config --quiet`, Dockerfile 및 Compose 빌드 컨텍스트·stage 복사 경계의 정적 검토.
- 후속 완료: wrapper OpenAPI 3.1 전달, 공개 REST path-only assertion, CSP/CDN 독립 `/docs` 렌더링과 무자격 API·MCP 경계 회귀 테스트.
- 미검증: 컨테이너 이미지 빌드·기동과 컨테이너 내부 HTTP. 이 작업 카드에 기록된 Codex Docker CLI 실패(`unix:///Users/winterholic/.orbstack/run/docker.sock: connect: no such file or directory`)가 이미 발생한 단계이므로 daemon이나 소켓을 건드리지 않고 런타임 검증을 생략했다.
- 미검증: 실제 DNS, Tunnel, 운영 환경변수와 운영 데이터. 이 범위에서 읽거나 변경하지 않았다.

## 2026-11-16 홈서버 운영 경로

Mac의 Docker 소켓 부재와 별개로 기존 `hs-ops`/`hs-edge` 계정에서 홈서버 Docker 29.1.2, Compose 5.0.0과 `edgectl`을 확인했다. `compose.homeserver.yaml`은 기존 서비스 정의를 재사용하고, 운영 스냅샷 소유 UID/GID와 release tag·영구 bind mount만 분리한다. Compose `!reset` 지원 버전이 필요하다.

- 코드: `/opt/homeserver/apps/context-gateway/releases/<full SHA>/`. `git archive`로 Gateway와 해당 배포 디렉터리만 전송한다. Private 코드·DB·사용자 문서는 이미지에 포함하지 않는다.
- 상태: `/opt/homeserver/apps/context-gateway/runtime/`. `/srv`는 root 소유이고 ops 비대화형 sudo 조회가 비밀번호를 요구해 기존 권한을 변경하지 않았다. 이번 서비스의 상태는 release 디렉터리 밖의 별도 bind 경로에 유지한다. 이 경로는 기존 `/opt/homeserver` 백업 범위에 포함하되 실제 백업 실행·복원은 이번 검증 범위가 아니다. 향후 `/srv/context-gateway`가 준비되면 중지·백업 후 mount 원본만 이전한다.
- 자격 파일: `runtime/secrets/deployment.env`, mode 0600. 원문은 git·보고서·빌드 문맥에 넣지 않는다.
- 네트워크: `172.31.0.0/24` 내부 브리지. control `.2:8788`, public `.3:8787`, DB `.4:5432`. 컨테이너는 호스트 포트를 공개하지 않는다. 해당 subnet은 이 홈서버에서 예약한 값이며 다른 호스트에서는 충돌 여부를 먼저 확인한다.
- 기존 host nginx는 이 브리지에 접속한다. `share.winterholic.net`은 public만, `share-control.winterholic.net`은 control 앱으로 전달하며 두 주소의 nginx access log는 꺼져 있다. 게시 인증은 control 앱의 별도 Bearer 토큰이 담당한다.
- 기존 Cloudflare `homeserver` Tunnel에 위 두 hostname만 추가했다. 설정은 변경 전 백업했으며 `edgectl`로 nginx/ingress 검사와 적용을 한다. Tunnel 자격 파일을 읽거나 변경하지 않는다.

### Context Sharing v2 control proxy 경로

기존 배포 문서와 당시 운영 경로는 `share-control.winterholic.net`에서 `/control/v1/`만 control 컨테이너(`172.31.0.2:8788`)로 전달하는 상태를 기록했다. Gateway control 앱의 신규 `POST /control/v2/credentials`와 `POST /control/v2/credentials/status`는 Private v2 발급·상태 확인에 필요하므로, control server에도 `/control/v2/`를 같은 control upstream으로 전달하는 별도 경로가 필요하다.

현재 worktree의 `deploy/context-gateway/nginx.homeserver.conf`에는 `location ^~ /control/v2/`가 추가되어 control upstream(`172.31.0.2:8788`)으로 전달한다. 이 location은 별도 control hostname에만 있고, 나머지 경로의 fallback 404와 public hostname 격리는 유지한다. 이 저장소 설정 변경은 아직 운영에 적용되지 않았다. 이 문서 교정에서는 nginx/Tunnel 설정을 바꾸거나 현재 운영 nginx를 조회하지 않았다. v2 발급을 별도 승인해 활성화하기 전에 승인된 운영 절차로 이 설정을 적용하고 `nginx -t`·`edgectl` 검증 및 control route 전달을 확인해야 한다.

실제 Docker 실행에서 교정한 항목: tmpfs flow list의 쉼표를 문자열로 묶었고, builder/runtime의 venv 절대 경로를 `/app/.venv`로 일치시켰다. Gateway Python 패키지를 `--no-editable`로 설치해 builder 소스 경로에 의존하지 않도록 했다. Node 22의 fetch Blob은 DOM Blob과 생성자가 달라 다운로드 검사를 ZIP MIME 및 실제 4바이트로 대조한다.

현재 release를 바꾸거나 다시 기동할 때는 아래 `CONTEXT_GATEWAY_IMAGE_TAG`를 의도한 full SHA로 먼저 지정한다. 기존 `deployment.env`에 이전 tag가 남아 있을 수 있으며, `current` 링크 교체만으로 Compose의 image tag는 바뀌지 않는다. 아래 값은 2026-11-16 사이트 제어 배포 revision이다. 자격 파일은 수정하거나 출력하지 않는다.

```bash
export CONTEXT_GATEWAY_IMAGE_TAG=032ace3b91c64fff1d0d555af39981f6aedcdd54
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f deploy/context-gateway/compose.homeserver.yaml config --quiet
docker compose --project-name context-gateway \
  --env-file /opt/homeserver/apps/context-gateway/runtime/secrets/deployment.env \
  -f deploy/context-gateway/compose.homeserver.yaml up -d --wait
```

## Private Publisher 자격 파일

기존 `TTAKKARI_CONTEXT_GATEWAY_*` 설정을 우선한다. 세 연결 값이 모두 비었고 `env=prod`일 때만 `<TTAKKARI_HOME의 상위>/private-resources/service-data/context-gateway/secrets/publisher.json`을 읽는다(기본 사용자 홈 기준). 보호된 `config.env`를 수정할 필요가 없다. 허용 필드는 `control_url`, `control_token`, `public_url` 세 개뿐이며 앱 정책·권한·일반 환경변수를 변경할 수 없다.

파일은 현재 프로세스 소유의 일반 파일이어야 하고 그룹/타인 권한이 없어야 한다(mode 0600 권장). symlink, 8KiB 초과, 알 수 없는 필드, 잘못된 HTTPS origin, 256비트 미만 길이의 토큰은 연결을 비활성화한다. 기존 설정이 일부만 지정됐을 때 파일 값으로 섞어 보완하지 않는다. 응답에는 자격 값이나 파일 오류 원문을 노출하지 않는다. 자격 파일은 도서관 색인·공유·백업 평문 산출 대상에 넣지 않는다.
