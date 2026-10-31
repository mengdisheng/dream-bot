import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { useRemote } from "./hooks";
import {
  eventPageSchema,
  eventSchema,
  messagePageSchema,
  taskPageSchema,
  taskSummarySchema,
  type TaskEvent,
  type TaskSummary,
} from "./types";
import type { z } from "zod";

const taskPath = "/api/tasks?view=summary&limit=50";
export function useTaskSummaries(active: boolean) {
  const first = useRemote(taskPath, taskPageSchema, active, 5000);
  const [older, setOlder] = useState<TaskSummary[]>([]);
  const [next, setNext] = useState<string | null>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<unknown>();
  useEffect(() => {
    if (!first.data) return;
    if (older.length && first.data.has_more && first.data.items.length &&
      !first.data.items.some((item) => older.some((loaded) => loaded.id === item.id))) {
      setNext(first.data.next_cursor);
    }
    setOlder((previous) => {
      const byId = new Map(previous.map((item) => [item.id, item]));
      first.data?.items.forEach((item) => byId.set(item.id, item));
      const merged = [...byId.values()];
      return merged.length === previous.length && merged.every((item, index) => item === previous[index]) ? previous : merged;
    });
  }, [first.data, older]);
  const firstIds = new Set(first.data?.items.map((item) => item.id));
  const items = [...(first.data?.items ?? []), ...older.filter((item) => !firstIds.has(item.id))]
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
  const setFirst = first.setData;
  const update = useCallback((value: TaskSummary) => {
    const summary = taskSummarySchema.parse(value);
    setOlder((previous) => previous.map((item) => item.id === summary.id ? summary : item));
    setFirst((previous) => previous ? { ...previous, items: previous.items.map((item) => item.id === summary.id ? summary : item) } : previous);
  }, [setFirst]);
  const cursor = next === undefined ? first.data?.next_cursor : next;
  async function loadMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api(`${taskPath}&cursor=${encodeURIComponent(cursor)}`, taskPageSchema);
      setOlder((previous) => {
        const byId = new Map(previous.map((item) => [item.id, item]));
        page.items.forEach((item) => byId.set(item.id, item));
        return [...byId.values()];
      });
      setNext(page.has_more ? page.next_cursor : null);
      setPageError(undefined);
    } catch (error) { setPageError(error); }
    finally { setLoadingMore(false); }
  }
  return { ...first, data: first.data ? { items } : undefined,
    error: pageError ?? first.error, hasMore: !!cursor, loadingMore, loadMore, update };
}

