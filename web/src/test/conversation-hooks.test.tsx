import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useConversationMessages, useConversations } from "../conversationHooks";

const json = (value: unknown) => new Response(JSON.stringify(value));
const conversation = (id: string) => ({ id, title: id, created_at: `2026-10-31T${String(20 - Number(id.slice(1))).padStart(2, "0")}:00:00Z`, updated_at: "2026-10-31T10:00:00Z", latest_task_id: null, latest_task_status: null });
const message = (id: string, conversationId = "c1") => ({ id, task_id: `${conversationId}-task`, role: "assistant", content: id, created_at: "2026-10-31T10:00:00Z" });
afterEach(() => vi.unstubAllGlobals());

it("preserves loaded conversation windows when a new first page advances and keeps paging passive", async () => {
  let refresh = false;
  const calls: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options: RequestInit) => {
    calls.push(options);
    if (String(input).includes("cursor=")) return json({ items: [conversation("c3")], next_cursor: null, has_more: false });
    return json({ items: refresh ? [conversation("c0"), conversation("c1")] : [conversation("c1"), conversation("c2")], next_cursor: "older", has_more: true });
  }));
  const { result } = renderHook(() => useConversations());
  await waitFor(() => expect(result.current.items).toHaveLength(2));
  await act(async () => { await result.current.loadMore(); });
  refresh = true; await act(async () => { await result.current.reload(true); });
  expect(result.current.items.map((item) => item.id)).toEqual(["c0", "c1", "c2", "c3"]);
  expect(result.current.hasMore).toBe(false);
  calls.forEach((options) => expect(new Headers(options.headers).get("X-Personal-Bot-Passive")).toBe("1"));
});

it("preserves message history and reopens the cursor when latest messages leave a gap", async () => {
  let refresh = false;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const path = String(input);
    if (path.includes("before=bridge")) return json({ items: [message("middle")], next_before: null, has_more: false });
    if (path.includes("before=older")) return json({ items: [message("old")], next_before: null, has_more: false });
    return json({ items: [message(refresh ? "new" : "first")], next_before: refresh ? "bridge" : "older", has_more: true });
  }));
  const { result } = renderHook(() => useConversationMessages("c1", true));
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  await act(async () => { await result.current.loadMore(); }); expect(result.current.hasMore).toBe(false);
  refresh = true; await act(async () => { await result.current.reload(true); }); expect(result.current.hasMore).toBe(true);
  await act(async () => { await result.current.loadMore(); });
  expect(result.current.items.map((item) => item.id)).toEqual(["first", "middle", "new", "old"]);
  expect(result.current.hasMore).toBe(false);
});

it("discards a delayed older message page after changing conversations and allows loading the new conversation", async () => {
  let resolve: ((response: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn((input: unknown) => {
    const path = String(input);
    if (path.includes("c1/messages") && path.includes("before=")) return new Promise<Response>((done) => { resolve = done; });
    const id = path.includes("c2/messages") ? "c2" : "c1";
    return Promise.resolve(json({ items: [message(id, id)], next_before: "older", has_more: true }));
  }));
  const { result, rerender } = renderHook(({ id }) => useConversationMessages(id, true), { initialProps: { id: "c1" } });
  await waitFor(() => expect(result.current.items).toHaveLength(1));
  let pending: Promise<void>;
  act(() => { pending = result.current.loadMore(); });
  rerender({ id: "c2" });
  await waitFor(() => expect(result.current.items[0]?.id).toBe("c2"));
  expect(result.current.loadingMore).toBe(false);
  await act(async () => { resolve!(json({ items: [message("late-c1")], next_before: null, has_more: false })); await pending; });
  expect(result.current.items.map((item) => item.id)).toEqual(["c2"]);
});
