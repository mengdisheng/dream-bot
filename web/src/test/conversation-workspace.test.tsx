import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationWorkspace } from "../pages/ConversationWorkspace";

vi.mock("../pages/Browser", () => ({ Browser: ({ expectedTaskId }: { expectedTaskId: string }) => <div data-testid="embedded-browser">浏览器会话 {expectedTaskId}</div> }));
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const empty = { items: [], next_before: null, next_cursor: null, next_after: 0, has_more: false };
const task = (id = "t1", status = "succeeded", conversationId = "c1") => ({ id, conversation_id: conversationId, title: `执行 ${id}`, status, revision: 7,
  created_at: "2026-10-31T10:00:00Z", updated_at: "2026-10-31T10:01:00Z", connection_id: "model-service", model_id: "test-model", tool_count: 0, token_count: 0,
  max_tools: 8, max_tokens: 1000, max_active_seconds: 300, active_seconds: 1, token_usage_estimated: false, unresolved_effect_count: 0, execution_mode: "agent" });
const message = (id: string, content: string, taskId = "t1") => ({ id, task_id: taskId, role: "assistant", content, created_at: "2026-10-31T10:00:00Z" });
const browser = (taskId: string | null = null) => ({ available: true, running: !!taskId, mode: taskId ? "agent" : "idle", browser_epoch: 3, device_id: null, task_id: taskId, human_idle_deadline: null, width: 1280, height: 720, url: taskId ? "https://example.com/" : null });
const callbacks = () => ({ ownerId: "owner", onAccepted: vi.fn(), onChanged: vi.fn(), attached: [], onAttached: vi.fn(), onFiles: vi.fn(), onSettings: vi.fn() });
function conversation(tasks = [task()], id = "c1") {
  return { id, title: "保留下来的会话", created_at: "2026-10-31T09:00:00Z", updated_at: "2026-10-31T10:01:00Z", latest_task_id: tasks.at(-1)?.id ?? null, latest_task_status: tasks.at(-1)?.status ?? null, tasks, next_before: null, has_more: false };
}
type Call = { path: string; options?: RequestInit };
function mockBackend({ tasks = [task()], browserTask = null, messages = [message("m1", "真实的历史回复")], files = [] as unknown[] }: { tasks?: ReturnType<typeof task>[]; browserTask?: string | null; messages?: ReturnType<typeof message>[]; files?: unknown[] } = {}) {
  const calls: Call[] = [];
  const fetcher = vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input); calls.push({ path, options });
    if (/^\/api\/conversations\/[^/?]+\?/.test(path)) return json(conversation(tasks));
    if (path.includes("/messages?")) return json({ ...empty, items: messages });
    if (path === "/api/browser/status") return json(browser(browserTask));
    if (path === "/api/files") return json({ items: files });
    if (/^\/api\/tasks\/[^/?]+$/.test(path)) return json(tasks.find((item) => path.endsWith(`/${item.id}`)) ?? task());
    return json(empty);
  });
  vi.stubGlobal("fetch", fetcher);
  return { calls, fetcher };
}
class Stream extends EventTarget {
  static instances: Stream[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { super(); Stream.instances.push(this); }
  close() { this.closed = true; }
  open() { this.onopen?.(); }
  delta(seq: number, text: string) { this.dispatchEvent(new MessageEvent("task_event", { data: JSON.stringify({ seq, task_id: "t1", kind: "model.text_delta", data: { text }, created_at: "2026-10-31T10:00:00Z" }) })); }
}
beforeEach(() => {
  localStorage.clear(); Stream.instances = [];
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.stubGlobal("EventSource", Stream);
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("starts a genuinely empty new conversation without reading or subscribing an old task", async () => {
  const { calls, fetcher } = mockBackend();
  const props = callbacks();
  render(<ConversationWorkspace {...props} conversationId={null} />);
  expect(screen.getByRole("heading", { name: "今天，想一起做点什么？" })).toBeInTheDocument();
  expect(calls).toHaveLength(0); expect(Stream.instances).toHaveLength(0);
  fetcher.mockImplementation(async (input, options) => {
    calls.push({ path: String(input), options });
    const body = JSON.parse(String(options?.body)) as Record<string, unknown>;
    return json({ accepted: true, request_id: body.client_request_id, conversation_id: "new-conversation", task_id: "new-task", task_revision: 1 });
  });
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox", { name: "任务内容" }), "帮我研究一个主题");
  await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(props.onAccepted).toHaveBeenCalledWith("new-conversation"));
  const packet = JSON.parse(String(calls[0].options?.body)) as Record<string, unknown>;
  expect(packet).toMatchObject({ mode: "new", execution_mode: "agent", text: "帮我研究一个主题" });
  expect(packet).not.toHaveProperty("task_id"); expect(packet).not.toHaveProperty("conversation_id");
});

it("continues completed conversation history as a new execution with the same conversation id and passive reads", async () => {
  const { calls, fetcher } = mockBackend({ messages: [message("m1", "第一次执行的回复", "t0"), message("m2", "第二次执行的回复")] });
  const props = callbacks();
  render(<ConversationWorkspace {...props} conversationId="c1" />);
  await screen.findByText("第一次执行的回复"); await screen.findByText("第二次执行的回复");
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation(async (input, options) => {
    if (String(input) === "/api/requests") { calls.push({ path: String(input), options }); const body = JSON.parse(String(options?.body)) as Record<string, unknown>; return json({ accepted: true, request_id: body.client_request_id, conversation_id: "c1", task_id: "t2", task_revision: 1 }); }
    return defaultFetch!(input, options);
  });
  const user = userEvent.setup(); await user.type(screen.getByRole("textbox", { name: "任务内容" }), "继续整理"); await user.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(props.onAccepted).toHaveBeenCalledWith("c1"));
  const post = calls.find((call) => call.path === "/api/requests");
  expect(JSON.parse(String(post?.options?.body))).toMatchObject({ conversation_id: "c1", mode: "new" });
  expect(JSON.parse(String(post?.options?.body))).not.toHaveProperty("task_id");
  for (const call of calls.filter((item) => item.options?.method === "GET")) expect(new Headers(call.options?.headers).get("X-Personal-Bot-Passive")).toBe("1");
});

it("keeps uncertain delivery locked until receipt lookup and retries using the original id", async () => {
  const packets: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    if (String(input).startsWith("/api/requests/")) return json({ error: { code: "not_found", message: "尚未接收", retryable: false } }, 404);
    const packet = JSON.parse(String(options?.body)) as Record<string, unknown>; packets.push(packet);
    if (packets.length === 1) throw new TypeError("connection lost");
    return json({ accepted: true, request_id: packet.client_request_id, conversation_id: "c1", task_id: "t1", task_revision: 1 });
  }));
  const props = callbacks(); render(<ConversationWorkspace {...props} attached={["f1"]} conversationId={null} />);
  const user = userEvent.setup(); await user.type(screen.getByRole("textbox", { name: "任务内容" }), "资料整理"); await user.click(screen.getByRole("button", { name: "发送" }));
  await screen.findByText("未确认接收"); expect(packets).toHaveLength(1); expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "核对接收" })); await screen.findByText("尚未查到接收记录");
  await user.click(screen.getByRole("button", { name: "按原编号重试" }));
  await waitFor(() => expect(props.onAccepted).toHaveBeenCalledWith("c1"));
  expect(packets).toHaveLength(2); expect(packets[0]).toEqual(packets[1]); expect(packets[1].file_ids).toEqual(["f1"]);
});

