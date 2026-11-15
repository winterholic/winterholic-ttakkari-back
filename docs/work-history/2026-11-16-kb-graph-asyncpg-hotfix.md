# 지식도서관 그래프 asyncpg bind 한도 hotfix

기준 브랜치 `origin/main` / `5f260651c722036e71ae413d33012af42dfbe629`에서 분리한
`hotfix/kb-graph-asyncpg-limit-20261116`의 로컬 커밋 `5172f31e42f475dc79f7817f24778e9d4c6df693`을 기록한다.
배포·push는 수행하지 않았다.

## 운영 관측

운영 조회는 `~/bin/ttk get`으로 수행했고 본문은 출력하지 않았다. Ops 도구 `ops_get_issue(LOG-376)`에서
API 이슈가 미해결이며, 최신 발생 릴리스가 `5f260651`, 경로가 `GET /api/assistant/kb/ontology/graph`,
오류가 asyncpg의 `the number of query arguments cannot exceed 32767`임을 확인했다.
해당 이슈는 조회 시점에 20회 기록되어 있었다.

| API | 이번 측정 | 결과 수/메타데이터 |
|---|---:|---|
| `GET /api/assistant/kb/summary` | 38.84초 | documents 7,701; revisions 7,947; passages 309,217; entities 41,346; assertions 1,651; relations 25; open conflicts 44,724 |
| `GET /api/assistant/kb/documents?limit=1` | 0.21초 | HTTP 200, total 7,701 |
| `GET /api/assistant/kb/documents/{id}` | 0.22초 | HTTP 200, 선택 문서 상세 |
| `GET /api/assistant/kb/search?q=python&limit=1` | 7.75초 | HTTP 200, 1건, `has_more=true` |
| `GET /api/assistant/kb/ontology/graph?limit=1` | 1.74초 | HTTP 500; Ops 로그에서 asyncpg bind 한도 오류 확인 |

이전 lead 관측은 summary 35초, documents 목록 0.19초/7,701건, search 8.51초/1건,
graph 1.14초/HTTP 500이다. 현재 측정에서도 목록·상세와 summary·검색의 지연 차이가 재현됐다.

## 원인 및 수정

`get_ontology_hubs`는 출처와 접근권한이 있는 문서 언급을 읽고 문서별 현재 revision을 확인한 뒤,
모든 후보 ID를 `KbEntity.id.in_(documents_by_entity)`에 넘겨 허브 후보를 가져온다. 이 ID 집합은
페이지 크기와 별개로 전체 후보를 포함한다. summary에서 관측한 접근 가능 엔터티 41,346개는 asyncpg
한도 32,767보다 크다. 운영 그래프 후보의 정확한 수는 별도 API로 노출되지 않으므로 이 수치와
엔드포인트 오류를 연결한 원인 판단은 **코드와 운영 수치에 근거한 추론**이다. 그래프의 다른 경로인
`current_revision_ids`도 전달 집합 전체를 `IN`에 펼치던 구조여서 함께 방어했다.

`uuid_id_batches`가 입력 UUID 전체를 10,000개 이하로 나눈다. `current_revision_ids`의 관측·revision
조회와 그래프 허브의 엔터티 조회가 모든 배치를 읽으며, 전체 후보를 버리지 않는다. 엔터티 조회의
tenant, active status, visibility, kind, domain 조건은 각 배치에 계속 적용된다. 전체 후보를 모은 뒤
기존 정렬, total 산출, offset/limit 페이지 처리를 수행하므로 정확한 count와 순서를 유지한다.

## 검증

- RED, 격리 PostgreSQL fixture에서 32,768 document ID를 기존 unbounded query에 넣었을 때
  `asyncpg.exceptions.InterfaceError: the number of query arguments cannot exceed 32767` 발생.
- GREEN, 동일 fixture에서 32,768 document ID의 `current_revision_ids` 및 32,768 hub entity ID 조회가
  10,000개 이하 배치로 완료됐고, 없는 ID는 결과에서 제외됨: `1 passed in 0.85s`.
- PostgreSQL 테스트 모듈: `tests/test_kb_v3_canonicals_postgres.py` → `2 passed in 0.92s`.
- 그래프 권한·현재성·페이지네이션, API graph/search/documents/detail/summary 계약의 선택 회귀:
  `5 passed in 2.84s`; 현재 revision supersede 계약: `1 passed in 0.09s`.
- Ruff lint: 변경 파일 3개 `All checks passed!`; `git diff --check` 통과.

실제 운영 DB를 직접 조회하지 않고 `ttk`와 Ops MCP만 사용했다. PostgreSQL 검증은 테스트 harness가 띄운
격리 임시 클러스터에서 실행했으며 운영 데이터에 쓰지 않았다.

## 남은 항목

- hotfix는 로컬 커밋에만 있다. 운영 graph 500은 이 커밋의 배포 전까지 해결됐다고 볼 수 없다.
- summary와 search는 느리다는 사실을 운영 API에서 재현했지만, 쿼리 계획이나 단계별 DB 시간을 볼
  수 없어 추가 병목의 원인은 확정하지 않았다. 이 두 API는 수정하지 않았다.
- `ruff format --check`는 두 기존 모듈의 수정 범위 밖 코드도 포맷 변경을 요구해 적용하지 않았다.
  확대 포맷은 하지 않았다.
