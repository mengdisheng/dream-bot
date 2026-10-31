import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Tasks } from "../pages/ChatTasks";
import { useTaskEvents, useTaskMessages, useTaskSummaries } from "../taskHooks";

const json = (value: unknown) => new Response(JSON.stringify(value));
const task = (id: string) => ({ id, title: `任务 ${id}`, status: "running", revision: 1,
  created_at: new Date(Date.UTC(2026, 9, 31, 12 - Number(id.slice(1)))).toISOString(), updated_at: "2026-10-31T10:00:00Z",
  connection_id: "c1", model_id: "m1", tool_count: 0, token_count: 0,
  max_tools: 10, max_tokens: 1000, max_active_seconds: 300, active_seconds: 1,
  token_usage_estimated: false, unresolved_effect_count: 0, execution_mode: "agent" });
const event = (seq: number, kind = "task.running", data: Record<string, unknown> = {}) => ({
  seq, kind, data, task_id: "t1", created_at: "2026-10-31T10:00:00Z",
});
class Stream extends EventTarget {
  static instances: Stream[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { super(); Stream.instances.push(this); }
  close() { this.closed = true; }
  open() { this.onopen?.(); }
  send(value: ReturnType<typeof event>) {
    this.dispatchEvent(new MessageEvent("task_event", { data: JSON.stringify(value) }));
  }
}
beforeEach(() => { Stream.instances = []; vi.stubGlobal("EventSource", Stream); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("loads bounded summary pages, merges duplicate IDs, and never requests task bodies", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input); calls.push(path);
    return json(path.includes("cursor=")
      ? { items: [task("t2"), task("t3")], next_cursor: null, has_more: false }
      : { items: [task("t1"), task("t2")], next_cursor: "next page", has_more: true });
  }));
  const { result } = renderHook(() => useTaskSummaries(false));
  await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
  await act(async () => { await result.current.loadMore(); });
  expect(result.current.data?.items.map((item) => item.id)).toEqual(["t1", "t2", "t3"]);
  expect(result.current.hasMore).toBe(false);
  expect(calls).toEqual(["/api/tasks?view=summary&limit=50", "/api/tasks?view=summary&limit=50&cursor=next%20page"]);
});

it("keeps loaded task windows when newly created tasks advance the first page", async () => {
  let refresh = false;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    if (String(input).includes("cursor=")) return json({ items: [task("t3")], next_cursor: null, has_more: false });
    return json({ items: refresh ? [task("t0"), task("t1")] : [task("t1"), task("t2")], next_cursor: "page2", has_more: true });
  }));
  const { result } = renderHook(() => useTaskSummaries(false));
  await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
  await act(async () => { await result.current.loadMore(); });
  refresh = true;
  await act(async () => { await result.current.reload(true); });
  expect(result.current.data?.items.map((item) => item.id)).toEqual(["t0", "t1", "t2", "t3"]);
  expect(result.current.hasMore).toBe(false);
});

it("keeps messages from previous latest windows after all older pages have loaded", async () => {
  let refresh = false;
  const message = (id: number) => ({ id: String(id), task_id: "t1", role: "assistant", content: String(id), created_at: new Date(Date.UTC(2026, 9, 31, 10, id)).toISOString() });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    if (String(input).includes("before=")) return json({ items: [message(1)], next_before: null, has_more: false });
    return json({ items: refresh ? [message(50), message(51), message(52)] : [message(49), message(50)], next_before: "older", has_more: true });
  }));
  const { result } = renderHook(() => useTaskMessages("t1", false, true));
  await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
  await act(async () => { await result.current.loadMore(); });
  refresh = true;
  await act(async () => { await result.current.reload(true); });
  expect(result.current.data?.items.map((item) => item.id)).toEqual(["1", "49", "50", "51", "52"]);
  expect(result.current.hasMore).toBe(false);
});

