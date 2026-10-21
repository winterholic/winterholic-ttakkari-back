# 따까리 범용 지식 도서관(kb) 설계 v2

> **상태: 초안: 독립 검토 전, 구현 금지.** 독립 검토를 통과하기 전에는 이 문서를 근거로 제품 코드·마이그레이션을 만들지 않는다. 원본은 2026-10-21 outbox 산출물(sha256 `6befe534d6c22d710bcfb0a5aa74d4420437110914e89ccdcbeba689be6e425a`)이고, 이 사본은 상태 줄 추가와 부록 A의 스크립트 위치만 바꿨다.

- 작성: 2026-10-21, 설계 세션. 제품 코드는 바꾸지 않았다. 측정은 `/tmp/kbv2`의 임시 클러스터와 임시 llama-server로만 했다.
- 기준 코드: `winterholic-ttakkari/back` HEAD `66c8402c2f92d6114a9dcf1df8c6ade80e40db1a`. 작업 트리에 다른 세션의 미커밋 collector 파일(`collector/pipeline.py`, `0062_collector_stage_items.py` 등)이 있다. 이 문서는 그 파일을 읽기만 했다.
- 표기: 코드 근거는 `경로:줄`이고 경로의 `back/`은 `~/development/projects/winterholic-ttakkari/back/`이다. 실측은 `[실측 Mn]`으로 표시하고 부록 A에 명령과 원 출력을 남겼다. 측정하지 않은 수치·버전·인과는 `확인 필요`로 표시한다.
- v1 독립 검토 원문(`outbox/4115e52e…/knowledge-library-design-review.md`)은 ttakkari 가드가 읽기를 막았다. 지적 2–6은 요청서의 요약을 근거로 삼았다(`검증 못함: 원문 접근 차단`). 지적 1, 7, 8, 9는 v1 문서 6·7단계에 반영된 내용으로만 확인했다.

---

## 0. 1쪽 요약

### 핵심 결정

| # | 결정 | 근거 |
|---|---|---|
| V1 | **후보 생성(재현율)과 순위화(정밀도)를 분리한다.** 후보는 어휘 BM25, 다국어 dense, 별칭 확장 세 경로에서 서버가 결정적으로 만든다. 순위는 교차 인코더 재순위가 정한다. RRF는 후보 합집합을 재순위 입력 크기로 자르는 데만 쓴다. | 지적 5. RRF는 이미 후보에 든 문서의 순서만 바꾼다. 누락된 후보는 되살리지 못한다. |
| V2 | **다국어 dense 경로(bge-m3)를 처음부터 기본으로 켠다.** v1의 "평가 후 켬"을 뒤집는다. 대신 평가 하네스가 "dense를 켜도 다른 유형이 떨어지지 않음"을 확인해야 출시한다. | 지적 2. 한국어 질의 대 영어 원문 8쌍 스모크에서 bge-m3 top-1 8/8 `[실측 M7]`. GPU 처리량 초당 130패시지, 질의 임베딩 p50 11ms `[실측 M6]`. CPU로는 초당 6.1패시지라 GPU 경로가 전제다 `[실측 M8]`. |
| V3 | **어휘 경로는 PostgreSQL FTS가 아니라 임베디드 Tantivy 투영(projection)으로 한다.** 정본은 계속 PostgreSQL이다. Tantivy 색인은 PG의 분석 결과로 언제든 다시 만든다. | 8단어 OR 질의 top-200: PG `ts_rank_cd`는 10만 건 p50 560ms, 100만 건 p50 5.7s. Tantivy는 10만 건 p50 0.83ms, 100만 건 p50 7.6ms `[실측 M3, M4, M5]`. |
| V4 | **벡터 색인은 이진 양자화(BQ) HNSW로 후보를 뽑고 halfvec 원 벡터로 재채점한다.** | 10만 건에서 full HNSW는 빌드 6분 44초·260MB, BQ HNSW는 23초·42MB `[실측 M9]`. 실제 bge-m3 벡터 1,815개에서 BQ 후보 100개 재채점의 top-10 일치율 0.989 `[실측 M10]`. |
| V5 | **질의 확장은 서버가 결정적으로 한다.** 에이전트의 `alt_queries`는 선택 입력(최대 2개)이고 평가는 이것 없이 통과해야 한다. | 지적 5. 같은 질문에 같은 결과가 나와야 회귀를 잴 수 있다. 호출 모델마다 변형이 달라 비용과 품질이 흔들린다. |
| V6 | **평가 하네스를 KB3보다 먼저 만든다.** 게이트 판단은 사람이 확인한 holdout과 사람 판정이 있는 공개 세트(MIRACL-ko, XOR-TyDi ko)로만 한다. LLM 판정은 dev 탐색용으로 따로 표시한다. | 지적 3. 사용자 원칙 "AI 결과만으로 정본 승격 금지"를 평가에도 적용한다. |
| V7 | **주장은 명제(claim)와 진술(assertion)로 나눈다.** 명제 식별에는 주어·측면·값·극성·양상·적용조건·유효시간이 들어간다. 출처는 진술 쪽에 둔다. 추정·의견처럼 누가 말했는지가 의미의 일부인 양상은 발화 주체를 적용조건에 넣는다. | 지적 6. v1 키 "개념·측면·적용조건"으로는 시점이 다른 값, 반대 극성, 기관별 추정치가 한 묶음에 섞인다. |
| V8 | **수집은 collector, 의미 추출은 kb가 맡는다.** kb는 collector outbox의 `kb` sink를 소비하는 어댑터를 소유한다. 추출 LLM 호출은 수집량이 아니라 "실제로 검색된 패시지 수와 고정 예산 중 작은 값"에 비례한다. | 사용자 결정(2026-10-20). collector ADR은 지식 모델과 분리를 요구한다(`docs/adr/collector-platform-20261020.md`). 패시지 우선 검색이라 claim 없이도 검색이 된다. |
| V9 | **저장 본문은 생산자 정규화(NFC)를 그대로 쓰고 NFKC는 색인 분석으로 옮긴다.** envelope 경로의 revision은 `normalized.sha256`과 `kb_revisions.text_sha256`이 같아야 받는다. | collector는 NFC로 정규화한다(`collector/textproc.py:3,48`). kb 기존 경로는 NFKC다(`kb_store.py:61-63`). 둘이 다르면 오프셋과 해시가 어긋난다. |

### v1에서 바뀐 결정

| v1 | v2 | 바꾼 이유 |
|---|---|---|
| D4 BM25가 1차, 벡터는 게이트 후 | 어휘(Tantivy)·dense·별칭 3경로 기본 | 교차 언어 재현율은 어휘 경로로 원리상 확보할 수 없다(지적 2). 비용은 실측으로 감당 가능 |
| PG FTS OR + GIN으로 후보 200 | Tantivy BM25(블록 최대 WAND) top-200 | PG OR 질의 지연이 규모에 선형으로 늘었다 `[실측 M3, M4]`. 빈도 필터 우회는 후보를 임의로 잘라 재현율을 잃는다 `[실측 M4]` |
| D5 RRF 결합 + 재순위는 플래그 | 재순위 기본(`mode=full`), RRF는 후보 절단용 | 재순위 20건 206ms, 50건 484ms `[실측 M11]` |
| D9 에이전트가 `queries[]` 최대 4개 | 서버 결정적 확장 + 선택 `alt_queries` 2개 | 지적 5 |
| D6 정본 키 = 개념·측면·조건 | 명제 키에 극성·양상·유효시간 추가, 출처는 진술로 분리, facet 레지스트리 | 지적 6 |
| D10 kb 안의 수집기 어댑터 | collector가 수집, kb는 envelope 소비자 | 사용자 결정 2026-10-20 |
| `kb_sources` = 발행처(신뢰 등급) | 구현된 `kb_sources`는 원본 바이트 정체다(`kb_models.py:34-54`). 발행처는 새 `kb_publishers` | 설계와 구현의 차이를 바로잡는다 |
| 현재 리비전 = 문서별 최대 `rev_no` | 현재 리비전 = 관측 시각(`kb_sources.collected_at`) 최대, 동률이면 `rev_no` | 늦게 도착한 옛 스냅샷이 최신이 되는 문제(collector `architecture.md:138`) |

### 지적 2–6 해소

| 지적 | 해소 방법 | 남은 위험 |
|---|---|---|
| 2 패러프레이즈·동의어·교차 언어 | dense 기본(V2), 다국어 별칭 사전, 재순위. 유형별 게이트(paraphrase, cross_lingual Recall@20) | bge-m3 실측은 8쌍 스모크뿐이다. 실제 재현율은 KB-E0 이후 측정 |
| 3 지연·자원 계측 없음 | 하네스가 단계별 타이머, 프로세스 RSS·CPU 시간, GPU 메모리, 색인 디스크를 매 실행 기록. 호스트 부하를 같이 저장하고 부하가 높으면 지연 게이트를 판정하지 않음(4.6) | 호스트가 다른 Run과 공유라 지연 분산이 크다 |
| 4 수백만 규모가 추정뿐 | 10만·100만 건을 임시 클러스터에서 실측하고 500만은 외삽(5장). 실데이터 100만 건 시험을 KB13 게이트로 둠 | 합성 텍스트와 무작위 벡터로 쟀다. 어휘 크기 증가와 실벡터 HNSW 빌드는 `확인 필요` |
| 5 `queries[]` 재현성·RRF | V1, V5. 후보 경로별 기여를 trace에 남겨 "후보 누락"과 "순위 밖"을 구분 | 서버 확장 사전의 품질이 새 의존점이 된다 |
| 6 시간·극성·출처 분리 | V7, facet 레지스트리, 상태 전이 표(6장) | 추출 품질이 낮으면 명제 키가 잘게 쪼개져 중복이 는다. 병합 후보 큐로 대응 |

### 구현 착수 조건

이 문서가 독립 검토를 통과하면 KB-E0(평가 하네스)부터 시작한다. KB3 이후 단위는 9장의 게이트를 통과해야 다음으로 간다.

---

## 1. 측정 환경

| 항목 | 값 | 근거 |
|---|---|---|
| 실행 환경 | 이 Claude 세션의 Bash(호스트에서 실행, `claude` 엔진은 능력 표에서 미측정). host_gpu 도구는 쓰지 못했다. `StartRequest`에 `--embedding`·`--reranking` 플래그가 없고(`back/src/ttakkari/host_gpu.py:65-107`) 벤치는 텍스트 생성 전용이다. 대신 같은 llama-server 바이너리(`config.py:332`의 `llama-b11655`)를 /tmp 모델로 직접 띄웠다 | 부록 A |
| GPU 사용 증거 | 같은 모델 CPU 전용(`--device none`) 초당 6.1패시지, 기본 장치 초당 130패시지. 두 서버의 같은 입력 벡터 코사인 0.9995 | `[실측 M6, M8]` |
| 호스트 부하 | 측정 시작 시 CPU 76%, load avg 16.67, 스왑 사용 92.3%(14.8GiB/16GiB), 가용 메모리 23.5GB | MCP `server_resources` 2026-10-21 21:24 KST |
| DB | PostgreSQL 17.11(Homebrew), pgvector 0.8.7, pg_trgm 1.6. 임시 클러스터 `shared_buffers=1GB`, `maintenance_work_mem=4GB`, 병렬 유지보수 작업자 4 | `/tmp/kbv2/pg`, 부록 A |
| 모델 | `gpustack/bge-m3-GGUF` Q8_0(634,553,760 B), `gpustack/bge-reranker-v2-m3-GGUF` Q8_0(635,676,416 B). HF 카드 라이선스 표기 MIT, Apache-2.0 | HF API `cardData.license`. 원 BAAI 저장소 라이선스 원문은 `확인 필요` |
| 라이브러리 | `tantivy` 0.26.2(MIT), `kiwipiepy` 0.24.1 + `kiwipiepy_model` 0.24.0(Apache-2.0), macOS arm64 Python 3.13 휠 설치 성공 | `pip show` |

부하가 높은 상태에서 잰 값이라 지연은 보수 쪽으로 치우쳤을 가능성이 크다. 다만 이 호스트는 평소에도 여러 Run이 같이 돌기 때문에 이 조건이 운영 조건에 가깝다고 본다(판단, `확인 필요`).

---

## 2. 현재 상태와 v1 설계의 차이

### 2.1 구현된 kb 저장층(KB1·KB2)

| 요소 | 구현 | 근거 |
|---|---|---|
| `kb_sources` | 원본 바이트 정체. `UNIQUE(source_key, sha256)`, `store_path`, `collected_at`, `origin_meta` | `kb_models.py:34-54` |
| `kb_documents` | `doc_key` 유일, `title`, `lang`만 있는 가변 껍데기. domain·scope·status·발행처 없음 | `kb_models.py:57-69` |
| `kb_revisions` | `rev_no`, `parser_version`, `body`, `text_sha256`, `char_count`, `valid_from/to`, `supersedes_id`. INSERT 때 해시·길이 트리거 검증 | `kb_models.py:72-99,159-171` |
| `kb_passages` | `revision_id`, `ord`, `heading`, `body`, `char_start/end`, `sha256`. INSERT 때 리비전 슬라이스·해시 검증 | `kb_models.py:102-122,172-192` |
| `kb_evidence` | 패시지 구간 인용, 해시 검증 | `kb_models.py:125-142,193-206` |
| 불변성 | 4개 표 UPDATE/DELETE 거부, 5개 표 TRUNCATE 거부, FK RESTRICT | `kb_models.py:208-241`, Alembic 0055·0058 |
| 현재성 | 문서별 최대 `rev_no` | `kb_models.py:6` |
| 수집 함수 | 같은 `(source_key, sha256)`이면 parser_version과 무관하게 최신 리비전을 돌려준다. 원본 바이트를 NFKC로 decode해 본문을 만든다 | `kb_store.py:180-194,228` |
| 분할 | 빈 줄 기준 문단, heading 항상 None | `kb_store.py:244-291` |

### 2.2 collector 계약(0059·0060 구현, 0062 미커밋)

