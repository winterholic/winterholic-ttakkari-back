# AI Research Lab — 초기 구현과 실제 검증

사용자의 전체 구축 지시(0~14장)를 받아 설계서에서 멈추지 않고 초기 API·MCP·관제탑을 구현했다. 프로젝트 전체 확장 작업은 아직 남아 있다.

- 별도 worktree `back-research-lab`, `front-research-lab`. 다른 작업자의 미커밋 변경을 가져오거나 되돌리지 않음.
- 작업 중 main에 KB1+KB2가 들어와 공개 연구 자료의 저장·근거 FK를 개인 `knowledge_*`에서 범용 `kb_*`로 변경. 새 Lab 스키마는 `0056_research_lab`, KB 0055 뒤에 연결. 신규 연구 방법/개념/관계/연구 단계는 열린 문자열.
- 지식은 `source_reported / unverified / locally_measured`를 구분. 실행을 가장한 입력과 부정확한 인용을 거부. 원문 bytes는 KB 해시 저장소, 인용은 NFKC+LF 패시지 기준. 연구 이력은 DB에서 수정/삭제 금지.
- `/research` UI: 연구 생성/계획/근거·관계/선택 이유/실험·조건 비교/타임라인. 같은 scope의 과거 방법을 다른 연구 실험에서도 선택 가능. `manage_research`는 기존 인증·공급자 하네스를 재사용한다.
- 직접 실행은 입력량·시간·동시성을 제한한 CPU lexical/BM25 평가. 외부 GPU/에이전트 receipt는 별도로 보존. 메모리 budget은 계획값이며 OS 메모리 제한을 구현했다고 주장하지 않음.
- Qwen3.5-4B host GPU 경로 f16/q8 KV 각각 3회, 96 generated tokens: 중앙 121.7337 / 122.4972 tok/s. 작고 짧은 파일럿이며 품질/유의한 개선/35B/장치별 layer 로그는 미검증. 시험 인스턴스 종료 확인.
- 실제 ADR 54개·3질문에서 lexical/BM25 모두 Recall@5·MRR 0.6667. 개선 미확인. C 경험적 발견과 D 상충 주장도 정확한 발췌/조건 미확정으로 저장.
- 새 GPT-6 Luna/Codex 세션이 기존 요약 없이 resume API를 읽어 상태·결과·다음 행동을 복원. 처음에는 실패가 없음을 보고했으며, 이후 실제 negative control `invalid_qrels`를 저장해 새 컨텍스트 후속 재개 검증 요청. 배포된 MCP 실행과 구분한다.
- 검증: 관련 backend/DB/generation/지식/API 101 passed, frontend 29 passed, 전체 front lint·typecheck/build, research migration parity 차이 0, 공식 브라우저 MCP의 1280/390 화면 및 실제 조작. 전체 Alembic check는 기존 다른 도메인 인덱스 차이가 남아 있어 전역 통과라고 쓰지 않음.
- PMS 프로젝트 `664a2c29-b3d1-425c-b016-6c430cb7a7dc`: 초기 6단계와 확장 5개 leaf를 실제 등록. 자동 수집/강화 선택/장시간 실행/다양한 시각화/운영 확대 검증은 미완료로 유지.

설계와 경계: [architecture](../research-lab/architecture.md). 실행 명령·표면별 한계: [verification](../research-lab/verification.md). 사용자 전달본 및 원본 evidence: `/Users/winterholic/.ttakkari/data/outbox/4871dd97-0082-4cb1-a189-b23ebf6a8be5`.

이 기록 작성 이후 커밋·push·기존 generation 배포 예약을 수행한다. 요청 수락을 배포 성공으로 기록하지 않으며 예약 응답은 outbox의 별도 JSON으로 보존한다. 운영 활성화, 실제 새 MCP 실행, 확대 연구는 후속 확인 대상이다.
