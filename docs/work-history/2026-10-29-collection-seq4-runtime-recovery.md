# Collection mission seq4 runtime recovery

## Scope

Continue the user's direct request to use the original design-system collection prompt, keep testing the collector, correct confirmed defects, and verify real acquisition, import, and search. This is a runtime and ownership handoff; it does not claim the mission is recovered.

## Current supported runtime evidence

Read-only `host_operation_start(kind="runtime_report")` returned job `bcf41156-b26b-48ca-85bb-191af62e6c0e`, status `succeeded`, at 2026-10-28 23:33:39 UTC:

- API release `c04c5a37a6750f9fafe7641a62afba4244d499b8`, generation `89f8ccbc-e9de-493d-9191-91307220d85b`, is active.
- `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809` is draining with two leases and two process groups. Keep their owner intact.
- The design-system mission `d0183376-fc0a-4720-a436-955c322bc10a` is at `run_seq=4`, `status=failed`; its only listed job `f30dfb8e-757d-40b5-a1a6-5d40f050b210` failed in `mission_prepare` after three attempts with `CollectorContractError` / `attempts_exhausted`.
- Runtime counts are all zero for discovered/acquired/accepted/imported. `source_count=21`; 11 listed source gaps require policy; the recorded search challenge is still `retryable=true`.
- No active acquisition reservation is listed for this mission. The status API does not expose policy rows or policy reservation details.

Read-only `ops_search_logs` queries for the mission ID, the failed job ID, and `CollectorContractError` each returned `total=0`. The supported mission status exposes neither the failing exception message nor retry caller/audit actor. The previous sequence-3 job was `e6a61468-778b-499f-bfd3-99076f29180b` per the original 8d task prompt. Mission `updated_at` is `2026-10-28T23:11:17.198987+00:00`; the original worker was tasked to make one guarded sequence-3 retry after correction, so it is plausible that its run created seq4. The caller and exact cause of this transition are **not proven by the supported read-only audit surface**.

The `read_session` record for worker `8d091c15-bf64-475a-8ad3-bb2aa84d9038` shows its correction report completed at 23:31 UTC. Its subsequent `e9b2e21d-1777-42c1-80b3-d10dd8a0ce72` and `a3085f21-e4ca-4e83-43c1-c293961bf205` follow-ups both failed before a model response with `Model "auto" does not support reasoning effort configuration (requested: "high")`. These failures do not establish execution of their prompts. Direct `get_worker` access returned `404 ... 이 작업 세션의 하위 세션이 아닙니다`; no direct worker retry or cancellation followed.

## Candidate and owner boundary

- Candidate branch `fix/collection-search-challenge-20261029` is remotely published at exact SHA `cb9e8f0d55168f6413a628c084b9c5c9c7341607` (verified with `git ls-remote`). Its code correction is `f22b97181b45303d597f01528027df146b17bda2`; it includes rights-boundary correction `2e03b33e928dc8e00e847c66d8941460cbe74046` as an ancestor.
- New delta on the candidate covers isolation of source-registration contract errors, advancing past repeated failed search-provider queries to an alternate paper query, and two focused unit tests. A fake alternate-provider candidate is test behavior, not a real paper acquisition.
- Independent verifier `7e68cf61-fc1e-44f4-88ac-3c0fd62d5eb4` was observed running at 2026-10-28 23:35 UTC against this exact new delta, reusing the earlier fixed-checkout evidence. Do not duplicate its check; consume its final result before integration.
- The candidate and original worker worktrees were left untouched. This record is on a separate worktree/branch `back-collection-continuation-20261029` / `fix/collection-continuation-20261029`.
- The single main/deploy owner remains `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`. Do not push main or create a deployment reservation from this task.

## Rights evidence checked in this continuation

Official W3C WAI guidance says most WAI materials can be copied with attribution, URL, copyright, and status, but must not be modified unless a resource has another explicit license. It identifies W3C Document License as the default absent a different license: <https://www.w3.org/WAI/about/using-wai-material/>.

The official `www.w3.org/robots.txt` was read. It disallows specific areas including WAI drafts, WAI test paths, and query paths; robots instructions are not a copyright or license grant: <https://www.w3.org/robots.txt>.

USWDS's official repository README describes public-domain/open-source content, but also directs readers to its LICENSE for attribution and notes that some parts are not public domain. The LICENSE enumerates third-party assets and exceptions: <https://github.com/uswds/uswds>, <https://github.com/uswds/uswds/blob/main/LICENSE.md>.

