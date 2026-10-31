import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { z } from "zod";
import {
  Plus,
  Save,
  FlaskConical,
  ExternalLink,
  Clock,
  Trash2,
  Bell,
  Download,
  KeyRound,
  X,
} from "lucide-react";
import { api, ApiError, download, safeLink } from "../api";
import { useAction, useRemote } from "../hooks";
import {
  listOf,
  connectionSchema,
  presetSchema,
  validationSchema,
  settingsSchema,
  scheduleSchema,
  sessionSchema,
  canUseAgent,
  canUseChat,
  hasActiveOverageApproval,
  type Connection,
} from "../types";
import {
  PageTitle,
  Refresh,
  ErrorNotice,
  Notice,
  Empty,
  Field,
  date,
  JsonView,
} from "../components";
import { BudgetEditor } from "./ChatTasks";
import { Memories } from "./FilesMemories";

const connectionsSchema = listOf(connectionSchema);
const presetsSchema = listOf(presetSchema);
const schedulesSchema = listOf(scheduleSchema);
const sessionsSchema = listOf(sessionSchema);
const deletedConnectionSchema = z.object({
  deleted: z.literal(true),
  id: z.string(),
  cleared_default_roles: z.array(z.enum(["chat", "agent"])),
});
const maintenanceSchema = z.object({
  stage: z.string(),
  active: z.boolean(),
  backup_configured: z.boolean(),
  recipient_public: z.string().nullable(),
  last_backup_at: z.string().nullable().optional(),
  local_commands: z.array(z.string()),
});
const tabs = [
  ["models", "模型"],
  ["memories", "记忆与偏好"],
  ["sessions", "账户与设备"],
  ["notifications", "通知"],
] as const;
const advancedTabs = [
  ["schedules", "定时任务"],
  ["maintenance", "备份与诊断"],
  ["budgets", "任务限额"],
] as const;
export function Settings({ active }: { active: boolean }) {
  const [tab, setTab] = useState<string>("models");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  return (
    <section className="settings-workspace">
      <PageTitle eyebrow="" title="设置">
        管理模型、偏好与已登录设备。
      </PageTitle>
      <div className="settings-tabs" role="tablist" aria-label="设置分类">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? "selected" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {tab === "models" && <Models active={active} />}{" "}
        {tab === "memories" && <Memories active={active} />}{" "}
        {tab === "sessions" && <Sessions />}{" "}
        {tab === "notifications" && <Notifications />}{" "}
      </div>
      <details
        className="advanced-settings"
        open={advancedOpen}
        onToggle={(event) => {
          const open = event.currentTarget.open;
          setAdvancedOpen(open);
          if (!open && advancedTabs.some(([id]) => id === tab)) setTab("models");
        }}
      >
        <summary>高级设置</summary>
        <p className="muted">计划、执行限额和维护工具，仅在需要时配置。</p>
        <div className="settings-tabs" role="tablist" aria-label="高级设置分类">
          {advancedTabs.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              className={tab === id ? "selected" : ""}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
          <a className="text-button" href="#tasks">任务记录</a>
        </div>
        {advancedOpen && (
          <div role="tabpanel">
            {tab === "schedules" && <Schedules active={active} />}
            {tab === "maintenance" && <Maintenance />}
            {tab === "budgets" && <Budgets active={active} />}
          </div>
        )}
      </details>
    </section>
  );
}

const entitlementLabels: Record<string, string> = {
  documented_supported: "官方资料允许",
  documented_restricted: "用途受限",
  needs_confirmation: "该用途未确认",
  not_applicable: "不适用套餐限制",
};
const scopeLabels: Record<string, string> = {
  interactive_chat: "交互问答",
  interactive: "交互问答",
  user_initiated_agent: "用户发起 Agent",
  agent: "用户发起 Agent",
  self_hosted_backend: "自建后端",
  backend: "自建后端",
  background_execution: "关闭页面后继续",
  background: "关闭页面后继续",
  unattended_schedule: "无人值守排程",
  scheduled: "无人值守排程",
  schedule: "无人值守排程",
  chat: "交互问答",
};
const checkLabels: Record<string, string> = {
  auth: "鉴权",
  text: "文本",
  stream: "流式",
  tools: "工具往返",
  vision: "视觉",
};
export function CapabilityChecks({
  checks,
}: {
  checks: Connection["capabilities"];
}) {
  return (
    <div className="capability-grid">
      {Object.entries(checks).map(([key, value]) => (
        <div key={key}>
          <span>{checkLabels[key] ?? key}</span>
          <strong
            className={
              value === "passed"
                ? "success-text"
                : value === "failed"
                  ? "danger-text"
                  : "muted"
            }
          >
            {value === "passed"
              ? "通过"
              : value === "failed"
                ? "未通过"
                : "未验证"}
          </strong>
        </div>
      ))}
    </div>
  );
}
export function OverageApprovalStatus({
  connection,
  now = Date.now(),
}: {
  connection: Connection;
  now?: number;
}) {
  const approval = connection.overage_approval;
  if (!approval?.expires_at) return null;
  const active = hasActiveOverageApproval(connection, now);
  const expired =
    approval.reason === "expired" ||
    (Number.isFinite(Date.parse(approval.expires_at)) &&
      Date.parse(approval.expires_at) <= now);
  const unavailable: Record<string, string> = {
    expired: "授权已到期，后续模型请求需要重新确认。",
    revoked: "授权已撤销。",
    connection_changed: "连接已变更，原授权不再适用。",
    ineligible: "当前连接不满足此授权的使用条件。",
  };
  return (
    <div
      className="overage-note"
      role="status"
      aria-label="积分包临时授权"
    >
      <strong>积分包临时授权</strong>
      <p>
        {active
          ? approval.can_create_task && approval.remaining_tasks > 0
            ? "授权有效，仅限本次个人交互任务。"
            : "本次新任务名额已用完；已登记任务可在有效期内继续。"
          : expired
            ? unavailable.expired
            : unavailable[approval.reason ?? ""] ?? "当前没有可用授权。"}
      </p>
      <small>到期：{date(approval.expires_at)}</small>
      <small>
        任务名额：已用 {approval.tasks_used} / {approval.max_tasks}，剩余{" "}
        {approval.remaining_tasks}。
      </small>
      {approval.budgets && (
        <small>
          每任务最多 {approval.budgets.max_tools} 次工具、{" "}
          {approval.budgets.max_tokens.toLocaleString()} tokens、{" "}
          {approval.budgets.max_active_seconds} 秒执行时间。
        </small>
      )}
      <small>仅允许本次已确认的积分包抵扣；不开放后台或定时任务。</small>
    </div>
  );
}
function Models({ active }: { active: boolean }) {
  const connections = useRemote(
    "/api/model-connections",
    connectionsSchema,
    active,
  );
  const presets = useRemote("/api/model-presets", presetsSchema, active);
  const settings = useRemote("/api/settings", settingsSchema, active);
  const action = useAction();
  const [editing, setEditing] = useState<Connection | null | undefined>();
  const [removing, setRemoving] = useState<Connection>();
  const [deletionNotice, setDeletionNotice] = useState("");
  const addButton = useRef<HTMLButtonElement>(null);
  const focusAfterRefresh = useRef(false);
  const [validation, setValidation] = useState<{
    id: string;
    result: z.infer<typeof validationSchema>;
  }>();
  const [expiryTick, setExpiryTick] = useState(0);
  const controlsBusy = action.busy || connections.loading || settings.loading;
  useEffect(() => {
    if (focusAfterRefresh.current && editing === undefined && !removing && !controlsBusy) {
      focusAfterRefresh.current = false;
      addButton.current?.focus({ preventScroll: true });
    }
  }, [controlsBusy, editing, removing]);
  useEffect(() => {
    const now = Date.now();
    const expiries = (connections.data?.items ?? [])
      .map((connection) =>
        Date.parse(connection.overage_approval?.expires_at ?? ""),
      )
      .filter((expiry) => Number.isFinite(expiry) && expiry > now);
    if (!expiries.length) return;
    const timer = window.setTimeout(
      () => setExpiryTick((value) => value + 1),
      Math.min(Math.max(Math.min(...expiries) - now + 1, 1), 2_147_483_647),
    );
    return () => window.clearTimeout(timer);
  }, [connections.data, expiryTick]);
  async function bind(connection: Connection, role: "chat" | "agent") {
    await action.run(
      async () => {
        if (!(role === "agent" ? canUseAgent(connection) : canUseChat(connection))) {
          throw new Error("该连接当前不可用于新任务，请刷新连接状态。");
        }
        await api("/api/settings/default-model", z.unknown(), {
          method: "POST",
          body: { role, connection_id: connection.id },
        });
        await settings.reload(true);
      },
      `已设置默认${role === "agent" ? "自动执行" : "问答"}模型。`,
    );
  }
  return (
    <>
      <div className="settings-section-heading">
        <div>
          <h2>模型连接</h2>
          <p>保存配置不调用模型。验证可能消耗套餐或 API 额度。</p>
        </div>
        <div className="actions">
          <Refresh
            busy={connections.loading}
            onClick={() => {
              void connections.reload();
              void presets.reload();
              void settings.reload();
            }}
          />
          <button ref={addButton} className="button" disabled={controlsBusy} onClick={() => setEditing(null)}>
            <Plus size={16} />
            添加连接
          </button>
        </div>
      </div>
      <ErrorNotice error={connections.error} />
      <ErrorNotice error={presets.error} />
      <ErrorNotice error={settings.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      {deletionNotice && <Notice>{deletionNotice}</Notice>}
      <details className="connection-guidance">
        <summary>添加连接前需要了解什么？</summary>
        <p className="muted">
          技术能力、套餐用途与超额扣费分别核对。明确禁止自建后端的套餐入口不可用于本
          Bot；可另行配置标准 API。ChatGPT 兼容指 API 协议，不代表网页订阅自带
          API 权益。
        </p>
      </details>
      {editing !== undefined && (
        <ConnectionForm
          key={editing?.id ?? "new"}
          connection={editing}
          presets={presets.data?.items ?? []}
          onCancel={() => setEditing(undefined)}
          onSaved={() => {
            focusAfterRefresh.current = true;
            setEditing(undefined);
            setValidation(undefined);
            void connections.reload(true);
          }}
        />
      )}
      {removing && (
        <DeleteConnection
          connection={removing}
          onCancel={() => setRemoving(undefined)}
          onRefresh={() => {
            void connections.reload(true);
            void settings.reload(true);
          }}
          onDeleted={(result) => {
            focusAfterRefresh.current = true;
            connections.setData((current) => current && ({
              ...current,
              items: current.items.filter((item) => item.id !== result.id),
            }));
            settings.setData((current) => current && ({
              ...current,
              default_chat_connection: current.default_chat_connection === result.id ? null : current.default_chat_connection,
              default_agent_connection: current.default_agent_connection === result.id ? null : current.default_agent_connection,
            }));
            setRemoving(undefined);
            setValidation(undefined);
            setDeletionNotice(result.cleared_default_roles.length
              ? "连接已删除，对应的默认模型设置已清除。历史任务和文件已保留。"
              : "连接已删除。历史任务和文件已保留。");
            void connections.reload(true);
            void settings.reload(true);
          }}
        />
      )}
      <div className="connection-list">
        {connections.data?.items.map((connection) => (
          <article className="connection-item panel" key={connection.id}>
            <div className="panel-top">
              <div>
                <h3>{connection.display_name}</h3>
              </div>
              <span
                className={`status ${connection.enabled ? "status-succeeded" : "status-paused"}`}
              >
                {connection.enabled ? "已启用" : "已停用"}
              </span>
            </div>
            <p className="mono connection-model">{connection.model_id}</p>
            <div className="connection-defaults">
              {settings.data?.default_chat_connection === connection.id && (
                <span className="status status-succeeded">默认问答模型</span>
              )}
              {settings.data?.default_agent_connection === connection.id && (
                <span className="status status-succeeded">默认执行模型</span>
              )}
            </div>
            <div className="actions connection-primary-actions">
              <button
                className="button secondary"
                disabled={controlsBusy}
                onClick={() => setEditing(connection)}
              >
                编辑
              </button>
              <button
                className="text-button danger-text"
                disabled={controlsBusy}
                onClick={() => setRemoving(connection)}
              >
                <Trash2 size={15} />
                删除连接
              </button>
            </div>
            <div className="overage-note">
              <strong>费用与授权</strong>
              <p>
                {connection.provider_overage_state === "confirmed_disabled"
                  ? "已记录关闭超额消费；以厂商当前后台设置为准。"
                  : connection.provider_overage_state === "enabled"
                    ? connection.provider_overage_kind === "purchased_credits"
                      ? "套餐耗尽后可能抵扣已购积分包；模型请求需要有效的费用授权。"
                      : "套餐耗尽后可能产生额外消费，请核对厂商设置。"
                    : "超额消费状态未知，请到厂商后台核对后付费和已购积分包抵扣。"}
              </p>
              <small>本平台不会自动切换连接或改用按量入口；厂商内部扣费规则仍然适用。</small>
            </div>
            <OverageApprovalStatus connection={connection} />
            <details className="connection-details">
              <summary>连接详情与验证</summary>
            <p className="muted">{connection.provider} · {connection.billing_mode}</p>
            <p className="muted break-all">
              {connection.protocol} · {connection.region} ·{" "}
              {connection.base_url}
            </p>
            <CapabilityChecks
              checks={
                validation?.id === connection.id
                  ? validation.result.checks
                  : connection.capabilities
              }
            />
            <h4>用途权益</h4>
            {Object.keys(connection.entitlements).length ? (
              <div className="entitlement-list">
                {Object.entries(connection.entitlements).map(
                  ([scope, evidence]) => (
                    <div key={scope}>
                      <span>{scopeLabels[scope] ?? scope}</span>
                      <strong
                        className={
                          evidence.status === "documented_restricted"
                            ? "danger-text"
                            : evidence.status === "needs_confirmation"
                              ? "warning-text"
                              : ""
                        }
                      >
                        {entitlementLabels[evidence.status]}
                      </strong>
                      {evidence.source_url && safeLink(evidence.source_url) && (
                        <a
                          href={safeLink(evidence.source_url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          官方依据
                          <ExternalLink size={12} />
                        </a>
                      )}
                      {evidence.quote && <small>{evidence.quote}</small>}
                      {evidence.checked_at && (
                        <small>核验：{date(evidence.checked_at)}</small>
                      )}
                    </div>
                  ),
                )}
              </div>
            ) : (
              <p className="warning-text">
                未返回用途依据，不能据此视为自动执行可用。
              </p>
            )}
            <details>
              <summary>账户、凭证与额度池</summary>
              <dl>
                <dt>账户</dt>
                <dd>{connection.account_id ?? "未知"}</dd>
                <dt>凭证标识</dt>
                <dd>{connection.credential_id ?? "未知"}</dd>
                <dt>计费产品</dt>
                <dd>{connection.billing_entry_id ?? "未知"}</dd>
              </dl>
              {connection.quota_bindings.length ? (
                <JsonView value={connection.quota_bindings} />
              ) : (
                <p className="muted">
                  剩余额度未知。本地累计不代表厂商余额，也不包含其他客户端消耗。
                </p>
              )}
            </details>
            {validation?.id === connection.id && (
              <div className="notice notice-muted">
                本次验证：{date(validation.result.validated_at)}。
                {validation.result.errors.length
                  ? "部分项目未通过；请检查连接权限与协议。"
                  : "请按分项结果判断可用范围。"}
              </div>
            )}
            <div className="actions">
              <button
                className="button secondary"
                disabled={
                  controlsBusy ||
                  connection.entitlements.backend?.status ===
                    "documented_restricted"
                }
                onClick={() =>
                  void action.run(async () => {
                    const result = await api(
                      `/api/model-connections/${encodeURIComponent(connection.id)}/validate`,
                      validationSchema,
                      { method: "POST" },
                    );
                    setValidation({ id: connection.id, result });
                    await connections.reload(true);
                  }, "验证结束，请查看分项结果。")
                }
              >
                <FlaskConical size={16} />
                验证连接
              </button>
              <button
                className="button secondary"
                disabled={controlsBusy || !canUseChat(connection)}
                onClick={() => void bind(connection, "chat")}
              >
                {settings.data?.default_chat_connection === connection.id
                  ? "默认问答模型"
                  : "设为问答模型"}
              </button>
              <button
                className="button secondary"
                disabled={controlsBusy || !canUseAgent(connection)}
                onClick={() => void bind(connection, "agent")}
              >
                {settings.data?.default_agent_connection === connection.id
                  ? "默认执行模型"
                  : "设为执行模型"}
              </button>
            </div>
            <small className="muted">
              验证最多发出 4 次模型请求，每次输出预算最多 2048 tokens，总时长最多 120
              秒；鉴权随真实请求确认，可能产生消耗。默认不测视觉。用途限制不因测试通过而解除。
            </small>
            </details>
          </article>
        ))}
      </div>
      {!connections.data?.items.length && (
        <Empty title="先添加一个模型连接">
          选择标准 API 或用途已确认允许的套餐。保存后再进行独立验证。
        </Empty>
      )}
    </>
  );
}

function ConnectionDialog({
  title, children, busy = false, onClose,
}: {
  title: string;
  children: ReactNode;
  busy?: boolean;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
    element.showModal();
    const focusTarget = element.querySelector<HTMLElement>("[data-dialog-initial-focus], input, select, textarea");
    focusTarget?.focus({ preventScroll: true });
    return () => {
      element.close();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="connection-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <header className="connection-dialog-heading">
        <h3 id={titleId}>{title}</h3>
        <button
          type="button"
          className="icon-button"
          aria-label={`关闭${title}`}
          disabled={busy}
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      {children}
    </dialog>
  );
}

function DeleteConnection({ connection, onCancel, onDeleted, onRefresh }: {
  connection: Connection;
  onCancel: () => void;
  onDeleted: (result: z.infer<typeof deletedConnectionSchema>) => void;
  onRefresh: () => void;
}) {
  const action = useAction();
  const stale = action.error instanceof ApiError && ["revision_conflict", "not_found"].includes(action.error.code);
  return (
    <ConnectionDialog title="删除模型连接" busy={action.busy} onClose={onCancel}>
      <div className="connection-delete-body">
        <p>确认删除「{connection.display_name}」？</p>
        <p>将删除此连接保存的配置和密钥，并清除它的默认模型设置。历史任务、用量记录和文件保留；其他连接仍引用的共享密钥会保留。</p>
        <p className="muted">有未结束任务或连接验证正在使用时，需要先处理完成再删除。</p>
        <ErrorNotice error={action.error} />
        {stale && <p className="warning-text">连接状态已更新，请取消后重新核对再删除。</p>}
        <div className="actions">
          <button
            type="button"
            className="button secondary"
            data-dialog-initial-focus
            disabled={action.busy}
            onClick={onCancel}
          >取消</button>
          <button
            type="button"
            className="button danger"
            disabled={action.busy || stale}
            onClick={() => void action.run(async () => {
              try {
                const result = await api(
                  `/api/model-connections/${encodeURIComponent(connection.id)}?expected_revision=${connection.revision}`,
                  deletedConnectionSchema,
                  { method: "DELETE" },
                );
                onDeleted(result);
              } catch (error) {
                if (error instanceof ApiError && ["revision_conflict", "not_found"].includes(error.code)) onRefresh();
                throw error;
              }
            })}
          >{action.busy ? "正在删除…" : "确认删除连接"}</button>
        </div>
      </div>
    </ConnectionDialog>
  );
}

function ConnectionForm({
  connection,
  presets,
  onCancel,
  onSaved,
}: {
  connection: Connection | null;
  presets: z.infer<typeof presetSchema>[];
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [provider, setProvider] = useState(connection?.provider ?? "custom");
  const [name, setName] = useState(connection?.display_name ?? "");
  const [region, setRegion] = useState(connection?.region ?? "cn");
  const [billing, setBilling] = useState(connection?.billing_mode ?? "api");
  const [protocol, setProtocol] = useState(
    connection?.protocol ?? "chat_completions",
  );
  const [url, setUrl] = useState(connection?.base_url ?? "");
  const [model, setModel] = useState(connection?.model_id ?? "");
  const [key, setKey] = useState("");
  const [enabled, setEnabled] = useState(connection?.enabled ?? true);
  const action = useAction();
  const preset = presets.find((item) => item.id === provider);
  const planEntitlements =
    billing !== "api" ? preset?.plan_entitlements : undefined;
  const endpointSuffix =
    protocol === "responses" ? "/responses" : "/chat/completions";
  const trimmedUrl = url.trim().replace(/\/+$/, "");
  const requestUrl = trimmedUrl.endsWith(endpointSuffix)
    ? trimmedUrl
    : trimmedUrl + endpointSuffix;
  function chooseBilling(value: string) {
    setBilling(value);
    if (preset && !Array.isArray(preset.base_urls))
      setUrl(preset.base_urls[value] ?? "");
  }
  function choose(id: string) {
    setProvider(id);
    const next = presets.find((item) => item.id === id);
    if (next) {
      const nextBilling = next.billing_modes[0] ?? "api";
      setBilling(nextBilling);
      setProtocol(next.protocols[0] ?? "chat_completions");
      setUrl(
        Array.isArray(next.base_urls)
          ? (next.base_urls[0] ?? "")
          : (next.base_urls[nextBilling] ?? ""),
      );
    }
  }
  return (
    <ConnectionDialog title={connection ? "编辑连接" : "添加模型连接"} busy={action.busy} onClose={onCancel}>
    <form
      className="connection-form panel"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          await api(
            connection
              ? `/api/model-connections/${encodeURIComponent(connection.id)}`
              : "/api/model-connections",
            connectionSchema,
            {
              method: connection ? "PATCH" : "POST",
              body: {
                display_name: name,
                provider,
                region,
                billing_mode: billing,
                protocol,
                base_url: url,
                model_id: model,
                enabled,
                ...(key ? { api_key: key } : {}),
                ...(connection
                  ? { expected_revision: connection.revision }
                  : {}),
              },
            },
          );
          setKey("");
          onSaved();
        });
      }}
    >
      <div className="form-grid">
        <Field label="连接名称">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={120}
          />
        </Field>
        <Field label="供应商">
          <select value={provider} onChange={(e) => choose(e.target.value)}>
            <option value="custom">自定义 OpenAI 兼容接口</option>
            {presets
              .filter((item) => item.id !== "custom")
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
          </select>
        </Field>
        <Field label="地区 / 站点">
          <input
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            required
          />
        </Field>
        <Field label="计费入口">
          <select
            value={billing}
            onChange={(e) => chooseBilling(e.target.value)}
          >
            {Array.from(
              new Set([
                billing,
                ...(preset?.billing_modes ?? ["api", "token_plan"]),
              ]),
            ).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </Field>
        <Field label="协议">
          <select
            value={protocol}
            onChange={(e) => setProtocol(e.target.value)}
          >
            {Array.from(
              new Set([
                protocol,
                ...(preset?.protocols ?? ["chat_completions", "responses"]),
              ]),
            ).map((value) => (
              <option key={value} value={value}>
                {value === "responses"
                  ? "OpenAI Responses"
                  : value === "chat_completions"
                    ? "OpenAI Chat Completions"
                    : value}
              </option>
            ))}
          </select>
        </Field>
        <Field label="模型 ID" hint="供应商没有模型列表接口时，可直接填写。">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            required
            autoComplete="off"
          />
        </Field>
      </div>
      <Field
        label="API 地址"
        hint="保留套餐专用路径。服务端保存前会校验和规范化；不会试探其他计费入口。"
      >
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
          placeholder="https://example.com/v1"
        />
      </Field>
      {url.trim() && <p className="muted mono">请求地址预览：{requestUrl}</p>}
      <Field label={connection ? "替换 API Key（留空保留原密钥）" : "API Key"}>
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          required={!connection}
          autoComplete="new-password"
        />
      </Field>
      {preset && (
        <div className="notice notice-muted">
          {Array.isArray(preset.notes) ? preset.notes.join(" ") : preset.notes}
        </div>
      )}
      {planEntitlements?.backend && (
        <div className="notice notice-muted">
          <div>
            <strong>
              自建后端：{entitlementLabels[planEntitlements.backend.status]}
            </strong>
            {planEntitlements.backend.quote && (
              <p>{planEntitlements.backend.quote}</p>
            )}
            {planEntitlements.backend.source_url &&
              safeLink(planEntitlements.backend.source_url) && (
                <a
                  href={safeLink(planEntitlements.backend.source_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  查看供应商依据
                </a>
              )}
          </div>
        </div>
      )}
      <label className="check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        启用此连接
      </label>
      <p className="muted">
        更改地址、模型、协议或密钥后，相关验证结果会失效。修改默认模型不会偷偷切换已经绑定的任务。
      </p>
      <ErrorNotice error={action.error} />
      <div className="actions">
        <button className="button" disabled={action.busy}>
          <Save size={16} />
          {action.busy ? "正在保存…" : "仅保存配置"}
        </button>
        <button type="button" className="button secondary" disabled={action.busy} onClick={onCancel}>
          返回
        </button>
      </div>
    </form>
    </ConnectionDialog>
  );
}

function Schedules({ active }: { active: boolean }) {
  const schedules = useRemote("/api/schedules", schedulesSchema, active);
  const action = useAction();
  const [editing, setEditing] = useState<
    z.infer<typeof scheduleSchema> | null | undefined
  >();
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [remove, setRemove] = useState<string>();
  function edit(item: z.infer<typeof scheduleSchema> | null) {
    setEditing(item);
    setTitle(item?.title ?? "");
    setPrompt(item?.prompt ?? "");
    setCron(item?.cron ?? "0 9 * * *");
    setTimezone(item?.timezone ?? "Asia/Shanghai");
  }
  return (
    <>
      <div className="settings-section-heading">
        <div>
          <h2>定时任务</h2>
          <p>由宿主按时唤醒；等待审批或额度的上一项未结束时，跳过新周期。</p>
        </div>
        <div className="actions">
          <Refresh
            onClick={() => void schedules.reload()}
            busy={schedules.loading}
          />
          <button className="button" onClick={() => edit(null)}>
            <Plus size={16} />
            添加排程
          </button>
        </div>
      </div>
      <ErrorNotice error={schedules.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      {editing !== undefined && (
        <form
          className="panel inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              await api(
                editing
                  ? `/api/schedules/${encodeURIComponent(editing.id)}`
                  : "/api/schedules",
                scheduleSchema,
                {
                  method: editing ? "PATCH" : "POST",
                  body: {
                    title,
                    prompt,
                    cron,
                    timezone,
                    enabled: editing?.enabled ?? false,
                    ...(editing ? { expected_revision: editing.revision } : {}),
                  },
                },
              );
              setEditing(undefined);
              await schedules.reload(true);
            }, "排程已保存，是否生效以宿主登记状态为准。");
          }}
        >
          <Field label="排程名称">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
            />
          </Field>
          <Field label="任务指令">
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              required
              rows={4}
            />
          </Field>
          <div className="form-grid">
            <Field
              label="五字段 Cron"
              hint="例如 0 9 * * * 表示所选时区每天 09:00。"
            >
              <input
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                required
              />
            </Field>
            <Field label="IANA 时区">
              <input
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                required
              />
            </Field>
          </div>
          <div className="actions">
            <button className="button" disabled={action.busy}>
              保存排程
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => setEditing(undefined)}
            >
              返回
            </button>
          </div>
        </form>
      )}
      <div className="schedule-list">
        {schedules.data?.items.map((item) => (
          <article className="panel schedule-item" key={item.id}>
            <div className="panel-top">
              <Clock size={18} />
              <h3>{item.title}</h3>
              <span className="status">
                {!item.enabled
                  ? "已停用"
                  : ["registered", "confirmed", "active"].includes(
                        item.registration_status,
                      )
                    ? "已登记启用"
                    : "正在登记"}
              </span>
            </div>
            <p>{item.prompt}</p>
            <p className="mono">
              {item.cron} · {item.timezone}
            </p>
            <p className="muted">
              下次触发：
              {item.next_fire_at
                ? `${date(item.next_fire_at)}（按设备时区显示；排程使用 ${item.timezone}）`
                : "未安排"}
            </p>
            <div className="actions">
              <button
                className="button secondary"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await api(
                      `/api/schedules/${encodeURIComponent(item.id)}`,
                      scheduleSchema,
                      {
                        method: "PATCH",
                        body: {
                          expected_revision: item.revision,
                          enabled: !item.enabled,
                        },
                      },
                    );
                    await schedules.reload(true);
                  }, "变更已保存，正在核对宿主登记。")
                }
              >
                {item.enabled ? "停用" : "启用"}
              </button>
              <button className="text-button" onClick={() => edit(item)}>
                编辑
              </button>
              <button
                className="text-button danger-text"
                onClick={() => setRemove(item.id)}
              >
                <Trash2 size={15} />
                删除
              </button>
            </div>
            {remove === item.id && (
              <div className="confirm-box">
                <p>删除排程会撤销后续周期，已经接收的任务仍可在任务页处理。</p>
                <div className="actions">
                  <button
                    className="button danger"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(async () => {
                        await api(
                          `/api/schedules/${encodeURIComponent(item.id)}`,
                          z.unknown(),
                          {
                            method: "DELETE",
                            body: { expected_revision: item.revision },
                          },
                        );
                        setRemove(undefined);
                        await schedules.reload(true);
                      }, "排程删除已提交。")
                    }
                  >
                    确认删除
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => setRemove(undefined)}
                  >
                    返回
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
      </div>
      {!schedules.data?.items.length && (
        <Empty title="尚未设置定时任务">
          先完成可用于无人值守用途的模型配置。
        </Empty>
      )}
    </>
  );
}

function Sessions() {
  const sessions = useRemote("/api/auth/sessions", sessionsSchema);
  const action = useAction();
  return (
    <>
      <div className="settings-section-heading">
        <div>
          <h2>设备会话</h2>
          <p>撤销后，该设备立即失去后续操作和浏览器输入权限。</p>
        </div>
        <Refresh
          onClick={() => void sessions.reload()}
          busy={sessions.loading}
        />
      </div>
      <ErrorNotice error={sessions.error} />
      <ErrorNotice error={action.error} />
      {sessions.data?.items.map((item) => (
        <article className="panel session-item" key={item.id}>
          <div>
            <h3>{item.current ? "当前设备" : "已登录设备"}</h3>
            <p className="break-all">{item.user_agent}</p>
            <small>
              最近活动 {date(item.last_seen_at)} · 到期 {date(item.expires_at)}
            </small>
          </div>
          <button
            className="button danger-outline"
            disabled={action.busy}
            onClick={() =>
              void action.run(async () => {
                await api(
                  `/api/auth/sessions/${encodeURIComponent(item.id)}`,
                  z.unknown(),
                  { method: "DELETE" },
                );
                if (item.current)
                  window.dispatchEvent(new Event("personal-bot:unauthorized"));
                else await sessions.reload(true);
              })
            }
          >
            撤销会话
          </button>
        </article>
      ))}
    </>
  );
}

function Notifications() {
  const key = useRemote(
    "/api/push/public-key",
    z.object({ public_key: z.string().nullable(), configured: z.boolean() }),
  );
  const subscriptions = useRemote(
    "/api/push/subscriptions",
    z.object({
      items: z.array(
        z.object({
          id: z.string(),
          created_at: z.string(),
          disabled: z.boolean(),
        }),
      ),
    }),
  );
  const action = useAction();
  const [permission, setPermission] = useState<string>(
    typeof Notification === "undefined"
      ? "unavailable"
      : Notification.permission,
  );
  async function enable() {
    await action.run(async () => {
      if (
        !("serviceWorker" in navigator) ||
        !("PushManager" in window) ||
        !key.data?.public_key
      )
        throw new Error("push_unavailable");
      const value = await Notification.requestPermission();
      setPermission(value);
      if (value !== "granted") return;
      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const raw = atob(
        key.data.public_key.replace(/-/g, "+").replace(/_/g, "/"),
      );
      const bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      const subscription =
        (await registration.pushManager.getSubscription()) ??
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: bytes,
        }));
      await api("/api/push/subscriptions", z.unknown(), {
        method: "POST",
        body: subscription.toJSON(),
      });
      await subscriptions.reload(true);
    }, "通知设置已处理。实际送达取决于浏览器和网络，站内记录始终保留。");
  }
  return (
    <>
      <h2>通知</h2>
      <p>
        任务完成、失败或需要处理时，始终保存站内通知。浏览器推送由你主动启用。
      </p>
      <ErrorNotice error={key.error} />
      <ErrorNotice error={subscriptions.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      <section className="panel inline-form">
        <Bell size={24} />
        <h3>浏览器推送</h3>
        <p>
          权限：
          {(
            {
              default: "尚未询问",
              granted: "已允许",
              denied: "已拒绝",
              unavailable: "当前浏览器不支持",
            } as Record<string, string>
          )[permission] ?? permission}
        </p>
        <p className="muted">
          推送正文只提示状态更新，不包含任务内容、文件、账号或审批细节。拒绝推送不影响任务运行。
        </p>
        <button
          className="button"
          disabled={
            action.busy ||
            !key.data?.configured ||
            permission === "denied" ||
            permission === "unavailable"
          }
          onClick={() => void enable()}
        >
          启用此设备推送
        </button>
        {!key.data?.configured && (
          <p className="muted">服务端推送公钥尚未配置。</p>
        )}
        {permission === "denied" && (
          <p className="muted">如需启用，请在浏览器站点设置中修改通知权限。</p>
        )}
      </section>
      {subscriptions.data?.items.map((item) => (
        <article className="panel session-item" key={item.id}>
          <div>
            <strong>{item.disabled ? "已失效订阅" : "已登记订阅"}</strong>
            <small>{date(item.created_at)}</small>
          </div>
          <button
            className="button secondary"
            disabled={action.busy}
            onClick={() =>
              void action.run(async () => {
                await api(
                  `/api/push/subscriptions/${encodeURIComponent(item.id)}`,
                  z.unknown(),
                  { method: "DELETE" },
                );
                await subscriptions.reload(true);
              }, "订阅已移除。")
            }
          >
            移除订阅
          </button>
        </article>
      ))}
    </>
  );
}

