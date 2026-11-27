# A1 운영 사례 조사 표: 복합 작업 조기 종료와 목표 계약

조사 2026-11-28 (1차 11-28 초, 보강 같은 날). 시각은 모두 UTC, Run 시각은 `read_session` 의 `at`(Run 생성 시각)이다.
**확인됨** = 읽기 전용 기록에서 직접 읽은 값(세션 턴·이벤트 종류·배포 목록·로그 검색). **추정** = 기록 사이를 이은 해석.

## 사용한 기록과 한계
- `read_session`: 사례 4개 세션의 최근 4~14턴(프롬프트·결과·`status`·`last_event_type`·`origin`·`engine`·세대/epoch).
- `ops_list_deploys`, `ops_deploy_log`, `ops_list_issues`, `ops_get_issue`, `ops_search_logs`(최근 72시간, source=worker/deploy).
- 2차 보강(11-28): 운영 `GET /api/runs/{id}` 와 `GET /api/runs/{id}/events` 를 읽기 전용으로 직접 조회해 Run 이벤트를 확인했다(`~/bin/ttk get`). `read_session` 은 Run `error` 와 개별 이벤트를 내주지 않아 1차에는 못 봤다.
- 여전히 못 본 것: **취소 요청을 누가 보냈는지**. 두 Run 의 이벤트 종류에는 취소 요청·중지 이벤트가 없고(`run.status`, `context.routing`, `worker.routing`, `agent.session`, `tool.call/result`, `message`, `usage`, `error`, `run.finished` 뿐), `Run.error` 는 둘 다 null 이며, `ops_search_logs` 는 두 Run id 로 0건이었다. 작업 세션은 `get_worker` 가 404(하위 세션 아님)였다.

## 사례별 확인표

### 사례 A: `ce4572d0` DS 조립기 미병합 정리·운영 배포 (작업 세션, 백그라운드 테스트 대기 중 취소)
| 항목 | 내용 | 구분 |
|---|---|---|
| 실행 | Run `a89f5f4c` 11-27 15:38:51 생성, 15:38:59 시작, engine=antigravity, profile=한숙 antigravity, model=claude-sonnet-5-5-high, origin=orchestrator | 확인됨(`GET /api/runs/{id}`) |
| 이벤트(105개) | tool.call 43 / tool.result 42 / message 12 / usage 1 / error 1 / run.finished 1 외. **seq102 `tool.call run_command`(`npm run test:browser > /tmp/dsk-b…`) 16:10:36 뒤 결과 없음 → seq103 `usage` 16:14:55.963059 → seq104 `error {"message":"interrupted"}` 16:14:55.963062 → seq105 `run.finished` 16:14:56.861607**(`status=cancelled`, `exit_code=1`, `error=null`) | 확인됨(`/events`) |
| 마지막 Run 종료 사유 | 마지막 `tool.call` 이 약 4분 19초 결과 없이 진행되던 중 `interrupted` 로 끊겨 `cancelled` 로 마감. 마지막 결과문은 영어 4줄("Waiting for the test chain to finish." / "I'll end this turn and wait for it." / "…stopping here until then.") | 확인됨 |
| **취소 요청 주체** | 두 Run 의 이벤트에 취소 요청·중지 이벤트가 없고 `Run.error` 는 null. 사용자 취소·기획의 중지·프로세스 중단 중 어느 것인지 이 기록으로는 구분되지 않는다. 기획 보고 `aa6efe8b` 도 "확실하지 않음" | **미확인** |
| 2번째 Run | `b4c391eb` 16:15:01 생성(origin=user "뭐야 왜 이럼? 버그아니냐?"), profile=안티그래비티 디폴트. 21개 이벤트: **seq18 `tool.call run_command`(grep) 16:15:27.046 → seq19 usage → seq20 `error interrupted` 16:15:27.831080 → seq21 `run.finished cancelled` 16:15:29.287917**. 시작 26초 뒤 같은 모양으로 끊김 | 확인됨 |
| 후속 Run 생성 | 이 세션에서는 없음. 기획이 16:15:29 보고에서 새 세션 `c4af6e16` 으로 다시 시작. 작업 세션에는 계약이 없고 취소된 Run 은 자동으로 잇지 않는 것이 의도(시나리오 8) | 확인됨 |
| 라우팅 | 첫 Run `worker.routing`: `trigger=initial`, `selected`=Antigravity claude-sonnet-5-5-high(한숙 계정, 5h·주간 사용 0%, 여유 100%), `blocked` 배열에는 **GitHub Copilot 만**(premium 100% 사용) 있고 Codex·Claude 는 없다. `candidates` 에 Codex 계정 2개(`limited_until=None`)와 Claude 가 있었고, 기획이 모델을 지정해 Antigravity 가 선택된 것으로 보인다. 두 번째 Run 도 같은 구성(기본 계정) | 확인됨 |
| 계정 한도와의 인과 | 선택 시점에 선택 계정의 한도가 없었고, 실행 중 한도·엔진 오류 이벤트(`error` 는 `interrupted` 하나뿐)도 없다. 직전 Codex 한도 LOG-419(15:30)는 이 Run 의 라우팅 `blocked` 에 나타나지 않아 Antigravity 선택의 원인으로 볼 근거가 없다(1차 문서의 추정을 철회). 한도 때문에 끊겼다는 기록은 없으나, 취소 주체를 모르므로 한도가 간접 원인이 아니라고 단정하지도 않는다 | 한도 이벤트 부재는 확인됨, 인과는 **미확인** |
| 배포·세대 전환 관련 | 없음. 배포 작업 목록에서 11-25 03:42(`3959c608`) 다음이 11-27 17:12(`663d56d5`)라 15:38~16:15 에 전환이 없다 | 확인됨 |
| 원인 | 백그라운드로 건 테스트 체인의 완료를 기다리며 긴 `run_command` 를 걸어 둔 채 진행하던 중 `interrupted` 로 취소됨 | 이벤트로 **확인됨**. 취소를 일으킨 주체와 이 대기 패턴이 취소를 유발했는지는 **미확인** |

