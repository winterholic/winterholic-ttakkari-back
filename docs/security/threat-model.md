# 따까리 위협 모델 (2026-10-19)

이 문서는 보안감사 재수립 1단계 산출물이다. 실행 항목은 [보안 WBS](wbs.md)에 있다.
코드 기준점은 `back` `1705c0d`(작업 트리에 다른 작업자의 미커밋 변경 있음), `front` `630040b`다.

표기 규칙은 다음과 같다.

- **확인됨**: 이번 조사에서 명령 실행 결과나 코드 줄로 직접 확인했다.
- **추론**: 코드 구조로 보아 그럴 가능성이 높지만 실행으로 재현하지 않았다.
- **미검증**: 근거가 없거나 확인하지 못했다. 수치는 `확인 필요`로 둔다.

이번 단계는 계획·조사 단계라 운영 코드 수정, 배포, 운영 데이터 변경, 외부 공격 시험을 하지 않았다.
`.env`, `~/.ttakkari/config.env`, 키체인 내용은 열지 않았다.

## 1. 시스템 요약과 신뢰 경계

```
[휴대폰·브라우저] --HTTPS--> Cloudflare --터널--> cloudflared --> Caddy 127.0.0.1:8787 --> uvicorn 세대 프로세스
        │                                                                       │
        │ (Vercel 정적 SPA, 공개 공유 /share)                                     ├─ PostgreSQL (localhost)
        │                                                                       ├─ 엔진 Run: claude / codex / agy / muse / qwen-local
        │                                                                       │    └─ stdio MCP 서버 `ttakkari` → 127.0.0.1:8787 /api/agent·/api/orchestrator
        │                                                                       ├─ 호스트 도구: llama-server, Chrome(토스·NotebookLM), soffice, docker(게임 빌드)
        │                                                                       └─ 배포 감독(deployer) → 세대 전환
[원격 데스크톱 WS] --> /api/remote-desktop/socket --> Mac 화면 공유(VNC)
```

| 경계 | 바깥 | 안쪽 | 현재 통제(근거) |
|---|---|---|---|
| B1 인터넷 → API | 익명 인터넷 사용자 | FastAPI | Cloudflare 터널, 백엔드는 루프백 바인딩(`deploy/install-services.sh:3,22`, 확인됨: `lsof`에서 caddy `127.0.0.1:8787`) |
| B2 소유자 브라우저 → API | 소유자(비밀번호 1개) | 소유자 API 289개 | Bearer access JWT(`security/auth.py:58-71`), refresh 쿠키 회전(`api/auth.py`) |
| B3 에이전트 프로세스 → API | 엔진 Run(프롬프트 인젝션 가능) | `/api/agent` 90개 + `/api/orchestrator` 15개 | 세션 한정 JWT `aud=ttakkari-agent`(`orchestrator/runtime.py:186-208`), 라우트별 pool 검사 |
| B4 work 세션 ↔ personal 자료 | 회사 세션(회사 계정 엔진) | 개인 기억·파일·메모·일정·주식 | 라우트별 `pool == "work"` 검사(부분 적용, 7절) |
| B5 에이전트 → 호스트 OS | 엔진이 실행하는 셸·파일 도구 | 사용자 홈, 비밀, 다른 Run | Claude·Muse PreToolUse guard(휴리스틱, `security/guard.py:3-6`), Codex seatbelt `workspace-write`(`agents/codex.py:80-83`), Antigravity `--sandbox`(`agents/antigravity.py:40`, 의미 미검증) |
| B6 외부 콘텐츠 → 에이전트 판단 | 웹·문서·회의록·GitHub·NotebookLM 응답 | 도구 호출 | 도구 설명의 "source data, not instructions" 문구뿐(추론: 기술적 통제 없음) |
| B7 공개 공유 | 링크를 가진 누구나 | 승인된 스냅샷 | capability 토큰, `ShareSecurityMiddleware`(`sharing/middleware.py`) |
| B8 배포 | push 권한이 있는 에이전트 | 운영 코드 실행 | `request_backend_deploy`, work 세션 차단(`api/deployments.py:25`) |

## 2. 자산 목록

