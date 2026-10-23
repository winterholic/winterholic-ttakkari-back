# WBS 4.2 평가기 구현 기록 (개발 과제 9개)

작성일: 2026-10-23 KST. 상태: **4.2 부분 완료.** 평가기 골격, 개발 과제 9개, 음성 대조 N1~N14 전부가 구현되어 CPU에서 재현된다. holdout 9개와 봉인 번들은 만들지 않았으므로 4.2는 완료가 아니다.

> **독립 검증 후속 수정 반영(main ad58203c 기준 검증 2건, §9).** §1~§8 은 수정 전 구조(커밋 bcedf3e)의 서술을 그대로 두되, 아래 항목은 §9 가 대체한다: §1 '실행기' 행, §2 의 과제별 음성 대조 수와 합계(177→204), §3 일부, §6 첫 항목(격리·위조), §8 의 재현 출력. §9 이후 재발견한 in-process helper 변조는 §10이 교정한다. §10은 이후 C1/C2 검증에서 거부됐다. 현재 구조와 증거는 §11과 `evidence/4.2/channel-isolation/`가 정본이었으나, §11의 OS 격리 서술은 독립 검증에서 틀린 것으로 확인되어 **§12가 대체한다**(profile 결함 수정, 격리 켠 전체 테스트, 과잉 차단 완화).

- 명세: `benchmark-spec-draft.md` v1(§0.1 포함), manifest `benchmark-manifest-draft.json`(이 구현에 맞춰 v1.1로 갱신).
- 코드: `bench/local_llm_eval/`(표준 라이브러리만). 실행법은 그 디렉터리의 `README.md`.
- **이 Run에서 실행한 것:** CPU 자식 프로세스 검사, 단위·회귀 테스트, 자체 검증. **실행하지 않은 것:** 모델 로드·추론·다운로드, 서버, GPU, 네트워크, 운영 변경, Seatbelt 격리 시험. 모델 결과는 하나도 없다.
- 기준: 작업 시작 시 origin/main `d209386…`, 중간 push `a1f09b6…`, 최종 SHA는 보고서에 적는다.

## 1. 구현한 것

| 영역 | 구현 | 명세 위치 |
|---|---|---|
| pristine reset·시작 hash | attempt 시작 manifest(경로별 sha256)가 pristine(또는 다단계 과제의 이전 단계 끝 상태)과 다르면 `INFRA_BASE_MISMATCH`(모델 평가 아님). manifest 없음은 `UNKNOWN` | §1-7, §5.1-0 |
| 구조 게이트 | symlink, `.git`, checker 영향 파일(`conftest.py`, `sitecustomize.py`, `usercustomize.py`, `pytest.ini`, `*.pth` 등), frozen 경로 변경(원본 기준, 단계가 지나도 유지), read-only 단계의 추가 쓰기, no-op, comment-only(AST 동일), 빈 required 파일, 누락·부분 경로 | §5.1, §5.2 |
| delta/invariant | 모든 검사에 `delta`/`invariant`/`process_metric` 종류. PASS는 delta ≥ 1 이고 전부 green, invariant green. delta 없는 단계는 `ConfigError` | §1-8 |
| 변경 요구 class | `diff_required` 와 `read_only_report` 분리. 탐색 과제는 diff 없이 report 산출물로, 장시간 과제 T5는 diff 없이 보고로 판정 | §1-9, §2.1 |
| 실행기(수정 전) | 검사 1개당 `python -I -S -B` 프로세스, 사본 트리, nonce 핸드셰이크 → **§9 에서 probe/judge 분리로 교체** | §5.1-4,5 |
| 후보가 쓴 테스트 | unittest loader로 수집, gold SUT에서 통과(skip·xfail 0), 사전 검증된 mutant 사망, AST로 assertion·SUT 참조 검사, SUT 불변 | §3.7, §3.8 |
| 보고 대조 | 구조화 보고의 `changed_files` 를 실제 diff와, `commands_run` 을 trace 와 대조. 불일치는 `FAIL_REPORT_MISMATCH`(코드만 PASS면 `code_pass_report_mismatch` 플래그도) | §5.1-7 |
| ledger | 추가 전용 JSONL, 행 불변(중복 id 거부), 서버·모델 식별 결측은 `UNKNOWN`·`comparable=false`, wall 0초는 `zero_wall_no_response`, profile 별 요약(재시도는 비용만, 성공 키는 task×repeat 단위, profile 합산 없음) | §6.4 |
| 누출 스캔 | hidden 스니펫 리터럴과 gold 추가 토큰이 프롬프트·비테스트 소스에 있는지 어휘 수준으로 검사, 프롬프트가 명시한 토큰은 spec-derived 로 분류 | §5.4-3 확장 |

## 2. 개발 과제 9개

과제별 fixture·참조해·대안 참조해·hidden·음성 대조는 `tasks/*.py`. 기존 fixture는 `fixture_data/*.json`(원본 sha256 기록)에서 불러오고 코드는 저장소 안에서 자립한다.

