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
        <p className="guide-note">프로그램 연결에는 별도로 발급한 reusable_api_v2 키를 raw Bearer로 반복 사용할 수 있습니다. 아래 연결 예시는 키를 환경변수로만 다룹니다. 기존 single_use_v1 키는 한 번 교환해 세션으로 사용하세요. share_link_v2 링크 원문은 REST·MCP Bearer로 사용할 수 없습니다.</p>
        <Snippet label="재사용 API 키 · curl" text={guide.reusableCurl} />
        <Snippet label="재사용 API 키 · MCP" text={guide.reusableMcp} />
        <details>
          <summary>기존 1회용 키 연결</summary>
          <div className="guide-body">
            <Snippet label="1회용 키 · curl 교환" text={guide.curl} />
            <Snippet label="세션 · MCP" text={guide.mcp} />
          </div>
        </details>
      </div>
    </details>
  );
}
