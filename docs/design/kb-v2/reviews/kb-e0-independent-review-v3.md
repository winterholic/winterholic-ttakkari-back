# KB-E0 수정본 독립 재검증 3회차 (5b788a3)

**판정: passed (DB, UI, 추론을 제외한 E0 로직 승인). SHA `5b788a38cd0d6360e37e106b136f1c1cf693caf8`** (branch `feat/kb-e0-harness`, 직전 검증 `2e62817`).

단서가 셋 있다. 모두 승인을 막지 않는다고 판단했지만 사실 그대로 적는다.

1. 작성자가 보고한 "mutation 26/26 killed"는 재현되지 않았다. 같은 스크립트를 두 번 다시 돌려 25/26이 나왔다.
2. 지연 퇴화 하한 0.1 ms는 크기를 고려하지 않는 절대값이라, 평균 지연이 수 ms 이하인 정상 입력을 `unverified`로 떨어뜨린다. 거짓 pass가 아니라 거짓 보류다.
3. 단위 테스트가 지키지 않는 판정 조건이 남아 있다. 직접 만든 속성 반례로는 모두 올바르게 동작함을 확인했다.

실제 PostgreSQL, 실추론, GPU, 브라우저, 모바일 UI 검증은 이번 범위가 아니라서 실행하지 않았다.

## 검증 환경

- clone: `/tmp/kb-e0-v3-verify`, SHA 고정 checkout. 공유 작업 트리, `git worktree`, 공유 git 설정은 쓰지 않았다. 소스는 수정하지 않았고 commit, push도 하지 않았다. 변이 실험은 모두 `/tmp`의 별도 복사본에서 했다.
- 런타임: Python 3.13.15, NumPy 2.5.4, Pydantic 2.14.0. 작성자 clone의 venv 인터프리터를 쓰고 `PYTHONPATH`를 이번 clone의 `src`로 지정했다. 스크립트가 `ttakkari.kb_eval.__file__`이 이번 clone 안인지 assert한다.
- 지난 검증의 outbox 스크립트는 이번 Run에서 접근할 수 없었다. 같은 반례를 `kb-e0-v3-regression.py`로 다시 작성했다(내용은 지난번과 같고, 지난번 확인 과정에서 고친 입력 오류 세 곳은 처음부터 반영했다).

## 1. N1 반례와 우회 시도

### 1-1. 지난번 반례 두 개: 막힘

| 입력 | 결과 |
|---|---|
| 후보 900 + `i*1e-9` ms, 대조군 1000 상수 | `relative_unverified`, 폭 8.9e-12, 이유 `degenerate_arm_variance`, `degenerate_ratio_ci` |
| 후보와 대조군 모두 `i*1e-9` jitter | `relative_unverified`, 폭 8.9e-13 |
| 비율 1.18, 후보에 `i*1e-6` ms jitter | `relative_unverified`, 폭 8.9e-9 |
| 품질 delta `i*1e-9` × 40 cluster, 같은 pilot | `inconclusive`, 이유 `degenerate_bootstrap_interval`, `power_not_established`, power `insufficient` |

### 1-2. 우회 시도

| 시도 | 결과 |
|---|---|
| 후보를 대조군의 정확히 0.9배로 만듦(두 arm은 크게 변하지만 비율이 상수) | `relative_unverified`, 폭 2.2e-16 |
| 위 입력에 곱셈 잡음 `1e-6*i` 추가 | `relative_unverified`, 폭 8.4e-5 |
| 후보를 대조군과 동일한 trace로 재생 | `relative_unverified`, 폭 0 |
| 후보만 jitter 1e-9, 대조군은 정상 변동(SD 30ms) | `relative_unverified`, 이유 `degenerate_arm_variance` |
| 6% block에만 +0.2ms, +2ms 이상치(p95가 두 값 사이를 오감) | `relative_unverified`(폭 2e-5, 2e-4). +20ms는 `relative_pass`(폭 2e-3) |
| `degenerate_width`에 NaN, 역전된 구간, `(inf, inf)` | 모두 퇴화로 판정. `(0.0, inf)`만 비퇴화 |

### 1-3. 하한을 조금 넘는 잡음: 통과함 (결함 아님)

