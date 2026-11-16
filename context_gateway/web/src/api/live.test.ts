// 실제 Gateway에 대한 어댑터 대조. 환경변수가 없으면 건너뛴다(npm test 기본 실행에는 영향 없음).
// 실행: scripts/live/start-gateway.sh && python3 scripts/live/seed.py && GATEWAY_LIVE_URL=http://127.0.0.1:18787 npx vitest run src/api/live.test.ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { GatewayClient } from "./client";

const base = process.env["GATEWAY_LIVE_URL"];
const keysFile = process.env["GATEWAY_LIVE_KEYS_FILE"] ?? "/tmp/ctx-gw-live/keys.json";

describe.skipIf(!base)("실제 Gateway 응답 대조", () => {
  const keys = (): Record<string, string> => JSON.parse(readFileSync(keysFile, "utf8")) as Record<string, string>;
  const client = () => new GatewayClient({ fetch: (input, init) => fetch(`${base}${String(input)}`, init) });

  it("교환→목록→메타데이터→스냅샷→전체 맥락→문서→검색→내보내기 3종을 어댑터로 읽는다", async () => {
    const k = keys();
    const c = client();
    const { expiresAt } = await c.exchange(k["alpha"]!);
    expect(expiresAt).toBeGreaterThan(Date.now());
    expect(expiresAt).toBeLessThanOrEqual(Date.now() + 60 * 60 * 1000 + 5000);

    const projects = await c.listProjects();
    expect(projects.map((p) => p.slug)).toEqual(["synthetic-alpha"]);
    const project = projects[0]!;
    expect(project.id).toBe(k["alphaProject"]);
    expect((await c.getProject(project.id)).latestSnapshotId).toBe(project.latestSnapshotId);

    const snapshots = await c.listSnapshots(project.id);
    expect(snapshots.length).toBeGreaterThanOrEqual(2);
    expect(snapshots[0]!.id).toBe(project.latestSnapshotId);
    expect(snapshots[0]).toMatchObject({ sourceRevision: "rev-2" });
    expect(snapshots[0]!.sourceRefs.length).toBeGreaterThan(0);
    expect(snapshots[0]!.generatedAt).not.toBeNull();
    expect(snapshots[0]!.verifiedAt).not.toBeNull();
    expect(snapshots[0]!.documents.map((d) => d.id)).toEqual(["overview", "config"]);
    expect(snapshots[0]!.documents.map((d) => d.contentType)).toEqual(["text/markdown", "application/json"]);

    const bundle = await c.getContext(project.id, null);
    expect(bundle.snapshotId).toBe(snapshots[0]!.id);
    expect(bundle.documents).toHaveLength(2);
    expect(bundle.documents[0]).toMatchObject({ sourceRevision: "rev-2" });

    const doc = await c.getDocument(project.id, "overview", snapshots[0]!.id);
    expect(doc.content).toContain("개요 v2");
    expect(doc.sourceRefs).toEqual([]); // 서버는 문서 단건에 출처를 주지 않는다 → 스냅샷에서 상속

    const found = await c.search(project.id, "알파", null);
    expect(found.hits.map((h) => h.id)).toEqual(["overview"]);

    const md = await c.download(project.id, "markdown", null);
    expect(md.filename).toBe(`context-${project.id.slice(0, 8)}.md`);
    expect(await md.blob.text()).toContain("# Approved overview");
    expect(JSON.parse(await (await c.download(project.id, "json", null)).blob.text()).documents).toHaveLength(2);
    const zip = await c.download(project.id, "zip", null);
    expect(zip.filename).toBe(`synthetic-alpha-${snapshots[0]!.id}.zip`);
    expect(new Uint8Array(await zip.blob.arrayBuffer()).slice(0, 2)).toEqual(new Uint8Array([0x50, 0x4b]));
  });

  it("과거 스냅샷 id로 이전 판을 읽는다", async () => {
    const k = keys();
    const c = client();
    await c.exchange(k["alpha2"]!);
    const project = (await c.listProjects())[0]!;
    const old = (await c.listSnapshots(project.id)).find((s) => s.sourceRevision === "rev-1")!;
    const doc = await c.getDocument(project.id, "overview", old.id);
    expect(doc.content).toContain("<script>");
    expect(doc.contentType).toBe("text/markdown");
  });

  it("재사용한 키·권한 없는 프로젝트·scope 부족을 계약대로 번역한다", async () => {
    const k = keys();
    const reused = client();
    await reused.exchange(k["alpha3"]!);
    await expect(client().exchange(k["alpha3"]!)).rejects.toMatchObject({ kind: "unauthorized" });
    await expect(reused.getProject(k["betaProject"]!)).rejects.toMatchObject({ kind: "not_found" });

    const ro = client();
    await ro.exchange(k["readonly"]!);
    const id = k["alphaProject"]!;
    await expect(ro.search(id, "a", null)).rejects.toMatchObject({ kind: "not_found" });
    await expect(ro.download(id, "zip", null)).rejects.toMatchObject({ kind: "not_found" });
    expect((await ro.getContext(id, null)).documents.length).toBeGreaterThan(0);
  });

  it("두 프로젝트 키는 각자의 문서만 돌려주고 섞지 않는다", async () => {
    const k = keys();
    const c = client();
    await c.exchange(k["both"]!);
    const projects = await c.listProjects();
    expect(projects.map((p) => p.slug)).toEqual(["synthetic-alpha", "synthetic-beta"]);
    const beta = await c.getContext(k["betaProject"]!, null);
    expect(JSON.stringify(beta)).toContain("BETA-ONLY-TEXT");
    const alpha = await c.getContext(k["alphaProject"]!, null);
    expect(JSON.stringify(alpha)).not.toContain("BETA-ONLY-TEXT");
  });
});
