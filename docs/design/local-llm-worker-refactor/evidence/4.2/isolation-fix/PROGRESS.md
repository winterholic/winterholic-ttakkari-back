# 4.2 리뷰 필수 조건 이행 증거 (2026-10-23)

기준: origin/main a7f8930. 이행자: Claude Sonnet 5.5(엔진 sandbox 밖). 다른 공급자가 별도 검증 예정.

- `root-read-bisect.txt`: 루트 `/` data-read가 없으면 `/bin/echo`도 exit 134. 규칙별 이분 결과.
- `profile-example.sb`: 수정 후 profile(루트 data-read 한 줄 추가).
- `sandbox.json`, `sandbox-exit.txt`: `python3 -m local_llm_eval.sandbox` ok true, exit 0. 원본 checkout deny 대상은 실제 원본 pyproject.toml.
- `pytest-isolation-on.txt`: 격리 on 413 passed, skip 0. `pytest-isolation-off.txt`: 격리 off 412 passed, 1 skipped.
- `selfcheck.txt`, `selfcheck.json`: problems 0, 참조 18/18, 음성 366/366. flags에서 sandbox_unavailable 사라짐.
- `variants.py.txt`(실행한 스크립트 원문, ruff 대상 제외를 위해 확장자를 붙임), `variants-result.*`: 정상 변형 46종×8과제(격리 on). `variants-before-4.2-review-fix.json`은 수정 전(a7f8930, 격리 off) 결과.
- 새 공격 페이로드 없음. 거부 회귀는 이름 접근/존재하지 않는 fd 번호 호출만 사용.
