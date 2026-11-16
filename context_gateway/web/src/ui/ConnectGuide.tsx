import { useState } from "react";
import { copyText } from "../clipboard";
import { connectionGuide } from "../guide";

function Snippet({ label, text }: { label: string; text: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  return (
    <div className="snippet">
      <div className="snippet-head">
        <span className="snippet-label">{label}</span>
        <button
          type="button"
          className="btn btn-sm"
          aria-label={`${label} 복사`}
          onClick={async () => setState((await copyText(text)) ? "done" : "failed")}
        >
          {state === "done" ? "복사됨" : state === "failed" ? "복사 실패" : "복사"}
        </button>
      </div>
      <pre className="code" tabIndex={0}>
        {text}
      </pre>
    </div>
  );
}

/** 명령 예시에는 환경변수 이름만 들어간다. 이 화면이 아는 키·세션 값은 여기로 넘어오지 않는다. */
export function ConnectGuide() {
  const guide = connectionGuide(window.location.origin);
  return (
    <details className="guide">
      <summary>curl·MCP로 쓰려면</summary>
      <div className="guide-body">
        <p className="guide-note">이 화면에서 쓴 키는 이미 교환돼 다시 쓸 수 없습니다. 다른 도구에는 별도의 새 1회용 키가 필요하고, 키와 세션은 아래처럼 환경변수로만 다룹니다.</p>
        <Snippet label="curl 예시" text={guide.curl} />
        <Snippet label="MCP 설정" text={guide.mcp} />
      </div>
    </details>
  );
}
