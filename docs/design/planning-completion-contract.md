# 기획 실행·완료 계약

2026-10-25 사용자 전면 감사 지시를 반영한 현행 코드 설명이다. 제품 전체 성능 향상을 측정한 보고서는 아니다.

## 실제 실행 경로와 책임

접수(`runs/desk.py`, `desk_qwen.py`)는 작업판·최근 대화로 응답/위임을 결정하고 원문과 첨부를 lead Run(origin=desk)에 넣는다. `DESK_ROUTING_CONTRACT`의 같은 프로젝트/같은 작업 검증은 기존 구현을 유지한다. 기획은 공통 전략과 전체 목표를 소유하며 기존 worker API로 실행을 위임한다. Worker는 배정된 범위의 코드·검증을 수행하고 `_enqueue_report`가 부모 기획에 보고 Run을 연결한다. `_post_lead_report`는 기획의 마지막 답을 접수에 복사하며 추가 LLM을 호출하지 않는다.

`Run.status=succeeded`는 프로세스/턴의 정상 종료다. 개별 테스트·Gate·단계·전체 목표는 같은 개념이 아니다. 기존 Run 상태 enum과 PMS task 상태는 그대로 둔다.

## 하네스 로딩과 모델별 판단

`manager._system_note_parts`가 기본 안내 → 도구 안내 → 실행 능력 → 역할 소개 → 사용자 공통/역할 지침 → 핀 기억 → `instructions.PLANNING_CONTRACT` → 라우팅 정책을 조립한다. 새 기획 계약은 사용자 역할 파일의 글자수 제한 바깥에서 항상 주입하며, 접수/worker에는 기획용 긴 본문을 넣지 않는다. `runtime.ORCHESTRATOR_BODY`의 턴 종료 우선 문구는 보고는 짧게 하되 목표 소유권과 후속 실행을 유지하도록 고쳤다.

엔진마다 기획 지침의 별도 사본을 만들지 않는다. Claude는 합본을 `--append-system-prompt`, Codex는 `developer_instructions`, Antigravity는 system instructions wrapper, Muse는 payload system_note로 받는다. 파일 자동 발견·hook·기본 시스템 프롬프트는 각 엔진의 독립성을 유지한다. 공통 누락이 관측 원인인 만큼 Codex 전용 보정/Claude 필수 심사/엔진 고정은 추가하지 않았다.

변하는 목표 상태는 `_compose_prompt`에서 사용자 입력 앞에 붙인다. 시스템 본문에 매번 진척 JSON을 넣어 캐시를 깨지 않으며, 엔진 교체·컨텍스트 reset·Qwen 비상 경로에도 원문 입력으로 전달된다. Qwen은 기존 MCP 없는 축소 하네스를 유지하므로 자체 checkpoint 도구 실행은 지원하지 않는다. 전체 클라우드와 같은 기획 능력을 검증했다고 간주하지 않는다.

## 전략과 분해

기존 자료·현재 코드에서 목표와의 차이, 정적으로 드러나는 누락과 공통 원인을 먼저 찾고 일괄/점진/병렬 중 비용·위험에 맞게 선택한다. 공통 원인은 하나의 수정 묶음, 실제로 독립인 항목은 병렬 후보로 둔다. 비싼 준비/전체 CI를 매 미세 수정에 반복하지 않고 좁은 재현→변경 묶음 검증→대표 통합/최종 회귀로 구성한다. 분석을 선행 조건으로 무한 반복하지 않는다.

새 사실로 가정·비용·공통 원인·누락 요구가 바뀔 때만 replan_reason을 기록하고 해당 전략을 수정한다. 작업 개수·계획서 길이는 목표 지표가 아니다. 기존 요구 ID/text·원래 목표·완료 증거를 보존한다.

## 영속 상태와 PMS 연계

`orchestrator/work.py`의 `WorkState`와 기존 `AppSetting`의 `lead-work:<session UUID>`를 쓴다. 새 테이블·마이그레이션·WBS 엔진은 없다. `manage_work`(기획 전용, 읽기/쓰기이므로 readOnlyHint 아님) → `/api/orchestrator/work`는 세션 토큰으로 자기 목표만 다룬다. Worker/일반 대화는 403, 회사 세션의 개인 PMS 연결은 기존 범위 검사로 404다.

- `goal`, 원문 요청 Run 참조, `strategy`, `requirements[{id,text,status,evidence}]`, `next_action`.
- `completed_work`, `validation`, `changed_files`, `decisions`, `replan_reason`, `blocker`, 선택적 `pms_project_id`.
- 세션 행 잠금 + revision 비교로 오래된 checkpoint 덮어쓰기를 409로 거절한다.
- 요구 삭제/본문 변경·목표 변경·연결 PMS 제거·완료 증거 소실은 거절한다. 전략 변경은 사유가 필요하다. 새로운 사용자 목표는 새 기획으로 분리하며, 기존 요구 정정은 기존 증거를 남긴 보완 기록으로 관리한다.
- 구조화 상태는 30,000자, 요구 최대 100개 등 명시적 상한으로 제한한다. 상세 증거는 저장소 경로를 연결한다.
- complete는 모든 요구 done+증거, 실행 중인 직속 worker 없음, 연결 PMS 말단 전부 done을 요구한다. 부모 rollup·가중 진행률은 재사용하며 100%를 목표 완료 증거로 해석하지 않는다.
- PMS 프로젝트 status=done 변경도 미완료 말단이 있으면 422로 거절한다. 기존 task enum/트리/데이터는 마이그레이션하지 않는다.

