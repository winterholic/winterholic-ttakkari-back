import { describe, expect, it, vi } from "vitest";
import { GatewayClient } from "./client";
import { GatewayError } from "./errors";
import { assertGatewayPath } from "./paths";
import { FakeGateway, KEY, PROJECT_A, PROJECT_B, SESSION_TOKEN, SNAP_A_OLD } from "../test/fakeGateway";

function setup(options: { onUnauthorized?: () => void } = {}) {
  const gateway = new FakeGateway();
  const client = new GatewayClient({ fetch: gateway.fetch, ...options });
  return { gateway, client };
}

async function failure(promise: Promise<unknown>): Promise<GatewayError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(GatewayError);
    return error as GatewayError;
  }
  throw new Error("expected rejection");
}

describe("GatewayClient 키 교환", () => {
  it("키를 Authorization 헤더로만 보내 POST /api/v1/auth/exchange를 한 번 호출한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    expect(gateway.calls).toHaveLength(1);
    const [call] = gateway.calls;
    expect(call).toMatchObject({ method: "POST", path: "/api/v1/auth/exchange", authorization: `Bearer ${KEY}`, search: "" });
    expect(call!.url).not.toContain(KEY);
    expect(client.hasSession).toBe(true);
  });

  it("같은 클라이언트에서 교환을 다시 부르면 네트워크 호출 없이 거절한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    const error = await failure(client.exchange(KEY));
    expect(error.kind).toBe("misuse");
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("교환이 실패한 클라이언트도 재사용하지 못한다(새 클라이언트로 다시 시도해야 한다)", async () => {
    const { gateway, client } = setup();
    expect((await failure(client.exchange("wrong-key"))).kind).toBe("unauthorized");
    expect((await failure(client.exchange(KEY))).kind).toBe("misuse");
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("동시에 두 번 불러도 서버에는 한 번만 간다", async () => {
    const { gateway, client } = setup();
    const results = await Promise.allSettled([client.exchange(KEY), client.exchange(KEY)]);
    expect(results.map((r) => r.status)).toEqual(["fulfilled", "rejected"]);
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("교환 응답의 expires_at을 epoch ms로 돌려준다", async () => {
    const { client } = setup();
    const before = Date.now();
    const { expiresAt } = await client.exchange(KEY);
    expect(expiresAt).toBeGreaterThan(before + 59 * 60 * 1000);
  });

  it("키가 이미 사용됐거나 틀리면 unauthorized로 구분해 알린다", async () => {
    const { gateway } = setup();
    await new GatewayClient({ fetch: gateway.fetch }).exchange(KEY);
    const reuse = await failure(new GatewayClient({ fetch: gateway.fetch }).exchange(KEY));
    expect(reuse).toMatchObject({ kind: "unauthorized", status: 401 });
  });

  it("빈 키는 서버에 보내지 않는다", async () => {
    const { gateway, client } = setup();
    expect((await failure(client.exchange("   "))).kind).toBe("bad_request");
    expect(gateway.calls).toHaveLength(0);
  });

  it("공백·제어문자가 섞인 키는 헤더 주입을 막기 위해 보내지 않는다", async () => {
    const { gateway, client } = setup();
    expect((await failure(client.exchange("abc\r\nX-Evil: 1"))).kind).toBe("bad_request");
    expect(gateway.calls).toHaveLength(0);
  });
});

describe("GatewayClient 데이터 요청", () => {
  it("세션 토큰을 Authorization으로 붙이고 URL·쿠키·저장소에는 남기지 않는다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    await client.listProjects();
    const call = gateway.callsTo("/api/v1/projects")[0]!;
    expect(call.authorization).toMatch(new RegExp(`^Bearer ${SESSION_TOKEN}`));
    expect(call.url).not.toContain(SESSION_TOKEN);
    expect(call.credentials).toBe("omit");
    expect(JSON.stringify({ ...localStorage })).not.toContain(SESSION_TOKEN);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SESSION_TOKEN);
  });

  it("교환 전에는 조회하지 않고, dispose 뒤에는 토큰이 사라져 조회가 막힌다", async () => {
    const { gateway, client } = setup();
    expect((await failure(client.listProjects())).kind).toBe("unauthorized");
    await client.exchange(KEY);
    client.dispose();
    expect(client.hasSession).toBe(false);
    expect((await failure(client.listProjects())).kind).toBe("unauthorized");
    expect(gateway.callsTo("/api/v1/projects")).toHaveLength(0);
  });

  it("프로젝트 목록·메타데이터·스냅샷·문서·전체 맥락·검색을 정규화해 돌려준다", async () => {
    const { client } = setup();
    await client.exchange(KEY);
    const [project] = await client.listProjects();
    expect(project).toMatchObject({ id: PROJECT_A, name: "알파 프로젝트", slug: "alpha" });
    expect((await client.getProject(PROJECT_A)).latestSnapshotId).not.toBeNull();
    const snapshots = await client.listSnapshots(PROJECT_A);
    expect(snapshots.map((s) => s.id)).toHaveLength(2);
    expect(snapshots[1]).toMatchObject({ verifiedAt: null, sourceRevision: "old0001" });
    expect(snapshots[0]!.documents.map((d) => d.id)).toEqual(["overview", "api"]);
    const doc = await client.getDocument(PROJECT_A, "overview", SNAP_A_OLD);
    // 문서 단건 응답에는 출처 필드가 없다(서버 계약). 스냅샷에서 상속하는 것은 mergeProvenance의 몫이다.
    expect(doc).toMatchObject({ id: "overview", title: "개요", sourceRevision: null, verifiedAt: null, sourceRefs: [] });
    expect(doc.content).toContain("이전판");
    const bundle = await client.getContext(PROJECT_A, null);
    expect(bundle.documents).toHaveLength(2);
    expect(bundle.documents[0]!.sourceRefs).toContain("docs/overview.md");
    const result = await client.search(PROJECT_A, "알파", null);
    expect(result.hits[0]).toMatchObject({ id: "overview" });
  });

  it("snapshot_id와 검색어는 쿼리 문자열로 인코딩해 보내고 JSON을 요청한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    await client.search(PROJECT_A, "a&b=c ?", SNAP_A_OLD);
    const call = gateway.callsTo("/search")[0]!;
    const params = new URL(call.url).searchParams;
    expect(params.get("q")).toBe("a&b=c ?");
    expect(params.get("snapshot_id")).toBe(SNAP_A_OLD);
    expect(call.accept).toContain("application/json");
  });

  it("허용 목록 밖의 경로(Private API·로그인·refresh)는 만들 수 없다", () => {
    for (const path of ["/api/auth/login", "/api/auth/refresh", "/api/v1/projects/../../private", "//evil.test/api/v1/projects", "/api/v1/projects/x/unknown"]) {
      expect(() => assertGatewayPath(path), path).toThrow(GatewayError);
    }
    for (const path of ["/api/v1/auth/exchange", "/api/v1/projects", `/api/v1/projects/${PROJECT_A}/documents/overview`, `/api/v1/projects/${PROJECT_A}/export`]) {
      expect(() => assertGatewayPath(path), path).not.toThrow();
    }
  });

  it("프로젝트 id가 UUID 모양이 아니면 경로에 끼워 넣지 않는다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    expect((await failure(client.getProject("../../api/auth/login"))).kind).toBe("misuse");
    expect(gateway.calls).toHaveLength(1);
  });
});

