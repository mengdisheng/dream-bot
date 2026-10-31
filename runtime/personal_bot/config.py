from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    app_dir: Path
    files_dir: Path
    runtime_id: str
    deployment_epoch: str
    public_key_path: Path
    worker_socket: str
    bridge_socket: str
    worker_uid: int
    ipc_gid: int
    production: bool = True

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            Path(os.getenv("PERSONAL_BOT_APP_DIR", "/data/app")),
            Path(os.getenv("PERSONAL_BOT_FILES_DIR", "/data/files")),
            os.getenv("PERSONAL_BOT_RUNTIME_ID", ""),
            os.getenv("PERSONAL_BOT_DEPLOYMENT_EPOCH", ""),
            Path(os.getenv("PERSONAL_BOT_GATEWAY_PUBLIC_KEY", "/run/personal-bot/gateway.pem")),
            os.getenv("PERSONAL_BOT_WORKER_SOCKET", "/run/personal-bot/worker.sock"),
            os.getenv("PERSONAL_BOT_HOST_BRIDGE", "/run/personal-bot/host/bridge.sock"),
            int(os.getenv("PERSONAL_BOT_WORKER_UID", "31002")),
            int(os.getenv("PERSONAL_BOT_IPC_GID", "31000")),
            os.getenv("PERSONAL_BOT_TESTING", "0") != "1",
        )