증거 문자열은 에이전트의 출처를 가진 주장이다. 코드가 외부 CI나 모든 파일 내용을 독립 재실행/인증하는 것은 아니다. 최종 보고 전에 실제 명령·출력·revision을 대조해야 한다.

## 턴 종료·후속·복구

`RunManager._finish`는 `work.after_run`을 같은 트랜잭션에서 호출한다. 목표 active이고 기획 큐·직속 worker·자기 세션의 명시적 계약 send_message 예약(`payload.work_continuation is True`)에 다음 실행 소유자가 없으면 기존 Run 큐에 origin=orchestrator 후속을 만든다. 기존 `_execute` finally의 dispatch와 generation wake/owner·fence를 그대로 사용한다. 별도 폴링/새 에이전트/매 턴 LLM 심사는 없다.

|관측|계약 상태/행동|
|---|---|
|Gate A만 통과|pending 요구 유지, complete 거절, next_action 실행/큐 연결|
|작업자/새 사용자 Run이 대기·실행 중|중복 후속 생성 안 함; 작업자 결과 대기로 표시|
|상태·증거의 진전 없이 자동 후속 2회|blocked와 이유/event 기록; 무한 자동 반복 차단|
|엔진 Run failed/cancelled|예약 소유권보다 먼저 blocked/paused 전이, 정확한 Run/engine/error를 blocker에 보존|
|서버 종료 interrupted|새 Run을 중복 생성하지 않고 기존 interrupted recovery가 같은 Run을 재개|
|모델/컨텍스트 교대|AppSetting 유지, 입력에 목표·전략·증거·미완료·다음 동작 복원|
|사용자 명시 중단|paused+사용자 요청 근거; paused/blocked/complete는 자동 후속 안 함|
|모든 조건 충족|complete; 후속 없음|

예약 프롬프트·설명·일반 origin은 소유권 근거로 쓰지 않는다. 명시적 표식이 없는 개인 예약은 취소하지 않으며, 목표가 paused여도 실행한다. 계약 예약 실행은 checkpoint/after_run과 같은 세션 행 잠금을 **상태 읽기 전부터 Run 등록 커밋까지** 유지한다. 계약 자체가 없거나 active가 아니면 건너뛴다. 예약으로 등록하는 Run에도 schedule_id/revision 계약 표식을 남긴다. 오래된 leased 스냅숏을 가진 실행기가 취소 후 목표 재개를 만나도 예약 자체의 현재 저장 상태가 pending/leased인지를 같은 세션 잠금 안에서 재확인해, 취소된 예약은 부활시키지 않는다. 취소와 non-active checkpoint는 이미 queued인 계약 후속도 정리한다. 직접 만든 후속은 usage 표식, 구버전 예약 Run은 request_key→해당 세션의 명시적 계약 예약 참조로 식별하며 개인/사용자/보고 Run은 보존한다. 취소된 queued Run이 claim과 경합하지 않도록 세대 claim도 같은 Run 행을 잠가 상태를 다시 읽는다. 이미 lease가 있으면 상태·이벤트를 직접 바꾸지 않고 소유 세대에 cancel 명령을 남긴다. 네이티브 시작 경로는 최초 진입과 계정 선택 후 상태를 재확인해 cancelled를 running으로 덮어쓰지 않는다. 현재 자동 후속 생성자는 예약 대신 Run을 직접 만들므로 표식 없는 기존 예약을 계약 소유로 소급 분류하지 않는다.

`active` 중 실제 대기 여부는 기존 queued/running Run 소유자로 구분한다. 외부 권한/인증 대기는 blocked의 이유와 다음 해소 동작으로 기록한다. paused를 단순 대기와 혼동하지 않는다.

접수 작업판은 목표 상태·미완료 수를 보고, 목표 계약이 없던 과거 세션의 succeeded는 '턴 종료(목표 완료 미확정)'로 표시한다. 실제 사용자 보고에도 미완료 상태를 붙인다. 목표 complete가 아닌 세션은 idle 자동 보관에서 제외한다. 기존 데이터에 목표 계약을 임의 생성하거나 과거 완료 판정을 바꾸지 않는다.

## 검증 범위

`tests/test_planning_contract.py`: A–E 중 결정론적 완료/후속/복원/재계획, 범위 경계, 증거 보존, stale revision, live worker 중복, 실제 manager 종료 연결, idle 보관, 사용자 보고/작업판, PMS 완료 차단.

합성 전략 비교는 `reports/planning-audit/model-evaluation.json`의 동일 입력 Claude Sonnet/Codex Luna 1회씩이다. 실제 SDK 코드를 이해하는 능력, 운영 완료율, 전후 지연·토큰 개선을 측정한 실험이 아니다.

## 롤아웃과 남은 한계

기존 AppSetting·Run 형식을 사용하며 새 backend부터 적용된다. 구세대 Run은 구세대 지침/종료 코드를 계속 사용하므로 이번 배포로 이미 실행 중인 엔진의 하네스가 바뀌었다고 주장하지 않는다. 새로운/재개 Run에서 계약을 checkpoint해야 자동 후속이 활성화된다. 과거 미완료 목표의 일괄 자동 재시작은 하지 않는다.

등록 요구의 완전성과 증거 진실성을 구조 검증만으로 보증할 수 없다. 실제 사용 후 누락률·후속 재지시·반복 검증·완료까지 시간/토큰을 같은 유형의 작업에 대해 측정해야 한다. 자동 후속 예산은 노골적인 무진전 반복을 제한하지만 새 증거가 계속 생기는 장기 작업의 총예산 제한은 기존 Run/account 제어를 따른다.
