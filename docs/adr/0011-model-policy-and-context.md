# ADR 0011. 오케스트레이터 모델 정책과 컨텍스트 사용률 기반 세션 교체

- 상태: 채택 (2026-10-13)
- 결정자: 사용자("오케스트레이션하는 애는 opus medium, sol high", "서브에이전트는 sonnet high, luna high", "간단한 작업이면 무조건 luna", "자동 핸드오프는 필요 없다", "50%면 고민, 70%면 갈아끼우기")

## 결정

### 모델
- 메인 오케스트레이터는 엔진마다 한 쌍만 쓴다: Claude `opus`/medium, Codex `gpt-6.1-sol`/high(`orchestrator_fallback_models`). 세션에 다른 모델이 저장돼 있어도 실행 때 이것으로 띄운다.
- 작업 세션은 모델을 안 고르면 claude→`sonnet`/high, codex·auto→`gpt-6-luna`/high. auto 기본을 Luna 로 둔 것은 토큰이 가장 싸서다.
- `opus`·`fable`·`gpt-6.1-sol` 작업 세션은 `escalation_reason` 이 있어야 만든다. 사유는 감사 로그(`orchestrator.escalate`)에 남는다. 하드 차단 대신 사유를 요구한 이유: 중요한 설계·추론은 실제로 필요하고, 무엇이 중요한지는 오케스트레이터가 판단한다.
- 작업 세션이 한도로 다른 엔진 계정으로 넘어가면 같은 급의 상대 모델로 바꾼다(`model_counterparts`: luna↔sonnet, sol↔opus). 이전엔 모델 없이 넘어가 그 엔진의 기본 모델로 떴다.
- 어떤 일에 어느 공급자를 쓸지, 계정 여유를 어떻게 볼지는 코드로 강제하지 않고 ORCHESTRATOR_NOTE 와 `account_usage` 도구로 판단하게 한다.

### 컨텍스트 사용률
- 계정 사용량(5시간·주간 한도)과 다르다. 대화 하나가 모델 컨텍스트 창을 얼마나 채웠는지다.
- Claude: 메인 대화의 마지막 assistant 메시지 usage(input + cache_creation + cache_read + output). 서브에이전트(`parent_tool_use_id`) 요청은 뺀다. 창 크기는 result 의 `modelUsage[model].contextWindow`.
- Codex: `exec --json` 의 `turn.completed` usage 는 턴 합계라 쓸 수 없다. 계정 `CODEX_HOME/sessions/**/rollout-*<thread_id>.jsonl` 의 마지막 `token_count`(`last_token_usage.total_tokens`, `model_context_window`)를 읽는다.
- Run.usage 에 `context_tokens, context_window, context_percent` 로 남긴다.
- 메인은 매 턴 지시 앞에 자기 사용률을 받는다. 시스템 프롬프트가 아니라 지시에 붙이는 이유: 매 턴 바뀌는 값이 시스템 프롬프트에 있으면 프롬프트 캐시가 깨진다. 작업 세션 값은 `list_workers`/`get_worker` 와 `[작업 보고]` 에 붙는다.
- 기준: 50% 이상 검토, 70% 이상 교체(`context_review_percent`, `context_replace_percent`).

### 교체
- 작업 세션: `message_worker(fresh_context=True)`. 메인: `reset_my_context`. 둘 다 `sessions.engine_sessions` 를 `{"_reset_at": 시각}` 으로 비워 다음 Run 이 새 엔진 대화로 시작한다. 그 시각 이전 대화는 인계문(`_handoff_context`)에도 싣지 않는다.
- 실행 중에 요청하면 그 Run 이 끝날 때 적용한다. 바로 비우면 끝나는 Run 이 엔진 대화 id 를 다시 써넣는다.
- 별도 핸드오프 문서는 만들지 않는다. 새 대화는 recall·work-history·작업 세션 기록으로 맥락을 찾는다.

## 버린 것
- 70% 에서 백엔드가 자동으로 대화를 끊기: 작업 중간에 끊기면 진행이 기록되지 않은 채 사라질 수 있다. 판단과 기록 확인은 오케스트레이터가 하고 백엔드는 수단만 준다.
- 작업 세션 승격의 하드 차단: 필요한 승격까지 막는다.

## 한계
- 사용률은 끝난 Run 기준이다. 긴 한 턴 안에서 차오르는 것은 그 턴이 끝나야 보인다.
- Codex rollout 형식은 공개 계약이 아니다. 형식이 바뀌면 값이 비고 "알 수 없음" 으로 보인다(실패하지 않는다).
