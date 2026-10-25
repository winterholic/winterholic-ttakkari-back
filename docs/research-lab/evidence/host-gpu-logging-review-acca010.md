# Independent review: host GPU logging argv (`acca010`)

- Reviewer: Codex, 2026-10-25.
- Repository: `projects/winterholic-ttakkari/back-research-v2` only.
- Reviewed revision: `acca0105fc55dfb1af2d97fe72fdf88f0405eb99`.
- Review gate: **PASS — static logging-option/argv regression gate only**.
- Actual A acceptance: **not run; remains unverified**. No server start, benchmark,
  process enumeration, Metal probe, deployment, or runtime restart was performed.

## Scope and reviewed change

The reviewed diff changes `build_args()` in `src/ttakkari/host_gpu.py` to append the
fixed pair `--log-verbosity`, `4` to the llama-server command. Its sole production
call site is the host GPU test supervisor (`HostGpuSupervisor.run`). This reviewed
change does not add a request-controlled flag or modify the request model/allowlist.
The production Qwen path does not call this builder. The nearby changes are a focused
argv assertion and an investigation note. No product source was edited during this
review.

Safety boundary: this option reaches the local host **test** server invocation only.
The diff does not change lease acquisition/reconciliation, cancellation, stop rules,
GPU/model settings, or production Qwen server arguments. Existing allowlist tests also
assert that unsupported flags such as `--api-key` remain absent. Additional trace
logging is therefore confined to the test server's existing log destination; the
review does not establish what the changed runtime will emit.

## Evidence inspected

The permitted historical test receipt was read from:

- `/tmp/rv2-stage2/home/data/host-gpu/server.log` — 3,719 bytes, 35 lines.
- `/tmp/rv2-stage2/home/data/host-gpu/state.json` — launch
  `c0de72b2e91c408db01ee1b7e15c09c1`, model `qwen3.5-4b-q4_k_m`,
  `gpu_layers=999`, `ctx_size=8192`, F16 KV, flash attention on.
- `/tmp/rv2-stage2/home/data/host-resource/lease.json` — same lease ID and launch;
  phase `released`, runtime status `stopped`, server exit code 0.

The old state command does **not** contain `--log-verbosity`. Its log reports
`verbosity = 3`, model loaded/listener started, and prompt/decode timings. The current
repository `device_evidence()` parser was run against that log and returned
`device_evidence=[]`. This supports the narrow observation that the historical
successful model-serving run had no parser-recognized placement lines. It is not
evidence of actual GPU offload, nor evidence about behavior after this argv change.

The lead's investigation note records that the exact installed `llama-b11655` binary's
read-only `--help` exited 0 and lists `--log-verbosity N`, default 3, with 4 as trace.
That help result and the official current llama.cpp `common/log.cpp` mapping are
reviewed as inherited evidence from that note; I did not execute a binary outside the
repository or fetch a second source revision in this repository-only review. The
lead also records that the b11655 tagged callback source could not be fetched. Thus,
that exact binary's callback behavior and the claim that the new option will yield
device-placement lines remain inferred until a changed-runtime receipt exists.

## Commands run in the repository

All Python invocations used the repository `.venv/bin/python` as requested.

```text
PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py -q --tb=short
.                                                                        [100%]
1 passed in 0.11s

PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py tests/test_host_gpu.py -k 'build_args or start_request or request_rejects' -q --tb=short
......                                                                   [100%]
6 passed, 31 deselected in 0.07s

.venv/bin/ruff check src/ttakkari/host_gpu.py tests/test_host_gpu_device_logging.py --output-format concise
All checks passed!

git diff --check -- src/ttakkari/host_gpu.py tests/test_host_gpu_device_logging.py
exit 0
```

The separately-run new test plus six selected request/argv tests give seven focused
static checks. No fake-server lifecycle test was treated as GPU evidence. The selected
tests show the fixed option is emitted and existing validated request parameters and
rejection behavior still pass; they cannot show that a real llama-server accepts the
changed invocation or emits Metal placement details.

## Review conclusion and remaining evidence

No wrong option, duplicate option, argument-injection path, or regression in the
selected request/argv checks was found. The small change is safe for the stated
test-server scope. **This passes only the static logging-option gate, not real A.**

For actual A, a runtime that includes this revision must produce a retained receipt
with the effective argv, model/request configuration, raw placement log lines,
measurement output, stopped server state, and released lease. Until then, placement
logging on `llama-b11655`, actual Metal placement, and runtime causality are
unverified/inferred. The prior historical start must not be repeated as a substitute:
its recorded argv predates this option.
