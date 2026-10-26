# P0 배포: Research 재사용 마이그레이션 분류 누락

`02141811-90f9-4145-afc2-17e1ee98de01` / `12aaf56d30e04d0e6bfc52523963e05663759297`의
공식 `ops_deploy_log(summary)`에서 release pytest 실패를 확인했다.
`1 failed, 4971 passed, 13 skipped, 2 xfailed, 4961 subtests passed in 1233.82s`.
실패는 `test_report_schema_separates_extraction_classification_scope_coverage`의
`all_collected_classified is True` 단언이다. 운영 반영 전 gate 실패다.

Research lead의 DB identity 교정은 이 후보에 포함되어 있어 재사용했다.
해당 교정과 dispatch 교착 검사를 반복하지 않았다.

## 관측과 최소 교정

Codex의 `/private/tmp/tk-p0-snapshot-fix-20261025`에서 `PYTHONPATH=$PWD/src`로
현재 worktree를 import한 `run_audit()`의 unmatched 목록은
`migrations: [0073_research_reuse_source]` 하나뿐이고 다른 범주는 모두 빈 목록이었다.
`classify_migration()`의 Research 규칙은 `research_lab` 문자열만 인식했다.
0073은 실제 `research_experiments`에 source lineage 열과 FK를 추가하는 migration이다.

분류 규칙에 정확한 `0073_research_reuse_source`를 추가했다.
알 수 없는 migration의 unmatched 판정, coverage unknown 계약, 실제 migration 파일은 그대로 둔다.

## 재현과 검증

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q unit_tests/test_platform_v2_inventory.py::test_report_schema_separates_extraction_classification_scope_coverage --tb=short
```

교정 전 실제 gate와 동일하게 `1 failed in 3.95s`.
신규 0072/0073 분류 회귀 및 기존 unknown 반례는 교정 전 `1 failed, 2 passed in 0.06s`.
0073만 unmatched로 실패했다.

교정 후 `PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q unit_tests/test_platform_v2_inventory.py --tb=short`
→ `57 passed in 55.66s`. 기존 unknown negative controls와 분류/coverage 분리 검사도 포함한다.
두 변경 Python 파일 ruff → `All checks passed!`; `git diff --check` → exit0.

전체 release gate, 운영 revision/readiness·백업/격리복원/스냅샷,
remote·혼합세대/lossless/rollback·실모델/MCP/resume은 별도 미검증이다.
기존 setpriority EPERM·Mach IPC·독립 initdb 실패를 재시도하거나 다른 엔진으로 우회하지 않았다.
