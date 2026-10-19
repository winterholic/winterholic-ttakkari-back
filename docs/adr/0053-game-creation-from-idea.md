# ADR 0053: 아이디어 한 줄에서 게임 프로젝트 생성 (Phase 3, TASK-G15)

- **상태**: 구현됨 (2026-10-19). 관제탑 생성 화면은 front `GameCreate`(TASK-G16).
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md) §8 Phase 3
- **관련**: ADR 0046(어댑터), 0047(데이터 계약), 0050(worker 연결·잠금), 0052(bridge)
- **구현**: `src/ttakkari/games/create.py`, `POST /api/games/create`(`src/ttakkari/api/games.py`), 검증 `tests/test_api_game_create.py`

## 배경

Phase 2 E2E는 scaffold 를 어댑터 직접 호출로 대체했다. 사용자는 PWA 에서 저장소 slug 를 입력해 이미 있는 저장소를 연결할 수만 있었고, 비개발자가 "아이디어 한 줄"로 게임을 시작할 경로가 없었다.

## 결정

`POST /api/games/create` 하나가 기존 부품을 순서대로 호출한다. 새 표·새 큐·새 워커 경로는 없다.

1. **이름·slug**: 이름을 안 주면 아이디어 첫 줄(60자)을 쓴다. slug 를 안 주면 이름의 ASCII 글자로 만들고, 한글처럼 남는 글자가 없으면 `game-<무작위 8hex>`를 쓴다. 자동 slug 가 이미 쓰이면 무작위 접미사로 피한다. 사용자가 slug 를 직접 지정했는데 이미 쓰이면 409 `exists`다. "쓰이는 slug"는 DB 행(보관된 게임 포함)이거나 디스크에 어떤 항목이든 있는 경우로, 등록되지 않은 디렉터리도 덮어쓰지 않는다.
2. **scaffold**: 엔진 어댑터(`get_adapter`)의 `scaffold_project`. 엔진은 지금 `web-phaser` 하나이며 알 수 없는 값은 422다. 템플릿 복사와 첫 커밋(`scaffold web-phaser project`)은 어댑터가 하던 그대로다.
3. **등록**: `register_game_project`(Workspace 연결, 선택적 PMS 프로젝트 생성). 기존 PMS 프로젝트 연결은 이 API 범위가 아니며 등록 API(`POST /api/games`)가 맡는다.
4. **잠금 결정**: 사용자가 요청에 적은 `locked_decisions`(최대 20개, 각 1~500자)가 있으면 매니페스트에 쓰고 같은 호출에서 승인 해시로 고정한다(`game.lock.approve`, `via=create` 감사 기록). 이 라우트는 사용자 인증이 필요하고 에이전트 토큰이 닿지 않으므로 사용자가 직접 입력한 결정만 승인된다. 결정을 안 적었으면 승인하지 않는다(빈 목록을 승인하면 이후 사용자가 결정을 추가할 때마다 `lock_violation`이 되어 교착된다).
5. **첫 개발 요청**: `start_development`(기본 true)이면 같은 호출에서 기존 `games.worker.start_request(kind="develop")`로 worker 세션·Run 을 접수한다. 요청 문장은 아이디어, 템플릿 구조 유지 지침(`bridge.ts`·`installBridge`·`preserveDrawingBuffer`), `publishState` 로 규칙 값 게시, 터치·키보드 지원, 외부 에셋 금지(코드로 그리기), README 기록, 잠금 결정을 담는다.

### 실패 처리

- scaffold 이전 거부(검증·slug 충돌·엔진)는 파일을 만들지 않는다.
- scaffold 이후 등록·승인이 실패하면 **이번 호출이 만든 디렉터리만** 지운다. 경로가 `games_root/<slug>` 와 정확히 같고 심볼릭 링크가 아닐 때만 삭제한다. scaffold 가 "이미 있음"으로 실패한 경우(동시에 같은 slug 를 만든 다른 호출)는 지우지 않는다.
- 게임 생성은 성공했는데 첫 개발 요청 접수만 실패하면 게임은 남기고 `201` 응답의 `request_error`({code, message})로 알린다. 관제탑에서 다시 요청할 수 있다.
- 생성은 `game.create`(성공·거부) 감사 기록을 남긴다.

## 하지 않는 것

- 아이디어에서 장르·잠금 결정을 LLM 으로 자동 제안하지 않는다. 이름과 장르는 사용자가 비우면 비운 채로 둔다(장르는 빈 문자열). 대화형 정리는 후속 단위다.
- 생성 시 템플릿 자동 빌드는 하지 않는다. 빌드는 격리 러너를 쓰고 같은 게임에서 동시에 하나만 돌 수 있어, 첫 개발 요청이 끝난 뒤의 빌드와 겹치지 않게 하려는 것이다. 개발 완료 후 빌드는 별도 단위(TASK-G17)에서 다룬다.
- 두 번째 엔진은 이 단위에 없다. `engine` 필드는 지금 `web-phaser` 만 허용한다.
