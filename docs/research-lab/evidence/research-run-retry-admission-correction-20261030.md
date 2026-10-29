# Research Run retry admission correction — 2026-10-30

## Scope and source

This topic-only correction responds to verifier 951 / Run `2d7ba429` in
`research-run-retry-independent-20261030.md`. Reference backend HEAD before
this correction: `165e80147d79ec42c1259b14e8bcd10b38abd949`; the retry product
implementation remains `add75ebda42759055a6b9fe0146fb5d1c8fb4ecd`, based on
`8deece0d14dfd2e0bc07d8d594e1c939d767f944`. Frontend source was not changed
(`ff129e8f7458225d3a50158d5b96f0cb4e3c2877`). The independent verifier report
was read in full and was not edited.

The correction is limited to the existing retry execution contract. It does
not add a retry mode, snapshot contract, or new validation policy. Requirements
§13 still do not require a native execution engine for every technique; this
work does not close the wider Research v2 objective.

## Root cause from source comparison

Before the change, `retry_run` took an Experiment row lock and rejected active
or queued siblings. `start_experiment_runs` used a plain `db.get`, did not run
that admission check, then committed queued rows. Thus retry's lock did not
serialize the normal-start path, and the process-local `_ACTIVE_TASKS` map
could not coordinate another process. The independent verifier's mock
interleave showed two queued rows and replacement of the active-task entry.

Normal start called `validate_experiment_plan` with current Study scope and
Experiment method, adapter, strategy, inputs, and budget; retry skipped that
call. The verifier reached an unexecutable method state through the public
method lifecycle PATCH and observed retry queue a row anyway.

## Correction

- Both normal start and retry now call one admission helper. It obtains the
  Experiment with `with_for_update=True`, then checks the local active-task
  map and durable queued/running Run rows before creating an owner or rows.
- Both paths retain the Experiment lock through plan/configuration checks and
  the reservation commit. The queued row is committed before background task
  creation, preserving the existing recovery behavior if scheduling fails.
- Both paths now call one planner helper with the current Study scope and the
  current Experiment method IDs, adapter, strategy, search space, fixed inputs,
  and budget. An invalid plan raises HTTP 422 before Run, Event, or owner
  creation.
- Retry's source Run validation and configuration existence check remain, as
  do the new Run UUID, origin, config ID, repeat index, seed, fresh raw output,
  lineage, and actor/event behavior. The original Run is not changed.
- Owner cleanup and queued recovery code were not changed. Their earlier
  focused lifecycle evidence is retained, not rerun for this correction.

## Regression evidence

The new DB mock pauses both contenders after their queued/running check so the
check/insert race is reachable. The fake transaction models Experiment
`FOR UPDATE` as a shared row lock released at commit; this verifies both entry
points request the same lock and that retry↔normal and normal↔normal admit one
reservation. It is not evidence of PostgreSQL lock behavior.

The new invalid-plan regression exercises both entry points with current plan
fields, verifies HTTP 422, and verifies no extra Run, Event, or owner creation.

Exact focused test command and output:

```text
PYTHONPATH=src ../back-research-v2-integration/.venv/bin/python -m pytest tests/test_research_retry_admission_unit.py -q
.....                                                                    [100%]
db reset: 5 selective (avg 1.4/120 tables), 0 forced full, 0 fell back to full
5 passed in 0.57s
```

The test functions use an in-memory fake DB. However, project `tests/conftest.py`
is session-autouse and, with no explicit `TTAKKARI_TEST_DB`, starts its temporary
test PostgreSQL cluster and resets the isolated test database. Therefore this
command did initialize an isolated temporary PG test environment; it did not
run a concurrent admission test against PostgreSQL. No production DB/API,
adapter, model, GPU, or browser was used.

Exact lint command and output:

```text
../back-research-v2-integration/.venv/bin/ruff check src/ttakkari/research/runner.py tests/test_research_retry_admission_unit.py
All checks passed!
```

The initially attempted `uv run --locked pytest ...` could not initialize
`/Users/winterholic/.cache/uv` (`Operation not permitted`). Invoking the existing
integration Python without `PYTHONPATH=src` then failed during pytest setup
because its import root lacked this topic's `ttakkari.deploy_test_receipt`.
The successful command above selected this topic's source. Its project fixture
then auto-started a temporary PG cluster; this side effect was only identified
after reading `tests/conftest.py`. No direct PG/initdb command or real PG race
probe was run.

## Transaction and verification limits

Source review found `create_async_engine` does not supply an explicit
`isolation_level`; the code commits the queued reservation while the Experiment
row lock is still held. The intended cross-process serialization follows the
database row lock and the post-lock durable queued/running check. This is a
code-level design inference, not a measured PostgreSQL concurrency result.
Actual PostgreSQL concurrent interleavings, transaction isolation overrides
outside this source, and browser UI remain unverified. No direct PG race probe
was run.

The prior owner lifecycle and retry lineage tests remain valid evidence for
their original revision only. This correction does not re-claim their full
suite as freshly rerun. Frontend was untouched, so its earlier 96-test receipt
was not repeated.

The original R1–16, native checkpoint resume, aggregate OS memory, legacy 92,
PMS 7.x/8.3–8.6, UX10, and full Research v2 acceptance remain open.
Main integration, publication, and deployment remain with owner
`fcb55b7d-a9ed-4160-a395-84fe291ab9ad`.