| 요소 | 내용 | 근거 |
|---|---|---|
| 작업 원장 | `collector_jobs` 멱등키 `(tenant_id, scope, source_key, request_key)`, `payload_hash`, 상태 10종 | `alembic/versions/0059_collector_intake.py:24-58` |
| outbox | `collector_outbox` `UNIQUE(tenant_id, sink, scope, envelope_id)`, 상태 pending/delivered/failed/delivery_unknown | `0059_collector_intake.py:60-91` |
| 실행 | cursor, 취소 요청, last_error, heartbeat | `0060_collector_job_runtime.py:20-27` |
| envelope | `urn:ttakkari:collector:ingestion-envelope:1.0.0`. 필수: source(key/kind/url/collected_at/published_at/valid_from/valid_to), access(tenant/scope/principal/sensitivity), rights, raw(sha/size/media_type/blob_handle), normalized(text/sha256/language/parser/normalizer/chunker/raw_alignment_handle/truncated), quality 6축, fact_check_status, dedupe, decision, errors, lineage, extensions | `docs/design/collector-platform/contracts/ingestion-envelope.schema.json` |
| 정규화 | NFC만. NFKC는 비교용으로만 쓰고 저장하지 않는다 | `collector/textproc.py:3,48,133-134` |
| 원본 저장 | `cblob:sha256:<hex>` 별도 내용 주소 저장소 | `collector/blobs.py:3,20,41-45` |
| 본문 상한 | 100만 자에서 자르고 `truncated` 표시 | `collector/parse.py:24,131` |
| 원문 정렬 맵 | 현재 `raw_alignment_handle: None` | `collector/pipeline.py:394` |
| 원본 보존 | 정책 허용 raw와 판정 이력 초기 30일 보존 제안, 총 20GiB 상한 | `collector-platform/architecture.md:96` |
| kb sink | S2-08 미구현. 현재 sink는 `export` | `collector-platform/stage-handler.md` "미검증·한계" |

### 2.3 v2가 해결해야 할 차이

| # | 차이 | v2 처리 |
|---|---|---|
| G1 | v1 설계의 `kb_sources`(발행처·신뢰 등급)와 구현(원본 바이트)이 다르다 | `kb_publishers` 신설(6.8) |
| G2 | `kb_documents`에 domain·scope·status·발행처·volatility가 없다 | 가변 표라 열 추가(7.4) |
| G3 | NFC(collector)와 NFKC(kb) 불일치 | V9 |
| G4 | 같은 원본을 새 파서로 다시 처리할 길이 없다(`kb_store.py:180-194`) | `ingest_revision(source_id, parser_version, body)` 경로 신설 |
| G5 | 원본 저장소가 둘이다(`kb/originals`, `cblob`) | kb는 collector 핸들을 참조하고 복사하지 않는다. 참조가 있는 blob은 collector 보존 정책으로 지우지 않는다(7.4) |
| G6 | collector 원본 30일 보존 제안과 kb 원본 영구 보존 원칙이 충돌한다 | kb 참조 = 고정(pin). 고정 해제는 kb 쪽 명시 이벤트로만 |
| G7 | 현재성이 도착 순서(`rev_no`)라 옛 스냅샷 재전송이 최신이 된다 | 관측 시각 기준(요약 표) |
| G8 | kb 행에 scope가 없다. collector는 personal/work/shared를 가진다 | `kb_documents.scope` 추가, 검색은 호출자 scope 집합으로 거른다 |

---

## 3. 검색 아키텍처 v2

### 3.1 기존 실패와 요구

기존 맥락 도서관의 실패(v1 2장): 조사 미분리(C1), IDF 없음(C2), 문장형 질의 전체 AND로 0건(C3), 이종 점수 혼합(C4), 프로젝트 가산 0.65(C5), 벡터 하한 0.80(C6). v2는 다음 네 원칙으로 이 실패를 막는다.

1. 후보를 만드는 단계에서 점수를 섞지 않는다. 각 경로는 자기 순위만 낸다.
2. 고정 가산점과 고정 유사도 하한을 두지 않는다. 문턱값은 평가 세트에서 보정한 재순위 점수 하나에만 둔다.
3. 같은 입력이면 같은 후보가 나온다. 서버 밖 모델이 만든 변형에 재현율을 기대지 않는다.
4. 어느 경로가 후보를 냈는지 trace에 남긴다. "원문 없음 / 후보 없음 / 후보 있으나 순위 밖 / 순위 안이나 조건 틀림"을 구분한다.

### 3.2 재현율 확보 방안 비교

| 방안 | 패러프레이즈 | 동의어·약어 | 한영 교차 | 희귀어·식별자 | 재현성 | 비용 | 판단 |
|---|---|---|---|---|---|---|---|
| A. 다국어 dense(bge-m3) 기본 | 강함 | 중간 | 강함(스모크 8/8 `[실측 M7]`) | 약함 | 결정적(같은 모델·양자화) | 색인 GPU 초당 130패시지, 질의 11ms `[실측 M6]`, 저장 100만 건당 halfvec 2.8GB + BQ 0.42GB `[실측 M9 외삽]` | **채택** |
| B. 서버 측 결정적 질의 확장 | 약함 | 사전 범위 안에서 강함 | 사전 범위 안에서 강함 | 영향 없음 | 결정적 | 사전 유지 비용 | **채택(보조)**. 형태소 원형화, 식별자 분해, 숫자·단위 정규화, 별칭 사전 조회만 한다. LLM으로 질의를 다시 쓰지 않는다 |
| C. 다국어 별칭 사전(개념 ko/en) | 없음 | 강함 | 강함(고유명사·전문어) | 강함 | 결정적 | 큐레이션. 초기에는 평가 코퍼스 범위 + Wikidata 레이블 덤프(CC0) 후보 | **채택(보조)**. B의 사전이 이것이다 |
| D. 에이전트 제공 `queries[]` | 중간 | 중간 | 중간 | 중간 | 낮음(모델·프롬프트마다 다름) | 호출 모델 토큰 | **선택 입력으로 강등**. 최대 2개, trace에 기록, 평가는 이것 없이 통과해야 함 |
| E. 서버 LLM 질의 재작성(HyDE 등) | 강함 | 강함 | 강함 | 약함 | 낮음 | 질의마다 LLM 호출 | **미채택**. 특정 LLM 비종속, 비용 비례 원칙과 충돌 |
| F. 학습형 희소(SPLADE, bge-m3 sparse) | 중간 | 중간 | 약함 | 중간 | 결정적 | 색인 추가 | **보류**. llama-server가 bge-m3 sparse·colbert 출력을 내는지 `확인 필요`. dense+BM25 결과가 게이트에 못 미칠 때 재검토 |

A만으로는 희귀어·코드 식별자·정확 수치 질의를 놓친다(dense 모델의 일반적 약점, 이 세션에서 측정하지 않음 `확인 필요`). 그래서 어휘 경로를 없애지 않고 병렬로 둔다. B·C는 교차 언어 고유명사(예: "다중 버전 동시성 제어" ↔ `MVCC`)처럼 dense가 놓칠 수 있는 짧은 질의를 보강한다.

### 3.3 파이프라인

```
질의 ─► [Q] 질의 해석(결정적)
          · 스크립트 분리(한글/라틴/코드), 언어 판별
          · Kiwi 분석(체언·용언 어간·외국어·숫자), 영문 소문자+Snowball, 식별자 분해
          · 의도 규칙(정의/비교/절차/시점/버전/부정/다중 홉)과 조건 추출(연도, 버전, 제품명)
          · 별칭 사전 조회 → 다른 언어 표기
        ─► [C] 후보 생성(재현율, 병렬)
          C1 어휘: Tantivy BM25, 원 질의 분석어, top 200
          C2 dense: bge-m3 질의 벡터 → BQ HNSW top 400 → halfvec 재채점 top 200
          C3 별칭 확장: 다른 언어 표기로 Tantivy 하위 질의, top 50
          C4 alt_queries(있을 때만): C1·C2를 같은 방식으로, 각 top 50
          C5 계층 확장(조건부, 3.4)
          C6 관계 확장(조건부, 3.4)
        ─► [F] 합집합 → 현재 리비전·scope·필터 검사(PG) → 동일 본문(sha) 접기
          → RRF(k=60)로 정렬해 상위 N(기본 40)만 남김
        ─► [R] 순위화(정밀도): bge-reranker-v2-m3 교차 인코더, 입력 = 질의 + [제목 > 절] + 패시지
          → 조건·시점 정합 검사(3.5) → 문서당 최대 2패시지
        ─► [A] 조립: claim·근거 span·출처·적용조건·신뢰 등급·검증 상태·충돌·최신성
          → coverage 판정(3.6) → 결과 계약(3.7)
```

`mode=fast`는 [R]을 건너뛰고 RRF 순서를 그대로 쓴다. 재순위 서버가 죽었을 때도 fast로 떨어지고 응답 `degraded`에 사유를 적는다.

### 3.4 기법별 사용 조건

| 기법 | 역할 | 언제 쓰나 | 언제 안 쓰나 |
|---|---|---|---|
| RRF | 경로별 순위를 하나의 후보 목록으로 합쳐 재순위 입력 크기(N)로 자른다. `fast`에서는 최종 순서 | 항상 [F] 단계 | 최종 점수나 coverage 판정에 쓰지 않는다. RRF 점수는 질의 간 비교가 안 된다 |
| 교차 인코더 재순위 | 상위권 정밀도, 조건 정합의 1차 신호, abstain 점수 | `mode=full`(기본). N은 40, p95 예산을 넘으면 24까지 줄인다 | 재순위 서버 불가, `fast` 요청 |
| 계층 탐색(문서→패시지) | 넓은 질의에서 문서 단위 문맥으로 찾고 그 안의 패시지를 끌어온다 | 의도가 정의·개요·비교이고 C1·C2 상위 결과가 여러 문서에 흩어졌을 때. 또는 C1·C2 상위 10의 재순위 최고점이 weak 구간일 때. 문서 단위 색인(제목+절 제목+첫 문단)에서 top 10 문서를 찾고, 문서마다 BM25+dense 상위 3패시지를 C5 후보로 넣는다 | 희귀어·식별자 질의(C1이 이미 정확), 사실 조회에서 strong |
| 관계 확장(1-hop) | 다중 홉·비교 질의에서 두 번째 개념의 근거를 끌어온다 | 의도가 다중 홉·비교이거나 `hops=1` 명시. 질의에서 찾은 개념의 정본 claim이 가리키는 목적 개념(object concept)의 claim 근거 패시지를 C6 후보로 넣는다. 최대 20패시지 | 가산점으로 쓰지 않는다(C5 회피). 2-hop 이상은 하지 않는다. v1의 오프라인 실험에서 1-hop·2-hop 이득이 0이었다(v1 0장 "사용자 지침과 다르게 간 부분" 3) |

### 3.5 질의 유형별 경로와 판정

| 유형 | 해석 신호 | 경로 차이 | 결과 판정 |
|---|---|---|---|
| 사실 조회 | 기본 | C1+C2+C3 | 재순위 |
| 정의 | "란", "뭐야", "정의", "what is" | + C5 | facet=`definition` claim이 있으면 앞에 둔다 |
| 비교 | "차이", "vs", "비교", "compare" | + C5, C6 | 두 대상 각각의 근거가 하나 이상 있어야 strong |
| 절차 | "방법", "어떻게", "how to", 명령형 | C1 비중 그대로, 패시지 길이 상한을 1,600자로 | 절 단위로 묶어 반환 |
| 시점 의존 | 연도, "현재", "최신", "as of", `as_of` 인자 | 동일 | `valid` 구간이 `as_of`를 포함하는 claim만 정본으로. 미지정이면 최신 구간. 시점 불명 claim은 `time_unresolved` 표시 |
| 버전 의존 | 제품명 + 버전 패턴, `conditions` 인자 | 동일 | 조건이 겹치지 않는 claim은 결과에서 내리지 않고 `condition_mismatch`로 표시하고 뒤로 보낸다 |
| 다중 홉 | 관계어 + 두 개념 | + C6 | 각 홉의 근거를 따로 붙인다. 한 홉이라도 근거가 없으면 weak |
| 패러프레이즈 | 해석 신호 없음(평가 유형) | C2가 주 경로 | 재순위 |
| 교차 언어 | 질의 언어 ≠ 원문 언어 | C2 + C3 | 인용은 원문 언어 그대로. 번역하지 않는다 |
| 부정형 | "아닌", "않는", "없는", "not", "except" | 동일 | 극성이 맞는 claim/패시지를 앞에. 재순위 모델이 부정을 잘 다루는지는 `확인 필요`라 유형별 게이트를 둔다 |
| 답 없음(abstain) | 해당 없음 | 동일 | coverage none(3.6) |
| 충돌 주장 | 같은 정본 묶음에 disputed claim | 동일 | 정본(있으면)을 앞에 두고 `conflicts`에 반대 claim과 근거를 함께 |

### 3.6 coverage와 abstain

- 점수원: 재순위 1위 점수 s1(원시 로짓). dense 코사인은 쓰지 않는다. bge-m3 스모크에서 정답 쌍 최소 0.545, 오답 쌍 최대 0.46으로 간격이 좁았다 `[실측 M7]`. 고정 코사인 하한은 C6과 같은 실패를 되풀이한다.
- 판정: `strong`은 s1 ≥ τ_strong이고 근거 span이 있을 때. `weak`는 τ_none ≤ s1 < τ_strong일 때. `none`은 s1 < τ_none이거나 후보가 0일 때.
- τ는 dev 세트에서 보정한다. τ_none은 abstain 질문의 coverage=none 재현율 0.8 이상을 만족하는 값 중 답 있는 질문의 잘못된 none 비율이 가장 낮은 값이다. τ_strong은 strong 결과의 근거 정확도 0.9 이상을 만족하는 최솟값이다. holdout에서 다시 확인한다. 모델이나 양자화가 바뀌면 다시 보정한다.
- `fast` 모드에는 재순위 점수가 없으므로 coverage를 `unknown`으로 낸다. 추측하지 않는다.

