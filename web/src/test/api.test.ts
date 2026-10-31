import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { api, ApiError, safeLink, setCsrf } from "../api";
import {
  receiptSchema,
  canUseAgent,
  canUseChat,
  connectionSchema,
  hasActiveOverageApproval,
} from "../types";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf("");
});
describe("same-origin API boundary", () => {
  it("includes cookies, CSRF on mutations, and never adds passive semantics to mutations", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          accepted: true,
          request_id: "r1",
          task_id: "t1",
          task_revision: 1,
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    setCsrf("csrf-test");
    await api("/api/requests", receiptSchema, {
      method: "POST",
      body: { text: "a" },
      passive: true,
    });
    const options = fetcher.mock.calls[0][1] as RequestInit;
    expect(options.credentials).toBe("same-origin");
    expect(options.cache).toBe("no-store");
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-test");
    expect(new Headers(options.headers).has("X-Personal-Bot-Passive")).toBe(
      false,
    );
  });
  it("marks passive reads and preserves sleeping as a distinct result", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "runtime_sleeping",
            message: "sleeping",
            retryable: true,
          },
        }),
        { status: 503 },
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    await expect(
      api("/api/tasks", z.unknown(), { passive: true }),
    ).rejects.toMatchObject({ code: "runtime_sleeping", sleeping: true });
    expect(
      new Headers((fetcher.mock.calls[0][1] as RequestInit).headers).get(
        "X-Personal-Bot-Passive",
      ),
    ).toBe("1");
  });
  it("never treats an invalid successful response as accepted", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ accepted: true }), { status: 200 }),
        ),
    );
    await expect(
      api("/api/requests", receiptSchema, { method: "POST" }),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("does not expose an upstream HTML body as a user-visible error", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>private-upstream-token</html>", { status: 502 }),
        ),
    );
    try {
      await api("/api/tasks", z.unknown());
      throw new Error("expected rejection");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as Error).message).not.toContain("private-upstream-token");
    }
  });
  it("rejects external API paths and executable links", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(
      api("https://other.test/api", z.unknown()),
    ).rejects.toMatchObject({ code: "invalid_path" });
    expect(fetcher).not.toHaveBeenCalled();
    expect(safeLink("javascript:alert(1)")).toBeUndefined();
    expect(safeLink("https://example.com/a")).toBe("https://example.com/a");
  });
});
describe("agent selection eligibility", () => {
  const base = {
    id: "c1",
    display_name: "test",
    provider: "custom",
    region: "cn",
    billing_mode: "api",
    protocol: "chat_completions",
    base_url: "https://api.example.test/v1",
    model_id: "test-model",
    capabilities: {
      auth: "passed",
      text: "passed",
      stream: "passed",
      tools: "passed",
      vision: "not_tested",
    },
    revision: 1,
    enabled: true,
    entitlements: {
      backend: { status: "documented_supported" },
      agent: { status: "documented_supported" },
      chat: { status: "documented_supported" },
    },
  };
  it("does not mistake successful tool tests for permitted backend usage", () => {
    const good = connectionSchema.parse(base);
    expect(canUseAgent(good)).toBe(true);
    expect(
      canUseAgent({
        ...good,
        entitlements: {
          ...good.entitlements,
          backend: { status: "documented_restricted" },
        },
      }),
    ).toBe(false);
    expect(canUseAgent({ ...good, entitlements: {} })).toBe(false);
    expect(
      canUseAgent({
        ...good,
        capabilities: { ...good.capabilities, tools: "not_tested" },
      }),
    ).toBe(false);
  });
  it("gates plan roles on confirmed overage settings without blocking standard paid APIs", () => {
    const standard = connectionSchema.parse({
      ...base,
      provider_overage_state: "enabled",
    });
    expect(canUseAgent(standard)).toBe(true);
    expect(canUseChat(standard)).toBe(true);
    const plan = { ...standard, billing_mode: "token_plan" };
    expect(canUseAgent(plan)).toBe(false);
    expect(canUseChat(plan)).toBe(false);
    expect(
      canUseAgent({ ...plan, provider_overage_state: "confirmed_disabled" }),
    ).toBe(true);
    expect(
      canUseChat({ ...plan, provider_overage_state: "confirmed_disabled" }),
    ).toBe(true);
  });
  it("uses only live, unexpired purchased-credit permission for new interactive tasks", () => {
    const now = Date.parse("2026-10-31T10:00:00Z");
    const plan = connectionSchema.parse({
      ...base,
      billing_mode: "m_plan",
      provider_overage_state: "enabled",
      provider_overage_kind: "purchased_credits",
      overage_approval: {
        active: true,
        can_create_task: true,
        kind: "purchased_credits",
        reason: null,
        expires_at: "2026-10-31T10:30:00Z",
        max_tasks: 3,
        tasks_used: 1,
        remaining_tasks: 2,
        budgets: {
          max_tools: 8,
          max_tokens: 100000,
          max_active_seconds: 300,
        },
      },
    });
    expect(canUseAgent(plan, now)).toBe(true);
    expect(canUseChat(plan, now)).toBe(true);
    expect(canUseAgent(plan, Date.parse("2026-10-31T10:30:00Z"))).toBe(false);
    const approval = plan.overage_approval!;
    for (const change of [
      { active: false },
      { can_create_task: false },
      { expires_at: "invalid" },
      { expires_at: null },
      { reason: "revoked" },
      { budgets: null },
    ]) {
      const changed = {
        ...plan,
        overage_approval: { ...approval, ...change },
      };
      expect(canUseChat(changed, now)).toBe(false);
      expect(canUseAgent(changed, now)).toBe(false);
    }
    expect(
      canUseAgent({ ...plan, provider_overage_kind: "postpaid" }, now),
    ).toBe(false);
    expect(
      canUseChat({ ...plan, provider_overage_state: "unknown" }, now),
    ).toBe(false);
    expect(
      canUseAgent(
        { ...plan, capabilities: { ...plan.capabilities, tools: "not_tested" } },
        now,
      ),
    ).toBe(false);
    expect(
      canUseChat(
        {
          ...plan,
          entitlements: { ...plan.entitlements, backend: { status: "documented_restricted" } },
        },
        now,
      ),
    ).toBe(false);
  });
  it("keeps existing-task permission distinct from permission to create another task", () => {
    const now = Date.parse("2026-10-31T10:00:00Z");
    const plan = connectionSchema.parse({
      ...base,
      billing_mode: "m_plan",
      provider_overage_state: "enabled",
      provider_overage_kind: "purchased_credits",
      overage_approval: {
        active: true,
        can_create_task: false,
        kind: "purchased_credits",
        reason: null,
        expires_at: "2026-10-31T10:30:00Z",
        max_tasks: 3,
        tasks_used: 3,
        remaining_tasks: 0,
        budgets: { max_tools: 8, max_tokens: 100000, max_active_seconds: 300 },
      },
    });
    expect(hasActiveOverageApproval(plan, now)).toBe(true);
    expect(canUseChat(plan, now)).toBe(false);
    expect(canUseAgent(plan, now)).toBe(false);
  });
});