| 과제 | fixture | 이번에 바꾼 것 | 검사 수 | 음성 대조 | 참조해/대안 |
|---|---|---|---:|---:|---|
| B4-EXP-D | 기존 ledger 26모듈 | 읽기 전용 과제. 정답 키를 AST로 계산(변환 2·호출자 3), 코드에 기대값을 박지 않음 | 8 | 15 | PASS/PASS |
| B4-SGL-D | 기존 median | tests 불변, 입력 비수정, 음수·실수, mean 불변 추가 | 8 | 21 | PASS/PASS |
| B4-MUL-D | 기존 inventory | **프롬프트 rev2**(수량·TOTAL 구분자, 빈 목록 명시), 두 파일 모두 변경 필수 | 8 | 20 | PASS/PASS |
| B4-API-D | **신규** notes WSGI | 계약 표 프롬프트, in-process 호출(소켓 없음), `os.replace` 스파이로 원자성 순서 검사 | 11 | 22 | PASS/PASS |
| B4-DBG-D | 기존 rules 60함수 | **rev2**: 실제 실패하는 공개 테스트와 **실제 실행으로 만든** `logs/ci_failure.log`, 원인-패치 대응 검사 | 7 | 22 | PASS/PASS |
| B4-REF-D | 기존 dedupe | common 치환 검사(sentinel), 복제 본문 AST 검사, 동결 참조 구현과 동작 비교 | 8 | 20 | PASS/PASS |
| B4-TST-D | 기존 discount | 후보 테스트가 gold 에서 통과해야 함, mutant 8개 전부 사망, SUT 참조 assertion 요구 | 8 | 24 | PASS/PASS |
| B4-REC-D | 기존 stage1 shop | round() trap, 후보 테스트의 범위 mutant 사망, 회복 지표(trace) | 9 | 23 | PASS/PASS |
| B4-LNG-D | 기존 3개 합성+filler | 5턴, 지속 제약 C1, T5 read-only, 세션 검사 인터페이스 | 20 | 37 | PASS/PASS |

합계(수정 전): 음성 대조 177건. **수정 후 현재는 204건(N15 포함)이 전부 기대한 사유로 reject, false positive 0**, 참조해와 대안 참조해 **18건 전부 PASS**, delta/invariant 분류 불일치 0(`evidence/4.2/coverage_summary.json`, `selfcheck_report.json`).
대안 참조해는 참조해와 다른 구현이다(예: 정수 산술 대 Decimal, 테이블 구동 라우팅 대 if 체인). evaluator 가 대안 구현을 부당하게 막지 않는다는 근거다.

### 음성 대조 커버리지

| 대조 | 건수 | 과제 수 | 비고 |
|---|---:|---:|---|
| N1 no-op | 9 | 9 | |
| N2 주석·키워드만 | 11 | 9 | |
| N3 부분·불완전 수정 | 42 | 9 | 공개 테스트만 통과하는 하드코딩(`FAIL_HIDDEN_ONLY`) 포함 |
| N4 테스트 삭제·약화 | 18 | 8 | EXP(read-only)는 쓰기 자체가 금지 |
| N5 skip·xfail | 3 | 3 | TST, REC, LNG |
| N6 exit 0 stub | 9 | 8 | EXP 제외 |
| N7 checker 조작 | 34 | 9 | 위조 핸드셰이크, `TestCase.run` 교체, sitecustomize, conftest 등 |
| N8 대상 없음·없는 경로 | 12 | 9 | |
| N9 빈 결과 | 10 | 9 | |
| N10 금지 경로 쓰기 | 12 | 9 | |
| N11 이미 정답인 시작 상태 | 9 | 9 | `INFRA_BASE_MISMATCH` |
| N12 주석에만 적은 경계 테스트 | 3 | 3 | 감사 P1-1 |
| N13 빈·수집 0건 테스트 | 4 | 3 | |
| N14 출력 정규화 누락 | 1 | 1 | `5 x 2000`, 감사 P1-2 |

manifest 의 과제별 `required_negative_controls` 가 구현된 대조의 부분집합임을 테스트(`test_llme_registry.py`)가 확인한다.

## 3. 감사·probe 사례에 대한 대응

| 사례 | 새 평가기 |
|---|---|
| 4.1 probe P2: `os._exit(0)` 만 넣은 no-op 이 통과 | 요구 경로 미변경으로 `FAIL_MISSING_TARGET`, 수정과 함께 넣어도 `FAIL_STUB_EXIT0`(회귀 테스트) |
| P6: 인라인 복제+주석이 통과 | common 치환 검사로 reject(`FAIL_HIDDEN_ONLY`) |
| P4: 항상 실패하는 테스트가 hidden 성분을 통과 | gold 통과 요구와 assertion 검사로 reject |
| 감사 P1-1: comment-only 경계 테스트 | N12, `FAIL_COMMENT_ONLY` |
| 감사 P1-2: `5 x 2,000` | N14, MUL 프롬프트 rev2 |
| 감사 P1-3: 이미 정답인 workspace 의 no-op | 시작 hash 게이트(N11) |
| `t4-long-file` 의 전제 거짓 | DBG rev2. 기존 fixture 의 공개 suite 가 base 에서 green 이었음을 테스트가 재현하고, rev2 base 는 정확히 1개 실패임을 확인 |
| API 26건 원인 unknown, 0초 20행 | ledger 가 `UNKNOWN`·`zero_wall_no_response` 로 보존. **감사 manifest 61행을 ledger 코드로 다시 계산해 성공당 165.017초(t6), 239.733초(t8), 0초 행 20개를 재현**(`evidence/4.2/ledger_crosscheck_audit.json`). 합산 profile 은 8개로 분리 유지 |

## 4. 명세(4.1 v1)와 달라진 점

