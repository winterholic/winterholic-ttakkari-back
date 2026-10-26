# KB v3 백엔드 계약 후속 교정

기준은 `kb-v3/api-20261025`의 `27ea2f44d24b84e0a5e57b05cacb626571376a76`이다.
원설계는 `back-kb-refactor-design/docs/design/kb-v3/knowledge-library-refactor.md`,
기존 교정 기록은 `2026-10-25-kb-v3-f1-f2-correction.md`, 프론트 관측은
`front-kb-v3/docs/work-history/2026-10-25-kb-v3-live-contract.md`를 참조했다.

## 원인과 변경

1. 접근 백필은 `rev_no DESC`로 출처를 고르고, 구조 백필의 `_latest_revision`은
   공통 `currency.current_revision`으로 관측 원장의 현재 출처를 골랐다. 같은 문서에서
   현재 rev1(work, event day10) 뒤에 오래된 rev2(personal, day1)가 도착하면 결과가 갈렸다.
   새 회귀 시험에서 접근 백필 personal, 구조 백필 work/work_only를 재현했다.
   정상 Collector는 scope별 문서 identity를 쓰므로 이 혼합 자료의 운영 발생은 확인하지 않았다.
   접근 백필도 문서 배치 → 공통 `current_revision_ids` → 출처 일괄 조회로 변경했다.
   현재성 SQL을 복제하지 않는다. 관측이 없으면 공통 함수의 rev_no 폴백을 그대로 쓴다.
   keyset·limit·배치 삽입·기존 접근 행 무변경·미상 work_only·work 출처 우선 정책을 유지한다.
2. 문서 상세의 충돌 요약은 공통 충돌 뷰와 별도로 구성되어 `resolved_by`와 `note`를
   생략했다. 두 필드만 추가했다. `kept_separate`의 시스템 리비전 대체 종결과 사용자 분리
   결정을 구분할 수 있으며 기존 id/kind/status/entity_name은 유지한다.
3. `conflicts_v3`와 실제 목록 쿼리는 이미 `entity_id`를 지원했지만 HTTP 라우트가 받지 않았다.
   라우트 끝에 선택 UUID 인자를 추가하고 전달한다. 기존 위치 인자의 순서는 유지한다.
   접근 판단과 상태·종류·전체 건수·페이지네이션은 기존 경로를 그대로 사용한다.

## 실제 검증

`test-driven-development`의 RED→GREEN, `systematic-debugging`의 호출 경로·업무 규칙·
환경 격리·반증 절차를 적용했다. 테스트 더블의 경계는 `dev-experts`/`dev-testing`으로 점검했다.

- RED: 신규 두 파일만 실행하여 `11 failed, 11 passed in 0.61s`, exit 1.
  실패는 현재 출처 불일치 2건, 상세 종결 메타데이터 누락 3건, HTTP 엔터티 필터/UUID 검증
  누락 6건이다. [원출력](../../review/kb-v3/backend-contract-followup/red.log.gz).
- 신규 GREEN: 같은 22건이 `22 passed in 0.54s`, exit 0.
  [원출력](../../review/kb-v3/backend-contract-followup/green-new.log).
- 최종 묶음: 기존 provenance_policy/conflict_redetection/access/backfill_pure 27건과 신규
  22건을 함께 실행하여 `49 passed in 0.63s`, exit 0.
  [명령·exit](../../review/kb-v3/backend-contract-followup/checks.json),
  [원출력](../../review/kb-v3/backend-contract-followup/green-final.log).
- 변경 소스 3개와 신규 시험 2개의 Ruff: `All checks passed!`, exit 0.
  [원출력](../../review/kb-v3/backend-contract-followup/ruff-final.log).
  첫 Ruff 실행의 시험 스타일 지적 2건은 고쳤고 [초기 출력](../../review/kb-v3/backend-contract-followup/ruff.log)도 보존했다.
- `git diff --check`: 출력 없음, exit 0.

접근 백필 시험은 메모리 SQLite의 최소 테이블과 DB I/O 더블을 사용한다. 공통 현재성 SQL과
새 출처 조회 SQL은 실제 실행하며, 수정 전 PostgreSQL LATERAL 문법만 의미가 같은 SQLite
상관 서브쿼리로 표현한다. 구조 백필에서 관련 없는 청킹·작업 등록 쓰기는 대체한다.
현재 work/늦은 과거 personal, 정상 personal, 리비전 없는 미상, 관측 없는 폴백,
limit 0/1/2/3/4/없음, 작은 배치 경계, 적용 재실행 멱등, 기존 hidden work 접근 행 및
문서·출처·리비전·관측 행 보존을 확인한다. 실제 원본 파일 불변 트리거 검증은 아니다.

HTTP 시험은 FastAPI 라우터를 인프로세스 ASGI transport로 호출한다. 실제 목록 쿼리의
필터 SQL을 SQLite에서 실행하고 모델 로딩만 더블로 대체한다. 실제 접근 함수와 응답 뷰를
실행하여 엔터티별 목록·미존재 ID·페이지 경계·work 격리·다른 tenant·접근 불가 후보·
종류 필터·잘못된 UUID 422를 확인한다. 문서 상세 시험은 조회 결과 더블로 응답 형성만 검증한다.

## 범위와 미검증

Codex 명령 샌드박스의 알려진 initdb SysV shmget 거부를 재시도하지 않았다. `tests/`,
collect-only, PostgreSQL/Docker, DB 포트, 서버 기동, 다른 엔진을 통한 우회를 실행하지 않았다.
실제 PostgreSQL 저장·조인·트랜잭션·트리거, 실서버 왕복, 운영 자료에서의 조건 발생,
사용자 E2E 10개·품질·운영 백필·배포는 이 교정의 증거가 아니다.

프론트가 기록한 영구 held 재시도, 별칭 201, degraded 코드 문구, 파일 수 상한 400,
추출 노이즈, 업로드 총량은 이 세 수정의 범위에 추가하지 않았다. 기존 다른 review 자료는
수정/삭제하지 않았다. main 병합·push·배포·PMS done은 실행하지 않았다.