it("reopens a page chain to bridge non-overlapping latest windows", async () => {
  let refresh = false;
  const message = (id: string) => ({ id, task_id: "t1", role: "user", content: id, created_at: "2026-10-31T10:00:00Z" });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input);
    if (path.startsWith("/api/tasks?")) {
      if (path.includes("cursor=bridge")) return json({ items: [task("t1"), task("t2")], next_cursor: null, has_more: false });
      if (path.includes("cursor=older")) return json({ items: [task("t2")], next_cursor: null, has_more: false });
      return json({ items: [task(refresh ? "t0" : "t1")], next_cursor: refresh ? "bridge" : "older", has_more: true });
    }
    if (path.includes("before=bridge")) return json({ items: [message("middle")], next_before: null, has_more: false });
    if (path.includes("before=older")) return json({ items: [message("old")], next_before: null, has_more: false });
    return json({ items: [message(refresh ? "new" : "first")], next_before: refresh ? "bridge" : "older", has_more: true });
  }));
  const { result } = renderHook(() => ({ tasks: useTaskSummaries(false), messages: useTaskMessages("t1", false, true) }));
  await waitFor(() => expect(result.current.tasks.data?.items).toHaveLength(1));
  await waitFor(() => expect(result.current.messages.data?.items).toHaveLength(1));
  await act(async () => { await result.current.tasks.loadMore(); await result.current.messages.loadMore(); });
  expect(result.current.tasks.hasMore).toBe(false);
  expect(result.current.messages.hasMore).toBe(false);
  refresh = true;
  await act(async () => { await result.current.tasks.reload(true); await result.current.messages.reload(true); });
  expect(result.current.tasks.hasMore).toBe(true);
  expect(result.current.messages.hasMore).toBe(true);
  await act(async () => { await result.current.tasks.loadMore(); await result.current.messages.loadMore(); });
  expect(result.current.tasks.data?.items.map((item) => item.id)).toEqual(["t0", "t1", "t2"]);
  expect(result.current.messages.data?.items.map((item) => item.id)).toContain("middle");
});

it("keeps slow automatic history reads single flight and refreshes snapshots after initial catchup", async () => {
  vi.useFakeTimers();
  let release: ((response: Response) => void) | undefined;
  const changes = vi.fn();
  const fetcher = vi.fn(async () => new Promise<Response>((resolve) => { release = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  renderHook(() => useTaskEvents("t1", false, changes));
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => {
    release!(json({ items: [event(5, "task.succeeded")], next_after: 5, has_more: false }));
    await Promise.resolve();
  });
  expect(changes).toHaveBeenCalledTimes(1);
  expect(changes).toHaveBeenCalledWith({ task: true, messages: true, approvals: false, tools: false });
});

it("drains more than1000 interleaved history events before SSE and accepts gaps while deduplicating", async () => {
  const paths: string[] = [];
  const changes = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input); paths.push(path);
    expect(new Headers(options?.headers).get("X-Personal-Bot-Passive")).toBe("1");
    const after = Number(new URL(path, "https://test.invalid").searchParams.get("after"));
    const all = Array.from({ length: 1101 }, (_, index) => event(index * 2 + 1));
    const rest = all.filter((item) => item.seq > after);
    const items = rest.slice(0, 200);
    return json({ items, next_after: items.at(-1)?.seq ?? after, has_more: rest.length > 200 });
  }));
  const { result } = renderHook(() => useTaskEvents("t1", false, changes));
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  expect(paths).toHaveLength(6);
  expect(result.current.events).toHaveLength(1101);
  expect(Stream.instances[0].url).toContain("after=2201");
  changes.mockClear();
  act(() => {
    Stream.instances[0].open();
    Stream.instances[0].send(event(2201));
    Stream.instances[0].send(event(2205, "model.text_delta", { text: "收到" }));
    Stream.instances[0].send(event(2210, "model.text_delta", { text: "回答" }));
  });
  expect(result.current.draft).toBe("收到回答");
  expect(result.current.events).toHaveLength(1101);
  expect(Stream.instances[0].closed).toBe(false);
  await new Promise((resolve) => setTimeout(resolve, 300));
  expect(changes).not.toHaveBeenCalled();
  expect(paths).toHaveLength(6);
});

