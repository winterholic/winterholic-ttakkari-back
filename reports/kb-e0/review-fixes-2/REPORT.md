# KB-E0 독립 재검증 2회차 결함 수정 보고서

상태: **독립 재검증 대기.** 2회차 판정은 failed였고, 이 보고서는 자가 검증 결과만 담는다. E0 완료, main 병합, 배포, 다음 KB 단위는 미승인이다.

- 기준: `feat/kb-e0-harness` `2e62817925983e47eaf0d6b38122d0e235b3815f`
- 2회차 판정: 이전 결함 4건은 검증자 반례로 막힘 확인. 새 결함 N1, N2.

## N1 퇴화 CI 판정이 정확한 값 일치만 잡음

**재현 (수정 전, `red-n1-repro.txt`)**

```text
N1-latency: relative_pass [0.9000000000881487, 0.9000000000970501] []
N1-quality: pass [] established
```

**수정.** 정확한 일치 비교(`low == high`)를 없앴다. 사전등록 하한 미만이면 퇴화로 보는 `degenerate_width(low, high, minimum)`로 바꿨다. NaN 폭도 퇴화로 처리한다.

| 기준 | 값 | 근거 | 적용 |
|---|---|---|---|
| `latency_resolution_ms` | 0.1 ms | timer 해상도 하한. fast SLO 200 ms의 0.05%로, 판정에 의미 있는 지연 변화보다 작고 ms 값의 float 잡음보다 훨씬 크다. | arm별 e2e 표준편차가 이보다 작으면 `degenerate_arm_variance` |
| `latency_ratio_ci_min_width` | 5e-4 | 위 해상도를 p95 ratio 척도로 옮긴 값(0.1/200) | ratio CI 폭이 이보다 작으면 `degenerate_ratio_ci` |
| `quality_ci_min_width` | 1e-3 | 비열등 margin 0.05의 1/50 | 어느 metric CI 폭이든 이보다 작으면 `degenerate_bootstrap_interval`로 `inconclusive` |
| `quality_dev_min_std` | 1e-3 | 같은 척도. 이보다 작은 dev 분산은 합산 순서 같은 float 잡음 수준이다. | dev delta 표준편차가 이보다 작으면 power `insufficient` |

- 기준값은 `kb-eval/statistical-policy.json`의 `degeneracy`에 근거와 함께 등록했다. 정책 version은 `kb-e0-statistics-v2`다.
- 코드 상수는 `validation.BOOTSTRAP_POLICY`에 있고, 테스트가 JSON과 같은지 강제한다.
- 이 값들은 실측 pilot 전의 공학적 하한이다. 하한을 넘는 것은 정보 있는 구간의 필요조건일 뿐 충분조건이 아니다.

**수정 후 (`green-n1-repro.txt`)**

```text
N1-latency: relative_unverified [0.9000000000881487, 0.9000000000970501] ['degenerate_arm_variance', 'degenerate_ratio_ci']
N1-quality: inconclusive ['degenerate_bootstrap_interval', 'power_not_established'] insufficient
```

**테스트 (`unit_tests/kb_eval/test_review_fixes_2.py`, RED 7건 `red-n1.txt` → GREEN `green-n1.txt`)**
- 재현 두 가지
- 경계값
  - `degenerate_width`: 하한과 같으면 비퇴화, 0.999배면 퇴화, NaN이면 퇴화
  - 지연 CI 폭: jitter 0.02(폭 약 2.6e-4)는 퇴화, 0.08(약 1e-3)은 비퇴화
  - 품질 CI 폭: 진폭 0.001(폭 약 6e-4)은 퇴화, 0.003(약 1.8e-3)은 비퇴화
  - power의 dev 분산: 진폭 0.0009는 insufficient, 0.01은 established
- 한 arm만 해상도 미만으로 상수인 경우
- 정상 대조: 변동 있는 지연의 pass/fail, 품질 중립 pass와 −0.10 손실 fail, 모두 이유 목록 `[]`

## N2 부하 불균형 테스트 약화와 판정 이유별 mutation

