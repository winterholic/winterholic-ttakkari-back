# P0 release gate: generic game smoke input sampling

Base: `bf8d31f99c24f04a6a42df27bc4aeb661332d631`, clean task worktree
`/private/tmp/tk-p0-snapshot-fix-20261025`. The shared checkout was not edited.

## Observed release failure

After an account handoff failed with the Qwen-local 16384 UTF-8 byte task limit,
the lead restored manage_work revision112 and inspected deployment
`9f007f8a-f4ee-4d5a-863d-fba764cbf2df` through the supported MCP tools.

The candidate failed release pytest at 2026-10-25T12:47:06Z:
`1 failed, 1458 passed, 10 skipped` in 652.46s. The failing test was
`test_game_smoke_generic::test_generic_smoke_passes_working_games_and_flags_broken_ones`.
`ops_deploy_log` range34-52 exposed `coin`, `desktop`, `inputEffect=none`, and
the sole problem "어떤 입력에도 상태·화면이 바뀌지 않았다". Console errors,
page errors, failed requests and bad responses were all empty. This is not the
previous snapshot failure: pytest stopped before reaching that integration test,
so the snapshot correction's full E2E result is still unverified.

The test's JSON evidence path from the log no longer existed under the allowed
temporary directory. Protected raw logs were not opened. The code previously
reported only the verdict/console tuple, losing its baseline and individual
input observations in the deployment log.

## Deterministic reproduction

The actual `smoke_generic.mjs` is copied unchanged into a temporary test folder
next to a minimal fake GameSession driver. Node runs the real scenario, crypto
hashes and verdict code; only the browser/game clock is replaced. Frames arrive
every 500ms at a phase that falls in the existing 150ms key-up gaps. Its
350ms-held/150ms-released keyboard probes all miss the frame, although the
simulated game responds on a frame whenever a key is held. Idle frame counts
are excluded by the scenario's existing noise filter.

RED: `PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q
unit_tests/test_game_smoke_timing.py` produced `1 failed, 1 passed in 0.17s`:
the responsive game was falsely classified `none`; the static negative control
was correctly rejected. This establishes a sampling defect under that schedule.
It does not prove the released browser used that frame schedule. Focus loss or
other causes of the release failure remain unexcluded until browser integration.

The keyboard probe now holds its key while polling for an observed non-noisy
state change or non-idle pixel change, with a 2-second maximum per input and
key release in `finally`. A real observed response ends further input exploration;
the static control still exhausts the bounded probes and fails. No verdict
criterion is relaxed. Mobile gestures retain their existing input behavior.
Failed coin/dungeon/late assertions now print multiline baseline/probe evidence.

GREEN after adding state-based and pixel-only cases:
`PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q
unit_tests/test_game_smoke_timing.py` -> `4 passed in 0.30s`.
Ruff on the two Python test files: `All checks passed!`;
`node --check tests/game_smoke/smoke_generic.mjs` and `git diff --check`: exit0.

## Environment and remaining validation

Codex command-sandbox `browser_preflight.py` returned exit78 / `status=blocked`,
`browser_launched=false`, with mach-register and launchservicesd/coreservicesd
mach-lookup denied. No local browser was launched or retried; no alternate
engine was used to bypass this denial. The Node regression starts no browser.

Actual Phaser/Playwright interaction, the complete release gate, snapshot E2E,
production P0 backup/restoration and remote activation remain unverified. The
deployment controller must run the full gate with `skip_tests=false`. Existing
Codex `setpriority` EPERM is not retried. No unchanged candidate is redeployed.
