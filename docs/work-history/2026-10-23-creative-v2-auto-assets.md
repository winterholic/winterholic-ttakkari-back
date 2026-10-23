# Creative Studio v2 — automatic asset generation

## 2026-10-23 12:07 KST — integration and real-generation attempt

### User request

For an approved v2 plan, automatically create scene imagery, per-scene voice, and music using existing local asset paths, feed them into v2 dispatch, QA the rendered media, and record receipts in `production.json`. At least one real R21 recipe must reach a shared video artifact. No paid API and no silent slideshow fallback.

### Branch isolation

- Active G34 engine work is in `/private/tmp/back-g34` (`feat/creative-engines-director`) and `/private/tmp/back-g34-integ` (`integ/g34-creative-engines-main-20261023`). The candidate is not main and had no push/deploy evidence. No files in G34's `director.py`, `pipeline.py`, or `managed_comfy.py` were edited.
- Backend task worktree: `/private/tmp/creative-v2-auto-assets-main`, branch `feat/creative-v2-auto-assets-main-20261023`, based on current `origin/main` `ad58203c1dbc7265452d10aa523171a6c664158b` after rebase.
- Frontend task worktree: `/private/tmp/creative-v2-auto-assets-front`, branch `feat/creative-v2-auto-assets-20261023`, based on current `origin/main` `d7a63d2094ae0818e423c1d55ca5d14ee97fdbd4` after rebase.
- Changes are uncommitted. Nothing was pushed and no backend deployment was requested.

### Implemented locally

- Authenticated async endpoint `POST /api/creative/v2/plans/{pid}/production/generate-assets` records progress and prevents concurrent duplicate task submission in one process.
- Generic asset-generation service requires the currently approved, renderable revision and a complete receipt; it registers all planned scene images, each scene voice, and the final music/voice mix, or persists an explicit failure.
- Managed generator adapter connects v2 to the existing managed Comfy Wan 2.2 draft profile, local Supertonic speech, local ACE-Step music, and FFmpeg mix. It currently supports only R21/16:9. Other recipes receive explicit blockers.
- UI adds the generation control and task progress/failure state; the existing dispatch remains the next step after asset registration.
- No edits to the active G34 implementation files. Added a preflight check of the exact Comfy Python interpreter so a no-device environment is blocked before task submission.

### Actual execution evidence and limitation

- Isolated plan: R21 (`C08.three_act` + `C10.dissolve`), three narrated scenes, approved revision 1 under `TTAKKARI_HOME=/private/tmp/creative-v2-asset-e2e-home`.
- First harness invocation was corrected after `/private/tmp/numbers.py` shadowed stdlib `numbers`. Correct command:

  ```sh
  PYTHONPATH=src TTAKKARI_HOME=/private/tmp/creative-v2-asset-e2e-home /Users/winterholic/development/projects/ttakkari-studio-v2-back/.venv/bin/python -c 'exec(compile(open("/private/tmp/creative-v2-asset-e2e.py").read(), "creative-v2-asset-e2e.py", "exec"))'
  ```

- Real managed Comfy task `2ac86aa4a8f24b75b0eea21dcfb8601b` ended `needs_reconcile` before producing its first scene output. The isolated `production.json` records the generation as `failed`; scene/voice/music asset maps remain empty. No render, QA, final MP4, or media share exists.
- Runtime log `/private/tmp/creative-v2-asset-e2e-home/data/managed-comfy/runtime.log` shows Comfy Darwin startup, then `torch.cuda.current_device()` failed with `AssertionError("Torch not compiled with CUDA enabled")` in `comfy/model_management.py:213`.
- The exact Comfy venv Python probe run in this Codex command environment returned `{'cuda_available': False, 'mps_available': False}`. `ManagedAssetGenerator.blockers()` now reports `Comfy PyTorch에서 CUDA 또는 MPS 장치를 사용할 수 없습니다. 현재 실행 환경에서 생성 작업을 시작할 수 없습니다.` before submitting another task.
- Ttakkari host resource snapshot simultaneously reported Apple M5 Max GPU available, normal memory pressure and ~31.3 GB available RAM, but 94.6% swap use. This is separate host telemetry; it does not override the exact child-Python device probe. Codex's measured `gpu_metal=blocked` is scoped to this nested engine. No CPU retry, second Comfy task, or paid image API was attempted. The host GPU tool offers a llama-server text model and is not an image-generation substitute.
- The first task lease record lives only under the isolated temp home. Process inspection (`ps -p 50413 ...`) was denied in the nested Codex sandbox, so no manual reconcile was attempted. Do not generalize that process-inspection denial to the Mac host.

### Verification

- Test-first regression: the new no-device blocker test first failed because `_probe_comfy_torch` did not exist, then passed after implementation.
- Backend related tests on the rebased task tree: `PYTHONPATH=src:unit_tests /Users/winterholic/development/projects/ttakkari-studio-v2-back/.venv/bin/python -m pytest -q unit_tests/test_creative_v2_asset_generation.py unit_tests/test_creative_v2_dispatch.py tests/test_route_inventory.py` → `32 passed in 94.52s`.
- Backend Ruff, compileall, and `git diff --check` passed after the preflight change.
- Frontend full Vitest after rebase: `140 passed | 2 skipped` files; `1344 passed | 10 skipped` tests.
- Frontend lint passed; UI lint remains at baseline 138 violations with no new violations.
- Frontend `npm run build` passed (`tsc -b && vite build`); existing large-chunk warning remains.

### Remaining acceptance gaps

1. Real Comfy scene output / actual image asset.
2. Real Supertonic speech execution and ACE-Step music execution (not reached in the failed first scene stage).
3. v2 dispatch, final MP4, independent media QA, and `production.json` success receipt for a real generated plan.
4. Frontend authenticated browser execution.
5. Final backend test result and independent validation of the actual Comfy/MPS runtime outside the nested Codex GPU sandbox.

Until the real media E2E and required checks pass, do not push or request deployment.
