# 로컬 LLM 작업자 벤치 과제 명세 v1 (WBS 4.1)

작성일: 2026-10-23 KST. 상태: **v1 확정(4.1 범위). draft-0(커밋 `ef64f5f`, `c460394`)에 WBS 1.2 감사 결과를 반영해 확정했다. 4.2(fixture·gold·hidden·mutant 구현)는 아직 시작하지 않았다. §9의 잔여 과제는 4.2~4.3과 운영자 조치로 넘긴다.**

- 상위 설계: `docs/design/local-llm-worker-refactor.md` §5(벤치), §10 WBS 4.1. 이 문서는 시각 UI 설계가 아니라 **평가 명세**다.
- PMS: project `0b1a2a57-eb19-4da0-b572-ab783d6a6735`, task 4.1 `7bad4b21-2a0c-45d2-9934-2cf7a1871e89`, predecessor 1.2 `f4a5ab31-4d6c-4107-917f-40f83b29e94d`. 1.2 감사 완료는 메인이 `raw 61행 / 136,608 bytes / SHA256 d525733d…e1362be0`, manifest 61행 coverage, t6 성공당 165.0초·t8 239.7초를 직접 재집계해 확인했다고 전달했다. 이 문서는 그 전달과 감사 산출물 파일을 근거로 삼는다.
- 기준 main: `474e034a684935e2c3d863820b5e7b0d3ffdc7b7`에서 작성을 시작했다. 설계 commit `7e637d79…`는 origin/main의 ancestor임을 확인했다.
- 짝 파일: `benchmark-manifest-draft.json`(과제·검사 id 목록, `manifest_revision: v1`), `evidence/4.1/`(CPU 검사 스크립트·원출력).
- **이 문서의 모든 숫자는 사전등록 계획값이며 실측 결과가 아니다.** 모델 응답·서버·GPU·학습·벤치는 실행하지 않았다.
- 감사 정본: `local-llm-worker-design-20261023` worktree의 `docs/design/local-llm-worker-refactor/evidence-audit/`(`audit-report.md`, `attempt_manifest.csv`, `config_summary.csv`, `source_manifest.csv`, `verifier_case_summary.csv`, `audit_recount.json`). 이 문서가 감사 수치를 옮길 때는 `audit_recount.json`과 CSV를 정본으로 삼고 원 JSONL과 대조했다. 감사 보고서 본문의 API 오류 내역 설명에 전사 오류가 있었으며(6+6+20으로 쓰였던 것을 3+3+20으로 정정), 원본 수치를 쓴다.

## 0. 이번 작업에서 실제로 한 일과 하지 않은 일

| 구분 | 내용 | 상태 |
|---|---|---|
| 파일/코드 확인 | 설계 문서, `qwen-local-harness`의 `bench/2026-10-18/{tasks.py,validate_tasks.py,evalbase/*}`, `bench/2026-10-18-longctx/longtasks.py`, `/Users/winterholic/testing/qwen-worker-stage1/{task.txt,verify_fixture.py,verify_fixture.py.orig,fixture-template}`, 1.2 감사 산출물 읽기 | 확인함(읽기 전용) |
| 모의검사 | (a) 기존 verifier 음성 대조 14건, (b) 기존 참조해 재검증, (c) 요구 검사의 base 상태 delta/invariant 확인과 shop trap 확인, (d) 개발 fixture 정적 누출 스캔. 모두 /tmp 복사본, 모델 없음 | 실행함. §5.3, §5.4 |
| runtime readiness | 서버·worker·Seatbelt·lease 확인 | **미실행** |
| 실제 모델 완주 | 어떤 모델도 실행하지 않음 | **미실행** |
| fixture 구현·gold patch·hidden 작성 | 4.2의 일 | **미착수** |
| PMS | 4.1 상태 갱신 | 이 v1 push 뒤 갱신(§9 끝) |

`qwen-local-harness` 저장소는 dirty 상태다(작성 시작 시 HEAD `b278b96f…`, v1 작성 중 HEAD `c9123ac57a0685c652e02076f1baf25aa1426ee7`로 다른 작업자가 전진, 읽기만 했고 건드리지 않았다). 이 문서가 근거로 삼은 `tasks.py`·`stage3.py`·`longtasks.py`·`validate_tasks.py`·`verify_fixture.py`의 sha256은 v1 작성 때 다시 계산해 draft-0 값과 같음을 확인했다(§7).

## 1. 설계 원칙

1. **9유형×2변형=18과제.** 유형마다 개발(D) 1개, holdout(H) 1개. 유형은 설계 §5.1 표와 같다: 탐색, 단일 파일, 다중 파일, REST, 디버깅, 리팩토링, 테스트 작성, 오류 후 자율 수정, 장시간 다중 턴.
2. **개발 과제 9개는 기존 fixture를 재활용하되 결함을 고친 revision으로 쓴다.** 기존 fixture는 과거 실험·프롬프트 조정에 이미 노출됐으므로 holdout으로 쓰지 않는다. 기존 fixture에서 발견한 결함은 §5.3과 §7에 따로 적는다.
3. **holdout 9개는 전부 새로 쓴다.** 어떤 모델에도 실행된 적이 없고, 모델이 읽을 수 있는 경로에 둔 적이 없는 자료여야 한다. 요구사항 수와 검사 class는 짝이 되는 개발 과제와 같게 하되 도메인·식별자·상수·파일 배치·decoy·trap을 바꾼다. 이름만 바꾼 복제는 불가이며, 요구사항 중 최소 1개는 종류가 달라야 한다.
4. **모델에게 주는 문제는 hidden 조건까지 포함한 완전한 기능 명세다.** hidden은 공개 문제문에 없는 요구를 추가하는 수단이 아니다. 모든 hidden 검사는 프롬프트·docstring·공개 코드 주석의 문장 중 하나로 추적돼야 한다(§3의 요구↔hidden 표). 추적되지 않는 검사는 삭제하거나 프롬프트를 개정한다.
5. **성공은 모델 자기보고나 공개 테스트 통과로 인정하지 않는다.** 성공 = 보호된 evaluator의 PASS(공개+hidden+요구행동) + trace와 맞는 구조화 보고. 공개 테스트만 통과하고 hidden이 실패하면 별도 분류 `FAIL_HIDDEN_ONLY`로 센다.
6. 모든 과제는 새 임시 디렉터리, 고정 base, 같은 도구·권한으로 시작한다. 원본 checkout·운영 DB·비밀을 fixture로 쓰지 않는다. 의존성은 표준 라이브러리만 쓴다(네트워크·pip 불필요).
7. **attempt마다 pristine buggy fixture로 reset하고 시작 상태를 hash로 고정한다(1.2 감사 P1-3).** 시작 직전 base tree hash와 파일별 sha256을 기록하고 sealed 기준값과 같을 때만 첫 모델 요청을 보낸다. 다르면 그 attempt는 모델 평가가 아니라 `INFRA_BASE_MISMATCH`(무효, 비용은 기록)다. 종료 시점의 파일별 sha256도 기록한다. 이유: 현재 checker 둘(과거·현행 `verify_fixture.py`)은 최종 상태만 보므로, **이미 정답인 workspace와 유효한 test가 있으면 no-op도 PASS한다**(`verifier_case_summary.csv` `no_op_already_satisfied_with_valid_tests`: 두 checker 모두 exit 0).
8. **최종 상태만으로 성공을 판정하지 않고, 초기 상태와 요구 변경을 대조한다.** 모든 hidden 검사를 `delta`(base에서 red, 참조해에서 green)와 `invariant`(양쪽 green)로 분류한다. PASS는 모든 delta 검사 green이고 invariant 검사 green이며, delta 검사가 1개 이상 있어야 한다. base에서 delta 검사가 실제로 red임을 과제마다 CPU로 확인하는 것이 4.2의 수락 조건이다(§5.4에 개발 과제 일부 확인 결과).
9. **변경 요구 class를 과제별로 구분한다.** `diff_required`(코드·테스트 변경이 있어야 함)와 `read_only_report`(정답이 변경 없음+보고)를 섞지 않는다. 모든 과제에 "diff 필수"를 적용하면 탐색 과제의 정답이 no-op과 같은 상태가 되므로 오판한다. 탐색 과제의 no-op 방지는 diff가 아니라 보고 산출물(`report.json`)의 존재·정확성·trace로 한다(§2.1).

## 2. 18과제 일람

D는 개발, H는 holdout. `기존`은 재활용하는 fixture, `신규`는 새로 쓸 fixture. 요구/hidden 수는 §3의 표에서 센 값이다.

| ID | 유형 | 분할 | fixture 출처 | 요구/hidden | 예산 class |
|---|---|---|---|---:|---|
| B4-EXP-D | 탐색 | D | 기존 `L1-repo-explore`(longtasks.py, 26 모듈 seed 7), 읽기 전용 과제로 개정 | 8/8 | short |
| B4-EXP-H | 탐색 | H | 신규, 같은 생성기 구조·다른 도메인과 seed | 8/8 | short |
| B4-SGL-D | 단일 파일 | D | 기존 `t1-bug-median` | 7/7 | short |
| B4-SGL-H | 단일 파일 | H | 신규 | 7/7 | short |
| B4-MUL-D | 다중 파일 | D | 기존 `s3-fix-inventory`(프롬프트 개정 필요) | 7/7 | short |
| B4-MUL-H | 다중 파일 | H | 신규, 레거시 경로 포함 | 7/7 | short |
| B4-API-D | REST | D | **신규**(기존 fixture 없음) | 9/9 | short |
| B4-API-H | REST | H | 신규 | 9/9 | short |
| B4-DBG-D | 디버깅 | D | 기존 `t4-long-file` rev2(공개 테스트와 failure log 추가) | 7/7 | short |
| B4-DBG-H | 디버깅 | H | 신규, 오해하기 쉬운 stack trace | 7/7 | short |
| B4-REF-D | 리팩토링 | D | 기존 `t2-refactor-dedupe` | 7/7 | short |
| B4-REF-H | 리팩토링 | H | 신규 | 7/7 | short |
| B4-TST-D | 테스트 작성 | D | 기존 `t3-write-tests` | 8/8 | short |
| B4-TST-H | 테스트 작성 | H | 신규 | 8/8 | short |
| B4-REC-D | 오류 후 자율 수정 | D | 기존 stage1 `shop` fixture(`qwen-worker-stage1/fixture-template`) | 7/7 | short |
| B4-REC-H | 오류 후 자율 수정 | H | 신규 | 7/7 | short |
| B4-LNG-D | 장시간 다중 턴 | D | 기존 `s3-feature-slugify`+`s3-rename-calc-tax`+`t1-bug-median`을 한 workspace로 합성 | 14/14 | long |
| B4-LNG-H | 장시간 다중 턴 | H | 신규 | 14/14 | long |