**도구.** `reason_mutation.py`가 `reasons.append("X")`, `blockers.append("X")`, `"reasons": [...]` 안의 각 이유를 하나씩 지운다. 그때마다 E0 테스트를 실행하고 원본으로 복원한다. 마지막에 소스 hash가 원상태인지 확인한다.

**기준선, 수정 전 `2e62817` (`mutation-baseline-2e62817.txt`): 26개 중 8개만 잡고 18개 생존.** 생존 원인은 세 가지였다.
1. 다른 이유가 함께 나와 가려진 경우: N2의 `load_imbalance`. `order_imbalance`, `profile_mismatch`, `insufficient_observation`, `no_observations`도 같은 상태였다.
2. 테스트가 상태만 단언하고 이유 목록은 확인하지 않은 경우: comparison 3개, runner 3개, metrics abstain, reproducibility 4개, 품질 `bootstrap_replicates_below_registered_minimum`.
3. 도달할 수 없는 분기: `runner.py`의 `public_regression_only`. 권리 hold 이후 fixture가 아닌 입력은 runner에 도달하지 못한다.

**수정**
- N2: `test_ab_load_bias_and_candidate_only_regression`이 `varied(blocks(biased=True))`를 쓰고 `reasons == ["load_imbalance"]`를 단언한다.
- 지연 이유: 변동 있는 입력에서 각 이유가 유일한 원인으로 나오는지 단언한다.
- comparison, runner, abstain, reproducibility, 품질 이유: 이유 목록 전체를 정확히 단언한다.
  - reproducibility에서 `stable_core_changed`는 수학적으로 `outside_boundary_near_tie` 없이 나올 수 없다. 그래서 단독 원인 대신 정확한 목록으로 고정했다.
- `public_regression_only`: 도달할 수 없는 분기를 제거하고 NOTE를 남겼다. 권리 검토 flow를 도입할 때 테스트와 함께 다시 넣어야 한다.

**최종 (`mutation-final.txt`)**

```text
mutants=26 killed=26 survived=0; sources restored
```

새 이유 `degenerate_arm_variance`가 추가되고 도달할 수 없던 1개가 빠져서 총수는 같은 26개다. `load_imbalance` 변이는 테스트 18개째에서 바로 실패해 다시 잡힌다.

## 회귀

- `tests-final.txt`: `154 passed, 4887 subtests passed` (E0 130 + 관련 24)
- `lint-final.txt`: `All checks passed!`, `23 files already formatted`. test smell 0건.
- 수기 oracle(Recall 1, nDCG .5861350236074894, MRR .5, false strong/abstain 1/2) 유지
- CLI (`cli-summary.txt`, `cli/`)

  | 단계 | 결과 |
  |---|---|
  | interrupt | exit 3 |
  | resume | exit 0 |
  | 재생 | exact_match |
  | stale manifest | exit 2 |
  | partial cache | exit 2 |
  | 권리 거부 | exit 2 |
  | 가짜 public 권리 | exit 2 |
  | split 누수 | exit 2 |

  이전 회차 CLI 증거(`review-fixes/cli`)는 덮어쓰지 않고 보존했다.
- 이전 회차 기존 테스트 중 입력이 바뀐 것은 N2의 `test_ab_load_bias_...` 하나다. 판정 의도는 강화됐다.

## 남은 위험 (낮음, 미수정, README에 기록)

- 로컬 artifact에 서명이 없다. checksum까지 다시 계산한 동일 파일 시스템 위조는 막지 못한다.
- fixture가 아닌 입력을 모두 hold하므로, E1a는 인증된 권리 검토 flow와 그 독립 검증이 먼저 있어야 시작할 수 있다.

## 실행 엔진

- 엔진은 Claude Code CLI 2.1.285다(`claude --version`, 이전 회차 기록).
- 모델 `claude-opus-5-5`는 system prompt에 적힌 자기 선언일 뿐 provider 측 증명이 아니다.
- 핵심 계산은 Python/NumPy이며 경량 모델 위임은 없다.

## 잔여 E0 차단 (미실행)

실제 DB manifest·관측, 정상 main 계약, 브라우저·모바일 UI, 인증된 reviewer·권리 flow, 실추론, 실서버·GPU 계측.