| ID | 자산 | 저장 위치(코드 근거) | 영향 |
|---|---|---|---|
| A1 | 소유자 비밀번호 해시 | `Credential` 테이블, Argon2(`security/passwords.py`) | 유출 시 오프라인 추측 |
| A2 | JWT 서명키 | `~/.ttakkari/config.env`의 `TTAKKARI_JWT_SECRET`(`config.py:19-23,42`) | **소유자 토큰 위조 = 전권**. 원격 데스크톱 연결만 비밀번호 재인증 필요 |
| A3 | refresh 토큰 | 쿠키 `ttk_refresh`, DB에는 SHA-256(`security/auth.py:49-55`) | 30일 세션 |
| A4 | access JWT | 프런트 메모리(`front/src/api/client.ts:74`) | 15분 소유자 권한, 폐기 불가 |
| A5 | 에이전트 세션 토큰 | MCP 서버 env `TTK_ORCH_TOKEN`(`orchestrator/runtime.py:211-220`) | Run 시간+10분 동안 해당 세션 범위 API |
| A6 | 다운로드·업로드 토큰 | `/api/dl/{token}` 5분 JWT, 업로드 토큰 `ttk_` 접두사(`api/transfers.py:228-241`) | 파일 열람·inbox 쓰기 |
| A7 | 외부 AI 계정 자격증명 | `~/.ttakkari/accounts/*`, Antigravity 키체인(`security/antigravity_keychain.py`), `~/.claude`, `~/.codex/auth.json` | 유료 계정 탈취·사용량 소진 |
| A8 | 금융·Google 세션 | `<home>/tossinvest-chrome`, `<home>/google-accounts`(세션·계정별 Chrome 프로필, ADR 0051), 이전 위치 `<home>/notebooklm-chrome`·`<home>/notebooklm/accounts`(심볼릭 링크로 남음) | 증권 계좌 열람, Google 계정 |
| A9 | 개인 기억·지식 원장 | `CtxMemory`, `CtxEntity`, 지식 assertion | 개인정보·건강·경력 |
| A10 | 메모·일정 | `Memo`, `CalendarEvent`(scope 필드 없음) | 개인 일정 노출 |
| A11 | 대화·Run 이벤트 | `ChatSession`, `RunEvent`(저장 전 마스킹 `runs/events.py`) | 업무·개인 대화 |
| A12 | 파일·산출물 | `~/.ttakkari/data/{artifacts,uploads,outbox}`, 작업공간 `~/development` 등 | 소스코드, 회사 자료 |
| A13 | 회의 녹음·전사 | meetings, local STT | 회사 회의 내용 |
| A14 | 운영 무결성 | 배포 세대, deployer, Caddy 설정 | 악성 코드 실행, 서비스 중단 |
| A15 | 호스트 가용성 | CPU·메모리(llama-server, STT, 게임 빌드), 디스크 | 서비스 중단 |
| A16 | 백업 | Time Machine, DB 덤프 | **확인됨: `tmutil latestbackup`가 "Failed to mount destination"(Code=17)으로 실패**. 코드에 DB 백업 경로 없음(추론, `grep pg_dump` 결과 0건) |

## 3. 행위자

| ID | 행위자 | 능력 | 비고 |
|---|---|---|---|
| T1 | 익명 인터넷 공격자 | 공개 URL로 HTTP 요청, 무차별 대입, 공개 공유 링크 추측 | 주된 외부 위협 |
| T2 | 악성 웹페이지 | 소유자 브라우저에서 CSRF·클릭재킹·XSS 시도 | 쿠키 사용 경로는 refresh/logout뿐 |
| T3 | 오염된 콘텐츠(프롬프트 인젝션) | 회수 문서·웹·GitHub 이슈·회의록·NotebookLM 응답·업로드 파일에 지시를 심음 | 에이전트는 승인 없이 도구 실행(ADR 0003) |
| T4 | 회사 세션 에이전트 | work pool 토큰, 같은 OS 사용자 권한의 셸 | 개인 자료가 회사 계정 엔진 로그로 넘어가는 것이 주 피해 |
| T5 | 오작동 에이전트 | 의도와 다른 파괴적 명령, 대량 삭제 | 악의 없이 "작업공간 파괴" 결과 |
| T6 | 같은 Mac의 다른 로컬 프로세스·사용자 | `ps`, `/private/tmp` 읽기, 루프백 포트 접속 | 단일 사용자 Mac이라 가능성은 낮음 |
| T7 | 공급망 | 악성 npm/PyPI 패키지, 엔진 CLI 업데이트 | CI 없음(확인됨: `back/.github`, `front/.github` 없음) |
| T8 | 기기 분실·세션 탈취 | 휴대폰의 로그인 상태, refresh 쿠키 | 비밀번호 변경으로 refresh만 폐기 |

## 4. 공격 표면 (코드에서 센 수)

