# ADR 0001. 단일 프로세스 FastAPI + PostgreSQL, 인메모리 Run 관리

- 상태: 채택 (2026-10-09)
- 결정자: 에이전트 제안, 사용자 기술스택 지정(FastAPI·PostgreSQL)

## 맥락
사용자 한 명이 Mac Studio 한 대에서 쓰는 앱이다. 동시 Run 은 많아야 몇 개다.

## 결정
- FastAPI 프로세스 하나가 API, Run 실행(asyncio subprocess), 미리보기 변환 큐를 모두 맡는다.
- 영속 상태(Run, 이벤트, Artifact, 감사 로그)는 PostgreSQL 에 둔다. 실행 중 프로세스 핸들과 SSE 깨우기 신호만 메모리에 둔다.
- 서버가 재시작되면 `queued/running` Run 을 `interrupted` 로 정리하고 남은 에이전트 프로세스 그룹을 종료한다.

## 대안
- Celery/RQ + Redis 워커: 프로세스 분리로 API 재시작과 Run 이 독립된다. 대신 인프라와 상태 동기화가 늘어난다. 단일 사용자 규모에서는 이득이 작다.
- uvicorn 워커 여러 개: 인메모리 핸들 때문에 쓸 수 없다. **워커는 1개로 고정한다.**

## 결과
- API 를 재시작하면 실행 중 Run 은 끊긴다. 엔진 대화(session id)는 보존되므로 같은 세션에서 이어서 지시할 수 있다.
