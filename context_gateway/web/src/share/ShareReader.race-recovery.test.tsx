import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ShareReader } from "./ShareReader";

const bundle = {
  project: { id: "11111111-1111-4111-8111-111111111111", slug: "synthetic", name: "현재 링크 맥락", description: "fixture" },
  snapshot: { id: "snap", published_at: "2026-11-19T01:00:00Z", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null, documents: [{ id: "doc", title: "문서", content_type: "text/markdown" }] },
  documents: [{ id: "doc", title: "문서", content_type: "text/markdown", content: "current body", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null }],
  link: { label: "회귀 fixture", expires_at: "2026-11-22T00:00:00Z", snapshot_policy: "pinned", scopes: ["read"] },
  session_expires_at: "2026-11-19T02:00:00Z",
};

const response = (status: number, body: unknown = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("공개 Reader 인증 경합 복구", () => {
  it("지연 logout이 현재 링크 쿠키를 지우지 않도록 새 방문의 exchange를 순서대로 완료한다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedLogout = deferred<Response>();
    let cookie: string | null = "session-initial";
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, bundle));
      if (path === "/api/v1/share/exchange") {
        cookie = "session-current";
        return Promise.resolve(response(204));
      }
      if (path === "/api/v1/share/logout" && init?.method === "POST") {
        return delayedLogout.promise.then((result) => { cookie = null; return result; });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "현재 링크 맥락" });

    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    await screen.findByText(/서버의 공유 세션 종료를 확인/);
    await act(async () => {
      window.location.hash = "token=current-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await screen.findByRole("heading", { name: "현재 링크 맥락" });

    await act(async () => { delayedLogout.resolve(response(204)); });
    expect(await screen.findByRole("heading", { name: "현재 링크 맥락" })).toBeInTheDocument();
    expect(cookie).toBe("session-current");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/context")).toHaveLength(3);
    expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/exchange")).toHaveLength(2);
  });

  it("구 exchange 뒤 현재 링크 교환 복구가 실패하면 어떤 링크 내용도 표시하지 않고 재시도를 제공한다", async () => {
    window.history.replaceState({}, "", "/s");
    const oldExchange = deferred<Response>();
    let cookie: string | null = "session-initial";
    let currentExchangeCalls = 0;
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, bundle));
      if (path === "/api/v1/share/exchange") {
        const token = JSON.parse(String(init?.body)).token;
        if (token === "old-link") return oldExchange.promise.then((result) => { cookie = "session-old"; return result; });
        currentExchangeCalls += 1;
        if (currentExchangeCalls === 1) { cookie = "session-current"; return Promise.resolve(response(204)); }
        if (currentExchangeCalls === 2) return Promise.reject(new TypeError("current link restore unavailable"));
        cookie = "session-current";
        return Promise.resolve(response(204));
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "현재 링크 맥락" });

    await act(async () => {
      window.location.hash = "token=old-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/exchange")).toHaveLength(1));
    await act(async () => {
      window.location.hash = "token=current-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await screen.findByRole("heading", { name: "현재 링크 맥락" });

    await act(async () => { oldExchange.resolve(response(204)); });
    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(currentExchangeCalls).toBe(2));
    expect(cookie).toBe("session-old");
    expect(alert).toHaveTextContent("공유 서버에 연결하지 못했습니다");
    expect(screen.queryByText("current body")).toBeNull();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    expect(fetcher.mock.calls.filter(([url]) => String(url) === "/api/v1/share/context")).toHaveLength(2);
    await screen.getByRole("button", { name: "다시 시도" }).click();
    expect(await screen.findByRole("heading", { name: "현재 링크 맥락" })).toBeInTheDocument();
    expect(currentExchangeCalls).toBe(3);
    expect(cookie).toBe("session-current");
  });

  it("현재 방문의 exchange 실패는 내용을 숨기고 한 번의 사용자 재시도만 제공한다", async () => {
    window.history.replaceState({}, "", "/s");
    let attempts = 0;
    let cookie: string | null = "session-old";
    const fetcher = vi.fn<typeof fetch>((input) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, bundle));
      if (path === "/api/v1/share/exchange") {
        attempts += 1;
        if (attempts === 1) return Promise.reject(new TypeError("exchange unavailable"));
        cookie = "session-current";
        return Promise.resolve(response(204));
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken="current-link" fetcher={fetcher} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("공유 서버에 연결하지 못했습니다");
    expect(screen.queryByText("current body")).toBeNull();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    expect(attempts).toBe(1);

    await screen.getByRole("button", { name: "다시 시도" }).click();
    expect(await screen.findByRole("heading", { name: "현재 링크 맥락" })).toBeInTheDocument();
    expect(cookie).toBe("session-current");
    expect(attempts).toBe(2);
  });
});