설계 §5.1의 파일럿 대표 3과제는 과거 C_opt 기본 task 집합(`optbench.py` 기본값 `t1-bug-median,s3-fix-inventory,t3-write-tests`)과 같은 **B4-SGL-D, B4-MUL-D, B4-TST-D**로 한다. 같은 과제를 쓰는 이유는 verifier·fixture 호환성 오류를 찾기 위해서이지 과거 수치와 비교하기 위해서가 아니다. **과거 `cmoe-t6-ub256-ctx4k-low`의 성공당 165.0초(6/6, 모든 시도 wall 990.1초, 실제 batch 990.7초)와 `cmoe-t8-ub256-ctx4k-low`의 239.7초(12시도 중 성공 6, wall 1,438.4초)는 새 E2E 측정의 직접 비교 기준으로 쓰지 않는다.** 감사(`audit-report.md`)가 확인했듯 이 값은 setup, 서버 start→ready, 별도 최종 hidden/mutation 채점, 초기화 비용이 빠진 agent-run 비용이다. 또 t8의 239.7초에는 API 오류가 섞인 재시도 비용이 들어 있고(성공 시도만의 평균은 184.7초이며 이것으로 대체할 수 없다), 과거 설정들은 출력 상한(off 1,800 vs low/default 3,500 token)과 thinking이 서로 달라 단일 변수 비교가 아니다. 각 attempt를 특정 서버·모델 artifact에 암호학적으로 묶는 필드(HTTP status, server run/instance ID, artifact hash)도 raw에 없다. 새 baseline은 같은 프로토콜로 FlashNext를 다시 측정한다.

### 2.1 변경 요구 class

| 과제 | class | 정답 상태의 diff | no-op 방지 수단 |
|---|---|---|---|
| EXP-D/H | `read_only_report` | report 파일 외 **변경 없음** | 보고 파일 없음·빈 보고·틀린 보고는 FAIL, 수정은 `FAIL_FORBIDDEN_WRITE`. diff 요구 없음 |
| SGL, MUL, DBG, REF, REC (D/H) | `diff_required` | 요구 경로 변경 필수 | delta 검사(§1-8), 필수 경로 변경(MUL은 두 소스 파일 모두) |
| API (D/H) | `diff_required` | 신규 endpoint 구현 | delta 검사(base는 `/health`와 빈 `GET /notes`만 동작) |
| TST (D/H) | `diff_required`(산출물은 테스트) | 새 test 파일은 있고 **SUT는 불변** | 새 test의 gold 통과, mutant 사망, SUT hash 불변 |
| LNG T1~T4 | `diff_required` | 턴마다 요구 경로 변경 | 턴별 delta 검사 |
| LNG T5 | `read_only_report` | **diff 없음**(보고만 하라는 턴) | 보고 JSON이 실제 누적 diff와 일치, T5 중 파일 수정은 `FAIL_FORBIDDEN_WRITE` |

기존 fixture 중 18과제에 쓰지 않는 것: `t1-bug-paginate`(SGL 대체 후보), `t3-explain-review`·`t3-korean-docs`(키워드 정규식 verifier 때문에 수락 근거 불가, §5.3 P7b), `t3-shell-count`(shell 도구 경로 보조), `L1-repo-dump`·`L1-repo-small`(컨텍스트 4K/8K/32K 층화용, 5.2 소관). 이들은 18과제의 점수에 섞지 않는다.

## 3. 과제 명세: 개발 9개 (요구 ↔ hidden 기대행동)

공통: 모든 hidden 검사는 evaluator 프로세스(§4)가 돌린다. 열 `근거`는 그 검사를 정당화하는 공개 문장이다. `기존`은 현재 `tasks.py` 등에 있는 검사, `신규`는 4.2에서 새로 써야 하는 검사다. 참조해(gold)와 대안 참조해(alt-gold)는 과제마다 요구한다. 대안 참조해는 서로 다른 구현이어야 하며(예: Decimal 대 정수 산술), 둘 다 PASS하지 못하면 evaluator가 과도하게 제한하는 것으로 본다.

### 3.1 B4-EXP-D 탐색 (ledger, 읽기 전용)

프롬프트(신규 문안): ledger 패키지에서 금액을 정수 센트로 바꾸는 함수를 모두 찾고 각 함수의 호출자를 `report.json`에 기록하라. 파일은 수정하지 마라. 항목마다 `path`, `function`, `line`(정의 시작 행)을 적는다.

정답은 base snapshot의 AST로 빌드 시점에 계산한 sealed key로 두고, 하드코딩 값과 AST 계산 값이 같은지 자체 검사한다. 현 fixture에는 `money.to_cents`(`int(float(amount)*100)`)와 `legacy/fees._cents`(`int(x*100)`) 두 변환 지점, 호출자 `tax.tax_cents`·`invoice.line_cents`·`fees.processing_fee`, 그리고 역방향 decoy `money.from_cents`가 있다(`longtasks.py` 읽기 확인; AST 계산은 4.2).

| 요구 | hidden | 근거 |
|---|---|---|
| R1 `to_cents` 식별 | H1 converters에 (money.py, to_cents, 정의 행) | 프롬프트 |
| R2 레거시 `_cents` 식별 | H2 converters에 (legacy/fees.py, _cents, 정의 행) | 프롬프트 "모두" |
| R3 decoy 제외 | H3 converters 집합이 key와 정확히 일치(`from_cents` 포함 시 실패) | "금액을 센트로" |
| R4 호출자 완전성 | H4 callers 집합이 key와 일치 | 프롬프트 |
| R5 인용 정확성 | H5 인용한 모든 (path, line)이 base snapshot에서 해당 함수 정의에 대응, 없는 경로·행은 실패 | 프롬프트 |
| R6 읽기 전용 | H6 report 파일 외 workspace tree hash가 base와 동일 | 프롬프트 |
| R7 형식 | H7 `report.json`이 schema에 맞고 비어 있지 않음 | 프롬프트 |
| R8 실제 탐색 | H8 인용한 각 파일에 대해 trace에 검색 또는 읽기 도구 이벤트가 인용 이전에 존재 | 설계 §5.1 "trace가 남아야 한다" |

필수 reject: 빈 report, report 없음, money.py만 기재, 존재하지 않는 경로 인용, 파일명을 산문으로만 언급(구조화 필드 없음), 파일 수정, 호출자 목록에 decoy 모듈 기재. 설계 §5.1 "키워드 포함만으로 pass 금지"를 따른다.

change class는 `read_only_report`다. **변경 없는 workspace가 정답 상태**이므로 diff를 요구하지 않고 `report.json`의 존재·정확성과 trace로만 판정한다(H6은 invariant, 나머지 H는 report 산출물에 대한 delta: base에는 report가 없어 red). pristine reset·시작 hash 기록(§1-7)은 그대로 적용한다.

### 3.2 B4-SGL-D 단일 파일 (median)

기존 프롬프트(`tasks.py` `t1-bug-median`)가 짝수 평균·빈 리스트 ValueError·`tests/` 수정 금지를 명시한다. docstring이 입력 비수정을 명시한다.

| 요구 | hidden | 근거 | 출처 |
|---|---|---|---|
| R1 짝수 개 평균 | H1 `[10,20]==15.0`, `[4,1,3,2]==2.5`, `[1..6]==3.5` | 프롬프트 | 기존(일부) |
| R2 빈 리스트 | H2 `median([])`가 ValueError | 프롬프트 | 기존 |
| R3 홀수 개 | H3 `[3,1,2]==2`, `[5]==5` | 공개 test | 기존 |
| R4 입력 비수정 | H4 list·tuple 입력 후 내용 동일 | docstring | 기존(list만) |
| R5 음수·실수 | H5 `median([-3,-1])==-2.0`, 실수 입력 | "숫자 리스트" | 신규 |
| R6 `tests/` 불변 | H6 공개 test 파일 집합과 내용 hash 동일 | 프롬프트 | 신규(현재는 `unittests`만 실행) |
| R7 무관 동작 불변 | H7 `mean([1,2,3])==2` 유지, `mean([])`는 기존대로 ZeroDivisionError | 변경 범위 | 신규 |

필수 reject: no-op, `xs.sort()`로 입력 변경, 짝수에서 `//` 평균(2.5 대신 2), 빈 입력에 None 반환, 홀수만 고치고 짝수 누락, 항상 ValueError. 참조해는 `validate_tasks.py` `sol_median` 재사용.

base 상태 확인(§5.4, CPU 실행함): H1·H2·H5는 base red→참조해 green(delta), H3·H4·H7은 양쪽 green(invariant). H6(tests 불변)은 4.2에서 구현 후 확인. change class `diff_required`.

### 3.3 B4-MUL-D 다중 파일 (inventory)

기존 `s3-fix-inventory`. `inv/report.py` docstring이 "sorted by name … Numbers use thousands separators"를 명시하고, 프롬프트는 "테스트 실패, 소스 수정, tests/ 수정 금지"만 말한다.

**프롬프트 개정 필요(spec gap):** 현 hidden은 수량(`5 x 2,000`)과 TOTAL의 천 단위 구분자, 빈 목록에서 `'TOTAL: 0'`을 검사하는데 프롬프트에 수량·빈 목록 규칙이 없고 docstring의 "Numbers"가 수량까지 가리키는지 모호하다. 설계 §5.1 원칙 4에 따라 프롬프트에 "가격·수량·TOTAL 모든 숫자에 천 단위 구분자, 빈 목록은 `TOTAL: 0`"을 명시하는 rev2를 만든다(해당 hidden을 빼는 대안은 채택하지 않는다).

