# Basename sample repair evidence

This is a direct CLI target run, not a platform agent E2E. The original is the real `/usr/bin/basename`; both commands receive the same argv vector and are compared on stdout bytes, stderr bytes, and exit code. No stderr or output normalization is used.

## Environment and commands

- Host: `macOS-27.0-arm64-arm-64bit-Mach-O`
- Python: `3.13.15`; replica subprocess uses the existing `../back/.venv/bin/python`
- Original: `/usr/bin/basename`
- Test: `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q scripts/test_reverse_compare_basename.py`
- Capture: `PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_compare_basename.py --output <capture-dir> --run-id <unique-run-id>`
- Server recomputation: `PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_compare_recheck.py --workspace <capture-dir> --report <capture-dir>/comparisons.json --output <capture-dir>/server-verify.json --run-id <same-unique-run-id>`

Each capture directory contains `comparisons.json`, a copy of the exact replica source used, `run.log`, and `.reverse/comparisons/*.json` raw captures. `server-verify.json` contains the recomputed comparison records. Run IDs are distinct and the earlier `basename-direct-run-20261107` evidence remains untouched.

## Results

| Capture | Run ID | Cases | Match | Mismatch | Unverified |
| --- | --- | ---: | ---: | ---: | ---: |
| Before | `basename-repaired-before-20261107T105000` | 16 | 12 | 4 | 0 |
| After | `basename-repaired-after-20261107T105500` | 16 | 16 | 0 | 0 |

Before mismatches observed against the original:

- Existing `-z`: original stderr starts `/usr/bin/basename:`, replica omitted the executable path.
- `////`: original prints `/`; replica printed an empty line.
- `-as .txt file.txt`: original accepts the combined flags; replica rejected `s` as an invalid option.
- `-s`: original says the option requires an argument; replica reported an illegal option.

The added vectors also exercise empty input, `-` and `--`, suffix equal to the whole basename, and suffix mode with multiple operands. All expected behavior comes from the live original command in this macOS environment; each unmodified and repaired replica run is retained in the two capture directories.

The verifier summaries are retained in `before-capture-20261107T105000/server-verify.json` and `after-capture-20261107T105500/server-verify.json`. Recomputed counts were respectively 12/4/0 and 16/0/0 for match/mismatch/unverified.

## Limitation

The observed `/usr/bin/basename` is the macOS implementation on the host above. This result does not establish behavior for other operating systems or prove that the web platform's agent executed this sample.
