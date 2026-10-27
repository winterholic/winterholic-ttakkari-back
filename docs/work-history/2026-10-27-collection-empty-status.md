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
