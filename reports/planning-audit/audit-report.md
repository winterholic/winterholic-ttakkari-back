# 따까리 기획 하네스 전면 감사·개선

2026-10-25 KST. 기준은 사용자 원본 요청의 최종 목표와 완료 조건이다. 코드·문서·대화 원문을 대조하고 실제 종료 경로를 수정했다. 이 보고서는 운영 완료율이 이미 개선됐다고 주장하지 않는다.

## 1. 주요 발견과 변경 전후

|발견|분류·영향|변경 전|이번 변경|
|---|---|---|---|
|F1. 짧은 턴 종료 우선|하네스·P1|decide/delegate/짧게 보고가 종료 기준처럼 작용할 여지|보고는 짧게, 목표 소유·후속은 유지. 실패 판정 뒤 교정도 같은 턴 시작|
|F2. 목표 계약과 후속 큐 부재|상태·종료·P1|`_finish`는 Run 성공과 보고를 처리하며 미완료 요구 대조/자기 후속 없음|기존 AppSetting에 목표 계약, 종료 시 active+후속 소유자 없음이면 기존 Run 큐에 연결|
|F3. succeeded를 작업판에서 완료로 표시|표현·종료·P1|마지막 Run succeeded → 완료|목표 상태/미완료 수 표시. 계약 없는 과거 Run도 턴 종료와 목표 완료 구분|
|F4. PMS 프로젝트 완료가 미완료 leaf와 독립|완료 검증·P1|0%/todo 말단이 있어도 프로젝트 done PATCH 200 재현|미완료 말단 있으면 422. 목표 complete도 연결 PMS/worker/요구 증거 검사|
|F5. 엔진 교대의 최근 발췌 한계|전달·복구·P2|최근 8턴, 필드별 1500자 handoff. 별도 context rotation snapshot은 있지만 전체 전략/미완료 계약 없음|모델 대화와 별개 목표·전략·요구·증거·파일·다음 동작 보존, 매 입력에 복원|
|F6. 미완료도 유휴 시간 뒤 자동 보관|발견·복구·P2|실행 Run 없음+시간 경과로 보관|계약이 있고 complete가 아니면 보관 제외|
|F7. 작은 부분 수정/큰 CI 반복|기획 전략·P2|AIP 기록에서 기존 전체 갭보다 개별 검사 통과 반복 지적|정적 갭/공통 원인→전략 선택→위험별 작업 묶음/검증. 필요할 때만 재계획|

P1/P2는 이번 업무의 수정 우선도다. 실제 데이터 손실이나 전체 시스템 장애가 발생했다고 해석하지 않는다. F1·F5가 특정 역사적 중단을 직접 일으켰다는 인과는 코드 기반 추론이다. F2~F4는 현재 코드를 실행하는 회귀로 재현했다.

## 2. 과거 사례의 근거 연결

### A. V2 Gate A 이후 중단

원본: session `ed811f7a-e003-4b0f-b6f6-66bdc3aa9f9a`, 2026-10-24 12:16 UTC. 사용자는 감사·설계만이 아니라 공용 기반·독립 모듈 전환·운영 검증까지 전면 구현을 지시했다. PMS는 프로젝트 `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286`, P0~P6와 P3.1/P3.2를 유지하고 있었다.

|시점(UTC, 10/24)|관측|
|---|---|
|13:54~14:12|검증 실패를 기록한 뒤 “새 교정 실행·배포는 시작하지 않았다”는 기획 응답 반복|
|14:25:05|사용자 “끝까지 다 한거야?” → 기획 응답은 작업 멈춤, A/B/C 모두 failed, 실행 중 0개를 보고|
|14:25:59|사용자가 결함 보완 후 원래 설계대로 배포하라고 재지시|
|15:08|사용자가 다시 중단 지적, 다른 모델 인계 요청|
|15:29 전후|Gate A·94건·Ruff 범위 한정 보고. 원문은 전체 V2 미완을 명시함|
|15:30:12|사용자 “전체가 미완이면 왜 멈춘거야 도대체?” → 그제야 FND-01/B·C 후속 배정|
|15:34:51|별도 배포 예약 be842911…은 release pytest 단계에서 report 집계 2!=1로 실패|

