import { asArray, asRecord, parseTime, toContext, toDocument, toProject, toSearch, toSnapshot } from "./adapters";
import { GatewayError } from "./errors";
import { assertGatewayPath, isUuid, paths } from "./paths";
import type { ContextBundle, DownloadedFile, ExchangeResult, ExportFormat, GatewayDocument, ProjectSummary, SearchResult, SnapshotInfo } from "./types";

export interface GatewayClientOptions {
  fetch?: typeof fetch;
  /** 교환 뒤 데이터 요청이 401을 받으면 한 번 부른다. 세션 상태를 비우는 책임은 호출자에게 있다. */
  onUnauthorized?: () => void;
}

interface RequestOptions {
  method?: "GET" | "POST";
  query?: Record<string, string | null>;
  accept?: string;
  signal?: AbortSignal;
  /** 키 교환은 Authorization에 1회용 키를, 나머지는 세션 토큰을 쓴다. */
  credential: string;
  isExchange?: boolean;
}

const KEY_PATTERN = /^[\x21-\x7e]+$/;
const EXTENSION: Record<ExportFormat, string> = { markdown: "md", json: "json", zip: "zip" };

/**
 * 같은 origin의 공개 REST만 부르는 클라이언트. 키는 교환 요청 한 번에만 쓰이고 보관하지 않으며,
 * 세션 토큰은 private 필드에만 있다(저장소·URL·로그에 쓰지 않는다). 인스턴스 하나당 교환은 한 번이다.
 */
export class GatewayClient {
  #token: string | null = null;
  #exchangeStarted = false;
  readonly #fetch: typeof fetch;
  readonly #onUnauthorized: (() => void) | undefined;

  constructor(options: GatewayClientOptions = {}) {
    this.#fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.#onUnauthorized = options.onUnauthorized;
  }

  get hasSession(): boolean {
    return this.#token !== null;
  }

  dispose(): void {
    this.#token = null;
  }

