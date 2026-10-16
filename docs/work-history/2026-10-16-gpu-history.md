# 서버 리소스 GPU 기록 — 2026-10-16

## 구현

- `system_resource_samples` 테이블과 Alembic `0036_system_resource_samples` 추가. 기록 시각, GPU 전체 사용률·사용 메모리·GPU 상태, 전체 수집 상태, CPU 전체 사용률·사용 메모리를 저장한다.
- 서버 시작 시 주기 수집기를 실행한다. Legacy lifespan과 generation maintenance 양쪽에 연결했으며 화면 접속 여부에 의존하지 않는다. 30초마다 기존 `sysinfo.snapshot()`을 사용한다. `TTAKKARI_RESOURCE_SAMPLE_SECONDS` 기본값은 30초이고 0은 테스트·개발에서 끈다.
- `collection_status=failed` 또는 GPU 미지원·수집 실패에는 GPU 값 `null`을 저장한다. 정상 유휴 값 `0`과 구별한다. GPU 센서 문제와 무관하게 snapshot이 성공하면 가능한 CPU·메모리 값은 계속 기록한다.
- 인증된 `GET /api/system/resources/history?range=1h|24h|7d`를 추가했다. 1시간은 30초 원본, 24시간은 1분 평균, 7일은 10분 평균이며 집계 포인트에 표본 수와 결측 수가 있다. 응답은 `Cache-Control: no-store`다.

## 용량 결정

- 원본 샘플 보존은 90일, 30초마다 연속 수집할 때 최대 약 259,200행이다. 매시간 만료 행을 삭제한다.
- 90일 원본은 나중에 SQL로 분석할 수 있고 UI 조회 상한은 7일이다. 별도 장기 rollup 테이블은 복잡도와 중복 보관을 감수할 이유가 없어 추가하지 않았다. UI의 장기 구간은 조회 때 집계한다.

## 검증

- pytest: `tests/test_system_resources.py tests/test_system_resource_history.py unit_tests/test_gpu_resources.py -q` → **27 passed**. 실제 로컬 수집기를 호출해 테스트 DB에 GPU·CPU·메모리 샘플이 저장되는 테스트 포함.
- Ruff: 변경된 수집기·모델·API·수명주기·테스트 파일 통과. Alembic heads: `0036_system_resource_samples (head)`. `git diff --check` 통과.
- 로컬 격리 backend(`ttakkari_test`, 테스트 전용 DB, 5초 간격으로 실행)에서 최근 60초 **12행**을 조회했다. 최근 행은 `status=success`, `gpu_status=available`, `gpu_percent=0.0`, CPU 38.9%, 메모리 45,979,025,408 bytes였다. 이는 개발 프로세스 관측이며 운영 상태로 일반화하지 않는다.
- 브라우저는 테스트 DB에 만든 7일/10분 합성 구간(결측 11개)을 읽어 그래프 선 분리와 0% 표시를 확인했다. API 1h·7d 호출 모두 HTTP 200. 1280px 데스크톱과 390px 모바일 스크린샷은 이번 Run outbox의 `gpu-history-desktop.png`, `gpu-history-mobile.png`다. 테스트 Vite에서만 서비스워커 MIME 오류가 있었고 그래프 화면/API 동작에는 영향이 없었다.
- 사용자 지정 스크린샷 outbox `65310d41-04cf-4e07-a7ee-7c30c1fba342`는 현재 Run의 쓰기 허용 경로가 아니어서 자동 등록되는 현재 Run outbox에 저장했다.

## 배포

- backend/frontend main 푸시와 백엔드 예약, Vercel 프론트 반영 상태는 완료 시점 기록을 덧붙인다.
