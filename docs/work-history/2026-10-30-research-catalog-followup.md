# Research Catalog read-path and gate evidence follow-up

- Continued `research/runtime-readback-20261030` from `aa4f89532a8751910f315f483eb20c33ff84acc2`; prior runtime-readback documents were read from that commit.
- `catalog_search` without `q` returned the existing Catalog (112 total); `target=rag` returned 14 methods. `method_get("hybrid_retrieval")` resolved to the same seeded system method ID `96f518c0-a0c1-42bc-b7c6-9f314ed69cfc`, with `adapter_hints=["retrieval_eval"]`. The earlier `q="research"`/`q="retrieval_eval"` zero-result observations remain query-specific.
- `select(candidate_source="catalog")` calls `ensure_catalog`, writes a `selection_recorded` event, and commits. No select was invoked against the unrelated Eval harness study. The local test-app pair in `final-ui-adoption-review-5a2650f.json` (`bfa7a763…` / `fbb86f2e…`) is not live Catalog evidence; live `method_get(fbb86f2e…)` returned 404. No PMS Catalog acceptance study/method pair was identified; parent Platform V2 PMS ID `e62fbc3f…` alone is not that target.
- Deployed revision `48eac0c6` contains four DB-backed async cases in `tests/test_research_catalog_selection.py`. Its full deployment gate selects the file in `pytest -q --timeout=120 -m "not security" ... --sw`; this module is not security-marked. The supported deploy log proves the gate ran and ended `6061 passed, 19 skipped, 2 xfailed, 5022 subtests passed`, but omits argv and node-level outcomes. Therefore none of those four named DB cases is claimed PASS. This corrects ambiguity with the earlier four focused unit tests (three Catalog units plus one unrelated scheduling test). No matching CI/JUnit artifact was found in this checkout.
- Exact evidence and read command/MCP results: [Catalog follow-up evidence](../research-lab/evidence/research-catalog-followup-20261030.md). No test, DB, production UI, deploy, restart, main, or other-Run action was run.
- Required next evidence: locate the exact PMS child and matching existing live owner-scoped study/method ID; collect a normal gate artifact with exact revision/argv/selection, skip flag+reason, four named outcomes/skip reasons, and isolated DB identity. Any shared gate logging change belongs to backend main owner `fcb55b7d`; this topic branch did not edit common gate code. Authenticated production UI remains unverified.
- At this entry's creation, the topic commit/publish state is to be recorded in the final response after the evidence-only commit and remote-ref check.

## DB4 deploy-gate receipt follow-up (2026-10-30)

The supported `ops_deploy_log(job="1b79dd88", mode="range", offset=1, lines=128)` read recovered the full stored log for job `1b79dd88-7bf1-4e01-b3b3-024ba0f8b251`, revision `48eac0c612d7186e8c418fe607a036ea2ab4da09`, status `succeeded`. It ends with `6061 passed, 19 skipped, 2 xfailed, 31 warnings, 5022 subtests passed in 1433.50s`; the preceding line only reports aggregate DB resets. The stored output contains no exact pytest argv, marker/bypass reason, isolated DB name, named DB4 terminal outcomes or skip reasons. The existing successful job therefore remains aggregate-only evidence; no named DB4 pass is inferred.

On infrastructure main, a small pytest receipt helper was added for exactly the four named `tests/test_research_catalog_selection.py` DB cases. The integration test `unit_tests/test_deploy_test_receipt.py` covers URL-to-database-name privacy, passed/skipped/xfail/not-run classification, revision/argv/marker/skip fields, and restricted registration to a full revision with a per-job isolated database. `tests/conftest.py` registers the hook; it activates only when the process has a 40-character release revision and a `ttakkari_gen_*` or `ttakkari_deploy_*` test database. Its line-oriented output contains the revision, each argv item, marker expression, `skip_tests=false` (the test process proves the bypass was not taken), isolated database name only, and one result/reason for every target node ID. It records tests not reached before `--sw` interruption as `not_run`, never as pass.

Focused verification in the integration workspace:

```text
$ env -u TTAKKARI_TEST_DB -u TTAKKARI_DATABASE_URL -u DATABASE_URL PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_deploy_test_receipt.py --tb=short
.......                                                                  [100%]
7 passed in 0.14s

$ /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/deploy_test_receipt.py unit_tests/test_deploy_test_receipt.py tests/conftest.py
All checks passed!

$ git diff --check
(exit 0)
```

This did not run any DB-backed suite or the deploy gate. The normal gate's `skip_tests=true` bypass path is owned by `deployment.py`/`generation_deployment.py`, currently being changed in a separate supervisor recovery task. Those files and gate behavior were not changed before that result. Therefore a pytest invocation can now report `skip_tests=false`, but an explicitly bypassed stage does not yet emit this per-test receipt; its bypass reason remains a follow-up delta to integrate after the supervisor work. No deployment reservation, service operation, database retry, or runtime change was made.

The evidence documents copied from `research/runtime-readback-20261030` are unmodified source copies. `docs/work-history/2026-10-29-research-platform-v2-integration.md` is identical at the e31 candidate and current main baseline (SHA-256 `2908e21d717cf520201b1ca27f5cdb00d75403093a5bc242eabb25b5872be4f0`); it was not overwritten or re-added. Historical text naming fcb55b7d as the then-current owner describes the state recorded by the source evidence. Current main integration and deployment ownership for this continuing recovery is held by root.