두 arm 모두 서로 다른 값의 순열 ramp(간격 eps)를 얹고 비율 0.9, 100 block으로 측정했다.

| eps | arm 표준편차 | CV | ratio CI 폭 | 판정 |
|---|---|---|---|---|
| 0.0035 ms | 0.101 ms | 1.1e-4 | 4.6e-5 | unverified (`degenerate_ratio_ci`) |
| 0.03 | 0.87 ms | 9.6e-4 | 3.5e-4 | unverified |
| 0.1 | 2.9 ms | 3.2e-3 | 1.2e-3 | **relative_pass** |
| 1.0 | 28.9 ms | 3.2e-2 | 1.1e-2 | relative_pass |

품질 쪽도 마찬가지다. 교대 ±0.002 delta(pilot도 같은 값)는 `pass`, power `established`, 중립 power CI `[0.981, 1.0]`이다. ±0.0015는 CI 폭 9e-4로 `degenerate_bootstrap_interval`에 걸린다.

판단: 지난번 N1은 1 ns 미만 잡음이 `pass`를 만드는 정수 문턱 문제였다. 이번 하한은 그보다 5~6자리 위에 있고, 하한을 넘는 입력이 통과하는 것은 하한 기반 검사의 본질이다. 작성자도 "필요조건일 뿐 충분조건이 아니다"라고 명시한다. 이 통과 영역은 보정(calibration) 문제로 남는다. 다만 pilot 표준편차 0.002(마진 0.05의 4%)로 power `established`가 나오는 점은, 실제 query별 delta의 표준편차가 보통 0.1 안팎이라는 점에 비춰 낙관적이다. pilot이 실측되기 전까지 `established`를 근거로 쓰면 안 된다. 현재는 `compare_runs`가 `dev_deltas`를 넘기지 않아 어떤 경로로도 `pass`가 나오지 않는다.

### 1-4. 크기를 바꿨을 때: 거짓 음성 확인 (낮음)

100 block, 대조군 평균 e2e와 CV별 판정(후보는 0.9배, 변동은 정규분포):

| 평균 e2e | CV 0.5% | 1% | 2% | 5% | 10% |
|---|---|---|---|---|---|
| 0.5 / 1 ms | UNVER | UNVER | UNVER | UNVER | UNVER |
| 2 ms | UNVER | UNVER | UNVER | UNVER | pass |
| 5 ms | UNVER | UNVER | UNVER | pass | pass |
| 20 ms | UNVER | pass | pass | pass | pass |
| 200 / 800 / 5000 ms | pass | pass | pass | pass | pass |

UNVER은 모두 `degenerate_arm_variance`다. 평균 20ms 이하에서 CV가 작은 정상 입력이 `relative_unverified`로 떨어진다. 구조적 이유는 두 가지다.

- arm 표준편차 하한이 절대값 0.1 ms라서 평균 지연에 비례하지 않는다.
- ratio CI 폭 하한 5e-4는 평균 5 ms에서 2.5 µs, 800 ms에서 0.4 ms에 해당한다. "0.1 ms = 200 ms SLO의 0.05%"라는 근거는 200 ms에서만 성립한다.

이 문제는 거짓 pass가 아니라 거짓 보류라서 안전한 쪽이다. 설계가 측정하려는 영역(SLO 200/800 ms, 설계 문서의 lexical 측정 p50 24 ms)에서는 CV 0.5% 이상이면 통과하므로 실제 영향은 작다. 평균 수 ms 이하의 경로(캐시 적중, 인메모리 조회)를 비교하게 되면 막힌다.

용어 문제도 있다. 정책과 README는 0.1 ms를 "timer 해상도"라고 부르지만, 이 호스트의 `perf_counter` 해상도는 4.17e-8초(`mach_absolute_time`)라 실제 해상도보다 약 2,400배 거칠다. 0.1 ms는 해상도가 아니라 변동 하한이다.

### 1-5. 정보가 많아 CI가 좁아지는 경우: 이상 없음

