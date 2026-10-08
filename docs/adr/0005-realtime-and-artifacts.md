# ADR 0005. SSE + DB 이벤트 로그, Artifact 는 outbox·git 변경 기반 수집

- 상태: 채택 (2026-10-09)

## 실시간
- Run 이벤트는 모두 `run_events` 에 seq 와 함께 저장하고, SSE 는 DB 에서 seq 이후를 읽어 보낸다. 메모리 신호는 깨우기만 한다.
- 모바일은 연결이 자주 끊긴다. `Last-Event-ID` 로 이어 받으면 유실이 없다. 서버 재시작 뒤에도 기록이 남는다.
- WebSocket 대신 SSE: 서버→클라이언트 단방향이면 충분하고(지시·취소는 REST), 터널·프록시 호환이 좋다.

## Artifact 수집
- 에이전트에게 Run 별 outbox(`~/.ttakkari/data/outbox/<run_id>`)를 알려주고, 사용자용 결과물은 거기 저장하게 한다. Run 이 끝나면 outbox 파일을 등록한다(`agent_output`).
- 워크스페이스에서 바뀐 파일은 git 체크포인트 diff + 새 미추적 파일, 그리고 Claude 의 Write/Edit 도구 이벤트로 모은다(`agent_modified`, 최대 50개).
- 에이전트가 말한 경로는 믿지 않는다. 등록 전에 File Broker 가 실제 경로·루트 포함·일반 파일 여부를 다시 확인한다.
- 200MB 이하는 관리 영역으로 복사해 원본이 바뀌어도 결과물을 보존한다. 큰 파일은 참조만 하고 sha256 을 남긴다. 기본 보존 90일.
- PPTX·DOCX 는 LibreOffice 가 있으면 PDF 로 변환한다. 없으면 `unavailable` 로 두고 원본 다운로드만 준다(현재 미설치, `brew install --cask libreoffice` 후 자동 사용).
