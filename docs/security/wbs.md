# 따까리 보안 WBS (2026-10-19 재수립)

위협 모델은 [threat-model.md](threat-model.md)에 있다. 이 문서는 항목 단위 실행 계획이다.
ID는 이전 감사의 `SEC-01~14`와 섞이지 않게 `SEC2-` 접두사를 쓴다.

## 1. 요약

- 항목 65개: P0 9, P1 32, P2 22, P3 2. 2026-10-19 네트워크·로그인·세션 감사([network-login-session-audit.md](network-login-session-audit.md))로 6절의 NAC·LGN·SES 16개를 추가했다. 프롬프트 인젝션 집중 감사([prompt-injection-audit.md](prompt-injection-audit.md))로 SEC2-PI-03~06을 추가하고 SEC2-PI-01·02를 갱신했다.
- 심각도 = 가능성(L, 1~3) × 영향(I, 1~3). 우선순위는 심각도와 함께 '정상 사용을 바꾸지 않고 바로 줄일 수 있는가'를 반영했다.
- 작업 분할 단위는 작업자 1명이 2시간 안에 끝낼 크기다. 이전 작업자가 2시간 제한으로 끊긴 일이 있었다.
- 모든 수정 항목의 완료 기준은 같다. 재현 테스트가 수정 전 실패하고 수정 후 통과하며, 정상 사용 대조군이 함께 통과해야 한다.
- 상태 표기: 확인됨 / 부분 확인 / 추론 / 미검증. 근거 없는 수치는 `확인 필요`.

## 2. 이전 감사 커버리지 갭

이전 산출물(outbox `05748f33…`, `99ba5296…` HTML·CSV·ZIP)은 이번 실행 환경의 가드가 다른 Run의 outbox 읽기를 막아 열지 못했다(`[ttakkari-guard] ttakkari 보호 영역 접근 차단`).
대신 같은 감사 세션 기록(`fdc0a390`, `16409987`, `bfed2e76`, `bf29d2a5`, `ad3cc06c`), 저장소 기록 `docs/work-history/2026-10-19-security-hardening.md`, 커밋(`9477a74`, `7cf7c45`, `efcd1ab`, `8f91130`)으로 판정했다.
커밋·테스트로 확인되지 않은 완료 주장은 미검증으로 취급했다.

| 영역 | 이전 감사 상태 | 근거 | 이번 판정 |
|---|---|---|---|
| 로그인 무차별 대입 방어(IP별·전역 버킷·동시성) | 실행 증거로 확인 | test_password_session_revocation.py, 독립 검증 bfed2e76 | 확인됨: 이번 재실행 통과 |
| refresh 회전·재사용·동시 갱신·비밀번호 변경 시 폐기 | 실행 증거로 확인 | efcd1ab 회귀 테스트 | 확인됨: 이번 재실행 통과 |
| refresh/logout CSRF(Origin·Sec-Fetch-Site) | 실행 증거로 확인 | test_csrf_defense.py | 확인됨 |
| RunEvent 저장 전 비밀 마스킹 | 실행 증거로 확인 | test_runs_events_masking.py | 확인됨. 다른 로그 경로는 미확인 → SEC2-SECRET-01 |
| Claude hook fail-closed | 실행 증거로 확인 | test_claude_hook_fail_closed.py | 확인됨. 다른 엔진은 미확인 → SEC2-EXEC-03 |
| work 세션: 대화·주식·Artifact·forget·entities 범위 | 실행 증거로 확인 | test_agent_scope_auth.py, 8f91130 | 확인됨. 나머지 라우트 미확인 → SEC2-AUTHZ-03 |
| /api/agent 인증 선언 누락 0 | 실행 증거(OpenAPI 1회 집계, 75개) | 재검증 세션 bf29d2a5 | 이번 라우트 펼침 집계 90개(OpenAPI 집계와 방식 차이 가능, 확인 필요). 인증 선언 ≠ scope 인가 → SEC2-GATE-00 |
| SQLi | 부분 실행(Artifact 필터 1건) + 코드 읽기(system.py) | work-history 2026-10-19 | 부분 확인 → SEC2-INJ-01 |
| XSS·산출물 CSP | 주장(코드 읽기) | api/artifacts.py CSP 확인, 브라우저 시험 없음 | 미검증 → SEC2-WEB-02 |
| 전체 테스트 2278 passed | 주장(다른 작업자 보고) | bfed2e76 보고 | 이번 세션 미재실행. 보안 묶음 157 passed만 재실행 |
| 배포 성공(efcd1ab, 8f91130) | 주장(예약 requested/testing/draining) | 예약 4e5dcb2c, 580a6d9d | 배포 성공 여부는 이 문서 범위 밖, 미검증 |
| Memo·CalendarEvent 범위 | 잔여로 기록(추론) | 재검증 세션 | 코드 확인됨 → SEC2-AUTHZ-01 |
| 에이전트 토큰 argv 노출 | 손대지 않음 | - | 확인됨(ps) → SEC2-AGT-01 P0 |
| 에이전트 토큰 Run 종료 후 유효 | 한계로만 언급 | - | 확인됨(코드) → SEC2-AGT-02 P0 |
| 엔진 실행 경계(서명키 읽기) | 손대지 않음(guard 함수만) | - | 추론 → SEC2-EXEC-01 P0 |
| 백업·복구 | 손대지 않음 | - | 확인됨(Time Machine 실패) → SEC2-OPS-01 P0 |
| create_pms_task 범위 | 손대지 않음 | - | 확인됨(코드) → SEC2-AUTHZ-02 |
| access JWT·원격제어 grant 즉시 폐기 | 한계로만 언급 | - | 확인됨(코드) → SEC2-AUTH-01 |
| 원격 데스크톱 WebSocket | 손대지 않음 | - | 미검증 → SEC2-RD-01 |
| 호스트 MCP 도구(host_gpu·reverse·NotebookLM·토스·ArtCraft·게임) | 손대지 않음 | - | 미검증 → SEC2-HOST-01~05 |
| 파일 변환기(soffice·HEIC·STT) | 손대지 않음 | - | 추론 → SEC2-FILE-01 |
| SSRF·외부 호출 | 손대지 않음 | - | 미검증 → SEC2-NET-01 |
| 네트워크 노출·포트 | 손대지 않음 | - | 확인됨(lsof) → SEC2-EXPO-01 |
| 프롬프트 인젝션·기억 오염 | 손대지 않음 | - | 추론 → SEC2-PI-01/02. 집중 감사에서 R1·R2·R3 재현(확인됨) → SEC2-PI-02~06 |
| 의존성·공급망·CI | 손대지 않음(SEC-07 미완) | - | 미검증, CI 부재 확인 → SEC2-SUP-01, GATE-01 |
| SPA 보안 헤더(CSP·frame-ancestors) | 손대지 않음(SEC-09 미완) | - | 부재 확인 → SEC2-WEB-01 |
| Antigravity 로그 파일 권한 | 손대지 않음 | - | 확인됨(0644) → SEC2-SECRET-01 |
| 사고 대응 | 손대지 않음(SEC-13 미완) | - | → SEC2-OPS-02 |

요약: 이전 감사는 **인증(로그인·refresh·CSRF)과 work 세션 일부 범위**를 실행 증거와 함께 깊게 봤다.
에이전트 토큰 노출, 실행 경계, 백업, 원격 WebSocket, 호스트 도구, 변환기, SSRF, 네트워크 노출, 프롬프트 인젝션, 공급망은 손대지 않았다.
이전 OpenAPI 집계(75개)와 이번 라우트 펼침 집계(90개)가 다르다. 그 사이 NotebookLM 등 기능이 추가됐지만 집계 방식 차이도 있을 수 있어 원인은 확인 필요다. 어느 쪽이든 scope 인가는 라우트별로 확인하지 않았다.

## 3. 실행 계획(Phase)

### Phase 0 · 선행 결정·기준선

인벤토리 스냅샷 테스트와 신뢰 모델 결정 초안. 이후 모든 항목의 검증 기반.

- 항목: SEC2-DEC-01, SEC2-GATE-00, SEC2-NAC-01(6절 추가분)
- 종료 검증: 독립 검증: 인벤토리 테스트가 의도적으로 추가한 미분류 라우트를 잡는지 다른 모델이 재현.

### Phase 1 · P0

토큰 노출 제거, 실행 경계 실측, 범위 매트릭스, 운영 설정 fail-fast, 백업 복구 지점.

- 항목: SEC2-AGT-01, SEC2-AGT-02, SEC2-EXEC-01, SEC2-AUTHZ-03, SEC2-AUTH-02, SEC2-OPS-01
- 종료 검증: 독립 검증(GPT/Claude verification): 각 항목 RED→GREEN 재실행, 실제 CLI MCP 스모크, 복원 리허설 재실행. 통과 후 배포 예약.

### Phase 2 · P1

범위 결함 수정, 토큰 즉시 폐기, 엔진 guard 동등성, 인젝션 평가셋, 호스트 도구·배포·의존성·로그.

- 항목: SEC2-NAC-02~06, SEC2-LGN-01~04, SEC2-SES-01~02(6절 추가분), SEC2-AUTHZ-01, SEC2-AUTHZ-02, SEC2-AUTH-01, SEC2-AUTH-04, SEC2-EXEC-03, SEC2-EXEC-04, SEC2-PI-01, SEC2-PI-02, SEC2-PI-03, SEC2-PI-04, SEC2-PI-06, SEC2-HOST-03, SEC2-HOST-04, SEC2-DEP-01, SEC2-FILE-02, SEC2-RD-01, SEC2-WEB-02, SEC2-SUP-01, SEC2-FILE-01, SEC2-SECRET-01, SEC2-GATE-01
- 종료 검증: 독립 검증: 항목별 RED/GREEN, 정상 사용 대조군, 실제 브라우저(WebKit) 원격제어·XSS 확인.

### Phase 3 · P2

헤더·SSRF·노출·공유·DoS·주입·호스트 도구 세부 검증.

- 항목: SEC2-SES-03~06(6절 추가분), SEC2-AUTH-03(6절로 대체), SEC2-AUTH-05, SEC2-WEB-01, SEC2-NET-01, SEC2-EXPO-01, SEC2-SHARE-01, SEC2-DOS-01, SEC2-INJ-01, SEC2-INJ-02, SEC2-EXEC-02, SEC2-HOST-01, SEC2-HOST-02, SEC2-HOST-05, SEC2-FILE-03, SEC2-SECRET-02, SEC2-PI-05, SEC2-OPS-02, SEC2-AUTHZ-04
- 종료 검증: 독립 검증: 검증 로그 표본 재실행.

### Phase 4 · P3·종결

정보 최소화와 독립 종결 감사.

- 항목: SEC2-EXPO-02, SEC2-GATE-02
- 종료 검증: 종결 감사 자체가 검증 지점.

의존 순서: GATE-00 → (AGT-01 → AGT-02), (EXEC-01 → DEC-01 → EXEC-03·EXEC-04·EXEC-02), (AUTHZ-03 → AUTHZ-01·AUTHZ-02), AUTH-01 → RD-01·OPS-02, PI-01 → (PI-02, PI-03 → PI-04 → PI-05, PI-06).
OPS-01(백업)은 의존이 없으니 Phase 1에서 가장 먼저 시작한다. 이후 수정 작업이 데이터를 바꾸기 전에 복구 지점을 확보하기 위해서다.

## 4. 항목 표

