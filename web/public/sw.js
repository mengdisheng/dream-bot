/* Only content-addressed public assets are cached. No API, HTML, or user data. */
const CACHE = "personal-bot-static-v1";
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter(
              (key) => key.startsWith("personal-bot-static-") && key !== CACHE,
            )
            .map((key) => caches.delete(key)),
        ),
      ),
  );
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (
    event.request.method !== "GET" ||
    url.origin !== self.location.origin ||
    !/^\/assets\/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?)$/.test(url.pathname)
  )
    return;
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const existing = await cache.match(event.request);
      if (existing) return existing;
      const response = await fetch(event.request);
      if (response.ok && response.type === "basic")
        await cache.put(event.request, response.clone());
      return response;
    }),
  );
});
self.addEventListener("push", (event) => {
  // Push content is deliberately generic; task details require authentication.
  let notificationId;
  try {
    const payload = event.data?.json();
    if (
      typeof payload?.notification_id === "string" &&
      /^[A-Za-z0-9_-]{1,128}$/.test(payload.notification_id)
    )
      notificationId = payload.notification_id;
  } catch {
    // An invalid payload still shows a generic notification.
  }
  event.waitUntil(
    self.registration.showNotification("梦 Bot 有新进展", {
      body: "任务状态已更新，请登录查看。",
      ...(notificationId ? { tag: `personal-bot-${notificationId}` } : {}),
      icon: "/icon.svg",
      data: { url: "/#tasks" },
    }),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow("/#tasks"));
});
