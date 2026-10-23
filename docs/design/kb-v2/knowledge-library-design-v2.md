# 따까리 범용 지식 도서관(kb) 설계 v2

> **상태: KB-E0 로직 완료 승인, 제품검색 미승인.** 활성 설계 정본은 이 파일이다. KB-E0 하네스는 main `1e9bb1a` 기준으로 독립 확인 4회차에서 passed를 받았다([3회차](reviews/kb-e0-independent-review-v3.md), [4회차](reviews/kb-e0-independent-review-v4.md), [E0 완료 기록](reviews/kb-e0-completion.md)). 승인 범위는 DB·UI·추론을 제외한 E0 로직이다. 실제 DB export adapter·모바일 UI·실추론·실서버 계측은 미실행 잔여다. 검색/수집 제품 구현·엔진/모델 선택·최종 품질·운영배포는 미승인이다. 다음 단위는 9.2 순서를 따르되 배정 전에는 시작하지 않는다.

> 2026-10-23 갱신: 사용자의 명시 지시로 검색·열람 최소 제품화(API `/api/assistant/kb/*`, UI `/knowledge-library`)를 진행했다. 위 '제품검색 미승인' 표기의 해제 근거와 범위, KB7 품질 gate 미통과 사실은 [ADR 0063](../../adr/0063-knowledge-library.md)이 정본이다.

> 역사적 상태(E0 착수 시점): **상태: KB-E0 착수 조건부 승인, 제품검색 미승인.** 활성 설계 정본은 이 파일이다. 설계 수정 `460237722654d6cf1d540f321eae504e63028175`의 [반영 독립 확인 원문](reviews/amendment-independent-review.md)을 보존한다. 승인 범위는 평가 스키마·runner·고정 manifest·통계·계측·실패 분류 및 재개 골격의 KB-E0 착수까지다. E0 완료·검색/수집 제품 구현·엔진/모델 선택·최종 품질·운영배포는 미승인이다. [대응표](reviews/v2-review-response.md), §9 완료조건 및 [E0 구현 추적](../../work-history/2026-10-22-kb-e0-harness.md)으로 이어간다.

> 역사적 상태(수정안 작성 당시): **상태: v2 독립 검토 반영안 — 반영 여부 독립 확인 대기, 제품 구현 금지.** 활성 설계 정본은 이 파일이다. 검토 대상은 `8e3dc2c11f25098bc2fd1fee3886b6c235b5651f`, 검토 Run은 `78470bf8-41e6-4721-aa1c-20a853841cb3`이다. [원 보고서](reviews/v2-design-review.md)의 `approve-with-changes` 판정을 보존한다. 이번 변경은 P1–13·F1–24의 문서 해법이며 실행 통과나 사용자 승인을 뜻하지 않는다. [대응표](reviews/v2-review-response.md)와 §12의 검증·책임 단위로 추적한다. 부모가 반영 독립 확인을 받은 뒤 KB-E0를 시작한다. 이번 작업에서는 하네스도 구현하지 않는다.

원 설계 산출물(sha256 `6befe534d6c22d710bcfb0a5aa74d4420437110914e89ccdcbeba689be6e425a`)과 아래 M1–M12·부록 A는 2026-10-21 설계 세션의 **역사적 측정**이다. 별도 검토 측정은 R 표기로 분리하며 이번 세션에서 다시 측정한 값으로 부르지 않는다. 충돌하는 설계 문장은 이번 본문 수정 및 §12가 대체한다. v1 두 사본은 역사 기록이고 ADR 0063의 KB1·KB2 구현 사실은 유지한다.

- 작성: 2026-10-21, 설계 세션. 제품 코드는 바꾸지 않았다. 측정은 `/tmp/kbv2`의 임시 클러스터와 임시 llama-server로만 했다.
- 기준 코드: `winterholic-ttakkari/back` HEAD `66c8402c2f92d6114a9dcf1df8c6ade80e40db1a`. 작업 트리에 다른 세션의 미커밋 collector 파일(`collector/pipeline.py`, `0062_collector_stage_items.py` 등)이 있다. 이 문서는 그 파일을 읽기만 했다.
- 표기: 코드 근거는 `경로:줄`이고 경로의 `back/`은 `~/development/projects/winterholic-ttakkari/back/`이다. 원 설계 실측은 `[실측 Mn]`으로 표시하고 부록 A에 명령과 원 출력을 남겼다. 측정하지 않은 수치·버전·인과는 `확인 필요`로 표시한다.
- v1 독립 검토 원문(`outbox/4115e52e…/knowledge-library-design-review.md`)은 ttakkari 가드가 읽기를 막았다. 지적 2–6은 요청서의 요약을 근거로 삼았다(`검증 못함: 원문 접근 차단`). 지적 1, 7, 8, 9는 v1 문서 6·7단계에 반영된 내용으로만 확인했다.

---

## 0. 1쪽 요약

### 핵심 결정

| # | 결정 | 근거 |
|---|---|---|
| V1 | **후보 생성(재현율)과 순위화(정밀도)를 분리한다.** 후보는 어휘 BM25, 다국어 dense, 별칭 확장 세 경로에서 서버가 결정적으로 만든다. 순위는 교차 인코더 재순위가 정한다. RRF는 후보 합집합을 재순위 입력 크기로 자르는 데만 쓴다. | 지적 5. RRF는 이미 후보에 든 문서의 순서만 바꾼다. 누락된 후보는 되살리지 못한다. |
| V2 | **다국어 dense 경로(bge-m3)를 처음부터 기본으로 켠다.** v1의 "평가 후 켬"을 뒤집는다. 대신 평가 하네스가 "dense를 켜도 다른 유형이 떨어지지 않음"을 확인해야 출시한다. | 지적 2. 한국어 질의 대 영어 원문 8쌍 스모크에서 bge-m3 top-1 8/8 `[실측 M7]`. GPU 처리량 초당 130패시지, 질의 임베딩 p50 11ms `[실측 M6]`. CPU로는 초당 6.1패시지라 GPU 경로가 전제다 `[실측 M8]`. |
| V3 | **Tantivy 채택을 철회한다.** `LexicalIndex` 포트 뒤에서 PG 단독(필터+BM25), Tantivy, 가능하면 pg_search를 KB-E0.5에서 동일 정책·실자료·gold로 비교한 뒤 선택한다(§12 P8). | R: 실텍스트 100만·213질의에서 PG 필터+앱BM25 p95 19.71ms / Recall@100 .878, Tantivy 8.27ms / .851. 질의 정책이 달라 우열 확정 불가. PG ts_rank_cd는 3501.07ms / .391로 나쁨. |
| V4 | **BQ HNSW+halfvec와 full HNSW는 비교 후보다.** KB-E0.5 실자료 규모 시험과 KB6 필터 평가 후 선택한다. | 10만 건에서 full HNSW는 빌드 6분 44초·260MB, BQ HNSW는 23초·42MB `[실측 M9]`. 실제 bge-m3 벡터 1,815개에서 BQ 후보 100개 재채점의 top-10 일치율 0.989 `[실측 M10]`. |
| V5 | **질의 확장은 서버가 결정적으로 한다.** 에이전트의 `alt_queries`는 선택 입력(최대 2개)이고 평가는 이것 없이 통과해야 한다. | 지적 5. 같은 질문에 같은 결과가 나와야 회귀를 잴 수 있다. 호출 모델마다 변형이 달라 비용과 품질이 흔들린다. |
| V6 | **평가 하네스를 KB3보다 먼저 만든다.** 공개 세트는 회귀 감시·기준선이며 독립 일반화나 사용자 승인이 아니다. 출시 품질 판정은 사람이 확인한 자체 holdout이 필요하다. LLM 판정은 dev 탐색용으로 분리한다. | 지적 3. 사용자 원칙 "AI 결과만으로 정본 승격 금지"를 평가에도 적용한다. |
| V7 | **주장은 명제(claim)와 진술(assertion)로 나눈다.** 명제 식별에는 주어·측면·값·극성·양상·적용조건·유효시간이 들어간다. 출처는 진술 쪽에 둔다. 추정·의견처럼 누가 말했는지가 의미의 일부인 양상은 발화 주체를 적용조건에 넣는다. | 지적 6. v1 키 "개념·측면·적용조건"으로는 시점이 다른 값, 반대 극성, 기관별 추정치가 한 묶음에 섞인다. |
| V8 | **수집은 collector, 의미 추출은 kb가 맡는다.** kb는 collector outbox의 `kb` sink를 소비하는 어댑터를 소유한다. 추출 LLM 호출은 수집량이 아니라 "실제로 검색된 패시지 수와 고정 예산 중 작은 값"에 비례한다. | 사용자 결정(2026-10-20). collector ADR은 지식 모델과 분리를 요구한다(`docs/adr/collector-platform-20261020.md`). 패시지 우선 검색이라 claim 없이도 검색이 된다. |
| V9 | **저장 본문은 생산자 정규화(NFC)를 그대로 쓰고 NFKC는 색인 분석으로 옮긴다.** envelope 경로의 revision은 `normalized.sha256`과 `kb_revisions.text_sha256`이 같아야 받는다. | collector 생산자 계약을 정규화의 **마지막 NFC**까지로 보강한다(§12 P6). 이전 구현의 선행 NFC만으로는 고정점을 보장하지 않는다(F10). kb 기존 경로는 NFKC다(`kb_store.py:61-63`). 둘이 다르면 오프셋과 해시가 어긋난다. |

### v1에서 바뀐 결정

| v1 | v2 | 바꾼 이유 |
|---|---|---|
| D4 BM25가 1차, 벡터는 게이트 후 | 어휘(LexicalIndex)·dense·별칭 3경로 기본 후보 | 교차 언어 재현율은 어휘 경로로 원리상 확보할 수 없다(지적 2). 비용은 실측으로 감당 가능 |
| PG FTS OR + GIN으로 후보 200 | PG 필터+BM25 / Tantivy / pg_search bake-off 뒤 선택 | M3·M4의 합성 최악 경로만으로 PG 전체를 배제할 수 없다. 필터의 재현율 손실도 미입증(검토 F3). |
| D5 RRF 결합 + 재순위는 플래그 | 재순위 기본(`mode=full`), RRF는 후보 절단용 | 재순위 20건 206ms, 50건 484ms `[실측 M11]` |
| D9 에이전트가 `queries[]` 최대 4개 | 서버 결정적 확장 + 선택 `alt_queries` 2개 | 지적 5 |
| D6 정본 키 = 개념·측면·조건 | 명제 키에 극성·양상·유효시간 추가, 출처는 진술로 분리, facet 레지스트리 | 지적 6 |
| D10 kb 안의 수집기 어댑터 | collector가 수집, kb는 envelope 소비자 | 사용자 결정 2026-10-20 |
| `kb_sources` = 발행처(신뢰 등급) | 구현된 `kb_sources`는 원본 바이트 정체다(`kb_models.py:34-54`). 발행처는 새 `kb_publishers` | 설계와 구현의 차이를 바로잡는다 |
| 현재 리비전 = 문서별 최대 `rev_no` | 모든 수집 관측 ledger의 event time으로 원본을 고른 뒤 그 원본의 승인된 파서 리비전 선택(§12 P5) | 내용 중복과 A→B→A 복귀·A→A 재확인·늦은 옛 snapshot을 구분 |

### 지적 2–6 해소

| 지적 | 해소 방법 | 남은 위험 |
|---|---|---|
| 2 패러프레이즈·동의어·교차 언어 | dense 기본(V2), 다국어 별칭 사전, 재순위. 유형별 보고, 검정력 확보 시에만 사전 등록한 집계 게이트 | bge-m3 실측은 8쌍 스모크뿐이다. 실제 재현율은 KB-E0 이후 측정 |
| 3 지연·자원 계측 없음 | 하네스가 단계별 타이머, 프로세스 RSS·CPU 시간, GPU 메모리, 색인 디스크를 매 실행 기록. 부하·대기·내부시간을 분리하고 같은 부하의 동시 기준선으로 판정(4.6, §12 P3) | 호스트가 다른 Run과 공유라 지연 분산이 크다 |
| 4 수백만 규모가 추정뿐 | 10만·100만 건을 임시 클러스터에서 실측하고 500만은 외삽(5장). 실데이터 100만 선택 시험을 KB-E0.5로 앞당기고 KB13은 통합 규모 시험으로 유지 | 합성 텍스트와 무작위 벡터로 쟀다. 어휘 크기 증가와 실벡터 HNSW 빌드는 `확인 필요` |
| 5 `queries[]` 재현성·RRF | V1, V5. 후보 경로별 기여를 trace에 남겨 "후보 누락"과 "순위 밖"을 구분 | 서버 확장 사전의 품질이 새 의존점이 된다 |
| 6 시간·극성·출처 분리 | V7, facet 레지스트리, 상태 전이 표(6장) | 추출 품질이 낮으면 명제 키가 잘게 쪼개져 중복이 는다. 병합 후보 큐로 대응 |

### 구현 착수 조건

현재 상태는 **KB-E0 로직 완료 승인, 제품검색 미승인**이다(2026-10-22, DB·UI·추론 제외). E0 착수 시점의 상태는 **KB-E0 착수 조건부 승인, 제품검색 미승인**였다. P1–13·F1–24 반영 독립 확인은 위 원문에 보존하며, E0 단계에서는 KB-E0 하네스만 구현했다. 원 검토 시점의 문서 보완 작업 범위는 역사 기록으로 유지한다. KB3 이후 단위는 9장·§12의 실제 DB 포함 독립 검증을 통과해야 다음으로 간다.

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
| G5 | 원본 저장소가 둘이다(`kb/originals`, `cblob`) | kb는 collector BlobStore의 pin API를 호출한다. collector는 kb 표를 읽지 않는다. 권리 만료·삭제는 pin보다 우선한다(§12 P7) |
| G6 | collector 원본 30일 보존 제안과 kb 원본 영구 보존 원칙이 충돌한다 | 정상 pin 해제는 kb 명시 이벤트. 권리 만료·삭제는 tombstone 경로가 우선(§12 P7) |
| G7 | 현재성이 도착 순서(`rev_no`)라 옛 스냅샷 재전송이 최신이 된다 | 내용 리비전과 독립된 관측 ledger의 event time 기준(§12 P5) |
| G8 | kb 행에 scope가 없다. collector는 personal/work/shared를 가진다 | `kb_documents.scope` 추가, 검색은 호출자 scope 집합으로 거른다 |

