"""One API process, one Store, TCP and credential-checked Unix listeners."""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import uvicorn

from .api import app
from .auth import PeerCredentialProtocol
from .config import Config


async def main():
    config = Config.from_env()
    path = Path(config.worker_socket)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chown(path,-1,config.ipc_gid)
    os.chmod(path,0o660)
    sock.listen(128)
    sock.setblocking(False)
    public = uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=8091,http=PeerCredentialProtocol,lifespan="off",access_log=False,log_level="warning"))
    private = uvicorn.Server(uvicorn.Config(app,http=PeerCredentialProtocol,lifespan="off",access_log=False,log_level="warning"))
    async with app.router.lifespan_context(app):
        processes = [asyncio.create_task(public.serve()),asyncio.create_task(private.serve(sockets=[sock]))]
        while not any(task.done() for task in processes) and not app.state.fatal:
            await asyncio.sleep(1)
        public.should_exit = private.should_exit = True
        await asyncio.gather(*processes)
    sock.close()
    path.unlink(missing_ok=True)
    if app.state.fatal:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