### 사례 B: `c4af6e16` DS 조립기 병합·운영 배포 (재시도, 상태 완료 + 본문 부분 충족)
| 항목 | 내용 | 구분 |
|---|---|---|
| 실행 | Run `40ca2be6` 11-27 16:16:13, engine=claude, model=sonnet, origin=orchestrator(기획이 엔진을 바꿔 재지시) | 확인됨 |
| 종료 사유 | `succeeded`, 결과 첫 줄 `상태: 완료`. 본문: `vercel --prod` 실패(`Upload aborted`, `Too many requests - try again in 24 hours`), 같은 커밋이 Git 연동 배포로 Ready, 완료 기준 5는 "**5. 배포: 부분 충족**" | 확인됨 |
| 후속 Run | 없음. 상태 줄과 검증 증거가 있어 기존 완료 계약상 위반이 아니었고, 작업 세션이라 계약도 없다. 기획이 16:53:05 보고를 읽고 사용자에게 정리해 전달 | 확인됨 |
| 계정·엔진 관련 | 막힌 것은 Vercel 요금제 업로드 한도이지 Ttakkari 계정 한도가 아니다. 같은 시간대 antigravity 한도 로그(LOG-420~423)는 다른 Run·엔진이다 | 확인됨 |
| 배포 전환 관련 | 없음(배포 작업 목록 공백 구간) | 확인됨 |
| 원인 | 상태 줄과 본문의 불일치. 다만 대체 경로(Git 연동 배포)로 실제 목표는 달성됐다 | 확인됨 |

