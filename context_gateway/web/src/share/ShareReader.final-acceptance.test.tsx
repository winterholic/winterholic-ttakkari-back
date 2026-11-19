import { act, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ShareReader } from "./ShareReader";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const baseBundle = {
  project: { id: PROJECT, slug: "synthetic", name: "기존 맥락", description: "fixture" },
  snapshot: { id: "snap", published_at: "2026-11-19T01:00:00Z", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null, documents: [{ id: "doc", title: "문서", content_type: "text/markdown" }] },
  documents: [{ id: "doc", title: "문서", content_type: "text/markdown", content: "fixture body", source_refs: ["fixture://public"], source_revision: "rev", generated_at: "2026-11-19T00:00:00Z", verified_at: null }],
  link: { label: "검수", expires_at: "2026-11-22T00:00:00Z", snapshot_policy: "pinned", scopes: ["read"] },
  session_expires_at: "2026-11-19T02:00:00Z",
};

const response = (status: number, body: unknown = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function namedBundle(name: string) {
  return { ...baseBundle, project: { ...baseBundle.project, name } };
}

type ReaderState = {
  cookie: string | null;
  expectedCookie: string | null;
  contentVisible: boolean;
  logoutSucceeded: boolean;
  retryErrorVisible: boolean;
  stopped: boolean;
};

function isSafeReaderState(state: ReaderState): boolean {
  if (state.contentVisible) {
    return !state.stopped && !state.logoutSucceeded && state.cookie === state.expectedCookie;
  }
  if (state.stopped && state.cookie === null && state.logoutSucceeded) return true;
  return !state.logoutSucceeded && state.retryErrorVisible;
}

function currentReaderState(cookie: string | null, expectedCookie: string | null, heading: string, stopped: boolean): ReaderState {
  const alert = screen.queryByRole("alert");
  return {
    cookie,
    expectedCookie,
    contentVisible: screen.queryByRole("heading", { name: heading }) !== null,
    logoutSucceeded: screen.queryByRole("heading", { name: "서버 공유 세션을 종료했습니다" }) !== null,
    retryErrorVisible: alert !== null && within(alert).queryByRole("button", { name: /다시 시도/ }) !== null,
    stopped,
  };
}

describe("공개 Reader 최종 독립 수락: 요청 완료 순서와 쿠키 정합성", () => {
  it("수락 판정은 일치 화면과 fail-closed 오류를 허용하고 권한 불일치와 거짓 성공을 거부한다", () => {
    expect(isSafeReaderState({ cookie: "session-new", expectedCookie: "session-new", contentVisible: true, logoutSucceeded: false, retryErrorVisible: false, stopped: false })).toBe(true);
    expect(isSafeReaderState({ cookie: "session-old", expectedCookie: "session-new", contentVisible: false, logoutSucceeded: false, retryErrorVisible: true, stopped: false })).toBe(true);
    expect(isSafeReaderState({ cookie: null, expectedCookie: null, contentVisible: false, logoutSucceeded: true, retryErrorVisible: false, stopped: true })).toBe(true);
    expect(isSafeReaderState({ cookie: "session-old", expectedCookie: "session-new", contentVisible: true, logoutSucceeded: false, retryErrorVisible: false, stopped: false })).toBe(false);
    expect(isSafeReaderState({ cookie: "session-late", expectedCookie: null, contentVisible: false, logoutSucceeded: true, retryErrorVisible: false, stopped: true })).toBe(false);
  });

  it("지연 exchange와 logout이 정리된 뒤에만 쿠키 삭제 성공 또는 재시도 가능한 오류로 수락한다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedExchange = deferred<Response>();
    let cookie: string | null = "session-initial";
    let logoutRequests = 0;
    const fetcher = vi.fn<typeof fetch>((input) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, baseBundle));
      if (path === "/api/v1/share/exchange") {
        return delayedExchange.promise.then((result) => { cookie = "session-from-late-exchange"; return result; });
      }
      if (path === "/api/v1/share/logout") {
        logoutRequests += 1;
        if (logoutRequests === 1) { cookie = null; return Promise.resolve(response(204)); }
        return Promise.reject(new TypeError("compensating logout unavailable"));
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "기존 맥락" });
    await act(async () => {
      window.location.hash = "token=slow-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url) === "/api/v1/share/exchange")).toBe(true));

    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    await act(async () => { delayedExchange.resolve(response(204)); });
    await waitFor(() => expect(logoutRequests).toBeGreaterThan(0));
    await waitFor(() => expect(isSafeReaderState(currentReaderState(cookie, null, "기존 맥락", true))).toBe(true));
  });

  it("현재 링크 내용은 쿠키와 일치하거나, 불일치 시 오류와 재시도로 fail-closed여야 한다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedLogout = deferred<Response>();
    let cookie: string | null = "session-initial";
    let newLinkExchanges = 0;
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const path = String(input);
      if (path === "/api/v1/share/context") return Promise.resolve(response(200, cookie === "session-new" ? namedBundle("새 링크 맥락") : baseBundle));
      if (path === "/api/v1/share/exchange") {
        newLinkExchanges += 1;
        cookie = "session-new";
        return Promise.resolve(response(204));
      }
      if (path === "/api/v1/share/logout" && init?.method === "POST") {
        return delayedLogout.promise.then((result) => { cookie = null; return result; });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "기존 맥락" });

    await screen.getByRole("button", { name: "이 브라우저에서 나가기" }).click();
    await act(async () => {
      window.location.hash = "token=new-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await act(async () => { delayedLogout.resolve(response(204)); });
    await waitFor(() => expect(isSafeReaderState(currentReaderState(cookie, "session-new", "새 링크 맥락", false))).toBe(true));
    expect(newLinkExchanges).toBeGreaterThan(0);
  });

  it("복구 exchange 실패 뒤에는 새 링크 내용과 구 링크 쿠키를 동시에 허용하지 않는다", async () => {
    window.history.replaceState({}, "", "/s");
    const delayedOldExchange = deferred<Response>();
    let cookie: string | null = "session-initial";
    let newLinkExchanges = 0;
    const exchangeTokens: string[] = [];
    const contextCookies: Array<string | null> = [];
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const path = String(input);
      if (path === "/api/v1/share/exchange") {
        const token = JSON.parse(String(init?.body)).token;
        exchangeTokens.push(token);
        if (token === "old-link") return delayedOldExchange.promise.then((result) => { cookie = "session-old"; return result; });
        newLinkExchanges += 1;
        if (newLinkExchanges === 1) { cookie = "session-new"; return Promise.resolve(response(204)); }
        return Promise.reject(new TypeError("restoring current link unavailable"));
      }
      if (path === "/api/v1/share/context") {
        contextCookies.push(cookie);
        const context = cookie === "session-new" ? namedBundle("새 링크 맥락") : cookie === "session-old" ? namedBundle("구 링크 쿠키 맥락") : baseBundle;
        return Promise.resolve(response(200, context));
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<ShareReader initialToken={null} fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "기존 맥락" });
    await act(async () => {
      window.location.hash = "token=old-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url) === "/api/v1/share/exchange")).toBe(true));
    await act(async () => {
      window.location.hash = "token=new-link";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    await act(async () => { delayedOldExchange.resolve(response(204)); });
    await waitFor(() => expect(isSafeReaderState(currentReaderState(cookie, "session-new", "새 링크 맥락", false))).toBe(true));
    expect(exchangeTokens).toEqual(["old-link", "new-link", "new-link"]);
    expect(contextCookies).toEqual(["session-initial", "session-new"]);
    expect(contextCookies).not.toContain("session-old");
    expect(screen.queryByRole("heading", { name: "새 링크 맥락" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "구 링크 쿠키 맥락" })).toBeNull();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
  });

  it("정상 인증 fixture에서 현재 session 쿠키와 화면이 일치하면 수락한다", async () => {
    window.history.replaceState({}, "", "/s");
    let cookie: string | null = null;
    const fetcher = vi.fn<typeof fetch>((input) => {
      if (String(input) === "/api/v1/share/exchange") { cookie = "session-new"; return Promise.resolve(response(204)); }
      if (String(input) === "/api/v1/share/context") return Promise.resolve(response(200, namedBundle("새 링크 맥락")));
      throw new Error(`unexpected request: ${String(input)}`);
    });
    render(<ShareReader initialToken="new-link" fetcher={fetcher} />);
    await screen.findByRole("heading", { name: "새 링크 맥락" });
    expect(isSafeReaderState(currentReaderState(cookie, "session-new", "새 링크 맥락", false))).toBe(true);
  });

  it("인증 실패 fixture는 보호 내용을 숨기고 재시도 안내를 보여 주면 수락한다", async () => {
    window.history.replaceState({}, "", "/s");
    const fetcher = vi.fn<typeof fetch>(() => Promise.reject(new TypeError("network unavailable")));
    render(<ShareReader initialToken="new-link" fetcher={fetcher} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("공유 서버에 연결하지 못했습니다");
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    expect(isSafeReaderState(currentReaderState("session-old", "session-new", "새 링크 맥락", false))).toBe(true);
  });
});
