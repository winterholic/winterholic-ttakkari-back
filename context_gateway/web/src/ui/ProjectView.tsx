import { useId, useState } from "react";
import type { GatewayClient } from "../api/client";
import type { DownloadedFile, ProjectSummary, SnapshotInfo } from "../api/types";
import { ConnectGuide } from "./ConnectGuide";
import { DocList, type Selection } from "./DocList";
import { ExportBar } from "./ExportBar";
import { ContextReader, DocumentReader } from "./Reader";
import { SearchPanel } from "./SearchPanel";
import { EmptyState, ProblemPanel, Skeleton } from "./StateViews";
import { formatTime, shortId } from "./format";
import { problemFor } from "./messages";
import { useAsync } from "./useAsync";

function snapshotLabel(snapshot: SnapshotInfo, isLatest: boolean): string {
  const revision = snapshot.sourceRevision ? ` · ${snapshot.sourceRevision.slice(0, 12)}` : "";
  return `${formatTime(snapshot.publishedAt)}${revision}${isLatest ? " · 최신" : ""}${snapshot.verifiedAt === null ? " · 미검증" : ""}`;
}

/**
 * 프로젝트 하나의 화면. 부모가 key={project.id}로 걸기 때문에 프로젝트를 바꾸면 이 안의 상태·요청 캐시가 통째로 버려진다.
 */
export function ProjectView({ client, project, save, notify }: { client: GatewayClient; project: ProjectSummary; save: (file: DownloadedFile) => void; notify: (message: string) => void }) {
  const snapshotSelectId = useId();
  const [chosenSnapshot, setChosenSnapshot] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [mobileView, setMobileView] = useState<"list" | "reader">("list");
  const [focusSignal, setFocusSignal] = useState(0);

  const meta = useAsync(`meta:${project.id}`, (signal) => client.getProject(project.id, signal));
  const snapshots = useAsync(`snapshots:${project.id}`, (signal) => client.listSnapshots(project.id, signal));

  const shown = meta.state.status === "ready" ? meta.state.data : project;
  const list = snapshots.state.status === "ready" ? snapshots.state.data : null;
  const fallbackId = list?.some((s) => s.id === project.latestSnapshotId) ? project.latestSnapshotId : (list?.[0]?.id ?? project.latestSnapshotId);
  const snapshotId = chosenSnapshot ?? fallbackId;
  const snapshot = list?.find((s) => s.id === snapshotId) ?? null;
  const documents = snapshot?.documents ?? [];

  const first = documents[0];
  const active: Selection | null =
    selection?.kind === "context" ? selection : selection?.kind === "document" && documents.some((d) => d.id === selection.id) ? selection : first ? { kind: "document", id: first.id } : null;
  const activeDocId = active?.kind === "document" ? active.id : null;
  const activeDoc = documents.find((d) => d.id === activeDocId);

  const doc = useAsync(activeDocId && snapshotId ? `doc:${project.id}:${snapshotId}:${activeDocId}` : null, (signal) => client.getDocument(project.id, activeDocId ?? "", snapshotId, signal));
  const context = useAsync(active?.kind === "context" ? `ctx:${project.id}:${snapshotId ?? "latest"}` : null, (signal) => client.getContext(project.id, snapshotId, signal));

  function open(next: Selection) {
    setSelection(next);
    setMobileView("reader");
    setFocusSignal((value) => value + 1);
  }

  const noSnapshots = list !== null && list.length === 0;

  return (
    <div className="project" data-view={mobileView}>
      <header className="project-head">
        <h1 className="project-title">{shown.name}</h1>
        {shown.description ? <p className="project-desc">{shown.description}</p> : null}
        <p className="project-meta">
          <code>{shown.slug}</code>
          <span aria-hidden="true"> · </span>
          <span>id {shortId(shown.id)}</span>
          {snapshot ? (
            <>
              <span aria-hidden="true"> · </span>
              <span>문서 {documents.length}개</span>
            </>
          ) : null}
        </p>
        {meta.state.status === "error" ? <ProblemPanel problem={problemFor(meta.state.error, "project")} onRetry={meta.reload} compact /> : null}
      </header>

      <aside className="sidebar" aria-label="문서 탐색">
        {list && list.length > 0 ? (
          <div className="field">
            <label htmlFor={snapshotSelectId} className="field-label">
              스냅샷
            </label>
            <select id={snapshotSelectId} className="input" value={snapshotId ?? ""} onChange={(event) => setChosenSnapshot(event.target.value)}>
              {list.map((item) => (
                <option key={item.id} value={item.id}>
                  {snapshotLabel(item, item.id === project.latestSnapshotId)}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        {snapshots.state.status === "loading" ? <Skeleton label="문서 목록 불러오는 중" lines={5} /> : null}
        {snapshots.state.status === "error" ? <ProblemPanel problem={problemFor(snapshots.state.error, "snapshots")} onRetry={snapshots.reload} compact /> : null}
        {noSnapshots ? <EmptyState title="게시된 스냅샷이 없습니다" hint="이 프로젝트에는 아직 읽을 수 있는 스냅샷이 없습니다. 게시되면 새 키로 다시 확인하세요." /> : null}
        {snapshot ? (
          <SearchPanel client={client} projectId={project.id} snapshotId={snapshotId} onOpen={(id) => open({ kind: "document", id })}>
            <DocList documents={documents} active={active} onSelect={open} />
          </SearchPanel>
        ) : null}

        <ExportBar client={client} projectId={project.id} snapshotId={snapshotId} save={save} notify={notify} />
        <ConnectGuide />
      </aside>

      <section className="reading" aria-label="본문">
        {active?.kind === "document" && activeDoc ? (
          <DocumentReader
            title={activeDoc.title}
            state={doc.state}
            provenance={doc.state.status === "ready" ? doc.state.data : null}
            focusSignal={focusSignal}
            onBack={() => setMobileView("list")}
            onRetry={doc.reload}
            save={save}
            notify={notify}
          />
        ) : null}
        {active?.kind === "context" ? <ContextReader state={context.state} focusSignal={focusSignal} onBack={() => setMobileView("list")} onRetry={context.reload} save={save} notify={notify} /> : null}
        {active === null ? (
          <EmptyState
            title={noSnapshots ? "읽을 문서가 없습니다" : "문서를 고르세요"}
            hint={snapshots.state.status === "error" ? "문서 목록을 불러오면 여기에서 읽을 수 있습니다." : noSnapshots ? "게시된 스냅샷이 없어 표시할 내용이 없습니다." : "왼쪽 목록에서 문서를 고르면 여기에 표시됩니다."}
          />
        ) : null}
      </section>
    </div>
  );
}