### 사례 C: `44ea5af9` 하람 통합 연구 환경 고도화 (기획 세션, 외부 대기 중 자동 이어가기 끝에 blocked)
| 항목 | 내용 | 구분 |
|---|---|---|
| 실행 | engine=codex, 11-27 17:25~18:07 의 14턴을 읽었다. 프롬프트가 `[미완료 목표 자동 이어가기]` 인 Run 이 8개(17:33:55, 17:36:39, 17:38:46, 17:39:39, 17:50:42, 17:53:48, 17:54:33, 17:54:58). 사이에 다른 세션이 보낸 메시지 Run 이 끼어 있다 | 확인됨 |
| 마지막 Run 종료 사유 | Run `9d89d89a`(17:54:58 시작, status=succeeded, exit_code 0, 이벤트 17개). **seq16 `run.finished` 17:55:32.645629 → seq17 `work.blocked` 17:55:32.652328, reason="No recorded progress after two automatic continuations; inspect execution evidence before resuming"**. 그 뒤 18:07:14 Run 은 외부 메시지로 시작해 `run.finished` | 확인됨(`/events`) |
| 후속 Run 생성 | 위 8개가 생성된 후속. 매번 "미완료" 목록 전체가 똑같고 "다음 동작"만 조금 달라졌다. 결과는 매번 "승인 대기, 팀 리드 배정 확인 중" 류 | 확인됨 |
| 미생성 이유 | 17:55:32 이후 후속 없음: `work.blocked`(진전 기록 없는 후속 상한) | 확인됨 |
| 계정·엔진 관련 | 이 시간대 한도·엔진 오류 이벤트 없음. LOG-419(codex 한도)는 15:30 으로 시각이 다르고 Run id(`700f5839`, `dbd3b2a6`)도 이 세션 Run 이 아니다 | 확인됨 |
| 배포·세대 전환 관련 | 관측 epoch 185(17:39:35) → 186(18:09:50), 배포 `72317149`(aad028e4)는 18:07:47 draining. `work.blocked`(17:55:32)는 전환보다 약 12분 앞이다 | 시각 순서는 확인됨, 무관은 **추정** |
| 원인 | 승인·러너 확보 같은 외부 대기를 계약이 표현하지 못해 턴마다 "대기" 보고를 하며 8회를 소모. 체크포인트가 갱신돼 진전으로 계산되다가 어느 시점에 멈춰 blocked | 확인됨(이벤트·프롬프트). 진전으로 계산된 정확한 필드는 **추정**(계약 필드 변경 이력은 못 봄) |
| 남은 부작용 | blocked 계약은 외부 메시지가 와도 풀리지 않는다. 18:07 Run 은 계약이 blocked 인 채 실행됐을 가능성이 크다 | `work.blocked` 이후 `work.continued` 없음은 확인됨, 계약 상태는 **추정**(기획이 체크포인트로 풀었는지는 못 봄) |

### 사례 D: `6121ffef` 하람 결정론적 하네스 전면 구현 (기획 세션, 단발 지시 처리)
| 항목 | 내용 | 구분 |
|---|---|---|
| 실행 | engine=codex, 4턴(16:00, 16:43, 16:56, 17:09), 모두 `succeeded`, origin=orchestrator. 프롬프트는 다른 세션의 개별 지시이고 결과는 "미실행/승인 대기"를 명시한 정직한 부분 보고 | 확인됨 |
| 종료·후속 | 마지막 이벤트가 모두 `run.finished`. `work.*` 이벤트 없음 | 확인됨 |
| 미생성 이유 | 단발 지시라 계약이 필요 없었거나 계약이 없었을 가능성. 계약 존재 여부는 도구로 못 봄 | **추정** |
| 계정·엔진·배포 | 관련 이벤트 없음(한도 로그의 Run id 와 불일치, 해당 시각 배포 전환 없음) | 확인됨 |
| 해석 | 조기 종료 사례가 아니라 정상 단발 처리로 본다 | 추정 |

## 여러 작업이 동시에 중단된 사례의 공통 원인
- **공통 인프라 원인은 찾지 못했다.** 사례 A(취소)와 사례 C(blocked)는 시각이 1.5시간 이상 떨어져 있고, 사례 A 구간에는 배포 전환·계정 한도 이벤트가 없다. 사례 C 의 blocked 는 세대 전환(18:07:47 draining)보다 앞선다.
- **공통 형태는 있다.** 세 사례(A·B·C)는 모두 "턴이 끝난 것"과 "범위가 끝난 것"을 혼동한다: A 는 대기하며 턴 종료, B 는 부분 충족을 완료로 선언, C 는 대기 보고로 후속을 소진.
- 사례 A 는 취소 주체가 미확인이라 "여러 작업이 한꺼번에 사라진" 것의 원인을 인프라로 단정할 수 없다. 이 기록으로는 같은 시각 다른 작업의 동시 취소도 확인되지 않았다.

