# Independent web evidence module

This module separates browser-observed behavior from screenshot pixels. It consumes the existing reverse comparison shape (exactly 14 top-level keys) and stores the web-specific evidence under one evidence item whose kind is web_capture. It does not add an API endpoint, start browsers, or execute user-supplied commands.

## Bounded capture contract

create_web_capture(root, ...) writes a canonical JSON raw capture to
.reverse/web-captures/<comparison-id>-<run-id>.json; it returns a comparison
record with the existing keys:

id, behavior, target_type, input, pre_state, original, replica, environment,
evidence_level, evidence, verified_at, run_id, result, difference.

The raw JSON has exactly these fields:

schema_version, target_type, comparison_id, behavior, input, pre_state,
environment, expected_viewport, origin_map, original, replica, run_id,
verified_at, complete.

Each original and replica observation has exactly:

run_id, observed_at, url, input, viewport, pre_state, environment, events, dom,
state, network, screenshot, complete, capture_state.

Both observations must use the same run ID, exact input, viewport, pre-state, and
environment. observed_at and verified_at are timezone-aware ISO-8601 timestamps
within the one-day freshness window. The viewport is {width,height,device_scale_factor};
width is at most 7680, height at most 4320, scale factor 1–3, and decoded pixels at
most 20 million. Both complete=true and capture_state=complete are required.
Events, semantic DOM nodes and state are bounded JSON. Network rows are:

    {method,url,status,request_body,response_body_sha256,response_headers}

Network request count is at most 500; events at most 500; DOM nodes at most 3000.
Raw JSON is limited to 1 MiB; each PNG is limited to 12 MiB. Only PNG is accepted.
Screenshot references have {path,sha256,size_bytes,viewport,format=png}. The
decoder verifies bytes, dimensions and actual RGBA pixel values. Paths must be
relative to root; path traversal, outside-root symlinks, non-regular files,
missing files, oversized JSON/images, and mismatching SHA/size are rejected.
The broker's resolve_in_root and ensure_regular_file policies are reused.

URL origins, including their original ports, stay in original.url, replica.url
and each network row. Network comparison maps an origin only when the caller
provides its exact HTTP(S) source-to-replica origin pair in origin_map. The path
and query remain exact. Methods, request JSON, status, response body SHA and
non-secret response headers also remain exact. No general URL, output, DOM, or
state normalization is done. Sensitive response header names are rejected.

## Recomputed outcomes

verify_web_capture(root, capture, expected_run_id=...) reopens the hashed raw
capture, checks the record/raw-capture correspondence, validates both observations,
and recomputes:

- functional_result: exact semantic events, DOM, state, and mapped network
  request/response fields. An absent observation, empty-on-both observations,
  differing shared preconditions, incomplete capture, malformed capture, or
  wrong run ID cannot yield a match.
- visual_result: exact PNG pixel equality. Different viewport metadata or pixel
  size, missing image, bad SHA, corrupt/truncated PNG, or invalid pixels yields
  unverified. Different actual pixels yields mismatch.
- result: the existing comparison result follows functional evidence only.
  Therefore result=match may coexist with visual_result=mismatch; consumers
  must render both independently. No aggregate reproduction score is produced.
- integrity.status: verified means file bytes and declared structural
  relationships were rechecked, not that a browser produced the data.
- provenance.status: always unverified until trusted RunEvent/tool-result
  linkage is implemented. Hashes establish file consistency only.

The record's incoming result, difference, and evidence result fields are never
used as verdicts. A capture writer cannot authenticate browser execution. The
current tool call evidence and hand-transcribed observation JSON can be replayed
with recomputed hashes; report that limitation explicitly.

### Future RunEvent/tool-result linkage

For a true execution provenance verdict, the integrator must attach a receipt to
the comparison with run_id, browser tool call IDs, matching tool.call and
tool.result RunEvent sequence numbers, configured tool name, argument hash,
result hash, and raw result artifact path. The server must load those events from
the same Run, verify matching call IDs/tool name/non-error status, hash the
canonical complete tool result bytes, then verify each referenced screenshot
artifact against the tool result's returned path and image hash. If stored events
are scrubbed or truncated, the receipt is insufficient and provenance remains
unverified; the server must not trust agent-authored receipt fields alone. Existing
RunEvent payloads are scrubbed on append, so the current module deliberately
does not claim this link exists.