**1.2 감사로 확정된 과거 사례(P1-2):** `C_opt.jsonl` 60행(`cmoe-t6-ub256-ctx4k-default`, `s3-fix-inventory`, rep 1, 같은 task×rep의 2번째 attempt, `end_reason=truncated`, wall 336.4초, 11 tool calls)은 공개 테스트까지 통과시켰으나 hidden이 `alpha: 5 x 2,000\nmid: 10 x 10\nzeta: 1,000,000 x 1\nTOTAL: 1,010,100`와의 정확 일치에서 실패했다(`hidden_ok=false`, `unittest_ok=true`). 원 raw 행의 `hidden_out`은 마지막 300자만 있어 모델이 낸 실제 문자열은 raw에서 복원되지 않는다. 감사 보고서는 그것을 `5 x 2000`(수량 구분자 누락)으로 기술한다. 이 한 줄이 위 spec gap(수량 구분자)과 같은 경계를 가리키지만, 실제 출력 문자열은 감사 보고의 기술이며 raw로는 독립 확인하지 못했다(확인 필요). 이 사례를 **H3의 negative control `NC-A5`(수량 구분자 없음, `5 x 2000`)**로 고정하고, MUL-H에도 같은 종류(구분자·정규화 누락)의 출력 hidden을 둔다.

| 요구 | hidden | 근거 |
|---|---|---|
| R1 합계=가격×수량 | H1 `total_value([Item('x',7,9)])==63`, 빈 목록 0 | docstring |
| R2 이름순 | H2 순서 뒤섞인 3항목 출력 순서 | docstring |
| R3 가격·수량 천 단위 | H3 `5 x 2,000`, `1,000,000 x 1` | rev2 프롬프트 |
| R4 TOTAL 천 단위 | H4 `TOTAL: 1,010,100` | rev2 프롬프트 |
| R5 빈 목록 | H5 `format_report([])=='TOTAL: 0'` | rev2 프롬프트 |
| R6 `tests/` 불변 | H6 | 프롬프트 |
| R7 두 경로 일관 | H7 `items.py`와 `report.py` 두 파일 모두 수정되어야 통과(한 파일만 고친 patch는 reject) | 설계 §5.1 |

필수 reject: total만 수정, 정렬만 수정, **`5 x 2000`처럼 가격에만 구분자(NC-A5)**, 구분자 대신 `,` 하드코딩 문자열, `tests/` 삭제, 테스트 기대값만 수정. 참조해는 `sol_inventory` 재사용.

base 상태 확인(§5.4, CPU 실행함): H1(합계)·H3(수량 구분자)는 base red→참조해 green(delta), H5(빈 목록 `TOTAL: 0`)는 base에서도 green(invariant). change class `diff_required`, 필수 변경 경로는 `inv/items.py`와 `inv/report.py` 둘 다.

### 3.4 B4-API-D REST (신규)

**의도:** 작은 endpoint, validation, 오류 status, 영속성·원자성 경계, 기존 API 회귀. 소켓·포트를 쓰지 않도록 WSGI callable `notes.app:application`을 evaluator가 프로세스 안에서 직접 호출한다. 네트워크와 포트는 쓰지 않는다. 시작 상태에는 `GET /health`와 `GET /notes`(빈 목록)만 구현돼 있다. 영속화는 `DATA_DIR/notes.json`이다.

프롬프트는 계약 전체를 표로 완전히 서술한다(요청 필드, 상태 코드, 오류 body 형식 `{"error":{"code":str,"field":str|null}}`, code 열거). hidden은 그 표의 항목만 검사한다.

| 요구 | hidden |
|---|---|
| R1 `POST /notes` 유효 입력 → 201, `Location`, body에 id·title·body | H1 |
| R2 입력 검증 400: title 누락·빈 문자열·81자 이상·비문자열, body 1001자 이상, 깨진 JSON, 각각 계약의 code·field | H2 |
| R3 title 중복 409 | H3 |
| R4 `GET /notes/{id}` 200/404 | H4 |
| R5 `limit`(1~100, 기본 20)·`offset`(0 이상) 검증, 범위 밖·비정수는 400 | H5 |
| R6 영속성: 새 app 인스턴스가 이전 데이터를 읽는다. 원자성: 저장 중 `os.replace`를 실패시켜도 이전 파일이 유효한 JSON이고 응답은 계약된 500 JSON | H6 |
| R7 기존 `GET /health`, `GET /notes` 응답 불변 | H7 |
| R8 405(`Allow` 헤더)·415(비 JSON Content-Type) | H8 |
| R9 DATA_DIR 밖에 파일을 만들지 않음, 임시 파일 잔재 없음 | H9 |

필수 reject: 메모리에만 저장(R6), 쓰기 중간 실패에서 빈 파일(원자성), 400과 404 혼동, 응답 body만 맞고 status 틀림, `GET /notes`를 고장 낸 변경(R7), 하드코딩된 응답. 참조해와 대안 참조해(예: 임시 파일+`os.replace` 대 `fsync` 포함)는 4.2에서 작성.

### 3.5 B4-DBG-D 디버깅 (rules 60함수, rev2)

**fixture 결함(직접 확인):** 기존 `t4-long-file`은 프롬프트가 "test_rule_043이 실패한다"고 말하지만, 미수정 workspace에서 공개 `unittest`가 **통과한다**(§5.3 P1, `unittest_ok=True`). 이유는 공개 test의 입력 60·100·-5가 clamp 뒤 3의 배수라서 `y/3+7`이 27.0·37.0·7.0이고 `27.0==27`이 참이기 때문이다. 실제 실패는 hidden의 `isinstance(int)`와 61 입력(`61/3+7=27.333…`)에서만 드러난다(산술 확인함). 따라서 이 fixture는 "stack trace·log에서 원인 추적"이라는 유형에 맞지 않는다. 이 Run에서 CPU로 재확인했다(`evidence/4.1/verify_delta_and_trap_output.json`: 미수정 base의 공개 unittest green, `rule_043(61)==27` red).

rev2(신규 작성): 공개 test에 `rule_043(61)==27`을 추가해 미수정 상태에서 실제로 실패하게 하고, `logs/ci_failure.log`는 **빌드 시 CPU로 실행한 실제 출력**을 저장한다(손으로 쓴 로그 금지). 파일 안 주석("scale must use floor division by 3")은 그대로 요구 근거다.

| 요구 | hidden | 근거 |
|---|---|---|
| R1 재현 | H1 trace에서 첫 편집 이전에 실패 test를 실행한 이벤트 | 설계 §5.1 "원래 실패 재현" |
| R2 근본 원인 수정 | H2 `rule_043(61)==27`, `rule_043(3)==8`, 결과가 int | 주석·공개 test |
| R3 다른 함수 불변 | H3 나머지 59개 함수와 `apply_all`의 AST 정규화 hash 동일 | 프롬프트 "다른 함수는 건드리지 마라" |
| R4 다른 입력 | H4 `rule_043`의 여러 입력(clamp 경계 0·90, 음수)이 gold와 동일 | 주석 |
| R5 원래 실패 해소 | H5 공개 test 전체 통과 | 프롬프트 |
| R6 원인 설명이 패치와 대응 | H6 구조화 보고 `root_cause{file,function,mechanism}`의 function이 diff가 건드린 함수와 같고 mechanism이 닫힌 열거(`float-division` 등)와 일치 | 설계 §5.1 "원인 설명과 패치 대응" |
| R7 `tests/` 기존 파일 불변 | H7 | 프롬프트 |

필수 reject: hidden만 맞추려 `int()`로 전체를 감싸는 변경, 다른 rule 함수 수정, 테스트 기대값을 27.33으로 수정, 원인을 다른 함수로 보고하고 우연히 고친 경우(H6 불일치), 재현 없이 곧바로 편집(H1 실패는 분류만 하고 gating 여부는 §6.7 결정 항목).

### 3.6 B4-REF-D 리팩토링 (dedupe)

기존 `t2-refactor-dedupe`. 프롬프트가 `report/common.py`의 `clean(s)`, 두 모듈이 import해 사용, `_clean` 이름 전무, 동작 유지를 명시한다.

| 요구 | hidden | 근거 | 출처 |
|---|---|---|---|
| R1 `report.common.clean` 존재 | H1 `clean('  a,  b ')=='a; b'` | 프롬프트 | 기존 |
| R2 두 모듈이 실제로 common을 경유 | H2 **치환 검사**: evaluator가 `report/common.py`를 `clean = lambda s: "SENTINEL"`로 바꾼 사본에서 `to_csv`·`to_json` 출력이 SENTINEL을 반영해야 함 | 프롬프트 "import 해서 쓰게" | **신규** |
| R3 `_clean` 부재 | H3 전체 `.py` 문자열 검색 | 프롬프트 | 기존 |
| R4 동작 동일 | H4 동결한 원본 `_clean`과 생성 문자열(유니코드 공백·쉼표·비문자열) 비교 | 프롬프트 | 신규 |
| R5 복제 본문 없음 | H5 패키지 안 비테스트 함수들의 정규화 AST 본문이 서로 같지 않음 | "중복 제거" | 신규 |
| R6 공개 test 통과·불변 | H6 | 프롬프트 | 기존(불변은 신규) |
| R7 공개 API 불변 | H7 `to_csv`·`to_json` 시그니처 | 프롬프트 | 신규 |

기존 hidden은 `'common' in s`와 `'def clean' not in s`의 문자열 검사라서 **인라인 복제본+주석 한 줄로 통과한다**(§5.3 P6). H2가 이를 막는다. 필수 reject는 P6 변형, common 파일만 만들고 미사용, `_clean`을 `_clean2`로 이름만 변경, 두 모듈 중 하나만 이전. 참조해는 `sol_dedupe` 재사용.

### 3.7 B4-TST-D 테스트 작성 (apply_discount)

기존 `t3-write-tests`. 프롬프트가 정상값, 경계 0·100, 범위 밖, 소수 반올림, `shop/discount.py` 수정 금지를 명시한다. 현 verifier는 mutant 3개(`>=100`, `<=0`, 반올림 1자리)와 정규식 `def test_` 개수를 센다.

