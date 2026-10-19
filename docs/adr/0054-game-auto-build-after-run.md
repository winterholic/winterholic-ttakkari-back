# ADR 0054: 개발·수정 요청 완료 후 자동 빌드 (Phase 3, TASK-G17)

- **상태**: 구현됨 (2026-10-19). 설정 `game_auto_build`(기본 true).
- **관련**: ADR 0050(빌드는 사용자·관제탑이 시작, Run 완료 → 빌드 연결은 후속으로 남겼음), ADR 0053(생성)
- **구현**: `src/ttakkari/games/autobuild.py`, `runs/manager.py::_finish` 끝의 훅 한 곳, 검증 `tests/test_game_autobuild.py`

## 배경

ADR 0050은 "worker 완료 후 자동 빌드 트리거"를 하지 않았다. 아이디어 한 줄로 게임을 만드는 흐름(ADR 0053)에서는 첫 개발 Run 이 끝나도 사용자가 별도로 빌드를 눌러야 플레이 화면이 생겨 흐름이 끊긴다. 수정 요청도 마찬가지다.

## 결정

- 요청 Run 의 `usage.game` 표식에 `auto_build: true` 가 있고 Run 이 `succeeded` 일 때만 `RunManager._finish` 가 커밋 뒤 `games.autobuild.after_run(run_id)` 를 백그라운드로 띄운다. 표식이 없으면 기존 동작과 같다. 전용 큐·훅 표는 없다.
- 표식은 `games.worker.run_tag` 가 단다: 요청의 `auto_build`(API 기본 true)가 true 이고, 종류가 `develop`·`modify` 이며 설정 `game_auto_build` 가 켜져 있을 때. `verify` 는 소스를 고치지 않으므로 달지 않는다. 생성 API(ADR 0053)의 첫 개발 요청은 항상 true 다.
- `after_run` 은 기존 `prepare_build`/`execute_build` 를 그대로 쓰고 빌드에 `run_id` 를 남긴다. 다음 경우는 빌드하지 않는다.
  - 마지막 성공·진행 중 빌드의 source commit 과 HEAD 가 같고 작업 트리도 깨끗하다(소스가 안 바뀜). 바뀐 게 없는데 같은 번들을 또 만들지 않는다.
  - 다른 빌드가 진행 중이다(`build_in_progress`). 사용자가 직접 시작한 빌드와 겹치지 않는다.
  - 잠금 결정이 승인본과 다르다(`lock_violation`). 빌드 행도 만들지 않으며 요청 목록의 "잠금 결정 변경 감지" 배지로 사용자가 알 수 있다.
- 빌드 실패는 Run 의 성공 여부를 바꾸지 않는다. 빌드 행이 `failed` 와 로그로 남고, `after_run` 은 어떤 예외도 밖으로 내지 않아 Run 종료 처리에 영향이 없다.
- 새 성공 빌드의 활성 빌드 전환은 기존 규칙(ADR 0050 `_follow_or_pin`)을 따른다. 사용자가 롤백으로 오래된 빌드에 고정해 둔 경우 자동 빌드가 고정을 덮지 않는다.

## 한계

- 서버가 Run 종료 직후 재시작되면 그 빌드는 만들어지지 않는다(복구 스캔 없음). 관제탑의 빌드 버튼이 대체 경로다.
- 소스 변경 판정은 git HEAD 와 작업 트리 상태만 본다. worker 가 커밋하지 않고 끝나도 더러운 트리로 빌드한다(빌드 행의 `source_dirty=true`).
- 테스트 환경은 `TTAKKARI_GAME_AUTO_BUILD=false` 로 두어 요청 테스트가 격리 빌드를 띄우지 않게 한다(`tests/conftest.py`).
