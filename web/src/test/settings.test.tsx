import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { OverageApprovalStatus, Settings } from "../pages/Settings";
import { connectionSchema } from "../types";

const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
const capabilities = {
  auth: "not_tested",
  text: "not_tested",
  stream: "not_tested",
  tools: "not_tested",
  vision: "not_tested",
};
const connection = {
  id: "c1",
  display_name: "测试标准API",
  provider: "custom",
  region: "cn",
  billing_mode: "api",
  protocol: "chat_completions",
  base_url: "https://api.example.test/plan/v3",
  model_id: "test-model",
  capabilities,
  revision: 1,
  enabled: true,
  entitlements: {
    backend: { status: "documented_supported" },
    chat: { status: "documented_supported" },
    agent: { status: "documented_supported" },
  },
};
// jsdom lacks native dialog methods; real-browser acceptance verifies top-layer
// placement, native focus containment, and Escape behavior separately.
beforeEach(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; } },
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
it("saves a model connection without invoking validation and only validates on click", async () => {
  const operations: { path: string; method: string; body: unknown }[] = [];
  let saved = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input),
        method = options?.method ?? "GET";
      operations.push({
        path,
        method,
        body: options?.body ? JSON.parse(String(options.body)) : undefined,
      });
      if (path === "/api/model-connections" && method === "POST") {
        saved = true;
        return json(connection);
      }
      if (path === "/api/model-connections")
        return json({ items: saved ? [connection] : [] });
      if (path === "/api/model-presets") return json({ items: [] });
      if (path === "/api/settings")
        return json({
          default_chat_connection: null,
          default_agent_connection: null,
          budgets: {
            max_tools: 30,
            max_tokens: 100000,
            max_active_seconds: 900,
          },
          timezone: "Asia/Shanghai",
        });
      if (path.endsWith("/validate"))
        return json({
          checks: {
            ...capabilities,
            auth: "passed",
            text: "passed",
            stream: "passed",
            tools: "passed",
          },
          errors: [],
          validated_at: "2026-10-31T10:00:00Z",
        });
      return json({});
    }),
  );
  render(<Settings active />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "添加连接" }));
  await user.type(screen.getByLabelText("连接名称"), "测试标准API");
  await user.type(screen.getByLabelText("模型 ID"), "test-model");
  await user.type(
    screen.getByLabelText("API 地址"),
    "https://api.example.test/plan/v3",
  );
  await user.type(screen.getByLabelText("API Key"), "test-only-secret");
  await user.click(screen.getByRole("button", { name: "仅保存配置" }));
  await screen.findByRole("heading", { name: "测试标准API" });
  expect(
    operations.filter((item) => item.path.endsWith("/validate")),
  ).toHaveLength(0);
  const post = operations.find((item) => item.method === "POST");
  expect(post?.body).toMatchObject({
    base_url: "https://api.example.test/plan/v3",
    api_key: "test-only-secret",
  });
  expect(
    screen.queryByDisplayValue("test-only-secret"),
  ).not.toBeInTheDocument();
  await user.click(screen.getByText("连接详情与验证"));
  await user.click(screen.getByRole("button", { name: "验证连接" }));
  await waitFor(() =>
    expect(
      operations.filter((item) => item.path.endsWith("/validate")),
    ).toHaveLength(1),
  );
  await screen.findByText("验证结束，请查看分项结果。");
});
it("does not request browser push permission merely by opening notification settings", async () => {
  const requestPermission = vi.fn();
  vi.stubGlobal("Notification", { permission: "default", requestPermission });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const path = String(input);
      if (path === "/api/settings")
        return json({
          default_chat_connection: null,
          default_agent_connection: null,
          budgets: {
            max_tools: 30,
            max_tokens: 100000,
            max_active_seconds: 900,
          },
          timezone: "Asia/Shanghai",
        });
      if (path === "/api/push/public-key")
        return json({ public_key: null, configured: false });
      return json({ items: [] });
    }),
  );
  render(<Settings active={false} />);
  await userEvent.setup().click(screen.getByRole("tab", { name: "通知" }));
  await screen.findByText("服务端推送公钥尚未配置。");
  expect(requestPermission).not.toHaveBeenCalled();
});
it("uses the chosen billing product endpoint, previews one suffix, and makes no model probes", async () => {
  const requests: { path: string; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, options?: RequestInit) => {
      const path = String(input);
      requests.push({ path, method: options?.method ?? "GET" });
      if (path === "/api/model-presets")
        return json({
          items: [
            {
              id: "test-provider",
              name: "测试供应商",
              billing_modes: ["api", "token_plan"],
              protocols: ["chat_completions"],
              base_urls: {
                api: "https://api.example.test/v1",
                token_plan: "https://plan.example.test/plan/v3",
              },
              notes: "套餐与标准 API 分开配置。",
              plan_entitlements: {
                backend: {
                  status: "documented_restricted",
                  quote: "此套餐不支持自建后端",
                },
              },
            },
          ],
        });
      return json({ items: [] });
    }),
  );
  render(<Settings active />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "添加连接" }));
  await screen.findByRole("option", { name: "测试供应商" });
  await user.selectOptions(screen.getByLabelText("供应商"), "test-provider");
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://api.example.test/v1",
  );
  await user.selectOptions(screen.getByLabelText("计费入口"), "token_plan");
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://plan.example.test/plan/v3",
  );
  expect(screen.getByText("此套餐不支持自建后端")).toBeInTheDocument();
  expect(
    screen.getByText(
      "请求地址预览：https://plan.example.test/plan/v3/chat/completions",
    ),
  ).toBeInTheDocument();
  await user.clear(screen.getByLabelText("API 地址"));
  await user.type(
    screen.getByLabelText("API 地址"),
    "https://plan.example.test/plan/v3/chat/completions/",
  );
  expect(
    screen.getByText(
      "请求地址预览：https://plan.example.test/plan/v3/chat/completions",
    ),
  ).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("计费入口"), "api");
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://api.example.test/v1",
  );
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  expect(requests.some((request) => request.path.includes("/validate"))).toBe(
    false,
  );
});
it("shows a temporary purchased-credit grant and distinguishes expiry from task-slot exhaustion", () => {
  const now = Date.parse("2026-10-31T10:00:00Z");
  const plan = connectionSchema.parse({
    ...connection,
    billing_mode: "m_plan",
    provider_overage_state: "enabled",
    provider_overage_kind: "purchased_credits",
    overage_approval: {
      active: true,
      can_create_task: true,
      kind: "purchased_credits",
      reason: null,
      expires_at: "2026-10-31T10:30:00Z",
      max_tasks: 3,
      tasks_used: 1,
      remaining_tasks: 2,
      budgets: { max_tools: 8, max_tokens: 100000, max_active_seconds: 300 },
    },
  });
  const { rerender } = render(<OverageApprovalStatus connection={plan} now={now} />);
  expect(screen.getByRole("status", { name: "积分包临时授权" })).toHaveTextContent("授权有效");
  expect(screen.getByText(/每任务最多 8 次工具/)).toHaveTextContent(/100,000 tokens、\s*300 秒/);
  expect(screen.getByText(/任务名额：已用 1 \/ 3/)).toHaveTextContent("剩余 2");
  rerender(
    <OverageApprovalStatus
      connection={{
        ...plan,
        overage_approval: {
          ...plan.overage_approval!,
          can_create_task: false,
          tasks_used: 3,
          remaining_tasks: 0,
        },
      }}
      now={now}
    />,
  );
  expect(screen.getByText(/本次新任务名额已用完/)).toHaveTextContent("已登记任务可在有效期内继续");
  rerender(<OverageApprovalStatus connection={plan} now={Date.parse("2026-10-31T10:30:00Z")} />);
  expect(screen.getByText(/授权已到期/)).toBeInTheDocument();
  expect(screen.queryByText(/授权有效/)).not.toBeInTheDocument();
});
it("expires the displayed permission without another server request or keepalive", async () => {
  vi.useFakeTimers();
  const now = Date.parse("2026-10-31T10:00:00Z");
  vi.setSystemTime(now);
  const plan = {
    ...connection,
    billing_mode: "m_plan",
    provider_overage_state: "enabled",
    provider_overage_kind: "purchased_credits",
    capabilities: { ...capabilities, auth: "passed", text: "passed", stream: "passed", tools: "passed" },
    overage_approval: {
      active: true,
      can_create_task: true,
      kind: "purchased_credits",
      reason: null,
      expires_at: new Date(now + 1000).toISOString(),
      max_tasks: 3,
      tasks_used: 1,
      remaining_tasks: 2,
      budgets: { max_tools: 8, max_tokens: 100000, max_active_seconds: 300 },
    },
  };
  const fetcher = vi.fn(async (input: unknown, options?: RequestInit) => {
    expect(options?.method ?? "GET").toBe("GET");
    const path = String(input);
    if (path === "/api/model-connections") return json({ items: [plan] });
    if (path === "/api/settings") return json({
      default_chat_connection: null,
      default_agent_connection: null,
      budgets: { max_tools: 30, max_tokens: 100000, max_active_seconds: 900 },
      timezone: "Asia/Shanghai",
    });
    return json({ items: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  await act(async () => { render(<Settings active={false} />); });
  fireEvent.click(screen.getByText("连接详情与验证"));
  const button = screen.getByRole("button", { name: "设为问答模型" });
  expect(button).toBeEnabled();
  const initialRequests = fetcher.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(1002); });
  expect(button).toBeDisabled();
  expect(screen.getByText(/授权已到期/)).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(initialRequests);
});

const settings = (defaultId: string | null = null) => ({
  default_chat_connection: defaultId,
  default_agent_connection: defaultId,
  budgets: { max_tools: 30, max_tokens: 100000, max_active_seconds: 900 },
  timezone: "Asia/Shanghai",
});

it("opens a focused editor, preserves an unchanged key, and restores focus after saving and refreshing", async () => {
  const operations: { path: string; method: string; body: unknown }[] = [];
  let current = connection;
  let finishRefresh: ((value: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input), method = options?.method ?? "GET";
    const body = options?.body ? JSON.parse(String(options.body)) : undefined;
    operations.push({ path, method, body });
    if (path === "/api/model-connections/c1" && method === "PATCH") {
      current = { ...connection, display_name: body.display_name, revision: 2 };
      return json(current);
    }
    if (path === "/api/model-connections") {
      if (current.revision === 2) return await new Promise<Response>((resolve) => { finishRefresh = resolve; });
      return json({ items: [current] });
    }
    if (path === "/api/settings") return json(settings("c1"));
    return json({ items: [] });
  }));
  render(<Settings active />);
  await screen.findByRole("heading", { name: connection.display_name });
  const user = userEvent.setup();
  const edit = screen.getByRole("button", { name: "编辑" });
  await user.click(edit);
  const dialog = screen.getByRole("dialog", { name: "编辑连接" });
  expect(dialog).toHaveAttribute("open");
  expect(screen.getByLabelText("连接名称")).toHaveFocus();
  expect(screen.getByLabelText("替换 API Key（留空保留原密钥）")).toHaveValue("");
  await user.clear(screen.getByLabelText("连接名称"));
  await user.type(screen.getByLabelText("连接名称"), "更新后的连接");
  await user.click(within(dialog).getByRole("button", { name: "仅保存配置" }));
  expect(screen.getByRole("button", { name: "添加连接" })).toBeDisabled();
  await act(async () => { finishRefresh?.(json({ items: [current] })); });
  await screen.findByRole("heading", { name: "更新后的连接" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "添加连接" })).toHaveFocus();
  expect(operations.filter((item) => item.method !== "GET")).toEqual([
    { path: "/api/model-connections/c1", method: "PATCH", body: {
      display_name: "更新后的连接", provider: "custom", region: "cn", billing_mode: "api",
      protocol: "chat_completions", base_url: connection.base_url, model_id: "test-model",
      enabled: true, expected_revision: 1,
    } },
  ]);
});

