# ADR 0005. SSE + DB 이벤트 로그, Artifact 는 outbox·git 변경 기반 수집

- 상태: 채택 (2026-10-09)

## 실시간
- Run 이벤트는 모두 `run_events` 에 seq 와 함께 저장하고, SSE 는 DB 에서 seq 이후를 읽어 보낸다. 메모리 신호는 깨우기만 한다.
- 모바일은 연결이 자주 끊긴다. `Last-Event-ID` 로 이어 받으면 유실이 없다. 서버 재시작 뒤에도 기록이 남는다.
- WebSocket 대신 SSE: 서버→클라이언트 단방향이면 충분하고(지시·취소는 REST), 터널·프록시 호환이 좋다.

## Artifact 수집
- 에이전트에게 Run 별 outbox(`~/.ttakkari/data/outbox/<run_id>`)를 알려준다. Run 이 끝나면 outbox 파일을 등록하되(`agent_output`), 분류는 항상 에이전트 파일(`area=agent`)이다. 에이전트가 outbox 를 작업 공간처럼 써서 로그·증거 JSON·브라우저 캡처 같은 중간 산출물이 함께 쌓이고, 경로나 이름만으로는 사용자가 요청한 결과물인지 판별할 수 없기 때문이다.
- 보관함 '파일' 탭에는 사용자 업로드·직접 등록과, 에이전트가 사용자에게 넘기려고 `share_file`(`POST /api/agent/share`)로 명시적으로 올린 파일만 둔다. 공유는 호출한 Run(토큰에 묶인 Run)에 연결하고, 이미 같은 세션·같은 원본 경로로 등록된 행이 있으면 새 행을 만들지 않고 그 행을 파일로 승격한다. 수집기도 Run 이 아니라 (세션, 원본 경로)로 중복을 판별해 같은 파일이 두 탭에 쌓이지 않게 한다.
- 이미 파일 탭에 있던 부산물은 마이그레이션 0052 가 확실한 것(`*.log`, outbox 바로 아래 `evidence/`·`browser/page-*`, `*-worker-evidence.json`)만 에이전트 파일로 옮긴다. 애매하면 옮기지 않는다. 사용자 결과물이 묻히는 쪽이 탭이 지저분한 쪽보다 나쁘다. 분류는 앱과 MCP(`set_artifact_area`)에서 되돌릴 수 있다.
- 워크스페이스에서 바뀐 파일은 git 체크포인트 diff + 새 미추적 파일, 그리고 Claude 의 Write/Edit 도구 이벤트로 모은다(`agent_modified`, 최대 50개).
- 에이전트가 말한 경로는 믿지 않는다. 등록 전에 File Broker 가 실제 경로·루트 포함·일반 파일 여부를 다시 확인한다.
- 200MB 이하는 관리 영역으로 복사해 원본이 바뀌어도 결과물을 보존한다. 큰 파일은 참조만 하고 sha256 을 남긴다. 기본 보존 90일.
- PPTX·DOCX 는 LibreOffice 가 있으면 PDF 로 변환한다. 없으면 `unavailable` 로 두고 원본 다운로드만 준다(현재 미설치, `brew install --cask libreoffice` 후 자동 사용).
