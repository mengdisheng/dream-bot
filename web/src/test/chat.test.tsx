import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Chat } from "../pages/ChatTasks";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());
describe("chat acceptance and composing", () => {
  it("queries an uncertain receipt before retrying with the original request id", async () => {
    const sent: Record<string, unknown>[] = [];
    let fail = true;
    const fetcher = vi.fn(
      async (input: string | URL | Request, options?: RequestInit) => {
        const path = String(input);
        if (path.startsWith("/api/tasks?view=summary")) return json({ items: [], next_cursor: null, next_before: null, has_more: false, next_after: 0 });
        if (path.startsWith("/api/requests/") && options?.method === "GET")
          return json(
            {
              error: {
                code: "not_found",
                message: "尚未接收",
                retryable: false,
              },
            },
            404,
          );
        if (path === "/api/requests" && options?.method === "POST") {
          const body = JSON.parse(String(options.body)) as Record<
            string,
            unknown
          >;
          sent.push(body);
          if (fail) {
            fail = false;
            throw new TypeError("connection lost");
          }
          return json({
            accepted: true,
            request_id: body.client_request_id,
            task_id: "task-1",
            task_revision: 1,
          });
        }
        return json({ items: [], next_cursor: null, next_before: null, has_more: false, next_after: 0 });
      },
    );
    vi.stubGlobal("fetch", fetcher);
    render(
      <Chat
        ownerId="test-owner"
        active={false}
        attached={["file-1"]}
        onAttached={vi.fn()}
        onFiles={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "任务内容" }),
      "整理测试资料",
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("未确认接收");
    expect(screen.queryByText(/已接收 · 任务/)).not.toBeInTheDocument();
    expect(localStorage.getItem("personal-bot:pending:test-owner")).toContain(
      "整理测试资料",
    );
    await user.click(screen.getByRole("button", { name: "核对接收" }));
    await screen.findByText("尚未查到接收记录");
    await user.click(screen.getByRole("button", { name: "按原编号重试" }));
    await screen.findByText(/已接收 · 任务 task-1/);
    expect(sent).toHaveLength(2);
    expect(sent[0].client_request_id).toBe(sent[1].client_request_id);
    expect(sent[1].file_ids).toEqual(["file-1"]);
    expect(localStorage.getItem("personal-bot:pending:test-owner")).toBeNull();
  });
  it("does not submit Chinese IME composition or shift-enter", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_path: unknown, options?: RequestInit) => {
        if (options?.method === "POST") posts.push(options.body);
        return json({ items: [], next_cursor: null, next_before: null, has_more: false, next_after: 0 });
      }),
    );
    render(
      <Chat
        ownerId="test-owner"
        active={false}
        attached={[]}
        onAttached={vi.fn()}
        onFiles={vi.fn()}
      />,
    );
    const input = screen.getByRole("textbox", { name: "任务内容" });
    fireEvent.change(input, { target: { value: "还在选词" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229, isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    await waitFor(() => expect(posts).toHaveLength(0));
    expect(input).toHaveValue("还在选词");
  });
  it("restores a pending request without auto-resubmitting it", async () => {
    localStorage.setItem(
      "personal-bot:pending:test-owner",
      JSON.stringify({
        client_request_id: "stable-1",
        text: "pending draft",
        mode: "new",
        file_ids: [],
      }),
    );
    const fetcher = vi
      .fn<
        (
          input: string | URL | Request,
          options?: RequestInit,
        ) => Promise<Response>
      >()
      .mockImplementation(async () => json({ items: [], next_cursor: null, next_before: null, has_more: false, next_after: 0 }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <Chat
        ownerId="test-owner"
        active={false}
        attached={[]}
        onAttached={vi.fn()}
        onFiles={vi.fn()}
      />,
    );
    expect(screen.getByText("未确认接收")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    expect(
      fetcher.mock.calls.every(
        (call) =>
          !(call[1] as RequestInit | undefined)?.method ||
          (call[1] as RequestInit).method === "GET",
      ),
    ).toBe(true);
  });
});
