# ADR 0069. 내 하네스 세팅을 한 곳에서 읽기 전용으로 조회한다

- 상태: 구현(2026-10-23). 사용자 요청에 따른 에이전트의 구현 설계이며 별도 사용자 승인 기록을 뜻하지 않는다.
- 요청: Claude Code·Codex·Antigravity 설정과 따까리가 세션에 주입하는 지침을 한 화면에서 찾고, "이 세션에는 무엇이 어떤 순서로 실리나"를 볼 수 있게 한다. 스마트폰(390px) 우선, 편집 기능 없음.
- 후속: 사용자 피드백으로 화면 구조를 7개 그룹 › 관심사 › 레이어 계층으로 바꿨다(ADR 0070). 이 문서의 "찾기/세션별로 보기" 두 보기는 0070 에서 그룹 구조 안으로 옮겨졌고 수집·보안·API 기반은 그대로다.
- 번호: 0067(agent-global-stop)·0068(creative-engine-adapters)이 다른 브랜치에서 쓰이고 있어 0069로 둔다.

## 사용 흐름

부가 기능 → 하네스(`/harness`). 두 가지 보기가 있다.

| 보기 | 목적 | 핵심 |
|---|---|---|
| 찾기 | 이름·설명·본문 통합 검색과 필터로 항목을 바로 찾는다 | 카테고리 13종 칩, 엔진·적용 범위·활성 여부·경고 필터, 본문 일치 조각 |
| 세션별로 보기 | 접수·기획·작업자·메인(옛) × 엔진을 고르면 실제로 합쳐지는 레이어를 순서대로 본다 | 레이어마다 출처·적용 여부·근거(코드/엔진 동작) 표시 |

선택·검색·필터는 URL에 남긴다. 모바일은 목록 → 상세로 전환하고, 1024px 이상은 2단(목록 | 상세 또는 컨트롤 | 레이어)이다.

## 수집 대상과 출처