it("does not navigate back or clear the next composer's attachments when an old send resolves after switching", async () => {
  let resolve: ((response: Response) => void) | undefined;
  const props = callbacks(); const { fetcher } = mockBackend();
  const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation((input, options) => String(input) === "/api/requests" ? new Promise<Response>((done) => { resolve = done; }) : defaultFetch!(input, options));
  const view = render(<ConversationWorkspace key="new" {...props} conversationId={null} />);
  fireEvent.change(screen.getByRole("textbox", { name: "任务内容" }), { target: { value: "旧页面提交" } }); fireEvent.submit(screen.getByRole("textbox", { name: "任务内容" }).closest("form")!);
  await waitFor(() => expect(resolve).toBeDefined());
  view.rerender(<ConversationWorkspace key="c1" {...props} conversationId="c1" />);
  await screen.findByText("真实的历史回复");
  await act(async () => { resolve!(json({ accepted: true, request_id: "accepted", conversation_id: "old", task_id: "t-old", task_revision: 1 })); });
  expect(props.onAccepted).not.toHaveBeenCalled(); expect(props.onAttached).not.toHaveBeenCalled(); expect(props.onChanged).toHaveBeenCalled();
});

it("opens only this conversation's browser and stops its associated task through a revision-checked confirmation", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const { calls, fetcher } = mockBackend({ tasks: [task("t1", "running")], browserTask: "t1" });
  const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation(async (input, options) => {
    if (String(input).endsWith("/commands")) { calls.push({ path: String(input), options }); return json({ error: { code: "revision_conflict", message: "任务已变化", retryable: false } }, 409); }
    return defaultFetch!(input, options);
  });
  render(<ConversationWorkspace {...callbacks()} conversationId="c1" />);
  const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "打开浏览器" }));
  const panel = screen.getByRole("dialog", { name: "会话浏览器" });
  expect(within(panel).getByRole("button", { name: "返回会话" })).toBeVisible();
  await within(panel).findByText("浏览器会话 t1");
  await waitFor(() => expect(within(panel).getByRole("button", { name: "停止任务" })).toBeEnabled());
  await user.click(within(panel).getByRole("button", { name: "停止任务" }));
  expect(within(panel).getByText(/外部操作不能通过取消撤回/)).toBeInTheDocument();
  expect(calls.filter((call) => call.path.endsWith("/commands"))).toHaveLength(0);
  await user.click(within(panel).getByRole("button", { name: "确认停止后续步骤" }));
  await within(panel).findByText(/数据已变化，请重新读取后确认/);
  const commands = calls.filter((call) => call.path.endsWith("/commands")); expect(commands).toHaveLength(1);
  expect(JSON.parse(String(commands[0].options?.body))).toMatchObject({ action: "cancel", expected_revision: 7 });
});