it("reads only selected task details and coalesces resource refreshes without delta request storms", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input); calls.push(path);
    if (path.startsWith("/api/tasks?")) return json({ items: [task("t1"), task("t2")], next_cursor: null, has_more: false });
    if (path === "/api/tasks/t1" || path === "/api/tasks/t2") return json(task(path.split("/").at(-1)!));
    if (path.includes("/history?")) return json({ items: [], next_after: 0, has_more: false });
    return json({ items: [] });
  }));
  render(<Tasks active={false} />);
  await screen.findByRole("heading", { name: "任务 t1" });
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  expect(calls).not.toContain("/api/tasks/t2");
  const base = calls.length;
  act(() => {
    Stream.instances[0].open();
    for (let i = 1; i <= 40; i++) Stream.instances[0].send(event(i * 3, "model.text_delta", { text: "字" }));
    Stream.instances[0].send(event(121, "model.input_measured", { message_utf8_bytes: 10 }));
  });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
  expect(calls).toHaveLength(base);
  act(() => {
    Stream.instances[0].send(event(125, "tool.succeeded"));
    Stream.instances[0].send(event(130, "approval.required"));
    Stream.instances[0].send(event(135, "task.succeeded"));
  });
  await waitFor(() => expect(calls).toHaveLength(base + 4));
  expect(calls.filter((path) => path === "/api/tasks/t1")).toHaveLength(2);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /任务 t2/ }));
  await screen.findByRole("heading", { name: "任务 t2" });
  expect(calls).toContain("/api/tasks/t2");
  act(() => Stream.instances[0].send(event(9000, "task.failed", { summary: "旧任务迟到事件" })));
  expect(screen.queryByText("旧任务迟到事件")).not.toBeInTheDocument();
});

it("merges older message pages and never leaks a late page into another selected task", async () => {
  let release: ((response: Response) => void) | undefined;
  const message = (id: string, taskId: string, stamp: string) => ({ id, task_id: taskId, role: "user", content: id, created_at: stamp });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input);
    if (path.includes("before=")) return new Promise<Response>((resolve) => { release = resolve; });
    const identity = path.includes("task_id=t2") ? "t2" : "t1";
    return json({ items: [message("new", identity, "2026-10-31T10:00:00Z")], next_before: "older", has_more: true });
  }));
  const { result, rerender } = renderHook(({ id }) => useTaskMessages(id, false, true), { initialProps: { id: "t1" } });
  await waitFor(() => expect(result.current.data?.items).toHaveLength(1));
  let pending: Promise<void>;
  act(() => { pending = result.current.loadMore(); });
  await waitFor(() => expect(release).toBeDefined());
  rerender({ id: "t2" });
  await waitFor(() => expect(result.current.data?.items[0]?.task_id).toBe("t2"));
  await act(async () => {
    release!(json({ items: [message("old", "t1", "2026-10-30T10:00:00Z")], next_before: null, has_more: false }));
    await pending;
  });
  expect(result.current.data?.items.map((item) => item.task_id)).toEqual(["t2"]);
  act(() => { pending = result.current.loadMore(); });
  await act(async () => {
    release!(json({ items: [message("older-t2", "t2", "2026-10-30T10:00:00Z")], next_before: null, has_more: false }));
    await pending;
  });
  expect(result.current.data?.items.map((item) => item.id)).toEqual(["older-t2", "new"]);
  expect(result.current.hasMore).toBe(false);
});

it("suppresses message polling while SSE is healthy and uses passive paged reads when disconnected", async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options: RequestInit) => {
    expect(String(input)).toContain("view=page&limit=50");
    calls.push(options);
    return json({ items: [], next_before: null, has_more: false });
  }));
  const { rerender } = renderHook(({ streaming }) => useTaskMessages("t1", false, streaming), { initialProps: { streaming: true } });
  await waitFor(() => expect(calls).toHaveLength(1));
  vi.useFakeTimers();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(calls).toHaveLength(1);
  rerender({ streaming: false });
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(calls).toHaveLength(2);
  expect(calls.every((options) => new Headers(options.headers).get("X-Personal-Bot-Passive") === "1")).toBe(true);
});

it("reconnects from the last seen cursor after a hidden tab and stops reconnecting on sleep", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get");
  visibility.mockReturnValue("visible");
  const paths: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input); paths.push(path);
    const after = Number(new URL(path, "https://test.invalid").searchParams.get("after"));
    return json({ items: after === 0 ? [event(5)] : [event(12)], next_after: after === 0 ? 5 : 12, has_more: false });
  }));
  const { result } = renderHook(() => useTaskEvents("t1", false, vi.fn()));
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  act(() => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")); });
  expect(Stream.instances[0].closed).toBe(true);
  act(() => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
  await waitFor(() => expect(Stream.instances).toHaveLength(2));
  expect(paths[1]).toContain("after=5");
  expect(Stream.instances[1].url).toContain("after=12");
  expect(result.current.events.map((item) => item.seq)).toEqual([5, 12]);
  act(() => Stream.instances[1].dispatchEvent(new Event("runtime_sleeping")));
  act(() => {
    visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange"));
    visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(paths).toHaveLength(2);
  expect(Stream.instances[1].closed).toBe(true);
  visibility.mockRestore();
});
