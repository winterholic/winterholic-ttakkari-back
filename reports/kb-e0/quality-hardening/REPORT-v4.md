# KB-E0 4회차 확인 후 정정 보고서

- 4회차 판정: main `1e9bb1a` passed(E0 로직 완료 승인, DB·UI·추론 제외). [원문](../../../docs/design/kb-v2/reviews/kb-e0-independent-review-v4.md)
- 이번 작업은 정정 두 가지와 E0 완료 기록이다. 다음 단위 구현은 시작하지 않았다.
- 독립 clone에서 `origin/main` `1e9bb1a` 기준으로 작업했다.

## 1. 동등 변이 오표기

| 변이 | 이전 표기 | 반례 테스트 (`test_quality_hardening.py`) | 결과 |
|---|---|---|---|
| Q1 `low > -margin`→`low > -1` | equivalent | `test_pass_requires_every_lower_bound_above_minus_margin`: ndcg에 드문 -0.5 이상치 2개. 하한 -0.0608, Holm p 0.0002, 실제 판정 `inconclusive` | killed |
| Q2 Holm 조건 제거 | equivalent | `test_pass_requires_holm_adjusted_p_within_alpha`: 드문 +0.5 이상치 2개. 하한 -0.0401, Holm p 0.0932, 실제 판정 `inconclusive` | killed |
| Q5 trials 하한 200→1 | equivalent | `test_power_requires_registered_two_hundred_trials`: trials=100이면 `insufficient`, 200이면 `established` | killed |
| Q8 손실 CI 상한 0.05→1 | equivalent | 반례 없음 | `undecided`로 변경 |

- 반례 입력은 검증자 스크립트를 보지 않고 보고서의 설명을 바탕으로 직접 만들었다.
- Q8을 `undecided`로 둔 근거는 이렇다. -0.10 손실이 통과하려면 재표본 평균이 +0.05를 넘으면서 그 재표본의 하한도 -0.05 위여야 한다. 큰 양의 잔차는 평균을 올리지만 구간도 넓힌다. 이 논증만으로는 증명이 되지 않으므로 동등이 아니라 미판정으로 적었다.
- 실행기는 `survived_equivalent`, `survived_undecided`, `killed_but_marked_non_kill`을 따로 보고한다.

## 2. 데이터가 많을수록 판정이 나빠지는 문제 (고침)

- **판단:** 고쳤다. 퇴화 판정의 목적은 "입력에 정보가 없음"을 가려내는 것이다. CI 폭은 표본 크기에 따라 줄어드는 양이라 이 목적에 맞지 않는다. 같은 원리의 구멍이 품질 쪽에도 있어 함께 고쳤다.
- **재현:** `red-v4.txt`, `red-quality-n-monotonic.txt`
  - 지연: CV 0.3% trace가 1,000 block에서는 pass, 5,000 block에서는 `degenerate_ratio_ci`
  - 품질: SD 0.01 delta가 4,000 cluster에서 `degenerate_bootstrap_interval`(폭 6.3e-4 < 1e-3)

| 정책 v4 | 값 | 근거 |
|---|---|---|
| `latency_min_log_ratio_sd` | 1e-4 | block별 짝지은 log(candidate/control)의 표준편차다. 평균 크기와 block 수 모두에 의존하지 않는다. 값은 0.2 상대 margin의 1/2000(`latency_min_cv`와 같음)이다. v3 폭 하한 5e-4를 대체한다. |
| `quality_min_delta_sd` | 1e-3 | 지표별 질문 단위 delta 표준편차다. margin 0.05의 1/50이다. v3 폭 하한 `quality_ci_min_width`를 대체한다. |
| 폭 0 가드 | `zero_width(low, high)` = `not high > low` | 폭이 정확히 0이거나 뒤집혔거나 NaN인 구간만 잡는다. p95 tie처럼 분산이 있어도 재표본이 통계량을 움직이지 못하는 경우다. |

- arm 분산 하한 `max(1 µs, 1e-4 × 평균)`과 dev pilot 하한 `quality_dev_min_std`는 그대로 뒀다.
- 새 테스트
  - n이 늘어도 판정이 같다: 지연 CV 0.05%·0.3% × 100·1,000·5,000 block, 품질 40·400·4,000 cluster
  - 정확히 비례하는 trace와 곱셈 잡음 1e-6은 `degenerate_paired_ratio`다.
  - 짝지은 비율 하한 경계: SD 0.5e-4는 퇴화, 2e-4는 비퇴화
  - 품질 SD 경계: 0.0009는 퇴화, 0.002는 비퇴화
  - p95 tie로 폭이 0인 CI는 `degenerate_ratio_ci`다.
  - `zero_width`의 NaN과 경계
- 폐기한 v3 폭 하한을 가정한 기존 테스트 3개는 지우고 위 테스트로 대체했다. 지연 float 잡음 테스트는 이유를 `degenerate_paired_ratio`로 바꿨다.

## 3. 검증 (지시 순서)

1. **E0 테스트:** `v4-step1-e0-tests.txt` → `218 passed`
2. **mutation 전체:** `mutation-v4.txt` → `total 110, killed 104, survived 6, survived_expected_kill []`
   - 생존 6개: equivalent 5개(L7, L15, L17, Q6, Q11), undecided 1개(Q8)
   - Q1·Q2·Q5는 killed다.
   - 새 가드 변이 N1a·N1b·N1e·N1i·N1j·N1k·N1l도 killed다.
3. **관련 DB 회귀:** `v4-step3-db-regression.txt` → `217 passed, 4887 subtests passed`
   - `tests/pg_sandbox.py` 임시 클러스터를 썼고, 실행 뒤 정리됐다.
   - 구성은 KB·knowledge·collector·context library DB 13개 파일과 관련 단위 3개 파일이다.
4. **contract:** `v4-step4-contract.txt` → `63 passed, 3774 deselected`

## 4. E0 완료 기록

- 설계 v2와 그 중복 정본 4개의 현재 상태를 `KB-E0 로직 완료 승인, 제품검색 미승인`으로 바꿨다. 대상은 ADR 0063, v1 설계 두 개, 대응표, reviews README다.
- 이전 상태 `KB-E0 착수 조건부 승인, 제품검색 미승인`은 역사 상태로 남겼다.
- `check_design_delivery.py`와 `test_design_status.py`의 현재 상태 검사도 새 문구로 바꿨다.
- 3·4회차 검토 원문은 byte 그대로 보존했다.
- 완료 기록은 `docs/design/kb-v2/reviews/kb-e0-completion.md`다.

## 배포

**배포 대기.** 지시대로 예약을 시도하지 않았다. 공유 로컬 main이 분기해 있어 모든 예약이 409로 막히는 상태다.
