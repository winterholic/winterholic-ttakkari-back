# KB-E0 보강분 독립 확인 4회차 (1e9bb1a)

**판정: passed (E0 로직 완료 승인). SHA `1e9bb1ad4ad5dcecefbb1e1b7555dcba4d688711`** (`origin/main`, `5b788a3` 이후 보강분이 rebase된 커밋).

작성자 보고 4건 중 3건은 재현됐고, 1건은 일부만 맞았다. 승인하지만 아래 정정은 E0 완료를 선언하기 전에 처리하기를 권한다.

- **일부만 맞은 보고:** mutation "동등 변이 9개"에서 3개(Q1, Q2, Q5)는 동등하지 않다. 판정 결과가 실제로 바뀌는 반례를 만들었다. 나머지 6개 중 5개는 타당하고 1개(Q8)는 판단하지 못했다. 코드는 맞게 동작하고 단위 테스트가 그 조건을 보호하지 못하는 것이다. 생존 0은 `expect="kill"` 기준의 수치다.
- **새 결함 없음, 낮은 지적 1건:** 지연 ratio CI 폭 하한이 정밀도에 벌점을 준다(4-3).
- 미실행: UI와 브라우저, 실추론, 실서버 지연·자원 측정. E0 하네스에는 DB export adapter가 없다.

## 검증 환경

- clone: `/tmp/kb-e0-v4-verify`, 새로 clone해 SHA를 고정했다. 공유 작업 트리, `git worktree`, 공유 git 설정은 쓰지 않았다. 소스는 수정하지 않았고 commit, push도 하지 않았다. 변이 실험, 저장소 mutation 도구, DB 일부 실행은 모두 `/tmp`의 별도 복사본(`/tmp/kbv4-*`)에서 했다.
- 런타임: Python 3.13.15, 작성자 clone의 venv 인터프리터, `PYTHONPATH`는 이번 clone의 `src`. 스크립트가 `ttakkari.kb_eval.__file__`이 이번 clone 안인지 assert한다.
- 이번 SHA(`origin/main`)에는 E0와 무관한 변경(creative director, host resource, managed comfy 등)이 함께 들어 있다. `5b788a3` 대비 `src/ttakkari/kb_eval`의 변경은 `latency.py`, `schema.py`, `validation.py` 세 파일뿐이다. E0 범위만 확인했고 나머지 파일은 검증하지 않았다.
- 지난 검증의 outbox 스크립트는 접근할 수 없어 같은 내용으로 다시 작성했다(`kb-e0-v4-regression.py`, `kb-e0-v4-extra-probes.py`, `kb-e0-v4-mutation.py`).

## 1. 저장소 mutation 도구 직접 실행

`kb-eval/mutation/run_mutation.py --rev 1e9bb1a`를 clone의 작업 트리가 아닌 복사본에서 실행했다(도구가 `ROOT/.venv/bin/python`을 요구해서 복사본에 심볼릭 링크를 걸었다).

```text
baseline: 204 passed in 26.20s
{"total": 105, "not_applicable": 0, "killed": 96, "survived": 9, "survived_expected_kill": [], "survived_equivalent": ["L7","L15","L17","Q1","Q2","Q5","Q6","Q8","Q11"], ...}  exit=0
```

작성자 보고와 같다(105개 적용, 96개 잡힘, 동등 표시 9개, 기대 대비 생존 0). 이 도구가 `D4b`(동등 표시였던 `open_holdout` 사전 검사)도 killed로 처리했으니, 동등으로 적은 항목이 실제로는 잡히는 경우도 있다.

### 1-1. 동등 변이 9개 판정

