# 따까리 범용 지식 도서관(kb) 설계 v1

- 작성: 2026-10-20, 설계 세션(1~4단계 분석·설계만. 코드 변경 없음)
- 기준 코드: `winterholic-ttakkari/back` main `f1e4a7c`, 로컬 PostgreSQL `ttakkari`(읽기 전용 트랜잭션으로 조회)
- 표기: 코드에서 확인한 사실은 `경로:줄`. 측정하지 않은 수치·버전·인과는 `확인 필요`. 경로의 `back/`은 `~/development/projects/winterholic-ttakkari/back/`이다.

---

## 0. 1쪽 요약

### 핵심 결정

| # | 결정 | 근거 |
|---|---|---|
| D1 | **같은 PostgreSQL 17 인스턴스, 별도 `kb_*` 테이블군.** 개인 맥락 도서관(`ctx_*`, `knowledge_*`)과 테이블·검색 풀·MCP 도구를 분리한다. 원장 불변식(불변 행, 해시 검증 span, append-only 검토 이력)은 그대로 복제한다. | 오늘 live `recall`에서 "맥락 도서관" 질의에 마케팅 스킬의 "도서관 사서" 문단이 섞였다(2.3 C8). 세계 지식과 개인 맥락은 신뢰 기준·범위·규모·갱신 주기가 다르다. DB를 나누면 백업·트랜잭션이 둘이 되므로 인스턴스는 공유한다. |
| D2 | **원본 bytes는 내용 주소 파일 저장소, 정규화 텍스트와 메타데이터는 DB.** `~/.ttakkari/kb/blobs/sha256/ab/cd/<sha>.zst` | 수백만 건의 PDF·HTML·자막 원본을 DB에 넣으면 백업·VACUUM 부담이 커진다. 검색·span 검증에 필요한 정규화 텍스트만 DB에 둔다. 같은 sha는 한 번만 저장한다. |
| D3 | **패시지 우선, 주장(claim)은 나중.** 수집 즉시 패시지 단위로 검색 가능해야 하고, LLM 추출 주장은 정본 층을 점진적으로 덧붙인다. | 기존 원장은 unit 16,472건이 들어왔지만 evidence 0, assertion 0, extraction job 0이다(1.2). 추출이 선행 조건이면 도서관이 비어 있는 기간이 길어진다. |
| D4 | **한국어 형태소 분석 후 색인 + IDF 가중 어휘 검색이 1차 경로.** Kiwi로 조사·어미를 떼고, 후보는 PG FTS(OR) + GIN, 순위는 BM25로 다시 매긴다. 벡터는 평가로 패러프레이즈 누락이 확인된 뒤 켠다. | 기존 실패의 주원인이 토큰화와 가중치다. `aip가` 토큰은 2개 조각만 맞고 `aip`는 219개가 맞는다. 문장형 질의의 FTS AND 일치는 0건이다(2.3 C1~C3). 벡터를 먼저 붙여도 이 문제는 그대로 남는다. |
| D5 | **점수 합산 대신 순위 융합(RRF)과 소수 후보 재순위화.** 서로 다른 경로의 점수를 더하지 않는다. 교차 인코더 재순위화는 기능 플래그로 두고 지연과 이득을 측정한 뒤 켠다. | 기존 `recall`은 어휘 점수, 엔터티 +0.8, 회의 벡터 ×8 점수를 한 리스트에서 정렬한다(`back/src/ttakkari/context/store.py:216,227`, `meetings/vectors.py:233-237`). 척도가 달라 순위가 흔들린다. |
| D6 | **정본은 "개념·측면·적용조건" 묶음마다 하나. 경쟁 주장은 함께 보존한다.** 정본 선정은 결정적 점수식과 사유를 이벤트로 남긴다. | 사용자 원칙 1, 4. 버전 차이(예: PG 16과 17)는 충돌이 아니라 적용조건 분리로 처리해야 오탐 충돌이 줄어든다. |
| D7 | **AI 생성물은 근거가 될 수 없다.** NotebookLM 답변, LLM 요약, 에이전트 메모는 `ai_generated`(T5) 출처로 저장할 수 있지만 정본 승격의 근거로 세지 않는다. 승격은 외부 원문 span에 근거해야 한다. | 사용자 원칙 4단계. 기존 원장도 "candidate를 확정으로 오인"을 주요 실패로 둔다(`back/docs/knowledge/architecture.md` 9절). |
| D8 | **에이전트가 스스로 고르는 도구(`kb_search`, `kb_get`, `kb_ingest`, `kb_ingest_status` + 추출·검토 3개).** 모든 Run에 자동 주입하지 않고, `recall`에도 섞지 않는다. 검색 결과에 `coverage`(none/weak/strong)를 붙여 외부 검색으로 넘어갈지 에이전트가 판단하게 한다. | 사용자 원칙 7. ADR 0007의 "매 Run 자동 주입은 토큰만 쓴다"는 판단을 유지한다. |
| D9 | **질의 변형은 호출 중인 에이전트가 만든다.** `kb_search`는 `queries[]`(한국어·영어 등 최대 4개)를 받는다. 서버는 별도 LLM을 부르지 않는다. | 사용자 원칙 6(특정 LLM 비종속). 한영 교차 검색을 추가 모델 없이 해결한다. 기존 추출도 "호출 모델이 bounded batch를 제출"하는 방식이다(`back/docs/knowledge/operations.md` 4). |
| D10 | **수집기 어댑터 계약 `discover → fetch → parse`와 출처 등급 T1~T5.** 유튜브·크롤러·NotebookLM·대량 적재는 같은 계약의 어댑터로 붙인다. | 사용자 추가 요구. 원본 보존·멱등·재개를 어댑터 밖 공통부가 보장하게 한다. |

### 사용자 지침과 다르게 간 부분

1. **예시 개념(Source, Document, Chunk, Knowledge, Evidence, Relation, Version)을 그대로 쓰지 않았다.** Document와 Version은 `kb_documents`(논리 저작물)와 `kb_revisions`(불변 수집 스냅샷)로 나눴다. 별도 "Knowledge" 객체를 두지 않고 `kb_claims`를 지식 단위로 삼았다. Relation은 두 개념을 잇는 claim의 한 형태로 흡수했다. 테이블 수와 조인 경로를 줄이기 위해서다.
2. **하이브리드 벡터 검색을 기본 경로에서 뺐다.** 측정된 실패가 토큰화·가중치 문제이고, 기존 e5 벡터는 무관한 회의에도 유사도 0.80 이상을 줬다(2.3 C6). 벡터는 투영(projection) 테이블과 플래그로 준비만 하고, 평가 게이트를 통과하면 켠다.
3. **관계 기반 검색(그래프, GraphRAG)은 최소화했다.** 10/15 오프라인 실험에서 텍스트만, 1-hop, 2-hop의 Recall@3이 모두 0.688로 같았다(`~/testing/ttakkari-knowledge-audit-20261015/retrieval-audit/README.md`). 개념 별칭 확장과 명시 호출 1-hop만 둔다.
4. **기존 맥락 도서관과 합치지 않았다.** 지침은 "합칠지 분리할지 근거"를 요구했고, 분리 근거는 D1이다. 원장 코드 패턴과 평가 하네스는 재사용한다.

---

## 1단계. 기존 자산 분석

### 1.1 실행 인프라(확인한 값)

| 항목 | 값 | 근거 |
|---|---|---|
| 호스트 | Mac Studio, Apple M5 Max, 18코어, 메모리 64 GiB, 데이터 볼륨 여유 406 GiB | `sysctl machdep.cpu.brand_string hw.memsize hw.ncpu`, `df -h ~` (2026-10-20) |
| GPU | Metal `MTL0: Apple M5 Max (53084 MiB)`, 호스트 llama-server 경로 존재 | `back/docs/adr/0037-execution-capabilities-and-host-gpu.md:8,19`, `back/src/ttakkari/host_gpu.py:1` |
| DB | PostgreSQL 17.11 (Homebrew), DB 크기 343 MB, `shared_buffers` 128MB(기본값) | `psql -c "select version()"` 등 |
| 확장 | 설치: `vector` 0.8.7, `pg_trgm` 1.6. 사용 가능: `unaccent`, `btree_gin`. 없음: `pg_bigm`, `pgroonga`, `pg_search` | `pg_available_extensions` 조회 |
| 임베딩 | `intfloat/multilingual-e5-large` 1024차원, fastembed, 요청별 자식 프로세스, CPU 2스레드 | `back/src/ttakkari/config.py:246-249`, `context/embedder.py:51-76` |
| 벡터 저장 | `Vector(1024)` 열 4곳, `ctx_chunks`에 HNSW cosine 인덱스 | `back/src/ttakkari/models.py:596,612,643,683,831`, `pg_indexes` |
| 어휘 인덱스 | `ctx_chunks`에 `to_tsvector('simple', content)` GIN과 `gin_trgm_ops` GIN. `knowledge_units`에는 본문 검색 인덱스 없음 | `pg_indexes` 조회 |

