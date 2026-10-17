# Qwen Flash Next 분리 모니터링 — 2026-10-17

사용자 요청: 접수 경량 Qwen과 Flash Next 사용량을 분리하고 CPU·메모리 자원 추이를 표시한다. 프론트의 같은 날짜 작업 기록과 연결된다.

- 인증된 `GET /api/accounts/qwen-flash-usage`를 추가했다. 별도 로컬 서버(기본 loopback 18987)의 `/props` model_alias를 먼저 확인한 뒤 `/metrics`만 조회한다. 접수용 집계 테이블이나 실행 경로를 변경하지 않는다.
- 입력은 cache 제외 처리 토큰, cache 재사용·출력 토큰, 처리 중·대기 요청 수를 별도로 반환한다. `server_lifetime`은 모든 클라이언트의 현재 서버 실행 누적이며 재시작 시 초기화된다. 일간/주간으로 오해하거나 접수 토큰과 합산하지 않는다.
- 서버 연결 실패·기동 중·모델 불일치·계측 부분 누락을 분리한다. 미확인 토큰은 null이며 유휴 0과 구별된다. 추론, 모델 로드, 프로세스 기동/종료는 수행하지 않는다.
- `.env.example`에 port/model 설정을 문서화했다. 스키마 변경 없음: Alembic `0037_worker_routing` 단일 head 및 protocol 1 유지.

## 직접 확인한 증거

- 초기 unit 테스트는 모니터 구현 부재로 9 failed, 구현 후 9 passed.
- 최종 명령: `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_monitor_20261017 PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_qwen_usage.py tests/test_system_resource_history.py tests/test_system_resources.py tests/test_deploy_gate.py tests/test_generation_deployment.py unit_tests/test_qwen_flash_monitor.py -q` → 63 passed in 5.84s. 별도 테스트 DB만 변경했다.
- 변경 Python 파일 Ruff 및 git diff --check 통과.
- 운영 `/api/version`은 검사 시 `8672449ce7ca3b12fc6ba2434b0e1d9078e68401`, runtime path는 제공된 배포 release `~/.ttakkari/deploy/releases/8672449ce7ca3b12fc6ba2434b0e1d9078e68401`과 일치한다. 새 모니터 함수를 명령 샌드박스에서 직접 호출했을 때 실제 로컬 Flash Next 서버에 GET하여 입력 6,732/cache 26,609/출력 1,804, processing/deferred 0을 관측했다. 이는 새 API의 운영 배포/사용자 로그인 검증과 다르다.
- 운영 자원 테이블은 READ ONLY 트랜잭션으로 최근 1시간 117행을 읽었다. 프론트 렌더링 검증에만 사용했다.
- 원시 결과: Run outbox `795b8234-b1ec-4b1f-a505-ccaa1fea56bc/backend-tests.txt`, `monitor-observation.json`.

배포는 clean main commit·push 후 controller 예약으로 진행한다. 이 기록 작성 시 새 API의 운영 반영은 미검증이며 예약 수락을 성공으로 표시하지 않는다.
