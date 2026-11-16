# Context Gateway 조사 근거

조사일: 2026-11-16 (Asia/Seoul)

## 기준점과 관찰 범위

기준 checkout은 `/Users/winterholic/development/projects/winterholic-ttakkari/back-context-gateway-20261116`이다. 지정한 workdir에서 수집한 기준 revision은 `98d027267793657e47d70f2d99ce934e71b8cf22`다. 첫 상태 확인 때 `context_gateway/`, `tests/test_context_sharing_scanner.py`가 미추적이었다. 조사 중 다른 작업의 변경이 계속 추가되어, 코드 근거 중 `context_gateway/`와 `src/ttakkari/context_sharing/`은 이 HEAD에 들어 있지 않은 당시 working tree의 관찰이다. 따라서 이 경로의 라인 근거는 HEAD 내용과 동일하다고 보증하지 않는다.

프론트엔드는 별도 checkout `/Users/winterholic/development/projects/winterholic-ttakkari/front`에서 확인했다. 그 checkout의 HEAD는 `f614d2b6955b340100cef9eb8f3711d5b1827162`였고 working tree도 수정·미추적 상태였다. 프론트 라인 근거 역시 해당 checkout의 조사 당시 working tree 기준이다.

## 실제 코드 경로

### Context Gateway 초안

- 독립 패키지가 `context_gateway/src/context_gateway/`에 있다. `__init__.py:1-3`은 Ttakkari 애플리케이션 코드를 import하지 않는 독립 패키지라고 설명한다.
- `apps.py:174-202`의 `create_public_app()`은 `/api/v1/auth/exchange`를 제공한다. `apps.py:204-268`에는 프로젝트 목록·컨텍스트·문서·검색 API가 있고, `apps.py:289-293`은 별도 HTTP MCP를 `/mcp`에 mount한다. MCP tool 정의는 `apps.py:296-378`에 있다.
- 관리 앱은 `apps.py:381-423`의 `create_control_app()`이다. `/control/v1/snapshots` 게시·폐기와 `/control/v1/keys` 발급·폐기 경로가 있고, `control_auth()`는 별도 설정 `control_token`과 비교한다. 키 발급은 프로젝트별 `read`·`search`·`export` scope를 받는다.
- Gateway 인증 구현 `auth.py:17-18,44-47,56-86,89-124`는 1시간 수명의 단회용 API 키를 세션 키로 교환하고, 만료·폐기·프로젝트 ACL·scope 및 분당 요청 수를 확인한다. `apps.py:195-202`의 exchange 경로와 `apps.py:301-314`의 MCP token verifier가 이 인증을 사용한다.
- 별도 publisher 쪽으로 보이는 `src/ttakkari/context_sharing/`도 조사 당시 working tree에 있었다. `scanner.py:1,33-57,82-100`은 정규식 기반 민감정보 탐지이며, 모듈 문서 자체가 회사 기밀이나 맥락상 비공개 내용을 찾지 못한다고 한정한다. `gateway.py:1-6,17,69-79,82-109,117-180`에는 control HTTP 어댑터가 있고, `config.py:157-162`에는 별도 control URL/token 및 public URL 설정이 있다.
- 연결 여부는 구분한다. 조사 당시 `rg -n 'context_sharing|context_gateway|context-gateway|ContextSharing|GatewayControl|build_control|scan_text' src/ttakkari/api src/ttakkari/main.py src/ttakkari/orchestrator/mcp_server.py ../front/src` 결과는 없었다(exit 1). 이 검색 범위에서는 publisher/scanner의 API·MCP·프론트 연결을 확인하지 못했다. 이는 검색한 경로의 정적 근거이며 실행 시 연결 상태는 검증하지 않았다.

### 기존 도서관·공유·인증·MCP

