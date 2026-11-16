import { GatewayError } from "./errors";

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const SEGMENT = "(?!\\.{1,2}$)[A-Za-z0-9._~%-]+";

// 공개 REST 7개 경로와 키 교환만 허용한다. Private API·로그인·refresh 같은 경로는 여기서 만들어지지 못한다.
const ALLOWED = [
  /^\/api\/v1\/auth\/exchange$/,
  /^\/api\/v1\/projects$/,
  new RegExp(`^/api/v1/projects/${UUID}$`),
  new RegExp(`^/api/v1/projects/${UUID}/(snapshots|context|search|export)$`),
  new RegExp(`^/api/v1/projects/${UUID}/documents/${SEGMENT}$`),
];

export function assertGatewayPath(path: string): void {
  if (!ALLOWED.some((pattern) => pattern.test(path))) {
    throw new GatewayError("misuse", "허용되지 않은 API 경로");
  }
}

export function isUuid(value: string): boolean {
  return new RegExp(`^${UUID}$`).test(value);
}

export const paths = {
  exchange: () => "/api/v1/auth/exchange",
  projects: () => "/api/v1/projects",
  project: (id: string) => `/api/v1/projects/${id}`,
  snapshots: (id: string) => `/api/v1/projects/${id}/snapshots`,
  context: (id: string) => `/api/v1/projects/${id}/context`,
  document: (id: string, doc: string) => `/api/v1/projects/${id}/documents/${encodeURIComponent(doc)}`,
  search: (id: string) => `/api/v1/projects/${id}/search`,
  exportAll: (id: string) => `/api/v1/projects/${id}/export`,
};
