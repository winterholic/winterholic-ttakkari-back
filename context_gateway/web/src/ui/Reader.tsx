import { useEffect, useRef, useState, type ReactNode } from "react";
import { copyText } from "../clipboard";
import { textFile } from "../download";
import { SafeMarkdown } from "../markdown/SafeMarkdown";
import type { ContextBundle, DownloadedFile, GatewayDocument, Provenance } from "../api/types";
import { ProvenanceBlock } from "./Provenance";
import { ProblemPanel, Skeleton } from "./StateViews";
import type { AsyncState } from "./useAsync";
import { problemFor } from "./messages";

const EXTENSION: Record<string, { ext: string; label: string }> = {
  "text/markdown": { ext: "md", label: "Markdown" },
  "application/json": { ext: "json", label: "JSON" },
  "text/plain": { ext: "txt", label: "텍스트" },
};
const extensionFor = (contentType: string) => EXTENSION[contentType] ?? { ext: "txt", label: "텍스트" };

interface ReaderActions {
  save: (file: DownloadedFile) => void;
  notify: (message: string) => void;
}

function Frame({ title, titleId, focusSignal, onBack, actions, children }: { title: string; titleId: string; focusSignal: number; onBack: () => void; actions: ReactNode; children: ReactNode }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusSignal > 0) heading.current?.focus();
  }, [focusSignal]);
  return (
    <>
      <div className="reader-head">
        <button type="button" className="btn btn-ghost reader-back" onClick={onBack}>
          ← 문서 목록
        </button>
        <h2 className="reader-title" id={titleId} tabIndex={-1} ref={heading}>
          {title}
        </h2>
        <div className="reader-actions">{actions}</div>
      </div>
      {children}
    </>
  );
}

function CopyAction({ text, notify }: { text: string; notify: (message: string) => void }) {
  const [failed, setFailed] = useState(false);
  return (
    <>
      <button
        type="button"
        className="btn"
        onClick={async () => {
          const ok = await copyText(text);
          setFailed(!ok);
          if (ok) notify("복사했습니다");
        }}
      >
        복사
      </button>
      {failed ? (
        <p className="inline-error" role="alert">
          복사하지 못했습니다. 브라우저가 클립보드를 막았습니다. 본문을 직접 선택해 복사하세요.
        </p>
      ) : null}
    </>
  );
}

function Body({ doc, headingOffset }: { doc: Pick<GatewayDocument, "content" | "contentType">; headingOffset: number }) {
  if (doc.contentType === "text/markdown") return <SafeMarkdown source={doc.content} headingOffset={headingOffset} />;
  return (
    <pre className="plain" tabIndex={0}>
      {doc.content}
    </pre>
  );
}

export function DocumentReader({ title, state, provenance, focusSignal, onBack, onRetry, save, notify }: ReaderActions & { title: string; state: AsyncState<GatewayDocument>; provenance: Provenance | null; focusSignal: number; onBack: () => void; onRetry: () => void }) {
  const ready = state.status === "ready" ? state.data : null;
  const format = ready ? extensionFor(ready.contentType) : null;
  return (
    <article className="reader" aria-labelledby="reader-title-doc" aria-busy={state.status === "loading"}>
      <Frame
        title={ready?.title ?? title}
        titleId="reader-title-doc"
        focusSignal={focusSignal}
        onBack={onBack}
        actions={
          ready && format ? (
            <>
              <CopyAction text={ready.content} notify={notify} />
              <button type="button" className="btn" onClick={() => save(textFile(`${ready.id}.${format.ext}`, ready.content, "text/plain;charset=utf-8"))}>
                {format.label}으로 저장
              </button>
            </>
          ) : null
        }
      >
        {state.status === "loading" || state.status === "idle" ? <Skeleton label="문서 불러오는 중" lines={7} /> : null}
        {state.status === "error" ? <ProblemPanel problem={problemFor(state.error, "document")} onRetry={onRetry} /> : null}
        {ready ? (
          <>
            <ProvenanceBlock provenance={provenance} />
            <Body doc={ready} headingOffset={2} />
          </>
        ) : null}
      </Frame>
    </article>
  );
}

function contextMarkdown(bundle: ContextBundle): string {
  const body = bundle.documents.map((doc) => `## ${doc.title}\n\n${doc.content}`).join("\n\n");
  return `# ${bundle.project.name}\n\n${body}\n`;
}

export function ContextReader({ state, focusSignal, onBack, onRetry, save, notify }: ReaderActions & { state: AsyncState<ContextBundle>; focusSignal: number; onBack: () => void; onRetry: () => void }) {
  const ready = state.status === "ready" ? state.data : null;
  return (
    <div className="reader" aria-busy={state.status === "loading"}>
      <Frame
        title="전체 맥락"
        titleId="reader-title-context"
        focusSignal={focusSignal}
        onBack={onBack}
        actions={
          ready ? (
            <>
              <CopyAction text={contextMarkdown(ready)} notify={notify} />
              <button type="button" className="btn" onClick={() => save(textFile(`${ready.project.slug}-context.md`, contextMarkdown(ready), "text/markdown;charset=utf-8"))}>
                Markdown으로 저장
              </button>
            </>
          ) : null
        }
      >
        {state.status === "loading" || state.status === "idle" ? <Skeleton label="전체 맥락 불러오는 중" lines={9} /> : null}
        {state.status === "error" ? <ProblemPanel problem={problemFor(state.error, "context")} onRetry={onRetry} /> : null}
        {ready ? (
          <>
            <p className="reader-lead">
              {ready.project.name} · 문서 {ready.documents.length}개를 한 번에 보여 줍니다.
            </p>
            {ready.documents.map((doc) => (
              <section key={doc.id} className="context-section" aria-labelledby={`ctx-${doc.id}`}>
                <h3 id={`ctx-${doc.id}`} className="context-title">
                  {doc.title}
                </h3>
                <ProvenanceBlock provenance={doc} />
                <Body doc={doc} headingOffset={3} />
              </section>
            ))}
          </>
        ) : null}
      </Frame>
    </div>
  );
}
