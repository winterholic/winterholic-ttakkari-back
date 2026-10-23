# local_llm_eval

로컬 LLM 작업자 코딩 벤치의 독립 평가기(WBS 4.2). 표준 라이브러리만 쓰고 모델·서버·네트워크·GPU를 건드리지 않는다.
명세: `docs/design/local-llm-worker-refactor/benchmark-spec-draft.md`(v1), 구현 기록: `docs/design/local-llm-worker-refactor/evaluator-implementation-4-2.md`.

## 실행

저장소 루트가 아니라 `bench/` 에서 실행한다(패키지 import 경로).

```sh
cd bench
python3 -m local_llm_eval.selfcheck                 # 개발 과제 9개: 분류·참조해·음성 대조 전부
python3 -m local_llm_eval.selfcheck B4-MUL-D        # 한 과제만
python3 -m local_llm_eval.selfcheck --json out.json # 전체 상세 보고
python3 -m pytest local_llm_eval/tests -q           # 단위·회귀 테스트
```

`ruff check`/`ruff format` 은 **저장소 루트에서** 실행한다(push 게이트가 루트 기준으로 import 순서를 본다).
push 게이트는 `bench/local_llm_eval/` 변경 시 평가기 테스트 전체를 별도로 실행한다.

## 구성

| 파일 | 역할 |
|---|---|
| `evaluator.py` | 시작 트리 직접 해시, 구조 게이트, delta/invariant 검사, 보고 대조, verdict |
| `hidden_runner.py`, `runner.py`, `judges.py` | probe 프로세스(후보 코드 실행, 원시 관측값만 반환, 비밀·기대값 없음)와 평가기 프로세스의 judge(기대값 비교) 분리, process group kill |
| `channel.py`, `sandbox.py` | 전용 framed pipe·bounded 수집, read-allowlist OS profile·실제 런타임, holdout 격리 필수 |
| `integrity.py` | 봉인 source loader, 후보 전용 import/builtins, runtime 속성 가드, 명시적 stdlib capability |
| `natives.py` | 공개 suite, frozen 파일, 후보가 쓴 테스트(mutant 사망) 검사 |
| `negatives.py`, `selfcheck.py`, `simulate.py` | 음성 대조 빌더, 자체 검증, 모델 없는 attempt 생성 |
| `ledger.py` | 추가 전용 attempt ledger, profile 별 요약 |
| `leakscan.py` | 정적 누출 스캔(어휘 수준) |
| `tasks/` | 개발 과제 9개(fixture, 참조해, 대안 참조해, hidden, 음성 대조) |
| `fixture_data/` | 2026-10-18 기존 fixture 를 한 번 추출한 JSON(출처 sha256 포함)과 DBG rev2 실패 로그 |
| `provenance/` | 추출·감사 대조 스크립트(저장소 밖 자료를 읽음) |

holdout 과제와 정답은 이 디렉터리에 없고 있어서도 안 된다(봉인 디렉터리, 명세 §4.2).

관측 신뢰 계약과 한계는 명세 §0.1.1, 구현 기록 §11을 따른다. judge 분리만으로 관측을 신뢰하지 않으며, Python 가드는 OS sandbox가 아니다. N16~N33은 gold 위에서 관측 경계 공격을 검증한다. 최신 CPU 실행 근거: `docs/design/local-llm-worker-refactor/evidence/4.2/channel-isolation/`.

격리 진단: `python3 -m local_llm_eval.sandbox`(exit 0 실측 통과, exit 2 환경이 sandbox 적용을 거부해 미검증, exit 1 profile 결함 또는 deny 불이행). 엔진 sandbox 밖의 이 호스트에서는 exit 0이며, push 게이트가 같은 진단을 돌려 exit 2는 사유만 기록하고 exit 1은 실패로 처리한다. 개발 모드는 격리 불가를 flag로 남기며 holdout 모드는 실행 전에 거부한다. 개발 정답은 공개되어 있으므로 PASS를 강한 무결성 인증으로 해석하지 않는다. 강한 보장은 봉인 holdout과 실제 검증된 OS 격리를 전제로 한다.
