import { cloneElement, isValidElement, useId, type ReactNode } from "react";
import { AlertCircle, RefreshCw, CheckCircle2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, errorMessage, safeLink } from "./api";
import { statusLabels, type Task } from "./types";

export const date = (value: string | null | undefined) =>
  value && Number.isFinite(Date.parse(value))
    ? new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
export const bytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KiB`
      : `${(value / 1024 ** 2).toFixed(1)} MiB`;
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className={`notice ${error instanceof ApiError && error.sleeping ? "notice-muted" : "notice-error"}`}
    >
      <AlertCircle size={18} />
      <div>
        {error instanceof ApiError && error.sleeping
          ? "工作环境已回收。记录仍保留；需要时可主动读取或发送。"
          : errorMessage(error)}
        {error instanceof ApiError && error.diagnosticId && (
          <small>诊断编号：{error.diagnosticId}</small>
        )}
        {error instanceof ApiError && ["revision_conflict", "data_conflict", "idempotency_conflict"].includes(error.code) && (
          <small>数据已变化，请重新读取后确认，原操作没有自动重试。</small>
        )}
      </div>
    </div>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="notice">
      <CheckCircle2 size={18} />
      <div>{children}</div>
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}
export function PageTitle({
  eyebrow,
  title,
  children,
  action,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
      {action}
    </header>
  );
}
export function Refresh({
  onClick,
  busy = false,
  label = "主动读取",
}: {
  onClick: () => void;
  busy?: boolean;
  label?: string;
}) {
  return (
    <button className="button secondary" disabled={busy} onClick={onClick}>
      <RefreshCw size={16} />
      {busy ? "正在读取…" : label}
    </button>
  );
}
export function Status({ task }: { task: Pick<Task, "status"> & { cancel_requested?: boolean } }) {
  return (
    <span className={`status status-${task.status}`}>
      {statusLabels[task.status]}
      {task.cancel_requested && task.status !== "cancelled"
        ? " · 正在停止"
        : ""}
    </span>
  );
}
export function JsonView({ value }: { value: unknown }) {
  return <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>;
}
export function TextContent({ text }: { text: string }) {
  return (
    <div className="prose-text markdown-content">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        urlTransform={(value) => safeLink(value) ?? ""}
        components={{
          a: ({ href, children }) => href && safeLink(href)
            ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
            : <span>{children}</span>,
          img: ({ alt }) => <span className="markdown-image-note">{alt ? `图片：${alt}` : "图片附件"}</span>,
          table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
        }}
      >{text}</ReactMarkdown>
    </div>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const generatedId = useId();
  const input = isValidElement<{ id?: string; "aria-describedby"?: string }>(
    children,
  )
    ? children
    : null;
  const id = input?.props.id ?? generatedId;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {input
        ? cloneElement(input, {
            id,
            "aria-describedby": hint
              ? `${id}-hint`
              : input.props["aria-describedby"],
          })
        : children}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
