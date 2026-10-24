# P1 frontend·runtime·test 현재 후보 차이 대조

2026-10-25 · root Codex 명령 샌드박스 · **정적 대조이며 P1 전체 완료가 아니다.**

소스는 backend `6f9802ab3197edb3f8257d36362d1e1b4e056341`, `src` tree `3b0619a1f50aca6bb799218f3794bee4853cc9f8`다. 기존 4개 Caddy case, pre-counter/slow-header 실험, 111개 회귀와 scanner 51개 검사는 재실행하지 않았다. 제품 코드 변경도 없다.

## 입력과 재사용

기존 `20261024/frontend-tests/summary.json`의 `input_provenance.source_sha256`, `baseline-manifest.json`, `corrected-source-manifest.json`, backend/frontend 테스트 분류를 읽었다. 257행의 `baseline-manifest`와 c8 후보 303파일을 현재 테스트 분모로 사용하지 않는다. 서로 다른 시점·범위다.

실행: `python3 review/platform-v2/20261025/p1_delta_review.py`. [구조화 원문](p1-frontend-runtime-tests-delta.json)에 입력 해시·HEAD·dirty 목록·실제 추가/변경/삭제 경로·분류 행·AST 출력·오류 원장을 보존했다. 스크립트는 수집 전후 HEAD/status와 입력 바이트를 재대조하고, addition/removal/change 음성 대조를 실행한다. 기존 scanner의 `main`/`write_outputs`는 호출하지 않는다.

`corrected-source-manifest.json`의 18개 파일 해시는 모두 현재 파일과 같다. 따라서 기존 분류기를 교체하지 않고 재사용할 수 있다. 바이트가 같은 테스트 분류만 재사용했다. 아래 수치는 실행 테스트 case 수가 아니다.

## Frontend: 두 checkout을 분리

| 대상 | 실제 입력 | 대조 결과 |
|---|---|---|
| 공유 `front` | HEAD `630040b1b7007a219a93c9d3d4491fab714c555d`, 기존 dirty 보존 | 기존 감사의 TS/TSX 266파일 모두 같은 바이트; 추가·삭제·변경 0 |
| 원격 main과 일치하는 `front-library-intent` | HEAD `da9d98ddab71fbbc3fee18da9ebecffdb3619300`, clean; `git ls-remote origin refs/heads/main` 일치 | TS/TSX 502파일: 기존 입력 대비 추가 236, 변경 48, 동일 218, 삭제 0 |

공유 dirty checkout의 변동이 없다는 사실은 원격 main 감사가 최신이라는 뜻이 아니다. 공유 디렉터리의 코드·Git 상태를 수정하거나 pull/reset하지 않았다. 원격 main과의 차이는 V2 변경량이 아니라 **옛 감사 입력과 현재 frontend main의 차이**다.

변경된 `src/App.tsx`를 기존 TypeScript AST 추출기로 읽었다. 경로 선언은 과거 38→현재 64(구체 경로 62, wildcard 2), 추가 26, 삭제 0, 레이아웃 1, 페이지 컴포넌트 이름 55다. 새 범위는 ArtCraft, Reverse, Collect/Knowledge, Creative 계획·기록, Games, GitHub, Harness, NotebookLM, TTS, Research, Remote Desktop, Stocks/TossInvest다. `coverage=complete`는 이 App AST 추출의 해석 상태이며 UI/기능 검수 완료가 아니다.

API 입력 3파일 중 바뀐 것은 `src/api/client.ts` 하나다. `stream.ts`와 `useConnection.ts`는 기존 바이트와 같아 재분석하지 않았다. 변경 client의 267개 정적 callsite를 읽었고, 함수명·HTTP method·URL tuple 기준 추가 87, 제거 2다. 제거 tuple 중 `singleAccountUsage`→`accountUsageOne`은 같은 GET 경로의 함수명 변경이므로 API 제거 2건이라고 부르지 않는다. query/body/응답 의미까지 같은지는 이 tuple 비교만으로 보장하지 않는다.

**해결하지 않은 API 추출 15건**은 원문 `front_changed_api.ast.api.unresolved`에 남겼다. `ga(id, rest)`와 `nlm(id, rest)` helper 호출이다. 실제 선언은 각각 `/api/google-accounts/${encodeURIComponent(id)}${rest}`, `/api/notebooklm/notebooks/${encodeURIComponent(id)}${rest}`이며, 로그인·세션 및 notebook 단건/소스/query 호출에 해당한다. helper body를 직접 확인했지만 이번에는 추출기를 확장하거나 15건을 자동 complete로 승격하지 않았다. 구문 오류는 0이다. 나머지 frontend 파일의 함수·컴포넌트 상태 의미와 새 테스트 실행은 미검증이다.

## Backend 테스트 입력 delta