### 3.7 결과 계약

```json
{
  "trace_id": "…",
  "coverage": "strong | weak | none | unknown",
  "query": {
    "intent": "version_dependent",
    "languages": ["ko"],
    "detected_conditions": {"product": "postgresql", "version": "17"},
    "as_of": null,
    "expansions": [{"from": "격리 수준", "to": "isolation level", "via": "alias:pg:isolation"}],
    "alt_queries_used": 0
  },
  "results": [{
    "rank": 1,
    "kind": "claim | passage",
    "passage": {
      "id": "…", "heading_path": ["13. Concurrency Control", "13.2 Transaction Isolation"],
      "quote": "…근거 span 중심 400자 이내…",
      "span": {"start": 120, "end": 211, "sha256": "…"},
      "lang": "en"
    },
    "claim": {
      "id": "…", "statement": "…",
      "proposition": {"subject": "pg:transaction", "facet": "default_isolation_level", "value": "read committed",
                      "polarity": true, "modality": "fact",
                      "conditions": {"product": "postgresql", "version": ">=9 <18"},
                      "valid": {"from": null, "to": null}},
      "review_level": "corroborated", "state": "active", "canonical": true,
      "assertions": 3, "independent_sources": 2
    },
    "source": {
      "publisher": "PostgreSQL Global Development Group", "trust_tier": 1, "kind": "official_docs",
      "document": {"title": "…", "url": "…", "published_at": "…"},
      "revision": {"id": "…", "observed_at": "2026-10-20T…", "raw_sha256": "…", "current": true},
      "license": "PostgreSQL"
    },
    "verification": {
      "quote_verified": true,
      "claim_status": "verified | corroborated | unverified | none",
      "ai_generated_source": false
    },
    "applicability": {"match": "exact | partial | mismatch | unknown", "notes": []},
    "conflicts": [{"claim_id": "…", "value": "…", "reason": "value_differs | polarity_opposite | time_overlap",
                   "evidence": [{"passage_id": "…", "quote": "…"}]}],
    "freshness": {"volatility": "medium", "last_verified_at": "…", "stale": false},
    "why": {"paths": ["lex:2", "dense:5", "alias:1"], "rrf_rank": 3, "rerank": 7.42}
  }],
  "degraded": [],
  "timings_ms": {"analyze": 3, "lex": 8, "embed": 11, "dense": 4, "fuse": 2, "rerank": 290, "assemble": 15},
  "budget": {"candidates": 312, "reranked": 40, "omitted": 0}
}
```

규칙:
- 근거 span이 없는 claim은 반환하지 않는다. 패시지 결과는 원문 인용이라 `quote_verified=true`지만 claim 해석은 없으므로 `claim_status=none`이다. 에이전트는 이를 "원문 근거, 해석은 미검증"으로 전달한다.
- `claim_status=unverified`(candidate)는 `include_unverified=true`일 때만 나온다.
- 근거가 T5(AI 생성) 출처뿐이면 `ai_generated_source=true`이고 claim_status는 unverified를 넘지 못한다.
- 응답 크기 상한 32KB. 넘으면 낮은 순위부터 quote를 줄이고 `budget.omitted`에 센다. 전문은 `kb_get`으로 가져온다.
- 예시의 PostgreSQL 값은 형식 설명용이며 사실 검증을 하지 않았다.

### 3.8 지연·자원 예산

| 단계 | 실측 근거 | 100만 패시지 예산(초기) |
|---|---|---|
| 질의 해석(Kiwi) | 문단당 약 1.2ms(초당 804문단, 평균 470자) `[실측 M12]` | 5ms |
| C1 어휘 | Tantivy 100만 p50 7.6ms, p95 10.6ms `[실측 M5]` | 20ms |
| 질의 임베딩 | p50 11.0ms, p95 14.8ms `[실측 M6]` | 30ms |
| C2 dense | 10만 BQ+재채점 p50 3.2ms, p95 4.5ms `[실측 M9]`. 100만은 `확인 필요` | 30ms |
| C3·C4 | C1·C2 반복 | 각 20ms |
| [F] PG 필터·접기 | 미측정 `확인 필요` | 30ms |
| [R] 재순위 40건 | 20건 206ms, 50건 484ms `[실측 M11]`에서 보간 약 390ms | 450ms |
| [A] 조립 | 미측정 `확인 필요` | 50ms |
| **합계 p95** | | **full 800ms, fast 200ms** |

GPU 메모리: 측정 중 임베딩 서버 RSS 594MB, 재순위 서버 RSS 330MB(`ps`, 부록 A). 운영 Qwen 서버와 같은 GPU를 쓴다. 측정 시점 스왑 사용이 92%라 상주 프로세스 두 개를 추가할 때 메모리 압박이 실제 위험이다(10장 R3).

### 3.9 투영(projection) 운영

- 정본은 PostgreSQL이다. Tantivy 색인, 벡터 표, 문서 단위 색인은 모두 다시 만들 수 있는 투영이다.
- `kb_passage_analysis(passage_id, analyzer_version, tokens text)`를 PG에 저장한다. Kiwi 분석은 비싸고(100만 패시지 약 30분/스레드 `[실측 M12 외삽]`) Tantivy 재구축은 싸다(100만 11.4초 `[실측 M5]`). 분석 결과를 PG에 두면 색인 손상 시 수십 초 안에 복구된다.
- `kb_index_log(seq bigserial, passage_id, op, created_at)`: 패시지 추가·현재성 변경·scope 변경을 기록한다. 투영 작업자는 `kb_projection_state(projection, build_id, last_seq)`까지 따라잡는다.
- Tantivy는 쓰기 주체가 하나여야 한다. 배포 세대가 셋까지 동시에 돌 수 있으므로 쓰기는 `generation.work` 안의 `kb-project` 작업 하나만 한다(collector-consume과 같은 패턴, `collector-platform/architecture.md:74`). 모든 세대는 읽기 전용으로 열고 커밋 시점에 reload한다.
- 색인 디렉터리: `data_dir/kb/index/lex/<analyzer_version>/<build_id>/`. 새 빌드가 끝나면 `kb_projection_state`의 활성 build_id를 원자적으로 바꾼다. 이전 빌드는 다음 빌드가 평가를 통과할 때까지 둔다.
- 임베딩·재순위 llama-server는 백엔드 세대가 자식 프로세스로 띄우는 기존 desk_qwen 방식(`config.py:318`)을 따르되 호스트당 하나만 둔다. 세대 전환 때 둘이 뜨지 않게 하는 소유 규칙은 구현 단위 KB6에서 정한다(`확인 필요`: 기존 desk_qwen_service가 세대 중첩을 어떻게 처리하는지 이번에 읽지 않았다).

---

## 4. 평가 하네스(KB3보다 먼저)

### 4.1 구성

```
kb-eval/
  corpora/<corpus_id>/manifest.json   # 문서 목록, 원본 sha256, 라이선스, 수집 시각, 버전
  gold/<set_id>/{dev,holdout}.jsonl   # 질문, 유형, 분야, 정답 span, 등급, 조건, 검증 기록
  systems/                            # raw_literal, legacy_lexical, bm25_kiwi, +dense, +alias, +rerank, full
  runs/<run_id>/                      # 결과, 지표, 지연, 자원, 호스트 부하, 코드 SHA, 모델 해시
```

- 하네스는 제품 코드 경로(`kb_search` 서비스 함수)를 그대로 호출한다. 평가 전용 검색 구현을 따로 만들지 않는다.
- 재사용: `knowledge/evaluation.py`의 `validate_split`(11), `score`(19), `paired_interval`(69), `bm25`(91). 새로 필요한 것은 nDCG 등급, 근거 정확도, abstain, 유형별 집계, 자원 계측이다.
- 실행 하나는 코퍼스 manifest 해시, gold 해시, 코드 SHA, 분석기·모델·양자화 해시, 색인 build_id를 기록한다. 하나라도 다르면 비교하지 않는다.

### 4.2 코퍼스와 라이선스

| 분야 | 자료 | 라이선스(확인 근거) | 쓰는 유형 |
|---|---|---|---|
| 개발 | PostgreSQL 16·17 공식 문서 | PostgreSQL License(`확인 필요`: 문서 저장소 COPYRIGHT 원문) | 버전 의존, 희귀어, 절차, 교차 언어(한국어 질문) |
| 개발 | Python 3.12·3.13 문서 | PSF License 계열(`확인 필요`: docs.python.org 저작권 표기 원문) | 버전 의존("Changed in version"), 정의 |
| 과학 | OpenStax Biology 2e, Chemistry 2e 일부 장 | CC BY 4.0(`확인 필요`: 각 책 표지 표기) | 정의, 패러프레이즈, 절차 |
| 과학 | BEIR SciFact | HF 카드 CC BY-SA 4.0. 원 데이터셋은 다른 표기일 수 있어 `확인 필요` | 충돌 주장(supports/refutes 라벨) |
| 경제·사회 | World Bank Open Data 지표 설명과 연도별 값 | CC BY 4.0(`확인 필요`) | 시점 의존, 충돌 주장(기관별 추정치) |
| 경제·사회 | 한국은행 경제용어 해설, KOSIS 지표 설명 | 공공누리 유형 `확인 필요`. 확인 전에는 쓰지 않는다 | 정의(한국어), 시점 |
| 역사·사회·상식 | 한국어·영어 위키백과 문서 쌍 | CC BY-SA 4.0 | 교차 언어, 다중 홉, 부정형 |
| 상식·교차 언어(공개 세트) | MIRACL ko | HF 카드 Apache-2.0. 사람이 판정한 관련성 라벨이 있음 | 사실 조회, 기준선 |
| 교차 언어(공개 세트) | XOR-TyDi QA(ko 질문 → 영어 위키백과) | HF 카드 MIT | 교차 언어 |
| 교차 언어(공개 세트) | MKQA(ko 포함) | HF 카드 CC BY 3.0 | 교차 언어, abstain(답 없음 표기 문항) |
| 한국어 독해 | KLUE-MRC | HF 카드 CC BY-SA 4.0 | 패러프레이즈(질문-문단 어휘 겹침이 낮은 문항) |
| 제외 | KorQuAD 1.0 | HF 카드 CC BY-ND 4.0. 질문을 고치거나 번역하면 2차 저작물이 되므로 원형 그대로만 참고용으로 쓰고 게이트에는 넣지 않는다 | |

HF 카드 표기(`/tmp/kbv2/lic.py` 실행 결과)는 원 라이선스 문서를 대신하지 않는다. KB-E0 완료 조건에 각 자료의 라이선스 원문 URL과 확인 시각을 manifest에 기록하는 일을 넣는다. 재배포 금지 자료는 저장하되 outbox·공유로 내보내지 않는다.

코퍼스 크기: 분야별 문서 300–500개, 합계 패시지 약 5만(`확인 필요`, 수집 후 확정). 여기에 MIRACL ko 코퍼스 일부를 방해 문서(distractor)로 섞어 10만 패시지 평가판을 만든다. 규모 시험(KB13)은 위키백과 덤프로 100만 패시지까지 늘린다.

### 4.3 질문 유형과 수량

| 유형 | 정의 | 분야별 목표(dev/holdout) |
|---|---|---|
| fact | 단일 사실 조회 | 3/3 |
| definition | 개념 정의 | 2/2 |
| comparison | 두 대상 차이 | 2/2 |
| procedure | 방법·절차 | 2/2 (개발·과학 중심) |
| temporal | 시점에 따라 답이 다름 | 2/2 |
| version | 적용 버전에 따라 답이 다름 | 2/2 (개발 중심, 다른 분야는 "개정판·법 개정") |
| multi_hop | 두 문서 이상 결합 | 2/2 |
| paraphrase | 원문 핵심어를 쓰지 않은 질문 | 3/3 |
| cross_lingual | 질문 언어 ≠ 원문 언어 | 3/3 |
| negation | 부정·예외 조건 | 2/2 |
| abstain | 코퍼스에 답 없음 | 3/3 |
| conflict | 출처끼리 다른 값 | 2/2 |

분야 4개 × 유형 12개 × 위 수량으로 자체 gold는 dev 112, holdout 112, 합계 224문항이다. 공개 세트는 MIRACL ko dev와 XOR-TyDi ko dev에서 각각 200문항을 고정 시드로 뽑는다(문항 수 원본 확인 `확인 필요`). 224문항의 Recall@10 95% CI 폭은 약 ±0.06으로 본다(이항 근사, 판단). 유형별 셀(분야 합쳐 8–12문항)은 CI가 넓어서 유형 게이트는 "하락 금지"만 판정하고 절대값은 전체 지표로만 판정한다.

### 4.4 정답 세트 제작 절차

LLM이 만든 문제를 LLM이 채점하는 순환을 끊는 것이 목적이다.

1. **표본 추출(기계)**: manifest의 패시지에서 분야·유형별로 고정 시드 무작위 추출한다. 검색 시스템 결과로 고르지 않는다.
2. **질문 작성**: 사람이 쓰거나 LLM이 초안을 쓴다. 초안에는 `drafted_by` 모델 ID를 남긴다.
3. **정답 span 기계 검증**: 정답마다 `(passage_sha256, start, end, span_sha256)`을 붙인다. 하네스가 동결 코퍼스에서 다시 잘라 해시를 맞춘다. 안 맞으면 문항을 버린다.
4. **유형 기계 검사**
   - paraphrase: 질문 분석어와 정답 패시지 분석어의 겹침 비율이 0.3 이하.
   - cross_lingual: 질문 언어 판별 결과 ≠ 정답 패시지 언어.
   - negation: 질문에 부정 표지가 있고 정답 라벨에 `polarity`가 있음.
   - abstain: 질문 핵심어(희귀 분석어 상위 3개)가 코퍼스에 나오는 패시지를 모두 모아 사람이 답 없음을 확인. 이 목록을 문항에 첨부한다.
   - temporal·version: 정답에 `as_of`나 `conditions`가 있고, 다른 시점·버전의 오답 패시지를 하나 이상 함께 라벨함.