따라서 “에이전트가 전체 완성이라고 거짓말한 사건”과 “전체 미완이라고 정확히 말하고도 후속 없이 멈춘 사건”을 구분한다. 이 사례의 핵심은 후자다. Gate가 실제 코드상의 전체 종료 조건으로 설정된 흔적은 없었다. 기획이 Gate 응답에서 턴을 끝냈고 backend가 목표별 남은 일과 다음 실행을 연결하지 않았다는 것이 관측/코드상 설명이다. 모델 내부 판단의 인과까지 증명한 것은 아니다.

이번 PMS readback(과거 시점 스냅샷 아님): P3.1만 done, P3.2 todo, P0/P1 doing, P2/P4/P5/P6 todo. parent P3은 doing/50%. 전체 leaf 8 중 done 1이다. 현재 원장 자체는 전체 완료라고 잘못 집계하지 않았다. 상태를 실행에 연결하지 않은 것이 주된 결함이었다. 별도 V2 진행 작업의 상태를 이번 감사가 덮어쓰지는 않았다.

원본·응답 발췌: `source-turns.json`; 현재 PMS: `pms-readback.json`; 저장소 기준: `docs/design/platform-v2/00-request-and-status.md`, `verified-corrections-20261024.md`.

### B. AIP 전체 포팅 전략의 부재

원본: session `5751bc1b-1866-4197-aad8-48582033e258`, 10/24 11:24~11:31 UTC. 사용자는 부분 수정/전체 CI 반복을 지적했고, 먼저 전체 소스를 AIP와 대조한 뒤 공통 갭을 묶는 방식을 제시했다. 이전 응답에는 비밀번호 검사에서 응답 수집 실패 시 가입·튜토리얼을 실행하지 못하는 직렬 검증과 기존 PRODUCT-GAPS 목록을 충분히 활용하지 않은 흐름이 기록돼 있다.

이번에 `.aip-harness/WORKFLOW.md`, `RESOURCES-AND-MAIN.md`, README를 직접 읽었다. 전체 소스/갭 대조, 공통 원인 묶음, 독립 여정 실패 수집, 기존 자원·증거 재사용은 범용화했다. AIP 앱별 순서, 무조건 전체 포팅 우선, SSD 삭제 규칙/출시 조건을 공통 하네스에 복사하지 않았다. 이번 범위에서 AIP 코드는 변경하지 않았다.

과거 응답의 10회·237분·실패 8회는 당시 에이전트의 측정 보고다. 이번 실행에서 원 GitHub CI 전체 로그를 재조회하지 않았으므로 신규 실측으로 재인용하지 않는다.

### C. 다른 반복 사례

- Studio session `b746a634-3c32-45b5-9a8f-50eaf1efe539`: 10/24 00:13 UTC에 기존 검사 `running/65%/종료 코드 없음`을 이유로 후속 구현을 하지 못했다고 보고, 00:45에 사용자가 원래 전체 검수·개선 범위를 다시 지적했다. 이후 단계별 구현과 영속 예약으로 이어졌다. 상태 복구/범위 축소 문제를 뒷받침하나, 원 검사가 실제로 죽었는지의 프로세스 증거는 이번에 확보하지 않았다.
- 접수 오분류: AIP 대화에 따까리 배포 분석 요청이 두 번 이어 붙었다는 사용자 원문과, 이후 `c8f1015`에서 같은 프로젝트/같은 작업 재사용 계약을 추가한 기록을 확인했다. 현재 origin 소스의 `DESK_ROUTING_CONTRACT`와 backend 검사에 이미 반영돼 있어 중복 재설계하지 않았다.
- be842911 배포 실패는 운영 배포 인프라의 별도 검증 실패다. 기획 판단 실패와 구분한다. 독립 교정 작업이 진행 중이므로 이번 코드에서 같은 report 집계 결함을 중복 수정하지 않는다.

## 3. 실제 아키텍처와 하네스 감사

