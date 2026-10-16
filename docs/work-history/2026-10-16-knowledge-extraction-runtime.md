# 2026-10-16 지식 기반 후속: 추출 실행 기록과 관계 경로

사용자 “다음 단계도 해라” 지시의 후속. [이전 작업](2026-10-16-knowledge-foundation.md), [운영 절차](../knowledge/operations.md), PMS 5.4.6을 잇는다. 구현 코드는 `9faad122b01596ba366d71b136f750d6a65c8707`이며 main의 Qwen v2 `e8f4a3f`를 rebase로 포함했다. backend 전용 `back-knowledge-foundation`에서 작업했고 별도 작업자를 띄우지 않았다.

## 코드 변경

- extraction v2는 unit별 reason을 보존하고 unsupported/error의 사유 누락을 거부한다. batch 입력·prompt SHA, cursor 구간, 서버에서 찾은 현재 Run의 session/engine/model/profile과 관측 시각을 기록한다. 사용자가 payload로 Run/model을 위조하지 못한다. 이 값은 runtime의 Run 레코드 관측이며 provider 응답 자체의 model attestation은 아니다. 비용은 전체 Run 관측값으로 추출별 비용과 구분한다. Run이 없으면 NULL/unreported이다.
- 상태/이력 GET와 MCP `knowledge_extraction_status`. 재시도는 성공/미지원 항목을 제외하고 네 종류의 일시 실패만 child job으로 최대3회 처리한다. 부모의 partial/오류 이력은 덮어쓰지 않는다. 같은 retry 요청은 같은 child를 반환한다. 권한/인증/한도 실패는 그대로 재시도하지 않는다. `knowledge_retry_extraction`은 write 도구이며 readOnly로 위장하지 않는다.
- 실제 자료 probe에서 graph 1-hop 결과가 k를 모두 차지하는 문제와 부분 문자열을 연결로 취급하는 문제가 드러났다. RED에서 `MakeAIP → AIP → 아리아리`의 두 번째 edge가 빠지고 `AIP-extra`가 잘못 연결됐다. 첫 단계에 budget을 나눠 주고 다음 단계는 완전한 endpoint label로만 확장한다. 각 결과에 hop/matched endpoint/path assertion IDs를 붙였다. canonical alias/entity ID를 전수 검증한 것으로 주장하지 않는다.
- 초기 새 경로 검증 중 SQL Select 변수로 질의 문자열을 덮어써 직렬화 실패가 발생했고 이름을 분리해 수정했다. 이후 API 회귀33건 통과. 이 실패는 격리 구현 테스트의 현상이며 운영 장애가 아니다.

## 실제 자료에서 실행한 범위

기존 격리 DB `ttakkari_knowledge_stage_20261016`에서 수행했다. 원본 export Downloads 열거를 반복하지 않았다. missing gold 문서는 `aip/aip/docs/design/01-reality-test.md`이며 파일은 있지만 등록 source가 없었다. 격리 DB에 해당 source만 추가해 색인(raw116units/기존검색18chunks), 기존 gold의 unique path coverage가7/8에서8/8이 됐다. 이것은 전체 지식 보존율이 아니다. source path가 끊긴 옛 `0008-context-library.md` 등록 건은 별개이며 production 설정은 바꾸지 않았다.

AIP 웹사이트 README12units, 디자인 시스템 README5units, MakeAIP README13units, Reality Test 첫20units를 현재 에이전트가 직접 읽어 처리했다. 4개 문서의 manifest146units 중50units 처리, 관계/속성 후보53개를 source_reviewed로 기록. README3개는 unit accounting을 끝냈고 Reality Test는20/116으로 running이다. 96units와 그 밖의 전체 자료가 남아 있다. user_confirmed는0이며 no_claim/부분 완료를 의미 전수 추출 완료로 해석하지 않는다.