## Core integration example

The API/core worker can pass the comparison already built for the v2 report:

    from pathlib import Path
    from ttakkari.reverse_web_evidence import verify_web_capture

    verified = verify_web_capture(
        Path(workspace_root),
        comparison_record,
        expected_run_id=str(current_run.id),
    )
    # Keep verified result and evidence[0].visual_result separate in result.latest.

Capture producers first save each configured-browser screenshot under the
workspace root and supply its actual relative path, SHA-256, byte size and viewport.
The create_web_capture call returns a record using the same 14-key public shape.
Do not promote provenance.status without the event/tool-result verification above.

## CLI use

Create an observations JSON file under the workspace root containing the keyword
arguments accepted by create_web_capture, then run:

    PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_web_evidence_capture.py --root /absolute/workspace --observations .reverse/input/web-observations.json
    PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_web_evidence_verify.py --root /absolute/workspace --comparison .reverse/comparisons/<id>-<run-id>.web.json --expected-run-id <run-id>

reverse_web_evidence_demo.py --port 0 serves an independently authored small
replica only on 127.0.0.1; stop it with Ctrl-C. The existing
scripts/reverse_e2e_targets.py start web starts the original test target and
its ownership-checked stop --pid-file shuts down only that process.

ReverseJob web prompts use the current process's dependency-enabled
`sys.executable` and resolve both capture scripts from the root containing the
loaded `src/ttakkari/reverse_tasks.py`. In a development checkout this uses the
existing backend `.venv`; in an immutable release it uses that release root's
`.venv/bin/python` and `scripts/`. `Releases.archive()` extracts tracked source
and scripts into the release root, while the runtime supplies its `.venv`
there. Keep the virtual-environment path as invoked: resolving a symlink to the
base interpreter can drop the environment's Pillow dependency. Prompts `cd` to
the actual job folder and quote paths containing spaces. Create
`.reverse/input/web-observations.json` there,
save the unedited configured-browser PNG bytes under `.reverse/web-captures/`,
and use the module's `reverse_web_evidence_capture.py` writer followed by
`reverse_web_evidence_verify.py`. For each run, original and replica must share
the exact input, pre-state, viewport and environment. The verifier must repeat
the observation in its own Run and store a separate comparison using its own Run
ID. The verifier does not reuse the implementation capture as its observation.

Label browser observations, manually transcribed values, secondary requests and
inferences separately. If the original is unavailable or the configured browser
fails, preserve a reason and keep the comparison `unverified`; request missing
access as `needs_input`. Writer and verifier commands test the capture module in
a job folder. They do not establish a platform ReverseJob browser E2E or
authenticated browser provenance. `provenance.status` remains `unverified` until
trusted RunEvent/tool-result linkage exists.

When `collect_outputs` builds a ZIP for a completed Run, it reopens each web raw
capture with that role's expected Run ID and rechecks the comparison, PNG paths,
byte counts and SHA-256 immediately before writing. Original relative paths are
preserved in the ZIP so the raw capture still resolves to its PNGs. Each ZIP
contains `reverse-web-evidence-manifest.json`, with distinct `implementation`
and `verifier` Run/session records, their comparisons, visual summaries,
integrity, provenance, and test/check logs under
`evidence/roles/<role>/checks/`. Included logs carry their own SHA and size;
missing or rejected records carry a reason. A web artifact that fails a check is
not packaged or marked included.

General source, test and log files retain the existing 1 MiB file limit. Web raw
JSON remains limited to 1 MiB and each verified PNG uses its own 12 MiB limit;
the general file limit is not raised for all archive members.

## Verification boundary and negative controls

Unit fixtures are explicitly synthetic and prove the verifier's recomputation
logic, not a browser run. The three controls are: same function/same pixels →
functional match and visual match; same function/different pixels → functional
match and visual mismatch; different state/same pixels → functional mismatch and
visual match. Missing/tampered image, viewport disagreement, truncation, empty
observations, symlink escape and wrong run ID do not become matches.

