import type { ReactNode } from "react";
import type { Problem } from "./messages";

export function Skeleton({ label, lines = 4 }: { label: string; lines?: number }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className="skeleton-line" style={{ inlineSize: `${index === lines - 1 ? 55 : 100 - (index % 3) * 8}%` }} />
      ))}
    </div>
  );
}

export function ProblemPanel({ problem, onRetry, compact = false }: { problem: Problem; onRetry?: () => void; compact?: boolean }) {
  return (
    <div className={compact ? "problem problem-compact" : "problem"} role="alert">
      <p className="problem-scope">{problem.scope}</p>
      <p className="problem-title">{problem.title}</p>
      <p className="problem-hint">{problem.hint}</p>
      {problem.retry && onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          다시 시도
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ title, hint, children }: { title: string; hint: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h2 className="empty-title">{title}</h2>
      <p className="empty-hint">{hint}</p>
      {children}
    </div>
  );
}
