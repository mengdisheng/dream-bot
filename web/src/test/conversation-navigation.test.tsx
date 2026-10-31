import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";

const json = (value: unknown) => new Response(JSON.stringify(value));
const task = { id: "t1", conversation_id: "c1", title: "任务执行标题", status: "succeeded", revision: 1, created_at: "2026-10-31T10:00:00Z", updated_at: "2026-10-31T10:00:00Z", connection_id: null, model_id: null, tool_count: 0, token_count: 0, max_tools: 8, max_tokens: 1000, max_active_seconds: 300, active_seconds: 1, token_usage_estimated: false, unresolved_effect_count: 0 };
const conversation = { id: "c1", title: "一直保留的会话标题", created_at: task.created_at, updated_at: task.updated_at, latest_task_id: task.id, latest_task_status: task.status };
beforeEach(() => {
  location.hash = "";
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
});
afterEach(() => { vi.unstubAllGlobals(); location.hash = ""; });

it("uses real recent conversations in the shell and new conversation clears historical content", async () => {
  const calls: { path: string; options?: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input); calls.push({ path, options });
    if (path === "/api/auth/status") return json({ initialized: true, authenticated: true, owner: { id: "owner", username: "测试用户" }, csrf_token: "test-csrf" });
    if (path.startsWith("/api/conversations?")) return json({ items: [conversation], next_cursor: null, has_more: false });
    if (path.startsWith("/api/conversations/c1?")) return json({ ...conversation, tasks: [task], next_before: null, has_more: false });
    if (path.includes("/messages?")) return json({ items: [{ id: "m1", task_id: "t1", role: "assistant", content: "已经完成的真实回复", created_at: task.created_at }], next_before: null, has_more: false });
    if (path === "/api/tasks/t1") return json(task);
    if (path === "/api/runtime") return json({ state: "READY", phase: "ready", runtime_id: "runtime", elapsed_seconds: 1, error: null, diagnostic_id: null });
    if (path === "/api/browser/status") return json({ available: true, running: false, mode: "idle", browser_epoch: 0, device_id: null, task_id: null, human_idle_deadline: null, width: 1280, height: 720, url: null });
    return json({ items: [], next_after: 0, has_more: false });
  }));
  render(<App />);
  await screen.findByText("已经完成的真实回复");
  expect(screen.getByRole("heading", { name: "一直保留的会话标题" })).toBeInTheDocument();
  const navigation = screen.getByRole("complementary", { name: "会话导航" });
  expect(within(navigation).getByRole("button", { name: "文件与成果" })).toBeInTheDocument();
  expect(within(navigation).getByRole("button", { name: "设置" })).toBeInTheDocument();
  expect(within(navigation).queryByRole("button", { name: "浏览器" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "新对话" }));
  await waitFor(() => expect(location.hash).toBe("#chat?new=1"));
  expect(screen.getByRole("heading", { name: "今天，想一起做点什么？" })).toBeInTheDocument();
  expect(screen.queryByText("已经完成的真实回复")).not.toBeInTheDocument();
  const before = calls.length;
  await act(async () => { await Promise.resolve(); });
  expect(calls.slice(before).some((call) => call.path.startsWith("/api/tasks/"))).toBe(false);
  const passive = calls.filter((call) => call.path !== "/api/auth/status");
  passive.forEach((call) => expect(new Headers(call.options?.headers).get("X-Personal-Bot-Passive")).toBe("1"));
});
