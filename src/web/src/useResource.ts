/**
 * Data fetching for the views.
 *
 * A block explorer is a read-only client of a node that may go away, so every
 * view gets the same three states -- loading, ready, failed -- and a `reload`
 * to ask again. Polling is opt-in per view and always aborts the in-flight
 * request, which keeps a tab open on a quiet tab from piling up sockets.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface Resource<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  updatedAt: number | undefined;
  reload: () => void;
}

interface Options {
  /** Re-run the loader every `pollMs` milliseconds while mounted. */
  pollMs?: number;
}

/**
 * Run `load` whenever `deps` change, keeping the previous value visible while
 * the next one is in flight so the UI does not flash empty on every poll.
 */
export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: Options = {},
): Resource<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined);
  const [nonce, setNonce] = useState(0);
  const loader = useRef(load);
  loader.current = load;

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  const pollMs = options.pollMs;

  useEffect(() => {
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    let active = true;

    const run = (): void => {
      controller?.abort();
      controller = new AbortController();
      setLoading(true);
      loader
        .current(controller.signal)
        .then((value) => {
          if (!active) return;
          setData(value);
          setError(undefined);
          setUpdatedAt(Date.now());
          setLoading(false);
        })
        .catch((failure: unknown) => {
          if (!active) return;
          if (failure instanceof DOMException && failure.name === "AbortError") return;
          setError(failure instanceof Error ? failure : new Error(String(failure)));
          setLoading(false);
        });
    };

    run();
    if (pollMs !== undefined) {
      timer = setInterval(run, pollMs);
    }

    return () => {
      active = false;
      controller?.abort();
      if (timer !== undefined) clearInterval(timer);
    };
    // The loader is held in a ref: `deps` is the caller's dependency list.
  }, [nonce, pollMs, ...deps]);

  return { data, error, loading, updatedAt, reload };
}

/** A clock that re-renders every `intervalMs`, for "12s ago" labels. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(timer);
    };
  }, [intervalMs]);
  return now;
}
