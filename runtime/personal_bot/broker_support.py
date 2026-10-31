"""Unix brokers authenticate the operating-system peer, never a supplied header."""
from __future__ import annotations

import os
import socket
import struct
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol


def attach_peer(protocol: Any, transport: Any) -> None:
    sock = transport.get_extra_info("socket")
    peer = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    original = protocol.app

    async def app(scope: dict, receive: Any, send: Any) -> None:
        scope = dict(scope)
        scope["personal_bot.peercred"] = {"pid": peer[0], "uid": peer[1], "gid": peer[2]}
        await original(scope, receive, send)

    protocol.app = app


class PeerHTTP(H11Protocol):
    def connection_made(self, transport: Any) -> None:
        attach_peer(self, transport)
        super().connection_made(transport)


class PeerWebSocket(WebSocketProtocol):
    def connection_made(self, transport: Any) -> None:
        attach_peer(self, transport)
        super().connection_made(transport)


class APIPeerOnly:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        peer = scope.get("personal_bot.peercred", {})
        allowed = int(os.environ.get("PERSONAL_BOT_API_UID", "31001"))
        if peer.get("uid") != allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"peer_denied"}'})
            return
        await self.app(scope, receive, send)


def serve(app: Any, path: str, *, mode: int = 0o660) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_socket():
            raise RuntimeError("Broker path is not a socket")
        target.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(target))
    os.chmod(target, mode)
    os.chown(target, os.geteuid(), 31000)
    sock.listen(128)
    config = uvicorn.Config(APIPeerOnly(app), http=PeerHTTP, ws=PeerWebSocket,
                            access_log=False, log_level="warning", timeout_keep_alive=10)
    import asyncio
    asyncio.run(uvicorn.Server(config).serve(sockets=[sock]))
