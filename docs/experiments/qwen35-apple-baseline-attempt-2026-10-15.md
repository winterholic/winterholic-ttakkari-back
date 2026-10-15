# Qwen3.5-35B-A3B Apple Silicon baseline attempt

Date: 2026-10-15 (Asia/Seoul)  
Host: Mac Studio, Apple M5 Max, 64 GiB unified memory, macOS 27.0.  
Track: Ttakkari PMS WBS 17.2. This is independent of the Qwen3.5-9B Shadow router; that router's goals and artifacts were not changed.

## Result

**No usable inference baseline.** The isolated `mlx_lm --version` probe failed during `mlx.core` import because Metal could not find an accessible device (`[metal::load_device] No Metal device available`). A pinned GGUF was downloaded and hash-verified. The first isolated llama.cpp b11655 attempt used `-ngl 0` but omitted `--device none`; context creation attempted Metal and failed with `failed to create command queue`. After reviewing `--help`, a single bounded retry used both `--device none` and `-ngl 0`. It loaded the model and reached loopback server-ready in 14.506 seconds according to the server log. Before sending any request, host available memory fell to 15.17 GiB and pressure changed to `warn`; the process was stopped and unloaded immediately. Thus there was no generation request, so TTFT, prefill/decode, request wall time, output quality and optimization A/B are not measured.

Fresh pre-load resources at 2026-10-15 23:17:48 KST: CPU 2.9%, 36.22 GiB available, memory pressure normal, swap 0, no `llama-server`. During first failed attempt, one snapshot showed process RSS 24.83 GiB, host available 27.40 GiB, pressure normal and swap 0. During retry, snapshots showed RSS 23.43 GiB / available 35.62 GiB / pressure normal early, then RSS 32.83 GiB / available 26.48 GiB / normal, and at model-ready RSS 19.09 GiB / available 15.17 GiB / pressure `warn` / swap 0. These are point samples, not certified peaks. After stopping the owned process, available memory recovered to 34.27 GiB, pressure normal, swap 0, and `llama-server` was absent from sampled processes. The prior 9B worker’s code, prompt, artifacts, and operational route were left untouched. Both owned lock cycles were released only after their model processes were unloaded and the owner IDs were verified: first cycle `c6ab567a-4140-4e6e-bf99-d1ca486d4d27` (initial Metal context failure); retry `d2a9d83e-3b5c-4a0b-b7f3-8b15118b2cee` (model ready, then safe stop before request).

## Executed probe and outcome

```text
command -v mlx_lm       -> /Users/winterholic/.local/bin/mlx_lm
command -v llama-server -> not found
mlx_lm --version        -> ImportError: [metal::load_device] No Metal device available.
```

The local isolated package metadata from the existing research manifest is `mlx-lm 0.32.0`, `mlx 0.32.3`, `mlx-metal 0.32.3`; upstream release tags and that local package are not assumed identical. No target weights were cached in the previously inspected experiment workspace. The initial MLX access probe did not load weights; the later bounded llama.cpp CPU pilot downloaded and hash-verified the pinned GGUF documented below. No engine was installed or built during this attempt.

## Compatibility findings from primary sources

- MLX-LM contains an upstream Qwen3.5 implementation in [`mlx_lm/models/qwen3_5.py`](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/qwen3_5.py). Its official server supports prefill-step sizing and KV-cache quantization; the upstream server guide warns that quantized KV cache disables batching and that large prefill-step × context matrices can consume the memory savings: [MLX-LM server guide](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md).
- MLX quantization is described by bit width, group size, mode, and optional mixed-quant predicates/recipes in [MLX-LM conversion source](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/convert.py). It is a different representation/calibration path from GGUF Q4_K_M. Calling both “4-bit” does not make them numerically equivalent; compare quality and runtime as separate treatments.
- llama.cpp's conversion code registers `Qwen3_5MoeForConditionalGeneration` / `Qwen3_5MoeForCausalLM` as Qwen3.5 MoE, confirming format conversion support in current upstream source: [llama.cpp Qwen conversion](https://github.com/ggml-org/llama.cpp/blob/master/conversion/qwen.py). A local build and Metal execution were not available in this shell.
- The official [Qwen3.5-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B) says thinking is enabled by default, describes a 262,144-token default context, and specifies `chat_template_kwargs: {"enable_thinking": false}` for direct/non-thinking responses. Cross-engine tests must pin this flag and the exact template/sampling parameters; changing these invalidates a paired comparison.

## Measurement status

| Measure | Status |
|---|---|
| Model artifact | Pinned revision and GGUF SHA256 verified; tokenizer/template hashes not measured |
| MLX 4-bit cold/warm inference | Not run; Metal device unavailable |
| GGUF Q4_K_M cold/warm inference | Model loaded only in bounded CPU retry; stopped before request when available fell below 16 GiB and pressure warned |
| TTFT, prefill/decode throughput, wall time | Not measured |
| Process RSS peak, system minimum available/compression/swap during inference | No inference; during-load point sample RSS 19.09 GiB / available 15.17 GiB / pressure warn / swap 0 caused stop; not a certified peak |
| Output correctness/quality regression gate | Not measured; no model output |
| Synthetic fixture/task results | Not run |
| Live/user data | Not accessed |
| 17.3 one-factor tuning | Not started; baseline prerequisite unmet |

The preregistered quality gate remains unchanged: no critical correctness regression, exact task/unit pass rate within 1 percentage point of baseline, no new malformed/truncated output; a setting must improve in at least 4 of 5 repetitions beyond run noise. The 17.3 optimizations remain proposals until an executable baseline exists.

