import { describe, expect, it } from "vitest";
import { GatewayError } from "./errors";
import { parseTime, toContext, toDocument } from "./adapters";

describe("문서 provenance", () => {
  const contextRaw = (docs: unknown[]) => ({
    project: { id: "p", slug: "p", name: "Project", description: "", latest_snapshot_id: "s" },
    snapshot: { id: "s", source_refs: ["snapshot-ref"], source_revision: "snapshot-rev", generated_at: "2026-11-16T00:00:00Z", verified_at: "2026-11-16T00:30:00Z", documents: [] },
    documents: docs,
  });

  it("문서 verified_at null과 미지정은 verified snapshot이어도 미상으로 남는다", () => {
    const bundle = toContext(contextRaw([
      { id: "a", title: "A", content: "a", source_refs: ["doc-a-ref"], source_revision: "doc-a-rev", generated_at: "2026-11-16T00:10:00Z", verified_at: "2026-11-16T00:20:00Z" },
      { id: "b-null", title: "B null", content: "b", source_refs: [], source_revision: null, generated_at: null, verified_at: null },
      { id: "b-omitted", title: "B omitted", content: "b" },
    ]), "p", null);

    expect(bundle.provenance.verifiedAt).toBe("2026-11-16T00:30:00Z");
    expect(bundle.documents.map((doc) => doc.verifiedAt)).toEqual(["2026-11-16T00:20:00Z", null, null]);
    expect(bundle.documents[1]).toMatchObject({ sourceRefs: [], sourceRevision: null, generatedAt: null });
    expect(bundle.documents[2]).toMatchObject({ sourceRefs: [], sourceRevision: null, generatedAt: null });
    expect(bundle.documents[0]).toMatchObject({ sourceRefs: ["doc-a-ref"], sourceRevision: "doc-a-rev" });
  });
});

describe("parseTime", () => {
  it("시간대 표기가 없는 서버 시각은 UTC로 읽는다", () => {
    expect(parseTime("2026-11-16T00:00:00")).toBe(Date.parse("2026-11-16T00:00:00Z"));
    expect(parseTime("2026-11-16T09:00:00+09:00")).toBe(Date.parse("2026-11-16T00:00:00Z"));
  });

  it("읽을 수 없으면 계약 오류다", () => {
    expect(() => parseTime("not a date")).toThrow(GatewayError);
  });
});

describe("toDocument", () => {
  it("content가 문자열이 아니면 계약 오류다", () => {
    expect(() => toDocument({ id: "a", title: "b", content_type: "text/markdown", content: 1 })).toThrow(GatewayError);
  });
});
