import { useRef, type KeyboardEvent } from "react";
import type { DocumentMeta } from "../api/types";

export type Selection = { kind: "context" } | { kind: "document"; id: string };

export function DocList({ documents, active, onSelect }: { documents: DocumentMeta[]; active: Selection | null; onSelect: (selection: Selection) => void }) {
  const list = useRef<HTMLUListElement>(null);

  // 위·아래 화살표로 목록 안을 오간다. Tab은 목록을 한 번에 지나가도록 그대로 둔다.
  function onKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [...(list.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (index < 0) return;
    event.preventDefault();
    buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
  }

  const isActive = (selection: Selection): boolean => active !== null && active.kind === selection.kind && (selection.kind === "context" || (active.kind === "document" && active.id === selection.id));

  return (
    <nav aria-label="문서 목록" className="doclist">
      <ul ref={list} onKeyDown={onKeyDown}>
        <li>
          <button type="button" className="doc-item doc-item-context" aria-current={isActive({ kind: "context" }) ? "true" : undefined} onClick={() => onSelect({ kind: "context" })}>
            <span className="doc-title">전체 맥락</span>
            <span className="doc-sub">문서 {documents.length}개를 이어서 읽기</span>
          </button>
        </li>
        {documents.map((doc) => {
          const selection: Selection = { kind: "document", id: doc.id };
          return (
            <li key={doc.id}>
              <button type="button" className="doc-item" aria-current={isActive(selection) ? "true" : undefined} onClick={() => onSelect(selection)}>
                <span className="doc-title">{doc.title}</span>
                <span className="doc-sub">{doc.id}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