| ID | P | 심각도(L×I) | 영역 | 제목 | 상태 | 의존 | task_type | Phase |
|---|---|---|---|---|---|---|---|---|
| SEC2-DEC-01 | P0 | 6 (2×3) | 실행 경계 | 에이전트 신뢰 모델 결정(같은 OS 사용자 문제) | 추론 | SEC2-EXEC-01 | design | 0 |
| SEC2-GATE-00 | P0 | 6 (3×2) | 검증 기반 | 보안 회귀 기준선과 라우트·도구 인벤토리 스냅샷 테스트 | 확인됨 | - | general | 0 |
| SEC2-AGT-01 | P0 | 6 (2×3) | 에이전트 토큰 | 에이전트 토큰의 명령줄(argv) 노출 제거 | 확인됨 | SEC2-GATE-00 | general | 1 |
| SEC2-AGT-02 | P0 | 4 (2×2) | 에이전트 토큰 | 에이전트 토큰을 Run 수명에 묶기(종료 즉시 무효) | 확인됨 | SEC2-AGT-01 | general | 1 |
| SEC2-EXEC-01 | P0 | 6 (2×3) | 실행 경계 | 엔진별 소유자 비밀 읽기 가능성 실측(canary) | 추론 | - | verification | 1 |
| SEC2-AUTHZ-03 | P0 | 6 (3×2) | 인가·범위 | 에이전트 라우트 105개·MCP 96개 work/personal 범위 매트릭스 테스트 | 부분 확인 | SEC2-GATE-00 | verification | 1 |
| SEC2-AUTH-02 | P0 | 3 (1×3) | 인증 | 운영 설정 안전값 확인과 기동 시 fail-fast | 부분 확인 | - | general | 1 |
| SEC2-OPS-01 | P0 | 6 (2×3) | 백업·복구 | DB·데이터 백업 복구 지점 확보와 복원 리허설 | 확인됨 | - | general | 1 |
| SEC2-AUTHZ-01 | P1 | 4 (2×2) | 인가·범위 | Memo·CalendarEvent에 scope 도입 | 확인됨(코드) | SEC2-AUTHZ-03 | general | 2 |
| SEC2-AUTHZ-02 | P1 | 2 (2×1) | 인가·범위 | create_pms_task 범위 검사 누락 수정 | 확인됨(코드) | SEC2-AUTHZ-03 | general | 2 |
| SEC2-AUTH-01 | P1 | 2 (1×2) | 인증 | access JWT·원격제어 grant 즉시 폐기(자격 epoch). **SEC2-SES-02가 `sid` 클레임으로 확장** | 확인됨(코드) | - | general | 2 |
| SEC2-AUTH-04 | P1 | 4 (2×2) | 인증 | 업로드 전용 토큰(ttk_) 권한·경로·폐기 검증 | 미검증 | - | verification | 2 |
| SEC2-EXEC-03 | P1 | 4 (2×2) | 실행 경계 | 엔진별 guard 동등성 확인(Antigravity hook 부재) | 확인됨(코드) | SEC2-EXEC-01 | verification | 2 |
| SEC2-EXEC-04 | P1 | 3 (1×3) | 실행 경계 | 하네스(~/.claude·~/.codex) 변경을 통한 지속성 대응 | 추론 | SEC2-DEC-01 | design | 2 |
| SEC2-PI-01 | P1 | 6 (2×3) | 프롬프트 인젝션 | 외부 콘텐츠→고위험 도구 경로 인벤토리와 인젝션 평가셋 | 부분 확인 | SEC2-GATE-00 | design | 2 |
| SEC2-PI-02 | P1 | 6 (2×3) | 프롬프트 인젝션 | 기억·엔터티 오염의 지속 주입(고정 항목 소유자 전용) | 확인됨(재현 R1) | SEC2-PI-01 | general | 2 |
| SEC2-PI-03 | P1 | 4 (2×2) | 프롬프트 인젝션 | 에이전트 간 메시지·인계 출처 envelope(작업 보고·handoff·접수 이력) | 확인됨(재현 R2) | SEC2-PI-01 | general | 2 |
| SEC2-PI-04 | P1 | 4 (2×2) | 프롬프트 인젝션 | 기억 출처 스키마와 사용자 결정 승격 금지(remember·메모·지식) | 확인됨(재현 R3) | SEC2-PI-03 | general | 2 |
| SEC2-PI-06 | P1 | 6 (2×3) | 프롬프트 인젝션 | egress canary 회귀 하네스와 소유 제어 지점 탐지 | 추론 | SEC2-PI-01 | general | 2 |
| SEC2-HOST-03 | P1 | 4 (2×2) | 호스트 도구 | NotebookLM: 로컬 파일 반출·세션 보관·범위 검증 | 미검증 | - | general | 2 |
| SEC2-HOST-04 | P1 | 3 (1×3) | 호스트 도구 | 토스증권 어댑터 읽기 전용·프로필 보호 검증 | 미검증 | - | verification | 2 |
| SEC2-DEP-01 | P1 | 6 (2×3) | 배포 | 배포 예약의 리비전 출처·게이트 검증 | 부분 확인 | SEC2-GATE-00 | verification | 2 |
| SEC2-FILE-02 | P1 | 4 (2×2) | 파일 | 민감 경로 정책 목록 완전성 | 추론 | - | general | 2 |
| SEC2-RD-01 | P1 | 3 (1×3) | 원격제어 WS | 원격 데스크톱 WebSocket 실제 브라우저 시험 | 미검증 | SEC2-AUTH-01 | verification | 2 |
| SEC2-WEB-02 | P1 | 6 (2×3) | 웹 프런트 | SPA 렌더러 XSS 코퍼스 시험 | 미검증 | - | verification | 2 |
| SEC2-SUP-01 | P1 | 4 (2×2) | 공급망 | 의존성 취약점 스캔과 도달성 판정 | 미검증 | - | verification | 2 |
| SEC2-FILE-01 | P1 | 3 (1×3) | 파일 | 문서·미디어 변환기 격리(soffice, HEIC, STT) | 추론 | - | general | 2 |
| SEC2-SECRET-01 | P1 | 4 (2×2) | 비밀 노출 | RunEvent 밖 로그의 비밀·개인정보 노출 | 부분 확인 | - | general | 2 |
| SEC2-GATE-01 | P1 | 4 (2×2) | 검증 기반 | 보안 회귀 묶음을 배포 게이트에 연결 | 확인됨(부재) | SEC2-GATE-00 | general | 2 |
| SEC2-AUTH-03 | P2 | 1 (1×1) | 인증 | 로그인 제한 정책 결정·루프백 IP 위조 (**6절 SEC2-LGN-01~04, NAC-03으로 대체**) | 추론 | - | general | 3 |
| SEC2-AUTH-05 | P2 | 2 (1×2) | 인증 | CSRF·CORS 전수 확인 | 부분 확인 | - | verification | 3 |
| SEC2-WEB-01 | P2 | 2 (1×2) | 웹 프런트 | SPA·API 보안 헤더(CSP, frame-ancestors, HSTS) | 확인됨(부재) | SEC2-WEB-02 | general | 3 |
| SEC2-NET-01 | P2 | 2 (1×2) | 외부 호출 | SSRF·외부 호출 목적지 검증 | 미검증 | - | verification | 3 |
| SEC2-EXPO-01 | P2 | 2 (1×2) | 네트워크 노출 | 포트·터널 노출 인벤토리와 루프백 관리 포트 | 확인됨(관측) | - | verification | 3 |
| SEC2-SHARE-01 | P2 | 2 (1×2) | 공개 공유 | 공개 공유 capability 토큰·다운로드 검증 | 미검증 | - | verification | 3 |
| SEC2-DOS-01 | P2 | 2 (1×2) | 가용성 | 자원 고갈 상한 점검(디스크·동시성·업로드) | 미검증 | - | verification | 3 |
| SEC2-INJ-01 | P2 | 2 (1×2) | 주입 | SQL 주입 전수 확인(raw text 17곳) | 부분 확인 | - | verification | 3 |
| SEC2-INJ-02 | P2 | 2 (1×2) | 주입 | 명령·인자 주입 확인(subprocess 75곳) | 부분 확인 | - | verification | 3 |
| SEC2-EXEC-02 | P2 | 2 (2×1) | 실행 경계 | guard strict xfail 2건 해소 검토 | 확인됨(xfail) | SEC2-DEC-01 | general | 3 |
| SEC2-HOST-01 | P2 | 2 (1×2) | 호스트 도구 | host_gpu 도구 허용 목록·자원 가드 검증 | 미검증 | - | verification | 3 |
| SEC2-HOST-02 | P2 | 2 (1×2) | 호스트 도구 | 리버스 엔지니어링 외부 저장소 처리 경계 | 미검증 | - | verification | 3 |
| SEC2-HOST-05 | P2 | 2 (1×2) | 호스트 도구 | ArtCraft MCP·게임 빌드 컨테이너 격리 확인 | 미검증 | - | verification | 3 |
| SEC2-FILE-03 | P2 | 2 (1×2) | 파일 | 작업공간·지식 출처 등록 경로 경계 | 부분 확인 | SEC2-FILE-02 | verification | 3 |
| SEC2-SECRET-02 | P2 | 1 (1×1) | 비밀 노출 | 오류 응답의 내부 정보 노출 | 미검증 | - | verification | 3 |
| SEC2-PI-05 | P2 | 3 (1×3) | 프롬프트 인젝션 | Run taint와 고위험 sink 강등, 외부 콘텐츠 Run 도구 프로필 축소 | 확인됨(코드) | SEC2-PI-04 | design | 3 |
| SEC2-OPS-02 | P2 | 2 (1×2) | 사고 대응 | 사고 대응 runbook(키·토큰·계정 회전) | 확인됨(부재) | SEC2-AUTH-01 | design | 3 |
| SEC2-AUTHZ-04 | P2 | 3 (1×3) | 인가·범위 | 토큰 용도(aud) 교차 매트릭스 | 부분 확인 | SEC2-GATE-00 | verification | 3 |
| SEC2-EXPO-02 | P3 | 1 (1×1) | 네트워크 노출 | 무인증 상태 엔드포인트 정보 최소화 | 확인됨(관측) | - | general | 4 |
| SEC2-GATE-02 | P3 | 2 (1×2) | 검증 기반 | 독립 종결 감사 | 해당 없음 | Phase 1~3 | verification | 4 |

## 5. 항목 상세

### SEC2-DEC-01 에이전트 신뢰 모델 결정(같은 OS 사용자 문제)

- 우선순위 P0, 심각도 6 (가능성 2 × 영향 3), 상태 **추론**, 영역 실행 경계, Phase 0, 권장 task_type `design`
- 자산: A2 서명키, A7 엔진 계정, A8 금융·Google 세션
- 위협 시나리오: 오염된 콘텐츠(T3)나 오작동 에이전트(T5)가 같은 사용자 권한으로 서명키·계정 파일을 읽으면 소유자 등가 권한을 얻는다.
- 현재 통제와 근거: guard는 휴리스틱이며 경계가 아니라고 명시(security/guard.py:3-6). Codex는 seatbelt workspace-write(agents/codex.py:80-83). Antigravity는 hook 없음(agents/antigravity.py:20-49).
- 검증 방법: SEC2-EXEC-01의 엔진별 canary 읽기 결과를 근거로 선택지 A/B/C의 비용·정상 사용 영향 표 작성. 결정은 사용자 확인 필요 항목으로 올린다.
- 수정 방향: A) 엔진을 별도 macOS 사용자로 실행 + 홈 권한 분리. B) 서명키를 백엔드 전용 키체인 ACL로 이동, Codex seatbelt에 ~/.ttakkari 읽기 거부, Antigravity sandbox 설정 확인. C) 위험 수용과 문서화.
- 의존: SEC2-EXEC-01
- 완료 기준: ADR 1건(선택지·근거·측정값·정상 사용 영향)과 사용자 결정 기록. 결정에 따른 후속 항목 ID 확정.
- 불변 조건(정상 사용): 에이전트의 ~/development 쓰기, 셸·테스트 실행, 네트워크 사용은 결정과 무관하게 유지한다.
- 작업 분할: 1) 선택지 비교 ADR 초안(2h)

### SEC2-GATE-00 보안 회귀 기준선과 라우트·도구 인벤토리 스냅샷 테스트

- 우선순위 P0, 심각도 6 (가능성 3 × 영향 2), 상태 **확인됨**, 영역 검증 기반, Phase 0, 권장 task_type `general`
- 자산: 전체
- 위협 시나리오: 새 기능이 추가될 때 범위 분류 없는 라우트가 생겨도 아무도 모른다. 이전 감사의 OpenAPI 집계는 /api/agent 75개였고, 이번 라우트 펼침 집계는 90개다(집계 방식 차이일 수 있어 확인 필요).
- 현재 통제와 근거: 없음. 이전 감사는 OpenAPI 1회 집계만 했다.
- 검증 방법: pytest가 app 라우트를 펼쳐 (method, path, auth 의존성, scope 분류)를 고정 목록과 비교. 분류 누락 라우트가 있으면 실패. MCP 도구도 role별 목록 비교.
- 수정 방향: tests/test_security_inventory.py 추가. 분류표(personal-only / work-allowed / shared / owner)를 코드에 둔다.
- 의존: 없음
- 완료 기준: 새 라우트를 분류 없이 추가하면 테스트가 실패(RED 시연)하고, 분류를 추가하면 통과. 현재 408 HTTP·1 WS·85/96 MCP가 모두 분류됨.
- 불변 조건(정상 사용): 테스트만 추가한다. 런타임 동작 변경 없음.
- 작업 분할: 1) 라우트 펼치기 헬퍼+스냅샷(2h) 2) MCP role 스냅샷(1h)

### SEC2-AGT-01 에이전트 토큰의 명령줄(argv) 노출 제거

- 우선순위 P0, 심각도 6 (가능성 2 × 영향 3), 상태 **확인됨**, 영역 에이전트 토큰, Phase 1, 권장 task_type `general`
- 자산: A5 에이전트 세션 토큰
- 위협 시나리오: 같은 사용자로 도는 다른 Run(work 세션, 인젝션된 작업자)이 ps -axww로 개인 오케스트레이터 토큰을 읽어 개인 범위 API를 호출한다.
- 현재 통제와 근거: 없음. Claude는 --mcp-config JSON을 argv로 전달(agents/claude.py:37-39), Codex는 -c mcp_servers.*.env=로 전달(agents/codex.py:87-91). ps 관측: claude 프로세스 4개 argv에 TTK_ORCH_TOKEN 문자열(값 미출력).
- 검증 방법: 단위: 각 어댑터 build_command 결과 argv 어디에도 토큰 값이 없음을 assert(합성 토큰). 통합: 격리 백엔드에서 Run 2개를 띄우고 한쪽에서 ps 출력에 다른 Run 토큰 해시가 없는지 확인.
- 수정 방향: MCP 설정을 실행별 0600 파일(guard 보호 영역, Run 종료 시 삭제)로 쓰고 경로만 전달. Codex는 CODEX_HOME 아래 config 파일 또는 env 상속으로 전달. Muse·Antigravity 경로도 대조.
- 의존: SEC2-GATE-00
- 완료 기준: argv 검사 테스트가 수정 전 실패·수정 후 통과. 실제 CLI(Claude·Codex·Antigravity·Muse 각 1회)에서 recall·list_artifacts MCP 호출 성공(대조군).
- 불변 조건(정상 사용): 모든 엔진의 MCP 도구 85/96개 호출, resume, desk warm pool 기동이 그대로 동작한다.
- 작업 분할: 1) Claude 파일 전달+테스트(2h) 2) Codex·Muse 전달 방식 변경+테스트(2h) 3) 실제 CLI MCP 스모크(verification, 2h)