5. **관련성 풀링**: 모든 기준 시스템의 top 20을 합친 풀에서 등급(0–3)을 매긴다. 검색 결과로 정답을 새로 정하지 않고, 풀 안의 다른 관련 패시지를 추가로 인정하는 데만 쓴다.
6. **사람 확인**
   - holdout 112문항은 사용자가 전부 확인한다. 확인 UI(8.3의 평가 화면)는 질문, 정답 span 하이라이트, 풀 상위 10, 유형 검사 결과를 한 화면에 보여 주고 "승인/수정/폐기"만 받는다. 문항당 1분 안팎으로 잡으면 약 2시간이다(`확인 필요`).
   - dev는 만든 세션과 다른 세션의 독립 검토로 확인하고 `verified_by=agent`로 표시한다. dev는 τ 보정과 개발 중 확인에만 쓰고 게이트에 쓰지 않는다.
7. **분리**: 같은 문서에서 나온 문항은 같은 group이고 group 단위로 dev/holdout을 나눈다. `validate_split`이 겹침을 거부한다. holdout 결과를 보고 시스템을 고친 횟수를 `holdout_peeks`로 센다. 3회를 넘으면 holdout을 새로 만든다.
8. **LLM 판정의 위치**: 근거 정확도 같은 판정에 LLM을 쓸 수 있지만 `judge=llm`으로 따로 집계하고 게이트 지표에 섞지 않는다. 검색 대상 모델과 같은 모델 계열을 판정자로 쓰지 않는다.

### 4.5 지표

| 지표 | 정의 | 비고 |
|---|---|---|
| Recall@k (k=10, 20) | 질문별 관련 패시지(등급 ≥1) 중 top k에 든 비율의 평균 | 후보 단계(RRF 후 N)와 최종 단계를 따로 잰다. 후보 Recall@N이 재현율 지표다 |
| nDCG@10 | 등급 0–3, 이상적 순위 대비 | 최종 순위 |
| MRR@10 | 첫 관련 결과 순위의 역수 | |
| 근거 정확도 | top 3 결과의 quote가 질문의 답을 실제로 지지하는 비율 | 사람 판정 표본 50건/게이트. 기계 근사: 정답 span과 quote 구간 겹침 |
| 조건 정확도 | temporal·version 질문에서 1위의 조건·시점이 맞는 비율 | |
| 충돌 표시율 | conflict 질문에서 반대 claim이 `conflicts`에 나온 비율 | claim층(KB10) 이후 |
| abstain 정확도 | abstain 질문의 coverage=none 재현율, 답 있는 질문의 잘못된 none 비율, 둘의 F1 | |
| 지연 | 단계별·전체 p50/p95, 모드별(fast/full) | 4.6 |
| 자원 | 질의당 CPU 시간, 프로세스 RSS 최대, GPU 메모리, 색인 디스크, 색인 처리량(패시지/초) | 4.6 |

모든 품질 지표는 전체·분야별·유형별로 집계하고, 비교는 같은 질문 쌍에서 paired bootstrap 95% CI(`paired_interval`)로 낸다.

### 4.6 지연·자원 측정 방법

- **지연**: 서비스 함수 안에서 `time.perf_counter_ns()`로 단계별 구간을 잰다(3.7 `timings_ms`). 하네스는 같은 질문 집합을 워밍업 1회 후 3회 반복하고 반복별 p50/p95와 그 중앙값을 낸다. 동시성 1과 4 두 조건을 잰다.
- **CPU**: 백엔드 프로세스와 llama-server 자식의 `psutil.Process.cpu_times()` 차이를 실행 전후로 기록하고 질문 수로 나눈다.
- **메모리**: 같은 프로세스들의 RSS를 100ms 간격으로 표본해 최대값을 기록한다.
- **GPU**: 기존 `server_resources`가 쓰는 수집기의 GPU 메모리 사용·할당값을 실행 전, 중(최대), 후로 기록한다. 장치별 활용률은 macOS에서 권한 없이 얻기 어려워 `확인 필요`로 두고, 대신 llama-server `/metrics`의 처리 토큰과 처리 시간을 기록한다.
- **디스크**: `pg_total_relation_size`(표·색인별)와 Tantivy 디렉터리 `du`, 원본 저장소 크기.
- **호스트 부하 기록**: 실행 전후 load average, CPU%, 스왑 사용, 동시에 돌던 Run 수를 저장한다. load average가 논리 코어 수(18)의 절반을 넘으면 그 실행의 지연 지표는 "참고"로 표시하고 지연 게이트 판정에 쓰지 않는다. 품질 지표는 부하와 무관하므로 그대로 쓴다.
- **재현성 검사**: 같은 run을 두 번 돌려 결과 순위가 완전히 같아야 한다. 다르면 비결정 요인(병렬 재채점 동률, GPU 수치 차이)을 찾을 때까지 게이트를 열지 않는다. GPU 수치 차이로 동률 순서가 바뀌는 경우에 대비해 동률은 passage_id로 정렬한다.

### 4.7 회귀 게이트

단계별 진입 게이트는 9장에 있다. 출시 후 검색 경로를 건드리는 모든 변경에 적용하는 상시 게이트는 다음과 같다.

| 항목 | 기준 |
|---|---|
| holdout 전체 Recall@10, nDCG@10 | 직전 기준 대비 차이의 95% CI 하한 ≥ −0.02 |
| 분야별 Recall@10 | 어느 분야도 점추정 하락 0.05 초과 금지 |
| 유형별 | abstain F1, 조건 정확도는 점추정 하락 0.05 초과 금지 |
| 공개 세트(MIRACL ko, XOR-TyDi ko) Recall@20 | CI 하한 ≥ −0.02 |
| 지연 | full p95 +20% 초과 금지, 절대 800ms 이하(부하 조건 충족 실행에서만) |
| 자원 | RSS 최대 +20% 초과 금지, 색인 디스크 +30% 초과 시 사유 기록 |
| 재현성 | 같은 run 두 번의 순위 일치 100% |

### 4.8 실패 분류

질문마다 자동 분류해 run 결과에 남긴다.

1. `not_in_corpus`: 정답 패시지가 색인에 없음(수집·분할 실패).
2. `no_candidate`: 어느 후보 경로에도 없음. 경로별로 몇 위였는지 trace에서 남긴다.
3. `cut_by_fusion`: 후보에는 있었으나 RRF 절단(N) 밖.
4. `ranked_out`: 재순위 후 top k 밖.
5. `wrong_condition`: top k 안이나 조건·시점이 틀린 claim이 앞섬.
6. `false_abstain` / `false_answer`.

1–3은 재현율 문제, 4–5는 정밀도 문제다. 개선 작업은 어느 칸을 줄이는지 먼저 밝힌다.

---

## 5. 규모 계획

### 5.1 실측

모두 `/tmp/kbv2` 임시 클러스터. 합성 패시지는 한글 음절 조합 6만 개와 영문 무작위 단어 6만 개로 된 12만 어휘를 Zipf(지수 1.05)로 뽑아 만들었다. 패시지 평균 688자, 1,038바이트, 리비전당 10패시지. 벡터는 무작위 1024차원이다.

| ID | 측정 | 10만 패시지 | 100만 패시지 |
|---|---|---|---|
| M1 | 리비전 COPY(해시·길이 트리거 포함) | 1만 리비전 2.1초, 81MB | 10만 리비전 22.0초, 807MB |
| M2 | 패시지 COPY(슬라이스·해시 트리거, tsv 생성열) | 8.6초 | 85.7초 |
| M2 | 패시지 표 크기(heap / toast / tsv GIN / 전체) | 156MB / 237MB / 43MB / 446MB | 1,558MB / `측정 안 함` / 314MB / 4,332MB |
| M2 | GIN 빌드, term 통계 생성 | 3.7초, 1.7초 | 49.2초, 19.0초 |
| M3 | PG `ts_rank_cd` OR 8단어 top 200 | p50 560ms, p95 942ms | p50 5,687ms, p95 8,809ms (20문항) |
| M4 | PG 빈도 필터(df/N ≤ 1%) + LIMIT 2000 + 앱 BM25 | p50 5.8ms, p95 20.4ms, 후보 p50 486 | p50 23.3ms, p95 103.2ms, 후보 p50 2000(상한 도달) |
| M5 | Tantivy 색인(분석된 토큰 입력) / 크기 / OR 8단어 top 200 | 1.4초 / 43MB / p50 0.83ms, p95 1.17ms | 11.4초 / 363MB / p50 7.55ms, p95 10.6ms |
| M9 | halfvec(1024) 표 | 276MB, 적재 16초 | `측정 안 함` |
| M9 | full HNSW(m=16, ef_c=64) | 빌드 404초, 260MB, ef 100 top 100 p50 8.3ms, p95 15.0ms | `측정 안 함` |
| M9 | BQ HNSW(`binary_quantize(v)::bit(1024)`) | 빌드 23초, 42MB. 후보 200 → halfvec 재채점 top 50 p50 3.2ms, p95 4.5ms | `측정 안 함` |
| M9 | IVFFlat(lists=316) | 빌드 46초, 265MB | `측정 안 함` |
| M9 | full HNSW 있는 상태 증분 INSERT | 1,000행 20.4초(행당 20ms) | |

| ID | 측정(실제 텍스트·모델) | 결과 |
|---|---|---|
| M6 | bge-m3 Q8_0 GPU 처리량(실제 `back/docs` 문단 1,813개, 평균 470자, 평균 230토큰) | 14.0초, 초당 130패시지, 초당 29,918토큰. 질의 1건 p50 11.0ms, p95 14.8ms |
| M7 | 한국어 질의 ↔ 영어 패시지 8쌍 | top-1 8/8, 정답 코사인 최소 0.545·평균 0.673, 오답 최대 0.46·평균 0.307 |
| M8 | bge-m3 CPU 전용(6스레드) | 초당 6.1패시지. GPU 대비 벡터 코사인 0.9995 |
| M10 | 실제 bge-m3 벡터 1,815개, BQ 해밍 후보 C개 → 원 벡터 재채점 top 10의 정확 top 10 일치율 | C=40 0.931, C=100 0.989, C=200 0.998 |
| M11 | bge-reranker-v2-m3 Q8_0, 질의 1 × 문서 n(700자) | n=20 206ms, n=50 484ms, n=100 965ms(중앙값). 8쌍 스모크 top-1 8/8 |
| M12 | Kiwi 0.24.1 단일 스레드 | 초당 377,373자(804문단). `AIP가 앱별 백엔드에서…` → `AIP, 앱, 백, 엔드, 줄이, 반복, 작업, 원칙, 처리` |

측정 한계:
- 합성 어휘가 12만으로 고정이다. 실제 코퍼스는 규모에 따라 어휘가 늘어 GIN과 Tantivy 색인이 더 커진다(`확인 필요`).
- 무작위 텍스트는 실제 텍스트보다 압축이 안 된다. TOAST 크기는 실제보다 클 가능성이 있다(`확인 필요`).
- HNSW 빌드는 무작위 벡터로 쟀다. 실제 임베딩은 군집이 있어 빌드 특성이 다르다(`확인 필요`).
- M10은 같은 코퍼스 안의 문서-문서 근접 일치율이다. 질문-정답 검색 재현율이 아니다. 규모가 커지면 C=100의 일치율이 떨어질 수 있다(`확인 필요`).
- M12에서 `백엔드`가 `백`+`엔드`로 쪼개졌다. 사용자 사전(별칭 사전에서 생성)이 필요하다는 실측 근거다.
- 부하가 높은 호스트에서 쟀다(1장).

### 5.2 규모별 계산

100만 건 값은 M1–M5 실측, 벡터는 10만 실측의 선형 외삽, 500만 건은 100만 실측의 선형 외삽이다. 외삽값은 모두 `확인 필요`다.

| 항목 | 10만 | 100만 | 500만 |
|---|---|---|---|
| 리비전 본문(PG) | 81MB 실측 | 807MB 실측 | 4.0GB |
| 패시지 표(PG, tsv·GIN 포함) | 446MB 실측 | 4.3GB 실측 | 21.7GB |
| 패시지 분석 토큰(`kb_passage_analysis`) | 약 0.1GB | 약 1GB | 약 5GB (토큰 열 크기 추정, `확인 필요`) |
| halfvec 표 | 276MB 실측 | 2.8GB | 13.8GB |
| BQ HNSW | 42MB 실측 | 0.42GB | 2.1GB |
| (대안) full HNSW | 260MB 실측 | 2.6GB | 13GB |
| Tantivy | 43MB 실측 | 363MB 실측 | 1.8GB |
| **PG+투영 합계(BQ 선택)** | **약 1.0GB** | **약 9.7GB** | **약 48GB** |
| 원본 blob | `확인 필요`(HTML 평균 크기에 좌우, collector 총 상한 20GiB 제안) | | |
| WAL·백업 여유 | 합계의 2배를 디스크 여유로 잡는다 | | |

v2에서는 PG `tsv` 생성열과 GIN을 두지 않는다(어휘 경로가 Tantivy로 감). 그만큼 패시지 표가 줄지만 얼마나 줄지는 측정하지 않았다(`확인 필요`). 위 합계는 줄이지 않은 값이다. 데이터 볼륨 여유 356GiB(`df -h /tmp`, 2026-10-21)에서 500만 건도 디스크는 문제 없다. 메모리가 먼저 문제다. BQ HNSW 2.1GB와 Tantivy 1.8GB는 OS 캐시에 올라야 지연 예산을 지킨다.

### 5.3 인덱스 선택

