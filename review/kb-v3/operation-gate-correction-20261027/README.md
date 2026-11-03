# Operation 성공 판정 교정 — 2026-10-27

## 기준과 원인

기준은 root d163a77a469ddea798d19e2a31a265cb077f38d1(src: dba3 operation + 접근 ec8 + main78c)다. ff7e의 두 번째 독립 검토가 같은 문서 entity에 붙은 무관 conflict 및 SQLite 손상 digest로 succeeded를 재현했다. 원문은 sibling operation-fixes/review/kb-v3/operation-independent-20261027/verification.md다. 원 구현자 metadata는 앞선 실패 범위로 남고 교정 후 승인으로 바꾸지 않았다.

계정 한도에 걸리기 전 lead가 같은 root 트리에 3개 미커밋 변경과 원시 로그를 남겼다. 이번 Run은 먼저 manage_work revision59/실제 git diff/독립 보고/로그를 대조하고 그 변경을 이어받았다. systematic-debugging의 추적은 registration→실제 추출/persistence→DB 집계→policy pair→conflict gate까지 이어진다. 원인은 pair 발견 뒤 `or (c.entity_id in entity_ids and policy_pair_found)`로 무관 conflict를 허용한 조건과 truthy digest/비어 있지 않은 slice만 검사한 근거 조건이다. SQLite 반례는 PG trigger 부재 환경에 한정되며 운영 데이터 손상을 관측한 결과가 아니다.

## 교정 범위

- 현재 30일/90일 revision의 활성/분쟁 중 L2 주장, positive polarity와 fact/requirement modality, refund_period/refund_period_days 술어와 정확한 30/90일 값을 요구한다. 정의 문장에 숫자가 포함된 것만으로는 통과하지 않는다.
- supports 근거를 주장과 같은 revision에 결속하고 유효 범위, UTF-8 quote SHA-256, 인용문 내 환불 기간을 검사한다.
- 동일 주체·술어·적용 조건인 현재 두 주장 ID를 정확히 참조하는 value_contradiction conflict만 통과시킨다. 같은 entity의 무관 conflict fallback을 제거했다.
- 반례 fixture는 실제 제품 등록/추출/저장을 통해 세 잡 모두 ready, 현재 L2 두 정책 주장과 근거가 생긴 것을 먼저 단언한 뒤 held/zero-L2/잘못된 근거/다른 revision/무관 conflict/잘못된 pair/정의문만 남는 상태를 만든다. partial을 허용해 잘못된 gate가 통과하는 시험을 없앴다. 모델과 indexing은 test adapter이므로 실 모델 성과가 아니다.
- 두 정책이 같은 조건에서 기간만 다르도록 공개 합성 본문을 맞췄고, L2 선별이 정의문만 선택하지 않도록 명시적 환불 의무 표현을 넣었다. 고정 source key는 그대로이며 재실행하면 원문 보존 규칙 아래 새 revision이 된다.
- kb_v3_verify의 plan에서 불필요한 OpenSSL prerequisite를 제거했다. fixed kind·DB 환경 전달·interpreter 선택·180초 runner와 30초 기본 관측은 유지한다.

## 실행 증거와 한계

이전 계정의 inherited 로그는 무손실 gzip+원문 SHA로 보존했다. red-behavior.log.gz는 실제 잘못된 succeeded 7건 및 OpenSSL 거부 1건을 잡은 8 failed/3 passed를 기록한다. 다른 RED/fixture 진단·중간 GREEN도 성공으로 바꾸지 않고 전부 보존했다. 당시 정확한 shell 명령이 보존되지 않은 로그는 inherited-log-manifest.json에 not retained로 명시했으며 명령을 재구성한 원본이라고 주장하지 않는다. green-final의 21 passed in 7.49s는 계정 교대 전 기록이다.

이번 Run: 소유 marker의 RunID a11cab9d/loopback56390/ttk_test를 확인한 PG에서 evidence bad digest/range 거부와 immutable source/revision/passage/evidence UPDATE 거부 2검사 **2 passed in 1.38s, exit 0**. PG의 모델 DDL/trigger 경계이며 실제 배포 migration·실 모델 검증은 아니다. resumed-checks.json과 postgres-evidence.log.gz에 원문/명령/해시가 있다. 초기 ruff 5건은 regex alias/import 정리 후 수정했다.

최종 현재 소스: test_kb_v3_verify_operation.py **21 passed in 6.55s, exit 0**; ruff **All checks passed!**. resumed-final-checks.json 및 해당 gzip 원출력, source-hashes.json을 보존한다. 이전 21/14 및 다른 74검사와 합산하지 않는다. git diff --check도 확인한다.

## 남은 실행

성공 gate 교정 범위만 저장한다. production/실 L2/PDF·상충 여정, detached runner 취소·프로세스 종료, 새 세대 identity/retirement 보호는 아직 미검증이다. 운영 host process group을 확인할 수 없는 Codex nested preexec/sysctl 실패는 같은 명령·다른 엔진/바이너리로 우회 재시도하지 않는다.

RuntimeLease와 RuntimeProcessGroup은 각각 Run FK를 가진 모델이므로 host operation에 가짜 Run을 만들거나 lease를 억지로 끼우지 않는다. generation.can_retire는 Run/_work/HTTP/research/TTS를 추적하지만 현재 host operation 소유권은 추적하지 않는다(코드 추론). 다음 문맥은 기존 JSON job/PID ownership·generation drain 계약을 함께 읽고 fixed kind의 active-generation write fence와 retirement 보호를 최소 범위로 구현·검증해야 한다. 이는 아직 채택/구현한 설계가 아니다.

main/push/deploy는 이 Run에서 하지 않았다. 이어서 generation 경계 교정→정확 SHA 독립 재검증→유효 기존 검사 재사용/영향 회귀→clean main push/full SHA generation 예약→운영 L2·백필·ID/hash 보존·원요구/PMS 대조로 진행한다.
