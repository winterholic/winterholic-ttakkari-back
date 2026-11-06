# ArtCraft final main integration and release gate

Date: 2026-11-07 (Asia/Seoul)

## Run identity and test database owner proof

Run command:

```sh
/Users/winterholic/bin/ttk get "/api/runs/$TTAKKARI_RUN_ID"
```

Exit 0. Run `d9af5337-e0ad-4642-8787-8db34c8b6d46` is `running`, engine `codex`, model `gpt-6-luna`, effort `high`, profile `회사 코덱스 ai2 (ai2@hyperez.io)`.

Before pytest or any DB-resetting hook, an async SQLAlchemy connection built from the injected `TTAKKARI_TEST_DB` ran `SET TRANSACTION READ ONLY` and:

```sql
SELECT current_database(), current_user, inet_server_port(),
       current_setting('data_directory'), current_setting('transaction_read_only')
```

The URL was not printed. The exact marker read was only `Path(data_directory).parent / "run-test-cluster.marker"`. Parsing used UTF-8 and `raw.splitlines()`, with each line split by `partition('=')`, rejecting missing separators, empty keys/values, and duplicate keys. Raw marker bytes and representation were emitted before parsing or validation.

```text
injected_run_id=d9af5337-e0ad-4642-8787-8db34c8b6d46
configured_db_identity=driver:postgresql+asyncpg,host:127.0.0.1,port:51413,database:ttakkari_test,username:ttk_test
sql_identity=database:ttakkari_test,user:ttk_test,port:51413,data_directory:/tmp/ttakkari-run-test-99596-e4122acb/data,transaction_read_only:on
exact_parent_marker=/tmp/ttakkari-run-test-99596-e4122acb/run-test-cluster.marker
marker_raw_bytes=137
marker_raw_repr=b'prefix=ttakkari-run-test-\\nfolder=ttakkari-run-test-99596-e4122acb\\nowner_pid=99596\\nrun_id=d9af5337-e0ad-4642-8787-8db34c8b6d46\\nport=51413\\n'
marker_parse={'prefix': 'ttakkari-run-test-', 'folder': 'ttakkari-run-test-99596-e4122acb', 'owner_pid': '99596', 'run_id': 'd9af5337-e0ad-4642-8787-8db34c8b6d46', 'port': '51413'}
marker_parse_error=None
marker_owner_uid=501,marker_owner=winterholic,folder_owner_uid=501,folder_owner=winterholic
marker_stat_error=None
checks={'url_host_loopback': True, 'url_sql_database': True, 'url_sql_user': True, 'url_sql_port': True, 'transaction_read_only': True, 'exact_data_parent': True, 'folder_prefix': True, 'marker_prefix': True, 'marker_folder': True, 'marker_run_id': True, 'marker_port': True, 'marker_owner_pid_positive': True, 'owner_uid_positive': True, 'owner_uid_is_current_user': True, 'marker_parse_valid': True, 'marker_read_valid': True}
DB_OWNER_PROOF=PASS
```

The earlier turn described a `JSONDecodeError` as evidence that the marker was empty. That conclusion was unsupported and is corrected here: the earlier script applied JSON parsing to a key=value marker and raised before printing its bytes. The earlier run's marker contents are not asserted or re-read. This proof applies only to Run `d9af5337-e0ad-4642-8787-8db34c8b6d46`.

## Integration and focused verification

