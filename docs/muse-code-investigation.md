# Muse Code 통합 조사 및 진행 기록 — 2026-10-17

상태: **백엔드·프론트 연동 코드 후보 작성. 실제 구독 인증·코딩·운영 검증 미완료. 운영 미배포.**

## 요청과 완료 기준

사용자는 Muse Code를 Claude Code·Codex와 동등한 독립 엔진으로 통합하고 실제 코딩·테스트·세션 재개·취소·운영·회귀 및 동등 조건 벤치마크의 실행 증거를 요구했다. 설계만으로 완료할 수 없다. 결제, 종량제 API 전환, 운영 데이터 삭제, 기존 설정의 파괴적 변경은 하지 않는다.

## 이번 실행에서 확인한 상태

- 백엔드 조사 시작 커밋: `41e007dd815ec1427d4430ec2ae13e3035ef7485`. `git pull --ff-only` 결과 Already up to date.
- 운영 `/api/version`: `c1d706274cb4d3fc2d25f138280184ba1653dfb0`, PID 89425. `/api/health`: `{"ok":true}`. 운영 버전과 개발 체크아웃은 다르다.
- 원 프론트 체크아웃에는 다른 작업의 미커밋 변경이 있어 수정하지 않았다. `back-muse-code`, `front-muse-code` Worktree / `feature/muse-code` 브랜치를 생성했다. 초기에는 프론트 코드가 없었고, 2차 구현에서 엔진 선택·타입·배지·사용량·검증 대기 상태를 확장했다.
- 조사 시작 당시 `command -v muse`는 exit 1. `~/.local/bin/muse`, `/opt/homebrew/bin/muse`, `~/.config/muse`도 없었다. 다른 머신이나 사용자의 구독 유무에 대한 증거가 아니다.
- 공식 설치 스크립트를 읽은 후 `MUSE_INSTALL_DIR=~/development/tooling/muse/bin MUSE_NO_MODIFY_PATH=1 MUSE_LOGIN=0`으로 설치했다. PATH·셸 설정을 수정하지 않았다. `muse --version`: **Muse Code 1.4.10 (1.4.10-R6427.1)**.
- 초기 미인증 진단 프로필은 `~/development/tooling/muse/runtime/{config,data,cache}`에 뒀다. 제품 기본값은 아래 운영 절차의 보호된 계정 영역이다. `XDG_CONFIG_HOME` 아래 `muse/settings.json`이 사용자 설정이다. `HOME`은 바꾸지 않았고 인증 파일·키체인을 직접 읽거나 복사하지 않았다.
- 중첩 Codex worker sandbox에서 `muse exec --json --model muse-wonder-full-1.0 --contributor-mode off --max-model-steps 1 …`는 **exit 1, missing meta credentials**. 모델 응답·구독 권한 거절에 도달한 결과가 아니다.
- 동일 환경의 `muse serve`는 MSP initialize 성공, `sessionMcp` grant, durable session 지원을 반환했다. `sandboxAvailability`는 **unavailable**이었다. 이유 필드는 없었다. sandbox를 끄거나 다른 실행 경로로 우회해 재시도하지 않았다. 운영 백엔드에서의 Muse sandbox 상태는 미검증이다.
- `account/read`는 experimentalApi가 필요했다. 해당 capability를 협상한 읽기 결과는 `{"state":"loggedOut","credentialRequired":true}`였다.
- `model/list`: `source=bundledCatalog`, `models=[]`. `usage/read`: `{}`. 사용량 0%, 무제한, 모델 접근 거절의 의미가 아니다.
- 공식 MSP `account/loginStart(type=deviceCode)`로 기기 인증을 시작했다. 브라우저를 직접 실행하지 않았다. 인증 링크·코드는 현재 대화와 알림에만 전달했다. 해당 요청은 이후 `expired`로 종료됐고 최종 account/read는 loggedOut이었다. 전달했던 코드는 만료되어 재사용할 수 없다. 결제 흐름은 시작하지 않았다.
- `exec --provider echo`의 실행은 **프로토콜 관찰 전용**이었다. 실제 모델 또는 코딩 성공으로 계산하지 않는다. foreign skills 61개와 catalog 압축 경고가 관찰됐으나 전체 스킬 호환성을 검증한 결과가 아니다.

