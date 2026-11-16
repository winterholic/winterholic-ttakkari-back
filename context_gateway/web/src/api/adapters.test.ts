import { describe, expect, it } from "vitest";
import { GatewayError } from "./errors";
import { mergeProvenance, parseTime, toDocument } from "./adapters";
import type { GatewayDocument, SnapshotInfo } from "./types";

const snapshot: SnapshotInfo = {
  id: "s",
  publishedAt: null,
  manifestSha256: null,
  documents: [],
  sourceRefs: ["repo://x@1"],
  sourceRevision: "rev1",
  generatedAt: "2026-11-16T00:00:00Z",
  verifiedAt: null,
};
const bare: GatewayDocument = { id: "d", title: "t", contentType: "text/markdown", sha256: null, content: "c", sourceRefs: [], sourceRevision: null, generatedAt: null, verifiedAt: null };

describe("mergeProvenance", () => {
  it("문서에 출처가 없으면 스냅샷 값을 쓰고, verified_at이 null이면 null로 남긴다", () => {
    expect(mergeProvenance(bare, snapshot)).toMatchObject({ sourceRefs: ["repo://x@1"], sourceRevision: "rev1", verifiedAt: null });
  });

  it("문서가 자기 출처를 가지고 있으면 그것을 우선한다", () => {
    const own = { ...bare, sourceRefs: ["docs/own.md"], sourceRevision: "own", verifiedAt: "2026-11-16T01:00:00Z" };
    expect(mergeProvenance(own, snapshot)).toMatchObject({ sourceRefs: ["docs/own.md"], sourceRevision: "own", verifiedAt: "2026-11-16T01:00:00Z", generatedAt: "2026-11-16T00:00:00Z" });
  });

  it("스냅샷을 모르면 문서를 그대로 둔다", () => {
    expect(mergeProvenance(bare, null)).toEqual(bare);
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
