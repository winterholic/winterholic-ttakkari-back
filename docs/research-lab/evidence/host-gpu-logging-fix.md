# Host GPU device evidence: logging investigation and narrow correction

- Lead Codex/GPT, 2026-10-25.
- The current backend was independently identified earlier as revision 600eb843;
  this change is local and has not been deployed or used in an actual GPU benchmark.
- Scope: host **test** server argv only. ResourceLease ownership, GPU settings,
  production Qwen servers, cancellation and stop rules are unchanged.

## Observations and execution boundary

The permitted historical test state `/tmp/rv2-stage2/home/data/host-gpu/state.json`
records launch `c0de72b2e91c408db01ee1b7e15c09c1`, qwen3.5-4b-q4_k_m,
llama-b11655, gpu_layers=999, ctx=8192, f16 KV, flash_attn=on. Its state is stopped,
exit code 0, and the corresponding test resource lease is released. These historical
records are not a fresh execution of A or proof of GPU layer placement.

The historical **test** server log is 3,719 bytes / 35 lines. Running the existing
`device_evidence` parser on it returned an empty list. Its first lines report
`verbosity = 3`; model load, listener startup and prompt/decode timings are present,
but no hardware placement lines. No protected deployment log files were read.
The lead did not launch any server, query OS processes or retry a denied Metal probe.

Read-only command on the exact server executable:

```text
.../llama/llama-b11655/llama-server --help
exit 0
-lv, --verbosity, --log-verbosity N ... 3: info; 4: trace; 5: debug; default: 3
```

The current official [common/log.cpp](https://raw.githubusercontent.com/ggml-org/llama.cpp/master/common/log.cpp)
maps GGML INFO/CONT records to trace verbosity 4 and drops them below the configured
threshold. [common/arg.cpp](https://raw.githubusercontent.com/ggml-org/llama.cpp/master/common/arg.cpp)
defines the numeric `--log-verbosity` option. This supports the logging hypothesis.
The b11655 tagged source could not be fetched through the web tool, so **the installed
binary's exact callback implementation and runtime causality remain inferred** until
the changed invocation actually produces placement logs on the approved host path.

## TDD and change

Added `tests/test_host_gpu_device_logging.py` before implementation. It asserts the
host test server argv explicitly requests `--log-verbosity 4`.

```text
PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py -q --tb=short
RED: 1 failed, missing --log-verbosity
GREEN after the argv change: 1 passed in 0.06s
```

The initial ordinary pytest invocation could not collect because another worker was
mid-edit in api/research.py (IndentationError). The pure argv tests do not require the
app or database, so `--noconftest` isolates them without changing the application.
That intermediate editing state is not a diagnosed product failure.

```text
PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py tests/test_host_gpu.py -k 'build_args or start_request or request_rejects' -q --tb=short
6 passed, 31 deselected in 0.09s
.venv/bin/ruff check src/ttakkari/host_gpu.py tests/test_host_gpu_device_logging.py --output-format concise
All checks passed!
git diff --check -- src/ttakkari/host_gpu.py tests/test_host_gpu_device_logging.py
exit 0
```

The six selected existing tests exclude the new separately executed test; total scope
is seven pure argument/request tests. No fake or actual GPU lifecycle test was run.

## Remaining A acceptance

Independent review of this small change is still required. Actual A must use a runtime
with the changed argv, retain device placement, request/config/measurement/raw output,
and verify lease restoration. An accepted deployment reservation alone does not prove
that runtime is active. Do not run unchanged starts hoping for different evidence.
