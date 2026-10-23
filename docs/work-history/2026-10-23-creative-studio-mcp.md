# Creative Studio and managed Comfy MCP

## Scope and runtime state

- Work was isolated in /private/tmp/creative-mcp-20261023, branch codex/creative-mcp-20261023, from fetched origin/main acbdc7b1be268d565193a0ad582a30d39d4a2e0d. The canonical checkout had 51 pre-existing staged/unstaged/untracked changes; it was not changed.
- No repository AGENTS.md or CLAUDE.md was found. The mcp-builder skill and its Python server guide were read.
- At implementation time, deploy job ca6d89f6-4153-4bf7-8323-f0808a9247eb identified active release dc2f165e96e21560f73bd230ebd2c2a359d7dcca at /Users/winterholic/.ttakkari/deploy/releases/dc2f165e96e21560f73bd230ebd2c2a359d7dcca, state draining (new traffic already switched). Its .release.json matched. The current MCP bridge was absent from that release.
- A prior unrelated cumulative reservation, 38d046c6-5fc1-4d2d-9b18-6352f67a27db for 5e19deb1dc9da9be7e0c1515c7d9458099ef9b17, was waiting_slot. This is separate from this MCP change.
- Implementation commit d1840eb13f6549f83fa0123ac0b499993166fa19 was fast-forwarded onto main. The tools catalog/read-only contract follow-up is 0f20180d763edf6bbad58e868ae4b55c388a5b42. The final source was pushed to origin/main after the normal pre-push hook passed.
- Deployment reservation e09ef230-651e-48b6-b08c-e07e0d96ecd3 targets 0f20180d763edf6bbad58e868ae4b55c388a5b42, strategy generations, status requested at 2026-10-23T04:04:36Z. Requested is not deployed or active.

## Tools and authorization

The shared ttakkari stdio MCP server now registers:

- Comfy: comfy_profiles, comfy_readiness, comfy_jobs, comfy_submit, comfy_job, comfy_cancel, comfy_reconcile.
- Studio: creative_v2_catalog, creative_v2_capabilities, creative_v2_recipe_matrix, creative_v2_registries, creative_v2_plans, creative_v2_plan, creative_v2_plan_create, creative_v2_plan_edit, creative_v2_plan_approve, creative_v2_production_assets, creative_v2_auto_assets_status, creative_v2_production_dispatch, creative_v2_production_render, creative_v2_production_status, creative_v2_production_media_link.

All agent service routes require the current OrchestratorSession token and a database-backed session check; work-pool sessions are denied. Comfy jobs are owned by the personal agent session, and foreign IDs return not-found. Reconcile still requires controller task, lease, runtime identity and process-ownership evidence. Comfy submit accepts only a registered profile, prompt (1–1000 chars), uint32 seed and stable 8–120 character idempotency key. The same key and arguments return the original task; changed arguments fail. Duplicate starts in the same controller are suppressed. Comfy jobs are paginated, and active tasks return node/step progress when available, receipt output hashes as asset IDs, and only bounded structured failure details (not raw runtime logs or host file paths).

Studio operations call the existing creative_v2 route handlers and PlanStore/production services, preserving strict request models, expected revisions, approval snapshots, render gates, dispatch idempotence, render limits and QA. Asset registration accepts only bounded base64 image/audio input (10 MiB; no local paths or URLs). Download uses the existing plan/media-bound short-lived URL.

Read-only hints match behavior; readiness, production status (which reconciles stored state), writes and execution tools are not marked read-only. New tools are in the Codex approval inventory and MCP scope manifest. The engine still starts the same ttakkari stdio server with the existing session token; tool visibility is generated from its registry.

## Existing capability limits

- Backend main contains the managed wan22-5b-draft Comfy profile and the v2 plan → approved dispatch → FFmpeg render → QA/receipt path.
- comfy_readiness runs only a fixed, 8-second, no-model-load Torch device check using the configured Comfy Python. Results identify source=backend_host, exact path and timestamp. It has not been called against the host from this Run.
- Automatic scene image/TTS/music generation remains unavailable. Its implementation is only on unmerged backend feature feat/creative-v2-auto-assets-main-20261023 (b51b3d3b79e551b4905b784b797a1357451e461b) and frontend feature feat/creative-v2-auto-assets-20261023 (1d8395fcce5e6bc88b3166257758bb834a4037f8). They were not merged or copied into this MCP change. creative_v2_capabilities and creative_v2_auto_assets_status explain this.
- Files-library sharing is unavailable because the existing production service only issues a short-lived media download URL; no production-to-library registration service exists. creative_v2_capabilities states this.
- No backend-host MCP calls, Comfy generation, real image/audio, MP4, or production media QA were performed in this Run.

## Verification

- Test-first red: the initial MCP registration/owner-filter tests failed because the tool and ownership paths did not exist.
- Focused Comfy + v2 service regressions: 207 passed in 96.18s.
- Follow-up after pagination, progress, tool classification and service-bridge checks: tests/test_creative_mcp_tools.py unit_tests/test_comfy_managed.py unit_tests/test_comfy_managed_api.py unit_tests/test_managed_comfy_receipt.py → 43 passed in 1.31s.
- MCP/route/role authorization inventory: tests/test_route_inventory.py tests/test_orchestrator.py::test_auto_approval_policy_covers_registered_tools tests/test_orchestrator.py::test_mcp_server_tools_by_role → 9 passed in 2.62s.
- Ruff, changed-module compileall, and git diff --check passed.
- Final normal pre-push gate on 0f20180: Ruff passed; pytest -m contract → 78 passed, 4277 deselected; changed test files → 76 passed. This is the repository's impacted gate, not the full backend suite.
- MCP list_tools and call_tool were exercised with the scoped service transport mocked. API bridge tests verify personal-session rejection, session-bound Comfy submit/idempotency, plan approval revision forwarding, and no-device readiness reported as not-ready. These are not live deployed MCP/API call evidence.

## Next-run verification order

After this revision is deployed, start a new personal Run so its existing ttakkari MCP process loads the new registry. Then:

1. creative_v2_catalog → creative_v2_recipe_matrix → creative_v2_capabilities and creative_v2_auto_assets_status.
2. comfy_profiles → comfy_readiness; continue only if this backend-host measurement reports ready=true and current resource preflight is acceptable.
3. creative_v2_plans/creative_v2_plan_create → inspect with creative_v2_plan → creative_v2_plan_edit if needed → creative_v2_plan_approve for the exact returned revision.
4. Until the generator feature is reviewed, merged and deployed, register only real existing inputs with creative_v2_production_assets. Then creative_v2_production_dispatch → creative_v2_production_render → creative_v2_production_status; require qa_passed and a final media ID before creative_v2_production_media_link.
5. For direct Comfy, comfy_submit (stable idempotency key) → comfy_job/comfy_jobs → comfy_cancel if requested, or comfy_reconcile only when safely indicated. Direct Comfy output is not automatically registered as a Studio scene asset.

Actual MCP activation, host readiness, automatic asset generation, TTS/music generation, successful MP4/QA receipt, and Files sharing remain separate unverified gates.
