# Independent GPT review of FND integration 2a50c860

Candidate: `2a50c860ae94256d643a58d373d1291ecc7997ad`. Detached review
worktree: `back-platform-v2-fnd-review-2a50c86`. Remote branch SHA matched via
`git ls-remote origin refs/heads/integration/platform-v2-fnd01-report-20261025`.

Existing focused tests pass: 69 passed in 9.71s, exit0. Command and raw output:
`root-fnd-focused-verification.txt`. This checks the supplied old-algorithm
model, report DB races, foundation fixtures and generation-deployment tests.
The worker's 2071-pass report was read in the candidate's evidence document;
its full run was not repeated here and the raw full-suite output was not opened.

Automatic first-transition approval remains withheld. The additional test
`tests/test_tts_first_transition_pending_delivery.py` fails (1 failed/0.62s).
Raw output: `root-fnd-pending-delivery-probe.txt`. A request that passed the old
proxy route but is delivered to the old API after the idle observation can pass
the old active check before activate, commit afterward and be failed by the old
guard with `generation_draining`. This is a deterministic OLD-ALGORITHM MODEL
and real isolated PostgreSQL measurement, not a measured live Caddy transport
race. It verifies the unproved boundary already disclosed by the author; it is
not evidence that the author concealed it or that production lost a job.

`drain_previous_tts` accepts once HTTP inflight and queued counts are zero after
a fixed sleep. It has no acknowledgment or upper bound for proxy-to-API requests
not yet represented in HTTP inflight. Therefore errors/timeouts fail closed,
but the accepted path is not proved fail-closed for delayed delivery or proxy
restart. Extending the sleep does not establish that proof.

Supported operational observation: `backend_deploy_status` for reservation
`11be2d66-eaf7-4165-af94-a189118b808f` returned `draining`, activated revision
`17212d95932d90192912e4aaf235cf8017971576`, runtime path
`/Users/winterholic/.ttakkari/deploy/releases/17212d95932d90192912e4aaf235cf8017971576`.
This means new requests switched, not that old generations retired. No runtime
files were read. The authorized Git object's TTS code still has the old active
check/commit algorithm and does not persist chunks_json at acceptance. The
worker's 917ea4b model must not be presented as the measured live binary.

Next required boundary: establish a supported proxy/transport admission barrier
or another verified lossless first transition. No production switch, main push,
service restart, new lease/fence or forced Run termination was performed.

## Follow-up: real isolated Caddy transport

`tests/test_tts_first_transition_caddy.py` launches its own Caddy v2.11.7 child on
ephemeral public/admin ports with a controlled asyncio upstream. Production
8787/20191, production processes and production DB are not touched. A forwarded
POST is delayed before the old-model API admission check. The real `/load`
response and candidate `drain_previous_tts` return before that check runs.
New POSTs receive 503, but the request in the old configuration later receives
202 and is then marked failed/generation_draining by the old algorithm.

Measurements: initial reproducer `1 passed in 1.31s`; a second run adding the
Caddy upstream diagnostic reports `1 passed in 0.59s`. These are successful
reproductions of the unsafe ordering, not acceptance-test approval. Raw outputs:
`root-fnd-caddy-delivery-probe.txt`, `root-fnd-caddy-counter-probe.txt`.
The diagnostic after the candidate's gate returns shows `num_requests: 1`,
whereas the modeled API's HTTP admission count is zero. A proxy-side count could
detect this reproduced case, but a counter-only extension has not been proved
to cover earlier pre-handler scheduling, proxy restart or direct-port requests.
It must not be described as a complete barrier without that verification.

This upgrades only the transport part of the earlier model evidence: Caddy and
isolated PostgreSQL are real; the old API/guard remains a model. No production
loss is observed. The author's background full-suite report does not close this
boundary and is not a reason to approve automatic first transition.

Reference inspected for installed version (not a deployed-binary binding):
https://raw.githubusercontent.com/caddyserver/caddy/v2.11.7/modules/caddyhttp/app.go
(reload waits for shutdown start, completion waiting is on process exit), and
reverseproxy/hosts.go plus reverseproxy/reverseproxy.go (upstream count scope).

DB scope clarification: the command inherited an explicitly provided
TTAKKARI_TEST_DB. Conftest rejects the production database name, but this was not
an independently created PostgreSQL cluster. Earlier "isolated PostgreSQL"
wording means the non-production test context only; a separate server was not
established by this execution. Only Caddy's own process/admin/public ports were
newly isolated by the reproducer.
