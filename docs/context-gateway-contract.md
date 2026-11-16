# Context Gateway v1 contract

## Boundary and deployment

`context_gateway` is a standalone Python distribution. It does not import the Ttakkari application and contains no source adapter, database broker, RAG, LLM, or original-service proxy. The Private Publisher sends only an explicitly approved snapshot to the separate control app. The public app reads that immutable snapshot from its own PostgreSQL database and snapshot volume.

Run the control app on port `8788` and the public app on `8787` as separate processes. The control app is not mounted into the public ASGI app; it has no OpenAPI or docs route. In the Compose example both host ports bind to loopback. A Private Publisher on the host calls `http://127.0.0.1:8788`; the public side may be exposed separately by a later infrastructure change. This contract does not change DNS or Tunnel configuration.

All control calls use `Authorization: Bearer <control-token>`. The token is provisioned out of band and must be an independent random secret. Control credentials and issued Gateway keys are never accepted in URL query parameters. TLS is required on any network path beyond loopback.

## Private Publisher JSON contract

All paths below are served by the separately launched control app at `http://127.0.0.1:8788` in the local Compose example. Send `Authorization: Bearer <control-token>` and `Content-Type: application/json` on JSON requests. The control token is the independently provisioned `CONTEXT_GATEWAY_CONTROL_TOKEN`; it is not a public session token or issued client key. Missing, malformed, or incorrect credentials return `401 {"detail":"unauthorized"}` with `WWW-Authenticate: Bearer`. Never put credentials in a URL, follow redirects with this credential, or log request/response credential bodies.

Successful JSON response examples below omit no response fields unless stated. Control API errors use FastAPI's `{"detail": ...}` envelope: request/schema and domain validation failures are `422`, hash mismatch is `409`, unknown revoke IDs are `404`, and missing/invalid control authentication is `401`. A malformed UUID path value is a request validation error (`422`). Do not infer success from a transport-level response alone; require the documented status and response fields.

### Publish an immutable snapshot

`POST /control/v1/snapshots` on the control port.

```http
Authorization: Bearer <control-token>
Content-Type: application/json
```

```json
{
  "project_slug": "synthetic-alpha",
  "project_name": "Synthetic Alpha",
  "project_description": "Approved context for an external consumer",
  "source_refs": ["opaque-source-ref-01"],
  "source_revision": "source-revision-42",
  "generated_at": "2026-11-16T00:00:00Z",
  "verified_at": null,
  "documents": [
    {
      "id": "overview",
      "title": "Approved overview",
      "content_type": "text/markdown",
      "content": "Only explicitly approved content belongs here.",
      "source_refs": ["note:overview-source"],
      "source_revision": "overview-revision-42",
      "generated_at": "2026-11-15T23:45:00Z",
      "verified_at": null,
      "sha256": "optional-lowercase-sha256-of-UTF8-content"
    }
  ]
}
```

Required fields are `project_slug`, `project_name`, `source_refs`, `source_revision`, `generated_at`, and a non-empty `documents` list. `project_description` and `verified_at` are optional; omitted or explicit `null` `verified_at` means there is no verified source approval timestamp. Set it only to the timezone-aware time when the snapshot content was actually verified; code inspection, snapshot generation, or publishing time is not a substitute. If present, `verified_at` must be at or after `generated_at`. `source_revision` must identify a source hash or revision actually observed from the source. Do not infer a Git SHA from the Publisher's code, checkout, or deployment; a Git SHA is valid only when it is itself the verified source revision being published. Document `sha256` is optional, but when supplied it must match the UTF-8 content bytes. The Gateway always calculates and stores a SHA-256 for each document and for the canonical manifest. The manifest records the snapshot-level `source_refs`, `source_revision`, `generated_at`, nullable `verified_at`, and the per-document hashes and source refs. `source_refs` should be opaque stable identifiers, not local paths, credentials, or copied source content.

