# KB-E0 구현 독립 검토

- 검토일: 2026-10-22 (Asia/Seoul)
- 대상 branch: `feat/kb-e0-harness`
- 고정 대상 SHA: `416cf22944a0556ee231cc69634c62fc4c7ba608`
- 독립 clone: `/Users/winterholic/development/projects/winterholic-ttakkari/kb-e0-independent-review`
- 비교 기준: 실제 merge-base `ba71814167d281d1d1ff31ba405692eb12f87a23` (`main`)
- 작성자 worker: 사용자가 제공한 실제 라우팅 `codex / gpt-6.1-sol`. 작성자 work-history에는 정확한 provider model ID가 런타임에 노출되지 않아 미확인이라고 적혀 있어 이 부분은 기록 간 불일치로 남긴다.
- 검증자 실행 환경: Codex engine. 이번 Run의 환경 probe는 GPU Metal, `ps`, `sysctl`을 이 환경에서 blocked로 측정했다. 이것은 host 전체 capability 판정이 아니다.

## 판정

**E0 부분 통과, 독립 승인 보류. 다음 단위 게이트를 닫는다.** 계산·manifest 고정·fixture CLI 재개/정확 재생의 여러 기본 경로는 확인했지만, 아래 두 correctness 결함과 권리/사람 provenance의 위조 가능성이 남았다. 또한 실제 DB manifest/관측 export, browser UI, 실추론, 실서버 지연·자원은 실행되지 않았다. `78 passed`와 lint 기록은 구현자 증거이며 본 판정을 대체하지 않는다.

**지금 막는 항목**

1. **높음 — split/gold 누수:** positive relevance passage가 case의 선언 문서에 속하는지 검사하지 않아 dev 질문이 holdout 문서를 정답으로 가리켜도 split component를 나누어 통과한다.
2. **높음 — 지연 gate 오판정:** `latency_report`가 bootstrap 횟수 1과 `elapsed_seconds=NaN`을 거부하지 않고 두 경우 모두 `relative_pass`를 반환한다.
3. **높음 — 권리 provenance 미인증:** 출처가 가짜인 `status=verified` 권리 manifest도 public bundle로 로드된다. 실제 공개자료 사용 전에 이 문자열/플래그만으로 권리 확인을 입증할 수 없다.
4. **중간 — holdout reviewer audit 위조:** 임의 actor 문자열에 `independent_reviewer` 역할을 전달하면 실제 인증 없이 `status=consumed` 기록을 만들 수 있다. 데이터 공개는 하지 않지만 감사 상태 및 1회 소비 상태를 위조할 수 있다.

## 변경 범위와 설계 대응

`main...HEAD` 실제 차이는 120개 경로(+7,925/-210)다. 설계 보완·원본 근거 복사본·응답/이력 문서, `kb-eval` fixture/README, 신규 `src/ttakkari/kb_eval/` 14개 모듈, `unit_tests/kb_eval/` 5개 파일, 실행 결과가 섞여 있다. 승인된 설계 SHA `460237722654d6cf1d540f321eae504e63028175` 반영은 문서·증거 범위에 확인된다.

검색한 변경 파일 목록에서 collector 실행 코드, main/API/MCP 제품 라우팅, Alembic migration, 실제 검색엔진/모델 어댑터, UI 구현은 없다. `docs/design/collector-platform/contract-compatibility.md`는 문서 변경이다. E0 README와 runner도 adapter를 `fixed_prediction_fixture`로 명시하고, 품질을 `not_authorized`, DB·browser·inference를 미확인/미실행으로 반환한다. 제품 검색이나 다음 KB 단위의 승인으로 해석할 수 없다.

