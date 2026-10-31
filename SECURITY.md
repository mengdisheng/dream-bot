# Security policy

Dream Bot executes agent-selected tools and generated code on a personal server. It is currently an experimental, single-owner application. It is not a hosted multi-tenant isolation product and has not received an independent security audit.

Report vulnerabilities privately using [GitHub private vulnerability reporting](https://github.com/mengdisheng/dream-bot/security/advisories/new). If that feature is temporarily unavailable, open only a non-sensitive issue asking for a private reporting channel; do not include exploit details or user data there.

Never put passwords, TOTP seeds, recovery codes, model keys, owner cookies, SSH credentials, private browser profiles, databases, raw HAR files, diagnostic archives or personal screenshots in public issues, pull requests or CI logs. Use synthetic examples and placeholder domains. If a secret was published, revoke/rotate it first; deleting a Git commit does not make a leaked secret safe.

The API alone owns application secrets and SQLite. Worker, browser and executor identities are separate; actual Unix peer credentials are checked. The Docker helper is outside the temporary container and only accepts a fixed project template. Generated code requires Landlock and seccomp; Chromium keeps its native sandbox. Failed isolation must fail closed. Do not request a fix based on privileged mode, SYS_ADMIN, unconfined policies or `--no-sandbox`.

Browser cookies are owner-wide, including across conversations. A fresh tab removes previous page history but does not provide account separation. Human takeover and approvals bind session/task/device/epoch authority; task cancellation cannot undo an external action that was already sent.

Model calls leave your server for the configured provider; browser and research tools access external websites. Keep provider terms and costs separate from application permissions. Opt-in push and notifications need their own deployment review. Protect the host, persistent directories, TLS keys and offline backup recovery identities.

Before redistribution, scan the **tracked tree and history**, review dependencies and image layers, and preserve third-party licenses. CI does not need production credentials or any paid model key. Security fixes target the current public branch; no long-term support window is promised for the alpha release.