it("cancels the focused editor through Escape without saving and restores the trigger", async () => {
  const fetcher = vi.fn(async (input: unknown, options?: RequestInit) => {
    expect(options?.method ?? "GET").toBe("GET");
    if (String(input) === "/api/model-connections") return json({ items: [connection] });
    if (String(input) === "/api/settings") return json(settings());
    return json({ items: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<Settings active />);
  await screen.findByRole("heading", { name: connection.display_name });
  const edit = screen.getByRole("button", { name: "编辑" });
  await userEvent.setup().click(edit);
  const dialog = screen.getByRole("dialog", { name: "编辑连接" });
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(edit).toHaveFocus();
  expect(fetcher.mock.calls.every((call) => (call[1]?.method ?? "GET") === "GET")).toBe(true);
});

it("only deletes after confirmation, prevents duplicate submission, and refreshes cleared defaults passively", async () => {
  const operations: { path: string; method: string; passive: boolean }[] = [];
  let removed = false;
  let finishDelete: ((value: Response) => void) | undefined;
  const finishReads: (() => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input), method = options?.method ?? "GET";
    operations.push({ path, method, passive: new Headers(options?.headers).get("X-Personal-Bot-Passive") === "1" });
    if (method === "DELETE") return await new Promise<Response>((resolve) => { finishDelete = resolve; });
    if (removed && ["/api/model-connections", "/api/settings"].includes(path)) {
      return await new Promise<Response>((resolve) => {
        finishReads.push(() => resolve(json(path === "/api/model-connections" ? { items: [] } : settings())));
      });
    }
    if (path === "/api/model-connections") return json({ items: removed ? [] : [connection] });
    if (path === "/api/settings") return json(settings(removed ? null : "c1"));
    return json({ items: [] });
  }));
  render(<Settings active />);
  await screen.findByRole("heading", { name: connection.display_name });
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: "删除连接" });
  await user.click(trigger);
  let dialog = screen.getByRole("dialog", { name: "删除模型连接" });
  expect(dialog).toHaveTextContent(connection.display_name);
  expect(dialog).toHaveTextContent("历史任务、用量记录和文件保留");
  expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(trigger).toHaveFocus();
  expect(operations.filter((item) => item.method === "DELETE")).toHaveLength(0);
  await user.click(trigger);
  dialog = screen.getByRole("dialog", { name: "删除模型连接" });
  await user.click(within(dialog).getByRole("button", { name: "确认删除连接" }));
  expect(within(dialog).getByRole("button", { name: "正在删除…" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeDisabled();
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(dialog).toBeInTheDocument();
  await act(async () => {
    removed = true;
    finishDelete?.(json({ deleted: true, id: "c1", cleared_default_roles: ["chat", "agent"] }));
  });
  await screen.findByText("连接已删除，对应的默认模型设置已清除。历史任务和文件已保留。");
  expect(screen.getByRole("button", { name: "添加连接" })).toBeDisabled();
  await waitFor(() => expect(finishReads).toHaveLength(2));
  await act(async () => { finishReads.forEach((finish) => finish()); });
  await waitFor(() => expect(screen.getByRole("button", { name: "添加连接" })).toHaveFocus());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: connection.display_name })).not.toBeInTheDocument();
  expect(operations.filter((item) => item.method === "DELETE")).toEqual([
    { path: "/api/model-connections/c1?expected_revision=1", method: "DELETE", passive: false },
  ]);
  const afterDelete = operations.slice(operations.findIndex((item) => item.method === "DELETE") + 1);
  expect(afterDelete.some((item) => item.path === "/api/settings")).toBe(true);
  expect(afterDelete.every((item) => item.method === "GET" && item.passive)).toBe(true);
});

