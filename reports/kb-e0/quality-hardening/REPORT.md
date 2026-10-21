# KB-E0 3회차 승인 후 품질 보강과 main 반영 보고서

- 3회차 판정: `5b788a3` passed. DB·UI·추론을 제외한 E0 로직이 승인 범위다.
- 이번 작업은 품질 보강과 main 반영이다. 보강분은 짧은 독립 확인 뒤 E0 완료로 기록할 예정이다. KB3 이후 단위는 시작하지 않았다.
- 원격 feature branch `feat/kb-e0-harness` `73e31c0520a04653e6c8e6e213afed6337c8fe76`은 rebase 전 기록이다.
- main 반영본은 `origin/main` `301831e` 위로 rebase했다. 충돌은 없었다. 범위 밖 파일 변경 0개, 커밋은 모두 이 작업의 것이며, E0 경로 내용은 `73e31c0`과 같다.
- `66c8402`는 이 clone에도 `origin/main`에도 없는 객체다. rebase로 끌려올 수 없다.

## 1. 테스트 보호 보강

### `insufficient_clusters` 판정이 다르게 나온 원인

stale bytecode 재사용이다. 직전 변이(`power_not_established`)와 이 변이(`insufficient_clusters`)는 이유 문자열이 똑같이 21자다. 그래서 변이 파일의 크기가 같고, 같은 초 안에 쓰이면 mtime도 같다. Python은 `.pyc` 유효성을 소스 크기와 초 단위 mtime으로만 판단하므로 앞 변이의 bytecode를 그대로 import했다.

- 결정적 재현: `stale_pyc_probe.py`, `stale-pyc-probe.txt`
  - 같은 크기·mtime으로 두 번째 소스를 쓰면 `power_not_established`가 반환된다.
  - 비어 있는 `PYTHONPYCACHEPREFIX`를 쓰면 `insufficient_clusters`가 반환된다.
- 제 이전 기록에서 245번 출력이 243번과 글자까지 같았던 이유로 본다. 이 부분은 추정이다.
- 결국 해당 이유는 실제로 테스트 공백이었다. 단일 cluster의 이유 목록을 정확히 단언하는 테스트를 추가했다.

### mutation 도구 (`kb-eval/mutation/`)

- 대상: 판정 이유 생성 코드 26개(자동 생성) + 조건·가드 변이 79개(`mutants.py`)
- 격리: 변이마다 `src`, `docs`, `unit_tests/kb_eval`, `kb-eval`, `pyproject.toml`을 임시 디렉터리에 복사한다(`--rev`면 `git archive`). 작업 트리는 바꾸지 않는다.
- 확인 장치
  - 변이마다 비어 있는 pycache prefix를 쓴다.
  - pytest plugin이 import된 패키지가 그 복사본인지 확인한다.
  - 대상 문자열이 정확히 한 번 나오지 않으면 `not_applicable`로 기록한다.
- `expect="equivalent"` 9개는 근거를 함께 적었다.
  - L7, L15, L17: schema가 먼저 거부한다.
  - Q1, Q2, Q8: 3회차에서도 반례를 만들지 못했다.
  - Q5, Q6: 다른 조건이 이미 포함한다.
  - Q11: 단일 cluster면 항상 `insufficient_clusters`가 붙는다.

| | 적용 | killed | survived (잡혀야 함) | survived (동등) |
|---|---|---|---|---|
| 수정 전 `5b788a3` (`mutation-before-5b788a3.txt`) | 102 (n/a 3) | 67 | **27** | 8 |
| 수정 후 (`mutation-after.txt`) | 105 | 96 | **0** | 9 |

- 수정 전에 살아남은 잡혀야 하는 변이 27개
  - 3회차가 반례로 지목한 12개: L1, L2, L12, L13, Q3, Q7, D1b, D1c, D1e, D3b, D3c, D3e
  - `insufficient_clusters`(이유와 조건 Q9)
  - 이번에 추가로 찾은 것: L10(SLO 값), L16(timer 단조성), L19(mode), Q4(p값 재중심), Q6b(유효 cluster), D1i(anchor 필수), D3f·D3h·D3i·D3j(권리 세부), R6(cache identity), X5(등급 범위)