Limits and validation: `project_slug` is 1–120 characters and matches `^[A-Za-z0-9][A-Za-z0-9._-]*$`; `project_name` is 1–240 characters; `project_description` is at most 4,000 characters; there are 1–100 snapshot-level `source_refs`; `source_revision` is 1–255 characters. `generated_at` must include a timezone. If non-null, `verified_at` must include a timezone and be at or after `generated_at`. There must be 1–1,000 documents and the combined UTF-8 document content must not exceed 20,000,000 bytes; an individual document's content is at most 2,000,000 characters. A document has `id` (1–120 characters, unique within the snapshot, ASCII letters/digits/dot/underscore/hyphen, starting with a letter or digit), `title` (1–500 characters), `content` (string), optional `content_type` (`text/markdown`, `text/plain`, or `application/json`; defaults to `text/markdown`), optional `source_refs` (up to 100 opaque references; defaults to `[]`), optional nullable `source_revision` (1–255 characters when present), optional nullable timezone-aware `generated_at`, optional nullable timezone-aware `verified_at` (at or after that document's `generated_at` when both are present), and optional lowercase 64-hex `sha256`. For document provenance fields, omission and explicit `null` both mean unknown; the manifest always emits all three fields with JSON `null` when unknown. Snapshot-level metadata is never copied into a document's fields. Set a document's `verified_at` only when that document itself was verified; publishing or verifying another document does not verify it.

### Canonical immutable manifest v1

