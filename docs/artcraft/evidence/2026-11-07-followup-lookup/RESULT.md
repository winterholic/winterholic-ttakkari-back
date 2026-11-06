# ArtCraft follow-up lookup regression

## Scope and starting point

- Worktree: `back-artcraft-followup-lookup-20261107`
- Clean base commit before edits: `27507965fec7b30d06fd0149ac1af171363df607`
- Profile for this Run, checked through `GET /api/runs/e2565714-2109-489c-80ab-3b62d8e34058`: `회사 코덱스 ai2 (ai2@hyperez.io)`.
- Changed only `_expects_output_file`, its tests, and this evidence record.

## Reproduction and fix

The exact approved follow-up prompt was tested with both `template_id=None` and `template_id="write-report"`:

> Without editing or creating a file, summarize the revised report's verification limits and next steps in English. Use only the latest saved report and explicitly say when a value is unmeasured.

Before the implementation change, the regression run showed `7 failed, 11 passed, 31 deselected`; both template cases returned `True`. Negative English lookup prompts also failed because negated create verbs counted as creation and `saved` matched the substring `save`. Korean report lookup was incorrectly treated as file-required when a non-info template was selected.

The prompt check now runs for all non-info templates. It removes supported English and Korean negated file actions before checking positive create/edit/export/save actions, and uses whole-word English matching so `saved` is not the `save` action. Positive mixed create-and-lookup, ordinary create/edit, and explicit export/save-as actions continue to require output.

## Follow-up root-cause trace (7a118088)

For `수정본의 검증 한계를 알려 주세요.` the value path is:

| Version | Lookup token | Create token | Final (`template_id=None`) |
|---|---|---|---|
| Parent of `7a118088` | `알려 주세요` → match | none (`수정` was not in the old create list) | `False` |
| `7a118088` | `알려 주세요` → match | `수정` → substring match inside noun `수정본의` | `True` |

Root cause: the Korean create alternatives `편집` and `수정` were unbounded substrings. The same false create token was visible in `편집된 보고서의 요약을 알려 주세요.`. In an actual edit request such as `보고서를 수정하고 변경 사항을 알려 주세요.`, `수정` is a verb and must remain a create token; the correction therefore needs a Korean action-ending boundary, not removal of edit intent.

The follow-up narrows only these edit tokens to action endings. The post-fix trace is:

| Prompt | Lookup token | Create token | Final (`template_id=None`) |
|---|---|---|---|
| `수정본의 검증 한계를 알려 주세요.` | `알려 주세요` | none | `False` |
| `편집된 보고서의 요약을 알려 주세요.` | `알려 주세요` | none | `False` |
| `보고서를 수정하고 변경 사항을 알려 주세요.` | `알려 주세요` | `수정하고` | `True` |
| `보고서를 편집하고 변경 사항을 알려 주세요.` | `알려 주세요` | `편집하고` | `True` |

## Follow-up verification

- Red: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'output_requirement_distinguishes'` → `8 failed, 20 passed, 35 deselected`; the four past-result/nominal prompts failed for both template IDs, while create/edit prompts passed.
- Green scoped regressions: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'output_requirement_distinguishes or follow_up_report_lookup or korean_negative_file_request'` → `31 passed, 32 deselected`.
- Summary/detail scope: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k report_lookup_without_output` → `1 passed, 62 deselected`.
- Direct trace for the exact preflight English prompt: `PYTHONPATH=src ../back/.venv/bin/python ...` → `template_id=None: False`; `template_id='write-report': False`.
- Lint and whitespace: `../back/.venv/bin/ruff check src/ttakkari/artcraft_tasks.py tests/test_artcraft_tasks.py` → `All checks passed!`; `git diff --check` → clean.

## Independent counterexamples and correction

The verifier checked immutable `7a118088` and supplied two additional failures. The observed token path was different in each case:

- `Do not create or save a file; summarize the report's verification limits.`: lookup `summarize` matched; the old negative pattern removed `Do not create` only, leaving `or save` as a positive create token; final result was `True`.
- `파일을 만들지 않고 보고서의 검증 한계를 요약해 주세요.`: lookup token did not match; no positive create token remained; the default final result was `True`. The missing Korean summary lookup is the cause, not a leftover `만들` create token.

The fix removes coordinated negative English file actions as one phrase, then still detects any later positive action. It adds common `요약해...` lookup forms. The earlier edit-noun correction remains in place.

## Independent-counterexample verification

- Red: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'output_requirement_distinguishes'` → `4 failed, 30 passed, 35 deselected`; both new lookup cases failed for both templates. The later positive `save` mixed request passed.
- Green combined regression: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'output_requirement_distinguishes or follow_up_report_lookup or korean_negative_file_request'` → `37 passed, 32 deselected`.
- Direct function trace: the two-template matrix returned `False` for both negative lookup examples; for `Do not create or save a file for this lookup; summarize the report's verification limits, but save the revised report as PDF.` it retained positive `save` and returned `True` for both templates.
- Summary/detail scope: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k report_lookup_without_output` → `1 passed, 68 deselected`.
- Lint and whitespace: `../back/.venv/bin/ruff check src/ttakkari/artcraft_tasks.py tests/test_artcraft_tasks.py` → `All checks passed!`; `git diff --check` → clean.

## Verification

- Initial runner setup: `../back/.venv/bin/pytest ...` could not collect because that venv's editable import path did not include this isolated worktree (`ModuleNotFoundError: ttakkari.runs.test_database`). Setting `PYTHONPATH=src` loaded this worktree and all subsequent commands completed.
- Red: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'follow_up_report_lookup or output_requirement_distinguishes'` → `7 failed, 11 passed, 31 deselected` (expected regression failures).
- Green focused: same command after the fix → `18 passed, 31 deselected`.
- Korean negative regression: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k korean_negative_file_request` → initially `1 failed`, then included in the passing full suite.
- Full requested unit file: `PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py` → `51 passed in 7.20s`.
- Route regression: included in the full suite; it removes only the simulated lookup Run's output rows from the isolated test DB, then confirms task summaries and task detail both leave `error` unset for a successful lookup with no output file.
- Lint: `../back/.venv/bin/ruff check src/ttakkari/artcraft_tasks.py tests/test_artcraft_tasks.py` → `All checks passed!`.
- Diff whitespace: `git diff --check` → expected clean.

## Limits preserved

No operational API mutation, provider call, shared DB inspection, app install change, push, or deployment was performed. API-path behavior was exercised only against the test suite's isolated database and fake agent. The prompt intent heuristic remains lexical: ambiguous phrasing outside the tested English/Korean action and negation forms is not semantically classified.
