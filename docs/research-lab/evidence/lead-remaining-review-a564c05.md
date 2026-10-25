# Lead reconciliation of remaining C/D/UX acceptance

- Date: 2026-10-25, lead Codex/GPT.
- Inspected independent report: `remaining-integration-review-a564c05.md` (8b9d622).
- Its product boundary is backend a564c05, unchanged retrieval code through f9e28aa,
  frontend d574b6e. The subsequent worker acknowledgment is not a new verification.

## Accepted scope and open defects

The report contains actual ASGI/isolated PostgreSQL commands and rows for 18 source
conflict cases, valid-source reload, source-free manual/import handling, actual CPU
retrieval with no primary improvement, and synthetic D candidate registration/reuse.
These scoped checks remain evidence; D's synthetic decode_tps=1.0 is not a benchmark.
The lead did not read other Runs' outbox raw JSON or screenshots, so their contents
are not independently confirmed here.

The metric issue needs a narrower diagnosis than separating candidate counts from
evaluation cutoffs: current backend intentionally gives top_k precedence. d574b6e
correctly names metrics with that effective value. However, buildPayload still emits
an ineffective k sweep and localErrors does not block it. Both the actual independent
API result (two k arms, both raw_output.k=20) and this lead's frontend execution support
that remaining defect.

Executed temporary Vitest negative control:

```text
pnpm exec vitest run src/pages/research/lead-shadowed-k.probe.test.ts
{"search_space":{"k":[5,20],"top_k":20},"errors":["방법을 하나 이상 선택하세요."]}
Tests 1 failed; expected a top_k conflict explanation, received none
```

The temporary probe was removed after execution. The implementation owner will add
the permanent regression with both method-selected UI and JSON input coverage.
This review does not invalidate d574b6e's passing metric-key normalization checks.

C's unsupported source_* request and absent clone UI confirm the missing structural
reuse journey. The existing study Node API already supports unverified interpretation
notes; the missing surface is entering and displaying that interpretation beside the
specific experiment. An automatic numeric summary must not become a claimed cause.
Re-execution and best-artifact PARTIAL results are missing acceptance evidence rather
than proof of missing implementations.

## Follow-up contract sent to existing implementation owners

- Backend 8730ff9d: four optional typed source UUID fields on experiment create/model,
  source configuration required whenever reuse is requested; derive/check study and
  experiment, validate optional run against the same configuration and scope. Copy
  source parameters into fixed inputs before explicit overrides and planner expansion;
  retain source IDs in new definition/GET/resume/event without modifying the source.
  Add nullable FK columns through additive migration 0073 and update generation contract.
- Frontend a9302d45: reject shadowed k sweep before creation with actionable guidance,
  add result-to-follow-up-config flow using the same four source fields, and reuse the
  existing Node(kind=interpretation) API for separately labeled researcher explanations.
  Keep the top_k precedence contract and the existing design system.
- B owner 20a0e2cf keeps runner/TTS ownership. The independent artifact review's static
  missing-path/deduplication concerns and mismatched historical Markdown/JSON IDs were
  sent for executed negative controls and correction; they are not claimed as runtime
  failures until reproduced.

## Additional artifact execution in this lead Run

The lead parsed the standard injected test DB identity as database=ttakkari_test,
user=ttk_test, host=127.0.0.1 without printing the URL. Executed:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_scenario_b_artifacts.py -q --tb=short
db reset: 1 selective (avg 3.0/115 tables), 0 forced full, 0 fell back to full
1 passed in 0.54s
```

This is the real isolated PostgreSQL/ASGI normal artifact-copy/metadata/content path
using a dummy WAV producer and the f9e28aa runner. It is not an actual synthesis/STT
rerun, nor coverage for missing files, duplicates or legacy IDs. The independent
artifact worker's own DB-identity mismatch still applies to that worker's environment.

No push or deployment was requested. A/Metal/RuntimeLease execution and final complete
UX/re-execution/best-artifact acceptance remain open.
