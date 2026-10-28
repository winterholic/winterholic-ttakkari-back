# Collection design-system rights correction

## Scope and result

- Worktree: `back-collection-fix-20261029`, branch `fix/collection-search-challenge-20261029`.
- Correction code commit: `2e03b33e928dc8e00e847c66d8941460cbe74046` (parent `34daa5091a8405b659265b085ea41824f702ebe2`).
- Removed the `reconcile_mission()` trigger that auto-registered design-system grants based on goal wording or a seed hostname, and removed the grant helper that created host-wide permissions for the Design Tokens preview host and USWDS host.
- The normal resolver remains the only path for website seeds: an existing active server-side policy must allow `store` and its host/path check must accept the URL. Unknown/unregistered seeds remain `source_policy_required` gaps. Existing per-file GitHub license registration and the independent Python-docs registration path were not changed.
- No source policy was written to a runtime database; no mission was retried. This does not authorize the Design Tokens or USWDS sites.

## Source and policy evidence

Official pages were opened via the web evidence tool at approximately `2026-10-28T23:10:14Z` (UTC):

- The old seed `https://design-tokens.github.io/community-group/format/` redirected to `https://www.designtokens.org/tr/drafts/format/`. The page identifies itself as a preview and says not to treat it as authoritative or implement it. The published stable report is `https://www.designtokens.org/TR/2025.10/format/`, dated 2025-10-28; it says it is not a W3C Standard and names the Community Final Specification Agreement (FSA). Sources: [preview](https://design-tokens.github.io/community-group/format/), [stable report](https://www.designtokens.org/TR/2025.10/format/).
- The Design Tokens repository `LICENSE.md` distinguishes reports licensed under the W3C Software and Document License from specification contributions made under the W3C CLA. The W3C 2015 license requires a NOTICE and pre-existing notices/terms on redistributed copies. The stable report separately points to the FSA. The collector's acquisition → knowledge/export paths have no verified preservation contract for the required notices, so this run leaves the stable specification unauthorized/unknown instead of mapping the generic 2015 license to a blanket grant. Sources: [repository LICENSE](https://github.com/design-tokens/community-group/blob/main/LICENSE.md), [W3C Software and Document License](https://www.w3.org/copyright/software-license-2015/), [FSA](https://www.w3.org/community/about/process/fsa-deed/).
- USWDS's official contribution page says some parts are outside the public domain and directs readers to its LICENSE for their complete attribution/licensing; it describes the rest as public domain/CC0. A host-wide policy has no path/content classifier for those exceptions. Therefore even though the repository license includes CC0 language, this does not establish a whole-host grant. Sources: [USWDS licensing and attribution](https://designsystem.digital.gov/about/contribute/), [USWDS site LICENSE](https://github.com/uswds/uswds-site/blob/main/LICENSE.md).

These are source observations, not a legal determination. Robots permission was not treated as a rights grant. No robots lookup or content-fetch bypass was performed. The earlier hard-coded `checked_at=2026-10-29T00:00:00Z` was later than the independent verifier's 2026-10-28 23:03 UTC observation; that fabricated future timestamp was removed with the unused automatic grant path.

## Regression and verification

The new tests call the real `reconcile_mission()` function with a fake DB boundary. Before the code change they failed on the actual registration calls:

```text
$ PYTHONPATH=src .../python -m pytest -q unit_tests/test_collection_mission_policy_boundary.py --tb=short
FAILED ... assert not grants  # W3C/USWDS host-wide registrations occurred
FAILED ... assert len(registered) == len(enqueued) == 1  # expected 1, observed 3 registrations
```

After the change:

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_collection_mission_policy_boundary.py --tb=short
..                                                                       [100%]
2 passed in 0.32s
```

The two regressions establish that an unregistered design-system URL creates no source grant and remains a `source_policy_required` gap while the prior search-challenge gap remains visible; an already active resolver policy can still create exactly one mission source and enqueue it while an unrelated unregistered URL stays a gap. The fake DB proves call-boundary behavior only, not PostgreSQL transactions, fencing, or live source reservations.

Additional checks:

```text
$ .../ruff check src/ttakkari/collector/missions.py unit_tests/test_collection_mission_policy_boundary.py unit_tests/test_collection_research.py
All checks passed!
$ git diff --check
(exit 0; no output)
```

The existing focused check for challenge classification and `jobs.finish`/`recover_orphans` cause preservation was already run by the independent verifier against this base and reported passed using function-level mocks. It was not rerun here because this correction does not touch those functions. PostgreSQL is reserved for normal CI/independent deployment gate; DB fencing and mission run sequence checks remain unverified here.

## Runtime follow-up

After this branch is independently reverified, integrated by the designated owner, and deployed, the coordinator should perform exactly one retry of mission `d0183376-fc0a-4720-a436-955c322bc10a` with `expected_run_seq=3`. Verify acquired items, actual KB imports, searchable results, and the retained `source_policy_required`/search-challenge coverage gaps from runtime state. No retry is performed by this correction owner. Full recovery is still open until that readback shows real acquisition, import, and search.