- 지연: CV 5%, 평균 500 ms에서 block 20,000개일 때 폭 3.7e-3으로 문턱(5e-4)보다 7배 넓다. 폭은 대략 1/√n으로 줄어 문턱은 block 수백만 개에서야 닿는다.
- 품질: delta ~ N(0, 0.1), cluster 40,000개에서 폭 1.9e-3으로 문턱 1e-3보다 넓다(이 입력은 65초 걸렸다).
- 즉 정밀할수록 퇴화로 오판하는 문제는 현실 범위에서 나타나지 않는다.

### 1-6. 정확히 같은 출력(delta가 모두 0)

`quality_family`에 delta 전부 0, 건강한 pilot을 넣으면 `inconclusive` (`degenerate_bootstrap_interval`)가 나온다. 표본 40개에서 차이가 0으로 관측됐다는 사실은 모집단 차이를 한정하지 못한다(재표본 구간 `[0, 0]`이 오히려 반보수적이다). 그래서 보수적 판정이 맞다고 본다. 비트 단위로 같은 순위를 내는 리팩터링을 증명하려면 정확 재생 같은 별도 경로가 필요하다. 지난 회차부터 있던 성질이다.

## 2. 하한 값의 근거

| 하한 | 값 | 판단 |
|---|---|---|
| 지연 arm 표준편차 | 0.1 ms | 근거 문구는 200 ms SLO 한 점에만 맞는다. 크기와 무관한 절대값이라 평균 수 ms 이하에서 거짓 보류가 난다(1-4). 이름이 "해상도"인 것도 부정확하다. |
| ratio CI 폭 | 5e-4 | `0.1/200`으로 도출했지만 위 근거를 그대로 물려받는다. `fast` 모드 SLO에는 맞고 `full`(800 ms)에는 4배 느슨하다. 방향은 안전한 쪽이다. |
| 품질 CI 폭 | 1e-3 | 마진 0.05의 1/50이라는 도출이 설명된다. 다만 "float 잡음 수준"이라는 표현은 맞지 않는다. float 잡음은 1e-16 단위이고 1e-3은 "사실상 상수"에 가깝다. 적용 크기는 일관된다. |
| 품질 pilot 표준편차 | 1e-3 | 같은 평가. 이 값 근처 pilot은 현실적인 변동에 비해 지나치게 작다(1-3). |

네 값 모두 "실측 pilot 전의 공학적 하한"이라고 정책에 명시되어 있다. 이 전제를 지키는 한 근거는 타당하다. 크기를 고려하는 형태(예: 평균에 비례하는 하한과 절대 하한의 조합)로 바꾸는 일은 E0.5 전에 해 두기를 권한다. 실측 trace로 재검토한다는 문구가 이미 있다.

위 표 외에, 정책 JSON(`kb-eval/statistical-policy.json`)은 여전히 런타임에서 읽히지 않는다. 코드 상수 `BOOTSTRAP_POLICY`가 권위이고, 단위 테스트가 두 쪽의 일치를 강제한다. 이번 실행에서 `policy json == BOOTSTRAP_POLICY constant: True`를 직접 확인했다.

## 3. 작성자 mutation 재실행과 직접 고른 변이

### 3-1. 작성자 스크립트 재실행: 25/26, 보고와 다름

`reports/kb-e0/review-fixes-2/reason_mutation.py`를 별도 복사본에서 `py` 경로만 바꿔 두 번 돌렸다(바이트코드 비활성/활성 각 1회).

```text
mutants=26 killed=25 survived=1; sources restored      (두 번 모두)
SURVIVED statistics.py 245 insufficient_clusters | 130 passed
```

작성자 기록(`mutation-final.json`)은 같은 mutant를 `killed`로 적고 출력은 `1 failed, 15 passed in 0.17s`다. 이 출력은 바로 앞 mutant(`statistics.py 243`)와 글자 그대로 같다. 새 복사본에 `insufficient_clusters` 변이만 직접 적용해도 130개가 모두 통과한다. 작성자 기록이 틀린 원인은 확인하지 못했다. 연속한 mutant가 같은 크기의 소스와 같은 초 단위 mtime을 가져 낡은 `.pyc`를 재사용했을 가능성을 의심했으나, 바이트코드를 켜고 재실행해도 재현되지 않아 가설은 검증하지 못했다.