집계 방법: 합성 설정(`TTAKKARI_NO_ENV_FILE=1`, 임시 HOME, 합성 서명키)으로 `ttakkari.main.app`을 import해 FastAPI 0.143의 `_IncludedRouter`를 재귀로 펼쳤다.
MCP 도구는 `TTK_ROLE`별로 `mcp_server.server.list_tools()`를 호출해 셌다. 모두 확인됨.

| 표면 | 수 | 인증 방식 |
|---|---|---|
| HTTP operation 전체 | **408** (GET 202, POST 136, DELETE 33, PATCH 28, PUT 9) | |
| 소유자 Bearer(`require_user`) | 289 (그중 `user_access` 54) | access JWT |
| 에이전트 토큰(`require_orchestrator`) | 105 (`/api/agent` 90, `/api/orchestrator` 15), 그중 `agent_access` 42 | 세션 JWT |
| 업로드 토큰 또는 소유자(`_simple_auth`) | 2 | `ttk_` 토큰 |
| 인증 의존성 없음 | **12**: `/api/auth/{login,refresh,logout}`, `/api/view/{token}`, `/api/dl/{token}`, `/api/{health,ready,bootstrap-ready,version,qwen-status}`, `/api/public/shares/{sid}`, `/api/public/shares/{sid}/download` | 토큰 경로는 핸들러 안에서 검사 |
| WebSocket | **1**: `/api/remote-desktop/socket` (Origin + 30초 티켓 + grant) | |
| SSE | `/api/runs/{id}/events`, `/api/runs/{id}/stream` 등 | Bearer |
| MCP 도구 | agent **85**, orchestrator **96**(전용 11) | 세션 JWT |
| 서브프로세스 호출 지점 | **75**, `shell=True`·`create_subprocess_shell` **0** | |
| raw SQL `text()` | **17**, f-string SQL 1(`api/system.py:159`) | |
| 외부 HTTP 클라이언트 사용 파일 | 13 (github, weather, usage, desk_qwen, host_gpu, 배포 등) | |
| 미들웨어 | `ShareSecurityMiddleware`, `TransferDiagnosticsMiddleware`, `CORSMiddleware`, `InflightMiddleware` | |
| 운영 `/docs`, `/openapi.json` | 숨김. 확인됨: `127.0.0.1:8787`에서 둘 다 404(`main.py:236`, `is_prod` 참으로 추정) | |

에이전트 라우트 105개 중 27개는 핸들러 본문에 `pool`/`scope`/`work` 문자열도 `agent_access` 의존성도 없다(휴리스틱 집계).
메모 7, 일정 4, notify, system resources, host-gpu 4, execution-capabilities 3, deploy 2(헬퍼 `_session`에서 검사), orchestrator context·luna·`create_pms_task`가 여기에 든다.
이 수는 "범위 검사가 없다"는 뜻이 아니라 "명시적 검사를 눈으로 대조해야 한다"는 목록이다.

### 4.1 루프백·호스트 노출 (확인됨: `lsof -iTCP -sTCP:LISTEN`)

| 주소 | 프로세스 | 메모 |
|---|---|---|
| 127.0.0.1:8787 | caddy | 백엔드 진입점, cloudflared가 연결 |
| 127.0.0.1:20191 | caddy admin API | 같은 사용자 로컬 프로세스가 프록시 설정을 바꿀 수 있음(추론) |
| 127.0.0.1:20241 | cloudflared metrics | |
| 127.0.0.1:18987 | llama-server(운영 Qwen) | 인증 없음(추론) |
| `*:3306` | mariadbd | **따까리 밖이지만 LAN 노출** |
| `*:5000`, `*:7000` | ControlCenter(AirPlay) | macOS 기본 |

## 5. 위협 시나리오 (STRIDE 요약)