| 변이 | 판정 | 근거 |
|---|---|---|
| L7 order 검사 | 동등(타당) | `LatencyBlock.order`가 `Literal['AB','BA']`라 스키마가 먼저 거부한다 |
| L15 control e2e 0 이하 | 동등(타당) | `e2e_ms`는 `gt=0` |
| L17 timer 유한/음수 검사 | 동등(타당) | 필드가 유한이고 `gt/ge 0`이며 `allow_inf_nan=False` |
| Q6 cluster 수 하한 20→1 | 동등(타당) | effective cluster 수 ≤ cluster 수라 effective ≥ 20이 이미 cluster ≥ 20을 포함 |
| Q11 `len(sizes) >= 2 and` 제거 | 동등(타당) | cluster 1개면 `insufficient_clusters` 이유가 항상 붙어 status가 `inconclusive`. `simulate_power` 쪽은 cluster ≥ 20이 별도로 필요 |
| Q8 power loss CI 상한 0.05→1 | 판단하지 못함 | 반례를 만들지 못했다. 중립 통과확률이 높은 pilot에서 -0.10 손실이 5% 넘게 통과하는 입력을 찾지 못함 |
| **Q5 power trials 하한 200→1** | **동등하지 않음** | 아래 |
| **Q1 pass 조건 `low > -margin`→`low > -1`** | **동등하지 않음** | 아래 |
| **Q2 pass 조건에서 Holm 제거** | **동등하지 않음** | 아래 |

**Q5 반례.** 작성자 근거는 "Wilson 상한 0/n ≤ 0.05는 n ≥ 약 72가 필요해서 더 적은 trial은 손실 한도를 못 맞춘다"이다. 그런데 변이는 하한을 1로 바꾸므로 n이 73~199인 구간이 남는다. 이 구간에서 Wilson 경계는 이미 만족하는데 등록된 최소 trial(200)만이 `insufficient`를 만든다.

```text
trials=73   status=insufficient loss_ci=[0, 0.050]
trials=100  status=insufficient loss_ci=[0, 0.037]
trials=199  status=insufficient loss_ci=[0, 0.019]
trials=200  status=established
```

변이를 실제로 적용하면 `simulate_power(pilot, trials=100)`이 `insufficient`에서 `established`로 바뀐다. 이 판정을 고정하는 테스트가 없다.

**Q1 반례.** 희소한 이상치 delta(40 cluster, 일부 ±0.3~0.5, 노이즈 0.002)에서 두 지표의 Holm 보정 p는 모두 0.05 이하(0.028, 0.043)지만 ndcg CI 하한이 -0.0585로 `-margin`(-0.05) 아래다. 원 코드는 `inconclusive`, 변이는 `pass`다(건강한 pilot에서 이유 목록은 둘 다 비어 있음). 즉 변이가 통과해서는 안 되는 입력을 `pass`로 만든다.

**Q2 반례.** 지난번에 근사 동등으로 적은 항목이다. 오른쪽으로 치우친 희소 양수 delta를 4,000개 탐색해 두 조건이 갈라지는 경우를 100건 찾았고 그중 92건이 Q2형이다(두 지표 CI 하한이 모두 `-margin` 위인데 Holm p > 0.05). 한 예에서 원 코드는 `inconclusive`(recall 하한 -0.0361, ndcg 하한 -0.0437, Holm 0.0776)이고 Holm을 뺀 변이는 `pass`다.

이 세 개는 모두 통계 gate의 `pass` 판정을 정하는 조건이다. 작성자의 mutation 문서에는 "동등"이라고 적혀 있어, 이 조건이 바뀌어도 아무 테스트도 실패하지 않는다는 사실이 가려져 있다. 코드는 맞게 동작한다.

## 2. stale pyc 설명 검증: 맞다

원인 설명이 맞다. 도구도 이를 실제로 막는다.

**설명 재현.** 작성자의 `stale_pyc_probe.py`는 mtime을 `os.utime`으로 강제로 맞춰 메커니즘만 보인다. 자연 발생 여부는 따로 확인했다. 지난번 하네스(`reason_mutation.py`, 변이를 소스에 직접 쓰고 pytest 후 복원)의 순서 그대로, 같은 크기의 두 변이를 연속으로 12번 돌렸다(`kb-e0-v4-stale-pyc-natural.py`, 바이트코드 활성).

```text
round 0: A killed wall=0.53s [1 failed, 15 passed in 0.35s] | B killed wall=0.33s [1 failed, 15 passed in 0.19s]
round 1: ...             | B killed wall=11.89s [1 failed, 88 passed in 11.75s]
...
B (insufficient_clusters) reported killed in 12/12 rounds
```