실제 영향은 작다. 단일 cluster에서는 `power_not_established` 등 다른 이유가 함께 붙고 `passed`도 `False`라서 상태는 같다. 바뀌는 것은 이유 목록뿐이다. 속성 반례(`P-Qc`)는 실제 코드에서 `insufficient_clusters`를 확인하고 변이 코드에서는 실패한다.

### 3-2. 직접 고른 변이 59개

대상은 이번 수정의 하한과 판정 조건, 지연 이유 조건, 품질 gate 분기, 지표 정의, 결함 1~4의 가드, 캐시/재생 가드다. 결과는 killed 42, SURVIVED 17이다.

**killed(주요 항목):** 이번 수정의 N1 변이 전부(arm 표준편차의 `.any()`→`.all()`, 두 하한을 1e-12 또는 0으로, `degenerate_width`의 NaN 안전성과 경계 포함 여부, 보고 필드), N2 대상이던 `load_imbalance`(L4), `order_load_imbalance`(L5), `profile_mismatch`(L6), `order_imbalance`(L7), 600초 하한(L3), 신뢰구간 판정 경계(L8, L9), SLO 값(L10), replicate 하한(L11), weight 검증(L14), p값 재중심(Q4), 지표 정의(X1~X5), split 검사 자체(D1a, D1d, D1f), 비-fixture 권리 hold(D3a, D3d, D3f), `open_holdout` 거부와 actor 표기(D4a~D4d), 재개·재생 가드(R1~R6).

**SURVIVED 17개**(단위 테스트가 변이를 알아채지 못함). 각각을 내 속성 반례로 다시 판정했다(`kb-e0-v3-extra-probes.py`, 실제 코드에서 18/18 PASS).

| 변이 | 직접 반례 | 판단 |
|---|---|---|
| L1 order 불균형 허용치 >1→>3 | AB 51/BA 49에서 `order_imbalance` 확인. 변이에서는 실패 | 테스트 공백 |
| L2 100 block 하한→10 | 99 block × 7초에서 `insufficient_observation`. 변이에서는 실패 | 테스트 공백 |
| L12 critical-path 검사 제거 | `queue+service > e2e`에서 `timer_critical_path_inconsistent`. 변이에서는 실패 | 테스트 공백 |
| L13 block id 중복 검사 제거 | `duplicate_latency_block`. 변이에서는 실패 | 테스트 공백 |
| Q3 fail 조건 `high < 1` | 넓은 CI의 경계 사례가 `inconclusive`. 변이에서는 `fail`로 바뀜 | 테스트 공백(inconclusive 영역 미검증) |
| Q7 power 중립 CI 하한 0.8→0 | pilot SD 0.2에서 중립 통과확률 CI `[0.30, 0.43]`로 `insufficient`. 변이에서는 실패 | 테스트 공백 |
| D1b `gold_document_mismatch` 제거 | holdout 쌍이 없는 문서 불일치를 거부. 변이에서는 실패 | 이중 방어(누수 자체는 D1a가 따로 막음) |
| D1c group join 제거 | 같은 group 라벨, 서로 다른 문서가 `split_leakage`. 변이에서는 실패 | 테스트 공백 |
| D1e anchor 문서 불일치 검사 제거 | `anchor_document_mismatch`. 변이에서는 실패 | 테스트 공백 |
| D3b, D3c, D3e 권리 세부 검사 | `fixture` + 외부 provenance, `review_evidence` 첨부, links 존재 + `transform_allowed=False`를 각각 거부. 변이에서는 실패 | 테스트 공백 |
| Q1 `low > -1`, Q2 Holm 무시 | 반례를 만들지 못함 | 근사 동등. 두 하한이 모두 `-margin`을 넘으면 Holm 보정 p도 대개 0.05 이하라서 서로 겹친다(엄밀한 증명은 못함, 편향 분포에서는 다를 수 있음) |
| Q5 power trial 하한 200→1, Q6 cluster 하한 20→1 | 반례를 만들지 못함 | 동등 변이. Wilson 상한 `0/n ≤ 0.05`는 n이 약 72 이상일 때만 성립하고, effective cluster ≥20이 cluster 수 ≥20을 이미 포함한다 |
| Q8 power loss CI 상한 0.05→1 | 반례를 만들지 못함 | 미판정. 중립 통과확률이 높은 조건에서 손실이 통과하는 pilot을 만들 수 없었다 |