후속 `MakeAIP` 2-hop 결과에 `MakeAIP —official_generator_for→ AIP —reality_test_uses_case_source→ 아리아리`가 들어왔다. AIP 웹사이트→AIP Design System→winterholic-design-system도 조회했다. 3개 검사 질의의 인용21건을 immutable unit의 문자 범위와 quote SHA에 재대조했다. 성공은 이 작은 source-reviewed probe 범위이며 독립 holdout/관계 recall/실제 provider-MCP 연결/답변 생성 정확도는 미검증이다. 제안·부정·추론은 원문에 맞게 유지하고 사용자 확정으로 승격하지 않았다. 이 실행의 추출별 추가 비용은 미측정이다.

같은 dev24에서 새 corpus347문서/15189units, relevance occurrence32와 unique gold8 중8개 coverage. 기존 Recall@5 .4167/nDCG .3321, 새 evidence .4375/.3017, graph2hop .4375/.3054, BM25 .5/.3640, hybrid .4792/.4149. hybrid paired recall delta+.0625, CI[-.1042,.2292]. 이전 corpus346문서 결과와 섞지 않으며 개선 확정/기본 검색 전환을 하지 않는다. nDCG가 내려간 경로와 CI 불확실성도 보존한다.

## 배포와 DB 관측

첫 확인 localhost8787은1784cf2, 이후 같은 주소가b8e681e/PID4827로 전환됐다. local PostgreSQL `ttakkari:5432`는0033_weather_analysis_engine이다. MCP recall이 반환한 최근 기억 UUID26414c1d...와 동일 row/source_ref/시각을 로컬 DB에서 확인했다. 새 knowledge/status와0034 활성 runtime의 DB identity 검증은 여전히 남는다.

이전 350d9662-fa51-4268-9205-3b9004a0887b는 실패가 아니라 누적e8f4a3f 예약3cba3a1e-d582-49fc-a83a-6b7f8b707249로 superseded였다. 읽은 job 기록의 b8e681e 예약721fe15f...는draining, e8f4a3f는requested였다. draining은 새 요청 전환을 뜻하고 구세대 종료 완료와 다르다. controller를 기다리는 polling이나 Run 안 서비스 재시작을 하지 않는다. 이번 후속 코드를 clean main에 합쳐 누적 예약한다.

산출물은 outbox/bf27713e-b271-400e-a06b-2ea1b2a968ab: staging-source-batches.json, staging-extraction-probe.json, staging-graph-path-probe.json, staging-dev-evaluation-with-source-and-claims.json, 테스트 로그. 원문 파일/production 지식 원장은 이 실험에서 수정하지 않았다.

남은 순서: 독립 배포 controller 활성화 → 새 API/runtime/DB 확인 → production registered backfill과 올바른 source 등록/끊긴 path 정정 → 원본 전수 수입·추출 → 독립 holdout/관계 recall/citation/p95. 별도 승인 대기로 바꾸지 않는다. meetings/app-chat adapter, legacy provenance/정정 overlay, recorded-time replay, 오류 큐 UI도 남는다.

## 최종 검증과 이행

- 초기 후속 전체 회귀1036 passed/2 xfailed/6 subtests. 이후 retry payload 위조 방지·실패4번째 재시도 거부를 추가하고 최신main e8f4a3f를 합쳤다.
- 최종 `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_knowledge_v2_test .venv/bin/python -m pytest -q`: **1068 passed,3 skipped,2 xfailed,6 subtests passed in207.25s**. 미실행/예상 실패를 통과 수에 포함하지 않았다.
- 새 경로·추출·기존 지식 API focused33건, 최신 추가 guard/MCP focused9건 통과. 전체 회귀가 이들 및 merged Qwen v2의 비모델 검사도 포함한다. Ruff와 git diff-check 통과.
- frontend 코드는 이번 단계에서 변경하지 않았다. 실제 provider 연결·모델 응답·운영 backfill·전수 추출은 여전히 미실행이다.
- code9faad12와 이 기록을 clean main에 합쳐 push하고, standing policy에 따라 누적 generation 배포를 예약한다. 예약 수락 후에는 완료/활성화로 오인하지 않고 턴을 반환한다. 최종 예약 ID/상태는 앱 deployment 기록과 사용자 보고가 정본이다.
