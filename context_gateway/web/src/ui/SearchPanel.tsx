import { Fragment, useId, useState, type FormEvent } from "react";
import type { GatewayClient } from "../api/client";
import { ProblemPanel, Skeleton } from "./StateViews";
import { problemFor } from "./messages";
import { useAsync } from "./useAsync";

function Highlight({ text, query }: { text: string; query: string }) {
  const lower = text.toLocaleLowerCase();
  const needle = query.toLocaleLowerCase();
  const parts: { at: number; text: string; hit: boolean }[] = [];
  let cursor = 0;
  while (needle !== "") {
    const at = lower.indexOf(needle, cursor);
    if (at < 0) break;
    if (at > cursor) parts.push({ at: cursor, text: text.slice(cursor, at), hit: false });
    parts.push({ at, text: text.slice(at, at + needle.length), hit: true });
    cursor = at + needle.length;
  }
  parts.push({ at: cursor, text: text.slice(cursor), hit: false });
  return (
    <>
      {parts.map((part) => (
        <Fragment key={part.at}>{part.hit ? <mark>{part.text}</mark> : part.text}</Fragment>
      ))}
    </>
  );
}

/**
 * 검색 입력과 결과. 결과가 준비되기 전·후에는 문서 목록 대신 결과를 보여 주고(children이 목록),
 * 오류일 때는 오류와 문서 목록을 함께 둔다.
 */
export function SearchPanel({ client, projectId, snapshotId, onOpen, children }: { client: GatewayClient; projectId: string; snapshotId: string | null; onOpen: (docId: string) => void; children: React.ReactNode }) {
  const inputId = useId();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const { state, reload } = useAsync(query === null ? null : `search:${projectId}:${snapshotId ?? "latest"}:${query}`, (signal) => client.search(projectId, query ?? "", snapshotId, signal));

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = draft.trim();
    if (trimmed !== "") setQuery(trimmed);
  }

  function clear() {
    setQuery(null);
    setDraft("");
  }

  return (
    <>
      <form className="search" role="search" onSubmit={submit}>
        <label htmlFor={inputId} className="field-label">
          문서 검색
        </label>
        <div className="search-row">
          <input id={inputId} className="input" type="search" value={draft} maxLength={300} onChange={(event) => setDraft(event.target.value)} autoComplete="off" spellCheck={false} />
          <button type="submit" className="btn" disabled={draft.trim() === ""}>
            검색
          </button>
        </div>
      </form>
      {query !== null && state.status === "loading" ? <Skeleton label="검색 중" lines={3} /> : null}
      {query !== null && state.status === "error" ? <ProblemPanel problem={problemFor(state.error, "search")} onRetry={reload} compact /> : null}
      {query !== null && state.status === "ready" ? (
        <section className="results" role="region" aria-label="검색 결과">
          <div className="results-head">
            <p>
              “{state.data.query}” · {state.data.hits.length}건
            </p>
            <button type="button" className="btn btn-ghost btn-sm" onClick={clear}>
              검색 지우기
            </button>
          </div>
          {state.data.hits.length === 0 ? (
            <p className="muted">일치하는 문서가 없습니다. 다른 단어로 검색해 보세요.</p>
          ) : (
            <ul>
              {state.data.hits.map((hit) => (
                <li key={hit.id}>
                  <button type="button" className="doc-item" onClick={() => onOpen(hit.id)}>
                    <span className="doc-title">{hit.title}</span>
                    <span className="doc-sub">일치 {hit.matches}회</span>
                    <span className="doc-snippet">
                      <Highlight text={hit.snippet.slice(0, 140)} query={state.data.query} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
      {query !== null && state.status === "error" ? (
        <button type="button" className="btn btn-ghost btn-sm" onClick={clear}>
          검색 지우기
        </button>
      ) : null}
      {query === null || state.status === "error" ? children : null}
    </>
  );
}
