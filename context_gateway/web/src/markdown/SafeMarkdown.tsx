import type { ComponentProps } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// rehype-raw를 쓰지 않으므로 마크다운 안의 HTML은 요소가 아니라 글자로 남는다. 링크와 이미지만 추가로 좁힌다.
const SAFE_LINK = /^(https?:|mailto:)/i;

function SafeLink({ href, children }: ComponentProps<"a">) {
  if (typeof href !== "string" || !SAFE_LINK.test(href.trim())) return <span className="md-dead-link">{children}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  );
}

function BlockedImage({ alt }: ComponentProps<"img">) {
  return <span className="md-blocked-image">이미지(외부 로드 차단): {alt || "대체 텍스트 없음"}</span>;
}

function ScrollTable(props: ComponentProps<"table">) {
  return (
    <div className="md-table-scroll" tabIndex={0} role="region" aria-label="표(가로로 스크롤)">
      <table {...props} />
    </div>
  );
}

const HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;

/** 문서 안의 #은 화면 제목 아래에 놓이므로 offset만큼 내려 화면의 제목 위계를 깨지 않게 한다. */
function shiftedHeading(index: number, offset: number) {
  const Tag = HEADINGS[Math.min(index + offset, HEADINGS.length - 1)]!;
  return function Heading({ children }: ComponentProps<"h1">) {
    return <Tag>{children}</Tag>;
  };
}

export function SafeMarkdown({ source, headingOffset = 0 }: { source: string; headingOffset?: number }) {
  const headings = Object.fromEntries(HEADINGS.map((tag, index) => [tag, shiftedHeading(index, headingOffset)]));
  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={(url) => (SAFE_LINK.test(url.trim()) ? url : "")} components={{ ...headings, a: SafeLink, img: BlockedImage, table: ScrollTable }}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