### SEC2-AGT-02 에이전트 토큰을 Run 수명에 묶기(종료 즉시 무효)

- 우선순위 P0, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨**, 영역 에이전트 토큰, Phase 1, 권장 task_type `general`
- 자산: A5
- 위협 시나리오: 유출된 토큰이 Run 종료 뒤에도 최대 2시간 10분 유효하다.
- 현재 통제와 근거: exp=run_timeout+600(orchestrator/runtime.py:186-193), 서버는 서명·만료만 검사(runtime.py:196-208).
- 검증 방법: 종료(succeeded/failed/cancelled)된 Run의 토큰으로 /api/agent/sessions 호출 시 401, 활성 Run 토큰 200. 보관된 세션 토큰 401.
- 수정 방향: 토큰에 run_id(또는 jti) 포함, require_orchestrator에서 Run 활성·세션 미보관 확인(짧은 TTL 캐시). warm pool 프로세스는 Run 배정 시 토큰 발급 시점 검토.
- 의존: SEC2-AGT-01
- 완료 기준: 위 RED/GREEN 테스트와 장기 Run(2시간 근접)·resume·세대 전환 중 Run 대조군 통과.
- 불변 조건(정상 사용): 실행 중인 Run의 MCP 호출은 끊기지 않는다. 세대 배포 draining 중인 Run도 유지.
- 작업 분할: 1) 토큰 클레임+검사+테스트(2h) 2) warm pool·resume 대조(verification, 1h)

### SEC2-EXEC-01 엔진별 소유자 비밀 읽기 가능성 실측(canary)

- 우선순위 P0, 심각도 6 (가능성 2 × 영향 3), 상태 **추론**, 영역 실행 경계, Phase 1, 권장 task_type `verification`
- 자산: A2, A7, A8
- 위협 시나리오: 에이전트가 python -c 등으로 guard를 우회하거나 guard 없는 엔진에서 ~/.ttakkari/config.env를 읽어 소유자 JWT를 위조한다.
- 현재 통제와 근거: Claude·Muse PreToolUse guard(휴리스틱), Codex seatbelt(읽기 제한 여부 확인 필요), Antigravity --sandbox(의미 미검증), qwen-local 자체 하네스.
- 검증 방법: 격리 백엔드(TTAKKARI_HOME=임시 디렉터리)에 같은 구조의 canary config.env·accounts 파일을 둔다. 엔진 5종 각각에 'cat', 'python3 -c open()', 'base64', 변수 확장 4가지로 읽기를 시켜 결과를 표로 기록. 실제 비밀 파일은 열지 않는다.
- 수정 방향: 측정만 한다. 수정은 SEC2-DEC-01 결정 후.
- 의존: 없음
- 완료 기준: 엔진×방법 매트릭스(읽힘/차단/오류)와 실행 로그. 각 칸에 명령과 출력 1~3줄.
- 불변 조건(정상 사용): 운영 HOME·실제 계정 파일을 건드리지 않는다.
- 작업 분할: 1) canary 환경+Claude·Codex(2h) 2) Antigravity·Muse·qwen-local(2h)

### SEC2-AUTHZ-03 에이전트 라우트 105개·MCP 96개 work/personal 범위 매트릭스 테스트

- 우선순위 P0, 심각도 6 (가능성 3 × 영향 2), 상태 **부분 확인**, 영역 인가·범위, Phase 1, 권장 task_type `verification`
- 자산: A9~A13
- 위협 시나리오: work 세션 토큰으로 개인 자료를 읽거나 바꾸는 라우트가 남아 있다. 이전 감사는 대화·주식·Artifact·forget·entities만 확인했다.
- 현재 통제와 근거: agent_access 42개, 핸들러 내부 pool 검사 다수. 명시 검사 문자열이 없는 라우트 27개(휴리스틱 집계).
- 검증 방법: 개인 canary 자료(기억·엔터티·메모·일정·파일·회의·PMS·NotebookLM·리버스·운영로그)를 만든 뒤 work 토큰으로 모든 /api/agent·/api/orchestrator 라우트를 호출하는 parametrize 테스트. 응답 본문에 canary 문자열이 있거나 쓰기가 성공하면 실패.
- 수정 방향: 발견 항목별 최소 수정(별도 항목으로 분리). 매트릭스 자체는 회귀 테스트로 남긴다.
- 의존: SEC2-GATE-00
- 완료 기준: 105개 라우트 각각 기대 결과(허용/403/404/필터)를 가진 테스트. 현재 결함은 xfail(strict)로 표시해 숨기지 않음.
- 불변 조건(정상 사용): work 세션의 회사 PMS·회사 파일·work 기억 작업은 계속 성공(대조군 포함).
- 작업 분할: 1) fixture+agent.py 기억·메모·일정(2h) 2) host_env·deploy·notebooklm·stocks·reverse·oplogs·design(2h) 3) orchestrator 15개(2h)

### SEC2-AUTH-02 운영 설정 안전값 확인과 기동 시 fail-fast

- 우선순위 P0, 심각도 3 (가능성 1 × 영향 3), 상태 **부분 확인**, 영역 인증, Phase 1, 권장 task_type `general`
- 자산: A2, A3
- 위협 시나리오: 운영 env가 prod가 아니면 서명키가 공개 상수(dev-insecure...)로 대체되고 쿠키 Secure가 꺼진다(config.py:405-413, api/auth.py:36-38).
- 현재 통제와 근거: prod에서 서명키 비면 기동 실패(config.py:409-411). 확인됨: 127.0.0.1:8787의 /openapi.json·/docs가 404라 운영 프로세스는 is_prod 참으로 추정.
- 검증 방법: 격리 실행으로 env=dev+프로덕션 public_share_origin 조합에서 경고/실패 여부 확인. 운영 응답에서 Set-Cookie 속성(Secure, SameSite=None, HttpOnly, Path)을 로그인 없이 확인 가능한 범위로 확인. 서명키 값은 읽지 않고 길이·상수 일치 여부만 백엔드 자체 점검으로 보고.
- 수정 방향: 기동 시 검사: 서명키 길이 32바이트 이상, dev 상수 금지(prod), cors_origins에 localhost만 있으면 경고, trust_proxy_ip_header 설정 확인. /api/health에 노출하지 않고 로그로만.
- 의존: 없음
- 완료 기준: 잘못된 설정 조합 테스트가 수정 전 통과(문제 없음 표시)·수정 후 기동 실패. 정상 운영 설정 대조군 기동 성공.
- 불변 조건(정상 사용): 현재 운영 설정으로 기동·세대 전환이 실패하지 않는다(배포 전 dry-run).
- 작업 분할: 1) fail-fast 검사+테스트(2h)

### SEC2-OPS-01 DB·데이터 백업 복구 지점 확보와 복원 리허설

- 우선순위 P0, 심각도 6 (가능성 2 × 영향 3), 상태 **확인됨**, 영역 백업·복구, Phase 1, 권장 task_type `general`
- 자산: A9~A14, A16
- 위협 시나리오: 오작동·인젝션 에이전트가 DB나 ~/.ttakkari/data를 지우면 복구할 수 없다. 사용자 요구 '작업공간 파괴 방지'의 최후 방어선이 없다.
- 현재 통제와 근거: 확인됨: tmutil latestbackup 실패(Failed to mount destination, Code=17). 코드에 pg_dump 경로 없음(grep 0건, generation_bootstrap.py 언급만). guard는 dropdb·광역 rm 일부만 차단(guard.py:22-50).
- 검증 방법: 격리 DB에 백업→삭제→복원 리허설 명령을 실행하고 행 수·체크섬 비교. 백업 파일 위치가 에이전트 쓰기 루트 밖인지 확인.
- 수정 방향: 일일 pg_dump(-Fc) + data 디렉터리 스냅샷을 에이전트 쓰기 불가 위치에 보관(보존 7~30일, 확인 필요). Time Machine 대상 복구는 사용자 조치 필요. 복원 runbook 작성.
- 의존: 없음
- 완료 기준: 복원 리허설 로그(행 수 일치). 백업 작업이 스케줄에 등록되고 최근 성공 시각을 운영 상태에서 조회 가능.
- 불변 조건(정상 사용): 백업 중 서비스 무중단, DB 잠금 최소(pg_dump 일관 스냅샷).
- 작업 분할: 1) 백업 스크립트+격리 복원 리허설(2h) 2) 스케줄·상태 노출(2h)

### SEC2-AUTHZ-01 Memo·CalendarEvent에 scope 도입

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨(코드)**, 영역 인가·범위, Phase 2, 권장 task_type `general`
- 자산: A10
- 위협 시나리오: 회사 세션이 개인 메모·일정을 읽고 회사 계정 엔진으로 넘긴다.
- 현재 통제와 근거: 없음. agent_list_memos·agent_create_memo·agent_list_events 등에 pool 검사 없음(api/agent.py:360-430).
- 검증 방법: work 토큰으로 개인 메모·일정 canary 조회 시 노출(RED). 수정 후 미노출, work 메모는 조회 가능.
- 수정 방향: scope 컬럼(personal 기본) + Alembic 마이그레이션 + 기존 행 personal 백필. work 세션은 work만 조회·생성. 소유자 UI는 둘 다 보되 구분 표시.
- 의존: SEC2-AUTHZ-03
- 완료 기준: RED/GREEN 테스트, 마이그레이션 up/down 테스트, 소유자 UI 목록 대조군.
- 불변 조건(정상 사용): 개인 세션과 소유자 UI의 기존 메모·일정·체크리스트 동작 불변. 일정 알림 유지.
- 작업 분할: 1) 모델·마이그레이션(2h) 2) 에이전트 API 필터+테스트(2h) 3) 프런트 구분 표시(design, 2h)

### SEC2-AUTHZ-02 create_pms_task 범위 검사 누락 수정

- 우선순위 P1, 심각도 2 (가능성 2 × 영향 1), 상태 **확인됨(코드)**, 영역 인가·범위, Phase 2, 권장 task_type `general`
- 자산: PMS
- 위협 시나리오: 회사 오케스트레이터가 개인 PMS 프로젝트에 작업을 추가하고 응답의 프로젝트 집계를 본다.
- 현재 통제와 근거: api/orchestrator.py:496-499는 pms._project만 호출. manage_pms는 work_only 검사(api/agent_pms.py:116-144).
- 검증 방법: work 오케스트레이터로 개인 프로젝트에 POST → 수정 전 201(RED), 수정 후 404. 회사 프로젝트는 201.
- 수정 방향: agent_pms의 work_only 규칙을 공통 헬퍼로 빼서 재사용.
- 의존: SEC2-AUTHZ-03
- 완료 기준: RED/GREEN 테스트와 회사 프로젝트 대조군.
- 불변 조건(정상 사용): 개인 오케스트레이터의 PMS 작업 등록 불변.
- 작업 분할: 1) 수정+테스트(1h)

### SEC2-AUTH-01 access JWT·원격제어 grant 즉시 폐기(자격 epoch)

- 우선순위 P1, 심각도 2 (가능성 1 × 영향 2), 상태 **확인됨(코드)**, 영역 인증, Phase 2, 권장 task_type `general`
- 자산: A4, 원격제어
- 위협 시나리오: 비밀번호 변경·전체 로그아웃 뒤에도 탈취된 access JWT가 15분, 원격제어 연결이 최대 2시간 유지된다.
- 현재 통제와 근거: require_user는 서명·만료만 검사(security/auth.py:58-71). logout·logout_all은 refresh만 폐기(api/auth.py:148-161).
- 검증 방법: 비밀번호 변경 후 기존 access로 /api/auth/me 200(RED) → 수정 후 401. 기존 원격제어 grant로 티켓 발급 401, 활성 WS 종료.
- 수정 방향: Credential에 epoch(또는 updated_at 기반 세대) 두고 access·grant에 포함, 메모리 캐시로 비교. 비밀번호 변경·logout_all 때 epoch 증가와 원격제어 grants 폐기.
- 의존: 없음
- 완료 기준: RED/GREEN 테스트, 정상 갱신 흐름(refresh 회전) 대조군, 요청당 추가 DB 조회 0(캐시) 측정.
- 불변 조건(정상 사용): 평상시 강제 재로그인 없음. 에이전트 Run 토큰은 SEC2-AGT-02 규칙을 따른다.
- 작업 분할: 1) epoch 클레임+검사(2h) 2) 원격제어 grant 연동(2h)

