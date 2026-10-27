# 수집 0건 표시 교정

원 수집 0건의 운영 원인 조회는 진단 배포 c664eb2e/b49cae56 및 예약 후속이 소유한다.
배포 대기 중에도 독립적으로 교정 가능한 표시 결함을 처리했다.

## 변경

- missions.view가 job 전체 종료·적재 대기 없음·실제 확보 자료 0건인 경우 `partial` 대신 기존 상태 `failed`를 반환한다.
- 1건 이상 확보한 임무의 성공/일부 수집 판정, 진행 중/재시도/적재 대기, 사용자 일시정지·취소 분기는 그대로다.
- DB 상태·수집 데이터·검색 공급자·정책·예산·재실행 경로는 변경하지 않았다.
- 따라서 이 교정은 수집을 실제로 복구한 증거가 아니다. 원 아침 임무의 직접 원인은 진단 배포 활성화 후 조회해야 한다.

## 증거

- 실제 missions.view를 호출하는 9개 회귀 중, 종료 작업 acquired=0의 3개가 RED(`partial != failed`), 기존 경계 6개는 PASS였다.
- 단일 분기를 교정한 뒤 같은 9개 모두 PASS.
- 관련 묶음: `PYTHONPATH=src ../back/.venv/bin/python -m pytest unit_tests/test_collection_mission_empty_result.py unit_tests/test_collection_runtime_report.py unit_tests/test_collection_adapters.py unit_tests/test_collection_plan.py unit_tests/test_collection_research.py -q`
  → `51 passed in 18.03s`.
- 변경 Python Ruff 및 `git diff --check` 통과.
- 회귀는 조회 집계 결과를 구성해 실제 view의 상태 분기를 검증한다. 운영 DB/HTTP/UI/실수집 증거로 확대하지 않는다.

## 다음

main push 후 누적 배포를 예약한다. 기존 진단 배포를 무작정 취소하거나 검사 중 상태를 반복 조회하지 않는다.
영속 예약 87639309-2085-488a-a394-c7cb9507c508(2026-10-27T11:21:32Z)의 후속은 최신 배포의 누적 관계를 확인해
활성화된 진단으로 실제 실패 코드를 읽고 원 단계 교정·원 임무 재실행·실적재/검색 확인까지 이어간다.

## 배포 시험 실패 교정 (20:20 KST 후속)

- 11:18:48Z MCP runtime_report `83a64e92-d2cc-43e3-9c3f-c5224d0f140b`에서 운영은 여전히
  `78c4a2e7`, collection 진단 미활성이었다. 수집 복구로 보고하지 않는다.
- 첫 진단 배포 `b49cae56`은 release pytest의 실제 Phaser 빌드 시험이 120초 제한으로 실패했다.
  이는 배포 시험 실패이며 운영 수집 0건의 원인으로 확대하지 않는다.
- 누적 배포 `a40a0203`의 로그는 `test_collection_research_flow.py:173`에서 실제 `failed`와
  기존 기대 `partial` 불일치를 기록했다(903 passed, 1 failed). Codex 명령 환경의 격리 PostgreSQL에서도
  동일 시험을 실행해 `1 failed in 1.18s`로 재현했다.
- 0건 종료를 failed로 표시하는 앞선 변경에 맞춰 통합시험 기대값을 교정하고, 공급자 실패와 정상 빈 응답
  각각 acquired/imported=0 및 failed를 함께 단언했다. 오류 원인 구분·재시도·회복 후 적재 검증은 유지했다.
- `PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_collection_research_flow.py tests/test_collection_missions.py unit_tests/test_collection_mission_empty_result.py unit_tests/test_collection_runtime_report.py --deselect=tests/test_collection_research_flow.py::test_script_shell_page_is_rendered_collected_and_searchable_through_the_real_pipeline -q`
  → `60 passed, 1 deselected in 8.38s`. 변경 Python Ruff와 git diff --check 통과.
- 처음 실행한 전체 61건 중 SPA 브라우저 시험은 imported=0으로 실패했고 나머지 60건은 통과했다.
  해당 시험의 브라우저 실행은 Codex 명령 환경에서 이루어졌으며 사전 preflight를 하지 못했다.
  추가 브라우저 실행은 중단했다. 실패의 세부 원인은 미검증이며 운영 장애로 일반화하지 않는다.
  위 deselect는 이 환경의 검증 범위를 명시한 명령으로만 사용했고 소스의 skip/배포 gate는 변경하지 않았다.
- 후속 지정 preflight 실행은 exit 78, `status=blocked`, `browser_launched=false`, macOS mach-register/
  launchservicesd/coreservicesd mach-lookup denied였다. 이는 이 Codex 명령 환경의 브라우저 IPC 제한이다.
  pre-push의 변경시험 전체 파일 실행도 이 시험을 호출하므로 진행 중 push를 Ctrl-C(exit130)로 중단했다.
  재실행은 PYTEST_ADDOPTS에 해당 브라우저 시험 하나의 deselect만 지정해 계약·비브라우저 변경시험을 실행한다.
  배포 controller의 전체 검사 설정 및 소스 skip은 바꾸지 않는다. 운영 브라우저 건강을 판단하는 증거는 아니다.
- 진단 배포 재예약 뒤 실제 mission 상세, 안전한 재실행 및 실적재/검색 확인 요구는 계속 미완료다.
