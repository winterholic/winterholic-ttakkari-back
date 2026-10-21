# Collector resume verification receipt — 2026-10-21

## Source and routing

- Starting local source: `66c8402c2f92d6114a9dcf1df8c6ade80e40db1a` (`main`). This checkout had unrelated staged API/orchestrator/run/schedule edits and deletions; they were left untouched.
- Remote source observed after fetch: `ba71814` (`origin/main`). It contains `0062_delivery_change_sets`, so collector schema migration is `0063_collector_stage_items` with that revision as its parent.
- Verification branch: `collector/s2-03-resume-20261021`, based on `origin/main`.
- Environment probe receipt: engine `codex`, measured `2026-10-21T14:16:57Z`; process list/sysctl/Metal were blocked in this engine sandbox. This is not a host capability claim.
- No tool in this Run exposes a routing receipt or account usage lookup. Actual model, remaining account quota, paid-overage status and confirmation of `reroute=True, task_type=general` are unverified. Model name not inferred.

## S2-02 race and attempt fencing

Contract retained in `collector/jobs.py`: cancel-first wins over succeeded/partial/retry_wait/budget_wait, while failed/policy_blocked/quarantined remain real outcomes. Regression definitions include cancel-first, cancel after final page boundary, completion-first then cancellation, and stale/wrong attempt-token fencing (`tests/test_collector_jobs.py`).

The DB-backed job tests were not executed in this run because the isolated PostgreSQL fixture failed before test collection. Thus neither race order nor attempt fencing has a passing execution receipt from this run.

## S2-03/04/06 scope

Collector policy, discovery, robots-aware bounded HTTP fetching, SSRF protections, raw content-addressed storage, extraction, normalization/language, dedupe, rule-only quality, purpose profiles and migration are present. No LLM/teacher/judge call was introduced. Public-network smoke tests, real IPv6 connectivity, rights-holder review, production DB migration and deployment were not run.

## Commands and raw output

`PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_collector_migration_chain.py unit_tests/test_collector_parser.py unit_tests/test_collector_policy.py unit_tests/test_generation_hardening.py`

```text
rc=0
75 passed, 68 subtests passed in 5.36s
```

`/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/collector src/ttakkari/collector_models.py src/ttakkari/collector/jobs.py src/ttakkari/collector/runner.py src/ttakkari/config.py src/ttakkari/generation_service.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py tests/test_collector_fetch_tls.py tests/test_collector_stage_migration.py unit_tests/test_collector_migration_chain.py unit_tests/test_collector_parser.py unit_tests/test_collector_policy.py alembic/versions/0063_collector_stage_items.py`

```text
rc=0
All checks passed!
```

`PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m alembic heads`

```text
rc=0
0063_collector_stage_items (head)
```

`/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python docs/design/collector-platform/check_delivery.py --closed`

```text
rc=0
design package: PASS; WBS/readback=25; stages=3/11/11; dependency DAG=PASS; external P0=1
fixture evidence=18 (4 positive/14 negative) + intake=10 (7 negative, tamper 4 rejected); S2-00 live=review 80%, S2-01 live=review 90% S2-02 live=review 85% (HTTP route implemented in S2-02); local document links=PASS; S2-01 implementation=reworked, awaiting independent verification; ML/deploy=NOT EXECUTED
```

`git diff --check`

```text
rc=0
(no output)
```

`uv lock --check --directory /private/tmp/ttakkari-collector-s2-03`

```text
rc=0
Using CPython 3.13.15
Resolved 196 packages in 3ms
```

DB-backed related command attempted once:

```text
.venv/bin/python -m pytest -q tests/test_collector_jobs.py tests/test_collector_fetch.py tests/test_collector_fetch_tls.py tests/test_collector_pipeline.py tests/test_collector_stage_migration.py unit_tests/test_collector_parser.py unit_tests/test_collector_policy.py
rc=4
ImportError while loading tests/conftest.py
PG_CLUSTER = pg_sandbox.start_cluster()
subprocess.CalledProcessError: Command '['/opt/homebrew/opt/postgresql@17/bin/initdb', '-D', '/tmp/ttakkari-test-48385-7f3ba591/data', '-A', 'trust', '-E', 'UTF8', '--locale=en_US.UTF-8', '--no-sync']' returned non-zero exit status 1.
```

The actual initdb stderr was not retained by pytest/fixture cleanup; root cause is unverified. No same-condition retry was made.

## Database, side effects and deployment

- DB identity: none; fixture failed before database creation. No shared PostgreSQL or production DB was used.
- Collector consume remains default-off. No schedules/stops were changed.
- Deployment was not requested and remains held for independent review by the main session.
- Commit SHA/tree and push result will be appended after commit.
