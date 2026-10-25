# KB v3 두 번째 통합 합류 (integration2) — 2026-10-25

## 범위와 체크아웃

- 브랜치 `kb-v3/integration2-20261025`, worktree `back-kb-v3-integration2`. 기반은 1차 통합본 `4ca1ed0d7f8471a3b73def4d9b8c5d7bd4d3cdec`이다.
  1차 통합 브랜치(`kb-v3/integration-20261025`)와 `back/` 본 작업트리는 건드리지 않았다.
- main 병합, push, 배포, 운영 DB 접근, PMS done 처리는 하지 않았다.

## merge 순서와 충돌 해결

git 텍스트 충돌은 한 건도 없었다. 아래는 텍스트가 겹치지 않아도 의미가 부딪친 지점과 해결이다.

| 합류 | 의미 충돌 | 해결 |
|---|---|---|
| evidence-flow `d9acc2d` | 없음. `stages.py`의 legacy Collector HTML hold와 resolver 경유 영속화는 그대로 유지. 미추적 `test_kb_v3_pinned_blocker_probe.py`는 4ca1ed0에서 RED(2 failed), 통합 브랜치에서 GREEN(2 passed)을 확인하고 포함. | 같은 브랜치의 `test_collector_delivery_queues_pipeline_job` 목(mock)에 `chunker_version`·`sha256`이 없어 이 브랜치 단독으로도 실패하고 있었다. 실제 Collector 계약(`contracts.py`)에는 필수 필드이므로 코드가 아니라 목을 고쳤다. |
| canonicals `7f1da58` | 어댑터 레지스트리의 canonicalizing이 `MissingStageProcessor`(held)였다. | 레지스트리에 `CanonicalizingProcessor`가 등록됨을 확인. 6단계가 모두 succeeded/degraded로 끝난다. 낡은 기대값(`held`, `missing_capability`) 2곳을 갱신. |
| search `fbf9089` | (a) `kb_access.entity_access_filter`가 `KbEntity.tenant_id == ctx.tenant_id("owner")`를 요구하는데, 실제 파이프라인이 만드는 엔터티 tenant는 `"personal"`이다. 검색의 엔터티·별칭·1-hop 경로가 실데이터에서 전부 비었다. 검색 브랜치의 `test_kb_retrieval` 픽스처가 엔터티 tenant를 `"owner"`로 지어서 가려져 있었다. (b) 별칭 필터가 모든 별칭에 "권한 있는 패시지 본문 출현"을 요구해서, 영어 문서만 있는 엔터티를 seed 한국어 별칭(쿠버네티스)으로 찾을 수 없었다. (c) 그래프 질의·충돌·정본이 각자 `"owner"`·visibility 조건을 하드코딩해 `kb_access`와 규칙이 둘이었다. | (a) `KbAccessContext.ontology_tenant_id`(기본 `"personal"`)를 추가해 엔터티 네임스페이스와 문서 접근 tenant를 다른 축으로 분리. (b) `seed`/`user_verified` 별칭은 출현 근거 면제, `extracted` 별칭만 출현 근거 요구. (c) `document_access_conditions`와 `from_caller_visibility`를 `kb_access`에 두고 `graph_query`·`conflicts`·`canonicals`가 이를 쓰게 바꿨다. 공개 함수에 `access: KbAccessContext | None` 인자를 추가했고 기존 `caller_visibility`는 같은 규칙으로 변환되는 호환 경로다. |
| subscription-l2 `9336fdb` | `stages.py`를 evidence-flow와 L2가 둘 다 수정했으나 겹침 없이 자동 병합. 마이그레이션 헤드가 `0073_kb_l2_budget_ledger`가 됨. | 아래 "L2" 참고. |

추가로 발견해 고친 시험 격리 결함: `unit_tests/test_kb_v3_pipeline.py`가 import 시점에 공유 `Table.__table__.dispatch._clear()`를 불러 같은 프로세스의 PG 시험 스키마에서 원장 불변 트리거(after_create DDL)를 지웠다. 이 파일과 `tests/test_kb_v3_canonicals_postgres.py`를 같은 pytest 호출에 넣으면 후자가 `DID NOT RAISE IntegrityError`로 실패했다(canonicals 브랜치 단독에서도 재현). 지금은 SQLite용 표를 `to_metadata` 사본(이벤트 없음)으로 만든다.

## 실 PostgreSQL 흐름(임시 pgvector/pgvector:pg17 컨테이너, 공유 DB·TTAKKARI_TEST_DB 미사용)

`scripts/kb_v3_real_db_flow.py`를 확장해 HTML(Collector 파서 경로), 상충 문서, 검색 함수 확인을 넣었다.
임베딩은 결정적 해시(`TTAKKARI_CONTEXT_EMBEDDER=hash`)다. 벡터 경로 결과는 의미 품질 증거가 아니다.

HTML 문서는 `collector.parse.extract_document` + 제목 구조 보존으로 정규화한 본문을 `kb_delivery.deliver_item`과 같은 순서(ingest, access 행, segment, enqueue)로 넣었다.
HTTP fetch와 outbox는 거치지 않았다. 그 전체 경로는 `tests/test_collector_integration.py`가 격리 PG에서 검증한다.

