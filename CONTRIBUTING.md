# Contributing

All first-party contributions are submitted under the project's MIT license. Keep changes focused, describe the behavior and evidence, and disclose which checks require native Linux or an actual provider. Use synthetic fixtures; never submit personal data or credentials.

## Source map

| Path | Purpose |
| --- | --- |
| `host/` | Go gateway, auth, lifecycle, restricted helper, backups and local CLI |
| `runtime/personal_bot/` | API, LangGraph worker, protocol adapters, brokers and sandbox |
| `runtime/tests/` | Deterministic regressions and opt-in isolated native probes |
| `web/` | React/TypeScript conversation interface and browser/file work panels |
| `deploy/` | Container, process launchers, systemd and fixed host installer |
| `docs/` | Architecture, public contracts, installation and translations |

## Checks

```sh
make test-host
make test-web
python3 -m venv runtime/.venv
runtime/.venv/bin/python -m pip install -e 'runtime[test]'
make test-runtime PYTHON=runtime/.venv/bin/python
```

Use Node 24 LTS and Go 1.26+; Python requires 3.12+. Frontend `npm ci` installs the committed lockfile. Go module sums are committed. Python ranges currently need an explicit resolved inventory for reproducible downstream deployments. Production needs SQLite 3.51.3+ with FTS5; the Dockerfile builds a checked upstream release. A distribution's older default SQLite is not enough for production.

The Windows/macOS unit suite cannot establish Linux peer-credential, namespace, AppArmor, Landlock or seccomp behavior. Real-browser cases are explicitly opt-in. The engineering probes can mutate isolated acceptance state; read their module headers and do not run them on an owner's instance. No CI job should use a real model key or authorize paid calls.

Do not weaken security controls to make a check pass. Keep passive reads passive, stable idempotent request IDs, unknown-effect reconciliation and explicit billing/approval boundaries. Update [contracts](docs/CONTRACT.md) for API changes and preserve legacy behavior unless a deliberate migration is documented.

For translations, keep the alpha status and evidence limitations, and do not imply the UI has localization that it does not implement. Architecture changes should update both `docs/ARCHITECTURE.md` and the checked-in SVG.
