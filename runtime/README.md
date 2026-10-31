# Agent runtime

Python 3.12+ / FastAPI / LangGraph / Playwright. The API is the sole online owner of SQLite, encrypted model credentials and file grants. Worker UID 31002 uses authenticated HTTP over a Unix socket; actual `SO_PEERCRED` is checked. Browser UID 31003 and executor UID 31004 have separate brokers. API UID is 31001, IPC GID 31000.

Production requires SQLite 3.51.3+ with FTS5, a runtime ID, deployment epoch and the gateway's Ed25519 public key. The Dockerfile builds the checked SQLite source. No model key belongs in the worker environment, source or image. Mount the host bridge directory rather than a replaceable socket inode.

Requests, task attempts, conversation membership, model steps, tool preparation/dispatch/results, approvals, reconciliation, checkpoints and outbox delivery are durable and distinct. An uncertain external effect must be reconciled; cancellation cannot undo an already dispatched side effect. Follow-ups use historical read-only evidence without inheriting old approvals or unrelated opaque model state.

Chat Completions and Responses have separate bounded adapters. Saving a model connection is non-billable; explicit validation and actual task dispatch can be billable. Entitlement, capability and supplier overage facts stay independent. No automatic provider or paid-endpoint fallback is implemented.

Run `python -m pytest -q` from this directory with the test extra installed, or use the root Makefile. Most model responses are deterministic substitutes. Three real Chromium fixtures are opt-in. Linux/UID/sandbox probes need a dedicated acceptance instance and must not be run against owner production state. Passing them is not a complete provider or fault/resource matrix.

See [architecture](../docs/ARCHITECTURE.md), [contracts](../docs/CONTRACT.md), [installation](../docs/INSTALL.md) and [current limitations](../docs/STATUS.md).