| 대상 | 선택 | 이유 |
|---|---|---|
| 어휘 | Tantivy(임베디드, Rust 라이브러리의 Python 바인딩) | M3·M5. 별도 서버가 아니라 백엔드 프로세스 안 라이브러리라 "분산 인프라 금지"와 맞는다 |
| 어휘 대안 | ParadeDB `pg_search` | PG 안에서 BM25를 하므로 정합성이 단순하다. Homebrew PG17에 설치 가능한지 `확인 필요`. 가능하면 KB5에서 Tantivy와 같은 하네스로 비교한다 |
| dense | pgvector BQ HNSW + halfvec 재채점 | M9·M10. 빌드 17배 빠르고 색인 6배 작다 |
| dense 대안 | IVFFlat | 빌드는 BQ HNSW보다 느리고(46초 대 23초) 크기는 full과 비슷하다. 쓰지 않는다 |
| 문서 단위 | Tantivy 별도 색인(제목+절 제목+첫 문단) + 문서 대표 벡터 BQ | 계층 탐색(3.4) |
| 메타데이터 필터 | PG B-tree(`kb_documents(scope, domain, status)`, `kb_revisions(document_id)`) | 후보 수백 개를 PG에서 거르는 데 충분 |
| claim 조건 | GIN `(conditions jsonb_path_ops)`, `(subject_concept_id, facet)` | v1 유지 |

### 5.4 쓰기 비용(패시지 100만 건 신규 적재 기준)

| 단계 | 시간 | 근거 |
|---|---|---|
| 리비전+패시지 PG 적재(트리거 포함, COPY 방식) | 약 1.8분 | M1·M2. 실제 적재 경로는 ORM 행 단위라 더 느리다(`확인 필요`). 배치 INSERT(500행)로 구현한다 |
| Kiwi 분석 | 약 30분/스레드, 4프로세스면 약 8분 | M12 외삽 |
| Tantivy 색인 | 약 12초 | M5 |
| bge-m3 임베딩 | 약 2.1시간(GPU) | M6 외삽. 500만은 약 10.7시간 |
| BQ HNSW | 새로 빌드하면 10만 23초에서 외삽해 100만 약 4분(로그 인자 고려 시 더 길 수 있음 `확인 필요`). 증분은 full HNSW 행당 20ms보다 빠를 것으로 보나 미측정 | M9 |

증분 수집(하루 수천 건)은 위 비용이 무시할 수준이다. 대량 적재(위키백과 덤프 등)는 다음 순서를 지킨다. ① PG 적재, ② 분석, ③ Tantivy 새 build, ④ 임베딩 백필 작업(재개 가능, cursor), ⑤ 벡터 색인 새로 빌드, ⑥ 평가, ⑦ 활성 build 전환. 임베딩 백필이 끝나기 전에는 새 패시지의 dense 경로가 비어 있으므로 응답 `degraded`에 `dense_backfill_pending`을 적는다.

### 5.5 재색인 절차

| 바뀌는 것 | 절차 | 전환 조건 |
|---|---|---|
| 분석기 버전 | 새 `analyzer_version`으로 `kb_passage_analysis` 전체 재생성(백그라운드) → 새 Tantivy build | 하네스 holdout 상시 게이트(4.7) 통과 |
| 임베딩 모델·양자화 | 새 `model_id`로 `kb_passage_vectors_<model_id>` 표 생성, 백필, `CREATE INDEX CONCURRENTLY` | 같은 게이트 + τ 재보정 |
| 재순위 모델 | 서버 교체만. 색인 변화 없음 | 같은 게이트 + τ 재보정 |
| 분할기 버전 | 새 parser/chunker 버전의 리비전·패시지를 새로 만든다(옛 행은 불변으로 남음). 근거가 옛 패시지를 가리키는 claim은 유지된다 | 같은 게이트 |
| Tantivy 색인 손상 | `kb_passage_analysis`에서 재구축(100만 약 12초) | 무결성 검사 통과 |

모든 전환은 `kb_projection_state`의 활성 포인터를 바꾸는 한 트랜잭션이다. 되돌리기는 포인터를 이전 build로 되돌린다.

### 5.6 재검토 신호

- Tantivy 100만 p95가 20ms를 넘거나, 쓰기 작업자의 따라잡기 지연이 5분을 넘으면 `pg_search` 또는 색인 분할(segment 정책)을 비교한다.
- 500만 건에서 BQ HNSW가 OS 캐시에 상주하지 못해 dense p95가 30ms를 넘으면 문서 단위 1차 필터 후 패시지 탐색(계층 색인)으로 바꾼다.
- `kb_passages` 단일 표 VACUUM이 1시간을 넘으면 `revision_id` 해시 파티션을 검토한다.
- 호스트 메모리 압박(스왑 증가)으로 재순위 서버가 종료되는 일이 주 1회를 넘으면 재순위 N을 줄이거나 CPU 대체 경로 없이 `fast`를 기본으로 바꾼다.

---

## 6. 정본 모델 v2

### 6.1 v1 키의 문제(지적 6)

v1 정본 묶음은 `(subject_concept, facet, conditions)`였다. 다음 경우가 한 묶음에 섞인다.

- 시점: "2015년 최저임금 5,580원"과 "2024년 최저임금 9,860원"(값 예시는 형식 설명용, 검증 안 함). valid time이 키에 없으면 충돌로 오탐한다.
- 극성: "X는 Y를 지원한다"와 "X는 Y를 지원하지 않는다"가 다른 버전 조건 없이 들어오면 같은 키에서 값만 다르게 보인다.
- 출처 의존: IMF와 세계은행의 같은 해 성장률 추정은 서로 틀린 것이 아니라 다른 추정이다.
- 다중값: "공용어"처럼 값이 여러 개인 측면은 값이 달라도 충돌이 아니다.

### 6.2 명제와 진술

| 개념 | 표 | 의미 |
|---|---|---|
| 명제(claim) | `kb_claims` | 세계에 대한 하나의 주장. 누가 말했는지와 무관한 내용 |
| 진술(assertion) | `kb_claim_assertions` | 특정 출처의 특정 리비전이 그 명제를 지지·반박·한정한다는 기록. 근거 span(`kb_evidence`)을 가리킨다 |
| 정본 묶음(group) | `kb_canonical_groups` | 같은 질문에 대한 경쟁 명제의 묶음 |
| facet | `kb_facets` | 측면의 형식과 충돌 규칙 |

### 6.3 식별자

```
proposition_key = sha256(JCS({
  subject: concept_key,
  facet: facet_key,
  object_or_value: 정규화 값(facet 값 형식에 따라: 개념 키, 숫자+단위 SI 환산, 날짜 ISO, 정규화 문자열),
  polarity: true|false,
  modality: fact|definition|estimate|opinion|interpretation|prediction,
  conditions: 정규화 조건(키 정렬, 버전은 범위 표기 정규화),
  attribution: 양상이 estimate/opinion/interpretation/prediction이면 발화 주체 개념 키, 아니면 null,
  valid: {from, to}  // 모르면 null. 수집 시각으로 채우지 않는다
}))

group_key = sha256(JCS({subject, facet, conditions, attribution}))
```

- JCS는 collector가 이미 구현한 순수 Python RFC 8785 구현(`ttakkari.collector.contracts.jcs`, `collector-platform/intake-binding.md:33`)을 공유한다.
- 출처는 명제 키에 넣지 않는다. 같은 사실을 여러 출처가 말하면 명제 하나에 진술이 여러 개다(독립 확인).
- 시점은 명제 키에 넣고 묶음 키에는 넣지 않는다. 시점이 다른 값은 같은 묶음의 다른 시간 구간이다.
- 극성은 명제 키에 넣는다. 반대 극성 명제는 같은 묶음에서 충돌 후보가 된다.
- 발화 주체는 묶음 키에 넣는다. IMF 추정과 세계은행 추정은 서로 다른 묶음이라 충돌하지 않는다. 대신 검색 결과 `conflicts`가 아니라 `alternatives`로 함께 보여 준다.

### 6.4 facet 레지스트리

| 열 | 의미 |
|---|---|
| `key` | 예: `default_isolation_level`, `population`, `capital`, `founded_date`, `definition`, `supports_feature` |
| `value_type` | concept / number_unit / date / string / boolean / text |
| `cardinality` | single(시점·조건별 값 하나) / multi |
| `tolerance` | 숫자 비교 허용 오차(상대값)와 반올림 규칙 |
| `time_bound` | true면 valid time 없는 명제를 `time_unresolved`로 둔다 |
| `volatility` | static / slow / medium / fast(최신성 주기, v1 4.6 유지) |
| `status` | active / candidate. 추출기가 새 facet을 제안하면 candidate로만 생긴다 |

facet은 처음에 평가 코퍼스 범위에서 사람이 정한다(KB9). 추출기가 레지스트리에 없는 facet을 내면 claim은 `facet_unregistered`로 candidate에 머문다. facet 이름을 자유 문자열로 두면 같은 측면이 여러 이름으로 흩어져 충돌 탐지가 무력해진다(기존 `ctx_relations` 자유 술어의 한계, v1 1.2).

### 6.5 정본 선정

묶음 안에서 시간 구간을 나눈다. 각 구간마다 다음 순서로 하나를 고른다.

1. `state=active`이고 그 구간을 덮는 명제만.
2. `review_level` 높은 순(verified > corroborated > span_checked). candidate는 정본이 될 수 없다.
3. 지지 진술의 최고 출처 등급(T1이 가장 높음). T5 진술은 세지 않는다.
4. 독립 출처 수(발행처가 다르고 근사 중복이 아닌 진술).
5. facet `volatility`가 medium·fast면 최근 검증 시각.
6. 동률이면 기존 정본 유지.

`cardinality=multi`인 facet은 정본이 하나가 아니라 "정본 집합"이다. 선정 결과와 항목별 값은 `kb_canonical_selections`(append-only)에 남긴다. 사용자가 지정한 정본(`selected_by=user`)은 정책 재계산이 덮지 않고, 새 근거로 바뀌어야 할 상황이면 검토 큐에 올린다.

### 6.6 상태 전이

두 축을 분리한다. `review_level`은 근거가 얼마나 확인됐는지, `state`는 명제가 지금 유효한지다. 최신성(stale)은 계산값이고 상태가 아니다.

**review_level**

| 전이 | 조건 | 주체 |
|---|---|---|
| (생성) → candidate | 진술 하나 이상, 근거 span 해시 일치(DB 트리거) | 추출기(규칙·로컬 모델·에이전트 batch) |
| candidate → span_checked | 추출자와 다른 주체가 원문 대조로 "quote가 명제를 지지함" 확인 | 에이전트(다른 Run) 또는 사용자 |
| span_checked → corroborated | 독립 출처 2개 이상의 지지 진술, 그중 하나가 T1 또는 T2 | 시스템 자동 계산 |
| corroborated → verified | 사용자 확인, 또는 사용자가 위임한 분야에서 T1 원문 직접 대조 | 사용자 API |
| 어느 수준 → 한 단계 이상 강등 | 근거 진술이 `evidence_lost`·`withdrawn`으로 빠져 조건을 못 채움 | 시스템 |

**state**

| 전이 | 조건 | 주체 |
|---|---|---|
| (생성) → active | 기본 | 시스템 |
| active → disputed | 같은 묶음, 겹치는 조건과 시간 구간에서 cardinality=single인데 허용 오차 밖 값이 있거나 반대 극성. 상대 명제가 span_checked 이상일 때만(candidate끼리는 충돌로 올리지 않고 검토 큐만) | 시스템 |
| disputed → active | 조건·시간 분리로 해소, 상대 명제가 retracted·superseded, 또는 사용자 판정 | 시스템·사용자 |
| active·disputed → superseded | 같은 출처의 새 리비전이 같은 묶음에서 다른 값을 말하고 옛 진술이 사라짐(정정), 또는 사용자가 새 명제로 대체 지정. `superseded_by` 기록 | 시스템(같은 출처 정정만)·사용자 |
| 어느 상태 → retracted | 모든 지지 진술 소멸(출처 철회, 404/410 후 재수집 불가, 논문 철회), 또는 사용자가 오류로 판정 | 시스템·사용자 |
| superseded, retracted | 종결. 다시 살리지 않는다. 필요하면 새 명제를 만든다 | |

- 소프트웨어의 "deprecated"(기능 폐기 예고)는 상태가 아니라 사실이다. `facet=deprecation_status`, 조건 `version>=X`인 명제로 저장한다.
- 모든 전이는 `kb_claim_events`(append-only, `UNIQUE(claim_id, version)`)에 행위자·사유·이전·이후 값과 함께 같은 트랜잭션으로 기록한다. 기록 시간 질의("지난달 kb는 무엇을 정본으로 알았나")는 이 이벤트를 재생해 계산한다.
- AI 생성(T5) 출처의 진술만 있는 명제는 candidate를 넘지 못한다. NotebookLM 답변은 인용한 원 소스를 수집해 그 span을 진술 근거로 써야 한다(v1 D7 유지).

### 6.7 충돌 탐지

새 명제나 진술이 들어올 때 같은 `group_key`만 본다(인덱스 조회 한 번).

1. 시간 구간이 겹치는가. 한쪽이 null이면 facet `time_bound=true`일 때 `time_unresolved`로 표시하고 충돌로 올리지 않는다.
2. 조건이 겹치는가. 버전 범위는 교집합, 지역·제품은 같음 비교.
3. facet `cardinality=single`에서 값이 허용 오차 밖인가, 또는 극성이 반대인가.
4. 셋 다 참이면 충돌 레코드(`kb_conflicts`, 두 명제 ID·사유·탐지 시각)를 만들고 6.6 전이를 적용한다.
5. 해소 시도 순서는 v1 4.4를 따른다(조건 분리 → 시간 분리 → 단위·반올림 정규화). 자동 해소는 정규화(단위·반올림)만 하고, 조건·시간 분리는 새 명제를 만드는 일이라 검토 큐로 보낸다.

### 6.8 스키마 초안

기존 표(0053–0058)는 고치지 않는다. 열 추가와 새 표만 한다.