- 도서관 구현은 `src/ttakkari/context/library.py:118-139`의 `library_catalog()`이며 등록 프로젝트와 활성 source를 범위별로 조회한다. 소유자 API는 `src/ttakkari/api/assistant.py:314-322`의 `/api/assistant/library`, `/api/assistant/recall`이다. Run용 API는 `src/ttakkari/api/agent.py:120-135`의 `/api/agent/library`, `/api/agent/recall`이다.
- 기존 Run MCP는 `src/ttakkari/orchestrator/mcp_server.py:43-49,190-192,422-435`에서 백엔드 API에 Bearer 헤더를 붙이고, `library_catalog` 및 `recall`을 `/api/agent` 경로로 호출한다. 이 경로는 Gateway의 별도 `/mcp` 서버와 구별된다.
- 기존 공유는 `src/ttakkari/api/shares.py:13-55`의 인증된 소유자 `/api/shares` API와 `src/ttakkari/api/public_shares.py:5-17`의 capability 기반 `/api/public/shares/{sid}`·download API로 나뉜다. `src/ttakkari/main.py:375-383`에서 기존 assistant/agent 및 공유 router가 앱에 등록된다.
- 기존 소유자 인증은 `src/ttakkari/api/auth.py:28-30,77-110`의 `/api/auth/login` 및 refresh-cookie 흐름과 `src/ttakkari/security/auth.py:20-23,58-74`의 API audience·owner subject Bearer 검증이다. Gateway의 단회용 project key/session ACL과 동일한 인증이라고 볼 근거는 없다.

### 프론트 진입점

- 기존 프론트의 공개 공유 진입은 `front/src/main.tsx:18-36`이다. `/share/`는 `AuthProvider` 바깥의 `PublicMeetingPage`로 분기한다. 소유자 인증 상태는 `front/src/auth.tsx:14-51`의 `AuthProvider`가 맡는다.
- 라우트는 `front/src/App.tsx:50-75`에 있으며 `/share/:shareId`, `/library`, `/context-library`가 있다. 공유 요청은 `front/src/api/client.ts:201-219`에서 owner API와 capability 공개 API로 나뉜다.
- 조사한 `front/src` 경로에서 Context Gateway 전용 진입점은 검색 결과로 확인하지 못했다(위 정적 검색 범위). 현재 확인한 `/context-library`는 기존 Ttakkari 도서관 진입점이다. 별도 Gateway UI의 존재·배포 상태는 미확인이다.

## 미확인 사항

- 직전 보고 원문은 다른 Run outbox에 있을 수 있어 지시대로 열지 않았다. 따라서 그 보고에 적힌 개별 미확인 질문은 확인·승계하지 않았고, 이 문서는 해당 결론을 전제로 하지 않는다.
- 이 조사는 파일 존재·정적 경로·라인 확인이다. DB 설정, 실제 요청, 배포/프록시 연결, API 및 MCP 런타임, Gateway ↔ publisher 통합 동작은 실행하지 않아 미확인이다.
- 미추적·수정 파일은 조사 중 변할 수 있다. 후속자는 위 revision과 `git status --short`를 다시 확인한 뒤 working-tree 근거를 재검증해야 한다.

## 재현 명령과 기준 출력

백엔드 checkout에서 실행:

```sh
git rev-parse HEAD
git status --short
```

기준 출력:

```text
98d027267793657e47d70f2d99ce934e71b8cf22
?? context_gateway/
?? tests/test_context_sharing_scanner.py
```

프론트 checkout revision과 두 핵심 앱 진입점을 확인한 명령:

```sh
git -C ../front rev-parse HEAD
nl -ba ../front/src/main.tsx | sed -n '18,36p'
```

기준 출력의 revision:

```text
f614d2b6955b340100cef9eb8f3711d5b1827162
```

핵심 정의 라인 재확인 예:

```sh
nl -ba context_gateway/src/context_gateway/apps.py | sed -n '174,202p;225,268p;289,314p;381,423p'
nl -ba src/ttakkari/context/library.py | sed -n '118,139p'
nl -ba src/ttakkari/api/agent.py | sed -n '120,135p'
nl -ba src/ttakkari/api/shares.py | sed -n '13,55p'
nl -ba src/ttakkari/api/auth.py | sed -n '28,30p;77,110p'
nl -ba src/ttakkari/orchestrator/mcp_server.py | sed -n '43,49p;190,192p;422,435p'
nl -ba ../front/src/main.tsx | sed -n '18,36p'
```