배포 런타임이 이 로컬 DB를 그대로 쓰는지는 이 세션에서 `/api/version`·runtime DB 식별로 확인하지 않았다(`확인 필요`). 아래 DB 수치는 로컬 `ttakkari` DB 기준이다.

### 1.2 맥락 도서관 구현 현황

| 층 | 구현 | 위치 |
|---|---|---|
| 온톨로지 | `ctx_entities`(이름·별칭·요약), `ctx_relations`(자유 문자열 술어 triple), `ctx_memories` | ADR 0007, `context/store.py:34-105` |
| RAG | `ctx_sources → ctx_documents → ctx_chunks`, 제목 기준 분할 1200자, 파일 sha 기준 증분 | `context/indexer.py:22-24` |
| 검색 | `terms()` 정규화 + `lexical()`(FTS, 부분 일치 비율, `word_similarity`) | `context/search.py:9-28` |
| 회의록 | 700자 창 + e5 벡터 + 어휘 혼합 | `meetings/vectors.py:186-246` |
| 지식 원장(ADR 0031) | `knowledge_revisions/units/evidence/assertions/assertion_evidence/review_events/extraction_jobs`. unit·evidence·review event는 UPDATE 금지 트리거, evidence는 DB에서 span 해시 검증 | `back/src/ttakkari/knowledge_models.py:25-269` |
| 원장 검색 | 같은 `lexical()`을 unit 본문에 적용 | `knowledge/store.py:447-459` |
| 추출 | 호출 모델이 MCP로 batch(최대 100건·20,000자)를 받아 후보 제출 | `back/docs/knowledge/operations.md` 4 |
| 평가 | gold JSON(dev/holdout 그룹 분리), Recall/MRR/nDCG, paired bootstrap, 오프라인 BM25 비교 | `knowledge/evaluation.py:11,19,69,91` |
| MCP | `library_catalog`, `recall`, `knowledge_recall` 등 | `orchestrator/mcp_server.py:140,147,932` |
| UI | 도서관 `근거·검토` 탭 | `front/src/features/knowledge/KnowledgeTab.tsx` |

로컬 DB 현황(2026-10-20 읽기 전용 조회): sources 79, documents 352, chunks 5,239(벡터 207), 활성 memory 348, entity 90, relation 15, knowledge revision 380, unit 16,472, **evidence 0, assertion 0, extraction job 0**, meeting chunk 296.

### 1.3 기존 스킬 자산(winterholic-skill)

- 정본은 `~/.claude`이고 공개판은 `~/development/projects/portfolio/opensource/winterholic-skill`이다. `update-winterholic-skill`은 출처 판정·익명화·`--only` 동기화 절차다(`~/.claude/skills/update-winterholic-skill/SKILL.md`).
- 전문가 팩: `dev-experts-skills` 95개 디렉터리(2.4 MB), `biz-experts-skills` 2.0 MB, `life-experts-skills` 1.4 MB, `stock-experts`. 맥락 도서관에는 각 `SKILL.md`만 241개 등록됐다(ADR 0009 "스킬 지식 등록").
- 재사용할 관례가 둘 있다.
  - 각 전문가 머리말의 `기준: <버전> (<연월>) · 부패 등급: 중간(반기)` 표기(예: `~/.claude/dev-experts-skills/dev-search/SKILL.md:8`). kb의 **적용 버전 + 변동성 등급(최신성 TTL)** 개념과 같다.
  - "버전 라벨이 낡았으면 공식 문서가 이긴다"(`~/.claude/skills/dev-experts/SKILL.md`). kb의 출처 등급 정책(T1 공식 문서 우선)과 같다.
- 스킬은 **절차적 지식**(어떻게 할지)이고, kb는 **선언적 지식**(무엇이 참인지)이다. 스킬 본문을 kb의 T3 큐레이션 출처로 수집할 수는 있지만 스킬 라우팅을 kb로 대체하지 않는다.

### 1.4 재사용 / 신규 구분

| 구성요소 | 판단 | 비고 |
|---|---|---|
| PostgreSQL 인스턴스, Alembic, 백업 경로 | 재사용 | 새 마이그레이션은 additive |
| 원장 불변식(UPDATE 금지 트리거, span 해시 검증 함수) | 패턴 재사용, `kb_*` 테이블에 새로 건다 | `knowledge_models.py:226-269` |
| 검토 이벤트·낙관적 version·사용자 확인은 사용자 API만 | 재사용 | 액터 구분 규칙 동일 |
| 추출 batch MCP(next/submit, cursor, 재시도 분류) | 재사용(kb용 스키마로 복제) | 추출기 버전·prompt SHA 기록 포함 |
| 평가 하네스(`score`, `paired_interval`, `validate_split`, `bm25`) | 재사용 | `knowledge/evaluation.py` |
| `scope_clause` | 불필요 | kb는 공용 지식이라 scope 대신 `visibility`(public/licensed/private-note)를 둔다 |
| `context/search.py`의 `terms`/`lexical` | **재사용 안 함** | 2.3의 C1~C3 원인 |
| e5 임베더 자식 프로세스 | 선택 재사용(벡터 투영 단계) | 모델은 교체 가능하게 |
| host GPU llama-server | 선택 재사용(재순위·임베딩 가속 후보) | 실행 검증 전 `확인 필요` |
| NotebookLM 연동(ADR 0045) | 수집기 어댑터의 한 종류 | 답변은 T5, 인용된 원 소스만 근거 |
| 신규 | 블롭 저장소, 한국어 분석기, 문서·리비전·패시지, 개념·별칭, claim·적용조건·정본 포인터, 출처 등급, 최신성 정책, 수집 어댑터, `kb_*` MCP, kb UI | |

---

## 2. 맥락 도서관 검색 연관성 문제 원인

### 2.1 기록된 증상

- 10/15 감사: source-first gold 24질의 6 cohort, live MCP `recall` K=5에서 Recall@3 0.271, nDCG@3 0.251, Recall@5 0.417, nDCG@5 0.314(`~/testing/ttakkari-knowledge-audit-20261015/retrieval-audit/README.md` "Latest measured result").
- 10/16 재감사: 같은 gold를 현재 `store.recall` 코드로 재생하니 Recall@5 0.458(95% CI 0.271~0.646), MRR@5 0.425, nDCG@5 0.398. cohort별 Recall@5는 cross_project_2hop 0.25, design_time 0.375, fact_lookup 0.5, alias 0.75(`~/testing/ttakkari-knowledge-audit-20261016-reaudit/data/current-db-gold-v1-replay.json`).
- 응답에 원문 hash·span이 없었다(필드 완전성 0/103, `.../retrieval-audit/findings.csv` F04).
- ADR 0009 스스로 한계를 적었다. "본문이 영어인 스킬은 한국어 일반어로 잘 안 걸린다", "동의어·표현 변화의 의미 검색은 보장하지 않는다"(`back/docs/adr/0009-context-library.md`).
- 로컬 `~/work-history/`에는 10/15 기록 파일이 없다(재감사 README도 같은 사실을 적었다). 감사 산출물과 `back/docs/work-history/2026-10-16-knowledge-*.md`가 1차 근거다.

### 2.2 오늘 재현한 관찰(2026-10-20)

- `terms("AIP가 앱별 백엔드에서 줄이려는 반복 작업은 무엇이며 어떤 원칙으로 처리하나?")` 출력: `['aip가', '앱별', '백엔드에서', '줄이려는', '반복', '작업은', '무엇이며', '어떤', '원칙으로', '처리하나']`. 이 질의는 10/15 gold F01이다.
- 같은 문장의 `plainto_tsquery('simple', …)` 일치 조각 수: **0**. `ilike '%aip가%'` 2건, `ilike '%aip%'` 219건, `ilike '%어떤%'` 192건.
- live `recall("맥락 도서관 검색 연관성 문제")` 1위는 회의록 PMS 연결 작업 기록이었다. 근거 묶음에는 `biz-content-marketing`의 "도서관 사서" 비유 문단과 `life-smart-buying` 체크리스트가 들어왔다. 회의 벡터 결과로 무관한 회사 회의(크롤러 동시성) 조각이 유사도 0.82~0.83으로 들어왔다. 이미 지워진 `docs/adr/0008-context-library.md` 경로의 조각도 나왔다(현재 0008은 `0008-memos-calendar.md`이고, `ctx_documents`에 옛 경로 1건이 남아 있다).
- `knowledge_units` 16,472건 중 40자 미만이 4,259건(26%)이다. 제목만 있는 unit(예: `## 맥락 도서관\n\n`)이 검색 결과에 다른 결과와 같은 점수(0.4617)로 나왔다.
- 모든 chunk 5,239건이 `entity_id`가 있는 출처에 속한다.

