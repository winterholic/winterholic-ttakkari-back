# ADR 0003. 전부 자동 실행 + 보험 계층

- 상태: 채택 (2026-10-09)
- 결정자: 사용자("전부 자동하고, 여차하면 인생망하지 않게 보험만 잘 깔면 된다")

## 결정
승인 프롬프트 없이 실행한다. 대신 되돌릴 수 없는 사고만 겹겹이 막는다.

| 계층 | Claude | Codex |
|---|---|---|
| 실행 권한 | `--dangerously-skip-permissions` | `approval_policy=never` |
| 쓰기 경계 | PreToolUse guard hook: Write/Edit 는 워크스페이스·outbox·tmp 안만 | OS 샌드박스 `workspace-write`(+네트워크). 사용자 전역 `danger-full-access` 를 덮는다 |
| 고위험 명령 | guard 가 sudo, 디스크 포맷, git 강제 푸시·히스토리 재작성·`git clean -f`, 키체인, 원격 스크립트 파이프 실행, 광역 재귀 삭제 등을 차단 | 샌드박스 밖 쓰기 불가 |
| 비밀 | guard 가 민감 파일 읽기·ttakkari 데이터 영역 접근 차단 | 읽기는 막지 못함(아래 한계) |
| 되돌리기 | git 워크스페이스는 Run 시작 시 `git stash create` 체크포인트를 `refs/ttakkari/runs/<id>` 에 남긴다 | 동일 |
| 비상 정지 | `POST /api/system/stop-all`, Run 별 취소, 기본 2시간 타임아웃 | 동일 |
| 반출 | File Broker: 민감 파일은 원격 열람·다운로드 차단, 파일 ID + 5분 서명 링크만 | 동일 |
| 정책 보호 | 설정은 `~/.ttakkari/config.env`, guard 가 에이전트의 `~/.ttakkari` 접근을 막는다(outbox 제외) | 샌드박스로 쓰기 불가 |

guard 는 hook 이 bypass 모드에서도 적용되는지 실측했다(`cat .env`, 워크스페이스 밖 Write 차단 확인).

## 알려진 한계 (수용)
- guard 는 셸 문자열 휴리스틱이다. 변수로 만든 경로(`F=.env; cat "$F"`), Bash 로 하는 워크스페이스 밖 쓰기(`touch ../x`)는 못 막는다. `tests/test_guard.py` 에 xfail 로 기록돼 있다.
- Codex 샌드박스는 읽기를 막지 않는다. 에이전트가 비밀을 읽어 네트워크로 보내는 것은 어느 엔진이든 막지 못한다. 이 앱의 반출 통제는 "원격 사용자·탈취된 토큰"에 대한 것이고, 에이전트 자체를 적대자로 보는 격리는 아니다.
- 체크포인트는 미추적 파일을 포함하지 않는다.

## 다음 후보
- Claude Code 자체 샌드박스 설정(Bash 쓰기 경계)을 켜서 guard 한계를 메우기. 개발 도구 캐시 경로 문제를 실측한 뒤 결정.
- 고위험 명령을 차단 대신 PWA 승인 요청으로 돌리기(Phase 2).
