# Existing A Q8 device-evidence correction

Observed 2026-10-30 KST by reading the preserved repository sources. This is a
follow-up to [research-gpu-intent-6a994c2b.md](research-gpu-intent-6a994c2b.md);
the earlier wording remains intact.

## Identity and evidence distinction

The Q8 treatment is `Run 0b3d1f3c-e86a-4488-8dd4-e2a3f15b4382` in study
`cb031b8c-2f55-4f7d-b771-6b116732798a`, experiment
`dd87c769-d339-49f6-878e-5b86dd1ac2a6`, configuration
`e9d56a9c-bff6-46bc-b9ae-e539255fb47f`. The saved Run record in
`operating-A-fcd3dd16.json` has the Q8 request (qwen3.5-4b-q4_k_m, ctx 4096,
key q8_0/value f16), launch ID `1fb953ed2f744e1695e325d6ab6f1cc8`, and start/stop
times `1792992954.600136` / `1792992958.460334`. Those request and exact times
match `operating-A-metal-6a994c2b.json`. The baseline comparison
`operating-baseline-comparison-fcd3dd16.json` records that the existing
`eb41e3a7-43cd-4f2b-8c03-79380a2e1163` study snapshot had 575 fields compared,
zero changed or missing, one added study entry, and was not truncated; the
Q8 Run identity and launch details are in the separate operating-A record.

The Q8 Run's own persisted `raw_output.device_evidence` has eight initialization
lines. Separately, the preserved same-request/same-time host status has eight
`device_evidence` lines containing offload and allocation evidence as well as
two initialization lines:

```text
offloaded 33/33 layers to GPU
MTL0_Mapped model buffer size = 2571.63 MiB
MTL0 KV buffer size = 98.00 MiB
MTL0 compute buffer size = 79.27 MiB
```

Thus the previous note correctly describes the Run's own raw field, but its
claim that the preserved same A Q8 status contains only initialization lines is
too broad. The preserved status supports placement for this Q8 treatment. It
does not establish baseline layer placement, a new launch, long-lived owner
crash recovery, an independent RuntimeLease, or general quality/performance
superiority. No 33/33 run was repeated.

## Source hashes

SHA-256 of the preserved source files read for this correction:

| File | SHA-256 |
|---|---|
| `research-gpu-intent-6a994c2b.md` | `81968cae6f3e44827045adf2101f67ee033b762198454db7cbd942cec2fddf1b` |
| `research-gpu-intent-6a994c2b.json` | `144ec9fd9087c228aff044a3ba3ffc62de97554d59e8076c703143501e7d5bd5` |
| `operating-A-fcd3dd16.json` | `025af5ad76738111f2bf31244791278bb3e73396916523d7a8beaaa98f2ac970` |
| `operating-A-metal-6a994c2b.json` | `35336a9e470e144c2b98b4214856824ff149176beba2422de6feda7ed35b08ed` |
| `operating-baseline-comparison-fcd3dd16.json` | `9d17567cbe0b6d8729f74e6954175d5ee822cdae57f461481aa2eb3d2d70083e` |

This source inspection does not alter the distinct saved Run raw evidence or
restate the stored succeeded status as a new execution.