### 2.3 원인 정리

| # | 원인 | 근거 | 영향 |
|---|---|---|---|
| C1 | 조사·어미가 붙은 어절을 그대로 검색어로 쓴다. 정규식 단어 분리만 하고 형태소 분석이 없다. | `context/search.py:12-14`, 2.2 첫 두 항목 | 가장 변별력 있는 고유명사(`AIP`)가 `aip가`로 바뀌어 거의 안 맞는다. |
| C2 | 단어 가중치가 모두 같다(IDF 없음). 점수의 중심인 `coverage`가 "맞은 단어 수/전체 단어 수"다. | `context/search.py:23-27` | `어떤`·`작업은` 같은 흔한 어절이 희귀어와 같은 무게를 갖는다. 흔한 단어 여러 개를 맞힌 무관한 조각이 올라온다. |
| C3 | `plainto_tsquery`는 모든 단어를 AND로 묶는다. 문장형 질의에서는 FTS가 거의 0건이라 GIN 인덱스와 `ts_rank_cd`가 사실상 기여하지 않는다. 대신 `icontains` OR 조건이 후보를 만든다. | `context/search.py:20-26`, 2.2 FTS 0건 | 순위가 부분 일치 비율에 의존하고, 선행 와일드카드 `ILIKE`는 규모가 커지면 순차 탐색이 된다. |
| C4 | 척도가 다른 점수를 한 리스트에서 정렬한다. 어휘 점수, 엔터티 +0.8, 회의는 `(sim-0.78)×8 + 어휘 + 0.15`. | `context/store.py:216,227`, `meetings/vectors.py:31-32,233-237` | 결과 종류 사이의 순위가 질의와 무관하게 정해진다. |
| C5 | 질의에 프로젝트 이름이 있으면 그 프로젝트 출처의 모든 조각에 +0.65를 더한다. 모든 chunk가 엔터티 연결 출처라서 해당 프로젝트 전체가 같은 가산점을 받는다. | `context/store.py:197-198`, 2.2 마지막 항목 | 프로젝트 안에서는 변별이 사라지고, 다른 프로젝트 문서가 필요한 질의(cross_project 0.25)는 밀려난다. |
| C6 | e5 유사도 하한 0.80으로 벡터 전용 후보를 받는다. e5 계열은 무관한 문장끼리도 코사인이 높게 나와 하한이 변별을 못 한다. | `meetings/vectors.py:31-32,222`, 2.2 회의 0.82~0.83 | 무관한 회의 조각이 상위에 섞인다. 개인 Run은 `all` 범위라 정책 위반은 아니지만 회사 회의가 개인 질의 결과를 차지한다. |
| C7 | 출처 루트 열거가 실패하면 예외로 끝나 지워진 파일의 조각을 정리하지 못한다. | `context/indexer.py:131-138`, 재감사 `index-error-summary.json`, 2.2 옛 0008 경로 | 낡은 문서가 계속 검색된다. |
| C8 | 성격이 다른 자료가 한 풀에 섞인다. 개인 프로젝트 기록과 241개 전문가 스킬(영어 본문 포함), 회사 회의가 같은 점수 공간에서 경쟁한다. | ADR 0009 "스킬 지식 등록", 재감사 `db-snapshot.json`(knowledge 범주 chunk 3,743/5,159) | 일반어 질의에서 스킬 문단이 개인 맥락을 밀어낸다. |
| C9 | 분할 단위가 들쭉날쭉하다. 제목만 있는 unit이 26%이고, 문서당 최대 2조각 제한 외에 문서 단위 집계가 없다. | 2.2 unit 길이, `context/store.py:228-239` | 동점이 많고 문맥 없는 조각이 근거로 나온다. |

인과의 강도: C1~C3은 코드와 재현 쿼리로 확인했다. 각 원인이 Recall 손실에 얼마씩 기여했는지는 제거 실험(ablation)을 하지 않아 `확인 필요`다.

### 2.4 새 설계가 이 문제를 피하는 방법

| 원인 | kb 설계 대응 | 위치 |
|---|---|---|
| C1 | 색인·질의 모두 Kiwi 형태소 분석으로 체언·용언 어간만 남긴다. 영문은 소문자화·어간 추출, 코드 식별자는 원형과 분해형을 함께 둔다. | 3.2 |
| C2 | BM25 재채점(IDF·길이 정규화). `kb_term_stats`로 문서 빈도를 유지하고 고빈도 어휘는 후보 생성에서 뺀다. | 3.2 |
| C3 | 후보 생성은 분석된 lemma의 OR tsquery + GIN. `ILIKE` 경로는 쓰지 않는다. | 3.2 |
| C4 | 경로별 순위를 RRF로 결합한다. 점수를 더하지 않는다. | 3.3 |
| C5 | 개념 일치는 "후보 추가"로만 쓰고 가산점을 주지 않는다. 가산은 재순위 단계의 특징값 하나로 측정 후 결정한다. | 3.4 |
| C6 | 벡터는 단독 하한 대신 RRF 후보로만 들어간다. 무관 질의 음성 대조군으로 abstain 임계값을 정한다. | 3.3, 3.7 |
| C7 | 문서 단위 수집 원장과 "관측 안 됨" 상태. 출처 열거 실패는 `partial`로 남기고, 사라진 문서는 상태 이벤트로 검색에서 내린다(원본 리비전은 보존). | 4.6 |
| C8 | kb는 `ctx_*`와 다른 테이블·도구. 개인 `recall`에 kb 결과를 섞지 않는다. kb 안에서도 `domain` 필터를 둔다. | D1, 3.8 |
| C9 | 패시지는 제목 경로를 앞에 붙여 300~1,200자 범위로 만들고, 짧은 절은 상위 절과 합친다. 결과는 문서 단위로 묶어 반환한다. | 2단계 3절, 3.5 |

기존 맥락 도서관 자체의 개선(C1~C7을 `recall`에 적용)은 이번 범위 밖이다. kb에서 분석기와 BM25가 검증되면 같은 모듈을 `recall`에 적용하는 별도 작업으로 제안한다.

---

## 2단계. 지식 모델 설계

### 1. 모델 선택

| 대안 | 장점 | 문제 | 판단 |
|---|---|---|---|
| A. 기존 `knowledge_*`에 kb 자료를 넣고 `origin`만 구분 | 테이블 증가 없음 | scope 의미(personal/work/shared)가 세계 지식과 맞지 않음. 적용조건·출처 등급·정본 포인터가 없음. 개인 recall 풀이 오염됨(C8) | 기각 |
| B. 그래프 DB(Neo4j 등) + 벡터 DB | 관계 질의 표현력 | 운영 대상 2개 추가, 이중 정본. 그래프 이득은 미입증(오프라인 1/2-hop 차이 없음) | 기각 |
| C. 예시 7개 개념을 1:1 테이블로 | 직관적 | Knowledge와 Evidence/Relation의 경계가 겹쳐 조인이 늘어남 | 변형 채택 |
| **D. 원장(문서·리비전·패시지) + 지식층(개념·claim·근거·이벤트) 2층, PostgreSQL 단일 정본** | 기존 원장 패턴 재사용, 투영 재생성 가능 | 형태소 분석기 의존성 추가 | **채택** |

### 2. 논리 구조

```
kb_sources (발행처: 사이트·기관·채널·출판사, 신뢰 등급)
  └─ kb_documents (논리 저작물: URL·DOI·ISBN·영상 ID 정규 키)
       └─ kb_revisions (불변 수집 스냅샷: 원본 sha, 수집 시각, 파서 버전)
            └─ kb_passages (정규화 텍스트 구간, 문자 오프셋, 제목 경로)
                 └─ kb_evidence (passage 안의 정확한 span)
kb_concepts (주제·개념 노드) ─ kb_concept_aliases (ko/en 별칭)
kb_claims (주장: 주어 개념, 측면, 값/목적 개념, 적용조건, 유효시간, 양상)
  ├─ kb_claim_evidence (claim ↔ evidence, supports/refutes/qualifies)
  └─ kb_claim_events (검토·상태·정본 변경 이력, append-only)
kb_canonical (개념·측면·조건 묶음 → 정본 claim 포인터)
투영: kb_passage_vectors(모델별), kb_term_stats, kb_concept_cards(파생 요약)
운영: kb_ingest_jobs, kb_extraction_jobs, kb_search_traces, kb_eval_*
```

### 3. 스키마(핵심 컬럼·인덱스)

이름과 타입은 목표 계약이다. Alembic 구현 때 현재 스키마와 대조해 확정한다.