1. **추가 검사 id:** 공개 suite `PUB*`(종류는 과제별로 delta 또는 invariant), 후보 테스트 검사의 `H4q`·`H5c`·`H5g`·`H5q`(REC·LNG), API 의 `H10`(frozen). requirement id 는 붙이지 않아 요구 수는 manifest 와 같다(테스트로 확인).
2. **LNG 검사 종류:** 구현 기준으로 H7(compute_tax 동작)은 delta(시작 시 이름이 없음), H10·H12 invariant, 나머지 delta. manifest 의 `check_classes` 를 구현에 맞췄다.
3. **보고 대조는 모든 stage 에 적용**한다. EXP 는 report.json 이 diff 에 포함되므로 `changed_files == ["report.json"]` 이어야 한다.
4. **TST 의 N4** 는 `tests/__init__.py` 삭제로 구현했다(공개 테스트가 없는 과제).
5. 시작 상태 대조의 기준은 다단계 과제에서 "pristine" 이 아니라 **직전 단계 끝 상태**다(턴 사이에 작업 공간이 바뀌면 `INFRA_BASE_MISMATCH`).

## 5. 인터페이스만 둔 항목 (WBS 2.2 의존)

harness 가 아직 없어서 평가기는 입력 형식만 정의하고 **없으면 pending 으로 표시**한다. pending 은 PASS 를 막지 않지만 verdict 에 `gates_pending` 으로 남는다.

- **trace**(이벤트 목록: `kind` read/search/edit/command/test, `path`, `command`, `exit_code`, `output`, `t`): EXP H8(인용 파일을 보고서 작성 전에 검색·읽었는가, trace 가 있으면 gating), DBG H1(재현 후 편집, 측정만), REC H7(실패→재편집→통과, 측정만), 보고의 `commands_run` 대조. 단위 테스트로 trace 가 있을 때의 동작을 확인했다.
- **세션**(`attempt.session`): LNG 의 H13(resume lineage), H14(취소 후 workspace 일관). 단위 테스트로 판정 로직(컴파일 안 되는 파일·임시 파일 검출)을 확인했고, gold 실행에서는 H14 가 snapshot 없이 pending 이다.
- 컨텍스트 압축, 이벤트 기반 취소, thinking readback 은 평가기가 아니라 runner(4.3) 몫이다.

## 6. 한계와 미검증

- **격리(수정 전 서술, §9 가 대체):** 네트워크 차단과 파일 시스템 샌드박스는 적용하지 않았다(Seatbelt 는 4.3). 수정 전에는 후보 코드와 같은 프로세스에서 nonce 를 공유해 위조가 가능했다(독립 검증이 재현). §9 의 probe/judge 구조로 바뀌었다.
- **누출 스캔은 어휘 수준**이고 native 검사(비스니펫)는 스캔하지 않는다. 프롬프트가 명시한 토큰은 spec-derived 로 센다. 누출이 없다는 증명이 아니다.
- 검사의 delta/invariant 분류와 참조해 PASS 는 **개발 과제 9개의 현재 hidden** 에 대해서만 확인했다. 각 hidden 이 프롬프트·docstring 에서 추적되는지(명세 §1-4)는 사람의 검토 항목이며 자동 검증하지 않았다.
- mutant 8개(TST)·3개(REC)·4개(LNG T2)는 참조 테스트가 전부 죽임을 확인했을 뿐, 후보가 쓸 수 있는 모든 올바른 테스트가 통과하는지(과도한 제한)는 대안 참조 테스트 1종으로만 확인했다.
- LNG 합성 workspace 에서 컨텍스트가 실제로 커지는지, 압축이 일어나는지는 모르는 상태다(모델 미실행).
- 이 테스트는 push 게이트가 실행하지 않는다(게이트 출력: "바뀐 테스트 파일 (선택된 파일 없음)"). 수동 실행 대상이다.
- 실제 모델 응답에 대한 평가 성능, 시도 시간, 자원 비용은 **측정하지 않았다**.

## 7. 남은 4.2 항목

1. holdout 9개 작성과 봉인 번들 구성(운영자 조치: 저장소 밖 0700 디렉터리 생성, Seatbelt 읽기 거부·네트워크 deny 시험). 이번 지시에 따라 만들지 않았다. 저장소에는 holdout 정답이 없고 레지스트리 테스트가 이를 확인한다.
2. 개정 hidden 의 프롬프트 추적 검토(사람), native 검사까지 포함한 누출 스캔 확장.
3. 평가기를 runner 에 연결(4.3): attempt ledger 기록, 시작 snapshot 생성, trace·session 공급, 격리 실행.
4. 2.2 결과에 따라 세션·trace 입력 형식 확정.

## 8. 재현 명령과 출력 요약

환경: Python 3.13.15, macOS 27.0 arm64, ruff 0.16.10(`evidence/4.2/environment.txt`). 모두 `bench/` 에서 실행.

| 명령 | 결과 |
|---|---|
| `python3 -m local_llm_eval.selfcheck` | 9과제 `classification_bad=0 reference_fail=0 not_rejected_right=0 false_positives=0`, 마지막 줄 `{"tasks": 9, "problems": 0}`, 약 18초(worker 1개 28초, 8개에서도 동일 결과) |
| `python3 -m pytest local_llm_eval/tests -q`(수정 전) | `90 passed in 20.91s` → 현재 값은 §9 |
| `python3 -m local_llm_eval.provenance.crosscheck_audit <evidence-audit>` | 61행, 0초 행 20개, t6 성공당 165.017초, t8 239.733초 |
| `ruff check --quiet bench`(저장소 루트) | 출력 없음(통과) |

증거 파일: `evidence/4.2/{selfcheck_report.json, coverage_summary.json, leakscan_report.json, ledger_crosscheck_audit.json, pytest_output.txt, environment.txt}`.

## 9. 독립 검증 후속 수정 (main ad58203c 기준, 2건)