## 각 원인과 이번 변경(커밋)의 대응
| 원인 | 막는 커밋 | 못 막는 부분 |
|---|---|---|
| 백그라운드 명령을 걸고 완료 알림을 기다리며 턴을 끝냄(A) | `829f9205` 한국어·`40ddef8b` 영어 문구 감지 → 완료 확인 Run. 영어 사례 문구로 시험함 | 사례 A 의 Run 은 `cancelled` 라 완료 확인 Run 이 붙지 않는다(취소는 이어가지 않는 것이 시나리오 8). 상태 줄이 없으면 기존 위반 처리 |
| 부분 충족을 완료로 선언(B) | `829f9205`: 사례 B 본문 재구성(`부분 충족` 포함)이 `partial` 로 판정됨을 실행해 확인했다. 단 B 는 대체 경로로 목표를 달성했으므로 이 규칙은 완료 확인 Run 1개를 더 쓰게 한다(오탐 아님, 비용) | "실패했습니다"처럼 부분 충족 문구가 없는 불일치는 못 잡는다 |
| 외부 대기 중 문구만 갱신하며 8회 이어가기(C) | `a18e2b67`: 문구만 바꾼 체크포인트는 진전이 아님. 재현 시험 `[True, True, False]` 로 blocked | 대기 상태를 계약이 직접 표현(대기 사유·재개 조건)하지는 않는다. blocked 후 외부 메시지로 자동 재개되지 않는 점도 그대로 |
| 설명·읽기 전용 지시가 계약을 열어 후속 2회+blocked(재검증 지적) | `ccf4e782` | — |
| 짧은 복합 자연어·한 줄 번호 항목이 계약을 못 엶 | `6ba843b8`: "조사하고 고쳐서 배포해", "이 부분 개선해줘: 1. … / 2. … / 3. …" 포함 변형 11개 | 동사·항목 구조 기반 휴리스틱이라 목록 밖 표현은 놓칠 수 있다 |
| 한도·엔진 일시 오류가 영구 중단으로 굳음 | `9682352e`(이전 턴): 대기·백오프·상한 | 이번 사례들에서는 발생하지 않음 |
| 작업 세션 취소의 주체·원인 규명 | 못 막음 | Run `error`·이벤트를 도구로 볼 수 없어 미확인. 후속 조사는 서버 쪽 Run 이벤트 열람이 필요 |

## 요약
- 확인됨(Run 이벤트 직접 조회): 사례 A 두 Run 모두 마지막 `tool.call` 이 결과 없이 진행되던 중 `error {"message":"interrupted"}` 직후 `run.finished status=cancelled`(`Run.error` null, exit_code 1). 사례 A 라우팅은 Copilot 만 blocked 이고 Antigravity 가 여유 100% 로 선택됨. 사례 B 의 `완료`+`부분 충족`, 사례 C 의 8회 자동 이어가기와 seq17 `work.blocked`(17:55:32), 사례 A 구간의 배포 전환 부재.
- 추정: 사례 C 에서 진전으로 계산된 필드, 사례 D 의 계약 부재, 사례 C 와 세대 전환의 무관.
- **미확인**: (1) 사례 A 의 **취소 요청 주체** — 취소 요청 이벤트가 기록되지 않아 사용자·기획·프로세스 중단을 가르지 못한다. (2) **계정 한도와의 인과** — 라우팅 시점·실행 중에 한도 기록이 없다는 것만 확인됐고, 1차의 "codex 한도로 antigravity 배정" 추정은 라우팅 기록과 맞지 않아 철회했다. 취소 주체를 모르므로 한도가 간접 원인이 아니라고도 단정하지 않는다.
- 후속 후보: 서버의 취소 경로(`/cancel`, `stop_worker`, 전역 중지)가 `run.cancel_requested` 같은 이벤트에 요청 주체를 남기도록 하는 것은 이번 범위 밖이다.
