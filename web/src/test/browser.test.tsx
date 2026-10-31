import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Browser } from "../pages/Browser";
import type { BrowserStatus } from "../types";

const viewers = vi.hoisted(() => ({
  instances: [] as {
    viewOnly: boolean;
    disconnect: ReturnType<typeof vi.fn>;
    dispatchEvent: (event: Event) => boolean;
    url: string;
  }[],
}));
vi.mock("@novnc/novnc", () => ({
  default: class FakeRfb extends EventTarget {
    viewOnly = true;
    scaleViewport = false;
    resizeSession = true;
    clipViewport = false;
    disconnect = vi.fn();
    url: string;
    constructor(_surface: HTMLElement, url: string) {
      super();
      this.url = url;
      viewers.instances.push(this);
    }
  },
}));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  viewers.instances = [];
});
const scopedStatus: BrowserStatus = {
  available: true, running: true, mode: "agent", browser_epoch: 1,
  device_id: null, task_id: "task1", human_idle_deadline: null,
  width: 1280, height: 720, url: "https://example.test", error: null,
};
function viewerReceipt() {
  return new Response(JSON.stringify({
    view_session_id: "scoped", runtime_id: "r1", browser_epoch: 1,
    ws_path: "/api/browser/view/scoped",
    expires_at: new Date(Date.now() + 60000).toISOString(), width: 1280, height: 720,
  }));
}
it("binds an embedded viewer to its task and discards a ticket after the conversation changes", async () => {
  let finish: ((response: Response) => void) | undefined;
  const bodies: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
    if (String(input) === "/api/browser/view-sessions") {
      bodies.push(JSON.parse(String(options?.body)));
      return await new Promise<Response>((resolve) => { finish = resolve; });
    }
    return new Response(JSON.stringify(scopedStatus));
  }));
  const view = render(<Browser active={false} embedded expectedTaskId="task1" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "打开观看" }));
  await waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toMatchObject({ expected_task_id: "task1" });
  view.rerender(<Browser active={false} embedded expectedTaskId="task2" />);
  await act(async () => { finish?.(viewerReceipt()); });
  expect(viewers.instances).toHaveLength(0);
});
it("does not create a viewer when the passive browser status belongs to another task", async () => {
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
    requests.push(String(input));
    return new Response(JSON.stringify(scopedStatus));
  }));
  render(<Browser active={false} embedded expectedTaskId="task2" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "打开观看" }));
  await screen.findByText(/这项任务的浏览器已关闭或已切换/);
  expect(requests.length).toBeGreaterThan(0);
  expect(requests.every((path) => path === "/api/browser/status")).toBe(true);
  expect(viewers.instances).toHaveLength(0);
});
it("does not reconnect from a late control response after the panel closes", async () => {
  let finish: ((response: Response) => void) | undefined;
  const controls: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
    if (String(input) === "/api/browser/control") {
      controls.push(JSON.parse(String(options?.body)));
      return await new Promise<Response>((resolve) => { finish = resolve; });
    }
    return new Response(JSON.stringify(scopedStatus));
  }));
  const view = render(<Browser active={false} embedded expectedTaskId="task1" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "接管浏览器" }));
  await waitFor(() => expect(controls).toHaveLength(1));
  expect(controls[0]).toMatchObject({ expected_task_id: "task1", action: "takeover" });
  view.unmount();
  await act(async () => { finish?.(new Response(JSON.stringify(scopedStatus))); });
  expect(viewers.instances).toHaveLength(0);
});
it("creates an explicit viewer and only enables input after acknowledged takeover; disconnect does not reconnect", async () => {
  localStorage.setItem("personal-bot:device", "test-device");
  const base: BrowserStatus = {
    available: true,
    running: true,
    mode: "agent",
    browser_epoch: 1,
    device_id: null,
    task_id: "task1",
    human_idle_deadline: null,
    width: 1280,
    height: 900,
    url: "https://example.test",
    error: null,
  };
  let status = base;
  let sessions = 0;
  const controls: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input);
      if (path === "/api/browser/control") {
        controls.push(JSON.parse(String(options?.body)));
        status = {
          ...base,
          mode: "human",
          browser_epoch: 2,
          device_id: "test-device",
        };
        return new Response(JSON.stringify(status));
      }
      if (path === "/api/browser/view-sessions") {
        sessions++;
        return new Response(
          JSON.stringify({
            view_session_id: `v${sessions}`,
            runtime_id: "r1",
            browser_epoch: status.browser_epoch,
            ws_path: `/api/browser/view/v${sessions}`,
            expires_at: new Date(Date.now() + 60000).toISOString(),
            width: 1280,
            height: 900,
          }),
        );
      }
      return new Response(JSON.stringify(status));
    }),
  );
  render(<Browser active />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "打开观看" }));
  await waitFor(() => expect(viewers.instances).toHaveLength(1));
  expect(viewers.instances[0].viewOnly).toBe(true);
  expect(viewers.instances[0].url).toBe(
    new URL("/api/browser/view/v1", location.origin).href.replace(
      /^http/,
      "ws",
    ),
  );
  await act(async () => {
    viewers.instances[0].dispatchEvent(new Event("connect"));
  });
  vi.useFakeTimers();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(61000);
  });
  expect(viewers.instances[0].disconnect).not.toHaveBeenCalled();
  expect(screen.getByText("画面已连接")).toBeInTheDocument();
  vi.useRealTimers();
  await user.click(screen.getByRole("button", { name: "接管浏览器" }));
  await waitFor(() => expect(viewers.instances).toHaveLength(2));
  expect(viewers.instances[0].disconnect).toHaveBeenCalled();
  expect(viewers.instances[1].viewOnly).toBe(false);
  expect(controls).toEqual([
    { action: "takeover", device_id: "test-device", browser_epoch: 1 },
  ]);
  await act(async () => {
    viewers.instances[1].dispatchEvent(new Event("disconnect"));
  });
  await screen.findByText(/观看连接已断开/);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  expect(sessions).toBe(2);
});