수치(0073 head에서 새 DB, 문서 5건: 영어 md, 한국어 md(extracting 강제 실패 후 `retry_pipeline_job(from_stage='extracting')` 재개), HTML, 상반 정의 2건):

| 항목 | 값 |
|---|---|
| 6단계 상태 | 5개 잡 모두 parsing/chunking/linking/indexing/canonicalizing succeeded, extracting degraded(`l2_disabled`, L2 호출 0), 잡 상태 degraded |
| 패시지 / 벡터 | 27 / 27 |
| 엔터티 / 별칭 | 14 / 20 (extracted 10, seed 10) |
| 주장 / 주장 근거 | 6 / 6 |
| 관계 / 관계 근거 | 3 / 3 (모두 candidate, 근거 없는 candidate 0) |
| 근거(evidence) | 29 |
| 정본 / 충돌 | 14 / 1 (`value_contradiction`, open, global) |
| 근거 없는 extracted alias | 0 |
| HTML parsing | `heading_spans=3`, `source_media_type=text/html`, 본문에 `# ` 제목 보존 |
| extracting 강제 실패 | 잡 degraded(`max_attempts_exceeded`), indexing succeeded(패시지 8/벡터 8 유지), 재개 후 엔터티 5→11, 주장 0→3, 근거 0→15, 두 번째 retry 호출은 `already_queued` |
| 마이그레이션 | `0073_kb_l2_budget_ledger` upgrade → downgrade 0071 → re-upgrade 성공 |
| 검색 | 어휘(영/한) 결과 있음. `쿠버네티스`/`k8s`가 seed 별칭으로 `Kubernetes`에 매칭(`matched_by=alias`). 영어 문서만 있는 상반 정의 문서 2건이 graph 경로로 검색됨(어휘 경로만으로는 한국어 본문 2건뿐). 개인 컨텍스트에서도 global 충돌 1건 노출, `has_conflict=True`. |

## 검색 평가 fixture

원인:
1. 생성기가 passage·인용 오프셋을 손으로 적었다. passage `end`가 본문 길이를 넘는 문서가 있었고(hpa 179 > 177), q1·q2의 `context_before`가 인용 위치와 어긋났다.
2. `predictions`가 사람이 쓴 순위였다. `coverage`가 없고 `score_bytes`가 null이어서 스키마 검증이 실패했다. 값을 채워 넣으면 측정이 아니라 지어낸 지표가 된다.

조치:
- 생성기가 오프셋을 본문에서 계산한다(인용이 정확히 한 번 나오는지 assert).
- `predictions`는 `scripts/collect_kb3_benchmark_predictions.py`가 임시 PG에서 `kb_retrieval.search`를 실제로 돌려 만든다. `score_bytes`는 반환 점수(소수 4자리 반올림)의 big-endian float64 16진수, `coverage`는 search가 반환한 값이다. `answer_correct`는 판정하지 않았다.
- 결과 디렉터리 `.kb_eval*_out`은 커밋하지 않았다.

해석 한계(지표를 만들어 내지 않기 위해 남긴다):
- 코퍼스가 6개 단일 passage 문서이고 k=10이라 recall@10은 구조적으로 1.0이다. 품질 근거가 아니다.
- 해시 임베딩이라 벡터 경로 순서는 임의다. 한국어 질의가 영어 문서를 찾는 것은 별칭 경로가 있을 때만 의미가 있는데, 이 fixture의 문서는 제목이 없는 단일 문장이라 엔터티가 추출되지 않는다.
- 재순위의 최신성 가산이 현재 시각에 의존하므로 같은 코드로 다시 측정하면 순위·점수 바이트가 조금 달라질 수 있다. 하네스 replay는 저장된 한 번의 측정 기록을 같은 입력으로 두 번 돌려 `exact_match`를 확인한 것이다.
- 하네스 판정은 `final_quality=not_authorized`, `quality_gate=inconclusive(power_not_established, fixture_only)`다.

## L2

`kb-v3/subscription-l2-20261025`의 커밋된 HEAD `9336fdb`(afcb5ab + 9336fdb)를 merge했다.
그 worktree에는 커밋되지 않은 변경(`daily_budget.py`, `subscription_transport.py` 수정과 미추적 시험·smoke 파일)이 남아 있고 다른 작업자 소유라 포함하지 않았다.

## 남은 결정 사항

- **관계 active 승격 경로가 없다.** 추출 파이프라인은 관계를 `candidate`로만 저장한다(설계). 검색의 1-hop 확장과 `query_1hop_subgraph`는 `active` 관계만 쓰므로, 승격 주체(L2 검증, 검토 승인 등)가 정해지기 전에는 파이프라인이 만든 관계로는 1-hop 결과가 나오지 않는다. 시험과 실 DB 흐름에서는 근거 행이 있는 관계를 직접 `active`로 바꿔(검토 승인 모사) 확장이 동작함을 보였다.
- 추출된 엔터티의 `tenant_id`는 `"personal"`이고 문서 접근 tenant는 `"owner"`다. 다중 tenant로 가려면 엔터티 tenant를 owner tenant와 연결하는 설계가 필요하다.
