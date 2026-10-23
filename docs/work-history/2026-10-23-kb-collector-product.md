# 지식 수집·도서관 제품 연결 — 2026-10-23

## 요청·작업 위치
사용자: "완성본배포까지 필요한 개발 다 진행해줘". 기존 단위 품질/독립 검증 조건의 면제는 아니다.
back-kb-complete / front-kb-complete, branch feat/kb-complete-20261023. 공유 back/의 dirty 변경은 건드리지 않았다.
처음 기준 back 38b5468, front cf87ec3. 동시 main 변경은 합치기 전에 fetch/rebase한다.

## 이번 구현
- 출처·정책 등록/변경, 주기 실행, 시작/정지/재시도, 항목·원문 export, owner 유용성 검토와 보류 자료의 명시적 적재 요청.
- 명시 등록한 managed sources만 소비하는 generation 작업. 일반 collector-consume flag는 계속 default off.
- KB 소비자가 collector 밖에 위치하며, 권리/해시/scope를 재검증하고 원문 pin과 DB/receipt를 묶는다. 전달 재실행은 outbox 단위 멱등이다.
- 개인/회사 목록·검색·상세·근거 접근 분리. 원장 불변 유지. 접근 철회는 별도 metadata.
- raw-v1의 기존 NFKC와 collector NFC를 구분. event-time 관측 기반 A→B→A 현재성.
- 선택 가능한 PG exact dense+lexical RRF, 재생성 가능한 벡터 투영, 로컬 E5 snapshot pin, 토큰 단위 전체 본문 처리.
- owner 리뷰+수집 당시/현재 train 권리 기반 group-heldout TF-IDF/logistic baseline. shadow-only, 사실성 미측정.
- read-only kb MCP 4개와 인증 세션 scope 검증.
- 모바일 수집/검토/품질 관리 화면, scope·검색 방식 선택. 운영 페이지 /knowledge-library.

## 실행 근거 (최종 집계 전)
- 기존 KB API 10 passed; 이후 수집·KB 회귀 묶음 149 passed. 후속 핵심 묶음 57 passed.
- 계약 검사 78 passed, 4302 deselected. 새 MCP catalog 누락 1건을 발견해 실제 목록과 분류표를 함께 보강한 뒤 통과.
- 프론트 전체 1347 passed, 10 skipped. 타입 검사·빌드·변경 파일 ESLint 및 ui-lint 통과(기존 baseline 138 유지).
- 실제 CPU E5: 한/영 교차 언어 3질의 모두 기대 문서 top1. 1024차원, 문서3+질의3 전체 2.517초. 작은 자체 smoke이며 품질 입증이 아니다.
- 실 DB/로컬 HTTP: source 등록→job→robots/fetch→원문 보관→KB import→검색→quote/hash→다른 scope 숨김, 정책 회수 시 검색/원문 export 차단을 확인.
- configured ttakkari_browser, API fixture UI: 320/390/1440에서 control overflow 0, horizontal scroll 0. 등록 폼·긴 제목/URL·보류 항목·품질 패널. 운영 사용자 데이터나 실제 휴대폰/IME 검증은 아니다. 브라우저 초기 favicon 404 외 앱 오류 0.
- 테스트 환경: 이 Run에 TTAKKARI_TEST_DB=ttakkari_test가 제공된다. 병렬 pytest 두 개가 같은 DB를 쓰며 run_events seq 충돌. 그 전체 회귀 결과는 무효 처리하고 중단했다(1138 passed, 14 errors; 14건은 당시 aiosqlite 미설치). 선언된 dev dependency aiosqlite==0.22.1을 설치했다. 이후 pytest는 반드시 직렬 실행한다.
- 별도 initdb 대안은 codex command 환경에서 rc1(/tmp/ttakkari-test-60292-1fc85c40/data); fixture가 stderr를 남기지 않아 원인은 미확인. 운영 PostgreSQL 실패나 host 불능으로 일반화하지 않는다. 같은 조건 재시도하지 않았다.

## 완료로 표시하면 안 되는 범위
- 전체 v2(E1a/E0.5/KB7/KB9+KB10 등) 품질·규모 검증과 독립 리뷰. 이번 운영 연결을 해당 단위 전부 완료로 대체하지 않는다.
- 공개 classifier·teacher/student·calibration·출처/언어별 frozen human holdout, 100만 규모 엔진 선택.
- collector blob/KB 원문의 만료 삭제·권한 tombstone 프로토콜, 304 observation, 완전한 reconciliation/receipt retry 운영.
- 운영 적용·readiness·인증된 운영 수집/검색은 배포 상태와 별도 확인한다. 예약 수락을 성공으로 쓰지 않는다.

## 인수 세션 검증 (2026-10-23)
- 새 oversized passage 회귀 테스트가 처음에는 `index_pending()`이 2를 반환해 실패했다. 50,000자 초과 입력을 명시적 실패로 처리하고, 해당 passage만 격리해 뒤의 정상 passage 색인을 계속하도록 고쳤다. 관련 KB·collector·agent·security-inventory와 배포 스키마 계약 묶음은 23 passed.
- 전체 백엔드 `PYTHONPATH=src .venv/bin/python -m pytest -q`: 4330 passed, 49 failed, 15 skipped, 2 xfailed, 5020 subtests passed (22m08s). 실패 목록은 게임/Google/KB 이전 마이그레이션, host GPU/process recovery, security inventory였다. 보안 inventory 실패는 새 보안 테스트 4개가 deployment inventory에 빠진 것이어서 `SECURITY_TEST_FILES`에 추가했고 inventory 포함 핵심 묶음 23 passed로 재실행했다. 전체 suite는 이 수정 후 재실행하지 않았다. 이전 마이그레이션 subprocess 실패의 stderr는 pytest가 노출하지 않아 원인은 미확인이다. Host GPU/process recovery 실패는 codex 시험 환경에서 관찰된 실패이며 호스트 운영 환경에 일반화하지 않는다.
- `ruff check .` 전체는 저장소 전반 104건으로 실패했다. 변경·신규 백엔드 Python 파일에 한정한 ruff는 통과했다.
- 프론트 전체: Vitest 1347 passed, 10 skipped; ESLint/UI lint 통과(위반 138건은 기존 기준선과 동일); TypeScript와 Vite build 통과. `pnpm`은 symlink node_modules를 workspace-hoist 디렉터리로 처리하며 실행을 거절했지만 동일 설치본의 바이너리를 직접 호출해 각 검사를 마쳤다.
- 브라우저 UI 확인은 headless 격리 프로필의 인증이 없어 `/login`으로 이동해 검증하지 못했다. 운영 DB 또는 인증된 운영 수집/검색은 검증하지 않았다.
