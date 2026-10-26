# 수집 플랫폼과 최신 main 통합

## 대상과 범위

- 작업 트리: `back-collection-main-20261026`, 브랜치 `integ/collection-main-20261026`.
- 입력: collection `3d53028b1d75c828db1be6102d2dca1e88ec3abb`, main `77c1238a2b8ade01c096dc74b4f4da3a52c89849`.
- 기존 collection 검증 트리와 review 증거는 보존한다. 이번 변경은 main과의 통합이며 C1–C7 전체 인수 완료가 아니다.

## 변경과 이유

- 기존 collection/research migration의 revision ID·부모·DDL을 보존하고, 두 head를 받는 no-op `0075_collection_research_merge`를 추가했다. generation contract는 해당 head를 목표로 하며 양쪽 기존 head와 중간 revision을 online 허용한다. 여러 DB head 거부는 유지한다.
- graph 검사에는 실제 Alembic upgrade와 같은 `implicit_base=True`를 사용한다. 최초 검사의 기본값은 sibling branch를 순회하지 않아 테스트 자체를 교정했다. 기존 실패 로그도 남겼다.
- migration inventory는 merge를 세대 배포, `0073_research_reuse_source`를 Research Lab 소유로 분류한다. 미지의 migration을 무조건 분류하는 fallback은 추가하지 않았다.
- `alembic/env.py`는 logging 설정 때 기존 application logger를 비활성화하지 않는다. 최신 main 배포 `2f8896ee-fc2e-4af9-8693-6ebc5fbf179e`의 LOG-245에서 transfer 진단 event 집합이 비어 실패했다. 별도 DB 없는 실제 env.py 실행으로 transfer logger의 disabled=False→True를 재현했다. 새 회귀는 migration 뒤 실제 diagnostics.emit의 event를 확인한다. 해당 배포 실패 전체가 해결됐다는 운영 재검증은 아직 없다.

## 실행 근거

원시 명령·출력은 `review/collection-main-20261026/`에 보존한다.

- `green-corrected.txt`: schema graph/admission/combined contract **19 passed**, exit 0.
- 양쪽 `*-upgrade.sql`/`*-upgrade.log`: 각 기존 branch head에서 head까지 실제 offline SQL 생성 exit 0. 빠진 branch의 CREATE와 최종 merge를 포함하고 이미 적용한 branch의 CREATE는 포함하지 않는 단언을 확인했다. DB 실행 증거는 아니다.
- `unit-regression.txt`: collection/collector/research/generation unit **240 passed, 4953 subtests passed**, exit 0. 이후 변경은 최종 단위 검사/receipt로 별도 귀속한다.
- `logging-red.txt` → `logging-green.txt`: logger 비활성화 회귀 1 failed를 확인한 뒤 관련 검사 **18 passed**, exit 0.
- `inventory-red.txt`: 연구 migration 누락과 merge 소유 오분류 **2 failed, 1 passed**. 모르는 migration은 unmatched를 유지한다.
- `main-regression.txt`: **20 failed, 91 passed, 1 skipped**. 19 실패는 새 worktree의 `.venv/bin/python` 부재, 1 실패는 inventory 분류 누락이었다. 테스트 단언/skip은 제거하지 않았다.
- 기존 `../back/.venv`를 worktree `.venv`로 연결한 뒤 `backup-regression-env-corrected.txt`: **28 passed, 1 skipped**, exit 0. stub 기반 배관 검사이며 운영 백업/복원 실행이 아니다.
- `inventory-green.txt`: 실제 트리 inventory와 분류/coverage 회귀 **89 passed**, exit 0.
- `final-unit-regression.txt` / `final-unit-receipt.json`: 최종 collection/collector/research/generation/logging **244 passed, 4953 subtests passed**, exit 0. 입력 파일 SHA-256이 실행 전후 동일함을 확인했다.
- 변경 Python 파일 ruff 검사 통과. source graph/SQL/단위 결과를 실DB 적용으로 승격하지 않는다.

## 미완료와 다음 실행

- 통합 브랜치에 커밋해 검토 가능한 상태로 보존한다. main push와 배포 예약은 원래 필수 인수와 배포 검사 결과를 대조해 이어간다.
- 기존 87db full 결과는 collection 제품 baseline 근거다. main 병합 뒤 제품 소스가 늘었으므로 이번 통합 전체의 full 통과로 재사용하지 않는다.
- 실제 격리 PostgreSQL migration/KB/live A–E, 실제 Qwen 응답·usage·desk 영향, 실제 서버 UI, 운영 배포와 이전 세대 drain 뒤 C2 dry-run/apply는 미완료다.
- Codex의 과거 initdb SysV shmget ENOSPC, AGY sandbox TCP EPERM, OpenCode 초기 generic 오류를 운영 전체의 장애로 일반화하지 않는다. 같은 조건의 실패를 근거 없이 재시도하지 않는다.
- 이번 Codex 명령 환경에서 `/usr/bin/ipcs -m` 읽기 전용 상태 조회도 `ipcs: sysctlbyname: Operation not permitted`(exit 1)였다. 시스템 공유메모리 상태 변화는 확인하지 못했다. 다른 명령/엔진으로 같은 대상을 우회 조회하거나 initdb를 재시도하지 않았다.
