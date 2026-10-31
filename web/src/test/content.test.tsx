import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { ErrorNotice, TextContent } from "../components";
import { ApiError } from "../api";

it("renders useful Markdown without executing HTML, remote images or unsafe links", () => {
  const { container } = render(<TextContent text={'## 调研结果\n\n**结论**与[来源](https://example.com)\n\n| 项目 | 结果 |\n| --- | --- |\n| 网页 | 已读取 |\n\n<script>window.secret=1</script>\n\n[危险链接](javascript:alert%281%29)\n\n![不可自动加载](https://remote.example/track.png)'} />);
  expect(screen.getByRole("heading", { name: "调研结果" })).toBeInTheDocument();
  expect(screen.getByRole("table")).toHaveTextContent("已读取");
  expect(screen.getByRole("link", { name: "来源" })).toHaveAttribute("rel", "noopener noreferrer");
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.queryByRole("link", { name: "危险链接" })).not.toBeInTheDocument();
  expect(screen.getByText("图片：不可自动加载")).toBeInTheDocument();
});

it("does not label a billing conflict as a stale edit", () => {
  render(<ErrorNotice error={new ApiError("overage_approval_required", "积分授权已结束", false, null, 409)} />);
  expect(screen.getByRole("alert")).toHaveTextContent("积分授权已结束");
  expect(screen.queryByText(/数据已变化/)).not.toBeInTheDocument();
});
