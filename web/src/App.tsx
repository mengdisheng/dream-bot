import { Component, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, Suspense, lazy, type ReactNode } from "react";
import { Bell, FolderOpen, LogOut, MoreHorizontal, PanelLeft, Plus, Settings as SettingsIcon, X } from "lucide-react";
import { z } from "zod";
import { api, ApiError, setCsrf } from "./api";
import { authSchema, runtimeSchema, notificationSchema, taskSchema, listOf, statusLabels, type Owner, type Runtime } from "./types";
import { useRemote } from "./hooks";
import { useConversations } from "./conversationHooks";
import { date, ErrorNotice, Empty } from "./components";
import { Auth } from "./Auth";
import { Tasks, clearPending } from "./pages/ChatTasks";
import { ConversationWorkspace } from "./pages/ConversationWorkspace";
import { Files, Memories } from "./pages/FilesMemories";
import "./conversation.css";
const Settings = lazy(() => import("./pages/Settings").then((module) => ({ default: module.Settings })));
type Page = "chat" | "files" | "settings" | "tasks" | "memories";
const notificationsSchema = listOf(notificationSchema);
function route() {
  const [name, query = ""] = location.hash.slice(1).split("?");
  const page: Page = ["files", "settings", "tasks", "memories"].includes(name) ? name as Page : "chat";
  const params = new URLSearchParams(query);
  return { page, conversation: params.has("new") ? null : params.get("conversation") ?? undefined };
}

