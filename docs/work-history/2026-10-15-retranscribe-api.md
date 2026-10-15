# 2026-10-15 · 다시 전사 UI 후속 백엔드

- 요청: 프론트 b8bd459의 다시 전사 UI에 원본/파생 회의 연결과 새 job 상태를 제공하고, 잘못된 본문을 이용한 지원 확인 요청이 회의를 생성하지 않는 동작을 고정한다. 프론트는 수정하지 않는다.
- 시작: backend main 1b540560f870a963223a78f6099e6a089849d0e9. `git pull --ff-only` 결과 최신 상태였으며 1ae3d7d와 NUL 정화 63dec4a를 포함한다.
- `meetings.source_meeting_id`에 직접 원본을 저장한다. 인덱스·자기 테이블 FK의 ON DELETE SET NULL을 추가했고, 파생 회의 삭제로 전파하지 않는다. 목록/상세 공용 읽기 모델이 한 번의 추가 쿼리로 각 회의의 직접 자식을 생성 시각·UUID 순으로 모은다. 검색·상태·페이지와 독립적인 ID 목록이다.
- migration 0027은 0026 다음에 실행한다. 기존 전체 STT job의 params에서 유효한 기존 원본을 찾아 연결하고, 자기 참조·고아·잘못된 ID·구간 후보를 제외한다. 여러 유효한 전체 job은 생성 시각·UUID가 이른 것을 사용한다. 원본 회의·전사·분석·job 내용·수정 시각은 보존한다.
- 재전사202 응답에 `stt_job: {id,status}`를 추가한다. 새 job의 queued 상태를 응답에 담은 뒤 worker를 깨운다. `stt_job_id`는 채택된 성공 전사 의미를 유지하므로 큐 등록 시 null이다. 이후 job 목록으로 상태를 갱신한다.
- 요청 본문의 auto_analyze는 strict JSON bool이다. 프론트의 문자열 probe와 문자열 true/false·숫자0/1·null·배열·객체는 생성 전에422다. 생략 기본false와 실제bool true/false는 허용한다. 인증401·유효한 본문의 없는 회의404·원음/런타임 없음409는 유지한다.
- TDD: 기능 누락으로 API6 실패/기존 안전 동작5 통과를 관측했고, 별도0026 DB에서 source 컬럼 부재로 migration 테스트가 실패함을 확인했다. 구현 후 관련27 통과. 추가 정상true·런타임 미준비 테스트를 포함해 전체 pytest를 별도 DB `ttakkari_retranscribe_41d9e099`에서 실행한다. 퇴역 외래키가 남은 기본 ttakkari_test는 변경하지 않는다.
- 변경 Python ruff check·새 파일 format check·compileall·git diff --check를 확인했다. FastAPI AST 검사기의 응답모델 경고는 기존22개와 변경 후22개의 메시지가 같으며 새 항목은 없다.
- 계약은 docs/stt/utterances-api.md와 docs/stt/README.md. 원음·개인 전사·DB 값은 커밋하지 않는다. 운영 마이그레이션·재시작·배포는 이 작업에서 실행하지 않는다. 배포 시0027을 먼저 적용한다.
- ~/work-history는 현재 파일시스템 쓰기 범위 밖이므로 저장소의 이 기록과 실행 결과함에 남긴다.
- 첫 전체 실행은654 passed/2 xfailed/1 failed였다. 실패는 기존 `test_waiting_run_resumes_same_run_after_reset_with_progress_handoff`에서 재개 프로필이 claude 대신codex로 선택된 것이다. 단독 재실행은1 passed in2.60s. 계정별 남은 초를 정수로 자른 뒤 현재 시각에 더하는 기존 쿨다운 계산이 타이밍에 따라 순서를 바꿀 가능성을 코드에서 확인했으나, 실패 실행의 개별 타임스탬프는 수집하지 않아 원인 확정은 하지 않는다. 해당 범위 코드는 변경하지 않고 전체 검사를 다시 실행한다.
- 최종 전체 검증: `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_retranscribe_41d9e099 .venv/bin/pytest -q --tb=short` → **655 passed, 2 xfailed in135.68s**。테스트 출력은 `tr -d '\000'`로 정화했다. migration0026→0027·되돌리기·재적용을 실제 별도 DB에서 검증했다. 전체 통과 후에는 이 기록만 갱신했다.