| 승인 기준 | 확인 결과 | 판정 |
|---|---|---|
| §4, §9, §12 P1 통계·분모·group split | 손 oracle로 3질문 지표와 두 abstain 분모를 대조했다. Holm/power 음성 대조도 확인. 하지만 relevance→문서 누수 반례를 수용함. | **실패** |
| P2 anchor/revision | 반복 인용은 `anchor_ambiguous`, NFKC-only는 `normalization_review_required`, revision 변경은 `gold_invalid/source_changed`로 거부. 좌표 mapping은 conservative invalid로 남음. | **부분 통과** |
| P3 AB/BA·queue/service·p95 CI·절대 SLO | fixture arithmetic은 p95 상대/절대 결과를 분리한다. invalid duration/replicate 반례는 녹색을 반환함. 실측 timer adapter는 없음. | **실패** |
| P4 고정 manifest/cache replay | 입력 1 byte 변조는 기존 run과 resume 거부, corrupt partial cache 거부, stop→resume→exact replay 확인. 실제 inference는 미실행. | **fixture 통과** |
| 사람/권리 provenance·최종 승격 제한 | `human_verified` flag와 human write는 거부하고 run/compare는 final quality를 만들지 못한다. 권리·reviewer 신원 문자열은 인증 없이 수용된다. | **부분 통과 / provenance 실패** |
| DB manifest/관측·모바일·실추론·제품 계약 | DB export adapter 및 실제 DB 없음, UI 없음/API unmounted, live inference/제품 adapter 없음. | **미실행 / 잔여 게이트** |

### 1. Split leak — 높은 심각도

코드 위치: `src/ttakkari/kb_eval/loader.py:56-64, 82-89`, `src/ttakkari/kb_eval/integrity.py:114-125`.

**재현 입력:** oracle fixture에서 dev `q1`의 선언 문서는 `d1,d2`다. `q1.relevance`에 `d3: 1`을 넣고, 새 holdout `q4`의 선언 문서와 gold를 `d3`로 만든다. `d3`는 기존에 선언 문서와 exact-body link가 없다.

**기대:** `q1`의 positive gold `d3`가 선언 문서 밖이므로 입력을 거부하거나, relevance가 문서 관계에 연결되어 `q1`과 `q4`가 같은 split component가 되어야 한다. 현재 `components()`는 query의 `documents`, caller의 `links`, group만 union한다.

**실제:** loader는 bundle을 수용했고 `components[q1] != components[q4]`였다. 출력: `loaded=yes; dev_q1_documents=['d1','d2']; dev_q1_relevance={'d1':3,'d2':1,'d3':1}; holdout_q4_documents=['d3']; components_same=False`.

**영향:** 질문에 대한 positive answer가 다른 split 문서에서 채점될 수 있고, 그 문서가 group graph에 들어가지 않아 train/dev/holdout 누수가 검사에서 빠진다. 고정 gold hash는 이 잘못된 관계를 재현 가능하게 만들 뿐 관계가 옳음을 증명하지 않는다.

### 2. 지연 gate 입력 무결성 — 높은 심각도

코드 위치: `src/ttakkari/kb_eval/latency.py:45-46, 62-76`.

**재현:** AB/BA 각 50개, profile 동일, load 동일, 각 block 6초, 두 arm 모두 e2e 500ms/queue 100ms/service 400ms를 넣는다.

- `latency_report(..., replicates=1)` 결과 `relative=relative_pass`, `ratio_ci=[1.0,1.0]`.
- 모든 `elapsed_seconds=float('nan')`, `replicates=1000` 결과도 `relative=relative_pass`, `reasons=[]`, `ratio_ci=[1.0,1.0]`.

**기대:** 부트스트랩 표본 수가 무효/불충분하거나 관찰 시간이 NaN이면 오류 또는 `relative_unverified/inconclusive`여야 한다. 기본값 10,000만으로는 caller가 1회 CI를 요구하는 경우를 막지 못한다. NaN 합은 `<600` 검사를 통과한다.

**영향:** 검증되지 않은 degenerate CI가 상대 지연 gate를 pass할 수 있다. 재현은 합성 timer 입력이며 제품 런타임의 실제 통과 주장은 아니다. 다만 P3 판정 함수 자체가 녹색으로 오판정하는 반례다.

### 3. 권리 및 사람 provenance

코드 위치: `src/ttakkari/kb_eval/loader.py:25-33`, `src/ttakkari/kb_eval/state.py:84-107`, `src/ttakkari/kb_eval/review_api.py:1-5, 48-59`.

**권리 반례:** bundle을 `kind=public`로 바꾸고 네 권리 항목 모두 `status=verified`, 저장/변형 허용 true, `original_url=https://license.invalid/not-a-license`, `basis=human checked`로 만들었다. `load_bundle()`은 이를 거부하지 않고 public bundle로 반환했다. loader는 URL에서 원문 존재/판본/권리 증거를 확인하지 않고 `status`와 허용 boolean을 선언값으로 취급한다.

