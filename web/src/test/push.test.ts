import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

type PushEvent = {
  data: { json: () => unknown };
  waitUntil: (promise: Promise<unknown>) => void;
};

function worker() {
  const handlers = new Map<string, (event: PushEvent) => void>();
  const showNotification = vi.fn().mockResolvedValue(undefined);
  runInNewContext(
    readFileSync(resolve("public", "sw.js"), "utf8"),
    {
      self: {
        addEventListener: (name: string, handler: (event: PushEvent) => void) =>
          handlers.set(name, handler),
        registration: { showNotification },
      },
    },
  );
  async function push(payload: unknown, malformed = false) {
    let work: Promise<unknown> | undefined;
    handlers.get("push")?.({
      data: {
        json: () => {
          if (malformed) throw new Error("invalid JSON");
          return payload;
        },
      },
      waitUntil: (promise) => {
        work = promise;
      },
    });
    await work;
  }
  return { showNotification, push };
}

it("deduplicates retry notifications by stable id while keeping different notifications distinct and private", async () => {
  const { showNotification, push } = worker();
  await push({
    notification_id: "notice-1",
    title: "private title",
    body: "private task",
    url: "https://other.test",
  });
  await push({ notification_id: "notice-1" });
  await push({ notification_id: "notice-2" });
  expect(showNotification.mock.calls[0]).toEqual(
    showNotification.mock.calls[1],
  );
  expect(showNotification.mock.calls[0][1]).toMatchObject({
    tag: "personal-bot-notice-1",
    data: { url: "/#tasks" },
  });
  expect(showNotification.mock.calls[2][1].tag).toBe("personal-bot-notice-2");
  expect(JSON.stringify(showNotification.mock.calls)).not.toContain(
    "private task",
  );
  expect(JSON.stringify(showNotification.mock.calls)).not.toContain(
    "https://other.test",
  );
});

it("still displays generic notifications for malformed or missing identifiers without collapsing unrelated notices", async () => {
  const { showNotification, push } = worker();
  await push(null, true);
  await push({ notification_id: "unsafe/id" });
  for (const call of showNotification.mock.calls) {
    expect(call[1]).not.toHaveProperty("tag");
    expect(call[1].data.url).toBe("/#tasks");
  }
});