## Safe next step

For a real baseline, arrange enough safe memory headroom that the same 16 GiB floor remains intact while the model is ready and during requests. The isolated binary did load when explicitly constrained with `--device none -ngl 0`, but this host reached 15.17 GiB available and pressure `warn` before the first request, so the safety gate correctly prevented inference. Do not lower the threshold or bypass Metal. Any future attempt needs fresh telemetry, an atomic lock, and pinned model/tokenizer/template hashes; run baseline only, and start one-factor tuning only after paired repeats and the fixed quality gate pass. Do not change production service, router, environment, STT/runtime, global, power or GPU settings.


## Pinned artifact and failed bounded CPU pilot

- Source: `lmstudio-community/Qwen3.5-35B-A3B-GGUF`, revision `529ae8d8705097e646043ff81181c6564b6a8449`; license advertised as Apache-2.0 on the model repository. File: `Qwen3.5-35B-A3B-Q4_K_M.gguf`, 21,169,116,992 bytes. SHA256 `f25d609171b8f80950a60f38696597f74025de4070682d2fb1eeffe306ca7d5d`; GGUF magic `GGUF\x03` verified. The Xet/HTTP hash `4c9023852adb1985d66b7921ce7a10335ce063319c435ff3bf2d003af5f52c3e` is metadata distinct from the file SHA256.
- Isolated binary: `/private/tmp/qwen35-router/llama/llama-b11655/llama-server`, version `0.6.0-dev (build 11655, commit 47f5a64ae)`. Invocation: `-m <pinned GGUF> -ngl 0 -t 2 -tb 2 -c 256 -b 16 -ub 16 -np 1 --no-warmup --host 127.0.0.1 --port 19483 --no-webui --jinja`.
- Owned lock: worker `c6ab567a-4140-4e6e-bf99-d1ca486d4d27`; coordinator PID 69342; llama-server PID 78408. The lock owner file records exact experiment, path, revision, hashes, process and resource snapshots.
- Load failed before readiness: `ggml_metal_init: error: failed to create command queue`; `ggml_backend_metal_device_init_backend: failed to allocate context`; `llama_init_from_model: failed to initialize the backend`; context fitting failed. No request or inference happened. The process was interrupted; its owned trap sent SIGTERM and waited. Follow-up Ttakkari resources no longer listed PID 78408 / any llama-server. No unrelated process was terminated.
- Pre-load at 23:17:48 KST: 36.22 GiB available, normal pressure, swap 0, CPU 2.9%. During load sample at 23:18:10 KST: llama-server RSS 24.83 GiB, available 27.40 GiB, normal pressure, swap 0, process CPU 99.9%. Post-stop at 23:18:22 KST: 33.17 GiB available, normal pressure, swap 0, llama-server absent. This is not a peak RSS or inference measurement.
- TTFT/prefill/decode/wall, cold/warm, quality and repetitions: not measured. No baseline is established; WBS 17.3 tuning and 17.4 coding-task evaluation remain blocked on an executable baseline. The shared lock was released after verifying each matching worker ID, unloaded model process, and no remaining llama-server.


## Single explicit CPU-only retry outcome

The first invocation omitted an explicit device constraint; its load attempted Metal and failed. Read-only `--help` confirmed `--device none` means no offloading. After recording and releasing the first failed worker lock, one fresh bounded retry was performed under new worker ID `d2a9d83e-3b5c-4a0b-b7f3-8b15118b2cee`, coordinator PID 80745. Pre-load Ttakkari resources at 23:20:25 KST: 32.54 GiB available, pressure normal, swap 0, CPU 7.6%; llama-server absent. Invocation repeated the same parameters with `--device none -ngl 0 -t 2 -tb 2 -c 256 -b 16 -ub 16 -np 1 --no-warmup --host 127.0.0.1 --port 19483 --no-webui --jinja`.

The server log reached `model loaded` and listening on loopback after 14.506 seconds. No request was sent. At 23:20:55 KST, available memory was 16,281,894,912 B (15.17 GiB), pressure `warn`, swap 0, while process RSS was 20,493,615,104 B (19.09 GiB). Both the `<16 GiB` and pressure-change gates required immediate stop. Ctrl-C stopped only PID 81054; post-stop resources at 23:21:03 KST recovered to 34.27 GiB available, pressure normal, swap 0; no llama-server remained. Owner ID and unloaded/no-request state were verified before removing only this worker’s owner.json and lock directory.

Result: CPU-only **model loading is feasible**, but this run provides no inference performance or quality evidence; host headroom is insufficient under the agreed threshold for even one request. No more retries or tuning in this run.

## Sources

- [MLX-LM Qwen3.5 implementation](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/qwen3_5.py)
- [MLX-LM server guide](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md)
- [MLX-LM conversion / quantization source](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/convert.py)
- [llama.cpp Qwen conversion source](https://github.com/ggml-org/llama.cpp/blob/master/conversion/qwen.py)
- [Official Qwen3.5-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B)

## Lock lifecycle

The worker acquired the shared lock atomically at 2026-10-15 23:03:31 KST (worker_id `5a8337d4-47f0-4cf2-9cca-05e95f56a32a`). After the access probe exited and post-probe telemetry remained normal, the worker verified the owner ID and the no-model/no-process state, then released only its own directory at 23:05:50 KST using owner-file unlink followed by `rmdir`. Full command/output and timestamps are in `qwen35-apple-probe-log-2026-10-15.txt`.
