# KB v3 독립 검증 F1/F2 교정

기준은 `ca23a4232aa83afeda43dcaf2e3e48b030810d77`이다. 기존 교정 작업 Run
`fbd5bb97-ba18-4fb7-8dd5-3f4e5301ca37`은 모델 코드 실행 전 내부 테스트 DB 초기화에서
`initdb` exit 1로 실패했다. MCP `ops_get_issue(LOG-238)`의 실제 배포 revision은
`600eb8432f7e1a3e64a9f3a07f175cd183fb90eb`이며 상세 stderr는 제공되지 않았다.
이 실패의 원인을 앞선 Codex 명령 샌드박스의 SysV shmget 거부와 같다고 단정하지 않는다.
이후 같은 initdb를 재시도하거나 다른 엔진으로 우회하지 않고 리드가 무DB 교정을 수행했다.

## 원인과 변경

- F1: 두 백필 경로가 공통으로 사용하는 `kb_access_backfill.classify`가
  `research:personal:` 문서 키를 명시적인 Collector 회사 출처보다 먼저 골랐다.
  research 키 또는 Collector 출처의 work 신호를 먼저 확인한다. 개인 키와 운영자 지정
  개인 접두사는 회사 출처를 이기지 않는다. 정상 개인 출처 및 미상 격리 정책은 유지한다.
  이 충돌 조합의 실제 운영 데이터 유입 여부는 확인하지 않았다.
- F2: 충돌 중복 방지가 open 쌍만 읽어 양쪽 유효로 해결된 동일 주장 쌍을 다시 열었다.
  같은 불변 주장 ID 쌍에 대한 수동 `resolved_both_valid` 결정도 중복 방지에 포함한다.
  새로운 주장 ID 및 `system:revision_supersession`으로 자동 종료된 쌍은 다시 탐지할 수 있다.
  현재 리비전 선택에 대한 낡은 rev_no 설명도 관측 원장 기준으로 바로잡았다.

## 실제 실행 증거와 한계

RED는 수정 전 실제 동작에서 `4 failed, 12 passed in 0.21s`, pytest exit 1이다.
F1에서 회사 출처가 개인으로 반환된 2건, F2에서 해결된 같은 쌍이 다시 생성된 2건이다.
원문은 [red.log](../../review/kb-v3/f1-f2-correction/red.log)에 보존했다.

GREEN 명령은 `env -u TTAKKARI_TEST_DB .venv/bin/pytest -q
unit_tests/test_kb_v3_provenance_policy.py unit_tests/test_kb_v3_conflict_redetection.py
unit_tests/test_kb_access.py unit_tests/test_kb_backfill_pure.py`이며
`27 passed in 0.45s`, pytest exit 0이다.
원문은 [green.log](../../review/kb-v3/f1-f2-correction/green.log)에 보존했다.

분류 10건, 충돌 재탐지 6건, 기존 무DB 접근/백필 11건이다. 충돌 시험은 실제 중복조회
SQL을 메모리 SQLite의 최소 테이블에서 실행하고, 주장·근거·현재성 DB 읽기는 I/O 더블을
사용하는 단위 시험이다. 정상 새 주장 탐지와 자동 종료 후 재탐지도 확인한다.
PostgreSQL 저장·조인·트랜잭션·실제 해결 API 왕복을 검증한 것은 아니다.

이 변경으로 DB 통합 검증, E5 검색 품질, 사용자 E2E 10개, 프론트 연동, 운영 배포가
통과했다는 주장은 하지 않는다. 다음 단계는 이 교정 커밋의 독립 검토와 지원되는 실행
환경에서 미검증 범위를 확인하는 것이다. 기존 통합 보고의 350/351개 숫자는 독립 확인하지
않았으며 새 증거로 사용하지 않았다. main 병합, push, 배포, 운영 백필, PMS done은 하지 않았다.