| ID | 시나리오 | 행위자 | 전제 | 영향 | 상태 | WBS |
|---|---|---|---|---|---|---|
| S1 | 엔진 명령줄의 에이전트 토큰을 `ps`로 읽어 다른 세션(개인 오케스트레이터) 권한으로 API 호출 | T4, T3, T6 | 같은 OS 사용자로 도는 다른 Run | work→personal 경계 무력화, 개인 기억·대화·주식 열람, 배포 요청 | **확인됨(노출)**: `ps` 결과 `claude` 프로세스 4개의 argv에 `TTK_ORCH_TOKEN` 문자열 존재(값은 출력하지 않음). Codex도 `-c mcp_servers.*.env=`로 argv 전달(`agents/codex.py:87-91`). 악용은 미실행 | SEC2-AGT-01 |
| S2 | 탈취한 에이전트 토큰을 Run 종료 뒤에도 사용 | T4, T3 | S1 또는 로그 유출 | 최대 `run_timeout_seconds+600`(기본 2시간 10분) 유효 | 확인됨(코드: `runtime.py:190`, 서버는 서명·만료만 검사 `runtime.py:196-208`) | SEC2-AGT-02 |
| S3 | 에이전트가 `~/.ttakkari/config.env`의 서명키를 읽어 소유자 JWT 위조 | T3, T5 | guard 우회(예: `python3 -c`) 또는 guard 없는 엔진 | 전권 | 추론. Claude·Muse guard는 휴리스틱이고 스스로 샌드박스가 아니라고 명시(`guard.py:3-6`). Antigravity에는 PreToolUse guard가 없음(`agents/antigravity.py:20-49`) | SEC2-EXEC-01 |
| S4 | 회사 세션이 개인 메모·일정을 읽고 회사 계정 엔진 로그로 넘김 | T4 | Memo·CalendarEvent에 scope 없음 | 개인정보 이동 | 확인됨(코드: `api/agent.py:360-430`에 pool 검사 없음), 실데이터 미시험 | SEC2-AUTHZ-01 |
| S5 | 회사 오케스트레이터가 `create_pms_task`로 개인 PMS 프로젝트에 작업 추가 | T4 | 개인 프로젝트 UUID 인지 | 개인 PMS 오염, 응답의 프로젝트 집계 노출 | 확인됨(코드: `api/orchestrator.py:496-499`는 scope 미검사, `api/agent_pms.py:116-144`의 `manage_pms`는 검사). 실행 재현 미실시 | SEC2-AUTHZ-02 |
| S6 | 비밀번호 변경·로그아웃 뒤에도 access JWT·원격제어 grant가 살아 있음 | T8 | 토큰 탈취 | 최대 15분 소유자 권한, 원격제어 연결 최대 2시간 | 확인됨(코드: `security/auth.py:58-71`은 서명만 검사, `api/auth.py:148-161` logout은 refresh만 폐기) | SEC2-AUTH-01 |
| S7 | 프롬프트 인젝션으로 배포·삭제·알림·외부 업로드 도구 호출 | T3 | 회수 문서·웹·GitHub 콘텐츠를 읽는 Run | 운영 코드 변경, 자료 삭제, NotebookLM으로 파일 반출 | 추론 | SEC2-PI-01, SEC2-HOST-03 |
| S8 | 에이전트가 `~/.claude`·`~/.codex`의 hook·지침을 바꿔 이후 모든 세션에 지속 | T3, T5 | 쓰기 허용 루트에 하네스 포함(`config.py:60,66`, 사용자 결정 10-11) | 대화형 세션까지 영향, guard 무력화 | 추론 | SEC2-EXEC-04 |
| S9 | 백업 없이 DB·작업공간 파괴 | T5, T3 | guard는 `dropdb`·광역 rm 등 일부만 차단 | 복구 불가 | **확인됨(백업 미동작)**: Time Machine 대상 마운트 실패 | SEC2-OPS-01 |
| S10 | 업로드 문서 변환(soffice)·미디어 처리 취약점 | T1(공유), T3 | 악성 docx/pptx/HEIC | 호스트 코드 실행 | 추론 | SEC2-FILE-01 |
| S11 | Antigravity 로그(`/private/tmp/ttakkari-agy-*.log`, 0644)에 프롬프트·지침이 평문 저장 | T6 | 로컬 다른 사용자 | 개인 지침·기억 요약 노출 | 확인됨(권한: 6개 파일 `-rw-r--r--`). 내용은 열지 않음 | SEC2-SECRET-01 |
| S12 | SPA에 CSP·frame-ancestors 없음 → XSS 피해 확대, 원격제어 비밀번호 입력 화면 클릭재킹 | T2 | 렌더러 XSS 또는 iframe 삽입 | refresh 호출로 토큰 획득 | 확인됨(`front/vercel.json`은 `/share/*`에만 헤더) | SEC2-WEB-01, SEC2-WEB-02 |
| S13 | 의존성 취약점·악성 패키지 | T7 | CI·감사 도구 없음 | 백엔드 코드 실행 | 미검증 | SEC2-SUP-01 |
| S14 | 공개 공유 토큰 추측·남용, 대용량 다운로드 DoS | T1 | 공개 엔드포인트 2개 | 스냅샷 노출, 대역폭 | 미검증 | SEC2-SHARE-01 |
| S15 | 로컬 프로세스가 `CF-Connecting-IP`를 위조해 IP별 로그인 제한·감사 IP 우회 | T3, T6 | 루프백 피어는 신뢰 프록시(`security/auth.py:76-88`) | 로그인 제한 회피(전역 버킷은 유지) | 추론 | SEC2-AUTH-03 |
| S16 | 루프백 관리 포트(caddy admin, llama-server)를 에이전트·브라우저 MCP가 조작 | T3 | 같은 호스트 | 프록시 경로 변경, 모델 서버 중단 | 추론 | SEC2-EXPO-01 |