**kb_sources**: 발행처
- `id uuid pk`, `key text unique`(예: `site:postgresql.org`, `yt-channel:UC…`, `doi-prefix:10.1038`), `name`, `kind`(official_docs/standard/government/academic/encyclopedia/news/book/blog/forum/video/ai_generated/personal_note), `trust_tier smallint check 1..5`, `tier_reason text`, `license text`, `homepage`, `created_at`.
- 등급은 출처 단위 기본값이다. 문서 단위로 덮어쓸 수 있다(`kb_documents.trust_tier_override`).

**kb_documents**: 논리 저작물
- `id uuid pk`, `source_id fk`, `canonical_key text unique`(정규화 URL, `doi:`, `isbn:`, `youtube:<id>`, `file:<sha>`), `title`, `authors jsonb`, `language`(ko/en/…), `domain`(dev/science/economy/history/society/general/…), `published_at`, `trust_tier_override`, `volatility`(static/slow/medium/fast), `current_revision_id`, `status`(active/gone/withdrawn), `duplicate_of uuid null`, `created_at`.
- 인덱스: `(source_id)`, `(domain, language)`, `(status)`.

**kb_revisions**: 불변 스냅샷
- `id uuid pk`, `document_id fk`, `raw_sha256 char(64)`, `raw_mime`, `raw_bytes bigint`, `blob_path text`, `text_sha256`, `parser text`, `parser_version text`, `analyzer_version text`, `fetched_at timestamptz`, `http_meta jsonb`(etag, last-modified, status), `applies jsonb`(문서 전체에 해당하는 적용조건, 예: `{"product":"postgresql","version":"17"}`), `simhash bigint`, `created_at`.
- `UNIQUE(document_id, raw_sha256, parser_version)`. UPDATE 금지 트리거.
- `current_revision_id` 이동은 문서 행에서만 한다. 옛 리비전을 삭제하지 않는다.

**kb_passages**: 검색 단위
- `id uuid pk`, `revision_id fk`, `ord int`, `heading_path text[]`, `start int`, `end int`(정규화 텍스트 기준), `text text`, `text_sha256`, `lang`, `analyzed text`(분석 결과 lemma 공백열), `tsv tsvector generated always as (to_tsvector('simple', analyzed)) stored`, `token_count int`.
- 인덱스: GIN `(tsv)`, `(revision_id, ord)`, `(text_sha256)`(중복 탐지).
- 검색은 `current_revision_id`의 패시지만 본다. 옛 리비전 패시지는 시점 질의(`as_of`)와 근거 보존용이다.
- 분할: 제목 경로 단위, 목표 300~1,200자, 짧은 하위 절은 상위 절과 합친다. 자막은 60~90초 창. 패시지 앞에 `[문서 제목 > 절]`을 붙인 텍스트를 색인에 쓴다(오프셋은 원문 기준 유지).

**kb_evidence**: 정확한 인용
- `id`, `passage_id fk`, `start`, `end`(패시지 텍스트 기준), `sha256`. 기존 `knowledge_validate_span`과 같은 DB 트리거로 검증한다.

**kb_concepts / kb_concept_aliases**
- concept: `id`, `key text unique`(예: `pg:mvcc`, 선택적 `wikidata:Q…`), `label_ko`, `label_en`, `kind`(entity/topic/quantity/event/product), `domain`, `parent_ids uuid[]`(분류 계층, 선택), `summary_card_id`(파생 카드).
- alias: `concept_id`, `alias`, `lang`, `normalized`, `source`(curated/extracted/wikidata), `status`(accepted/candidate/conflict). 인덱스: `(normalized)` btree, `gin_trgm_ops`.
- 동명이의어는 같은 alias가 여러 concept에 붙을 수 있다. 자동 병합하지 않는다.

**kb_claims**: 지식 단위
- `id uuid pk`, `fingerprint char(64) unique`(정규화 subject+facet+value+conditions 해시), `subject_concept_id fk`, `facet text`(예: `default_isolation_level`, `capital`, `founded_year`, `definition`), `statement text`(사람이 읽는 한 문장), `value_text text`, `value_json jsonb`(수치·단위·날짜), `object_concept_id fk null`(관계형 claim), `conditions jsonb`(적용조건), `modality`(fact/definition/estimate/opinion/interpretation/prediction), `polarity bool`, `valid_from`, `valid_to`(세계 시간), `recorded_at`(시스템이 안 시각), `review_level`, `state`, `version int`, `best_tier smallint`, `independent_sources int`(파생 캐시), `last_verified_at`, `review_due_at`.
- `review_level`: candidate / span_checked / corroborated / verified. `state`: active / disputed / superseded / retracted / stale.
- 인덱스: `(subject_concept_id, facet)`, GIN `(conditions jsonb_path_ops)`, `(state, review_level)`, `(review_due_at)`.
- claim 본문(`subject/facet/value/conditions`)은 바꾸지 않는다. 바뀌면 새 claim을 만들고 `superseded` 이벤트로 잇는다. `state`, `review_level`, 파생 캐시만 이벤트와 같은 트랜잭션에서 갱신한다.

**kb_claim_evidence**: `claim_id`, `evidence_id`, `stance`(supports/refutes/qualifies), `extraction_job_id`, `added_at`. PK `(claim_id, evidence_id)`.

**kb_claim_events**: `id`, `claim_id`, `version`, `actor`(user/agent:<run>/system), `event`(created/reviewed/corroborated/disputed/superseded/retracted/canonical_set/stale_marked/evidence_changed/evidence_lost), `from_state`, `to_state`, `reason`, `payload jsonb`, `created_at`. `UNIQUE(claim_id, version)`, UPDATE 금지.

**kb_canonical**: `group_key char(64) pk`(subject_concept_id+facet+정규화 conditions 해시), `claim_id fk`, `selected_by`(policy/user), `score jsonb`(선정 근거 항목별 값), `selected_at`. 변경은 `canonical_set` 이벤트로 남긴다.

**투영 테이블**
- `kb_term_stats(term text pk, df int, updated_at)`, `kb_corpus_stats(passages int, avg_tokens real)`: BM25용. 수집 트랜잭션과 별도로 배치 갱신해도 된다(근사 허용).
- `kb_passage_vectors(passage_id, model_id, vec halfvec(1024))`, HNSW(halfvec_cosine_ops). 모델을 바꾸면 새 `model_id`로 다시 채운다.
- `kb_concept_cards(concept_id, generation, text, derived_from_claim_ids uuid[], generated_by, created_at)`: 개념 요약 카드. **파생물이며 근거가 아니다.** 입력 claim이 바뀌면 stale 처리한다.

### 4. 식별자 전략

| 대상 | 식별자 | 이유 |
|---|---|---|
| 행 PK | UUIDv7(앱에서 생성) | 시간 순서라 수백만 행에서도 btree 지역성이 좋다. 기존 uuid4 테이블과 타입 호환. PG18 내장 `uuidv7()` 여부는 `확인 필요` |
| 문서 | `canonical_key` 자연키 | 같은 저작물 재수집을 멱등으로. URL은 스킴·호스트 소문자화, 추적 파라미터(utm_*, fbclid) 제거, 끝 슬래시 정규화 |
| 원본 | sha256(raw bytes) | 블롭 내용 주소, 중복 제거 |
| 패시지 | sha256(text) + (revision, ord) | 리비전 간 같은 패시지 재사용 판단 |
| claim | fingerprint | 같은 주장이 여러 출처에서 오면 하나의 claim에 근거가 쌓인다 |
| 개념 | 사람이 읽는 `key` + 선택적 Wikidata QID | 외부 서비스 비종속. QID는 보조 연결 |

### 5. 원본 저장 위치

- 원본 bytes: `~/.ttakkari/kb/blobs/sha256/<2>/<2>/<sha>.zst`. 쓰기는 임시 파일 → fsync → rename. 같은 sha가 있으면 건너뛴다. `~/.ttakkari` 아래라 에이전트 작업 세션이 직접 고칠 수 없다(이 세션에서도 `~/.ttakkari/models` 읽기가 가드로 차단됐다). 수집은 백엔드 프로세스만 한다.
- DB: 정규화 텍스트(패시지), 메타데이터, 블롭 경로와 해시.
- 무결성 점검 CLI: 블롭 sha 재계산과 DB 대조. 백업 대상에 블롭 디렉터리를 포함해야 한다. 현재 백업 정책이 `~/.ttakkari` 하위 새 디렉터리를 포함하는지는 `확인 필요`(`back/src/ttakkari/backup_status.py` 확인 대상).
- 라이선스상 재배포가 금지된 자료도 개인 보관은 하되, 공유 기능(ADR 0030)으로 원문을 내보내지 않는다(`kb_sources.license`로 판단).

