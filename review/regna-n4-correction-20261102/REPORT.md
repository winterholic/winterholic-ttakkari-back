# Regna N4 connector F1/F2/F3 source correction

## Result and scope

**상태: 완료 — 요청된 connector source correction 단위.** Base was `a07ca4cff1136eb5825e740c1576e52543addb1c`. This is a source/test correction, not completion of the Regna research. N1/N3/N4/N5/N6 remain unfinished as recorded in the supplied continuation contract.

No API/MCP boundary, `host_gpu` personal-only policy, N3 implementation/31 pins, final-isolated N4/15 pins, frozen raw/oracle/scorer, or prior independent evidence was edited. The untracked `review/regna-n4-independent-20261102/` was left untouched. No host operation, model or asset load, real lease, push, deploy, forbidden host probe, or full host suite ran.

## Routing receipt

The correction was requested with `engine=auto`, `task_type=general`, `strict_engine=false`, in `pool=work`. The accessible session record identifies the actual run as `codex / gpt-6-luna` (run `89e6853f-1a5b-41b0-a688-cd1c260a5f46`). The record does not expose selected effort, so effort remains unverified. The routing record and its source are in `evidence/routing-receipt.json`.

## F1 — reuse only a valid identical baseline

The production receipt at the path named by `review/regna-n4-independent-20261102/evidence/historical-ledger-schema.json` was read. Its request body is nested under `cells[].generations[].request_receipts[]`; cell identity, generation mode, the runtime/model/template asset hashes, successful cell outcome, raw integrity and usage are stored alongside that request. The tokenizer evidence states that tokenizer identity is bound by the model artifact SHA. The receipt has no standalone input-identity hash field.

Reuse eligibility now requires the exact expected serialized request SHA for the expected cell, that same recorded `cell_id`, the supported historical receipt/generation context, matching runtime/model/template SHA values and generation mode, a completed operation, a successful captured cell response, durable verified raw output and exact integer usage. The historical schema binds input identity through the request-body SHA already calculated for each fixed cell; it does not require a newly invented `input_identity_sha256` field. Tokenizer identity is checked through the model artifact SHA as the historical evidence specifies.

Failed transport/runtime attempts, foreign assets, absent identity context and incomplete output/raw/usage remain in `prior_attempts` with an eligibility and reason. They do not enter `exact_baseline_matches` or block a grant. A positive historical receipt fixture with matching identity and complete usable output still yields `BaselineReuseError`, so duplicate valid baseline reuse remains refused. The old minimum-record positive fixture in `tests/test_regna_n4_execution.py` was replaced with the actual historical receipt shape and its valid completion evidence.

## F2 — bind review to a stable ledger inventory

Production discovery now records the supported ledger inventory before scanning and re-discovers it after scanning. It compares normalized path, device/inode, byte size and timestamps, then retains the existing content digest checks. Added/deleted/replaced inventory entries and per-file mutation fail closed. Explicit `ledger_files` test paths do not trigger production discovery, and their in-scan byte mutation remains rejected.

The new regression injects added, removed and replaced production receipts. The separate explicit-path regression mutates the original file during review and expects refusal. This establishes filesystem mutation handling; there was no observation of an external concurrent writer, so this report does not claim an operational duplicate run.

## F3 — contain runs, claims and evidence paths

Before `mkdir` or `chmod`, `run_operation()` checks the `runs` path and each relevant ancestor, the job evidence root, and the claim boundary for symlinks and resolved containment under the configured data directory. It repeats the check after directory creation. `claim_grant()` uses the same guarded directory preparation when called by the operation.

The tmp-only `run_operation()` regression covers symlinks at both `runs` and `claims`. It checks rejection, unchanged outside directory mode and existing content, and absence of a job evidence directory outside `data_dir`. This is a filesystem-precondition test; it does not assert that a remote caller can create a symlink.

## RED/GREEN and verification

The new narrow regressions were first run against the base source. The observed run exited 1: the positive F1 test saw four body-only matches instead of one; failed/foreign attempts caused grant refusal; production inventory addition/removal did not raise; and the initial F3 test setup reached an invalid job-id seam before its intended assertion. The test setup was corrected to use a UUID and fake job update before GREEN. For replacement, the base implementation already caught a replacement during an active file scan through its inode check; the added inventory-level check also covers the discovery boundary. The prior immutable reviewer counterexample report/test remains untouched and records the original F3 behavior.

Final captured commands (all stdout/stderr/exit files are in `evidence/`):

| Command | Result |
|---|---|
| `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_n4_corrections.py` | 8 passed; exit 0 |
| `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_n4_execution.py` | 11 passed; exit 0 |
| `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/research/regna_n4_combo_first6.py tests/test_regna_n4_execution.py tests/test_regna_n4_corrections.py` | All checks passed; exit 0 |
| `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff format --check src/ttakkari/research/regna_n4_combo_first6.py tests/test_regna_n4_execution.py tests/test_regna_n4_corrections.py` | 3 files already formatted; exit 0 |
| `git diff --check` | exit 0 |

The existing connector evidence for the N3 focused 37 tests, host/API owner 9 tests, MCP allowlist 1 test, N3 31 pins and N4 15 pins was reused because those files and identities were not invalidated. The source/pin identity receipt confirms the prior N3 and N4 before/after pin maps match; the N4 plan and source-pin bytes remain `4b088845…6a19` and `765c36b5…3c82d` respectively.

## Exact changes

- `src/ttakkari/research/regna_n4_combo_first6.py` — F1 historical eligibility and preserved attempt reasons; F2 stable discovery inventory; F3 symlink/containment checks.
- `tests/test_regna_n4_execution.py` — upgrade the positive identical-baseline fixture to a supported complete historical receipt.
- `tests/test_regna_n4_corrections.py` — focused F1/F2/F3 regressions, including positive reuse and external-path immutability.
- `review/regna-n4-correction-20261102/REPORT.md` and `evidence/*` — correction report and command/source/routing receipts.

## Evidence references

- Actual historical receipt schema: `review/regna-n4-independent-20261102/evidence/historical-ledger-schema.json` (read only).
- Independent finding and original counterexample evidence: `review/regna-n4-independent-20261102/` (read only).
- Producer test/pin command evidence: `review/regna-n4-connector-20261102/evidence/` (read only).
- Before/after source and frozen pin identity: `evidence/source-pin-identity.json`.
- Actual run route receipt: `evidence/routing-receipt.json`.
