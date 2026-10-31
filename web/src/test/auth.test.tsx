import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Auth } from "../Auth";

vi.mock("qrcode", () => ({
  default: {
    toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,dGVzdA=="),
  },
}));
afterEach(() => vi.unstubAllGlobals());
it("enrolls locally and keeps one-time recovery codes visible until the owner confirms storage", async () => {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input);
      calls.push({ path, body: JSON.parse(String(options?.body)) });
      return new Response(
        JSON.stringify(
          path.endsWith("/enrollment")
            ? {
                enrollment_id: "e1",
                totp_uri: "otpauth://totp/Test?secret=TESTONLY",
                totp_secret: "TESTONLY",
              }
            : {
                owner: { id: "owner1", username: "test-owner" },
                csrf_token: "test-csrf",
                recovery_codes: ["test-recovery-1", "test-recovery-2"],
              },
        ),
        { headers: { "Content-Type": "application/json" } },
      );
    }),
  );
  const authenticated = vi.fn();
  render(<Auth initialized={false} onAuthenticated={authenticated} />);
  const user = userEvent.setup();
  await user.type(
    screen.getByLabelText("一次性初始化凭据"),
    "test-setup-token",
  );
  await user.type(screen.getByLabelText("用户名"), "test-owner");
  await user.click(screen.getByRole("button", { name: "设置双重验证" }));
  await screen.findByAltText("本地生成的双重验证二维码");
  await user.type(screen.getByLabelText("密码"), "a-test-only-password");
  await user.type(screen.getByLabelText("验证器动态码"), "123456");
  await user.click(screen.getByRole("button", { name: "完成设置" }));
  await screen.findByText(/test-recovery-1/);
  expect(authenticated).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "进入工作台" })).toBeDisabled();
  expect(calls).toHaveLength(2);
  expect(calls[1].body).toMatchObject({
    enrollment_id: "e1",
    setup_token: "test-setup-token",
    username: "test-owner",
    totp_code: "123456",
  });
  expect(localStorage.length).toBe(0);
  await user.click(screen.getByLabelText("我已经保存好恢复码"));
  await user.click(screen.getByRole("button", { name: "进入工作台" }));
  await waitFor(() =>
    expect(authenticated).toHaveBeenCalledWith({
      id: "owner1",
      username: "test-owner",
    }),
  );
});
