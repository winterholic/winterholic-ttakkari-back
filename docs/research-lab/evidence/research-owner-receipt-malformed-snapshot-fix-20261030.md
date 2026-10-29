# Research owner receipt: malformed snapshot correction

Observed 2026-10-30 KST on `topic/research-execution-owner-receipts-20261030`.
This is a narrow correction for the detail API regression identified against
the fixed source commit `da13f18c956671b0b1d27c7827d6742c318d9817`, based on
`e31b397b9ab8b001d00fd47e6a3ce1adac808da4`.

## Reproduction before correction

The independent report and probe were read as evidence and left unchanged:
`/Users/winterholic/development/ttakkari-research-owner-verification-20261030/verification-report.md`
and `http_flock_probe.py`. They showed an authenticated GET to
`/api/research/runs/{run_id}` returning 500 for a JSON array snapshot. The
serializer `research.service.run_out` already returns the saved JSONB value;
the new `execution_owner_receipt` call introduced the `.get()` assumption in
the detail path.

This worktree added helper counterexamples and an authenticated detail route
regression before changing production code. On the fixed starting source, the
focused test command produced:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-correction-20261030 uv run --locked pytest unit_tests/test_research_owner_receipt.py -q --tb=short
...FFFFFFF.F                                                             [100%]
8 failed, 4 passed in 0.64s
```

The helper raised `AttributeError: 'list' object has no attribute 'get'` for
truthy invalid snapshots; falsy array/boolean inputs were incorrectly reported
as `unrecorded`. The authenticated detail request reproduced
`<Response [500 Internal Server Error]>`.

## Minimal correction and contract

`execution_owner_receipt` now distinguishes a missing/`None` snapshot from a
present non-dictionary JSON value before accessing the owner key. Missing or
`None` remains `unrecorded / owner_checkpoint_missing`. A non-object snapshot
returns `unverified / environment_snapshot_invalid`, with no lock probe. An
object snapshot with a malformed non-object owner checkpoint remains
`unverified / owner_checkpoint_invalid`, also without a lock probe. Valid owner
metadata follows the existing lock observation path unchanged.

The detail response continues to serialize the original `environment_snapshot`
unchanged. No common `owner_token` behavior, global list observation, lease,
migration, stored checkpoint, lock file, or owner was changed.

The existing global list observer is a separate concern: it calls
`owner_token(row.environment_snapshot or {})` before building list receipts.
For malformed non-object snapshots on candidate queued/running rows, that
pre-existing consumer can still fail; it was not expanded into this detail
regression correction. List behavior for this malformed input was not executed
in this correction run.

## After-correction evidence

Command:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-correction-20261030 uv run --locked pytest unit_tests/test_research_owner_receipt.py -q -s --tb=short
......................HTTP detail malformed snapshot: 200 unverified environment_snapshot_invalid snapshot_preserved= ['not', 'an', 'object']
.
23 passed in 0.48s
```

The focused checks include truthy and falsy arrays, strings, numbers, and
booleans at the snapshot level; the same non-object forms at the owner
checkpoint level; missing/`None` legacy snapshots; and an authenticated FastAPI
detail request using an isolated fake DB. Invalid snapshots and owner
checkpoints assert that lock probing is not called. This is route-level test
evidence, not a PostgreSQL integration or production API observation.

## Scope retained

This correction does not complete the parent Research recovery objective. Real
PostgreSQL, scope isolation against persisted rows, production API, list
behavior for malformed snapshots, native checkpoint resume, aggregate OS
memory, legacy92, PMS 7.x 8.3/8.4/8.5 review, UX10, readiness, deployment, and
the full controller gate remain unverified or open. No service, operational
database, GPU/model, deployment reservation, or real owner lock was exercised
or modified by this correction.

## List observer follow-up — 2026-10-30

The next scoped correction on the same topic branch addresses only the
pre-existing global list observer path described above. The starting revision
was `e5c43b351f3f69a72aab18609965a387d88161ad` on
`topic/research-execution-owner-receipts-20261030`.

### Red: list helper regression before production edit

Added parameterized malformed snapshot and owner-checkpoint cases before
changing production code. Active queued/running rows must stay in
`unverified_run_ids`; malformed values must not be passed to `owner_token` or
lock probing. The focused red command produced:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-list-20261030 uv run --locked pytest unit_tests/test_research_owner_receipt.py -q -k list_owner_observer --tb=short
FFFFFFFFF........
9 failed, 8 passed, 23 deselected in 0.49s
```

All nine snapshot shapes failed because the old observer called
`owner_token(row.environment_snapshot or {})`; truthy non-objects raised in
`.get()`, while falsy non-objects were still incorrectly parsed as an empty
object. The eight malformed owner checkpoint cases passed before the fix
because `owner_token` already rejected those safely.

### Narrow change

`observe_execution_owners` now checks `isinstance(snapshot, dict)` before
calling `owner_token`. A non-object snapshot is not parsed or lock-probed; if
its Run is queued/running, the Run ID is appended to the existing
`unverified_run_ids` list. `None` and object snapshots keep their prior path.
The observer's response shape, candidate cap/truncation, lock observations for
valid owners, query filters, readiness, and raw stored snapshots are unchanged.
The owner parser and detail receipt helper were not changed in this follow-up.

### Green: helper and authenticated HTTP evidence

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-list-20261030 uv run --locked pytest unit_tests/test_research_owner_receipt.py -q -k 'list_owner_observer or authenticated_run_list' --tb=short -s
.................HTTP filtered list with global malformed candidate: 200 runs= ['407e0fb4-89b3-459d-b37f-d2bd0a942ee8'] unverified_run_ids= ['0bf2f42b-3bb3-4b8d-89cd-de4d876da567'] snapshot_preserved= ['malformed', 'global', 'candidate']
.
18 passed, 23 deselected in 0.46s
```

The route test used an isolated fake DB and mocked readiness while exercising
the real FastAPI route and authentication dependency. Unauthenticated list
returned 401. An authenticated list filtered to the visible Run's study
returned 200 with only that Run, while a queued malformed candidate from the
global candidate query remained in `unverified_run_ids`. Its source
`environment_snapshot` remained the original array, and the probe was asserted
not to run. The visible legacy `None` snapshot kept its existing `unrecorded`
receipt. This confirms route behavior with the fake DB only; it does not verify
PostgreSQL query execution or persisted-row scope isolation.

Existing valid-token lock error classification comparison:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-list-20261030 uv run --locked pytest tests/test_research_external_resources.py::test_lock_observation_error_is_unverified_not_held_or_free -q --tb=short
.                                                                        [100%]
db reset: 1 selective (avg 3.0/120 tables), 0 forced full, 0 fell back to full
1 passed in 0.63s
```

Changed-file Ruff and whitespace check:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-owner-receipt-list-20261030 uv run --locked ruff check src/ttakkari/research/resources.py unit_tests/test_research_owner_receipt.py && git diff --check
All checks passed!
```

The previous detail malformed-snapshot HTTP PASS remains the evidence for the
detail path; it was not rerun because this change does not touch the detail
receipt or serializer. The earlier broader 23-test result and prior Ruff run
are not counted as new verification here. PostgreSQL integration, production
API, service/readiness, real owner locks, deployment, and the parent Research
recovery objective remain outside this follow-up.
