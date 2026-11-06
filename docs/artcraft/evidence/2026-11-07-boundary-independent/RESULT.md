# ArtCraft collector boundary independent verification

Date: 2026-11-07 (Asia/Seoul)

## Verdict

**Accept c140efba9ee8e1419dc739c446d5096ac4131b4f for the collector dependency-boundary and test-runtime restoration fixes.** This is not acceptance of a complete release or production operation: this verification did not run the full release/security suite, push, deploy, inspect deployment state, call a provider, or exercise ArtCraft production workflows.

## Run and DB identity, established before every pytest command

Command:

```sh
/Users/winterholic/bin/ttk get "/api/runs/$TTAKKARI_RUN_ID"
```

Exit 0. Response fields: `id=0c306e77-dc18-43b2-917d-43db1fd7a293`, `status=running`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`.

The first SQL attempt used the absent release-local `.venv/bin/python` and exited 127 (`zsh:1: no such file or directory`). No pytest had run. The retry used the repository's existing Python environment and ran async SQL inside a read-only transaction; the URL itself was not printed:

```sh
PYTHONPATH=src ../back/.venv/bin/python - <<'PY'
# SQLAlchemy async engine from os.environ["TTAKKARI_TEST_DB"]
await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
SELECT current_database(), current_user, inet_server_port(),
       current_setting('data_directory'), current_setting('transaction_read_only')
# Read only Path(data_directory).parent / "run-test-cluster.marker"
PY
```

Exit 0. Raw identity summary:

```text
injected_run_id=0c306e77-dc18-43b2-917d-43db1fd7a293
configured_db_identity=driver:postgresql+asyncpg,host:127.0.0.1,port:52343,database:ttakkari_test,username:ttk_test
sql_identity=database:ttakkari_test,user:ttk_test,port:52343,data_directory:/tmp/ttakkari-run-test-99596-d994cc92/data,transaction_read_only:on
exact_parent_marker=/tmp/ttakkari-run-test-99596-d994cc92/run-test-cluster.marker
marker_contents={"folder":"ttakkari-run-test-99596-d994cc92","owner_pid":"99596","port":"52343","prefix":"ttakkari-run-test-","run_id":"0c306e77-dc18-43b2-917d-43db1fd7a293"}
marker_owner=winterholic,folder_owner=winterholic
```

Configured URL identity and SQL database/user/port matched. Only the exact marker returned by the SQL data directory was read. Every pytest below ran after this proof; no initdb was run.

## Immutable source and parent red/target green

- Target checkout `/private/tmp/artcraft-boundary-target-20261107`: full HEAD `c140efba9ee8e1419dc739c446d5096ac4131b4f` (detached).
- Parent checkout `/private/tmp/artcraft-boundary-parent-20261107`: full HEAD `8a0b289c3d0d432180de0a70bc3304170d6e57c6` (detached).
- Original release checkout remained read-only at target SHA.

Parent command:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_collector_pipeline.py::test_collector_package_has_no_llm_or_knowledge_dependency -q -x --timeout=120 -p no:cacheprovider
```

Exit 1, expected red:

```text
AssertionError: precise_eval.py imports ttakkari.kb_pipeline.subscription_transport
1 failed in 0.67s
db reset: 1 selective; 0 forced full; 0 fell back to full
```

Same command in target checkout: exit 0, expected green:

```text
. [100%]
db reset: 1 selective; 0 forced full; 0 fell back to full
1 passed in 0.83s
```

The structural guard diff from parent to target is empty. Its full forbidden-prefix tuple, `runner.py` exception, and required `ttakkari.collector_models` assertion are unchanged; no allowlist, skip, or weakened expected value was introduced.

## Boundary and outcome checks

Independent probe command:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest docs/artcraft/evidence/2026-11-07-boundary-independent/test_transport_probe.py -q -x --timeout=120 -p no:cacheprovider
```

Exit 0: `5 passed in 0.29s`. It injects actual `L2TransportError` at `collector_precise_runtime.evaluate`, checks diagnosis/consumed/retry_after/engine/account/model and original `__cause__`, verifies unrelated exception identity through evaluate and availability, checks personal/work acceptance plus negative-scope rejection, malformed schema cases, and availability's waiting tuple.

Transport mission command:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_collection_missions.py::test_precise_runtime_translates_transport_errors_without_losing_retry_or_receipt -q -x --timeout=120 -p no:cacheprovider
```

Exit 0: `3 passed in 1.34s`; `db reset: 3 selective, 0 forced full, 0 fell back to full`. Cases verify timeout/consumed=true → unresolved with no retry; timeout/consumed=false → pending/waiting with retry; usage-limit → pending/limited with retry; each keeps retry timestamp and engine/account/model receipt, reservation remains `unknown` and fully charged, and `_runtime` identity returns to its initial object.

Unavailable/waiting classification:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_collection_mission_stage2.py::test_ready_engine_with_unavailable_scope_stays_waiting -q -x --timeout=120 -p no:cacheprovider
```

Exit 0: `1 passed in 1.02s`; `db reset: 1 selective, 0 forced full, 0 fell back to full`.

Settled-result recovery after simulated worker crash and then unavailable engine:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_collection_missions.py::test_precise_result_survives_worker_crash_after_budget_settlement -q -x --timeout=120 -p no:cacheprovider
```

Exit 0: `1 passed in 1.03s`; `db reset: 1 selective, 0 forced full, 0 fell back to full`.

## Focused regression summaries (serial)

All commands used `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest <path> -q -x --timeout=120 -p no:cacheprovider`, from the target checkout:

| Path | Exit | Raw summary |
|---|---:|---|
| `unit_tests/test_collection_precise_eval.py` | 0 | `10 passed in 0.24s` |
| `unit_tests/test_collection_precise_subscription.py` | 0 | `18 passed in 0.35s` |
| `tests/test_collector_pipeline.py` | 0 | `29 passed in 3.66s`; 29 selective resets, 0 forced full/fallback |
| `tests/test_collection_missions.py` | 0 | `45 passed in 6.77s`; 45 selective resets, 0 forced full/fallback |
| `tests/test_collection_mission_stage2.py` | 0 | `5 passed in 1.39s`; 5 selective resets, 0 forced full/fallback |

The table commands were run individually in the listed order. The separately reported transport, waiting, and recovery checks were run afterward.

## Delta review and static checks

- `git diff 8a0..c140 -- tests/test_collector_pipeline.py`: empty. Guard rules/expectations are preserved.
- `git diff 548df81697be1bb4e87e6efb145f68a6fb03ee55..c140 --` over precise collector source/tests and ArtCraft paths showed only the intended boundary changes in `precise_eval.py`, `collector_precise_runtime.py`, `test_collection_missions.py`, and `test_collection_precise_subscription.py`; no direct ArtCraft pin/lookup/updater/video CLI delta. Existing source548 and ArtCraft feature verification can therefore be reused only for those unchanged paths.
- Ruff command over both changed source modules, affected tests, and this independent probe: exit 0, `All checks passed!`.
- `git diff --check`: exit 0, no output (verification artifact was then still untracked; it was added before the final repeat check and commit).

## Limits and acceptance scope

No new product or release code was changed. No full release/security suite, security `host_operations` command, environment probe, provider call, Git push, deployment lookup/reservation, runtime polling, service restart, or production ArtCraft operation was performed. Candidate acceptance here is limited to the dependency-boundary fix, retry/accounting behavior, and test-runtime restoration at the stated immutable source SHA.