**기대:** 권리 확인 provenance를 확인할 수 없으면 public input을 deny/hold해야 한다. 현재 schema에는 검토자 인증·검증 evidence identity가 없다. 실제 공개자료는 이번 실행에서 다운로드/평가하지 않았다. runner는 public 결과를 `public_regression_only`로 제한하고 `final_quality=not_authorized`로 유지하므로 최종 pass 승격은 차단한다.

**사람 label 반례의 방어:** `verification=human_verified` import는 `human_attestation_required: authenticated import not implemented`로 거부됐다. API human write도 501 거부이며 router는 제품 app에 mount되지 않았다.

**holdout audit 반례:** `open_holdout(..., actor='user-approved-reviewer', role='independent_reviewer')`는 외부 인증 없이 `{status:'consumed', holdout_peeks:1, authorization:'external_auth_required'}`를 기록했다. 실제 holdout data를 반환하진 않지만 임의 문자열로 reviewer 소비 기록을 만들 수 있고 이후 정당한 reviewer를 막을 수 있다. 이 결과는 사람 확인 증거로 인정할 수 없다.

## 통과한 독립 계산 및 CLI 확인

직접 작성한 수기 oracle은 10개 문서 중 3문항이다. Q1의 grade 3/1 정답이 rank 3/2, Q2의 grade 2 정답이 rank 2, Q3는 no-answer인데 strong을 냈다. 수기로 계산한 aggregate는 Recall `1.0`, nDCG `0.5861350236074894`, MRR `0.5`; false strong `1/2`, false abstain `1/2`, no-answer none recall `0/1`이다. 구현 결과와 일치했다. 미판정/실패 사례를 추가하면 total/invalid와 전체 answerable Recall bounds `[2/3,1.0]`도 유지했다.

- 반복 quote는 `anchor_ambiguous`; NFKC-only match는 `normalization_review_required`로 막힘.
- 손으로 기대한 Holm 입력 `(0.01,0.04,0.03)`은 `(0.03,0.06,0.06)` 조정값과 일치. 두 group/no power는 `inconclusive`; NaN 품질 점수는 거부.
- 같은 값의 paired timing은 상대 `relative_pass`여도 control/candidate가 각각 absolute SLO 실패하면 둘 다 `absolute_fail`로 남김. AB/BA + load 불균형 fixture는 `relative_unverified`.
- 캐시 입력 1 byte 변경, stale manifest, partial cache JSON corruption은 거부. 손상 checkpoint는 신뢰해 결과를 만들지 않고 유효 cache를 재검증해 재구성했다. checkpoint는 cache로부터 재생성되는 파생 관측물이다.
- 사람 확인 boolean은 거부되고, 권리-denied fixture는 CLI exit 2로 fail, public/dev 입력도 `final_quality=not_authorized`.

## 실행 기록

### 이번 독립 clone에서 직접 실행

런타임: 작성자 clone의 Python 3.13 venv 인터프리터만 사용하고 `PYTHONPATH`를 이번 고정 clone의 `src`로 지정했다. Python `3.13.15`, NumPy `2.5.4`, Pydantic `2.14.0`. 시스템 기본 Python `3.14.7`은 NumPy가 없어 import 단계에서 실패했으므로 검증 실행에 사용하지 않았다.

```text
$ PYTHONPATH=<fixed-clone>/src <python-3.13-venv>/bin/python -m pytest unit_tests/kb_eval -q
54 passed in 4.74s
exit=0

$ PYTHONPATH=<fixed-clone>/src <python-3.13-venv>/bin/python -m pytest unit_tests/test_knowledge_evaluation.py unit_tests/test_kb_store_pure.py unit_tests/test_collector_contracts.py -q
24 passed, 4887 subtests passed in 1.26s
exit=0

$ <python-3.13-venv>/bin/python docs/design/kb-v2/reviews/check_design_delivery.py /tmp/kbv2-review/docs/design/kb-v2/reviews
evidence: 18/18 exact sha256+size; source byte comparison: True
coverage: P1-13=13/13, F1-24=24/24 plus F9a; negative controls rejected
UNEXECUTED: actual PostgreSQL/kill/replay, inference/GPU/CPU, million benchmark, product harness, deployment
exit=0

$ PYTHONPATH=<fixed-clone>/src <python-3.13-venv>/bin/python <outbox>/kb-e0-independent-oracles.py
hand oracle: Recall=1.0, nDCG=0.5861350236074894, MRR=0.5; false strong=1/2; false abstain=1/2; assertions all passed
counterexamples: latency replicates=1 -> relative_pass; NaN elapsed -> relative_pass; public fake rights -> accepted; fake reviewer -> consumed
exit=0
```

