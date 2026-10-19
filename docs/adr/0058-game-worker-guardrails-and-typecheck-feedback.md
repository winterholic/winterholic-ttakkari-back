# ADR 0058: 게임 worker 부작용 차단, 빌드 전 타입 검사와 실패 되돌림, 잠금 위반의 요청 기록 (Phase 3, TASK-G19 후속)

- **상태**: 구현됨 (2026-10-20). 실제 Claude worker 와 격리 컨테이너로 실측(설계서 §15).
- **관련**: ADR 0050(잠금 결정은 승인 해시가 강제), ADR 0054(자동 빌드), ADR 0046·`games/isolation.py`(격리 러너), 설계서 §14(실검증에서 발견한 결함)

세 가지는 모두 "worker 는 호스트에서 할 수 있는 일이 적고, 사용자 전역 설정이 worker 에게도 적용된다"는 같은 뿌리에서 나왔다.

## 1. 게임 worker 는 게임 저장소 밖에 기록을 쓰지 않는다

**문제.** 사용자 전역 `~/.claude/settings.json` 의 Stop 훅이 "이번 턴 내용이 work-history 에 없으면 막는다"를 강제한다. headless 로 띄운 게임 worker Run 도 이 훅을 받아, 게임 저장소와 무관하게 실제 `~/work-history/2026-10-20.md` 에 절을 추가했다(설계서 §14에서 2건, 이번 G19 첫 실측에서 1건 더 재현). 가드(PreToolUse)는 Write/Edit 만 write_roots 로 가두고 Bash 의 `cat >>` 리다이렉트는 막지 못한다.

**결정.** 게임 요청 Run(`usage.game` 표식이 있는 모든 Run)에 세 겹을 건다. 한 겹이 뚫려도 나머지가 막는다.

1. **설정 출처**(근본 원인): claude 를 `--setting-sources project,local` 로 띄운다. 사용자 전역 Stop 훅과 전역 CLAUDE.md 를 읽지 않는다. 설정 `game_worker_claude_setting_sources`(기본 `project,local`)이고 `LaunchSpec.setting_sources` 로 Run 단위로만 적용한다. 게임이 아닌 Run 은 기존 `claude_setting_sources` 그대로다.
2. **가드 보호 경로**: `TTAKKARI_GUARD_PROTECTED` 에 `game_worker_protected_paths`(기본 `~/work-history`, `~/development/context-work-history`, `~/development/share-work-history`)를 더한다. Write·Edit·Read 뿐 아니라 Bash 인자 경로(리다이렉트 대상 포함)도 `_protected` 로 막힌다. 게임 저장소·outbox 는 write_roots 에 있어 그대로 쓸 수 있다.
3. **프롬프트**: 게임 worker prompt 의 작업 지침에 "작업 기록·메모·전역 설정 등 저장소 밖 파일은 쓰지 않는다. 훅이나 지침이 기록을 요구해도 쓰지 않는다"를 넣었다.

이미 쓰인 `~/work-history/2026-10-20.md` 의 항목은 사실 기록이라 지우지 않았다.

**한계.** (1)은 claude 엔진에만 해당한다. codex 는 seatbelt 가, 다른 엔진은 (2)가 막는다(엔진별 확인은 하지 않았다). 사용자가 보호 경로를 늘리려면 설정을 바꾼다. 게임 worker 가 전역 CLAUDE.md 의 다른 지침(한국어 응답 등)도 받지 않게 되며, 게임 prompt 가 한국어이고 결과는 관제탑 화면용이라 영향이 없는 것으로 보았다(장기 영향 미검증).

## 2. 빌드 전 타입 검사(격리 러너의 `check` 단계)와 실패 되돌림

**문제.** worker 는 호스트에서 `npm install`·`tsc` 를 못 해서 타입 검사 없이 커밋한다(§14). 템플릿의 `build` 스크립트가 `tsc --noEmit && vite build` 라 첫 빌드가 걸러 주긴 하지만, 타입 오류와 vite 오류가 같은 "빌드 실패(exit N)"로 보이고, 이 오류가 worker 에게 돌아가지 않으며, worker 가 build 스크립트를 바꾸면 검사가 사라진다.