| 표 | 주요 열 | 불변성 |
|---|---|---|
| `kb_publishers` | id, key(예 `site:postgresql.org`), name, kind, trust_tier 1..5, tier_reason, license_default, homepage | 가변. 변경은 `kb_publisher_events`에 기록 |
| `kb_documents` 열 추가 | publisher_id, canonical_url, domain, scope(personal/work/shared), status(active/gone/withdrawn), volatility, published_at, trust_tier_override, duplicate_of | 가변 껍데기(기존과 같음) |
| `kb_sources` 열 추가 | store_kind(kb/collector), envelope_id | 불변. ADD COLUMN DEFAULT는 UPDATE 트리거를 부르지 않는다(`확인 필요`: 0055 트리거가 ALTER에 반응하지 않음을 마이그레이션 테스트로 확인) |
| `kb_revisions` 열 추가 | normalization(nfkc-raw-v1 / producer-nfc-v1), producer_text_sha256, truncated, chunker_version | 불변 |
| `kb_intake_receipts` | envelope_id, adapter_version, payload_hash, result, revision_id, created_at. `UNIQUE(envelope_id, adapter_version)` | append-only |
| `kb_passage_analysis` | passage_id, analyzer_version, tokens | 불변(버전별 새 행) |
| `kb_passage_vectors_<model>` | passage_id, vec halfvec(1024) | 투영 |
| `kb_index_log`, `kb_projection_state` | 3.9 | 로그는 append-only |
| `kb_concepts`, `kb_concept_aliases` | v1 3절 유지 + alias `lang`, `source`(curated/wikidata/extracted), `status` | 가변 + 이벤트 |
| `kb_facets` | 6.4 | 가변 + 이벤트 |
| `kb_claims` | id, proposition_key unique, group_key, subject_concept_id, facet_key, value_json, object_concept_id, polarity, modality, attribution_concept_id, conditions, valid_from, valid_to, statement, review_level, state, version, 캐시(best_tier, independent_sources, last_verified_at) | 명제 열 불변(트리거), 상태·캐시만 이벤트와 같은 트랜잭션에서 갱신 |
| `kb_claim_assertions` | id, claim_id, evidence_id, stance(supports/refutes/qualifies), revision_id, publisher_id, asserted_at(출처 발행 시각), extractor(id, version, prompt_sha), status(active/evidence_lost/withdrawn) | 행 불변, status 변경은 이벤트 |
| `kb_claim_events` | v1과 같음 | append-only |
| `kb_canonical_groups`, `kb_canonical_selections` | group_key, 구간, claim_id(들), selected_by, score jsonb | 선택 기록 append-only, 현재 포인터는 가변 |
| `kb_conflicts` | id, group_key, claim_a, claim_b, reason, detected_at, resolved_at, resolution | 해소는 이벤트 |
| `kb_extraction_queue` | passage_id, extractor_version, priority, reason(query_hits/user/eval), budget_reservation_id, status | 운영 |
| `kb_search_traces` | trace_id, query 해시, 경로별 후보 순위, 최종 결과, 지연, 모드 | 보존 90일 |

---

## 7. 수집 플랫폼 계약과 kb 경계

### 7.1 책임 분담

| 책임 | collector | kb |
|---|---|---|
| 탐색·fetch·robots·SSRF·권리 정책 | 소유 | 관여 안 함 |
| 원본 bytes 저장 | 소유(`cblob`) | 참조·고정(pin) |
| 본문 추출·정규화(NFC)·언어·근사 중복 | 소유 | 결과를 그대로 받음 |
| 품질 6축·factuality 신호 | 소유 | 순위·수용 판단의 참고 신호로만. 근거로 쓰지 않는다 |
| 작업 원장·재시도·예산 | 소유(S2-05 예산 bucket) | 추출 예산도 같은 bucket을 쓴다 |
| 수용(intake) 검증 | outbox 전달 | **kb 어댑터 소유** |
| 발행처·신뢰 등급 | `kb:source_policy` 확장으로 힌트 전달 | 확정(사람이 정한 publisher 표) |
| 패시지 분할 | envelope의 `chunker_version`은 collector 내부용 | **kb 소유**. 검색 단위라 평가와 함께 바뀌어야 한다 |
| 분석·색인·검색 | 없음 | 소유 |
| claim 추출·정본·충돌 | 없음 | 소유 |

### 7.2 envelope → kb 매핑

| envelope | kb |
|---|---|
| `source.key` | `kb_documents.doc_key` |
| `source.url` | `kb_documents.canonical_url`(정규화는 collector `netguard` 결과) |
| `source.collected_at` | `kb_sources.collected_at`(현재성 판단 기준) |
| `source.published_at` | `kb_documents.published_at`(문서 수준), 진술 `asserted_at` |
| `source.valid_from/to` | `kb_revisions.valid_from/to` |
| `access.scope`(collector가 인증 주체에서 산출) | `kb_documents.scope` |
| `rights.permission`, `purposes`, `license_id`, `retain_until` | 수용 판단, `kb_sources.origin_meta.rights`. store 미허용이면 수용 거부 |
| `raw.sha256`, `byte_size`, `media_type`, `blob_handle` | `kb_sources.sha256`, `byte_size`, `media_type`, `store_path`(=핸들), `store_kind=collector` |
| `normalized.text`, `sha256`, `language`, `parser_version`, `normalizer_version` | `kb_revisions.body`, `producer_text_sha256`(=`text_sha256`), `kb_documents.lang`, `parser_version`, `normalization=producer-nfc-v1` |
| `normalized.truncated` | `kb_revisions.truncated`. 검색 결과에 `partial_document` 표시 |
| `dedupe.exact_of`, `cluster_id` | `kb_documents.duplicate_of` 후보. 자동 병합 안 함 |
| `decision.state` | `accept`만 수용. `hold`·`error`는 수용 안 함 |
| `quality.*`, `fact_check_status` | `kb_sources.origin_meta.quality`. 정본·승격 계산에 쓰지 않는다 |
| `extensions["kb:normalized_doc"]` | 제목, 저자, 절 구조, 버전 힌트 → 분할기 입력, `applies` |
| `extensions["kb:source_policy"]` | publisher 매핑 힌트. trust_tier는 publisher 표가 이긴다 |
| `lineage` | `origin_meta.lineage` |

### 7.3 수용 검증(kb 어댑터)

순서대로 하나라도 실패하면 `kb_intake_receipts.result`에 사유를 남기고 outbox에 실패를 돌려준다. 부분 수용은 없다.

1. schema major가 1인지. 아니면 `unsupported_schema`.
2. `decision.state=accept`, `rights.permission`에 store 허용.
3. `sha256(UTF-8(normalized.text)) == normalized.sha256`. 텍스트에 NUL 없음(`kb_store.py`의 기존 NUL 거부 규칙 재사용).
4. `normalized.text`가 NFC 고정점인지(`unicodedata.is_normalized("NFC", text)`). 아니면 생산자 계약 위반으로 거부.
5. `blob_handle`이 존재하고 `raw.sha256`·`byte_size`와 맞는지(BlobStore port로 확인). 원본 보존 불가(`unpreserved:` 핸들, `stage-handler.md` 흐름 3)면 수용은 하되 `kb_sources.origin_meta.raw_preserved=false`로 표시하고 이 리비전의 진술은 corroborated 계산에서 "원본 재검증 불가"로 표시한다.
6. `kb:*` 확장은 kb 쪽 strict validator로 검사한다. 모르는 확장 버전은 거부(`contract-compatibility.md:37`).
7. 멱등: `(envelope_id, adapter_version)`이 이미 있으면 같은 `payload_hash`일 때 기존 결과를 돌려주고, 다르면 거부.
8. 현재성: 같은 문서의 기존 리비전보다 `collected_at`이 이르면 리비전은 저장하되 현재가 되지 않는다(G7).
9. 한 트랜잭션에서 source·revision·passages·analysis·index_log·receipt를 쓴다. 임베딩은 index_log를 보고 투영 작업자가 비동기로 한다.

### 7.4 기존 코드와의 차이와 필요한 변경

| 항목 | 변경 | 위치 |
|---|---|---|
| NFC/NFKC(G3) | envelope 경로는 NFC 본문 저장. 기존 `kb-ingest` CLI 경로(`normalize_text`, NFKC)는 `normalization=nfkc-raw-v1`로 남긴다. 분석기가 두 경우 모두 NFKC+casefold를 색인 단계에서 적용한다 | `kb_store.py:61-63`, 새 어댑터 |
| 재처리(G4) | `ingest_revision(source_id, parser_version, normalization, body)` 신설. 같은 원본·같은 parser_version이면 멱등 | `kb_store.py` |
| blob 이중화(G5·G6) | kb는 `cblob:` 핸들을 참조한다. collector 보존 정리 작업은 `kb_sources.store_path`에 있는 핸들을 지우지 않는다(참조 검사). `kb-verify`는 BlobStore port로 두 저장소를 모두 확인한다 | `collector/blobs.py`, `kb_store.verify_integrity` |
| 현재성(G7) | 현재 리비전 조회를 `ORDER BY kb_sources.collected_at DESC, rev_no DESC`로 바꾼다. 불변 행을 건드리지 않는다 | `kb_store.py`, 모델 docstring `kb_models.py:6` |
| scope(G8) | `kb_documents.scope`. 검색은 호출 세션의 허용 scope 집합(personal 세션: personal+shared, work 세션: work+shared)으로 거른다 | 새 마이그레이션, `kb_search` |
| 본문 상한 | collector가 100만 자에서 자른다(`parse.py:24`). kb는 truncated 리비전을 받되 표시한다 | |
| 원문 정렬 맵 | collector가 `raw_alignment_handle=None`(`pipeline.py:394`). 원본 바이트 하이라이트는 범위 밖으로 둔다(v1 6단계 2 유지) | |

### 7.5 claim 추출 소유와 LLM 비용 통제

**소유: kb.** 명제 키, facet 레지스트리, 정본, 충돌은 모두 kb의 의미 모델이다. collector가 추출하면 collector가 kb 모델을 알아야 해서 collector ADR의 "지식 모델을 import하지 않는다"(`collector-platform/architecture.md:43`)를 어긴다.

**LLM 호출이 수집량에 비례하지 않게 하는 규칙**

1. **검색은 claim 없이 된다.** 패시지 우선이라 수집한 문서는 claim 추출 없이 바로 검색된다. 추출은 품질을 높이는 선택 단계다.
2. **결정적 추출기 먼저.** LLM 없이 구조에서 뽑는다. 예: Python 문서의 `Changed in version X` 블록, 릴리스 노트 표, 위키백과 정보상자, World Bank 지표 표, 용어 사전의 표제어-정의 형식. 출처 형식별 추출기는 결정적이고 비용이 0이며 결과는 candidate다.
3. **수요 기반 큐.** LLM 추출 대상은 `kb_extraction_queue`에 들어간 패시지뿐이다. 들어가는 경우는 셋이다. ① 최근 30일 실제 `kb_search` 응답 top 3에 n회 이상 나온 패시지(기본 n=2), ② 사용자가 요청한 문서, ③ 평가 gold의 정답 패시지. 수집만 된 패시지는 들어가지 않는다.
4. **캐시.** 추출 결과는 `(passage.sha256, extractor_version, prompt_sha)`로 캐시한다. 같은 본문은 다른 문서·리비전에 있어도 다시 추출하지 않는다.
5. **절대 예산.** collector S2-05 예산 bucket에 `kb-extract` 항목을 두고 호출 전에 선예약한다(`collector-platform/architecture.md:86-92`). 일 한도를 넘으면 큐가 기다린다. 한도를 강제할 수 없는 엔진은 자동 추출에 쓰지 않는다(같은 문서 90행).
6. **로컬 모델 우선.** 1차 추출기는 호스트의 Qwen3.5-4B(llama-server, `config.py:313`)다. 금액은 0이지만 GPU 시간을 쓰므로 같은 큐와 일 처리 상한을 둔다. 로컬 모델의 추출 품질은 `확인 필요`이고 KB10 게이트에서 잰다.
7. **결과는 candidate뿐.** 어떤 추출기든 결과는 candidate다. 승격은 6.6 규칙(다른 주체의 원문 대조, 독립 출처, 사용자)으로만 한다.

결과: 추출 LLM 호출 수 ≤ min(질의로 드러난 고유 패시지 수 + 사용자 요청 + gold, 일 예산). 수집량 N이 10배가 돼도 이 상한은 변하지 않는다.

---

## 8. 에이전트 인터페이스와 UI

### 8.1 MCP 도구

| 도구 | 입력 | 출력 | 권한 |
|---|---|---|---|
| `kb_search` | `query`(필수), `alt_queries?` 최대 2, `intent?`, `as_of?`, `conditions?`, `domain?[]`, `k`(기본 6, 최대 20), `mode`(full/fast, 기본 full), `include_unverified`(기본 false), `hops`(0/1) | 3.7 결과 계약 | 읽기, 자동 승인 |
| `kb_get` | `id`(passage/document/revision/claim/concept), `history?`, `full_text?` | 상세, 리비전 이력, 진술·근거·반박, 충돌, 이벤트 | 읽기 |
| `kb_evidence` | `passage_id`, `start?`, `end?` | 원문 구간, 해시, 리비전·원본 정보, 앞뒤 문맥 | 읽기 |
| `kb_ingest` | `url` 또는 `file`(세션 파일), `reason`, `domain?` | collector job ID(sink=kb) | 쓰기. collector `POST /jobs`를 세션 scope로 호출하는 얇은 래퍼. 권리 정책이 등록된 host만 성공 |
| `kb_ingest_status` | `job_id` | collector 단계 카운터 + kb 수용 결과(receipt) | 읽기 |
| `kb_extract_batch` / `kb_submit_claims` | 큐에서 bounded batch(최대 20패시지·20,000자) / 명제 후보 + 정확한 quote | 수락·거부 사유 | 쓰기(candidate만). 예산 예약 필요 |
| `kb_review` | `claim_id`, `decision`(span_checked/disputed), `expected_version`, `reason` | 새 version | 에이전트는 span_checked·disputed까지. 추출한 Run과 같은 Run은 거부 |

정본 수동 지정, verified 승격, publisher 등급 변경, facet 등록은 사용자 API(UI)만 한다.

### 8.2 자율 사용 정책