| 요구 | hidden | 출처 |
|---|---|---|
| R1 수집된 테스트가 4개 이상 | H1 `unittest` loader의 `countTestCases`, 정규식 대신 사용 | 신규 |
| R2 **gold에서 통과** | H2 | 신규. 현 `hidden_ok`는 gold 통과를 요구하지 않음(§5.3 P4) |
| R3 경계 0·100 | H3 mutant M1 `>=100`, M2 `<=0` 사망 | 기존 |
| R4 범위 밖 | H4 M4 상한 검사 제거, M5 하한 검사 제거, M7 잘못된 예외형(TypeError) 사망 | 신규 |
| R5 소수 반올림 | H5 M3 `round(...,1)`, M6 반올림 제거 사망 | 기존+신규 |
| R6 공식 mutant 전체 사망 | H6 M8 `price*percent/100`(공식 뒤바뀜) 포함 8/8 | 신규 |
| R7 우회 금지 | H7 skip·expectedFailure 0건(runtime 결과와 정적 AST 모두), 각 test가 `apply_discount` 호출을 포함하는 assertion을 1개 이상 가짐, assertion 없는 test·SUT를 import하지 않는 test 실패 | 신규 |
| R8 SUT 불변 | H8 `shop/discount.py` hash 동일 | 신규 |

각 mutant은 프롬프트의 요구문 하나에 대응해야 하며, 동치 mutant는 사전에 CPU로 판별해 제외 이유를 기록한다. mutant 파일과 목록은 evaluator 쪽에만 둔다. 필수 reject: 항상 실패하는 test(M 사망만으로는 안 됨, R2), 전부 skip, assertion 없는 test, 함수 호출 없이 상수 비교, SUT 수정, `tests/test_discount.py` 위치 위반. 참조해는 `sol_tests` 재사용+대안 참조해 1개.

### 3.8 B4-REC-D 오류 후 자율 수정 (shop)

기존 stage1 fixture(`task.txt`: 장바구니 합계 버그 수정, `apply_discount` 0~100 밖은 ValueError, `tests/test_pricing.py`에 -1·101 테스트 추가). `pricing.py` docstring이 half-up과 범위를 명시한다.

**내장 trap(산술 확인함):** 가장 흔한 첫 수정 `round(price*(100-percent)/100)`은 Python 은행가 반올림 때문에 `apply_discount(1005,50)`에서 502.5→**502**를 내서 공개 test `==503`을 실패시킨다. 첫 patch 뒤 `run_tests`가 실제로 실패하고 모델이 정수 산술 등으로 다시 고쳐야 한다. 별도 오류 주입(스크립트된 CI 메시지, 도구 오류 주입)은 harness 구현이 필요하므로 v1 범위가 아니다. **이 trap은 이 Run에서 CPU로 확인했다**(`evidence/4.1/verify_delta_and_trap_output.json`): 미수정 base의 공개 unittest는 3건 실패, cart 수정과 `round()` 패치(NP1)를 함께 적용하면 `FAILED (failures=1)`이고 실패는 `503`을 기대하는 half-up test이며, 정수 산술 `(p*(100-pct)*2+100)//200` 패치는 3건 OK다. 단 이는 trap이 존재한다는 확인일 뿐 어떤 모델이 이 경로를 밟는지는 모른다. holdout REC-H에도 같은 방식의 사전 확인을 4.2에서 요구한다.

과거 `verify_fixture.py`의 두 checker는 서로 다르다. 과거본(`verify_fixture.py.orig`, sha256 `9ed432cb…`)은 `new_test_covers_bounds`가 문자열 존재만 검사해 주석 한 줄(`# ValueError -1 101`)만 있는 test도 PASS했고, 현행본(`003d178f…`)은 실제 discovery와 경계 mutation을 요구해 이를 FAIL시킨다(`verifier_case_summary.csv`). **현행본도 final-state checker여서 이미 정답인 workspace와 유효한 test가 있으면 no-op을 PASS한다.** 따라서 REC는 §1-7·§1-8의 reset·delta 규칙 없이는 수락 근거가 될 수 없다. change class `diff_required`.

| 요구 | hidden | 출처 |
|---|---|---|
| R1 subtotal이 수량을 곱함 | H1 `subtotal()==850`, `total(10)==765` | 기존 `verify_fixture.py` |
| R2 discount half-up | H2 `(999,15)=849`, `(1005,50)=503`, `(1,50)=1`, `(200,100)=0`, `(200,0)=200` | 기존 |
| R3 범위 밖 ValueError | H3 -1, 101, 추가로 -100, 1000 | 기존+신규 |
| R4 새 test가 범위를 실제로 검사 | H4 후보 test를 런타임 mutant(범위 밖 호출이 정상 반환)에 돌려 사망, assertion 기반 | 기존 `verify_fixture.py`(수정본) |
| R5 gold에서 후보 test 통과, 전체 test 통과 | H5 | 신규 |
| R6 기존 `test_cart.py` 불변 | H6 | 신규 |
| R7 복구 trace | H7 (측정 항목, 성공 판정 아님) 첫 patch 뒤 실패→재편집→통과 순서를 trace에서 판정해 `trap_activated`, `recovered`, 재시도 비용을 기록 | 설계 §5.1 |

과거 stage1 fx-improved-3의 14턴/994.73초/6 tests는 감사가 `WORK_LOG.md`와 `.runtime/logs/worker-runs.jsonl`의 `a0c73b28b621` 행으로 두 단계 추적한 **실제 당시 worker 영수증**이다(14 turns, `Bash=5/Read=5/Edit=3`, 수정 전 3 failure→수정 후 6 OK). 그러나 작은 Python 과제 1건의 사례이므로 이 과제나 다른 과제로의 성공 확대 해석을 금지한다. comment-only false positive는 이미 고쳐진 최종 fixture에서 경계 test 내용을 주석으로 바꿔 재검증할 때 생긴 것이라 과거 코드 실행 결과를 취소하지 않고 "새 경계 test가 실제 assertion이었다"는 세부 판정만 신뢰하지 않는다.

### 3.9 B4-LNG-D 장시간 다중 턴 (composite-A)

workspace: `textutil`(slugify fixture), `billing`(rename fixture), `stats`(median fixture)를 서로 겹치지 않는 패키지로 합친 것에 L1의 필러 모듈 26개를 읽기 전용 decoy로 추가한다. 공용 `tests/__init__.py`는 하나, test 파일 이름은 겹치지 않는다. 합성은 4.2 작업이다(이 문서는 구현하지 않음).

사용자 턴은 고정 문안으로 스크립트한다(문안 hash를 manifest에 등록). 턴 사이에 모델이 보는 사용자 메시지는 이전 턴의 평가 결과에 의존하지 않는다.

| 턴 | 사용자 요구(요지) | 요구 |
|---|---|---|
| T1 | median 버그 수정. **`tests/test_stats.py`는 이후 모든 턴에서 수정 금지**(지속 제약 C1) | R1 median 정상 동작(SGL-D의 H1~H5 부분집합), R2 C1 선언 |
| T2 | `slugify`에 `max_len` 추가, CLI `--max-len N`, `tests/test_slug.py`에 max_len 테스트 추가 | R3 `max_len` 동작(끝 `-` 없음), R4 CLI 전달, R5 새 test 실행·mutant 사망 |
| T3 | `calc_tax`→`compute_tax` 이름 변경(정의·import·호출·export·tests) | R6 이름 잔존 0, R7 동작 불변 |
| T4 | `billing.invoice.invoice_label(amount, text, max_len)` 추가: T3의 `compute_tax`와 T2의 `slugify(max_len=)`를 써서 `"{slug}:{total}"` 반환 | R8 두 이전 변경에 의존하는 호출(치환 검사), R9 동작, R10 `calc_tax`·median 회귀 없음 |
| T5 | 지금까지 변경을 파일별 JSON으로 보고 | R11 보고 파일 목록이 실제 diff와 일치, 없는 파일 언급 없음, R12 C1(`test_stats.py` hash 불변) |
| 세션 | T3 완료 직후 중단, 같은 session lineage로 resume 후 T4 진행 | R13 resume 이어짐 확인, **T4의 첫 write/replace 이벤트 직후 worker를 취소하고 resume** 후 R14 workspace 일관(모든 `.py` 컴파일, 부분 파일 없음) 및 T4 완료 |

설계 §5.5의 장기 과제 한도 30분·60턴을 따른다. 컨텍스트 압축 발생 여부는 harness가 지원하는지 **확인 필요**이며, 강제로 일으키지 않고 발생·미발생을 기록해 층화한다. resume이 지원되지 않는 profile은 `resume_unsupported`로 기록하고 fresh-context 이어가기를 별도 층으로 둔다. 취소 지점 이벤트 훅은 harness에 있는지 **확인 필요**(없으면 시간 기반 취소로 대체하고 그 사실을 기록).

hidden은 각 턴 종료 시점의 workspace snapshot을 evaluator가 복사해 별도로 검사한다(모델에게 보이지 않음). 최종 성공은 T1~T5 turn 판정과 세션 판정이 모두 PASS인 경우만이다. 한 턴이 실패해도 다음 사용자 턴은 같은 스크립트로 계속 진행하고, 턴별 판정은 따로 기록한다.

필수 reject: T1 제약 위반(test_stats.py 수정), T3에서 별칭 `calc_tax=compute_tax` 잔존, T4가 `compute_tax`를 호출하지 않고 값을 복제, T5 보고에 존재하지 않는 파일, 취소 뒤 반쯤 쓴 파일.

## 4. holdout 9개 명세와 보호

### 4.1 holdout 구조 명세 (내용은 sealed)

이 문서는 저장소에 있으므로 holdout의 도메인·상수·정답·mutant·기대값을 쓰지 않는다. 아래는 짝 개발 과제와 같게 맞출 **구조 제약**만 적는다.

| ID | 짝 | 구조 제약 (sealed 번들에 상세) |
|---|---|---|
| B4-EXP-H | EXP-D | 파일 25~30개, 변환 지점 2곳 중 하나가 비직관적 위치(레거시 하위 패키지), 이름이 비슷한 decoy 2개 이상, 호출자 3개 이상. 읽기 전용 |
| B4-SGL-H | SGL-D | 파일 1개, 경계·빈 입력·입력 비수정 요구 각 1개 이상, 공개 test는 요구 일부만 커버 |
| B4-MUL-H | MUL-D | 소스 2파일 이상, 공개 test에 드러나지 않는 두 번째 경로(레거시)가 있고 프롬프트에는 모든 요구가 서술됨 |
| B4-API-H | API-D | 같은 WSGI 방식, 다른 리소스, 충돌(409) 또는 용량 제약, 페이징 검증, 원자 저장 요구 포함 |
| B4-DBG-H | DBG-D | 실제 CPU 실행으로 만든 failure log, 최상단 frame이 원인이 아니라 호출자 쪽을 가리킴(원인은 2 frame 이상 아래), 설명-패치 대응 판정 포함 |
| B4-REF-H | REF-D | 호출자 3곳 이상, 외부 동작·부작용(호출 횟수 등)이 유지돼야 하고 치환 검사 가능 |
| B4-TST-H | TST-D | SUT에 경계 3개 이상(구간 요금표 등), mutant 8개, 동치 mutant 사전 판별 |
| B4-REC-H | REC-D | 언어·표준 라이브러리의 흔한 오해(반올림 모드·음수 나눗셈 등) 때문에 첫 수정이 공개 test에서 실패. naive patch가 실패함을 CPU로 사전 확인 |
| B4-LNG-H | LNG-D | 5턴·지속 제약 1개·중단/취소 1회·턴 간 의존 호출 1개, 개발과 다른 지속 제약 종류 |

