# Conversation workspace

React + strict TypeScript + Vite. All application traffic uses authenticated, same-origin `/api` routes. Model calls stay server-side. Fonts are bundled; no remote font CDN or analytics service is required.

```sh
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

The primary navigation is new/recent conversations, files and settings. Browser viewing/takeover and safe file preview open in the conversation's work panel. Advanced settings retain schedules, budgets, backups and diagnostics. Required external approvals, billing authorization and unresolved-effect reconciliation stay visible when relevant.

Passive summary/message cursor pages, coalesced event invalidation and SSE resume keep global event-sequence gaps and stale-response races distinct. Text deltas update display without triggering full message fetches. Stable request IDs survive uncertain delivery; reconcile before retrying a task submission.

The noVNC client uses explicit short-lived viewer tickets bound to the expected task. Client `viewOnly` is only an interaction hint: server-side per-frame owner/session/task/device/epoch checks are authoritative. No clipboard/file-transfer bridge or automatic reconnect is enabled. Release hands control back but keeps the task paused until explicit continuation.

The current UI is primarily Chinese. The multi-language README files translate documentation, not application strings. Unit tests use deterministic HTTP/RFB substitutes and do not establish provider compatibility or real sandbox behavior. See [status](../docs/STATUS.md) for the separate native/real-browser evidence boundaries.

Third-party license files in `public/third-party-licenses/` are included in build output. noVNC remains MPL-2.0; fonts retain their supplied OFL licenses; first-party code is MIT.