### 6. 버전·유효기간

| 축 | 컬럼 | 의미 |
|---|---|---|
| 발행 시간 | `kb_documents.published_at` | 자료가 나온 시점 |
| 수집 시간 | `kb_revisions.fetched_at` | 따까리가 원본을 가져온 시점 |
| 유효 시간(valid time) | `kb_claims.valid_from/valid_to` | 주장이 세계에서 참인 기간. 모르면 NULL. 수집 시각으로 채우지 않는다 |
| 기록 시간(transaction time) | `kb_claims.recorded_at` + `kb_claim_events.created_at` | 시스템이 그렇게 믿기 시작한/바꾼 시점 |

- "2020년 기준 한국 기준금리"는 valid time 질의, "지난달 따까리는 뭐라고 알고 있었나"는 기록 시간 질의다. 후자는 `kb_claim_events`를 시점까지 재생해 계산한다. 현재 `knowledge_recall`의 as_of는 valid time만 다룬다(`back/docs/knowledge/operations.md` "아직 남은 것"). kb는 처음부터 두 축을 분리한다.
- 적용 버전은 시간과 다른 축이다. `conditions`의 `version` 범위로 표현한다(예: `{"product":"postgresql","version":">=17 <18"}`). 비교는 후보를 가져온 뒤 앱에서 한다. 규모 문제가 확인되면 `int4range` 열로 정규화한다.

---

## 3단계. 검색 아키텍처

### 3.1 기법 비교와 채택

| 기법 | 해결하는 문제 | 비용 | 판단 |
|---|---|---|---|
| 형태소 분석 + BM25 | C1~C3, 고유명사·정확 용어 | CPU만, 색인 시 분석 1회 | **v1 채택** |
| 개념 별칭 확장(ko↔en) | 교차 언어, 약어 | 별칭 큐레이션 | **v1 채택** |
| 에이전트 제공 다중 질의 | 교차 언어·패러프레이즈 일부 | 호출 모델 토큰 | **v1 채택** |
| 순위 융합(RRF) | C4 | 무시할 수준 | **v1 채택** |
| 문서 단위 집계·계층 반환 | C9, 근거 묶음 | 낮음 | **v1 채택** |
| 밀집 벡터 | 패러프레이즈 | 임베딩 계산·저장. 100만 패시지 halfvec 약 2 GB + HNSW(`확인 필요`) | **게이트 후 채택** |
| 교차 인코더 재순위 | 상위권 정밀도 | 질의당 30~50쌍 추론 | **게이트 후 채택** |
| 1-hop 관계 확장 | "관련 개념" 질문 | 낮음 | 명시 호출만 |
| GraphRAG·커뮤니티 요약 | 전역 요약 질문 | 갱신 비용 큼, 근거 희석 | 미채택 |
| 외부 검색엔진(OpenSearch+nori) | 대규모 한국어 FTS | 서버 운영 | 미채택(재검토 신호 3.9) |

### 3.2 어휘 경로

색인:
1. 정규화: NFKC, 소문자화, 전각·반각 통일.
2. 언어별 분석
   - 한글: Kiwi(`kiwipiepy`) 형태소 분석. 일반명사·고유명사·동사/형용사 어간·외국어·숫자를 남기고 조사·어미·기호를 뺀다. 복합명사는 결합형과 분해형을 함께 넣는다. 라이선스·성능·사전 갱신은 `확인 필요`. 대안은 mecab-ko 계열.
   - 영문: 소문자화 + Snowball 영어 어간(앱에서). 불용어 소수만 제거.
   - 코드 식별자: `camelCase`, `snake_case`, `a.b.c`를 원형과 분해형 둘 다 넣는다.
3. 결과를 `analyzed` 열에 저장하고 `to_tsvector('simple', analyzed)` 생성 열 + GIN 인덱스.
4. `kb_term_stats` df 갱신.

질의:
1. 같은 분석기로 lemma를 뽑는다.
2. df/N > 5%인 lemma는 후보 생성 tsquery에서 뺀다(순위 계산에는 남긴다). 임계값은 평가로 조정한다.
3. 후보 생성: lemma를 `|`(OR)로 묶은 tsquery + GIN, `ts_rank_cd` 상위 200.
4. 재채점: 후보 200개에 대해 앱에서 BM25(k1=1.2, b=0.75)를 계산한다. tf는 `analyzed` 열에서, df는 `kb_term_stats`에서 가져온다. 기존 오프라인 구현(`knowledge/evaluation.py:91-113`)과 같은 식이다.
5. 희귀 lemma만 맞는 패시지가 200위 밖으로 밀리는 누락은 평가의 rare_term cohort로 측정한다. 누락이 확인되면 희귀어별 하위 질의를 따로 돌려 합친다.

### 3.3 융합과 재순위

```
queries[] ──► 분석 ──► (a) BM25 후보                         ┐
            └──► 개념 별칭 일치 ──► (b) 개념 claim·패시지 후보 ┤
            └──► [플래그] (c) 벡터 후보                       ┘
                         ▼
                  RRF(k=60) 결합 → 문서별 최대 3패시지
                         ▼
        [플래그] 교차 인코더 재순위(상위 40) → 상위 k
                         ▼
          근거 패키지 조립(claim·span·출처·조건·충돌·최신성)
```

- RRF: 각 경로·각 질의 변형의 순위를 `1/(60+rank)`로 더한다. 점수 척도가 달라도 결합이 안정적이다.
- abstain: 최종 1위의 BM25 정규화 값과(켜져 있으면) 재순위 점수가 음성 대조군 분포 기준선보다 낮으면 `coverage: none`. 임계값은 평가 세트의 unanswerable 질의로 정한다(3.7).
- 재순위 후보 모델: `BAAI/bge-reranker-v2-m3`(다국어). 실행 경로는 host GPU llama-server의 reranking 모드 또는 CPU 자식 프로세스. GGUF 가용성·llama-server 지원 여부·지연은 `확인 필요`. 켜는 조건: 같은 holdout에서 nDCG@10 +0.03 이상, p95 지연 예산 이내.
- 벡터 후보 모델: 기존 e5-large 재사용과 bge-m3 비교. 켜는 조건: paraphrase·cross_lingual cohort Recall@10 +0.05 이상, 다른 cohort 하락 0.02 미만. 100만 패시지 임베딩 처리량은 `확인 필요`(CPU 2스레드 자식 프로세스로는 수시간 이상으로 추정, GPU 경로 측정 필요).

### 3.4 개념·계층 경로

1. 질의 n-gram을 `kb_concept_aliases.normalized`와 정확 일치, 실패 시 trigram 유사도 0.6 이상으로 찾는다.
2. 일치한 개념의 정본 claim과 그 근거 패시지를 후보 (b)에 넣는다. **가산점은 주지 않는다**(C5 회피).
3. 별칭의 다른 언어 표기를 질의 변형으로 추가한다(예: "다중 버전 동시성 제어" → `MVCC`, `multiversion concurrency control`).
4. 계층 반환: `개념 카드(있으면) → 정본 claim → 근거 패시지 → 원문 위치` 순. 개념 카드에는 파생물 표시를 붙인다.

### 3.5 결과 계약(근거 패키지)

```json
{
  "rank": 1,
  "kind": "claim | passage",
  "claim": {
    "id": "…", "statement": "PostgreSQL 기본 격리 수준은 READ COMMITTED다",
    "conditions": {"product": "postgresql", "version": ">=9 <=17"},
    "valid": {"from": null, "to": null},
    "review_level": "corroborated", "state": "active", "canonical": true,
    "conflicts": [{"claim_id": "…", "summary": "…", "reason": "condition_overlap"}]
  },
  "evidence": [{
    "passage_id": "…", "quote": "…정확한 원문 구간…",
    "span": {"start": 120, "end": 211}, "span_sha256": "…",
    "document": {"title": "…", "url": "…", "published_at": "…"},
    "source": {"name": "PostgreSQL Documentation", "trust_tier": 1, "kind": "official_docs"},
    "revision": {"id": "…", "fetched_at": "2026-10-20T…", "raw_sha256": "…"}
  }],
  "freshness": {"volatility": "medium", "last_verified_at": "…", "stale": false},
  "why": ["bm25:rank2", "concept:pg:isolation", "rerank:0.91"]
}
```

응답 머리: `coverage`(none/weak/strong), `trace_id`, `budget`(후보 수, 생략 수), `filters_applied`. 근거 없는 claim은 반환하지 않는다. candidate claim은 `include_candidates=true`일 때만 `unverified` 표시와 함께 반환한다. 위 예시 값(격리 수준·버전 범위)은 형식 설명용이며 사실 검증을 하지 않았다.

