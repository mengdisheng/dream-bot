import { z } from "zod";

export const ownerSchema = z.object({ id: z.string(), username: z.string() });
export const authSchema = z.object({
  initialized: z.boolean(),
  authenticated: z.boolean(),
  owner: ownerSchema.nullable(),
  csrf_token: z.string().optional(),
});
export const loginSchema = z.object({
  owner: ownerSchema,
  csrf_token: z.string(),
  recovery_codes: z.array(z.string()).optional(),
});
export const enrollmentSchema = z.object({
  enrollment_id: z.string(),
  totp_uri: z.string(),
  totp_secret: z.string(),
});
export const runtimeSchema = z.object({
  state: z.string(),
  phase: z.string(),
  runtime_id: z.string().nullable(),
  elapsed_seconds: z.number(),
  server_time: z.string().optional(),
  idle_since: z.string().nullable().optional(),
  idle_deadline: z.string().nullable().optional(),
  idle_blockers: z.array(z.string()).default([]),
  last_removed_at: z.string().nullable().optional(),
  last_removal_reason: z.string().nullable().optional(),
  safety_suspended: z.boolean().default(false),
  suspension_reason: z.string().nullable().optional(),
  error: z.string().nullable(),
  diagnostic_id: z.string().nullable(),
});
export const statusSchema = z.enum([
  "queued",
  "running",
  "paused",
  "waiting_approval",
  "waiting_human",
  "waiting_model",
  "needs_review",
  "succeeded",
  "failed",
  "cancelled",
]);
export const taskSchema = z.object({
  id: z.string(),
  conversation_id: z.string().nullable().optional(),
  title: z.string(),
  status: statusSchema,
  revision: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
  connection_id: z.string().nullable(),
  model_id: z.string().nullable(),
  summary: z.string().nullable().optional(),
  wait_reason: z.string().nullable().optional(),
  result: z.string().nullable().optional(),
  tool_count: z.number(),
  token_count: z.number(),
  max_tools: z.number(),
  max_tokens: z.number(),
  max_active_seconds: z.number(),
  active_seconds: z.number(),
  token_usage_estimated: z.boolean(),
  unresolved_effect_count: z.number().default(0),
  cancel_requested: z.boolean().default(false),
  pause_requested: z.boolean().default(false),
  execution_mode: z.enum(["chat", "agent"]).default("agent"),
});
export const taskSummarySchema = taskSchema.pick({
  id: true,
  title: true,
  status: true,
  revision: true,
  created_at: true,
  updated_at: true,
  execution_mode: true,
  wait_reason: true,
  tool_count: true,
  token_count: true,
  active_seconds: true,
  unresolved_effect_count: true,
  cancel_requested: true,
  conversation_id: true,
}).extend({ token_usage_estimated: z.boolean().optional() });
export const taskPageSchema = z.object({
  items: z.array(taskSummarySchema),
  next_cursor: z.string().nullable(),
  has_more: z.boolean(),
});
export const receiptSchema = z.object({
  accepted: z.literal(true),
  request_id: z.string(),
  task_id: z.string(),
  task_revision: z.number(),
  conversation_id: z.string().optional(),
});
export const pendingSchema = z.object({
  client_request_id: z.string(),
  text: z.string(),
  mode: z.enum(["new", "supplement"]),
  task_id: z.string().optional(),
  conversation_id: z.string().optional(),
  file_ids: z.array(z.string()),
  execution_mode: z.enum(["chat", "agent"]).default("agent"),
});
export const messageSchema = z.object({
  id: z.string(),
  task_id: z.string(),
  role: z.string(),
  content: z.string(),
  created_at: z.string(),
});
export const messagePageSchema = z.object({
  items: z.array(messageSchema),
  next_before: z.string().nullable(),
  has_more: z.boolean(),
});
export const conversationSchema = z.object({
  id: z.string(),
  title: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  latest_task_id: z.string().nullable(),
  latest_task_status: statusSchema.nullable(),
});
export const conversationPageSchema = z.object({
  items: z.array(conversationSchema),
  next_cursor: z.string().nullable(),
  has_more: z.boolean(),
});
export const conversationDetailSchema = conversationSchema.extend({
  tasks: z.array(taskSummarySchema),
  next_before: z.string().nullable(),
  has_more: z.boolean(),
});
export const eventSchema = z.object({
  seq: z.number(),
  task_id: z.string(),
  kind: z.string(),
  data: z.record(z.string(), z.unknown()),
  created_at: z.string(),
});
export const eventPageSchema = z.object({
  items: z.array(eventSchema),
  next_after: z.number().int().nonnegative(),
  has_more: z.boolean(),
});
export const approvalSchema = z.object({
  id: z.string(),
  task_id: z.string(),
  task_revision: z.number(),
  status: z.string(),
  action: z.object({
    name: z.string(),
    arguments: z.record(z.string(), z.unknown()),
    account: z.string().nullable().optional(),
    site: z.string().nullable().optional(),
    file_versions: z.unknown().optional(),
  }),
  binding_hash: z.string().optional(),
  created_at: z.string(),
  decided_at: z.string().nullable().optional(),
  revision: z.number().optional(),
});
export const toolSchema = z.object({
  id: z.string(),
  name: z.string(),
  arguments: z.record(z.string(), z.unknown()),
  status: z.string(),
  revision: z.number(),
  task_id: z.string(),
  task_revision: z.number(),
  result: z.unknown().optional(),
  error: z.unknown().optional(),
  approval_id: z.string().nullable().optional(),
  created_at: z.string(),
  dispatched_at: z.string().nullable().optional(),
  finished_at: z.string().nullable().optional(),
});
export const checkSchema = z.enum(["passed", "failed", "not_tested"]);
export const checksSchema = z.object({
  auth: checkSchema.default("not_tested"),
  text: checkSchema.default("not_tested"),
  stream: checkSchema.default("not_tested"),
  tools: checkSchema.default("not_tested"),
  vision: checkSchema.default("not_tested"),
});
export const entitlementSchema = z.object({
  status: z.enum([
    "documented_supported",
    "documented_restricted",
    "needs_confirmation",
    "not_applicable",
  ]),
  source_url: z.string().nullable().optional(),
  checked_at: z.string().nullable().optional(),
  quote: z.string().nullable().optional(),
});
export const overageApprovalSchema = z.object({
  active: z.boolean(),
  can_create_task: z.boolean(),
  kind: z.literal("purchased_credits").nullable(),
  reason: z.string().nullable(),
  expires_at: z.string().nullable(),
  max_tasks: z.number().int().nonnegative(),
  tasks_used: z.number().int().nonnegative(),
  remaining_tasks: z.number().int().nonnegative(),
  budgets: z
    .object({
      max_tools: z.number().int().positive(),
      max_tokens: z.number().int().positive(),
      max_active_seconds: z.number().int().positive(),
    })
    .nullable(),
});
export const connectionSchema = z.object({
  id: z.string(),
  display_name: z.string(),
  provider: z.string(),
  region: z.string(),
  billing_mode: z.string(),
  protocol: z.string(),
  base_url: z.string(),
  model_id: z.string(),
  capabilities: checksSchema,
  revision: z.number(),
  enabled: z.boolean(),
  entitlements: z.record(z.string(), entitlementSchema).default({}),
  provider_overage_state: z.string().default("unknown"),
  provider_overage_kind: z.string().nullable().optional(),
  overage_approval: overageApprovalSchema.optional(),
  account_id: z.string().nullable().optional(),
  credential_id: z.string().nullable().optional(),
  billing_entry_id: z.string().nullable().optional(),
  quota_bindings: z
    .array(
      z.object({
        quota_group_id: z.string(),
        unit: z.string(),
        model_ids: z.array(z.string()).default([]),
      }),
    )
    .default([]),
  validated_at: z.string().nullable().optional(),
});
export const presetSchema = z.object({
  id: z.string(),
  name: z.string(),
  billing_modes: z.array(z.string()),
  protocols: z.array(z.string()),
  base_urls: z.union([z.array(z.string()), z.record(z.string(), z.string())]),
  notes: z.union([z.string(), z.array(z.string())]),
  plan_entitlements: z.record(z.string(), entitlementSchema).optional(),
});
export const validationSchema = z.object({
  checks: checksSchema,
  errors: z.array(z.unknown()),
  validated_at: z.string(),
});
export const budgetsSchema = z.object({
  max_tools: z.number(),
  max_tokens: z.number(),
  max_active_seconds: z.number(),
});
export const settingsSchema = z.object({
  default_chat_connection: z.string().nullable(),
  default_agent_connection: z.string().nullable(),
  budgets: budgetsSchema,
  timezone: z.string(),
  backup_enabled: z.boolean().optional(),
  strict_budget_mode: z.boolean().optional(),
});
export const fileSchema = z.object({
  id: z.string(),
  name: z.string(),
  media_type: z.string(),
  size: z.number(),
  status: z.string(),
  version: z.number(),
  task_id: z.string().nullable().optional(),
  created_at: z.string(),
  deleted_at: z.string().nullable().optional(),
});
export const memorySchema = z.object({
  id: z.string(),
  content: z.string(),
  status: z.string(),
  source: z.unknown(),
  revision: z.number(),
  created_at: z.string(),
});
export const scheduleSchema = z.object({
  id: z.string(),
  title: z.string(),
  prompt: z.string(),
  cron: z.string(),
  timezone: z.string(),
  enabled: z.boolean(),
  revision: z.number(),
  registration_status: z.string(),
  next_fire_at: z.string().nullable(),
});
export const browserSchema = z.object({
  available: z.boolean(),
  running: z.boolean(),
  display_running: z.boolean().optional(),
  viewer_count: z.number().int().nonnegative().optional(),
  mode: z.enum(["idle", "agent", "human"]),
  browser_epoch: z.number(),
  device_id: z.string().nullable(),
  task_id: z.string().nullable(),
  human_idle_deadline: z.string().nullable(),
  width: z.number(),
  height: z.number(),
  url: z.string().nullable(),
  error: z.string().nullable().optional(),
});
export const viewerSchema = z.object({
  view_session_id: z.string(),
  runtime_id: z.string(),
  browser_epoch: z.number(),
  ws_path: z.string(),
  expires_at: z.string(),
  width: z.number(),
  height: z.number(),
});
export const sessionSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  last_seen_at: z.string(),
  expires_at: z.string(),
  user_agent: z.string(),
  current: z.boolean(),
});
export const notificationSchema = z.object({
  id: z.string(),
  task_id: z.string(),
  category: z.string(),
  created_at: z.string(),
  read_at: z.string().nullable(),
});
export const listOf = <T extends z.ZodType>(schema: T) =>
  z.object({ items: z.array(schema) });
