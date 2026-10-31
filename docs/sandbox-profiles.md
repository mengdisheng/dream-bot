# Runtime sandbox profiles

`deploy/seccomp-runtime.json` is the outer Docker seccomp profile for the
native amd64 runtime. Its source is Microsoft's fixed Playwright v1.64.1 file:

https://raw.githubusercontent.com/microsoft/playwright/v1.64.1/utils/docker/seccomp_profile.json

The upstream download is 12,997 bytes. SHA256:
`cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
It was independently fetched over verified HTTPS and matched the deployment
download on 2026-10-31. Upstream licensing is Apache-2.0.

Three minimal rules are appended for native `amd64`:

- Allow `landlock_create_ruleset` (444), `landlock_add_rule` (445), and
  `landlock_restrict_self` (446), so the fresh executor can install filesystem
  restrictions before accepting untrusted code.
- Deny `clone3` explicitly with `SCMP_ACT_ERRNO`, `errnoRet: 38` (`ENOSYS`).
  On the actual Debian/glibc image, the upstream default `EPERM` caused thread
  creation to fail with "can't start new thread". Reporting `ENOSYS` lets glibc
  fall back to the existing `clone` rules. This does not allow `clone3`.
- Allow `chroot` (161) without the upstream outer-container capability
  condition. Actual Chromium startup reached `sys_chroot('/proc/self/fdinfo/')`
  and failed because its sandbox receives `CAP_SYS_CHROOT` inside its own user
  namespace, while the upstream profile checks the container's initial
  capabilities. Kernel capability checks still apply; the container receives
  no `CAP_SYS_CHROOT`. No `mount` rule is changed or added. The fresh executor's
  independent inner seccomp filter continues to reject `chroot` and namespace
  creation before any generated code runs.

Existing upstream rules and architecture mappings are unchanged; JSON
whitespace is normalized. No unrelated syscall permissions were added.
The resulting checked-in file SHA256 is
`f52d392ad153f5e8ec36bc501ebc4f4b30032f1a12abef08455efa9ede190b22`.

The default remains `SCMP_ACT_ERRNO`. Upstream already explicitly permits
`clone`, `setns`, and `unshare`, which Chromium needs to establish its own
unprivileged namespaces. The profile does not grant Linux capabilities.
Upstream capability-conditional syscall rules are retained, including its
`CAP_SYS_ADMIN` branch, but the fixed container template never grants
`SYS_ADMIN`, so that branch does not activate. Do not add that capability or
use `privileged`, `seccomp=unconfined`, `apparmor=unconfined`, or Chromium
`--no-sandbox` to make a failing test pass.

This outer policy permits trusted API, browser and executor startup. The fresh
executor then installs its separate native x86_64 seccomp allowlist and Landlock
rules; that inner filter rejects networking, exec/fork/clone, namespace changes,
ptrace, io_uring, unknown calls and compatibility-ABI attempts. The outer
Playwright architecture mappings alone are not an ABI restriction for generated
code. No ordinary execution fallback is permitted if inner isolation fails.

The helper must load the installed root-owned, non-writable profile as JSON and
pass its contents to the Docker API. A syscall resolver that cannot recognize
the Landlock names must fail startup; it must not silently discard the rule.
AppArmor is separately scoped to this project and must remain enabled; its
installation and actual kernel evidence are recorded by deployment operations.

Required native Linux evidence remains: successful Chromium startup with its
own renderer seccomp filter and user namespace; actual executor Landlock ABI,
filesystem/network/exec denials; UID/socket boundaries; no VNC/X11 TCP listener;
and real RFB viewing, control transfer and revocation. The policy file by itself
does not establish these checks. ARM64 requires separate native validation before support is advertised.