응답 크기: 오늘 live `recall` 한 번의 응답이 수십 KB였다(원문 1,200자 조각 + knowledge 묶음 중복). kb는 quote를 근거 span 중심 400자 이내로 자르고, 전문은 `kb_get`으로 가져오게 한다.

### 3.6 한국어·영어 혼합 질의

- 한 질의 안에 한글·영문·코드가 섞이면 스크립트별로 나눠 각각 분석한 뒤 합친다.
- 교차 언어는 세 겹으로 처리한다. ① 개념 별칭(결정적), ② 호출 에이전트가 준 `queries[]` 번역 변형, ③ 게이트 후 다국어 벡터.
- 영어 원문만 있는 지식(대부분의 공식 문서)을 한국어로 물을 때 ①과 ②가 없으면 어휘 경로로는 못 찾는다. 도구 설명에 "한국어 질문이면 영어 변형을 함께 넣으라"를 명시한다.

### 3.7 평가 하네스

**질문 유형(cohort)**

| cohort | 예 | 보는 실패 |
|---|---|---|
| fact_ko | "광합성의 명반응은 어디서 일어나나" | 기본 정확도 |
| fact_en | "default isolation level in PostgreSQL" | 영문 |
| cross_lingual | 한국어 질문, 영어 원문 | 교차 언어 |
| paraphrase | 원문 단어를 쓰지 않은 질문 | 벡터 필요성 판단 |
| rare_term | 고유명사·약어·코드 식별자 | 후보 생성 누락 |
| version_conditional | "PG 16과 17에서 다른 점" | 적용조건 |
| temporal | "2015년 기준 한국 최저임금" | valid time |
| conflict | 출처끼리 다른 수치 | 충돌 표시 |
| related_concept | "MVCC와 관련된 개념" | 1-hop |
| unanswerable | 도서관에 없는 질문 | abstain |

**정답 세트 구성법**
1. source-first: 원문 패시지를 먼저 고르고 질문을 쓴다. paraphrase cohort는 원문 단어를 베끼지 않는다.
2. 라벨: 질의별 관련 패시지와 등급 0~3, 필요한 적용조건, 정답 claim(있으면). 기존 gold 형식(`id/group/cohort/query/relevance`)을 확장한다(`back/docs/knowledge/operations.md` 6).
3. 분리: 같은 문서에서 나온 질문은 같은 group. dev/holdout을 group 단위로 나누고 기존 `validate_split`으로 겹침을 거부한다.
4. 크기: 초기 목표 domain 3개 × cohort 10개 × 4~6문항 = 120~180문항. 통계력 충분성은 주장하지 않는다(paired bootstrap CI 공개).
5. 라벨러: 에이전트가 원문을 보고 1차 라벨, 다른 세션이 독립 검토. 사용자에게는 애매한 문항만 묻는다. 검색 결과로 정답을 역산하지 않는다.

**지표**

| 지표 | 정의 |
|---|---|
| Recall@k (k=5,10) | 질의별 관련 패시지 회수율의 macro 평균 |
| nDCG@10 | 등급 0~3 |
| MRR@10 | 첫 관련 결과 순위 |
| Abstain P/R | unanswerable에서 coverage=none 비율, 답 있는 질문에서 잘못 none인 비율 |
| Citation precision | 반환한 quote가 질문의 답을 실제로 지지하는 비율(표본 판정) |
| Condition accuracy | 버전·시점 질문에서 맞는 조건의 claim이 1위인 비율 |
| 지연 | p50/p95(경로별: 어휘, +벡터, +재순위) |
| 자원 | 색인 크기, 질의당 CPU 시간, GPU 메모리, 수집 처리량(패시지/분) |

비교 기준선: `raw_literal`, `legacy_lexical`(기존 `lexical()`을 kb 패시지에 적용), `bm25_kiwi`, `+concept`, `+vector`, `+rerank`. 같은 코퍼스·같은 gold·같은 코드 버전에서만 비교한다. 실패 사례는 질의별로 "원문 미수집 / 후보 없음 / 후보 있으나 순위 밖 / 순위 안이나 조건 틀림"으로 분류해 남긴다.

### 3.8 에이전트 인터페이스(MCP 초안)

| 도구 | 입력 | 출력 | 권한 |
|---|---|---|---|
| `kb_search` | `queries: string[1..4]`, `k=8`, `domain?`, `conditions?{product,version,region,…}`, `as_of?`(valid time), `min_review="span_checked"`, `include_candidates=false`, `hops=0..1` | 3.5의 근거 패키지 | 읽기, 자동 승인 |
| `kb_get` | `id`(claim/document/concept/passage), `history=false` | 상세, 버전·이벤트, 근거·반박, 충돌, 패시지 전문 | 읽기 |
| `kb_ingest` | `url` 또는 `path` 또는 `text`+`source_meta`, `domain`, `reason`, `adapter?` | `job_id`, 예상 단계 | 쓰기(원본 추가만) |
| `kb_ingest_status` | `job_id` | 단계·처리량·오류·생성 문서 | 읽기 |
| `kb_next_batch` / `kb_submit_batch` | 기존 추출 batch 계약과 같음. 제출 항목은 claim 후보 + 정확한 quote | 수락·거부 사유 | 쓰기(candidate만) |
| `kb_review` | `claim_id`, `decision`, `expected_version`, `reason` | 새 version | 에이전트는 span_checked/disputed까지. verified·정본 수동 지정은 사용자 API |

도구 설명(초안):
- `kb_search`: "일반 지식(개발, 과학, 경제, 역사, 사회, 상식)을 따까리가 보관한 원문 근거와 함께 찾는다. 개인 맥락·프로젝트 기록은 `recall`을 쓴다. 한국어 질문이면 영어 표현도 `queries`에 함께 넣는다. `coverage`가 none이나 weak이면 외부 검색으로 넘어가고, 쓸 만한 원문을 찾으면 `kb_ingest`로 보관한다. unverified 결과는 미검증이라고 밝힌다."
- 이 문장이 "필요할 때만 쓰고, 외부 검색 결과를 쌓는" 순환을 만든다. 비서 지침(`~/.ttakkari/harness/instructions.md`)에는 도구 존재와 원칙 한 줄만 넣는다. 자동 주입은 하지 않는다.
- 회사 Run 노출: kb는 공용 지식이라 work/personal 구분이 없지만, `visibility=private-note` 자료는 개인 Run에서만 보인다.

### 3.9 재검토 신호

- 어휘 경로 p95가 300 ms(초기 예산, 측정 전)를 넘거나 GIN 후보 생성이 순차 탐색으로 바뀌면: `shared_buffers`·`work_mem` 조정 → `pg_search`(ParadeDB, BM25 내장) 또는 임베디드 Tantivy 투영을 비교한다. Homebrew PG17에 pg_search 설치 가능 여부는 `확인 필요`.
- 패시지 5천만 건을 넘거나 단일 테이블 VACUUM이 운영에 영향을 주면 `kb_passages`를 `revision_id` 해시 파티션으로 나눈다.
- 형태소 분석이 신조어·도메인 용어를 계속 놓치면 사용자 사전(개념 별칭에서 자동 생성)을 Kiwi에 넣는다.

---

## 4단계. 지식 관리 정책

### 4.1 출처 신뢰 등급

| 등급 | 종류 | 예 |
|---|---|---|
| T1 | 1차 공식 | 표준 문서(RFC, ISO), 공식 문서·릴리스 노트, 법령 원문, 정부·중앙은행 통계, 동료심사 논문 원문 |
| T2 | 편집 검증 2차 | 교과서(OpenStax 등), 편집 백과, 주요 언론의 사실 보도, 학술 리뷰 |
| T3 | 전문가·공동체 | 위키백과, 저명 기술 블로그, Stack Overflow 채택 답변, 사용자 큐레이션 스킬 |
| T4 | 일반 웹·영상 | 개인 블로그, 유튜브 강의, 포럼 글 |
| T5 | AI 생성·출처 불명 | NotebookLM·LLM 답변, 요약 카드, 출처 없는 메모 |

- 등급은 출처 기본값이고 문서 단위로 조정할 수 있다. 조정에는 사유가 필요하다.
- 역사처럼 1차 사료와 해석이 갈리는 영역은 사실 진술(`modality=fact`)과 해석(`modality=interpretation`)을 나눠 각자 정본을 둔다.

### 4.2 검토 수준과 승격 규칙

| review_level | 조건 | 누가 |
|---|---|---|
| candidate | 추출됨. quote가 원문 span과 일치(DB 트리거) | 추출 batch(에이전트) |
| span_checked | quote가 claim을 실제로 지지하는지 추출자와 다른 Run이 원문 대조 | 에이전트 |
| corroborated | 서로 독립인 출처 2개 이상의 근거. 그중 하나는 T1 또는 T2. 독립성은 발행처가 다르고 simhash 중복이 아닌 경우 | 시스템 자동 계산 |
| verified | 사용자가 확인. 또는 사용자가 위임한 영역에서 T1 원문을 직접 대조한 검토 | 사용자 API(위임 규칙은 사용자 설정) |