`kb-e0-independent-oracles.py`, `kb-e0-independent-cli.py`, CLI receipt, split-leak 입력과 권리-provenance 반례 입력은 이번 Run outbox에 있다. 원시 CLI stdout/stderr 및 추가 audit 출력은 독립 clone의 `reports/kb-e0/independent-cli/`에 보존했다.

```text
CLI actual subprocess sequence on this SHA:
interrupt --limit 1: exit=3, interrupted, completed=1
resume: exit=0, complete_fixture, completed=3
same-input replay: exit=0, exact_match/exact_match
rights-denied fixture: exit=2, rights_unverified:qrels
stale manifest: exit=2, resume_manifest_mismatch
partial cache JSON: exit=2, JSONDecodeError
malformed derived checkpoint + valid cache: exit=0, reconstructed complete_fixture
```

### 작성자 evidence와 미실행

작업 기록의 `78 passed, 4887 subtests passed`는 E0 54개와 관련 회귀 24개의 합으로, 이번 검증에서 각각 분리해 다시 실행했다. 작성자의 lint 및 test-smells 통과는 참고 기록이며 여기서 별도 실행하지 않았다. 구현자 기록에 positive-control power simulation은 합성 분포 조건에서 pass/fail 양방향을 확인했다고 기재되어 있다. 독립 검증에서는 직접 수기 지표, Holm, underpowered·NaN 음성 대조를 추가했다.

실제 DB manifest/관측 export와 계약 회귀는 실행하지 않았다. 사용자가 금지한 알려진 Codex command sandbox `initdb`/`shmget Operation not permitted` 경로를 재시도하지 않았다. 현재 작업의 직접 실행 오류로 표현하지 않는다. 이 환경에서 관측하지 않은 DB/host/production capability를 추론하지 않는다.

실제 browser 검증은 실행하지 않았다. 새 모바일 UI는 존재하지 않고 `review_api`는 unmounted ASGI skeleton뿐이므로 화면을 브라우저에서 검증할 대상이 없다. 320/390px 검증은 미실행이다. 실제 DB adapter/fixture, 실추론·모델, 실서버 queue/service/stage, GPU/host resource, 실제 공개자료 라이선스 review, authenticated reviewer flow, E1b human holdout도 미실행이다.

## 다음 게이트

1. relevance passage마다 owning `doc_key`를 manifest에서 resolve하고 case `documents`/anchor 관계를 검증한다. split component는 모든 positive gold source를 포함하고 위 cross-split counterexample을 거부해야 한다.
2. `latency_report` 입력을 strict schema로 검사한다. finite `elapsed_seconds`, 명시된 충분한 bootstrap 수, 재표본 분포/불확실성이 있는 known-good와 negative control을 양방향 확인한다.
3. rights review evidence를 단순 `verified` boolean에서 분리하고, 미확인/접근 불가/불완전 provenance는 public 데이터의 평가 전 단계에서 deny/hold한다.
4. 실제 인증된 reviewer context 없이 holdout을 `consumed`로 표시하지 않는다. 브라우저 UI가 만들어지기 전까지 모바일은 API skeleton/미실행으로 남긴다.
5. DB manifest/관측 fixture 및 정상 계약 gate를 DB 허용된 검증 환경에서 실행한다. 실 inference는 E0 인터페이스 요구와 실측 완료 주장을 계속 분리한다.

이 항목들 및 현재 미실행 E0 완료 조건이 닫히기 전 **E0 완료, E0.5 엔진 선택, KB3/제품 검색, collector 코드 변경, 최종 품질 승인, commit/push/main 병합/배포는 승인되지 않는다.** 이번 Run은 해당 행위를 하지 않았다.