결론: 생존한 변이 중 동작 결함으로 이어지는 것은 없다. 속성 반례가 있는 항목은 실제 코드가 맞게 동작하고 단위 테스트만 보호하지 않는 경우다. 그러나 작성자의 "26/26"은 `reasons.append(...)`와 `"reasons": [...]` 리터럴만 대상으로 한 점수라서 판정 이유를 만드는 조건과 가드의 보호 정도는 보여 주지 않는다. 앞선 두 회차에서 밝힌 테스트 공백(`M4`, `M12`, `M13`)도 그대로다.

## 4. `public_regression_only` 분기 삭제

설계 요구를 어기지 않는다.

- 요구: 공개 세트는 회귀 감시 전용이며 독립 일반화나 사용자 승인으로 표시하면 안 된다(설계 V6, 402행, E1a 완료 조건 839행).
- 현재: 비-fixture 입력은 `validate_rights`가 loader와 runner 양쪽에서 `rights_review_authentication_required`로 거부한다. 이 거부를 지나는 공개 결과 경로가 없고, `final_quality`는 `not_authorized`로 고정이다. 삭제된 분기는 도달 불가능한 코드였다.
- 검증: `kind=public`, `custom`, 위조한 `review_evidence`, 로드 뒤 `kind` 변조가 모두 거부됐다. 정상 fixture 보고서에는 `public_regression_only` 항목이 없고 `final_quality=not_authorized`다.
- 남는 부담: E1a에서 공개 자료를 처음 받아들일 때 이 제한을 테스트와 함께 다시 넣어야 한다. 작성자도 `kb-eval/README.md`와 `runner.py`의 NOTE에 이를 명시했다. 현재 단위 테스트는 이 항목이 "없음"을 단언하므로, 그때 같이 고쳐야 한다.

## 5. 이전 결함 1~4와 수기 oracle, CLI 회귀: 변화 없음

`kb-e0-v3-regression.py` 실행 결과 `122/122 PASS`. 내용은 지난 회차와 같다.

- **결함 1(split 누수):** 지난번 재현 입력을 포함한 12가지가 모두 같은 사유로 거부된다. 새로 `passage id`가 다른 문서 key와 같은 이름 혼동 반례(S9)도 `gold_document_mismatch:q1:d5`로 거부된다.
- **결함 2(latency gate):** 원 재현 입력(`replicates=1`, NaN/inf/음수/0/문자열/None duration, weight 이상값, arm 값 이상값, block 99개/599초/AB 편중/빈 입력/중복 block, 비list 입력)이 모두 거부 또는 `relative_unverified`다. 양성 대조는 `relative_pass`, 음성 대조(650 대 500)는 `relative_fail`, 둘 다 절대 실패여도 상대는 pass인 분리가 유지된다.
- **결함 3(권리):** 가짜 URL "human checked" public, custom, 위조 evidence, 외부 provenance, https URI, `unverified`, 중복 component, `database_export`, 로드 뒤 변조 두 가지가 모두 거부된다. 정상 fixture는 로드되고 보고서는 `authority=none`이다.
- **결함 4(audit 위조):** `open_holdout` 6가지 입력이 모두 거부되고 audit 파일과 디렉터리가 생기지 않으며 기존 파일도 그대로다. human event, 버전 위조, `human_verified` 위조도 거부된다.
- **수기 oracle(fixture):** Recall 0.75, nDCG 0.381735339808, MRR 0.75, candidate recall 0.75가 12자리까지 일치한다. false strong 1/2, false abstain 1/2, 답 없음 none recall 0/1, abstain F1 0, 전체 answerable Recall 구간 `[0.75, 0.75]`도 일치한다. 반복 quote는 `anchor_ambiguous`, 손상한 quote는 `gold_invalid`로 `input_invalid`. Holm `(0.01, 0.04, 0.03)` → `(0.03, 0.06, 0.06)`.
  작성자 보고서가 적은 "Recall 1, nDCG .586"은 `kb-eval/fixtures/oracle.json`이 아니라 단위 테스트 쪽 oracle의 값이다. 서로 다른 데이터라 모순은 아니다.
