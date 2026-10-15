# 2026-10-16 최초 안전 전환 자동화

- 사용자 승인: “최초 1회 안전전환하고 괜찮으면 운영활성화해”. 기존 설계·구현은 유지하고 운영 bootstrap의 누락 부분을 보완했다.
- 조사 시점 운영: main 및 공개/로컬 SHA `4bdb330ba360b3e46bbcdb0ef26c190c9b5a8f55`, PID 58822, health ok. 실행 Run은 이 요청 1개, STT running 및 preview processing은 0개. 원본 launchd는 checkout의 uv/Python을 사용했다.
- 기존 외부 감독의 idle 배포 성공 뒤 새 감독이 사용자 승인 파일을 읽어 같은 예약에서 최초 전환을 이어간다. 내부 Run에서 launchctl·재시작·잠금 파일 작성을 하지 않는다.
- 중간 legacy 앱은 holding 상태로 consumer/복구/예열을 시작하지 않고 새 요청에 503 Retry-After를 준다. 첫 migration 전에 실행 중 Run/STT/preview가 있으면 실패해 기존 재시작을 막는다. 구버전 HTTP·색인의 전수 계측은 없으므로 최초 구버전 재시작은 새 drain 보장 범위 밖이다.
- 고정 release·전용 Python/Node·Caddy validate → standby → legacy 서비스/plist 정리 → proxy → health/version/인증 조회/SSE cursor → DB 활성화 → 재검증 → 고정 supervisor/plist/exec → 활성화 marker 순서다.
- 활성화 전 실패는 legacy 모드를 재기동한다. 활성화 시도 이후에는 작업을 보존하고 recovery_pending으로 남긴다. 자동 무한 재시도는 하지 않는다. 결과 Web Push와 배포 JSON에 상태를 남긴다.
- 구체적 운영 결과는 배포 예약 id와 최신 실행 기록으로 확인한다. 이 기록은 구현 내용을 설명하며 운영 전환 완료를 주장하지 않는다.
- 실행 검증: `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_bootstrap_b54d9cb4 .venv/bin/python -m pytest -q -x` → **819 passed, 2 xfailed, 6 subtests passed in 160.76s**. 이후 추가한 결과 알림·최종 공개 재검증·plist/복구 검사까지 반영한 최종 관련 회귀는 별도 격리 DB에서 `pytest tests/test_generation_bootstrap.py tests/test_generation_deployment.py tests/test_generations.py tests/test_deployment.py tests/test_generation_processes.py tests/test_push.py -q -x` → **83 passed in 12.58s**. 실제 두 ASGI/Caddy와 가짜 CLI 검사 포함. `ruff check src tests alembic`, `git diff --check` 통과. 외부 감독은 예약 후 고정 커밋의 전체 검사를 다시 수행한다.
- 원시 로그: `/Users/winterholic/.ttakkari/data/outbox/b54d9cb4-a48a-441a-96bc-6dc84fae2f49/full-regression.txt`, `final-focused-tests.txt`.
