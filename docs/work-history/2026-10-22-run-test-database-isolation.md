# Run별 격리 테스트 DB와 Qwen 수집 반영 (2026-10-22)

## 결과
- main 반영 커밋: 0701925(Run 전용 테스트 DB), 4dae0c5(Qwen Flash 60초 수집·최종 엔진 표시, 7c0dedd cherry-pick), 39f96a9(고아 클러스터 회수), 451fd21(ADR).
- 설계: 엔진 시도마다 백엔드(샌드박스 밖)가 /tmp 아래 전용 PostgreSQL 클러스터를 띄우고 TTAKKARI_TEST_DB만 엔진 env로 넘긴다. 공유 호스트 PostgreSQL에는 createdb를 하지 않는다. 종료·취소·오류 때 backend가 정리하고, 기동 시 owner PID가 죽은 마커 폴더만 회수한다.
- 후속 테스트(unit_tests/test_run_test_database_manager.py): 시작 실패, spawn 실패, 취소(시작 중·pump 중), 엔진 종료 뒤 close 순서, 테스트 러너의 중첩 생성 금지, 살아 있는 세대 소유 클러스터 보존. 15 passed.

## 실측 검증 (Claude 엔진, 호스트 Mac Studio, 2026-10-22 23:08~23:45 KST)
- 환경: Codex 중첩 샌드박스가 아닌 Claude 작업 환경. 운영 DB·공유 PostgreSQL에는 createdb하지 않고 `/tmp/ttk-rtdb-verify/root` 아래에서만 `RunTestDatabase`를 직접 구동했다(스크립트 check.py, crash.py).
- 실제 initdb/start 성공, 두 클러스터 동시 기동: 포트 58541·58551(서로 다름), DB 이름 ttakkari_test, 폴더 모드 0700, 자식 env 키 PGHOST/PGPORT/TTAKKARI_TEST_DB.
- 인증: 잘못된 비밀번호 InvalidPasswordError, A 계정으로 B 포트 접속도 InvalidPasswordError.
- 독립성: A에만 marker_a, B에만 marker_b 테이블. A close 뒤 B 쿼리 정상.
- 정리: close 뒤 폴더 삭제, postmaster 사망, 루트 비어 있음. SIGKILL(owner+postmaster) 뒤 reap_stale이 폴더를 회수했고 고아 shm 수(nattch 0)는 21 그대로였다.
- Qwen 60초 수집: tests/test_qwen_usage.py, tests/test_qwen_emergency_e2e.py, unit_tests/test_qwen_flash_monitor_jobs.py 20 passed. 실서버 Flash를 읽기 전용 snapshot()으로 조회: metrics_status=available, server running. llama-server는 건드리지 않았다. 배포된 backend 안에서 60초마다 실제로 도는 것은 미확인.
- 전체 pytest(origin/main 병합 후보 38dbce5 기준) 25 failed, 14 errors, 3979 passed. 원인은 모두 환경이었다: 작업 트리에 .venv 없음(alembic·restore 스크립트 경로) 25건, aiosqlite 미설치 14건. .venv 심볼릭 링크 후 해당 파일 56 passed 1 skipped, aiosqlite를 임시 경로에 설치해 tests/delivery 48 passed. 단일 전체 실행으로 통과한 것은 아니며 재실행분을 합친 값이다.

## 발견한 호스트 위험
- SysV 공유 메모리 슬롯 상한 `kern.sysv.shmmni=32`가 32/32 찼고 그중 27개가 nattch 0·생성 PID 사망인 고아였다(다른 테스트·작업자 클러스터가 SIGKILL로 남긴 것). 이 상태에서는 initdb가 실패했다(원인은 슬롯 고갈로 추정, 당시 stderr 원문은 잡지 못함). 생성 PID가 죽은 nattch 0 세그먼트 6개(131073, 18087938, 3866627, 21889029, 5046278, 4849672)만 ipcrm으로 제거하자 바로 성공했다. 나머지 21개 고아는 남겨 뒀다.
- 제품 reaper는 자기 마커에 기록된 shm만 회수하므로 이 고아들을 치우지 못한다. Run 클러스터가 늘면 같은 상한에 닿을 수 있다. 후속: 외부 고아 정리 정책 또는 shmmni 상향(관리자 권한 필요, 호스트 소유자 결정).

## 배포
- 이전 409는 공유 checkout HEAD(integ/g1-66, dirty)가 451fd21의 조상이 아니어서 생겼다. 이후 다른 흐름이 75a12d6(origin/main 병합, 451fd21·fa1f92d 포함)을 예약했고 작업 시점에 testing이었다(job 28e7b324-2da7-43d9-a14e-3681e331c142, 607f8bd 예약은 superseded). 그 뒤 origin/main에 추가된 73edf7a·b3bb225는 테스트와 문서만 바꾼다.
- 공유 checkout의 소유자 변경(stash/reset/커밋)과 HEAD는 건드리지 않았다.

## 미검증
- 배포된 backend 안에서 실제 Run이 만든 클러스터(Run 경로 전체: manager→엔진 env 전달→정리)는 아직 못 봤다. 위 검증은 RunTestDatabase를 직접 호출한 것이다.
- Claude Seatbelt 프로파일상 shmget 차단은 없으나 커널 실행 probe는 미실행이다.
