import { parseTime } from "../api/adapters";

const dateTime = new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" });

/** 서버가 준 ISO 시각을 사용자 시간대로 보여 준다. 읽을 수 없으면 원문을 그대로 둔다(숨기지 않는다). */
export function formatTime(iso: string | null): string {
  if (iso === null) return "기록 없음";
  try {
    return dateTime.format(parseTime(iso));
  } catch {
    return iso;
  }
}

export function minutesLeft(expiresAt: number, now: number): number {
  return Math.max(0, Math.ceil((expiresAt - now) / 60_000));
}

export const shortId = (id: string): string => id.slice(0, 8);