---

## 3. 검색 아키텍처 v2

### 3.1 기존 실패와 요구

기존 맥락 도서관의 실패(v1 2장): 조사 미분리(C1), IDF 없음(C2), 문장형 질의 전체 AND로 0건(C3), 이종 점수 혼합(C4), 프로젝트 가산 0.65(C5), 벡터 하한 0.80(C6). v2는 다음 네 원칙으로 이 실패를 막는다.

1. 후보를 만드는 단계에서 점수를 섞지 않는다. 각 경로는 자기 순위만 낸다.
2. 고정 가산점과 고정 유사도 하한을 두지 않는다. coverage는 언어쌍·후보 정책별 보정 점수와 span 지지·조건 검사로 정한다(§12 P11).
3. 같은 입력이면 같은 후보가 나온다. 서버 밖 모델이 만든 변형에 재현율을 기대지 않는다.
4. 어느 경로가 후보를 냈는지 trace에 남긴다. "원문 없음 / 후보 없음 / 후보 있으나 순위 밖 / 순위 안이나 조건 틀림"을 구분한다.

### 3.2 재현율 확보 방안 비교

| 방안 | 패러프레이즈 | 동의어·약어 | 한영 교차 | 희귀어·식별자 | 재현성 | 비용 | 판단 |
|---|---|---|---|---|---|---|---|
| A. 다국어 dense(bge-m3) 기본 | 강함 | 중간 | 강함(스모크 8/8 `[실측 M7]`) | 약함 | 캐시 경로 결정적, 실추론 허용오차(§12 P4) | 색인 GPU 초당 130패시지, 질의 11ms `[실측 M6]`, 저장 100만 건당 halfvec 2.8GB + BQ 0.42GB `[실측 M9 외삽]` | **채택** |
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
          C1 어휘: LexicalIndex(엔진은 KB-E0.5 선택), 원 질의 분석어, top 200, scope/current/domain 사전 필터
          C2 dense: bge-m3 → 선택된 HNSW 경로, 허용 scope/current 필터 + iterative scan → top 200(§12 P12)
          C3 별칭 확장: LexicalIndex 하위 질의 top 50, 모호 별칭은 도메인 확정 때만
          C4 alt_queries(있을 때만): C1·C2를 같은 방식으로, 각 top 50
          C5 계층 확장(조건부, 3.4)
          C6 관계 확장(조건부, 3.4)
        ─► [F] 사전 필터된 합집합 → PG 권한·현재성 재확인 → 동일 본문(sha) 접기
          → 가중 RRF(k=60, C3 가중 ≤0.5)로 상위 N(기본 40), C3 단독 후보 ≤25%
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

§12 P11을 적용한다. dense 코사인은 abstain 기준으로 쓰지 않는다. 재순위 점수 s1은 `(질의 언어, 원문 언어, 모델/양자화, N, 후보 정책)`별 dev 보정값이다. `strong`은 s1 ≥ τ_strong에 더해 유효한 인용 span, 질문 답의 지지, 조건 일치를 모두 요구한다. 핵심 개념의 원문/승인 별칭 매칭은 보조 신호이며 단순 어휘 겹침으로 교차 언어 정답을 거부하지 않는다. 지지·조건을 검사하지 못하면 weak/unknown이다. `none`은 보정된 τ_none 이하 또는 허용 corpus에서 후보가 없을 때다.

near-miss abstain과 답 있는 문항을 언어쌍별로 나눠 false strong·false abstain을 따로 보정한다. 표본·검정력이 부족한 언어쌍은 `coverage=unknown`, `calibration_status=insufficient_sample`이며 strong으로 올리지 않는다. fast와 dense 장애의 미검증 교차 언어 경로도 unknown이다. dev 보정 후 사람 확인 holdout에서 한 번만 확정 시험하며 F1 하나로 통과시키지 않는다.

### 3.7 결과 계약

