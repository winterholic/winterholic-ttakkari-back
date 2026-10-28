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
