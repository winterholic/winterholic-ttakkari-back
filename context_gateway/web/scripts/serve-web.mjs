// dist/를 같은 origin으로 서빙하며 /api·/mcp를 (a) 합성 Gateway 또는 (b) 실제 Gateway로 보낸다. 브라우저 검증 재현용이며 배포 코드가 아니다.
//   node scripts/serve-web.mjs                                      합성 API (기본, 포트 4173)
//   node scripts/serve-web.mjs --upstream http://127.0.0.1:18787    실제 Gateway로 프록시
//   --port <n>  --dist <dir>
// 합성 키: synthetic-<시나리오>-<1..9> (1회용). 시나리오는 아래 SCENARIOS. POST /__synthetic/reset 으로 되돌린다.
import { createServer, request as httpRequest } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const PORT = Number(opt("--port", "4173"));
const UPSTREAM = opt("--upstream", null);
const DIST = opt("--dist", join(dirname(fileURLToPath(import.meta.url)), "..", "dist"));

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const S_NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2";
const S_OLD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1";
const S_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1";

const HOSTILE =
  '# 개요\n\n합성 **데이터**다.\n\n<script>window.__pwned = 1</script>\n\n<img src=x onerror="window.__pwned=1">\n\n[javascript 링크](javascript:alert(1)) [정상 링크](https://example.com/a)\n\n![외부 이미지](https://tracker.invalid/p.png)\n\n| 열 | 값 |\n|---|---|\n| a | 1 |\n\n```\ncode block\n```\n';
const LONG = Array.from({ length: 40 }, (_, i) => `## 절 ${i + 1}\n\n긴 본문 단락입니다. 모바일 폭에서 줄바꿈과 스크롤이 어떻게 보이는지 확인하는 합성 문장이 반복됩니다. ${"가나다라마바사 ".repeat(12)}\n`).join("\n");
const snapshots = [
  {
    id: S_NEW, project: A, published: "2026-11-16T03:00:00Z", refs: ["opaque-ref-synthetic-alpha", "opaque-ref-2"], rev: "rev-2", generated: "2026-11-16T02:00:00Z", verified: "2026-11-16T02:30:00Z",
    docs: [
      { id: "overview", title: "Approved overview", type: "text/markdown", content: HOSTILE },
      { id: "long", title: "긴 문서 (스크롤 확인용)", type: "text/markdown", content: LONG },
      { id: "config", title: "Config", type: "application/json", content: '{"a": 1, "list": [1, 2, 3]}' },
    ],
  },
  {
    id: S_OLD, project: A, published: "2026-11-15T03:00:00Z", refs: ["opaque-ref-old"], rev: "rev-1", generated: "2026-11-15T02:00:00Z", verified: null,
    docs: [{ id: "overview", title: "Approved overview", type: "text/markdown", content: "# 개요 (이전판)\n\n검증 시각이 없는 스냅샷.\n" }],
  },
  {
    id: S_B, project: B, published: "2026-11-16T04:00:00Z", refs: ["opaque-ref-synthetic-beta"], rev: "rev-b", generated: "2026-11-16T03:30:00Z", verified: "2026-11-16T03:45:00Z",
    docs: [{ id: "secret", title: "Beta only", type: "text/markdown", content: "BETA-ONLY-TEXT\n" }],
  },
];
const projects = [
  { id: A, slug: "synthetic-alpha", name: "Synthetic Alpha", description: "합성 프로젝트 Alpha", latest_snapshot_id: S_NEW },
  { id: B, slug: "synthetic-beta", name: "Synthetic Beta", description: "합성 프로젝트 Beta", latest_snapshot_id: S_B },
];
const FULL = ["read", "search", "export"];
const SCENARIOS = {
  alpha: { projects: [A], scopes: FULL },
  both: { projects: [A, B], scopes: FULL },
  empty: { projects: [], scopes: FULL },
  readonly: { projects: [A], scopes: ["read"] },
  ratelimit: { projects: [A], scopes: FULL, docs429: true }, // 문서·전체 맥락·내보내기가 429 + Retry-After: 20
  short: { projects: [A], scopes: FULL, ttlMs: 15000 }, // 세션 15초 뒤 만료(만료 후 상태 초기화 확인)
  slow: { projects: [A], scopes: FULL, delayMs: 1500 }, // 데이터 응답 1.5초 지연(로딩 상태 확인)
  broken: { projects: [A], scopes: FULL, snapshots503: true }, // 스냅샷 목록 503(부분 오류 확인)
};
const used = new Set();
const sessions = new Map();
const shareSessions = new Map();
const shareDownOnce = new Set();
let seq = 0;

