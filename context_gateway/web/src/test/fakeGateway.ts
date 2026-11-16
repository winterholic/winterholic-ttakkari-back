// 합성 Gateway. 실제 서버(apps.py) 응답 모양을 코드에서 읽어 흉내 낸 것이며 실제 서버로 검증한 값이 아니다.
// 키·세션·문서는 전부 가짜다.

export const PROJECT_A = "11111111-1111-4111-8111-111111111111";
export const PROJECT_B = "22222222-2222-4222-8222-222222222222";
export const SNAP_A_NEW = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2";
export const SNAP_A_OLD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1";
export const SNAP_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1";
export const KEY = "synthetic-one-time-key-0001";
export const KEY_TWO_PROJECTS = "synthetic-one-time-key-0002";
export const SESSION_TOKEN = "synthetic-session-token-0001";
export const B_SECRET = "BETA-ONLY-SECRET-TEXT";

interface Doc {
  id: string;
  title: string;
  content: string;
}

interface SnapshotFixture {
  id: string;
  projectId: string;
  publishedAt: string;
  sourceRefs: string[];
  sourceRevision: string;
  generatedAt: string;
  verifiedAt: string | null;
  docs: Doc[];
}

export const SNAPSHOTS: SnapshotFixture[] = [
  {
    id: SNAP_A_NEW,
    projectId: PROJECT_A,
    publishedAt: "2026-11-16T03:00:00+00:00",
    sourceRefs: ["repo://alpha@abc1234", "docs/overview.md"],
    sourceRevision: "abc1234",
    generatedAt: "2026-11-16T02:00:00+00:00",
    verifiedAt: "2026-11-16T02:30:00+00:00",
    docs: [
      { id: "overview", title: "개요", content: "# 알파 개요\n\n알파 프로젝트는 **합성 데이터**다.\n\n- 항목 하나\n- 항목 둘" },
      { id: "api", title: "API 노트", content: "## 엔드포인트\n\n`GET /things` 는 목록을 돌려준다." },
    ],
  },
  {
    id: SNAP_A_OLD,
    projectId: PROJECT_A,
    publishedAt: "2026-11-15T03:00:00+00:00",
    sourceRefs: ["repo://alpha@old0001"],
    sourceRevision: "old0001",
    generatedAt: "2026-11-15T02:00:00+00:00",
    verifiedAt: null,
    docs: [{ id: "overview", title: "개요", content: "# 알파 개요 (이전판)\n\n옛 내용." }],
  },
  {
    id: SNAP_B,
    projectId: PROJECT_B,
    publishedAt: "2026-11-16T04:00:00+00:00",
    sourceRefs: ["repo://beta@def5678"],
    sourceRevision: "def5678",
    generatedAt: "2026-11-16T03:30:00+00:00",
    verifiedAt: "2026-11-16T03:45:00+00:00",
    docs: [{ id: "secret", title: "베타 비밀", content: `# 베타\n\n${B_SECRET}` }],
  },
];

const PROJECTS = [
  { id: PROJECT_A, slug: "alpha", name: "알파 프로젝트", description: "합성 알파", latest_snapshot_id: SNAP_A_NEW },
  { id: PROJECT_B, slug: "beta", name: "베타 프로젝트", description: "합성 베타", latest_snapshot_id: SNAP_B },
];

export interface RecordedCall {
  method: string;
  path: string;
  search: string;
  url: string;
  authorization: string | null;
  accept: string | null;
  credentials: RequestCredentials | undefined;
}

export type Fault = { status: number; headers?: Record<string, string>; body?: unknown } | "network";

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

const toMeta = (d: Doc) => ({ id: d.id, title: d.title, content_type: "text/markdown", sha256: "0".repeat(64) });
const snapshotOut = (s: SnapshotFixture) => ({
  id: s.id,
  source_refs: s.sourceRefs,
  source_revision: s.sourceRevision,
  generated_at: s.generatedAt,
  verified_at: s.verifiedAt,
  published_at: s.publishedAt,
  manifest_sha256: "f".repeat(64),
  documents: s.docs.map((d) => toMeta(d)),
});

export class FakeGateway {
  readonly calls: RecordedCall[] = [];
  /** 인스턴스마다 복사본을 써서 테스트가 문서를 바꿔도 서로 새지 않는다. */
  readonly snapshots: SnapshotFixture[] = structuredClone(SNAPSHOTS);
  /** 키 → 허용 프로젝트. 교환되면 used가 true가 된다. */
  readonly keys = new Map<string, { used: boolean; projects: string[]; scopes: string[] }>([
    [KEY, { used: false, projects: [PROJECT_A], scopes: ["read", "search", "export"] }],
    [KEY_TWO_PROJECTS, { used: false, projects: [PROJECT_A, PROJECT_B], scopes: ["read", "search", "export"] }],
  ]);
  private readonly sessions = new Map<string, { projects: string[]; scopes: string[]; expiresAt: number }>();
  private tokenSeq = 0;
  /** 다음에 매칭되는 요청 하나에만 적용되는 장애. 경로 부분 문자열로 고른다. */
  private readonly faults: { match: string; fault: Fault; sticky: boolean }[] = [];
  /** 지정한 경로 요청을 수동으로 풀어 줄 때까지 붙잡는다. 경쟁 상태 테스트용. */
  private readonly holds: { match: string; release: Promise<void> }[] = [];
  /** 교환 응답의 세션 만료까지 남은 ms. */
  sessionTtlMs = 60 * 60 * 1000;
  /** 계약 위반을 흉내 내기 위해 검색 응답에 다른 프로젝트 id를 심는다. */
  leakProjectInSearch = false;
  leakProjectInContext = false;