The `designsystem.digital.gov/robots.txt` endpoint was inaccessible through the web evidence tool. No alternate access path or scraping workaround was attempted. These observations do not authorize a host-wide policy. The exposed Ttakkari MCP tools have mission status/retry and runtime reports, but no source-policy registry read/write tool. No source policy was written. Continue with existing active, host/path-scoped `store` policies only; leave unregistered sources as gaps.

## Next steps

1. Consume verifier `7e`'s exact-CB9 result. If it identifies a reproducible code defect, fix it in an isolated branch with a regression test; do not repeat its PostgreSQL `initdb` denial or route around the sandbox.
2. Hand the verified candidate SHA and this runtime evidence to the single integration/deploy owner. Wait for the normal gate and deployment status; the prior requested `e7e9ae2b-639b-42d1-b3b4-9d44c931957b` is for revision `7d34ede60234804d41f57f0ea207edaf4b24e879`, so it does not establish that CB9 is deployed.
3. Read the deployed revision and source-policy/reservation state through supported tools. The current API does not expose policy rows, so treat policy availability as unverified unless a supported read is added by the owner.
4. After the correction is deployed and no active mission job/reservation exists, read the then-current `run_seq` and issue exactly one `collection_mission_retry` with that value. Do not use stale expected sequence 3.
5. Verify acquisition, knowledge import, search, and remaining source/challenge gaps separately. The mission is not complete until those runtime values are real and the required policy/coverage gaps remain visible.

## Independent CB9 verdict and confirmed follow-up corrections

The independent verifier's scoped verdict for candidate `cb9e8f0d55168f6413a628c084b9c5c9c7341607` was **FAILED as an integration/deployment gate**. It verified the two new candidate tests (`2 passed`) and the no-grant/active-policy boundary tests (`2 passed`) with exact import/blob provenance. Its synthetic provider trace preserved failures `[0, 1, 2]` and search budget, and confirmed no unbounded retry. It did not use real network or PostgreSQL.

The verifier identified two concrete code defects in the candidate:

1. When fallback query index 4 ran before unvisited query index 3, `search_cursor` remained at 3; after query 3 completed, index 4 ran again. The skip itself was not falsely recorded complete, and budget bounded the loop, but the fallback duplicated and consumed a search slot.
2. The registration and enqueue error gaps retained only reason/message; they discarded exception class and explicit code. A synthetic exception's explicit `retryable` attribute was also lost. This does not prove every production contract error has a `retryable` field.

On isolated branch `fix/collection-continuation-20261029`, added regression tests before production edits. RED runs:

```text
test_advanced_alternate_query_is_not_repeated_after_cursor_catches_up
1 failed: actual calls contained an extra `UI UX research paper arxiv`

test_reconcile_mission_isolates_register_source_contract_error
test_reconcile_mission_preserves_enqueue_error_classification
2 failed: KeyError: 'error_type'
```

The correction adds per-run `consumed_indices` so an ahead-of-cursor query is not re-run, while skipped lower queries remain pending and retryable failures still take precedence. It also stores `error_type`, explicit `error_code` (preferring `.code`, then `.error_code`), and `retryable` only when the caught exception supplies a boolean. It does not invent retry semantics for `CollectorContractError`, whose actual class has `error_code="invalid_envelope"` and no `retryable` attribute.

GREEN and regression output:

```text
$ env -u TTAKKARI_TEST_DB -u TTAKKARI_DATABASE_URL -u DATABASE_URL PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_collection_design_system_flow.py unit_tests/test_collection_mission_policy_boundary.py unit_tests/test_collection_research.py --tb=short
.............................. [100%]
30 passed in 0.42s

$ /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/collector/research_rounds.py src/ttakkari/collector/missions.py unit_tests/test_collection_design_system_flow.py
All checks passed!

$ git diff --check
(exit 0; no output)
```

These are unit/fake-provider/fake-DB checks. PostgreSQL transaction/fencing, the exact active source policies, actual public-source fetches, and all real acquisition/import/search remain unverified. The verifier's earlier `initdb` denial was `shmget(...): Operation not permitted` in its `exec_command` environment before test collection; do not retry that target or treat it as a host-wide failure. No mission retry, policy write, main push, or deployment reservation was performed by this continuation.

