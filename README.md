# Dream Bot · 梦 Bot

**A self-hosted personal agent with a conversation workspace and a disposable runtime.**

[![License: MIT](https://img.shields.io/badge/first--party-MIT-285847.svg)](LICENSE)
[![Status: experimental alpha](https://img.shields.io/badge/status-experimental_alpha-68736B.svg)](docs/STATUS.md)
[![CI](https://github.com/mengdisheng/dream-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/mengdisheng/dream-bot/actions/workflows/ci.yml)

**English** · [简体中文](docs/i18n/README.zh-CN.md) · [繁體中文](docs/i18n/README.zh-TW.md) · [日本語](docs/i18n/README.ja.md) · [한국어](docs/i18n/README.ko.md) · [Español](docs/i18n/README.es.md) · [Français](docs/i18n/README.fr.md) · [Deutsch](docs/i18n/README.de.md) · [Português](docs/i18n/README.pt-BR.md) · [Русский](docs/i18n/README.ru.md) · [العربية](docs/i18n/README.ar.md) · [हिन्दी](docs/i18n/README.hi.md)

Dream Bot keeps conversations, files and browser profiles on your own server. It creates **at most one temporary Docker runtime** when work needs it, then **deletes that container after 300 idle seconds**. A small authenticated gateway stays available while the agent sleeps. Browser viewing and takeover happen **inside the conversation**.

All first-party frontend, backend, host management, sandbox, deployment code and tests are published here under MIT. There is no separate proprietary control plane or paid edition. Model services are configured separately and may charge for use; their terms and limits still apply. The current application UI is primarily Chinese; the translations above cover the documentation.

## What you can do

- Ask questions, continue a persistent conversation, and inspect each execution without losing the full audit history.
- Give the agent explicitly selected files; read common document formats and create downloadable artifacts.
- Open a real Chromium browser in the conversation panel, take control for login or verification, then explicitly hand it back.
- Connect OpenAI-compatible **Chat Completions** or **Responses** APIs, including configurable Chinese providers and eligible Token/Coding Plans. Saving a connection makes no model call; validation is a separate, potentially billable action.
- Review external actions before approval, reconcile uncertain effects, and pause or cancel further execution.
- Manage confirmed memories, schedules, devices, optional notifications and encrypted backups. Advanced settings stay out of the main conversation flow.

Protocol compatibility does not establish subscription entitlement. There is no automatic provider or billing-endpoint fallback. Each actual provider, model, region and plan needs its own validation. [Model setup](docs/MODELS.md)

## Architecture

![Dream Bot architecture](docs/assets/architecture.svg)

The host gateway owns authentication, scheduling and lifecycle management. The restricted helper alone talks to Docker. Inside the temporary container, the API, Worker, browser broker and executor use different Linux identities; model keys and SQLite belong only to the API. Generated code runs behind Landlock and an inner seccomp policy. Chromium keeps its own sandbox enabled.

[Detailed architecture, data flow and lifecycle diagrams →](docs/ARCHITECTURE.md)

## Stack

| Area | Languages and components |
| --- | --- |
| Web | TypeScript, React, Vite, CSS, Lucide, noVNC |
| Resident host | Go, SQLite, authenticated reverse proxy, restricted Docker helper |
| Agent runtime | Python 3.12+, FastAPI, LangGraph, Playwright |
| Persistence | SQLite/SQL, files, browser profile, age-encrypted backups |
| Deployment | Dockerfile, systemd, POSIX shell/Make, optional PowerShell SSH tunnel |

## Run it yourself

The current deployment target is **Linux amd64**, systemd and cgroup v2, Docker, AppArmor, unprivileged Chromium user namespaces, and kernel Landlock/seccomp support. Start with **4 GB RAM** and leave headroom for the host and other services. The compact template limits the runtime to 1 CPU / 2 GiB / 256 PIDs and the project slice to 1.5 CPU / 2432 MiB. ARM64 is not validated; unsupported sandbox capabilities fail closed.

```sh
git clone https://github.com/mengdisheng/dream-bot.git
cd dream-bot
# Read the installation guide before running root-level installation commands.
```

[Installation and SSH-tunnel HTTPS](docs/INSTALL.md) · [中文安装指南](docs/INSTALL.zh-CN.md) · [Development](CONTRIBUTING.md) · [API contracts](docs/CONTRACT.md)

There is **no default username, password or shared initialization secret**. Each installation generates its own one-time setup token; you choose the owner account, password and TOTP. Enter model keys only in your own settings page.

## Runtime policy

- Automatic reads, SSE catch-up and read-only browser viewing neither wake the runtime nor keep it alive.
- Agent browser use starts Chromium/Xvfb. x11vnc starts only after an authenticated viewer connects, and stops 30 seconds after the last viewer leaves.
- After 300 idle seconds, the runtime is deleted when no executable work, human takeover or unacknowledged control outbox blocks safe removal.
- Persistent data is independent of the container. Deletion/recreation is not a VM memory snapshot: live DOM, sockets and process memory are not restored.
- Switching conversations replaces the browser tab and its back history; the owner's browser profile/cookies remain shared. This is a **single-owner** application, not a multi-tenant security boundary.

## Project status

This is an **experimental source release**. The r19 implementation has native amd64, real browser/sandbox and web verification, but the full clean-install, provider, fault, resource, rollback and architecture matrix is still incomplete. MiniMax-driven autonomous browser end-to-end and ARM64 remain open. Unit tests use simulated model responses where indicated. See [validation boundaries and remaining work](docs/STATUS.md).

No production data, accounts, cookies, model keys, SSH credentials, private operation scripts, screenshots or raw deployment receipts are included.

## License and security

First-party code: [MIT](LICENSE), including commercial use and modification. Dependencies retain their licenses; see [third-party notices](THIRD_PARTY_NOTICES.md), especially noVNC's MPL-2.0 and the Apache-2.0-derived seccomp profile. No container image or bundled upstream binary is published with this initial source release.

Please report security issues privately through [GitHub's vulnerability reporting page](https://github.com/mengdisheng/dream-bot/security/advisories/new). Do not post keys, passwords, conversations or diagnostic archives in public issues. [Security policy](SECURITY.md)
