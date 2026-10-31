import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { z } from "zod";
import { useRemote } from "../hooks";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
it("stops automatic reads after sleeping and permits an explicit active read", async () => {
  const fetcher = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        error: {
          code: "runtime_sleeping",
          message: "sleep",
          retryable: true,
        },
      }),
      { status: 503 },
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  const schema = z.object({ value: z.string() });
  const { result } = renderHook(() =>
    useRemote("/api/tasks", schema, false, 20),
  );
  await waitFor(() => expect(result.current.error).toBeDefined());
  const count = fetcher.mock.calls.length;
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 70));
  });
  expect(fetcher).toHaveBeenCalledTimes(count);
  fetcher.mockResolvedValue(
    new Response(JSON.stringify({ value: "awake" }), { status: 200 }),
  );
  await act(async () => {
    await result.current.reload();
  });
  expect(result.current.data?.value).toBe("awake");
  const last = fetcher.mock.calls.at(-1);
  expect(
    new Headers((last?.[1] as RequestInit).headers).has(
      "X-Personal-Bot-Passive",
    ),
  ).toBe(false);
});

it("rejects reload closures from a previously selected path", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    calls.push(String(input));
    return new Response(JSON.stringify({ value: String(input) }));
  }));
  const schema = z.object({ value: z.string() });
  const { result, rerender } = renderHook(({ path }) => useRemote(path, schema, false), { initialProps: { path: "/api/tasks/t1" } });
  await waitFor(() => expect(result.current.data?.value).toBe("/api/tasks/t1"));
  const oldReload = result.current.reload;
  rerender({ path: "/api/tasks/t2" });
  await waitFor(() => expect(result.current.data?.value).toBe("/api/tasks/t2"));
  await act(async () => { await oldReload(true); });
  expect(calls).toEqual(["/api/tasks/t1", "/api/tasks/t2"]);
  expect(result.current.data?.value).toBe("/api/tasks/t2");
});
