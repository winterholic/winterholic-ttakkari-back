# Research Lab v2 최종 UI 인수 보충 검증

검증일: 2026-10-26 (Asia/Seoul)

## 판정

이번 새 증거로 교정 대상 UI 경로는 **PASS**다. 브라우저에서 원본 설정 재사용, 연구자 해석 저장, 최선 결과 채택을 실행하고 reload 뒤 화면을 다시 확인했다. k=5와 k=7 실행 결과는 각각 그 cutoff로 측정됐고, 원본 k=5 구성은 변하지 않았다. 이 bounded fixture의 수치로 성능 향상을 주장하지 않는다.

이는 UI 인수 보충 결과다. main 통합·배포·Metal·실제 TTS 재합성을 검증하지 않았다.

## 대상과 고정된 환경

- Backend worktree: back-research-v2, HEAD ac33d5c5c32e1b6f355ea1b1b1e7e01b596a5497 (사용자 지정, 제품 커밋 1bd67e8 포함).
- Frontend worktree: front-research-v2, HEAD 5a2650fde8d5c46f50c9f08ce67f14917f14da98.
- 이 검증은 위 두 worktree만 사용했다. 제품 파일은 변경하지 않았다.
- backend: 해당 worktree .venv/bin/python -m uvicorn으로 outbox의 얇은 wrapper를 실행했다. wrapper는 get_settings(), init_engine(settings.database_url), 실제 app을 사용하며 --lifespan off로 시작했다. port 56998.
- frontend: TTAKKARI_NO_ENV_FILE=1 VITE_API_BASE=http://127.0.0.1:56998 .../node_modules/.bin/vite --host 127.0.0.1 --port 56999 --strictPort. Backend를 띄우기 전에 정확한 CORS origin http://127.0.0.1:56999를 주입했다. 기존 listener는 건드리지 않았다.
- 별도 ORM evidence helper도 .venv/bin/python을 사용했다. TTAKKARI_DATABASE_URL="$TTAKKARI_TEST_DB"로 표준 주입 DB를 연결하고 현재 DB/user/address가 ttakkari_test / ttk_test / 127.0.0.1인 것을 단언했다. DSN과 비밀번호는 출력하거나 파일에 기록하지 않았다. 앱 migration head 0073과 vector, pg_trgm 준비는 이 Run DB에만 적용했다.
- Backend health, Vite 응답 및 정확한 origin의 CORS preflight는 모두 HTTP 200이었다.
- 브라우저는 첫 navigate에서 정상 실행됐다. 첫 화면에서 비밀번호 입력과 로그인 버튼을 확인하고 일반 UI 로그인으로 /research에 진입했다. Mac Studio 응답 중 문구를 로딩이나 중단 근거로 사용하지 않았다. 이후 refresh와 보호된 Research GET 요청이 200이었다.
- Vite의 /sw.js는 HTML을 돌려 service worker MIME 오류를 냈다. 로그인 후 기능에는 차단을 일으키지 않았다. 인증 전 refresh 401은 로그인 전 요청이고, browser에서 별도 API origin을 직접 열어 얻은 401도 Research UI의 인증 흐름과 분리했다.

## 새로 실행한 실제 UI 여정

테스트 연구 RAG 검색 cutoff 재사용 UI 검증에 ko_tech_docs_v1 32문서 bounded CPU 검색 실행을 준비했다. 원본 실험은 retrieval_eval, BM25, k=5, recall_at_5이며 일반 API로 생성·실행했다. UI 인수 대상은 그 원본에서 후속 실험을 만드는 흐름이었다.

### 원본 k=5 재사용

결과 분석 화면에서 원본 설정을 선택하고 이 설정으로 후속 실험 → 설계실 → 만들고 바로 실행을 눌렀다. 설계실에서 값을 고치지 않았다. UI의 생성 POST는 201이었고 후속 설정은 {k:5, preset_id:ko_tech_docs_v1, retriever_mode:bm25}로 저장됐다. top_k나 다른 default가 추가되지 않았고, 설정 hash는 원본과 후속 모두 645f5195…d4e5로 같았다. 원본 설정 재조회에서도 파라미터와 hash가 그대로였다. Run은 local_measured / succeeded, 측정값 recall_at_5=0.9167이었다.