- 수정 후에는 모두 잡힌다. 테스트는 `unit_tests/kb_eval/test_quality_hardening.py`에 있다.
- Q4 테스트는 정확한 이항 확률 P(k≥28 | Bin(40, 1/2))와 p값을 비교한다.

## 2. 지연 퇴화 하한

| 기준 | 값 | 근거 |
|---|---|---|
| `timer_resolution_ms` (절대) | 0.001 | 측정 해상도 하한이다. 기준 호스트 `perf_counter`는 `mach_absolute_time`, 해상도 4.17e-8초(`timer-resolution.txt`)이고, 1µs는 그보다 24배 거친 cross-host 하한이다. 변동 하한이 아니다. |
| `latency_min_cv` (상대) | 1e-4 | 0.2 상대 margin의 1/2000이다. arm 변동이 이보다 작으면 p95 ratio 판정을 움직일 수 없어 상수로 본다. |
| arm 규칙 | | e2e 표준편차 < `max(timer_resolution_ms, latency_min_cv × 평균 e2e)` 이면 `degenerate_arm_variance` |
| `latency_ratio_ci_min_width` | 5e-4 | 0.2 margin의 1/400이다. ratio 폭은 원래 스케일과 무관하므로 fast·full 모드에 똑같이 적용된다. |

- 정책 version은 v3이다. 품질 쪽 근거 문구의 "float 잡음"을 "사실상 상수"로 고쳤다. 하한 근처 pilot의 `power=established`를 실측 pilot 전 승인 근거로 쓰지 않는다는 점도 적었다.
- RED: `red-latency-scale.txt`, 18건. 3회차 표의 UNVER 칸과 같다.
- GREEN: 평균 0.5~5000 ms × CV 0.5~10%의 40칸이 모두 이유 `[]`, `relative_pass`다.
- 거짓 pass 대조
  - 1e-9 jitter는 네 스케일 모두 `unverified`다.
  - 평균 5000 ms, CV 5e-5는 상대 하한이 잡는다.
  - 평균 0.5 ms, CV 2e-4는 절대 하한이 잡는다.
  - 평균 2 ms에서 1.5배 회귀는 `relative_fail`이다.

## 3. 검증 결과

- **단위 테스트:** `tests-final.txt` → `228 passed, 4887 subtests passed` (E0 204 + 관련 24). 이전 154 대비 늘었다.
- **lint·format:** `All checks passed!`, `26 files already formatted`. test smell 0건.
- **CLI:** `cli-summary.txt`. interrupt 3, resume 0, exact_match. stale manifest, partial cache, 권리 거부, 가짜 public, 누수 입력은 모두 exit 2다.
- **실제 PostgreSQL:** rebase한 트리에서 `tests/pg_sandbox.py` 임시 클러스터(initdb, `/tmp/ttakkari-test-<pid>-*`)로 실행했다. 이 엔진(Claude)에서 initdb가 성공했다(`pg-sandbox-probe.txt`, 기동 0.6초, 정지 뒤 폴더 삭제 확인).
  - KB·knowledge·collector·context library DB 회귀 13개 파일: `db-kb-regression.txt` → `193 passed`
  - 실행 전부터 있던 다른 pid의 `/tmp/ttakkari-test-98690-*`는 건드리지 않았다.
- **main 계약 묶음:** `pytest -m contract`: `contract.txt` → `63 passed, 3760 deselected`
- **E0 하네스와 DB:** E0 하네스 자체에는 DB export adapter가 없다. 위 DB 결과는 E0 통합이 DB 기반 제품 회귀를 깨지 않았다는 뜻이다. 실제 DB manifest·관측 adapter 검증은 여전히 없다.

## 남은 것

- UI·브라우저, 실추론, 실서버·GPU 계측, 인증된 reviewer·권리 flow, 실제 DB export adapter
- 낮음 지적 2건(서명 없는 artifact, E1a 시작 조건)은 README의 남은 위험에 있다.

## 실행 엔진

- 엔진은 Claude Code CLI 2.1.285다.
- 모델 `claude-opus-5-5`는 system prompt 자기 선언일 뿐 provider 측 증명이 아니다.
- 계산은 Python/NumPy이며 경량 모델 위임은 없다.
