# Dream Bot public source

Read README.md, docs/ARCHITECTURE.md and docs/CONTRACT.md before changes.

Never commit keys, passwords, sessions, runtime state, deployment inventories, raw diagnostics or personal screenshots. Use synthetic test fixtures and reserved example domains. No real provider call is authorized by ordinary tests.

Maintain separate API/Worker/browser/executor identities, actual SO_PEERCRED checks, API-only secrets and SQLite, Chromium sandbox, Landlock/seccomp and the fixed host template. No unrestricted fallback, privileged container, SYS_ADMIN or unconfined policy.

Automatic reads, event subscriptions and read-only viewing must not wake or keep the runtime alive. Preserve one temporary container, 300-second safe idle deletion and independent persistent data.

Use strict TypeScript without blanket any/ts-ignore. Keep model calls server-side. Preserve durable request IDs, task revisions, approval/epoch boundaries and unknown external-effect reconciliation. Run the relevant source suites and describe real/native/provider evidence separately. Do not label ARM64, fresh installation, provider compatibility or complete fault/resource acceptance as verified without evidence.