`tests`, `unit_tests`, `benchmark_tests`의 Python 입력을 같은 기준으로 해시 대조했다. 이전 입력 대비 새 파일 25, 변경 19, 동일 308, 삭제 0이다. 변경 19에는 `tests/conftest.py`, `tests/tts_helpers.py`도 포함되므로 모두 테스트 파일이라고 합산하지 않는다.

현재 `test_*.py` 333파일 중 **286파일은 동일 입력 해시로 분류 재사용**, **47파일만 현재 AST 분류**했다. 47은 추가·변경 테스트 파일 및 과거 스캐너가 제외했던 감사 테스트 5파일이다. 이 5파일도 현재 분모에는 포함해 누락을 숨기지 않았다. AST 오류 0, 정적 테스트 함수 정의 합계 3,886이다. pytest 수집 case 수, 통과 수, parameter/subtest 수는 미측정이다. 도우미·conftest 전체 경로도 별도 원장에 있다.

신규 25파일의 범위는 Step2 소유권/drain, remote Qwen/TTS, Creative/Game queue, Background consumers, residual 감독, migration merge/복수 head 거절, report 경쟁, FND1 transport 거절이다. 상세 파일명과 재사용 근거는 JSON의 `back_delta`/`backend_tests`를 참조한다. 전체 suite 실행이나 DB-backed 통합 검증을 대체하지 않는다.

## Runtime 소유권·자원 매핑

다음 5파일의 현재 해시는 기존 `p1-inventory-after-classification.json.source_manifest`와 모두 같다. 코드 정의 위치는 JSON `runtime_source_bindings`에 기록했다. 아래는 정적 배선이며 현재 운영 소유권 관측이 아니다.

| 경계 | local API / 비세대 rollback | remote API | executor / 공유 자원 |
|---|---|---|---|
| `main.lifespan` | 비세대 경로는 Run·Qwen·STT·TTS·background·주기 작업을 시작하고 claim-stop→drain→stop | 세대 모드에서는 `generation_service.lifespan`에 위임; bootstrap hold는 수동적 yield | DB·파일 경로 공유; bootstrap은 독립 전환 성공 증거가 아님 |
| `generation_service.start_api_services` | preview/STT/TTS/background와 Qwen owner 시작 | 해당 소비자 시작 안 함, Qwen `owner=False` | `executor.start_background_services`가 preview/STT/TTS/background 및 `maintenance_jobs` 시작 |
| Run/주기 작업 | local active에서 Run claim, maintenance 수행 | `coordinate_runs`의 `manager.remote`이면 즉시 반환, maintenance 미기동 | executor의 RunManager/동일 maintenance 목록. DB command 및 기존 소유권 계약 재사용 |
| NotebookLM·Reverse·Context index | background pump가 domain별 queued record를 가져감 | `deferred()`로 접수·조회, 같은 영속 큐 사용 | `BackgroundConsumers`: pause/resume과 stop 구분, 새 claim 중지 후 inflight 자연 완료; executor stop은 무상한 대기 |
| Creative assets·Game builds | `queued_work` 추적 및 rollback 소비 경로 | 접수만 기록 | `queued_work`는 큐가 아니라 claim 스위치/진행 작업 추적. domain의 production JSON/game_builds가 영속 큐 |
| Qwen·TTS·GPU·파일 | local API가 소유한 프로세스만 정리 | Qwen 자식 소유 안 함, TTS 실행 안 함 | executor Qwen owner; 외부 URL은 재사용. GPU `ResourceLeaseManager` 및 기존 도메인 예산·공유 파일 유지 |
| ArtCraft·Google login | API 잔여 실행 | API 잔여 실행 유지 | 새 시작 drain503, 기존 작업 30s/20s 경계; 예외 재시도·deadline 유지·rollback cancel join. 영구 cleanup 실패시 ready=false 유지 |

executor는 immutable `release_revision` 없이 기동을 거절한다. ownership 상실은 완료나 자식 종료로 해석하지 않고 진행 작업 drain을 유지한다. timeout 후 자연 완료를 계속 기다리는 경로가 있으므로 유한 시간 강제 퇴역을 보장한다고 쓰지 않는다. local 종료는 1,800초 공통 예산 이후 취소를 기록하는 다른 계약이다.

## 남은 범위와 연결

P1은 review를 유지한다. 이 대조는 현재 source 분모와 실행 소유권 배선을 정합화했다. backend loader/source-byte binding, 새 frontend 기능의 실제 backend 응답·권한·로딩/빈/오류 상태, 15개 helper 추출, scripts/배포 shell·설정의 의미 전수 대조와 실제 런타임 자원 소유권은 미완료다.

P2의 현행 계약은 [실행부 분리 통합 계약](../../../docs/design/platform-v2/current-execution-contract.md)으로 연결한다. FND1 자동 전송 장벽은 미구현이며 기존 명시 거절을 유지한다. DB9건·library/V2 실제 DB 통합·왕복/복원·전체 gate·독립 residual·운영 모델/MCP/재개·장기 Run·P4/P6를 완료로 올리지 않는다. main push/배포 예약 없음.