describe("GatewayClient 오류 번역", () => {
  it("교환 뒤 401은 onUnauthorized를 한 번 부르고 토큰을 지운다", async () => {
    const onUnauthorized = vi.fn();
    const { gateway, client } = setup({ onUnauthorized });
    await client.exchange(KEY);
    gateway.expireAllSessions();
    expect((await failure(client.listProjects())).kind).toBe("unauthorized");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(client.hasSession).toBe(false);
  });

  it("404는 권한 없음과 없음을 구분하지 않는 not_found로 전달한다", async () => {
    const { client } = setup();
    await client.exchange(KEY);
    expect(await failure(client.getProject(PROJECT_B))).toMatchObject({ kind: "not_found", status: 404 });
  });

  it("429는 Retry-After 초를 읽어 rate_limited로 전달하고 세션은 유지한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    gateway.failNext("/api/v1/projects", { status: 429, headers: { "retry-after": "60" }, body: { detail: "rate limit exceeded" } });
    expect(await failure(client.listProjects())).toMatchObject({ kind: "rate_limited", retryAfterSeconds: 60 });
    expect(client.hasSession).toBe(true);
  });

  it("fetch 자체가 실패하면 network, 5xx는 server로 구분한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    gateway.failNext("/api/v1/projects", "network");
    expect((await failure(client.listProjects())).kind).toBe("network");
    gateway.failNext("/api/v1/projects", { status: 503 });
    expect((await failure(client.listProjects())).kind).toBe("server");
    gateway.failNext("/api/v1/projects", { status: 409, body: { detail: "snapshot integrity check failed" } });
    expect((await failure(client.listProjects())).kind).toBe("server");
  });

  it("응답 본문 모양이 계약과 다르면 contract 오류로 거절한다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    gateway.failNext("/api/v1/projects", { status: 200, body: { not: "a list" } });
    expect((await failure(client.listProjects())).kind).toBe("contract");
  });

  it("검색·전체 맥락 응답에 요청하지 않은 프로젝트가 섞여 오면 버린다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    gateway.leakProjectInSearch = true;
    expect((await failure(client.search(PROJECT_A, "알파", null))).kind).toBe("contract");
    gateway.leakProjectInContext = true;
    expect((await failure(client.getContext(PROJECT_A, null))).kind).toBe("contract");
  });
});

describe("GatewayClient 다운로드", () => {
  it("Authorization을 붙인 fetch로 받아 Blob과 파일 이름을 돌려준다", async () => {
    const { gateway, client } = setup();
    await client.exchange(KEY);
    const file = await client.download(PROJECT_A, "zip", null);
    const call = gateway.callsTo("/export")[0]!;
    expect(call.authorization).toMatch(/^Bearer /);
    expect(new URL(call.url).searchParams.get("format")).toBe("zip");
    expect(call.url).not.toContain(SESSION_TOKEN);
    expect(file.filename).toMatch(/^alpha-.*\.zip$/);
    expect(file.blob.size).toBe(4);
  });

  it("Content-Disposition이 없으면 형식별 기본 이름을 만든다", async () => {
    const { client } = setup();
    await client.exchange(KEY);
    expect((await client.download(PROJECT_A, "markdown", null)).filename).toBe(`context-${PROJECT_A.slice(0, 8)}.md`);
    expect((await client.download(PROJECT_A, "json", null)).filename).toBe(`context-${PROJECT_A.slice(0, 8)}.json`);
  });

  it("다운로드 중 401도 세션 만료로 처리한다", async () => {
    const onUnauthorized = vi.fn();
    const { gateway, client } = setup({ onUnauthorized });
    await client.exchange(KEY);
    gateway.expireAllSessions();
    expect((await failure(client.download(PROJECT_A, "json", null))).kind).toBe("unauthorized");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
