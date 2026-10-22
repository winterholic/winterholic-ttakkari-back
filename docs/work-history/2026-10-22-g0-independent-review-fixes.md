# G0 CPU 독립검수 3건 수정

- 일자: 2026-10-22 (Asia/Seoul)
- 대상: `codex/g0-cpu-control`, 시작 HEAD `15fa9409f0c8141320ec46e2e3e451570f594db8`
- 기준: `ttakkari-studio-lab/design/ai-director-architecture-adr-draft.md` §6 및 `design/execution-order.md` 1번
- 범위: 독립검수의 동시 attempt 실행, 상태 역행, 성공 출력 CAS 무결성 세 건만 수정. GPU host lease·G1·Lab·API 계약 변경은 없음.

## 재현과 원인

독립 반례 스크립트 실행:

```text
PYTHONPATH=/Users/winterholic/development/projects/winterholic-ttakkari/back-g0-cpu/src \
  /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python \
  /Users/winterholic/.ttakkari/data/outbox/3e1ca4cf-791d-49c6-8ffb-e6078395845b/g0_cpu_counterexamples.py
duplicate renderer invocations: 2
final states: ['review_pending', 'review_pending']
state after running(seq=1) then queued(seq=2): queued
state after succeeded event with missing output file: review_pending
```

## 수정

- attempt UUID별 `flock`을 renderer와 회수 구간 전체에 유지한다. attempt receipt에 claim ID·process ID·시각을 기록한다. 살아있는 소유자가 있으면 같은 attempt 호출은 직렬화되고, 소유 프로세스 종료 시 kernel lock이 풀려 재시작의 `resume_pending`이 남은 `running/collecting`을 다시 claim할 수 있다. 단순 `running` 영구 거부를 하지 않는다.
- 이벤트 sequence가 커도 `queued/running/collecting/succeeded`의 전진 순서를 거스르는 상태는 적용하지 않고 `engine.invalid_transition_ignored`로 기록한다. sequence는 관측된 최신값으로 유지하며 기존 중복·늦은 이벤트와 terminal rollback 방어를 보존한다.
- succeeded 반영 전 출력 개수, 각 CAS 객체의 존재·SHA-256, 경로·byte receipt를 확인한다. 누락/변조/부분 회수는 `failed`와 `error_kind/error_details` 및 `attempt.output_integrity_failed` 이벤트로 남긴다. 로컬 execute 경로의 review 전환도 같은 검증을 거친다.

새 동시성 검사는 renderer 진입/해제 Event와 두 번째 호출 진입 Event로 동기화하며 제한 시간 후 renderer를 반드시 해제한다. 고정 2-party barrier를 사용하지 않는다.

## 검증

- 구현 전 새 반례 테스트: `5 failed, 9 deselected`; 동시 renderer 횟수, 더 높은 sequence의 역행, 누락·변조·부분 출력이 각각 예상대로 검출됨.
- 회귀:
  `PYTHONPATH=/Users/winterholic/development/projects/winterholic-ttakkari/back-g0-cpu/src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_creative_director.py unit_tests/test_creative_studio.py unit_tests/test_creative_catalog.py`
  출력: `32 passed in 1.83s` (기존 27개 포함, 새 동작 검사 5개).
- `ruff check src/ttakkari/creative/director.py src/ttakkari/api/creative.py unit_tests/test_creative_director.py tests/test_creative_api.py` → `All checks passed!`
- 같은 4개 파일의 `ruff format --check` → `4 files already formatted`; `git diff --check` → 출력 없음, exit 0.
- 도메인 회귀 안의 ASGI in-process API 경로는 실행됐지만 PostgreSQL-backed `tests/test_creative_api.py`는 이번 Run에서 미실행. 기존 보고서의 `initdb` SysV IPC denial 명령을 재시도하지 않았고, 운영 PostgreSQL에 대한 판단 근거로 사용하지 않는다.
- 실제 GPU/managed runtime, 배포 검증은 실행하지 않음. G1 자원 lease는 범위 밖.

