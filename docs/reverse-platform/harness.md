# Reverse comparison harness v2

`ttakkari.reverse_compare` runs two argv commands with the same `input.argv`, UTF-8 stdin, working directory and inherited process environment; it never invokes a shell. CLI stdout and stderr are kept as readable text plus base64 of captured bytes. If a configured output limit truncates either stream, or a command is missing or times out, the comparison stays `unverified`. Raw execution captures are written under `.reverse/comparisons/<id>-<run_id>.json` with SHA-256 references. `verify_comparisons` reopens and hashes the capture before recomputing the result.

The API path sends the same method, serialized request body and headers to both allowlisted HTTP(S) endpoints. The default allowlist is loopback (`localhost`, `127.0.0.1`, `::1`); callers may supply explicit hostnames. It records exact response body bytes, status, nonsecret response headers, optional state snapshots before/after the request, and request errors. Header values whose names indicate authorization, cookies, tokens, keys, secrets or passwords are replaced with `[REDACTED]` in logs. The demo runs only two disposable local servers.

The web path accepts observations captured through the configured browser tools. It preserves events, DOM, state, network observations, screenshot references and a separate `visual_assessment` evidence item. A functional `match` does not imply visual `match`; absent a screenshot assessment, visual evidence is explicitly `unverified`. Browser snapshots and screenshots remain raw evidence. The demo fixture has a same-function red button so the pixel comparison produces an intentional visual mismatch.

## Run the samples

From the backend worktree, with the existing `back/.venv` Python and `PYTHONPATH=src`:

```sh
../back/.venv/bin/python scripts/reverse_compare_basename.py --output /tmp/reverse-basename --run-id basename-run-1
../back/.venv/bin/python scripts/reverse_compare_api.py --output /tmp/reverse-api --run-id api-run-1
../back/.venv/bin/python scripts/reverse_compare_recheck.py --workspace /tmp/reverse-basename --report /tmp/reverse-basename/comparisons.json --output /tmp/reverse-basename/recomputed.json --run-id basename-run-1
```

For web, run `PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_compare_web_demo.py --output /tmp/reverse-web --serve`, observe `/original/` and `/replica/` using `ttakkari_browser`, then save the resulting observations and screenshots and call `scripts/reverse_compare_web_capture.py`. That script stores a functional comparison and keeps a separate visual result in `evidence[kind=visual_assessment]`.

## Captured target run

On 2026-11-07 KST, the harness directly ran the real `/usr/bin/basename` against the source fixture `scripts/reverse_compare_basename_replica.py`: nine vectors covered plain input, Korean and spaces, suffixes, trailing slashes, root and empty values, `-s`, `-a`, missing arguments and an invalid option. Eight matched; the invalid-option error remains a mismatch because the original and replica print different command prefixes. No output normalization was applied. This is a harness target run, not a platform agent E2E.

The local API demo produced four comparisons: successful JSON/state transition, 422 validation, 404 route, and an 8 KiB response. All four matched and the server verifier recomputed all four from hashed raw captures. The configured browser demo recorded the same click, DOM state, and completion state on both pages. The pixel-exact 1280×720 screenshots differ at 2,146 pixels (button color); visual result is `mismatch`. Browser capture is isolated and local; this is not a production-site check.

## Contract note for platform owners

The report contract has exactly the declared comparison keys, so this harness keeps visual judgement as a distinct `visual_assessment` evidence item inside the allowed `evidence` array rather than adding a top-level field. Consumers that render visual status should read that evidence kind separately from `result`.

CLI byte equality uses `stdout_bytes_b64` and `stderr_bytes_b64`; the server comparator must prefer these when present, because two different invalid UTF-8 byte strings can decode to the same replacement text. API direct verification recomputes status/body-bytes/headers/state from captures. Current verifier deliberately leaves browser-observed web comparisons `unverified` as direct platform comparisons; the harness itself retains its browser-observed functional result and separate visual verdict. If platform status needs a server-verified web `match`, the contract needs an explicit rule for accepting browser observations and independently validating screenshot references.
