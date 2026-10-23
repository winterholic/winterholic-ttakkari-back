# ADR 0070. 하네스 조회를 7개 그룹 › 관심사 › 레이어 계층으로 재설계한다

- 상태: 구현(2026-10-23). 사용자 피드백("내가 보기에는 어렵다. 각 카테고리와 관심사별로, 레이어 구조에 맞게 볼 수 있는 구조면 좋겠다")에 따른 ADR 0069의 후속이다. 에이전트의 구현 설계이며 별도 승인 기록을 뜻하지 않는다.
- 바뀌는 것: 평면 목록(카테고리 13종 필터 + 통합 검색)이 아니라 그룹 → 관심사 → 레이어로 들어가는 구조가 1차 내비게이션이다. 수집·보안·마스킹·읽기 전용 원칙은 0069 그대로다.

## 1. 구조

| 단계 | 내용 |
|---|---|
| 그룹(7) | Claude · Codex · Muse · Qwen Code · Antigravity(메인 하네스 5) / 따까리 / 프로젝트별. 항목은 정확히 한 그룹에 속한다 |
| 관심사 | 지침(규칙 포함) · 스킬 · 훅 · 권한·설정 · MCP · 메모리 · 에이전트·커맨드 · 플러그인. 따까리 그룹은 주입 지침 · 라우팅·모델·계정 · 가드 · MCP · 실행 환경 · 메모리. 그룹에 항목이 하나라도 있는 관심사만 보인다. "없음"과 근거만 있는 관심사는 흐리게 보인다 |
| 레이어 | 그룹마다 정의한 위→아래 순서. 아래 레이어가 위 레이어 위에 얹힌다. 레이어마다 순서의 근거를 `code`(이 저장소 코드로 확인) 또는 `external`(엔진 CLI 동작이라 확인 필요)로 밝힌다 |

레이어 정의(`harness_view/taxonomy.py` GROUP_LAYERS):

- Claude: 사용자 전역(~/.claude) → 따까리 계정 폴더(CLAUDE_CONFIG_DIR) → 프로젝트 → 세션 주입
- Codex: 사용자 전역(~/.codex) → 프로젝트 → 세션 주입
- Muse: 번들(플러그인) → 사용자 설정(~/.config/muse) → 프로젝트 → 세션 주입
- Qwen Code: 하네스 저장소 → 실행 시 생성 → 프로젝트(로드 안 함: `--bare`, `--setting-sources user`) → 세션 주입(어댑터)
- Antigravity: 내장 → 사용자 전역(~/.gemini, ~/.agents) → 프로젝트 → 세션 주입
- 따까리: 코드 내장 → 설정(config) → 비서 지침 파일 → DB → 측정·생성(출처 종류별 레이어)
- 프로젝트별: 프로젝트 루트 → 하위 폴더

프로젝트 레이어는 항목을 직접 세지 않고 프로젝트별 하네스로 가는 링크(해당 엔진의 프로젝트 수·항목 수)를 단다. 세션 주입 레이어는 `layers.build`(세션별 레이어 뷰와 같은 코드)에서 전달 방식·가드·도구 레이어를 꺼내 보여 주고, 프롬프트 합본은 개수만 요약한다.

## 2. 분류 규칙(`taxonomy.classify`)

수집기가 붙인 `hint_group·hint_layer·hint_concern`이 먼저고, 없으면 파생한다.

