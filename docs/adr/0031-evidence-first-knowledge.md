# ADR 0031. 원본 추적과 근거·시간·정정 이력을 가진 지식 기반

상태: 실행 설계 채택, 2026-10-16 사용자 즉시 구현 지시.

기존 파일·선별카드/triple만으로는 전체 입력 처리상태, 관계 누락, 과거 결정과 현재 상태, 근거 정정을 충분히 표현하지 못한다. 전체 의미 보존율이 미측정이라는 이유로 구조 개선 착수를 미루지 않는다.

[실행 설계](../knowledge/architecture.md)의 SourceRevision/Unit→Evidence→Assertion/ReviewEvent→검색 projection 경계를 적용한다. PostgreSQL 정본 위 지식 모델을 재설계하며 외부 graph/search engine은 교체 가능한 projection으로 둔다. 회사/개인 scope와 문서·기억의 기존 임베딩 정책은 유지한다. ADR 0009의 1-hop 고정/자동 추출 없음은 향후 K3/K4에서 근거 검증·budget·회귀 gate를 가진 후보 추출과 질문별 graph 경로로 대체할 계획이며, 현재 구현이 이미 바뀐 것으로 읽으면 안 된다.

초기 tranche는 읽기 전용 context-inventory CLI뿐이다. 추가 스키마·추출/검색·UI·전수 재처리는 PMS 5.4.6 KD/K0~K8의 후속 작업이다. 원본 영구삭제·민감 추론 자동 확정·scope 확대를 포함하지 않는다.

출처 추적 증가에 따른 저장·운영 비용을 감수한다. 검증은 input accounting·관계 precision/recall·retrieval·citation·배포 runtime을 분리하며, 구조 구현과 검색 품질 개선 완료를 같은 상태로 보고하지 않는다.
