# Context Gateway 외부 웹 UI 배포

## 공개 진입점

Compose의 `public` 서비스는 `uvicorn context_gateway.web_public:app`으로 시작한다. 이 모듈은 기존 `create_public_app()`을 변경 없이 생성해 `WebServingApp`으로 감싼다. `/api/v1`, `/mcp`, `/healthz`만 기존 앱으로 전달하고 UI는 `web-dist/index.html`과 `web-dist/assets/`의 빌드 파일만 제공한다. UI는 `/`에서 열리며 SPA fallback은 없다. `/control`, `/docs`, `/openapi.json`, 소스맵, 소스 파일, 설정 파일은 정적 응답 대상이 아니다.

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

`tests/test_web_serving.py`는 실제 `create_public_app()`에 ASGI 요청을 보내지만 lifespan을 시작하지 않는다. 따라서 DB에 연결하지 않고 무자격 API·MCP 인증 경계에서 401을 확인한다. 임시 빌드 디렉터리의 index와 JS/CSS는 200, control 경로와 source map·소스·설정·경로 탈출은 404인지 확인한다.

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
- 미검증: 컨테이너 이미지 빌드·기동과 컨테이너 내부 HTTP. 이 작업 카드에 기록된 Codex Docker CLI 실패(`unix:///Users/winterholic/.orbstack/run/docker.sock: connect: no such file or directory`)가 이미 발생한 단계이므로 daemon이나 소켓을 건드리지 않고 런타임 검증을 생략했다.
- 미검증: 실제 DNS, Tunnel, 운영 환경변수와 운영 데이터. 이 범위에서 읽거나 변경하지 않았다.