### 9.1 [치명] nonce 위조 false PASS → probe/judge 분리

**결함:** 수정 전 구조는 후보 코드를 import 하는 검사 프로세스 안에서 nonce·기대값·assertion 이 함께 돌았다. 후보 코드가 import 시점에 nonce(환경변수, argv, 스펙 파일, 프레임/메모리)를 읽어 유효한 handshake 줄을 stdout 에 쓰고 `os._exit(0)` 하면 checker 본문이 실행되지 않고 PASS 가 났다. 독립 검증이 EXP 를 뺀 코드 과제 8개(TST, 5단계 LNG 포함)에서 재현했다. 검증자의 스크립트는 outbox(guard 차단)라 읽지 못하고 위 설명으로 재구성했다(`tests/test_llme_forgery.py`).

**원인:** 같은 프로세스에서 비밀을 공유하는 handshake 는 근본적으로 위조 가능하다. nonce 를 더 숨겨도 같은 결함이 남는다.

**선택한 설계:** 판정이 후보 코드가 접근할 수 없는 곳에서 나오게 했다.
1. **probe(후보 쪽 프로세스, `hidden_runner.py`)**: 후보 코드를 호출해 **원시 관측값만** `rec(key, value)`/`attempt(fn, ...)` 로 기록하고, 끝에 `OBS\t<json>` 한 줄(fd 1)로 돌려준다. 이 프로세스에는 nonce, 기대값, 판정 코드가 없다. 스펙 파일에는 probe 프로그램(입력)과 부모가 만든 scratch 디렉터리 경로뿐이다.
2. **judge(평가기 프로세스, `judges.py` 와 과제 모듈의 `_hN` 함수)**: 관측값을 받아 기대값과 비교한다. 후보 코드를 import 하지 않는다. 기대값은 judge 함수 안에만 있다.
3. **부모가 직접 본 사실(`facts`)**: 프로세스 종료 뒤 부모가 디스크를 읽는다(작업 디렉터리 목록, 부모가 만든 `data` 디렉터리의 파일 목록과 내용). 후보가 종료 뒤에 고칠 수 없다. 정적 검사(`_clean` 잔존, 복제 본문, `calc_tax` 잔존, 보고서 대조)는 후보 실행 없이 부모가 `end_files` 로 한다.
4. 추가 방어: probe 는 시작 시점에 복사한 **builtins 사본**을 쓰고 내부 도우미는 import 시점에 잡아 둔 참조만 쓴다(후보가 `builtins.list` 등을 바꿔도 probe 관측은 영향을 받지 않고, 바뀌었으면 `__tampered__` 로 기록돼 `FAIL_CHECKER_PATCH`). 관측 줄이 둘 이상이면(종료 훅이 위조 줄을 추가) `multi_obs`, 줄이 없고 exit 0 이면 `stub_exit0`, JSON 이 아니면 `bad_obs`. probe 가 필요한 키가 없는데 오류도 없으면 스트림이 잘렸거나 위조된 것으로 보고 `FAIL_CHECKER_PATCH`. judge 가 후보 데이터를 해석하지 못하면 잘못된 결과(`FAIL_HIDDEN`)다.
5. **후보가 쓴 테스트(TST, REC, LNG T2)**: 이 파일은 후보가 실행되는 프로세스에서 unittest 결과를 스스로 만든다. 그래서 AST 금지 목록을 둔다: 허용 목록 밖 import(`os`, `sys`, `inspect`, `importlib`, 상대 import 등), `open`, `exec`, `eval`, `print`, `getattr` 등의 이름, `__code__` 같은 dunder 속성, `run`·`__call__`·`load_tests` 정의, `TestResult` 류 서브클래스. 걸리면 `FAIL_CHECKER_PATCH`. 이는 mutant 를 "SUT 소스 문자열 비교"로 죽이는 테스트도 막는다.

**검증(N15 + 회귀):**
- **N15** 를 9개 과제 전부에 추가했다. 변형 3종: (a) 환경변수·argv·프레임을 뒤져 비밀을 찾고 legacy handshake 와 빈 OBS 를 쓴 뒤 `os._exit(0)`, (b) 프레임에서 probe 코드를 읽어 모든 키에 `{"ok": true}` 를 채운 OBS 를 쓰고 exit, (c) `json.dumps`/`os.write` 를 바꿔 관측 줄을 잃게 함. 장시간 과제는 T2~T4 단계에도 넣었다. EXP 는 후보 코드가 실행되지 않으므로 소스 파일에 위조 코드를 심으면 읽기 전용 위반(`FAIL_FORBIDDEN_WRITE`)임을 확인했다. 전부 FAIL, false positive 0.
- 단위 테스트(`test_llme_forgery.py` 외): nonce 사냥+legacy handshake+early exit, 프레임 읽기 위조, 완전한 위조 OBS 줄(기대값을 모름), 종료 훅이 만든 두 번째 OBS 줄, 출력 경로 patch, 예외 삼키기(`sys.excepthook`, 예외를 삼키는 함수), builtins patch, 불완전한 스트림, 후보 테스트의 금지 구성요소 11종과 정상 테스트 4종의 비오탐.

