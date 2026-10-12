# ADR 0007. 비서 맥락: 온톨로지 + RAG, 따까리 기능의 MCP 도구화, 비서 하네스

- 상태: 채택(초기 틀) (2026-10-11)
- 결정자: 사용자("나에 관한 맥락들 편하게 불러올 수 있게 온톨로지랑 rag", "따까리 기능들은 다 mcp로", "비서관리에서 ttakkari 전용 하네스를 따로 관리", "pg벡터 써도 될 듯")

> 2026-10-12: 임베딩 모델·벡터 혼합 검색 결정은 [ADR 0009](0009-context-library.md)로 대체했다.
> 아래 모델 비교·로딩 수치는 당시 기록이다. 현재는 모델 없는 키워드 검색과 명시적 온톨로지를 사용한다.

## 결정

### 저장소: PostgreSQL + pgvector + pg_trgm (별도 벡터 DB 없음)
단일 사용자·수만 건 규모라 Chroma 같은 별도 서버를 두지 않는다. 같은 DB 라 트랜잭션·백업이 하나다.
임베딩은 로컬 `intfloat/multilingual-e5-large`(fastembed, 1024차원, API 키 없음, `~/.ttakkari/models`).
한국어 질의 3종 비교에서 MiniLM 은 엉뚱한 문장을 1위로 골랐고 e5-large 는 모두 맞혔다(10-11 실측). 처음 로드 약 23초라 서버 시작 때 백그라운드로 올린다.
테스트·저사양은 `TTAKKARI_CONTEXT_EMBEDDER=hash`(결정적 해시 임베딩)로 돈다.

### 온톨로지(나에 관한 구조화된 사실)
- `ctx_entities`: 사람·프로젝트·조직·장소·도구·주제·취향 등. 이름·별칭·요약·속성, `pinned`.
- `ctx_relations`: (주어, 술어, 목적어) 삼항. 예) winterholic —works_on→ Ttakkari.
- `ctx_memories`: 사실·취향·결정·사건·지침·메모 한 줄씩. 관련 엔터티, 출처(manual/agent/ingest), `pinned`.

### RAG(문서 조각)
- `ctx_sources`(색인할 폴더·패턴) → `ctx_documents` → `ctx_chunks`(제목 단위로 자른 조각 + 임베딩).
- 기본 출처는 없다. 비서 관리 > 지식에서 `~/work-history`, 핸드오프 폴더 등을 추가한다. 내용이 바뀐 파일만 다시 색인한다(sha256).

### 불러오기(recall): 하이브리드
벡터 유사도(코사인) + 글자 유사도(pg_trgm)를 섞고, 걸린 엔터티의 1단계 관계와 연결된 기억을 붙여 돌려준다.
매 Run 에 통째로 넣지 않는다. 고정(pinned) 기억과 비서 지침만 짧게 시스템 프롬프트에 넣고, 나머지는 에이전트가 `recall` 도구로 필요할 때 부른다(토큰 절약).

### 따까리 기능의 MCP 도구화
모든 Run 에 `ttakkari` MCP 서버를 붙인다(이전엔 오케스트레이터만). 세션 한정 토큰(`aud=ttakkari-agent`).
- 공통: `recall` `remember` `get_entity` `link` `forget` · `list_artifacts` `share_file` · `notify_user` · `list_sessions` `read_session`
- 오케스트레이터만: `spawn_worker` `message_worker` `list_workers` `get_worker` `stop_worker`

### 비서 하네스(따까리 전용)
`~/.ttakkari/harness/instructions.md` 를 비서 관리 > 지침에서 고친다. 모든 Run 의 시스템 프롬프트에 붙는다.
`~/.ttakkari` 안이라 에이전트는 못 고친다(사용자가 정하는 규칙). 전역 `~/.claude` 하네스와는 별개다.

## 버린 것
- Chroma·Qdrant 등 별도 벡터 DB: 운영 대상이 하나 늘고 DB 와 정합성을 따로 맞춰야 한다. 규모상 이득이 없다.
- 클라우드 임베딩 API: 키·비용·외부 반출. 개인 맥락이라 로컬에 둔다.
- 매 Run 에 관련 맥락 자동 주입: 무엇이 관련 있는지 모르는 시점에 넣으면 토큰만 쓴다. 고정 기억만 넣는다.

## 알려진 한계 / 다음
- 엔터티 자동 추출은 아직 없다. 에이전트가 `remember`/`link` 로 쌓거나 사용자가 직접 넣는다.
- 기억 충돌·만료(유효 기간) 처리는 필드만 있고 정책이 없다.
- 리랭커 없음. 품질이 부족하면 bge-reranker 같은 교차 인코더를 뒤에 붙인다.
