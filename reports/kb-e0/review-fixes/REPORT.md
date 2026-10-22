# KB-E0 독립검증 결함 수정 보고서

상태: **독립 재검증 대기.** E0 완료, 독립 승인, 다음 KB 단위, main 병합, 배포는 모두 미승인이다. 이 보고서는 자가 검증 결과만 담는다.

- 기준: `feat/kb-e0-harness` `416cf22944a0556ee231cc69634c62fc4c7ba608`
- 독립검증 판정: 78470bf8, E0 부분통과/독립승인보류, 기능결함 4건 재현
- 검토 원문: `docs/design/kb-v2/reviews/kb-e0-independent-review.md` (sha256 `9ac2cfea…a7ca`, 원본과 일치)
- 작업 위치: 독립 clone `/Users/winterholic/development/kb-e0-harness-a28c55e0`. 공유 tree, 다른 세션 dirty, 공유 git 설정은 건드리지 않았다.

## 결함 대응표

| # | 결함 | 루트 조건 | 수정 | RED 증거 | GREEN 증거 |
|---|---|---|---|---|---|
| 1 높음 | split leak | `components()`가 선언 `documents`, link, group만 union했다. relevance passage의 실제 소유 문서는 graph 밖이었다. | loader가 passage마다 manifest에서 소유 `doc_key`를 resolve한다. positive gold 소유 문서가 선언 문서 밖이면 `gold_document_mismatch`. `components()`는 `passage_owners`를 필수로 받아 positive gold 소유 문서도 union한다. | `red-four-defects.txt` (DID NOT RAISE) | `green-r1.txt`. CLI `split_leak` exit 2 `gold_document_mismatch:q1:d3` |
| 2 높음 | latency false pass | 입력 형/범위 검사가 없었고 replicates 하한이 없었다. NaN duration 합계는 비교에서 조용히 통과했다. | `LatencyBlock` strict schema(finite, gt/ge 0, weight=1 고정, 결측 stage 사유 필수). 사전등록 정책 `kb-eval/statistical-policy.json`(10,000회, 근거와 "충분성 근거 아님" 명시) 미만이면 `relative_unverified`, CI는 `null`. 같은 경계를 품질 통계(`cluster_interval`, `holm`, `_family`, `simulate_power`)에 적용. | `red-four-defects.txt` (replicates=1, NaN 모두 relative_pass) | `green-r2.txt` |
| 2 보완 | 퇴화 CI pass | 상수 trace는 10,000회에서도 CI [1,1]로 `relative_pass`였다. 품질 family도 상수 delta면 같은 구멍이 있었다. | 재표본 spread가 0이면 `degenerate_ratio_ci` → `relative_unverified`. 품질 family는 `degenerate_bootstrap_interval` → `inconclusive`. `cluster_interval.sampling_status=unverified_degenerate_interval`. | `red-degenerate-latency-ci.txt`, `red-degenerate-ci.txt` | `green-degenerate-ci.txt` |
| 3 높음 | 권리 provenance | loader가 `status=verified`와 허용 boolean 선언을 권리 확인으로 취급했다. | `rights.validate_rights`: 인증된 권리 검토 flow가 없으므로 비 fixture 입력은 loader와 runner 양쪽에서 `rights_review_authentication_required`로 hold. 증거 identity/version/content hash/확인자 context는 schema에서 분리했지만 서술 정보로만 둔다. fixture는 명시적 `provenance_kind=synthetic`, `fixture://`, evidence 없음일 때만 실행하고 `authority=none`을 기록. | `red-four-defects.txt`, `red-rights-runner-boundary.txt` | `green-r3.txt`. CLI `fake_public_rights` exit 2 category `rights` |
| 4 중간 | reviewer audit 위조 | `open_holdout`이 actor/role 문자열만으로 `consumed` 파일을 배타 생성했다. | 인증 flow가 없으므로 mkdir/open/hash 전에 `reviewer_authentication_required`로 거부한다. 파일 시스템 변경 0. 거부가 향후 정당한 개봉 slot을 소비하지 않는다. 인접 경계: review event label에 `actor_authentication=unauthenticated_claim` 기록. | `red-four-defects.txt` (consumed audit 생성), `red-review-actor-claim.txt` | `green-r4.txt`, `green-review-actor-claim.txt` |