- Latest `origin/main` fetched as `b4a5c173e9a4c4e082a78c2663056630030403c9`; `git ls-remote origin refs/heads/main` matched.
- A new isolated branch `release/artcraft-final-20261107` was created at that full SHA. Existing release, audit and dirty main worktrees were left intact.
- `c140efba9ee8e1419dc739c446d5096ac4131b4f` was merged with content conflicts in `src/ttakkari/collector/precise_eval.py` and `tests/test_collection_missions.py`. Base/ours/theirs showed latest main caught `L2TransportError` directly while c140 introduced a collector-neutral error and runtime translation; the resolved code keeps c140's neutral boundary and the latest-main surrounding code. The c140 runtime-translation test, including retry/receipt fields and `_runtime` restoration, was retained. Paths were resolved with path-specific `git add`; `git diff --name-only --diff-filter=U` returned no paths. The merge commit is `62d59d91837288b35b938b672625ea84ca3eabf4`.
- The original independent checkout's `HEAD` and this repository's `git rev-parse 8e64efff02f17bcc5d85cbef3f9bf3c222eca409^{commit}` both returned `8e64efff02f17bcc5d85cbef3f9bf3c222eca409`; its parent is c140. It was merged as commit `8e4b343f5004fc627cbb5dc84853b1edb53dc600`. `git merge-base --is-ancestor` returned 0 for c140, evidence 8e64, ArtCraft pin/lookup source commit `cc9f9dd6da7a0adfa5258f8ec875768af9d916de`, updater source commit `f1b69cf3691ba378d6c775eb94985cdfe129d233`, collector source commit `ead9ef8f9f409301d0ea2bfa17b20d5c5b8ba9bc`, and runtime-restoration test commit `80b3116ec4e132f5cf668463c6479e1c5ffc5815`.
- Latest-main ArtCraft source remains in the final tree: `api/artcraft.py` has update/rollback/status routes and `account_id` inputs; `artcraft_tasks.py` has pin resolution and lookup; `artcraft_update.py` has apply/rollback/update-info handlers. These product files are inherited from latest main rather than part of this collector-only delta. The delta from `origin/main` contains collector source/tests, both evidence files, and work-history.
- The integrated tests exposed an outdated latest-main assertion for `precise_eval.L2TransportError`. It was replaced with the collector-neutral contract assertion; that new test first failed because the module still exposed the unused alias. Removing the unused import made it pass. The subscription suite then exposed invalid-scope availability propagating `PreciseEvaluatorTransportError`; runtime availability now returns `(False, diagnosis, retry_after)` for that neutral error, matching the existing diagnostic test.
- Final focused commands and results after the correction:

```text
PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_collection_precise_subscription.py -q -x --timeout=120 -p no:cacheprovider
21 passed in 0.33s

PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_collection_precise_eval.py -q -x --timeout=120 -p no:cacheprovider
10 passed in 0.25s

PYTHONPATH=src ../back/.venv/bin/pytest 'tests/test_collection_missions.py::test_precise_runtime_translates_transport_errors_without_losing_retry_or_receipt' -q --timeout=120 -p no:cacheprovider
3 passed in 1.09s; db reset 3 selective, 0 forced full/fallback

PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_missions.py -q -x --timeout=120 -p no:cacheprovider
45 passed in 5.77s; db reset 45 selective, 0 forced full/fallback

PYTHONPATH=src ../back/.venv/bin/pytest 'tests/test_collector_pipeline.py::test_collector_package_has_no_llm_or_knowledge_dependency' -q -x --timeout=120 -p no:cacheprovider
1 passed in 0.63s; db reset 1 selective, 0 forced full/fallback

../back/.venv/bin/ruff check src/ttakkari/collector/precise_eval.py src/ttakkari/collector_precise_runtime.py tests/test_collection_missions.py unit_tests/test_collection_precise_subscription.py
All checks passed!

git diff --check
[no output; exit 0]
```

- The first unit-suite run failed on the obsolete shared-error assertion; the corrected neutral-boundary test then passed. A subsequent unit-suite run found the availability propagation failure described above; the focused regression and full subscription suite passed after the runtime fix. Ruff first reported duplicate imports/constants, then import formatting; both were corrected, and the final Ruff run passed. These failures were corrected in the same transport-boundary scope; no guard was weakened.
- The local full security suite was not rerun (prohibited due to the prior Codex host-operations EPERM). The supported controller's required security/full gate remains enabled for the deployment reservation. At this point there has been no push, deploy request, restart, account switch, new initdb, or provider operation.
