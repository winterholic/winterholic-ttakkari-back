# 따까리 백엔드 에이전트 안내

## 현재 작업 방식 (2026-11-02~)
- 사용자는 따까리를 직접 고칠 시간이 거의 없다. 최근 2주 작업이 부진했고, 부족한 부분의 수정·개발을 에이전트(Claude, Codex)가 맡는다.
- 매 단계 승인을 원하지 않는다. 일반 수정은 알아서 진행하고 결과만 보고한다. 배포, 푸시, 삭제처럼 되돌리기 어려운 동작은 신중히 한다.
- 설계 근거는 `docs/adr/`를 먼저 읽고, API 계약은 `docs/api-contract.md`를 따른다.
- 여러 세션이 같은 main을 동시에 바꾼다. 내 변경만 경로를 지정해 커밋하고, 남의 변경은 되돌리지 않는다.

## 실행 중인 따까리 API 호출
- `~/bin/ttk get|post|patch <경로> [JSON]`를 쓴다. 키체인에서 로그인하고 비밀번호·토큰은 출력하지 않는다. 키체인 값(`security find-generic-password -w`)을 직접 읽지 않는다.
- 예: `ttk get /api/sessions`, 오케스트레이터에 지시는 `ttk post /api/sessions/<id>/runs '{"prompt":"..."}'`.
- `/api/orchestrator/*` 워커 API는 오케스트레이터 세션 전용 JWT(`aud=ttakkari-orch`)가 필요해 `ttk`로 부를 수 없다. 워커는 오케스트레이터 세션에 Run을 보내 간접으로 다룬다.