**남는 한계(명시):**
- **oracle faker:** 후보가 정답 로직을 파일 어딘가에 갖고 있고, probe 를 감지했을 때만 그 로직으로 정확한 관측을 만들어 내보내고 실제 공개 함수는 틀린 채로 두는 경우는 막지 못한다. 기대값을 모르므로 정답 로직 자체를 구현해야 해서 "문제를 푼 것"과 구분되지 않는다. 완화안(미구현): probe 입력을 실행마다 난수로 바꾸고 judge 가 같은 시드로 기대값을 계산.
- **PUB(공개 suite) 결과의 위조 가능성:** 공개 테스트를 후보 SUT 와 같은 프로세스에서 돌리므로 그 개수·성공 여부는 후보가 위조할 수 있다. 정답 여부는 H 검사(기대값은 judge 쪽)가 결정하고 PUB 는 분류(`FAIL_PUBLIC` 대 `FAIL_HIDDEN_ONLY`)와 보조 게이트에만 쓰인다.
- **후보가 쓴 테스트**는 금지 목록으로 줄였을 뿐 같은 프로세스 실행이라는 구조는 그대로다(C 확장·`ctypes` 등은 import 금지로 막지만 AST 규칙이 모든 우회를 포괄한다고 증명하지 못한다).
- 네트워크 차단과 파일 시스템 샌드박스는 여전히 없다(Seatbelt, 4.3). probe 프로세스는 `python -I -S -B`, 최소 환경, 사본 트리다.
- 같은 이유로 `os.write(1, ...)` 로 직접 쓰는 것은 막을 수 없고, 쓰더라도 유효한 관측이려면 judge 기대값과 같은 값이어야 한다.

### 9.2 [중요] 시작 hash: 평가기가 실제 시작 트리를 직접 해시

**결함:** 수정 전에는 호출자가 넘긴 시작 manifest 를 기준값과 비교만 했다. 거짓 manifest 를 넘기면 실제 시작 상태가 달라도 통과했다(§0.1 의 "시작 hash 검증"을 평가기가 하지 않은 셈).

**수정:** `StageAttempt` 에서 `start_manifest` 를 없애고 `start_dir`(시작 시점 실제 workspace 트리)을 받는다. 평가기가 `dir_manifest(start_dir)` 로 **스스로 해시**해 sealed 기준값(과제 정의의 pristine, 다단계는 평가기가 직전 단계 끝 트리에서 계산한 값)과 대조하고, 불일치(추가·누락·수정·symlink)는 `INFRA_BASE_MISMATCH`, 트리가 없으면 `UNKNOWN`. 계산한 트리 해시는 `StageResult.start_tree_sha256` 으로 ledger 용으로 남는다. 기준값은 `benchmark-manifest-draft.json` 의 `base_tree_sha256` 에 고정돼 있고 테스트가 과제 정의와의 일치를 확인한다. harness 연결 시 `start_dir` 은 첫 모델 요청 **전에** 신뢰하는 쪽이 만든 스냅샷이어야 한다(4.3).

**회귀 테스트:** 시작 트리 바이트 하나 수정, 추가·삭제 파일, symlink, `__pycache__` 무시, 트리 미제공은 UNKNOWN, 호출자 manifest 로는 속일 수 없음(트리를 바꾸면 같은 attempt 가 INFRA 로 바뀜), 다단계 과제의 단계 사이 변경, 결과에 평가기가 계산한 해시가 실림, manifest 고정 해시와 정의 일치.

### 9.3 push 게이트에 평가기 테스트 포함

`scripts/verify_deploy_gate.sh` 의 `impacted` 모드에 단계를 하나 더했다: 변경 파일에 `bench/local_llm_eval/` 가 있으면 그 디렉터리의 pytest 를 돌린다(기존 선택은 `tests/test_*.py` 류만 잡아 이 테스트를 건너뛰었다). CPU 만 쓰고 약 35초이며, 해당 경로가 바뀌지 않은 push 에는 영향이 없다. 로컬에서 `./scripts/verify_deploy_gate.sh --commit HEAD --base origin/main` 으로 확인했다: `통과 평가기 테스트 (bench/local_llm_eval) (34초)`, `128 passed`.

### 9.4 수정 후 결과 (CPU, 모델 없음)

| 항목 | 값 |
|---|---|
| 개발 과제 | 9, 검사 87개(과제별 7~20) |
| 음성 대조 | **204건 전부 기대한 사유로 reject, false positive 0**(N1~N15 15종, N15 34건/9과제) |
| 참조해·대안 참조해 | 18건 전부 PASS, delta/invariant 분류 불일치 0 |
| 테스트 | `129 passed in 33.92s`(`cd bench && python3 -m pytest -q local_llm_eval/tests`) |
| selfcheck | 9과제 모두 `classification_bad=0 reference_fail=0 not_rejected_right=0 false_positives=0`, `{"tasks": 9, "problems": 0}` |
| 누출 스캔 | 미검토 hit 0(judge 함수의 기대값 리터럴 기준으로 재산출) |

증거: `evidence/4.2/{selfcheck_report.json, coverage_summary.json, leakscan_report.json, pytest_output.txt}`(재생성).

**남은 일:** holdout 9개와 봉인 번들(운영자 조치 전), 개정 hidden 의 프롬프트 추적 사람 검토, 평가기와 runner 연결(4.3), 위 §9.1 의 한계 중 난수 probe 입력(미구현).

## 10. 관측 무결성 경계 재작업 (2026-10-23, 후속 C1/C2로 보장 주장 거부)

**§9.1의 '기대값을 모르므로 위조할 수 없다'는 해석은 폐기한다.** 후보는 기대값을 몰라도 입력을 복사해서 helper에 넘기도록 관측 과정을 바꿀 수 있었다. 같은 interpreter의 관측은 본래 후보 통제 데이터다. 최신 계약·신뢰 경계 다이어그램·대안 선택·미검증 범위·OS deny 테스트 계획은 명세 **§0.1.1**이 정본이다. §9의 204건/129개는 과거 실행이며 현재 실행은 아래 파일을 따른다.