const json = (res, status, body, headers = {}) => {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(JSON.stringify(body));
};
const EXT = { "text/markdown": "md", "text/plain": "txt", "application/json": "json" };
const meta = (d) => ({ id: d.id, title: d.title, content_type: d.type, extension: EXT[d.type], sha256: "0".repeat(64) });
const snapOut = (s) => ({ id: s.id, source_refs: s.refs, source_revision: s.rev, generated_at: s.generated, verified_at: s.verified, published_at: s.published, manifest_sha256: "f".repeat(64), documents: s.docs.map(meta) });

async function synthetic(req, res, url) {
  if (url.pathname.startsWith("/api/v1/share/")) return syntheticShare(req, res, url);
  const m = /^Bearer (\S+)$/.exec(req.headers.authorization ?? "");
  const token = m?.[1];
  if (url.pathname === "/api/v1/auth/exchange" && req.method === "POST") {
    const k = /^synthetic-([a-z]+)-([1-9])$/.exec(token ?? "");
    const scenario = k && SCENARIOS[k[1]];
    if (!scenario || used.has(token)) return json(res, 401, { detail: "unauthorized" }, { "www-authenticate": "Bearer" });
    used.add(token);
    const session = `synthetic-session-${++seq}`;
    const expires = Date.now() + (scenario.ttlMs ?? 3600_000);
    sessions.set(session, { ...scenario, expires });
    return json(res, 200, { access_token: session, token_type: "Bearer", expires_at: new Date(expires).toISOString() });
  }
  const s = token && sessions.get(token);
  if (!s || s.expires <= Date.now()) return json(res, 401, { detail: "unauthorized" }, { "www-authenticate": "Bearer" });
  if (s.delayMs) await new Promise((resolve) => setTimeout(resolve, s.delayMs));
  if (url.pathname === "/api/v1/projects") return json(res, 200, projects.filter((p) => s.projects.includes(p.id)));
  const r = /^\/api\/v1\/projects\/([^/]+)(?:\/(snapshots|context|search|export)|\/documents\/([^/]+))?$/.exec(url.pathname);
  if (!r) return json(res, 404, { detail: "not found" });
  const sub = r[2] ?? (r[3] ? "document" : "project");
  const need = sub === "search" ? "search" : sub === "export" ? "export" : "read";
  if (!s.projects.includes(r[1]) || !s.scopes.includes(need)) return json(res, 404, { detail: "not found" });
  const project = projects.find((p) => p.id === r[1]);
  const snaps = snapshots.filter((x) => x.project === r[1]).sort((a, b) => b.published.localeCompare(a.published));
  if (sub === "project") return json(res, 200, project);
  if (sub === "snapshots") return s.snapshots503 ? json(res, 503, { detail: "unavailable" }) : json(res, 200, snaps.map(snapOut));
  if (s.docs429 && ["document", "context", "export"].includes(sub)) return json(res, 429, { detail: "rate limit exceeded" }, { "retry-after": "20" });
  const snap = snaps.find((x) => x.id === (url.searchParams.get("snapshot_id") ?? project.latest_snapshot_id));
  if (!snap) return json(res, 404, { detail: "not found" });
  const full = (d) => ({ ...meta(d), content: d.content });
  if (sub === "context") return json(res, 200, { project, snapshot: snapOut(snap), documents: snap.docs.map(full) });
  if (sub === "document") {
    const d = snap.docs.find((x) => x.id === decodeURIComponent(r[3]));
    return d ? json(res, 200, full(d)) : json(res, 404, { detail: "not found" });
  }
  if (sub === "search") {
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const results = snap.docs.map((d) => ({ id: d.id, title: d.title, snippet: d.content.slice(0, 500), matches: d.content.toLowerCase().split(q).length - 1 })).filter((x) => x.matches > 0);
    return json(res, 200, { project_id: project.id, snapshot_id: snap.id, query: url.searchParams.get("q"), results });
  }
  const format = url.searchParams.get("format");
  if (format === "json") return json(res, 200, { project, snapshot: snapOut(snap), documents: snap.docs.map(full) });
  if (format === "markdown") {
    res.writeHead(200, { "content-type": "text/markdown; charset=utf-8" });
    return res.end(snap.docs.map((d) => `# ${d.title}\n\n${d.content}`).join("\n\n"));
  }
  if (format === "zip") {
    res.writeHead(200, { "content-type": "application/zip", "content-disposition": `attachment; filename="${project.slug}-${snap.id}.zip"` });
    return res.end(Buffer.from([0x50, 0x4b, 0x05, 0x06, ...new Array(18).fill(0)]));
  }
  return json(res, 422, { detail: "bad format" });
}