holdout 요건: (1) 요구↔hidden 매핑표와 gold·alt-gold·mutant·negative control을 개발 과제와 같은 수준으로 작성한다. (2) 작성한 컨텍스트와 이후 profile을 튜닝하는 컨텍스트를 분리한다. (3) 어떤 모델·harness 프로필도 실행한 적이 없음을 `ledger`에 기록한다.

### 4.2 holdout 정답을 어디서 보호하나 (제안, 구현 미착수)

위험 지점: 이 back 저장소와 `qwen-local-harness/bench`는 같은 계정의 다른 Run·클라우드 worker가 읽을 수 있고, 개발 과제의 gold는 이미 `validate_tasks.py`에 있다. 로컬 모델이 실행되는 Seatbelt 프로필이 이 경로들을 **읽기 거부**하는지는 이 Run에서 확인하지 않았다(**확인 필요**).

1. **sealed 번들 위치(제안값, 4.3에서 확정):** 저장소·worktree·`~/.ttakkari` 밖의 디렉터리 하나, 예) `/Users/winterholic/bench-sealed/local-llm-holdout-r1/`, 모드 0700. `~/.ttakkari` 아래는 원격 자동 Run의 보호 영역이라 evaluator도 읽지 못하므로 쓰지 않는다. 번들 내용은 `hidden/`, `gold/`, `alt_gold/`, `mutants/`, `negative_controls/`, `eval_keys/`, `manifest.sealed.json`.
2. **저장소에는 commitment만 둔다:** 번들 각 파일의 sha256과 번들 전체 hash, 과제·검사 개수. 내용·정답·상수는 두지 않는다. 이 문서와 manifest의 `sealed_bundle_ref`는 지금 `null`이다.
3. **모델 workspace에는 `public/`만 복사한다:** 프롬프트, 공개 코드·공개 test, base commit 1개만 가진 새 `git init`(원격·reflog·gold 객체 없음). hidden·gold·mutant·evaluator 경로는 workspace의 어떤 파일·환경변수·도구 설명에도 나타나지 않는다.
4. **접근 차단 검증은 모델 없이 CPU로 한다(4.3):** worker의 Seatbelt/read roots가 sealed 디렉터리와 `qwen-local-harness/bench`, back 저장소를 읽지 못하는지, 네트워크가 막혀 있는지 deny 테스트로 확인한다. 이 Run에서는 실행하지 않았다. 차단이 확인되지 않으면 holdout 평가를 시작하지 않는다.
5. **더 강한 대안(확인 필요):** 별도 OS 사용자 또는 평가 때만 마운트하는 암호화 이미지. 이 호스트에서 가능한지 확인되지 않았다.
6. **evaluator는 모델 프로세스가 끝난 뒤에만 번들을 연다.** 코딩 모델과 동시에 번들을 읽는 경로가 없다.
7. **holdout은 profile당 1회용:** 튜닝 쪽에는 집계 pass 수만 돌려주고 과제별 실패 상세·로그는 최종 판정 전까지 봉인한다. 상세를 열어 디버깅하면 해당 holdout revision은 폐기하고 새 revision을 만든다(설계 §5.1과 같음).
8. **개발 과제 쪽 오염 규칙:** 프롬프트·시스템 문구·도구 설명을 수정할 때 개발/holdout fixture의 hidden 전용 리터럴과 gold 전용 식별자(예: `ROUND_HALF_UP`)가 들어가면 자동 경보를 내는 leak scan을 둔다. 일반 지침은 허용하되 특정 fixture의 해법 토큰은 금지한다.

## 5. 독립 평가 경계와 실패 분류

### 5.1 평가 절차 (제안)

0. **시작 전 reset과 hash 고정(§1-7).** attempt마다 새 디렉터리에 pristine buggy fixture(`__pycache__`·`.git` 이력 없는 단일 base commit)를 복원하고 base tree hash와 파일별 sha256을 ledger에 기록한다. sealed 기준값과 다르면 모델 요청 없이 `INFRA_BASE_MISMATCH`로 attempt를 무효 처리하고 소요 시간을 기록한다. 종료 시점 파일별 sha256도 기록한다.
1. 모델 프로세스 종료 확인(프로세스 그룹 부재) 뒤 workspace를 snapshot하고 `git diff --binary`로 **시작 snapshot(위 0) 대비** patch를 만든다. patch hash를 기록한다. `diff_required` 과제에서 patch가 비어 있으면 `FAIL_NOOP`이고, `read_only_report` 과제에서는 patch가 report 외 파일을 건드리면 `FAIL_FORBIDDEN_WRITE`다(§2.1).
2. 새 평가 worktree를 base tree에서 만들고 patch를 적용한다. patch가 `tests/`의 동결 파일, `.git`, `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`, `pytest.ini`, evaluator 경로를 건드리면 `FAIL_FORBIDDEN_WRITE`.
3. 동결 공개 test를 canonical 사본으로 복원해 실행하고, 그 뒤 sealed hidden을 복사해 실행한다. 모델이 쓴 `tests/`를 hidden 판정에 쓰지 않는다(TST 과제의 산출 test는 예외로 §3.7 방식으로만 사용).
4. **종료 코드만 믿지 않는다.** hidden 러너는 nonce와 실행한 검사 수를 `HIDDEN_DONE <nonce> <n>` 형태로 출력하고, 기대 검사 수와 일치할 때만 성공이다. 줄이 없는 exit 0은 `FAIL_STUB_EXIT0`. unittest는 custom result로 수집 수·skip·expectedFailure를 읽는다.
5. 평가 프로세스는 `python -I`로 실행해 환경변수·user site·cwd 경로를 줄이고, 네트워크 없음, 시간 제한, 프로세스 그룹 kill. 결과 receipt는 workspace 밖에 쓴다.
6. 코딩 모델과 다른 검증 컨텍스트가 patch와 원시 명령/출력을 확인한다. LLM judge는 보조 설명 평가에만 쓰고 pass/fail에 관여하지 않는다.
7. 구조화 보고(`status`, `changed_files`, `commands_run`, 과제별 필드)는 trace·diff와 대조한다. **대조 불일치는 `FAIL_REPORT_MISMATCH`이며 성공이 아니다.** 설계 §5.2의 "success는 public+hidden+요구행동+보고 근거가 충족된 경우에만"을 따른다(v1 결정). 코드만 PASS인 시도는 별도 열 `code_pass_report_mismatch`로도 센다.
8. **시도 가치 판정 순서:** (a) 시작 hash 일치, (b) 금지 경로·우회 검사, (c) delta·invariant 검사, (d) 보고 대조. (a)가 틀리면 모델 평가가 아니다.

### 5.2 verdict와 실패 분류

verdict는 `PASS`, `FAIL`(모델 원인), `INFRA`(원인이 입증된 비모델 장애), `UNKNOWN`(원인 불명)이다. UNKNOWN은 임의로 INFRA로 제외하지 않는다. 결측은 0으로 채우지 않는다.

| code | 의미 |
|---|---|
| FAIL_NOOP | 변경 없음 또는 빈 patch |
| FAIL_COMMENT_ONLY | 주석·문자열만 추가해 키워드 충족 |
| FAIL_PARTIAL_PATHS | 필요한 파일·경로 일부 누락 |
| FAIL_TEST_TAMPER | 기존 test 삭제·약화·기대값 수정 |
| FAIL_SKIP_XFAIL | skip·xfail·expectedFailure 도입 |
| FAIL_STUB_EXIT0 | exit 0 stub, 검사 수 handshake 부재 |
| FAIL_CHECKER_PATCH | checker monkeypatch, sitecustomize·conftest·검사 우회 |
| FAIL_MISSING_TARGET | 없는 파일·함수·경로 인용 또는 대상 없음 |
| FAIL_EMPTY_RESULT | 빈 report·빈 출력·수집 0건 |
| FAIL_FORBIDDEN_WRITE | 금지 경로 수정 |
| FAIL_PUBLIC / FAIL_HIDDEN / FAIL_HIDDEN_ONLY | 공개 실패 / hidden 실패 / 공개만 통과 |
| FAIL_REGRESSION | 기존 동작 회귀 |
| FAIL_CAUSE_PATCH_MISMATCH | 원인 설명과 패치 불일치(DBG) |
| FAIL_FALSE_DONE | 완료 보고했으나 위 FAIL 중 하나(플래그, 중복 계수 아님) |
| FAIL_REPORT_MISMATCH | 보고가 trace·diff와 불일치 |
| INFRA_BASE_MISMATCH | 시작 hash가 sealed 기준과 다름(무효 attempt, 비용은 기록) |
| INFRA_* / TIMEOUT / GUARD_STOP / TELEMETRY_STALE / UNKNOWN | 비모델 원인 또는 미상. HTTP status·finish reason·error stage 보존 |

**API·비모델 오류의 회계(1.2 감사 불변식):** 과거 C_opt의 `api_error` 26행(3+3+20: 양의 경과시간·응답 없음 3, 도구·completion 관측 3, wall 0초·응답 없음 20)은 raw에 HTTP status·body·server 식별자가 없어 **원인 unknown**이다. 같은 방식으로 원인 증거(HTTP status, error stage, server run/instance ID, 요청 body hash)가 없는 오류는 `UNKNOWN`으로 두고 임의로 INFRA로 제외하지 않는다. wall 0초 행은 비용에 포함하되 `zero_wall_no_response` 플래그를 달아 별도 집계한다(성공·실패·무시 어느 쪽으로도 조용히 처리하지 않는다). 재시도는 새 분모가 아니라 같은 `(task, repeat)` 키의 추가 비용이다(§6.4).

