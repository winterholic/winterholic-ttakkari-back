import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ShareReader, consumeShareFragment } from "./ShareReader";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const DOC = "# 승인 맥락\n\n![외부](https://tracker.invalid/p.png)\n\n<script>alert(1)</script>";
const response = (status: number, body: unknown, headers?: HeadersInit) => new Response(status === 204 ? null : JSON.stringify(body), { status, headers });

function context() {
  return {
    project: { id: PROJECT, slug: "synthetic", name: "합성 승인 프로젝트", description: "승인된 요약" },
    snapshot: { id: "snapshot-1", published_at: "2026-11-19T01:00:00Z", source_refs: ["repo://synthetic"], source_revision: "abc1234", generated_at: "2026-11-19T00:00:00Z", verified_at: null, documents: [{ id: "overview", title: "승인 맥락", content_type: "text/markdown" }] },
    documents: [{ id: "overview", title: "승인 맥락", content_type: "text/markdown", content: DOC, source_refs: ["repo://synthetic"], source_revision: "abc1234", generated_at: "2026-11-19T00:00:00Z", verified_at: null }],
    link: { label: "동료 검토", expires_at: "2026-11-22T00:00:00Z", snapshot_policy: "pinned", scopes: ["read", "export"] },
    session_expires_at: "2026-11-19T01:15:00Z",
  };
}

describe("공개 공유 Reader", () => {
  it("fragment 원문을 메모리로 옮기며 history에서 지우고 cookie 교환 뒤 승인 맥락을 표시한다", async () => {
    window.history.replaceState({}, "", "/s#token=synthetic-share-secret");
    const token = consumeShareFragment();
    expect(token).toBe("synthetic-share-secret");
    expect(window.location.href).not.toContain("synthetic-share-secret");
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, context()));
    render(<ShareReader initialToken={token} fetcher={fetcher} />);
    expect(await screen.findByRole("heading", { name: "합성 승인 프로젝트" })).toBeInTheDocument();
    expect(screen.getByText("공유 만료")).toBeInTheDocument();
    expect(screen.getAllByText("repo://synthetic")).toHaveLength(2);
    expect(screen.getByText(/외부 로드 차단/)).toBeInTheDocument();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(document.querySelector("script:not([src]), img, [onerror]")).toBeNull();
    const [url, init] = fetcher.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/share/exchange");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
    expect(init?.referrerPolicy).toBe("no-referrer");
    expect(init?.redirect).toBe("error");
    expect(JSON.parse(String(init?.body))).toEqual({ token: "synthetic-share-secret" });
    const contextInit = fetcher.mock.calls[1]![1];
    expect(contextInit?.credentials).toBe("same-origin");
    expect(contextInit?.cache).toBe("no-store");
    expect(contextInit?.referrerPolicy).toBe("no-referrer");
    expect(contextInit?.redirect).toBe("error");
    expect(new Headers(contextInit?.headers).has("authorization")).toBe(false);
    expect(String(fetcher.mock.calls[1]![0])).toBe("/api/v1/share/context");
    expect(document.body.innerHTML).not.toContain("synthetic-share-secret");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("export scope가 있을 때만 쿠키 인증 fetch로 Markdown을 내려받는다", async () => {
    const save = vi.fn();
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, context()))
      .mockResolvedValueOnce(new Response("# 공유 맥락", { status: 200, headers: { "content-type": "text/markdown" } }));
    render(<ShareReader initialToken="secret" fetcher={fetcher} save={save} />);
    await screen.findByRole("heading", { name: "합성 승인 프로젝트" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Markdown 내려받기" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(String(fetcher.mock.calls[2]![0])).toBe("/api/v1/share/export?format=markdown");
    expect(new Headers(fetcher.mock.calls[2]![1]?.headers).has("authorization")).toBe(false);
  });

  it("401·폐기·잘못된 토큰에서는 API Key 입력 없이 원 링크 재열기를 안내한다", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(401, { detail: "unauthorized" }));
    render(<ShareReader initialToken="bad-secret" fetcher={fetcher} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("공유 링크가 만료되었거나 폐기되었습니다");
    expect(screen.queryByLabelText(/키/)).toBeNull();
    expect(screen.getByText(/원래 공유 링크를 다시 여세요/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("bad-secret");
  });

  it("잘못된 fragment는 교환에 보내지 않고, 일반 저장소나 검색 가능한 URL에 남기지 않는다", () => {
    window.history.replaceState({}, "", "/s#token=bad%0Asecret");
    expect(consumeShareFragment()).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("read-only 링크에는 export 경로를 표시하지 않는다", async () => {
    const readonly = context();
    readonly.link.scopes = ["read"];
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(200, readonly));
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "합성 승인 프로젝트" });
    expect(screen.queryByRole("button", { name: "Markdown 내려받기" })).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "전체 맥락 복사" }));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("export 권한이 취소되어 서버가 403을 반환하면 응답 본문을 노출하지 않는다", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(200, context()))
      .mockResolvedValueOnce(response(403, { detail: "scope denied synthetic-secret" }));
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "합성 승인 프로젝트" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Markdown 내려받기" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("이 공유 링크에는 내려받기 권한이 없습니다");
    expect(document.body.textContent).not.toContain("scope denied");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("쿠키 세션으로 다시 방문하고 로그아웃하면 서버 세션을 폐기한다", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, context()))
      .mockResolvedValueOnce(response(204, {}));
    render(<ShareReader initialToken="logout-secret" fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "합성 승인 프로젝트" });
    await userEvent.setup().click(screen.getByRole("button", { name: "이 브라우저에서 나가기" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");
    expect(screen.queryByRole("button", { name: "다시 시도" })).toBeNull();
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/share/exchange", "/api/v1/share/context", "/api/v1/share/logout"]);
    expect(document.body.innerHTML).not.toContain("logout-secret");
  });

  it("같은 탭에서 원 링크 fragment를 다시 열면 즉시 주소를 정리하고 재교환한다", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(200, context()))
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, context()));
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "합성 승인 프로젝트" });
    window.location.hash = "token=reopened-secret";
    await waitFor(() => expect(String(fetcher.mock.calls[1]?.[0])).toBe("/api/v1/share/exchange"));
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(JSON.parse(String(fetcher.mock.calls[1]![1]?.body))).toEqual({ token: "reopened-secret" });
    expect(document.body.innerHTML).not.toContain("reopened-secret");
  });

  it("site 중단(503)과 네트워크 오류를 비밀 없는 재시도 안내로 표시한다", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(503, { detail: "synthetic failure" }));
    render(<ShareReader initialToken="synthetic-token" fetcher={fetcher} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("공유 맥락을 표시하지 못했습니다");
    expect(document.body.textContent).not.toContain("synthetic-token");
  });

  it("네트워크 실패 후 재시도하면 반복 교환으로 공유 쿠키를 갱신하고 다시 읽는다", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("offline synthetic-secret"))
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, context()));
    render(<ShareReader initialToken="retry-token" fetcher={fetcher} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("공유 서버에 연결하지 못했습니다");
    await userEvent.setup().click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("heading", { name: "합성 승인 프로젝트" })).toBeInTheDocument();
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/v1/share/exchange", "/api/v1/share/exchange", "/api/v1/share/context",
    ]);
    expect(document.body.textContent).not.toContain("offline synthetic-secret");
  });
});