### 사용자가 k=7로 명시 변경

같은 원본에서 후속 실험 화면을 다시 열고 UI의 k 입력만 7로 바꿔 저장·실행했다. 요청된 주요 지표는 recall_at_7; 새 구성은 {k:7, preset_id:ko_tech_docs_v1, retriever_mode:bm25}이며 top_k가 없다. Run은 local_measured / succeeded; 실제 저장된 metric은 recall_at_7=0.9479와 MRR/NDCG@7이었다. k=5와 k=7은 서로 다른 cutoff이므로 두 수치를 성능 개선으로 비교하지 않는다.

### 성공·실패 해석 노트

성공 노트를 실제 화면에서 입력하고 성공 Run에 연결해 저장했다. 실패 노트도 실패 Run의 실패 원인 해석 남기기에서 입력하고 저장했다. 두 POST /nodes가 각각 201, 이후 resume GET이 200이었다. reload 후 제목·본문·experiment/run 조건과 연구자 해석 (미검증) 표지를 모두 화면에서 확인했다.

Frontend revision의 src/pages/research/views/ResultAnalysisView.tsx는 노드 payload에 kind, title, summary, conditions, details를 만들며 epistemic_status를 보내지 않는다. Browser network 도구는 요청 body를 노출하지 않지만 실제 POST는 201이었고 DB ORM과 resume 재조회에서 서버 저장값 unverified가 확인됐다.

실패 Run은 명시적 synthetic test_fixture 행이다. fixture_execution_failure와 메시지는 retrieval adapter를 실행하지 않았고 측정 결과도 없다고 분명히 쓴다. 화면은 이 행을 실패 목록에 보존하고 평균에서 제외했다. 실패 해석도 실제 adapter 실패가 아니라고 기록했다. 따라서 이 부분은 실패 표시/기록 왕복 증거이며 실제 실행 실패 주입 증거가 아니다.

### 최선 결과 채택

분석 화면에서 Baseline 구성 및 성공한 k=5 Run을 선택했다. 채택 목표, recall_at_5, bounded fixture 조건과 단일 반복의 불확실성, 성능 비교가 아니라는 이유를 채워 이 설정 채택 → 기록을 실행했다. /best-results POST는 201, 뒤이은 resume GET은 200이었다. reload 후 연구 기록 화면에서 같은 설정 이름, 목표, recall_at_5, 제약, 근거가 다시 보였다. DB에는 해당 BestResult가 experiment_id, configuration_id, run_id를 연결해 저장됐다.

이 연구의 ResearchArtifact 연결 행은 0개다. 기록·결과물 화면도 연결된 결과물이 없습니다라고 보였다. 따라서 파일 metadata/content/sha를 조회하거나 artifact 링크를 여는 경로는 해당 대상 파일이 없어 적용할 수 없었다. 가짜 채택 artifact는 만들지 않았다.

## 원문 UX 10문 판정

기존 유효 증거는 인용한 C 검토 문서의 범위만 유지했다. API/PG 회귀, migration/FK/source ID 계약, runner 등록 실패 및 UX8 rerun을 반복하지 않았다.