### 5.3 negative control과 기존 verifier probe 증거

**필수 negative control 세트(4.2에서 모델 실행 전에 모두 수행):** N1 no-op, N2 주석·키워드만, N3 한 파일 누락, N4 test 삭제, N5 skip/xfail, N6 exit 0 stub, N7 checker monkeypatch(sitecustomize·conftest·`TestCase.run` 교체), N8 대상 없음·없는 경로 인용, N9 빈 결과·빈 report, N10 금지 경로 수정, **N11 이미 정답인 workspace(시작 hash 불일치로 무효, 또는 pristine에서 no-op이면 FAIL_NOOP)**, **N12 주석에만 경계 조건을 적은 test(assertion 없음)**, **N13 빈 test 파일·수집 0건**, **N14 출력 정규화 누락(`5 x 2000`)**. 18과제 각각에 해당하는 N을 manifest `required_negative_controls`에 둔다. 양성 대조: gold와 alt-gold는 PASS해야 한다. 과제당 reject해야 할 부분 patch 목록은 §3의 "필수 reject"이다.

**1.2 감사가 확정한 사례의 매핑(`verifier_case_summary.csv`, `checker_mutations.csv`):**

| 감사 사례 | 과거 checker | 현행 checker | 이 명세의 negative control |
|---|---|---|---|
| `comment_only_correct_baseline`(경계 test 내용을 주석 `# ValueError -1 101`로 교체) | PASS(false positive) | FAIL | N12, REC·TST 필수. 평가기는 실제 discovery와 mutant 사망으로만 인정 |
| `no_task_change_buggy_baseline`(코드 미수정+주석) | FAIL | FAIL | N1, N2 |
| `no_op_already_satisfied_with_valid_tests` | PASS | PASS | **N11**, §1-7·§1-8로만 막힘. 두 checker 모두 막지 못함 |
| `valid_tests_correct_baseline` | PASS | PASS | 양성 대조(gold) |
| `test_file_deleted`, `test_file_missing` | FAIL | FAIL | N4, N3 |
| `empty_test_file` | FAIL | FAIL | N13 |
| `pricing_file_missing`(필수 source 누락) | FAIL | FAIL | N8(대상 없음), N3 |
| raw 60행 `s3-fix-inventory`(`5 x 2,000` 정확 일치 실패) | n/a | n/a | N14=`NC-A5` |

위 감사 사례는 stage1 shop fixture의 checker를 대상으로 한다. 이 Run의 §5.3 probe 14건은 `tasks.py` 계열 verifier를 대상으로 한 별개 검사이며, 둘의 결과를 하나의 verifier에 대한 결과로 합치지 않는다.

**이번 Run의 모델 없는 probe(실제 실행, `evidence/4.1/`):** 기존 verifier(`tasks.py` HEAD 상태)를 /tmp 복사본에서 돌렸다. 환경은 Python 3.13.15, macOS 27.0, arm64이며 호출 방식은 `cd /tmp/bench-probe-4-1 && PYTHONDONTWRITEBYTECODE=1 python3 verifier_probe_existing.py /tmp/bench-probe-4-1/b`다.

| probe | 대상 | 결과 | 해석 |
|---|---|---|---|
| P1 no-op ×5 | inventory, median, dedupe, long-file, write-tests | 5건 모두 reject. 단 **long-file은 `unittest_ok=True`**(공개 test가 미수정에서 통과) | §3.5 fixture 결함 |
| **P2 exit0-init** | inventory, `inv/__init__.py`가 `os._exit(0)` | **`hidden_ok=True, unittest_ok=True` (false positive)** | 종료 코드만 보는 verifier가 no-op 수준 patch를 통과시킴. 설계 §5.2의 "exit0 stub" 위험이 실제 재현됨 |
| P3 sitecustomize exit0 | inventory | reject | 이 호출 방식에서는 cwd의 sitecustomize가 import되지 않았음. 다른 경로·환경에서의 일반화 금지 |
| P4 항상 실패하는 test 3개 | write-tests | `hidden_ok=True`, `unittest_ok=False`(종합은 reject) | hidden 성분이 gold 통과를 요구하지 않음. 종합 판정이 `unittest_ok`에 기대고 있음 |
| P5 전부 skip | write-tests | mutant 0/3 사망, reject | |
| **P6 dedupe 인라인 복제+주석** | dedupe | **`hidden_ok=True, unittest_ok=True` (false positive)** | 문자열 검사 verifier |
| P7 키워드 나열(한글 4자 연속 없음) | explain-review | reject | |
| **P7b 키워드 나열(연속 한글 포함)** | explain-review | **false positive** | 정규식 키워드 verifier. 이 과제를 수락 근거에서 제외 |
| P8 빈 todo_report | shell-count | reject | |
| P9 한국어 보고만 | korean-docs | reject | |

false positive는 14건 중 3건(P2, P6, P7b)이다. 이는 **verifier 결함의 존재 증거**이지 과거 모델 결과의 오염 정도를 보여주지 않는다. 과거 성공 행 중 이런 경로로 통과한 것이 있는지는 감사 범위에 없었고 열려 있다(§8).

### 5.4 요구 검사의 base 상태 확인과 참조해·누출 검사 (이 Run, 모델 없는 CPU)

원스크립트·원출력은 `evidence/4.1/`에 있다. 환경: Python 3.13.15, macOS 27.0, arm64. 호출은 /tmp 복사본에서 `PYTHONDONTWRITEBYTECODE=1 python3 <script> <bench/2026-10-18 복사본> <stage1 경로>` 형태다. 소스 sha256은 §7과 같다.

1. **참조해 재검증(`validate_tasks.py`):** 11개 기존 과제 모두 `untouched_fails=True solution_passes=True`, 마지막 줄 `BAD 0`. 이는 기존 hidden에 대한 확인이며 **개정된 hidden·프롬프트(rev2)·신규 과제의 참조해 검토는 4.2에 남는다.** `untouched_fails`는 hidden+unittest 합산이고 `t4-long-file`은 공개 unittest 단독으로는 green이다(§3.5).
2. **delta/invariant 확인(`verify_delta_and_trap.py`):** 개발 과제의 선택한 검사 11건이 모두 기대대로다. delta(base red, 참조해 green): SGL.H1·H2·H5, MUL.H1·H3, REF.H1. invariant(양쪽 green): SGL.H3·H4·H7, MUL.H5, REF.H4. 다른 H 검사, API·TST·LNG 전체, holdout 전체는 구현 전이라 **미확인**이며 4.2에서 같은 방식으로 확인해야 한다. shop trap 확인은 §3.8.
3. **정적 누출 스캔(`leak_scan_dev.py`, 자동 추출, 완전하지 않음):** hidden 코드 문자열에서 추출한 리터럴(문자열 4자 이상, 정수 100 이상)이 프롬프트·비테스트 소스·공개 test에 나타나는지 봤다. SGL·MUL에서는 프롬프트·비테스트 소스 hit이 없다(MUL은 공개 test의 `100` 하나가 overlap). TST·REF·DBG·REC의 hit은 전부 프롬프트가 이미 명시한 식별자·파일명(`test_discount.py`, `clean`, `common.py`, `rules.py`), 소스에 원래 있는 `def rule_` 같은 구문 조각, 또는 docstring이 명시한 범위(100, 101)이며 기대값 누출로 보이지 않는다(자동 분류를 사람이 읽고 판정). REC는 공개 `test_cart.py`가 hidden과 같은 값(849, 503, 850, 999, 1005)을 포함하는데 이는 docstring의 half-up 규칙에서 도출되는 spec 기반 중복(public_test_overlap)으로 분류한다. **한계:** 정수 100 미만(63, 27 등)과 짧은 문자열은 스캔 밖이므로 누출이 없다는 증명이 아니다. 4.2에서 hidden 전체 리터럴과 gold diff hunk를 대상으로 확장하고 holdout에도 적용한다.

## 6. 실행 프로토콜 고정값 (사전등록 계획값, 실측 아님)

측정 전에 확정하고 결과를 본 뒤 유리하게 바꾸지 않는다. 변경하면 프로토콜 새 revision으로 baseline부터 다시 비교한다. 4.3이 최종 확정한다.

### 6.1 반복·seed·표본

| 항목 | 계획값 |
|---|---|
| 정식 표본 | 18과제×3반복=54시도/profile. 개발 9×3=27, holdout 9×3=27 |
| 파일럿 | B4-SGL-D, B4-MUL-D, B4-TST-D 각 2회=6시도(호환성·verifier 오류 탐지용, 수락 근거 아님. 과거 165.0초·239.7초와 비교하지 않음, §2) |
| 반복 id | r ∈ {1,2,3} |
| seed | `seed = r*100 + turn_index`(과거 `agentbench.py`의 호출 규칙과 연속). seed를 지원하지 않거나 적용을 읽어서 확인하지 못한 엔진은 seed 통제를 주장하지 않는다 |
| 같은 pair | 동일 task×r×seed를 FlashNext와 후보에 같은 prompt hash·tool 집합으로 준다 |
| 순서 | task 순서는 `sha256(task_set_revision)`로 시드한 순열을 모든 profile이 공유. profile 간 AB/BA는 r의 홀짝으로 교차 |
| 시간 블록 | 3반복은 서로 다른 시간 블록. 같은 서버 프로세스를 공유(적재 비용을 반복마다 3배 하지 않기 위함). 서버 재시작 반복은 baseline의 noise floor 측정에만 선택적으로 사용(v1 결정: 정식 표본은 profile당 1회 적재, 4.3이 재확인) |

### 6.2 출력·시간·thinking 예산 (설계 §5.5 초기안과 동일)

| 항목 | short 과제(8유형) | long 과제(LNG) |
|---|---|---|
| 벽시계 상한 | 10분(600초) | 30분(1800초) |
| 모델 호출 턴 | 20 | 60 |
| 호출당 `max_tokens` | 4096, **reasoning 토큰 포함** | 4096 포함 |
| 도구 명령 timeout | 30초(공개 test 실행 선례) | 30초 |
| 도구 결과 truncation | 모든 arm에서 같은 상한을 기록하고 동일하게 적용 | 동일 |
| 자동 재시도 | 0. 증명된 INFRA에만 최대 1회, 별도 attempt 행(`retry_of`)이며 비용에 포함하고 완료 수에 중복 합산하지 않음 | 동일 |