killed 실행 한 번의 벽시계 시간이 약 0.3초라서 연속한 두 변이 파일이 같은 초에 쓰일 확률이 높다. 12번 중 7번은 B가 앞 변이 A와 똑같은 `1 failed, 15 passed in 0.16s`를 냈다(앞 변이의 bytecode를 재사용). 나머지 5번은 정상적으로 새로 컴파일돼 11~12초 걸려 `88 passed` 뒤에서 실패했다. 이 SHA에는 `insufficient_clusters`를 고정하는 새 테스트가 있어서 B가 실제로 잡히는 경우이고, 3회차 SHA(`5b788a3`)에서는 B가 실제로 생존해야 하는데 같은 이유로 가짜 killed가 났을 것이다. 3회차 검증에서 내 재실행이 25/26을 얻은 것도 이 확률에 걸린 결과로 설명된다. 내가 앞서 "가설 미확인"이라 쓴 부분을 이번에 확인했다.

**도구가 막는지.** 새 도구는 변이마다 새 임시 복사본, 빈 `PYTHONPYCACHEPREFIX`, `PYTHONDONTWRITEBYTECODE=1`을 쓰고, pytest 플러그인이 import된 패키지가 변이 복사본 안인지 확인한다.

```text
$ kb-e0-v4-tool-check.py (같은 크기의 두 변이를 --workers 1로 연속 5회)
round 0..4: power_not_established=killed (1 failed, 15 passed in 0.43s) | insufficient_clusters=killed (1 failed, 88 passed in 11.5s; first=test_single_cluster_reports...)
isolation guard, matching root: exit=0 :: 1 passed
isolation guard, foreign root:  exit=3 :: INTERNALERROR> RuntimeError: mutant isolation broken: imported /private/tmp/kbv4-tool/src/...
```

5회 모두 `insufficient_clusters`가 앞 변이의 실패(`15 passed`)가 아니라 자기 테스트(`88 passed`)에서 실패한다. 격리 가드는 import 경로가 틀리면 즉시 중단한다.

## 3. 직전 변이 59개와 반례 18개 재실행

**내 변이 59개:** (지난번 N1 항목 3개를 새 코드에 맞게 5개로 바꿔 60개로 실행) `55 killed / 5 survived`. 생존 5개는 Q1, Q2, Q5, Q6, Q8로, 작성자의 동등 표시와 같은 집합이다. 지난번 17개 생존은 12개가 해소됐다(L1, L2, L12, L13, Q3, Q7, D1b, D1c, D1e, D3b, D3c, D3e). 바이트코드 재사용 문제는 없다(변이마다 새 복사본과 빈 pycache).

**반례 스크립트:** 지난 18개에 Q1과 Q5 반례 2개를 더해 20개를 만들었다. 실제 코드에서 `20/20 PASS`.

## 4. 새 지연 하한

### 4-1. 지난번 거짓 보류: 해소됨

같은 seed와 프로토콜로 만든 격자(평균 e2e 0.5~5000 ms × CV 0.5~10%, 40칸)가 이번에는 모두 `pass`다. 지난번에는 평균 20 ms 이하의 18칸이 `UNVER:arm_variance`였다.

### 4-2. 거짓 pass 시도: 막힘

| 입력 | 결과 |
|---|---|
| 후보 900 + `i*1e-9`, 대조군 1000 상수 | unverified (`degenerate_arm_variance`, `degenerate_ratio_ci`), 폭 8.9e-12 |
| 두 arm 모두 `i*1e-9` | unverified, 폭 8.9e-13 |
| 비율 1.18, 후보 `i*1e-6` | unverified, 폭 8.9e-9 |
| 후보 = 0.9 × 대조군(두 arm은 변하지만 비율이 상수), + 곱셈 잡음 1e-6, 동일 trace 재생 | unverified (`degenerate_ratio_ci`) |
| 평균 0.5 ms에 1e-9 ramp(절대 1 µs 하한 아래) | unverified |
| 평균 5 ms, 0.1 µs 간격 순열(CV 약 3e-4) | unverified, 폭 2.3e-4 |
| 평균 200 ms, 1 µs 간격 순열(CV 약 1.4e-4) | unverified, 폭 6.5e-5 |
| 변동 있는 양성 대조(900 대 1000), 음성 대조(650 대 500) | pass / fail 유지 |

하한을 조금 넘는 입력(순열 ramp, eps=0.1 ms, arm 표준편차 2.9 ms, CV 0.32%)은 지난번처럼 `relative_pass`다. 하한 기반 검사의 본질이라 결함이 아니라 보정 문제다.