### SEC2-AUTH-04 업로드 전용 토큰(ttk_) 권한·경로·폐기 검증

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **미검증**, 영역 인증, Phase 2, 권장 task_type `verification`
- 자산: A6, A12
- 위협 시나리오: 유출된 단축어 업로드 토큰으로 임의 dest_dir에 파일을 쓰거나 덮어쓴다.
- 현재 통제와 근거: 토큰 해시 조회·revoked_at 확인(api/transfers.py:228-241). 대상 경로는 is_sensitive·홈 검사(transfer/service.py:62).
- 검증 방법: 토큰으로 dest_dir에 ../, 심볼릭 링크, ~/.claude, 작업공간 밖 경로, 기존 파일명 충돌을 넣는 테스트. 토큰으로 /simple 외 라우트 호출 401.
- 수정 방향: 토큰은 inbox 하위로만 쓰기 제한(옵션), 덮어쓰기 금지, 마지막 사용 IP 기록.
- 의존: 없음
- 완료 기준: 경로 탈출 케이스 전부 거절 테스트, 정상 단축어 업로드 대조군.
- 불변 조건(정상 사용): 아이폰 단축어 업로드·HEIC 변환·위치 제거 동작 불변.
- 작업 분할: 1) 경로·권한 테스트+수정(2h)

### SEC2-EXEC-03 엔진별 guard 동등성 확인(Antigravity hook 부재)

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨(코드)**, 영역 실행 경계, Phase 2, 권장 task_type `verification`
- 자산: A12, A14
- 위협 시나리오: 같은 위험 명령이 Claude에서는 막히고 Antigravity에서는 실행된다.
- 현재 통제와 근거: Claude hook(agents/claude.py:26-27), Muse hook(security/muse_hook.py), Codex seatbelt, Antigravity는 --dangerously-skip-permissions --sandbox만(agents/antigravity.py:38-40).
- 검증 방법: 임시 작업공간에서 무해한 위험 명령 코퍼스(쓰기 루트 밖 더미 디렉터리 rm -rf, 보호 경로 canary 읽기, git push --force 더미 원격)를 엔진별 실행해 차단 여부 표로 기록.
- 수정 방향: agy CLI가 hook을 지원하면 동일 guard 연결, 아니면 sandbox 설정 강화 또는 결정 문서화.
- 의존: SEC2-EXEC-01
- 완료 기준: 엔진×명령 매트릭스와 차이 해소 또는 수용 기록.
- 불변 조건(정상 사용): 정상 개발 명령(pytest, git commit, pnpm build) 허용 유지.
- 작업 분할: 1) 코퍼스 실행·기록(2h) 2) 보완(2h)

### SEC2-EXEC-04 하네스(~/.claude·~/.codex) 변경을 통한 지속성 대응

- 우선순위 P1, 심각도 3 (가능성 1 × 영향 3), 상태 **추론**, 영역 실행 경계, Phase 2, 권장 task_type `design`
- 자산: A7, A14
- 위협 시나리오: 인젝션된 에이전트가 settings.json hook·CLAUDE.md·skills를 바꿔 이후 모든 세션(대화형 포함)에 지시를 심거나 guard를 끈다.
- 현재 통제와 근거: 쓰기 허용 루트에 ~/.claude, ~/.codex 포함(config.py:60,66, 사용자 결정 10-11). 토큰 파일만 민감 목록(broker/policy.py:47).
- 검증 방법: 격리 HOME에서 에이전트가 settings.json hooks를 바꾸면 다음 Run에 반영되는지 확인. 변경 감지 수단 유무 확인.
- 수정 방향: hook 정의 파일 변경 시 알림+diff 기록(차단 아님), 하네스 git 추적, Run 종료 보고에 하네스 변경 표시.
- 의존: SEC2-DEC-01
- 완료 기준: 하네스 변경이 알림·기록에 남는 테스트, 사용자 요청 하네스 수정 대조군.
- 불변 조건(정상 사용): 사용자가 요청한 하네스 수정은 계속 가능하다.
- 작업 분할: 1) 감지·알림(2h)

### SEC2-PI-01 외부 콘텐츠→고위험 도구 경로 인벤토리와 인젝션 평가셋

- 우선순위 P1, 심각도 6 (가능성 2 × 영향 3), 상태 **추론**, 영역 프롬프트 인젝션, Phase 2, 권장 task_type `design`
- 자산: A12, A14
- 위협 시나리오: 회수 문서·웹·GitHub 이슈·회의록·NotebookLM 응답·업로드 파일의 지시가 배포·삭제·알림·외부 업로드를 유발한다.
- 2026-10-19 갱신: 상태 추론 → **부분 확인**. 작업 분할 1)(sink·source 인벤토리)은 [prompt-injection-audit.md](prompt-injection-audit.md) 1~2장으로 완료했다. 진입점 15개 중 이메일, 백엔드 PDF 파서, GitHub→에이전트 경로는 미구현이다. 완화 1차는 SEC2-PI-03·05로 구체화했다. 남은 범위는 2) 실제 엔진 평가셋(T-S2·T-S3, 유도율 기준선)이다.
- 현재 통제와 근거: 일부 도구 설명에 'source data, not instructions' 문구. 기술적 출처 표시·제한 없음.
- 검증 방법: 합성 인젝션 문서를 recall·회의록·GitHub mock에 심고 실제 엔진 Run이 notify_user/forget/request_backend_deploy/notebooklm_add_source를 호출하는지 기록(격리 백엔드, 도구는 mock).
- 수정 방향: 고위험 sink 목록화, 도구 결과에 출처 태그, 배포는 커밋 작성 세션·origin/main 포함 확인, 외부 반출 도구는 민감 경로 차단. 승인 프롬프트는 추가하지 않는다.
- 의존: SEC2-GATE-00
- 완료 기준: 평가셋 N건(확인 필요)과 엔진별 유도율 기준선, 완화 후 재측정.
- 불변 조건(정상 사용): 정상 사용자 지시에 의한 같은 도구 호출은 그대로 성공.
- 작업 분할: 1) sink·source 인벤토리(design, 2h) 2) 평가셋+하네스(2h) 3) 완화 1차(2h)

### SEC2-PI-03 에이전트 간 메시지·인계 출처 envelope

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨(재현 R2)**, 영역 프롬프트 인젝션, Phase 2, 권장 task_type `general`
- 자산: A11, A14
- 위협 시나리오: 외부 문서에 오염된 작업자가 결과 본문에 `[작업 보고]`·`[접수에서 넘긴 사용자 요청]`을 써서, 기획 세션이 위조된 사용자 요청에 따라 작업자 생성·배포 예약을 한다. 계정 전환 인계가 작업 보고를 `사용자:`로 표기해 출처가 사용자로 바뀐다.
- 현재 통제와 근거: 서버 머리말 문자열뿐(`runs/manager.py:1849-1889`, `1781-1807`, `runs/desk.py:303-329`, `runs/context_usage.py:104-160`). `read_session`은 `origin`을 노출한다(좋은 선례, `api/agent.py:299-311`).
- 검증 방법: T-S6a·T-S6b·T-S6c·T-S7b(prompt-injection-audit.md 4.2). 격리 DB, fake 엔진 `PI_FORGE`.
- 수정 방향: 공통 envelope 함수(서버 nonce, 줄 인용, origin·run·worker 속성)를 작업 보고, 기획 보고, 접수 이력, 계정·한도·중단 인계, context_handoff, 자동 recall 삽입, 회의 전사 구분자에 적용한다. `_handoff_context`는 `Run.origin`별로 표기한다. `spawn_worker`/`message_worker`에 선택 `inputs` 인자와 서버 `derived_from` 기록을 더한다. Qwen spawn-queue 작업자에는 `qwen_degraded_intent`를 표기한다. 승인 단계는 추가하지 않는다.
- 의존: SEC2-PI-01
- 완료 기준: 위조 줄이 인용 안에만 남는 테스트, origin별 인계 표기 테스트, 정상 보고·인계 대조군.
- 불변 조건(정상 사용): 기획이 받는 보고 내용, 알림, 접수 중계 동작 불변. 보고당 토큰 증가량은 `확인 필요`.
- 작업 분할: 1) envelope 함수 + 작업 보고·인계(2h) 2) 접수 이력·context_handoff·자동 recall·회의 전사(2h) 3) spawn `inputs`·`derived_from`·Qwen 표기(2h)

### SEC2-PI-04 기억 출처 스키마와 사용자 결정 승격 금지

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨(재현 R3, 코드)**, 영역 프롬프트 인젝션, Phase 2, 권장 task_type `general`
- 자산: A9, A10
- 위협 시나리오: 외부 문장이 `remember(kind=decision|preference|instruction)`로 저장되어 이후 사용자 결정처럼 쓰인다. 에이전트가 사용자 메모에 덧붙인 문장이 `source=user`로 남는다. 에이전트가 지식 원장 추출물을 `source_reviewed`로 스스로 승격한다.
- 현재 통제와 근거: `remember`에 `source=agent`·`source_ref`가 남고 회수 결과에 노출된다(대조군 확인). 지식 원장은 증거 span 일치와 에이전트 `user_confirmed` 금지가 있다(`knowledge/store.py:287-289`, `api/knowledge.py:413-414`). 메모 덧붙임은 출처 구분이 없다(`api/agent.py:387-415`).
- 검증 방법: T-S8a·T-S8b·T-S8c·T-S5.
- 수정 방향: `CtxMemory.speaker`(user|agent|document|external)와 `derived_from`을 둔다. `remember`에 선택 `user_quote`를 받아, 세션 사슬의 user·desk Run 프롬프트 부분 문자열이면 `speaker=user`, 아니면 결정류를 `speaker=agent, kind=note`로 강등하고 이유를 돌려준다(차단 아님). 메모 덧붙임에는 서버 경계 줄과 `agent_appended`를 붙인다. 지식 원장은 사용자 주어의 decision·preference 승격에 증거 단위 `speaker=user`를 요구한다. 회수 결과에 `speaker`·`author`를 싣는다.
- 의존: SEC2-PI-03(provenance 체인 재사용)
- 완료 기준: 강등·경계 테스트와 사용자 인용 저장 대조군.
- 불변 조건(정상 사용): 사용자가 말한 사실·선호 저장, 회수 순위, 메모 덧붙임 기능 유지.
- 작업 분할: 1) 스키마 + `remember` 규칙(2h) 2) 메모 경계·회수 표시(2h) 3) 지식 승격 규칙(1h)

### SEC2-PI-05 Run taint와 고위험 sink 강등, 외부 콘텐츠 Run 도구 프로필 축소

- 우선순위 P2, 심각도 3 (가능성 1 × 영향 3), 상태 **확인됨(코드)**, 영역 프롬프트 인젝션, Phase 3, 권장 task_type `design`
- 자산: A9, A12, A14
- 위협 시나리오: 회의 전사·리버스 대상·웹 문서·NotebookLM 답변을 읽은 Run이 배포 예약, 고정 기억 변경, 임의 URL의 NotebookLM 등록을 한다. 회의 분석 Run은 쓰기 도구가 필요 없는데 MCP 전체 도구를 받는다(`meetings/pipeline.py:318-341`, `runs/manager.py:907-913`).
- 현재 통제와 근거: 서버측 범위·kind 검사(work/personal)는 확인됨. 같은 범위 안의 sink 선택은 모델 판단뿐이다.
- 검증 방법: T-S7a·T-S7c와 정상 `clean` Run 대조군.
- 수정 방향: 1단계로 MCP `TTK_ROLE=reader` 프로필을 회의 분석·날씨 Run에 적용한다(쓰기 도구 미등록). 2단계로 `Run.usage.taint`(clean|derived|external|compromised)를 Run 종류·도구 이벤트·provenance로 판정하고, `external` Run의 고위험 sink를 강등한다. 결정류 기억은 PI-04 규칙, 고정 항목은 제안 저장, 배포는 SEC2-DEP-01의 리비전 출처 조건 재사용, `notebooklm_add_source(url)`는 사용자 프롬프트에 나온 URL만 허용한다. 승인 단계는 추가하지 않는다. Dual-LLM 격리는 장기 연구로 둔다.
- 의존: SEC2-PI-04, SEC2-DEP-01(배포 조건 공유)
- 완료 기준: 강등 테스트, `clean` Run 동일 호출 성공 대조군, taint 판정 표.
- 불변 조건(정상 사용): 사용자 지시 배포·기억·NotebookLM 등록은 추가 확인 없이 성공. 회의 분석 결과 품질 불변.
- 작업 분할: 1) reader 프로필(2h) 2) taint 판정(2h) 3) sink 강등(2h)

### SEC2-PI-06 egress canary 회귀 하네스와 소유 제어 지점 탐지

