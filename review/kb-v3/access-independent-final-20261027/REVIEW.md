# KB v3 access delta — independent final review

**판정: 검토한 교정 delta에서 차단 우회 반례를 찾지 못했다.** 확인된 Run 소유 PostgreSQL에서 좁은 delta·stale-head·파생 접근 검사를 실행했고 모두 통과했다. 이는 이 commit의 범위 검토 결과이며 production 배포·브라우저·실제 L2 실행 검증은 아니다.

## 검토 대상

- 기준 변경: `ec8e9658735f29da5311fd37e0db665b2d863654^` → `ec8e9658735f29da5311fd37e0db665b2d863654`
- 검토 checkout HEAD: `52e6c12cb140f545bee13d788650f6afbc125415`
- 대상 product source는 HEAD와 ec8 사이에서 일치함: `kb_access.py`, `kb_library.py`, `kb_retrieval.py`, 두 KB API 모듈, `collector/scope_merge.py`의 diff는 비어 있음. 이후 checkout 변경은 review/browser 기록이다.
- 검토는 위 교정 delta와 지정된 좁은 반례만 다뤘다. 전체 401 baseline, 기존 E5 151 및 adjacent 74, 브라우저 검사는 반복하지 않았다.

## 결과

| 구분 | 확인 결과 | 판정 |
|---|---|---|
| 소유자 UI | owner context는 tenant의 global + work_only를 함께 조회하고 hidden/다른 tenant는 공통 조건에서 제외한다. 기본 단일 도서관 경로다. | 통과 |
| 개인 에이전트 | global visibility만 허용한다. | 통과 |
| 회사 에이전트 | 신뢰된 work pool context가 명시적 `pool=work` 또는 legacy `scope=work`만 허용한다. access 행이 없는 미분류 문서는 work agent에 열리지 않는다. global 출처도 명시적 engine grant가 없으면 열리지 않는다. | 통과 |
| grant / revoke | 기존 access 행에서는 pool만 바꾸며, legacy work scope의 revoke는 scope만 library 값으로 정규화한다. 기존 origin_scope/visibility는 유지된다. access 행이 없으면 안전한 work/work_only 기본 행을 만든다. | 통과 |
| 파생 데이터 | 문서·passage·entity·추출 alias·assertion·relation 필터가 tenant/hidden/source 조건을 공유한다. alias는 허용 원문 passage 출현 또는 curated source 조건을 요구한다. canonical/graph 검색은 해당 접근 필터를 사용한다. | 코드 추적 + 좁은 실증 통과 |
| scope_merge | 고정 0075 대신 release contract의 schema head와 DB 단일 head 목록을 일치시킨다. control row lock, active generation/revision, 모든 live generation active 확인, 구세대 lease/process-group drain 확인을 유지한다. | 코드 추적 + 현재 0076 및 stale 0075 테스트 통과 |
| 추출/L2 pool 경계 | merge는 source/document access pool을 좁은 쪽으로 합치고 legacy scope를 정규화한다. L2 CLI 자체는 실행하지 않았다. | 코드 검토; L2 미검증 |

## 실행 증거

marker `/tmp/ttakkari-run-test-92353-de5584d5/run-test-cluster.marker`의 run_id가 `3e8620a1-b4c8-4479-a97d-686b1f605acc`, port가 `61291`이었다. 설정 DB의 안전하게 표시한 식별자는 `postgresql+asyncpg://ttk_test@127.0.0.1:61291/ttakkari_test`로 일치했다. 동일 prefix의 다른 marker는 다른 run ID와 port였다. 비밀번호/DSN 원문은 보고서에 기록하지 않았다.

- `unit_tests/test_kb_access.py`: 8 passed.
- 전용 delta + scope merge unsafe-runtime 파라미터 + access unit: 19 passed. 포함된 케이스는 grant/revoke 후 list/search/detail/evidence, personal+work 파생 엔터티/assertion/relation, owner 단일 도서관, 현재 head 운영, 0075 거부다.
- alias/canonical source evidence와 personal graph/alias 격리 반례 2건: 2 passed.
- SQL 조건 순수 확인: owner/personal/work 컨텍스트와 document/entity/alias/assertion/relation 모두 tenant + hidden + explicit work pool OR legacy work scope 조건 확인. work는 미분류 문서 제외.
- 총 출력의 명령·stdout/stderr·exit code 및 checkout SHA: [execution.log](execution.log).

## 미검증 / 범위

- 실제 production 요청, 브라우저 재검사, 배포 상태는 확인하지 않았다.
- 실제 L2 CLI 실행은 알려진 중첩 Codex 샌드박스 EPERM 제약 때문에 재시도하지 않았다. 따라서 L2 실실행 결과는 미검증이다.
- DB 실증은 위 marker로 소유권이 확인된 Run DB에 한한다. 다른 DB나 production 상태를 의미하지 않는다.
- 이 결과는 ec8 교정 delta에만 귀속한다. 소스/main/push/deploy는 변경하지 않았다.