Every published snapshot stores a canonical JSON manifest whose SHA-256 is `manifest_sha256`. Its required top-level fields are:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1`; version of this manifest format. |
| `snapshot_id` | Gateway snapshot UUID, also returned as response `id`. |
| `project_id` | Gateway project UUID, also returned in the response. |
| `project_slug` | Stable Publisher project slug. |
| `version` | Positive integer sequence per project, starting at 1; includes revoked snapshots and is never reused. |
| `created_at` | Gateway UTC timestamp when this immutable snapshot was created. |
| `status` | Immutable publication state `published`; current revocation state is held in PostgreSQL and reported separately. |
| `source_refs` | Snapshot-level opaque source references. |
| `source_revision` | The source revision/hash actually confirmed by the Publisher. |
| `generated_at` | Source snapshot generation time with timezone. |
| `verified_at` | Actual source approval/verification time, or JSON `null` when not verified. |
| `documents` | Document metadata array described below. |

Each manifest document includes `id`, `title`, `content_type`, `extension`, `path`, `sha256`, `source_refs`, `source_revision`, `generated_at`, and `verified_at`. `path` is a relative path under this snapshot's storage directory, in the form `documents/<document-id>.<extension>`; it is generated by the Gateway and must not contain an absolute path or `..` component. `sha256` is the lowercase SHA-256 of the stored UTF-8 document bytes. `source_refs` contains the opaque references specific to that document (or `[]` when none were supplied). The document-level revision and timestamps describe that document and are independent of the snapshot-level values; they may differ between documents in one snapshot. Unknown values are JSON `null` in the manifest. The manifest is immutable after publish; revoke changes only the database lifecycle state, never the hashed manifest.

Example manifest (hash value is illustrative):

```json
{
  "schema_version": 1,
  "snapshot_id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67",
  "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
  "project_slug": "synthetic-alpha",
  "version": 1,
  "created_at": "2026-11-16T00:05:02Z",
  "status": "published",
  "source_refs": ["opaque-source-ref-01"],
  "source_revision": "source-revision-42",
  "generated_at": "2026-11-16T00:00:00Z",
  "verified_at": null,
  "documents": [
    {
      "id": "overview",
      "title": "Approved overview",
      "content_type": "text/markdown",
      "extension": "md",
      "path": "documents/overview.md",
      "sha256": "<64 lowercase hex characters>",
      "source_refs": ["note:overview-source"],
      "source_revision": "overview-revision-42",
      "generated_at": "2026-11-15T23:45:00Z",
      "verified_at": null
    }
  ]
}
```

Each call creates a new immutable revision. Its content is staged in the dedicated snapshot volume, renamed into its final directory, and then the snapshot row and project latest pointer are committed together in PostgreSQL. If the database commit fails the new directory is removed. A failed hash check returns `409` and publishes no snapshot. A successful response is `201` with the snapshot ID (`id`), Gateway project UUID (`project_id`), timestamps, source metadata, per-document hashes, and manifest hash; it does not include source document contents. The `project_id` is the UUID to place in a later key ACL.

The successful response shape is:

```json
{
  "id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67",
  "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
  "schema_version": 1,
  "version": 1,
  "created_at": "2026-11-16T00:05:02Z",
  "status": "active",
  "source_refs": ["opaque-source-ref-01"],
  "source_revision": "source-revision-42",
  "generated_at": "2026-11-16T00:00:00Z",
  "verified_at": null,
  "published_at": "2026-11-16T00:05:02Z",
  "manifest_sha256": "<64 lowercase hex characters>",
  "documents": [
    {
      "id": "overview",
      "title": "Approved overview",
      "content_type": "text/markdown",
      "extension": "md",
      "path": "documents/overview.md",
      "sha256": "<64 lowercase hex characters>",
      "source_refs": ["note:overview-source"],
      "source_revision": "overview-revision-42",
      "generated_at": "2026-11-15T23:45:00Z",
      "verified_at": null
    }
  ]
}
```

The success response deliberately returns document metadata and hashes, never document bodies or the supplied optional content hash as a separate field. On invalid payload/domain values the response is `422 {"detail":"<reason>"}` (Pydantic shape errors may use a structured `detail` array); on a supplied hash mismatch or publish attempt for a revoked project it is `409`. The Publisher should persist the returned `project_id`, snapshot `id`, version, manifest hash, and document paths/hashes as its receipt.

### Permanently revoke project sharing

`POST /control/v1/projects/{project_id}/revoke` permanently withdraws a Gateway project. The Publisher uses the Gateway UUID returned by publish; there is no request body.

```http
POST /control/v1/projects/7bf808c4-b492-442e-9e7e-c753b42b420d/revoke
Authorization: Bearer <control-token>
```

The endpoint atomically marks the project sharing as permanently revoked, revokes every snapshot ever published under it, clears its latest-snapshot pointer, and records an audit event. Existing sessions are denied that project on their next request. Repeated calls are idempotent and return `200` with the same project identifier, `revoked: true`, the original `revoked_at`, and the total `snapshots_revoked` count:

```json
{
  "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
  "revoked": true,
  "revoked_at": "2026-11-16T01:15:00Z",
  "snapshots_revoked": 3
}
```

An unknown UUID returns `404 {"detail":"not found"}`. A revoked project cannot be published to again with its slug and cannot receive new key grants; both attempts return `409 {"detail":"project sharing has been revoked"}`. Existing keys may retain their ACL rows for audit, but all project listing and read authorization filters out the revoked project; an unexchanged one-time key with no other active project grant cannot be exchanged. Project sharing revocation is irreversible; use a new project slug/identity for a future, separately approved share.

Per-snapshot revocation remains available at `POST /control/v1/snapshots/{snapshot_id}/revoke` and does not revoke project sharing. It revokes only that snapshot; if it was latest, the latest pointer moves to the newest remaining active snapshot or `null`.

### Issue a one-time key

`POST /control/v1/keys` with the same control header:

```json
{
  "label": "synthetic-client",
  "projects": {
    "<project-uuid>": {"scopes": ["read", "search", "export"]}
  }
}
```

Allowed scopes are `read`, `search`, and `export`. Response `201` contains `key_id`, the raw 256-bit random `key`, and `expires_at` exactly one hour after issuance. The raw key is returned once; PostgreSQL stores only its SHA-256 hash. The Publisher must deliver it to the intended client over a protected channel and must not log or persist the response body. Attempting to grant a revoked project returns `409 {"detail":"project sharing has been revoked"}`.

`label` is 1–160 characters. `projects` must contain at least one Gateway project UUID; each grant must contain at least one allowed scope. A nonexistent project UUID or invalid scope returns `422 {"detail":"one or more projects do not exist"}` or `422 {"detail":"invalid scopes"}` respectively. Successful response:

```json
{
  "key_id": "87fae3bb-8b59-4e96-a10b-7152eb18e231",
  "key": "<one-time URL-safe encoding of 32 random bytes>",
  "expires_at": "2026-11-16T01:00:00+00:00"
}
```

The current API has no `ttl`, `ttl_seconds`, or requested-expiration field: issuance is fixed at 3,600 seconds (one hour). The current Pydantic request model uses its default extra-field behavior, so an unrecognized TTL field is ignored rather than applied; callers must omit TTL fields and treat returned `expires_at` as authoritative. The exchanged session expires no later than this original key expiry.

### Revoke a key

`POST /control/v1/keys/{key_id}/revoke` with the same control header and no body. Returns `200 {"revoked":true,"key_id":"87fae3bb-8b59-4e96-a10b-7152eb18e231"}`. Repeated revocation is idempotent; an unknown UUID returns `404 {"detail":"not found"}`. Revoking a key revokes all its project grants and makes its issued sessions fail their next authorization check.

## Public REST contract

The public app publishes OpenAPI 3.1 at `/openapi.json`. Every protected request requires `Authorization: Bearer <session-token>`. The endpoint `/api/v1/auth/exchange` accepts the one-time key in that header once and returns a session token whose expiry is capped at the key's original expiry. The session cannot extend the one-hour issuance window.

| Method and path | Scope | Response |
|---|---|---|
| `POST /api/v1/auth/exchange` | one-time key | `access_token`, `token_type`, `expires_at` |
| `GET /api/v1/projects` | `read` | projects granted to this key |
| `GET /api/v1/projects/{project_id}` | `read` | project metadata |
| `GET /api/v1/projects/{project_id}/snapshots` | `read` | active snapshot metadata |
| `GET /api/v1/projects/{project_id}/context?snapshot_id=…` | `read` | snapshot documents as JSON or Markdown |
| `GET /api/v1/projects/{project_id}/documents/{doc_id}` | `read` | one document as JSON or Markdown |
| `GET /api/v1/projects/{project_id}/search?q=…&snapshot_id=…` | `search` | matching document IDs, titles, snippets, and counts |
| `GET /api/v1/projects/{project_id}/export?format=markdown\|json\|zip` | `export` | selected snapshot export |

Context and document endpoints negotiate `Accept: text/markdown` or `Accept: application/json`; unsupported Accept values return `406`. Export `markdown` and `json` return those media types, while `zip` returns `application/zip`. No endpoint reads or proxies an internal source URL. Unknown, unauthorized, or project-revoked IDs all return `404`; missing, expired, reused, or revoked credentials return the same `401` response. Session requests are limited per minute in PostgreSQL and checked against project ACLs, scopes, and project sharing revocation on every call.

## MCP contract

`/mcp` uses the official Python MCP SDK Streamable HTTP transport. It exposes exactly four read-only tools, all marked `readOnlyHint`:

- `list_projects()` — projects allowed by the session's `read` scope.
- `get_project_context(project_id, snapshot_id?)` — one snapshot's approved documents.
- `search_project_context(project_id, query, snapshot_id?)` — matching document snippets (`search` scope).
- `get_document(project_id, document_id, snapshot_id?)` — one approved document.

MCP requests require the same session Bearer header. HTTP authentication rejects invalid sessions before protocol handling; each tool then applies the same live key/session checks, ACL, scope, rate limit, audit, and snapshot integrity verification as REST.

## Storage and error behavior

The Gateway PostgreSQL database contains projects, immutable snapshot manifests and metadata, API key hashes, project ACL/scope rows, session hashes and expiry/revocation/rate counters, and audit events. Successful control key issuance records `key_issued`; successful one-time exchange records `key_exchanged`; each successfully authorized REST or MCP read records `api_read` with key, session, and requested project identifiers. Rejected authentication, ACL/scope failures, and rate-limited requests do not emit `api_read`. Audit rows never contain raw API keys or session tokens. Snapshot document bytes live only in the dedicated snapshot volume. Public runtime uses a database role that can read snapshots and update only one-time key consumption, session counters, and audit rows; it cannot publish or alter snapshots. The control process uses the database owner role.

On each read the Gateway hashes the stored canonical manifest and every document before returning content. A mismatch or missing file returns `409` with a generic integrity error and does not return document bytes. Snapshot database rows are protected by a PostgreSQL trigger: metadata cannot be updated or deleted, and revocation cannot be undone. Files are written read-only and never overwritten by the application.

## Run the isolated Compose example

Copy `deploy/context-gateway/.env.example` to `deploy/context-gateway/.env` (untracked); replace each placeholder with an independent URL-safe random value. Set `CONTEXT_GATEWAY_MCP_ALLOWED_HOSTS` to the actual Host header used by the public MCP endpoint. From the repository root run:

```sh
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml config -q
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml up --build -d
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml ps
```

Only the Gateway images, dedicated PostgreSQL service, and named `gateway_db` / `gateway_snapshots` volumes are present. The Gateway image build context is only `context_gateway/`; the PostgreSQL role-setup script is copied into its image at build time. The internal Compose network has no outbound route; PostgreSQL has no published port; no Private service or host volume is mounted. The control port is loopback-bound and separately authenticated. The public process mounts snapshot storage read-only and uses a limited database role. If a DB volume already existed before the public role was added, the image init script will not rerun; create that role and its schema grants with the control database owner before starting the public process.
