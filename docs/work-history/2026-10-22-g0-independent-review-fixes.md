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