it("unmounts the viewer when changing conversations and hides a browser belonging to another conversation", async () => {
  const { fetcher } = mockBackend({ browserTask: "t1" });
  const props = callbacks(); const view = render(<ConversationWorkspace key="c1" {...props} conversationId="c1" />);
  await userEvent.click(await screen.findByRole("button", { name: "打开浏览器" })); await screen.findByTestId("embedded-browser");
  const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation(async (input, options) => {
    if (String(input).startsWith("/api/conversations/c2?")) return json(conversation([task("t2", "succeeded", "c2")], "c2"));
    return defaultFetch!(input, options);
  });
  view.rerender(<ConversationWorkspace key="c2" {...props} conversationId="c2" />);
  await screen.findByText("真实的历史回复");
  expect(screen.queryByTestId("embedded-browser")).not.toBeInTheDocument(); expect(screen.queryByRole("button", { name: "打开浏览器" })).not.toBeInTheDocument();
  expect(Stream.instances[0].closed).toBe(true);
});

it("ignores a delayed file preview from the previous conversation", async () => {
  const file = { id: "f1", name: "结果.md", media_type: "text/markdown", size: 10, status: "generated", version: 1, task_id: "t1", created_at: "2026-10-31T10:00:00Z" };
  const { fetcher } = mockBackend({ files: [file] }); const defaultFetch = fetcher.getMockImplementation();
  let resolve: ((response: Response) => void) | undefined;
  fetcher.mockImplementation((input, options) => String(input).endsWith("/f1/download") ? new Promise<Response>((done) => { resolve = done; }) : defaultFetch!(input, options));
  const props = callbacks(); const view = render(<ConversationWorkspace key="c1" {...props} conversationId="c1" />);
  await userEvent.click(await screen.findByRole("button", { name: /结果.md/ })); await waitFor(() => expect(resolve).toBeDefined());
  view.rerender(<ConversationWorkspace key="new" {...props} conversationId={null} />);
  await act(async () => { resolve!(new Response("旧文件预览")); });
  expect(screen.queryByText("旧文件预览")).not.toBeInTheDocument(); expect(screen.queryByLabelText("文件预览")).not.toBeInTheDocument();
});

