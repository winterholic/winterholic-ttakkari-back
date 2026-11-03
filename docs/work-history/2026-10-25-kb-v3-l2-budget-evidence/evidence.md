# KB v3 L2 daily budget: RED/GREEN 원출력 증거 (2026-10-25)

이 문서는 이번 Run에서 새로 실행한 결과만 가리킨다. 이전 Run의 요약이나 PASS 기록은 재사용하지 않았다.

## 반례
daily budget=1, max_calls_per_doc=1, 실제 PostgreSQL(tests/conftest.py 임시 클러스터), 문서 2건을 서로 다른 ExtractingProcessor
호출로 동시에 실행한다. 두 호출이 `_l2_calls_today` 읽기 직후 asyncio.Barrier(2)에서 만나, 둘 다 사용량 0을 읽은 상태로 어댑터를
호출하게 만든다. 어댑터 종류는 legacy_cli, subscription_cli 두 파라미터로 같은 ExtractingProcessor 경로를 탄다.
테스트: tests/test_kb_v3_l2_budget_concurrency.py::test_two_extractors_share_one_daily_call_budget

## RED (고정 4ca1ed0d7f8471a3b73def4d9b8c5d7bd4d3cdec, 수정 없음)
- 실행 시각: red.started / red.finished (UTC), base SHA: red.base_sha
- 방법: `git worktree add --detach ../back-kb-v3-l2-red 4ca1ed0d...` 후 테스트 파일만 afcb5ab5에서 복사(추적되지 않는 파일 1개만 추가).
  import 경로는 PYTHONPATH로 해당 worktree의 src 를 가리켜 확인했다.
- 명령: `PYTHONPATH=<red-worktree>/src .venv/bin/python -m pytest -v -k two_extractors tests/test_kb_v3_l2_budget_concurrency.py -p no:cacheprovider`
- 결과: `2 failed, 7 deselected`, exit=1, 두 파라미터 모두 `assert 2 == 1` (어댑터 실호출 2회). 원출력: red.stdout.txt

## GREEN (구현 commit, SHA는 green.impl_sha)
- 명령: `.venv/bin/python -m pytest -v -k two_extractors tests/test_kb_v3_l2_budget_concurrency.py -p no:cacheprovider`
- 결과: `2 passed, 7 deselected`, exit=0. 원출력: green.stdout.txt
- 같은 파일 전체: `9 passed`, exit=0. 원출력: green.full_budget_file.stdout.txt
- 참고: green.impl_sha 는 증거 파일을 추가하기 직전 HEAD 이며, 구현 commit afcb5ab5 와 같다.