- scope=project 또는 프로젝트 수집기 → 프로젝트별. 프롬프트·가드·capability·정책·따까리 MCP 도구 → 따까리(고정 기억은 메모리 관심사).
- 그 밖에는 엔진으로: claude/codex/antigravity/muse/qwen-local. 공용 `~/.agents`(shared)는 npx skills 류가 여러 도구용으로 까는 폴더이고 규칙 폴더가 Antigravity 규칙이라 Antigravity에 센다(Codex가 읽는지는 확인 필요).
- qwen-local 어댑터가 건드리는 항목(짧은 계약, Qwen 설정, 활성화)은 Qwen Code 그룹의 세션 주입 레이어다. 접수용 Qwen 라우터 프롬프트는 접수 담당이라 따까리 그룹이다.
- Codex `*.rules`는 명령 허용·금지 정책이라 지침이 아니라 "권한·설정"이다. Codex `references/*.md`는 지침이다.
- `validate`가 모든 항목의 그룹·관심사·레이어 정의 소속과 id 유일성을 확인한다(시험).

## 3. 새 수집 경로

| 대상 | 읽는 곳 | 비고 |
|---|---|---|
| Muse | `~/.config/muse/{settings,trust}.json`, `auth.json`은 존재만 확인(열지 않음), `~/.local/share/muse/plugins/cache/builtin/<플러그인>/<스킬>/<해시>/SKILL.md`(스킬당 최신 해시), 번들 플러그인 폴더 이름 | 위치 근거: `agents/muse.py`, Muse 번들 스킬 `manage-settings`·`migrate`·`create-skill`. 지침(규칙)·개인 메모리·사용자 훅·MCP는 파일이 없고 저장 위치도 확인되지 않아 "없음 + 근거"로 둔다(확인 필요). 따까리가 Run 마다 주입하는 PreToolUse 훅과 MCP는 코드 근거로 세션 주입 레이어에 둔다 |
| Qwen Code | `qwen-local-harness` 저장소(설정 `qwen_local_worker_path`가 홈 안일 때 그 위치, 아니면 기본 위치)의 `AGENTS.md`, `prompts/worker.md`, `skills/qwen-local-delegate/SKILL.md`, `config/harness.conf`, `.runtime/settings.json`(env 값 숨김), `src/qwen_local/runtime.py`(AST 로 `--agents` 정의와 고정 인자만 추출, 실행하지 않음) | qwen-worker 는 Claude Code(`--bare`)를 로컬 Qwen 모델에 연결한다. `.runtime/profile`(로그인·세션)은 패턴에 없다. 훅·MCP·메모리·플러그인은 설계상 없음(근거 문구) |
| Antigravity 보강 | `~/.gemini/config/mcp_config.json`, `antigravity-cli/builtin/skills/*/SKILL.md` | |
| Codex 보강 | `memories_1.sqlite` 존재 여부와 `[features] memories` 값만 | sqlite 를 열지 않는다 |
| 프로젝트 | 개발 루트(깊이 3) 폴더마다 `CLAUDE.md`, `.claude/{CLAUDE.md,settings.json,settings.local.json,rules,agents,commands,skills}`, `AGENTS.md`, `.codex/{config.toml,hooks.json,agents}`, `GEMINI.md`, `.gemini/settings.json`, `.agents/{rules,skills,memory}`, `.mcp.json` | 깊이별 `*/` 접두 패턴으로 SafeFiles 에 등록. git worktree(.git 파일)·의존성 폴더는 건너뜀. 프로젝트 키는 `projects/<이름>`·`hyperez/<이름>`(두 단계) 또는 첫 폴더. `hyperez/` 아래는 회사 범위. Qwen 하네스 저장소는 Qwen 그룹이 따로 다뤄 프로젝트에서 제외 |

보안 불변 조건은 그대로다: 클라이언트는 경로를 주지 않는다(프로젝트 `key`는 스냅샷 조회용이고 `^\w[\w.\-]*(/\w[\w.\-]*)?$` 로 `..`·숨김 폴더·절대경로를 거른다), 화이트리스트 밖과 심볼릭 링크 탈출은 읽지 않고, `.env`·`auth.json`·`.credentials.json` 등은 이름으로 거르며, 설정은 구조 마스킹(env·headers 값 전체)·본문은 scrub_prose, `~/.ttakkari` 보호 영역은 0069의 harness·accounts 메모리 패턴만 읽는다.