자세한 현행 경로·필드·상태·한계는 `docs/design/planning-completion-contract.md`에 있다.

접수 → lead Run → worker Run → report Run → lead 판단 → 접수 보고 구조는 유지한다. 같은 프로젝트/관심사 라우팅 검사, 역할별 concurrency, generation lease/fence, 기존 resume_interrupted/queued와 context rotation을 재사용한다. PMS의 parent rollup과 선후행 구조도 유지한다.

공통 지침은 source `instructions.PLANNING_CONTRACT` 한 곳에서 유지하고 기존 합본 조립 경로로 전달한다. 별도 하네스 로더·모델별 중복 사본을 만들지 않는다. 보호된 사용자 harness 파일은 이미 현재 대화에 주입된 내용과 코드의 파일명/순서/제한으로 대조했으며 직접 읽거나 수정하지 않았다. 원문 파일과 주입 텍스트의 바이트별 동일성은 미검증이다.

모델별 전달 방식은 Claude append-system-prompt, Codex developer_instructions, Antigravity wrapper, Muse system_note다. 동일 공통 계약을 받으므로 Codex에만 별도 행동 규칙을 추가할 근거가 없었다. 로컬 Qwen은 비상 축소 모드로 goal 입력은 받지만 MCP checkpoint는 없다는 한계를 유지한다.

## 4. 실제 변경 파일

- `runs/instructions.py`: 공통 기획 실행 계약. `orchestrator/runtime.py`: 짧은 턴/후속 방치 유도 문구 교정.
- `orchestrator/work.py`: typed 목표 상태, revision/보존/완료 검사, 제한된 자동 후속.
- `api/orchestrator.py`, `orchestrator/mcp_server.py`, `tool_policy.py`, `api/assistant.py`: manage_work 세션 권한·MCP·카탈로그 연결.
- `runs/manager.py`: 종료 hook, prompt 복원, 접수 보고에 목표 상태 표시.
- `runs/desk.py`, `lead_archive.py`: 전체 목표와 턴 성공 구분, 미완료 보관 방지.
- `api/pms.py`: 미완료 leaf가 있는 프로젝트 done 거절.
- `harness_view/layers.py`, `ttakkari_side.py`: 실제 공통 기획 계약을 하네스 화면에 노출.
- `tests/test_planning_contract.py`, 기존 route/tool 인벤토리 2개: 회귀·권한·도구 분류.
- ADR 0012/0013, 기획 완료 계약 설명, 이번 work-history 및 재현 증거.

## 5. Claude·Codex 비교

합성 입력 한 건을 동일하게 전달했다. 실제 runtime receipt는 Claude/sonnet high, Codex/gpt-6-luna high였다. 원 출력과 입력은 `model-evaluation.json`에 보존한다.

|객관 조건|Claude|Codex|
|---|---|---|
|결함 전체·통합·배포 목표 보존|충족|충족|
|SDK 3개 공통 직렬화 원인 묶음|충족|충족|
|독립 UI 작업 구분|병렬 명시|독립 분리 명시|
|좁은 검사/묶음 통합/최종 CI 구분|충족|충족|
|Gate A 성공과 전체 완료 구분|충족|충족|
|모델 교체 전 증거·변경 파일 재사용|충족|충족|
|전략 변경 시 원래 요구·증거 보존|충족|충족|
|다음 동작/추가 사용자 지시 불필요|충족|충족|

이는 지침을 준수하는 합성 계획 응답의 한정 검사다. 공통 원인 정보를 입력에 제공했고 실제 앱 전체 코드를 새로 분석하게 하지 않았다. 개선 전 대조군/반복 표본/실제 제품 완료율/토큰·시간 개선은 측정하지 않았다. 모델 일반 우열이나 하네스 변경의 인과 효과를 주장할 수 없다.

## 6. 회귀·운영 검증

상세 실행 결과는 `verification.json`과 원 로그(`planning-contract.log`, `automatic-dispatch.log`, `full-regression.log`)를 기준으로 기록한다.