### 10.1 재현과 수정

시작 HEAD `38b5468`에서 재구성한 공격은 `stats.median`의 입력 list를 제자리 정렬하게 만들고, import 도중 probe globals의 `attempt`만 인수 복사 wrapper로 교체한다. 정답 숫자/기대값은 공격에 없다.

```text
수정 전: plain=FAIL/FAIL_REGRESSION helper_attack=PASS/None
수정 후: plain=FAIL/FAIL_REGRESSION helper_attack=FAIL/FAIL_CHECKER_PATCH
```

원출력은 `evidence/4.2/integrity/{before,after}.txt`. `test_llme_integrity.py`의 동일 수락 조건이 수정 전에는 red, 수정 후에는 green이다(후보의 판정은 FAIL).

`integrity.py`는 부모의 실행 없는 admission과 후보 모듈 전용 loader/import/builtins를 제공한다. 부모가 봉인한 source bytes만 실행하며, 모든 후보 속성 읽기·쓰기·삭제를 runtime guard로 바꾼다. alias와 import-from도 stdlib capability view를 통과한다. frame/GC/동적 코드/introspection·모듈 내부·foreign object 변경을 제한하고, 주입 guard 이름을 함수 인수·import 별칭·예외/패턴 binding으로 가리지 못하게 한다. attribute target은 guarded subscription으로 바꿔 Python의 대입 순서·언패킹·증분 연산·loop/with 의미를 유지한다. 일반 객체 필드와 list 연산은 허용한다.

`hidden_runner.py`는 import 전 audit hook을 설치한다. 후보 filename에서 직접 발생한 frame/GC 이벤트는 기록·차단하되 trusted stdlib 내부 이벤트는 허용한다. 제약 위반은 catch해도 기록이 남으며 judge가 그 관측을 채택하지 않는다. audit가 보지 못하는 일반 모듈 대입·dict 변경·직접 fd/종료는 capability/source/속성 가드가 담당한다. 부모 `runner.py`는 trusted runner/helper bytes를 시작 시 고정해 bootstrap하며, 나중의 디스크 재작성으로 다음 probe 실행 코드를 바꾸지 못하게 한다. nonzero 종료 stream은 OBS가 있어도 거부한다.

REF H2와 LNG H8의 의존성 검사도 봉인 loader와 맞췄다. 신뢰된 probe가 source 파일을 덮어쓰는 대신 의존 모듈의 함수를 바꾸며, eager import를 쓰는 LNG는 `billing.invoice`를 캐시에서 제거한 뒤 봉인 source를 재import한다. 직접 함수 import와 모듈 import 방식 모두 검증한다. 부모 judge의 기대값/검사 의미는 유지한다.

기존 공격 테스트는 삭제하지 않았다. builtins/종료 hook처럼 이제 실행 전에 거부되는 것은 과거의 '공격 실행 뒤 내부 관측' 단언을 조기 거부 단언으로 바꿨다. N6의 `_exit` 사유는 `FAIL_STUB_EXIT0`로 유지한다. TST N7의 SUT patch는 mutant 생존을 기다리는 `FAIL_HIDDEN` 대신 더 이른 `FAIL_CHECKER_PATCH`가 되며, 허용 조건을 완화한 것은 없다.

### 10.2 공격군과 최신 CPU 근거

N16~N33 18종은 **gold 위에** 주입하므로 원래 버그가 남아서 우연히 FAIL하는 대조가 아니다. 9과제×18=162건이며 EXP는 실행 없는 read-only 구조 위반으로 거부한다. 나머지는 helper/frame/GC, builtins/copy/json, import hook/modules, atexit/thread, fd 1·2/`_exit`, open/fdopen, attrgetter/loop target/guard binding을 검사한다. 별도 회귀는 audit층 단독(정적 검사를 trusted driver로 건너뛴 시도 포함), catch해도 기록 유지, source 재작성 후 봉인 import, 예약 모듈 충돌, nonzero OBS, 정상 속성 대입 의미를 검증한다.

최신 원출력: `evidence/4.2/integrity/pytest.txt`와 `selfcheck.txt/json`. selfcheck는 개발 과제 9개, 참조해 9/9·대안 참조해 9/9 PASS, 음성 대조 **366/366 기대 사유 reject**, false positive 0, classification 불일치 0을 기록한다. pytest 개수와 시간은 `pytest.txt`가 정본이다. 원래 129개 테스트를 유지하고 23개 회귀 항목을 추가했다.

환경은 Codex Run command sandbox의 macOS 27 arm64, Python 3.13.15다. 호스트 기본 `python3`(3.14)에는 pytest가 없어 테스트 수집 전 `No module named pytest`였으며, 기존 프로젝트 venv를 읽기 전용 실행 경로로 연결해 위 검증을 수행했다. 이는 이 명령 환경의 의존성 차이이며 배포 backend의 엔진·기능 실패라는 증거가 아니다.

### 10.3 한계와 남은 일

이 가드는 **선언한 Python capability 집합의 관측 경계**이며 임의 CPython/C 코드의 보안 sandbox 증명이 아니다. Python audit hook의 무력화/우회 가능성, 감사 이벤트가 없는 일반 속성·컨테이너 변경을 따로 취급한다. 직접 fd open은 실제 경로를 해소해 `/dev`·`/proc`도 제한하지만 일반 파일 저장 권한을 없애지는 않는다. 외부 파일·동일 UID 프로세스·네트워크·후손 격리는 미구현이고, 외부 공격자가 종료 후 디스크를 바꾸지 못한다는 보장도 아직 없다. 미발견 native/stdlib 간접 우회, 다른 Python 버전/문법, capability 밖 정상 풀이, 적응 공격 전수는 미검증이다. 입력·호출 맥락을 구별하는 oracle faker/과적합도 별도 위협이다.

