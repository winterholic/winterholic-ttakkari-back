# Execution isolation integration — candidate, not operational approval

Worktree: `back-platform-v2-execution-all-20261025`.
Branch: `integration/platform-v2-execution-all-20261025`.

Preserved exact candidates by merge ancestry:
- FND/report integration `2a50c860ae94256d643a58d373d1291ecc7997ad`.
- Step2 safety `f462562de7b6e81175442f8dcfe34ab87428e566`.
- Step3 Qwen plus test correction `aa0455d9b59e3e6ba62c218eac47b7fcc7f85523`.
- Step4 TTS `5751037faceeadc1e284f5ab84671550b392c3ab`.
- Step5 Creative/game `604f4fcc8e34685324eb5e8ada3d0289b0c9bdb3`.
- Step6 background consumers `b94a5a043a932c8d2977d34dff95330e02541683`.

The first merged snapshot is `3423a98`. Both new migrations originally pointed
to 0068. The contract test observed two heads (1 failed/1 passed); Step6 was
renumbered to 0070 after the 0069 game migration, and the deployment contract
names 0070 and includes 0069 as an online predecessor. The DB-free head tests
then reported 2 passed/0.17s. The supplied background-column round-trip is also
included in the 189-test run below. A full independent upgrade/downgrade chain
and a deployed schema transition remain unverified.

Common shutdown correction:
- Close Run, Creative/game, background-consumer and TTS admission before waiting.
- Wait for service drain tasks concurrently with the executor heartbeat/commands.
- Executor replacement requests an unbounded background drain, preserving current
  jobs; explicit finite cancellation APIs retain their old meaning.
- Recheck TTS claim admission after asynchronous readiness/budget waits.
- Remote Qwen startup does not retry an internal TypeError as a process owner.
- Reconcile old test doubles and expectations with the combined contracts.
  Wiring-test cleanup now releases its own fake job even on assertion failure;
  previously its leaked task caused cross-loop and later game-drain failures.

Test-first evidence:
- `integrated-drain-red.txt`: 5 failing new shutdown checks before correction.
- `integrated-drain-green.txt`: 5 passed/0.39s after correction.
- `tts-claim-stop-race-red.txt`: 3 failing suspended-tick claim checks before the
  admission recheck. These three pass in the combined regression below.
- `integrated-execution-regression.txt`: first combined run 7 failed/179 passed
  in 124.73s. Generated test Bearer representations were masked in the receipt.
- `integrated-execution-regression-2.txt`: 189 passed in 62.00s, exit0. It records
  the exact command/cwd and 15 selected unit/integration files. This is not the
  full `tests` + `unit_tests` suite and does not supersede the FND rejection.
- After import/doc cleanup, changed-file Ruff reports `All checks passed!` and
  `git diff --check` is clean. The two affected wiring-test files were rerun.

DB scope: these combined tests use the explicitly provided non-production test
DB with conftest's production-name guard. They are not evidence of a separately
launched PostgreSQL server. When TTAKKARI_TEST_DB was unset, this Codex command
environment's initdb stage exited 1 and conftest hid its captured stderr
(`integration-migration-head-red.txt`). No cause or host-wide failure is inferred;
the DB-free contracts were then run with --noconftest.

Required follow-up, still open:
1. First immutable-old-generation TTS transition: independent Caddy transport
   reproduction is preserved in review branch `verify/platform-v2-fnd-2a50c86-20261025`
   at `724b648`. `/load` plus API-inflight-zero is not a transport barrier. No
   main push, deployment reservation or production cutover is justified by these
   passing tests while this accepted-work safety boundary remains unproved.
2. Local rollback/API retirement: source inspection shows readiness does not yet
   account for background-consumer inflight/pending pump work, while local API
   shutdown calls stop(0). Add a scoped reproduction and correct that boundary;
   this is currently a code observation, not a measured operational failure.
3. Explicit executor replacement/protocol activation wiring, remaining API long
   execution audit, full integration gate, model/MCP/resume/E2E and operational
   transition/rollback remain open. Activation remains disabled.

Original worktrees and shared dirty `back` are preserved. No production models,
host services, launchctl or restart scripts were executed by this integration.
