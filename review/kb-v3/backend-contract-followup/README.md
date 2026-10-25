# Backend contract followup evidence

Base revision: `27ea2f44d24b84e0a5e57b05cacb626571376a76`.
Environment: Codex command sandbox, SQLite memory and DB I/O doubles only.
No PostgreSQL cluster, Docker, TCP service, or network server was started.

| Output | Command | Exit |
| --- | --- | --- |
| `red.log.gz` | `env -u TTAKKARI_TEST_DB .venv/bin/pytest -q unit_tests/test_kb_access_backfill_currency.py unit_tests/test_kb_v3_contract_followup.py` (before production change) | 1 |
| `green-new.log` | Same command after production change | 0 |
| `green.log` | Final six-file pytest command in `checks.json`, before two lint-only test edits | 0 |
| `ruff.log` | Five-file Ruff command in `checks.json`, before two lint-only test edits | 1 |
| `green-final.log` | Exact command in `checks.json`, after the lint edits | 0 |
| `ruff-final.log` | Exact command in `checks.json`, after the lint edits | 0 |
| `diff-check.log` | `git diff --check` | 0 |

The logs contain the original combined stdout/stderr. `red.log.gz` is lossless
gzip: pytest emitted trailing spaces, so the original bytes are compressed
without editing them. `gzip -cd red.log.gz` displays the output; its
decompressed SHA-256 is recorded in `red.log.sha256`. Empty `diff-check.log` is
the successful command's empty output, not a missing test result. Full changes
and scope limitations are recorded in
`docs/work-history/2026-10-26-kb-v3-backend-contract-followup.md`.

The tested code was the working tree based on the base revision, committed with
these records. Resolve the followup revision with the commit containing this
file; a commit cannot record its own SHA inside its own tree.