과거 `agentbench.py`는 timeout 시 `end="timeout"`으로 취소했다. 상한 초과와 cleanup 시간은 따로 기록하며 timeout을 성공으로 세지 않는다.

thinking: 주 arm A는 thinking 끄기(`enable_thinking=false`). 보조 arm B는 `low`이며 thinking 토큰 상한 1024를 엔진이 지원하고 요청 적용을 응답에서 readback으로 확인할 수 있을 때만 실행한다(**확인 필요**). 적용 확인이 안 되는 엔진·모델은 해당 arm을 실행하지 않고 비교 주장을 하지 않는다. A와 B는 별도 표다. 모델 권장 sampler arm도 별도 표(설계 §5.1의 공통 기능 비교 대 엔진 특화 비교 구분).

sampling: temperature 0.6(과거 선례), 그 밖의 top_p·top_k는 엔진 기본값을 기록하고 두 arm에서 같아야 한다. 이 값은 사전등록 계획값이다.

### 6.3 cache 조건

- 비교의 주 조건은 "서버 적재·워밍 완료, 과제 간 prompt cache 없음". 과제가 바뀔 때 새 대화·새 slot을 쓰고, 엔진이 slot 비우기를 지원하면 호출한다. **OS file cache는 비우지 않는다**(공유 호스트 전역 상태).
- 한 과제 안에서 턴 사이의 prefix 재사용은 운영과 같으므로 허용하고 `cache_prompt` 설정과 호출별 prompt/cached 토큰(예: `timings.cache_n`, `prompt_n`)을 기록한다.
- 층화 변수: 적재 후 첫 요청 여부(cold_load_first_request), 새 prefix(warm_new_prefix), prefix 적중 비율 구간. 모델 적재 시간은 campaign 분모에 포함하되 별도 행(load→ready)으로 기록한다.
- 시작·종료 시점에 `vm_stat` page-ins·swap used·available·pressure를 기록한다. 압박 중단 기준은 설계 §5.5를 그대로 따른다(available≥16GiB 등, 기존 guard를 완화하지 않음).

### 6.4 전체 비용 기록 (attempt ledger 필드)

attempt 행은 변경하지 않는 기록이다. 1.2 감사가 지적한 raw 결측(HTTP status, server run/instance ID, model artifact hash가 attempt에 없음)을 막기 위해 **아래 필드는 결측 불가**이고, 비어 있으면 그 attempt는 비교 표에 들어가지 않고 `UNKNOWN`으로 남는다. 필드: `attempt_id, task_id, split, profile_id, repeat, seed_scheme, prompt_sha256, workspace_base_sha, base_tree_sha, file_sha256_at_start{path: sha256}, file_sha256_at_end{path: sha256}, server_run_id, server_instance_id, model_artifact_sha256, engine_build, config_digest, enqueue_ts, lease_acquire_ts, load_start_ts, ready_ts, first_request_ts, first_token_ts, last_token_ts, tool_time_s, verify_time_s, cleanup_time_s, retry_of, end_reason, verdict, fail_code, trace_sha256, patch_sha256, evaluator_sha256, sealed_bundle_sha256(holdout만), http_status/finish_reason/error_stage per call, prompt_tokens/cached/completion/reasoning tokens, telemetry samples ref`. 분해: queue, acquire, load→ready, TTFT(first request→first streamed token; `prompt_tokens/pp_rate`로 대체 금지), prefill, decode, tool/명령, 검증, 복구·재배정, end-to-end. 모델 선택 시간과 handoff 시간도 campaign 비용에 포함한다.

완료 비용 정의는 설계 §5.3을 그대로 쓴다: `campaign 전체 경과시간 / 검증된 고유 작업 완료 수`. 분모는 evaluator의 PASS만 센다. 성공이 0이면 N/A와 총비용을 표시한다. 모델 비교는 local-only, cloud fallback 없음.

**회계 불변식(1.2 감사에서 채택):**
1. attempt 행 1개는 ledger 1행이다. 중복·재시도·0초·API 오류·timeout·truncation 행을 제외하지 않고 행 단위로 보존한다(과거 C_opt는 61행 전부 manifest에 있고 제외 0행).
2. 성공 분모는 설정(profile)별 고유 `(task, repeat)` 키다. 재시도는 새 분모가 아니라 같은 키의 추가 비용이다. 서로 다른 설정 사이에 task×repeat가 겹치므로 설정을 가로질러 성공 행을 독립 표본 수나 하나의 성공률로 합치지 않는다(과거 성공 raw행 29개는 그렇게 합산하지 않았다).
3. 비용에는 실패와 재시도 시간이 포함된다. 성공 시도만의 평균을 오류 비용 포함 지표 대신 쓰지 않는다(과거 t8: 오류 포함 성공당 239.7초 vs 성공 시도 평균 184.7초).
4. 병렬 실행에서 task wall 합과 batch elapsed를 구분하고 throughput 시간은 batch elapsed를 쓴다(과거 conc2: 개별 wall 합 589.1초는 경과시간이 아니고 batch 310.1초, 직렬 대조 254.4초).
5. E2E는 setup, 서버 start→ready, load, agent loop, cleanup, 독립 hidden/mutation 채점, 재시도 비용을 나누어 기록하고 전체 wall을 함께 낸다. generation tok/s와 코딩 E2E는 다른 지표다.

### 6.5 정량 gate 인용

성공률 90% 이상, 완료당 E2E 20% 이상 감소, peak 실행 메모리 30% 이상 감소 등은 설계 §5.4의 **사전 등록용 제안**이며 이 초안이 새로 정한 값이 아니다. 54시도는 통계적 비열등성을 보장하는 크기가 아니다. 구간이 넓으면 "미확정"으로 두고 유형별 반복을 추가한다.

### 6.6 보고 스키마와 trace 요구

구조화 보고 공통 필드: `status`, `changed_files[]`, `commands_run[]`, `notes`. 과제별 추가: EXP는 report 파일, DBG는 `root_cause`, LNG의 T5는 파일별 변경 JSON. 모델의 "완료"와 evaluator verdict는 독립이다. trace에는 실제 검색→읽기→편집→명령 이벤트와 시각이 남아야 한다. 답변 코드만 내거나 trace가 없는 시도는 성공이 아니다.

### 6.7 결정 사항 (v1)

v1에서 정한 것:

1. **서버 재시작:** 정식 표본은 profile당 1회 적재하고 3반복은 서로 다른 시간 블록으로 둔다. 재시작 반복은 baseline의 noise floor 측정에만 선택적으로 쓴다. 4.3이 재확인한다.
2. **보고-diff 불일치:** 실패(`FAIL_REPORT_MISMATCH`)로 센다(§5.1-7). 설계 §5.2의 성공 정의를 따른 것이다.
3. **DBG의 재현 전 편집:** 성공 판정의 gating이 아니라 측정 항목(`reproduced_before_edit`)으로 기록한다. 프로세스를 이유로 정답 patch를 실패시키지 않는다.

이 문서가 정하지 않고 넘기는 것(§9 잔여 과제와 같다):

4. holdout 번들의 최종 위치와 접근 차단 수준. 디렉터리 생성과 Seatbelt read 거부 deny 테스트는 운영자 조치가 필요하다(§4.2).
5. 도구 오류·CI 메시지 주입을 REC 과제에 넣을지. harness 구현이 필요하므로 별도 WBS이며 v1 과제에는 포함하지 않는다.
6. 컨텍스트 압축·resume·이벤트 기반 취소를 harness가 지원하는지. 2.2가 반례 2건으로 재작업 중이므로 v1은 harness 동작에 의존하는 주장을 하지 않고, 지원되지 않으면 해당 층을 `unsupported`로 기록한다.

## 7. 기존 fixture 재활용 매핑

| 기존 자료 | 사용 | 개정 필요 | 출처 해시(sha256) |
|---|---|---|---|
| `bench/2026-10-18/tasks.py` `t1-bug-median`, `t3-write-tests`, `t2-refactor-dedupe`, `t4-long-file` | SGL-D, TST-D, REF-D, DBG-D | TST: gold 통과 요구 추가. REF: 치환 검사 추가. DBG: 공개 test 추가, failure log | `2ca19a31d2501f600d667b10934548f389a69bdae8c69b6f041943914965519c` |
| `bench/2026-10-18/evalbase/stage3.py` `s3-fix-inventory`, `s3-feature-slugify`, `s3-rename-calc-tax` | MUL-D, LNG-D | MUL: 프롬프트 개정. LNG: 합성 | `291343d777f991410f3f7028e378f24c157588f79b16d065f4966e4c646fa8cc` |
| `bench/2026-10-18/validate_tasks.py` | 참조해(`SOL`) 재사용: median, inventory, slug, rename, dedupe, tests, long-file | 정답이 같은 저장소 안에 있으므로 모델이 읽을 수 없게 해야 함(§4.2) | `dcd09bf00d5ae1f4ea3bbdfc345ed32ea151a47043cb9f6ba33770c0f3036718` |
| `bench/2026-10-18-longctx/longtasks.py` `L1-repo-explore` | EXP-D, LNG-D의 decoy | 읽기 전용 과제로 개정, answer key AST 계산 | `a3c4022c7fceca2a732ce0a0fc82dd071da9e29e6353a8badb82546aee7c63aa` |
| `/Users/winterholic/testing/qwen-worker-stage1/fixture-template` | REC-D | 첫 patch trap의 CPU 사전 확인 | `cart.py b088cc83…`, `pricing.py c70d9e01…`, `test_cart.py d6f4d63c…` |
| `…/qwen-worker-stage1/task.txt` | REC-D 프롬프트 | | `3a4849e00d30b83c8bcab9d95d4b8e5b9169436ad410b919ff2974fd5743b42b` |
| `…/qwen-worker-stage1/verify_fixture.py` | REC-D 평가의 mutation 방식 | evaluator 규격으로 이관, final-state checker라는 한계는 §1-7·§1-8로 보완 | 현행 `003d178fc682315bf6dfbd9478a29060d9818b5f2de65a13f29cb25621605584`, 과거 `.orig` `9ed432cb121bfc7f2698362f5b8af80edc45652a2792c48a05799b15cdaa5ec2`(감사 보고서와 일치, v1 작성 때 재계산) |
| `evalbase/qc.py` | 호출 필드 선례(`finish`, `http_status`, `error_phase`) | dirty 파일이라 근거로 삼지 않고 필드 이름만 참고 | `7e678473…`(작업 트리 값, 수정 중) |

