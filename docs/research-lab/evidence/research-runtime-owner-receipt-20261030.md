# Research execution-owner receipt readout

Observed and implemented 2026-10-30 KST on topic branch
`topic/research-execution-owner-receipts-20261030`, based on
`e31b397b9ab8b001d00fd47e6a3ce1adac808da4`.

## Existing contract and precise gap

- `runtime_leases.run_id` references the agent `runs` table; it cannot represent
  `research_runs`. Research already persists its own fenced
  `environment_snapshot.research_execution_owner` (contract, token, generation,
  accepted time) and holds a generation-scoped OS flock across the batch.
- The Research list API already reports observed held locks globally. The
  existing run detail returns its saved environment/resource checkpoints,
  including host `launch_id`, cleanup result/error, and crash recovery evidence.
- The missing connection was a run-scoped API observation of whether the saved
  Research owner lock is currently held or available. A terminal Run's status
  alone cannot supply that observation; the global active-lock list also omits
  released rows.

## Narrow product delta

Added `execution_owner_receipt` to Research Run list and detail responses. It
joins the existing owner checkpoint to its Study/Experiment/Run/adapter scope
and makes one fresh nonblocking flock observation. It reports `owned`,
`released`, `unverified`, or `unrecorded`, with evidence basis and observation
time. Existing resource launch/cleanup receipts remain in the saved
`environment_snapshot`; there is no parallel lease table, new migration,
resource cleanup action, or old-Run backfill. Existing authenticated route
dependencies and owner-token fencing remain in force.

`released` means the OS lock was available at this read; it is not a durable
historical release event. Missing legacy checkpoints stay `unrecorded` and
probe failures stay `unverified`. This observation does not prove GPU/CPU
exclusivity or cleanup of a child resource; those claims remain bound to their
separate saved resource/recovery receipts.

## Regression and checks

The new DB-free unit contract first failed on all three cases because the
per-Run receipt helper did not exist. After the minimal implementation:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-runtime-receipts uv run --locked pytest unit_tests/test_research_owner_receipt.py -q --tb=short
...                                                                      [100%]
3 passed in 0.27s

$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-runtime-receipts uv run --locked ruff check src/ttakkari/research/ownership.py src/ttakkari/api/research.py unit_tests/test_research_owner_receipt.py
All checks passed!

$ git diff --check
exit 0
```

The unit checks cover held/released lock observations, run scope, absent/invalid
owner checkpoints, and failed lock probe/close. No database, runtime API, host
GPU, model, browser, service, or deployment was exercised. Thus HTTP/isolated-DB
integration and production presentation remain unverified for the normal
controller gate.

## Still open

Research native checkpoint resume, per-resource crash cleanup beyond existing
observations, total memory/OS ceiling, legacy92, PMS7.x 8.3/8.4/8.5 review,
original authentication UX10, and the full controller gate remain open. This
topic is not a status promotion or closure of the parent Research objective.