A configured-browser capture of the local original and independently authored
replica is a separate observation from these fixtures. It validates that the
module can consume real browser screenshots and observation records. Neither that
module-level check nor this script is a ReverseJob agent E2E; only a new platform
job run through the actual API/agent path can establish that.

## 2026-11-07 configured-browser capture

This is a module-consumption check using the configured `ttakkari_browser`, not a
platform ReverseJob E2E. The original is the loopback target served by the existing
`scripts/reverse_e2e_targets.py`; the comparison page was served separately by
`scripts/reverse_web_evidence_demo.py`. It is a developer demo replica. Its separate
script and process are visible, but its independent authoring history is not
verified from Git, so this report does not claim an independently authored agent
implementation.

The previous Run's PID file identified PID 3049, kind `web`, and the canonical
`reverse_e2e_targets.py` script. The supported ownership-checked stop command
confirmed the owner token and PID, returned `{"stopped":true,"pid":3049,"kind":"web"}`,
and removed that PID file. New servers bound only to loopback: original
`http://127.0.0.1:55945/` and demo `http://127.0.0.1:55980/`.

Using one configured-browser session and a 390×844 CSS viewport, both pages were
reloaded to the same initial state (quantity 1, idle status, zero requests, no
user events). The successful flow filled quantity 3 and clicked “준비 요청”. Both
pages displayed “준비 완료 · 3개 · 요청 1건” and the same event list:
“요청 성공: 3개”, “수량 변경: 3”. The boundary flow reloaded each page, filled 6,
and clicked the button. Both retained the idle/zero-request status and displayed
only “수량 변경: 6”; no successful submission event appeared. The `type=number`
input's max=5 constraint blocked submission in both pages. Console output reported
zero errors and warnings.

Browser-tool network output showed each document GET returning HTTP 200. The tool
did not return body hashes or headers, so those fields were obtained by separate
loopback GETs and explicitly labeled secondary/transcribed in the manifest. The
HTML response hashes differ (`76812e…574e34` original, `2e85dc…4fdd7` demo), so the
module correctly reports a functional mismatch under its exact network-response
contract even though the observed semantic events, DOM and state agree. The PNGs
also differ: the verifier found 15,169 different pixels for the successful flow
and 15,295 for the boundary flow. Integrity was recomputed as verified; execution
provenance remains unverified because the browser tool interface did not expose
RunEvent linkage or tool call IDs. The viewport's device scale factor is also not
exposed; the schema records 1 and the manifest calls that value unverified.

The Run's exact browser actions, timestamps, URLs, accessibility observations,
screenshot source paths, secondary HTTP-hash method and authorship limitation are
recorded in `.reverse/web-evidence/browser-observation-manifest.json`. The actual
PNG files, writer inputs and writer/verifier CLI outputs are kept under
`.reverse/web-evidence/`. These are raw browser screenshots; they were not generated
from fixtures or edited to force a match.

Reproduce from the repository root while both loopback servers are running:

```sh
PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_web_evidence_capture.py \
  --root .reverse/web-evidence --observations input-observations.json
PYTHONPATH=src ../back/.venv/bin/python scripts/reverse_web_evidence_verify.py \
  --root .reverse/web-evidence \
  --comparison .reverse/comparisons/web-real-20261107-browser-observation-20261107.web.json \
  --expected-run-id browser-observation-20261107
```

The boundary input is `input-boundary-observations.json`; its report is
`.reverse/comparisons/web-boundary-20261107-browser-observation-20261107.web.json`.
The expected results are functional mismatch from the differing document response,
visual mismatch from actual pixels, integrity verified, and provenance unverified.
The existing synthetic negative controls remain the module unit tests: same
function/same pixels, same function/different pixels, and different state/similar
pixels. They do not turn this demo comparison into an agent E2E.

Core integration remains a separate call after a comparison record exists:

```python
from pathlib import Path
from ttakkari.reverse_web_evidence import verify_web_capture

checked = verify_web_capture(
    Path(workspace_root), comparison_record, expected_run_id=str(current_run.id)
)
# Preserve checked["result"] and checked["evidence"][0]["visual_result"] separately.
# Never promote provenance.status without trusted RunEvent/tool-result linkage.
```