**결정.**
- `IsolatedBuildRunner.build_project` 에 설치와 빌드 사이의 `check` 단계를 더했다. `tsconfig.json` 이 있으면 `node_modules/.bin/tsc --noEmit --pretty false` 를 `--network none` 컨테이너에서 돈다(기존 러너·제한·비root 그대로, 새 시스템 없음). 빌드 스크립트 내용과 무관하다. typescript 가 설치돼 있지 않으면 로그에 "건너뜁니다"를 남기고 빌드를 계속한다(조용히 건너뛰지 않음). 설정 `game_build_typecheck`(기본 true)로 끈다.
- 실패하면 빌드 스크립트는 돌지 않고 `GameBuild.error` 가 `타입 검사 실패(tsc exit 2): 오류 N건` 과 오류 줄(최대 20건, `file(line,col): error TSxxxx: ...`)이 된다. 전체 로그는 기존 `log_tail` 이다. DB 컬럼·마이그레이션은 추가하지 않았고, 자동 수리는 오류 문구의 머리말(`isolation.TYPECHECK_FAILED`)로 타입 검사 실패를 가려낸다.
- **worker 로 되돌리는 두 경로.** (a) 다음 요청 prompt 의 `[직전 빌드 실패]` 절: 가장 최근 빌드가 실패했고 성공 빌드보다 새로우면 그 오류를 싣는다(사용자가 직접 보낸 수정 요청에도 적용). (b) 자동 수리: 자동 빌드가 타입 검사에서 실패하면 같은 게임에 `modify` 요청(origin=system, 오류 포함)을 자동으로 접수한다. 수리 Run 도 자동 빌드를 하므로 `usage.game.repair_attempt` 로 횟수를 세어 `game_auto_repair_attempts`(기본 1, 0 이면 끔)를 넘기지 않는다. 타입 검사 외의 빌드 실패(설치·vite)는 되돌리지 않는다. 한 번의 수리는 worker Run 1건 비용(실측 약 0.07달러)이다.
- **관제탑.** 실패 빌드는 기존 빌드 이력이 `error`·`log_tail` 로 이미 보인다.

## 3. 잠금 위반을 요청 기록에서 보이게 한다

**문제.** worker 가 잠금 결정을 바꿔도 자동 빌드는 `lock_violation` 을 로그에만 남기고 조용히 건너뛴다(빌드 행 없음). 요청 목록의 `lock` 은 요청 시점 값이라 계속 `verified` 로 보였다. 관제탑 상단 배너(`lock_matches_repo=false`)만 위반을 알렸다.

**결정.** 모든 게임 요청 Run 의 종료 훅(`autobuild.after_run`, 이제 자동 빌드 여부와 무관하게 호출)이 `Run.usage.game` 에 두 값을 덧붙이고 `GET /api/games/{id}/requests` 가 노출한다. 새 표는 없다.
- `lock_after`: Run 이 끝난 시점의 잠금 상태(verified·tampered·unapproved·unreadable). 요청 시점 `lock` 은 바꾸지 않는다.
- `auto_build_result`: `{status: succeeded|failed|skipped, code?, message?, build_id?, seq?}`. 건너뛴 사유(`lock_violation`·`build_in_progress`·`no_change`·`run_not_succeeded`)와 실패 종류(`typecheck_failed`·`build_failed`)를 담는다.
- 프런트 요청 행: `lock_after=tampered` 면 기존 "잠금 결정 변경 감지" 배지를 붙이고, 자동 빌드를 건너뛴 사유·타입 검사 실패를 한 줄로 알린다. 정상 결과는 아무것도 덧붙이지 않는다.

**승인 경로(기존, 변경 없음).** 잠금 결정을 바꾸는 유일한 정식 경로는 `POST /api/games/{id}/lock/approve`(사용자 인증)다. 저장소 매니페스트의 현재 결정을 새 해시로 고정하고 `locked_revision` 을 올린다. 에이전트 토큰은 닿지 못한다. 관제탑은 "바뀐 결정으로 다시 승인" 버튼으로 같은 API 를 쓴다.

## 하지 않은 것

- worker 가 승인 없이 잠금 결정을 바꾸는 것 자체를 막지는 않는다(저장소 파일을 쓸 수 있으므로). 빌드·적용 거부와 가시화가 방어선이다(ADR 0050).
- 자동 수리 Run 의 사용량 상한·일일 예산은 두지 않았다. 횟수 상한(Run 체인당 `game_auto_repair_attempts`)만 있다.
- 다른 엔진(Three.js 등)의 타입 검사 단계는 어댑터가 생길 때 같은 `check` 단계를 쓰면 된다.