- **AI 생성 출처(T5)의 근거는 승격 계산에 세지 않는다.** NotebookLM 답변이 인용한 원 소스 패시지를 수집해 그 span을 근거로 써야 한다.
- 에이전트가 쓴 요약·개념 카드는 claim이 아니다. 검색 결과에 `derived` 표시로만 나온다.
- 기본 `kb_search`는 `span_checked` 이상과 claim 없는 패시지(원문 그대로)를 반환한다. candidate claim은 명시 요청 시 `unverified`로 나온다.

### 4.3 정본 선정

정본 묶음 = `(subject_concept, facet, 정규화 conditions)`. 묶음 안에서 다음 순서로 고른다.

1. `state=active`만 대상. 모든 후보가 disputed면 정본 없음으로 표시한다.
2. `review_level` 높은 순.
3. 근거 최고 등급(`best_tier`) 높은 순.
4. 독립 출처 수 많은 순.
5. 변동성이 medium/fast인 영역이면 `last_verified_at` 최신 순. static/slow면 이 항목을 건너뛴다.
6. 동점이면 기존 정본 유지(불필요한 교체 방지).

선정 결과와 각 항목 값은 `kb_canonical.score`와 `canonical_set` 이벤트로 남긴다. 사용자가 수동 지정하면 `selected_by=user`이고 정책 재계산이 덮지 않는다. 새 근거로 바뀔 상황이면 검토 큐에 올린다.

### 4.4 충돌 처리

- 탐지: 같은 subject·facet에서 conditions가 겹치고 valid time이 겹치는데 값이 다르거나 polarity가 반대면 충돌 묶음을 만든다.
- 먼저 해소 가능성을 본다. ① 조건 차이(버전·지역·정의 차이)로 설명되면 conditions를 나눠 두 claim으로 분리한다. ② 시간 차이면 valid time을 나눈다. ③ 단위·반올림 차이면 정규화 후 같은 claim으로 합친다.
- 해소가 안 되면 둘 다 `disputed`로 두고 검색 결과에 양쪽 근거를 함께 보여 준다. 정본이 있으면 정본을 앞에 두고 `conflicts`를 붙인다.
- 반박 근거(refutes)도 근거로 저장한다. 삭제하지 않는다.

### 4.5 중복 관리

| 수준 | 방법 |
|---|---|
| 원본 | raw sha256 같으면 블롭 재사용 |
| 문서 | canonical_key 정규화. 다른 URL이 같은 내용이면 simhash 해밍 거리 3 이하를 중복 후보로 표시(합치지 않고 `duplicate_of`로 연결) |
| 패시지 | text sha256 같으면 검색 결과에서 하나만 보이고 나머지는 "같은 내용 N곳"으로 접는다 |
| claim | fingerprint 같으면 근거만 추가. 의미상 같은 다른 표현은 병합 후보 큐(자동 병합 금지) |
| 개념 | 별칭 충돌 큐. 동명이의어 자동 병합 금지 |

신디케이션·미러 사이트가 같은 기사를 복제하면 독립 출처로 세지 않는다.

### 4.6 최신성 평가와 갱신

| volatility | 예 | 재검증 주기(초기값) |
|---|---|---|
| static | 수학 정의, 확정된 역사 사실 | 없음 |
| slow | 과학 합의, 법 원칙 | 3년 |
| medium | 소프트웨어 버전·API, 기술 권장 사항 | 6개월(스킬의 "부패 등급: 중간(반기)"와 맞춤) |
| fast | 가격, 금리, 통계 최신값, 뉴스 | 7일부터 30일 사이에서 출처별 지정 |

- `review_due_at = last_verified_at + 주기`. 지나면 `stale` 표시를 붙여 계속 반환한다(삭제하지 않음). 에이전트는 stale 결과를 보면 외부 확인 후 `kb_ingest`로 새 리비전을 넣는다.
- 감시 출처(watch): 사용자가 지정한 문서만 주기적으로 재수집한다. ETag/Last-Modified로 변경을 먼저 확인한다.
- 새 리비전이 들어오면 텍스트 diff로 바뀐 패시지만 다시 분석·색인한다. 그 패시지를 근거로 쓰던 claim에 `evidence_changed` 이벤트를 남기고 추출 batch 대상에 넣는다. 근거 span이 사라진 claim은 `evidence_lost`로 표시하고 검토 큐에 올린다. 옛 리비전 근거는 보존한다.
- 문서가 사라지면(404/410, 파일 삭제) `kb_documents.status=gone`. 패시지는 현재 검색에서 내리고 claim 근거는 "보존된 옛 리비전"으로 표시한다(C7 회피). 출처 열거 자체가 실패하면 기존 문서를 건드리지 않고 작업을 `partial`로 끝낸다.

### 4.7 미검증 자료의 저장과 구분

- 미검증 자료도 저장한다. 구분은 세 곳에서 강제한다. ① DB: `review_level` CHECK와 승격 경로를 사용자/시스템 API로 제한. ② 검색: 기본 필터 `min_review=span_checked`. ③ 표시: 응답의 `review_level`, `unverified` 플래그, UI 배지.
- 에이전트 응답 규칙(도구 설명에 포함): candidate이거나 T4/T5만 근거인 내용은 "미검증"이라고 말한다.

---

## 확장 인터페이스(이번 구현 범위 아님)

### 수집기 어댑터 계약

```python
class Collector(Protocol):
    name: str            # "file", "url-html", "pdf", "youtube", "notebooklm", "crawler", "bulk-jsonl"
    version: str

    async def discover(self, spec: dict, cursor: str | None) -> DiscoverPage:
        """수집 후보 목록. 대량 수집은 cursor로 이어 간다."""

    async def fetch(self, item: DiscoverItem) -> RawArtifact:
        """원본 bytes·mime·URL·fetched_at·http meta. 원본을 고치지 않는다."""

    async def parse(self, raw: RawArtifact) -> NormalizedDoc:
        """정규화 텍스트, 절 구조(제목 경로·오프셋), 언어, 메타데이터(제목·저자·발행일·적용 버전 힌트)."""
```

공통부(어댑터 밖)가 보장하는 것: 블롭 저장, 문서 정규 키·멱등, 리비전 생성, 패시지 분할·분석·색인, 작업 원장(`kb_ingest_jobs`: queued/running/succeeded/partial/failed, cursor, 항목별 결과·사유), 재시도 분류(일시 오류만), 속도 제한, robots.txt·라이선스 기록.

| 후속 기능 | 어댑터 | 메모 |
|---|---|---|
| NotebookLM MCP 고도화 | `notebooklm` | 노트북 소스(URL·문서·영상)는 원 소스 어댑터로 따로 수집한다. NotebookLM 답변은 T5 문서로 저장하고, 인용 번호를 원 소스 패시지에 매핑해 claim 후보의 근거로 제안한다. ADR 0045 3.3절 "지식 원장 연계"의 대상을 kb로 바꾼다 |
| 유튜브 대량 수집 | `youtube` | 채널=source, 영상=document, 자막 시간 구간=passage. 수동 자막 우선, 자동 자막은 품질 표시. 수집 도구와 약관은 `확인 필요` |
| 자율 크롤링 | `crawler` | 시드·허용 도메인·깊이·일일 예산을 명시한 작업만. robots·속도 제한 필수. 결과는 T3/T4 기본 |
| 대량 적재 | `bulk-jsonl`, `wikipedia-dump` | 배치 커밋, `kb_term_stats` 재계산은 적재 끝에 한 번. 벡터 투영은 별도 작업 |

### 다른 확장점

- `Analyzer`(형태소 분석기 교체), `VectorProjector`(모델 교체), `Reranker`(모델·실행 경로 교체), `ClaimExtractor`(호출 에이전트 batch가 기본, 로컬 모델은 선택).
- 검색 trace(`kb_search_traces`: 질의 해시, 경로별 후보 순위, 최종 결과 ID, 지연)는 평가 재현과 실패 분석용. 보존 기간 90일(초기값).

---

## 5단계. 구현 계획

### 원칙

- 각 작업은 하루 이내 단위, additive 마이그레이션, 기능 플래그 `kb_enabled`.
- 검증은 좁은 테스트 → 전용 테스트 DB 전체 회귀 → 실제 MCP 호출 순. 구조 구현 완료와 검색 품질 입증을 같은 상태로 보고하지 않는다.
- 배포는 사용자 상시 정책(검증 후 commit·push·`request_backend_deploy`)을 따른다.

### 작업 단위