## 6. 설계상 가정과 그 한계

1. **에이전트는 같은 macOS 사용자로 돈다.** 파일 권한으로는 에이전트와 백엔드를 구분할 수 없다.
   따라서 guard는 "사고 방지 보험"이고 보안 경계가 아니다(ADR 0003, `guard.py:3-6`).
   S1·S3·S8은 이 가정에서 나온다. 해결 방향은 SEC2-EXEC-01에서 결정한다(별도 OS 사용자, 키 보관 위치 이동, 위험 수용 중 택일).
2. **소유자는 한 명이다.** 소유자 API 289개에는 객체 단위 인가가 필요 없고, 경계는 인증·토큰 용도(aud)·work/personal 범위다.
3. **work 세션의 주 위험은 데이터 이동이다.** 회사 계정 엔진(회사 Claude 등)으로 개인 자료가 넘어가는 것을 막는 것이 목적이다.
   회사 업무 자체를 막으면 안 된다.
4. **사용자 요구는 "최악만 면하자"다.** 에이전트 행동을 잠그는 승인 프롬프트·전역 잠금은 수정 방향에서 배제한다.
   대신 토큰 수명 단축·노출 경로 제거·범위 검사·백업처럼 정상 사용을 바꾸지 않는 통제를 우선한다.

## 7. 이미 있는 통제 (근거)

| 통제 | 근거 | 이번 확인 |
|---|---|---|
| 로그인 IP별 실패 제한, 전역 실패 검증 버킷, 동시 검증 1 | `security/auth.py:99-223`, `config.py:337-343` | 확인됨: 아래 회귀 실행 통과 |
| refresh 회전·재사용 탐지·동시 갱신 직렬화, 비밀번호 변경 시 refresh 폐기 | `api/auth.py`, `tests/test_password_session_revocation.py` | 확인됨 |
| refresh/logout CSRF(Origin·Sec-Fetch-Site) | `api/auth.py` `_check_csrf`, `tests/test_csrf_defense.py` | 확인됨 |
| refresh 쿠키 `HttpOnly`, 운영 `Secure; SameSite=None`, 경로 한정 | `api/auth.py:33-44` | 코드 확인, 운영 응답 헤더는 미검증 |
| RunEvent 저장 전 비밀 마스킹 | `runs/events.py`, `oplog/scrub.py`, `tests/test_runs_events_masking.py` | 확인됨 |
| Claude hook fail-closed | `security/claude_hook.py`, `tests/test_claude_hook_fail_closed.py` | 확인됨 |
| work 세션 대화·주식·Artifact·forget·entities 범위 | `api/agent.py`, `api/stocks.py:28`, `tests/test_agent_scope_auth.py` | 확인됨 |
| 오케스트레이터 전용 도구 서버측 kind 검사 | `api/orchestrator.py:125-129` | 코드 확인 |
| 배포 요청 work 차단 | `api/deployments.py:25` | 코드 확인 |
| 파일 반출 민감 경로 정책 | `broker/policy.py:16-47` | 확인됨: `tests/test_broker_policy.py` 통과, 목록 완전성은 미검증 |
| 산출물 열람 CSP sandbox·nosniff | `api/artifacts.py:65-68,386-387`, 프런트 `iframe sandbox=""`(`ArtifactViewer.tsx:456`) | 코드 확인 |
| 운영 docs/openapi 숨김 | `main.py:236` | 확인됨(404) |

회귀 실행(이번 세션, `back` 작업 트리, 격리 DB `ttakkari_test`):

```
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_agent_scope_auth.py tests/test_csrf_defense.py \
    tests/test_runs_events_masking.py tests/test_claude_hook_fail_closed.py tests/test_api_auth.py \
    tests/test_api_artifacts.py tests/test_guard.py tests/test_password_session_revocation.py \
    tests/test_broker_policy.py tests/test_transfers.py
157 passed, 2 skipped, 2 xfailed in 28.16s
```

xfail 2건은 `tests/test_guard.py`의 알려진 guard 한계(변수 확장 민감 경로 읽기, 일반 Bash 외부 쓰기)다.
