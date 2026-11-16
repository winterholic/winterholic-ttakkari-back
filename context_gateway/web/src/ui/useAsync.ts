import { useCallback, useEffect, useRef, useState } from "react";

export type AsyncState<T> = { status: "idle" } | { status: "loading" } | { status: "ready"; data: T } | { status: "error"; error: unknown };

type Settled<T> = { status: "ready"; data: T } | { status: "error"; error: unknown };

const isAbort = (error: unknown): boolean => error instanceof DOMException && error.name === "AbortError";

/**
 * key가 바뀌면 이전 key의 결과는 즉시 버려지고 loading으로 읽힌다(한 프레임이라도 다른 프로젝트·문서 내용이 새 제목 아래 남지 않게 한다).
 * 늦게 도착한 이전 요청은 live 플래그와 abort로 무시한다. key가 null이면 요청하지 않는다.
 */
export function useAsync<T>(key: string | null, loader: (signal: AbortSignal) => Promise<T>): { state: AsyncState<T>; reload: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [stored, setStored] = useState<{ key: string; attempt: number; settled: Settled<T> } | null>(null);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  // 같은 key를 다시 열 때 요청을 반복하지 않는다. 스냅샷은 불변이고 세션은 최대 1시간이라 오래된 값 위험이 작다.
  const cache = useRef(new Map<string, T>());

  useEffect(() => {
    if (key === null) return;
    if (cache.current.has(key)) return;
    const controller = new AbortController();
    let live = true;
    loaderRef.current(controller.signal).then(
      (data) => {
        if (!live) return;
        cache.current.set(key, data);
        setStored({ key, attempt, settled: { status: "ready", data } });
      },
      (error: unknown) => {
        if (live && !isAbort(error)) setStored({ key, attempt, settled: { status: "error", error } });
      },
    );
    return () => {
      live = false;
      controller.abort();
    };
  }, [key, attempt]);

  const reload = useCallback(() => {
    if (key !== null) cache.current.delete(key);
    setAttempt((value) => value + 1);
  }, [key]);

  if (key === null) return { state: { status: "idle" }, reload };
  const cached = cache.current.get(key);
  if (cached !== undefined) return { state: { status: "ready", data: cached }, reload };
  if (stored && stored.key === key && stored.attempt === attempt) return { state: stored.settled, reload };
  return { state: { status: "loading" }, reload };
}
