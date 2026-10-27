# 수집 0건 복구: 운영 진단·안전 재시도 경로 (복구 미완료)

## 현재 운영 증거

- 2026-10-28 07:08 KST `host_operation_start(runtime_report)` d710d53a-e9b2-45ba-8206-a5983b46af05:
  active `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809`, 배포 4b5b73fc succeeded.
- 원 임무 `d0183376-fc0a-4720-a436-955c322bc10a`(디자인 시스템·UI/UX 지식 백과사전 구축):
  run1, acquired/accepted/imported 0, mission_prepare failed, attempts_exhausted, attempts 3,
  검색 사용 13, 첫 gap 5개 source_policy_required. 정책 gap과 handler 실패의 인과는 미확정.
- 기존 세션 1cf16f1d 마지막 작업은 게임 진단 로컬 3a7e20cf에서 push 제약으로 종료.
  진단 배포가 뒤에 성공했어도 실제 수집 재실행은 이어지지 않았다.
- 이번 `ops_list_issues(query=collector,since_hours=48)`, `ops_search_logs(query=d0183376,since_hours=48)`,
  `ops_search_logs(query=collector handler failed,since_hours=72)` 모두 total 0.
  제품 DB 작업 오류가 없다는 뜻은 아니다.

## 진행과 범위

`jobs.finish`는 재시도 소진 시 원 code를 attempts_exhausted로 덮지만 runner가 남긴 예외 type은 보존한다.
기존 runtime_report는 type을 투영하지 않고, 기존 사용자 임무 API는 사용자 로그인 인증을 요구한다.
세션 토큰으로 사용할 임무 진단/재시도 MCP 경로가 없어 실제 실패 증거 조회 및 재실행을 완료할 수 없었다.
이는 복구 수단의 누락이며 원 수집 장애의 직접 원인을 입증하지 않는다.

- 기존 임무 모듈에 agent GET 진단 / POST retry API를 추가한다. 기존 세션 토큰과 owner tenant를 검사한다.
  기존 `operations.personal` 정책을 재사용해 개인 운영 도구의 pool 경계를 유지한다.
  앞선 work 단독 시험의 403은 묶음 시험에서 재현되지 않아 정책 존재의 근거로 쓰지 않았다.
  work 토큰이 200인 부정 사례를 확인한 뒤 명시적인 공통 정책 검사를 연결했다.
- 진단은 count, 현재 회차 job code/type, gap, 검색 실패만 반환한다. 원 요청 context/goal,
  문서 본문, raw error message는 제외하고 문자열을 기존 scrub_value로 마스킹한다.
- 재시도는 기존 mission-control 트랜잭션 잠금 아래 expected_run_seq를 검사한다.
  회차 변경·진행 중 job·사용자가 paused/cancelled한 임무는 409. 기존 rerun 경로로 새 회차 하나를 만들며
  정책·예산·수집/적재 파이프라인을 우회하지 않는다. 재호출이 새 회차를 취소하지 못한다.
- MCP collection_mission_status(read-only)와 collection_mission_retry(write), 도구/라우트 manifest를 연결한다.
- 별도 깨끗한 worktree `back-collection-recovery-20261028`, base c70bce7. 기존 진단 worktree와
  미커밋 변경이 많은 기본 back은 변경하지 않았다. 3a7e20cf를 무조건 재적용하지 않는다.

## 실행 검증

- 구현 전 `pytest tests/test_collection_agent.py -q --tb=short`: 5 failed (새 API가 없어 404).
- 구현 후 같은 새 API 5개 통합 검사: 통과. 진단 type 노출/본문 미노출, 새 회차 단일 생성,
  중복 요청의 409 및 기존 새 job 보존, active/paused 보호, 인증/타 tenant 404, 임의 파라미터 422를 확인.
- `PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_collection_agent.py tests/test_route_inventory.py
  tests/test_collection_missions.py tests/test_collection_research_flow.py tests/test_orchestrator.py -q --tb=short
  -k 'not test_script_shell_page_is_rendered_collected_and_searchable_through_the_real_pipeline'`
  → **99 passed, 1 deselected in 20.13s**. 격리 PostgreSQL, 운영 DB 미접속.
- 앞선 확장 검사에서 실제 Chromium SPA 시험 1건은 imported 0 != 1로 실패했다. 기존 Codex 명령 환경의
  browser IPC 제한 관측이 있어 이후 실행에서 해당 시험을 제외했다. 원인·브라우저 동작은 미검증이며,
  같은 브라우저 실행을 반복하거나 배포 전체 시험을 제외하지 않는다.
- 변경 Python Ruff `All checks passed!`, `git diff --check` 출력 없음.
- 첫 push는 계약 검사에서 UI 도구 목록 2개 누락 및 보안 검사 inventory 누락을 잡아 거절했다
  (82 passed, 2 failed; 변경 검사 51 passed). 해당 두 등록 위치를 보완하고 다시 push 게이트를 실행한다.
  인증과 시험 DB는 이 Codex 환경에서 실행됐으며 샌드박스 문제로 일반화하지 않는다.

## 다음 실행

push 계약 검사와 배포 예약 뒤 controller에 대기를 맡긴다. 활성화 후 새 MCP status 도구로
원 임무의 실제 오류 type 및 검색 실패를 확인하고 해당 경로만 교정한다. 원인을 확인하기 전
단순 정책 해제/무조건 rerun을 하지 않는다. 안전한 retry(run_seq 확인) 후 실제 수집, KB 적재,
검색을 각각 확인해야 원 목표가 끝난다. 본 변경과 단위/격리 통합 검사는 운영 복구 증거가 아니다.