  async exchange(rawKey: string): Promise<ExchangeResult> {
    const key = rawKey.trim();
    if (key === "") throw new GatewayError("bad_request", "키를 입력하세요");
    if (!KEY_PATTERN.test(key)) throw new GatewayError("bad_request", "키 형식이 올바르지 않습니다");
    if (this.#exchangeStarted) throw new GatewayError("misuse", "키는 한 번만 교환할 수 있습니다");
    this.#exchangeStarted = true;
    const body = asRecord(await this.#request(paths.exchange(), { method: "POST", credential: key, isExchange: true }), "exchange");
    const token = body["access_token"];
    const expires = body["expires_at"];
    if (typeof token !== "string" || token === "" || typeof expires !== "string") throw new GatewayError("contract", "교환 응답 형식이 계약과 다름");
    const expiresAt = parseTime(expires);
    this.#token = token;
    return { expiresAt };
  }

  async listProjects(signal?: AbortSignal): Promise<ProjectSummary[]> {
    return asArray(await this.#get(paths.projects(), {}, signal), "projects").map(toProject);
  }

  async getProject(id: string, signal?: AbortSignal): Promise<ProjectSummary> {
    const project = toProject(await this.#get(paths.project(this.#projectId(id)), {}, signal));
    if (project.id !== id) throw new GatewayError("contract", "다른 프로젝트의 메타데이터");
    return project;
  }

  async listSnapshots(id: string, signal?: AbortSignal): Promise<SnapshotInfo[]> {
    return asArray(await this.#get(paths.snapshots(this.#projectId(id)), {}, signal), "snapshots").map(toSnapshot);
  }

  async getContext(id: string, snapshotId: string | null, signal?: AbortSignal): Promise<ContextBundle> {
    return toContext(await this.#get(paths.context(this.#projectId(id)), { snapshot_id: snapshotId }, signal), id, snapshotId);
  }

  async getDocument(id: string, docId: string, snapshotId: string | null, signal?: AbortSignal): Promise<GatewayDocument> {
    const doc = toDocument(await this.#get(paths.document(this.#projectId(id), docId), { snapshot_id: snapshotId }, signal));
    if (doc.id !== docId) throw new GatewayError("contract", "다른 문서가 돌아왔습니다");
    return doc;
  }

  async search(id: string, query: string, snapshotId: string | null, signal?: AbortSignal): Promise<SearchResult> {
    return toSearch(await this.#get(paths.search(this.#projectId(id)), { q: query, snapshot_id: snapshotId }, signal), id);
  }

  /** 내보내기도 Authorization을 붙인 fetch로 받아 Blob으로 돌려준다. URL에는 토큰이 없다. */
  async download(id: string, format: ExportFormat, snapshotId: string | null, signal?: AbortSignal): Promise<DownloadedFile> {
    const response = await this.#send(paths.exportAll(this.#projectId(id)), { query: { format, snapshot_id: snapshotId }, accept: "*/*", signal, credential: this.#session() });
    let blob: Blob;
    try {
      blob = await response.blob();
    } catch (error) {
      throw this.#asNetworkError(error, signal);
    }
    const fallback = `context-${id.slice(0, 8)}.${EXTENSION[format]}`;
    return { blob, filename: filenameFrom(response.headers.get("content-disposition")) ?? fallback };
  }

  #projectId(id: string): string {
    if (!isUuid(id)) throw new GatewayError("misuse", "프로젝트 id 형식이 올바르지 않습니다");
    return id;
  }

  #session(): string {
    if (this.#token === null) throw new GatewayError("unauthorized", "세션이 없습니다");
    return this.#token;
  }

  async #get(path: string, query: Record<string, string | null>, signal?: AbortSignal): Promise<unknown> {
    return this.#request(path, { query, signal, credential: this.#session() });
  }

  async #request(path: string, options: RequestOptions): Promise<unknown> {
    const response = await this.#send(path, options);
    try {
      return await response.json();
    } catch (error) {
      if (isAbort(error)) throw error;
      throw new GatewayError("contract", "응답을 JSON으로 읽을 수 없습니다");
    }
  }

  async #send(path: string, options: Pick<RequestOptions, "method" | "query" | "accept" | "signal" | "credential" | "isExchange">): Promise<Response> {
    assertGatewayPath(path);
    const params = new URLSearchParams();
    for (const [name, value] of Object.entries(options.query ?? {})) if (value !== null) params.set(name, value);
    const target = params.size > 0 ? `${path}?${params.toString()}` : path;
    let response: Response;
    try {
      response = await this.#fetch(target, {
        method: options.method ?? "GET",
        headers: { Authorization: `Bearer ${options.credential}`, Accept: options.accept ?? "application/json" },
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        ...(options.signal ? { signal: options.signal } : {}),
      });
    } catch (error) {
      throw this.#asNetworkError(error, options.signal);
    }
    if (response.ok) return response;
    throw this.#translate(response, options.isExchange === true);
  }

  #asNetworkError(error: unknown, signal?: AbortSignal): unknown {
    if (isAbort(error) || signal?.aborted) return error;
    return new GatewayError("network", "서버에 연결하지 못했습니다");
  }

  #translate(response: Response, isExchange: boolean): GatewayError {
    const status = response.status;
    if (status === 401) {
      if (!isExchange) {
        this.#token = null;
        this.#onUnauthorized?.();
      }
      return new GatewayError("unauthorized", "인증되지 않았습니다", { status });
    }
    if (status === 403) return new GatewayError("forbidden", "접근 권한이 없습니다", { status });
    if (status === 404) return new GatewayError("not_found", "찾을 수 없거나 볼 권한이 없습니다", { status });
    if (status === 429) {
      const seconds = Number.parseInt(response.headers.get("retry-after") ?? "", 10);
      return new GatewayError("rate_limited", "요청이 너무 많습니다", { status, ...(Number.isFinite(seconds) && seconds >= 0 ? { retryAfterSeconds: seconds } : {}) });
    }
    if (status === 400 || status === 406 || status === 422) return new GatewayError("bad_request", "요청을 처리할 수 없습니다", { status });
    return new GatewayError("server", "서버에서 오류가 발생했습니다", { status });
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function filenameFrom(disposition: string | null): string | null {
  const match = disposition ? /filename="?([^";]+)"?/i.exec(disposition) : null;
  const name = match?.[1]?.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim();
  return name ? name : null;
}
