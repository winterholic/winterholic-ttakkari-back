# 2차 검증 (같은 브랜치, 구현 commit 9336fdb 이후)

1차 RED/GREEN 은 `evidence.md` 참고. 이 문서는 2차에서 새로 실행한 결과만 가리킨다.

## 리드 의심점에 대한 코드 대조
- `claim_daily_budget` / `budget_remaining_after_claim` 은 이 브랜치에 없다(grep 0건). `stages.py` 는 `reserve_daily_budget` 을 쓰고
  `ExtractionOptions.daily_budget_remaining` 에는 **예약받은 호출 수(reserved_calls)** 를 넘긴다. 전역 잔여량은
  `daily_budget_remaining_after_reservation` 으로 detail 에만 기록한다(관측용, 호출 상한이 아님).
- 실제 ExtractingProcessor 경로 테스트(`tests/test_kb_v3_l2_budget_boundaries.py`): budget=1 단독 정확히 1호출, budget=3 에
  passage 6개 문서는 3호출, budget=0 은 호출 0·원장 0행, 연속 3문서 budget=2 는 [1,1,0] 호출이고 finished stage detail 과
  원장이 같은 호출을 이중 계수하지 않음, timeout 은 청구되고 계정 거부(미호출)는 환불, 가용 계정 없음은 예약 자체를 만들지 않음,
  UTC 날짜 변경 시 새 예산 + 전날 lease 차단, 원장 이전 stage baseline 이 예산을 막음.
- 예약은 어댑터 이름과 무관하게 `available` 인 모든 어댑터에 걸린다. `build_l2_adapter` 호출처는 `stages.py` 한 곳이다.
- 실제 subscription 어댑터와 fake codex 실행파일로 budget=1 동시 2문서를 돌렸을 때 CLI 실호출은 1회
  (`test_two_concurrent_documents_reach_the_cli_once_at_budget_one`).

## 2차에서 찾아 고친 결함 (RED -> GREEN)
| 결함 | 수정 |
|---|---|
| 전날 active lease 가 계속 active 로 남아 in_flight 가 정리되지 않음 | `daily_budget._expire_leases` 가 `budget_date <= day` 를 회수 |
| 실행파일이 없어 프로세스가 뜨지도 않았는데 슬롯을 소모로 청구 | `subscription_transport` 가 `consumed=False` 로 환불 |
| claude 엔진은 스키마를 못 받아 실제 응답이 스키마 밖 fenced JSON 이 되어 전부 invalid_json | `desk._claude_cmd(..., output_schema)` 가 `--json-schema` 전달(스키마 없는 기본 호출은 불변) |
| API 키 환경변수가 구독 CLI 에 상속되면 종량제로 새 수 있음 | `desk._ask(subscription_only=True)` 가 `_PAID_API_ENV_KEYS` 제거, transport 는 항상 켬 |

- RED: `red_incremental.stdout.txt` (base `red_incremental.base_sha` 에 새 테스트 2파일만 복사) -> `4 failed, 15 passed`.
- GREEN: `green_incremental.stdout.txt` -> `74 passed` (예산 경계, CLI 계약, 기존 L2, desk, visibility, integration flow).
- 첫 합산 실행에서 test_desk 가 OSError 로 48개 에러였으나 개별 실행과 재실행은 통과했다. 환경 일시 오류로 보며 원인은 규명하지 못했다.

## CLI 계약 테스트 (fake codex 실행파일, `tests/test_kb_v3_subscription_cli_contracts.py`)
read-only/ephemeral/hooks·shell·web·collab·plugins 비활성/사용자 config 의 MCP 서버 비활성/approval never, 고유 임시 cwd 와
schema 파일(호출 후 삭제), 계정 pool 선택(work_only 는 work 계정만, personal+work_only 와 unknown 은 CLI 미호출),
passage 본문이 로그에 없음, quota 응답 후 cooldown 과 재호출 차단, invalid JSON/schema 위반 분류와 청구,
timeout/cancel 시 프로세스 종료와 임시 디렉터리 삭제, API 키 env 미상속.

## 실제 구독 CLI smoke (합성 공개 passage 3개, 운영 DB/설정 무변경)
- 스크립트 `smoke_subscription_l2.py`. 임시 `TTAKKARI_HOME`, 접속 불가 DB URL, DB 계정 행 없이 기본 CLI 로그인으로 Profile 을 직접 만들었다.
- codex 0.162.1: 실제 응답 **없음**. `smoke_codex.stdout.txt` 는 3건 nonzero_exit. 직접 `desk._ask` 로 원문 오류를 보니
  `401 Unauthorized: Missing bearer or basic authentication`(기본 `~/.codex` 에 로그인 없음). 관리 계정은 보호 경로
  `~/.ttakkari/accounts` 라 가드가 막았고 우회하지 않았다. codex 실응답 smoke 는 **미실행/미검증**.
- claude 2.1.285: 수정 전 `smoke_claude_before_json_schema_fix.stdout.txt` 는 3건 invalid_json(응답은 왔으나 스키마 밖).
  수정 후 `smoke_claude.stdout.txt` 는 3건 성공, evidence_quote 총 30개(11+12+7) 전부 본문의 정확한 부분문자열, 지연 15~65초.
  추출 품질은 인용 정확성과 엔티티 목록을 눈으로 본 수준이며 정답 대비 정밀도/재현율은 측정하지 않았다.
- 모델/계정: 모델은 CLI 기본값(별도 probe 1회의 modelUsage 에 `claude-opus-5-5`). 계정은 호스트 기본 claude 로그인 하나. 쿼터 lookup 은 하지 않았다.
- 추가 과금 없음: **코드 추론**(구독 CLI 만 호출, API 키류 env 제거, 종량제 fallback 경로 없음)이며 실측이 아니다.
  claude 결과 JSON 의 `total_cost_usd` 는 list 기준 환산값(`costBasis: list`)이라 실제 청구액이 아니다.

## 알려진 한계 / 합류 계약
- personal pool 후보는 앱 규칙상 모든 계정(work 계정 포함)이다. personal+global 문서가 회사 pool 계정으로 나갈 수 있는지는
  앱의 기존 계정 정책이며 이 브랜치는 바꾸지 않았다. 필요하면 리드가 정책을 정한다.
- 프롬프트(passage 본문)는 CLI argv 로 전달된다(`desk._ask` 기존 방식). 같은 호스트의 프로세스 목록에서 보일 수 있다.
- quota 로 거절된 호출도 보수적으로 소모 처리한다(과환불 방지, 과청구 방향).
- `deploy/generation-contract.json` 의 schema_revision 이 0071 이라 0072/0073 head 와 어긋난다(4ca1ed0d 부터 있던 문제, 미수정).
- push/merge/deploy 없음. 948de533 HTML/alias 변경, `extractor_step.py`, `kb_delivery.py` 는 건드리지 않았다.
