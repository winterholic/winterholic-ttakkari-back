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

## Integration state before verification

- Latest `origin/main` fetched as `b4a5c173e9a4c4e082a78c2663056630030403c9`; `git ls-remote origin refs/heads/main` matched.
- A new isolated branch `release/artcraft-final-20261107` was created at that full SHA. Existing release, audit and dirty main worktrees were left intact.
- Merging accepted `c140efba9ee8e1419dc739c446d5096ac4131b4f` produced content conflicts only in `src/ttakkari/collector/precise_eval.py` and `tests/test_collection_missions.py`. The conflict contents were reconciled against the base/ours/theirs versions: the neutral collector transport exception and actual runtime translation/restoration cases from c140 are retained, while surrounding latest-main content is preserved. The worktree files no longer contain conflict markers. Both paths and the work-history record were registered with path-specific `git add`; `git diff --name-only --diff-filter=U` returned no paths. No regression tests have run yet.
- Independent evidence target is `8e64efff02f17bcc5d85cbef3f9bf3c222eca409`, whose parent is c140. It has not yet been merged into the final branch.
- No pytest or hook has run in this Run before this DB proof. No commit, push, deploy request, service restart, account switch, new initdb, or provider operation has occurred.
