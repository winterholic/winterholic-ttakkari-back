# ADR 0046: Phase 2 실험용 첫 게임 어댑터 — Web / Phaser 3

- **상태**: Phase 2 검증 대상으로 잠정 채택 (2026-10-19). 보안 빌드 경계와 모바일 피드백 실측을 통과해야 유지한다.
- **결정 범위**: 첫 번째 구현·E2E 검증 후보. 영구 엔진 표준이나 모든 장르 지원을 선언하지 않는다.
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md)

## 맥락

게임 화면 중심의 모바일 관제탑에서 사용자가 직접 실행·플레이하고 시각 피드백을 보내려면 브라우저 게임이 첫 시도로 적합한지 검증할 필요가 있다. 로컬 `node --version`은 `v26.9.0`, `npm --version`은 `11.19.1`을 출력했고 Playwright Chromium/WebKit 캐시도 발견했다. Phaser/Vite 프로젝트 의존성 설치·빌드와 게임 실행은 아직 검증하지 않았다.

## 결정

첫 실험은 **Web / Phaser 3 (TypeScript + Vite 후보)**로 진행한다. 실제 Phaser 패키지 버전, 라이선스, 빌드 결과와 배포 가능한 런타임 헤더는 구현 시작 시 확인한다. 사용자에게 지원 범위·제약·대체 방법을 표시하고, 이 선택을 다른 엔진과 장르에 일반화하지 않는다.

선정 근거는 엔진 실행을 브라우저 기반으로 실험할 수 있고 현재 Node/npm이 설치되어 있다는 점이다. Phaser가 다른 후보보다 AI 생성 오류가 적다거나, 설치 없이 즉시 빌드된다거나, 사용자의 폰에서 완벽한 반응성을 제공한다는 비교 실험은 없다.

## 보안·운영 조건

- 게임 빌드 스크립트와 `npm install` lifecycle은 호스트 프로세스에서 그대로 실행하지 않는다. CSP·File Broker 경로 검증은 OS 프로세스 격리가 아니다. 비밀 없는 무권한 격리 실행 경계와 음성 탈출 검증을 먼저 통과해야 한다.
- 게임 산출물은 기존 Artifact, Git workspace/Run 기록을 재사용한다. 사용자 빌드별 immutable snapshot이 실제로 보존되는지 Phase 2에서 확인하고, 불가할 때만 최소 메타데이터 확장을 제안한다. 별도 버전관리나 작업 큐는 두지 않는다.
- 기존 Artifact inline 응답은 `sandbox; default-src 'none'` CSP와 bearer 인증을 사용하므로 실행 가능한 게임 서빙으로 재사용하지 않는다. 실행 콘텐츠는 PWA와 별도 사이트 origin에서 제공하고 Ttakkari 자격증명·세션 쿠키를 전달하지 않는다. sandbox/CSP/CORS와 `postMessage` 검증을 통합 보안 테스트로 확인한다.
- 빌드 ID, source commit, Artifact, 테스트 결과를 묶어 불변 스냅샷으로 보존한다. 롤백은 기존 Run checkpoint ref를 직접 덮어쓰지 않고 선택한 정상 소스로 새 복구 변경/빌드를 생성하며, 기존 빌드도 보존한다.
- Phaser는 2D 범위로 한정한다. 3D·엔진 전용 기능 또는 모바일에서 부족한 입력·렌더링은 지원 불가/제약과 Godot 등 대체 검토 경로로 사용자에게 알린다.

## 검증 노트 (2026-10-19)

- ADR 0044는 이미 [`0044-tossinvest-stocks.md`](0044-tossinvest-stocks.md)에서 토스증권 어댑터에 사용되고 ADR 0045는 NotebookLM 통합에 사용된다. 중복을 제거해 게임 문서를 0046으로 이동했다. Alembic `0044_reverse_reconstruction` / `0045_tossinvest_runs`는 별도 migration namespace이며 ADR과는 번호 체계가 다르다.
- 실행 명령: `node --version` → `v26.9.0`; `npm --version` → `11.19.1`.
- 실행 파일 검색: `command -v godot godot4 Unity UnrealEditor ffmpeg`는 해당 이름을 찾지 못했고 `/Applications` 최상위 이름 검색 결과도 없었다. 이는 해당 검색 범위의 결과다. `~/Library/Caches/ms-playwright`에는 Chromium/WebKit 및 Playwright ffmpeg 디렉터리가 있지만 Playwright CLI는 PATH에 없다.
- `env_probe.py`는 이 Codex 실행 환경에서 Metal 검사 차단 결과를 냈다. 이는 호스트 GPU 상태를 증명하지 않는다.
- 기존 코드 확인: worker 분류는 `src/ttakkari/runs/worker_routing.py:21,71-80`, Run dispatch는 `src/ttakkari/runs/manager.py:248-360`, worker 생성 MCP는 `src/ttakkari/orchestrator/mcp_server.py:558-600`이다. 게임별 요청에서 worker를 시작해 피드백·게임 소스·빌드까지 연결하는 기능은 현재 구현됐다고 볼 수 없다.
- 기존 `/api/artifacts/{id}/content`는 `src/ttakkari/api/artifacts.py:65-71,312-330`에 정의되고, 다운로드용 토큰 경로도 별도다. 일반 Artifact path/CSP를 게임 실행 경계로 재사용하지 않는다.
- File Broker `src/ttakkari/broker/policy.py:102-164`는 파일 경로 검증기다. `npm install`/빌드 프로세스의 호스트 접근을 격리하지 않는다.
- 구현 순서는 Alembic CLI `uv run ttakkari migrate` → router 등록 `src/ttakkari/main.py` → 프론트 `BrowserRouter` route(`front/src/App.tsx`) → worker tool `@orchestrator_tool`/`server.tool()` 등록(`mcp_server.py`) 경계를 따른다.
- 지연시간 `0ms`, 100% 자동화, AI 환각·수정 오류의 후보 대비 우위는 측정 자료가 없어 근거 있는 주장으로 취급하지 않는다.