도구 설명과 비서 지침(`~/.ttakkari/harness/instructions.md`, 한 줄)에 넣는다. 매 Run 자동 주입은 하지 않는다(v1 D8 유지).

**호출한다**
- 사용자가 일반 지식(개발, 과학, 경제, 역사, 사회, 상식)에 대해 출처나 정확한 값을 원할 때.
- 버전·시점에 따라 답이 달라지는 기술 사실을 말하려 할 때(예: 특정 버전의 기본값, API 변경).
- 에이전트가 기억에 의존한 답을 쓰려는데 그 내용이 검증 가능한 공개 사실일 때.

**호출하지 않는다**
- 개인 맥락, 프로젝트 기록, 사용자 선호: `recall`을 쓴다. kb와 섞지 않는다.
- 저장소 코드로 바로 확인할 수 있는 것.
- 같은 세션에서 같은 질문의 결과를 이미 받았을 때.
- 잡담, 창작, 의견 요청.

**결과 처리**
- `coverage=strong`: 근거를 인용해 답한다. `claim_status`가 unverified·none이면 "원문 근거는 있으나 해석은 미검증"이라고 밝힌다.
- `weak`: 쓸 수 있는 부분만 쓰고 부족하다고 밝힌다. 한 번 더 부를 때는 `alt_queries`나 `conditions`를 바꾼다. 같은 질문에 최대 2회.
- `none`: 외부 검색으로 넘어간다. 찾은 자료가 T1–T3 출처이고 다시 쓸 가치가 있으면 `kb_ingest`를 부른다. 검색 결과마다 수집하지 않는다. Run당 `kb_ingest` 최대 3건.
- `conflicts`가 있으면 양쪽을 모두 보여 주고 정본이 있으면 정본임을 밝힌다.
- `stale=true`면 그 사실을 말하고, 중요하면 외부 확인 후 `kb_ingest`로 새 리비전을 넣는다.

### 8.3 기본 UI(`/knowledge`)

collector의 `/collect`, 개인 `/context-library`와 독립된 루트다(`collector-platform/architecture.md:111`).

| 탭 | 내용 |
|---|---|
| 검색 | 질의창, 필터(분야, 조건, 시점, 검증 수준, mode). 결과 카드: 인용문 하이라이트, 출처·등급 배지, 검증 배지(검증됨/확인됨/미검증/원문만), 적용 조건 일치 표시, 충돌·대안·stale 배지, 경로 설명(why). 하단에 trace 펼치기 |
| 문서 | 문서 목록(발행처, 분야, scope, 상태). 상세: 리비전 타임라인(관측 시각, 현재 표시), 리비전 간 diff, 패시지 목록, 원본 메타·해시, collector job·receipt 링크 |
| 주장 | 명제 상세: 명제 필드, 진술 목록(출처·stance·근거 span), 이벤트 이력, 정본 여부와 선정 점수, 충돌·대체 관계 |
| 검토 | 큐 4종: candidate 승격 대기, 충돌, evidence_lost, stale. 항목마다 원문 대조 화면과 결정 버튼(낙관적 version) |
| 평가 | 최근 run 표(전체·분야·유형 지표, CI), 지연·자원, 게이트 통과 여부, 실패 분류 칸별 개수. gold 확인 화면(4.4의 6) |
| 설정 | publisher 등급, facet 레지스트리, 별칭 사전, 추출 예산·큐 상태 |

모든 화면은 empty·loading·error·partial·권한 없음 상태를 갖고, 320/390/768px와 1440px에서 확인한다. 모바일에서는 검색과 검토만 1차 동선으로 둔다(사용자가 주로 휴대폰으로 본다).

---

## 9. 구현 계획 v2

### 9.1 원칙

- 단위마다 additive 마이그레이션, 기능 플래그 `kb_enabled`(검색), `kb_intake_enabled`(collector sink), `kb_extract_enabled`(추출).
- 단위마다 완료 → 독립 검증(새 컨텍스트, 작성자와 다른 세션) → 게이트 판정 → 다음 단위. 게이트 미달이면 다음 단위로 가지 않는다.
- 검증 순서는 좁은 테스트 → 전용 테스트 DB 전체 회귀 → 하네스 run → 실제 MCP 호출이다. 구조 구현 완료와 검색 품질 입증을 같은 상태로 보고하지 않는다.
- 배포는 사용자 상시 정책을 따른다.
- **담당자 제약**: 아래 표의 "핵심"으로 표시한 단위(검색 경로, 원장·불변식, 명제·정본, 수용 검증, 평가 지표 계산)는 Flash 같은 경량 모델에 맡기지 않는다. 최상위 모델이 작성하고 다른 세션이 독립 검토한다. 경량 모델은 UI 레이아웃(계약 확정 후), 데이터 변환 스크립트(기계 검사 동반), 문서 정리에만 쓴다. gold 라벨은 어떤 모델에게도 최종 판정을 맡기지 않는다(4.4).

### 9.2 단위

| 코드 | 작업 | 선행 | 핵심 |
|---|---|---|---|
| KB-E0 | 평가 하네스 골격: manifest·gold 형식, 지표, 지연·자원 계측, 호스트 부하 기록, 재현성 검사, 기준 시스템 `raw_literal`·`legacy_lexical` | 없음 | 핵심 |
| KB-E1 | 평가 코퍼스 수집(collector 경유 또는 동결 스냅샷), 공개 세트 로더(MIRACL ko, XOR-TyDi ko), 자체 gold 224문항 제작과 사람 확인 | KB-E0 | 핵심(라벨), 변환 스크립트는 경량 허용 |
| KB3 | 분석기: Kiwi(+사용자 사전), 영문 Snowball, 식별자 분해, NFKC+casefold, 버전 기록 | KB-E0 | 핵심 |
| KB4 | kb 계약 보강 마이그레이션(6.8의 열 추가, publishers, receipts, analysis, index_log) + `ingest_revision` + 현재성 규칙 변경 | KB3 | 핵심 |
| KB5 | collector `kb` sink 어댑터(7.3) + 분할기 v2(절 구조, 300–1,200자, 짧은 절 병합) | KB4, collector S2-08 계약 | 핵심 |
| KB6 | 어휘 투영(Tantivy 쓰기 작업자, 재구축 CLI) + 추론 서버 소유(bge-m3, reranker llama-server 자식) + dense 투영(BQ HNSW, 백필 작업) | KB5 | 핵심 |
| KB7 | `kb_search` v2 파이프라인(3.3), coverage 보정, 결과 계약, trace | KB6 | 핵심 |
| KB8 | MCP 도구(`kb_search`, `kb_get`, `kb_evidence`, `kb_ingest`, `kb_ingest_status`) + 도구 정책 + 자율 사용 지침 | KB7 | 핵심(정책 문구), 등록 코드는 일반 |
| KB9 | 개념·별칭·facet 레지스트리 + 시드(평가 코퍼스 범위, Wikidata 레이블 후보) + 별칭 확장 경로 C3 + Kiwi 사용자 사전 생성 | KB7 | 핵심 |
| KB10 | 명제·진술·이벤트·정본·충돌 표 + 결정적 추출기 + 수요 기반 큐 + 추출 batch MCP + `kb_review` | KB9, collector S2-05 | 핵심 |
| KB11 | 최신성(review_due, stale), 감시 출처 재수집(collector 스케줄 사용) | KB10 | 일반 |
| KB12 | UI `/knowledge`(8.3) | KB8(검색·문서), KB10(주장·검토) | UI는 경량 허용, 상태·권한 로직은 일반 |
| KB13 | 규모 시험: 위키백과 기반 실데이터 100만 패시지, 합성 500만 패시지(저장·쓰기·지연만) | KB7 | 핵심 |

권장 순서: KB-E0 → KB-E1(코퍼스 준비는 KB3과 병행) → KB3 → KB4 → KB5 → KB6 → KB7 → KB8 → KB13 → KB9 → KB10 → KB11 → KB12(검색·문서 화면은 KB8 직후 먼저 가능).

### 9.3 단위별 완료 기준, 독립 검증, 게이트

**KB-E0 평가 하네스**
- 완료 기준: `ttakkari kb-eval run --system <s> --gold <set>`가 지표·지연·자원·부하·해시를 run 디렉터리에 쓴다. `legacy_lexical`(기존 `context/search.py`의 `lexical()`을 kb 패시지에 적용)과 `raw_literal` 기준선이 돈다.
- 독립 검증: 손으로 계산한 작은 예제(질문 3개, 패시지 10개)에서 Recall·nDCG·MRR·abstain 값이 일치. 결과를 일부러 섞은 변이에서 지표가 떨어짐. 자원 필드 null 없음. 같은 run 두 번 순위 일치.
- 게이트: 위 검증 전부 통과. 품질 기준값은 없다(기준선 기록이 목적).

**KB-E1 코퍼스·gold**
- 완료 기준: 4분야 manifest(라이선스 원문 URL·확인 시각 포함), 공개 세트 2종 고정 표본, 자체 gold dev 112·holdout 112. 모든 정답 span 해시 검증 통과. 유형 기계 검사 통과. holdout 사람 확인 100%.
- 독립 검증: 다른 세션이 표본 30문항을 원문과 대조해 span·유형·등급 오류율을 낸다. 오류율 10% 초과면 해당 유형 재작업.
- 게이트: holdout 사람 확인 완료, `validate_split` 통과, 기준선 2종의 지표 기록.

**KB3 분석기**
- 완료 기준: 골든 테스트(조사 제거 `AIP가 → aip`, 복합어 결합형·분해형, 혼합 스크립트, 코드 식별자, NFKC 호환문자, 부정 표지 보존). 분석기 버전 문자열.
- 독립 검증: 골든 기대값을 작성자가 아닌 세션이 원문 기준으로 검토. Kiwi 처리량 재측정(목표 단일 스레드 초당 30만 자 이상, M12 기준).
- 게이트: 하네스에서 `bm25_kiwi`(임시 메모리 BM25, 하네스 내부 구현)가 `legacy_lexical` 대비 holdout Recall@10 CI 하한 > 0. 희귀어 포함 문항 Recall@10 ≥ 0.80(설계 예산).

**KB4 계약 보강**
- 완료 기준: 마이그레이션 왕복, 불변 트리거가 새 열 추가 후에도 UPDATE·DELETE를 막음, `ingest_revision` 멱등, 현재성 규칙(늦게 도착한 옛 스냅샷이 현재가 안 됨) 테스트.
- 독립 검증: 불변식 반례 테스트를 다른 세션이 작성(새 열 경유 우회, 같은 원본 다른 parser 동시 수집, TRUNCATE).
- 게이트: 기존 `ctx_*`·`knowledge_*`·`kb_*`·collector 테스트 회귀 0.

**KB5 수용 어댑터·분할기**
- 완료 기준: 7.3 순서 1–9 각각의 양성·음성 fixture. collector outbox → kb receipt 왕복. 분할 길이 분포 300–1,200자 비율 ≥ 90%, 제목만 있는 패시지 0.
- 독립 검증: 다른 세션이 envelope 변조 fixture(해시 불일치, NFC 아님, 다른 scope, hold 결정, 모르는 kb 확장, 재전송)를 만들어 거부 확인. 입력 수 = 수용 + 거부 + 보류 검산.
- 게이트: KB-E1 코퍼스 전체가 이 경로로 들어가고 `not_in_corpus` 실패 0.

**KB6 투영·추론 서버**
- 완료 기준: Tantivy 쓰기 작업자(단일 소유, 세대 drain 존중), 재구축 CLI, `kb_projection_state` 전환, 임베딩 백필(재개 가능), BQ HNSW, 재순위 서버. 서버 상주 메모리·GPU 메모리 기록.
- 독립 검증: 세대 두 개 동시 실행에서 쓰기 주체가 하나인지(파일 잠금 경합 테스트). 색인 삭제 후 재구축 결과 순위가 같음. BQ 재채점 top-10 일치율을 KB-E1 코퍼스 실벡터로 재측정.
- 게이트: BQ 재채점 일치율(C=200) ≥ 0.97. 어휘 경로 p95 ≤ 20ms, dense 경로 p95 ≤ 30ms(KB-E1 코퍼스 규모, 부하 조건 충족 run).

**KB7 검색 v2**
- 완료 기준: 3.3 전 단계, fast/full, degraded 처리, coverage 보정(dev), 결과 계약 JSON Schema 검증, trace.
- 독립 검증: 다른 세션이 holdout을 한 번만 돌린다(`holdout_peeks` 기록). 실패 분류 칸별 사례 10개를 원문으로 확인.
- 게이트(holdout, 사람 확인 세트 기준, 모두 설계 예산):
  - 전체 Recall@10 ≥ 0.80, nDCG@10 ≥ 0.65, MRR@10 ≥ 0.60.
  - paraphrase·cross_lingual 후보 Recall@40(재순위 입력) ≥ 0.85, 그리고 `bm25_kiwi` 대비 Recall@20 CI 하한 > +0.10.
  - 다른 유형은 `bm25_kiwi` 대비 점추정 하락 0.02 초과 금지.
  - XOR-TyDi ko Recall@20, MIRACL ko nDCG@10은 기록하고 `bm25_kiwi` 대비 CI 하한 > 0.
  - abstain F1 ≥ 0.80, 근거 정확도(사람 표본 50) ≥ 0.90, 조건 정확도 ≥ 0.80.
  - full p95 ≤ 800ms, fast p95 ≤ 200ms.

**KB8 MCP**
- 완료 기준: 도구 5종, 정책 등록, REST·MCP 목록 일치, 자율 사용 지침 문구.
- 독립 검증: 실제 Run에서 일반 지식 질문 5개와 개인 맥락 질문 5개를 줘서 kb·recall 선택이 맞는지 기록(실행 증거). 응답 크기 ≤ 32KB.
- 게이트: 오선택 1건 이하. 개인 질문에서 kb 호출 0.