it.each([
  ["connection_in_use", "此连接有未结束任务，请先完成或取消任务", false],
  ["revision_conflict", "模型配置已经更新", true],
])("keeps the delete confirmation after %s without retrying or invoking a model", async (code, message, stale) => {
  const operations: { path: string; method: string; passive: boolean }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input), method = options?.method ?? "GET";
    operations.push({ path, method, passive: new Headers(options?.headers).get("X-Personal-Bot-Passive") === "1" });
    if (method === "DELETE") return new Response(JSON.stringify({ error: { code, message, retryable: false } }), { status: 409, headers: { "Content-Type": "application/json" } });
    if (path === "/api/model-connections") return json({ items: [connection] });
    if (path === "/api/settings") return json(settings("c1"));
    return json({ items: [] });
  }));
  render(<Settings active />);
  await screen.findByRole("heading", { name: connection.display_name });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "删除连接" }));
  const dialog = screen.getByRole("dialog", { name: "删除模型连接" });
  await user.click(within(dialog).getByRole("button", { name: "确认删除连接" }));
  await within(dialog).findByText(message);
  expect(dialog).toBeInTheDocument();
  expect(operations.filter((item) => item.method !== "GET")).toHaveLength(1);
  if (stale) {
    expect(within(dialog).getByRole("button", { name: "确认删除连接" })).toBeDisabled();
    expect(within(dialog).getByText("连接状态已更新，请取消后重新核对再删除。")).toBeInTheDocument();
    expect(operations.slice(-2).every((item) => item.method === "GET" && item.passive)).toBe(true);
  }
});

