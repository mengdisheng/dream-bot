import { useEffect, useState, type FormEvent } from "react";
import { z } from "zod";
import {
  Send,
  Plus,
  Paperclip,
  ArrowUpRight,
  Pause,
  Play,
  Square,
  RotateCcw,
  Check,
  X,
  ArrowDown,
} from "lucide-react";
import { api, ApiError } from "../api";
import { useAction, useRemote } from "../hooks";
import { useTaskEvents, useTaskMessages, useTaskSummaries } from "../taskHooks";
import {
  listOf,
  taskSchema,
  approvalSchema,
  toolSchema,
  receiptSchema,
  pendingSchema,
  type Task,
  type Pending,
} from "../types";
import {
  PageTitle,
  ErrorNotice,
  Empty,
  Status,
  Refresh,
  Notice,
  JsonView,
  date,
  Field,
  TextContent,
} from "../components";

const approvalsSchema = listOf(approvalSchema);
const toolsSchema = listOf(toolSchema);
const pendingKey = (owner: string) => `personal-bot:pending:${owner}`;
export function clearPending(owner: string) {
  try {
    localStorage.removeItem(pendingKey(owner));
  } catch {
    /* Browser storage may be disabled. */
  }
}
function savedPending(owner: string): Pending | undefined {
  try {
    const raw = localStorage.getItem(pendingKey(owner));
    const result = pendingSchema.safeParse(raw ? JSON.parse(raw) : undefined);
    return result.success ? result.data : undefined;
  } catch {
    return undefined;
  }
}
function savePending(owner: string, pending: Pending) {
  try {
    localStorage.setItem(pendingKey(owner), JSON.stringify(pending));
  } catch {
    /* Sending still works without local draft persistence. */
  }
}
const liveStatuses = [
  "queued",
  "running",
  "paused",
  "waiting_approval",
  "waiting_human",
  "waiting_model",
  "needs_review",
];

