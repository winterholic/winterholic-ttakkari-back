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
이 테스트는 push 게이트의 '바뀐 테스트 파일' 선택에 잡히지 않으므로 수동으로 실행해야 한다.

## 구성

| 파일 | 역할 |
|---|---|
| `evaluator.py` | 시작 hash 검증, 구조 게이트, delta/invariant 검사, 보고 대조, verdict |
| `hidden_runner.py`, `runner.py` | 검사 1개당 `python -I -S -B` 자식 프로세스, nonce 핸드셰이크, process group kill |
| `natives.py` | 공개 suite, frozen 파일, 후보가 쓴 테스트(mutant 사망) 검사 |
| `negatives.py`, `selfcheck.py`, `simulate.py` | 음성 대조 빌더, 자체 검증, 모델 없는 attempt 생성 |
| `ledger.py` | 추가 전용 attempt ledger, profile 별 요약 |
| `leakscan.py` | 정적 누출 스캔(어휘 수준) |
| `tasks/` | 개발 과제 9개(fixture, 참조해, 대안 참조해, hidden, 음성 대조) |
| `fixture_data/` | 2026-10-18 기존 fixture 를 한 번 추출한 JSON(출처 sha256 포함)과 DBG rev2 실패 로그 |
| `provenance/` | 추출·감사 대조 스크립트(저장소 밖 자료를 읽음) |

holdout 과제와 정답은 이 디렉터리에 없고 있어서도 안 된다(봉인 디렉터리, 명세 §4.2).