## 8. 원자료 감사(WBS 1.2) 결과 반영 현황

1.2 감사가 끝났고 메인이 raw 61행 / 136,608 bytes / SHA256 `d525733d010a2efa284ee6b40a8d5c10c31db1a8fbf8cbbdff2c3733e1362be0`, manifest 61행 coverage, t6 성공당 165.0초·t8 239.7초를 직접 재집계했다. 이 문서는 감사 산출물(`audit_recount.json`, CSV)을 정본으로 읽었고, 아래 항목은 반영 위치를 적는다. 열린 항목은 열린 채로 둔다.

| 항목 | 상태 | 반영 위치·메모 |
|---|---|---|
| 감사 고정 hash와 §7 소스 hash | 반영함 | `C_opt.jsonl`, `C_table.md`, `verify_fixture.py`(현행·`.orig`), `tasks.py` 등 sha256을 다시 계산해 감사와 같음 확인. `qwen-local-harness` HEAD는 작성 중 전진했고 fixture 소스 hash는 그대로 |
| 61행 manifest coverage, 제외 0 | 반영함 | `attempt_manifest.csv`의 `raw_line` 1..61을 직접 확인. 성공 raw행 29, API 오류 26, wall 0초 20, timeout 1 |
| 6/6·성공당 165.0초의 3과제 | 확정 | `cmoe-t6-ub256-ctx4k-low`가 `s3-fix-inventory`, `t1-bug-median`, `t3-write-tests` 6행(모두 성공). 새 E2E 직접 비교 기준에서 제외(§2) |
| 병렬 대조 310.1초 대 254.4초 | 확정 | 두 설정 모두 `t1-bug-median`, `t3-write-tests`만(각 설정 기대 6키 중 4키). 589.1초는 경과시간이 아님(§6.4-4) |
| API 26건 원인 unknown | 반영함 | §5.2. 3(양의 경과·응답 없음)+3(도구/completion 관측)+20(wall 0초). 감사 보고서 본문의 6+6+20 표기는 전사 오류이며 JSON·CSV가 정본 |
| comment-only false positive | 반영함 | N12, §5.3 매핑. 과거 checker만 PASS, 현행 FAIL |
| 빈/누락 test, 대상 없음 | 반영함 | N13, N4, N3, N8 |
| inventory `5 x 2,000` hidden | 반영함 | §3.3, N14=NC-A5. 실제 출력 문자열은 감사 보고의 기술이며 raw로 독립 복원하지 못함 |
| pristine reset, 시작 hash, 요구 변경 대조 | 반영함 | §1-7·§1-8·§5.1-0, N11 |
| 탐색 전용 과제와 수정 필수 과제 구분 | 반영함 | §1-9, §2.1 |
| 14턴 기록의 범위 | 반영함 | §3.8. 1건 사례, 영수증 `a0c73b28b621` |
| 4B/FlashNext 분리, 16일 Common30·10/17 31행 correction | 이 명세는 사용하지 않음 | 코딩 과제 점수에 섞지 않는다는 설계 §5.1을 유지 |
| 10/17 efficiency 원 raw(`raw_data.zip`) | **열림(보류)** | 지정 경로와 검색한 outbox에서 발견하지 못함. 원 JSONL 독립 추적은 미완이며 31행 CSV correction 수준으로만 재사용 가능. 이 명세는 그 수치를 쓰지 않았고, 원본이 찾아지기 전에는 쓰지 않는다 |
| P2·P6·P7b가 과거 성공 행에서 실제로 쓰였는지 | **열림** | 감사 보고에서 언급을 찾지 못함. trace에 `os._exit`, 인라인 복제, 키워드 나열이 있는지 별도 확인 필요 |
| `tasks.py` 계열 verifier의 변경 이력과 행별 판정 버전 | **열림** | 감사는 stage1 checker의 과거·현행만 비교. C_opt 행의 판정에 쓰인 `tasks.py` 계열 verifier 버전 매핑은 미확인 |
| `t4-long-file` 전제 불일치가 과거 성공률에 미친 영향 | **열림** | fixture 결함 자체는 반영(§3.5). 과거 행 영향은 미확인 |
| mock 대상 오류 행 처리 | **열림** | 감사 보고에서 언급을 찾지 못함 |
| fx2 partial·테스트 미실행 기록의 checker 연결 | **부분** | fx3 영수증과 checker 재검증은 감사가 추적. fx2는 감사에서 별도 확인하지 못함 |

## 9. 확정 범위, 미검증 범위, 잔여 과제

**4.1이 확정한 것:** 18과제 명세(유형·분할·fixture 출처·요구↔hidden 매핑·필수 reject·change class), 평가 경계와 실패 분류, negative control 세트, holdout 구조 제약과 보호 방침, 실행 프로토콜 계획값, 감사 반영 현황.

**미검증(이 Run에서 확인하지 못함):**
- fixture·gold·alt-gold·mutant·negative control의 실제 동작. 개정한 hidden과 rev2 프롬프트의 오탐 여부. §5.4는 기존 hidden 일부와 shop trap만 확인했다.
- B4-API, B4-DBG rev2, B4-LNG 합성, 모든 holdout은 존재하지 않는다.
- Seatbelt read 거부, 네트워크 차단, resume, 이벤트 취소, thinking readback, slot 비우기 지원, 컨텍스트 압축 발생.
- §6의 숫자 전부가 사전등록 계획값이다. 실측 결과로 인용하지 않는다.
- probe와 스캔은 개발 fixture 일부에 대한 검사이며 verifier 전체의 신뢰도나 누출 없음을 증명하지 않는다.

**4.2로 넘기는 잔여 과제:**
1. 모든 과제에서 delta/invariant 분류를 base와 gold에서 CPU로 확인(§1-8), 필수 reject·N1~N14 실행, gold·alt-gold 작성과 독립 검토.
2. MUL rev2 프롬프트, DBG rev2(공개 test와 실제 실행 failure log), LNG composite 합성과 턴 문안 hash 고정, EXP answer key AST 계산, API fixture, REC gold.
3. evaluator의 handshake·`python -I`·patch 적용 경로·시작 hash 검사, 누출 스캔을 hidden 전체 리터럴과 gold hunk로 확장.
4. holdout 9개 작성과 sealed 번들 구성(작성 컨텍스트 분리).

**운영자 조치와 외부 의존:** holdout 번들 위치 생성과 Seatbelt/네트워크 deny 테스트(§4.2). 2.2(하네스 수정 독립 검증)는 반례 2건으로 재작업 중이며 그 결과에 따라 resume·취소·압축 층의 지원 여부가 정해진다. 10/17 efficiency 원 raw 확보 전에는 해당 자료를 쓰지 않는다. 열린 §8 항목(P2·P6·P7b의 과거 trace 사용, `tasks.py` 계열 verifier 판정 버전 매핑, `t4-long-file`의 과거 영향, mock 대상 오류)은 별도 확인이 필요하다.

## 부록 A. 이번 Run의 원명령과 출력 요약

| 명령 | 핵심 출력 |
|---|---|
| `git merge-base --is-ancestor 7e637d79… origin/main` | exit 0 (`ancestor-ok`) |
| `git rev-parse origin/main` | `474e034a684935e2c3d863820b5e7b0d3ffdc7b7` |
| `git worktree add --detach /Users/winterholic/development/local-llm-bench-spec-4-1-20261023 origin/main` | `HEAD의 현재 위치는 474e034` |
| `git -C qwen-local-harness rev-parse HEAD` | `b278b96f45865b9dd9104a5735d10ad56344d854` |
| `git diff --quiet HEAD -- tasks.py stage3.py longtasks.py validate_tasks.py` | exit 0(수정 없음) |
| `python3 verifier_probe_existing.py …/b` | 14 probes, false positive 3 (`P2-exit0-init`, `P6-dedupe-inline-copy`, `P7b-explain-keywords-contiguous`) |
| 산술 확인 | `round(1005*(100-50)/100)=502`, `61/3+7=27.333333333333332`, `60/3+7==27 → True` |

probe 파일 hash: `verifier_probe_existing.py` `44d161fa53115b93def792b28577cb42c0790e5e8e0c3b717e3466be1286a013`, `verifier_probe_output.json` `b9991292007e7feafd9fef2c8c521fd0236de87da263343c4c3f8f5392dcda52`.

probe 스크립트는 첫 실행 뒤 push 게이트(ruff)를 통과시키려고 import 정리, 파일 열기 방식, 포맷만 바꿨다. 로직은 같고, 바뀐 스크립트로 재실행한 출력이 첫 출력과 바이트 단위로 같음을 cmp로 확인했다(위 해시는 바뀐 스크립트 기준).

**v1 추가 실행(1.2 반영 뒤, 모두 /tmp 복사본·모델 없음, Python 3.13.15 / macOS 27.0 / arm64):**

| 명령 | 핵심 출력 |
|---|---|
| `shasum -a 256 tasks.py stage3.py validate_tasks.py longtasks.py verify_fixture.py verify_fixture.py.orig` | §7 값과 동일(`2ca19a31…`, `291343d7…`, `dcd09bf0…`, `a3c4022c…`, `003d178f…`, `9ed432cb…`) |
| `python3 validate_tasks.py`(복사본) | 11과제 모두 `untouched_fails=True solution_passes=True`, `BAD 0` |
| `python3 verify_delta_and_trap.py <bench 복사본> <fixture-template>` | claims 12건 전부 `claim_holds=true`, shop: pristine 3건 실패, naive `round()` 패치 `FAILED (failures=1)`, 정수 half-up 패치 `OK` |
| `python3 leak_scan_dev.py <bench 복사본> <stage1>` | §5.4-3 요약 |
| `ruff check`·`ruff format` | 새 스크립트 2개 통과(push 게이트 대비) |

v1 증거 파일 6개(`evidence/4.1/`)의 sha256은 manifest `evidence_files`에 전부 기록했다(스크립트는 ruff 통과 상태에서 해시했고 출력은 그 스크립트의 실행 결과다).

차단 기록: `~/.ttakkari` 아래 도구 결과 파일 읽기와 outbox 디렉터리 목록 조회, 재귀 삭제는 ttakkari guard가 막았다. 우회하지 않고 worktree에서 문서를 다시 읽거나 삭제 없이 진행했다.