독립 재검수는 남아 있다. PMS 6.1은 `doing` 유지이며 완료 판정이 아니다.

## 2026-10-22 독립 재검수 후속: in-flight terminal race

- 추가 검수 원문: `outbox/a4b3b8b8-d8c7-453b-ae85-7f9a5f9867bb/g0-cpu-independent-recheck-report.md`. 해당 exact HEAD `3ec1561dc34d5f213ddae11bbe6defea614341af`에서 앞선 세 결함과 cross-process live claim, 죽은 renderer process의 stale claim 재개는 통과했다. 별도 in-flight race는 renderer hold 중 `failed(seq=1)` 기록 뒤 renderer를 풀면 `review_pending`으로 바뀌어 불합격이었다.
- 직접 코드 대조: renderer 결과 후 `collecting` 전환과 각 `outputs` receipt 기록은 `cancelled/review_pending`만 보호했다. 최종 성공 분기는 `cancelled`만 검사했다. 예외 분기도 `failed`를 terminal로 보지 않아 이미 기록된 engine failure 종류/상세를 `collect_failed`로 덮을 수 있었다.
- RED: 새 Event 기반 검사 5개 중 4개 실패(성공 반환, renderer 예외, 두 번째 출력 회수 중 failed, succeeded 이벤트 선행 후 receipt 재기록); in-flight cancel 보호 검사는 기존 동작을 확인해 통과했다.
- GREEN 수정: 하나의 terminal/unowned claim 가드를 renderer 결과, 각 출력 전후, 최종 성공, 예외 처리에 공통 적용했다. renderer가 claim을 소유하는 동안 `ingest_event`의 failed/cancelled/succeeded는 claim을 빼앗지 않고, failed는 `engine_failed`와 event ID/sequence를 first-error 증거로 남긴다. 실행 소유자가 terminal을 확인하면 자기 claim만 비우고 terminal 상태·실패 증거를 유지한다. 회수 중 이미 CAS에 기록된 파일과 앞선 output receipt는 지우지 않는다.
- 검증: 동일 G0 회귀 명령(`test_creative_director.py`, `test_creative_studio.py`, `test_creative_catalog.py`) 재실행 결과 `37 passed in 2.14s`. 여기에는 기존 32개와 이번 추가 5개가 포함된다. 이번 diff는 cross-process claim 획득 알고리즘을 바꾸지 않았다. 그 항목과 죽은 process 재개 결과는 위 exact-3ec 독립검수 보고서에 있고 이번에는 재실행하지 않았다.
- 정적 검사: 관련 Ruff check 통과, 4개 관련 Python 파일 format check 통과, `git diff --check` exit 0.
- 이번 Run에서 PostgreSQL-backed API, GPU/managed runtime, 배포는 미실행. `initdb` denial을 재시도/우회하지 않았다. G1/Lab/PMS 변경 없음.

## 2026-10-22 별도 main 통합 점검

