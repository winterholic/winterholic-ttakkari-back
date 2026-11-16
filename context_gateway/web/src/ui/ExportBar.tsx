import { useState } from "react";
import type { GatewayClient } from "../api/client";
import type { DownloadedFile, ExportFormat } from "../api/types";
import { downloadExport } from "../download";
import { ProblemPanel } from "./StateViews";
import { problemFor, type Problem } from "./messages";

const FORMATS: { format: ExportFormat; label: string }[] = [
  { format: "markdown", label: "Markdown" },
  { format: "json", label: "JSON" },
  { format: "zip", label: "ZIP" },
];

export function ExportBar({ client, projectId, snapshotId, save, notify }: { client: GatewayClient; projectId: string; snapshotId: string | null; save: (file: DownloadedFile) => void; notify: (message: string) => void }) {
  const [pending, setPending] = useState<ExportFormat | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);

  async function run(format: ExportFormat) {
    if (pending) return;
    setPending(format);
    setProblem(null);
    try {
      const file = await downloadExport(client, projectId, format, snapshotId, save);
      notify(`저장했습니다: ${file.filename}`);
    } catch (error) {
      setProblem(problemFor(error, "export"));
    } finally {
      setPending(null);
    }
  }

  return (
    <fieldset className="export">
      <legend>스냅샷 전체 내려받기</legend>
      <div className="export-buttons">
        {FORMATS.map(({ format, label }) => (
          <button key={format} type="button" className="btn" disabled={pending !== null} aria-busy={pending === format} onClick={() => run(format)}>
            {label} 내려받기
          </button>
        ))}
      </div>
      {pending ? <p className="export-pending">{pending} 파일을 받는 중입니다…</p> : null}
      {problem ? <ProblemPanel problem={problem} compact /> : null}
    </fieldset>
  );
}
