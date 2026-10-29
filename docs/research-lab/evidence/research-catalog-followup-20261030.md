# Research Catalog read path and deploy-gate evidence follow-up

- Observed: 2026-10-30 KST, on branch `research/runtime-readback-20261030`, starting from `aa4f89532a8751910f315f483eb20c33ff84acc2`.
- Scope: supported read-only Catalog calls, source-level side-effect check, reconstruction of the successful `48eac0c6` gate evidence, and exact target-ID recovery from existing records.
- No tests, local database, production database, UI login, deployment, or service control were run. No research/experiment/fixture was created.

## Catalog read path

The previous `catalog_search(q="research")` and `catalog_search(q="retrieval_eval")` results remain valid query-specific observations: each was empty. The dashboard's `methods=112` is an aggregate and did not establish search behavior.

The MCP schema accepts `operation="catalog_search"` with `data` filters `q`, `target`, `layer`, `goal`, `intervention`, `research_method`, `execution_status`, `lifecycle_status`, `origin`, `limit`, and `offset`. In `src/ttakkari/api/research.py`, the agent route forwards those values to `list_methods(...)`; it does not call the catalog initializer. The supported `method_get` branch resolves an ID/slug through `get_method(...)`.

Read-only MCP calls and compact returned fields:

```text
manage_research(operation="catalog_search", data={limit: 50, offset: 0})
=> total=112, limit=50, offset=0; first-page items are returned. Facets include targets.rag=14.

manage_research(operation="catalog_search", data={target: "rag", limit: 50, offset: 0})
=> total=14. Matching examples:
   96f518c0-a0c1-42bc-b7c6-9f314ed69cfc hybrid_retrieval adapter_hints=[retrieval_eval]
   a956053c-1af3-42fd-bb0c-444c0b9639a9 cross_encoder_reranking adapter_hints=[retrieval_eval]
   8b625ba9-702c-4054-af94-8e5d4c22ed33 reciprocal_rank_fusion adapter_hints=[retrieval_eval]
All 14 returned candidates had scope=system, origin=system_seed, and source_study_id/source_node_id=null.

manage_research(operation="method_get", method_id="hybrid_retrieval")
=> id=96f518c0-a0c1-42bc-b7c6-9f314ed69cfc, slug=hybrid_retrieval,
   scope=system, origin=system_seed, lifecycle_status=validated,
   targets=[rag,recommender_search], adapter_hints=[retrieval_eval],
   execution_status=immediately_executable, related_experiments=[].

manage_research(operation="method_get", method_id="fbb86f2e-479a-4074-95f6-c2d7686bd79c")
=> 404 research_method_not_found.
```

This establishes that the deployed owner-scoped Catalog read path receives seeded Catalog methods, and that the Catalog search item ID resolves through `method_get`. It does not establish that a Catalog candidate was persisted into a study by `select`.

The `select` path is state-changing. `select_methods()` calls `ensure_catalog()`, obtains candidates, appends a `selection_recorded` event, and commits. The MCP `manage_research` operation dispatches `select` to that route. No `select` call was made: the only live personal study previously identified was the unrelated Eval harness study, and using it would write acceptance data to an unrelated study.

## Existing target-ID record check

- Existing local evidence `docs/research-lab/evidence/final-ui-adoption-review-5a2650f.json` records a **test-app** pair: study `bfa7a763-3e8d-4179-9e59-50655b9df500` and method `fbb86f2e-479a-4074-95f6-c2d7686bd79c`. The live `method_get` above returned 404 for that method ID, so this fixture evidence is not an existing live Catalog target.
- Existing live readback records the unrelated personal Eval harness study `eb41e3a7-43cd-4f2b-8c03-79380a2e1163`; its resume success is not Catalog acceptance evidence.
- The Research documents mention the parent Platform V2 PMS project `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286`, but a PMS Catalog-acceptance child ID linked to a live Research study/method pair was not found in the two-hop record check. This pair remains unidentified; IDs above must not be promoted to that target.

## Successful `48eac0c6` gate: what is established

The exact `48eac0c612d7186e8c418fe607a036ea2ab4da09` source tree was available locally. Its `generation_deployment.py`, `tests/test_research_catalog_selection.py`, `tests/conftest.py`, and `pyproject.toml` blobs match this checkout (empty `git diff --stat` for those paths). The deployed test file has exactly four async DB-backed named cases:

1. `test_catalog_selection_keeps_failure_conditions_scope_and_event`
2. `test_catalog_selection_reports_bounded_candidate_and_failure_coverage`
3. `test_catalog_relation_preserves_other_source_node_condition`
4. `test_catalog_selection_initializes_seed_catalog_for_direct_callers`

