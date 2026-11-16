import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown } from "./SafeMarkdown";

function renderMd(source: string) {
  return render(<SafeMarkdown source={source} />).container;
}

describe("SafeMarkdown", () => {
  it("제목·목록·강조·인라인 코드·표·코드 블록을 렌더한다", () => {
    const c = renderMd("# 제목\n\n- 하나\n- 둘\n\n**굵게** 와 `코드`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nconst x = 1;\n```\n");
    expect(c.querySelector("h1")).toHaveTextContent("제목");
    expect(c.querySelectorAll("li")).toHaveLength(2);
    expect(c.querySelector("strong")).toHaveTextContent("굵게");
    expect(c.querySelector("table td")).toHaveTextContent("1");
    expect(c.querySelector("pre code")).toHaveTextContent("const x = 1;");
  });

  it("원시 HTML(script·iframe·이벤트 핸들러)은 요소로 만들지 않고 글자로 보여 준다", () => {
    const c = renderMd('<script>window.__pwned = 1</script>\n\n<img src=x onerror="window.__pwned=1">\n\n<iframe src="https://evil.test"></iframe>\n\n<a href="javascript:alert(1)" onclick="x()">클릭</a>');
    expect(c.querySelector("script, iframe, img, style, object, embed")).toBeNull();
    expect(c.querySelector("[onerror], [onclick]")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(c.textContent).toContain("<script>");
  });

  it("javascript:·data:·vbscript: 링크는 href를 만들지 않는다", () => {
    const c = renderMd("[a](javascript:alert(1)) [b](data:text/html;base64,PHNjcmlwdD4=) [c](vbscript:x) [d](JaVaScRiPt:alert(1))");
    expect(c.querySelectorAll("a[href]")).toHaveLength(0);
    expect(c.textContent).toContain("a");
  });

  it("http(s)·mailto 링크는 새 탭·noopener·noreferrer로 연다", () => {
    const c = renderMd("[문서](https://example.test/a) [메일](mailto:a@example.test)");
    const links = c.querySelectorAll("a");
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
      expect(link.getAttribute("rel")).toContain("noreferrer");
    }
  });

  it("이미지는 외부를 불러오지 않고 대체 텍스트만 보여 준다", () => {
    const c = renderMd("![로고](https://tracker.test/pixel.png)");
    expect(c.querySelector("img")).toBeNull();
    expect(c).toHaveTextContent("로고");
    expect(c).toHaveTextContent("이미지");
  });

  it("코드 블록 안의 HTML도 글자로만 나온다", () => {
    const c = renderMd("```html\n<script>alert(1)</script>\n```");
    expect(c.querySelector("script")).toBeNull();
    expect(c.querySelector("pre code")?.textContent).toContain("<script>alert(1)</script>");
  });

  it("빈 문서도 오류 없이 그린다", () => {
    renderMd("");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