type Message = z.infer<typeof messagePageSchema>["items"][number];
export function useTaskMessages(taskId: string | undefined, active: boolean, streaming: boolean) {
  const path = taskId ? `/api/messages?task_id=${encodeURIComponent(taskId)}&view=page&limit=50` : null;
  const latest = useRemote(path, messagePageSchema, active, streaming ? 0 : 5000);
  const [older, setOlder] = useState<{ taskId: string; items: Message[]; cursor: string | null }>();
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<unknown>();
  const currentPage = latest.dataPath === path ? latest.data : undefined;
  const currentOlder = older?.taskId === taskId ? older : undefined;
  const cursor = currentOlder ? currentOlder.cursor : currentPage?.next_before;
  useEffect(() => {
    if (!taskId || !currentPage) return;
    setOlder((previous) => {
      const sameTask = previous?.taskId === taskId;
      const merged = new Map((sameTask ? previous.items : []).map((item) => [item.id, item]));
      const gap = sameTask && previous.items.length > 0 && currentPage.has_more && currentPage.items.length > 0 &&
        !currentPage.items.some((item) => merged.has(item.id));
      currentPage.items.forEach((item) => merged.set(item.id, item));
      return { taskId, items: [...merged.values()],
        cursor: sameTask && !gap ? previous.cursor : currentPage.has_more ? currentPage.next_before : null };
    });
  }, [taskId, currentPage]);
  const byId = new Map<string, Message>();
  currentOlder?.items.forEach((message) => byId.set(message.id, message));
  currentPage?.items.filter((message) => message.task_id === taskId).forEach((message) => byId.set(message.id, message));
  const items = [...byId.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  const currentTask = useRef(taskId);
  useEffect(() => { currentTask.current = taskId; setPageError(undefined); }, [taskId]);
  async function loadMore() {
    if (!path || !taskId || !cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api(`${path}&before=${encodeURIComponent(cursor)}`, messagePageSchema);
      if (currentTask.current !== taskId) return;
      setOlder((previous) => ({ taskId,
        items: [...page.items, ...(previous?.taskId === taskId ? previous.items : [])],
        cursor: page.has_more ? page.next_before : null }));
      setPageError(undefined);
    } catch (error) { if (currentTask.current === taskId) setPageError(error); }
    finally { setLoadingMore(false); }
  }
  return { ...latest, data: currentPage ? { items } : undefined,
    error: pageError ?? latest.error, hasMore: !!cursor, loadingMore, loadMore };
}

export type TaskInvalidation = { task: boolean; approvals: boolean; tools: boolean; messages: boolean };
const emptyInvalidation = (): TaskInvalidation => ({ task: false, approvals: false, tools: false, messages: false });

/** History and SSE share the same global, increasing cursor. Gaps belong to other tasks. */
export function useTaskEvents(taskId: string | undefined, active: boolean,
  onChanged: (resources: TaskInvalidation) => void) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>();
  const seq = useRef(0);
  const generation = useRef(0);
  const historyRequest = useRef<{ taskId: string; generation: number } | null>(null);
  const stopped = useRef(false);
  const source = useRef<EventSource | null>(null);
  const text = useRef("");
  const callback = useRef(onChanged);
  const invalid = useRef(emptyInvalidation());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => { callback.current = onChanged; }, [onChanged]);
  const append = useCallback((items: TaskEvent[]) => {
    const fresh: TaskEvent[] = [];
    for (const item of items) {
      if (item.task_id !== taskId || item.seq <= seq.current) continue;
      seq.current = item.seq;
      if (item.kind === "model.input_measured") continue;
      if (item.kind === "model.text_delta") {
        if (typeof item.data.text === "string") text.current += item.data.text;
      } else {
        fresh.push(item);
        if (item.kind === "model.completed" || item.kind.startsWith("task.")) text.current = "";
      }
    }
    if (fresh.length) setEvents((previous) => [...previous, ...fresh]);
    setDraft(text.current);
  }, [taskId]);
  const reload = useCallback(async (passive = false) => {
    if (!taskId || (passive && historyRequest.current?.taskId === taskId)) return;
    const request = ++generation.current;
    historyRequest.current = { taskId, generation: request };
    source.current?.close(); source.current = null;
    setConnected(false); setReady(null); setLoading(true); stopped.current = false;
    try {
      let after = seq.current;
      const changed = emptyInvalidation();
      let firstPage = true;
      while (true) {
        const page = await api(`/api/tasks/${encodeURIComponent(taskId)}/history?after=${after}&limit=200`,
          eventPageSchema, { passive: passive || !firstPage });
        firstPage = false;
        if (request !== generation.current) return;
        append(page.items);
        for (const item of page.items) {
          if (item.kind === "model.text_delta" || item.kind === "model.input_measured") continue;
          changed.task = true;
          if (item.kind.startsWith("approval.")) changed.approvals = true;
          if (item.kind.startsWith("tool.") || item.kind.startsWith("approval.")) changed.tools = true;
          if (item.kind.startsWith("task.")) changed.messages = true;
        }
        if (!page.has_more) break;
        if (page.next_after <= after) throw new ApiError("invalid_cursor", "过程记录游标没有前进，请重新读取。");
        after = page.next_after;
      }
      setError(undefined); setReady(taskId);
      if (changed.task) callback.current(changed);
    } catch (failure) {
      if (request !== generation.current) return;
      setError(failure);
      if (failure instanceof ApiError && ["runtime_sleeping", "runtime_faulted", "safety_suspended"].includes(failure.code)) stopped.current = true;
    } finally {
      if (historyRequest.current?.generation === request) historyRequest.current = null;
      if (request === generation.current) setLoading(false);
    }
  }, [taskId, append]);
  const cancel = useCallback(() => {
    generation.current++; historyRequest.current = null; source.current?.close(); source.current = null;
    if (timer.current) clearTimeout(timer.current);
    timer.current = undefined; invalid.current = emptyInvalidation();
  }, []);
  useEffect(() => {
    seq.current = 0; text.current = ""; stopped.current = false;
    setEvents([]); setDraft("");
    void reload(!active);
    return cancel;
  }, [taskId, active, reload, cancel]);
  useEffect(() => {
    if (!taskId || ready !== taskId || typeof EventSource === "undefined" || document.visibilityState !== "visible") return;
    const stream = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?after=${seq.current}`, { withCredentials: true });
    source.current = stream;
    stream.onopen = () => { if (source.current === stream) setConnected(true); };
    stream.onerror = () => { if (source.current === stream) setConnected(false); }; // Native EventSource resumes using Last-Event-ID.
    stream.addEventListener("task_event", (event: MessageEvent<string>) => {
      if (source.current !== stream) return;
      try {
        const parsed = eventSchema.safeParse(JSON.parse(event.data));
        if (!parsed.success || parsed.data.task_id !== taskId || parsed.data.seq <= seq.current) return;
        const next = parsed.data;
        append([next]);
        if (next.kind === "model.text_delta" || next.kind === "model.input_measured") return;
        invalid.current.task = true;
        if (next.kind.startsWith("approval.")) invalid.current.approvals = true;
        if (next.kind.startsWith("tool.") || next.kind.startsWith("approval.")) invalid.current.tools = true;
        if (next.kind.startsWith("task.")) invalid.current.messages = true;
        if (!timer.current) timer.current = setTimeout(() => {
          timer.current = undefined;
          const resources = invalid.current; invalid.current = emptyInvalidation();
          callback.current(resources);
        }, 250);
      } catch { stream.close(); setConnected(false); setReady(null); }
    });
    stream.addEventListener("runtime_sleeping", () => {
      if (source.current !== stream) return;
      stopped.current = true; stream.close(); setConnected(false); setReady(null);
    });
    return () => { stream.close(); if (source.current === stream) source.current = null; };
  }, [taskId, ready, append]);
  useEffect(() => {
    const visibility = () => {
      if (document.visibilityState === "hidden") {
        source.current?.close(); source.current = null; setConnected(false); setReady(null);
      } else if (!stopped.current) void reload(true);
    };
    document.addEventListener("visibilitychange", visibility);
    return () => document.removeEventListener("visibilitychange", visibility);
  }, [reload]);
  useEffect(() => {
    if (connected || !taskId) return;
    const id = setInterval(() => {
      if (!stopped.current && document.visibilityState === "visible") void reload(true);
    }, 5000);
    return () => clearInterval(id);
  }, [connected, taskId, reload]);
  return { events, draft, connected, loading, error, reload };
}
