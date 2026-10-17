# ADR 0011. 오케스트레이터 모델 정책과 컨텍스트 사용률 기반 세션 교체

> 2026-10-17 후속 결정: 사용자의 개선 요청에 따라 기획·작업 세션의 **다음 Run 실행 직전** 서버가 임계치를 검사한다. 50% 이상은 연속성·새 단계·기록·재로딩 비용 검토, 70% 이상은 원문 발췌 인계를 저장하고 새 엔진 문맥으로 시작한다. 실행 중인 Run을 끊거나 작업을 복제하지 않는다. 일반 handoff·task-handoff를 활용한다는 최신 사용자 요청이 과거 문서 금지보다 우선한다. [상세 계약](../api-contract.md#컨텍스트-수명-관리-2026-10-17).

> 2026-10-16 후속 결정: 신규 auto 작업자의 배정은 `runs/worker_routing.py`의 task_type 정책으로 대체했다. Antigravity 포함, 모델별 quota·fallback·대화 보존·배정 영수증·idle reroute는 [API 계약](../api-contract.md#작업자-task-routing-2026-10-16)을 따른다. 아래의 과거 native-only auto/Luna 기본·디자인 native Claude 고정 설명은 레거시 세션의 이력이며 신규 auto 작업자에 적용하지 않는다. 메인·접수·별도 회의/날씨 경로는 기존 계약을 유지한다.
- 상태: 채택 (2026-10-13)
- 결정자: 사용자("오케스트레이션하는 애는 opus medium, sol high", "서브에이전트는 sonnet high, luna high", "간단한 작업이면 무조건 luna", "자동 핸드오프는 필요 없다", "50%면 고민, 70%면 갈아끼우기")

## 결정

### 모델
- 메인 오케스트레이터는 엔진마다 한 쌍만 쓴다: Claude `opus`/medium, Codex `gpt-6.1-sol`/high(`orchestrator_fallback_models`). 세션에 다른 모델이 저장돼 있어도 실행 때 이것으로 띄운다.
- 작업 세션은 모델을 안 고르면 claude→`sonnet`/high, codex·auto→`gpt-6-luna`/high. auto 기본을 Luna 로 둔 것은 토큰이 가장 싸서다. auto에서는 신선한 계정 상한 여유가 모델의 엔진 선호보다 우선하며 Claude가 선택되면 동일 등급인 `sonnet`/high로 실행한다. 모든 사용량이 미확인일 때는 모델·유휴·기존 우선순위로 고른다(2026-10-15).
- `opus`·`fable`·`gpt-6.1-sol` 작업 세션은 `escalation_reason` 이 있어야 만든다. 사유는 감사 로그(`orchestrator.escalate`)에 남는다. 하드 차단 대신 사유를 요구한 이유: 중요한 설계·추론은 실제로 필요하고, 무엇이 중요한지는 오케스트레이터가 판단한다.
- 작업 세션이 한도로 다른 엔진 계정으로 넘어가면 같은 급의 상대 모델로 바꾼다(`model_counterparts`: luna↔sonnet, sol↔opus). 이전엔 모델 없이 넘어가 그 엔진의 기본 모델로 떴다.
- 접수·기획·작업·일반 대화는 신선한 5시간·주간 사용량을 보고 허용 후보 중 `상한 여유 / (다른 실행 중 Run 수 + 1)`가 큰 계정에 배정한다. 이는 실제 소모 예측이 아닌 동시 배정 분산 기준이다. 고정 엔진 작업도 그 엔진 안에서 비교하며, 접수는 설정된 엔진 순서를 지킨다. 점수가 같으면 초기화 시각이 가까운 계정, 그다음 모델 선호·유휴·기존 우선순위를 따른다. 값이 stale/null 인 후보는 순위 산정에서 기존 폴백을 쓴다(ADR 0006).
- 계정별 `max_used_percent` 는 기본 100이며 설정에서 조정한다. 5시간·주간 중 하나라도 마지막 사용량이 상한 이상이면 그 계정은 값이 낡아도 새 Run 을 배정하지 않는다. 해당 창 초기화·새로운 상한 미만 측정·상한 상향 때 해제된다. 초기화 시각이 없는 값은 90초 후 다시 판단한다.
- 디자인·시각 작업은 Claude 를 우선한다. Claude 에 작업 여유가 없으면 GPT 로 넘기지 않고 관련 PMS 프로젝트 WBS/백로그에 기록한 뒤 Claude 한도 초기화 후 진행한다고 사용자에게 보고한다.
- 보호 계정 기본 상한: `내 클로드` 95%, `Daddy codex` 95%(5시간·주간). 다른 계정 기본은 100%이며 설정에서 조정할 수 있다.

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
- 과거 결정(2026-10-13)은 별도 문서 없이 recall·work-history·작업 세션 기록을 사용하는 것이었다. **2026-10-17 대체:** 단기 교대는 일반 handoff, 장기 작업은 task-handoff를 활용하고 기존 기록을 링크한다. 자동 교대는 원문 발췌를 `Run.usage.context_handoff`에 저장한다.

## 버린 것
- 70%에서 실행 중인 작업을 끊는 방식은 계속 쓰지 않는다. 2026-10-17부터는 직렬 실행 경계에서 다음 Run의 엔진 문맥만 자동 교대한다.
- 작업 세션 승격의 하드 차단: 필요한 승격까지 막는다.

## 한계
- 사용률은 끝난 Run 기준이다. 긴 한 턴 안에서 차오르는 것은 그 턴이 끝나야 보인다.
- Codex rollout 형식은 공개 계약이 아니다. 형식이 바뀌면 값이 비고 "알 수 없음" 으로 보인다(실패하지 않는다).