async function syntheticShare(req, res, url) {
  if (url.pathname === "/api/v1/share/exchange" && req.method === "POST") {
    let input;
    try { input = JSON.parse(await new Promise((resolve, reject) => { let body = ""; req.setEncoding("utf8"); req.on("data", (chunk) => { body += chunk; }); req.on("end", () => resolve(body)); req.on("error", reject); })); }
    catch { return json(res, 400, { detail: "bad request" }); }
    const token = input?.token;
    if (typeof token !== "string" || !/^synthetic-share-(read|export|revoked|down)-[1-9]$/.test(token) || token.includes("revoked")) return json(res, 401, { detail: "unauthorized" });
    if (token.includes("down") && !shareDownOnce.has(token)) {
      shareDownOnce.add(token);
      return json(res, 503, { detail: "synthetic unavailable" });
    }
    const session = `synthetic-share-session-${++seq}`;
    shareSessions.set(session, { token, expires: Date.now() + 15 * 60_000 });
    res.writeHead(204, { "set-cookie": `cg_share_session=${session}; Path=/api/v1/share; Max-Age=900; HttpOnly; Secure; SameSite=Strict`, "cache-control": "no-store", "referrer-policy": "no-referrer" });
    return res.end();
  }
  const cookie = /(?:^|;\s*)cg_share_session=([^;]+)/.exec(req.headers.cookie ?? "")?.[1];
  const session = cookie && shareSessions.get(cookie);
  if (!session || session.expires <= Date.now()) return json(res, 401, { detail: "unauthorized" });
  const hasExport = session.token.includes("export");
  if (url.pathname === "/api/v1/share/context" && req.method === "GET") {
    const project = { id: A, slug: "synthetic-share", name: "합성 승인 맥락", description: "합성 API가 반환한 승인 요약입니다.", latest_snapshot_id: S_NEW };
    const docs = [{ id: "overview", title: "승인 맥락", content_type: "text/markdown", content: HOSTILE, source_refs: ["repo://synthetic-share"], source_revision: "synth123", generated_at: "2026-11-19T00:00:00Z", verified_at: null }];
    return json(res, 200, { project, snapshot: { ...snapOut(snapshots[0]), documents: docs.map(meta) }, documents: docs, link: { label: "합성 동료 검토", expires_at: new Date(Date.now() + 3 * 86400_000).toISOString(), snapshot_policy: "pinned", scopes: hasExport ? ["read", "export"] : ["read"] }, session_expires_at: new Date(session.expires).toISOString() });
  }
  if (url.pathname === "/api/v1/share/export" && req.method === "GET") {
    if (!hasExport) return json(res, 403, { detail: "scope denied" });
    if (url.searchParams.get("format") !== "markdown") return json(res, 422, { detail: "bad format" });
    res.writeHead(200, { "content-type": "text/markdown; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer" });
    return res.end("# 합성 승인 맥락\n\n합성 응답입니다.\n");
  }
  if (url.pathname === "/api/v1/share/logout" && req.method === "POST") {
    shareSessions.delete(cookie);
    res.writeHead(204, { "set-cookie": "cg_share_session=; Path=/api/v1/share; Max-Age=0; HttpOnly; Secure; SameSite=Strict", "cache-control": "no-store" });
    return res.end();
  }
  return json(res, 404, { detail: "not found" });
}

function proxy(req, res) {
  const target = new URL(UPSTREAM);
  const up = httpRequest({ hostname: target.hostname, port: target.port, path: req.url, method: req.method, headers: { ...req.headers, host: target.host } }, (r) => {
    res.writeHead(r.statusCode ?? 502, r.headers);
    r.pipe(res);
  });
  up.on("error", () => json(res, 502, { detail: "upstream unreachable" }));
  req.pipe(up);
}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'";

createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://local");
  if (url.pathname === "/__synthetic/reset" && req.method === "POST" && !UPSTREAM) {
    used.clear();
    sessions.clear();
    shareSessions.clear();
    shareDownOnce.clear();
    return json(res, 200, { reset: true });
  }
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/mcp")) {
    return UPSTREAM ? proxy(req, res) : synthetic(req, res, url).catch(() => json(res, 500, { detail: "synthetic error" }));
  }
  const rel = normalize(url.pathname === "/" || url.pathname === "/s" ? "/index.html" : url.pathname).replace(/^(\.\.[/\\])+/, "");
  try {
    const body = await readFile(join(DIST, rel));
    res.writeHead(200, { "content-type": TYPES[extname(rel)] ?? "application/octet-stream", "content-security-policy": CSP, "referrer-policy": "no-referrer", "x-content-type-options": "nosniff", "cache-control": "no-store" });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
  }
}).listen(PORT, "127.0.0.1", () => console.log(`serving ${DIST} on http://127.0.0.1:${PORT} (${UPSTREAM ? `proxy -> ${UPSTREAM}` : "synthetic API"})`));
