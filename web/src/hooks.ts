import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { z } from "zod";
import { api, ApiError } from "./api";

export function useRemote<T>(
  path: string | null,
  schema: z.ZodType<T>,
  active = false,
  interval = 0,
) {
  const [data, setData] = useState<T>();
  const [dataPath, setDataPath] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(false);
  const stopped = useRef(false);
  const mounted = useRef(true);
  const schemaRef = useRef(schema);
  const version = useRef(0);
  const inFlight = useRef(false);
  const currentPath = useRef(path);
  useLayoutEffect(() => { currentPath.current = path; }, [path]);
  useEffect(() => {
    schemaRef.current = schema;
  }, [schema]);
  const reload = useCallback(
    async (passive = false) => {
      if (!path || currentPath.current !== path || (passive && inFlight.current)) return;
      const requestVersion = ++version.current;
      inFlight.current = true;
      setLoading(true);
      try {
        const next = await api(path, schemaRef.current, { passive });
        if (mounted.current && currentPath.current === path && requestVersion === version.current) {
          setData(next);
          setDataPath(path);
          setError(undefined);
          stopped.current = false;
        }
        return next;
      } catch (e) {
        if (mounted.current && currentPath.current === path && requestVersion === version.current) {
          setError(e);
          if (
            e instanceof ApiError &&
            [
              "runtime_sleeping",
              "runtime_faulted",
              "safety_suspended",
            ].includes(e.code)
          )
            stopped.current = true;
        }
        return undefined;
      } finally {
        if (mounted.current && currentPath.current === path && requestVersion === version.current) {
          setLoading(false);
          inFlight.current = false;
        }
      }
    },
    [path],
  );
  const invalidate = useCallback(() => {
    mounted.current = false;
    version.current++;
    inFlight.current = false;
  }, []);
  useEffect(() => {
    mounted.current = true;
    stopped.current = false;
    inFlight.current = false;
    setData(undefined);
    setDataPath(null);
    setError(undefined);
    void reload(!active);
    return invalidate;
  }, [reload, active, invalidate]);
  useEffect(() => {
    if (!interval) return;
    const id = window.setInterval(() => {
      if (!stopped.current && document.visibilityState === "visible")
        void reload(true);
    }, interval);
    return () => window.clearInterval(id);
  }, [reload, interval]);
  return { data, dataPath, error, loading, reload, setData };
}
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  async function run<T>(
    operation: () => Promise<T>,
    success?: string,
  ): Promise<T | undefined> {
    setBusy(true);
    setError(undefined);
    setNotice("");
    try {
      const value = await operation();
      if (success) setNotice(success);
      return value;
    } catch (e) {
      setError(e);
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, notice, run, setError, setNotice };
}