export function Chat({
  ownerId,
  active,
  attached,
  onAttached,
  onFiles,
}: {
  ownerId: string;
  active: boolean;
  attached: string[];
  onAttached: (ids: string[]) => void;
  onFiles: () => void;
}) {
  const tasks = useTaskSummaries(active);
  const [selected, setSelected] = useState<string>();
  const [newMode, setNewMode] = useState(false);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<Pending | undefined>(() =>
    savedPending(ownerId),
  );
  const [delivery, setDelivery] = useState<
    "idle" | "sending" | "unknown" | "not_found"
  >(() => (savedPending(ownerId) ? "unknown" : "idle"));
  const [receipt, setReceipt] = useState<z.infer<typeof receiptSchema>>();
  const [error, setError] = useState<unknown>();
  const [checking, setChecking] = useState(false);
  const [executionMode, setExecutionMode] = useState<"agent" | "chat">("agent");
  const summary = tasks.data?.items.find((task) => task.id === selected) ??
    tasks.data?.items.find((task) => liveStatuses.includes(task.status)) ?? tasks.data?.items[0];
  const currentId = selected ?? summary?.id;
  const detail = useRemote(currentId ? `/api/tasks/${encodeURIComponent(currentId)}` : null, taskSchema, active);
  const history = useTaskEvents(currentId, active, (resources) => {
    if (resources.task) { void tasks.reload(true); void detail.reload(true); }
    if (resources.messages) void messages.reload(true);
  });
  const messages = useTaskMessages(currentId, active, history.connected);
  const current = detail.data && detail.data.id === currentId ? detail.data : summary?.id === currentId ? summary : undefined;
  const currentResult = detail.data && detail.data.id === currentId ? detail.data.result : undefined;
  const updateSummary = tasks.update;
  useEffect(() => { if (detail.data) updateSummary(detail.data); }, [detail.data, updateSummary]);
  const reloadMessages = messages.reload;
  useEffect(() => {
    if (!history.connected || !currentId) return;
    void reloadMessages(true);
  }, [history.connected, currentId, reloadMessages]);
  const supplement =
    !newMode && !!current && liveStatuses.includes(current.status);
  async function submitPacket(packet: Pending) {
    if (delivery === "sending") return;
    setPending(packet);
    savePending(ownerId, packet);
    setDelivery("sending");
    setError(undefined);
    try {
      const accepted = await api("/api/requests", receiptSchema, {
        method: "POST",
        body: packet,
      });
      setReceipt(accepted);
      clearPending(ownerId);
      setPending(undefined);
      setDelivery("idle");
      setSelected(accepted.task_id);
      setNewMode(false);
      setText("");
      onAttached([]);
      await tasks.reload(true);
      await messages.reload(true);
    } catch (e) {
      setError(e);
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
        setDelivery("idle");
        setPending(undefined);
        clearPending(ownerId);
      } else setDelivery("unknown");
    }
  }
  async function checkReceipt() {
    if (!pending) return;
    setChecking(true);
    setError(undefined);
    try {
      const accepted = await api(
        `/api/requests/${encodeURIComponent(pending.client_request_id)}`,
        receiptSchema,
      );
      setReceipt(accepted);
      clearPending(ownerId);
      setPending(undefined);
      setDelivery("idle");
      setText("");
      onAttached([]);
      setSelected(accepted.task_id);
      await tasks.reload(true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setDelivery("not_found");
        setError(undefined);
      } else setError(e);
    } finally {
      setChecking(false);
    }
  }
  function send(event?: FormEvent) {
    event?.preventDefault();
    if (!text.trim() || pending || delivery === "sending") return;
    void submitPacket({
      client_request_id: crypto.randomUUID(),
      text: text.trim(),
      mode: supplement ? "supplement" : "new",
      ...(supplement && current ? { task_id: current.id } : {}),
      file_ids: attached,
      execution_mode: supplement
        ? (current?.execution_mode ?? "agent")
        : executionMode,
    });
  }
  const locked = delivery === "sending" || !!pending;
  return (
    <>
      <PageTitle
        eyebrow="01 / CONVERSATION"
        title="从一件事开始"
        action={
          <button
            className="button secondary"
            onClick={() => {
              setNewMode(true);
              setSelected(undefined);
              setReceipt(undefined);
            }}
          >
            <Plus size={17} />
            新任务
          </button>
        }
      >
        说出目标，查看进展，留下可以继续的工作。
      </PageTitle>
      <div className="chat-layout">
        <section className="conversation panel">
          <div className="panel-top">
            <strong>{supplement && current ? current.title : "新对话"}</strong>
            {current && !newMode && <Status task={current} />}
            <Refresh
              busy={tasks.loading}
              onClick={() => {
                void tasks.reload();
                void messages.reload();
              }}
              label="读取记录"
            />
          </div>
          <ErrorNotice error={tasks.error} />
          <ErrorNotice error={messages.error} />
          <ErrorNotice error={detail.error} />
          <ErrorNotice error={history.error} />
          <div className="messages" aria-live="polite">
            {!newMode && messages.hasMore && <button className="text-button" disabled={messages.loadingMore} onClick={() => void messages.loadMore()}>
              {messages.loadingMore ? "正在读取…" : "加载更早消息"}
            </button>}
            {(!messages.data?.items.length || newMode) && (
              <div className="chat-intro">
                <span className="eyebrow">你的个人助手</span>
                <h2>有目标，也有过程。</h2>
                <p>
                  可以研究一个主题、整理指定文件，或在浏览器里完成需要你审批的操作。
                </p>
                <button
                  className="suggestion"
                  onClick={() =>
                    setText(
                      "研究我提供的主题，整理可靠来源并生成一份 Markdown 摘要。",
                    )
                  }
                >
                  研究主题，整理来源
                  <ArrowUpRight size={16} />
                </button>
                <button className="suggestion" onClick={onFiles}>
                  添加文件后开始整理
                  <Paperclip size={16} />
                </button>
              </div>
            )}
            {!newMode &&
              messages.data?.items.map((message) => (
                <article
                  key={message.id}
                  className={`message ${message.role === "user" ? "from-user" : "from-assistant"}`}
                >
                  <header>
                    <strong>
                      {message.role === "user"
                        ? "你"
                        : message.role === "assistant"
                          ? "Bot"
                          : "任务记录"}
                    </strong>
                    <time>{date(message.created_at)}</time>
                  </header>
                  <TextContent text={message.content} />
                </article>
              ))}
            {currentResult &&
              !newMode &&
              !messages.data?.items.some(
                (message) => message.content === currentResult,
              ) && (
                <article className="message from-assistant">
                  <header>
                    <strong>任务结果</strong>
                  </header>
                  <TextContent text={currentResult} />
                </article>
              )}
            {!newMode && history.draft && current?.status === "running" && (
              <article className="message from-assistant"><header><strong>Bot · 正在回答</strong></header><TextContent text={history.draft} /></article>
            )}
            {receipt && (
              <Notice>
                已接收 · 任务 {receipt.task_id}。关闭页面后任务会继续运行。
              </Notice>
            )}
          </div>
          <form className="composer" onSubmit={send}>
            <div className="composer-mode">
              <span>
                {supplement ? `补充当前任务 · ${current?.id}` : "创建新任务"}
              </span>
              {supplement && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setNewMode(true)}
                >
                  改为新任务
                </button>
              )}
            </div>
            {!supplement && (
              <label className="execution-mode">
                <span>用途</span>
                <select
                  aria-label="任务执行方式"
                  value={executionMode}
                  disabled={locked}
                  onChange={(event) =>
                    setExecutionMode(
                      event.target.value === "chat" ? "chat" : "agent",
                    )
                  }
                >
                  <option value="agent">执行任务 · 可以使用工具</option>
                  <option value="chat">仅回答 · 不调用工具</option>
                </select>
              </label>
            )}
            {attached.length > 0 && (
              <div className="attachment-list">
                {attached.map((id) => (
                  <span className="attachment" key={id}>
                    <Paperclip size={12} />
                    {id}
                    <button
                      type="button"
                      aria-label={`移除附件 ${id}`}
                      onClick={() =>
                        onAttached(attached.filter((item) => item !== id))
                      }
                      disabled={locked}
                    >
                      <X size={13} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              aria-label="任务内容"
              placeholder="想让 Bot 做些什么？"
              value={text}
              disabled={locked}
              onChange={(e) => setText(e.target.value)}
              maxLength={20000}
              rows={4}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing &&
                  e.keyCode !== 229
                ) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <div className="composer-bottom">
              <button
                className="text-button"
                type="button"
                onClick={onFiles}
                disabled={locked}
              >
                <Paperclip size={16} />
                添加文件
              </button>
              <span>Enter 发送 · Shift + Enter 换行</span>
              <button
                type="submit"
                className="button"
                disabled={!text.trim() || locked}
              >
                <Send size={16} />
                {delivery === "sending" ? "正在发送…" : "发送"}
              </button>
            </div>
            <ErrorNotice error={error} />
            {pending && delivery !== "sending" && (
              <div className="delivery-unknown" role="status">
                <strong>
                  {delivery === "not_found" ? "尚未查到接收记录" : "未确认接收"}
                </strong>
                <p>保留原请求编号核对。此时不能确定任务已经开始。</p>
                <code>{pending.client_request_id}</code>
                <div className="actions">
                  <button
                    type="button"
                    className="button secondary"
                    disabled={checking}
                    onClick={() => void checkReceipt()}
                  >
                    {checking ? "正在核对…" : "核对接收"}
                  </button>
                  {delivery === "not_found" && (
                    <button
                      type="button"
                      className="button"
                      onClick={() => void submitPacket(pending)}
                    >
                      按原编号重试
                    </button>
                  )}
                </div>
                <details>
                  <summary>查看待核对内容</summary>
                  <div className="prose-text">{pending.text}</div>
                  <small>
                    {pending.file_ids.length} 个附件 ·{" "}
                    {pending.execution_mode === "chat" ? "仅回答" : "执行任务"}
                  </small>
                </details>
                <small>
                  接收未查明前保留原编号，避免重复创建任务。退出登录只会清理此设备草稿，不会取消可能已经接收的任务。
                </small>
              </div>
            )}
          </form>
        </section>
        <aside className="evidence-panel">
          <span className="eyebrow">工作记录</span>
          <h2>当前与最近任务</h2>
          {tasks.data?.items.map((task) => (
            <button
              className={`task-shortcut ${current?.id === task.id && !newMode ? "selected" : ""}`}
              key={task.id}
              onClick={() => {
                setSelected(task.id);
                setNewMode(false);
              }}
            >
              <Status task={task} />
              <strong>{task.title}</strong>
              <small>{date(task.updated_at)}</small>
            </button>
          ))}
          {tasks.hasMore && <button className="text-button" disabled={tasks.loadingMore} onClick={() => void tasks.loadMore()}>加载更多任务</button>}
          {!tasks.data?.items.length && (
            <p className="muted">任务持久接收后会出现在这里。</p>
          )}
          <div className="aside-note">
            <ArrowDown size={18} />
            <p>
              任务完成后可以下载产物。环境空闲五分钟后自动销毁，任务记录继续保留。
            </p>
          </div>
        </aside>
      </div>
    </>
  );
}

export function Tasks({ active }: { active: boolean }) {
  const tasks = useTaskSummaries(active);
  const [selected, setSelected] = useState<string>();
  const [filter, setFilter] = useState("all");
  const filtered =
    tasks.data?.items.filter(
      (task) =>
        filter === "all" ||
        (filter === "waiting"
          ? task.status.startsWith("waiting") || task.status === "needs_review"
          : filter === "finished"
            ? ["succeeded", "failed", "cancelled"].includes(task.status)
            : ["queued", "running", "paused"].includes(task.status)),
    ) ?? [];
  const current = filtered.find((task) => task.id === selected) ?? filtered[0];
  return (
    <>
      <PageTitle
        eyebrow="02 / TASKS"
        title="每一步，都有记录"
        action={
          <Refresh onClick={() => void tasks.reload()} busy={tasks.loading} />
        }
      >
        继续已有工作，处理等待，核对已经发生的操作。
      </PageTitle>
      <ErrorNotice error={tasks.error} />
      <div className="segmented" aria-label="任务筛选">
        {[
          ["all", "全部"],
          ["active", "进行中"],
          ["waiting", "等待处理"],
          ["finished", "已结束"],
        ].map(([id, label]) => (
          <button
            className={filter === id ? "selected" : ""}
            key={id}
            onClick={() => setFilter(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="task-layout">
        <section className="task-list panel">
          {!filtered.length && (
            <Empty title="这里暂时没有任务">
              在聊天页提交第一项任务，或主动读取已有记录。
            </Empty>
          )}
          {filtered.map((task) => (
            <button
              key={task.id}
              className={`task-list-row ${current?.id === task.id ? "selected" : ""}`}
              onClick={() => setSelected(task.id)}
            >
              <div>
                <Status task={task} />
                <small>{date(task.updated_at)}</small>
              </div>
              <strong>{task.title}</strong>
              <span>{task.wait_reason ?? task.id}</span>
              {task.unresolved_effect_count > 0 && (
                <small className="warning-text">
                  {task.unresolved_effect_count} 项外部操作待核对
                </small>
              )}
            </button>
          ))}
          {tasks.hasMore && <button className="text-button" disabled={tasks.loadingMore} onClick={() => void tasks.loadMore()}>加载更多任务</button>}
        </section>
        {current ? (
          <SelectedTask
            key={current.id}
            identity={current.id}
            onLoaded={tasks.update}
            onChanged={() => void tasks.reload(true)}
            active={active}
          />
        ) : (
          <section className="panel">
            <Empty title="选择任务查看过程" />
          </section>
        )}
      </div>
    </>
  );
}

function SelectedTask({ identity, onChanged, onLoaded, active }: { identity: string; onChanged: () => void; onLoaded: (task: Task) => void; active: boolean }) {
  const detail = useRemote(`/api/tasks/${encodeURIComponent(identity)}`, taskSchema, active);
  useEffect(() => { if (detail.data) onLoaded(detail.data); }, [detail.data, onLoaded]);
  return <><ErrorNotice error={detail.error} />{detail.data && <TaskDetail task={detail.data} active={active} onChanged={() => { onChanged(); void detail.reload(true); }} />}</>;
}

export function TaskDetail({
  task,
  onChanged,
  active,
  compact = false,
  onStream,
}: {
  task: Task;
  onChanged: () => void;
  active: boolean;
  compact?: boolean;
  onStream?: (draft: string, connected: boolean) => void;
}) {
  const approvals = useRemote(
    `/api/approvals?task_id=${encodeURIComponent(task.id)}`,
    approvalsSchema,
    active,
  );
  const tools = useRemote(
    `/api/tasks/${encodeURIComponent(task.id)}/tool-calls`,
    toolsSchema,
    active,
  );
  const action = useAction();
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [reconcile, setReconcile] = useState<string>();
  const [outcome, setOutcome] = useState("unresolved");
  const [evidence, setEvidence] = useState("");
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [visibleEvents, setVisibleEvents] = useState(200);
  const [diagnostics, setDiagnostics] = useState(false);
  const history = useTaskEvents(task.id, active, (resources) => {
    if (resources.task) onChanged();
    if (resources.approvals) void approvals.reload(true);
    if (resources.tools) void tools.reload(true);
  });
  const events = history.events;
  const streamClosed = !history.connected;
  useEffect(() => { onStream?.(history.draft, history.connected); }, [history.draft, history.connected, onStream]);
  async function command(kind: "pause" | "continue" | "cancel" | "rerun") {
    await action.run(
      async () => {
        await api(
          `/api/tasks/${encodeURIComponent(task.id)}/commands`,
          z.unknown(),
          {
            method: "POST",
            body: {
              command_id: crypto.randomUUID(),
              expected_revision: task.revision,
              action: kind,
            },
          },
        );
        onChanged();
        await history.reload(true);
      },
      kind === "cancel"
        ? "停止请求已提交；已经派发的外部操作仍可能需要核对。"
        : kind === "rerun"
          ? "已请求创建关联新任务。"
          : "操作已提交。",
    );
    setCancelConfirm(false);
  }
  const eventLabel: Record<string, string> = {
    "task.queued": "任务已接收",
    "task.running": "开始执行",
    "task.supplement": "已补充任务内容",
    "task.pause": "已请求暂停",
    "task.continue": "已请求继续",
    "task.cancel": "已请求取消",
    "task.paused": "已暂停",
    "task.cancelled": "已取消",
    "task.succeeded": "任务完成",
    "task.failed": "任务失败",
    "task.needs_review": "需要核对执行结果",
    "task.waiting_model": "等待模型可用",
    "task.waiting_human": "等待人工处理",
    "task.recovered": "任务已恢复",
    "task.budgets_changed": "限额已更新，未完成任务已暂停",
    "approval.required": "等待审批",
    "approval.decided": "已处理审批",
    "model.text_delta": "收到模型回答",
    "model.completed": "模型调用完成",
    "model.interrupted": "模型调用中断，用量待核对",
    "tool.dispatched": "已派发操作",
    "tool.succeeded": "操作完成",
    "tool.failed": "操作失败",
    "tool.unknown": "操作结果待核对",
    "tool.reconciled": "已核对外部操作",
    "browser.human_control": "浏览器已由人工控制",
    "artifact.published": "已保存正式产物",
    "file.deleted": "文件已删除",
    "restore.needs_review": "备份已恢复，任务需要核对",
    accepted: "任务已接收",
    started: "开始执行",
    model_started: "正在调用模型",
    tool_prepared: "准备操作",
    tool_completed: "操作完成",
    approval_requested: "等待审批",
    paused: "已暂停",
    cancelled: "已取消",
    succeeded: "任务完成",
    failed: "任务失败",
    waiting_model: "等待模型可用",
  };
  return (
    <section className={`task-detail panel ${compact ? "task-detail-compact" : ""}`} aria-label="当前执行与审批">
      <div className="panel-top">
        <Status task={task} />
        {(!compact || diagnostics) && <span className="mono">修订 {task.revision}</span>}
        <Refresh
          label="读取最新"
          busy={history.loading}
          onClick={() => {
            onChanged();
            void history.reload();
            void approvals.reload();
            void tools.reload();

          }}
        />
      </div>
      {(!compact || diagnostics) && <><h2>{task.title}</h2><p className="muted mono">{task.id}</p></>}
      {task.wait_reason && (
        <div className="notice notice-muted">{task.wait_reason}</div>
      )}
      {(!compact || diagnostics) && <div className="task-metrics">
        <span>
          <strong>{task.tool_count}</strong>工具调用
        </span>
        <span>
          <strong>{task.token_count.toLocaleString()}</strong>
          {task.token_usage_estimated
            ? "累计 token（估算，部分用量未知）"
            : "累计 token"}
        </span>
        <span>
          <strong>{Math.ceil(task.active_seconds)}</strong>有效执行秒数
        </span>
        <span>
          <strong>{task.unresolved_effect_count}</strong>待核对操作
        </span>
      </div>}
      <div className="actions">
        {(!compact || ["running", "queued"].includes(task.status)) &&
        <button
          className="button secondary"
          disabled={action.busy || !["running", "queued"].includes(task.status)}
          onClick={() => void command("pause")}
        >
          <Pause size={15} />
          暂停
        </button>}
        {(!compact || ["paused", "waiting_model", "waiting_human"].includes(task.status)) &&
        <button
          className="button secondary"
          disabled={
            action.busy ||
            !["paused", "waiting_model", "waiting_human"].includes(
              task.status,
            ) ||
            task.unresolved_effect_count > 0
          }
          onClick={() => void command("continue")}
        >
          <Play size={15} />
          继续
        </button>}
        {(!compact || !["succeeded", "failed", "cancelled"].includes(task.status)) &&
        <button
          className="button danger-outline"
          disabled={
            action.busy ||
            ["succeeded", "failed", "cancelled"].includes(task.status)
          }
          onClick={() => setCancelConfirm(true)}
        >
          <Square size={14} />
          {compact ? "停止任务" : "取消"}
        </button>}
        {(!compact || diagnostics) && <>
        <button
          className="button secondary"
          disabled={action.busy}
          onClick={() => void command("rerun")}
        >
          <RotateCcw size={15} />
          重新执行
        </button>
        <button
          className="text-button"
          onClick={() => setBudgetOpen(!budgetOpen)}
        >
          调整本任务限额
        </button>
        </>}
        {compact && <button className="text-button" onClick={() => setDiagnostics(!diagnostics)} aria-expanded={diagnostics}>{diagnostics ? "收起执行详情" : "执行详情"}</button>}
      </div>
      {cancelConfirm && (
        <div className="confirm-box">
          <strong>停止这项任务的后续步骤？</strong>
          <p>
            已经发送的消息、订单或其他外部操作不能通过取消撤回。不确定的结果会保留待核对。
          </p>
          <div className="actions">
            <button
              className="button danger"
              disabled={action.busy}
              onClick={() => void command("cancel")}
            >
              确认停止后续步骤
            </button>
            <button
              className="button secondary"
              onClick={() => setCancelConfirm(false)}
            >
              返回
            </button>
          </div>
        </div>
      )}
      {budgetOpen && (
        <BudgetEditor
          key={`${task.id}:${task.revision}`}
          path={`/api/tasks/${encodeURIComponent(task.id)}/budgets`}
          revision={task.revision}
          defaults={task}
          onSaved={() => {
            action.setNotice(
              "限额已更新，累计用量保留；未完成任务已暂停，原审批已失效。请核对后主动继续。",
            );
            onChanged();
            setBudgetOpen(false);
          }}
        />
      )}
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      {approvals.data?.items
        .filter((item) => item.status === "pending")
        .map((item) => (
          <article className="approval" key={item.id}>
            <div className="eyebrow">需要你的审批</div>
            <h3>{item.action.name}</h3>
            <dl>
              <dt>网站</dt>
              <dd>{item.action.site ?? "未提供，审批前请核对"}</dd>
              <dt>账号声明</dt>
              <dd>{item.action.account ?? "未提供，审批前请核对"}</dd>
              {(!compact || diagnostics) && <><dt>任务修订</dt><dd>{item.task_revision}</dd></>}
            </dl>
            {compact ? <ActionArguments value={item.action.arguments} /> : <JsonView value={item.action.arguments} />}
            {item.action.file_versions !== undefined &&
              item.action.file_versions !== null && (
                compact ? <ActionArguments value={item.action.file_versions} /> : <JsonView value={item.action.file_versions} />
              )}
            <p className="muted">
              账号身份未自动确认，请在浏览器核对当前账号后批准。
              批准后仍需核对页面与动作。目标、正文或附件改变会使这份批准失效。
            </p>
            <div className="actions">
              <button
                className="button"
                disabled={action.busy || item.task_revision !== task.revision}
                onClick={() =>
                  void action.run(async () => {
                    await api(
                      `/api/approvals/${encodeURIComponent(item.id)}/decision`,
                      z.unknown(),
                      {
                        method: "POST",
                        body: {
                          command_id: crypto.randomUUID(),
                          expected_revision: task.revision,
                          decision: "approve",
                        },
                      },
                    );
                    await approvals.reload(true);
                    onChanged();
                  }, "审批已提交，动作将在重新核对后执行。")
                }
              >
                <Check size={16} />
                批准本次操作
              </button>
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await api(
                      `/api/approvals/${encodeURIComponent(item.id)}/decision`,
                      z.unknown(),
                      {
                        method: "POST",
                        body: {
                          command_id: crypto.randomUUID(),
                          expected_revision: task.revision,
                          decision: "reject",
                        },
                      },
                    );
                    await approvals.reload(true);
                    onChanged();
                  }, "已拒绝本次操作。")
                }
              >
                <X size={16} />
                拒绝
              </button>
            </div>
          </article>
        ))}
      <ErrorNotice error={approvals.error} />
      {approvals.data?.items
        .filter((item) => item.status !== "pending")
        .map((item) => (
          <div className="notice notice-muted" key={item.id}>
            <div>
              <strong>
                {item.action.name} ·{" "}
                {(
                  {
                    approved: "已批准，待核对并执行",
                    rejected: "已拒绝",
                    invalidated: "审批已失效",
                    consumed: "已使用本次审批",
                  } as Record<string, string>
                )[item.status] ?? item.status}
              </strong>
              <small>
                审批状态不代表操作已经完成，请以操作记录和结果为准。
              </small>
            </div>
          </div>
        ))}
      {tools.data?.items
        .filter(
          (item) =>
            ["unknown", "needs_review"].includes(item.status) ||
            (item.status === "dispatched" &&
              task.unresolved_effect_count > 0 &&
              ["needs_review", "cancelled", "paused"].includes(task.status)),
        )
        .map((item) => (
          <article className="review-box" key={item.id}>
            <h3>核对外部操作 · {item.name}</h3>
            {compact ? <ActionArguments value={item.arguments} /> : <JsonView value={item.arguments} />}
            <p>不要在尚未查明结果时重新提交同一操作。</p>
            {reconcile !== item.id ? (
              <button
                className="button secondary"
                onClick={() => {
                  setReconcile(item.id);
                  setEvidence("");
                  setOutcome("unresolved");
                }}
              >
                记录核对结果
              </button>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action.run(async () => {
                    await api(
                      `/api/tasks/${encodeURIComponent(task.id)}/tool-calls/${encodeURIComponent(item.id)}/reconciliation`,
                      z.unknown(),
                      {
                        method: "POST",
                        body: {
                          command_id: crypto.randomUUID(),
                          expected_task_revision: task.revision,
                          expected_tool_revision: item.revision,
                          outcome,
                          evidence,
                        },
                      },
                    );
                    setReconcile(undefined);
                    await tools.reload(true);
                    onChanged();
                  }, "核对记录已保存；已取消任务不会因此自动继续。");
                }}
              >
                <Field label="核对结果">
                  <select
                    value={outcome}
                    onChange={(e) => setOutcome(e.target.value)}
                  >
                    <option value="unresolved">仍不能确认</option>
                    <option value="confirmed_done">已确认完成</option>
                    <option value="confirmed_not_done">已确认未执行</option>
                  </select>
                </Field>
                <Field label="核对依据">
                  <textarea
                    value={evidence}
                    onChange={(e) => setEvidence(e.target.value)}
                    required
                    placeholder="例如第三方订单状态、发送记录或可核对的结果。请勿填写密码。"
                  />
                </Field>
                <button className="button" disabled={action.busy}>
                  保存核对记录
                </button>
              </form>
            )}
          </article>
        ))}
      <ErrorNotice error={tools.error} />
      <details className="execution-history" open={compact ? undefined : true}>
      <summary>{compact ? `查看执行过程 · ${events.length} 项` : "过程记录"}</summary>
      {streamClosed && (
        <p className="muted">
          实时连接尚未连接。断线会按事件编号续传，休眠不会取消任务；可主动读取最新记录。
        </p>
      )}
      <ErrorNotice error={history.error} />
      {events.length > visibleEvents && <button className="text-button" onClick={() => setVisibleEvents((count) => count + 200)}>显示更早过程记录</button>}
      <ol className="timeline">
        {events.slice(-visibleEvents).map((event) => (
          <li key={event.seq}>
            <span className="timeline-index">
              {String(event.seq).padStart(2, "0")}
            </span>
            <div>
              <strong>{eventLabel[event.kind] ?? "任务状态更新"}</strong>
              {typeof event.data.summary === "string" && (
                <p>{event.data.summary}</p>
              )}
              <small>{date(event.created_at)}</small>
            </div>
          </li>
        ))}
      </ol>
      {!events.length && <p className="muted">暂无可显示的过程记录。</p>}
      </details>
      {!compact && task.result && (
        <>
          <h3 className="section-label">结果</h3>
          <div className="result-text">
            <TextContent text={task.result} />
          </div>
        </>
      )}
    </section>
  );
}

function ActionArguments({ value }: { value: unknown }) {
  const labels: Record<string, string> = { url: "页面", target: "目标", to: "接收人", recipient: "接收人", subject: "主题", body: "正文", text: "内容", content: "内容", amount: "金额", currency: "币种", account: "账号", file_ids: "附件", files: "附件", file_id: "文件", version: "版本", name: "名称", action: "动作" };
  const entries: [string, unknown][] = typeof value === "object" && value !== null && !Array.isArray(value) ? Object.entries(value) : [["content", value]];
  return <div className="decision-parameters"><dl>{entries.map(([key, item]) => <div key={key}><dt>{labels[key] ?? key}</dt><dd>{typeof item === "string" ? item : JSON.stringify(item, null, 2) ?? "—"}</dd></div>)}</dl><details><summary>原始参数</summary><JsonView value={value} /></details></div>;
}

export function BudgetEditor({
  path,
  revision,
  onSaved,
  defaults,
}: {
  path: string;
  revision?: number;
  onSaved: () => void;
  defaults?: {
    max_tools: number;
    max_tokens: number;
    max_active_seconds: number;
  };
}) {
  const [tools, setTools] = useState(defaults?.max_tools ?? 30);
  const [tokens, setTokens] = useState(defaults?.max_tokens ?? 100000);
  const [seconds, setSeconds] = useState(defaults?.max_active_seconds ?? 900);
  const action = useAction();
  return (
    <form
      className="inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          await api(path, z.unknown(), {
            method: "PATCH",
            body: {
              max_tools: tools,
              max_tokens: tokens,
              max_active_seconds: seconds,
              ...(revision !== undefined
                ? {
                    command_id: crypto.randomUUID(),
                    expected_revision: revision,
                  }
                : {}),
            },
          });
          onSaved();
        }, "限额已更新。");
      }}
    >
      <div className="form-grid">
        <Field label="工具调用上限">
          <input
            type="number"
            min="1"
            value={tools}
            onChange={(e) => setTools(Number(e.target.value))}
            required
          />
        </Field>
        <Field label="累计 token 上限">
          <input
            type="number"
            min="1"
            value={tokens}
            onChange={(e) => setTokens(Number(e.target.value))}
            required
          />
        </Field>
        <Field label="有效执行秒数">
          <input
            type="number"
            min="1"
            value={seconds}
            onChange={(e) => setSeconds(Number(e.target.value))}
            required
          />
        </Field>
      </div>
      <p className="muted">
        修改不会清零已累计用量。token
        限额是平台请求上界控制，无法保证厂商实际账单或内部积分包不会增加。
        {revision !== undefined &&
          "保存会暂停未完成任务并使原审批失效，需核对后主动继续。"}
      </p>
      <ErrorNotice error={action.error} />
      <button className="button secondary" disabled={action.busy}>
        保存限额
      </button>
    </form>
  );
}