**KB13 규모 시험**
- 완료 기준: 실데이터 100만 패시지(위키백과 ko·en 일부 + KB-E1 코퍼스), 합성 500만 패시지 저장·적재.
- 독립 검증: 5장 실측표를 실데이터 값으로 갱신. 외삽과 30% 이상 차이 나는 항목은 원인 기록.
- 게이트: 100만에서 같은 holdout Recall@10 하락 ≤ 0.03(방해 문서 증가 효과), full p95 ≤ 1.0s, 어휘 p95 ≤ 30ms. 500만 합성에서 어휘 p95 ≤ 60ms, dense p95 ≤ 60ms, 디스크 합계가 5.2 계산의 ±30% 안.

**KB9 개념·별칭·facet**
- 완료 기준: 표, 시드, C3 경로, Kiwi 사용자 사전 생성.
- 독립 검증: 별칭 시드 표본 50개를 원문(위키데이터 레이블·공식 용어집)과 대조.
- 게이트: cross_lingual 짧은 질의(3어절 이하) Recall@10이 KB7 대비 CI 하한 ≥ 0, 점추정 +0.03 이상. 다른 유형 하락 0.02 초과 금지.

**KB10 명제·정본**
- 완료 기준: 6장 전체, 상태 전이 표의 모든 전이에 테스트, 결정적 추출기 2종 이상(Python "Changed in version", World Bank 표), 수요 큐, 예산 예약 연동.
- 독립 검증: 다른 세션이 전이 반례(T5 근거로 승격 시도, 같은 Run의 자기 검토, candidate끼리 충돌 승격, 다중값 facet 충돌 오탐, 시점 null 충돌)를 테스트로 작성. 수집량 10배 시험에서 추출 LLM 호출 수가 변하지 않음을 확인.
- 게이트: conflict 유형 충돌 표시율 ≥ 0.80, temporal·version 조건 정확도 KB7 대비 하락 없음. 로컬 모델 추출 candidate의 span_checked 통과율을 기록(기준값 없음, 이후 비교용).

**KB11 최신성**: 시계 고정 테스트, 감시 출처 재수집이 collector 스케줄로 돈다. 게이트: stale 표시 정확(고정 시계 fixture 100%).

**KB12 UI**: 8.3 화면, 상태별 화면, 320/390/768/1440 브라우저 확인 스크린샷. 게이트: 검토 결정이 낙관적 version 충돌을 올바르게 거부, 사용자 API 외 경로로 verified가 생기지 않음.

---

## 10. 독립 검토에서 집중해 볼 위험

| # | 위험 | 왜 위험한가 | 확인 방법 |
|---|---|---|---|
| R1 | Tantivy 투영과 PG 정본의 불일치 | 색인에만 있거나 PG에만 있는 패시지, 현재성 변경 반영 지연. 검색 결과가 없는 패시지를 가리키면 근거 계약이 깨진다 | [F] 단계에서 PG 재확인을 생략하지 않는지, index_log 따라잡기 실패 시 동작 |
| R2 | 배포 세대 중첩에서 쓰기 주체·추론 서버 중복 | 세대가 셋까지 동시 실행된다. Tantivy 쓰기 잠금 경합, llama-server 두 벌로 GPU 메모리 초과 | 3.9의 소유 규칙이 generation drain과 맞는지. desk_qwen 선례 확인(이번에 읽지 않음) |
| R3 | 호스트 메모리 압박 | 측정 시 스왑 92%. 상주 서버 두 개(약 0.9GB RSS)와 BQ·Tantivy 캐시 | 메모리 예산을 KB6 게이트에 넣었는지 |
| R4 | dense 기본화의 근거가 얕음 | 교차 언어 근거는 8쌍 스모크다. 패러프레이즈 이득, 희귀어 손실은 미측정 | KB7 게이트가 "다른 유형 하락 금지"를 실제로 막는지 |
| R5 | 재순위 점수 하나로 coverage를 정함 | 재순위 모델의 부정형·버전 조건 처리 능력 미측정. τ가 분야마다 다를 수 있다 | 분야별 τ가 필요한지 dev에서 확인 |
| R6 | 사람 확인 예산 | 사용자 한 명이 holdout 112문항(약 2시간, `확인 필요`)과 근거 정확도 표본을 봐야 한다. 지연되면 게이트가 막힌다 | 확인 UI를 KB-E1에 앞당겼는지, 대체 경로(공개 세트만으로 잠정 판정)를 허용할지 사용자 결정 필요 |
| R7 | NFC 저장 결정 | 기존 v1 계약(NFKC 저장)을 바꾼다. 두 정규화 방식 리비전이 공존한다 | 분석기가 두 경우를 같은 색인 토큰으로 만드는지 골든 테스트 |
| R8 | blob 공유와 collector 보존 정책 | collector 정리 작업이 kb 참조 blob을 지우면 원본 보존 원칙이 깨진다 | 참조 검사가 collector 정리 경로에 실제로 들어가는지(S2 담당과 합의 필요) |
| R9 | 명제 키 과분할 | 추출 품질이 낮으면 같은 사실이 값 표기 차이로 다른 명제가 되고, 독립 확인이 쌓이지 않는다 | facet 값 정규화 규칙과 병합 후보 큐 |
| R10 | 합성 데이터 측정의 외삽 | 어휘 크기 고정, 무작위 벡터, 부하 높은 호스트 | KB13 게이트가 외삽 ±30%를 확인하는지 |
| R11 | 라이선스 | HF 카드 표기만 확인했다. KorQuAD ND 같은 제약 | KB-E1 완료 기준의 원문 확인 |

---

## 11. 미확인 항목

| 항목 | 상태 |
|---|---|
| v1 독립 검토 원문 | 가드가 읽기 차단. 요청서 요약으로 대체 |
| 원 BAAI 모델 라이선스 원문, 데이터셋 라이선스 원문 | HF 카드 표기만 확인 |
| bge-m3 실제 재현율(패러프레이즈, 희귀어), 재순위 모델의 부정형·조건 처리 | 미측정(8쌍 스모크만) |
| llama-server의 bge-m3 sparse·colbert 출력 지원 | 확인 필요 |
| ParadeDB `pg_search`의 Homebrew PG17 설치 가능 여부 | 확인 필요 |
| 100만·500만 건 벡터 색인 빌드 시간, BQ 일치율 | 외삽(10만 실측 기반) |
| 실제 텍스트의 TOAST 압축률, 어휘 증가에 따른 색인 크기 | 확인 필요 |
| PG에서 tsv·GIN을 뺄 때의 패시지 표 크기 | 확인 필요 |
| [F]·[A] 단계 지연 | 미측정 |
| 실제 적재 경로(ORM, 배치 INSERT) 처리량 | 미측정(COPY만 측정) |
| GPU 활용률 계측 방법(macOS 무권한) | 확인 필요 |
| desk_qwen_service의 세대 중첩 처리 | 이번에 읽지 않음 |
| 0055 불변 트리거가 ADD COLUMN DEFAULT에 반응하지 않는지 | 확인 필요(마이그레이션 테스트로) |
| 한국은행·KOSIS 자료 이용 조건 | 확인 필요. 확인 전 사용 안 함 |
| 사람 확인 소요 시간(문항당 1분) | 추정 |
| 성능 목표치(Recall@10 0.80, p95 800ms 등) | 설계 예산, 실측 아님 |
| 배포 런타임이 로컬 DB와 같은지 | v1과 같이 확인 필요 |

---

## 부록 A. 측정 명령과 원 출력

스크립트는 이 문서 옆 `measure/` 폴더에 있다. 측정 때 쓴 원본에 저장소 ruff 검사를 피하려고 각 `.py` 첫 줄에 `# ruff: noqa`만 더했다(`gen.py`, `schema.sql`, `bench_*.py`, `smoke_xling.py`, `bq_recall.py`, `cmp_pool.py`, `lic.py`). 대용량 데이터와 임시 클러스터는 측정 후 지웠다.

```
# 임시 클러스터
initdb -D /tmp/kbv2/pg -U kb --no-locale -E UTF8
# postgresql.conf: port=55432, shared_buffers=1GB, maintenance_work_mem=4GB, max_parallel_maintenance_workers=4
LC_ALL=C pg_ctl -D /tmp/kbv2/pg start
# 확장: vector 0.8.7, pg_trgm 1.6

# M1·M2 (10만)
python3 gen.py 10000           # 리비전 1만 × 10패시지
\copy rev ...   → 2112 ms
\copy pas ...   → 8644 ms
rev 81 MB | pas 402 MB | avg_chars 688 | avg_bytes 1038
heap 156 MB | toast 237 MB
create index pas_tsv using gin(tsv) → 3660 ms ; gin 43 MB ; total 446 MB
term_stats: 118,415 terms, df>5% 190개, df>1% 902개

# M3 (10만) bench_lex.py
{"or_rank_top200": {"n": 200, "p50_ms": 560.3, "p95_ms": 942.0}, "or_count_only": {"p50_ms": 24.2, "p95_ms": 28.8, "median_hits": 100000}}
# M4 (10만) bench_lex2.py
{"df<=0.05": {"p50_ms": 21.3, "p95_ms": 38.6, "cand_p50": 2000}, "df<=0.01": {"p50_ms": 5.8, "p95_ms": 20.4, "cand_p50": 486, "cand_p95": 2000}}

# M1·M2 (100만)
\copy rev → 22034 ms ; \copy pas → 85703 ms ; gin → 49193 ms ; term_stats → 19009 ms
rev 807 MB | heap 1558 MB | pas_total 4332 MB | gin 314 MB | terms 118,456
# M3·M4 (100만)
{"df<=0.05": {"p50_ms": 27.2, "p95_ms": 37.2}, "df<=0.01": {"p50_ms": 23.3, "p95_ms": 103.2, "cand_p50": 2000}}
{"or_rank_top200": {"n": 20, "p50_ms": 5686.7, "p95_ms": 8808.9}, "or_count_only": {"p50_ms": 390.8, "p95_ms": 766.5}}

# M5·M12 Tantivy 0.26.2 / Kiwi 0.24.1
10만: {"tantivy_index_100k_s": 1.4, "tantivy_size_mb": 43.3, "tantivy_or8_top200": {"p50_ms": 0.83, "p95_ms": 1.17}}
100만: {"tantivy_index_1m_s": 11.4, "tantivy_size_mb": 362.8, "tantivy_or8_top200": {"p50_ms": 7.55, "p95_ms": 10.6}}
Kiwi: {"kiwi_load_s": 0.36, "chars_per_s": 374029–377373, "paras_per_s": 797–804}
      ["AIP/SL","앱/NNG","백/NNG","엔드/NNG","줄이/VV","반복/NNG","작업/NNG","원칙/NNG","처리/NNG"]

# M6 bge-m3 Q8_0, llama-server b11655 --embedding --pooling cls --parallel 4 --ctx-size 8192 --gpu-layers 999
{"passages": 1813, "dim": 1024, "avg_chars": 470, "prompt_tokens": 417360, "seconds": 14.0,
 "passages_per_s": 130.0, "tokens_per_s": 29918, "query_p50_ms": 11.0, "query_p95_ms": 14.8}
# M7
{"top1": "8/8", "pos_min": 0.545, "pos_mean": 0.673, "neg_max": 0.46, "neg_mean": 0.307}
# M8 (--device none --gpu-layers 0 --threads 6, 풀링 인자 없이 모델 기본)
{"gpu_vs_cpu_cos": [0.9995, 0.9996, 0.9995], "cpu_passages_per_s": 6.1}
# 서버 RSS: 임베딩 593,632 KB, 재순위 330,224 KB (ps -o rss)

# M9 (10만, 무작위 벡터)
insert halfvec(1024) → 16056 ms ; vec_total 276 MB
hnsw (m=16, ef_construction=64, halfvec_cosine_ops) → 403875 ms ; 260 MB
{"ef40_top100": {"p50_ms": 7.9, "p95_ms": 20.5}, "ef100_top100": {"p50_ms": 8.3, "p95_ms": 15.0},
 "ef200_top100": {"p50_ms": 11.1, "p95_ms": 13.6}, "insert_1000_with_hnsw_s": 20.39}
hnsw on binary_quantize(v)::bit(1024) bit_hamming_ops → 23179 ms ; 42 MB
ivfflat lists=316 → 45684 ms ; 265 MB
{"bq_c200_rescore_top50": {"p50_ms": 3.2, "p95_ms": 4.5}, "uses_index": true}
# 주의: ef_search가 LIMIT보다 작으면 pgvector는 ef_search개까지만 돌려준다(ef40 행은 top 40 상당)

# M10 실제 bge-m3 벡터
{"n": 1815, "overlap@10_C40": 0.931, "overlap@10_C100": 0.989, "overlap@10_C200": 0.998}

# M11 bge-reranker-v2-m3 Q8_0 --reranking --parallel 4
{"smoke_top1": "8/8", "min_gap": 11.047, "n20_median_ms": 206, "n50_median_ms": 484, "n100_median_ms": 965}

# 라이선스(HF API cardData) lic.py
miracl/miracl apache-2.0 | apple/mkqa cc-by-3.0 | klue/klue cc-by-sa-4.0 | KorQuAD/squad_kor_v1 cc-by-nd-4.0
akariasai/xor_tydi_qa mit | castorini/mr-tydi apache-2.0 | BeIR/scifact cc-by-sa-4.0
google-research-datasets/natural_questions cc-by-sa-3.0 | facebook/belebele cc-by-sa-4.0
```

## 부록 B. 용어

| 용어 | 뜻 |
|---|---|
| 투영(projection) | PG 정본에서 언제든 다시 만들 수 있는 파생 색인·표 |
| 명제(claim) | 출처와 무관한 주장 내용 |
| 진술(assertion) | 특정 출처 리비전이 명제를 지지·반박·한정한 기록 |
| 정본 묶음(group) | 같은 주어·측면·조건·발화 주체를 공유하는 경쟁 명제 집합 |
| BQ | 이진 양자화. 벡터의 각 차원을 부호 1비트로 줄인 것 |
| RRF | 순위 역수 융합. 점수 대신 순위로 결합 |
| coverage | 검색 결과가 질문에 답할 근거를 얼마나 갖췄는지(strong/weak/none/unknown) |