```json
{
  "trace_id": "…",
  "coverage": "strong | weak | none | unknown",
  "calibration_status": "calibrated | insufficient_sample | degraded",
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
      "quote_hash_verified": true,
      "claim_status": "verified | corroborated | unverified | none",
      "ai_generated_source": false
    },
    "applicability": {"match": "exact | partial | mismatch | unknown", "notes": []},
    "conflict_check": {"status": "partial", "extracted_passages_in_group": 3, "candidate_passages_unextracted": 2, "build_id": "…", "checked_at": "…", "scope": "query_candidates"},
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
- 근거 span이 없는 claim은 반환하지 않는다. 패시지 결과는 원문 인용이라 `quote_hash_verified=true`지만 claim 해석은 없으므로 `claim_status=none`이다. 에이전트는 이를 "원문 근거, 해석은 미검증"으로 전달한다.
- `quote_hash_verified`는 저장 본문의 슬라이스 해시 일치만 뜻하며 명제 지지 확인이 아니다. span은 저장 본문에서 문장 단위 후보를 재탐색하고 원문 좌표로 검증한다(§12 F22·F24).
- `conflicts=[]`는 충돌 부재 보증이 아니다. `conflict_check`는 질의 후보 snapshot 안의 검사 범위만 표시하며 전 corpus 완전성을 주장하지 않는다(§12 P13).
- `claim_status=unverified`(candidate)는 `include_unverified=true`일 때만 나온다.
- 근거가 T5(AI 생성) 출처뿐이면 `ai_generated_source=true`이고 claim_status는 unverified를 넘지 못한다.
- 응답 크기 상한 32KB. 넘으면 낮은 순위부터 quote를 줄이고 `budget.omitted`에 센다. 전문은 `kb_get`으로 가져온다.
- 예시의 PostgreSQL 값은 형식 설명용이며 사실 검증을 하지 않았다.

### 3.8 지연·자원 예산

아래 M 수치는 원 설계 측정이고 우측은 미검증 초기 목표다. 검토에서 임베딩 p50 73.2ms/p95 103.9ms, 재순위 n50 715ms였으므로 .8s 목표가 이미 충족됐다고 읽지 않는다. 병렬 단계 p95를 더해서 전체 p95를 계산하지 않는다. 전체·대기·내부 비용은 P3 실제 부하 시험으로 별도 측정한다.

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
| **전체 e2e p95 목표** | | **full 800ms, fast 200ms** |

메모리의 기존 RSS 594MB·330MB는 역사적 설계 측정이다. 독립 검토 RSS는 863MB·687MB, Kiwi 약 540MB/프로세스였으며 Metal 통합 메모리 총량은 미확인이다. 서버는 호스트 단일 소유, Kiwi 3세대·캐시·벡터 working set과 swap-in을 §12 P10 예산으로 검증한다.

### 3.9 투영(projection) 운영

PG가 정본이다. 어휘 엔진은 KB-E0.5 뒤 선택하고, 외부 투영을 선택한 경우 §12 P9의 직렬화 로그·멱등 replay·crash checkpoint를 반드시 구현한다. `kb_passage_analysis(passage_id, analyzer_version, tokens)`는 버전별 재생성 가능한 투영이다. Kiwi 실제 산문 처리량(검토 82–101k자/초)과 재구축 비용은 5.4에 반영한다.

`kb_projection_state`의 키는 `(projection_kind, analyzer_version, embedding_model_id, quantization, query_policy_version, index_schema_version, visibility_partition)`이다. 각 세대는 지원하는 키만 pin하며 전역 단일 포인터를 쓰지 않는다(§12 P12). build 변경은 검증된 디렉터리와 durable checkpoint를 준비한 다음 PG 포인터를 원자 교체한다. 구세대 reader lease가 남으면 구 build를 유지한다.

추론은 세대 자식이 아닌 호스트 단일 외부 서비스 모드다. 백엔드 세대는 시작·종료 권한 없이 health/capability 확인 후 접속한다. 같은 모델 해시의 중복 상주는 금지한다(§12 P10). 이번 문서 작업은 서비스를 기동하거나 설치하지 않는다.

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
- 재사용 후보: `knowledge/evaluation.py`의 `validate_split`(11), `score`(19), `bm25`(91). 기존 질의 독립 `paired_interval`(69)은 gate에서 쓰지 않고 §12 P1의 문서 군집 bootstrap으로 대체한다. 새로 필요한 것은 nDCG 등급, 근거 정확도, abstain, 유형별 집계, 자원 계측이다.
- 실행 하나는 코퍼스 manifest 해시, gold 해시, 코드 SHA, 분석기·모델·양자화 해시, 색인 build_id를 기록한다. paired 비교에서는 corpus/gold를 고정하고 코드·분석기·모델·build 중 사전 등록한 처리 요인만 달라야 한다. 나머지가 달라지면 비교 불가로 기록한다.

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
| 상식·교차 언어(공개 세트) | MIRACL ko | 코드 Apache-2.0과 데이터·qrels·Wikipedia corpus 권리를 분리 확인. 데이터 재배포 허용은 미확인 | 사실 조회, 기준선 |
| 교차 언어(공개 세트) | XOR-TyDi QA(ko 질문 → 영어 위키백과) | 저장소 MIT는 코드. data/qrels/corpus 조건은 별도 미확인 | 교차 언어 |
| 교차 언어(공개 세트) | MKQA(ko 포함) | HF 카드 CC BY 3.0 | 교차 언어, abstain(답 없음 표기 문항) |
| 한국어 독해 | KLUE-MRC | HF 카드 CC BY-SA 4.0 | 패러프레이즈(질문-문단 어휘 겹침이 낮은 문항) |
| 제외 | KorQuAD 1.0 | HF 카드 CC BY-ND 4.0. 질문을 고치거나 번역하면 2차 저작물이 되므로 원형 그대로만 참고용으로 쓰고 게이트에는 넣지 않는다 | |

HF 카드 표기(`/tmp/kbv2/lic.py` 실행 결과)는 원 라이선스 문서를 대신하지 않는다. KB-E0 완료 조건에 각 자료의 라이선스 원문 URL과 확인 시각을 manifest에 기록하는 일을 넣는다. 저장 자체의 권리도 확인한다. 저장 허용이 확인되지 않은 자료는 제외하며, 재배포 권리는 별도 확인한다. 공개 세트의 계열 학습 노출·split 중복·코드/data/qrels/corpus 라이선스 구분은 §12 F12의 blocker 목록으로 관리한다.

코퍼스 크기: 분야별 문서 300–500개, 합계 패시지 약 5만(`확인 필요`, 수집 후 확정). 여기에 MIRACL ko 코퍼스 일부를 방해 문서(distractor)로 섞어 10만 패시지 평가판을 만든다. 선택 시험 KB-E0.5부터 실텍스트 100만을 쓰고 KB13에서 통합 검증한다. corpus 5만·10만 목표는 수집 전 추정이다.

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

위 12유형의 분야별 기본 합은 **28문항/분야**, 4분야 dev 112·holdout 112(총 224)라는 초기 계획이다. 이는 표본수·검정력을 충족한다는 뜻이 아니다. F21 보완 유형은 아래와 같고, 중복 태그를 허용한다. 유형당 각 split 최소 8문항을 수집 목표로 삼되 합계는 중복 태그 때문에 단순 합산하지 않는다. 유형 셀은 보고용이며 작은 셀의 하락 금지 gate를 제거한다.

| 보완 태그 | 필수 반례 |
|---|---|
| typo / keyword_short / code_mixed | 오타·붙여쓰기, 초단문, 한영 혼용 |
| identifier / numeric_table | 오류코드·함수·버전 문자열, 숫자·표·단위 환산 |
| multi_answer / aggregate / ambiguous | 여러 정답, 집계, 되묻기가 필요한 불충분 질의 |
| revision_current / duplicate | 옛/현재 리비전 동시 존재, 복제·근사 중복 |
| near_miss_abstain / double_negation / injection | 같은 주제 근접 오답, 이중 부정, 수집 원문 내 지시문 |

공개 세트 고정 표본의 원본 문항 수·허용 split은 로더에서 확인한다. 자체 holdout은 §12 P1의 검정력과 P11의 언어쌍별 최소 표본을 충족하도록 확대하거나 `inconclusive`로 남긴다. **112개/약 2시간은 사용자 미확인 작업량 추정**이며 인간 확인을 면제하는 결정이 아니다.

### 4.4 정답 세트 제작 절차

1. KB-E1a에서 권리 확인한 공개 세트 manifest와 원본 qrels·기준선부터 고정한다. 하네스·공개 기준선은 사람 확인 질문과 독립적으로 진행 가능하다.
2. 자체 gold 초안은 corpus 문서 group 단위로 기계 표집하고 질문 작성자·초안 모델을 기록한다. **KB5의 생산자 정규화·분할기 확정 뒤 KB-E1b에서 사람 최종 확인**한다.
3. gold anchor는 `{doc_key, source_revision_id, raw_sha256, body_sha256, source_uri, observed_at, quote, quote_sha256, match_normalization_version, normalized_quote, context_before/after, answer_id}`다. 안정된 doc_key+인용문으로 passage 재분할에 견디되 인용 근거 리비전·시점·권리를 버리지 않는다. 매 실행 동결 source revision의 원문을 확인한 뒤 현 passage에 remap한다(§12 P2).
4. paraphrase·언어·부정·조건의 기계 검사는 초안 검증일 뿐이다. abstain은 핵심어가 있으나 답이 빠진 near-miss를 포함하고, lexical·dense·rerank·별칭 후보 풀과 판정 범위를 사람이 확인한다. corpus 전체 답 부재를 기계가 입증했다고 표시하지 않는다.
5. 관련성 풀링은 정답을 새로 만드는 절차가 아니다. KB6 후 KB7 확정 시험 전에 dense·rerank·lexical top20을 추가하여 **새 미판정 항목만** 확인한다. unjudged는 비관련으로 묵시 대체하지 않고 judged@k·판정 누락률을 보고한다. holdout 결과를 본 뒤 pool을 고쳤으면 해당 holdout은 소비된 것으로 처리한다.
6. 사용자가 자체 holdout을 전부 확인한다는 기존 조건을 유지한다. 공개 세트로 잠정 진행할지 답은 아직 없다. 모바일 확인 UI는 320/390px에서 질문·인용·풀·검사 결과와 승인/수정/폐기를 제공하며 중단·재개·원문 접근·라벨 버전 이력을 보장한다. UI 골격은 E0, 공개 기준선은 E1a, 자체 사람 확인은 E1b 책임이다.
7. 원문 문서·근사 중복·교차 언어 대응 문서를 같은 group에 넣고 dev/holdout을 분리한다. 일반 개발은 dev만 본다. holdout은 사전 등록한 비교에 독립 검증자가 **한 번** 연다. 열람·사용 결과를 보고 수정하면 새 숨김 holdout이 필요하며 기존의 3회 허용 규칙은 폐기한다(§12 P1).
8. dev 독립 원문 확인은 `verified_by=agent`, LLM judge는 `judge=llm`으로 분리한다. 둘 다 사람 확인 holdout의 대체물이 아니다. 공개 벤치마크 통과는 독립 일반화·사용자 승인을 뜻하지 않는다.

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

모든 품질 지표는 전체·분야별·유형별·언어쌍별로 보고한다. 비교는 같은 질문 쌍의 차이를 문서 group 단위로 군집 bootstrap한다(§12 P1). false strong과 false abstain, judged@k, anchor invalid/ambiguous를 별도 집계한다.

### 4.6 지연·자원 측정 방법

§12 P3·P4·P10을 따른다. 워밍업 뒤 후보와 동시 기준선을 무작위 AB/BA block으로 교차 실행하고 동시성 1/4, 같은 질의·N·cache 정책·host load를 기록한다. 부하 높다는 이유만으로 gate에서 제외하지 않는다. end-to-end, queue wait, service 내부 단계 시간을 분리하며 외부 추론 요청의 대기와 처리도 기록한다. 절대 SLO와 같은 부하에서의 상대 회귀를 각각 판정한다.

RSS·CPU·Metal 지표는 단일 호스트 서비스와 세대별 Kiwi를 구분해 표본하며 RSS를 GPU 점유량으로 부르지 않는다. `vm_stat` swap-in pages/sec×실제 page size를 10분 이상 측정한다. 접근 불가 지표는 null 사유와 `unverified`를 쓰고 0으로 채우지 않는다. 재현성은 캐시 replay의 정확 일치와 실추론의 허용오차를 분리한다(§12 P4).

### 4.7 회귀 게이트

| 항목 | 기준과 판정 범위 |
|---|---|
| 1차 품질 | 자체 사람 확인 holdout Recall@10, nDCG@10 각각 비열등 마진 Δ=.05(설계 정책). 군집 CI·Holm 보정·검정력 ≥.80 사전 확인. 수 부족/CI가 경계 걸치면 inconclusive(§12 P1) |
| 분야·유형 셀 | 점추정·CI·표본수·회귀 사례 보고용. 작은 셀의 .02/.05 하락 금지 규칙 폐기 |
| 공개 세트 | 사전 고정 비교에서 회귀 감시. 계열 학습 노출 표시. 독립 일반화·출시 승인으로 해석 금지 |
| abstain/조건 | P1·P11의 최소 표본·검정력 확보 뒤 false strong·false abstain 분리 판정. 미보정 언어쌍은 unknown |
| 지연 | 같은 부하의 동시 기준선 대비 p95 ≤1.20, full ≤800ms·fast ≤200ms 절대 SLO 별도. 높아진 부하는 면제 사유가 아님(P3) |
| 자원 | P10의 3세대·cache·server 메모리 및 swap-in 예산. RSS+20%는 기준 대비 보조 지표 |
| 재현성 | 캐시·고정 build replay 정확 일치, 실추론은 P4의 사전 고정 ε/near-tie 집합 기준 |

품질 pass/fail/inconclusive, 지연 absolute_pass/absolute_fail와 relative_pass/relative_fail을 분리한다. 미실행·관측 실패는 unverified이며 통과가 아니다. 모든 필수 게이트가 pass가 될 때만 다음 구현 단위를 승인한다.

### 4.8 실패 분류

질문마다 자동 분류해 run 결과에 남긴다.

1. `not_in_corpus`: 원문 anchor는 유효하나 대응 passage가 색인에 없음(수집·분할 실패). anchor invalid/ambiguous는 별도 평가 입력 오류이며 not_in_corpus로 뭉개지 않는다.
2. `no_candidate`: 어느 후보 경로에도 없음. 경로별로 몇 위였는지 trace에서 남긴다.
3. `cut_by_fusion`: 후보에는 있었으나 RRF 절단(N) 밖.
4. `ranked_out`: 재순위 후 top k 밖.
5. `wrong_condition`: top k 안이나 조건·시점이 틀린 claim이 앞섬.
6. `false_abstain` / `false_strong` / `false_answer`; `filtered_out`·`index_stale`·`calibration_insufficient` 별도.

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

이 표는 종전 Tantivy+BQ 가정의 역사적 용량 계획이다. KB-E0.5 이전에는 PG tsv/GIN 제거도 Tantivy 추가도 확정하지 않는다. 356GiB 여유는 당시 관측일 뿐 현재 여유·500만 적재 가능성의 보증이 아니다. 선택된 엔진의 PG/외부 색인·구 build·3세대 working set·WAL·백업을 다시 합산한다.

### 5.3 인덱스 선택

| 대상 | 선택 | 이유 |
|---|---|---|
| 어휘 | LexicalIndex: PG 필터+BM25 / Tantivy / 가능시 pg_search | KB-E0.5 동일 정책 bake-off로 선택. M3·M5만으로 확정하지 않는다 |
| 어휘 대안 | ParadeDB `pg_search` | PG 안에서 BM25를 하므로 정합성이 단순하다. Homebrew PG17에 설치 가능한지 `확인 필요`. 가능하면 KB-E0.5에서 같은 정책·gold로 비교한다 |
| dense | pgvector full HNSW vs BQ+halfvec 재채점 | M9·M10은 후보 근거. 실벡터 100만·필터 recall·부하 지연 후 선택 |
| dense 대안 | IVFFlat | 빌드는 BQ HNSW보다 느리고(46초 대 23초) 크기는 full과 비슷하다. 쓰지 않는다 |
| 문서 단위 | LexicalIndex 문서 색인(제목+절 제목+첫 문단) + 선택된 문서 대표 벡터 | 계층 탐색(3.4) |
| 메타데이터 필터 | PG B-tree(`kb_documents(scope, domain, status)`, `kb_revisions(document_id)`) | 각 후보 경로의 scope/current/domain 필터를 먼저 적용하고 PG에서 보안 재확인(§12 P12) |
| claim 조건 | GIN `(conditions jsonb_path_ops)`, `(subject_concept_id, facet)` | v1 유지 |

### 5.4 쓰기 비용(패시지 100만 건 신규 적재 기준)

| 단계 | 시간 | 근거 |
|---|---|---|
| 리비전+패시지 PG 적재(트리거 포함, COPY 방식) | 약 1.8분 | M1·M2. 실제 적재 경로는 ORM 행 단위라 더 느리다(`확인 필요`). 배치 INSERT(500행)로 구현한다 |
| Kiwi 분석 | 688자×100만 / 80k자/초 = 단일 약 2.39시간; 이상적 4프로세스 35.8분 + 오버헤드(미측정). 실제 짧은 산문 1.75억자는 5프로세스 893초(검토, 부하 있음) | M12 repo docs 377k자/초를 일반화하지 않음. KB3 자체 산문 기준선×80% 게이트 |
| Tantivy 색인 | 약 12초 | M5 |
| bge-m3 임베딩 | 약 2.1시간(GPU) | M6 외삽. 500만은 약 10.7시간 |
| BQ HNSW | 새로 빌드하면 10만 23초에서 외삽해 100만 약 4분(로그 인자 고려 시 더 길 수 있음 `확인 필요`). 증분은 full HNSW 행당 20ms보다 빠를 것으로 보나 미측정 | M9 |

증분 수집(하루 수천 건)은 위 비용이 무시할 수준이다. 대량 적재(위키백과 덤프 등)는 다음 순서를 지킨다. ① PG 적재, ② 분석, ③ 선택된 어휘 엔진 새 build, ④ 임베딩 백필 작업(재개 가능, cursor), ⑤ 벡터 색인 새로 빌드, ⑥ 평가, ⑦ 활성 build 전환. 임베딩 백필이 끝나기 전에는 새 패시지의 dense 경로가 비어 있으므로 응답 `degraded`에 `dense_backfill_pending`을 적는다.

### 5.5 재색인 절차

| 바뀌는 것 | 절차 | 전환 조건 |
|---|---|---|
| 분석기 버전 | 새 `analyzer_version`으로 `kb_passage_analysis` 전체 재생성(백그라운드) → 새 LexicalIndex build | 하네스 holdout 상시 게이트(4.7) 통과 |
| 임베딩 모델·양자화 | 새 `model_id`로 `kb_passage_vectors_<model_id>` 표 생성, 백필, `CREATE INDEX CONCURRENTLY` | 같은 게이트 + τ 재보정 |
| 재순위 모델 | 서버 교체만. 색인 변화 없음 | 같은 게이트 + τ 재보정 |
| 분할기 버전 | 새 parser/chunker 버전의 리비전·패시지를 새로 만든다(옛 행은 불변으로 남음). 근거가 옛 패시지를 가리키는 claim은 유지된다 | 같은 게이트 |
| Tantivy 색인 손상 | `kb_passage_analysis`에서 재구축(100만 약 12초) | 무결성 검사 통과 |

전환은 버전 tuple별 활성 포인터를 바꾸는 한 트랜잭션이다(§12 P12). 구세대는 자신이 지원하는 tuple·모델 endpoint를 계속 pin한다. 되돌리기도 tuple별 이전 build로 하며 tombstone revocation fence는 되돌리지 않는다.

### 5.6 재검토 신호

- 선택된 LexicalIndex의 100만 p95가 20ms를 넘거나, 쓰기 작업자의 따라잡기 지연이 5분을 넘으면 `pg_search` 또는 색인 분할(segment 정책)을 비교한다.
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
4. 독립 출처 수(발행처·근사 중복뿐 아니라 인용 체인·공통 원전·전재를 조사하여 independence_basis 확인한 진술).
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
| span_checked → corroborated | 독립 출처 2개 이상의 지지 진술, 그중 하나가 T1 또는 T2 | 시스템 계산(각 independence_basis 확인 완료 필요) |
| corroborated → verified | 사용자 확인, 또는 사용자가 위임한 분야에서 T1 원문 직접 대조 | 사용자 API |
| 어느 수준 → 한 단계 이상 강등 | 근거 진술이 `evidence_lost`·`withdrawn`으로 빠져 조건을 못 채움 | 시스템 |

**state**

| 전이 | 조건 | 주체 |
|---|---|---|
| (생성) → active | 기본 | 시스템 |
| active → disputed | 같은 묶음, 겹치는 조건과 시간 구간에서 cardinality=single인데 허용 오차 밖 값이 있거나 반대 극성. 상대 명제가 span_checked 이상일 때만(candidate끼리는 충돌로 올리지 않고 검토 큐만) | 시스템 |
| disputed → active | 조건·시간 분리로 해소, 상대 명제가 retracted·superseded, 또는 사용자 판정 | 시스템·사용자 |
| active·disputed → superseded | 같은 출처의 새 리비전의 명시 정정 근거가 원문 대조로 확인됨(인용 미발견만으로는 불가), 또는 사용자가 새 명제로 대체 지정. `superseded_by` 기록 | 시스템(같은 출처 정정만)·사용자 |
| 어느 상태 → retracted | 출처의 명시 철회 또는 사용자 오류 판정. 404/410·인용 미발견·evidence_lost는 검토 큐이며 자동 철회 금지 | 감사 가능한 철회 정책·사용자 |
| superseded/retracted → active | 동일 proposition_key 행을 유지하고 명시 `reopen` 이벤트(새 근거·권한·사유·expected_version)로 재개. 과거 종결 이벤트는 보존 | 권한 있는 검토자·사용자 |

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

기존 원장 의미는 보존하되 열·새 표와 tombstone 전용 제한 함수/인가 트리거를 추가한다. 일반 UPDATE/DELETE 우회는 허용하지 않는다(§12 P7).

| 표 | 주요 열 | 불변성 |
|---|---|---|
| `kb_publishers` | id, key(예 `site:postgresql.org`), name, kind, trust_tier 1..5, tier_reason, license_default, homepage | 가변. 변경은 `kb_publisher_events`에 기록 |
| `kb_documents` 열 추가 | publisher_id, canonical_url, domain, scope(personal/work/shared), status(active/gone/withdrawn), volatility, published_at, trust_tier_override, duplicate_of | 가변 껍데기(기존과 같음) |
| `kb_sources` 열 추가 | store_kind(kb/collector), envelope_id | 불변. ADD COLUMN DEFAULT는 UPDATE 트리거를 부르지 않는다(`확인 필요`: 0055 트리거가 ALTER에 반응하지 않음을 마이그레이션 테스트로 확인) |
| `kb_revisions` 열 추가 | normalization(nfkc-raw-v1 / producer-nfc-v1), producer_text_sha256, truncated, chunker_version | 불변 |
| `kb_observations` | observation_id, document_id, source_id, event_time, receipt_time, envelope_id, receipt_id, external_version, clock_status, eligibility, parser_policy_id | 모든 고유 수집 관측 append-only; 전달 재시도는 멱등 |
| `kb_intake_receipts` | envelope_id, adapter_version, payload_hash, result, revision_id, created_at. `UNIQUE(envelope_id, adapter_version)` | append-only |
| `kb_passage_analysis` | passage_id, analyzer_version, tokens | 버전별 새 행; 일반 수정 금지, tombstone의 인가된 내용 제거 예외(P7) |
| `kb_passage_vectors_<model>` | passage_id, vec halfvec(1024) | 투영 |
| `kb_index_log`, `kb_projection_state` | 3.9/P9, E0.5 선택된 비동기/외부 투영별 필요 결정 | 로그는 append-only, checkpoint는 durable 검증 뒤 갱신 |
| `kb_concepts`, `kb_concept_aliases` | v1 3절 유지 + alias `lang`, `source`(curated/wikidata/extracted), `status` | 가변 + 이벤트 |
| `kb_facets` | 6.4 | 가변 + 이벤트 |
| `kb_claims` | id, proposition_key unique, group_key, subject_concept_id, facet_key, value_json, object_concept_id, polarity, modality, attribution_concept_id, conditions, valid_from, valid_to, statement, review_level, state, version, 캐시(best_tier, independent_sources, last_verified_at) | 명제 열 불변(트리거), 상태·캐시만 이벤트와 같은 트랜잭션에서 갱신 |
| `kb_claim_assertions` | id, claim_id, evidence_id, stance(supports/refutes/qualifies), revision_id, publisher_id, asserted_at(출처 발행 시각), extractor(id, version, prompt_sha), status(active/evidence_lost/withdrawn), independence_basis, applies_provenance | 행 불변, status 변경은 이벤트 |
| `kb_claim_events` | v1과 같음 | append-only |
| `kb_canonical_groups`, `kb_canonical_selections` | group_key, 구간, claim_id(들), selected_by, score jsonb | 선택 기록 append-only, 현재 포인터는 가변 |
| `kb_conflicts` | id, group_key, claim_a, claim_b, reason, detected_at, resolved_at, resolution | 해소는 이벤트 |
| `kb_extraction_queue` | passage_id, extractor_version, priority, reason(query_hits/user/eval), budget_reservation_id, status | 운영 |
| `kb_search_traces` | trace_id, query 해시, 경로별 후보 순위, 최종 결과, 지연, 모드 | personal 원문 질의 저장 금지; 일별 keyed HMAC 질의 digest+최소 메타 7일, payload 없는 집계 90일(§12 F24) |

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
| `source.collected_at` | `kb_observations.event_time`; `kb_sources.collected_at`은 최초 원본 메타일 뿐 현재성 기준이 아님 |
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
| `extensions["kb:normalized_doc"]` | 제목, 저자, 절 구조, 버전 힌트 → 분할기 입력, `applies`; 명제 conditions에 상속하고 필드별 provenance 보존(§12 F24) |
| `extensions["kb:source_policy"]` | publisher 매핑 힌트. trust_tier는 publisher 표가 이긴다 |
| `lineage` | `origin_meta.lineage` |

### 7.3 수용 검증(kb 어댑터)

순서대로 하나라도 실패하면 `kb_intake_receipts.result`에 사유를 남기고 outbox에 실패를 돌려준다. 부분 수용은 없다.

1. schema major가 1인지. 아니면 `unsupported_schema`.
2. `decision.state=accept`, `rights.permission`에 store 허용.
3. `sha256(UTF-8(normalized.text)) == normalized.sha256`. 텍스트에 NUL 없음(`kb_store.py`의 기존 NUL 거부 규칙 재사용).
4. **모든 삭제·공백 정리 뒤 마지막 NFC** 및 최종 UTF-8 해시라는 생산자 버전 계약 확인. 비NFC면 `producer_contract_mismatch`로 격리·명시 재발행 요청(무한 재시도/소비자 재해시 금지). 구 생산자 호환 전환은 §12 P6.
5. `blob_handle`이 존재하고 `raw.sha256`·`byte_size`와 맞는지(BlobStore port로 확인). 원본 보존 불가(`unpreserved:` 핸들, `stage-handler.md` 흐름 3)면 수용은 하되 `kb_sources.origin_meta.raw_preserved=false`로 표시하고 이 리비전의 진술은 corroborated 계산에서 "원본 재검증 불가"로 표시한다.
6. `kb:*` 확장은 kb 쪽 strict validator로 검사한다. 모르는 확장 버전은 거부(`contract-compatibility.md:37`).
7. 멱등: `(envelope_id, adapter_version)`이 이미 있으면 같은 `payload_hash`일 때 기존 결과를 돌려주고, 다르면 거부.
8. 모든 고유 수집은 observation을 추가한다. event_time 순으로 현재 원본을 선택하므로 A→B→A의 마지막 A도 현재가 되고 A→A는 last_verified_at을 갱신한다. 늦게 받은 옛 event는 receipt_time이 커도 현재를 바꾸지 않는다(§12 P5).
9. 사전 검증·분할·분석은 트랜잭션 밖에서 준비하고 권리/pin을 재확인한다. 한 수용 트랜잭션에서 source·revision·passages·analysis·observation·pin·index_log·receipt를 결속한다. 로그 전역 잠금은 seq 할당 전부터 commit까지 유지한다(§12 P9). 임베딩은 비동기다.

### 7.4 기존 코드와의 차이와 필요한 변경

| 항목 | 변경 | 위치 |
|---|---|---|
| NFC/NFKC(G3) | envelope 경로는 NFC 본문 저장. 기존 `kb-ingest` CLI 경로(`normalize_text`, NFKC)는 `normalization=nfkc-raw-v1`로 남긴다. 분석기가 두 경우 모두 NFKC+casefold를 색인 단계에서 적용한다 | `kb_store.py:61-63`, 새 어댑터 |
| 재처리(G4) | `ingest_revision(source_id, parser_version, normalization, body)` 신설. 같은 원본·같은 parser_version이면 멱등 | `kb_store.py` |
| blob 이중화(G5·G6) | kb는 `cblob:` 핸들을 참조한다. collector 보존 정리 작업은 자신의 pin 레지스트리만 읽는다. 권리 삭제는 pin보다 우선하며 kb 어댑터에 tombstone 통지한다(§12 P7). `kb-verify`는 BlobStore port로 두 저장소를 모두 확인한다 | `collector/blobs.py`, `kb_store.verify_integrity` |
| 현재성(G7) | 현재 원본은 observation event_time 기준, 현재 파서 리비전은 해당 source의 활성 parser policy 기준이다. receipt_time·최대 전역 rev_no를 현재성 근거로 쓰지 않는다(§12 P5). | `kb_store.py`, 모델 docstring `kb_models.py:6` |
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
6. **핵심 의미 추출·원장 판단에는 경량 모델 금지.** 이전 Qwen3.5-4B 기본 선택은 철회한다. 검증된 상위 모델·결정적 추출만 후보를 만들며 호출 전 예산을 예약한다. 배정 모델이 Flash 등 경량으로 fallback하면 작업을 멈추고 진행 상태를 보고한다. 모델·양자화·프롬프트별 품질은 KB10에서 별도 검증한다.
7. **결과는 candidate뿐.** 어떤 추출기든 결과는 candidate다. 승격은 6.6 규칙(다른 주체의 원문 대조, 독립 출처, 사용자)으로만 한다.

결과: 추출 LLM 호출 수 ≤ min(질의로 드러난 고유 패시지 수 + 사용자 요청 + gold, 일 예산). 수집량 N이 10배가 돼도 이 상한은 변하지 않는다.

---

## 8. 에이전트 인터페이스와 UI

### 8.1 MCP 도구

| 도구 | 입력 | 출력 | 권한 |
|---|---|---|---|
| `kb_search` | `query`(필수), `alt_queries?` 최대 2, `intent?`, `as_of?`, `conditions?`, `domain?[]`, `k`(기본 6, 최대 20), `mode`(full/fast, 기본 full), `include_unverified`(기본 false), `hops`(0/1) | 3.7 결과 계약 | 읽기, 자동 승인 |
| `kb_get` | `id`(passage/document/revision/claim/concept), `history?`, `full_text?` | 상세, 리비전 이력, 진술·근거·반박, conflict_check, 이벤트. extract_missing은 별도 예산/쓰기 권한 경로(P13) | 읽기(추출 요청은 쓰기) |
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

### 9.2 단위와 착수 순서

| 코드 | 작업 | 선행·책임 |
|---|---|---|
| KB-E0 | 하네스 골격·manifest/anchor schema·군집 지표·power·AB 지연·캐시 replay·모바일 라벨 확인 골격 | 로직 완료 승인(main `1e9bb1a`, 독립 확인 4회차 passed, DB·UI·추론 제외). 실제 DB export·모바일 UI·실추론은 잔여. 평가 담당(핵심) |
| KB-E1a | 권리 확인 공개 세트 로더·고정 corpus/qrels·공개 기준선·노출/라이선스 manifest | E0. 평가/데이터 담당 |
| KB-E0.5 | 동일 query policy·실텍스트 100만·gold bake-off, 선택 ADR. full/BQ 실벡터 비교도 이 단계에서 먼저 시도 | E0+E1a. 검색/평가 담당(핵심). 실벡터 비용 미충족이면 선택 inconclusive, KB6 확정 차단 |
| KB3 | 분석기·버전·자체 산문 throughput 기준선·독립 골든 | E0. 분석기 담당(핵심) |
| KB4 | 관측 ledger·ingest_revision·인가 tombstone·pin 계약·로그 프로토콜 schema | KB3+collector 계약 합의. 원장 담당(핵심) |
| KB5 | collector kb sink·최종 NFC 계약·토큰 대응 분할·인용 anchor remap | KB4. kb 어댑터 담당(핵심); collector 코드는 별도 소유자 |
| KB-E1b | 자체 gold 최종 pool·사람 확인·검정력 충족 holdout 동결 | KB5 및 KB6의 dense/rerank pooling 뒤, KB7 확정 시험 전. 평가 담당+사용자. 초안은 먼저 가능 |
| KB6 | 선택 엔진 투영·checkpoint/replay·단일 외부 추론 서비스·필터 dense·버전별 build | KB5+E0.5 선택. 검색/운영 담당(핵심) |
| KB7 | 검색·언어쌍 coverage·conflict_check 응답·trace | KB6+E1b. 검색 담당(핵심) |
| KB8 | 검색 도구·kb_get·evidence·ingest·status·도구 선택 정책 | KB7. MCP 담당(정책 핵심) |
| KB9 | 개념·facet·모호 별칭 제한·OOV 사전·C3 | KB7. 검색/지식 담당(핵심) |
| KB10 | 명제·진술·상태 이벤트·정본·독립 출처·수요 추출·검토 | KB9+예산 계약. 원장 담당(핵심) |
| KB11 | 관측 기반 최신성·재수집 | KB10. 원장/collector 담당 |
| KB12 | 검색·문서·주장 검토 UI, 모바일 상태·권한 | KB8/KB10. UI 담당; 상태/권한은 핵심 검토 |
| KB13 | 통합 실데이터 100만·합성 500만 저장/지연 | KB7. 평가 담당(핵심); E0.5의 선택 시험을 대체하지 않음 |

E0 → E1a/E0.5와 KB3 → KB4 → KB5 → KB6 → E1b 최종 동결 → KB7 순이다. KB-E1b는 KB6의 구현 입력이 아니므로 순환 의존이 없다. 공개 세트 준비·하네스·설계 보완은 사용자 사람 확인 답과 독립적으로 진행하며, 사람 확인·검정력이 미충족이면 출시 품질은 차단된다.

### 9.3 단위별 완료 기준·독립 검증

아래는 **미실행 완료 조건**이다. 문서 해법이 있다는 이유로 통과 체크하지 않는다. 각 단위 작성자 외 검증 세션이 실제 DB 증거(명령·환경·SHA·stdout)를 남긴 뒤 다음 단위를 연다. KB4·KB10 검증자는 작성자의 반례 목록을 보기 전에 자신의 반례를 먼저 봉인하고 이후 목록과 합친다. 같은 계열 모델의 맹점을 줄이되 모델 다양성을 실행 증거 대용으로 쓰지 않는다.

| 단위 | 완료·반례·판정 |
|---|---|
| E0 | 손계산 3질문/10문서의 Recall·nDCG·MRR·abstain 일치, 군집/중복 누수·Holm·표본 부족·cache key 변조 음성 대조. DB manifest/관측 fixture 실행. deterministic replay 정확 일치와 실제 추론 미실행 상태를 분리. 모바일 중단/재개 확인 골격. P1–4 해법의 기계 판정부터 구현 |
| E1a | data/qrels/code/corpus 별 권리·원문 URL·시각·허용 split·학습 노출 상태, 고정 seed 및 모든 제외 건수. 공개 기준선 저장. 재배포 미확인 자료는 내보내지 않음. 사용자 승인·일반화라고 표시하면 실패 |
| E0.5 | P8 정책 양쪽 적용(필터 on/off 동일 조합), 동일 gold/실 corpus·실 avgdl·결정적 후보 절단, Recall@20/100·p95·쓰기/메모리·운영 복잡도 비교. full/BQ 실벡터 규모/필터 결과. 선택 근거·미실행 pg_search 명시. 결과 없으면 의존성 추가 확정 금지 |
| KB3 | 조사·OOV 100개·혼용·식별자·수치/단위·부정/이중부정·NFKC 경계의 독립 골든. 자체 산문 고정 표본 baseline의 처리량 ≥80%(동일 부하 상대 비교); corpus 구성·Kiwi RSS 기록. bm25_kiwi를 제품 LexicalIndex 포트로 호출하며 별도 하네스 검색 구현을 만들지 않음 |
| KB4 | 실제 DB migration forward/rollback와 기존 불변식, 관측 A→B→A/A→A/늦은 event/동률·미래 시각, pin 만료 경합·tombstone 인가 거부·복구 시험(P5–7). 일반 사용자 세션 변수 조작으로 UPDATE/DELETE 가능하면 실패. 기존 ctx/knowledge/collector 독립 DB 회귀 |
| KB5 | 수용 1–9의 양/음성 fixture, 네 NFC 반례·기존 생산자 재발행, parser 재처리, 재전송·다른 scope·rights·확장 변조. 입력=수용+거부+보류 검산. 제목뿐 passage 0, 300–1200자 목표≥90%는 토큰 상한 우선. anchor invalid=0인 적격 corpus에서 not_in_corpus=0; invalid를 제외해 성공률을 부풀리면 실패 |
| E1b | P2 anchor 검증·KB6 포함 풀 judged@k 확보·group split 누수 0. 사용자 전수 확인 조건 유지. P1 검정력/언어쌍 표본 부족이면 inconclusive. 확인 112개·2시간은 추정이며 완료 수로 기록 금지 |
| KB6 | P9 실제 PG 커밋 순서 역전·commit 중 kill·동일 구간 replay·checkpoint 단계별 crash, PG 기대 pid 수/hash 대조. P10 서버 하나/모델·3세대 메모리·swap-in·장애·긴 입력 배치 격리. P12 scope 10%/current 10% filtered recall, exact filtered 기준 대조·서로 다른 분석기 두 세대 동시 질의. BQ C200 top10 일치≥.97은 실벡터 측정 때만 적용; latency lexical≤20ms/dense≤30ms는 절대 목표로 별도 판정. 선택된 다른 경로에도 품질·자원 예산 적용 |
| KB7 | 응답 schema·fast/full·degraded·P11 보정·P13 conflict_check, 동일 조건 캐시. 사전 등록 holdout 1회. 전체 R@10 .80/nDCG@10 .65/MRR@10 .60·abstain F1 .80·근거 .90·조건 .80은 정책 목표이며 표본·검정력 충족 전 pass 금지. 1차 비교는 §4.7, 작은 유형별 CI>+.10/하락금지 제거. paraphrase/cross 후보 R@40 .85는 보고·확장 표본 확보 시 확정 gate. full .8s/fast .2s와 AB p95비율 모두 확인 |
| KB8 | 실제 Run 도구 선택 균형 표본 최소30개(일반15/개인15), 오선택률·CI 및 오선택≤1/30, 개인 질문 kb 호출0을 정책 smoke로 보고(일반화 근거 아님). 도구 설명 바뀌면 재시험. REST/MCP 목록 일치·응답32KB |
| KB9 | 별칭50개 원문 대조, 모호 PG/Go/Rust/배/Java 도메인 강제, C3 단독≤N의25%·weight≤.5. 질의별 순위 상승/하락·OOV100·숫자/날짜/단위·부정 반례. 소표본 +.03/CI≥0 게이트 대신 §4.7 |
| KB10 | 모든 전이·UNIQUE proposition_key·종결/reopen·evidence_lost·인용 재탐색·partial conflict·applies 상속·independence_basis 반례(P13/F24). T5/동일 Run 자기승격 금지. 수집10배에 추출호출 상한 불변, 모델 fallback이면 중단. 충돌 .80·조건 비열등은 검정력 확보 후; 부분 추출을 무충돌로 표시하면 실패 |
| KB11/12 | 고정 시계 stale/last_verified_at·권리 만료·현재성 시험. UI 320/390/768/1440 상태·권한·버전충돌, 사용자 API 외 verified 불가 |
| KB13 | 100만 통합 Recall 하락≤.03·full≤1s/lex≤30ms, 합성500만 lex/dense≤60ms·용량계획±30% 정책 목표. 부하/정책/표본과 외삽 차이 기록; 합성 시험으로 검색 품질 통과 주장 금지 |

---

## 10. 독립 검토에서 집중해 볼 위험

| # | 위험 | 왜 위험한가 | 확인 방법 |
|---|---|---|---|
| R1 | Tantivy 투영과 PG 정본의 불일치 | 색인에만 있거나 PG에만 있는 패시지, 현재성 변경 반영 지연. 검색 결과가 없는 패시지를 가리키면 근거 계약이 깨진다 | [F] 단계에서 PG 재확인을 생략하지 않는지, index_log 따라잡기 실패 시 동작 |
| R2 | 배포 세대 중첩에서 쓰기 주체·추론 서버 중복 | 세대가 셋까지 동시 실행된다. Tantivy 쓰기 잠금 경합, llama-server 두 벌로 GPU 메모리 초과 | 3.9의 소유 규칙이 generation drain과 맞는지. 호스트 단일 외부 서비스·구모델 동시지원·P10 3세대 시험 |
| R3 | 호스트 메모리 압박 | 측정 시 스왑 92%. 검토 RSS 서버1.55GB+Kiwi .54GB×3+선택 색인/cache/Metal 메모리 | 메모리 예산을 KB6 게이트에 넣었는지 |
| R4 | dense 기본화의 근거가 얕음 | 초기 8쌍·검토 18/20은 방향 확인 소표본이다. 패러프레이즈 이득, 희귀어 손실은 미측정 | KB7 게이트가 "다른 유형 하락 금지"를 실제로 막는지 |
| R5 | 언어쌍 coverage 보정의 표본 부족 | 재순위 모델의 부정형·버전 조건 처리 능력 미측정. τ가 분야마다 다를 수 있다 | P11 언어쌍별 false strong/false abstain·미보정 unknown 확인 |
| R6 | 사람 확인 예산 | 사용자 한 명이 holdout 112문항(약 2시간, `확인 필요`)과 근거 정확도 표본을 봐야 한다. 지연되면 게이트가 막힌다 | 모바일 UI E0·자체 확인 E1b 순서를 지켰는지, 대체 경로(공개 세트만으로 잠정 판정)를 허용할지 사용자 결정 필요 |
| R7 | NFC 저장 결정 | 기존 v1 계약(NFKC 저장)을 바꾼다. 두 정규화 방식 리비전이 공존한다 | 분석기가 두 경우를 같은 색인 토큰으로 만드는지 골든 테스트 |
| R8 | blob 공유와 collector 보존 정책 | collector 정리 작업이 kb 참조 blob을 지우면 원본 보존 원칙이 깨진다 | collector 소유 pin API·rights 우선 tombstone·복구가 P7 계약대로인지 |
| R9 | 명제 키 과분할 | 추출 품질이 낮으면 같은 사실이 값 표기 차이로 다른 명제가 되고, 독립 확인이 쌓이지 않는다 | facet 값 정규화 규칙과 병합 후보 큐 |
| R10 | 합성 데이터 측정의 외삽 | 어휘 크기 고정, 무작위 벡터, 부하 높은 호스트 | KB13 게이트가 외삽 ±30%를 확인하는지 |
| R11 | 라이선스 | HF 카드 표기만 확인했다. KorQuAD ND 같은 제약 | KB-E1 완료 기준의 원문 확인 |

---

## 11. 미확인 항목

| 항목 | 상태 |
|---|---|
| v1 독립 검토 원문 | 가드가 읽기 차단. 요청서 요약으로 대체 |
| 원 BAAI 모델 라이선스 원문, 데이터셋 라이선스 원문 | HF 카드 표기만 확인 |
| bge-m3 실제 재현율(패러프레이즈, 희귀어), 재순위 모델의 부정형·조건 처리 | 원8쌍/검토20쌍 방향 확인, MIRACL10만 공개 측정 있음. 자체 holdout·부정/버전 일반화는 미검증 |
| llama-server의 bge-m3 sparse·colbert 출력 지원 | 확인 필요 |
| ParadeDB `pg_search`의 Homebrew PG17 설치 가능 여부 | 확인 필요 |
| 100만·500만 건 벡터 색인 빌드 시간, BQ 일치율 | 외삽(10만 실측 기반) |
| 실제 텍스트의 TOAST 압축률, 어휘 증가에 따른 색인 크기 | 확인 필요 |
| PG에서 tsv·GIN을 뺄 때의 패시지 표 크기 | 확인 필요 |
| [F]·[A] 단계 지연 | 미측정 |
| 실제 적재 경로(ORM, 배치 INSERT) 처리량 | 미측정(COPY만 측정) |
| GPU 활용률 계측 방법(macOS 무권한) | 확인 필요 |
| desk_qwen_service의 세대 중첩 처리 | 검토자가 세대별 자식임을 코드로 확인. 이번 선택은 host 단일 외부 서비스이며 실제 수명/배포 시험은 미실행 |
| 0055 불변 트리거가 ADD COLUMN DEFAULT에 반응하지 않는지 | 확인 필요(마이그레이션 테스트로) |
| 한국은행·KOSIS 자료 이용 조건 | 확인 필요. 확인 전 사용 안 함 |
| 사람 확인 소요 시간(문항당 1분) | 추정 |
| 성능 목표치(Recall@10 0.80, p95 800ms 등) | 설계 예산, 실측 아님 |
| 배포 런타임이 로컬 DB와 같은지 | v1과 같이 확인 필요 |

---

## 12. 독립 검토 반영 설계 계약 (2026-10-21)

이 절 제목의 판정 단위는 **설계 해법 명시**다. 구현·실제 DB·모델 검증 통과는 모두 별도이며 이번에 실행하지 않았다. P별 책임은 역할 단위이며 담당자 실명 배정·사용자 승인으로 해석하지 않는다. 검토자의 원 수치·판정은 [원 보고서](reviews/v2-design-review.md)와 [출처 manifest](reviews/source-manifest.json) 그대로 보존한다. 현재 본문과 다른 역사적 M 표·부록 명령은 당시 측정 기록이다.

### P1 — F4: 통계 판정과 holdout 사용

**선택.** 질문 차이 d를 paired로 계산하되 원문 문서 group을 재표집 단위로 하는 cluster bootstrap(고정 seed, 10,000회)을 쓴다. 같은 문서·근사 중복·번역 문서가 두 split으로 갈 수 없다. 다중 문서 질문은 연결 component 전체를 group으로 묶는다. group 크기가 다르면 질문 가중 평균이라는 estimand를 유지하며 각 resample에서 뽑힌 group의 질문 합/질문 수로 계산한다. 원문의 `paired_interval`은 독립 질의 가정이라 그대로 gate에 쓰지 않는다.

- 1차 family는 자체 holdout Recall@10·nDCG@10 두 비교다. 각각 비열등 마진 Δ=.05를 정책 허용 최대 손실로 **사전 고정**한다. 관측값에 맞춰 마진을 넓혀 통과시키지 않는다. one-sided family α=.05, Holm step-down을 적용한다. bootstrap 차이를 영가설 경계 −Δ로 재중심화한 검정의 보정 p와 family CI를 함께 보고하며 표본 설계는 보수적 Bonferroni α=.025/비교로 잡는다. 대립 가설은 평균 차이 >−Δ. pass=모든 보정 하한>−Δ, fail=동시 상한<−Δ인 필수 지표 존재, 나머지 inconclusive. 확정 회귀가 아닌데 fail로 단정하지 않되 inconclusive도 다음 단계 차단이다.
- 검정력은 dev의 group 분산·크기·질문 상관을 보존한 모의 재표집으로 산출한다. 참 차이0에서 pass 확률≥.80와 −.10 손실에서 pass≤.05를 확인한다. 목표 검정력 .80은 정책이며 실측치가 아니다. 정규 근사 계획식 `n_eff ≈ ((z_(1−α)+z_.80)*sd/Δ)^2`. 검토 sd=.335, α=.025, Δ=.05를 넣으면 약353 **독립 유효 질문**이 필요하다. 군집이면 원문 질문수는 더 커진다. 112개가 충분하다는 주장을 폐기한다. 실제 power 결과 없는 gate는 `inconclusive: power_not_established`다.
- 분야·유형의 8–12문항 셀은 CI·표본수·사례를 보고할 뿐 veto로 쓰지 않는다. 특정 유형을 차단 gate로 올리려면 별도 표본 설계·power·family를 사전 등록하고 이후 숨김 세트를 모은다. 공개 데이터와 자체 holdout은 합산하여 n을 부풀리지 않는다.
- abstain은 dev/holdout **각각** 답 없음 최소40 + 답 있음 최소40을 초기 하한으로 둔다(near-miss≥절반). 언어쌍 보정별로도 이 하한을 요구하므로 초기 224개 계획이 늘어날 수 있다. 최소수는 충분성 보증이 아니다. false strong·false abstain의 이항/군집 CI를 각 분모와 보고한다. 정책 목표는 false strong≤.05, false abstain≤.20, F1≥.80. dev pilot 기반 power≥.80 및 CI 경계 충족 전에는 `inconclusive`이며 F1 점추정만으로 pass 금지. 오류율 0/40의 단측95% 상한도 약.072로 .05 미만 보증이 안 된다.
- holdout 개봉은 독립 검증자 1회, 비교·코드 SHA·모델·threshold·분모·기준선을 등록한 뒤 한다. 개발자는 dev를 쓴다. 결과 보고 수정한 순간 holdout을 소비하고 다음 판정은 새 숨김 set으로 한다. `holdout_peeks`는 0/1 및 열람자·시각·용도를 감사한다. 기존 3회 허용 규칙은 폐기. 여러 후보 탐색은 dev에서 하고 최종 한 후보만 holdout으로 보낸다. 공개 회귀 세트의 반복 사용은 횟수/적응 이력 공개하며 독립 검증으로 포장하지 않는다.

**반례/완료.** E0 평가 담당은 질문 독립 bootstrap이 좁은 CI를 만드는 상관 fixture, unequal group size, 누수 component, threshold 변경·holdout 재사용·40개 미만·중립 변경 power fixture를 만든다. 독립 검증자는 손계산·음성 대조·실제 DB split manifest를 확인한다. 통계 구현과 경험적 검정력은 이번 **미실행**, E0가 완료 책임이다.

### P2 — F5: 경계 변화에 견디는 gold와 출처 버전

**선택.** §4.4 anchor는 안정된 doc_key+원문 인용을 의미 식별자로 쓰고 `source_revision_id/raw_sha256/body_sha256/observed_at/source_uri`를 **인용의 출처 버전 근거로 계속 보존**한다. passage ID/offset은 매 run 파생 결과이고 gold의 정체가 아니다. 공개 qrels의 외부 doc/passage ID는 원 데이터의 라벨 provenance로 보존하고 내부 doc_key 대응표를 manifest에 둔다.

1. 동결 corpus에서 source hash·quote hash를 확인한다. 같은 저장 본문을 새 chunker로 나눈 경우 인용을 문자열 검색하여 codepoint span을 다시 찾는다. 일치가 여럿이면 앞뒤 문맥·절 제목으로 유일화하고 여전히 여러 개면 `anchor_ambiguous`로 판정 불가다.
2. 정확 일치가 안 되면 버전 고정 `NFKC+casefold+공백 접기` 비교와 **원문 좌표 매핑**으로 후보를 찾는다. 이 비교는 passage remap 보조일 뿐 수학·식별자 의미 동일성 증명이 아니다. `x²/x2`, `µ/μ`, `㈜/(주)`처럼 손실 정규화 충돌이면 자동 승인하지 않고 검토한다. 소비자 원문을 재작성하지 않는다.
3. revision body/source가 바뀌면 `gold_invalid: source_changed`로 표시하고 사람이 새 출처 버전을 재확인할 때까지 **일반화·회귀 gate에서 사용하지 않는다**. 새 본문에 같은 문자열이 있어도 그 문서가 정정했는지 알 수 없으므로 자동 유효화하지 않는다. normalization-only 이동도 새 mapping과 근거를 검토·버전 기록한다. “리비전 비의존”은 분할 경계 비의존이지 출처 시점 삭제가 아니다.
4. 원문에서 유효한 answer span과 겹치는 새 passage를 답으로 map한다. passage 단독으로 답을 지지하도록 quote의 ≥.8을 덮는 passage를 인정하고, 분할 경계에 걸리면 인접 passage의 최소 span-cover 집합을 `answer_id` 하나로 평가한다. answer-level Recall은 여러 chunk를 중복 정답으로 세지 않고 하나의 답이 충족됐는지 센다. 공개 benchmark native passage Recall과 자체 answer Recall은 별도 metric 이름으로 기록하여 수치 혼용을 막는다.
5. invalid·ambiguous를 조용히 분모에서 빼지 않는다. 수/원인·전체 분모를 보고하고 필수 gold가 하나라도 무효면 그 gate는 `input_invalid`다. `not_in_corpus`는 유효 anchor에 대응 passage가 없는 경우만이다.

**순서/완료.** E1a 공개 로더·기준선은 먼저 한다. 자체 gold 초안은 먼저 가능하지만 **KB5 정규화·분할 확정→KB6 dense/rerank pool 추가→E1b 사람 최종 확인/동결** 순이다. 사람이 기존 112개를 다 확인할지 공개로 잠정 진행할지는 미결이며 기존 인간 확인 조건을 유지한다. KB5/E1b 평가 담당은 단락 이동·경계 재분할·NFKC 길이 변화·중복 인용·출처 정정에서 remap/invalid를 독립 검증한다. 이번 anchor 실행은 미실행이다.

### P3 — F6: 부하 중 지연 판정

**선택.** load>9면 제외하는 규칙을 폐기한다. 후보와 기준선(고정 `bm25_kiwi+동일 reranker`, 같은 N·quote 길이·cache 정책)을 같은 호스트 시간 창에서 무작위 AB/BA block으로 번갈아 실행한다. “동시 기준선”은 같은 부하 창의 interleaved control이며 둘이 GPU를 동시에 독점하게 하여 상호간섭을 새로 만들지 않는다. 동시성1/4, warm/cold 별 조건을 고정하고 워밍업1회 뒤3회 반복한다. 최소100 query pairs/조건, 10분 관찰을 초기 하한으로 삼고 p95 ratio CI 안정성은 E0 pilot에서 정한다(미실행).

end-to-end=admission wait+서비스 대기+내부 처리+직렬화/전송이며 critical path 시간과 stage별 합(병렬이므로 단순 합 불가)을 함께 기록한다. 모델 queue wait와 tokenize/prefill/inference 처리, DB pool wait와 execute를 분리한다. 측정 불가 내부항목은 missing 사유를 쓰고 e2e를 계속 판정한다.

- 상대 회귀: bootstrap block CI의 `p95(candidate)/p95(control)` 상한≤1.20. 양 시스템의 부하/대기 분포와 AB 순서 불균형을 검사한다. 둘 다 느려져 비율만 좋아도 절대 통과가 아니다.
- 절대 SLO: full p95≤800ms·fast≤200ms는 **관찰된 공유 부하 조건에서의 e2e** 목표다. load·CPU·동시 Run·swap-in·server queue를 함께 적고 초과는 `absolute_fail`로 남긴다. 높은 load라는 이유로 미판정 처리하지 않는다. 비교 baseline도 SLO를 못 지키면 host capacity 원인 후보로 분류하되 통과로 바꾸지 않는다.
- 기준선이 없거나 계측·부하 profile이 비교 불가면 `relative_unverified`로 반환한다. 준비한 별도 제한 부하 profile을 측정할 수는 있지만 profile을 바꾼 시험을 운영 SLO 통과로 대체하지 않는다. 자동 N 감소는 별도 query_policy_version으로 평가·coverage 재보정해야 한다.

**반례/완료.** E0 성능 담당: 양쪽에 동일 대기만 추가, 후보 내부비용만 +30%, AB 순서 부하 치우침, control도 .8s 초과 fixture를 검사한다. E0 타이머·ratio 판정, KB6/7 실제 서버·DB 부하 시험으로 닫는다. 이번 부하 지연은 재측정하지 않았다.

### P4 — F13: replay와 실추론 재현성

**선택.** cache key는 `(model file sha, quantization, server binary/build hash, tokenizer/pooling, normalized input bytes sha, query/passage role, truncation/window policy, analyzer/query-policy version)`이다. rerank는 질의·패시지 둘, context/조건·N 정책을 포함한다. build_id·candidate snapshot과 벡터·점수를 run artifact에 고정한다. 빈 cache를 “같은 run replay”라고 부르지 않는다.

- 캐시 replay: 같은 corpus/gold/build/run cache에서 후보 pid·score bytes·정렬·metric이 **100% 일치**해야 한다. 동률 tie-breaker는 stable doc_key/revision/passage key다. checkpoint·wall-clock 관측값은 비교 대상에서 제외하고 목록을 schema에 명시한다.
- 실제 추론: 고정 build·같은 서버 profile에서 벡터 cosine 차≤1e−4, rerank 원시 로짓 절대차 ε_s=1e−3를 초기 engineering tolerance로 등록한다(미검증 정책, 모델별 E0/KB6 pilot에서 **holdout 보기 전에** 확정). 기준이 실측과 맞지 않으면 원인·새 calibration version을 남기며 조용히 완화하지 않는다.
- 엄격 점수차>2ε_s인 결과의 상대 순서·경계 안팎은 같아야 한다. near-tie는 기준 top-k 경계 점수±2ε_s 집합 안에서만 교환 가능하며 공통 stable top-k overlap≥.99(질의 평균), 모든 변경은 near-tie로 설명되어야 한다. 단일 k=10에서 1개 바뀐 .9도 질의별로 드러내고 경계 near-tie에 속하는지 명시한다. stable core는100% 일치해야 하며 near-tie로 설명되지 않는 변경은 평균 overlap과 무관하게 실패다. threshold 양옆 ε_s 구간은 strong/none 확정 대신 weak/unknown으로 경계 표시한다.
- HNSW 재빌드는 별도 비결정 경로다. frozen embedding의 exact search 기준 recall과 품질·자원 gate를 비교하며 “같은 순위 100%”를 요구하지 않는다. 캐시 replay 검사와 실추론/새 HNSW build 검사는 별개 기록이다.

**반례/완료.** E0 평가 담당이 cache role·모델 hash·입력1byte·N/window 정책 변경으로 miss를 확인하고, near-tie 안/밖·threshold 경계 fixture를 시험한다. KB6 실제 단건/배치/동시 추론 반복은 아직 미실행이다. 검토 det.py의 16/64 exact vectors만으로 tolerance 통과를 단정하지 않는다.

### P5 — F1: 수집 관측 ledger와 현재성

**선택.** 내용 주소 `kb_sources(source_key,raw_sha)`·파서 리비전과 수집 사건 `kb_observations`를 분리한다. 새 관측마다 `{observation_id, document_id, source_id, event_time, receipt_time, envelope_id, receipt_id, external_version, clock_status, parser_policy_id}`를 남긴다. event_time은 생산자가 실제 관측한 `source.collected_at`, receipt_time은 DB 수용 시간이다. 재전송은 같은 observation/envelope ID로 멱등이며 **새 수집 A→A는 새 ID**를 가져야 한다. 거부/보류 수집도 receipt 감사에 남기되 현재성 eligible 관측과 구분한다.

현재성은 문서 lock 아래 eligible observation의 event_time 최대가 가리키는 source를 먼저 고른다. 그 source에 대해 활성 parser/normalizer/chunker 정책의 성공 리비전을 고른다. 단순 전역 최대 rev_no·최초 sources.collected_at은 쓰지 않는다. 파서 업그레이드의 재처리 event는 관측 freshness를 갱신하지 않는다. 파서 준비 미완이면 이전 서비스 리비전을 `processing_pending`으로 표시하고 새 source를 과거 원본인 것처럼 숨기지 않는다.

- t1:A, t2:B, t3:A → source A 재사용, observation 3개, current A의 활성 parser revision. A→A도 observation은 추가되며 `last_verified_at=max(eligible event_time)`가 된다.
- receipt t4에 event t0의 옛 snapshot 도착 → 역사로 저장, current/last_verified는 t3 유지. receipt_time이 최신이라는 이유로 바꾸지 않는다.
- event_time 동률·상충 snapshot은 권위 있는 외부 version order가 있으면 그것을 쓰고, 없으면 `current_unresolved` 검토 큐(기존 확정 current 유지+표시). 임의 UUID나 receipt order로 사실 순서를 발명하지 않는다. 미래/시계 이상은 `clock_suspect` 보류, 허용 시계 편차는 producer 계약에 버전 고정 후 적용한다.
- 성공 관측 선택·문서 current pointer·index membership 변화·receipt는 같은 트랜잭션이다. current 변경은 옛/current pid upsert/delete 이벤트를 모두 내며 A 복귀가 옛 passage를 되살려도 새 current generation과 authorization fence를 적용한다.

**반례/완료.** KB4 원장 담당이 실제 DB A→B→A/A→A/역순 event 수용·동률·동시 parser·전달 중복을 검증한다. KB5 collector owner에게 stable observation identity·event time 요구를 반환한다. 이번 DB 전이는 미실행이다.

### P6 — F10: 생산자 NFC 경계 계약

**선택 (권장안 수용).** 생산자의 정규화 순서를 `decode/LF → zero-width/control 제거 → space collapse/trim → 최종 NFC → UTF-8 sha256`로 고정하고 normalizer_version을 올린다. 비교 NFKC는 저장 경로와 분리한다. KB는 받은 text를 다시 정규화·재해시하지 않는다. strict NFC 수용은 **새 생산자 버전 계약**과 함께 활성화한다. 옛 버전의 비NFC envelope는 `producer_contract_mismatch`로 격리·receipt 기록·새 envelope ID와 새 payload hash로 재발행하도록 요청하며 같은 ID payload를 바꿔 멱등 검사를 피하지 않는다. 생산자 갱신 전에 NFC strict sink를 운영 활성화하지 않는다.

경계 fixture는 `e\u00ad\u0301`, `\u1112\u200d\u1161`, `a\u200c\u0300`, `e\u0008\u0301` 네 입력이다. 제거로 결합 문자가 다시 인접한 뒤 NFC가 고정점이어야 한다. 한글 자모·ZWJ/ZWNJ·soft hyphen/control, LF/CRLF·공백·빈 본문·NUL 거부·재정규화 멱등·sha(text) 일치를 검사한다. 단순 첫 NFC+문자 삭제만으로 고정점이 된다고 보지 않는다.

**책임/완료.** KB5 어댑터/collector 계약 담당. collector `textproc`의 마지막 NFC·normalizer version·hash 계산·재발행 코드/fixture는 **타 작업 소유자에게 반환할 변경 목록**이며 이번에는 어떤 collector 제품 코드도 수정하지 않는다. [collector 변경 요청](reviews/collector-contract-followups.md)을 반환한다. 이 계약은 합의·구현·DB 검증 전 KB5 활성화 차단이다.

### P7 — F11: 단방향 pin·tombstone·복구

**선택.** collector가 `collector_blob_pins(handle, owner_namespace, owner_key, lease/rights metadata)`와 BlobStore `pin/unpin/revoke/status` port를 소유한다. kb 어댑터만 호출하며 collector는 kb schema/import/참조 검사를 하지 않는다. 같은 PG에서 pin 수용과 kb receipt를 하나의 트랜잭션으로 결속할 수 있는 adapter API를 계약으로 제공한다. 분리 DB이면 권리 확인된 임시 pin 예약→kb commit→확정의 멱등 saga가 필요하며 계약 없이 blob GC를 금지한다. 정상 GC는 자기 pin만 보고, 만료·권리 철회·사용자 삭제는 pin보다 우선한다.

`tombstone(request_id, target_doc/source, reason_class, authority_ref, actor_id, requested_at, scope, expected_version)`는 append-only 비내용 감사 이벤트다. 임의 session variable로 불변 트리거를 끄지 않는다. 평상시 app role은 원장 UPDATE/DELETE/TRUNCATE·tombstone 함수 EXECUTE 권한이 없다. 별도 deletion worker login role만 승인된 요청을 처리한다. 고정 search_path·스키마 한정 SECURITY DEFINER 함수는 PUBLIC EXECUTE를 revoke하고 승인 요청의 대상·tenant·actor·단계·현재 transaction identity를 검증한다. 전용 NOLOGIN owner role만 content clear 권한을 갖는다. trigger는 전용 함수 소유 context+승인 요청/현재 tx 연계를 함께 확인하며, 일반 app이 GUC·request_id를 위조해도 통과할 수 없다. 소유자/슈퍼유저는 별도 운영 경계로 감사하며 DB 인가 검증을 생략하지 않는다. 이는 PostgreSQL [SECURITY DEFINER 안전 사용](https://www.postgresql.org/docs/17/sql-createfunction.html)의 권한/search_path 전제를 따라 설계한 것이며 아직 구현 시험하지 않았다.

삭제 순서:

1. 요청 승인 뒤 권리/접근 revocation fence를 PG에 commit한다. 검색·kb_get·BlobStore 읽기는 이 fence를 가장 먼저 확인하여 파생물 정리 중에도 반환하지 않는다. collector 권리 만료는 명시 revoke event를 내고 재시작 때 overdue도 재조정한다.
2. 제한 함수가 본문·quote payload를 비우고 tombstone metadata·원본 hash/이벤트 연결을 남긴다. schema에는 tombstoned/payload_state를 추가하고 empty/NULL payload와 원래 hash/길이를 구분한다. 일반 insert 해시 검증은 유지하고 승인된 내용 제거 경로만 payload-state 제약을 만족하도록 별도 검증한다. tombstone 원문을 정상 본문으로 재해시·재인용하지 않는다. hash 자체가 개인 식별 가능하면 비내용 salted commitment 또는 키 파기 방식으로 최소화하고 무조건 원해시 영구 보존이라고 약속하지 않는다. 일반 append-only 원장은 그대로이며 이 **인가된 내용 제거 예외**만 별도 audit로 남긴다.
3. 정리 대상은 raw blob(모든 owner 참조 권리 확인), revision.body, passage.body, evidence quote, 분석 tokens, lexical/doc index, vectors, query/embedding/rerank cache, claim 추출 캐시·statement 내 원문 복제, 라벨/평가 corpus·export artifact·read replica를 포함한다. 다중 source 공유 blob은 같은 byte를 재노출할 수 있는 alias도 fence한다. 비내용 사건 관계만 남긴다. `kb_index_log op=tombstone`은 우선 처리하며 모든 버전 tuple build·구세대가 적용해야 한다.
4. worker는 대상별 완료 manifest·cursor·재시도 기록을 남기고 crash 뒤 같은 request_id로 재개한다. 물리 색인은 delete commit 뒤 segment merge/old build 제거까지 잔류 byte가 있으므로 “접근 차단”과 “물리 제거” 상태를 분리한다. 회복 실패는 `purge_pending`으로 남고 반환 금지는 유지한다. 요청을 끝냈다고 숨기지 않는다.
5. 복제본·백업·기발행 export에는 revocation 전파 목록과 ack/최종 만료시각을 둔다. 보유 백업은 암호화 key 폐기 또는 보존 만료의 검증 가능한 경로가 있어야 하며, immutable 백업의 즉시 물리 삭제를 보증하지 않는다. 복원 시 서비스 개방 **이전** tombstone 원장을 재적용한다. 미회수 export는 `external_copy_unverified`; 사용자에게 삭제 성공으로 표시하지 않는다. 새 ingest/replay도 fence를 확인하여 부활을 막는다.

**반례/완료.** KB4 제한 함수·audit/DB 인가, KB5 pin producer API, KB6 모든 build/cache purge·복원 ack를 각 소유자가 닫는다. 실제 DB app-role GUC/함수/직접 DML 공격, pin/만료 경합, 삭제 중 kill·재개·구세대 stale index·백업 복원 후 재노출 0을 독립 검증한다. 권리 세부 법률 해석은 이 설계의 실행 보증이 아니며 이번 삭제는 미실행이다.

### P8 — F3: 어휘·벡터 선택의 증거 게이트

**선택.** Tantivy 채택·PG 전체 불가 주장을 철회한다. KB-E0.5에서 동일 `LexicalIndex` port·query_policy_version, 같은 Kiwi tokens·stop/df 정책(on/off의 동일 arm), 동일 적격 scope/current corpus·native qrels·query set으로 비교한다. PG 단독은 GIN 후보+앱/SQL BM25를 포함하고 LIMIT2000 절단 순서·cutoff trace를 결정적으로 고정한다. Tantivy도 같은 df 제거 정책을 적용한다. BM25 k1/b·실제 avgdl·IDF 정의 차이를 명시하고 점수 동등성을 보장하지 못하면 recall/운영 비교로 한정한다. `pg_search`는 설치 가능할 때만 arm에 포함하며 미설치도 명시 결과다.

실텍스트 100만의 자연 표본과 qrels-positive enrichment 표본을 분리하고 positive 밀도·seed·누락 분모를 기록한다. 같은 gold와 query policy를 양쪽에 적용하여 Recall@20/100·후보 누락·p95·index/write catchup·build·memory·crash/운영 복잡도를 함께 판단한다. 선택 기준은 먼저 유효 gold·사전 등록 비열등 gate, 다음 절대 지연/메모리 예산, 둘 다 충족하면 외부 투영·복구 비용이 적은 쪽이다. 성능 차이를 보고 기준을 바꾸지 않는다. 측정 전 `tantivy`/`pg_search` 제품 의존성을 추가 확정하지 않는다.

검토 R1은 [원 raw](reviews/evidence/lexcmp_1m.out.txt)의 **213질의/실텍스트100만**, PG df≤1%+LIMIT2000+앱BM25 p95 **19.71ms**, Recall@100 **.878**; Tantivy **8.27ms/.851**; PG 순정 ts_rank_cd **3501.07ms/.391**이다. query policy가 다르고 positive enrichment(판정 문서2,835개 포함), avgdl=40 근사, 부하 load12–40·swap37–44GB, 질의 소표본이라는 조건을 함께 보존한다. PG가 더 우수하다고 확정하거나 현재 실측으로 재포장하지 않는다. 원 합성의 OR median_hits=100%는 PG 최악 경로이고 실제 OR 매칭 중앙값4.3%와 다르다.

BQ는 full HNSW와 **실벡터100만**·같은 filtered exact 기준·언어쌍/질의 gold에서 비교한 뒤 확정한다. M10 .989는 1,815개 **문서간** 근접 top10 overlap(C100), 검색 정확도가 아니다. 검토 10만 질의→passage BQ C200 .997은 방향 근거이며 100만 증명 아님. 교차 언어18/20 역시 후보77개 소표본 방향 확인이다.

**완료/책임.** E0.5 검색/평가 담당, 독립 검증은 동일 정책 config hash·군집 CI·AB 지연·중복/빈 결과/흔한 질의 반례·실데이터 선별 편향을 확인한다. 이번에는 million benchmark·공유 GPU 기동 없이 원 raw를 보존했다. pg_search 및 실벡터100만은 여전히 미실행이다.

### P9 — F2: commit 순서·멱등 replay·durable checkpoint

**선택.** PG 단독이면 같은 DB index의 트랜잭션 일관성을 쓰고 외부 lexical projection을 추가하지 않는다. 외부 투영을 선택할 경우 **하나의 전역 transaction advisory lock으로 모든 index-affecting 쓰기 트랜잭션을 직렬화**한다. xid+xmin 방식은 선택하지 않는다. xid만 기록하면 commit 순서/가시성/long-running snapshot·wraparound를 저절로 해결하지 못하고, xmin 안전 경계·xid8 비교·해당 경계 이하 모든 행 스캔/진전 증명을 추가해야 하므로 이번 설계에는 복잡도가 더 크다.

잠금이 실제 누락을 막는 조건:

1. 수용·CLI·사용자 수정·scope/current 변경·tombstone·재색인 outbox 등 **모든** index-affecting transaction이 동일 DB의 동일 lock key를 **첫 seq 할당 전 및 정본 membership 변경 전** 획득하고 commit/rollback까지 유지한다. 파일 처리·분석·추론은 잠금 밖에서 준비하고 잠금 안에서 권리/버전을 재확인한다. 여러 문서 lock도 전역 lock 뒤 고정 순서로 취한다.
2. log sequence는 `CACHE 1`, 사전 `nextval` 예약/클라이언트 seq 지정/sequence reset/reuse 금지다. CACHE>1이면 서로 다른 connection이 낮은 번호를 뒤에 쓸 수 있어 lock만으로 부족하다. 일반 app에는 direct INSERT/nextval 권한을 주지 않고 제한 mutation 함수가 **lock 획득 후** 내부 할당한다. backfill도 동일 경로다. 한 batch가 거대 transaction으로 잠금을 독점하지 않도록 500 passage 이하를 초기 상한으로 두고 실제 lock wait·throughput을 gate에 기록한다.
3. 그러면 commit된 최대 seq를 본 시점에 더 작은 seq가 뒤늦게 commit될 수 없다. rollback gap은 가능하지만 뒤에 채워지지 않으므로 안전하다. transaction 종료에 잠금이 해제되는 성질은 [PG17 advisory locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS)에 근거하며 이 전체 protocol의 안전성은 **추론**, 실제 DB 검증은 KB6다. lock 획득을 호출자 관례에만 맡기거나 seq default가 lock보다 먼저 실행되면 이 증명이 깨진다.

외부 writer는 `(projection_kind,version tuple,build_id)`당 하나다. DB session advisory owner lease+파일 writer lock을 함께 쓰고 세대 draining이면 새 batch를 시작하지 않는다. DB connection을 잃으면 쓰기를 중지한다. drain은 진행 batch durable commit 후 객체/file lock을 명시 해제한다. generation.work만으로 host file 소유권을 증명하지 않는다.

재생은 같은 pid의 batch events를 마지막 desired state로 coalesce하고 **delete(pid)→현재 PG state를 add, 또는 delete만** 적용한다. 동일 pid를 같은 commit 안에 여러 번 add하지 않는다. tombstone/fence·old current는 add 금지. 한번 더 재생해도 pid 하나와 같은 내용 hash이며 scope/current metadata도 덮는다. 집계 count뿐 아니라 sorted pid+content digest로 PG 기대 집합을 검증한다.

reader는 `WHERE seq > checkpoint ORDER BY seq ASC LIMIT batch`로 같은 snapshot의 구간을 읽는다. 낮은 seq를 건너뛰는 SKIP LOCKED/역순 절단은 금지하며 읽은 구간의 모든 event를 적용한 최대 seq까지만 진전한다. checkpoint 순서는 ①log batch 읽기 ②delete/add ③외부 index commit 및 durable flush 완료 ④commit 파일 목록/hash·build_id·last_seq를 sidecar 임시파일에 쓰고 fsync→atomic rename→parent dir fsync ⑤PG checkpoint를 그 durable manifest까지 갱신이다. PG checkpoint를 먼저 올리지 않는다. sidecar는 index와 원자적이라고 가정하지 않는다. 시작 시 index fingerprint/manifest/PG가 안 맞으면 마지막 검증된 immutable build에서 재생 또는 전체 rebuild한다. 알 수 없는 상태로 last_seq를 추측해 진전시키지 않는다. index commit 후③~④ 사이 crash는 이전 checkpoint에서 멱등 재생하고,⑤ 뒤 crash는 fingerprint를 검증 후 재개한다. 활성 build pointer는 이 검증을 마친 후 별도 PG transaction으로 교체한다. 사용 라이브러리의 durable flush 보장이 확인되지 않으면 checkpoint 활성화 차단이다.

**대가/완료.** 쓰기 병렬성·긴 transaction 처리량을 포기하여 누락 방지의 증명을 단순화한다. KB6 검색/DB 담당이 실제 PG 두 connection seq10 지연/seq11 역전 시도를 먼저 실패 재현하고, 잠금 경로에서는 역전이 차단되는지 시험한다. lock 생략·CACHE32·nextval 선할당을 negative control로 삼는다. index commit 직전/도중/직후, sidecar rename 전후, PG cp 전후 worker kill 및 중복 replay·권한우회·drain 재개를 시험하고 pid 수/hash·checkpoint≤durable state를 확인한다. 이번 실제 DB/kill 시험은 미실행이다.

### P10 — F7/F14: 추론 수명·장애·토큰·메모리

**선택.** 호스트 서비스 감독자가 bge embedding/rerank를 각각 모델 해시당 하나만 소유한다. 세대는 외부 URL client로 접속하고 서비스 시작/종료를 하지 않는다. 기존 desk_qwen 자식 방식은 세대마다 생기므로 단일 소유의 선례로 삼지 않는다. 감독자가 model-id별 소유 lock+endpoint registry+health/config hash를 관리하고 구세대가 참조한 모델은 lease가 끝날 때까지 보존한다. 서로 다른 구/신 모델의 동시 상주도 P10 예산 안에서만 허용하며 불가능하면 모델 전환을 drain 이후로 직렬화한다. 현재 Run에서 launchd/서버를 조작하지 않는다.

초기 프로파일은 검토 바이너리 `llama-b11655`, Q8_0 embedding `--embedding --pooling cls --parallel 4 --ctx-size 8192 -b 2048 -ub 2048`, rerank `--reranking`과 같은 batch/ctx 한도를 **후보 설정**으로 고정한다. 이것을 모든 긴 입력 지원 보증으로 쓰지 않는다. 실제 슬롯 총한도2048/tokenizer special tokens·제목·질의·passage template를 `/tokenize`로 검사한다. 클라이언트는 전체 serialized input≤2048를 보장하며 query/title/template를 먼저 세고 남는 budget 안에 원문 정렬 가능한 passage window를 넣는다. query 하나가 상한을 넘으면 `query_too_long`으로 거부한다. 글자수 `[:700]`으로 문제를 숨기지 않는다.

긴 passage는 문장·표 행·코드 경계로 **여러 token-bounded embedding window**로 나누고 parent passage+원문 span을 저장한다. retrieval은 window를 찾고 parent를 중복 접는다. rerank는 질문에 맞는 window를 budget 내 결합하며 생략 범위를 `truncated_context`로 표시한다. passage 원문·quote를 조용히 잘라 바꾸지 않는다. 정확 token counting 불가면 근사만으로 서버 안전을 보증하지 않고 `token_budget_unverified`로 격리한다. 한 배치 실패 시 bounded binary split/항목 단위 재시도(최대2회/항목)로 문제 입력만 `embedding_failed`로 남기며 정상 입력을 잃지 않는다.

장애 정책: embedding 서버 불가이면 `dense_unavailable`과 언어쌍 미검증을 표시하고 lexical+alias 결과는 coverage unknown/weak까지만 반환한다. CPU query embedding은 **같은 모델**·동일 tolerance/품질·p95 CPU 예산≤200ms가 KB6에서 검증된 profile에만 opt-in으로 허용한다. 미검증이면 CPU를 자동 기동하지 않는다. CPU backfill은 기본 중지이며 운영자가 정한 quota 안에서만 재개한다. M8 초당6.1은 원 설계 단일 측정·검토 미재현이므로 100만/6.1=약45.54시간은 **계산**, SLO 보증 아님. GPU 목표100만≤6시간(정책)은 실제 부하 측정 뒤 KB6 판정하며 못 지키면 backlog/degraded와 ETA를 보고한다. 재순위 장애는 fast+coverage unknown, 모델 자동 경량 fallback은 금지다.

| 64GB 호스트 KB 초기 예산 (미측정 정책) | 상한/계산 |
|---|---|
| 서버2개 RSS | 검토 .863+.687=1.550GB 관측; Metal 총량 아님. 단일모델쌍 RSS 계획상한2GB, Metal 포함 총량 별도 측정 |
| Kiwi 3세대 | .540GB×3=1.620GB 관측기반 계획, 상한2GB. 재색인 worker 풀은 별도 quota 안에서 허용 |
| 색인/벡터/OS working set | 선택 lexical+vector hot set·구 build overlap 상한6GB(설계 예산, 100만 기준 재측정) |
| query/embedding/rerank cache | 전체1GB·항목/TTL 제한, model/tenant/scope 분리, tombstone 즉시 무효화 |
| KB 전체 증분 unified memory | 상한12GB(위 상한11GB+관리 여유1GB); 단순 RSS 합을 총량이라고 부르지 않음. Qwen 등 기존 호스트 부하와 함께 3세대 peak 검증 |
| swap-in / pressure | 10분 관찰 중 60초 rolling swap-in 평균≤1MiB/s, pressure critical 0. 기준 호스트만 이미 초과해도 capacity fail로 기록하며 KB가 통과했다고 하지 않음 |

**완료/책임.** KB6 운영/검색 담당: 3세대 join/drain 중 모델 hash당 server 수1, endpoint 소유권·lease·구모델, 서버 kill→degraded, CPU 미검증 거부, 토큰2047/2048/2049(완성 serialized 입력 기준), 표·코드2356토큰·한 문제+정상31개 배치, cache·Kiwi pool·swap-in 예산을 실제 DB/서비스로 독립 검증한다. GPU 메모리 관측이 권한상 안 되면 `unverified`이며 RSS로 대신 통과하지 않는다. 이번 GPU·서버·CPU 검증은 미실행이다.

### P11 — F9a: 언어쌍 coverage와 근접 오답

**선택.** `(q_lang,source_lang,model/quantization,N,query_policy)`별 dev calibration을 쓴다. 기본 ko→ko, ko→en, en→ko, en→en 각각을 별도 보고하며 평가 corpus에 없는 조합은 calibrated라고 부르지 않는다. P1의 split별 답 있음/없음40+40 하한과 near-miss 절반을 언어쌍마다 요구하고 power 불충족은 unknown이다. 분야 차이는 dev에서 점검해 충분한 표본 확보 시 별도 calibration group을 만들며 작은 셀별 임계값을 과적합하지 않는다.

τ_none/τ_strong은 dev에서만 선정한다. strong에는 재순위 점수·span 존재뿐 아니라 **질문이 요구한 사실/관계·수치·극성·시점/버전이 인용으로 지지되는지** 검사 결과가 필요하다. 어휘 겹침은 승인 별칭/양언어 concept mapping으로 보완하고 단독 strong 근거로 쓰지 않는다. 일반 관계 지지 판정의 신뢰가 미검증이면 strong을 주지 않는 보수적 정책이며 향후 상위 모델 판정 도입도 별도 하네스 검증이 필요하다.

false strong은 “답 없음 또는 잘못된 답인데 strong 반환”/해당 질문 수, false abstain은 “답 있는데 none 반환”/답 있음 수로 분모를 분리한다. 답 없음 전체의 none recall/F1·답 있는 오류도 별도 보고한다. near-miss는 GNI/GNP 혼동, 환율/기준금리, 에베레스트 해발고도/끓는점, 같은 버전의 반대 기능, 수치 단위·이중부정 등을 포함한다. 검토 단일 최적 τ=−2.29·18/20을 운영 임계값으로 가져오지 않는다.

**완료/책임.** KB7 검색/평가 담당이 dev 보정 파일·split·분모·threshold hash를 고정하고 독립 검증자가 holdout 한 번에서 두 오류의 CI·power·unknown 동작을 확인한다. 모델/N/후보 정책/원문 언어가 바뀌면 재보정한다. 실제 보정은 이번 미실행이다.

### P12 — F15/F17: 사전 scope/current·filtered dense·세대 호환

**선택.** 인증 principal에서 얻은 tenant+허용 scope는 사용자 인자로 넓힐 수 없다. C1/C2/C3 모든 후보 생성에 tenant/scope/domain·tombstone fence·current 조건을 적용하고 PG 최종 재확인도 유지한다. 기본 current-only lexical projection은 관측 현재 source의 활성 parser revision만 포함하고 비현재 passage를 제거한다. 역사 검색은 명시 `history`/`as_of` 권한·모드를 쓰며 기본 current와 합치지 않는다. event-time snapshot as_of와 claim valid-time을 별도 필드/모드로 구분한다. 단순 `as_of`가 어느 시간을 뜻하는지 모호하면 요청을 되묻는다.

PG dense는 허용 visibility partition을 선택한 뒤 WHERE 필터와 pgvector iterative_scan을 사용한다. ANN 내부는 필터 후 후보 부족이 생길 수 있으므로 [pgvector iterative scans](https://github.com/pgvector/pgvector#iterative-index-scans)의 `strict_order`/`relaxed_order`, ef_search/max_scan_tuples·scan memory 한도를 profile에 고정한다. iterative scan을 켰다고 exact 사전 필터 recall이 보장되는 것은 아니다. scope/current 적격 비율100/10/1%, 옛 revision90%, heavy duplicates의 **filtered exact full-vector top-k**를 기준으로 recall을 잰다. 기본 exact 대비 ANN Recall@20≥.95·예산 초과0(설계 목표); 부족하면 늘린 bounded scan→작은 적격집합 exact fallback→`filtered_recall_unverified` 순으로 처리한다. 반환0을 `no_candidate`로만 숨기지 않는다. 1% case가 실제로 지연을 못 지키면 partition/엔진 선택을 재검토하며 통과를 주장하지 않는다.

build registry 키는 `(tenant/visibility partition, analyzer_version, embedding_model_id, quantization, query_policy_version, index_schema_version)`이다. 세대 시작 시 지원 tuple을 선언하고 해당 tuple 활성 build·모델 endpoint를 reader lease로 pin한다. 전역 포인터 하나를 새 분석기로 바꾸지 않는다. 새 tuple은 backfill·검증 뒤 그 tuple 포인터만 원자 교체한다. 구세대 draining·진행 query가 참조하는 구 tuple/build/model은 GC하지 않는다. rollback도 지원 tuple으로만 돌아가며 tombstone fence는 모든 tuple에 우선 적용한다. 필요한 구/신 모델 동시 메모리 예산이 안 되면 모델 변경을 drain 이후로 미룬다(세대 일반 배포를 불필요하게 막는 규칙은 아님).

**완료/책임.** KB6 검색/세대 담당: 다른 analyzer 두 세대 동시 질의에서 각자의 토큰/build 정확 일치, model endpoint mismatch 거부, scope10/1%·과거90%·duplicate filtered exact 비교, history mode 현재성 혼합 금지, stale index의 권한/삭제 차단·reader lease GC 음성 테스트. KB7 API 담당은 history와 valid-time schema를 닫는다. 이번 필터/세대 DB 검증은 미실행이다.

### P13 — F8/F9: 명제 유일성·종결/재개·인용 손실·충돌 범위

**선택.** `proposition_key UNIQUE`를 유지하고 동일 명제를 새 행으로 만들지 않는다. superseded/retracted는 현재 episode의 닫힘이며 **reopen은 같은 claim_id의 새 이벤트**다. `kb_claim_events UNIQUE(claim_id,version)` 낙관 잠금으로 `(expected_version,actor,reason,evidence_ids,prior_state,next_state)`를 원자 기록한다. 동일 재시도 event_id는 멱등, version 충돌은 재검토한다. 바뀐 값/조건/극성/valid라면 다른 proposition_key이며 기존 명제의 재개와 혼동하지 않는다.

새 리비전에서 인용을 못 찾으면 먼저 저장 인용의 정확 검색→공백/NFC 불변 매칭→원문 offset remap·문단 이동 검색을 한다. 출처 본문이 달라진 경우 의미상 같은지 상위 검토자가 확인하며 인용 재부착 이벤트를 남긴다. 아직 못 찾은 것은 `evidence_lost`+review queue이고 404/410·재수집 실패·수요 추출 미실행과 마찬가지로 **자동 retracted/superseded 금지**다. 활성 진술 부족은 review_level/canonical eligibility를 낮출 수 있으나 명제가 거짓이라는 판정을 발명하지 않는다. 자동 철회는 검증 가능한 출처의 명시 철회 이벤트만, superseded도 명시 정정·비교 가능한 새 근거 또는 사용자 지정을 요구한다.

충돌 탐지는 추출된 group 안에서만 가능하므로 응답마다 `conflict_check:{status:complete|partial|not_run,extracted_passages_in_group,candidate_passages_unextracted,scope,build_id,checked_at}`를 넣는다. complete도 **표시된 query candidate snapshot/조건 범위**에만 해당하며 전체 corpus 무충돌 보증이 아니다. 후보 미추출>0·scope 밖·예산 부족·추출 오류면 partial이다. `conflicts=[]`만으로 무충돌이라고 말하지 않는다. `kb_get(extract_missing=true)`는 명시 쓰기 권한·예산 예약 작업 ID를 반환하는 별도 경로로 정의하고 읽기 호출이 몰래 과금·상태 변경하지 않게 한다.

**완료/책임.** KB10 원장 담당: evidence_lost→새 근거→같은 key reopen, 명제 값 변경은 새 key, 명시 철회만 자동 전이, 인용 이동/NFC/새 chunker/404, version 경합·duplicate event, 부분 추출 empty conflicts를 실제 DB와 API로 독립 검증한다. 검증자는 반례를 먼저 독자 작성한 뒤 이 목록과 대조한다. 이번 전이/추출은 미실행이다.

### F12·F16·F18–24 추가 계약

| 항목 | 이번 설계 처리·잔여 완료 책임 |
|---|---|
| F12 학습 노출·라이선스 | BGE-M3 논문은 MIRACL/Mr.TyDi 계열 학습·MIRACL/MKQA 평가를 설명한다([논문](https://arxiv.org/abs/2402.03216), [학습 목록](https://huggingface.co/datasets/Shitao/bge-m3-data/blob/main/README.md)). 이를 test 문항 직접 학습이라고 단정하지 않는다. split/문서 중복·배포 checkpoint lineage와 XOR-TyDi/Mr.TyDi 동일성은 **미확인**이다. E1a manifest는 benchmark family exposure·split overlap verified/unverified·checkpoint·native qrels version을 기록한다. 공개 통과는 회귀 감시이며 독립 일반화/사용자 승인 아님. code/data/qrels/corpus 권리를 각각 URL·version·시각·저장/변형/재배포 허용으로 확인한다. MIRACL repo Apache/논문 CC BY와 Wikipedia corpus, XORQA repo MIT와 dataset 파일 권리를 혼용하지 않는다. 미확인 항목을 법적 허용으로 표시하지 않는다. 자체 holdout 사람 확인 미결 조건 유지, E0·E1a는 계속 가능 |
| F16 확장 | C3-only 후보≤N/4, weight≤.5, 모호 PG/Go/Rust/배/Java는 도메인 확정 때만. lexical 원질의 exact 식별자 경로 유지. KB9 OOV100·날짜/단위·부정 골든과 질의별 상승/하락 report. 정책 변경은 P11 재보정 |
| F18 BQ | P8에 .989의 1,815개 문서간 범위·실벡터100만 미실행을 명시. full/BQ는 아직 비교 후보 |
| F19 Kiwi | 자체 산문 기준선 대비≥80%, 고정 corpus·부하 AB로 KB3 측정. repo docs377k 일반화 폐기. 688×100만/80k=2.39시간/스레드는 계산, 4프로세스35.8분은 이상적 병렬 외삽. 프로세스당.54GB를 worker quota에 합산 |
| F20 의존성 | 검토 tantivy0.26.2 휠은 cp313 macOS arm64/universal2, abi3 아님이라는 확인을 보존. Python3.14 휠 존재는 미확인. 선택 뒤 설치 matrix/rollback 시험을 KB6에 요구한다. Kiwi Apache-2.0 LICENSE 및 학습 자료 귀속 NOTICE를 배포물에 유지. 모델/data 별도 권리 확인, 라이브러리 추가는 E0.5 뒤 |
| F21 유형 | §4.3 보완 태그·각 split8개 초기 목표, 언어쌍 abstain은 별도40+40/P1 power. 집계·모호 질의는 검색 결과만으로 답 생성 성공이라 표시하지 않음. injection 문서는 신뢰 안 하는 데이터로 인용하고 툴 실행/명령으로 승격하지 않음. E1b가 최종 pool 책임 |
| F22 span | NFKC 분석 토큰 위치를 저장 본문 codepoint offset으로 재사용 금지. 인용/하이라이트는 저장 NFC(legacy는 그 revision의 NFKC) 본문에서 재탐색·hash 검증한다. `㈜→(주)`, `ﬁ→fi`, `x²/x2`, `µ/μ`, `℃/°C`, 전각·결합문자·줄바꿈을 골든에 넣는다. `…→...`도 실제 NFKC 길이 변화이며 작은 Unicode 계산으로 확인한다. 원 리뷰는 수정하지 않는다. KB3/5 완료 |
| F23 순서 | E1a 공개/기준선→KB5 normalization/chunker→KB6 pool→E1b 사람확인. E0.5를 선택 앞에, 도구선택≥30·설명변경 재시험·모바일 확인·독립 반례 먼저 작성은 §9 반영. E1b 지연이 설계·하네스·공개 기준선을 막지 않음 |
| F24 출처독립 | assertion `independence_basis={origin_chain,primary_source,syndication_checked,near_duplicate_checked,reviewer,status}`. 서로 다른 발행처/표현만으로 독립 판정 금지. 인용 체인·전재·같은 원전 파생은 한 group; unknown은 corroborated 독립 수에서 제외. span_checked→corroborated는 이 검토가 끝난 진술만 자동 계산. KB10 |
| F24 조건/인용 | 문서 applies의 version/edition/valid hint는 명제 conditions에 필드별 origin과 상속, passage/명시 추출 조건이 구체적이면 우선하되 충돌 시 unresolved queue. valid null/time_bound=false라도 version 조건 누락을 무조건 disputed로 만들지 않음. quote_hash_verified는 byte/구간 무결성만. reranker는 passage/window를 점수화하고, 원문 문장 경계 규칙이 span 후보를 고른 뒤 원문 재탐색·해시 검증; 의미 지지는 별도 coverage/검토. KB5/10 |
| F24 trace 개인정보 | personal 원문 query·prompt·quote를 일반 trace에 저장 금지. query digest는 일별 keyed HMAC(키는 secret 저장소, trace와 분리), payload 없는 최소 원인/지연·build 메타7일, 비식별 집계90일. access 통제·tombstone 연동·export redaction. hash만이면 비식별이라는 가정 금지. 평가 query 저장은 별도 명시 opt-in·권리 corpus만. KB7 실제 로그 검사 전 unverified |

### 12.1 검증 범위와 미실행 보존

이번은 문서 정합성·원본 checksum·작은 수학/Unicode 모델 검증만 한다. 원 검토 §6의 미확인(합성100만 재측정, CPU 처리량/질의지연, 실벡터100만/500만 외삽, pg_search, sparse/colbert, data/qrels 라이선스, split 노출, 실제 GPU 통합메모리/Qwen 동시상주, 부정·버전 재순위, writer drain timing)은 **그대로 미실행/미확인**이다. 문서 해법과 smoke 계산은 실제 DB·추론·배포 증거가 아니다. 이번에는 제품 파일·마이그레이션·collector 구현·서버 수명·운영 데이터·배포 예약을 변경하지 않는다.


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