### 4-3. 낮은 지적: 정밀할수록 `unverified`가 됨

ratio CI 폭 하한 5e-4는 평균과 무관한 값이라 지연 크기 문제는 사라졌지만, 폭은 대략 1/√n으로 줄어든다. CV가 매우 작은(안정적인) 시스템에서 block이 늘면 `pass`가 `unverified`로 뒤집힌다.

| CV | n=100 | n=1,000 | n=5,000 | n=20,000 |
|---|---|---|---|---|
| 0.05% | pass(폭 5.1e-4) | unverified | unverified | unverified |
| 0.1% | pass | unverified | unverified | unverified |
| 0.3% | pass | pass(7.0e-4) | unverified(4.4e-4) | unverified |

일반 서버의 CV는 보통 이보다 훨씬 크고(지난번 CV 5%는 n=1,000,000 안팎이 되어서야 문턱), 결과는 거짓 보류쪽이라 안전하다. 그래도 같은 trace에 데이터를 더 모았는데 판정이 나빠지는 성질은 정상적이지 않다. 퇴화 판정을 CI 폭이 아니라 arm 분산 같은 정보량 기준으로 바꾸는 쪽이 일관된다.

### 4-4. 1e-4 상대 하한이 margin 0.2 기준으로 타당한가

도출은 설명된다(0.2의 1/2000). 다만 두 가지를 확인했다.

- n ≥ 100의 지연 trace에서 arm 상대 하한은 한 arm이 상수이고 다른 arm만 변하는 경우를 빼면 binding하지 않는다. 실제 결정은 ratio CI 폭 하한(5e-4)이 한다(위 표에서 CV 3e-4~1e-3은 모두 ratio 규칙에 걸림). 그러므로 1e-4가 틀려도 영향이 작다.
- 그 값 자체는 사전등록 공학적 하한이며 실측 trace로 재검토한다는 단서가 정책 JSON과 README에 있다. 지금 값은 이 전제에서 타당하다.

정책 JSON과 코드 상수의 일치를 직접 확인했다(`policy json == BOOTSTRAP_POLICY: True`, version `kb-e0-statistics-v3`). 절대 하한 1 µs는 이 호스트 해상도(4.17e-8초)의 24배라는 설명이 맞고, 이전 이름과 달리 "해상도"라는 표현이 이제 정확하다.

## 5. 회귀: 이전과 같음

`kb-e0-v4-regression.py`: **122/122 PASS**. 항목은 지난 회차와 같다.
- 결함 1(split 누수), 결함 2(latency gate 원 재현 입력), 결함 3(권리), 결함 4(audit 위조): 모두 같은 사유로 막힌다.
- 수기 oracle: Recall 0.75, nDCG 0.381735339808, MRR 0.75, false strong/abstain 1/2, 답 없음 none recall 0/1, abstain F1 0, 전체 answerable Recall 구간 `[0.75, 0.75]`. 반복 quote와 손상한 quote는 `input_invalid`. Holm `(0.01, 0.04, 0.03)` → `(0.03, 0.06, 0.06)`.
- 캐시/위조: `resume_manifest_mismatch`, `cache_key_mismatch`, `replay_mismatch`, `unregistered_treatment`.
- CLI(실제 subprocess): `--limit 1` exit 3, 재개 exit 0, 정확 재생 byte 동일, 하드 킬(`os._exit(137)`) 뒤 재개 byte 동일, 동시 실행 `artifact_writer_active`, 누수·권리·public·holdout 입력 exit 2.

## 6. DB 테스트

이 엔진(Claude)에서 `initdb`가 성공했다. `tests/pg_sandbox.py`가 `/tmp/ttakkari-test-<pid>-*` 임시 클러스터를 띄우는 방식이고 공유 PostgreSQL에는 DB를 만들지 않는다. 실행 전부터 있던 다른 pid의 `/tmp/ttakkari-test-98690-*`는 건드리지 않았다.

```text
$ pytest tests/test_collector_{api,intake,jobs,migration}.py tests/test_context_library.py tests/test_kb_{defects_reproduce,library,migration}.py tests/test_knowledge{,_extraction,_graph_paths,_migration}.py tests/test_usage_collector.py
2 failed, 201 passed in 22.73s
```

