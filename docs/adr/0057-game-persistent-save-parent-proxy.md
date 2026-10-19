# ADR 0057: 게임 영구 세이브 — 부모 PWA 대리 보관 (Phase 3, TASK-G20)

- **상태**: 구현됨 (2026-10-19). 이 기기 localStorage 에만 보관한다.
- **관련**: ADR 0052 §3(메모리 + export/import 로 결정하고 영구 저장은 Phase 3 으로 넘김), ADR 0049(sandbox·origin·postMessage 계약)
- **구현**: 템플릿 `templates/web-phaser/src/bridge.ts`(ready 알림, `save.loaded`), front `src/features/games/gameSave.ts`·`useGameSave.ts`, 검증 `tests/test_game_bridge.py`, front `gameSave.test.ts`·`useGameSave.test.tsx`

## 결정

ADR 0052 의 선택지 2(부모가 localStorage 에 대리 보관)를 쓴다. sandbox 와 별도 origin 경계는 그대로이고 게임에 저장소 권한을 주지 않는다. 서버(선택지 3)는 쓰지 않는다.

- **게임 쪽**: 새 요청 종류는 없다. 이미 있는 `save-export`/`save-import` 위에 두 가지만 더했다. (1) 부팅 뒤 부모 origin 으로만 `{type: 'ttakkari:ready'}` 를 알린다. (2) `save.loaded` Promise 는 부모가 세이브를 넣어 주면(또는 3초가 지나도 안 오면) 풀린다. 진행 저장이 필요한 게임은 시작할 때 `await save.loaded` 뒤에 `save.get` 으로 읽는다. 부모가 없어도 3초 뒤 풀려 영원히 기다리지 않는다.
- **부모 쪽**(`useGameSave`): 게임 iframe 이 `ready` 를 보내면(출처 창과 origin 검증) 저장된 세이브를 한 번 `save-import` 로 넣는다. `ready` 를 모르는 이전 템플릿 빌드는 2.5초 뒤 폴백으로 한 번 넣는다(게임이 그때 읽는지는 게임에 달렸다). 화면이 보이는 동안 5초마다 `save-export` 로 내보내 **바뀐 값만** 저장하고, 탭이 숨겨질 때(visibilitychange·pagehide)에도 한 번 시도한다.
- **키와 범위**: `localStorage["ttakkari-game-save:v1:<game_id>"]`. 게임 단위이며 빌드가 바뀌어도 이어진다. 값은 `{data, savedAt, buildSeq}` 이고 `data` 는 64KB 이하의 JSON 객체 문자열이다(템플릿 `MAX_SAVE_CHARS` 와 같음).
- **보호 장치**: 빈 세이브(`{}`)는 저장하지 않는다. 부팅 직후 비어 있는 게임 메모리가 실제 저장을 덮어쓰지 못하게 하려는 것이다. 불러오기를 시도하기 전에는 내보내기도 하지 않는다. 다른 창이 보낸 `ready`·응답, 요청 id 가 다른 응답, 객체가 아닌 값은 버린다. localStorage 가 막혔거나 가득 차면 던지지 않고 저장하지 않는다.
- **지우기**: 플레이 화면 아래의 "저장 지우기"(2단계 확인)가 저장을 지우고 열린 게임의 메모리 세이브도 `save-import {}` 로 비운다(안 비우면 다음 주기에 다시 저장된다).
- **develop 요청 문장**(ADR 0053)이 새 게임 worker 에게 `save.set/get` 과 `await save.loaded` 를 쓰라고 안내한다.

## 한계

- **이 기기·이 브라우저에만 있다.** 다른 기기와 동기화되지 않고, 브라우저 데이터를 지우면 사라지며, iOS Safari 는 오랫동안 쓰지 않은 사이트 데이터를 정리할 수 있다(미검증). 기기 간 이어하기가 필요하면 서버 저장(선택지 3)을 새 단위로 연다. 서버 저장은 게임 origin 이 자격증명이 없으므로 부모 대리 호출이 필요하다.
- 보관 주기가 5초라 그 사이에 닫으면 마지막 몇 초가 빠질 수 있다. 탭이 숨겨질 때의 저장은 비동기 응답을 기다리므로 보장되지 않는다.
- 세이브 스키마 호환은 게임이 책임진다. 게임을 수정한 뒤 이전 세이브가 맞지 않으면 "저장 지우기"로 지울 수 있다.
- 이전 템플릿으로 만든 게임은 `save.loaded` 가 없다. 그 게임은 폴백 불러오기가 닿는 시점에 따라 값을 못 읽을 수 있다.
