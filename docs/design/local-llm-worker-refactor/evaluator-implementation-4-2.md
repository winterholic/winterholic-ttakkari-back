# WBS 4.2 평가기 구현 기록 (개발 과제 9개)

작성일: 2026-10-23 KST. 상태: **4.2 부분 완료.** 평가기 골격, 개발 과제 9개, 음성 대조 N1~N14 전부가 구현되어 CPU에서 재현된다. holdout 9개와 봉인 번들은 만들지 않았으므로 4.2는 완료가 아니다.

> **진행 중 기록(독립 검증 후속 수정, main ad58203c 기준):** 독립 검증이 평가기 결함 2건을 확인했다. (1) [치명] nonce handshake 위조: 후보 코드가 검사 프로세스에서 nonce를 읽어 handshake를 출력하고 `os._exit(0)` 하면 false PASS. 같은 프로세스에서 비밀을 공유하는 방식은 근본적으로 위조 가능하므로 **probe/judge 분리**(후보 프로세스는 원시 관측값만 반환, 기대값 비교는 평가기 프로세스)로 구조를 바꾼다. (2) [중요] 평가기가 시작 workspace 를 직접 해시하지 않고 호출자가 넘긴 manifest 를 믿음 → `start_dir` 을 평가기가 직접 해시. 이 상자는 수정 완료 시 아래 §9 로 대체된다(상태: 구현 중). 아래 §1~§8 은 수정 전(커밋 bcedf3e) 기록이다.

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
| 실행기 | 검사 1개당 `python -I -S -B` 프로세스, 격리된 트리 사본, nonce·검사 id 핸드셰이크, 줄 위조·exit0·`os._exit`·`TestCase.run` 등 monkeypatch 탐지, timeout 시 process group kill | §5.1-4,5 |
| 후보가 쓴 테스트 | unittest loader로 수집, gold SUT에서 통과(skip·xfail 0), 사전 검증된 mutant 사망, AST로 assertion·SUT 참조 검사, SUT 불변 | §3.7, §3.8 |
| 보고 대조 | 구조화 보고의 `changed_files` 를 실제 diff와, `commands_run` 을 trace 와 대조. 불일치는 `FAIL_REPORT_MISMATCH`(코드만 PASS면 `code_pass_report_mismatch` 플래그도) | §5.1-7 |
| ledger | 추가 전용 JSONL, 행 불변(중복 id 거부), 서버·모델 식별 결측은 `UNKNOWN`·`comparable=false`, wall 0초는 `zero_wall_no_response`, profile 별 요약(재시도는 비용만, 성공 키는 task×repeat 단위, profile 합산 없음) | §6.4 |
| 누출 스캔 | hidden 스니펫 리터럴과 gold 추가 토큰이 프롬프트·비테스트 소스에 있는지 어휘 수준으로 검사, 프롬프트가 명시한 토큰은 spec-derived 로 분류 | §5.4-3 확장 |

## 2. 개발 과제 9개

과제별 fixture·참조해·대안 참조해·hidden·음성 대조는 `tasks/*.py`. 기존 fixture는 `fixture_data/*.json`(원본 sha256 기록)에서 불러오고 코드는 저장소 안에서 자립한다.

| 과제 | fixture | 이번에 바꾼 것 | 검사 수 | 음성 대조 | 참조해/대안 |
|---|---|---|---:|---:|---|
| B4-EXP-D | 기존 ledger 26모듈 | 읽기 전용 과제. 정답 키를 AST로 계산(변환 2·호출자 3), 코드에 기대값을 박지 않음 | 8 | 14 | PASS/PASS |
| B4-SGL-D | 기존 median | tests 불변, 입력 비수정, 음수·실수, mean 불변 추가 | 8 | 19 | PASS/PASS |
| B4-MUL-D | 기존 inventory | **프롬프트 rev2**(수량·TOTAL 구분자, 빈 목록 명시), 두 파일 모두 변경 필수 | 8 | 18 | PASS/PASS |
| B4-API-D | **신규** notes WSGI | 계약 표 프롬프트, in-process 호출(소켓 없음), `os.replace` 스파이로 원자성 순서 검사 | 11 | 20 | PASS/PASS |
| B4-DBG-D | 기존 rules 60함수 | **rev2**: 실제 실패하는 공개 테스트와 **실제 실행으로 만든** `logs/ci_failure.log`, 원인-패치 대응 검사 | 7 | 20 | PASS/PASS |
| B4-REF-D | 기존 dedupe | common 치환 검사(sentinel), 복제 본문 AST 검사, 동결 참조 구현과 동작 비교 | 8 | 18 | PASS/PASS |
| B4-TST-D | 기존 discount | 후보 테스트가 gold 에서 통과해야 함, mutant 8개 전부 사망, SUT 참조 assertion 요구 | 8 | 21 | PASS/PASS |
| B4-REC-D | 기존 stage1 shop | round() trap, 후보 테스트의 범위 mutant 사망, 회복 지표(trace) | 9 | 21 | PASS/PASS |
| B4-LNG-D | 기존 3개 합성+filler | 5턴, 지속 제약 C1, T5 read-only, 세션 검사 인터페이스 | 19 | 26 | PASS/PASS |

합계: 음성 대조 **177건 전부 기대한 사유로 reject, false positive 0**, 참조해와 대안 참조해 **18건 전부 PASS**, delta/invariant 분류 불일치 0(`evidence/4.2/coverage_summary.json`, `selfcheck_report.json`).
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

- **격리:** 검사 프로세스는 `python -I -S -B`, 최소 환경, 사본 트리에서 돌지만 **네트워크 차단과 파일 시스템 샌드박스는 적용하지 않았다**(Seatbelt 는 4.3). 후보 코드는 검사 프로세스 안에서 import 되므로 의지가 있는 공격자는 nonce 를 읽을 수 있다(`hidden_runner.py` 에 명시). 현재 단계에서 막는 것은 우발적·소박한 우회(위조 줄, exit0, monkeypatch, sitecustomize)다.
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
| `python3 -m pytest local_llm_eval/tests -q` | `90 passed in 20.91s` |
| `python3 -m local_llm_eval.provenance.crosscheck_audit <evidence-audit>` | 61행, 0초 행 20개, t6 성공당 165.017초, t8 239.733초 |
| `ruff check --quiet bench`(저장소 루트) | 출력 없음(통과) |

증거 파일: `evidence/4.2/{selfcheck_report.json, coverage_summary.json, leakscan_report.json, ledger_crosscheck_audit.json, pytest_output.txt, environment.txt}`.
