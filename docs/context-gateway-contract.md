# Context Gateway v1 contract

## Boundary and deployment

`context_gateway` is a standalone Python distribution. It does not import the Ttakkari application and contains no source adapter, database broker, RAG, LLM, or original-service proxy. The Private Publisher sends only an explicitly approved snapshot to the separate control app. The public app reads that immutable snapshot from its own PostgreSQL database and snapshot volume.

Run the control app on port `8788` and the public app on `8787` as separate processes. The control app is not mounted into the public ASGI app; it has no OpenAPI or docs route. In the Compose example both host ports bind to loopback. A Private Publisher on the host calls `http://127.0.0.1:8788`; the public side may be exposed separately by a later infrastructure change. This contract does not change DNS or Tunnel configuration.

All control calls use `Authorization: Bearer <control-token>`. The token is provisioned out of band and must be an independent random secret. Control credentials and issued Gateway keys are never accepted in URL query parameters. TLS is required on any network path beyond loopback.

## Private Publisher JSON contract

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
  "verified_at": "2026-11-16T00:05:00Z",
  "documents": [
    {
      "id": "overview",
      "title": "Approved overview",
      "content_type": "text/markdown",
      "content": "Only explicitly approved content belongs here.",
      "sha256": "optional-lowercase-sha256-of-UTF8-content"
    }
  ]
}
```

Required fields are `project_slug`, `project_name`, `source_refs`, `source_revision`, `generated_at`, `verified_at`, and a non-empty `documents` list. `project_description` is optional. Document `sha256` is optional, but when supplied it must match the UTF-8 content bytes. The Gateway always calculates and stores a SHA-256 for each document and for the canonical manifest. The manifest records `source_refs`, `source_revision`, `generated_at`, `verified_at`, and the per-document hashes. `source_refs` should be opaque stable identifiers, not local paths, credentials, or copied source content.

Each call creates a new immutable revision. Its content is staged in the dedicated snapshot volume, renamed into its final directory, and then the snapshot row and project latest pointer are committed together in PostgreSQL. If the database commit fails the new directory is removed. A failed hash check returns `409` and publishes no snapshot. A successful response is `201` with the snapshot ID (`id`), Gateway project UUID (`project_id`), timestamps, source metadata, per-document hashes, and manifest hash; it does not include source document contents. The `project_id` is the UUID to place in a later key ACL.

### Revoke a snapshot

`POST /control/v1/snapshots/{snapshot_id}/revoke` with the same control header. Returns `200 {"revoked": true, "snapshot_id": "…"}` or `404`. Revocation is irreversible. If it was the latest pointer, PostgreSQL switches the project pointer to the newest remaining active snapshot in the same transaction; otherwise the pointer becomes null. Every REST and MCP read rejects revoked snapshots.

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

Allowed scopes are `read`, `search`, and `export`. Response `201` contains `key_id`, the raw 256-bit random `key`, and `expires_at` exactly one hour after issuance. The raw key is returned once; PostgreSQL stores only its SHA-256 hash. The Publisher must deliver it to the intended client over a protected channel and must not log or persist the response body.

### Revoke a key

`POST /control/v1/keys/{key_id}/revoke` with the same control header. Returns `200 {"revoked": true, "key_id": "…"}` or `404`. Every session checks the parent key on every request, so revocation takes effect immediately.

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

Context and document endpoints negotiate `Accept: text/markdown` or `Accept: application/json`; unsupported Accept values return `406`. Export `markdown` and `json` return those media types, while `zip` returns `application/zip`. No endpoint reads or proxies an internal source URL. Unknown and unauthorized project IDs both return `404`; missing, expired, reused, or revoked credentials return the same `401` response. Session requests are limited per minute in PostgreSQL and checked against project ACLs and scopes on every call.

## MCP contract

`/mcp` uses the official Python MCP SDK Streamable HTTP transport. It exposes exactly four read-only tools, all marked `readOnlyHint`:

- `list_projects()` — projects allowed by the session's `read` scope.
- `get_project_context(project_id, snapshot_id?)` — one snapshot's approved documents.
- `search_project_context(project_id, query, snapshot_id?)` — matching document snippets (`search` scope).
- `get_document(project_id, document_id, snapshot_id?)` — one approved document.

MCP requests require the same session Bearer header. HTTP authentication rejects invalid sessions before protocol handling; each tool then applies the same live key/session checks, ACL, scope, rate limit, audit, and snapshot integrity verification as REST.

## Storage and error behavior

The Gateway PostgreSQL database contains projects, immutable snapshot manifests and metadata, API key hashes, project ACL/scope rows, session hashes and expiry/revocation/rate counters, and audit events. Snapshot document bytes live only in the dedicated snapshot volume. Public runtime uses a database role that can read snapshots and update only one-time key consumption, session counters, and audit rows; it cannot publish or alter snapshots. The control process uses the database owner role.

On each read the Gateway hashes the stored canonical manifest and every document before returning content. A mismatch or missing file returns `409` with a generic integrity error and does not return document bytes. Snapshot database rows are protected by a PostgreSQL trigger: metadata cannot be updated or deleted, and revocation cannot be undone. Files are written read-only and never overwritten by the application.

## Run the isolated Compose example

Copy `deploy/context-gateway/.env.example` to `deploy/context-gateway/.env` (untracked); replace each placeholder with an independent URL-safe random value. Set `CONTEXT_GATEWAY_MCP_ALLOWED_HOSTS` to the actual Host header used by the public MCP endpoint. From the repository root run:

```sh
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml config -q
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml up --build -d
docker compose --env-file deploy/context-gateway/.env -f deploy/context-gateway/compose.yaml ps
```

Only the Gateway images, dedicated PostgreSQL service, and named `gateway_db` / `gateway_snapshots` volumes are present. The Gateway image build context is only `context_gateway/`; the PostgreSQL role-setup script is copied into its image at build time. The internal Compose network has no outbound route; PostgreSQL has no published port; no Private service or host volume is mounted. The control port is loopback-bound and separately authenticated. The public process mounts snapshot storage read-only and uses a limited database role. If a DB volume already existed before the public role was added, the image init script will not rerun; create that role and its schema grants with the control database owner before starting the public process.