The isolated implementation and these corrections were committed and pushed as **`58a1617a916a15987e0db7c673b29399ab5f1fbc`** on `fix/collection-continuation-20261029`. `git ls-remote` returned the same SHA; `git show --check` and `git status` showed a clean worktree. This commit is the new code candidate; independent verification of this follow-up code and normal integration/deploy gates remain with the coordinator/owner.

## Latest runtime/deployment recheck

At 2026-10-28 23:39:58 UTC, supported runtime job `7dbafb9e-683f-424b-a7dd-698f83a5c6f6` again reported active release `c04c5a37a6750f9fafe7641a62afba4244d499b8` in generation `89f8ccbc-e9de-493d-9191-91307220d85b`, with `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809` still draining with two leases. The mission remained `run_seq=4`, failed, all acquisition/import counts 0, with the same failed `mission_prepare` job and `search_provider_challenge retryable=true`. No retry occurred.


## Read-only observation API delta

The requested support-path audit found no deployed route or MCP tool that reads the active owner source-policy row/path match, cached robots status, or the mission's current linked-job reservation state together. Existing `collection_mission_status` returns bounded mission counts, gaps, and an exception type/code; `runtime_report` returns generation/lease totals and mission summaries, not per-mission policy/reservation details. `ops_search_logs` returned zero for mission ID / job ID / `CollectorContractError`, so it did not recover a more specific failure message or retry actor.

Read-only runtime evidence at 2026-10-29 08:41 KST still showed API revision `c04c5a37a6750f9fafe7641a62afba4244d499b8` active in generation `89f8ccbc-e9de-493d-9191-91307220d85b`; `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809` was draining with two leases. Mission `d0183376-fc0a-4720-a436-955c322bc10a` remained `run_seq=4`, failed, acquired/imported 0; its final stored classification was `attempts_exhausted` / `CollectorContractError` after three attempts. This status view did not reveal source-policy rows, cached robots, or a caller for the failed run. Deployment reservation `1ec180ed-987a-42c3-95a0-1918917bbcd1` for `6a5589c5e08de35d66aed9b53f5cd6b6245fc756` was `testing`; that revision is docs-only and excludes both collection candidates.

To close only this observation gap, added a separate branch `fix/collection-observation-20261029`, based on `origin/main` at `6a5589c5e08de35d66aed9b53f5cd6b6245fc756`; it does not move or edit `fix/collection-continuation-20261029` at `75c6a4588312d35401af035f6efe19176ba56ce9`. Code commit: `cc669e58` (`feat(collection): expose scoped recovery diagnostics`). The existing owner-authenticated mission status route now returns, for each mission source path, the matched active owner/library policy version and bounded rights/path fields, cached robots status/timestamps without body, and current-run job active counts/status. If no policy allows the path, the response lists up to 10 active policies for the same host or exact seed and their stable rejection reason (for example `path_excluded_by_policy`), retaining each policy's allowed/excluded paths. Job errors expose only bounded explicit classification fields (including final exception type/code/cause where stored). URL credentials, query, fragments, raw exception messages, robots bodies, user goal/context, and policy notes are excluded. Policy selection reuses the collector's existing active-policy, permission, host/path check; queries are limited to this owner tenant/library scope. No policy was changed, no new acquisition permission was granted, and the API performs reads only.

Validation:

```text
RED: new unit test first failed collection because the diagnostic helpers were absent; the path-denial case then failed because mismatched policy rows were hidden (`KeyError: candidates`).
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .../python -m pytest -q unit_tests/test_collection_mission_diagnostics.py --tb=short
4 passed in 0.52s
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .../python -m pytest -q tests/test_route_inventory.py -k route_inventory_matches_manifest --tb=short
1 passed, 6 deselected in 0.72s
ruff check [changed Python files]
All checks passed!
git diff --check
exit 0
```

These tests cover read-only policy matching/formatting and response filtering with fake DB objects, not live PostgreSQL or the running API. The prior verifier's PostgreSQL `initdb` failure (`shmget: Operation not permitted`) remains unverified; this work did not retry that target or use another engine. The deployed `c04c5a37` therefore still lacks this new observation view until the single integration/deploy owner (`fcb55b7d-a9ed-4160-a395-84fe291ab9ad`) accepts the separate branch and normal gates pass. Do not rerun the old `75c6` independent check or retry the mission here. After the observation delta is normally deployed, read the policy/cache/current-job status through the endpoint before the owner performs any guarded retry.
