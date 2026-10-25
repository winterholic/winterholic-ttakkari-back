# Scenario B lead review — f9e28aa

- Reviewed product revision: `f9e28aa8cc2a6461f22d34f086be14c481992b98`.
- Reviewer: lead GPT, Codex command sandbox, 2026-10-25.
- Verdict: **B comparison FAIL; artifact integration independently pending**.

## Executed negative control

Command: `PYTHONPATH=src .venv/bin/python docs/research-lab/evidence/repro-scenario-b-double-normalization.py`.

The baseline's `_normalize_with_options` output retains `HTTP 200 OK` and `15.5MB`.
`chunk_text`, which the adapter calls next, always invokes `normalize_text`.
Both baseline and candidate therefore send the same Korean expansion to synthesis.
The probe prints `baseline_candidate_synthesis_equal= True` and fails its assertion.
This is an executed preprocessing defect, not an inference from equal audio hashes alone.

The committed raw export `stage2-b-supplement-evidence.json` also reports equal
baseline/candidate per-chunk WAV hashes, normalized-reference STT strings, and CER/WER.
These observations cannot establish a comparison of normalization-disabled versus enabled synthesis.
The previous supplement's PASS remains a worker claim superseded by this review.
No claim is made that the speech recordings or measured synthesis times are fabricated.

The lead inspected the runner/adapter diff and the artifact persistence test, but did
not execute the new PostgreSQL/ASGI artifact test in this review. A fresh independent
verification worker owns that scope. Other Runs' outbox files were not read.

## Authorized follow-up already queued

- Existing B implementer `20a0e2cf-ec25-44e6-bb7a-9666fa5ef316`, correction Run
  `b4ad5289-227d-404a-b9eb-ffa79a9d5bf1`: preserve the disabled input through chunking,
  add a failing provider-input regression first, then rerun the two actual synthesis/STT
  configurations and preserve new evidence separately from these historical measurements.
- Fresh verifier `c371f490-f29b-41eb-85e2-088ce0a3dc70`, Run
  `24c58f0e-f2fb-4e8b-88f6-1f1aa7ac6ed6`: independently check f9e28aa artifact storage/API
  and negative controls; no product source changes.
- Integration verifier `fd0fed7d-8d43-44f5-aaf9-87e83a91965a` retains C/D, source contracts,
  frontend metrics and original UX questions; it was informed of the B change.

## A diagnostic boundary (no new GPU run)

Read-only `GET http://127.0.0.1:8787/api/version` returned 200 and
`600eb8432f7e1a3e64a9f3a07f175cd183fb90eb` (PID 6372).
Supported `ops_deploy_log(job="600eb843", mode="summary")` returned succeeded,
reservation `045547ce-ba54-415f-b044-4705b4b1fe34`, and the corresponding release path.
The lead read that release's `src/ttakkari/host_gpu.py` only, without modifying it.
AST comparison shows its `status` and `device_evidence` functions equal the checkout's;
`status` does **not** expose `launch_id` by design. Missing public launch ID alone does
not establish an absent adapter or a failed backend.

Supported `host_gpu_status` returned `stopped`, `running=false`, `ready=false`,
`server_exit_code=0`, `device_evidence=[]` for the previous launch. No start, bench,
lease mutation, protected log read, or sandbox bypass was attempted in this review.
Actual A adapter execution, Metal evidence and RuntimeLease restoration remain unverified.
