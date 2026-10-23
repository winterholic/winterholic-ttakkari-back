# 지식 도서관 검색·열람 최소 제품화 (ADR 0063, 2026-10-23)

## 요청과 범위
- 사용자 지시: "하다 만 작업 확인해서 이어서 프론트랑 백 배포까지 해봐, 지식도서관 페이지는 만들어지지도 않았네". 이 명시 지시로 ADR 0063의 '제품검색 미승인' 표기를 해제했다. v2 KB7 품질 gate(사람 확인 holdout·검정력·coverage 보정), KB-E1b, E0.5는 여전히 미완료다. 검색 품질을 입증했다는 뜻이 아니다.
- 하다 만 단계: main에는 KB1+KB2 원장(0055)과 KB-E0 하네스(2522d78)만 있고, 수집 CLI(`kb-ingest`, `kb-verify`) 외에 사용자용 API·MCP·UI가 없었다. v2 §9.2에서 이번에 구현한 것은 KB7·KB8·KB12 중 현재 원장으로 가능한 최소 범위다. collector 플랫폼, 2층 정본(claim), 엔진 교체, MCP 도구는 하지 않았다.
- 작업 위치: 공유 checkout의 다른 세션 미커밋 변경을 건드리지 않으려고 `origin/main`(d209386) 기준 worktree `back-kb-library`·`front-kb-library`(브랜치 feat/kb-library-20261023)에서 작업했다.

## 구현
- 백엔드 `src/ttakkari/kb_library.py`, `api/kb_library.py`, 라우트 `/api/assistant/kb/*`(summary, documents 목록·상세·등록, search, passages/{id}/evidence). 소유자 인증(`require_user`), `tests/test_route_inventory.py`에 6개 등록.
- 검색은 `context/search.py`의 기존 PostgreSQL 수단을 `kb_passages`에만 적용한다. 새 엔진·마이그레이션·색인 없음. 기본은 현재 리비전(문서별 최대 rev_no)만, `current_only=false`면 이전 버전 포함. 발췌와 강조 구간은 코드포인트 기준이다.
- 등록은 `ingest_source`(멱등·불변)와 `segment_revision`을 그대로 호출한다. source_key 생략 시 본문 해시 기반 키.
- 근거 위치: 리비전 본문 오프셋, 줄 번호, 앞뒤 문맥 200자, 구간 sha256, 저장 본문 대조 결과(`hash_verified`, 명제 지지 확인 아님).
- **검증 중 발견한 결함**: 기존 `ttakkari kb-ingest` CLI는 리비전만 만들고 패시지로 나누지 않아, CLI로 수집한 문서는 검색되지 않았다(격리 DB에서 passages=0 관측). `kb-ingest`가 수집 직후 `segment_revision`을 호출하도록 고치고, 이미 패시지 없이 쌓인 리비전을 나누는 `ttakkari kb-segment`(멱등)를 추가했다. 운영 DB에 CLI로 수집한 문서가 있다면 배포 뒤 `kb-segment`를 한 번 실행해야 검색된다(운영 DB 접근은 이 Run에서 하지 않아 현황 미확인).
- 프론트 `/knowledge-library`(`pages/KnowledgeLibraryPage.tsx`, `features/kb/*`): 검색·문서·등록 3탭, 상태는 URL(q, view, dq, doc, rev). 빈·로딩·오류(다시 시도)·다음 쪽 오류 상태, 640px 이하 한 열(문서는 목록→상세 전환), 1024px 이상 2단. 부가 기능 목록(`FEATURES`)에 추가해 데스크톱 모달과 모바일 더보기에서 진입한다. 마크다운 링크 분류(`linkTarget.ts`)에도 루트 추가.
- 개인 맥락 도서관(`/context-library`, `/api/assistant/knowledge`)과 경로·테이블·검색 풀을 섞지 않았다.

## 알려진 한계
- kb 행에 scope 열이 없어 개인/회사 구분 없이 소유자 인증만으로 보인다(v2 G8). scope 열 도입 시 검색·목록 필터를 같이 넣어야 한다.
- 검색은 단어 일치 기반이며 `to_tsvector`를 매 질의 계산한다(색인 없음). 패시지 수가 커지면 느려지므로 규모 gate 전에 색인 결정이 필요하다. 의미 검색·재순위·abstain·coverage는 없다.
- URL 수집은 collector(KB5) 소관이라 UI는 텍스트·.txt/.md 파일(브라우저에서 읽어 본문으로 전송)만 받는다.
- 패시지 분할은 빈 줄 기준 단순 분리기(heading 없음)다.

## 검증
검증 결과와 배포 기록은 아래에 이어 적는다.

### 실행 결과 (worktree back-kb-library, origin/main d209386 기준)
- 신규 시험 `tests/test_kb_library_api.py` 10개(인증 401, 멱등 등록·버전, 입력 거부, 목록 페이지네이션·LIKE 메타문자, 상세·리비전, 검색 현재/이전 버전·강조·페이지네이션·불용어, 근거 위치·해시, 패시지 없는 리비전 백필, CLI 수집 후 검색) 통과. `tests/test_route_inventory.py`·kb 기존 시험·`unit_tests/kb_eval` 219개 통과.
- 전체 `PYTHONPATH=src .venv/bin/python -m pytest -q`: 최초 `10 failed, 4292 passed, 14 errors`. 이 중 `unit_tests/kb_eval/test_design_status.py` 1건은 ADR 상태 문구를 바꾼 내 변경 때문이라 해제 사실과 품질 gate 미통과를 함께 요구하는 시험으로 고쳤다(재실행 통과). 나머지 9 failed·14 errors(마이그레이션 시험 5개 파일, delivery)는 공유 `.venv`(다른 체크아웃에 editable 설치)에 `aiosqlite`가 없어 생긴 환경 문제로, origin/main 그대로의 별도 worktree에서도 같은 9 failed·14 errors가 재현됐다.
- ruff: 변경·신규 파일 `All checks passed!`. 전체 `ruff check src`에는 이번 변경과 무관한 기존 4건(artcraft·deploy_gate·security/guard)이 있다.
- 마이그레이션(alembic head)을 적용한 격리 PostgreSQL 클러스터에서 `kb-ingest`로 샘플 수집 → API 검색 → 근거 위치 조회를 확인했다(수정 전에는 passages 0이라 검색 0건, 수정 후 2건·hash_verified true). **운영 DB의 실제 문서로 한 검증이 아니다.** 운영 DB 접근에는 비밀(.env)이 필요해 이 Run에서 하지 않았다.
