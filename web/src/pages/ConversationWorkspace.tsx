import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { z } from "zod";
import { ArrowLeft, ArrowUp, ArrowUpRight, ChevronDown, FileText, Globe, Paperclip, Plus, X, Download, Square } from "lucide-react";
import { api, ApiError, download } from "../api";
import { useAction, useRemote } from "../hooks";
import { useConversationMessages } from "../conversationHooks";
import { browserSchema, conversationDetailSchema, fileSchema, listOf, pendingSchema, receiptSchema, taskSchema, statusLabels, type Pending, type TaskSummary } from "../types";
import { bytes, date, ErrorNotice, TextContent } from "../components";
import { clearPending, TaskDetail } from "./ChatTasks";

const Browser = lazy(() => import("./Browser").then((module) => ({ default: module.Browser })));
const filesSchema = listOf(fileSchema);
type FileItem = z.infer<typeof fileSchema>;
const live = new Set(["queued", "running", "paused", "waiting_approval", "waiting_human", "waiting_model", "needs_review"]);
const pendingKey = (owner: string) => `personal-bot:pending:${owner}`;
function readPending(owner: string) {
  try { const value = pendingSchema.safeParse(JSON.parse(localStorage.getItem(pendingKey(owner)) ?? "null")); return value.success ? value.data : undefined; }
  catch { return undefined; }
}
function savePending(owner: string, value: Pending) {
  try { localStorage.setItem(pendingKey(owner), JSON.stringify(value)); } catch { /* Request id remains stable within this mounted composer. */ }
}
function clearMatchingPending(owner: string, requestId: string) {
  if (readPending(owner)?.client_request_id === requestId) clearPending(owner);
}

