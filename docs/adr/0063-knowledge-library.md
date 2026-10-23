# ADR 0063. 범용 지식 도서관(Knowledge Library)과 2층 아키텍처

- 상태: KB1+KB2 구현 사실 채택(2026-10-20). 후속 설계 정본은 v2. KB-E0 로직 완료 승인(2026-10-22, DB·UI·추론 제외). **2026-10-23 사용자의 명시 지시("지식도서관 페이지는 만들어지지도 않았네, 이어서 프론트랑 백 배포까지")로 '제품검색 미승인' 표기를 해제하고 검색·열람 최소 제품화(아래 "2026-10-23 최소 제품화")를 진행한다.** 이 최소 제품화는 v2 KB7 품질 gate(사람 확인 holdout, 검정력, coverage/abstain 보정)를 통과한 것이 아니다. 해당 gate와 KB-E1b·E0.5는 여전히 미완료이며 검색 품질을 입증했다고 쓰지 않는다. 이전 상태: KB-E0 착수 조건부 승인, 제품검색 미승인.
- 활성 후속 설계: [v2](../design/kb-v2/knowledge-library-design-v2.md), [검토 대응표](../design/kb-v2/reviews/v2-review-response.md). [v1](../kb/design-v1.md)·[v1 사본](../design/knowledge-library.md)은 역사 기록.
- 선행: ADR 0007·0009(맥락 도서관), ADR 0031(근거 중심 원장)

> 아래 저장 경로·NFKC·불변 트리거·동일 바이트 멱등성은 KB1·KB2 **현재 구현 사실**이다. v2는 envelope NFC/collector 소유 pin·관측 ledger·인가 tombstone 예외를 설계하며 아직 구현되지 않았다. 기존 스키마가 이미 바뀌었다는 뜻이 아니다.

## 배경

기존 개인 맥락 도서관(`ctx_*`, `knowledge_*`)은 프로젝트 기록·개인 메모·회사 회의록 등 개인 맥락 검색에 맞춰져 있었다.
세계 지식(공식 기술 문서, 표준, 학술 자료 등)과 개인 맥락은 신뢰 기준·범위·갱신 주기가 다르며, 같은 검색 풀에 섞일 경우 일반어 질의에서 스킬/외부 지식 문단이 개인 맥락을 밀어내거나 그 반대의 연관성 저하가 발생했다(2026-10-20 감사 C8).

## 결정

1. **인스턴스 공유, 별도 `kb_*` 테이블 분리 (D1)**
   - 개인 맥락 도서관과 테이블·검색 풀·MCP 도구를 완전히 분리한다.
   - 단, 운영 및 트랜잭션 단순화를 위해 PostgreSQL 17 인스턴스는 공유한다.
2. **2층 아키텍처 (D2, D3)**
   - 1층: 원본·수집 원장(`kb_sources` -> `kb_documents` -> `kb_revisions` -> `kb_passages` -> `kb_evidence`).
   - 2층: 지식·정본층(`kb_concepts`, `kb_claims`, `kb_canonical` 등, 후속 KB9+KB10).
   - 수집 즉시 패시지 단위 검색이 가능하며(패시지 우선), LLM 기반 claim 추출은 점진적으로 덧붙인다.
3. **내용 주소 파일 저장소 (D2)**
   - 원본 bytes는 DB가 아닌 내용 주소(sha256) 파일 저장소에 보관한다.
   - 위치: `data_dir / "kb" / "originals" / ab / cd / <sha256>`. 데이터 스냅샷 백업 대상에 자동 포함된다.
   - 정규화 텍스트, 메타데이터, 패시지 오프셋만 DB에 둔다.