  failNext(match: string, fault: Fault, sticky = false): void {
    this.faults.push({ match, fault, sticky });
  }

  hold(match: string): () => void {
    let release!: () => void;
    const promise = new Promise<void>((resolve) => (release = resolve));
    this.holds.push({ match, release: promise });
    return release;
  }

  expireAllSessions(): void {
    this.sessions.clear();
  }

  callsTo(pathPart: string): RecordedCall[] {
    return this.calls.filter((c) => c.path.includes(pathPart));
  }

  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(typeof input === "string" || input instanceof URL ? input.toString() : input.url, "http://gateway.test");
    const headers = new Headers(init?.headers);
    const method = (init?.method ?? "GET").toUpperCase();
    const call: RecordedCall = {
      method,
      path: url.pathname,
      search: url.search,
      url: url.toString(),
      authorization: headers.get("authorization"),
      accept: headers.get("accept"),
      credentials: init?.credentials,
    };
    this.calls.push(call);
    const hold = this.holds.find((h) => url.pathname.includes(h.match));
    if (hold) await hold.release;
    const faultIndex = this.faults.findIndex((f) => url.pathname.includes(f.match));
    if (faultIndex >= 0) {
      const entry = this.faults[faultIndex]!;
      if (!entry.sticky) this.faults.splice(faultIndex, 1);
      if (entry.fault === "network") throw new TypeError("Failed to fetch");
      return json(entry.fault.body ?? { detail: "fault" }, entry.fault.status, entry.fault.headers);
    }
    return this.route(url, method, call);
  };

  private bearer(call: RecordedCall): string | null {
    const match = /^Bearer (\S+)$/.exec(call.authorization ?? "");
    return match?.[1] ?? null;
  }

  private route(url: URL, method: string, call: RecordedCall): Response {
    const path = url.pathname;
    if (path === "/api/v1/auth/exchange" && method === "POST") {
      const raw = this.bearer(call);
      const key = raw ? this.keys.get(raw) : undefined;
      if (!key || key.used) return json({ detail: "unauthorized" }, 401, { "www-authenticate": "Bearer" });
      key.used = true;
      const token = `${SESSION_TOKEN}-${++this.tokenSeq}`;
      const expiresAt = Date.now() + this.sessionTtlMs;
      this.sessions.set(token, { projects: key.projects, scopes: key.scopes, expiresAt });
      return json({ access_token: token, token_type: "Bearer", expires_at: new Date(expiresAt).toISOString() });
    }
    const raw = this.bearer(call);
    const session = raw ? this.sessions.get(raw) : undefined;
    if (!session || session.expiresAt <= Date.now()) return json({ detail: "unauthorized" }, 401, { "www-authenticate": "Bearer" });
    if (path === "/api/v1/projects") return json(PROJECTS.filter((p) => session.projects.includes(p.id)));
    const m = /^\/api\/v1\/projects\/([^/]+)(?:\/(snapshots|context|search|export)|\/documents\/([^/]+))?$/.exec(path);
    if (!m) return json({ detail: "not found" }, 404);
    const projectId = m[1]!;
    const sub = m[2] ?? (m[3] ? "document" : "project");
    const needScope = sub === "search" ? "search" : sub === "export" ? "export" : "read";
    if (!session.projects.includes(projectId) || !session.scopes.includes(needScope)) return json({ detail: "not found" }, 404);
    const project = PROJECTS.find((p) => p.id === projectId)!;
    const snapshots = this.snapshots.filter((s) => s.projectId === projectId).sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
    if (sub === "project") return json(project);
    if (sub === "snapshots") return json(snapshots.map(snapshotOut));
    const wanted = url.searchParams.get("snapshot_id") ?? project.latest_snapshot_id;
    const snapshot = snapshots.find((s) => s.id === wanted);
    if (!snapshot) return json({ detail: "not found" }, 404);
    const full = (d: Doc) => ({ ...toMeta(d), content: d.content });
    if (sub === "context") {
      const leaked = this.leakProjectInContext ? PROJECTS.find((p) => p.id !== projectId)! : project;
      return json({ project: leaked, snapshot: snapshotOut(snapshot), documents: snapshot.docs.map(full) });
    }
    if (sub === "document") {
      const doc = snapshot.docs.find((d) => d.id === decodeURIComponent(m[3]!));
      return doc ? json(full(doc)) : json({ detail: "not found" }, 404);
    }
    if (sub === "search") {
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const results = snapshot.docs
        .map((d) => ({ id: d.id, title: d.title, snippet: d.content.slice(0, 500), matches: d.content.toLowerCase().split(q).length - 1 }))
        .filter((r) => r.matches > 0);
      return json({ project_id: this.leakProjectInSearch ? PROJECT_B : projectId, snapshot_id: snapshot.id, query: url.searchParams.get("q"), results });
    }
    const format = url.searchParams.get("format");
    if (format === "json") return json({ project, snapshot: snapshotOut(snapshot), documents: snapshot.docs.map(full) });
    if (format === "markdown") {
      return new Response(snapshot.docs.map((d) => `# ${d.title}\n\n${d.content}`).join("\n\n"), { headers: { "content-type": "text/markdown; charset=utf-8" } });
    }
    if (format === "zip") {
      return new Response(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), {
        headers: { "content-type": "application/zip", "content-disposition": `attachment; filename="${project.slug}-${snapshot.id}.zip"` },
      });
    }
    return json({ detail: "bad format" }, 422);
  }
}
