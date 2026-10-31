# Install on Linux amd64

This is an experimental source installation. The running amd64 implementation has been exercised, but the public guide still needs an independent clean-machine acceptance run. Read the commands before using them on a server with other workloads. This guide does not require any credential from the project's author.

## Prerequisites

- A maintained Linux amd64 host with systemd and cgroup v2, Docker Engine, AppArmor, kernel Landlock/seccomp and working unprivileged Chromium user namespaces. The tested host family is Ubuntu 24.04.
- At least 4 GB RAM for the compact profile, adequate disk space, and conservative headroom for other services. Source builds can need more memory than idle operation. Do heavy builds/backup maintenance with the project runtime stopped.
- Git, Make, Python 3.12+, Node.js 24 LTS (or another version allowed by `web/package.json`), npm, Go 1.26+ and OpenSSL. Install these through their official packages; do not change global security policies to get a test to pass.
- Outbound access to Go modules, npm, PyPI, Debian packages, official SQLite downloads, Playwright Chromium downloads and your selected model provider. A build/network failure is not permission to disable TLS verification.

ARM64 is not validated. The executor contains native x86_64 syscall rules, so changing `GOARCH` alone is insufficient.

## Build from source

Use `/opt/personal-bot/src` as the checkout because the current installer expects that deployment prefix. All initial commands below run on the target Linux host.

```sh
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /opt/personal-bot/src
git clone https://github.com/mengdisheng/dream-bot.git /opt/personal-bot/src
cd /opt/personal-bot/src
make build-host
make build-web
docker build --pull=false -t dream-bot:local -f deploy/Dockerfile .
```

The Dockerfile installs its declared open-source dependencies, downloads Chromium and builds a hash-verified SQLite library. It does not receive a model key, SSH credential or existing user database. Python dependency ranges are not a full reproducible lock; review and pin your resulting dependency inventory before operating a long-lived fork. The web lockfile and Go module sums are included.

If the host is already busy, perform the build on a separate compatible Linux amd64 builder and transfer verified artifacts. Do not mount unrelated credentials into the build context. The initial release publishes source, not a prebuilt container image.

## Install the fixed host template

The installer creates only the project's account, directories, network, service units and fixed configuration. Inspect `deploy/install-host.py`, `host/*.example.json`, `deploy/systemd/` and `deploy/seccomp-runtime.json` first. It refuses to replace configuration while the project gateway is active; upgrades require a separate maintenance procedure.

```sh
sudo install -d -m 0755 /opt/personal-bot/build
sudo install -m 0755 build/personal-bot-helper build/personal-bot-gateway \
  build/personal-botctl build/personal-bot-tls /opt/personal-bot/build/
IMAGE_ID="$(docker image inspect dream-bot:local --format '{{.Id}}')"
sudo python3 deploy/install-host.py --image "$IMAGE_ID"
sudo -u personal-bot-gateway /opt/personal-bot/bin/personal-botctl init
sudo systemctl enable --now personal-bot-helper.service personal-bot-gateway.service
sudo /opt/personal-bot/bin/personal-botctl doctor
sudo /opt/personal-bot/bin/personal-botctl status
```

`init` returns the **path** to the local one-time setup token, not a shared repository credential. Read that file privately on your own server when completing enrollment. The token is consumed after setup. There is no default owner username or password.

The generated limits are runtime 1 CPU / 2 GiB / 256 PIDs; project slice 1.5 CPU / 2432 MiB, with a 768-MiB conservative host reserve. The runtime's shared memory and scratch budget count toward its memory limit. Review capacity before changing limits. The helper verifies its immutable local image ID and fixed data roots; it does not accept arbitrary container templates from the web UI.

## SSH-tunnel HTTPS and enrollment

The shipped TLS listener binds to `127.0.0.1:8443`, with the internal gateway on `127.0.0.1:8090`. From your own computer, use your existing verified SSH host alias:

```sh
ssh -N -L 127.0.0.1:18443:127.0.0.1:8443 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o StrictHostKeyChecking=yes YOUR_VERIFIED_SSH_ALIAS
```

Windows users can instead run `deploy/start-ssh-test.ps1 -HostAlias YOUR_VERIFIED_SSH_ALIAS`.

Open `https://localhost:18443`. The installer generates a test certificate unless one exists. Verify the certificate fingerprint over the already verified SSH connection and configure trust for your own endpoint, or supply a correctly issued certificate. Do not disable certificate verification globally or expose the unauthenticated internal runtime port. `origin` in `/etc/personal-bot/host.json` must match the browser origin for cookies, CSRF and WebSockets.

Enroll with your local one-time token, choose a username and strong password, configure TOTP, and save recovery codes offline. Never post these values in an issue. Keys generated under `control/`, TLS private keys, owner cookies and browser profiles are private state.

## Configure a model

Use **Settings → Models → Add connection**. Select the provider, protocol, region, model and billing entry from the provider's current documentation. API keys stay server-side and are encrypted at rest. Saving makes no model request; explicit validation may consume credits. Read [MODELS.md](MODELS.md) before enabling a subscription connection or temporary purchased-credit authorization.

The primary UI lists conversations. The browser opens from the current conversation when that execution actually uses it. Files open in the adjacent work panel. Schedules, budgets, backups and diagnostics are under advanced settings.

## Operation and maintenance

```sh
sudo /opt/personal-bot/bin/personal-botctl status
sudo /opt/personal-bot/bin/personal-botctl suspend "planned maintenance"
# Inspect and complete maintenance; do not force-delete a busy runtime.
sudo /opt/personal-bot/bin/personal-botctl resume
```

The gateway can remain resident while no agent container exists. Active requests can create one; automatic reads cannot. Natural deletion requires 300 idle seconds and a safe checkpoint/outbox handoff. A durable memory safety suspension requires an administrator's review and explicit resume.

Configure a valid **public age recipient** for encrypted backups and keep its private identity offline. Backup and restore have authority-changing semantics; read `host/ctl.go` and the architecture document before using `backup`, `restore`, `upgrade` or `rollback`. Do not restore a previous database over newer user activity as part of an application-only rollback.

## Verification

```sh
make test-host
make test-web
python3 -m venv runtime/.venv
runtime/.venv/bin/python -m pip install -e 'runtime[test]'
make test-runtime PYTHON=runtime/.venv/bin/python
```

The ordinary runtime suite skips three opt-in real Chromium cases. Native peer credentials and sandbox probes need Linux and their specified UIDs. `runtime/tests/linux_browser_probe.py`, `linux_broker_probe.py` and `native_flow_probe.py` are **engineering acceptance probes for an isolated acceptance instance**, not production smoke tests: they can change test state and start browsers/tools. Read each module's instructions first. Mock provider results never prove real-provider compatibility.

If user namespaces, Landlock, seccomp, AppArmor, peer credentials or host memory admission fail, keep the runtime stopped and investigate. Do not use privileged containers, `SYS_ADMIN`, unconfined policies, `--no-sandbox` or an unrestricted execution fallback.
