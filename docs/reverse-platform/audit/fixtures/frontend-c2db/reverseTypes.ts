/** Reverse API types. V2 fields stay optional so old records remain readable. */

export type ReverseStatus = "queued" | "running" | "waiting" | "partial" | "done" | "failed" | "needs_input";
export type ReverseKind = "reconstruction" | "git_url" | "local_path";
export type ReverseTargetType = "auto" | "cli" | "web" | "api" | "source";
export type ReverseMode = "auto" | "analyze" | "reconstruct" | "analyze_reconstruct";
export type ComparisonResult = "match" | "mismatch" | "unverified";

export interface ReverseStage {
  key: string; label: string; status: "pending" | "running" | "waiting" | "done" | "failed";
  started_at: string | null; finished_at: string | null; evidence: string[]; notes: string[];
}

export interface ReverseRecommendation {
  target_type: ReverseTargetType; mode: ReverseMode; reasons: string[];
}

export interface ReverseComparison {
  id: string; behavior: string; target_type: ReverseTargetType; input: Record<string, unknown>;
  pre_state: Record<string, unknown>; original: Record<string, unknown> | null;
  replica: Record<string, unknown>; environment: Record<string, unknown>;
  evidence_level: "direct" | "observed" | "inferred";
  evidence: { kind: string; path: string; sha256?: string }[];
  verified_at: string | null; run_id: string; result: ComparisonResult;
  difference: Record<string, unknown> | null;
}

export interface ReverseSnapshot {
  source?: { status: string; files?: string[] };
  execution?: { status: string; checks?: { command: string; exit_code?: number | null; log?: string; reason?: string; required?: boolean }[] };
  direct_comparison?: { status: ComparisonResult; count: number; matched: number; mismatched: number; unverified: number };
  independent_verification?: { status: string; run_id?: string | null; verdict?: string | null };
  comparisons?: ReverseComparison[];
}

export interface ReverseV2Result {
  schema_version: 2;
  job?: { title?: string; target_type?: ReverseTargetType; mode?: ReverseMode };
  recommendation?: ReverseRecommendation;
  plan?: string[];
  latest?: ReverseSnapshot | null;
  history?: (ReverseSnapshot & { run_id?: string; created_at?: string; status?: string })[];
}

export interface ReverseSummary {
  id: string; target: string; title?: string | null; target_type?: ReverseTargetType; mode?: ReverseMode;
  recommendation?: ReverseRecommendation; plan?: string[];
  result?: ReverseV2Result | LegacyReverseResult | null;
  latest?: ReverseSnapshot | null;
  independent_verification?: ReverseSnapshot["independent_verification"];
  direct_comparison?: ReverseSnapshot["direct_comparison"];
  kind: ReverseKind; status: ReverseStatus; revision: string | null; stage: string; error: string | null;
  created_at: string; updated_at: string; overview?: string; workspace_id?: string;
  session_id?: string; verifier_session_id?: string | null; output_path?: string;
  progress?: string | null; waiting_reason?: string | null;
}

export interface ReverseArchitecture { title: string; body: string; evidence: string[] }
export interface ReverseModule { path: string; role: string; evidence: string[] }
export interface ReverseFlow { title: string; steps: string[]; evidence: string[] }
export interface ReverseIdea { title: string; body: string; evidence: string[] }
export interface LegacyReverseResult {
  overview: { text: string; evidence: string[] }; architecture: ReverseArchitecture[]; modules: ReverseModule[];
  flows: ReverseFlow[]; ideas: ReverseIdea[]; limitations: string[];
  stats: { files: number; bytes: number; langs: [string, number][]; revision: string | null; files_parsed: number;
    model?: { engine: string; model: string; account?: string; via: string; files_read: number; merged: boolean; masked: number } };
}

export interface ReverseDetail extends ReverseSummary {
  stages: ReverseStage[];
  result: LegacyReverseResult | ReverseV2Result | null;
  reconstruction?: {
    summary: string; source_files: string[]; observations: { finding: string; evidence: string }[];
    hypotheses?: { finding: string; evidence?: string }[]; unknowns?: string[];
    source_preview?: { path: string; content: string }[];
    checks: { command: string; exit_code: number; log: string }[]; limitations: string[]; verification: string;
  } | null;
  rounds?: { id: string; session_id: string; status: string; role: string; result_text: string; error: string | null; engine: string; model: string | null }[];
  activity?: { type: string; at: string; text: string }[];
  outputs?: import("./types").ArtifactOut[];
}

export const isReverseV2Result = (result: ReverseDetail["result"]): result is ReverseV2Result =>
  !!result && "schema_version" in result && result.schema_version === 2;
