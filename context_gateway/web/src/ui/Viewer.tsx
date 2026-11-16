import { useEffect, useId, useRef, useState } from "react";
import type { GatewayClient } from "../api/client";
import type { DownloadedFile } from "../api/types";
import { EmptyState, ProblemPanel, Skeleton } from "./StateViews";
import { minutesLeft } from "./format";
import { problemFor } from "./messages";
import { ProjectView } from "./ProjectView";
import { useAsync } from "./useAsync";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function Viewer({ client, expiresAt, onSignOut, save }: { client: GatewayClient; expiresAt: number; onSignOut: () => void; save: (file: DownloadedFile) => void }) {
  const projectSelectId = useId();
  const now = useNow(30_000);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [flash, setFlash] = useState("");
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const projects = useAsync("projects", (signal) => client.listProjects(signal));

  useEffect(
    () => () => {
      if (flashTimer.current !== null) clearTimeout(flashTimer.current);
    },
    [],
  );

  function notify(message: string) {
    setFlash(message);
    if (flashTimer.current !== null) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlash(""), 4000);
  }

  const list = projects.state.status === "ready" ? projects.state.data : null;
  const project = list?.find((item) => item.id === projectId) ?? list?.[0] ?? null;
  const left = minutesLeft(expiresAt, now);

  return (
    <div className="viewer">
      <header className="appbar">
        <p className="brand">
          Context Gateway <span className="badge badge-neutral">읽기 전용</span>
        </p>
        <p className="session" title={new Date(expiresAt).toLocaleString("ko-KR")}>
          세션 {left > 0 ? `${left}분 남음` : "곧 만료"}
        </p>
        <button type="button" className="btn btn-ghost" onClick={onSignOut}>
          로그아웃
        </button>
      </header>

      {list && list.length > 1 ? (
        <div className="project-switch">
          <label htmlFor={projectSelectId} className="field-label">
            프로젝트
          </label>
          <select id={projectSelectId} className="input" value={project?.id ?? ""} onChange={(event) => setProjectId(event.target.value)}>
            {list.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <main className="viewer-main">
        {projects.state.status === "loading" ? <Skeleton label="프로젝트 불러오는 중" lines={6} /> : null}
        {projects.state.status === "error" ? <ProblemPanel problem={problemFor(projects.state.error, "projects")} onRetry={projects.reload} /> : null}
        {list && list.length === 0 ? <EmptyState title="볼 수 있는 프로젝트가 없습니다" hint="이 키에 허용된 프로젝트가 없거나 모두 폐기됐습니다. 게시자에게 새 키를 요청하세요." /> : null}
        {project ? <ProjectView key={project.id} client={client} project={project} save={save} notify={notify} /> : null}
      </main>

      <div className="flash" role="status">
        {flash}
      </div>
    </div>
  );
}