function Maintenance() {
  const backups = useRemote(
    "/api/backups",
    z.object({
      items: z.array(
        z.object({
          id: z.string(),
          created_at: z.string(),
          size: z.number().optional(),
          verified: z.literal(true),
        }),
      ),
    }),
  );
  const maintenance = useRemote("/api/maintenance/status", maintenanceSchema);
  const [recipient, setRecipient] = useState("");
  const action = useAction();
  return (
    <>
      <div className="settings-section-heading">
        <div>
          <h2>备份与诊断</h2>
          <p>网页管理备份接收公钥、下载完整加密归档并导出脱敏诊断。</p>
        </div>
        <Refresh
          onClick={() => {
            void backups.reload();
            void maintenance.reload();
          }}
        />
      </div>
      <ErrorNotice error={backups.error} />
      <ErrorNotice error={maintenance.error} />
      <ErrorNotice error={action.error} />
      {action.notice && <Notice>{action.notice}</Notice>}
      <section className="panel inline-form">
        <h3>备份接收公钥</h3>
        <p className="muted">
          只提交 age
          接收公钥。恢复私钥由你独立保管，不填写到网页或长期保存在服务器。
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              await api("/api/settings/backup-recipient", z.unknown(), {
                method: "PUT",
                body: { recipient },
              });
              setRecipient("");
              await maintenance.reload(true);
            }, "备份接收公钥已保存。");
          }}
        >
          <Field label="age 接收公钥">
            <input
              value={recipient}
              onChange={(e) => setRecipient(e.target.value)}
              pattern="age1[a-z0-9]+"
              placeholder="age1…"
              required
              autoComplete="off"
            />
          </Field>
          <button className="button" disabled={action.busy}>
            <KeyRound size={16} />
            保存公钥
          </button>
        </form>
      </section>
      <section className="panel inline-form">
        <h3>维护状态</h3>
        {maintenance.data ? (
          <dl>
            <dt>当前状态</dt>
            <dd>
              {maintenance.data.active
                ? `维护中 · ${maintenance.data.stage}`
                : "未进行维护"}
            </dd>
            <dt>备份配置</dt>
            <dd>
              {maintenance.data.backup_configured
                ? "已配置接收公钥"
                : "尚未启用"}
            </dd>
            <dt>最近完整备份</dt>
            <dd>{date(maintenance.data.last_backup_at)}</dd>
            <dt>接收公钥</dt>
            <dd className="mono">
              {maintenance.data.recipient_public ?? "未配置"}
            </dd>
          </dl>
        ) : (
          <p className="muted">尚未读取维护状态。</p>
        )}
        <button
          className="button secondary"
          disabled={action.busy}
          onClick={() =>
            void action.run(
              () =>
                download(
                  "/api/diagnostics/export",
                  "personal-bot-diagnostics.json",
                  "POST",
                ),
              "脱敏诊断已导出。",
            )
          }
        >
          <Download size={16} />
          导出脱敏诊断
        </button>
        <p className="muted">
          默认不含正文、密钥、Cookie，不会自动上传给开发者。休眠环境的实时信息标为不可用。
        </p>
      </section>
      <h3 className="section-label">可下载备份</h3>
      {backups.data?.items.map((item) => (
        <article className="panel session-item" key={item.id}>
          <div>
            <strong>{date(item.created_at)}</strong>
            <small>{item.id} · 已校验的完整归档</small>
          </div>
          <button
            className="button secondary"
            disabled={action.busy || !item.verified}
            onClick={() =>
              void action.run(() =>
                download(
                  `/api/backups/${encodeURIComponent(item.id)}/download`,
                  item.id,
                ),
              )
            }
          >
            <Download size={16} />
            下载加密归档
          </button>
        </article>
      ))}
      {!backups.data?.items.length && (
        <Empty title="没有可下载的完整备份">
          备份未配置、尚未完成或失败时，不会显示成可恢复归档。
        </Empty>
      )}
      <div className="notice notice-muted">
        恢复、升级和回滚由宿主本地 personal-botctl
        执行。恢复旧备份会撤销会话、暂停排程，并将未完成任务转入核对；记忆和删除状态也回到备份时点。
      </div>
    </>
  );
}
function Budgets({ active }: { active: boolean }) {
  const settings = useRemote("/api/settings", settingsSchema, active);
  const [saved, setSaved] = useState(false);
  useEffect(() => setSaved(false), []);
  return (
    <>
      <h2>任务默认限额</h2>
      <p>这些值用于以后创建的任务。当前任务请在任务详情单独调整。</p>
      <ErrorNotice error={settings.error} />
      {saved && <Notice>默认限额已保存。现有任务累计用量保持不变。</Notice>}
      {settings.data && (
        <section className="panel">
          <BudgetEditor
            path="/api/settings/task-budgets"
            defaults={settings.data.budgets}
            onSaved={() => {
              setSaved(true);
              void settings.reload(true);
            }}
          />
        </section>
      )}
      <div className="notice notice-muted">
        <div>
          <strong>严格预算边界</strong>
          <p>
            平台按已知用量和请求上界限制后续调用；用量缺失时标记估算或未知。此限额不是厂商账单保证，已发出的请求仍可能计费。请另行核对厂商超额后付费、积分包抵扣和其他客户端消耗。
          </p>
        </div>
      </div>
    </>
  );
}