- 최초 RED: 목표 완료 API 부재로 Gate A 완료 요청 404 실패. 추가 RED: PMS 0%인데 done PATCH 200 허용, 작업판 succeeded→완료 오표시, live worker 있어도 목표 complete 허용, checkpoint 증거/PMS 연결 소실, 엔진 실패 후 active 방치가 각각 재현됨.
- ⚠️ 이전 F-1 완료 판정과 '미해결 회귀 0건 확정'은 철회한다. 독립 검증 `5f0a668c`가 개인 예약 문구 오분류를 재현했고, 상태 확인→Run 등록 사이 잠금 누락을 지적했다. 원 구현 작업자 `826d2939`의 verification_status=failed를 기록했다.
- 재수정 전 RED: 개인 예약 분류·취소 후 개인 예약 보존·실제 다중 DB 취소 경합 3개 테스트가 실패했다(`cancel-red.log`).
- 재수정: 정확한 boolean `work_continuation=True`만 계약 예약으로 인정. 프롬프트/설명/origin 추정 제거. 개인 예약은 성공한 턴의 후속 실행 소유자로도 계산하지 않는다. 계약 예약은 상태 읽기 전 세션 행 잠금을 얻고 Run 등록 커밋까지 유지한다.
- 독립 전용 DB에서 관련 계약·예약·기획·route 검사와 과거 실패 12건을 실행: **105 passed in 31.85s** (`cancel-dedicated-green.log`). 다중 DB 테스트는 `pg_blocking_pids`로 스케줄러가 취소 트랜잭션에 막혔음을 확인한 뒤 취소를 커밋하며, 결과 skipped와 새 활성 Run 부재를 검증한다.
- 원 전체 suite 로그는 **13 failed, 4570 passed, 21 skipped, 2 xfailed** 기록 그대로다. 1건은 manage_work 분류 누락(앞선 수정/51건 통과 기록), 3건은 Muse, 9건은 migration이다. 이 합계만으로 기준선 동일 실패나 전체 통과를 보증하지 않는다.
- 기준선 `917ea4b`를 git archive로 별도 경로에 복원하고 동일 interpreter, 전용 테스트 DB, PG 접속 환경, 명시적 release revision으로 과거 Muse 3+마이그레이션 9건을 실행: **12 passed in 14.25s** (`baseline-917ea4b-final.log`). 현재 코드의 같은 12건도 위 105건에 포함되어 통과했다. '기준선도 실패했다'는 이전 실행 주장은 근거 부족으로 철회한다. 최초 전체 검사와 환경이 달라 정확한 당시 실패 원인의 확정은 하지 않는다.
- 기준선 비교 준비 중 공유 fixture DB reset deadlock, 임시 클러스터 initdb exit 1, archive에서 git revision 부재를 관측했다. 전용 DB 생성/제거와 명시적 revision으로 비교를 구성했다. Codex 실행 환경의 관측이며 운영 장애를 뜻하지 않는다.
- ⚠️ 64e90c9도 독립 재검증 FAIL: scheduler-first로 이미 커밋된 자동 Run이 목표 paused 후 queued로 남았다. 105건 통과는 그 반례를 포함하지 않았다.
- 추가 RED 8건: 이미 등록된 예약·직접/구버전 계약 후속·paused checkpoint 정리, cancelled 네이티브 재진입, leased queued 취소 명령 부재, generation claim 경합, 프로필 선택 중 취소 덮어쓰기, 취소 뒤 목표 재개를 만난 오래된 예약 스냅숏 부활. `scheduler-first-{red,owned-red,claim-red,profile-red,stale-red}.log`에 원 출력 기록.
- 추가 교정: 신규 예약 Run에도 계약 표식 기록, queued 계약 후속 정리, 소유 lease는 기존 세대 cancel 명령 사용, generation claim Run 잠금, 네이티브 진입 및 프로필 선택 후 상태 재확인. 취소된 예약 스냅숏이 목표 재개 후 부활하지 않도록 현재 예약 행 상태도 확인. 사용자 요청/개인 예약/보고 Run은 정리 대상에서 제외한다.
- 재수정 관련 검사 **125 passed in 17.03s** (`scheduler-first-green.log`), 실행·중지·복구 확대 검사 **140 passed in 37.26s** (`scheduler-first-lifecycle-green.log`). 전용 DB는 실행 후 제거했다. scheduler-first 테스트는 실제 dispatcher의 선택을 호출하되 프로세스 생성만 Mock으로 차단해, 취소된 후속이 선택되지 않음을 확인한다. 세대 소유 Run은 cancel 명령 생성/lease 보존까지만 이 신규 검사에서 확인했다.
- 전체 suite는 이번 재수정 뒤 다시 실행하지 않았다. 최종 독립 재검증과 배포 컨트롤러 전체 gate는 별도 판정이다.

