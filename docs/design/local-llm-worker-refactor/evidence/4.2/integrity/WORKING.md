# WBS 4.2 관측 무결성 중간 checkpoint (후속 결과는 §10/summary.json)

2026-10-23. 작업 브랜치 fix/llme-observation-integrity-20261023, 시작 HEAD 38b5468.
처음 git pull --ff-only 는 detached HEAD라 실패. 작업 브랜치 만든 뒤 git pull --ff-only origin main 실행, Already up to date.

- 수정 전 재현 원출력 before.txt: plain=FAIL/FAIL_REGRESSION helper_attack=PASS/None. 입력 복사형 attempt 교체 공격은 정답 상수 없이 작동.
- 구현 중: integrity.py의 소스 admission, sealed source loader, 후보 모듈 전용 builtins/import, module capability view, AST 속성 load/store 가드. 일반 속성 쓰기는 guarded subscription으로 바꿔 RHS 순서/언패킹/증분/loop/with 의미 보존. audit hook은 후보 filename 직접 호출의 frame/GC 이벤트를 기록. trusted stdlib의 inspect/traceback은 허용.
- runner는 시작 시 trusted helper/runner bytes를 고정하고 -c bootstrap으로 실행. 부모는 후보 소스 내용을 spec으로 봉인, 실행 중 디스크 소스 변경은 다음 import에 반영하지 않음. 판정은 여전히 부모에서만.
- N16~N33(18종)을 gold 위에 주입하여 9과제에 추가. EXP는 read-only 쓰기 위반으로 거부. 나머지는 checker patch 거부. 구 helper 복사 공격도 test_llme_integrity.py에 있음.
- REF/LNG의 신뢰된 dependency sentinel probe를 디스크 소스 쓰기 대신 trusted module patch로 변경(봉인 loader와 충돌 방지). LNG는 billing/__init__ eager import 때문에 invoice 캐시를 trusted probe에서 제거 후 재import.
- TST N7(SUT patch) 사유는 기존 mutant 결과 FAIL_HIDDEN에서 조기 FAIL_CHECKER_PATCH로 더 정확하게 변경. PASS 허용 없음. 기존 테스트의 공격 실행 후 내부 관측 단언 일부는 실행 전 거부 단언으로 변경했으며 테스트 삭제 없음.
- 진행 중 검증: 수정 전 1 failed 증거 보존. generalized 9과제 테스트는 N33 admission 추가 후 10 passed 확인했으나 이후 추가 변경이 있으므로 최종 증거 아님. 일반 attribute assignment 의미 보존 테스트 red→green 확인.
- 중간 selfcheck.txt/json는 아직 실패가 남은 과거 출력이며 최종 증거 아님(LNG sentinel, TST N7 예상 코드 후속 수정됨). 최종 전체 테스트/selfcheck/18 references 재실행 필요.

남은 일: focused 결과 확인 → 추가 경계 회귀(예약 binding/import alias, sealed source 변경, audit swallowed attempt, 정확한 fd 별칭) → lint/format → 전체 pytest/selfcheck 최종 증거 갱신 → manifest/명세 §0.1/구현 §10/README 갱신 → commit, main fetch/rebase, pre-push 게이트를 거쳐 origin/main push → PMS 4.2 설명(doing 유지) 갱신. 모델/서버/OS sandbox/holdout/정답 봉인 미실행.

남은 위협: CPython audit hook/AST는 범용 sandbox 보증이 아니다. 모든 후보 interpreter 출력은 본래 비신뢰이며 허용 capability subset의 가드에 조건부로만 채택한다. 파일/네트워크/동일 UID 프로세스 접근 OS 격리는 범위 밖. native/capability API의 간접 우회, 새 Python AST/버전, 적응 공격 전수는 미검증. OS deny 계획을 문서에 남겨야 함.

후속 CPU 검증: pytest.txt의 152 passed in 79.14s, selfcheck.txt의 problems=0, 참조해18/18 PASS, 음성366/366 기대사유 거부. 위의 중간 실패/남은 검증 항목은 당시 기록이며 최신 CPU 상태는 summary.json과 구현 §10이 대체한다. commit/push/PMS 기록은 최종 응답과 원격 commit을 확인한다.
