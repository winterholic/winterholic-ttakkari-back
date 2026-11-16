import type { GatewayClient } from "../api/client";
import { isGatewayError } from "../api/errors";

export type AuthNotice = { kind: "expired" | "rejected" | "network" | "rate_limited" | "invalid_input" | "server" | "signed_out"; retryAfterSeconds?: number };
export type AuthState = { status: "signed_out"; notice: AuthNotice | null } | { status: "exchanging" } | { status: "active"; sessionId: number; expiresAt: number };

export interface SessionStoreOptions {
  createClient: (hooks: { onUnauthorized: () => void }) => GatewayClient;
  now?: () => number;
}

const SIGNED_OUT: AuthState = { status: "signed_out", notice: null };

/**
 * 키·세션의 유일한 보관처. 값은 이 객체의 메모리(와 GatewayClient의 private 필드)에만 있다.
 * 로그아웃·만료·401이면 클라이언트를 버리고 상태를 signed_out으로 되돌려, 화면 트리(sessionId로 key를 건다)가 통째로 사라지게 한다.
 */
export class SessionStore {
  readonly #createClient: SessionStoreOptions["createClient"];
  readonly #now: () => number;
  readonly #listeners = new Set<() => void>();
  #state: AuthState = SIGNED_OUT;
  #client: GatewayClient | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #seq = 0;

  constructor(options: SessionStoreOptions) {
    this.#createClient = options.createClient;
    this.#now = options.now ?? (() => Date.now());
  }

  getState = (): AuthState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  get client(): GatewayClient | null {
    return this.#state.status === "active" ? this.#client : null;
  }

  async signIn(rawKey: string): Promise<void> {
    if (this.#state.status !== "signed_out") return;
    this.#set({ status: "exchanging" });
    const client = this.#createClient({ onUnauthorized: () => this.#end(client, { kind: "expired" }) });
    try {
      const { expiresAt } = await client.exchange(rawKey);
      if (expiresAt <= this.#now()) {
        client.dispose();
        this.#set({ status: "signed_out", notice: { kind: "expired" } });
        return;
      }
      this.#client = client;
      this.#set({ status: "active", sessionId: ++this.#seq, expiresAt });
      this.#timer = setTimeout(() => this.#end(client, { kind: "expired" }), Math.max(0, expiresAt - this.#now()));
    } catch (error) {
      client.dispose();
      this.#set({ status: "signed_out", notice: noticeFor(error) });
    }
  }

  signOut(): void {
    if (this.#client) this.#end(this.#client, { kind: "signed_out" });
  }

  /** 절전 복귀처럼 타이머가 늦게 도는 경우를 위해 화면이 다시 보일 때 부른다. */
  checkExpiry(): void {
    if (this.#state.status === "active" && this.#client && this.#state.expiresAt <= this.#now()) this.#end(this.#client, { kind: "expired" });
  }

  #end(client: GatewayClient, notice: AuthNotice): void {
    client.dispose();
    if (this.#client !== client) return;
    this.#client = null;
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    this.#set({ status: "signed_out", notice });
  }

  #set(next: AuthState): void {
    this.#state = next;
    for (const listener of [...this.#listeners]) listener();
  }
}

function noticeFor(error: unknown): AuthNotice {
  if (!isGatewayError(error)) return { kind: "server" };
  switch (error.kind) {
    case "unauthorized":
      return { kind: "rejected" };
    case "network":
      return { kind: "network" };
    case "rate_limited":
      return error.retryAfterSeconds === null ? { kind: "rate_limited" } : { kind: "rate_limited", retryAfterSeconds: error.retryAfterSeconds };
    case "bad_request":
      return { kind: "invalid_input" };
    default:
      return { kind: "server" };
  }
}