| 코드 | 작업 | 선행 | 검증 |
|---|---|---|---|
| KB0 | ADR(번호는 현재 최신 0060 다음) "범용 지식 도서관" + 이 문서를 `back/docs/` 아래 기존 관례(`docs/knowledge/`와 같은 형태의 새 폴더)로 옮김 | 없음 | 문서 리뷰 |
| KB1 | 마이그레이션: `kb_sources/documents/revisions/passages/evidence` + 불변·span 트리거 | KB0 | 업그레이드/다운그레이드 왕복(빈 원장만 다운그레이드 허용), UPDATE 거부·span 불일치 거부 테스트 |
| KB2 | 블롭 저장소(`kb/blobs.py`): 원자적 쓰기, sha 검증, 무결성 CLI `ttakkari kb-fsck` | KB1 | 중복 쓰기 멱등, 손상 탐지, 경로 탈출 거부 테스트 |
| KB3 | 분석기(`kb/analyze.py`): Kiwi + 영문 어간 + 식별자 분해, 분석기 버전 기록 | 없음 | 조사 제거(`AIP가`→`aip`), 혼합 질의, 코드 식별자 골든 테스트 |
| KB4 | 수집 공통부 + `file`(md/txt/pdf), `url-html` 어댑터 + `ttakkari kb-ingest` CLI + `kb_ingest_jobs` | KB1~KB3 | 같은 입력 재실행 멱등, 변경 시 새 리비전·현재 포인터 이동, 실패 시 partial |
| KB5 | 패시지 분할기 + `kb_term_stats` 배치 갱신 | KB4 | 길이 분포(300~1,200자 비율), 제목 단독 패시지 0건 |
| KB6 | `kb_search` v1: 후보 생성·BM25 재채점·문서 집계·근거 패키지·trace | KB5 | 단위 테스트 + 평가 하네스 기준선 |
| KB7 | 평가 코퍼스·gold(아래) + `ttakkari kb-eval` | KB4(병행 가능) | dev/holdout 그룹 분리 검사, 기준선 수치 기록 |
| KB8 | MCP `kb_search/kb_get/kb_ingest/kb_ingest_status` + 도구 정책 등록(`orchestrator/tool_policy.py`) | KB6 | REST·MCP 목록 일치, 실제 Run에서 호출 1회(실행 증거 기록) |
| KB9 | 개념·별칭 테이블 + 시드(평가 코퍼스 범위) + 별칭 확장 경로 | KB6 | cross_lingual cohort 개선 측정 |
| KB10 | claim·근거·이벤트·정본 테이블 + 추출 batch MCP + `kb_review` | KB8 | 승격 규칙(T5 근거로 승격 불가), 충돌 탐지, 정본 재계산 이벤트 테스트 |
| KB11 | 최신성: `review_due_at`, stale 표시, 감시 출처 재수집 작업 | KB10 | 시계 고정 테스트, ETag 경로 |
| KB12 | 벡터 투영(플래그) + RRF | KB7 | 게이트 판정(3.3) |
| KB13 | 재순위(플래그) | KB7 | 게이트 판정, p95 측정 |
| KB14 | UI: 도서관에 "지식" 탭(검색, 문서·리비전·근거 보기, claim 검토, 충돌·stale 큐) | KB8, KB10 | 320/390px·데스크톱 브라우저 검증 |

권장 순서: KB0 → KB1 → KB2·KB3(병행) → KB4 → KB5 → KB7(코퍼스 준비는 KB4 직후 시작) → KB6 → KB8 → KB9 → KB10 → KB11 → KB12·KB13 → KB14.

### 실제 자료로 평가하는 방법

| 분야 | 코퍼스(라이선스) | 규모 목표 | 특화 cohort |
|---|---|---|---|
| 개발 | PostgreSQL 16·17 공식 문서(PostgreSQL License), Python 3.12·3.13 문서(PSF) | 문서 400개 안팎 | version_conditional, rare_term, cross_lingual(한국어 질문) |
| 과학 | OpenStax Biology·Chemistry 일부 장(CC BY), 한국어 위키백과 대응 문서(CC BY-SA) | 장 20개 + 문서 200개 | paraphrase, fact_ko/en |
| 경제·사회 | 한국은행 경제용어 해설·통계 설명 자료, 통계청 지표 설명(이용 조건 `확인 필요`) | 용어 500개 | temporal, conflict(기준 시점이 다른 수치) |
| 역사·상식 | 한국어·영어 위키백과 문서 쌍(CC BY-SA) | 문서 300개 | cross_lingual, related_concept |
| 내부 | 전문가 스킬 팩(T3) 일부 | 50개 | 기존 도서관과의 비교용 |

절차:
1. 코퍼스를 KB4로 수집하고 수집 원장의 입력 수 = 성공 + 실패 + 제외를 검산한다.
2. 분야별 source-first 질문을 만든다(3.7). 질문 작성 세션과 라벨 검토 세션을 분리한다.
3. 기준선(`legacy_lexical`, `raw_literal`)과 `bm25_kiwi`를 같은 holdout에서 비교한다. 초기 검토 기준(실측 아님): 전체 Recall@10 ≥ 0.80, nDCG@10 ≥ 0.65, unanswerable abstain recall ≥ 0.8, 어휘 경로 p95 ≤ 300 ms.
4. 규모 시험: 위키백과 덤프 일부로 패시지 100만 건을 합성 적재해 색인 크기·GIN 후보 생성 지연·적재 처리량을 측정한다. 이 측정 전에는 "수백만 건 확장 가능"을 검증된 사실로 보고하지 않는다.
5. 실패 사례를 cohort별로 분류해 다음 게이트(벡터·재순위) 판단에 쓴다.

### 구현 세션에 넘길 첫 작업 단위

**KB1 + KB2: `kb_sources/documents/revisions/passages/evidence` 마이그레이션과 블롭 저장소.**
- 범위: Alembic 새 리비전(현재 최신 `0052_artifact_area_byproducts` 다음), `kb_models.py`, `kb/blobs.py`, 단위 테스트.
- 완료 조건: 업그레이드·다운그레이드 왕복, 불변 트리거·span 검증 트리거 동작, 블롭 원자적 쓰기·중복 멱등·손상 탐지 테스트 통과, 기존 `ctx_*`/`knowledge_*` 테스트 회귀 없음(전용 테스트 DB).
- 같은 세션에서 병행 가능: KB3 분석기 골든 테스트. 의존성 추가 전에 Kiwi 라이선스와 macOS arm64 휠을 확인한다.

---

## 미확인 항목

| 항목 | 상태 |
|---|---|
| 배포 런타임이 로컬 `ttakkari` DB와 같은지 | 확인 필요(`/api/version`, knowledge status의 DB 식별) |
| Kiwi(`kiwipiepy`) 라이선스, arm64 휠, 처리 속도 | 확인 필요 |
| `bge-reranker-v2-m3` GGUF와 llama-server reranking 지원, 지연 | 확인 필요 |
| e5-large/bge-m3로 100만 패시지 임베딩 처리 시간, halfvec+HNSW 크기 | 확인 필요(추정치만) |
| `pg_search`의 Homebrew PG17 설치 가능 여부 | 확인 필요 |
| PG18 `uuidv7()` 제공 여부 | 확인 필요 |
| 백업이 `~/.ttakkari/kb/blobs`를 포함하는지 | 확인 필요 |
| 한국은행·통계청 자료 이용 조건, 유튜브 자막 수집 도구 약관 | 확인 필요 |
| 기존 도서관 원인 C1~C9 각각의 Recall 기여도 | 제거 실험 미실시 |
| 성능 목표치(Recall@10 0.80, p95 300 ms 등) | 설계 예산이며 실측 아님 |
| 10/15 work-history 원문 | 로컬 `~/work-history/2026-10-15.md` 없음. 감사 산출물로 대체 |

## 참고한 근거

- 코드: `back/src/ttakkari/context/{search,store,indexer,embedder}.py`, `meetings/vectors.py`, `knowledge_models.py`, `knowledge/{store,evaluation}.py`, `orchestrator/{mcp_server,tool_policy}.py`, `config.py`
- 문서: `back/docs/adr/0007, 0009, 0021, 0031, 0037, 0045`, `back/docs/knowledge/{architecture,operations}.md`
- 감사: `~/testing/ttakkari-knowledge-audit-20261015/retrieval-audit/`, `~/testing/ttakkari-knowledge-audit-20261016-reaudit/`
- 스킬: `~/.claude/skills/update-winterholic-skill/SKILL.md`, `~/.claude/skills/dev-experts/SKILL.md`, `~/.claude/dev-experts-skills/dev-search/SKILL.md`
- 실행 관찰(2026-10-20): 로컬 DB 읽기 전용 조회, `terms()` 실행, live MCP `recall` 2회
