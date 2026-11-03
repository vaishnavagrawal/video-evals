"use client";
import { useCallback, useEffect, useRef, useState } from "react";

/** Load once, then re-load every `intervalMs` while `shouldPoll(data)` is true and the tab is visible. */
export function usePoll<T>(load: () => Promise<T>, shouldPoll: (d: T) => boolean, intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const loadRef = useRef(load);
  const pollRef = useRef(shouldPoll);
  useEffect(() => {
    loadRef.current = load;
    pollRef.current = shouldPoll;
  });

  const refresh = useCallback(async () => {
    try {
      const d = await loadRef.current();
      setData(d);
      setError(null);
      return d;
    } catch (e) {
      setError(e as Error);
      return null;
    }
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const tick = async () => {
      const d = document.visibilityState === "hidden" ? undefined : await refresh();
      if (stopped) return;
      if (d === null) timer = setTimeout(tick, intervalMs * 3); // error: back off, keep trying
      else if (d === undefined || pollRef.current(d)) timer = setTimeout(tick, intervalMs);
    };
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [refresh, intervalMs]);

  return { data, error, refresh, setData };
}