export function ConversationWorkspace({ ownerId, conversationId, onAccepted, onChanged, attached, onAttached, onFiles, onSettings, onPanelChange }: {
  ownerId: string; conversationId: string | null | undefined;
  onAccepted: (conversationId: string) => void; onChanged: () => void;
  attached: string[]; onAttached: (ids: string[]) => void; onFiles: () => void; onSettings: () => void;
  onPanelChange?: (open: boolean) => void;
}) {
  const detailPath = conversationId ? `/api/conversations/${encodeURIComponent(conversationId)}?limit=50` : null;
  const conversation = useRemote(detailPath, conversationDetailSchema, false);
  const currentConversation = conversation.dataPath === detailPath ? conversation.data : undefined;
  const currentTaskId = currentConversation?.latest_task_id ?? undefined;
  const taskPath = currentTaskId ? `/api/tasks/${encodeURIComponent(currentTaskId)}` : null;
  const task = useRemote(taskPath, taskSchema, false);
  const currentTask = task.dataPath === taskPath ? task.data : undefined;
  const [stream, setStream] = useState({ draft: "", connected: false });
  const messages = useConversationMessages(conversationId, stream.connected);
  const files = useRemote(conversationId ? "/api/files" : null, filesSchema, false);
  const browser = useRemote(conversationId ? "/api/browser/status" : null, browserSchema, false, 5000);
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"chat" | "agent">("agent");
  const [pending, setPending] = useState(() => readPending(ownerId));
  const [delivery, setDelivery] = useState<"idle" | "sending" | "unknown" | "not_found">(() => readPending(ownerId) ? "unknown" : "idle");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<unknown>();
  const [panel, setPanel] = useState<{ kind: "browser"; taskId: string } | { kind: "file"; file: FileItem; text?: string; image?: string }>();
  const [previewLoading, setPreviewLoading] = useState(false);
  const [olderTasks, setOlderTasks] = useState<TaskSummary[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null>();
  const [loadingTasks, setLoadingTasks] = useState(false);
  const [oldTaskId, setOldTaskId] = useState<string>();
  const [receiptVisible, setReceiptVisible] = useState(false);
  const alive = useRef(true);
  const panelRef = useRef<HTMLElement>(null);
  const conversationRef = useRef<HTMLElement>(null);
  const browserTrigger = useRef<HTMLButtonElement>(null);
  const fileTrigger = useRef<HTMLButtonElement | null>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const previewGeneration = useRef(0);
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 900px)").matches);
  useEffect(() => { const query = matchMedia("(max-width: 900px)"); const change = () => setMobile(query.matches); query.addEventListener("change", change); return () => query.removeEventListener("change", change); }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onPanelChange?.(!!panel && mobile); return () => onPanelChange?.(false); }, [panel, mobile, onPanelChange]);
  useLayoutEffect(() => { if (conversationRef.current) conversationRef.current.inert = !!panel && mobile; }, [panel, mobile]);
  const panelKind = panel?.kind;
  useEffect(() => { if (panelKind) panelRef.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, [panelKind]);
  useEffect(() => () => { if (panel?.kind === "file" && panel.image) URL.revokeObjectURL(panel.image); }, [panel]);
  useEffect(() => {
    if (panel?.kind === "browser" && browser.data && (!browser.data.running || browser.data.task_id !== panel.taskId)) {
      setPanel(undefined); browserTrigger.current?.focus();
    }
  }, [browser.data, panel]);
  const updateStream = useCallback((draft: string, connected: boolean) => setStream((old) => old.draft === draft && old.connected === connected ? old : { draft, connected }), []);
  useLayoutEffect(() => { const node = messagesRef.current; if (node && stickToBottom.current) node.scrollTop = node.scrollHeight; }, [messages.items.length, stream.draft]);
  const reloadConversation = conversation.reload, reloadTask = task.reload, reloadMessages = messages.reload, reloadFiles = files.reload;
  const changed = useCallback(() => { void reloadConversation(true); void reloadTask(true); void reloadMessages(true); onChanged(); }, [reloadConversation, reloadTask, reloadMessages, onChanged]);
  useEffect(() => { if (currentTask?.status) void reloadFiles(true); }, [currentTask?.status, reloadFiles]);
  useEffect(() => {
    if (!currentConversation) return;
    const page = currentConversation;
    if (olderTasks.length && page.has_more && page.tasks.length && !page.tasks.some((item) => olderTasks.some((loaded) => loaded.id === item.id))) setOlderCursor(page.next_before);
    setOlderTasks((previous) => {
      const byId = new Map(previous.map((item) => [item.id, item]));
      page.tasks.forEach((item) => byId.set(item.id, item));
      const merged = [...byId.values()];
      return merged.length === previous.length && merged.every((item, index) => item === previous[index]) ? previous : merged;
    });
  }, [currentConversation, olderTasks]);
  const tasksById = new Map(olderTasks.map((item) => [item.id, item]));
  currentConversation?.tasks.forEach((item) => tasksById.set(item.id, item));
  const executions = [...tasksById.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  const hasMoreExecutions = olderCursor === undefined ? currentConversation?.has_more : !!olderCursor;
  const artifacts = files.data?.items.filter((item) => item.task_id && tasksById.has(item.task_id) && !item.deleted_at && ["generated", "uploaded"].includes(item.status)) ?? [];
  const browserTaskId = browser.data?.running && browser.data.task_id && tasksById.has(browser.data.task_id) ? browser.data.task_id : undefined;
  const locked = delivery === "sending" || !!pending;
  const supplement = !!currentTask && live.has(currentTask.status);
  function closePanel() {
    const kind = panel?.kind; previewGeneration.current++; setPanel(undefined);
    requestAnimationFrame(() => (kind === "browser" ? browserTrigger.current : fileTrigger.current)?.focus());
  }
  async function sendPacket(packet: Pending) {
    if (delivery === "sending") return;
    setPending(packet); savePending(ownerId, packet); setDelivery("sending"); setError(undefined);
    try {
      const accepted = await api("/api/requests", receiptSchema, { method: "POST", body: packet });
      clearMatchingPending(ownerId, packet.client_request_id);
      if (!alive.current) { onChanged(); return; }
      setPending(undefined); setDelivery("idle"); setText(""); onAttached([]); setReceiptVisible(true);
      onChanged(); onAccepted(accepted.conversation_id ?? accepted.task_id);
      void messages.reload(true); void conversation.reload(true);
    } catch (failure) {
      if (!alive.current) return;
      setError(failure);
      if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500) { clearMatchingPending(ownerId, packet.client_request_id); setPending(undefined); setDelivery("idle"); }
      else setDelivery("unknown");
    }
  }
  function send(event?: FormEvent) {
    event?.preventDefault(); if (!text.trim() || locked) return;
    void sendPacket({ client_request_id: crypto.randomUUID(), text: text.trim(), mode: supplement ? "supplement" : "new", ...(conversationId ? { conversation_id: conversationId } : {}), ...(supplement && currentTask ? { task_id: currentTask.id } : {}), file_ids: attached, execution_mode: supplement ? (currentTask?.execution_mode ?? mode) : mode });
  }
  async function checkReceipt() {
    if (!pending || checking) return; setChecking(true); setError(undefined);
    try {
      const accepted = await api(`/api/requests/${encodeURIComponent(pending.client_request_id)}`, receiptSchema);
      clearMatchingPending(ownerId, pending.client_request_id); if (!alive.current) return;
      setPending(undefined); setDelivery("idle"); setText(""); onAttached([]); onChanged(); onAccepted(accepted.conversation_id ?? accepted.task_id);
    } catch (failure) { if (alive.current) { if (failure instanceof ApiError && failure.status === 404) { setDelivery("not_found"); setError(undefined); } else setError(failure); } }
    finally { if (alive.current) setChecking(false); }
  }
  async function openFile(file: FileItem, trigger: HTMLButtonElement) {
    fileTrigger.current = trigger; const generation = ++previewGeneration.current;
    setPreviewLoading(true); setError(undefined);
    try {
      if (file.size > 2 * 1024 ** 2) throw new ApiError("preview_size", "文件较大，请下载后查看。");
      const image = ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.media_type);
      const textFile = ["text/plain", "text/markdown", "text/csv"].includes(file.media_type) || /\.(txt|md|csv)$/i.test(file.name);
      if (!image && !textFile) { await download(`/api/files/${encodeURIComponent(file.id)}/download`, file.name); return; }
      const response = await fetch(`/api/files/${encodeURIComponent(file.id)}/download`, { credentials: "same-origin", cache: "no-store", redirect: "error" });
      if (!response.ok) throw new ApiError("preview_failed", "暂时无法读取文件，请重试。");
      const value = image ? { image: URL.createObjectURL(await response.blob()) } : { text: await response.text() };
      if (!alive.current || generation !== previewGeneration.current) { if (value.image) URL.revokeObjectURL(value.image); return; }
      setPanel({ kind: "file", file, ...value });
    } catch (failure) { if (alive.current && generation === previewGeneration.current) setError(failure); }
    finally { if (alive.current && generation === previewGeneration.current) setPreviewLoading(false); }
  }
  async function loadExecutions() {
    const before = olderCursor === undefined ? currentConversation?.next_before : olderCursor;
    if (!conversationId || !before || loadingTasks) return; setLoadingTasks(true);
    try { const result = await api(`/api/conversations/${encodeURIComponent(conversationId)}?limit=50&before=${encodeURIComponent(before)}`, conversationDetailSchema, { passive: true }); if (alive.current) { setOlderTasks((old) => [...result.tasks, ...old]); setOlderCursor(result.has_more ? result.next_before : null); } }
    catch (failure) { if (alive.current) setError(failure); } finally { if (alive.current) setLoadingTasks(false); }
  }
  const oldTask = useRemote(oldTaskId ? `/api/tasks/${encodeURIComponent(oldTaskId)}` : null, taskSchema, false);
  const selectedOldTask = oldTask.data?.id === oldTaskId ? oldTask.data : undefined;
  return <div className={`cw-conversation-workspace ${panel ? "has-panel" : ""}`}>
    <section className="cw-conversation" ref={conversationRef} aria-label="会话消息">
      <div className="cw-message-scroll" ref={messagesRef} onScroll={(event) => { const node = event.currentTarget; stickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 100; }}>
        <div className="cw-messages">
          <ErrorNotice error={conversation.error} /><ErrorNotice error={messages.error} />
          {!conversationId && <div className="cw-empty-state"><span className="cw-empty-mark">梦</span><h1>今天，想一起做点什么？</h1><p>从一个问题、一份文件，或一个还没成形的想法开始。</p><div className="cw-suggestions">{["帮我研究一个主题", "整理我提供的资料", "规划一个轻松的周末"].map((value) => <button key={value} onClick={() => { setText(value); inputRef.current?.focus(); }}>{value}</button>)}</div></div>}
          {conversationId && messages.hasMore && <button className="cw-load-earlier" disabled={messages.loadingMore} onClick={() => { stickToBottom.current = false; void messages.loadMore(); }}>{messages.loadingMore ? "正在读取…" : "查看更早消息"}</button>}
          {messages.items.map((message) => <article className={`cw-message ${message.role === "user" ? "cw-user-message" : "cw-agent-message"}`} key={message.id}>{message.role !== "user" && <span className="cw-agent-avatar" aria-hidden="true">梦</span>}<div className="cw-message-content">{message.role !== "user" && <span className="cw-agent-name">梦</span>}<TextContent text={message.content} /><time>{date(message.created_at)}</time></div></article>)}
          {stream.draft && currentTask?.status === "running" && <article className="cw-message cw-agent-message"><span className="cw-agent-avatar" aria-hidden="true">梦</span><div className="cw-message-content"><span className="cw-agent-name">梦 · 正在回答</span><TextContent text={stream.draft} /></div></article>}
          {browserTaskId && <div className="cw-browser-card"><span className="cw-tool-icon"><Globe size={24} /></span><div><span className="cw-card-kicker">本次会话的浏览器</span><strong>{browser.data?.url ? (() => { try { return new URL(browser.data.url).hostname; } catch { return "浏览网页"; } })() : "浏览网页"}</strong><span>{browser.data?.mode === "human" ? "人工接管中" : "正在使用浏览器"}</span></div><button ref={browserTrigger} className="button secondary" onClick={() => setPanel({ kind: "browser", taskId: browserTaskId })}>打开浏览器<ArrowUpRight size={15} /></button></div>}
          {artifacts.length > 0 && <div className="cw-artifacts" aria-label="本次会话的文件与成果">{artifacts.map((file) => <button className="cw-artifact" key={file.id} disabled={previewLoading} onClick={(event) => void openFile(file, event.currentTarget)}><FileText size={23} /><span><strong>{file.name}</strong><small>{bytes(file.size)} · {file.status === "generated" ? "生成的成果" : "会话文件"}</small></span><ArrowUpRight size={17} /></button>)}</div>}
          {currentTask && <TaskDetail key={currentTask.id} task={currentTask} active={false} compact onStream={updateStream} onChanged={changed} />}
          {(executions.length > 1 || hasMoreExecutions) && <details className="cw-previous-executions" open={executions.some((item) => item.id !== currentTaskId && (live.has(item.status) || item.unresolved_effect_count > 0)) ? true : undefined}><summary>执行记录 · {executions.length} 项</summary>{executions.map((item) => <button key={item.id} className="cw-execution-row" aria-pressed={oldTaskId ? item.id === oldTaskId : item.id === currentTaskId} onClick={() => setOldTaskId(item.id === currentTaskId ? undefined : item.id)}><span>{item.title}{item.id === currentTaskId ? " · 最近一次" : ""}</span><small>{statusLabels[item.status]}{item.unresolved_effect_count ? " · 需要核对" : ""}</small></button>)}{hasMoreExecutions && <button disabled={loadingTasks} onClick={() => void loadExecutions()}>加载更早执行</button>}<ErrorNotice error={oldTask.error} />{selectedOldTask && <><h3 className="cw-selected-execution">{selectedOldTask.title}</h3><TaskDetail key={selectedOldTask.id} task={selectedOldTask} active={false} compact onChanged={() => { void oldTask.reload(true); changed(); }} /></>}</details>}
          {receiptVisible && <p className="cw-receipt" role="status">已接收，你可以继续补充。关闭页面不会取消任务。</p>}
        </div>
      </div>
      <div className="cw-composer-area"><form className="cw-composer" onSubmit={send}>
        {attached.length > 0 && <div className="attachment-list">{attached.map((id, index) => <span className="attachment" key={id}><Paperclip size={13} />{files.data?.items.find((file) => file.id === id)?.name ?? `附件 ${index + 1}`}<button type="button" aria-label={`移除附件 ${index + 1}`} disabled={locked} onClick={() => onAttached(attached.filter((value) => value !== id))}><X size={13} /></button></span>)}</div>}
        <textarea ref={inputRef} aria-label="任务内容" placeholder={conversationId ? "继续聊，或告诉我下一步…" : "想做什么，直接告诉我…"} rows={2} maxLength={20000} value={text} disabled={locked} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); } }} />
        <div className="cw-composer-bottom"><button type="button" className="cw-icon-button" aria-label="添加文件" onClick={onFiles} disabled={locked}><Plus size={21} /></button><button type="button" className="cw-model-chip" onClick={onSettings}>{currentTask?.model_id ?? "默认模型"}<ChevronDown size={12} /></button>{!supplement && <select className="cw-mode-select" aria-label="任务执行方式" value={mode} disabled={locked} onChange={(event) => setMode(event.target.value === "chat" ? "chat" : "agent")}><option value="agent">自动执行</option><option value="chat">仅回答</option></select>}<button className="cw-send-button" aria-label="发送" type="submit" disabled={!text.trim() || locked}><ArrowUp size={20} /></button></div>
        <ErrorNotice error={error} />{pending && delivery !== "sending" && <div className="cw-delivery-unknown" role="status"><strong>{delivery === "not_found" ? "尚未查到接收记录" : "未确认接收"}</strong><p>先核对接收记录，避免重复创建任务。</p><div className="actions"><button type="button" className="button secondary" disabled={checking} onClick={() => void checkReceipt()}>{checking ? "正在核对…" : "核对接收"}</button>{delivery === "not_found" && <button type="button" className="button" onClick={() => void sendPacket(pending)}>按原编号重试</button>}</div><details><summary>查看待核对内容</summary><p>{pending.text}</p><small>{pending.file_ids.length} 个附件</small></details></div>}
      </form><p className="cw-composer-note">{delivery === "sending" ? "正在提交，请勿重复发送…" : "重要操作会请你确认 · Enter 发送，Shift + Enter 换行"}</p></div>
    </section>
    {panel && <aside className="cw-work-panel" ref={panelRef} aria-label={panel.kind === "browser" ? "会话浏览器" : "文件预览"} role={mobile ? "dialog" : undefined} aria-modal={mobile ? true : undefined} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); closePanel(); } if (event.key === "Tab" && mobile) { const controls = [...(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex="0"]') ?? [])].filter((element) => element.getClientRects().length); const first = controls[0], last = controls[controls.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } }}><header className="cw-panel-header"><span>{panel.kind === "browser" ? <Globe size={17} /> : <FileText size={17} />}{panel.kind === "browser" ? "会话浏览器" : panel.file.name}</span><button className="cw-panel-close" aria-label="返回会话" onClick={closePanel}><ArrowLeft size={16} /><span>返回会话</span></button></header>{panel.kind === "browser" ? <><BrowserTaskStop key={panel.taskId} taskId={panel.taskId} onChanged={changed} /><Suspense fallback={<p className="cw-panel-loading">正在打开浏览器…</p>}><Browser key={panel.taskId} active={false} expectedTaskId={panel.taskId} embedded /></Suspense></> : <><div className="cw-file-content">{panel.image ? <img src={panel.image} alt={panel.file.name} /> : <TextContent text={panel.text ?? ""} />}</div><button className="button secondary cw-file-download" onClick={() => void download(`/api/files/${encodeURIComponent(panel.file.id)}/download`, panel.file.name).catch(setError)}><Download size={16} />下载文件</button></>}</aside>}
    <ErrorNotice error={files.error instanceof ApiError && files.error.sleeping ? undefined : files.error} />
  </div>;
}

/** Keep cancellation available while the mobile viewer covers the conversation. */
function BrowserTaskStop({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
  const task = useRemote(`/api/tasks/${encodeURIComponent(taskId)}`, taskSchema, false, 5000);
  const action = useAction();
  const [confirm, setConfirm] = useState(false);
  const current = task.data?.id === taskId ? task.data : undefined;
  const canStop = !!current && live.has(current.status) && !current.cancel_requested;
  async function stop() {
    if (!current || !canStop || action.busy) return;
    await action.run(async () => {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/commands`, z.unknown(), {
        method: "POST", body: { command_id: crypto.randomUUID(), expected_revision: current.revision, action: "cancel" },
      });
      setConfirm(false); await task.reload(true); onChanged();
    }, "停止请求已提交；已经派发的外部操作仍可能需要核对。");
  }
  return <div className="cw-browser-task-actions"><button className="button danger-outline" disabled={!canStop || action.busy} onClick={() => setConfirm(true)}><Square size={13} />{current?.cancel_requested ? "正在停止任务" : "停止任务"}</button>{confirm && <div className="confirm-box"><strong>停止这项任务的后续步骤？</strong><p>已经发送的消息、订单或其他外部操作不能通过取消撤回。不确定的结果会保留待核对。</p><div className="actions"><button className="button danger" disabled={!canStop || action.busy} onClick={() => void stop()}>确认停止后续步骤</button><button className="button secondary" disabled={action.busy} onClick={() => setConfirm(false)}>返回浏览器</button></div></div>}<ErrorNotice error={task.error} /><ErrorNotice error={action.error} />{action.notice && <p role="status">{action.notice}</p>}</div>;
}