| 카테고리 | 읽는 곳 | 비고 |
|---|---|---|
| 지침 파일 | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/development` 아래 프로젝트의 CLAUDE.md·AGENTS.md·GEMINI.md(깊이 3, git worktree 제외) | 없는 파일은 "없음"으로 표시 |
| 설정 | `~/.claude/settings.json`, Codex `config.toml`, Antigravity `settings.json` | 구조 마스킹. env·headers 는 키 이름만 |
| 훅 | Claude `settings.json`·Codex `hooks.json`·Gemini `hooks.json` | 훅 하나가 항목 하나. Codex 는 `hooks.state` 로 활성 판정, 스크립트 존재 확인 |
| 스킬 | `~/.claude/{skills,dev-experts-skills,biz-experts-skills,life-experts-skills,stock-experts}`, `~/.agents/skills`, `~/.codex/skills` | `skillOverrides`·frontmatter 로 활성 판정. 심볼릭 링크 별칭은 원본에 묶는다 |
| 에이전트·규칙·메모리·플러그인 | `agents/`, `commands/`, `rule-cards/`, Codex `agents`·`rules`·`references`, `memory/`, `enabledPlugins`, 따까리 계정별 `projects/*/memory` | 빈 폴더는 "없음" |
| MCP | settings·`~/.claude.json`(mcpServers 키만)·Codex `mcp_servers`, 따까리 MCP 서버와 도구 전부 | 도구는 `mcp_server.server.list_tools()` 와 `tool_policy` 에서 그대로 꺼낸다 |
| 따까리 프롬프트 | `~/.ttakkari/harness/{instructions,desk,lead,worker}.md`, 코드 상수(`SYSTEM_NOTE`, `AGENT_NOTE`, 역할 안내, 라우팅 정책 문구, `capabilities.prompt_note()`), 접수 Qwen 라우터 프롬프트, Qwen 짧은 계약, 고정 기억(DB) | 문구는 실제 주입에 쓰는 상수·함수를 import 한다 |
| 안전 가드 | `security/guard.py` 의 고위험 정규식·차단 명령, `broker/policy.py` 의 민감 이름·폴더, 경로 경계, 엔진별 강제 수단 | 정규식 한 줄이 항목 하나 |
| 라우팅·모델·Qwen | `worker_routing.policy()`, 설정 허용 필드(모델·접수·Qwen·실행), `app_settings.qwen_worker_activation`, `engine_accounts` | 설정은 허용 목록 필드만 |
| 실행 환경 | `capabilities.view_engine()` 엔진별 측정 | 측정 안 됨도 표시 |

항목마다 출처 경로, 적용 범위(글로벌/프로젝트/계정/따까리), 엔진, 수정 시각, 크기(자·줄), 활성 여부와 이유, 실리는 세션 종류를 낸다.

## 실제 주입 지점과 일치 보장

`runs/manager.py` 의 `_system_note` 를 `_system_note_parts`(레이어 id, 본문 목록)로 나누고, 기존 함수는 그 목록을 `\n\n` 으로 잇기만 한다. 세션별 보기는 같은 목록을 쓰므로 주입 코드와 화면이 어긋나지 않는다. `tests/test_harness_view_api.py::test_layers_match_the_real_injected_parts` 가 기획·메인·작업자의 레이어 id·글자 수가 실제 목록과 같음을 검증한다.

엔진별 전달 방식은 어댑터 코드 기준이다: Claude `--append-system-prompt`·`--settings`(guard 훅)·`--setting-sources`, Codex `developer_instructions`, Antigravity `<ttakkari_system_instructions>` 래퍼, Muse `spec.json`, Qwen 로컬은 레이어를 모두 빼고 짧은 계약만, 접수는 로컬 Qwen 라우터 → codex/claude 폴백.

각 레이어에는 `evidence` 가 붙는다. `code` 는 이 저장소 코드로 확인한 것이고, `external` 은 엔진 CLI 가 스스로 읽는 파일(예: Claude Code 의 `~/.claude/CLAUDE.md`, Codex 의 `AGENTS.md`)이라 따까리 코드 밖의 동작이다. 후자는 화면에 "엔진 동작 · 확인 필요"로 구분한다.

## 보안

- 소유자 JWT 전용(`user_access`). 에이전트 세션 토큰은 `aud` 가 달라 401 이다. MCP·agent 라우터는 만들지 않았다. 라우트 인벤토리(`test_route_inventory`)에 `owner-scoped` 로 등록했다.
- 클라이언트는 경로를 주지 않는다. 항목 id 는 `sha1(category|출처 키)[:14]` 이고 서버 스냅샷에서만 조회한다.
- 읽기는 `harness_view/safe.py` 의 `SafeFiles` 만 쓴다. 루트와 상대 패턴이 코드에 고정돼 있고(`*` 는 폴더 경계를 넘지 않는다), 심볼릭 링크는 실제 경로가 허용 루트·패턴 안일 때만 읽는다. `.credentials.json`·`auth.json`·`history.jsonl`·`antigravity-oauth-token`·`.env` 는 패턴에 있어도 이름으로 거른다. `~/.claude.json` 은 `mcpServers` 키만 꺼내고 나머지(계정 정보)는 응답에 싣지 않는다. 따까리 계정 폴더는 `*/projects/*/memory/*.md` 만 읽는다. 안티그래비티 `mcp/` 세션 폴더는 토큰이 있을 수 있어 읽지 않는다.
- 존재 확인(깨진 참조 검사)은 허용 루트 안 경로만 한다. 밖이면 판정을 보류한다.
- 마스킹: 설정 구조는 `mask_config`(env·headers·secrets 값 전체, 비밀스러운 키의 값, `--api-key X` 같은 인자 값, 명령 문자열은 엄격 규칙). 문서 본문은 `oplog.scrub.scrub_prose`(알려진 비밀 값·PEM·URL 자격·토큰 형식·비밀처럼 생긴 값)이다. 기존 `scrub` 는 `token:`·`Basic Auth` 같은 산문까지 가려 문서를 못 읽게 해서 문서용을 따로 두었다. **한계**: 형식을 모르는 임의 문자열 비밀은 본문에서 못 잡는다. 설정 값은 구조로 가리므로 이 한계가 없다.
- 읽기 전용이다. 쓰기·삭제·실행 엔드포인트가 없고 subprocess 를 띄우지 않는다. 운영 DB 는 계정·앱 설정·고정 기억을 SELECT 만 한다.

## API

| 경로 | 설명 |
|---|---|
| `GET /api/harness?refresh=` | 요약(카테고리·엔진·범위 개수, 경고 종류별 개수와 목록, 읽은 시각) |
| `GET /api/harness/items?q=&category=&engine=&scope=&active=&warned=&present=&session=&group=&limit=&cursor=` | 검색·필터. 기본 30, 최대 60. 본문 일치는 `matched_in`·`snippet` |
| `GET /api/harness/items/{id}?offset=&length=` | 상세. 본문 기본 12,000자·최대 40,000자 구간 |
| `GET /api/harness/layers?session=&engine=&pool=` | 세션 종류·엔진·개인/회사별 레이어 순서 목록 |

스냅샷은 메모리에 20초 캐시하고(`refresh=true` 로 즉시 갱신), 파일 읽기는 스레드에서 한다. 파일당 256KB 상한이다.

## 경고(간단히 감지 가능한 것만)

깨진 참조(마크다운 링크·`~/` 경로가 가리키는 파일 없음, 훅 스크립트 없음, `skillOverrides` 가 없는 스킬을 가리킴, 활성 플러그인의 설치 기록 없음), 중복 이름(같은 엔진·카테고리, 심볼릭 별칭 제외, 이름마다 한 줄), 너무 긴 문서(CLAUDE.md 400줄·20,000자, AGENTS.md 32KiB, MEMORY.md 200줄, 따까리 지침 8,000자). 백틱 안 상대 경로는 예시가 많아 오탐이 커서 검사하지 않는다.

## 범위 밖

편집·삭제·동기화, 프로젝트 `.mcp.json` 전수, Antigravity 가 GEMINI.md 를 실제로 읽는지의 확인, 엔진 CLI 내부 로드 순서의 실측, 변경 이력 추적. DB 스키마·세대 프로토콜 변경은 없다. 새 MCP 도구는 없다.