## 4. API(추가)

| 경로 | 설명 |
|---|---|
| `GET /api/harness` | 기존 응답에 `groups`(카드: 항목·없음·경고·존재 여부·관심사별 개수, 프로젝트는 개수)와 `labels`가 붙는다 |
| `GET /api/harness/groups/{group}` | 그룹 상세: 관심사 목록, 레이어 스택(순서·근거·적용 여부·개수·관심사별 개수·프로젝트 링크·세션 주입 목록), 메모 |
| `GET /api/harness/projects` | 프로젝트 목록(회사 먼저, 엔진·관심사별 개수), 살펴본 프로젝트 수, 하네스 파일이 없는 프로젝트 이름 |
| `GET /api/harness/projects/detail?key=` | 프로젝트 → 엔진 → 관심사 → 항목 요약(관심사당 120개까지) |
| `GET /api/harness/items` | `hgroup`·`concern`·`layer`·`project`·`pool` 필터 추가. 그룹을 고르고 검색어가 없으면 레이어 순서로 정렬. 각 행에 `hgroup·concern·layer·project·pool·crumb` |
| `GET /api/harness/items/{id}` | `crumb`(그룹 › [프로젝트 › 엔진 ›] 관심사), `layer_label`, `counterparts` 추가 |

대응 항목(`counterparts`)은 엔진마다 한두 개뿐인 지침·설정에서만 항목 링크를 주고(메인 하네스끼리는 대표 지침 파일만, 프로젝트 안에서는 같은 프로젝트·레이어의 다른 엔진), 스킬·MCP 처럼 목록이 긴 관심사는 같은 관심사 링크만 준다.

## 5. 화면

- 첫 화면: 7개 그룹 카드(설정 있음/없음, 항목·없음·경고, 관심사별 개수). 모바일은 카드 → 그룹 화면, 1024px 이상은 왼쪽 그룹 트리(선택한 그룹 아래에 관심사, 프로젝트별은 프로젝트) + 본문.
- 그룹 화면: 레이어 스택(숫자 칩으로 레이어·관심사 필터) → 관심사 칩 → 목록 → 상세. 항목을 열면 데스크톱은 스택을 접고 목록|상세 2단이다. 따까리 그룹은 "관심사별 | 세션별 레이어" 탭이고 0069의 세션별 합성 뷰가 여기로 옮겨 왔다.
- 프로젝트별: 프로젝트 카드(회사 배지, 회사 먼저) → 프로젝트 → 엔진 → 관심사 → 항목.
- 통합 검색: 머리글 검색 아이콘(`find=1`). 결과에 그룹 › 관심사 경로와 그룹 범위 칩. 상세의 경로 칸과 대응 항목은 그 그룹·프로젝트 자리로 이동한다.
- 상태는 모두 URL(`g`·`c`·`l`·`p`·`v`·`item`·`find`·`q`·`fg`·`warn`·`s`·`le`·`pool`).

## 6. 한계·확인 필요

- 레이어 순서 중 사용자 → 프로젝트 → 세션 주입의 엔진 CLI 로드 순서는 코드가 아니라 CLI 동작이라 `external`(확인 필요)로 표시한다. 세션 주입과 Qwen 하네스 내부 순서만 코드로 확인했다.
- Muse 의 규칙·개인 메모리 저장 위치, Antigravity 가 `~/.agents`·GEMINI.md 를 읽는지, Codex 가 `~/.agents/skills` 를 읽는지는 확인하지 못했다.
- 따까리 홈(~/.ttakkari)의 실제 비서 지침·계정 폴더는 이 구현을 검증하는 동안 읽지 않았다.
- 프로젝트 수집은 깊이 3·스캔 4000폴더 상한이고 `.mcp.json`·`.claude/settings*.json`은 구조 마스킹만 한다(형식을 모르는 값 비밀은 설정 값 구조 마스킹으로 처리).
