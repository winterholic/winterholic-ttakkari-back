# Qwen3.5-9B Metal failure context for 35B diagnostic

Prepared 2026-10-15 KST from existing experiment records and retained files. No model was loaded or run for this handoff. The `--version`, SHA-256, `otool -L`, and file timestamp checks below are read-only identification checks against the retained candidate binary; exact failure-time process identity is not preserved.

## Findings to compare with the Foundation/Metal probes

- The Qwen3.5-9B record reports llama.cpp **b11655** and the error text `failed to create command queue`. That code path was reported after a default Metal device had been obtained. This is a different failure stage from the 35B worker's Foundation+Metal and Foundation+Metal+CoreGraphics probes, which both returned device count 0 and default device nil. Do not infer a shared cause.
- The retained candidate binary is `/private/tmp/qwen35-router/llama/llama-b11655/llama-server`, modified 2026-10-15 21:33:49 KST. Its read-only version output is:

  ```text
  version: 0.6.0-dev (build 11655, commit 47f5a64ae)
  built with AppleClang 21.0.0.21000101 for Darwin arm64
  ```

- Retained candidate `llama-server` SHA-256: `1bf5602a2be50097c44282a6ecc63234d94ee8f96d39fd45fed6eff4e74b6b41`.
- Its `otool -L` output includes `@rpath/libggml-metal.0.dylib` (current version 0.26.0). The retained `libggml-metal.0.26.0.dylib` links Foundation, Metal, MetalKit, CoreFoundation, Objective-C runtime, and libc++; it does not directly list CoreGraphics. This describes the retained candidate files, not proof of the loaded failure-time images.
- Candidate Metal dylib SHA-256: `4713a856caa7dcff47cd14ab89e84355a39ba5201550940380081c4d1901e8da`.

## Qwen run and sample integrity

- Project records identify model `lmstudio-community/Qwen3.5-9B-GGUF`, Q4_K_M, revision `d9006465af6fc714af653e381fbfa55ead5af84b`. Retained GGUF path: `/private/tmp/qwen35-router/model/Qwen3.5-9B-Q4_K_M.gguf`; SHA-256 `cd76ec205963b3b33350093e6904d9de16c4e666fd104e1f632d25c7f15f2a13`; file timestamp 2026-10-15 22:51:38 KST.
- `experiments/qwen35-router/replay.py` only POSTs to `http://127.0.0.1:18786`; it does not launch or stop `llama-server`. The server launch wrapper, complete argv, parent/Run process context, and raw stderr for the command-queue failure were not retained in the inspected work-history, measurement JSON, replay code, or pilot results.
- The measured post-prompt-tuning pilot is a distinct **CPU-only synthetic pilot**: 3 cases × 3 serial repeats, 9/9 schema-valid and label-correct, pass³ 3/3, p50 5.9242 s, p95 10.5138 s. Retained host snapshot: available memory 36,021,878,784 bytes, pressure normal; process RSS snapshot 10,512,285,696 bytes (not peak). Per-sample CPU, a contemporaneous process list, and exact pilot start/end time are absent, so contention cannot be ruled in or out.
- Around the same broader run window, the 35B work log records an owner-unidentified `llama-server` at CPU 199.8% and RSS about 10.5 GB. This matches the Qwen pilot's RSS scale but the 35B log lacks PID, argv, model path, port, and log path; it does **not** prove it was the Qwen process or that any particular latency sample overlapped. Treat pilot latency as contention status **unknown**, preserve the observations, and do not adjust or discard samples without process-level evidence.

## Raw probe provenance

The 35B diagnostic-only probe report is `/Users/winterholic/.ttakkari/data/outbox/93416d08-9532-4504-9733-3ade03d7c6f0/metal-probe-log-2026-10-15.txt`. It records 2026-10-15 KST on Mac17,14 / M5 Max / macOS 27.0 build 26A428: both Foundation+Metal and Foundation+Metal+CoreGraphics returned `MTLCopyAllDevices.count=0` and `MTLCreateSystemDefaultDevice=nil` (exit 20). No model was loaded and no command buffers were submitted. That report also notes the later 23:08:36 KST lock owner `c6ab567a-4140-4e6e-bf99-d1ca486d4d27`; that lock belonged to that worker and was not touched here.

## Sources inspected

- `experiments/qwen35-router/measurement-summary.json`
- `experiments/qwen35-router/pilot-results.json`
- `experiments/qwen35-router/replay.py`
- `docs/work-history/2026-10-15-qwen35-router.md`
- `docs/work-history/2026-10-15-inference-lock-coordination.md`
- Retained binary and model files under `/private/tmp/qwen35-router/`
- The 35B raw probe report linked above

## Delivery status

The callable Ttakkari tool list in this run does not expose `message_worker` or another worker-session send operation. This file is the shared-repository handoff note; direct delivery to session `d6768faa-766c-4f8a-a580-e0f8fde7e938` is not confirmed. No other worker's lock, process, service, or environment was changed.