실패 2건은 `tests/test_knowledge_migration.py`이고 원인은 `FileNotFoundError: .../kb-e0-v4-verify/.venv/bin/alembic`이다. 검증 clone에 `.venv`가 없어서다(코드 결함 아님). `.venv` 심볼릭 링크를 건 복사본에서 같은 파일을 다시 돌려 `2 passed`를 확인했다. 합계 203 passed. 작성자의 193은 파일 구성이 달라 직접 비교하지 못했다.

```text
$ pytest -m contract   (복사본)
63 passed, 3760 deselected in 10.34s
```

이 결과는 E0 통합이 DB 기반 제품 회귀를 깨지 않았다는 뜻일 뿐이다. E0 하네스 자체에는 DB export adapter가 없어서 DB manifest/관측 검증은 여전히 없다.

## 실행한 명령과 핵심 출력

```text
$ pytest unit_tests/kb_eval                                 -> 204 passed in 23.88s
$ pytest (관련 회귀 3개 파일)                                  -> 24 passed, 4887 subtests passed
  (E0 204 + 24 = 228, 작성자 보고와 일치)
$ ruff check src/ttakkari/kb_eval unit_tests/kb_eval kb-eval -> All checks passed!
$ kb-eval/mutation/run_mutation.py --rev 1e9bb1a             -> total 105, killed 96, survived 9 (모두 expect=equivalent), exit 0
$ kb-e0-v4-stale-pyc-natural.py (옛 하네스 순서 12회)          -> B reported killed in 12/12 (12회 중 7회는 앞 변이 결과를 재사용)
$ kb-e0-v4-tool-check.py                                    -> 5/5 insufficient_clusters killed by its own test; guard aborts on foreign root
$ kb-e0-v4-latency-floor.py                                 -> 격자 40칸 모두 pass, 거짓 pass 시도 9개 모두 unverified
$ kb-e0-v4-regression.py                                    -> 122/122 PASS
$ kb-e0-v4-extra-probes.py                                  -> 20/20 PASS
$ kb-e0-v4-mutation.py (내 변이)                             -> 55 killed / 5 survived (Q1, Q2, Q5, Q6, Q8)
$ kb-e0-v4-equiv-probes.py / q-mutant-demo.py / q2-demo.py   -> Q5: insufficient→established, Q1/Q2: inconclusive→pass (변이 적용 시)
$ pytest (DB 13개 파일)                                      -> 2 failed(.venv/alembic 없음), 201 passed; 복사본 재실행 2 passed
$ pytest -m contract                                         -> 63 passed, 3760 deselected
```

## 미실행

- UI와 브라우저(320/390px), 실추론, GPU, 실서버 queue/service/stage와 자원 측정: 이번 범위 밖이라 실행하지 않았다.
- 실제 DB manifest·관측 adapter: E0 하네스에 없다. 실제 공개 자료 권리 검토와 인증된 reviewer flow도 없다.
- Q8 반례 탐색은 성과가 없었고, 이 SHA의 E0 외 변경 파일(creative director 등)은 검증하지 않았다.

## 완료 선언 전 권고

1. `kb-eval/mutation/mutants.py`에서 Q1, Q2, Q5의 `expect="equivalent"`를 `kill`로 바꾸고, 위 반례를 단위 테스트로 추가한다(`kb-e0-v4-equiv-probes.py`, `kb-e0-v4-q-mutant-demo.py`, `kb-e0-v4-q2-demo.py`가 입력을 갖고 있다). Q8은 "미판정"으로 표기한다.
2. 지연 퇴화 판정에서 ratio CI 폭 하한을 arm 분산 기준으로 대체하거나, 정밀도에 벌점을 주지 않는다는 가정을 문서에 명시한다(낮은 지적).
3. 지난 회차의 권고 중 남은 것: 품질 pilot 하한 근처의 `power=established`는 실측 pilot 전에는 승인 근거로 쓰지 않는다(README에 이미 문구가 반영됨).

E0 로직 완료는 승인한다. E0.5 엔진 선택, KB3/제품 검색, 최종 품질 승인은 이 보고서의 범위가 아니며 승인하지 않는다.