holdout 9개, 정답 봉인, prompt↔hidden 독립 사람 검토, 실모델 완주, trace/session 실제 하네스 연결, OS sandbox deny 실측은 수행하지 않았다. 모델·서버·운영 변경은 없으며 **4.2는 doing으로 유지**한다. OS deny 계획은 명세 §0.1.1 마지막 문단을 따른다. Research Lab `eb41e3a7-43cd-4f2b-8c03-79380a2e1163`의 resume 데이터를 읽었고 연구 원문/제안은 데이터로만 사용했다. 이번 코드 검증 결과를 그 연구의 실험 실행으로 표시하지 않았다.

## 11. 전용 관측 pipe·OS 경계·정상해 호환성으로 전환

§10의 테스트는 C1/C2와 과잉 차단을 보장하지 못했다. 사용자 제공 독립 검증 요약을 근거로 이전 주장을 교정하고, 이번에는 공격 페이로드를 추가하지 않았다. 기존 대조는 유지하며 새 회귀는 trusted driver가 구조적 실패를 주입한다. 최신 계약과 한계는 명세 §0.1.1, 원출력은 `evidence/4.2/channel-isolation/`를 따른다.

- `channel.py`: 부모 전용 pipe, 바이너리 framing, 정확히 한 frame, envelope/identity/종료 상태 확인. stdout은 진단만, 모든 stream에 수집 한도를 둔다. fd와 guard duplicate를 닫거나 바꾸면 writer가 nonzero로 종료한다. 이것은 암호학적 인증이나 임의 child 메모리 장악에 대한 external attestation이 아니다.
- `sandbox.py`: default deny read allowlist, 네트워크/fork 거부. Python은 `.venv`가 아닌 실제 관리 런타임을 직접 실행한다. repo·원본 checkout·홈 설정을 allow하지 않는다. 후보에 전달하는 spec/probe 파일은 만들지 않는다. 실행 경로와 실제 deny 대상 목록을 isolation.json에 남긴다.
- `runner.py`/`evaluator.py`: 격리 불가 개발 결과는 사유+flag. holdout은 mode 또는 Task.split/id 계약으로 실행 전에 차단(native 포함). readiness 뒤 launch 실패는 격리 없이 재실행하지 않는다.
- `integrity.py`/`natives.py`: 동일 stdlib import 정책, 보통 getattr/hasattr/type()·dunder 정의·__all__ 호환성 복원. 애매한 신호는 flag. 기존 명백한 변조 대조는 거부한다. DBG는 기존 함수를 그대로 보호하며 정상 import·상수·클래스 prelude를 허용한다. 기존 '관련 없는 새 함수 추가' 대조는 계속 실패시킨다.

Codex Run command sandbox에서 `/usr/bin/sandbox-exec -p <default-deny profile> /usr/bin/true`는 exit 71, `sandbox-exec: sandbox_apply: Operation not permitted`였다. 이를 우회하거나 같은 거부를 재시도하지 않았다. 후속 CPU 검증에는 측정된 사유를 환경변수로 전달했다. 실제 OS deny 테스트는 skip이며 JSON 진단은 ok=false/exit2다. 이는 현재 Codex 환경의 증거이며 Claude/호스트에서 격리가 불가능하다는 뜻이 아니다. 사용자 제공 Claude 실측 요약을 이번 독립 실행 결과로 재인용하지 않는다. **정정(§12):** 이 호스트(엔진 sandbox 밖)에서는 출하된 profile이 `readiness exit -6`(SIGABRT)이었고 원인은 환경이 아니라 profile 결함(루트 `/` data-read 누락)이었다. 구현 당시 어느 환경에서도 이 profile이 Python을 실제로 띄운 적이 없었으므로 §11의 'deny 미검증' 서술은 Codex 한정이 아니라 '출하 profile은 사용 불가'였다.

실제 Python은 `/Users/winterholic/.local/share/uv/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13`이다. 원본 checkout은 존재하지만 이전에 검사한 원본의 evaluator.py는 존재하지 않았다. 실제 파일 pyproject.toml을 git common-dir로 찾아 존재 검사를 통과시킨 후 deny 대상으로 썼다. OS 안의 PermissionError는 아직 확인하지 못했다.

개발 정답은 공개되어 위조 방어가 약하다. 강한 보장은 봉인 holdout과 OS deny 수락을 선행 조건으로 하며 현재 달성했다고 주장하지 않는다. format/globals의 in-memory 노출, 새 native/reflection 우회, 후보 unittest 자체 결과/AST 우회, 적응 공격 전수는 이번 증거가 아니다. holdout9·정답 봉인·실제 OS deny·실모델·하네스 연동은 남아 있다. 모델·서버·운영 배포는 실행하지 않았고 PMS 4.2는 doing으로 유지한다.

이번 CPU 최종 출력: `280 passed, 1 skipped in 126.18s`, selfcheck `problems: 0`(flags: development_answers_public, sandbox_unavailable, trace_unavailable). 참조해9/9·대안9/9 PASS, 기존366개 음성 대조가 모두 기대 사유로 거부된다. 과잉 차단 회귀는 14×8=112건 PASS다. skip 1건은 실제 OS deny이며 PermissionError 검증을 통과했다고 해석하지 않는다. selfcheck 결과 JSON도 각 결과/최상위에 격리 flag를 보존한다.