it.each(["connections", "settings"])("waits for an in-flight %s refresh before opening mutation dialogs", async (slowResource) => {
  const operations: { path: string; method: string; body: unknown }[] = [];
  const refreshed = { ...connection, display_name: "服务端已更新的连接", revision: 2 };
  let current = connection;
  let connectionReads = 0, settingsReads = 0;
  let finishRefresh: ((value: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, options?: RequestInit) => {
    const path = String(input), method = options?.method ?? "GET";
    const body = options?.body ? JSON.parse(String(options.body)) : undefined;
    operations.push({ path, method, body });
    if (path === "/api/model-connections/c1" && method === "PATCH") {
      current = { ...refreshed, display_name: body.display_name, revision: 3 };
      return json(current);
    }
    if (path === "/api/model-connections") {
      connectionReads++;
      if (connectionReads === 2) {
        current = refreshed;
        if (slowResource === "connections") return await new Promise<Response>((resolve) => { finishRefresh = resolve; });
      }
      return json({ items: [current] });
    }
    if (path === "/api/settings") {
      settingsReads++;
      if (settingsReads === 2 && slowResource === "settings") return await new Promise<Response>((resolve) => { finishRefresh = resolve; });
      return json(settings());
    }
    return json({ items: [] });
  }));
  render(<Settings active />);
  await screen.findByRole("heading", { name: connection.display_name });
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "编辑" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "主动读取" }));
  for (const name of ["添加连接", "编辑", "删除连接"]) {
    const button = screen.getByRole("button", { name });
    expect(button).toBeDisabled();
    await user.click(button);
  }
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(operations.every((item) => item.method === "GET")).toBe(true);
  await act(async () => {
    finishRefresh?.(json(slowResource === "connections" ? { items: [refreshed] } : settings()));
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "编辑" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "编辑" }));
  expect(screen.getByLabelText("连接名称")).toHaveValue(refreshed.display_name);
  await user.clear(screen.getByLabelText("连接名称"));
  await user.type(screen.getByLabelText("连接名称"), "刷新后保存的连接");
  await user.click(screen.getByRole("button", { name: "仅保存配置" }));
  await screen.findByRole("heading", { name: "刷新后保存的连接" });
  expect(operations.find((item) => item.method === "PATCH")?.body).toMatchObject({ expected_revision: 2 });
  expect(screen.queryByRole("heading", { name: connection.display_name })).not.toBeInTheDocument();
});


