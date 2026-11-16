// 서버 응답(apps.py 기준, 실서버로 검증하지 않음)을 화면 모양으로 바꾼다. 모양이 다르면 GatewayError("contract")로 거절한다.
import { GatewayError } from "./errors";
import type { ContextBundle, DocumentMeta, GatewayDocument, ProjectSummary, Provenance, SearchHit, SearchResult, SnapshotInfo } from "./types";

type Rec = Record<string, unknown>;

const contract = (what: string): GatewayError => new GatewayError("contract", `응답 형식이 계약과 다름: ${what}`);

export function asRecord(value: unknown, what: string): Rec {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw contract(what);
  return value as Rec;
}

export function asArray(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw contract(what);
  return value;
}

function str(rec: Rec, key: string, what: string): string {
  const value = rec[key];
  if (typeof value !== "string") throw contract(`${what}.${key}`);
  return value;
}

function optStr(rec: Rec, key: string): string | null {
  const value = rec[key];
  return typeof value === "string" && value !== "" ? value : null;
}

/** 서버 시각은 시간대 표기가 빠질 수 있다(DB가 naive datetime을 돌려주는 경우). 그때는 UTC로 읽는다. */
export function parseTime(value: string): number {
  const normalized = /(Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const ms = Date.parse(normalized);
  if (Number.isNaN(ms)) throw contract(`시각 ${value}`);
  return ms;
}

export function toProject(raw: unknown): ProjectSummary {
  const rec = asRecord(raw, "project");
  return {
    id: str(rec, "id", "project"),
    slug: str(rec, "slug", "project"),
    name: str(rec, "name", "project"),
    description: typeof rec["description"] === "string" ? rec["description"] : "",
    latestSnapshotId: optStr(rec, "latest_snapshot_id"),
  };
}

function toProvenance(rec: Rec): Provenance {
  const refs = Array.isArray(rec["source_refs"]) ? rec["source_refs"].filter((v): v is string => typeof v === "string") : [];
  return {
    sourceRefs: refs,
    sourceRevision: optStr(rec, "source_revision"),
    generatedAt: optStr(rec, "generated_at"),
    verifiedAt: optStr(rec, "verified_at"),
  };
}

function toMeta(raw: unknown): DocumentMeta {
  const rec = asRecord(raw, "document");
  return { id: str(rec, "id", "document"), title: str(rec, "title", "document"), contentType: optStr(rec, "content_type") ?? "text/markdown", sha256: optStr(rec, "sha256") };
}

export function toDocument(raw: unknown): GatewayDocument {
  const rec = asRecord(raw, "document");
  return { ...toMeta(rec), ...toProvenance(rec), content: str(rec, "content", "document") };
}

export function toSnapshot(raw: unknown): SnapshotInfo {
  const rec = asRecord(raw, "snapshot");
  return {
    id: str(rec, "id", "snapshot"),
    publishedAt: optStr(rec, "published_at"),
    manifestSha256: optStr(rec, "manifest_sha256"),
    documents: asArray(rec["documents"], "snapshot.documents").map(toMeta),
    ...toProvenance(rec),
  };
}

export function toContext(raw: unknown, requestedProjectId: string, requestedSnapshotId: string | null): ContextBundle {
  const rec = asRecord(raw, "context");
  const project = toProject(rec["project"]);
  const snapshot = toSnapshot(rec["snapshot"]);
  if (project.id !== requestedProjectId) throw contract("다른 프로젝트의 맥락");
  if (requestedSnapshotId && snapshot.id !== requestedSnapshotId) throw contract("다른 스냅샷의 맥락");
  const provenance: Provenance = { sourceRefs: snapshot.sourceRefs, sourceRevision: snapshot.sourceRevision, generatedAt: snapshot.generatedAt, verifiedAt: snapshot.verifiedAt };
  const documents = asArray(rec["documents"], "context.documents").map(toDocument);
  return { project, snapshotId: snapshot.id, provenance, documents };
}

export function toSearch(raw: unknown, requestedProjectId: string): SearchResult {
  const rec = asRecord(raw, "search");
  if (str(rec, "project_id", "search") !== requestedProjectId) throw contract("다른 프로젝트의 검색 결과");
  const hits: SearchHit[] = asArray(rec["results"], "search.results").map((item) => {
    const hit = asRecord(item, "search.result");
    return { id: str(hit, "id", "hit"), title: str(hit, "title", "hit"), snippet: typeof hit["snippet"] === "string" ? hit["snippet"] : "", matches: typeof hit["matches"] === "number" ? hit["matches"] : 0 };
  });
  return { projectId: requestedProjectId, snapshotId: str(rec, "snapshot_id", "search"), query: typeof rec["query"] === "string" ? rec["query"] : "", hits };
}