### 기존 테스트 입력 변경 (의도 유지)

퇴화 CI를 unverified로 바꾸면서 상수 입력에 기대던 기존 테스트 4개의 **입력만** 블록별 고유 jitter(`varied`)로 바꿨다. 판정 의도는 그대로다.

- `test_control_also_exceeds_slo_relative_is_separate`: 상대 pass와 양 arm 절대 fail 분리 유지. CI 단언을 `[0.9,0.9]`에서 `0.85 < low < high < 0.95`로 변경.
- `test_ab_load_bias_and_candidate_only_regression`: candidate-only 회귀 `relative_fail` 유지.
- `test_primary_family_positive_and_regression_controls_are_statistical_only`: 중립 pass, −0.10 회귀 fail 유지. delta를 `±0.01`, `−0.09/−0.11` 교대값으로 변경.
- `test_r2_known_good_adequate_replicates_...`: 변동 입력에서 pass, replicates=1에서 unverified.

## 보존 확인한 독립 통과 범위

- 수기 oracle Recall 1.0, nDCG 0.5861350236074894, MRR 0.5, false strong 1/2, false abstain 1/2: `test_independent_hand_oracle_is_preserved` 통과.
- 전체: `tests-final.txt` → `136 passed, 4887 subtests passed` (E0 112 + 관련 24). 이전 78(E0 54 + 관련 24) 대비 증가분은 이번 반례·경계 테스트다.
- lint/format: `lint-final.txt` → `All checks passed!`, `22 files already formatted`. test smell 0건.

## CLI 원시 출력 (`cli/`, `run_cli_sequence.py`)

```text
interrupt 3 interrupted              (completed=1)
resume 0 complete_fixture            exact_match
exact_replay 0 complete_fixture      exact_match/exact_match
stale_manifest 2 integrity           resume_manifest_mismatch
partial_cache 2 input                JSONDecodeError
rights_denied 2 rights               rights_unverified:qrels
fake_public_rights 2 rights          rights_review_authentication_required
split_leak 2 input                   gold_document_mismatch:q1:d3
```

`split-leak-input.json`은 현재 권리 형식으로 다시 만들었다. 이전 형식 원본은 `split-leak-input.pre-rights-format.json`에 보존했다. 이전 형식은 권리 검사에서 먼저 막혀 누수 검사에 도달하지 않았다.

## 실행 engine/model

- routing receipt: Ttakkari 계정 인계. codex(iuholic83) 세션이 usage limit으로 중단됐고 같은 Run에서 다른 엔진이 이어받았다.
- runtime 직접 관측: codex 구간은 session metadata `gpt-6.1-sol`, CLI 0.162.1. 이어받은 구간은 `claude --version` → `2.1.285 (Claude Code)`. 모델 ID `claude-opus-5-5`는 system prompt 자기 선언이며 provider endpoint 증명이 아니다. `runtime-direct-observation.json`.
- 핵심 계산은 Python/NumPy이며 경량 모델 위임은 없다.

## 잔여 E0 차단 (미실행)

- 실제 DB manifest/관측 fixture와 정상 main 계약 검증. 알려진 Codex initdb/shmget 거부는 재시도하지 않았다.
- 브라우저/모바일 UI(UI 없음, review_api unmounted)와 인증된 reviewer/권리 검토 flow. 현재는 fail-closed 거부만 있다.
- 실추론, 실서버 queue/service/stage 계측, GPU/host 자원 측정.
- anchor가 positive passage가 없는 선언 문서를 가리키는 경우는 누수가 아니므로(선언 문서는 graph에 포함) 이번에 거부 규칙을 추가하지 않았다.
