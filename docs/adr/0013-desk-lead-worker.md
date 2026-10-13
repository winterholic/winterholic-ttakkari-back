# ADR 0013. 접수 → 기획 → 작업 3단 구조, 역할별 지침, 역할별 실행 자리

- 상태: 채택(첫 구현) (2026-10-13)
- 결정자: 사용자("메인 오케스트레이터가 계속 바빠서 바로 질문해도 대답이 안 온다", "개수 제한이 이상하다", "최초 오케스트레이터 → 간단하면 바로 대답, 복잡하면 sol·opus → sonnet·luna", "바쁘면 다른 세션 띄워서", "luna 먼저, haiku", "접수는 전역지침을 모르는 게 낫다", "기획·작업은 전역지침·전역하네스 다 걸려야")

## 문제(2026-10-13 운영 DB 확인)
- 메인 오케스트레이터와 작업 세션이 동시 실행 자리 3개(`max_concurrent_runs`)를 같이 썼다. 작업 세션 3개가 돌면 메인은 시작도 못 했다(메인 queued 4 / 작업 running 3).
- 메인 세션 하나가 사용자 메시지와 작업 보고를 한 줄(세션 대기열)로 처리했다. 보고를 처리하거나 직접 일하는 동안 질문이 그 뒤에 섰다.

## 결정

### 세션 종류
| kind | 무엇 | 사이드바 |
|---|---|---|
| `desk` | 접수. 사용자가 대화하는 곳. 새 대화의 기본 | 보임 |
| `orchestrator` + `parent_id`=접수 | 기획 세션. 접수가 넘긴 일을 맡아 작업 세션을 부린다 | 접수 화면 오른쪽 |
| `orchestrator`(부모 없음) | 옛 메인 오케스트레이터. 그대로 동작한다 | 보임 |
| `worker` | 작업 세션. 부모는 기획(또는 옛 메인) | 기획 화면 오른쪽 |

기획을 새 종류로 만들지 않고 `orchestrator` 를 그대로 썼다. 모델 정책(opus medium / sol high), MCP 작업 세션 도구, 한도 전환·대기, 컨텍스트 교체가 이미 이 종류에 붙어 있다. 옛 대화도 깨지지 않는다.

### 접수(desk)
- 메시지마다 따로 돈다. 세션 대기열에 줄 서지 않고, 엔진 대화를 이어 쓰지 않는다. 앞 메시지가 처리 중이어도 다음 메시지가 바로 시작한다(자리 `max_concurrent_desk`, 기본 6).
- 한 번에 고른다: `{"action":"answer","reply":…}` 또는 `{"action":"delegate","reply":…,"lead_id":…|null,"title":…}`. 도구는 없다. 백엔드가 작업판(기획 세션 상태·작업 세션 수·최근 결과)과 최근 대화 6턴을 프롬프트에 넣는다.
  - 근거(2026-10-13 실측, 30문항 분류): luna(effort low) 30/30·첫 답 중앙값 4.8초, haiku 29/30·7.4초, 로컬 kanana-2-3b 8bit 23/30·0.06초, kanana-1.5-8b 4bit 23/30(작업 요청 4건 놓침). 작은 모델은 여러 번 오가는 도구 호출에 약하다(Kanana-2 카드의 BFCL-v3 Multi-Turn 17%·5.5%).
- 엔진 순서 `desk_engine_order`(기본 codex → claude), 모델 `desk_models`(luna low / haiku). 엔진 안에서는 계정 우선순위·켜기·회사 범위·한도 쉬는 중을 그대로 따른다. 한도면 그 계정을 쉬게 하고(`cool_down`), 시간 초과(`desk_timeout_seconds`, 25초)·출력 형식 오류면 다음 계정으로. 다 실패하면 기획으로 넘긴다. 접수 때문에 메시지가 멈추지 않게 한다.
- 전역 하네스를 받지 않는다.
  - Codex: `--disable hooks`(전역 Stop hook 이 기록을 강요해 추가 턴 3번, 입력 84.5k→20.4k, 20.9초→4.3~4.9초 실측), `--ephemeral`, `-s read-only`, 사용자 config.toml 의 MCP 서버 끄기, 빈 작업 폴더(`~/.ttakkari/desk`), `developer_instructions`=접수 지침.
  - Claude: `--settings {"disableAllHooks":true}`, `--setting-sources local`(전역 CLAUDE.md·스킬 제외), `--tools ""`, `--strict-mcp-config`, `--no-session-persistence`, `--system-prompt`=접수 지침. `disableAllHooks` 는 따까리 보험 hook 까지 끄므로 도구 없는 접수에만 쓴다.