it("keeps unfinished earlier executions selectable with approval and recovery controls", async () => {
  const old = { ...task("t0", "waiting_approval"), unresolved_effect_count: 1 }; const recent = task();
  const { fetcher } = mockBackend({ tasks: [old, recent] }); const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation(async (input, options) => {
    if (String(input) === "/api/approvals?task_id=t0") return json({ items: [{ id: "a1", task_id: "t0", task_revision: 7, status: "pending", action: { name: "发送邮件", arguments: { recipient: "测试收件人", body: "需要核对的正文" } }, created_at: "2026-10-31T10:00:00Z" }] });
    return defaultFetch!(input, options);
  });
  render(<ConversationWorkspace {...callbacks()} conversationId="c1" />);
  const row = await screen.findByRole("button", { name: /执行 t0/ }); expect(row.closest("details")).toHaveAttribute("open");
  await userEvent.click(row); await screen.findByRole("heading", { name: "发送邮件" });
  expect(screen.getByText("测试收件人")).toBeVisible(); expect(screen.getByText("需要核对的正文")).toBeVisible();
  expect(screen.getByRole("button", { name: "批准本次操作" })).toBeEnabled();
  const sections = screen.getAllByRole("region", { name: "当前执行与审批" });
  await userEvent.click(within(sections[1]).getByRole("button", { name: "执行详情" }));
  expect(within(sections[1]).getByRole("button", { name: "重新执行" })).toBeEnabled();
});

it("renders continuous text deltas without reloading messages, files or full task bodies", async () => {
  const { calls } = mockBackend({ tasks: [task("t1", "running")] });
  render(<ConversationWorkspace {...callbacks()} conversationId="c1" />);
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  await act(async () => { Stream.instances[0].open(); });
  const before = calls.length;
  await act(async () => { for (let i = 1; i <= 100; i++) Stream.instances[0].delta(i * 3, "字"); });
  expect(screen.getByText("字".repeat(100))).toBeInTheDocument();
  expect(calls).toHaveLength(before);
});

it("retains earlier execution windows and reopens paging when more than a page of new executions creates a gap", async () => {
  let refresh = false;
  const initial = task("t50"), earliest = task("t0"), middle = task("t75");
  const newest = Array.from({ length: 50 }, (_, index) => task(`t${100 + index}`));
  const { fetcher } = mockBackend({ tasks: [initial] }); const defaultFetch = fetcher.getMockImplementation();
  fetcher.mockImplementation(async (input, options) => {
    const path = String(input);
    if (path.startsWith("/api/conversations/c1?")) {
      if (path.includes("before=bridge")) return json({ ...conversation([middle]), latest_task_id: newest.at(-1)!.id });
      if (path.includes("before=older")) return json(conversation([earliest]));
      return json({ ...conversation(refresh ? newest : [initial]), next_before: refresh ? "bridge" : "older", has_more: true });
    }
    if (path === `/api/tasks/${newest.at(-1)!.id}`) return json(newest.at(-1));
    return defaultFetch!(input, options);
  });
  render(<ConversationWorkspace {...callbacks()} conversationId="c1" />);
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByText("已完成")).toBeInTheDocument());
  await user.click(screen.getByText("执行记录 · 1 项"));
  await user.click(await screen.findByRole("button", { name: "加载更早执行" }));
  expect(await screen.findByRole("button", { name: /执行 t0/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "加载更早执行" })).not.toBeInTheDocument();
  refresh = true; await user.click(screen.getByRole("button", { name: "读取最新" }));
  await screen.findByRole("button", { name: /执行 t149/ });
  expect(screen.getByRole("button", { name: /执行 t50/ })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "加载更早执行" }));
  expect(await screen.findByRole("button", { name: /执行 t75/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /执行 t0/ })).toBeInTheDocument();
});