- **캐시/위조:** 예측을 바꿔 같은 out 디렉터리로 재실행하면 `resume_manifest_mismatch`, manifest만 지우거나 cache만 남기고 바꾸면 `cache_key_mismatch`, cache payload와 hash를 함께 위조하면 `replay_mismatch`. 비교 단계의 위조 report는 `replay_mismatch`, 선언하지 않은 처리 변경은 `unregistered_treatment`. 정상 비교는 항상 `inconclusive`이고 `final_quality=not_authorized`.
- **CLI(실제 subprocess):** `--limit 1` 중단 exit 3, 재개 exit 0(3개 완료), 정확 재생 exit 0과 `deterministic.json` byte 동일, 새 디렉터리 실행도 byte 동일, `os._exit(137)` 하드 킬 뒤 재개 후 byte 동일하고 임시 파일 없음, 동시 실행 exit 2 `artifact_writer_active`, 누수/권리/public 입력 exit 2, holdout 포함 번들 exit 2, 바뀐 입력 exit 2, 손상된 cache exit 2.

## 실행한 명령과 핵심 출력

```text
$ python -m pytest unit_tests/kb_eval -q
130 passed in 23.38s
$ python -m pytest unit_tests/test_knowledge_evaluation.py unit_tests/test_kb_store_pure.py unit_tests/test_collector_contracts.py -q
24 passed, 4887 subtests passed in 1.34s
$ python -m ruff check ... ; python -m ruff format --check ...
All checks passed! / 23 files already formatted
$ python kb-e0-v3-regression.py <clone>
122/122 PASS
$ python kb-e0-v3-n1-probes.py <clone>
A1~A4: 지난 반례 4개 모두 unverified/inconclusive. B2 eps=0.1: relative_pass(CV 0.32%). C: 크기 표. D/E: 정밀도 벌점 없음.
$ python kb-e0-v3-mutation.py <clone>       (직접 고른 변이 59개)
killed 42 / SURVIVED 17
$ python kb-e0-v3-extra-probes.py <clone>   (생존 변이용 속성 반례)
18/18 PASS  (변이 적용 시 해당 반례가 실패)
$ reason_mutation.py (작성자 스크립트) 재실행 x2
mutants=26 killed=25 survived=1; sources restored
```

원시 출력은 outbox의 `raw/`에 있다. 지난번과 달리 이번에는 검증 clone의 `git status`가 보고서 한 파일만 untracked다.

## 미실행

- 실제 PostgreSQL: 범위 밖 지시에 따라 시도하지 않았다.
- 실추론, GPU/Metal, 브라우저와 모바일 UI(320/390px): 미실행.
- 설계 문서 전달 검증 스크립트(`check_design_delivery.py`)와 실제 공개 자료 권리 검토, 인증된 reviewer flow, 실서버 지연·자원 측정: 미실행(E0 로직 범위 밖).
- 변이 59개 중 Q8(power loss CI 상한)은 반례를 만들지 못해 판정하지 않았다.

## 남은 권고 (승인을 막지 않음)

1. 지연 퇴화 하한을 평균에 비례하는 값과 절대값의 조합으로 바꾸고, "timer 해상도" 표현을 변동 하한으로 고친다. 평균 수 ms 이하 경로를 비교하기 전에 해 둔다.
2. 단위 테스트를 보강한다. 100 block 하한, order 불균형 허용치, critical-path 검사, block id 중복, 품질 `inconclusive` 영역, power 중립 CI 하한, 결함 1~4의 세부 가드(D1c, D1e, D3b, D3c, D3e)가 대상이다. 이 보강을 마치면 작성자의 "26/26"과 같은 방식의 점수로는 보이지 않는 구멍이 닫힌다.
3. 작성자의 `reason_mutation.py` 결과 기록이 재현되지 않은 원인을 확인하고, 필요하면 mutant마다 `PYTHONDONTWRITEBYTECODE=1`을 쓰도록 한다.
4. 품질 pilot의 `quality_dev_min_std`를 실측 pilot에서 재산정하기 전에는 `power=established`를 승인 근거로 쓰지 않는다.

이 판정은 DB, UI, 추론을 제외한 E0 로직의 승인이다. E0 완료, E0.5 엔진 선택, KB3/제품 검색, 최종 품질 승인은 여전히 승인되지 않았다.
