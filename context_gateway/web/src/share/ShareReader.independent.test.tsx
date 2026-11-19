import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ShareReader } from "./ShareReader";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const bundle = {
  project: { id: PROJECT, slug: "synthetic", name: "최신 합성 맥락", description: "fresh" },
  snapshot: { id: "snap", published_at: "2026-11-19T01:00:00Z", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null, documents: [{ id: "doc", title: "문서", content_type: "text/markdown" }] },
  documents: [{ id: "doc", title: "문서", content_type: "text/markdown", content: "fresh body", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null }],
  link: { label: "검수", expires_at: "2026-11-22T00:00:00Z", snapshot_policy: "pinned", scopes: ["read"] },
  session_expires_at: "2026-11-19T02:00:00Z",
};

const response = (status: number, body: unknown) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("공개 Reader 독립 지연 반례", () => {
  it("같은 document에서 같은 원 링크 fragment를 연속 두 번 열면 각 방문마다 다시 교환하고 최신 context를 읽는다", async () => {
    window.history.replaceState({}, "", "/s");
    const refreshed = (name: string, body: string) => ({
      ...bundle,
      project: { ...bundle.project, name },
      documents: [{ ...bundle.documents[0], content: body }],
    });
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(200, bundle))
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, refreshed("첫 재개방 맥락", "first refresh")))
      .mockResolvedValueOnce(response(204, {}))
      .mockResolvedValueOnce(response(200, refreshed("두 번째 재개방 맥락", "second refresh")));
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });

    for (let i = 0; i < 2; i += 1) {
      await act(async () => {
        window.location.hash = "token=same-original-link";
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      });
      await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/exchange")).toHaveLength(i + 1));
      await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/context")).toHaveLength(i + 2));
    }
    expect(await screen.findByRole("heading", { name: "두 번째 재개방 맥락" })).toBeInTheDocument();
    expect(screen.getByText("second refresh")).toBeInTheDocument();
  });

  it("다른 링크 context 요청 중 더 새 링크를 열면 늦은 응답이 최신 화면을 덮지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const earlierContext = deferred<Response>();
    let contextCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path === "/api/v1/share/exchange") return response(204, {});
      contextCalls += 1;
      if (contextCalls === 1) return response(200, bundle);
      if (contextCalls === 2) return earlierContext.promise;
      return response(200, { ...bundle, project: { ...bundle.project, name: "더 새 링크 맥락" } });
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });

    await act(async () => {
      window.location.hash = "token=other-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/context")).toHaveLength(2));
    await act(async () => {
      window.location.hash = "token=newer-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
    expect(await screen.findByRole("heading", { name: "더 새 링크 맥락" })).toBeInTheDocument();

    await act(async () => { earlierContext.resolve(response(200, bundle)); });
    expect(screen.getByRole("heading", { name: "더 새 링크 맥락" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
  });

  it("서버 logout 실패를 세션 종료 성공으로 안내하지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(200, bundle))
      .mockRejectedValueOnce(new TypeError("logout unavailable"))
      .mockResolvedValueOnce(response(204, {}));
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });
    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    expect(await screen.findByRole("alert")).toHaveTextContent("화면을 비웠지만 서버 세션은 종료되지 않았습니다");
    expect(screen.getByRole("alert")).toHaveTextContent("서버 쿠키");
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
    expect(screen.getByRole("button", { name: "서버 로그아웃 다시 시도" })).toBeInTheDocument();
    await screen.getByRole("button", { name: "서버 로그아웃 다시 시도" }).click();
    expect(await screen.findByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");
  });

  it("늦은 이전 링크 exchange가 새 링크 쿠키를 덮으면 현재 링크로 다시 맞추고 이전 context를 읽지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const oldExchange = deferred<Response>();
    let cookie: string | null = "session-initial";
    const exchangeTokens: string[] = [];
    const contextCookies: Array<string | null> = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      if (path === "/api/v1/share/exchange") {
        const token = JSON.parse(String(init?.body)).token;
        exchangeTokens.push(token);
        if (token === "old-link") return oldExchange.promise.then((result) => { cookie = "session-old"; return result; });
        cookie = "session-new";
        return response(204, {});
      }
      if (path === "/api/v1/share/context") {
        contextCookies.push(cookie);
        if (cookie === "session-initial") return response(200, bundle);
        if (cookie === "session-new") return response(200, { ...bundle, project: { ...bundle.project, name: "새 링크 맥락" } });
        return response(200, { ...bundle, project: { ...bundle.project, name: "이전 권한 맥락" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });

    await act(async () => {
      window.location.hash = "token=old-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/exchange")).toHaveLength(1));
    await act(async () => {
      window.location.hash = "token=new-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(await screen.findByRole("heading", { name: "새 링크 맥락" })).toBeInTheDocument();

    await act(async () => { oldExchange.resolve(response(204, {})); });
    await waitFor(() => expect(contextCookies).toHaveLength(3));
    expect(exchangeTokens).toEqual(["old-link", "new-link", "new-link"]);
    expect(contextCookies).toEqual(["session-initial", "session-new", "session-new"]);
    expect(contextCookies).not.toContain("session-old");
    expect(cookie).toBe("session-new");
    expect(screen.getByRole("heading", { name: "새 링크 맥락" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "이전 권한 맥락" })).toBeNull();
  });

  it("logout 뒤 완료된 지연 exchange는 쿠키를 다시 폐기하고 context를 불러오지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedExchange = deferred<Response>();
    let contextCalls = 0;
    const fetcher = vi.fn<typeof fetch>((input) => {
      const path = String(input);
      if (path === "/api/v1/share/context") {
        contextCalls += 1;
        return Promise.resolve(response(200, bundle));
      }
      if (path === "/api/v1/share/exchange") return delayedExchange.promise;
      if (path === "/api/v1/share/logout") return Promise.resolve(response(204, {}));
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });

    await act(async () => {
      window.location.hash = "token=slow-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/exchange")).toHaveLength(1));
    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    expect(await screen.findByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");

    await act(async () => { delayedExchange.resolve(response(204, {})); });
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/logout")).toHaveLength(2));
    expect(contextCalls).toBe(1);
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");
  });

  it("새 링크를 연 뒤 늦게 끝난 export 응답은 다운로드하지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedExport = deferred<Response>();
    const save = vi.fn();
    let contextCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path === "/api/v1/share/context") {
        contextCalls += 1;
        return response(200, contextCalls === 1 ? { ...bundle, link: { ...bundle.link, scopes: ["read", "export"] } } : { ...bundle, project: { ...bundle.project, name: "새 링크 맥락" } });
      }
      if (path === "/api/v1/share/export?format=markdown") return delayedExport.promise;
      if (path === "/api/v1/share/exchange") return response(204, {});
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} save={save} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });
    await screen.getByRole("button", { name: "Markdown 내려받기" }).click();
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url) === "/api/v1/share/export?format=markdown")).toBe(true));

    await act(async () => {
      window.location.hash = "token=new-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(await screen.findByRole("heading", { name: "새 링크 맥락" })).toBeInTheDocument();
    await act(async () => { delayedExport.resolve(new Response("stale markdown", { status: 200 })); });
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "새 링크 맥락" })).toBeInTheDocument();
  });

  it("logout 뒤 늦게 끝난 export 응답은 다운로드하지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedExport = deferred<Response>();
    const save = vi.fn();
    const fetcher = vi.fn<typeof fetch>((input) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, { ...bundle, link: { ...bundle.link, scopes: ["read", "export"] } }));
      if (path === "/api/v1/share/export?format=markdown") return delayedExport.promise;
      if (path === "/api/v1/share/logout") return Promise.resolve(response(204, {}));
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} save={save} />);
    await screen.findByRole("heading", { name: "최신 합성 맥락" });
    await screen.getByRole("button", { name: "Markdown 내려받기" }).click();
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url) === "/api/v1/share/export?format=markdown")).toBe(true));
    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    expect(await screen.findByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");

    await act(async () => { delayedExport.resolve(new Response("stale markdown", { status: 200 })); });
    expect(save).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "최신 합성 맥락" })).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("서버 공유 세션을 종료했습니다");
  });
});
