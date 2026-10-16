# 지식 원장 구현과 운영 이행

2026-10-16 후속 구현. 설계 정본은 [architecture.md](architecture.md), 진행 기록은 [work-history](../work-history/2026-10-16-knowledge-foundation.md), WBS는 5.4.6이다. 구현과 운영 완료를 구분한다.

## 들어온 기능

- `knowledge_revisions/units/evidence/assertions/assertion_evidence/review_events/extraction_jobs` 7개 additive 테이블. 기존 ctx ID·벡터·원문 파일을 유지한다.
- 원본 bytes/revision SHA와 정확한 text unit/span SHA를 구분한다. unit·evidence·review event의 SQL UPDATE를 거부하고 span의 원문 일치/해시를 DB에서도 검사한다. ChatGPT unit은 conversation/node/message/parent/JSON pointer/part 문자 위치와 발화자·시점·분기를 기록한다. 이전 분기도 수입하되 현재 사실 검색은 현재 분기의 검토된 주장만 사용한다. 첨부 payload는 해석하지 않고 미지원으로 계산한다.
- Markdown 색인은 원문 공백/줄바꿈을 보존하는 revision을 함께 생성한다. 동일 수입은 멱등이며 옛 revision 재실행은 현재 pointer를 되돌리지 않는다. 기존 등록 문서 backfill은 파일만 읽고 기존 검색 청크나 원본을 삭제하지 않는다.
- `/api/assistant/knowledge`는 사용자 인증, `/api/agent/knowledge`는 세션 토큰과 서버에서 정한 work/shared 범위를 적용한다. 출처 비활성화·삭제는 다음 검색부터 적용하며 삭제된 출처의 원장이 무제한 자료로 변하지 않는다.
- 후보는 exact quote 없이 생성할 수 없다. 사용자 확인은 사용자 API에서만 가능하다. 에이전트는 source_reviewed까지 기록한다. 낙관적 version으로 동시 수정 충돌을 감지한다. 정정/철회와 원문·과거 검토는 함께 남으며 재수입이 닫힌 주장을 부활시키지 않는다.
- 기본 `recall`의 기존 results 순위와 선택 kinds를 유지하고, 추가 `knowledge` 묶음에 원문·검토된 주장을 반환한다. raw evidence에도 알려진 정정/철회 상태를 붙인다. 명시적 `knowledge_recall`은 1/2-hop, scope/valid-time/current-review-state, 원문 단위의 문서별 중복 제거와 budget 표시를 지원한다.
- 도서관 `근거·검토` 탭: 범위, 집계, 원문 검색, 사용자 확인/보류/해석 정정/철회, 원문 해시·발화자·분기·이력. 압축된 검색 결과는 상세 이력을 읽은 뒤 검토한다.

## 이행 순서

1. 독립 배포 controller가 0034 migration, 프로토콜/세대 readiness를 검증하고 활성화한다. `/api/version`과 인증된 `/api/assistant/knowledge/status`의 runtime revision/DB 이름·port를 함께 확인한다. 파일 symlink만으로 runtime 동일성을 단정하지 않는다.
2. `ttakkari knowledge-backfill --limit 100 --offset 0 --timeout 120 --output /new/backfill-000.json`. `next_offset`으로 이어서 실행한다. 범위는 등록 문서이며 원본 export 전체가 아니다. 부분 실패는 `partial`/exit2, 읽기 제한 시간 초과는 `unknown`/exit2이다. 디스크 원문과 기존 ctx 검색 데이터는 변경하지 않는다.
3. 원본 export는 접근 가능한 실행 환경에서 shard 목록을 먼저 인벤토리한다. `ttakkari knowledge-import-chatgpt /export/conversations-000.json --namespace user-export-20261013 --scope personal --timeout 60 --output /new/import-000.json`. 같은 원본에는 namespace를 유지한다. 원본 파일은 수정하지 않는다. 모든 shard를 대조하기 전 전체 export 보존을 선언하지 않는다. 알려진 Downloads 열거 제한을 같은 sandbox에서 반복하지 않는다.
4. 계정 한도·scope를 확인하고 `knowledge_sources` → `knowledge_start_extraction` → `knowledge_next_batch` → `knowledge_submit_batch`. 호출 모델을 그대로 쓰며 별도 provider/model을 몰래 띄우지 않는다. 최대 batch100/20,000자이고 cursor·extractor version으로 재개한다. 원문별 candidates/no_claim/unsupported/error를 기록하며 raw 보존과 의미 추출 완료는 별개다. 후보를 pinned나 사용자 확인으로 자동 승격하지 않는다.
5. 원문 대조 후 `knowledge_review_assertion`; 해석이 모호하면 disputed. 사용자는 도서관에서 확인/정정한다. 사용자 확인을 전체 평가의 선행 대기로 삼지 않는다.
6. source-first gold JSON은 `dev`, `holdout`, `label_authority`를 가진다. 각 질의에 `id/group/cohort/query/relevance{source_path:0..3}`를 선언한다. 계열 group이 겹치면 평가를 거부한다. `ttakkari knowledge-eval /gold.json --split holdout --scope all --k 5 --timeout 120 --output /new/eval.json`. raw_literal은 질의 전체의 literal 대조이고 BM25와 다르다. 현재 ingested 원문 범위를 대상으로 하며 미수입 export를 포함했다고 해석하지 않는다.
7. cohort·paired CI·권한·정정 재수입·인용 경로·실제 모델 응답·p95를 확인하고 기본 검색 전환을 결정한다. 하이브리드 후보 성능을 보고 즉시 control 순위를 교체하지 않는다.

## 롤백과 미완료

운영 롤백은 애플리케이션/검색 경로의 이전 generation으로 한다. additive ledger와 정정/철회 이력은 남긴다. 0034 downgrade는 빈 원장에만 허용하고 데이터가 있으면 `knowledge_ledger_contains_data`로 거부한다. 기존 ctx 데이터를 정정하는 overlay는 provenance가 검증된 연결에만 적용해야 하며 문자열 유사성으로 기존 기억을 지우지 않는다.

아직 남은 것: 전체 export 전수 대조/원본 접근, 회의·앱 대화 전용 adapter, 기존 기억·관계의 검증된 원문 lineage와 legacy 정정 overlay, 실제 모델/Run/비용을 포함한 전수 추출 운영, 부분 실패 재추출의 새 작업 단위, 기록시각 당시 상태의 bitemporal 조회(현재 as_of는 valid-time와 현재 검토 상태), 독립 holdout·관계 recall·답변 citation/p95, 모바일 오류 큐/미처리 범주 가시화, 배포 runtime·DB 동일성 및 canary. 현재 코드를 전체 WBS 완료로 표시하지 않는다.

개발 자료의 source-reviewed label과 사용자 확정은 구분한다. 초기 holdout 80질의/관계60edge는 목표이며 달성 수치가 아니다. embedding 신규 설치/다운로드·유료 초과 사용은 이 경로에 없다.