- 넘길 때: 작업판의 `lead_id` 가 이 접수의 기획 세션이면 그 세션 대기열에 넣고, 아니면 새 기획 세션을 만든다. 기획에는 `[접수에서 넘긴 사용자 요청]` + 원문 + 최근 대화 4턴, 첨부(`context_artifact_ids`)를 넘긴다(Run `origin=desk`).

### 기획(lead)
- 지침: `LEAD_NOTE`(사용자와 직접 대화하지 않음, 매 턴 마지막 답이 사용자에게 보고로 간다) + 기존 오케스트레이터 본문. 옛 메인은 `ORCHESTRATOR_NOTE` 그대로.
- 기획 Run 이 끝날 때마다 마지막 답을 접수 대화에 보고 Run 으로 붙인다(`origin=report`, 바로 succeeded, 모델 호출 없음). 취소이고 답이 없으면 붙이지 않는다.
- 작업 세션 보고는 기획 세션으로 간다(부모가 기획이므로 기존 `_enqueue_report` 그대로). 그래서 사용자 질문이 작업 보고 뒤에 줄 서지 않는다.

### 역할별 지침(`~/.ttakkari/harness`)
| 파일 | 붙는 곳 |
|---|---|
| `instructions.md`(공통) | 일반 대화·기획·작업 |
| `desk.md`(접수) | 접수만. 없으면 코드 기본값(`runs/instructions.DESK_DEFAULT`) |
| `lead.md`(기획) | 기획(옛 메인 포함) |
| `worker.md`(작업) | 작업 세션 |
- API: `GET/PUT/DELETE /api/assistant/instructions?role=common|desk|lead|worker`(기본 common). DELETE 는 파일을 지워 기본값으로 되돌린다.
- 파일 하나 최대 `instructions_max_chars`(8000자). 전에는 고정 기억 한도(`context_brief_chars`, 3000자)를 같이 썼고, 지침이 2988자라 조금만 늘어도 뒤쪽(work-history 절)이 잘릴 상태였다.
- 기존 `instructions.md` 의 "1. 오케스트레이션 방침"(모델·계정·컨텍스트)은 `lead.md` 로 옮겼다(백업 `instructions.md.bak-split-20261013`).

### 실행 자리
`max_concurrent_runs`(3, 전부 공유) → 역할별: 접수 `max_concurrent_desk` 6, 기획 `max_concurrent_leads` 4, 작업·일반 대화 `max_concurrent_workers` 6. 기획 한 오케스트레이터당 동시 작업 세션 상한(20)은 그대로.

## 버린 것
- 로컬 LLM 접수(kanana): 빠르지만 분류 정확도 77%, 작업 요청을 놓치는 쪽으로 틀렸다. 메모리도 3.5~4.7GB 를 계속 차지한다.
- 접수를 상주 프로세스로: 첫 구현은 CLI 1회 실행으로 했다. 지연의 대부분이 기동 시간이라 체감이 부족하면 다음 후보다.
- 접수에 도구 주기(메모·일정 조회 등): 작은 모델의 다단계 도구 호출이 불안정하다. 조회도 일단 기획으로 넘긴다.
- 계정 수 기반 동적 자리: 첫 구현은 설정값으로 했다. 맥 자원(빌드·브라우저)이 상한을 정하는 경우가 많아 수치를 운영하며 정한다.

## 알려진 한계
- 접수 지연은 엔진 기동이 대부분이다(실측 luna 6~8.5초, haiku 4~7초, 계정 폴더·지침 길이에 따라 다름).
- Codex 전역 `~/.codex/AGENTS.md`·계정 폴더의 AGENTS.md 가 접수 호출에 들어가는지는 확인하지 못했다(확인 필요). 입력이 hook 을 끈 뒤에도 약 19k 토큰이었다.
- 접수 둘이 동시에 같은 새 일을 넘기면 기획 세션이 둘 생길 수 있다(작업판은 넘기는 순간의 상태다).
- 기획 보고는 기획의 매 턴마다 붙는다. 작업 보고를 정리하는 턴도 보고로 올라온다.
