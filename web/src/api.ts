import { z } from "zod";
const errorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    retryable: z.boolean(),
    diagnostic_id: z.string().nullable().optional(),
  }),
});
let csrfToken = "";
export function setCsrf(value: string) {
  csrfToken = value;
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public retryable = false,
    public diagnosticId: string | null = null,
    public status = 0,
  ) {
    super(message);
    this.name = "ApiError";
  }
  get sleeping() {
    return this.code === "runtime_sleeping";
  }
}
export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "暂时无法完成操作，请稍后重试。";
}
export async function api<T>(
  path: string,
  schema: z.ZodType<T>,
  options: {
    method?: string;
    body?: unknown;
    passive?: boolean;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  if (!path.startsWith("/api/"))
    throw new ApiError("invalid_path", "无效的服务地址。");
  const method = options.method ?? "GET";
  const headers = new Headers({ Accept: "application/json" });
  if (method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  if (options.passive && method === "GET")
    headers.set("X-Personal-Bot-Passive", "1");
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) body = options.body;
  else if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: options.signal ?? AbortSignal.timeout(125000),
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      "network_unknown",
      "连接中断，尚不能确认服务器是否已接收。",
      true,
    );
  }
  let value: unknown;
  try {
    value = response.status === 204 ? {} : await response.json();
  } catch {
    throw new ApiError(
      "invalid_response",
      "服务器返回了无法识别的响应。",
      true,
      null,
      response.status,
    );
  }
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("personal-bot:unauthorized"));
    const parsed = errorSchema.safeParse(value);
    if (parsed.success) {
      const e = parsed.data.error;
      throw new ApiError(
        e.code,
        e.message,
        e.retryable,
        e.diagnostic_id ?? null,
        response.status,
      );
    }
    throw new ApiError(
      "request_failed",
      "操作未能完成，请稍后重试。",
      response.status >= 500,
      null,
      response.status,
    );
  }
  const parsed = schema.safeParse(value);
  if (!parsed.success)
    throw new ApiError(
      "invalid_response",
      "服务返回的数据格式不匹配，请保留当前页面并重试。",
      false,
      null,
      response.status,
    );
  return parsed.data;
}
export async function download(
  path: string,
  name: string,
  method: "GET" | "POST" = "GET",
): Promise<void> {
  const headers = new Headers();
  if (method === "POST" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
  if (!response.ok)
    throw new ApiError(
      "download_failed",
      "文件暂时无法下载，请确认登录状态后重试。",
      true,
    );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function localDownload(
  text: string,
  name: string,
  type = "text/plain;charset=utf-8",
) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function safeLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
