import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Settings } from "../pages/Settings";

const json = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { "Content-Type": "application/json" },
});
afterEach(() => { vi.unstubAllGlobals(); });

const plan = {
  id: "plan",
  display_name: "我的模型",
  provider: "custom",
  region: "cn",
  billing_mode: "m_plan",
  protocol: "chat_completions",
  base_url: "https://models.example.test/v1",
  model_id: "example-model",
  capabilities: { auth: "passed", text: "passed", stream: "passed", tools: "passed", vision: "not_tested" },
  entitlements: {},
  revision: 1,
  enabled: true,
  provider_overage_state: "enabled",
  provider_overage_kind: "purchased_credits",
  overage_approval: {
    active: false,
    can_create_task: false,
    kind: "purchased_credits",
    reason: "expired",
    expires_at: "2026-01-01T00:00:00Z",
    max_tasks: 3,
    tasks_used: 1,
    remaining_tasks: 2,
    budgets: { max_tools: 8, max_tokens: 100000, max_active_seconds: 300 },
  },
};

function fetcher() {
  return vi.fn(async (input: unknown) => {
    const path = String(input);
    if (path === "/api/model-connections") return json({ items: [plan] });
    if (path === "/api/settings") return json({
      default_chat_connection: "plan", default_agent_connection: "plan",
      budgets: { max_tools: 30, max_tokens: 100000, max_active_seconds: 900 },
      timezone: "Asia/Shanghai",
    });
    return json({ items: [] });
  });
}

it("keeps model actions and actual fee warnings visible while diagnostics and validation are collapsed", async () => {
  const request = fetcher();
  vi.stubGlobal("fetch", request);
  render(<Settings active={false} />);
  await screen.findByRole("heading", { name: "我的模型" });
  expect(screen.getByRole("button", { name: "编辑" })).toBeVisible();
  expect(screen.getByRole("button", { name: "删除连接" })).toBeVisible();
  expect(screen.getByText("默认问答模型", { selector: "span" })).toBeVisible();
  expect(screen.getByText("默认执行模型", { selector: "span" })).toBeVisible();
  expect(screen.getByText(/套餐耗尽后可能抵扣已购积分包/)).toBeVisible();
  expect(screen.getByText(/授权已到期，后续模型请求需要重新确认/)).toBeVisible();
  expect(screen.getByRole("button", { name: "验证连接" })).not.toBeVisible();
  expect(screen.getByText(/chat_completions · cn/)).not.toBeVisible();
  const initialReads = request.mock.calls.length;
  await userEvent.setup().click(screen.getByText("连接详情与验证"));
  expect(screen.getByRole("button", { name: "验证连接" })).toBeVisible();
  expect(screen.getByText(/chat_completions · cn/)).toBeVisible();
  expect(request).toHaveBeenCalledTimes(initialReads);
  expect(request.mock.calls.every(([path]) => !String(path).includes("/validate"))).toBe(true);
});

it("exposes everyday settings first and loads advanced tools only after selecting them", async () => {
  const request = fetcher();
  vi.stubGlobal("fetch", request);
  render(<Settings active={false} />);
  await screen.findByRole("heading", { name: "我的模型" });
  expect(within(screen.getByRole("tablist", { name: "设置分类" })).getAllByRole("tab").map((element) => element.textContent)).toEqual([
    "模型", "记忆与偏好", "账户与设备", "通知",
  ]);
  expect(screen.getByRole("tab", { name: "定时任务" })).not.toBeVisible();
  expect(screen.getByRole("link", { name: "任务记录" })).not.toBeVisible();
  expect(request.mock.calls.some(([path]) => String(path) === "/api/schedules")).toBe(false);
  const user = userEvent.setup();
  await user.click(screen.getByText("高级设置"));
  expect(screen.getByRole("tab", { name: "定时任务" })).toBeVisible();
  expect(screen.getByRole("link", { name: "任务记录" })).toHaveAttribute("href", "#tasks");
  expect(request.mock.calls.some(([path]) => String(path) === "/api/schedules")).toBe(false);
  await user.click(screen.getByRole("tab", { name: "定时任务" }));
  await waitFor(() => expect(request.mock.calls.some(([path]) => String(path) === "/api/schedules")).toBe(true));
  await user.click(screen.getByText("高级设置"));
  await screen.findByRole("heading", { name: "我的模型" });
  expect(screen.getByRole("tab", { name: "模型" })).toHaveAttribute("aria-selected", "true");
  await user.click(screen.getByRole("tab", { name: "记忆与偏好" }));
  await waitFor(() => expect(request.mock.calls.some(([path]) => String(path) === "/api/memories")).toBe(true));
});
