# Operation 교정 후보 통합과 인접 검증 — 2026-10-27

구현 후보는 `dba3bdfbc0e64ea191776dab9a8e977dfd84fa63`, 기준은 a873a2a다. root 04504e59에 정상 merge한 중간 후보는 `36abf6e1690e03a94a3ca7640f6ba040ba68ec67`이며 conflict는 없었다. 아직 독립 승인 또는 배포한 결과가 아니다.

구현자의 commands.json, RED/GREEN 원문을 직접 읽었다. 원 RED는 6 failed/5 passed 중 registry 단언 한 건 외 5건이 새 max_wait_seconds 인자 부재 TypeError다. 이것만으로 실제 held/잘못된 인용/무관한 충돌 반례가 검출됐다고 보지 않는다. 보존 GREEN은 14 passed in 2.38s다(전달 보고의 1.77s와 다른 실행이므로 합산하지 않음). 이 로그는 root 재시험 결과가 아니다. 원문 공백을 훼손하지 않도록 root의 두 로그는 gzip으로 옮겼고 원래 경로/보존 경로/원문 SHA/원 커밋의 대응은 implementation-log-storage.json에 있다. 구현자의 원 보고서는 원 커밋 시점 기록을 유지한다.

새 문맥 task_type=verification을 ff7e에 요청했다. Run `2e32eb46-34d9-4190-ab9a-d270db1ca178`의 대상은 dba3 고정이며 제품 수정은 하지 않는다. 실제 L2 상태·주장-근거·정책 쌍, fake 성공 시험의 검출력, generation/소유/취소/HEAVY runner 및 유한 관측을 재검증한다. root와 다른 DB/검증 범위로 진행한다. 이전 ff7e 문맥 61%여서 fresh_context로 시작했다.

root는 Run 소유 DB(56c418a7/loopback63578/ttk_test) marker를 확인하고 현재 PYTHONPATH=src에서 host_operations, collection_scope_operations, route_inventory, generation_coexistence 4파일을 직렬 실행했다. 결과는 **10 failed, 74 passed in 35.57s, exit 1**이다. commands/HEAD/원문 SHA는 adjacent.json, 원시 stdout/stderr는 adjacent.log.gz다. 성공한 기존 74건을 operation 최종 승인으로 확대하지 않는다.

실패 중 프로세스 실행에서 `SubprocessError: Exception occurred in preexec_fn`과 psutil의 `PermissionError: Operation not permitted (originated from sysctl() malloc 1/3)`가 직접 관측됐다. inline performance 실패는 응답에 `OSError:`만 나왔고, 다른 두 최종 상태/exit-code 단언 실패도 원출력 그대로 남겼다. 후자의 정확 원인은 확정하지 않는다. AST 비교는 runner의 _child_setup/_execute/_group_gone/_kill_group 및 inline.performance_baseline 구현이 부모 04504e59와 동일하다는 것을 확인했다. 변경되지 않은 경로라는 사실은 모든 실패의 원인을 확정하거나 운영 환경을 검증하는 증거가 아니다. 동일 sandbox 실패 재시도나 subprocess/OS 보호 우회는 하지 않았다.

실 L2 CLI·production host operation·실제 종료 프로세스 그룹·새 generation lease·운영 PDF/상충·백필은 미검증이다. known Codex nested CLI EPERM은 재시도하지 않았다. main/push/deploy도 하지 않았다. 다음은 ff7e 독립 결과 대조→필요 교정 및 좁은 실행 증거→root 통합/최종 회귀→clean main push/full SHA generation 배포 예약이다. 기존 원요구/PMS는 계속 active다.