A: 부분 Gate complete 거절, 미완료 식별, 실제 manager 종료에서 후속 큐 연결, 최종 complete 뒤 후속 없음.
B: 공통 원인·검증 비용에 따른 계획 선택은 두 실제 모델 응답으로 한정 평가했다. 코드 테스트만으로 기획 능력 개선을 보증하지 않는다.
C/D: 엔진 전환·context reset 뒤 목표/증거/다음 동작 복원, 실제 모델별 합성 응답, 기존 resume/context 회귀 유지.
E: 전략 변경 사유 요구, 요구/완료 증거 보존, 중복/불필요 재실행 방지, stale revision 거절.
F-1: 정확한 계약 표식/개인 예약 보존/다중 DB 취소 경계 회귀를 추가했다. 최종 판정은 현재 verification.json의 재검증 상태를 따른다.

배포 예약 접수는 운영 반영 성공과 구분하며, 예약 ID와 상태를 배포 단계에서 추적한다.

## 7. 남은 위험과 미측정

1. 목표 계약은 장기 실행에서 manage_work checkpoint를 사용해야 활성화된다. 기존 실행 중인 구세대 엔진에 소급 주입하지 않는다.
2. 등록하지 않은 사용자 요구의 누락과 거짓/부정확한 evidence는 구조 검증만으로 탐지하지 못한다. 실행 명령·출력 대조와 요구 전수 확인이 필요하다.
3. paused의 사용자 의사와 전략의 효율성은 의미 판단이므로 모델 지침에 의존한다. 코드가 자연어 의미를 완전히 판정하지 않는다.
4. 진전 없는 자동 후속은 2회로 차단하지만 증거 필드의 변화를 실제 가치 증가로 완전히 판별하지 않는다.
5. 외부 장애/권한의 해소는 기존 도구·재개 경로를 따르며 불가능한 환경을 우회하지 않는다.
6. 실제 운영 완료율·요구 누락률·반복 검증·재지시·시간/토큰 개선은 동등 조건의 후속 운영 관측이 필요하다. 이번 결과는 실측 개선치가 아니다.
7. V2 전체 리팩터링·AIP 포팅·Studio 검수 완성은 각 원 작업의 목표이며 이번 감사가 완료했다고 표시하지 않는다.
8. work_continuation=True는 명시적 계약 예약 opt-in이다. 표식 없는 과거 예약은 개인 예약으로 유지하며 자동 계약 소유로 추정하지 않는다. 새 API 권한 경계나 표식 생성자를 추가한 것은 아니다.


## 참고 근거

- 사용자 원문/기획 응답: source-turns.json의 session ID·UTC 시각. 별도 보호 로그를 우회하지 않고 MCP read_session 사용.
- 배포 실패: ops_deploy_log(be842911-db67-460a-986c-2912b20b4372), release pytest report 병합 테스트 2!=1.
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents): 상태를 명시적으로 인계하고 부분 진척을 전체 완료로 오인하는 실패를 다룬다. 이번 구조 선택의 참고이며 특정 모델의 결함 증거로 사용하지 않았다.
- Anthropic, [Building effective agents](https://www.anthropic.com/research/building-effective-agents): 필요한 만큼의 단순한 구성과 작업 특성에 맞는 제어 경로 선택. 신규 프레임워크/필수 LLM 검사 추가의 근거로 쓰지 않았다.
