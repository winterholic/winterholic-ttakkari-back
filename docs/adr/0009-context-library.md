# ADR 0009. 임베딩 모델 없는 맥락 도서관

- 상태: 채택 (2026-10-12)
- 결정자: 사용자. 외부 텍스트 임베딩 모델 없이 RAG·온톨로지를 개발하고 자주 작업하는 프로젝트 맥락을 등록한다.
- 대체: ADR 0007의 e5-large·fastembed·벡터 혼합 검색. 저장된 기억·문서·관계와 nullable 벡터 열은 보존한다.
- 2026-10-13: 회의록만 로컬 e5-large 벡터를 더했다([ADR 0021](0021-clovanote-and-meeting-vectors.md)). 기억·문서는 여전히 아래 어휘 검색이다.

> 2026-10-16: [ADR 0031](0031-evidence-first-knowledge.md)과 [지식 기반 실행 설계](../knowledge/architecture.md)로 구조 고도화 착수. 아래 내용은 현재 운영 경로의 기준이며, 새 원본 원장·assertion·추출/검색 경로는 단계별 구현 전이다. scope와 기존 임베딩 정책은 유지한다.

## 구조

큰 종류(projects / work-history / knowledge / health) → 프로젝트 이정표 → 근거 문서·기억의 세 층이다.
목차는 별도 정본을 복제하지 않고 `ctx_entities`, 활성 `ctx_sources`에서 생성한다.
건강 데이터처럼 아직 없는 종류는 채우지 않는다. 지침에는 종류와 도구 사용 원칙만 둔다.

온톨로지는 프로젝트·도구·조직·사람·주제와 명시적인 관계를 사용한다.
프로젝트의 별칭, 목적, 로컬 코드 경로, 읽을 문서, 확인 날짜는 엔터티의 aliases/summary/attributes에 둔다.
관계는 `part_of`, `uses_design_system`, `related_to`, `works_on` 등을 사용하되 기존 사용자 술어도 보존한다.
문서 출처의 `entity_id`로 프로젝트에 연결하고, 기억은 기존 `entity_ids`로 연결한다.
관계가 있다는 이유만으로 다른 프로젝트의 모든 문서를 재귀 확장하지 않는다. 일치 엔터티의 1단계 관계만 표시한다.

## 검색

1. 검색어를 정규화하고 소수의 구체적 단어로 나눈다. 한국어 조사는 부분 일치가 보완한다.
2. 이름·별칭이 실제로 포함된 엔터티를 찾고 연결된 기억·문서 후보를 더한다.
3. PostgreSQL `simple` 전문 검색, 단어 포함 비율, `pg_trgm.word_similarity`, 제목 일치로 순위를 매긴다.
4. 기억 보관 상태·출처 활성 상태·범위를 먼저 제한한다. 문서당 최대 2조각, 결과 최대 30개다.
5. 결과에 원문 경로·제목·절·프로젝트·범위·색인 시각을 붙인다. 기억은 source_ref/updated_at을 붙인다.

