# 2026-10-16 무중단 배포 3슬롯

사용자 요청: 두 슬롯을 세 슬롯으로 늘리도록 리팩터링하고, 회의록 계정 수정이 배포 가능한지 먼저 확인한다.

## 먼저 확인한 운영 상태

- 이 Run에서 loopback `/api/version`과 `/api/ready`, 배포 예약 JSON을 직접 조회했다. 회의 개인 모델 체인 SHA `6b07313103a0207114dde8e13ba34c715b971e17`은 active/ready였다.
- 회의 수정 예약 `a38f315a-e37d-437c-a779-36b6692424b5`는 `succeeded`, updated_at `2026-10-16T07:40:16.721502+00:00`(16:40 KST). 회사 계정을 켜야 하는 구버전은 운영에서 교체됐다. 실제 회의 재분석은 실행하지 않았다.
- `supervisor.json`은 여전히 초기 tools SHA `3054e5e0c5ce15d04724724b3e8a874b1ad1e888`의 immutable 경로를 가리킨다. 앱 배포가 이 감독의 코드를 교체하지 않는다. 이는 파일과 해당 코드의 `enter_pinned_supervisor` 경로를 대조한 결과이며 실제 감독 PID의 실행 경로를 별도로 관측한 것은 아니다.

## 구현

- `generation.PORTS=(8791,8792,8793)`를 공통 기준으로 사용. 등록 상한 3세대, 동일 origin의 살아 있는 소유자 중복 거절. DB CAS·Run 소유권·전역 lane/세션/계정 제한은 유지한다.
- 감독 루프가 기존 `draining`/`retiring` 예약들을 정리하면서 전환 전 예약 한 건도 진행한다. 세 슬롯이 차면 `waiting_slot`. recovery/rollback이 남아 있으면 새 전환을 막는다.
- 배출 단계는 이전 세대 UUID만 확인한다. 이미 다음 배포가 활성화된 뒤 이전 배포를 다시 활성화하거나 오래된 candidate 슬롯을 다시 만지지 않는다. UUID가 바뀐 슬롯의 종료와 최신 세대 위에 오래된 rollback을 적용하는 행위는 거절한다.
- current/previous 기록을 공개·로컬 전환 확인 시점으로 옮겼다. 늦은 배출 완료가 최신 커밋 기록을 되돌리지 않는다.
- 슬롯 3의 proxy 영속화, active 슬롯 복원, 원래 retirement crash 복구를 검사했다. README·설계 문서·MCP/에이전트 안내를 함께 갱신했다.
- 외부 호스트 전용 `python -m ttakkari.deployment upgrade-supervisor --revision <full SHA>`를 추가했다. 테스트 생략 없는 활성화 배포 영수증, immutable 릴리스·현재 active UUID·readiness·로컬/공개 버전을 확인하고, 배포 변경 잠금 아래 supervisor plist/marker만 교체한 뒤 감독만 kickstart한다. Run 환경은 명시적으로 거절하며 앱 세대·기존 엔진·프록시를 재시작하지 않는다. 새 감독의 실제 시작은 `supervisor-runtime.json`의 SHA·실행 경로·PID·ports로 확인할 수 있다.

## 검증과 환경 범위

분리 worktree `back-three-slots`, branch `feat/three-generation-slots-20261016`, 기준 main `8d3f922`. dev-experts의 Python/리팩터링 원칙과 TDD를 적용했다. 첫 5개 행동 검사는 기존 2세대 상한·없는 세 번째 포트·순차 대기 때문에 모두 실패하는 것을 확인한 뒤 구현했다. 외부 감독 갱신도 진입점 부재 실패를 먼저 확인했다.

- 집중 통합(실제 ASGI 3프로세스·Caddy·격리 PostgreSQL·가짜 엔진 포함): 80 passed in 18.00s. 이어 추가한 감독 갱신/루프 검사는 파일 단독 24 passed in 2.35s. 최종 전체 검사에는 이들 및 슬롯 3 복구 parameter가 모두 포함됐다.
- 최종 명령:
  `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_test_three_slots_1016 .venv/bin/python -m pytest -q -x --tb=short --junitxml=<outbox>/three-slots-all-final.xml`
  결과: **981 passed, 2 xfailed, 6 subtests passed in 189.26s**.
- `ruff check src tests alembic` → `All checks passed!`, `git diff --check` → 출력 없음/0.
- 격리 테스트에서 두 이전 가짜 CLI의 PID가 두 차례 전환 후에도 유지되고, 실제 프록시로 세 번째 세대의 새 Run 완료와 이전 두 Run의 소유자 취소 전달을 확인했다. 운영 실계정 LLM 부하 검증과 구분한다.
- Run 명령 환경에서 `uv sync --frozen --quiet`가 `/Users/winterholic/.cache/uv/sdists-v9/.git` 쓰기 권한 거부로 초기화에 실패했다. 같은 거부를 재시도하지 않고 기존 back 가상환경을 읽어 사용했다. 첫 전체 검사는 분리 worktree의 `.venv/bin/alembic` 경로 부재로 589 passed 뒤 중단됐다. 테스트 동안 `.venv -> ../back/.venv`를 연결해 경로를 맞추고 전체 검사를 다시 통과했다. symlink는 검사 후 제거했으며 기존 가상환경은 수정하지 않았다. 이 두 실패는 Run 테스트 환경에 한정되며 운영 배포기의 환경 빌드 실패를 뜻하지 않는다.
- 원시 결과: `/Users/winterholic/.ttakkari/data/outbox/03fc443f-ec31-4e3a-bf49-a1218478462a/three-slots-all-final.log`, `three-slots-all-final.xml`, 집중 검사 로그/XML과 첫 환경 실패 로그/XML.

## 운영 적용 경계

검증한 변경은 clean main에 통합·push한 뒤 `request_backend_deploy`로 예약한다. 예약 ID·전체 SHA와 실제 접수 상태는 이 Run의 최종 보고/메모리에 남긴다. 예약 접수를 배포 성공으로 보고하지 않는다.

**아직 미실행:** 기존 고정 감독의 외부 교체, 새 감독 실행 경로·PID 확인, 운영 세 번째 슬롯으로 실제 배포 전환. Run 내부 launchctl/서비스 재시작 금지 지침 때문에 이 세션에서 해당 제어를 실행하지 않는다. 감독 교체 명령 파일을 outbox에 제공한다. 앱 예약 이후 폴링만 하려고 Run을 유지하지 않는다. 회사 회의 실재분석 및 장시간 부하 검증도 미실행이다.
