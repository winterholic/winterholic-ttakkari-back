# 채택 Run 보고서 연결 수정 기록 (2026-11-07)

## 기준 및 변경

- 시작 HEAD: `e2fa45dbc2051c5300f6cd8c5ab8650b1ca83c34`.
- 시작 시 tracked 변경은 없었다. `docs/research-lab/evidence/independent-d4f069e8-20261107/`는 untracked 증거이며 읽기만 했다. 별도 작업자의 파일은 stage하지 않았다.
- 기능·테스트 커밋: `a9e2e3b8dce6003eb0d0cdbf8d5c3a98447f2470` (`fix(research): link adoption report to selected run`). 기능 수정과 회귀 테스트만 이 한 커밋에 있다.
- 변경 파일: `src/ttakkari/research/service.py`, `tests/test_research_report_selected_run.py`.
- 수정: 채택 섹션에 선택 Experiment/Configuration/Run ID와 해당 Run의 주요 측정값·단위를 출력한다. 참조 소속관계와 성공 상태, 측정값의 Run/Experiment/Configuration/metric 키를 모두 확인하며, 유효한 일치 측정이 없으면 `미확인`으로 남긴다. 반복 실험 표에는 Run ID를 추가하고, Run 소속관계가 맞지 않거나 유한하지 않은 측정값은 출력하지 않는다.
- 원자료 및 artifact 등록·연결, 기존 report 상태/지표, 실행 안전 중단·요청 키·채택 gate·pagination 코드는 수정하지 않았다.

## 재현 및 검증

- 테스트 우선 RED:

```text
$ UV_CACHE_DIR=/private/tmp/research-report-linkage-uv-cache uv run pytest -q tests/test_research_report_selected_run.py
2 failed in 0.65s
2 failed in 0.65s
```

- RED 이후 선택 Run 0.9와 같은 Configuration의 반복 Run 0.3을 두고, 선택 Run의 실제 측정 연결·반복 Run ID를 정답 assertion으로 검증했다. 누락·불일치 측정은 다른 Run 값으로 대체하지 않는 반례도 포함했다.
- 좁은 회귀:

```text
$ UV_CACHE_DIR=/private/tmp/research-report-linkage-uv-cache uv run pytest -q tests/test_research_report_selected_run.py tests/test_research_v2_api_contract.py::test_export_report_returns_artifact_metadata
....                                                                     [100%]
db reset: 4 selective (avg 6.0/132 tables), 0 forced full, 0 fell back to full
4 passed in 1.28s
```

- Ruff:

```text
$ UV_CACHE_DIR=/private/tmp/research-report-linkage-uv-cache uv run ruff check src/ttakkari/research/service.py tests/test_research_report_selected_run.py
All checks passed!
```

## 기존 독립 증거 적용 범위

- [독립 검증 보고서](evidence/independent-d4f069e8-20261107/REPORT.md)의 85개 회귀와 9개 경계 회귀는 해당 고정 SHA에서 직접 통과한 기존 증거로 보존한다. 이번 변경은 보고서 출력과 새 보고서 테스트에 국한되므로 runner의 안전 중단·retry·요청 키·pagination 근거는 이번 수정으로 바뀌지 않았다. 전체 85개 및 9개는 반복 실행하지 않았다.
- 9개 반례 중 기존 보고서 선택 Run 연결 실패는 결함 관측을 확인하는 반례였으며 안전 수용 통과로 해석하지 않았다. 새 assertion과 artifact 회귀는 위 좁은 명령에서 별도로 통과했다.
- 실제 GPU/REGNA 실행, 운영 DB/API 쓰기, 브라우저 UI 및 MCP transport는 실행하지 않았다. push와 배포도 하지 않았다.
