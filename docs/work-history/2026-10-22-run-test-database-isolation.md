# Run별 격리 테스트 DB와 Qwen 수집 반영 (2026-10-22)

## 결과
- main 반영 커밋: 0701925(Run 전용 테스트 DB), 4dae0c5(Qwen Flash 60초 수집·최종 엔진 표시, 7c0dedd cherry-pick), 39f96a9(고아 클러스터 회수), 451fd21(ADR).
- 설계: 엔진 시도마다 백엔드(샌드박스 밖)가 /tmp 아래 전용 PostgreSQL 클러스터를 띄우고 TTAKKARI_TEST_DB만 엔진 env로 넘긴다. 공유 호스트 PostgreSQL에는 createdb를 하지 않는다. 종료·취소·오류 때 backend가 정리하고, 기동 시 owner PID가 죽은 마커 폴더만 회수한다.
- 후속 테스트(unit_tests/test_run_test_database_manager.py): 시작 실패, spawn 실패, 취소(시작 중·pump 중), 엔진 종료 뒤 close 순서, 테스트 러너의 중첩 생성 금지, 살아 있는 세대 소유 클러스터 보존. 15 passed.

## 미검증
- 실제 initdb 기동은 배포된 backend 안에서 아직 실행하지 못했다. 이 작업 환경의 샌드박스는 initdb가 shmget: Operation not permitted로 실패한다(재시도·우회 안 함).
- Claude Seatbelt 프로파일상 shmget 차단은 없으나 커널 실행 probe는 미실행이다.
- 배포: request_backend_deploy(451fd21)이 409(현재 브랜치 이력에 없음). 공유 back checkout HEAD가 다른 작업자의 integ/g1-66-main-20261022이고 dirty라 옛 ensure_revision이 거부한다. 수정(fa1f92d)은 main에 있으나 아직 배포되지 않았다.
- 후속 테스트 커밋은 pre-push 게이트(계약 묶음이 initdb를 띄움)가 이 환경에서 실패해 푸시하지 못했다. --no-verify는 쓰지 않았다.
