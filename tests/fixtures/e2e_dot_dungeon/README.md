# 도트 던전 E2E 기록물 (TASK-G14)

`v1/`, `v2/` 는 로컬 격리 따까리의 **실제 worker(Claude sonnet, 따까리 `/api/games/{id}/requests` 경로의 Run)가 쓴 소스**다. 만든 사람은 사람이 아니다.

- `v1/`: 개발 요청(develop)의 결과. 저장소 커밋 `09aa3c6`(템플릿 scaffold `6935a9e` 위). 템플릿과 다른 파일만 둔다.
- `v2/`: 스크린샷 주석 수정 요청("캐릭터가 배경에 묻혀 보여. 더 선명하게 하고 공격 이펙트 추가")의 결과. 저장소 커밋 `6be3ad8`.

`tests/test_e2e_game_studio.py` 는 이 소스를 **재생**해 격리 빌드·브라우저 규칙 검증·롤백·잠금 거부를 재현한다.
재생은 worker 실행이 아니다. 실제 worker 단계의 증거(Run ID, 커밋, 빌드 ID, 해시, 스크린샷)는 `docs/design/game-studio.md` §12 와 보고서 증거 JSON 에 있다.
