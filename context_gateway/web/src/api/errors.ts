export type GatewayErrorKind =
  | "unauthorized" // 401: 키·세션이 없거나 만료됐거나 이미 사용됐다. 서버가 이유를 구분해 주지 않는다.
  | "forbidden" // 403: 서버는 보내지 않지만 앞단 프록시가 보낼 수 있다.
  | "not_found" // 404: 없거나 이 키로 볼 수 없다. 서버는 둘을 구분하지 않는다(존재 여부 비노출).
  | "rate_limited" // 429
  | "network" // fetch가 응답 없이 실패
  | "server" // 5xx, 409(스냅샷 무결성 실패) 등
  | "bad_request" // 400·406·422
  | "contract" // 응답이 기대한 모양이 아니거나 다른 프로젝트 데이터가 섞여 왔다
  | "misuse"; // 클라이언트 내부 규칙 위반(교환 재호출, 허용되지 않은 경로 등)

export class GatewayError extends Error {
  readonly kind: GatewayErrorKind;
  readonly status: number | null;
  readonly retryAfterSeconds: number | null;

  constructor(kind: GatewayErrorKind, message: string, options: { status?: number; retryAfterSeconds?: number } = {}) {
    super(message);
    this.name = "GatewayError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
  }
}

export function isGatewayError(value: unknown): value is GatewayError {
  return value instanceof GatewayError;
}