벡터 공간·학습 모델·추가 LLM 인덱싱 호출은 없다. PostgreSQL 전문 검색 GIN 인덱스를 사용한다.
다만 부분 일치·유사도 후보와 제목 결합은 순차 탐색할 수 있다. 실제 규모에서 지연을 측정한 뒤 인덱스를 더한다.
동의어·표현 변화의 의미 검색은 보장하지 않는다. 별칭·다른 키워드로 재검색하고 원문을 확인한다.
관련 없는 질문은 빈 결과로 돌려주며, 결과의 점수는 정확도 확률이 아니다.
PostgreSQL 기능 근거: [text search controls](https://www.postgresql.org/docs/current/textsearch-controls.html),
[pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html).

## 범위와 출처

scope는 personal / work / shared다. 회사 Run의 recall·엔터티 상세·도서관 목차·고정 기억·계정 전환 검색은 work/shared로 제한한다.
개인 Run은 사용자 전체 맥락을 찾을 수 있다. 이 구분은 파일 시스템 접근 제어를 대신하지 않는다.
기존 데이터는 personal로 보존하며 추측으로 회사 자료로 재분류하지 않는다.
자동으로 기억을 추출하거나 오래된 결정을 삭제하지 않는다. 확인 날짜와 근거를 보고 충돌을 판단한다.

색인은 UTF-8 텍스트만 읽고 숨김·생성 폴더·심볼릭 링크·민감 경로를 제외한다.
파일 개수 상한이나 디렉터리 열거 오류가 나면 불완전한 목록으로 기존 문서를 지우지 않는다.
개별 파일 읽기 오류·크기 초과는 이전 조각을 보존하고 last_error와 로그에 남긴다.
같은 파일을 두 출처가 덮어쓰지 못한다. SHA가 같은 파일은 다시 처리하지 않는다.

## 목록 등록

사용자 자료는 공개 코드 저장소 밖 로컬 JSON과 프로젝트 카드에 둔다.
`ttakkari context-import catalog.json`은 경로를 먼저 검증하고 엔터티·관계·기억·출처를 등록한 다음 색인한다.
반복 실행은 엔터티 이름·기억 키·원문 경로로 중복을 피한다. 사용자 수동 기억과 보관한 기억은 덮어쓰지 않는다.
색인 실패는 명령 실패로 알리고 성공한 등록분은 보존한다. 재실행으로 이어갈 수 있다.

```json
{
  "projects": [{
    "name": "Example", "aliases": ["예제"], "summary": "프로젝트 목적",
    "scope": "personal", "attributes": {"root_path": "/path/to/repo", "checked_at": "2026-10-12"},
    "sources": [{"path": "/path/to/repo/README.md", "category": "projects"}],
    "facts": [{"key": "purpose", "content": "확인한 사실", "source_ref": "/path/to/repo/README.md"}]
  }],
  "relations": []
}
```

`GET /api/assistant/library?scope=all|personal|work`, `GET /api/agent/library`, 공통 MCP `library_catalog`로 목차를 본다.
비서 관리의 기억·엔터티·출처 API는 scope와 출처 메타데이터를 노출한다. 기존 필드는 유지한다.

`library_catalog` 응답의 `collections`는 프로젝트가 아닌 지식·작업 기록 모음(엔터티+knowledge/work-history 출처)의 2단 색인이다.
이름·별칭·요약·범위·출처 수만 담고 문서 본문은 담지 않는다. 같은 범위 규칙을 따른다.

## 스킬 지식 등록(2026-10-12)

`~/.claude/skills`, `dev-experts-skills`, `biz-experts-skills`, `life-experts-skills`, `stock-experts`의 각 스킬 `SKILL.md`만 knowledge로 등록했다(241개).
중첩 팩(`video-framework/packs/*`), `synced`, `.trash`, 심볼릭 링크 스킬은 제외한다.
범위는 내용으로 나눴다. 개발 전문가 스킬과 범용 작업 방식 스킬은 shared, EZ Work 스킬 4개는 work,
비즈니스·생활·주식 전문가 스킬과 local-only 스킬은 personal이다. 근거는 회사 세션에 노출해도 되는 범용 지식인지 여부다.
`~/.claude/skills`는 work·shared·personal이 섞여 한 폴더 출처로 둘 수 없어 파일 출처를 쓴다.
한계: 본문이 영어인 스킬은 한국어 일반어("디버깅")로 잘 안 걸린다. 스킬 이름·영어 키워드·도메인 용어로는 잘 걸린다.


## 검증 기준

모델 import 불가 환경에서도 저장·검색, 한국어 별칭에서 키워드가 없는 연결 문서 발견, 미일치 빈 결과,
회사 검색·고정 기억 범위, 비활성 출처 제외, 생성 폴더·심볼릭 링크 제외, 읽기 실패·상한에서 기존 자료 보존,
반복 목록 등록의 중복 방지와 수동 기억 보존, REST/MCP 도구 목록의 일치를 테스트한다.