## 공식 규격 조사

공식 사용자 문서:

- [개요·설치](https://dev.meta.ai/docs/muse-code)
- [설정](https://dev.meta.ai/docs/muse-code/configuration)
- [인증](https://dev.meta.ai/docs/muse-code/auth)
- [구독](https://dev.meta.ai/docs/muse-code/subscriptions)
- [권한·sandbox](https://dev.meta.ai/docs/muse-code/permissions)
- [모델](https://dev.meta.ai/docs/models)
- [Wonder](https://dev.meta.ai/models/muse-wonder)
- [공식 SDK 및 MSP](https://github.com/meta-models/muse-code-sdk)
- [Hooks](https://meta-models.github.io/muse-code-sdk/next/guides/extend/hooks/)

설정 경로는 `~/.config/muse/settings.json`, 프로젝트 지침은 `AGENTS.md` 우선이고 `CLAUDE.md` 등도 정해진 순서로 읽는다. 프로젝트 hook은 `.muse/hooks.json`이다. `.claude`나 `.codex`를 `.muse`로 복사하지 않는다. 공통 `.agents/skills`와 foreign roots의 자동 발견을 이용하되 실제 필요한 skill만 검사한다. 원격 전역 설정의 일괄 이식은 아직 실행하지 않았다.

`muse exec --json`의 원시 record와 MSP notification은 다른 형식이다. `exec --session-id <uuid>`가 headless 재개이고 `muse resume`은 interactive이다. 실행 종료 0은 테스트 성공을 보증하지 않는다. `muse serve`는 공식 MSP 호스트이고 SDK가 세션·명령·프로세스 수명 관리를 지원한다. MCP·subagents·skills·hooks는 공식 문서에 있지만 이 사용자 프로필에서 실제 도구 호출까지 검증한 것은 아니다.

## 모델 및 구독 판정

| 모델 | 공식 역할·지원 | 이 계정의 실제 접근 |
|---|---|---|
| muse-wonder-full-1.0 | Muse Code Power/Maximum 구독 전용. API 없음 | 미검증: 인증 전 중단 |
| muse-wonder-1.0 | 코딩·추론. Muse Code 전 플랜 및 API | 미검증 |
| muse-spark-1.3 / 1.2 | 코딩·추론. Muse Code 및 API | 미검증 |
| Spark 1.3/1.2 contributor | 학습 활용에 동의하는 별도 모드 | 사용하지 않음 |
| muse-spark-1.1 | API 지원, Muse Code 미지원 | 코딩 엔진 후보에서 제외 |
| sam-3.1 | 이미지·동영상 segmentation | 코딩 엔진에서 제외 |
| muse-voice-transcribe-1.0 | 음성 전사 | 코딩 엔진에서 제외 |
| muse-image-1.0 | 이미지 생성·편집 | 코딩 엔진에서 제외 |

사용자가 제시한 목록은 사용자 관측으로 보존하며, 구독 권한 검증 결과로 승격하지 않는다. Wonder Full의 올바른 ID는 `muse-wonder-full-1.0`이다. `muse-wonder-full` 별칭 지원은 확인하지 않았다.

Muse Code 구독의 기본 Contributor 모드는 향후 학습에 콘텐츠 활용을 허용한다. 이번 진단은 매번 `--contributor-mode off`로 실행했다. 추가 API key는 구독과 별개로 종량제일 수 있으므로 사용하지 않는다. 구독 quota·API token cost·실행 토큰은 다른 단위다.

## 기존 아키텍처에서 확인한 연결 지점

확인한 코드 범위는 agents/base.py, claude.py, codex.py, antigravity.py 일부, runs/manager.py, profiles.py, worker_routing.py, context_usage.py, config.py, models.py, schemas.py, api/accounts.py, 관련 ADR 0002·0029, 프론트의 엔진·계정 선언이다. 전체 프로젝트 감사가 끝난 것은 아니다.

- `AgentAdapter.build_command/parse_line`이 공통 계약이며 RunManager가 timeout, process, stderr, events, artifact, checkpoint, 결과·재시작 처리를 소유한다.
- 조사 시작 시 엔진 enum·API Literal·MCP spawn_worker·프로필 라우팅·프론트 타입과 표시는 Claude/Codex/Antigravity를 열거했다. 2차 구현에서는 Muse를 추가했다. 공급자를 등록하는 것만으로 사용량과 재개가 보장되지 않는다.
- 세션 키에 계정·모델을 반영하고 재개 ID와 workspace가 달라지면 실패해야 한다.
- 계정 quota 미관측은 unknown으로 남기고 기존 자동 라우팅에 무조건 여유 계정으로 넣지 않는다.
- 훅 환경은 Muse가 정리하므로 기존 Claude hook을 그대로 복사하면 TTAKKARI_GUARD_* 변수가 전달되지 않을 수 있다. managed hook의 명시적 환경 정책과 payload mapping을 검증해야 한다.
- Muse hooks는 모델 도구 sandbox 밖에서 실행된다. 프로젝트 trust, 사용자 hooks, credential 취급을 구분해야 한다.

전역 설정의 비밀값을 출력하지 않는 구조 조사에서는 `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, 양쪽 skills 경로, Claude hook 이벤트 13종, Codex의 MCP 서버 이름 obsidian을 확인했다. Codex 전역은 danger-full-access/never였지만 따까리 Codex 어댑터는 실행별 workspace-write/never로 덮어쓰는 구조다. Muse에 전역 권한을 복사하지 않는다. 전역 설정 파일과 인증 저장소는 수정하지 않았다.

## 통합 설계 초안 — 미구현 부분 포함

목표 경로는 `Ttakkari RunManager → Muse adapter → pinned official SDK → muse serve → native Muse tools/model`이다. Meta model API로 자체 agent loop를 만들지 않는다. 초기 기술 선택은 Node >=20의 `@muse-code/sdk@1.4.10`이며 runtime dependency가 없다는 장점을 이용한다. Python SDK는 현재 pydantic 2.13.5를 exact pin하고 기존 backend는 2.14.0이어서 주 Python 환경을 수정하지 않았다. 설치 CLI와 SDK의 schema fingerprint는 같다.

1. SDK bridge가 initialize, account lane, sandbox, sessionMcp, durable sessions를 확인한다. API key lane과 미관측 sandbox는 거절한다. accountLogin만으로 유효한 구독 플랜·모델 권한이 검증된 것은 아니다.
2. 세션 생성·재개, MCP construction config, 추가 workspace roots, 명령 UUIDv7 및 서버 ACK를 공식 SDK로 처리한다. ACK는 실행 완료가 아니다.
3. item/started·completed를 도구 호출·결과로 정규화하고 messagePhase와 turn terminal을 구분한다. contextUsage와 tokenUsage를 분리한다. SDK 이벤트를 bridge stdout으로 전달하고 알려진 이벤트를 공통 이벤트로 정규화한다. 미정의 이벤트의 공통 저장은 후속 검토 사항이며 transport EOF를 성공으로 처리하지 않는다.
4. native sandbox를 유지한다. Muse 전용 PreToolUse guard의 도구명·인자 mapping, 보호 경로, Hook failure 시 동작을 실제 검증한 뒤 작업 실행을 연다. hook 설치만으로 차단 효과를 주장하지 않는다.
5. 전체 오케스트레이션은 따까리가 맡는다. Muse 내부 delegation은 명시된 작은 범위·횟수 안에서만 허용하고 outer spawn_worker 재귀를 막는다. 구체 제한 필드·실제 적용은 추가 확인이 필요하다.
6. 기존 개인/회사 범위·동시성·취소·배포 generation 규칙을 유지한다. 최초에는 명시 엔진 선택으로 검증하고 실제 성능 근거 없이 auto 정책의 선호 순서를 바꾸지 않는다.
7. 계정·엔진·모델 선택 UI와 상태·로그·취소·결과 UI를 기존 공통 컴포넌트에 확장한다. 인증·구독 접근·usage 미관측을 서로 다른 상태로 표시한다.

## 1차 커밋에서 작성한 코드와 검증 범위

- `.muse-runtime/package*.json`: 공식 TS SDK 1.4.10 고정 설치.
- `.muse-runtime/probe.mjs`: SDK를 통해 실제 CLI initialize/account/catalog/usage를 조회. 모델 호출을 수행하지 않으며 준비 미충족 시 exit 2.
- `.muse-runtime/contract.mjs`와 테스트: API credential 거절, sandbox 미관측 거절, sessionMcp grant, 재개 identity/workspace/model, MCP stdio 설정·추가 roots의 계약 검사. **제품 런타임에 연결되지는 않았다.**
- `src/ttakkari/agents/muse_protocol.py`: 인증 조사에 쓴 독립 transport spike. 정식 제품 어댑터가 아니며 2차 실행기는 공식 SDK를 사용한다. `unit_tests/test_muse_protocol.py`는 요청/알림 분리, 동시 요청 ID, UUIDv7, 오류, EOF, malformed frame, timeout의 deterministic 검사다.

1차 커밋의 단위·계약 테스트는 실제 개발·운영 검증을 대체하지 않는다. 2차 구현에서는 Worktree의 공통 RunManager에 Muse를 연결했으나, 원 main 및 운영에는 적용하지 않았다.

## 운영 및 벤치마크 미검증 목록

| 요구 시나리오 | 현재 결과 |
|---|---|
| 설치 및 공식 버전 확인 | 확인 |
| 구독 인증 성공·Wonder Full 실제 응답 | 미검증: 프로필 loggedOut |
| 다중 파일 수정·테스트 실행·결과 회수 | 미실행 |
| 긴 작업 추적·작업 중 취소 | 미실행 |
| 실제 대화 재개·무작위 표식 회수 | 미실행 |
| 모델 프로세스 비정상 종료·복구 | 미실행; transport EOF 단위 검사만 있음 |
| 인증 만료·사용량 제한 | 미실행; 현재 로그인 누락은 만료 재현이 아님 |
| 동시 작업·기존 엔진 병렬 | 미실행 |
| 서비스 재시작 이후 동작 | 미실행 |
| 기존 Claude/Codex 회귀 | 격리 DB 및 가짜 기존 엔진을 이용한 회귀 검사 통과. 실제 운영 모델 smoke test는 미실행 |
| 운영 runtime의 Muse sandbox 및 MCP 도구 실행 | 미실행; 중첩 worker 결과와 구분 |
| Claude/Codex/Muse 비교 benchmark | 미실행: Muse 실제 모델 접근 미확인 |

벤치마크 설계: 동일 시작 커밋의 별도 Worktree, 동일 요구·숨겨진 검증·wall-time·tool budget·network·데이터 모드·개입 정책을 고정한다. 버그 수정, 다중 파일 기능, 아키텍처 리팩토링, 장시간 작업을 포함한다. 각 실행의 테스트·diff·실패·재시도·human intervention·실측 quota 변화·시간을 원시 행으로 저장한다. 성공 과제당 비용은 구독 배분 가정과 실제 관측 quota를 명시하며 API 정가로 환산하지 않는다. 동일 모델을 다른 하네스에서 시험하지 않았다면 모델 효과와 하네스 효과를 인과적으로 분리했다고 주장하지 않는다. 실제 과제 선택·공정성 검증·실행은 아직 하지 않았다.

## 이어서 할 일과 안전 경계

기기 인증이 완료되면 먼저 `account/read`에서 accountLogin lane을 확인하고 live catalog·구독 tier를 조회한다. Wonder Full 실제 요청, 별도 coding fixture, MCP 도구·세션 재개를 서로 다른 증거로 남긴다. 중첩 sandbox의 unavailable은 그대로 미검증으로 유지하고, 정식 운영 runtime의 검증은 실제 어댑터 경로에서 별도로 해야 한다. sandbox 해제, 인증 파일 복사, 과금 API 전환, 결제는 하지 않는다.

작업은 엔진·관리 UI 코드 후보와 회귀 검증까지 진행됐지만 실제 인증·코딩·배포·운영 검증·벤치마크는 완료되지 않았다. 인증만 끝나면 모두 된다는 뜻도 아니다. 운영 안전 검사와 전체 요청의 완료 기준을 각각 통과해야 한다.


## 2차 구현 후보와 실행 증거

- `agents/muse.py`를 공통 ADAPTERS에 등록했다. 엔진 enum, 세션/API/MCP Literal, 계정·모델 정책, XDG 프로필, 로그인 명령, 사용량 출처, 프론트 표시를 확장했다. 기존 auto 라우팅의 선호 순서는 바꾸지 않았다.
- `.muse-runtime/bridge.mjs`는 공식 SDK로 `muse serve`를 띄운다. API credential lane과 sandbox 미관측을 거절하고 Contributor를 끈다. MCP construction config, 다중 workspace roots, resume identity/workspace/model 확인, turn terminal·오류·quota·토큰·context 이벤트와 취소·EOF 종료 코드를 처리한다. 실제 모델 성공은 아직 없다.
- `security/muse_hook.py`는 별도 private policy 파일로 guard를 받는다. Muse가 지우는 환경변수에 의존하지 않는다. 정책 파일·MCP 설정은 0600으로 두고 정상·실패 종료 때 정리한다. 기존 managed hook이 있으면 덮어쓰지 않고 실행을 거절한다. 미확인 native tool과 중첩 위임은 차단하며, native subagent 활용은 이 단계에서 미완료다.
- `TTAKKARI_MUSE_ENABLED=false`가 기본이다. 활성화와 설치 유무를 구분하고 사용량 UI는 `execution_enabled=false`를 검증 대기로 표시한다. 이 플래그는 운영 검증을 통과했다는 인증서가 아니다.
- 실제 설치 CLI를 **MuseAdapter → 공식 SDK bridge**로 실행한 실패 경로 검사 결과는 exit 1, `muse/error`, `normalized_is_error=true`였다. 이유는 구독 로그인 누락이었다. 이를 실제 모델 응답·코딩 성공으로 계산하지 않는다.
- 모델 목록에 없다는 이유만으로 Wonder Full을 사용할 수 없다고 단정하지 않는다. 공식 모델 allowlist·명시적 선택 표면을 사용하며, 권한은 실제 호출로 별도 확인한다. SAM·전사·이미지 모델 등은 세션 생성 또는 실행 큐 이전에 거절한다.
- 새 테스트: Python 단위·계약 32건, Node 계약 5건, Muse API 3건. API 검사는 인증·모델을 사용하지 않는 격리 DB 검사이며 비활성 엔진이 실패로 끝나는 실패 경로 검사를 포함한다.
- 기존 회귀 185건 통과 후 사용량 수집·Muse API를 포함한 최종 회귀는 **201 passed in 49.04s**였다. 원문은 outbox/backend-regression.txt다. 테스트 DB는 ttakkari_muse_test_20261017이며 운영 DB와 분리했다. 기존 엔진 실행은 테스트용 엔진을 사용하므로 운영 모델 smoke test를 대체하지 않는다.
- 프론트 관련 4개 파일 테스트는 Muse 검증 대기를 추가한 뒤 32건 통과했다. TypeScript·Vite build 및 lint 통과. 실제 브라우저 앱 조작과 운영 UI는 미검증이다.
- 원 backend main에는 다른 작업의 미커밋 변경(Qwen adapter 및 공통 파일)이 확인됐다. front main에도 다른 변경이 있다. 이 변경들을 임의로 커밋하거나 덮어쓰지 않았으며, 독립 브랜치의 코드와 운영 main 반영을 구분한다. 배포 예약은 하지 않았다.

프로덕션 기본 Muse 프로필은 기존 계정 보호 영역인 `~/.ttakkari/accounts/muse-default`로 지정했다. 앞서 사용한 `~/development/tooling/muse/runtime`는 로그인하지 않은 진단 프로필이다. 해당 device-code 흐름은 expired로 종료됐고 account/read는 loggedOut이었다. 인증 정보를 작업 저장소나 outbox로 복사하지 않는다.

현재 호스트 선택은 기존 따까리와 동일한 Mac Studio native CLI/SDK 실행이다. 별도 컨테이너를 도입하지 않는다. Linux의 bubblewrap과 컨테이너 인증·파일 접근은 실제 검증하지 않았으므로 지원 완료로 표시하지 않는다.

### 운영 활성화 전 필수 절차

1. 고정 SDK를 운영 의존성 경로에 `npm ci --prefix .muse-runtime --ignore-scripts`로 설치한다. 배포 코드의 bridge는 설정된 runtime root에서 SDK를 읽는다. 요청 시 패키지를 다운로드하지 않는다.
2. 계정 소유자가 공식 device-code 또는 안내된 CLI로 구독 로그인한다. auth.json·키체인을 직접 복사하지 않는다.
3. 운영 경로의 sandbox available과 필수 managed guard 설치·실제 차단을 확인하고, API lane은 계속 거절한다. 외부 유료 API로 전환하지 않는다.
4. 실제 Full 응답, 다중 파일 수정·테스트·MCP·재개·취소·동시 실행·서비스 재시작을 개별 실행한다. 현재 중첩 샌드박스의 unavailable을 우회하지 않는다.
5. 동시 작업 변경과 통합해 clean source를 만든 뒤 generation 배포를 예약한다. 공유 source의 미커밋 상태는 active Run 유무와 다른 배포 조건이다. 예약 수락과 배포 성공을 구분한다.
6. 동일 조건 실제 프로젝트 벤치마크를 실행하고 원시 결과와 구독 사용량 관측을 제출한다.


## 계정별 사용량 요청 후속 — 2026-10-17

사용자가 구독 로그인을 마친 뒤 사용량 화면 추가와 계정별 구분을 요청했다. 공식 SDK의 읽기 전용 account/read·usage/read는 모델 실행·sandbox 준비와 분리해 호출한다. `--usage-only` 조회 모드는 모델·세션·도구를 실행하지 않는다. 실제 조회 결과 login=ok, windows=[], updated_at=null이었다. 구독 사용량이 제공되지 않은 것이며 0%나 무제한이 아니다. 실행 활성화는 계속 기본 false다.

계정 사용량 API는 공식 조회의 로그인 상태를 전달하고, 프론트는 개인/회사 모드에서 계정을 선택해 한 계정의 상세만 표시한다. 엔진 배지·계정 전체 이름·해당 모드 순위를 구분한다. Muse의 로그인·사용량 미제공·실행 검증 전은 별개 표시다. 이 변경은 Wonder Full 모델 응답·MCP·코딩·운영 sandbox 검증을 대체하지 않는다.

Qwen local 변경 f98075a와 충돌을 통합했고 공통 RunManager의 로컬 슬롯 해제 및 Muse 임시 파일 정리를 모두 유지했다. 통합 후 선택 회귀 218건, Muse 단위 34건, SDK 계약 5건을 실행했다. 세대 배포 관련 검사는 별도 원문을 outbox에 저장했다. 실제 모델·비교 벤치마크는 여전히 미검증이다.
