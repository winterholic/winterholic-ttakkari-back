# AI Creative Studio — first working slice

2026-10-20. User authorized implementation, testing, a real 30-second video and a simple studio UI. PMS project: `a92035b0-65e0-41b0-86ae-b30020e85333`. Production deployment is a separate gate; local results do not establish production operation.

## Implemented boundary

Authenticated `/api/creative/projects` APIs and `/creative` frontend routes support project creation, editable brief/style, immutable image/audio/video imports, scene editing, captions, camera motion, fade/cut transitions, a derived explicit timeline, local procedural sample assets, durable serial rendering, previous output comparison and MP4 playback/download. Final output is H.264/AAC, 24 fps, 720p landscape/portrait/square. Maximum project length is 240 seconds, 60 scenes, 60 seconds per scene. Uploads are capped at 100 MiB and 15 minutes.

The built-in graphics and music buttons are explicitly **procedural**, not AI model generation. The actual AI image example was generated through the available agent image tool and imported through the same asset API. Automatic LLM planning, Korean vocal generation, lyric alignment and neural video generation are not implemented in this slice.

## Domain and storage

`creative/service.py` owns a single-user manifest store at `Settings.home/creative-studio/<project UUID>`. `ProjectInput` holds intention/type/style/aspect/length, immutable `assets/` holds assets, `Scene` holds prompt/caption/duration/asset/camera/transition, and `timeline` stores start/end intervals for visual, music and caption tracks. Generation jobs record generator/prompt/settings/references/output/time. Render jobs preserve an immutable input snapshot, cache key, state/progress/error/timing/RSS/output and per-scene cache hits. IDs join every object. Scene intervals are derived from ordered durations, not separately editable competing clocks.

For the initial single-Mac slice, atomic JSON manifests avoid adding six SQL tables and a contested migration number. `os.replace` + file fsync and per-project `flock` guard writes; a revision check rejects stale edits. Media bytes never go into the manifest. This is not a multi-host store. Database-backed paging, retention and Artifact-library linking are follow-ups when project count grows. Existing Artifact records have their own retention lifecycle; source assets here must remain available for later scene edits. Existing auth, short-lived download tokens, range-enabled FileResponse and app home conventions are reused.

## Rendering and ownership

`creative/render.py::scan` is registered in the **existing** generation maintenance loop as `creative-render`; legacy mode uses a cancellable local loop. Generation work ownership keeps active work in the generation drain calculation. A cross-process renderer lock serializes all projects and all generations. The lock FD is inherited by the FFmpeg child, so a dead parent cannot immediately start a competing child. Cancellation terminates and waits for its child. Recovery marks interrupted running jobs as failed, preserving evidence; retry creates a new attempt. At most three active/pending renders per project.

Each scene is independently hashed and cached. Final mux copies the already encoded video and encodes audio once. Scenes fade within their assigned intervals (dip through black, not an overlapping cross dissolve), so the music clock remains unchanged. A final input snapshot and SRT are saved beside every successful render. A final media check requires audio/video and a duration error under 150 ms. Human quality review remains separate.

No local neural model loads at web startup. FFmpeg runs on demand, two codec threads, serial filter processing and bounded timeouts. Asset imports reject unsupported formats; image metadata is stripped and resolution bounded; audio/video demuxers and protocols are restricted. No arbitrary path or URL input is accepted. Signed media links bind project, kind and object ID; cross-project use is rejected.

## Measured examples

- `little-signal-30s.mp4`: 30.00 s, three separately generated AI images, original procedural 120 BPM instrumental, three Korean caption intervals, zoom/pan/zoom-out, fades. Render 4.463 s, measured FFmpeg-child peak RSS 656,719,872 bytes. Full decode exit 0, 720 frames. Browser playback reached 30 s with no media error. These are **isolated local test** measurements, not host GPU or production generation measurements.
- Browser-created sample: first render 3.597 s; changing the first caption then rendering took 1.489 s and reused two scene encodes. 390 px viewport/document widths matched; no page overflow. 1440 px desktop layout was also captured.
- `facebook/musicgen-small`: CPU, 2 threads, 150 new tokens, 2.94 s audio. Download+load 52.508 s, total 59.571 s, peak process RSS 3,198,304,256 bytes. Process exited normally, releasing its model. Weights are CC-BY-NC-4.0; this test is **not used in the demo video** and does not validate Korean lyrics or commercial music quality.
- Existing faster-whisper-large-v3: CPU int8, 2 threads, existing 3.09 GB model. Loaded and processed that instrumental test in 5.424 s, peak process RSS 2,865,135,616 bytes; VAD returned no speech. This only verifies the existing runtime/model can run. Korean singing recognition/alignment is unverified.

Evidence and downloadable files: `/Users/winterholic/.ttakkari/data/outbox/609f309e-7994-4e71-8387-aec2551f3178`. Browser checks used the configured Ttakkari browser against an isolated local fixture, with the real studio API and components; login/session/MCP resume/production deployment are distinct checks.

## Reproduction

Backend: `uv sync --dev`, then `python -m pytest tests/test_creative_api.py unit_tests/test_creative_studio.py tests/test_generations.py unit_tests/test_generation_hardening.py -q`.

UI fixture: set `TTAKKARI_NO_ENV_FILE=1` and `TTAKKARI_HOME=<worktree>/.creative-test-home`, run `python -m uvicorn scripts.creative_smoke_server:app --host 127.0.0.1 --port 8897`; frontend `TTAKKARI_NO_ENV_FILE=1 VITE_API_BASE=http://127.0.0.1:8897 npm run dev -- --host 127.0.0.1 --port 5187`, open `/creative-smoke.html`. The fixture exposes a DEV-only token and refuses production/default homes. It is never mounted by the production app. Production Vite builds only `index.html`.

## Next gates

1. Reserve clean committed backend through the deployment controller; verify the deployed SHA, readiness, API, persisted assets and render recovery after cutover in a later turn. Do not restart services from a Run or poll a reservation in an active Run.
2. ACE-Step 1.5 isolated host-runtime adapter and Korean 30 s vocal comparison, with exact license/checkpoint, load/unload, elapsed time and memory evidence. Keep CPU/MPS measurements distinct. No paid API or new subscription without user approval.
3. Existing LLM Run planning as a structured editable plan; stage inputs and revisions must remain independently rerunnable. Do not feed asset prompts to unrestricted shell commands.
4. Non-LLM beat/silence analysis, explicit subtitle intervals and manual lyric correction; singing alignment needs annotated Korean references.
5. Single-scene neural video experiment (LTX/Wan only if measured viable); keep static-camera paths for the rest. Character reference/seed/prompt versions and defects are recorded per output.
6. A complete 2–4 minute production, with pacing/music/subtitle/character review, mobile playback and deployment-restart validation. This has not been produced yet.