- 우선순위 P1, 심각도 6 (가능성 2 × 영향 3), 상태 **추론**, 영역 프롬프트 인젝션, Phase 2, 권장 task_type `general`
- 자산: A2, A5, A7, A9
- 위협 시나리오: 인젝션된 Run이 비밀·개인 기억을 MCP 인자(NotebookLM 텍스트·URL 쿼리, 리버스 설명, 작업자 프롬프트), 브라우저 MCP 탐색 URL, git push, 셸 `curl`로 내보낸다.
- 현재 통제와 근거: egress 탐지 없음. 셸 네트워크 개방(`agents/codex.py:136`). RunEvent 마스킹은 저장 경로만 다룬다(`runs/events.py`).
- 검증 방법: T-S4a·T-S4b. 테스트별 `CANARY-PI-<uuid>`, `127.0.0.1:0` mock sink, NotebookLM 가짜 어댑터, 외부 호스트 호출 시 실패하는 전송 가드.
- 수정 방향: 1) 회귀 하네스(fixture, fake 엔진 `PI_EXFIL`) 2) 백엔드 소유 지점(MCP 쓰기 인자, 브라우저 MCP 래퍼, 작업공간 pre-push)에서 canary 일치 시 해당 호출만 거절하고 `security.canary_hit` 감사·알림. 실제 비밀 패턴은 탐지 기록만 3) 운영 미끼(canary 파일·기억) 배치는 사용자 결정 후. 셸 로컬 프록시 관측은 시험 후 판단. 1순위 방어는 SEC2-DEC-01의 읽기 거부다.
- 의존: SEC2-PI-01. SEC2-EXEC-01(canary 읽기 실측)·SEC2-SECRET-01(로그 canary)과 대상이 달라 중복이 아니다.
- 완료 기준: canary 반출 RED/GREEN, 정상 URL·텍스트 등록 대조군, 외부 호출 0 확인.
- 불변 조건(정상 사용): 정상 외부 조사·NotebookLM 등록·git push 불변. 오탐은 canary 일치에 한정.
- 작업 분할: 1) 하네스(2h) 2) MCP 인자 탐지(2h) 3) 브라우저·pre-push(2h)

### SEC2-HOST-03 NotebookLM: 로컬 파일 반출·세션 보관·범위 검증

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **미검증**, 영역 호스트 도구, Phase 2, 권장 task_type `general`
- 자산: A8, A12
- 위협 시나리오: 에이전트가 notebooklm_add_source로 민감 로컬 파일이나 회사 자료를 개인 Google로 올린다.
- 현재 통제와 근거: work 차단 agent_access(api/notebooklm.py:43), 업로드 상한 50MB(config.py:295), 세션 디렉터리 소유자 전용(설계 문구, config.py:278).
- 검증 방법: add_source에 .env·~/.ssh·회사 루트(~/development/hyperez) 경로 전달 테스트, work 토큰 403 테스트, 세션 파일 권한 확인(내용 미열람).
- 수정 방향: 파일 소스에 broker 정책(is_sensitive, company_roots) 적용.
- 의존: 없음
- 완료 기준: 민감·회사 경로 거절 테스트와 정상 문서 업로드 대조군.
- 불변 조건(정상 사용): 개인 노트북 생성·질의·소스 추가 정상.
- 작업 분할: 1) 경로 정책+테스트(2h)

### SEC2-HOST-04 토스증권 어댑터 읽기 전용·프로필 보호 검증

- 우선순위 P1, 심각도 3 (가능성 1 × 영향 3), 상태 **미검증**, 영역 호스트 도구, Phase 2, 권장 task_type `verification`
- 자산: A8
- 위협 시나리오: 주문·이체 화면 이동이나 로그인된 Chrome 프로필 탈취로 금융 피해.
- 현재 통제와 근거: ADR 0044 읽기 전용 설계, work 403(api/stocks.py:28), 수동 트리거 300초 간격(config.py:268).
- 검증 방법: 어댑터 코드에서 탐색 URL 허용 목록 정적 검사, 주문 경로 문자열 부재 테스트, 원격 디버깅 포트 사용 여부·바인딩 확인.
- 수정 방향: URL 허용 목록 강제, 프로필 디렉터리 0700 확인.
- 의존: 없음
- 완료 기준: 허용 목록 밖 이동 거절 테스트와 정상 잔고 조회 대조군(mock).
- 불변 조건(정상 사용): 현행 조회 기능 불변.
- 작업 분할: 1) 정적 검사+테스트(2h)

### SEC2-DEP-01 배포 예약의 리비전 출처·게이트 검증

- 우선순위 P1, 심각도 6 (가능성 2 × 영향 3), 상태 **부분 확인**, 영역 배포, Phase 2, 권장 task_type `verification`
- 자산: A14
- 위협 시나리오: 인젝션된 개인 에이전트가 검증 안 된 커밋을 push하고 배포를 예약한다.
- 현재 통제와 근거: work 세션 차단(api/deployments.py:25), deploy_gate.py 존재(내용 대조 필요), 스키마·준비 상태 검사(README).
- 검증 방법: origin/main에 없는 SHA, dirty 트리, 테스트 실패 커밋으로 예약 시도 → 거절 여부 테스트(격리 deployer).
- 수정 방향: origin/main 포함·서명 또는 커밋 작성자 확인, 보안 회귀 묶음을 배포 게이트에 포함(SEC2-GATE-01).
- 의존: SEC2-GATE-00
- 완료 기준: 거절 케이스 테스트와 정상 예약 대조군.
- 불변 조건(정상 사용): 사용자 standing policy의 적극 배포 흐름 유지.
- 작업 분할: 1) 게이트 대조·테스트(2h)

### SEC2-FILE-02 민감 경로 정책 목록 완전성

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **추론**, 영역 파일, Phase 2, 권장 task_type `general`
- 자산: A7, A8
- 위협 시나리오: 목록에 없는 자격 파일(.git-credentials, ~/.config/gh/hosts.yml, ~/.cloudflared/<UUID>.json, Chrome Cookies)이 반출·색인된다.
- 현재 통제와 근거: broker/policy.py:16-47 글롭·디렉터리 목록. cert.pem은 *.pem으로 막힘. ~/.cloudflared/<UUID>.json은 확인됨(파일 존재, 권한 0400)이나 목록 패턴과 불일치(추론).
- 검증 방법: 표 기반 is_sensitive 테스트에 후보 경로 20개(확인 필요) 추가 → RED 확인.
- 수정 방향: 패턴 추가, ~/.cloudflared·~/.config/gh·Chrome 프로필 디렉터리 차단.
- 의존: 없음
- 완료 기준: 후보 경로 RED→GREEN, 일반 소스 파일 반출 대조군.
- 불변 조건(정상 사용): 작업공간 일반 파일 열람·다운로드 불변.
- 작업 분할: 1) 테스트+패턴(1h)

### SEC2-RD-01 원격 데스크톱 WebSocket 실제 브라우저 시험

- 우선순위 P1, 심각도 3 (가능성 1 × 영향 3), 상태 **미검증**, 영역 원격제어 WS, Phase 2, 권장 task_type `verification`
- 자산: 호스트 화면 제어
- 위협 시나리오: Origin 위조·티켓 재사용·grant 연장으로 화면 제어권 획득.
- 현재 통제와 근거: Origin 허용 목록·동시 2개(api/remote_desktop.py:121-123), 30초 일회 티켓, 재인증 grant(api/remote_desktop.py:91-107).
- 검증 방법: 격리 백엔드+가짜 VNC 서버로 Playwright(WebKit): 잘못된 Origin, 티켓 재사용, 만료 grant, 3번째 연결, 비밀번호 변경 후 기존 연결 처리. 실제 화면 공유는 쓰지 않는다.
- 수정 방향: 발견 시 최소 수정. 비밀번호 변경 연동은 SEC2-AUTH-01.
- 의존: SEC2-AUTH-01
- 완료 기준: 시나리오별 결과 로그와 정상 연결 대조군.
- 불변 조건(정상 사용): 아이폰 원격 조작 정상 연결 유지.
- 작업 분할: 1) 가짜 VNC+WS 시험(2h)

### SEC2-WEB-02 SPA 렌더러 XSS 코퍼스 시험

- 우선순위 P1, 심각도 6 (가능성 2 × 영향 3), 상태 **미검증**, 영역 웹 프런트, Phase 2, 권장 task_type `verification`
- 자산: A4(메모리 토큰), refresh 쿠키
- 위협 시나리오: 에이전트 출력·회의록·GitHub·NotebookLM 응답의 HTML/마크다운이 SPA origin에서 실행되어 refresh로 토큰을 얻는다.
- 현재 통제와 근거: dangerouslySetInnerHTML 사용 0건(grep), 산출물 미리보기 iframe sandbox=""(ArtifactViewer.tsx:456), API 열람 응답 CSP sandbox(api/artifacts.py:65-68).
- 검증 방법: XSS 페이로드(javascript: 링크, SVG onload, 마크다운 raw HTML, 이미지 onerror)를 각 렌더러에 넣는 vitest + 실제 브라우저 확인.
- 수정 방향: 마크다운 raw HTML 비활성 확인, 링크 프로토콜 허용 목록.
- 의존: 없음
- 완료 기준: 페이로드 미실행 테스트와 정상 렌더링 대조군.
- 불변 조건(정상 사용): 마크다운·코드·이미지 표시 품질 불변.
- 작업 분할: 1) 렌더러 목록+vitest(2h) 2) 브라우저 확인(1h)

### SEC2-SUP-01 의존성 취약점 스캔과 도달성 판정

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **미검증**, 영역 공급망, Phase 2, 권장 task_type `verification`
- 자산: A14
- 위협 시나리오: 알려진 취약 의존성(FastAPI 0.143 생태계, Vite, LibreOffice 등)이 악용된다.
- 현재 통제와 근거: lock 파일만 있음(uv.lock, pnpm-lock.yaml). CI 없음(.github 없음).
- 검증 방법: uvx pip-audit 또는 osv-scanner로 uv.lock, pnpm audit --prod로 front. 결과별 도달성(import·호출 경로) 판정.
- 수정 방향: 도달 가능한 High 이상 갱신, 나머지는 수용 기록.
- 의존: 없음
- 완료 기준: 스캔 원본 출력, 항목별 판정표, 갱신 후 전체 테스트 통과.
- 불변 조건(정상 사용): 갱신으로 정상 기능 회귀 없음(전체 pytest·vitest).
- 작업 분할: 1) 스캔+판정(2h) 2) 갱신(2h)

### SEC2-FILE-01 문서·미디어 변환기 격리(soffice, HEIC, STT)

- 우선순위 P1, 심각도 3 (가능성 1 × 영향 3), 상태 **추론**, 영역 파일, Phase 2, 권장 task_type `general`
- 자산: A15, 호스트
- 위협 시나리오: 악성 docx/pptx/오디오가 변환기 취약점으로 호스트 코드를 실행하거나 자원을 고갈시킨다.
- 현재 통제와 근거: soffice 경로 탐색·subprocess 실행(artifacts/preview.py:27-77), shell=True 0건. 타임아웃·프로필 격리는 대조 필요.
- 검증 방법: 변환 호출 인자 검사(임시 UserInstallation, 매크로 비활성, 타임아웃), 대용량·압축 폭탄 파일 테스트(격리).
- 수정 방향: 임시 프로필·타임아웃·크기 상한, 가능하면 sandbox-exec 프로필.
- 의존: 없음
- 완료 기준: 인자·타임아웃 테스트와 정상 미리보기 대조군.
- 불변 조건(정상 사용): PPTX·DOCX 미리보기, HEIC 변환, 회의 전사 정상.
- 작업 분할: 1) soffice 격리(2h) 2) 미디어·STT 상한 대조(2h)

### SEC2-SECRET-01 RunEvent 밖 로그의 비밀·개인정보 노출

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **부분 확인**, 영역 비밀 노출, Phase 2, 권장 task_type `general`
- 자산: A5, A9, A11
- 위협 시나리오: 엔진 로그·guard.log·배포 로그·접근 로그(URL 속 다운로드 토큰)에 비밀이 남고 다른 사용자가 읽는다.
- 현재 통제와 근거: RunEvent 마스킹 확인됨. Antigravity 로그는 /private/tmp/ttakkari-agy-<run>.log, 권한 0644 확인됨(6개). 내용 미열람.
- 검증 방법: 격리 Run에 canary 토큰·비밀번호를 넣고 모든 로그 경로를 grep. 파일 권한 검사 테스트.
- 수정 방향: 엔진 로그를 0600 사설 디렉터리로, 보존 기간, scrub 적용. 다운로드 토큰 URL 접근 로그 마스킹.
- 의존: 없음
- 완료 기준: canary 미검출, 로그 권한 0600 테스트, 진단 정보 보존 대조군.
- 불변 조건(정상 사용): 운영 이슈 수집·배포 로그 조회 기능 유지.
- 작업 분할: 1) 로그 경로 목록+canary(2h) 2) 권한·scrub 수정(2h)

### SEC2-GATE-01 보안 회귀 묶음을 배포 게이트에 연결

- 우선순위 P1, 심각도 4 (가능성 2 × 영향 2), 상태 **확인됨(부재)**, 영역 검증 기반, Phase 2, 권장 task_type `general`
- 자산: 전체
- 위협 시나리오: 수정한 보안 결함이 이후 기능 작업에서 회귀한다.
- 현재 통제와 근거: CI 없음. deploy_gate.py 내용 대조 필요.
- 검증 방법: 게이트에 pytest -m security 실행을 넣고, 일부러 깨뜨린 커밋이 거절되는지 격리 deployer로 확인.
- 수정 방향: pytest marker, 게이트 단계 추가, strict xfail 수 변화 보고.
- 의존: SEC2-GATE-00
- 완료 기준: 깨진 커밋 거절·정상 커밋 통과 로그.
- 불변 조건(정상 사용): 게이트 추가 시간 60초 이내(확인 필요), 무중단 배포 유지.
- 작업 분할: 1) marker+게이트(2h)