export function RuntimeStrip({ runtime }: { runtime: Runtime | undefined }) {
  const [now, setNow] = useState(performance.now());
  const anchor = useMemo(
    () => ({
      time: runtime?.server_time ? Date.parse(runtime.server_time) : Date.now(),
      received: performance.now(),
    }),
    [runtime?.server_time],
  );
  useEffect(() => {
    const id = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!runtime) return <span className="runtime-state">正在读取环境状态</span>;
  const labels: Record<string, string> = {
    ABSENT: "环境已销毁",
    STARTING: "环境启动中",
    RECOVERING: "正在恢复任务",
    READY: "环境可用",
    DRAINING: "保存并回收中",
    STOPPING: "正在删除环境",
    FAULTED: "环境故障",
  };
  const deadline = runtime.idle_deadline
    ? Date.parse(runtime.idle_deadline)
    : NaN;
  const seconds = Number.isFinite(deadline)
    ? Math.max(
        0,
        Math.ceil(
          (deadline - (anchor.time + Math.max(0, now - anchor.received))) /
            1000,
        ),
      )
    : null;
  return (
    <div className="runtime-state">
      <span
        className={`runtime-dot ${runtime.state === "READY" ? "is-ready" : ""}`}
      />
      <strong>
        {runtime.safety_suspended
          ? "环境已暂停保护"
          : runtime.state === "ABSENT" && !runtime.last_removed_at
            ? "环境未启动"
            : (labels[runtime.state] ?? runtime.state)}
      </strong>
      <span>
        {runtime.safety_suspended
          ? runtime.suspension_reason
          : runtime.state === "READY"
            ? runtime.idle_blockers.length
              ? `占用：${runtime.idle_blockers.join("、")}`
              : seconds === null
                ? "空闲后自动销毁"
                : `空闲回收 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
            : ["STARTING", "RECOVERING"].includes(runtime.state)
              ? `${runtime.phase} · ${Math.floor(runtime.elapsed_seconds)} 秒`
              : runtime.state === "ABSENT"
                ? "数据已保留 · 操作时按需启动"
                : (runtime.error ?? runtime.phase)}
      </span>
    </div>
  );
}

export default function App() {
  const [owner, setOwner] = useState<Owner | null>(null);
  const [initialized, setInitialized] = useState(true);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<unknown>();
  const check = useCallback(async () => {
    setChecking(true);
    try { const result = await api("/api/auth/status", authSchema); setInitialized(result.initialized); setOwner(result.authenticated ? result.owner : null); setCsrf(result.csrf_token ?? ""); setError(undefined); }
    catch (failure) { setError(failure); } finally { setChecking(false); }
  }, []);
  useEffect(() => { void check(); }, [check]);
  useEffect(() => { const expired = () => { if (owner) clearPending(owner.id); setOwner(null); setCsrf(""); }; window.addEventListener("personal-bot:unauthorized", expired); return () => window.removeEventListener("personal-bot:unauthorized", expired); }, [owner]);
  async function logout() { await api("/api/auth/logout", z.unknown(), { method: "POST" }); if (owner) clearPending(owner.id); setOwner(null); setCsrf(""); }
  if (checking) return <main className="loading-screen"><span className="brand-mark">梦</span><p>正在读取账号状态…</p></main>;
  if (error && !owner) return <main className="loading-screen"><ErrorNotice error={error} /><button className="button" onClick={() => void check()}>重新连接</button></main>;
  if (!owner) return <Auth initialized={initialized} onAuthenticated={setOwner} />;
  return <AuthenticatedWorkspace key={owner.id} owner={owner} onLogout={logout} />;
}
function AuthenticatedWorkspace({ owner, onLogout }: { owner: Owner; onLogout: () => Promise<void> }) {
  const [page, setPage] = useState<Page>(() => route().page);
  const [selected, setSelected] = useState<string | null | undefined>(() => route().conversation);
  const [navOpen, setNavOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [noticeOpen, setNoticeOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [error, setError] = useState<unknown>();
  const [attached, setAttached] = useState<string[]>([]);
  const [retrying, setRetrying] = useState(false);
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 900px)").matches);
  const sidebar = useRef<HTMLElement>(null);
  const main = useRef<HTMLDivElement>(null);
  const topbar = useRef<HTMLElement>(null);
  const navButton = useRef<HTMLButtonElement>(null);
  const conversations = useConversations();
  const runtime = useRemote("/api/runtime", runtimeSchema, false, 5000);
  const notifications = useRemote("/api/notifications", notificationsSchema, false, 15000);
  const chosen = selected === undefined ? conversations.items[0]?.id : selected;
  const title = page === "chat" ? conversations.items.find((item) => item.id === chosen)?.title ?? (chosen ? "会话" : "新对话") : ({ files: "文件与成果", settings: "设置", tasks: "执行记录", memories: "记忆与偏好" } as const)[page];
  useEffect(() => { const query = matchMedia("(max-width: 900px)"); const change = () => setMobile(query.matches); query.addEventListener("change", change); return () => query.removeEventListener("change", change); }, []);
  useEffect(() => { const change = () => { const value = route(); setPage(value.page); if (value.page === "chat") setSelected(value.conversation); setNavOpen(false); setMoreOpen(false); }; window.addEventListener("hashchange", change); return () => window.removeEventListener("hashchange", change); }, []);
  useLayoutEffect(() => { if (sidebar.current) sidebar.current.inert = mobile && (!navOpen || panelOpen); if (main.current) main.current.inert = mobile && navOpen; if (topbar.current) topbar.current.inert = panelOpen; }, [mobile, navOpen, panelOpen]);
  useEffect(() => { if (navOpen && mobile) sidebar.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, [navOpen, mobile]);
  const reloadConversations = conversations.reload;
  const changed = useCallback(() => { void reloadConversations(true); }, [reloadConversations]);
  const onPanelChange = useCallback((open: boolean) => setPanelOpen(open), []);
  const selectConversation = useCallback((id: string | null) => { setSelected(id); setPage("chat"); setNavOpen(false); setPanelOpen(false); location.hash = id ? `chat?conversation=${encodeURIComponent(id)}` : "chat?new=1"; }, []);
  function navigate(next: Page) { setPage(next); setNavOpen(false); setPanelOpen(false); setMoreOpen(false); location.hash = next === "chat" && chosen ? `chat?conversation=${encodeURIComponent(chosen)}` : next; }
  function closeNav() { setNavOpen(false); requestAnimationFrame(() => navButton.current?.focus()); }
  async function notification(item: z.infer<typeof notificationSchema>) {
    setError(undefined);
    try {
      await api(`/api/notifications/${encodeURIComponent(item.id)}/read`, z.unknown(), { method: "POST" });
      void notifications.reload(true); setNoticeOpen(false);
      if (item.task_id) { const task = await api(`/api/tasks/${encodeURIComponent(item.task_id)}`, taskSchema, { passive: true }); if (task.conversation_id) selectConversation(task.conversation_id); else navigate("tasks"); }
      else navigate("chat");
    } catch (failure) { setError(failure); }
  }
  return <div className={`cw-app ${navOpen ? "cw-nav-open" : ""} ${panelOpen ? "cw-panel-open" : ""}`}>
    <a className="skip-link" href="#workspace" onClick={(event) => { event.preventDefault(); document.getElementById("workspace")?.focus(); }}>跳转到工作区</a>
    <aside className="cw-sidebar" ref={sidebar} aria-label="会话导航" onKeyDown={(event) => { if (!mobile || !navOpen) return; if (event.key === "Escape") { event.preventDefault(); closeNav(); } if (event.key === "Tab") { const controls = [...(sidebar.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href]') ?? [])].filter((element) => element.getClientRects().length); const first = controls[0], last = controls[controls.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } }}>
      <div className="cw-brand"><span className="cw-brandmark">梦</span><span>梦 Bot</span><button className="cw-icon-button cw-nav-close" aria-label="关闭会话导航" onClick={closeNav}><X size={20} /></button></div>
      <button className="cw-new-conversation" onClick={() => { selectConversation(null); setAttached([]); }}><Plus size={18} />新对话</button>
      <div className="cw-sidebar-label">最近会话</div><nav className="cw-recent-conversations" aria-label="最近会话">{conversations.items.map((item) => <button key={item.id} aria-current={page === "chat" && chosen === item.id ? "page" : undefined} className={page === "chat" && chosen === item.id ? "selected" : ""} onClick={() => selectConversation(item.id)}><span>{item.title}</span><small>{item.latest_task_status ? statusLabels[item.latest_task_status] : "尚未开始"}</small></button>)}{conversations.hasMore && <button disabled={conversations.loadingMore} onClick={() => void conversations.loadMore()}>{conversations.loadingMore ? "正在读取…" : "更多会话"}</button>}</nav>
      {!conversations.items.length && <p className="cw-sidebar-empty">{conversations.error instanceof ApiError && conversations.error.sleeping ? "历史保存在服务器，需要时读取。" : "开始一段新的对话。"}</p>}
      <button className="cw-read-conversations" disabled={conversations.loading} onClick={() => void conversations.reload(false)}>{conversations.loading ? "正在读取…" : "读取会话"}</button>
      <div className="cw-sidebar-bottom"><button className={`cw-side-action ${page === "files" ? "selected" : ""}`} onClick={() => navigate("files")}><FolderOpen size={18} />文件与成果</button><button className={`cw-side-action ${page === "settings" ? "selected" : ""}`} onClick={() => navigate("settings")}><SettingsIcon size={18} />设置</button><div className="cw-account"><span>{owner.username.slice(0, 1).toUpperCase()}</span><div>{owner.username}<small>我的空间</small></div></div></div>
    </aside>
    {navOpen && mobile && <button className="cw-nav-backdrop" aria-label="收起会话导航" onClick={closeNav} />}
    <div className="cw-main" ref={main}><header className="cw-topbar" ref={topbar}><div className="cw-topbar-title"><button className="cw-icon-button cw-mobile-menu" ref={navButton} aria-label="展开会话导航" aria-expanded={navOpen} onClick={() => setNavOpen(!navOpen)}><PanelLeft size={20} /></button><h1>{title}</h1></div><div className="cw-topbar-tools"><button className="cw-icon-button cw-notifications-button" aria-label="站内通知" aria-expanded={noticeOpen} onClick={() => { setNoticeOpen(!noticeOpen); setMoreOpen(false); }}><Bell size={18} />{notifications.data?.items.some((item) => !item.read_at) && <i />}</button><button className="cw-icon-button" aria-label="更多选项" aria-expanded={moreOpen} onClick={() => { setMoreOpen(!moreOpen); setNoticeOpen(false); }}><MoreHorizontal size={20} /></button></div></header>
      {moreOpen && <aside className="cw-more-menu" aria-label="更多选项"><button onClick={() => navigate("tasks")}>执行记录</button><button onClick={() => { navigate("chat"); }}>返回会话</button><details><summary>运行详情</summary><RuntimeStrip runtime={runtime.data} /><ErrorNotice error={runtime.error} /></details><button onClick={() => void onLogout().catch(setError)}><LogOut size={15} />退出登录</button></aside>}
      {noticeOpen && <aside className="cw-notifications" aria-label="站内通知"><h2>通知</h2><ErrorNotice error={notifications.error} />{!notifications.data?.items.length && <Empty title="暂无通知" />}{notifications.data?.items.map((item) => <button key={item.id} onClick={() => void notification(item)}><span>{({ completed: "任务完成", failed: "任务失败", approval: "需要你确认", human: "需要你处理", runtime_faulted: "工作环境需要处理" } as Record<string, string>)[item.category] ?? "任务有新进展"}</span><small>{date(item.created_at)}</small></button>)}</aside>}
      <div className="cw-global-notices"><ErrorNotice error={error} /><ErrorNotice error={conversations.error instanceof ApiError && conversations.error.sleeping ? undefined : conversations.error} />{runtime.data?.safety_suspended && <div className="notice notice-error" role="alert">工作环境已保护暂停：{runtime.data.suspension_reason ?? "请检查服务器资源。"}</div>}{runtime.data?.state === "FAULTED" && <div className="notice notice-error" role="alert"><span>{runtime.data.error ?? "工作环境暂时不可用。"}</span>{!runtime.data.safety_suspended && <button className="button secondary" disabled={retrying} onClick={() => { setRetrying(true); void api("/api/runtime/retry", z.unknown(), { method: "POST" }).then(() => runtime.reload(true)).catch(setError).finally(() => setRetrying(false)); }}>重试环境</button>}</div>}</div>
      <main id="workspace" tabIndex={-1} className={`cw-workspace ${page !== "chat" ? "cw-secondary-page" : ""}`}><Suspense fallback={<p className="cw-loading">正在加载…</p>}>{page === "chat" && <ConversationWorkspace key={chosen ?? "new"} ownerId={owner.id} conversationId={chosen} onAccepted={selectConversation} onChanged={changed} attached={attached} onAttached={setAttached} onFiles={() => navigate("files")} onSettings={() => navigate("settings")} onPanelChange={onPanelChange} />}{page === "files" && <><button className="cw-back-link" onClick={() => navigate("chat")}>返回会话</button><Files active={false} attached={attached} onFileDeleted={(id) => setAttached((old) => old.filter((value) => value !== id))} onAttach={(ids) => { setAttached(ids); navigate("chat"); }} /></>}{page === "settings" && <><button className="cw-back-link" onClick={() => navigate("chat")}>返回会话</button><Settings active={false} /></>}{page === "tasks" && <><button className="cw-back-link" onClick={() => navigate("chat")}>返回会话</button><Tasks active={false} /></>}{page === "memories" && <><button className="cw-back-link" onClick={() => navigate("settings")}>返回设置</button><Memories active={false} /></>}</Suspense></main>
    </div>
  </div>;
}

export class AppBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    /* Private page contents are deliberately not logged. */
  }
  render() {
    return this.state.failed ? (
      <main className="loading-screen">
        <h1>页面暂时无法显示</h1>
        <p>已接收的任务不会因页面错误自动取消。</p>
        <button className="button" onClick={() => location.reload()}>
          重新打开页面
        </button>
      </main>
    ) : (
      this.props.children
    );
  }
}
