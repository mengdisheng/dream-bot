from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.requests import Request
from uvicorn.protocols.http.h11_impl import H11Protocol

from .config import Config
from .errors import BotError


def unb64(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("Invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_owner(request: Request, config: Config, role: str = "owner") -> dict:
    raw = request.headers.get("X-Personal-Bot-Assertion", "")
    try:
        payload, signature = raw.split(".")
        key = serialization.load_pem_public_key(config.public_key_path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("wrong key")
        key.verify(unb64(signature), payload.encode("ascii"))
        claims = json.loads(unb64(payload))
        if (claims.get("role") != role or claims.get("method") != ("GET" if request.scope["type"] == "websocket" else request.method)
                or claims.get("path") != request.url.path
                or claims.get("deployment_epoch") != config.deployment_epoch
                or not isinstance(claims.get("exp"), (int, float))
                or not time.time() < claims["exp"] <= time.time() + 65
                or not claims.get("sub") or not claims.get("request_id")):
            raise ValueError("invalid claims")
        if claims.get("runtime_id") is not None and claims["runtime_id"] != config.runtime_id:
            raise ValueError("stale runtime")
        if role == "control" and config.production and claims.get("runtime_id") != config.runtime_id:
            raise ValueError("control runtime identity required")
        return claims
    except Exception as exc:
        raise BotError("unauthorized", "身份凭据无效或已过期", 401) from exc


def verify_worker(request: Request, config: Config) -> None:
    # This value is injected from the accepted Unix socket, never from a header.
    peer = request.scope.get("extensions", {}).get("personal_bot.peercred")
    if not peer or peer[1] != config.worker_uid:
        raise BotError("worker_unauthorized", "执行器身份验证失败", 403)


class PeerCredentialProtocol(H11Protocol):
    def connection_made(self, transport) -> None:
        sock = transport.get_extra_info("socket")
        self._peercred = None
        if sock is not None and sock.family == socket.AF_UNIX and hasattr(socket, "SO_PEERCRED"):
            self._peercred = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        super().connection_made(transport)
        original = self.app
        peer = self._peercred

        async def authenticated_app(scope, receive, send):
            scope.setdefault("extensions", {})["personal_bot.peercred"] = peer
            await original(scope, receive, send)

        self.app = authenticated_app
