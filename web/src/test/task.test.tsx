import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Tasks } from "../pages/ChatTasks";

afterEach(() => vi.unstubAllGlobals());
it("sends cancellation with the expected revision and surfaces conflicts without retrying", async () => {
  const task = {
    id: "t1",
    title: "测试取消任务",
    status: "running",
    revision: 7,
    created_at: "2026-10-31T10:00:00Z",
    updated_at: "2026-10-31T10:00:00Z",
    connection_id: "c1",
    model_id: "m1",
    tool_count: 1,
    token_count: 5,
    max_tools: 12,
    max_tokens: 3000,
    max_active_seconds: 240,
    active_seconds: 17,
    token_usage_estimated: true,
    unresolved_effect_count: 0,
  };
  const commands: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, options?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/commands")) {
        commands.push(
          JSON.parse(String(options?.body)) as Record<string, unknown>,
        );
        return new Response(
          JSON.stringify({
            error: {
              code: "revision_conflict",
              message: "任务已变化",
              retryable: false,
            },
          }),
          { status: 409 },
        );
      }
      return new Response(
        JSON.stringify(path === "/api/tasks/t1" ? task : { items: path.startsWith("/api/tasks?view=summary") ? [task] : [], next_cursor: null, has_more: false, next_after: 0 }),
      );
    }),
  );
  render(<Tasks active />);
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "测试取消任务" });
  expect(
    screen.getByText("累计 token（估算，部分用量未知）"),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "调整本任务限额" }));
  expect(screen.getByLabelText("工具调用上限")).toHaveValue(12);
  expect(screen.getByLabelText("累计 token 上限")).toHaveValue(3000);
  expect(screen.getByLabelText("有效执行秒数")).toHaveValue(240);
  await user.click(screen.getByRole("button", { name: "取消" }));
  await screen.findByText("停止这项任务的后续步骤？");
  expect(commands).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "确认停止后续步骤" }));
  await screen.findByText(/数据已变化，请重新读取后确认/);
  await waitFor(() => expect(commands).toHaveLength(1));
  expect(commands[0]).toMatchObject({ expected_revision: 7, action: "cancel" });
  expect(screen.queryByText("已取消")).not.toBeInTheDocument();
});
