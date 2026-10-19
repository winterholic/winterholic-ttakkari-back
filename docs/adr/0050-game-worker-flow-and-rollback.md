# ADR 0050: 게임 worker 연결, 잠금 결정 강제, 활성 빌드와 롤백 (TASK-G08, G12 백엔드)

- **상태**: G08 구현됨, G12 백엔드는 같은 문서의 아래 절에서 이어서 기록 (2026-10-19)
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md) §5.1, §6.1
- **관련**: ADR 0047(데이터 계약), ADR 0049(게임 origin)

## G08: 개발·수정·검증 요청은 기존 세션·Run으로 넘긴다

전용 큐를 만들지 않는다. 게임 요청 1건은 게임 workspace의 `ChatSession(kind=worker, engine=auto)`와 그 Run 하나다. 모델·계정 선택, 한도 대기, 결과 보고는 기존 `queue_run`·`worker_routing`·`RunManager` 그대로다.

| 경로 | 호출자 | 동작 |
|---|---|---|
| `POST /api/games/{id}/requests` | 인증된 사용자(PWA) | `kind`(develop·modify·verify)와 요청 문장. worker 세션과 Run을 만들고 `{session_id, run_id, ...}`를 돌려준다. |
| `GET /api/games/{id}/requests` | 사용자 | `Run.usage.game.game_id`로 이 게임의 요청 Run만 나열한다(별도 표 없음). |
| MCP `spawn_worker(game_id=, game_build_id=)` | 오케스트레이터 | worker가 게임 workspace에서 돌고 prompt 앞에 프로젝트·빌드·잠금 결정이 붙는다. 개인 모드 오케스트레이터만 가능(회사 범위 403). |

- 상태 읽기: `GET /api/sessions/{id}`, `GET /api/runs/{run_id}`, MCP `get_worker`(기존 도구).
- TaskType은 기존 값만 쓴다: develop·modify → `general`, verify → `verification`(독립 검토이며 소스를 고치지 않는다고 prompt에 명시).
- prompt는 self-contained다(`games/worker.py`): 게임 slug·엔진·저장소 경로, 대상 빌드(seq·commit·번들 해시 앞 16자·잠금 검증 여부), 잠금 결정, 금지 사항(호스트에서 npm 실행 금지, 매니페스트 id/engine/locked_decisions 변경 금지, 다른 경로 접근 금지), 사용자 요청.
- Run에는 `usage.game = {game_id, kind, build_id, lock}` 표식을 남겨 요청 목록·후속 피드백 연결(G11)의 근거로 쓴다.

### 잠금 결정은 prompt가 아니라 승인 해시가 강제한다

- 에이전트는 저장소의 `ttakkari-game.json`을 고칠 수 있으므로 prompt 문구는 권고일 뿐이다. 사용자가 승인한 결정의 sha256(`game_projects.locked_decisions_sha256`)과 현재 매니페스트가 다르면 `builds.prepare_build`가 `lock_violation`(409)으로 거부하고 빌드 행도 만들지 않는다. 실행 직전(`execute_build`)에도 다시 검사해 prepare 이후의 변경은 failed 빌드로 닫는다. 아직 승인한 적이 없으면 막을 대상이 없다(`lock_verified=false`로만 기록).
- 변조 상태에서도 요청 접수는 허용한다. prompt에 경고와 복구 방법(`git checkout -- ttakkari-game.json`)을 넣어 에이전트가 되돌릴 수 있게 해야 교착되지 않는다. 되돌리기 전에는 빌드와 적용(활성화·롤백)이 거부된다.
- 승인본을 바꾸는 유일한 경로는 사용자 인증이 필요한 `POST /api/games/{id}/lock/approve`다. 에이전트 토큰은 이 라우트에 닿지 않는다.

### 이 단위가 하지 않는 것

- worker 완료 후 자동 빌드 트리거: 빌드는 아직 사용자/관제탑이 `POST /api/games/{id}/builds`로 시작한다. Run 완료 → 빌드 연결은 G11·G14에서 한다.
- 시각 피드백(GameFeedback) 저장과 요청 연결: G11.