- 별도 checkout `/Users/winterholic/development/projects/winterholic-ttakkari/back-g0-main-integration`에서 원격 `main` `ba71814167d281d1d1ff31ba405692eb12f87a23`을 기준으로 `codex/g0-cpu-control`의 정확 검수 HEAD `2756f8b4dc23cbb546c995775fe95969b1e4dd1d`를 `--ff-only`로 통합했다. 최종 로컬 `main`도 `2756f8b4dc23cbb546c995775fe95969b1e4dd1d`이며, feature와 트리가 같다. G1 `561f6f6`는 조상이 아니다.
- 통합 checkout에서 `unit_tests/test_creative_director.py`, `unit_tests/test_creative_studio.py`, `unit_tests/test_creative_catalog.py`: `37 passed in 1.95s`. ASGI in-process 경로는 이 회귀 안의 `test_authenticated_router_connects_cpu_service_without_http_or_gpu`에서 실행됐다.
- API route inventory 함수 단독 실행은 G0 endpoint 5개가 `ROUTE_SCOPE`에 없어 실패했다: GET production, POST production, artifact upload, attempt, attempt cancel. 따라서 API 경계/route gate는 통과로 판정하지 않았다.
- `tests/test_route_inventory.py`를 pytest로 포함한 시도는 `tests/conftest.py`의 PostgreSQL fixture `initdb -D /tmp/ttakkari-test-58161-fbfa0e9d/data ...`에서 exit 1로 수집이 중단됐다. 출력은 `CalledProcessError`이며 구체 OS 원인은 노출되지 않았다. 이 initdb 명령을 재시도하거나 우회하지 않았다. PostgreSQL-backed `tests/test_creative_api.py`와 배포 서버 게이트는 미실행이다.
- 정적 검사: Ruff 4개 관련 파일 `All checks passed!`, format `4 files already formatted`, `git diff --check` 통과.
- 실제 `8791/api/health`와 `/api/version` 응답은 `200`, active runtime revision/path는 `b1a3d162d9c2749d344d6b9a37783c6f456fc02e`, `/Users/winterholic/.ttakkari/deploy/releases/b1a3d162d9c2749d344d6b9a37783c6f456fc02e`. 배포 source 설정은 공유 checkout `/Users/winterholic/development/projects/winterholic-ttakkari/back`이다.
- 그 공유 checkout은 HEAD `66c8402c2f92d6114a9dcf1df8c6ade80e40db1a`, tracked/index/untracked 변경이 있고 `origin/main`과 분기됐다. G0 후보는 해당 HEAD의 조상이 아니므로 이 checkout을 수정하지 않았다. 원격 `main`은 계속 `ba718141...`이며 push·deploy reservation은 하지 않았다.
- PMS 검색에서 “CPU 6.1”과 일치하는 항목을 특정하지 못했다. WBS `15.6.1`은 PhotoCraft로 확인돼 잘못된 항목을 수정하지 않았다. CPU/GPU/전체 플랫폼 완료율 변경 없음.
- 미완료: route manifest의 5개 owner 경계 분류 및 독립 재검수 여부 결정, source checkout ancestry 안전 정렬, PostgreSQL/API와 backend schema/protocol/readiness/ownership/drain 게이트, main push 및 배포 예약.

## 2026-10-22 route manifest 분류 보완과 main 반영

- `tests/test_route_inventory.py`의 `ROUTE_SCOPE`에 G0 production 경로 5개를 `owner-plain`으로 추가했다. 값은 라이브 `collect_routes()`가 보고한 인증 선언과 같고 기존 `/api/creative/*` 경로와도 일치한다. 런타임 코드 변경은 없다.
- 라이브 라우트와 분류표 대조: missing `[]`, stale `[]`, changed `{}`, bad labels `{}`. 이 대조는 pytest 대신 함수를 직접 호출해 얻었고, pytest 경로는 conftest의 PostgreSQL `initdb` 한계 때문에 다시 실행하지 않았다.
- 재검증: CPU 37개 `37 passed in 3.22s`, `test_creative_director`+`test_creative_api` 21개 통과, Ruff check 통과. `tests/test_route_inventory.py`는 기존부터 format check 불일치가 있어(이번 5줄과 무관한 기존 긴 줄) 일괄 포맷하지 않았다.
- 독립 재검수 조건: 이번 변경은 테스트 분류표 5줄이며 G0 로직은 검수된 `2756f8b`와 같다. 추가 G0 로직 변경이 생기면 독립 재검수를 다시 받는다.
- 배포 차단: 배포 source `back`의 HEAD `66c8402`는 원격에 없는 로컬 전용 커밋이고 tracked/index 변경 50건이 있다. 후보는 이를 포함할 수 없고 ff-only 정렬도 불가능해 수정하지 않았다. `request_backend_deploy`는 같은 409를 반복하지 않으려고 호출하지 않았다.