| # | UX 질문 | 판정 | 근거 |
|---|---|---|---|
| 1 | 시도 가능한 방법은 무엇인가? | 부분 | 기존 catalog 근거 범위만 유지. 이번 UI 검증은 방법 검색을 반복하지 않았다. |
| 2 | 현재 실행 가능한 방법은 무엇인가? | 부분 | 이전 adapter 지원 evidence만 유지. 새 catalog/adapter 탐색은 하지 않았다. |
| 3 | 이전 시도는 무엇인가? | PASS | 기록 화면에서 원본·후속 k=5·후속 k=7과 성공/실패 출처를 확인했다. 기존 실패 기록도 남았다. |
| 4 | 적용한 설정은 무엇인가? | PASS | 후속 k=5는 source 값 그대로, 명시 변경 k=7은 새 cutoff로 저장·실행됐다. |
| 5 | 설정 조합은 무엇인가? | 부분 | 이 fixture에서는 조합 실험을 수행하지 않았다. 기존 증거 범위 이상을 주장하지 않는다. |
| 6 | 개선/악화 폭은 얼마인가? | 부분 | 서로 다른 cutoff의 단일 실행뿐이다. 동조건 반복·개선 결론을 검증하지 않았다. |
| 7 | 성공 이유·실패 원인은 무엇인가? | 부분 | 성공 및 실패 해석을 UI에서 저장·reload했다. 실패 행은 synthetic 표시 fixture라 실제 실행 실패 원인은 미검증이다. |
| 8 | 실험을 다시 실행할 수 있는가? | PASS (기존 증거) | 기존 c-reuse-review-763dc03-e1158f0.md에서 실패 Run rerun PASS. 이번에는 중복하지 않았다. |
| 9 | 현재 최고 결과물은 무엇인가? | 부분 | BestResult 설정/Run/목표/지표/조건/근거는 채택해 reload했다. 파일 artifact는 연결되지 않았고 이 연구에 결과물 파일도 없다. |
| 10 | 이전 연구를 어떻게 재사용하는가? | PASS | source UI에서 k=5 무수정 후속 실행, k=7 명시 변경, source 연결 및 reload를 확인했다. 짧은 출처 배지/monitor 잔여는 낮은 표시 개선으로 유지했다. |

## 원시 근거와 명령 결과

비밀을 제외한 상세 DB ORM snapshot, 생성 payload/metric 요약, source와 k=5/k=7 configuration/run evidence는 같은 디렉터리의 JSON 파일에 있다. research-lab-db-evidence.py는 RunDB identity assertion 후 study-scoped ORM 데이터를 썼다. 첫 helper 호출은 TTAKKARI_TEST_DB를 TTAKKARI_DATABASE_URL로 전달하지 않아 fallback DB user 인증에서 실패했다. 표준 주입값을 연결한 재실행은 성공했다.

성공 명령:

    TTAKKARI_NO_ENV_FILE=1 TTAKKARI_HOME=<Run outbox>/testhome TTAKKARI_DATABASE_URL="$TTAKKARI_TEST_DB" .venv/bin/python <Run outbox>/research_lab_db_evidence.py

출력:

    {"identity":{"database":"ttakkari_test","user":"ttk_test","server_address":"127.0.0.1","password_or_dsn_emitted":false},"experiments":3,"configurations":3,"runs":4,"nodes":[["interpretation","k=5 bounded CPU 검색 실행의 관측","unverified"],["interpretation","허용된 실패 표시 fixture의 범위","unverified"],["decision","최적 결과물 채택: Baseline","validated"]],"best_results":1,"research_artifacts":0}

Browser network receipt에는 각 노드 저장 POST 201, 최선 결과 저장 POST 201, 이후 연구 resume GET 200이 기록됐다. 데스크톱 1440px와 모바일 390px 화면 모두 새로고침 후 확인했다. 페이지 스크린샷은 요청대로 자동 outbox에 저장됐고 JSON의 screenshots에 파일명이 기록돼 있다.

## 인수 범위 밖

- 실제 adapter 실패 주입은 수행하지 않았다. 실패행 표시/저장은 합성 fixture만 사용했다.
- WAV/JSON artifact가 연결된 Run은 없어서 기존 등록 서비스, 파일 content/hash와 UI artifact 링크 관계는 검증되지 않았다. B 시나리오의 실제 TTS 재합성은 기존 b670 evidence를 재사용했고 반복하지 않았다.
- UX8 기존 PASS, prior API/PG·migration/source-boundary·runner-registration-failure evidence는 반복하지 않았다.
- main 통합, push, deploy, Metal 경로는 수행하지 않았다.