export const unknownObject = z.record(z.string(), z.unknown());
export type Owner = z.infer<typeof ownerSchema>;
export type Runtime = z.infer<typeof runtimeSchema>;
export type Task = z.infer<typeof taskSchema>;
export type TaskSummary = z.infer<typeof taskSummarySchema>;
export type Conversation = z.infer<typeof conversationSchema>;
export type TaskEvent = z.infer<typeof eventSchema>;
export type Pending = z.infer<typeof pendingSchema>;
export type Connection = z.infer<typeof connectionSchema>;
export type Approval = z.infer<typeof approvalSchema>;
export type BrowserStatus = z.infer<typeof browserSchema>;

export const statusLabels: Record<z.infer<typeof statusSchema>, string> = {
  queued: "排队中",
  running: "执行中",
  paused: "已暂停",
  waiting_approval: "等待审批",
  waiting_human: "等待人工",
  waiting_model: "等待模型",
  needs_review: "待核对",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
export function hasActiveOverageApproval(
  connection: Connection,
  now = Date.now(),
): boolean {
  const approval = connection.overage_approval;
  if (
    connection.provider_overage_state !== "enabled" ||
    connection.provider_overage_kind !== "purchased_credits" ||
    !approval?.active ||
    approval.reason !== null ||
    approval.kind !== "purchased_credits" ||
    !approval.budgets ||
    !approval.expires_at
  ) {
    return false;
  }
  const expiry = Date.parse(approval.expires_at);
  return Number.isFinite(expiry) && expiry > now;
}
function canStartWithBilling(connection: Connection, now: number): boolean {
  return (
    connection.billing_mode === "api" ||
    connection.provider_overage_state === "confirmed_disabled" ||
    (hasActiveOverageApproval(connection, now) &&
      connection.overage_approval?.can_create_task === true &&
      connection.overage_approval.remaining_tasks > 0)
  );
}
export function canUseAgent(
  connection: Connection,
  now = Date.now(),
): boolean {
  const scopes = ["backend", "agent"];
  return (
    connection.enabled &&
    canStartWithBilling(connection, now) &&
    connection.capabilities.tools === "passed" &&
    connection.capabilities.text === "passed" &&
    scopes.every((scope) =>
      ["documented_supported", "not_applicable"].includes(
        connection.entitlements[scope]?.status ?? "needs_confirmation",
      ),
    )
  );
}
export function canUseChat(
  connection: Connection,
  now = Date.now(),
): boolean {
  return (
    connection.enabled &&
    canStartWithBilling(connection, now) &&
    connection.capabilities.text === "passed" &&
    ["backend", "chat"].every((scope) =>
      ["documented_supported", "not_applicable"].includes(
        connection.entitlements[scope]?.status ?? "needs_confirmation",
      ),
    )
  );
}