The release code's normal pytest command is `<release>/.venv/bin/python -m pytest -q --timeout=120`. The main full-suite invocation adds `-m "not security" -o cache_dir=<job-specific-main-cache> --sw`; the security-marked suite is run in the earlier security-gate stage. Up to 10 prior failed node IDs may be run first, but the code comments and flow confirm they remain in the later full suite. A failing node may be resumed by stepwise retries (maximum four full-gate command runs). The overall test stage can be bypassed only when the deploy job's `skip_tests` flag is set; the successful job's pytest progress and final summary prove pytest did run. This target file has no security marker, so its four tests are in the main selection when collection succeeds.

The supported deploy log for job `1b79dd88-7bf1-4e01-b3b3-024ba0f8b251` records revision `48eac0c612d7186e8c418fe607a036ea2ab4da09`, status `succeeded`, and these terminal lines:

```text
db reset: 3663 selective (avg 3.8/120 tables), 1 forced full, 1 fell back to full
6061 passed, 19 skipped, 2 xfailed, 31 warnings, 5022 subtests passed in 1433.50s (0:23:53)
```

The stored log has 128 lines / 10,624 bytes. It contains pytest progress, warning details, DB-reset aggregate, and suite totals; it contains no invocation argv, collected node IDs, four named case outcomes, or named skip reasons. The existing prior `ops_search_logs(query="test_research_catalog_selection.py", source="deploy")` observation was zero matches and is not repeated here. Repository inspection found no CI workflow or JUnit/pytest artifact for this release.

Therefore:

- **Confirmed from source:** four named DB tests exist at the deployed revision and are selected by the full-suite marker rule; full pytest ran and the aggregate gate succeeded.
- **Not established:** PASS/SKIP/XPASS per named DB case. Neither the four unit-test PASS result (`3` Catalog-focused unit tests plus `1` unrelated scheduling-profile test) nor the aggregate `6061 passed` is a substitute for named outcomes.
- **Correction to the prior “four case” ambiguity:** the earlier `4 passed, 19 deselected` result was three Catalog-focused unit tests plus one scheduling-profile unit test. The actual DB-backed suite is the four named async tests listed above. No per-case PASS is asserted for that suite.

The available evidence gap is exact and bounded: the supported deploy log omits the test command/selection receipt and per-node outcome/skip fields; there is no corresponding CI/JUnit artifact in this checkout, and the earlier supported log search returned zero. The minimum useful evidence delta is a normal gate artifact containing full revision, exact pytest argv/marker expression, whether `skip_tests` bypassed the stage (and reason), the four node IDs with terminal outcomes and skip reasons, plus the isolated test-DB identity (name only, no DSN/secret). A code change to the common gate may be needed to emit that receipt, but this branch did not edit it: the backend `main` owner is `fcb55b7d`, and common gate edits must be coordinated there.

## Commands and source/output evidence

Read-only source and git commands used in this follow-up:

```sh
git status --short --branch
git log -1 --format='%H %s'
git show --stat --oneline aa4f89532a8751910f315f483eb20c33ff84acc2
rg -n "catalog_search|method_get" src/ttakkari/api/research.py src/ttakkari/orchestrator/mcp_server.py
sed -n '1550,1678p' src/ttakkari/api/research.py
sed -n '370,405p' src/ttakkari/api/research.py
sed -n '720,825p' src/ttakkari/generation_deployment.py
rg -n '^async def test_|^def test_' tests/test_research_catalog_selection.py
git diff --stat 48eac0c612d7186e8c418fe607a036ea2ab4da09 HEAD -- src/ttakkari/generation_deployment.py tests/test_research_catalog_selection.py tests/conftest.py pyproject.toml
```

Key outputs: starting HEAD was exactly `aa4f89532a8751910f315f483eb20c33ff84acc2`; the deployed revision has four matching test definitions; deployed-vs-checkout diff stat for gate/test/fixture/config paths was empty.

Supported MCP read calls:

```text
ops_list_deploys(limit=20): job 1b79dd88-7bf1-4e01-b3b3-024ba0f8b251, revision 48eac0c6, succeeded.
ops_deploy_log(job=1b79dd88-7bf1-4e01-b3b3-024ba0f8b251, mode=range, offset=1, lines=8):
  lines 1-8 contain a blank line, “added 3 packages in 277ms”, and progress dots; no command line.
ops_deploy_log(job=1b79dd88-7bf1-4e01-b3b3-024ba0f8b251, mode=tail, lines=120):
  lines 9-128 include progress, warnings, DB-reset aggregate, and suite totals quoted above.
```

## Remaining acceptance and branch handoff

Still required to close the original PMS Research target: identify the exact PMS child target and matching pre-existing owner-scoped live Research study/method IDs, then obtain a read receipt tied to those IDs without writing into the unrelated Eval study. Authenticated production UI remains unverified. Per-case DB gate outcomes remain unverified unless a retained normal gate artifact is recovered or the gate owner emits the minimum receipt above. No code, shared-main, deployment, restart, other-Run, or UI-authentication action was taken.

Topic commit and publish state are recorded by the follow-up work-history entry after this evidence file is committed.
