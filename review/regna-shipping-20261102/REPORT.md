# Regna N3 + N4 fixed CPU connector shipping review

**Status: source integration candidate complete; normal push and deployment blocked at the existing gate boundary.** This is an integration candidate for backend source. It does not complete the Regna model research, N1/N3/N4/N5/N6 research stages, or any live model operation.

## Canonical base and candidate

- Selected base: `83302612c8edaaa40844a423e7223d2ee346e382`. A read-only `git fetch origin main` returned this revision; `origin/main` and the active API runtime release reported by `runtime_report` also identify this SHA. The newest deploy record for it is `draining` (reservation `3eada617-5ac1-45b1-a9ac-1e0ddaf72e31`).
- The existing local `main` worktree is at `4ea068afc0ce85ff1e431e0e1315e26ae9dc75f3`, has extensive unrelated dirty files, and is not the current deployment base. It was not modified. The clean shipping worktree was created at `/Users/winterholic/development/projects/winterholic-ttakkari/back-regna-shipping-20261102` from source document HEAD `27d706d962ac41d7fdf7592296f93c2449b0a02b`.
- The source candidate is 18 commits ahead of the selected base, with the base an ancestor. Those commits are the N3 Qwen2.5 CPU connector and focused follow-ups, then the N4 connector and correction follow-ups; no whole divergent branch merge was needed. The candidate preserves that exact ordered history.
- Source implementation target `68283b6597a5af180bfcd2616fcd554f144cd21d` and document HEAD `27d706d962ac41d7fdf7592296f93c2449b0a02b` have identical tracked `src/` and `tests/` trees. N4 connector file SHA-256 is `a67579049167432a21df72be26b815f33f1add77c89339abf8aad3b637b1b4bf`. N3 historical integration candidate `748c35c666d3bf1b221d15fa193ccbe44627e7b4` is in the ancestry. No integration conflict or implementation delta was found against the current canonical base.
- Pre-report integration candidate SHA: `27d706d962ac41d7fdf7592296f93c2449b0a02b`. The only commit added in this shipping worktree is this review/evidence record; the final full SHA is reported after commit. It is not published remotely.

## Reused source acceptance evidence

No source/test files changed while moving to the shipping worktree, and no inputs, frozen assets, pins, or runtime environment were changed. The independent N4 correction acceptance is therefore reusable without repeating tests:

- `tests/test_regna_n4_corrections.py` + `tests/test_regna_n4_execution.py`: **19 passed**, exit 0.
- Independent actual-receipt and symlink probes: **10 passed**, exit 0.
- Earlier N3 focused regression: **37 passed**, exit 0. Existing owner/API/MCP tests (9), fixed MCP allowlist (1), N3 pins (31), and isolated N4 pins (15) remain unchanged and were accepted in the source evidence.
- N4 producer summary records N3 manifest SHA-256 `370ab02657803d134e2ca147f6ce78404c3c70d52b636ec4baaf8bed04cffe0c`, N3 31/31 and N4 15/15 pins unchanged. Frozen Regna raw data, oracle/scorer, shared N3 pins, and final-isolated N4 pins were not edited.

The full historical test-output receipts were copied unchanged under `evidence/`. A range check across all 18 historical commits reports trailing whitespace inside captured pytest output files; these are verbatim logs, not source or test edits. The focused source/document diff check and this report's working-tree check are recorded separately; the raw evidence was preserved byte-for-byte.

## Deployment support-path review

- Current MCP catalog exposes `request_backend_deploy(revision)` as the normal reservation tool. It does not expose an arbitrary shell runner or exact pre-push gate executor. The `host_operation` catalog is limited to named runtime, backup, restore, snapshot, and collection operations; it cannot run the Git pre-push script.
- The controller source in the active `83302612…` release accepts a published ancestor of `HEAD` or `origin/main`, then runs its security gate and `uv run pytest -q -x` before preparing a generation. The API request records a reservation; it is not a separate pre-push gate and requires the requested revision to be available in the source history.
- The prior normal N3 push pre-push attempt is already reported as **10 failed / 104 passed** in the supplied task context: five pre-exec failures, one `sysctl` permission failure, and four unresolved failures. The independent diagnosis found the existing runner/tests identical to the base and did not establish a code defect. The same hook/gate was not rerun, no engine change or skip was used, and no push was attempted in this Run.
- `request_backend_deploy` was not called: the required pushed revision is absent, and this Run retains `pool=work`. No session-scope conversion or alternate-session request was attempted. Reservation ID/state: none. Deployment outcome: not executed.
- Therefore this candidate is not presented as pushed or deployed. The next supported action is to resolve the already observed pre-push environment failures in an authorized normal gate environment, then perform one normal push, read back the remote full SHA, and request deployment for that exact SHA from an authorized personal deployment session.

## Scope and routing

- Requested assignment: `engine=auto`, `task_type=general`, quota scope `work`. The current Run does not expose selected actual engine/model or effort in a routing receipt; these fields are unverified and are not inferred from the Codex interface.
- No live N3/N4 operation, model/asset load, lease acquisition, host GPU operation, personal host action, service restart, force push, hook/test weakening, or frozen research data mutation occurred.
- Source and tests are unchanged from the independently accepted source candidate. The only new commit in this worktree contains this shipping report and copied, unchanged acceptance evidence.

## Evidence index

- `evidence/01-base-and-worktree.*` — selected base, ancestry, commit count, and clean shipping worktree.
- `evidence/02-source-identity.*` — source/document HEAD and tracked `src/` + `tests/` identity.
- `evidence/03-focused-diff-check.*` — base-to-candidate source/test/contract-document whitespace check.
- `evidence/04-controller-review.*` — controller source inspection from the current release revision.
- `evidence/05-deployment-tool-state.txt` — runtime, deploy reservation, and MCP tool catalog observations.
- `evidence/06-existing-prepush-block.txt` — inherited gate outcome and explicit no-retry decision.
- `evidence/regna-n4-correction-independent-20261102/`, `evidence/regna-n4-independent-20261102/`, `evidence/regna-n4-connector-20261102/`, and `evidence/regna-n4-correction-20261102/` — copied source-side reports and command receipts.
- `evidence/07-source-candidate.*` — source candidate SHA, branch status, and source/review commit split before the shipping report commit.