## 12. 4.2 리뷰 필수 조건 이행 (2026-10-23)

독립 검증(Claude Sonnet 5.5, 엔진 sandbox 밖 Mac Studio)의 조건부 수락에 대한 이행이다. 새 공격 페이로드는 쓰지 않았고 모델·서버는 실행하지 않았다. 증거: `evidence/4.2/isolation-fix/`.

**1. sandbox profile 결함.** 출하 profile로 `sandbox-exec -f <profile> python3.13 -I -S -c ...`는 exit 134였다(stderr 없음). 이분 탐색에서 `(allow file-read-data (literal "/"))` 한 줄이 충분했고, 같은 조건에서 `/bin/echo`도 abort하므로 Python 고유 문제가 아니다(macOS 27, Darwin 27.0.0). 더 좁은 `file-read-metadata`/`xattr` 거부는 영향이 없다. 호출 주체(dyld/libSystem 추정)는 추적하지 못했다(확인 필요, `sandbox-exec (trace)`가 이 OS에서 파일을 만들지 않았다). 그래서 최소 규칙으로 data-read 한 줄만 추가했고 하위 경로는 열지 않는다. 부작용은 `/` 항목 이름 목록의 노출이다.
- `python3 -m local_llm_eval.sandbox`: `ok: true`, exit 0. deny 대상은 평가기 소스·gold·probe 소스·원본 checkout(`TTAKKARI_DEPLOY_SOURCE_ROOT`=실제 원본의 `pyproject.toml`)·`~/.zshrc`·`/etc/hosts`이며 모두 `PermissionError`. 홈과 `/private/tmp` 목록, `/dev/fd/0` 경로 open, network, fork, 작업 영역 밖 쓰기도 `PermissionError`.
- 이전 판정 기준 변경: `Availability.defect`를 추가해 환경 거부(sandbox_apply EPERM, 측정 사유 override, 플랫폼 없음)와 우리 결함(그 밖의 readiness 실패)을 구분한다. deny 테스트는 환경 거부일 때만 skip하고 결함이면 실패한다. 종료 코드는 0 통과, 2 환경 거부, 1 결함/deny 실패다.

**2. 격리를 켠 전체 테스트와 gate.** 격리 on `413 passed`(skip 0, deny 테스트 실행), 격리 off(`LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE`) `412 passed, 1 skipped`. selfcheck는 `problems: 0`, 참조해 18/18 PASS, 음성 대조 366/366 기대 사유 거부이며 flags에서 `sandbox_unavailable`이 사라졌다. pre-push gate(`scripts/verify_deploy_gate.sh`)는 `bench/local_llm_eval/` 변경 시 평가기 테스트를 `-rs`로 돌리고 이어서 `local_llm_eval.sandbox`를 실행한다. exit 0이면 통과 줄, exit 2(엔진 sandbox 등 환경 거부)이면 사유를 적고 건너뛰며 gate를 깨지 않는다. exit 1(격리 가능한데 deny가 거부하지 않거나 profile 결함)이면 gate 실패다. 즉 이번 `-6` 같은 결함은 더 이상 조용히 skip되지 않는다.

**3. 과잉 차단 완화.** 허용·거부와 이유는 명세 §0.1.1 '4.2 리뷰 후속 완화'가 정본이다. 요약: dunder 읽기 27종 허용(`super().__init__`, `__class__`/`__name__`/`__doc__` 등), `dir()`·후보 소유 인스턴스의 `vars()`, `sys` 상수·재귀 한도, `os.getcwd/environ/getenv/sep`, `threading` 동기화 객체, `argparse`, `shutil` 복사·이동·삭제. 거부 유지: dunder 쓰기와 `__dict__`/`__globals__`/`__self__`/`__getattribute__`/`__setattr__` 등, `sys.modules/stdout/_getframe/exit`, `Thread`/`Timer`, 모듈·클래스에 대한 `vars`, `traceback`, 후보 작성 테스트의 sys/os/threading/shutil. `io.FileIO`는 거부, `io.open`은 내장 `open`과 같은 가드로 교체했다. 남은 위험(격리 off에서 `pathlib` 등의 `/dev/fd` 경로 open)은 명세에 적었다. 판정 경계(전용 pipe frame, 부모 판정, OS 격리)는 건드리지 않았다. 기존 `test_sys_exit_zero_is_an_error_observation`은 정적 import 거부에서 런타임 capability 거부(`module capability sys.exit`)로 바뀐 점을 반영했으며 여전히 거부다.

정상 변형 표본(`evidence/4.2/isolation-fix/variants.py.txt`, 46종×8과제, 격리 on): 이전에 0/8이던 `super().__init__`, `__class__.__name__`, `os.getcwd`, `os.environ`, `sys.maxsize`, `sys.setrecursionlimit`, `threading.Lock`, `shutil`이 7/8로 올랐다. 나머지 1과제는 후보가 쓴 테스트 파일을 제약하는 TST의 의도된 import 집합이다. 여전히 실패: `traceback`(이유 있는 거부), `object.__setattr__`, `w.__name__ = ...`(dunder 쓰기 거부), DBG의 '새 함수 추가' 대조.

미검증/한계: 정확한 syscall 원인 미추적. Python 3.13.15/macOS 27 한 조합. 허용한 capability 조합에 대한 적응 공격 전수 미검증. 봉인 holdout, 실모델, trace/session 연동은 미완이다. PMS 4.2는 doing 유지.
