# Qwen worker 검증 후속

연결 adapter의 기본 6턴이 standalone 기본 20턴을 덮어쓰는 회귀를 수정했다.
`TTAKKARI_QWEN_LOCAL_TURNS`로 1~60 범위에서 조절하고 기본 20턴을 사용한다.
활성화 gate와 auto-routing 정책은 변경하지 않는다.

검증: 별도 소유 테스트 DB에서 adapter/Run/routing 70 passed (13.32초),
DB 없는 command budget 회귀 1 passed, Ruff 전체 및 compileall/diff check 통과.
테스트 DB는 실행 후 정리했다. 실제 모델/Seatbelt/native host monitor의 운영 검증을 뜻하지 않는다.

별도 qwen-local-harness는 9932231에서 SIGTERM/SIGINT 취소 시 자식 정리와 잠금 해제,
정리 실패의 명시적 기록, generation backend port 차단 목록을 보완했다.
실제 자기 CLI 프로세스 취소 및 synthetic host/server 회귀를 포함해 81개 검증을 통과했다.
원시 기록은 해당 프로젝트 WORK_LOG.md와 Run outbox a311845c-d949-4ebb-b4f5-a9c3484d21c5를 따른다.

남은 경계: standalone doctor readiness 계약, OS/native guard 강제 적용과 제품 모델 수락.
검증 전에는 기본 비활성과 명시 선택 제한을 유지한다.
