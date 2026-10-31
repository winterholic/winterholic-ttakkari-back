# 계정관리 응답 분리

기존 GET /api/accounts는 Muse usage_for를 await한 다음 모든 계정의 identity/readiness를 동기 확인했다.
프론트의 개인·회사 목록은 그 하나의 요청이 끝날 때까지 skeleton으로 남았다.

- details=false는 DB 정보·메모리 cooldown만 반환한다. status_pending=true이고 로그인/실행 가능은 미관측으로 표시한다. 이 표시값은 실제 배정 정책을 변경하지 않는다.
- 기존 기본 목록 응답 형태는 유지하며 사용량 수집을 새로 시작하지 않고 캐시된 Muse 관측을 쓴다. 동기 identity/readiness 확인은 to_thread에서 실행한다.
- GET /api/accounts/{id}/status는 DB 트랜잭션을 먼저 commit해 반환한 뒤 계정 하나만 확인한다. Muse는 기존 usage_for TTL/in-flight 공유를 재사용한다. 25초 timeout은 504로 반환한다. 응답에는 이름·순서·켜기 등 수정 가능한 메타데이터가 없어 늦게 온 상태가 편집값을 덮지 않는다.
- 프론트가 기존 백엔드를 먼저 만나도 details 파라미터를 무시한 기존 응답을 받아 동작하며 status_pending이 없는 응답에는 추가 상태 조회를 보내지 않는다.

검증: unit/API/route inventory 51 passed; 사용량 수집기/Muse pump/orchestrator 회귀 25 passed; ruff 통과.
테스트는 실제 임시 PostgreSQL과 가짜 공급자를 썼다. blocked provider event가 다른 계정 응답을 막지 않는지, summary가 공급자·identity를 전혀 호출하지 않는지, status owner 인증/404/schema/504를 확인했다.
운영 공급자 지연 수치는 측정하지 않았다. 브라우저 합성 지연은 운영 성능 수치가 아니다.
