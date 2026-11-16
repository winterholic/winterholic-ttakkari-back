import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayClient } from "../api/client";
import { FakeGateway, KEY, SESSION_TOKEN } from "../test/fakeGateway";
import { SessionStore } from "./sessionStore";

function setup() {
  const gateway = new FakeGateway();
  const store = new SessionStore({ createClient: (hooks) => new GatewayClient({ fetch: gateway.fetch, ...hooks }) });
  return { gateway, store };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
});
afterEach(() => {
  vi.useRealTimers();
});

describe("SessionStore", () => {
  it("처음에는 로그아웃 상태이고 클라이언트가 없다", () => {
    const { store } = setup();
    expect(store.getState()).toEqual({ status: "signed_out", notice: null });
    expect(store.client).toBeNull();
  });

  it("키를 교환하면 active가 되고 키 교환 요청은 한 번뿐이다", async () => {
    const { gateway, store } = setup();
    await store.signIn(KEY);
    expect(store.getState().status).toBe("active");
    expect(store.client?.hasSession).toBe(true);
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("교환 중이거나 active일 때 다시 signIn해도 교환을 더 보내지 않는다", async () => {
    const { gateway, store } = setup();
    const first = store.signIn(KEY);
    await store.signIn(KEY);
    await first;
    await store.signIn(KEY);
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("이미 쓴 키는 rejected 안내와 함께 로그아웃 상태로 돌아간다", async () => {
    const { store, gateway } = setup();
    gateway.keys.get(KEY)!.used = true;
    await store.signIn(KEY);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "rejected" } });
    expect(store.client).toBeNull();
  });

  it("네트워크 오류·429·잘못된 입력을 각각 구분한 안내로 남긴다", async () => {
    const { store, gateway } = setup();
    gateway.failNext("/auth/exchange", "network");
    await store.signIn(KEY);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "network" } });
    gateway.failNext("/auth/exchange", { status: 429, headers: { "retry-after": "60" } });
    await store.signIn(KEY);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "rate_limited", retryAfterSeconds: 60 } });
    await store.signIn("   ");
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "invalid_input" } });
  });

  it("세션 만료 시각이 되면 클라이언트를 버리고 expired 안내로 초기화한다", async () => {
    const { store } = setup();
    await store.signIn(KEY);
    const client = store.client!;
    const listener = vi.fn();
    store.subscribe(listener);
    vi.advanceTimersByTime(60 * 60 * 1000 + 1);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "expired" } });
    expect(store.client).toBeNull();
    expect(client.hasSession).toBe(false);
    expect(listener).toHaveBeenCalled();
  });

  it("만료 직전에는 유지한다", async () => {
    const { store } = setup();
    await store.signIn(KEY);
    vi.advanceTimersByTime(60 * 60 * 1000 - 1000);
    expect(store.getState().status).toBe("active");
  });

  it("데이터 요청이 401을 받으면 즉시 expired로 초기화한다", async () => {
    const { store, gateway } = setup();
    await store.signIn(KEY);
    gateway.expireAllSessions();
    await store.client!.listProjects().catch(() => undefined);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "expired" } });
    expect(store.client).toBeNull();
  });

  it("signOut은 토큰을 지우고 이후 요청을 막는다", async () => {
    const { store, gateway } = setup();
    await store.signIn(KEY);
    const client = store.client!;
    store.signOut();
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "signed_out" } });
    expect(client.hasSession).toBe(false);
    await expect(client.listProjects()).rejects.toMatchObject({ kind: "unauthorized" });
    expect(gateway.callsTo("/api/v1/projects")).toHaveLength(0);
  });

  it("다시 로그인하면 새 sessionId가 부여되어 이전 화면 상태를 재사용하지 않게 한다", async () => {
    const { store, gateway } = setup();
    await store.signIn(KEY);
    const first = store.getState();
    store.signOut();
    gateway.keys.set("synthetic-key-again", { used: false, projects: [], scopes: ["read"] });
    await store.signIn("synthetic-key-again");
    const second = store.getState();
    expect(first.status === "active" && second.status === "active" && first.sessionId !== second.sessionId).toBe(true);
  });

  it("키와 세션 토큰은 저장소에 쓰이지 않는다", async () => {
    const { store } = setup();
    await store.signIn(KEY);
    const dump = JSON.stringify({ ...localStorage, ...sessionStorage });
    expect(dump).not.toContain(KEY);
    expect(dump).not.toContain(SESSION_TOKEN);
    expect(location.href).not.toContain(KEY);
  });

  it("만료가 지난 시각이 교환 응답으로 오면 곧바로 expired로 처리한다", async () => {
    const { store, gateway } = setup();
    gateway.sessionTtlMs = -1000;
    await store.signIn(KEY);
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "expired" } });
  });

  it("타이머가 늦게 돌아도 checkExpiry가 만료를 잡아낸다", async () => {
    const { store } = setup();
    await store.signIn(KEY);
    vi.setSystemTime(Date.now() + 61 * 60 * 1000);
    store.checkExpiry();
    expect(store.getState()).toEqual({ status: "signed_out", notice: { kind: "expired" } });
  });
});
