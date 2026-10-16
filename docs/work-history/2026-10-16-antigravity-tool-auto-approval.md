# Antigravity 무인 실행 도구 승인

2026-10-16 사용자 요청: Claude의 bypass permissions와 같은 옵션을 Antigravity 실행에 적용한다.

설치된 `/opt/homebrew/bin/agy --help`에서 `--dangerously-skip-permissions`를 확인했다. 도움말은 모든 도구 승인 요청을 묻지 않고 자동 승인하는 옵션으로 설명한다. `AntigravityAdapter.build_command`에 이 옵션을 추가했다. 기존 `--sandbox`, `--mode accept-edits`, 허용 디렉터리, MCP 수명 관리, timeout·취소 처리는 유지한다. 현재 정책을 ADR 0029에 반영했고 과거 날짜의 미사용 기록은 당시 증거로 남겼다.

이 변경은 CLI의 도구 승인 대기를 없애는 범위다. 운영 backend 9d0b6fa의 `RunManager._terminate`에서 관측한 `os.killpg(..., SIGTERM)` PermissionError나 Argon의 빈 최종 응답이 해결됐다는 뜻은 아니다.

검증:
- 기존 명령 구성 테스트의 기대값을 먼저 변경하고 DB 없이 실행해 자동 승인 옵션 누락으로 실패하는 것을 확인했다.
- 수정 후 `PYTHONPATH=src <backend-venv>/bin/python -m pytest --noconftest tests/test_antigravity_adapter.py -q`: 9 passed.
- Ruff: All checks passed. `git diff --check`: 통과.
- 첫 일반 pytest 실행은 공통 DB fixture에서 `meetings`를 drop하려다 외부 FK 의존성으로 차단됐다. 같은 DB 검사를 반복하지 않고 DB가 필요 없는 어댑터 테스트를 `--noconftest`로 실행했다. 운영 DB나 스키마를 수정하지 않았다.
- 실제 CLI 모델 응답·MCP 호출·재개 및 운영 반영은 이 기록 작성 시 미검증. 기존 sandbox 거부를 반복 재현하지 않았다.

다른 작업자의 manager.py·test_antigravity_run.py 변경과 충돌하지 않도록 별도 worktree에서 준비했다. 배포는 canonical backend의 변경 통합·검증·clean 상태가 필요하다.
