# ArtCraft exact account pin — implementation evidence

Date: 2026-11-07 KST
Worktree: `back-artcraft-account-pin-20261107`
Base: `27507965fec7b30d06fd0149ac1af171363df607`
Implementation commit: `4995d783aa6038aa33d9d5d7557afde619782d53`

## Contract and persisted behavior

- `TaskIn.account_id` is an optional EngineAccount UUID. Omission keeps the previous automatic selection behavior.
- The creation API checks exact account existence, personal pool membership, enabled state, execution readiness, usage quota and model cooldown. Failure returns HTTP 503 `account_unavailable`; no other account is considered.
- The selected UUID is stored in nullable `sessions.account_id` and `Run.usage.artcraft.account_id`. Migration `0077_artcraft_account_pin` adds the nullable column and index without a foreign key, so deleting an account cannot clear a pin and silently enable fallback.
- `RoundIn.account_id` may repeat the saved UUID. A different UUID, or adding a pin after task creation, returns HTTP 409 `account_pin_immutable`.
- Manager eligibility filters candidates to that exact enabled account. A limited account is the only quota wait candidate. After usage-limit, authentication, readiness, or other account failure, the manager cannot choose a peer account or emergency route. Queue recovery and restart re-evaluate the same persisted session UUID through `_eligible_profiles`.

## Current Run routing check

- `read_session(session_id=12e81f08-9629-4023-ac21-35a30640c318, last=1)` returned `execution.head_run_id=820bd34c-12f6-4cbd-a591-9128fafc1b79`.
- `/Users/winterholic/bin/ttk get /api/runs/820bd34c-12f6-4cbd-a591-9128fafc1b79` returned `profile=회사 코덱스 ai2 (ai2@hyperez.io)`, `engine=codex`, `model=gpt-6-luna`.
- The attempted `read_session` using `CODEX_SESSION_ID` returned 404 `not_found`; the worker session ID above came from the Run API and then produced the matching session read. No account toggle, account change or worker creation occurred.

## TDD and isolated verification

The tests ran through `tests/conftest.py` using the configured test database. Before relying on it, the connection target was checked without reading application data:

```text
current_database = ttakkari_test
server_port = 58819
data_directory = /tmp/ttakkari-run-test-99596-737d6f41/data
```

The data directory is a run-specific temporary PostgreSQL cluster, separate from production and release databases. No release DB was inspected or shared.

RED, before implementation:

```text
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py -k 'account_id_is_persisted or invalid_account_id'
2 failed
AttributeError: 'ChatSession' object has no attribute 'account_id'
AssertionError: expected 503, got 201 (unknown account_id was ignored)
```

GREEN and regression checks:

```text
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_artcraft_tasks.py
36 passed in 6.68s

PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_worker_routing.py tests/test_worker_quota_recovery.py tests/test_api_runs.py tests/test_failed_runs.py tests/test_run_dispatch_transaction.py
132 passed in 22.57s

PYTHONPATH=src ../back/.venv/bin/ruff check src/ttakkari/api/artcraft.py src/ttakkari/artcraft_tasks.py src/ttakkari/models.py src/ttakkari/runs/manager.py tests/test_artcraft_tasks.py alembic/versions/0077_artcraft_account_pin.py
All checks passed!

PYTHONPATH=src ../back/.venv/bin/alembic heads
0077_artcraft_account_pin (head)

git diff --check
(exit 0; no output)
```

Focused fake-engine scenarios assert that a higher-priority same-engine account does not replace the request UUID on initial or follow-up Runs; non-existent, wrong-pool, disabled, unready and limited requested accounts fail closed; a live quota failure produces `account.limited` + `run.waiting` with no `engine.fallback` or emergency routing event; and an authentication-classified fake-engine failure terminates on the pinned profile with no alternate account. The wait-state candidate inspection returned only the requested UUID and confirmed the same ID in Run usage metadata.

## Manager impact and boundaries

The common manager path changes only when `ChatSession.account_id` is non-null: it filters eligible profiles and prevents the exhausted-account emergency handoff. Existing engine pins, automatic account selection, shared ArtCraft task admission locks, updater behavior, and unspecified requests remain on their previous paths. The worker routing, quota recovery and ordinary Run regression set above passed.

Not exercised: real provider execution, operator-controlled account login/readiness, release database, migration upgrade against production-like data, deployment and push. All execution verification used fake providers and the isolated temporary test PostgreSQL cluster.

Clean implementation commit: `4995d783aa6038aa33d9d5d7557afde619782d53`.