### SEC2-AUTH-03 로그인 제한 정책 결정·루프백 IP 위조

> 2026-10-19 갱신: 로그인 제한은 SEC2-LGN-01~04, 루프백 IP 위조는 SEC2-NAC-03으로 세분화해 대체했다. 이 항목은 두 항목이 완료되면 닫는다.

- 우선순위 P2, 심각도 1 (가능성 1 × 영향 1), 상태 **추론**, 영역 인증, Phase 3, 권장 task_type `general`
- 자산: A1
- 위협 시나리오: 루프백 피어가 신뢰 프록시라 로컬 프로세스가 CF-Connecting-IP를 위조해 IP별 제한·감사 IP를 우회한다. 5회 실패 15분 잠금은 사용자 결정 대기.
- 현재 통제와 근거: client_ip(security/auth.py:76-88), 전역 실패 버킷은 유지.
- 검증 방법: 127.0.0.1에서 위조 헤더로 6회 실패 시 잠금 대상 IP 확인 테스트.
- 수정 방향: cloudflared→Caddy 경로만 헤더 신뢰(Caddy가 헤더 재작성), 정책값은 사용자 결정.
- 의존: 없음
- 완료 기준: 위조 무시 테스트와 Cloudflare 경유 정상 IP 대조군.
- 불변 조건(정상 사용): 정상 로그인·자동 로그인 영향 없음.
- 작업 분할: 1) 테스트+수정(2h)

### SEC2-AUTH-05 CSRF·CORS 전수 확인

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **부분 확인**, 영역 인증, Phase 3, 권장 task_type `verification`
- 자산: A3
- 위협 시나리오: 쿠키를 쓰는 다른 경로나 CORS credentials 설정 오류.
- 현재 통제와 근거: refresh/logout CSRF 확인됨. 나머지는 Bearer.
- 검증 방법: 쿠키 읽는 라우트 전수 grep 테스트, CORS preflight 응답 검사(허용 origin·credentials).
- 수정 방향: 발견 시 최소 수정.
- 의존: 없음
- 완료 기준: 쿠키 라우트 목록 고정 테스트.
- 불변 조건(정상 사용): 프런트·단축어·MCP 호출 불변.
- 작업 분할: 1) 확인(1h)