4. **불변성과 구간 검증**
   - `kb_sources`, `kb_revisions`, `kb_passages`, `kb_evidence`는 `BEFORE UPDATE OR DELETE` 트리거로 행 수정과 삭제를 엄격히 거부한다.
   - `kb_revisions`, `kb_passages`, `kb_evidence`의 외래키는 `ON DELETE RESTRICT`로 결속되어 부모 삭제로 인한 연쇄 삭제(CASCADE)가 차단된다(Alembic 0055).
   - 정규화는 NFKC 및 LF('\n') 줄바꿈을 강제하며, 오프셋은 정규화 텍스트의 0-based Unicode 코드포인트 단위로 고정된다.
   - `kb_evidence`는 `BEFORE INSERT` 트리거로 패시지 본문 슬라이스의 sha256 다이제스트 일치를 강제한다.
   - 논리 문서(`kb_documents`)만 제목·언어 등 갱신이 가능한 가변 껍데기로 유지한다.
5. **무결성 점검 및 멱등성**
   - 원본 쓰기는 임시 파일 -> flush + fsync -> 원자적 rename -> DB insert -> DB commit 순서를 준수한다.
   - 동일한 바이트와 소스 키의 재수집은 기존 행을 반환하며 멱등하다. 파일 유실 시 재작성으로 복원한다(`healed_file`).
   - `ttakkari kb-verify` CLI로 DB 행과 원본 저장소 파일의 해시·크기 정합성을 대조하고, `--clean-orphans` 옵션으로 DB 트랜잭션 실패로 남은 고아 파일을 안전하게 정리한다.

## 2026-10-23 최소 제품화(검색·열람·등록)

사용자 지시로 v2의 KB7/KB8/KB12 전체가 아니라, 현재 원장(kb_*) 위에서 사람이 쓸 수 있는 최소 범위만 구현했다.

- API `/api/assistant/kb/*`(소유자 인증, 개인 맥락 도서관 `/api/assistant/knowledge`와 별개 라우터):
  `GET summary`, `GET documents`(페이지네이션·제목/키 필터), `POST documents`(텍스트 등록), `GET documents/{id}`(버전·패시지), `GET search`, `GET passages/{id}/evidence`.
- 검색은 맥락 도서관이 쓰는 기존 PostgreSQL 수단(simple tsvector + 부분 일치 + word_similarity, `context/search.py`)을 `kb_passages`에만 적용한다. 새 엔진·임베딩·재순위·인덱스·마이그레이션은 없다. 기본은 문서별 현재 리비전(최대 rev_no)만 검색하고 `current_only=false`로 이전 버전을 포함한다. 점수는 순위용이며 질의 간 비교나 coverage 판정에 쓰지 않는다. 패시지 수가 늘면 seq scan이라 느려지므로 규모 gate(v2 §5, KB13) 전에 색인 결정이 필요하다.
- 등록은 기존 `ingest_source`(멱등·불변·sha256 파일 저장소)와 `segment_revision`을 그대로 호출한다. `source_key`를 생략하면 본문 해시로 정해 같은 글의 재등록이 문서를 늘리지 않고, 같은 `source_key`에 다른 본문은 새 리비전이다. URL 수집·권리 정책은 collector 소관(KB5)이라 받지 않는다.
- 근거 위치는 패시지의 리비전 본문 오프셋, 줄 번호, 앞뒤 문맥, 구간 해시 일치 여부를 돌려준다. `hash_verified`는 저장 본문 슬라이스 일치만 뜻하며 명제 지지 확인이 아니다.
- kb 행에 scope 열이 없으므로(v2 G8) 개인/회사 scope 필터는 아직 없다. 모든 kb 문서는 소유자 인증만으로 보인다. scope 열이 생기는 단위에서 검색·목록 필터를 함께 추가해야 한다.
- UI는 `/knowledge-library`(검색·문서·등록 3탭, 상태는 URL). 에이전트용 MCP 도구(kb_search 등), 주장·정본, 검토 UI는 범위 밖이다.

## 후속 작업

v2 §9/§12의 E0 선행·단위별 독립 검증을 따른다. 기존 단계번호와 PG/Tantivy 선택은 구현 승인으로 쓰지 않는다. 수집은 독립 collector 플랫폼, kb는 표준 envelope 소비·원장/검색/정본 소유다. 엔진·삭제 경로·생산자 계약 변경은 v2 반영 확인 및 각 단위 실제 DB 검증 전 활성화하지 않는다.
