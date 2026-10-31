import { useCallback, useEffect, useRef, useState } from "react";
import RFB from "@novnc/novnc";
import {
  Monitor,
  Hand,
  Play,
  MoveRight,
  Maximize,
  PlugZap,
  Unplug,
  ShieldCheck,
} from "lucide-react";
import { api, ApiError } from "../api";
import { useAction, useRemote } from "../hooks";
import { browserSchema, viewerSchema, type BrowserStatus } from "../types";
import {
  PageTitle,
  Refresh,
  ErrorNotice,
  Notice,
  Empty,
  date,
} from "../components";

function deviceId() {
  try {
    let value = localStorage.getItem("personal-bot:device");
    if (!value) {
      value = crypto.randomUUID();
      localStorage.setItem("personal-bot:device", value);
    }
    return value;
  } catch {
    return crypto.randomUUID();
  }
}
export function Browser({ active, expectedTaskId, embedded = false }: {
  active: boolean;
  expectedTaskId?: string;
  embedded?: boolean;
  onClose?: () => void;
}) {
  const browser = useRemote("/api/browser/status", browserSchema, active, 5000);
  const action = useAction();
  const [device] = useState(deviceId);
  const [connected, setConnected] = useState(false);
  const [viewerMessage, setViewerMessage] = useState("");
  const [remaining, setRemaining] = useState<number | null>(null);
  const surface = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | undefined>(undefined);
  const epoch = useRef<number | undefined>(undefined);
  const mounted = useRef(true);
  const connectionAttempt = useRef(0);
  const mine =
    browser.data?.mode === "human" && browser.data.device_id === device;
  const disconnect = useCallback((message = "观看已关闭。任务不会因此取消。") => {
    connectionAttempt.current++;
    rfb.current?.disconnect();
    rfb.current = undefined;
    epoch.current = undefined;
    surface.current?.replaceChildren();
    setConnected(false);
    setViewerMessage(message);
  }, []);
  const cleanup = useCallback(() => {
    mounted.current = false;
    connectionAttempt.current++;
    rfb.current?.disconnect();
    rfb.current = undefined;
    surface.current?.replaceChildren();
  }, []);
  useEffect(() => {
    mounted.current = true;
    return cleanup;
  }, [cleanup, expectedTaskId]);
  useEffect(() => {
    if (rfb.current && browser.data) {
      if (expectedTaskId && browser.data.task_id !== expectedTaskId) {
        disconnect("浏览器已属于另一项任务，请返回当前会话。");
        return;
      }
      rfb.current.viewOnly = !(
        browser.data.mode === "human" && browser.data.device_id === device
      );
      if (epoch.current !== browser.data.browser_epoch) {
        disconnect("控制权或运行环境已变化，请重新打开观看。");
      }
    }
  }, [browser.data, device, expectedTaskId, disconnect]);
  useEffect(() => {
    if (!browser.data?.human_idle_deadline) {
      setRemaining(null);
      return;
    }
    const deadline = Date.parse(browser.data.human_idle_deadline);
    const tick = () =>
      setRemaining(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [browser.data?.human_idle_deadline]);
  async function openViewer(status?: BrowserStatus) {
    disconnect("正在建立观看连接…");
    const attempt = connectionAttempt.current;
    await action.run(async () => {
      const current =
        status ?? (await api("/api/browser/status", browserSchema, { passive: !!expectedTaskId }));
      if (!mounted.current || attempt !== connectionAttempt.current) return;
      if (expectedTaskId && (!current.running || current.task_id !== expectedTaskId))
        throw new ApiError("browser_task_changed", "这项任务的浏览器已关闭或已切换，请返回会话。", false, null, 409);
      browser.setData(current);
      const session = await api("/api/browser/view-sessions", viewerSchema, {
        method: "POST",
        body: { device_id: device, ...(expectedTaskId ? { expected_task_id: expectedTaskId } : {}) },
      });
      if (!mounted.current || attempt !== connectionAttempt.current) return;
      if (!/^\/api\/browser\/view\/[A-Za-z0-9_-]+$/.test(session.ws_path))
        throw new ApiError("viewer_path", "服务返回了无效的观看地址。");
      if (
        !Number.isFinite(Date.parse(session.expires_at)) ||
        Date.parse(session.expires_at) <= Date.now()
      )
        throw new ApiError(
          "viewer_expired",
          "观看票据已到期，请主动重新打开。",
        );
      if (!surface.current) return;
      const ws = new URL(session.ws_path, location.origin);
      ws.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const next = new RFB(surface.current, ws.href, { shared: true });
      next.scaleViewport = true;
      next.resizeSession = false;
      next.clipViewport = false;
      next.viewOnly = !(
        current.mode === "human" && current.device_id === device
      );
      rfb.current = next;
      epoch.current = session.browser_epoch;
      next.addEventListener("connect", () => {
        if (mounted.current && rfb.current === next) {
          setConnected(true);
          setViewerMessage("");
        }
      });
      next.addEventListener("disconnect", () => {
        if (mounted.current && rfb.current === next) {
          setConnected(false);
          setViewerMessage(
            "观看连接已断开。不会自动重连或自动交还控制权；可主动重新打开。",
          );
        }
      });
      next.addEventListener("securityfailure", () => {
        if (mounted.current && rfb.current === next) {
          next.disconnect();
          setConnected(false);
          setViewerMessage("观看鉴权未通过，请重新读取状态。");
        }
      });
    });
  }
  async function control(kind: "takeover" | "release" | "transfer") {
    disconnect("正在核对控制权…");
    const attempt = connectionAttempt.current;
    await action.run(
      async () => {
        const next = await api("/api/browser/control", browserSchema, {
          method: "POST",
          body: {
            action: kind,
            device_id: device,
            browser_epoch: browser.data?.browser_epoch,
            ...(expectedTaskId ? { expected_task_id: expectedTaskId } : {}),
          },
        });
        if (!mounted.current || attempt !== connectionAttempt.current) return;
        browser.setData(next);
        await openViewer(next);
      },
      kind === "release"
        ? "已交还控制，任务仍保持暂停。请在会话中明确继续。"
        : "控制请求已处理，请以当前控制状态为准。",
    );
  }
  const data = browser.data;
  return (
    <div className={embedded ? "browser-embedded" : undefined}>
      {!embedded && <PageTitle
        eyebrow="03 / BROWSER"
        title="看见，也能亲自处理"
        action={
          <Refresh
            onClick={() => void browser.reload()}
            busy={browser.loading}
          />
        }
      >
        一个真实浏览器，明确的控制权。密码、验证码与网站验证由你接管处理。
      </PageTitle>}
      <ErrorNotice error={browser.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      <div className="browser-layout">
        <section className="browser-main panel">
          <div className="browser-toolbar">
            <div>
              <span className={`runtime-dot ${connected ? "is-ready" : ""}`} />
              <strong>
                {data?.mode === "human"
                  ? mine
                    ? "由你控制"
                    : "另一设备正在控制"
                  : data?.mode === "agent"
                    ? "由 Bot 控制"
                    : "浏览器待命"}
              </strong>
              <small>{connected ? "画面已连接" : "画面未连接"}</small>
            </div>
            <div className="actions">
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() => void openViewer()}
              >
                <PlugZap size={16} />
                {connected ? "重新打开观看" : "打开观看"}
              </button>
              {connected && (
                <button
                  className="icon-button"
                  aria-label="关闭观看"
                  onClick={() => disconnect()}
                >
                  <Unplug size={17} />
                </button>
              )}
              <button
                className="icon-button"
                aria-label="全屏查看浏览器"
                onClick={() => {
                  void surface.current
                    ?.requestFullscreen?.()
                    .catch(() =>
                      action.setError(
                        new ApiError(
                          "fullscreen_failed",
                          "当前浏览器无法进入全屏。",
                        ),
                      ),
                    );
                }}
              >
                <Maximize size={17} />
              </button>
            </div>
          </div>
          <div className="browser-url">
            <Monitor size={15} />
            <span>{data?.url ?? "尚未打开页面"}</span>
          </div>
          <div
            className="browser-surface"
            ref={surface}
            aria-label="远程浏览器画面"
            tabIndex={0}
          />
          {!connected && (
            <div className="browser-disconnected">
              <Monitor size={36} />
              <h2>{viewerMessage || "按需打开浏览器观看"}</h2>
              <p>
                {data?.error ??
                  "仅观看不会延长工作环境寿命。画面关闭后，可返回会话继续。"}
              </p>
            </div>
          )}
          <div className="browser-caption">
            <ShieldCheck size={15} />
            <span>
              {mine
                ? "人工输入已启用；Agent 内容采集已暂停。"
                : "当前为观看模式；输入权限由服务端核验。"}
            </span>
          </div>
        </section>
        <aside className="browser-controls">
          <div className="panel inline-form">
            <span className="eyebrow">控制权</span>
            <h2>{mine ? "你正在接管" : "需要时，亲自接手"}</h2>
            {data?.mode === "human" && !mine ? (
              <button
                className="button"
                disabled={action.busy}
                onClick={() => void control("transfer")}
              >
                <MoveRight size={16} />
                转移到此设备
              </button>
            ) : mine ? (
              <button
                className="button"
                disabled={action.busy}
                onClick={() => void control("release")}
              >
                <Play size={16} />
                交还并保持暂停
              </button>
            ) : (
              <button
                className="button"
                disabled={action.busy || data?.available === false}
                onClick={() => void control("takeover")}
              >
                <Hand size={16} />
                {action.busy ? "正在交接…" : "接管浏览器"}
              </button>
            )}
            <p className="muted">
              接管确认后才开放输入。断线不会自动把控制权交还 Agent。
            </p>
            {!embedded && data?.task_id && <p className="mono">关联任务：{data.task_id}</p>}
            {remaining !== null && (
              <div
                className={`idle-countdown ${remaining <= 60 ? "warning-text" : ""}`}
              >
                <strong>
                  {Math.floor(remaining / 60)}:
                  {String(remaining % 60).padStart(2, "0")}
                </strong>
                <span>人工接管无输入剩余时间</span>
                <small>
                  到期 {date(data?.human_idle_deadline)}
                  。到期关闭浏览器后，会按实际活动时间判断环境回收，可能很快销毁。
                </small>
              </div>
            )}
          </div>
          <details className="aside-note browser-technical">
            <summary>连接详情</summary>
            <p>
              网站登录状态可能保留，但不保证长期免登录。重建后会重新核对页面，不会自动提交遗留表单。
            </p>
            {data?.display_running !== undefined && <p>画面服务：{data.display_running ? "运行中" : "未运行"} · 当前观看 {data.viewer_count ?? 0} 个连接。无人观看 30 秒后关闭画面服务，浏览器页面继续保留。</p>}
            <p>观看会话不提供剪贴板或文件传输。</p>
          </details>
          {data && !data.available && (
            <Empty title="浏览器能力当前不可用">
              {data.error ?? "请查看诊断信息。系统不会以截图模拟接管。"}
            </Empty>
          )}
        </aside>
      </div>
    </div>
  );
}
