# ArtCraft inventory gate classification correction

Date: 2026-11-07 (Asia/Seoul)

## Identity, roots, and DB proof

- Run API: `2f83bfca-c91a-4468-b9c6-5b52240de955`, `running`, engine `codex`, model `gpt-6-luna`, effort `high`, profile `회사 코덱스 ai2 (ai2@hyperez.io)`; the required API+jq command exited 0.
- Editing worktree: `/private/tmp/platform-v2-inventory-gate-20261107`, created clean at base `a4387faf0b0110ae56364585418dd48a6a0569fe` (fetched `origin/main`; `git ls-remote` matched). Reproduction worktree: `/private/tmp/platform-v2-inventory-repro-df8-20261107`, detached at immutable `df8ec39cdc70387708a10dbb1688b31cd4796bba`. The dirty `/development/projects/winterholic-ttakkari/back` main checkout was not edited.
- Before pytest, an async SQLAlchemy connection executed `SET TRANSACTION READ ONLY` and read SQL identity. URL value was not printed. `DB_OWNER_PROOF=PASS`: `postgresql+asyncpg`, loopback `127.0.0.1`, database `ttakkari_test`, user `ttk_test`, port `56021`; SQL transaction was read-only. The exact parent marker was `/tmp/ttakkari-run-test-99596-d40a0fb6/run-test-cluster.marker`; parsed `run_id` matched this Run, marker folder and port matched SQL data directory/port, and marker/folder UID `501` matched current UID `501`.

## Diagnosis

- On immutable df8, `run_audit()` returned `all_collected_classified=False`, with exactly `source_files: ["src/ttakkari/subscription_contract.py"]` unmatched. The requested pytest reproduced the same assertion at line 687: `1 failed in 5.91s`, exit 1.
- Latest fetched `origin/main` (`a4387faf0b0110ae56364585418dd48a6a0569fe`) had no effective correction: direct `run_audit()` also returned `False` and the same one unmatched file.
- The assertion calls `run_audit()` without an `AuditContext`, so it scans the real source root derived from the checked-out scanner. That makes the test source-root-dependent, but this is a real file in both df8 and latest main, not a synthetic fixture artifact. The cause is the missing source classification rule.
- `subscription_contract.py` holds the neutral bounded subscription diagnosis contract. KB subscription transport imports and re-exports it; collector precise evaluation also imports it directly. The inventory assigns this shared contract to `domain.knowledge_rag`, the existing owner of the subscription transport surface.

## Change and TDD evidence

- Added one exact `classify_source_file()` mapping for `/subscription_contract.py` to `domain.knowledge_rag`.
- Extended the existing classifier negative-control test to require that mapping while retaining the unknown `src/ttakkari/alien/teleport.py -> unmatched` assertion. No unmatched handling, extraction, coverage, or classification-count assertion was weakened.
- RED: after adding the test assertion first, `test_unknown_inputs_negative_controls` failed as expected (`unmatched` vs `domain.knowledge_rag`), exit 1.
- GREEN: `test_unknown_inputs_negative_controls` plus the exact report-schema failure test: `2 passed in 5.36s`, exit 0.
- Related file: `PYTHONPATH=. /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest unit_tests/test_platform_v2_inventory.py -q -x --timeout=120 -p no:cacheprovider` -> `57 passed in 84.24s`, exit 0.
- Direct post-fix `run_audit()` -> `all_collected_classified=True`, `unmatched={}`, while `coverage_all_reconciled=False`; coverage remains independently unproven.
- Import/root restoration check after commit: a fresh Python process confirmed `platform_v2_inventory.py` resolves inside `/private/tmp/platform-v2-inventory-gate-20261107`; snapshots around `run_audit()` confirmed `sys.path`, `TTAKKARI_NO_ENV_FILE`, and cwd were unchanged after the call. Output: `sys_path_restored=True env_restored=True cwd_restored=True`, exit 0. `git status --porcelain` was empty.
- Ruff: `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check scripts/platform_v2_inventory.py unit_tests/test_platform_v2_inventory.py` -> `All checks passed!`, exit 0.
- `git diff --check` -> no output, exit 0. Tests and direct audit ran in separate child processes with `PYTHONPATH=.` rooted at the candidate worktree. Final worktree status was clean apart from the intended branch-ahead commit; no generated files remained.
- An initial attempt to use `.venv/bin/python` inside the new worktree failed because it has no local venv (zsh exit 127). Retried with the existing shared `/development/projects/winterholic-ttakkari/back/.venv` interpreter. One pytest invocation used `PYTHONPATH=src` and failed collection because `scripts` was not importable (exit 4); rerunning with `PYTHONPATH=.` collected and produced the intended RED.

## Commit

- Source/test commit: `70907e695a7312a862358e764b65e515bcdc6b53`
- Base: `a4387faf0b0110ae56364585418dd48a6a0569fe`
- Diff: `scripts/platform_v2_inventory.py` (+3), `unit_tests/test_platform_v2_inventory.py` (+4); 7 insertions, 0 deletions. This work-history file is committed separately to include the full source commit SHA.
- No push, deployment request, controller gate, or operational test was performed; those remain the Lead's next step.
