# ADR 0031. 원본 추적과 근거·시간·정정 이력을 가진 지식 기반

상태: 실행 설계 채택, 2026-10-16 사용자 즉시 구현 지시.

기존 파일·선별카드/triple만으로는 전체 입력 처리상태, 관계 누락, 과거 결정과 현재 상태, 근거 정정을 충분히 표현하지 못한다. 전체 의미 보존율이 미측정이라는 이유로 구조 개선 착수를 미루지 않는다.

[실행 설계](../knowledge/architecture.md)의 SourceRevision/Unit→Evidence→Assertion/ReviewEvent→검색 projection 경계를 적용한다. PostgreSQL 정본 위 지식 모델을 재설계하며 외부 graph/search engine은 교체 가능한 projection으로 둔다. 회사/개인 scope와 문서·기억의 기존 임베딩 정책은 유지한다. 기존 lexical 결과 순위는 유지하며 추가 knowledge 묶음과 명시적 knowledge 도구가 정확한 근거/검토된 관계를 제공한다. 2-hop은 명시 호출에 한정하며 전역 GraphRAG나 배경 모델 자동 추출은 활성화하지 않았다. 후보 추출은 호출 중인 모델이 bounded MCP batch를 제출하는 방식이다.

초기 tranche는 읽기 전용 context-inventory CLI였다. 후속 코드에는 0034 근거 원장·정확한 span·후보 추출 MCP·정정/검토 이력·추가 근거 검색·평가 CLI·모바일 검토가 구현됐다. 운영 전수 수입/추출·독립 holdout·runtime 검증은 아직 PMS 5.4.6 KD/K0~K8에서 진행한다. [운영 절차](../knowledge/operations.md)를 따른다. 원본 영구삭제·민감 추론 자동 확정·scope 확대를 포함하지 않는다.

출처 추적 증가에 따른 저장·운영 비용을 감수한다. 검증은 input accounting·관계 precision/recall·retrieval·citation·배포 runtime을 분리하며, 구조 구현과 검색 품질 개선 완료를 같은 상태로 보고하지 않는다.
