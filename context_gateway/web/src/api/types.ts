// 화면이 쓰는 정규화된 모양. 서버 응답은 adapters.ts가 이 모양으로 바꾼다(서버 계약이 바뀌면 거기만 고친다).

export interface ProjectSummary {
  id: string;
  slug: string;
  name: string;
  description: string;
  latestSnapshotId: string | null;
}

export interface DocumentMeta {
  id: string;
  title: string;
  contentType: string;
  sha256: string | null;
}

/** 근거 표시에 쓰는 출처 정보. verifiedAt이 null이면 화면에서 "미검증"으로 보인다. */
export interface Provenance {
  sourceRefs: string[];
  sourceRevision: string | null;
  generatedAt: string | null;
  verifiedAt: string | null;
}

export interface SnapshotInfo extends Provenance {
  id: string;
  publishedAt: string | null;
  manifestSha256: string | null;
  documents: DocumentMeta[];
}

export interface GatewayDocument extends DocumentMeta, Provenance {
  content: string;
}

export interface ContextBundle {
  project: ProjectSummary;
  snapshotId: string;
  provenance: Provenance;
  documents: GatewayDocument[];
}

export interface SearchHit {
  id: string;
  title: string;
  snippet: string;
  matches: number;
}

export interface SearchResult {
  projectId: string;
  snapshotId: string;
  query: string;
  hits: SearchHit[];
}

export type ExportFormat = "markdown" | "json" | "zip";

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

export interface ExchangeResult {
  expiresAt: number;
}