### SEC2-WEB-01 SPA·API 보안 헤더(CSP, frame-ancestors, HSTS)

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **확인됨(부재)**, 영역 웹 프런트, Phase 3, 권장 task_type `general`
- 자산: A4
- 위협 시나리오: CSP 없는 SPA에서 XSS 피해 확대, 비밀번호 재입력 화면 클릭재킹.
- 현재 통제와 근거: vercel.json 헤더는 /share/*만. 로컬 API 응답에 보안 헤더 없음(server: uvicorn만, Cloudflare 추가 여부 확인 필요).
- 검증 방법: 배포 SPA 응답 헤더 확인, CSP report-only로 위반 수집.
- 수정 방향: report-only → enforce, frame-ancestors 'none'.
- 의존: SEC2-WEB-02
- 완료 기준: enforce 후 주요 화면 브라우저 스윕 무오류.
- 불변 조건(정상 사용): PWA·푸시·원격제어·외부 폰트 정상.
- 작업 분할: 1) report-only(2h) 2) enforce(2h)

### SEC2-NET-01 SSRF·외부 호출 목적지 검증

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 외부 호출, Phase 3, 권장 task_type `verification`
- 자산: 루프백 서비스
- 위협 시나리오: 사용자·에이전트가 준 URL로 서버가 루프백 관리 포트나 내부 주소를 호출한다.
- 현재 통제와 근거: github 고정 API, weather 고정 호스트, reverse는 https 허용 호스트(config.py:384), NotebookLM URL은 Google이 가져옴(추론).
- 검증 방법: URL을 받는 모든 입력 목록화 후 127.0.0.1:20191, 169.254.169.254, file:// 입력 테스트. github logs 리다이렉트 추적 대상 확인.
- 수정 방향: 목적지 허용 목록·리다이렉트 제한.
- 의존: 없음
- 완료 기준: 거절 테스트와 정상 GitHub·날씨 대조군.
- 불변 조건(정상 사용): GitHub 탐색·리버스 수집 정상.
- 작업 분할: 1) 목록+테스트(2h)

### SEC2-EXPO-01 포트·터널 노출 인벤토리와 루프백 관리 포트

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **확인됨(관측)**, 영역 네트워크 노출, Phase 3, 권장 task_type `verification`
- 자산: A14, A15
- 위협 시나리오: Caddy admin(127.0.0.1:20191)·llama-server를 로컬 프로세스가 조작, LAN에 열린 mariadbd(*:3306).
- 현재 통제와 근거: 백엔드 루프백 바인딩 확인됨. Cloudflare Access 사용 여부·터널 ingress 기본 404 여부 미검증(ttakkari.yml 0644, 내용 미열람).
- 검증 방법: lsof 스냅샷 비교 스크립트, 터널 ingress 규칙 확인(사용자 승인 하에), 외부에서 공개 경로 12개 외 응답 없음 확인.
- 수정 방향: Caddy admin off 또는 유닉스 소켓, mariadb 바인딩 루프백(따까리 밖, 사용자 결정).
- 의존: 없음
- 완료 기준: 노출 표와 변경 후 lsof 비교.
- 불변 조건(정상 사용): 서비스 접속·배포 전환 정상.
- 작업 분할: 1) 인벤토리·권고(2h)

### SEC2-SHARE-01 공개 공유 capability 토큰·다운로드 검증

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 공개 공유, Phase 3, 권장 task_type `verification`
- 자산: 공유 스냅샷
- 위협 시나리오: 토큰 추측·폐기 후 접근·콘텐츠 실행.
- 현재 통제와 근거: ShareSecurityMiddleware 헤더(sharing/middleware.py:9), 공개 라우트 2개, public_sharing_enabled.
- 검증 방법: 토큰 엔트로피·만료·폐기 테스트, 다운로드 Content-Type·CSP 확인, 존재하지 않는 sid 응답 시간 차이.
- 수정 방향: 발견 시 최소 수정.
- 의존: 없음
- 완료 기준: 시나리오 테스트 통과 로그.
- 불변 조건(정상 사용): 공유 링크 열람 정상.
- 작업 분할: 1) 검증(2h)

### SEC2-DOS-01 자원 고갈 상한 점검(디스크·동시성·업로드)

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 가용성, Phase 3, 권장 task_type `verification`
- 자산: A15
- 위협 시나리오: 업로드(200MB, 조각 전송 20GB)·Run 동시 16×3·STT 2시간·호스트 GPU로 디스크·메모리 고갈.
- 현재 통제와 근거: InflightMiddleware, 설정 상한(config.py:187-189, 325-329), host_gpu 메모리 가드.
- 검증 방법: 디스크 여유 하한 시 업로드 거절 여부, SSE 연결 수 상한 확인(격리).
- 수정 방향: 디스크 워터마크, 연결 상한.
- 의존: 없음
- 완료 기준: 상한 테스트와 정상 대용량 전송 대조군.
- 불변 조건(정상 사용): 동영상 전송 정상.
- 작업 분할: 1) 점검(2h)

### SEC2-INJ-01 SQL 주입 전수 확인(raw text 17곳)

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **부분 확인**, 영역 주입, Phase 3, 권장 task_type `verification`
- 자산: DB
- 위협 시나리오: 동적 SQL·정렬 키·LIKE 와일드카드로 데이터 노출.
- 현재 통제와 근거: f-string SQL 1곳(api/system.py:159, 서버 고정 버킷), 정렬은 SORTS dict(oplog/views.py:111). 이전 감사 탐침 1건.
- 검증 방법: 17곳 개별 검토 표 + 검색 파라미터 퍼징 테스트.
- 수정 방향: 발견 시 바인딩.
- 의존: 없음
- 완료 기준: 검토 표와 퍼징 테스트.
- 불변 조건(정상 사용): 검색 결과 불변.
- 작업 분할: 1) 검토+테스트(2h)

### SEC2-INJ-02 명령·인자 주입 확인(subprocess 75곳)

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **부분 확인**, 영역 주입, Phase 3, 권장 task_type `verification`
- 자산: 호스트
- 위협 시나리오: '-'로 시작하는 git ref·파일명·모델명이 옵션으로 해석된다.
- 현재 통제와 근거: shell=True 0건 확인됨.
- 검증 방법: 외부 입력이 닿는 subprocess 목록화, '--upload-pack=' 같은 옵션 주입 테스트.
- 수정 방향: '--' 구분자, 값 검증.
- 의존: 없음
- 완료 기준: 목록과 테스트.
- 불변 조건(정상 사용): 기존 명령 동작 불변.
- 작업 분할: 1) 목록+테스트(2h)

### SEC2-EXEC-02 guard strict xfail 2건 해소 검토

- 우선순위 P2, 심각도 2 (가능성 2 × 영향 1), 상태 **확인됨(xfail)**, 영역 실행 경계, Phase 3, 권장 task_type `general`
- 자산: A12
- 위협 시나리오: 변수 확장 민감 경로 읽기, 일반 Bash 외부 쓰기.
- 현재 통제와 근거: tests/test_guard.py strict xfail 2건.
- 검증 방법: xfail 테스트가 통과하도록 보완 후 정상 명령 코퍼스 대조.
- 수정 방향: 확장 인식 경로 판정, 리디렉션 대상 검사. 경계가 아니라 보험임을 유지.
- 의존: SEC2-DEC-01
- 완료 기준: xfail 2건 통과, 정상 개발 명령 오탐 0.
- 불변 조건(정상 사용): 일반 개발 셸 허용.
- 작업 분할: 1) 보완(2h)

### SEC2-HOST-01 host_gpu 도구 허용 목록·자원 가드 검증

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 호스트 도구, Phase 3, 권장 task_type `verification`
- 자산: A15
- 위협 시나리오: 임의 모델·포트 기동, 메모리 고갈.
- 현재 통제와 근거: 모델 허용 목록·포트 범위·예약 포트(config.py:215-228), work 차단(api/host_env.py:37).
- 검증 방법: 허용 목록 밖 모델·예약 포트·동시 2개 기동 거절 테스트.
- 수정 방향: 발견 시 최소 수정.
- 의존: 없음
- 완료 기준: 거절 테스트.
- 불변 조건(정상 사용): 허용 모델 시험 정상.
- 작업 분할: 1) 검증(1h)

### SEC2-HOST-02 리버스 엔지니어링 외부 저장소 처리 경계

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 호스트 도구, Phase 3, 권장 task_type `verification`
- 자산: 호스트, 회사 자료
- 위협 시나리오: 외부 저장소 코드가 호스트에서 실행되거나 회사 자료가 개인 모델로 간다.
- 현재 통제와 근거: https 허용 호스트, company_roots 거부(config.py:384-386), 파일 크기 상한(reverse_tasks.py:66).
- 검증 방법: clone 후 빌드·실행 경로 부재 확인, 회사 경로 입력 거절 테스트.
- 수정 방향: 발견 시 최소 수정.
- 의존: 없음
- 완료 기준: 테스트.
- 불변 조건(정상 사용): 리버스 기능 정상.
- 작업 분할: 1) 검증(2h)

### SEC2-HOST-05 ArtCraft MCP·게임 빌드 컨테이너 격리 확인

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **미검증**, 영역 호스트 도구, Phase 3, 권장 task_type `verification`
- 자산: 호스트
- 위협 시나리오: 빌드 스크립트의 네트워크·마운트 탈출.
- 현재 통제와 근거: docker 자원 상한·레지스트리 허용(config.py:309-319), games/egress_proxy.js, ArtCraft 경로 guard(guard.py:167-187).
- 검증 방법: 빌드 컨테이너에서 허용 밖 호스트 접속·호스트 경로 쓰기 시도 테스트.
- 수정 방향: 발견 시 최소 수정.
- 의존: 없음
- 완료 기준: 테스트.
- 불변 조건(정상 사용): 게임 빌드 정상.
- 작업 분할: 1) 검증(2h)

### SEC2-FILE-03 작업공간·지식 출처 등록 경로 경계

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **부분 확인**, 영역 파일, Phase 3, 권장 task_type `verification`
- 자산: A12
- 위협 시나리오: 심볼릭 링크·TOCTOU로 허용 루트 밖이나 민감 파일이 색인·반출된다.
- 현재 통제와 근거: indexer가 민감·따까리 홈 거부, 심볼릭 링크 제외(context/indexer.py:102,123), allowed_roots(config.py:57-61).
- 검증 방법: 심볼릭 링크·하위 .env·이름 변경 경쟁 테스트.
- 수정 방향: 발견 시 최소 수정.
- 의존: SEC2-FILE-02
- 완료 기준: 테스트.
- 불변 조건(정상 사용): 지식 출처 등록 정상.
- 작업 분할: 1) 검증(2h)

### SEC2-SECRET-02 오류 응답의 내부 정보 노출

- 우선순위 P2, 심각도 1 (가능성 1 × 영향 1), 상태 **미검증**, 영역 비밀 노출, Phase 3, 권장 task_type `verification`
- 자산: 내부 경로·SQL
- 위협 시나리오: 500·4xx 응답에 스택·경로·SQL이 섞인다.
- 현재 통제와 근거: broker 오류 메시지 사용자 노출 수준(broker/policy.py:52-58).
- 검증 방법: 예외 유발 요청 코퍼스로 응답 본문 검사.
- 수정 방향: 전역 예외 처리기에서 요약만 반환.
- 의존: 없음
- 완료 기준: 테스트.
- 불변 조건(정상 사용): 사용자 오류 메시지 유용성 유지.
- 작업 분할: 1) 검증(1h)

### SEC2-PI-02 기억·엔터티 오염의 지속 주입(고정 항목 소유자 전용)

- 2026-10-19 갱신: P2 → **P1**, 상태 추론 → **확인됨(재현 R1)**, Phase 3 → 2, task_type `general`. 에이전트 `describe_entity`가 기존 고정 엔터티의 `pinned`를 유지한 채 `summary`를 바꾸고(`context/store.py:44-70`, `api/agent.py:167-175`), 그 요약이 모든 Run 시스템 노트에 들어간다(`runs/manager.py:1712-1714`). 수정 방향: 에이전트는 고정 엔터티·고정 기억의 요약·별칭을 바꾸지 못하고 `proposed_summary`로 저장하며, 반영은 앱에서 사용자가 한다. 재현 T-S1(prompt-injection-audit.md 4.2). 작업 분할: 1) 불변식 + T-S1 RED/GREEN(2h). 아래는 원래 기록이다.
- 우선순위 P2(원래), 심각도 2 (가능성 1 × 영향 2), 상태 **추론**(원래), 영역 프롬프트 인젝션, Phase 3, 권장 task_type `verification`
- 자산: A9
- 위협 시나리오: 인젝션된 에이전트가 remember·describe_entity로 저장한 지시가 이후 recall·고정 요약(context/store.py:135-147)으로 다른 Run에 주입된다.
- 현재 통제와 근거: 에이전트 remember는 scope=session.pool, pinned 미지정(api/agent.py:137-147). describe_entity 고정 여부 확인 필요.
- 검증 방법: 에이전트 경로로 pinned 요약에 들어갈 수 있는지 테스트, 출처 표시 여부.
- 수정 방향: 에이전트 작성 기억 출처 표시, 고정은 소유자만.
- 의존: SEC2-PI-01
- 완료 기준: 테스트.
- 불변 조건(정상 사용): 사용자 지시 기억 저장 정상.
- 작업 분할: 1) 검증(2h)

### SEC2-OPS-02 사고 대응 runbook(키·토큰·계정 회전)

- 우선순위 P2, 심각도 2 (가능성 1 × 영향 2), 상태 **확인됨(부재)**, 영역 사고 대응, Phase 3, 권장 task_type `design`
- 자산: A2~A8
- 위협 시나리오: 유출 의심 시 무엇을 어떤 순서로 바꿀지 없다.
- 현재 통제와 근거: logout_all(api/auth.py:160-161), 감사 기록 record().
- 검증 방법: 격리 환경에서 서명키 회전→전 토큰 무효 리허설.
- 수정 방향: runbook 문서와 회전 CLI.
- 의존: SEC2-AUTH-01
- 완료 기준: 리허설 로그.
- 불변 조건(정상 사용): 회전 후 재로그인만으로 복구.
- 작업 분할: 1) runbook+리허설(2h)

### SEC2-AUTHZ-04 토큰 용도(aud) 교차 매트릭스

- 우선순위 P2, 심각도 3 (가능성 1 × 영향 3), 상태 **부분 확인**, 영역 인가·범위, Phase 3, 권장 task_type `verification`
- 자산: A4~A6
- 위협 시나리오: 다운로드 토큰·에이전트 토큰으로 소유자 API를 부르거나 그 반대.
- 현재 통제와 근거: aud 분리(api/download/ttakkari-agent), sub=owner 확인(security/auth.py:64-66). 이전 감사 일부 확인.
- 검증 방법: aud 3종×라우트 군 3종 교차 호출 테스트.
- 수정 방향: 발견 시 최소 수정.
- 의존: SEC2-GATE-00
- 완료 기준: 9칸 매트릭스 테스트.
- 불변 조건(정상 사용): 정상 토큰 흐름 불변.
- 작업 분할: 1) 테스트(1h)

### SEC2-EXPO-02 무인증 상태 엔드포인트 정보 최소화

- 우선순위 P3, 심각도 1 (가능성 1 × 영향 1), 상태 **확인됨(관측)**, 영역 네트워크 노출, Phase 4, 권장 task_type `general`
- 자산: 운영 정보
- 위협 시나리오: /api/version·/api/qwen-status가 리비전·모델 상태를 공개한다.
- 현재 통제와 근거: /api/version 200 확인됨.
- 검증 방법: 응답 필드 목록 검토.
- 수정 방향: 공개 필요한 필드만 남김(배포 감독 의존성 확인).
- 의존: 없음
- 완료 기준: 필드 축소 테스트와 배포 감독 대조군.
- 불변 조건(정상 사용): 배포 준비 상태 확인 흐름 유지.
- 작업 분할: 1) 축소(1h)

### SEC2-GATE-02 독립 종결 감사

- 우선순위 P3, 심각도 2 (가능성 1 × 영향 2), 상태 **해당 없음**, 영역 검증 기반, Phase 4, 권장 task_type `verification`
- 자산: 전체
- 위협 시나리오: 완료 주장이 실행 증거 없이 누적된다.
- 현재 통제와 근거: 이 WBS.
- 검증 방법: 다른 공급자 모델이 P0·P1 각 항목의 RED/GREEN 증거를 재실행.
- 수정 방향: 미달 항목 재개방.
- 의존: Phase 1~3
- 완료 기준: 항목별 재실행 로그와 판정표.
- 불변 조건(정상 사용): 감사는 읽기·격리 실행만.
- 작업 분할: 1) P0 재실행(2h) 2) P1 재실행(2h)

## 6. 네트워크 접근 통제·로그인 제한·세션 보안 트랙 (2026-10-19 추가)

근거와 설계는 [network-login-session-audit.md](network-login-session-audit.md)에 있다. 기존 항목은 복제하지 않고 확장·대체만 했다.
매핑: NAC-01~06은 SEC2-EXPO-01의 실행안, NAC-03은 SEC2-AUTH-03 흡수, LGN-01~04는 SEC2-AUTH-03 대체, SES-02는 SEC2-AUTH-01 확장, SES-04는 SEC2-RD-01 연계, SES-05는 SEC2-WEB-01·02 의존.
공통 불변 조건: 에이전트 Run 실행·승인 흐름, CLI·파일·개발 작업은 바뀌지 않는다. 사용자 명령 실행에 승인·재인증 단계를 추가하지 않는다. 모든 항목의 시작은 사용자 승인 후다.

### 6.1 항목 표

| ID | P | 심각도(L×I) | 영역 | 제목 | 상태 | 의존 | task_type | 사용자 승인 |
|---|---|---|---|---|---|---|---|---|
| SEC2-NAC-01 | P0 | 6 (2×3) | 네트워크 경계 | 공개 터널 노출면 축소·Tailscale 경계 방식 결정(ADR) | 확인됨 | SEC2-EXPO-01 | design | 필요 |
| SEC2-NAC-02 | P1 | 4 (2×2) | 네트워크 경계 | tailnet Grants·Mac 노출 포트·방화벽 점검 | 확인됨(관측), ACL 미확인 | SEC2-NAC-01 | verification | 필요(콘솔·따까리 밖) |
| SEC2-NAC-03 | P1 | 2 (2×1) | 네트워크 경계 | 프록시 이중 리스너와 신뢰 IP 헤더 경로 분리 (AUTH-03 흡수) | 추론(코드 확인됨) | - | general | 불필요 |
| SEC2-NAC-04 | P1 | 6 (2×3) | 네트워크 경계 | Tailscale Serve 병행 도입과 공개 경로 목록 확정, 배포 검증 URL 의존 정리 | 미검증 | NAC-01, NAC-03 | general | 필요 |
| SEC2-NAC-05 | P1 | 6 (2×3) | 네트워크 경계 | SPA same-origin 서빙과 PWA origin 이전(재설치·푸시 재구독) | 미검증 | NAC-04 | general | 필요 |
| SEC2-NAC-06 | P1 | 6 (2×3) | 네트워크 경계 | 공개 터널 허용목록(cloudflared ingress) 전환과 롤백 리허설 | 미검증 | NAC-05 | verification | 필요(시점) |
| SEC2-LGN-01 | P1 | 6 (2×3) | 로그인 제한 | DB 영속 단계 지연 스키마·상태 기계(shadow 모드) | 확인됨(현황) | - | general | 정책값 필요 |
| SEC2-LGN-02 | P1 | 6 (2×3) | 로그인 제한 | login·원격제어 재인증 연결, 신뢰 기기 쿠키, 계정 경보, 더미 검증 | 확인됨(현황) | LGN-01 | general | 신뢰 기기 쿠키 필요 |
| SEC2-LGN-03 | P1 | 2 (1×2) | 로그인 제한 | Mac 로컬 해제 CLI(login-status·login-unlock)와 set-password 연동 | 확인됨(현황) | LGN-01 | general | 불필요 |
| SEC2-LGN-04 | P1 | 4 (2×2) | 로그인 제한 | 레이스·재시작·Run 무영향·부하 통합 시험과 shadow→enforce 전환 | 미검증 | LGN-02, LGN-03 | verification | enforce 전환 승인 |
| SEC2-SES-01 | P1 | 4 (2×2) | 세션 | refresh 재사용 오탐 계측·동시 갱신 잠금·유예창 | 확인됨(DB), 원인 추론 | - | general | 불필요 |
| SEC2-SES-02 | P1 | 2 (1×2) | 세션 | access에 `sid` 클레임, 서버 폐기 확인, 기기별 세션 목록·폐기 (AUTH-01 확장) | 확인됨(코드) | SEC2-AUTH-01 | general | 불필요 |
| SEC2-SES-03 | P2 | 2 (1×2) | 세션 | refresh 계열 절대 수명 상한과 만료·폐기 행 정리 | 확인됨 | SES-02 | general | 상한 값 필요 |
| SEC2-SES-04 | P2 | 2 (1×2) | 세션 | SSE·원격 데스크톱 연결 주기 재검증 | 확인됨(코드) | SES-02, SEC2-RD-01 | general | 불필요 |
| SEC2-SES-05 | P2 | 2 (1×2) | 세션 | refresh 쿠키 SameSite=Strict·`__Secure-` 전환(iOS PWA 실측) | 확인됨(운영 응답) | NAC-05, SEC2-WEB-02 | verification | 불필요 |
| SEC2-SES-06 | P2 | 2 (1×2) | 세션 | 새 기기 로그인·잠금·재사용 알림과 429 감사 | 확인됨(코드) | LGN-02 | general | 불필요 |

### 6.2 항목 상세

#### SEC2-NAC-01 공개 터널 노출면 축소·Tailscale 경계 방식 결정(ADR)

- 우선순위 P0, 심각도 6 (2×3), 상태 **확인됨**, 영역 네트워크 경계, 권장 task_type `design`
- 자산: A1~A4, A14, 호스트 명령 실행 전체
- 위협 시나리오: 공개 도메인이 소유자 API 408개 전체를 노출하고 방어선이 비밀번호 1개뿐이다. 비밀번호가 맞으면 에이전트 실행으로 이어진다.
- 현재 통제와 근거: 우회 접근은 확인되지 않음(공개 GET·헤더 위조 시험, 보고서 3절). 터널 ingress에 경로 필터 없음(`~/.cloudflared/ttakkari.yml`). Tailscale 설치·실행 중, Serve·Funnel 설정 없음.
- 검증 방법: ADR 초안에 선택지 A(Serve)·B·C·D·E·F 비교, A1·A2·A3 origin 방식 비교, 실기기 쿠키 시험 계획.
- 수정 방향: Serve 병행 → same-origin PWA → 공개 경로 허용목록. 경계는 네트워크에서 강제, Tailscale 헤더는 신뢰하지 않는다.
- 완료 기준: ADR 1건(Proposed)과 사용자 결정 기록. 결정에 따른 NAC-02~06 범위 확정.
- 불변 조건: 에이전트 실행·MCP 루프백 경로, 공개 공유 링크, 세대 배포 검증은 유지한다.
- 작업 분할: 1) ADR 초안(2h)

#### SEC2-NAC-02 tailnet Grants·Mac 노출 포트·방화벽 점검

- 우선순위 P1, 심각도 4 (2×2), 상태 **확인됨(관측), ACL 미확인**, task_type `verification`
- 위협 시나리오: Mac 방화벽이 꺼져 있고 `mariadbd *:3306`, AirPlay `*:5000`·`*:7000`, `rapportd`가 LAN에 열려 있다. tailnet의 다른 노드(윈도우 5, ubuntu 서버 1)가 닿을 수 있다(ACL 내용 확인 필요).
- 검증 방법: 사용자가 내보낸 ACL을 읽어 Mac Studio 대상 규칙 목록화. 노드별 필요 접근 확인. 외부 노드에서 3306 연결 거절 확인(변경 후).
- 수정 방향: Grants 최소화(예시는 보고서 4.3), 3306 루프백 바인딩(따까리 밖, 사용자 결정), 앱 방화벽 활성, Mac Studio 키 만료 해제.
- 의존: SEC2-NAC-01. 중복 방지: EXPO-01의 관측을 입력으로만 사용.
- 완료 기준: 변경 전후 `lsof`·ACL 비교표.
- 불변 조건: 기존 노드의 필요한 접근(예: 서버↔Mac)이 깨지지 않는다(사전 확인).
- 작업 분할: 1) ACL 판독·영향표(2h) 2) 적용·검증(1h)

#### SEC2-NAC-03 프록시 이중 리스너와 신뢰 IP 헤더 경로 분리

- 우선순위 P1, 심각도 2 (2×1), 상태 **추론(코드 확인됨)**, task_type `general`
- 위협 시나리오: 루프백 피어가 보내는 `CF-Connecting-IP`를 무조건 신뢰(`security/auth.py:79-88`)해서, Serve 도입 후 tailnet 클라이언트가 IP 키와 감사 IP를 위조할 수 있다.
- 검증 방법: 격리 Caddy로 8788 경로의 헤더 제거 시험, 8787 경로는 유지.
- 수정 방향: `generation_deployment.py:220-226`의 `proxy_config`에 8788 리스너(헤더 제거) 추가. 앱은 8788 경로의 출처를 피어로 판단.
- 완료 기준: 위조 헤더 무시 테스트, Cloudflare 경유 정상 IP 대조군, 배포 감독 검증 통과.
- 불변 조건: 세대 전환·`stream_close_delay`·`/api/ready` 404 규칙 유지. 배포 시스템 변경이므로 격리 시험 후 배포.
- 작업 분할: 1) proxy_config+테스트(2h)

#### SEC2-NAC-04 Tailscale Serve 병행 도입과 공개 경로 목록 확정

- 우선순위 P1, 심각도 6 (2×3), 상태 **미검증**, task_type `general`
- 수정 방향: P0 사전(HTTPS 인증서, ACL 백업) 후 `tailscale serve`를 병행 추가. 공개 경로 목록(`/api/health`, `/api/version`, `/api/public/shares/*`)과 `/api/qwen-status`·`/api/dl`·`/api/view`·단축어 업로드의 소비처를 확정. 부트스트랩 스모크의 하드코딩 공개 URL(`generation_bootstrap.py:143`)을 설정값으로.
- 검증 방법: iPhone 셀룰러+Tailscale에서 health·로그인·SSE·푸시. 공개 터널은 그대로 두고 비교.
- 완료 기준: 실기기 결과표, 롤백(`tailscale serve reset`) 리허설 로그.
- 불변 조건: 병행 기간에 공개 경로 동작 변화 없음.
- 작업 분할: 1) 사전·Serve 병행(2h) 2) 실기기 검증(2h)

#### SEC2-NAC-05 SPA same-origin 서빙과 PWA origin 이전

- 우선순위 P1, 심각도 6 (2×3), 상태 **미검증**, task_type `general`
- 위협·배경: API가 `*.ts.net`으로 가면 Vercel SPA와 cross-site라 iOS에서 refresh 쿠키가 막힐 수 있다.
- 수정 방향: Serve 뒤에서 SPA 정적 파일 제공(같은 origin). 빌드 배포 경로, PWA 재설치, 푸시 재구독, localStorage 초기화 안내. `/share/*`는 Vercel 유지.
- 검증 방법: 실기기에서 앱 재실행 시 재로그인 없이 복구, 푸시 수신, 원격 데스크톱 연결.
- 완료 기준: 7일 병행 사용 기록과 문제 없음 확인.
- 불변 조건: 병행 기간에 기존 Vercel PWA로 복귀 가능.
- 작업 분할: 1) 정적 서빙 경로(2h) 2) 프런트 빌드 설정·재구독(2h) 3) 실기기 시험(2h)

#### SEC2-NAC-06 공개 터널 허용목록 전환과 롤백 리허설

- 우선순위 P1, 심각도 6 (2×3), 상태 **미검증**, task_type `verification`
- 수정 방향: `ttakkari.yml` ingress에 `path` 정규식(보고서 4.4 P4)을 적용하고 터널을 재시작. 격리 터널로 `path` 매칭 동작을 먼저 확인.
- 검증 방법: 공개 도메인에서 `/api/workspaces`·`/api/auth/login` 404, health·version·공유 링크 정상, 세대 배포 검증 통과.
- 완료 기준: 롤백(백업 yml 복원+터널 재시작) 리허설 로그. NAC-05 완료 전에는 시작하지 않는다.
- 불변 조건: 소유자는 tailnet 경로로 계속 사용, 비상 경로는 Mac 로컬·노트북(tailnet).
- 작업 분할: 1) 격리 시험(1h) 2) 적용·검증(1h)

#### SEC2-LGN-01 DB 영속 단계 지연 스키마·상태 기계

- 우선순위 P1, 심각도 6 (2×3), 상태 **확인됨(현황)**, task_type `general`
- 위협 시나리오: 메모리·IP 단위 제한이 배포·재시작마다 초기화되고 분산 추측을 계정 단위로 막지 못한다(`security/auth.py:99-223`).
- 수정 방향: `login_throttle`·`login_devices` 표, `security/login_throttle.py`, 예약 방식 상태 기계, `login_throttle_mode=shadow`. 단계값(60·300·900·3600초)과 예산(5, 계정 20)은 설정.
- 검증 방법: 시간 주입 단위·DB 테스트(보고서 5.5의 1 내지 3, 5, 6, 12).
- 완료 기준: RED→GREEN, 마이그레이션 up/down, shadow에서 실제 기록 수집 시작.
- 불변 조건: shadow에서는 어떤 로그인도 거절하지 않는다.
- 작업 분할: 1) 마이그레이션·모델(1h) 2) 상태 기계+테스트(2h)

#### SEC2-LGN-02 login·원격제어 재인증 연결, 신뢰 기기 쿠키, 계정 경보

- 우선순위 P1, 심각도 6 (2×3), 상태 **확인됨(현황)**, task_type `general`
- 수정 방향: `api/auth.py` login 예약→검증→결과, `ttk_dev` 발급(login·refresh), 신뢰 기기는 계정 키·전역 버킷에서 제외, 더미 검증, `api/remote_desktop.py` reauthenticate 교체. 구 `LoginLimiter`는 CPU 보호로 축소.
- 검증 방법: 보고서 5.5의 4, 6, 7, 8, 11.
- 완료 기준: RED→GREEN, 정상 로그인·자동 로그인·refresh 대조군, 기존 limiter 테스트 갱신.
- 불변 조건: 정상 로그인 경로와 기존 12개 세션 유지(다음 refresh에서 쿠키 발급).
- 작업 분할: 1) login 연결+쿠키(2h) 2) 원격제어·계정 경보(2h)

#### SEC2-LGN-03 Mac 로컬 해제 CLI

- 우선순위 P1, 심각도 2 (1×2), 상태 **확인됨(현황)**, task_type `general`
- 수정 방향: `ttakkari login-status`, `login-unlock [--source IP|--all]`, `set-password`가 잠금·신뢰 기기 초기화(`cli.py:67-80` 패턴). HTTP 라우트 없음.
- 검증 방법: CLI 테스트, 라우트 인벤토리 테스트로 해제 라우트 부재 고정.
- 완료 기준: RED→GREEN.
- 불변 조건: 해제 기능이 일반 API에 노출되지 않는다.
- 작업 분할: 1) CLI+테스트(1h)

#### SEC2-LGN-04 통합 시험과 shadow→enforce 전환

- 우선순위 P1, 심각도 4 (2×2), 상태 **미검증**, task_type `verification`
- 검증 방법: 20 병렬 오답 시 검증 정확히 5회, 재시작·두 세대 지속, 에이전트·SSE·refresh 무영향, 출처 200개 부하, shadow 3 내지 7일 기록 검토.
- 수정 방향: enforce 전환은 사용자 승인 후 설정값 변경. 롤백은 `shadow`로 되돌림.
- 완료 기준: 시험 로그와 shadow 기록 요약, 정상 소유자 오차단 0건.
- 작업 분할: 1) 통합 시험(2h) 2) shadow 검토·전환(1h)

#### SEC2-SES-01 refresh 재사용 오탐 계측·동시 갱신 잠금·유예창

- 우선순위 P1, 심각도 4 (2×2), 상태 **확인됨(DB), 원인 추론**, task_type `general`
- 위협·현상: `auth.refresh_reuse` 13건(10-13부터), 같은 기간 로그인 19건. 유예 없이 계열 전체 폐기(`api/auth.py:133-140`)라 소유자가 반복 강제 로그아웃된다.
- 수정 방향: 1) 재사용 감사에 UA·경과 시간·`replaced_by` 기록(`replaced_by` 현재 미사용). 2) 프런트 Web Locks로 탭 간 refresh 직렬화(`front/src/api/client.ts:111-125`). 3) 서버 20 내지 30초 유예(새 쿠키 재발급, `auth.refresh_grace` 기록).
- 검증 방법: 동시 refresh 2개 테스트(유예 안·밖), 진짜 재사용(유예 밖) 계열 폐기 대조군.
- 완료 기준: 유예 안 재사용은 로그아웃되지 않고, 유예 밖 재사용은 폐기. 배포 후 재사용 이벤트 감소 확인.
- 불변 조건: 탈취 재사용 탐지는 유지.
- 작업 분할: 1) 계측(1h) 2) 프런트 잠금(1h) 3) 서버 유예(2h)

#### SEC2-SES-02 `sid` 클레임·서버 폐기 확인·세션 목록 API

- 우선순위 P1, 심각도 2 (1×2), 상태 **확인됨(코드)**, task_type `general`
- 수정 방향: access에 `sid`=계열 ID와 credential 세대 클레임(AUTH-01의 epoch와 동시 설계), `require_user`가 짧은 TTL(5초) 캐시로 계열 활성 확인. `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{family}`. 로그인 시 같은 기기 쿠키의 이전 계열 폐기. `sid` 없는 토큰은 시스템 토큰(`generation_bootstrap.py:137` 스모크)으로 15분 허용.
- 검증 방법: 폐기 후 5초 안에 access 401, 요청당 DB 조회 증가 측정, 배포 스모크 대조군.
- 완료 기준: RED→GREEN과 기존 refresh 회전 대조군.
- 불변 조건: 평상시 강제 재로그인 없음.
- 작업 분할: 1) 클레임·검사(2h) 2) 세션 API·프런트 목록(2h)

#### SEC2-SES-03 refresh 계열 절대 수명 상한과 정리

- 우선순위 P2, 심각도 2 (1×2), 상태 **확인됨**
- 수정 방향: 계열 시작 시점 기준 절대 상한(권고 180일, 90일 대안, 사용자 결정), 만료·폐기 후 30일 지난 행과 감사 오래된 행 정리를 기존 retention 루프에 추가. 짧은 만료로 바꾸지 않는다.
- 완료 기준: 상한 테스트와 정리 테스트.
- 작업 분할: 1) 상한+정리(2h)

#### SEC2-SES-04 SSE·원격 데스크톱 연결 주기 재검증

- 우선순위 P2, 심각도 2 (1×2), 상태 **확인됨(코드)**
- 수정 방향: SSE(`api/sessions.py:283-324`)와 원격 데스크톱 릴레이(`remote_desktop.py:105`)가 60초마다 `sid`·credential 세대를 확인하고 폐기 시 종료. SSE는 Last-Event-ID로 재연결하므로 정상 흐름에 영향 없음.
- 완료 기준: 로그아웃·비밀번호 변경 후 60초 안에 연결 종료 테스트.
- 작업 분할: 1) SSE(1h) 2) 원격 데스크톱(1h)

#### SEC2-SES-05 refresh 쿠키 SameSite=Strict·`__Secure-` 전환

- 우선순위 P2, 심각도 2 (1×2), 상태 **확인됨(운영 응답: SameSite=none)**
- 수정 방향: PWA와 API가 같은 사이트이거나 same-origin이 되면 Strict로 전환. iOS 홈 화면 PWA 실기기 시험 후 적용. NAC-05 결과에 의존.
- 완료 기준: 실기기에서 재로그인 없이 복구, CSRF 테스트 유지.
- 작업 분할: 1) 실기기 시험·적용(2h)

#### SEC2-SES-06 새 기기 로그인·잠금·재사용 알림과 429 감사

- 우선순위 P2, 심각도 2 (1×2), 상태 **확인됨(코드)**
- 수정 방향: 새 계열 생성, 잠금 level 2 이상, 계정 경보, refresh_reuse를 푸시(중복 억제). 429·잠금 전이를 감사에 남기되 요청마다 쓰지 않는다(감사 테이블 보호).
- 완료 기준: 알림 발송·중복 억제 테스트.
- 작업 분할: 1) 알림·감사(2h)
