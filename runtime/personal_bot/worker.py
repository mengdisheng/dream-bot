"""Separate unprivileged worker; load LangGraph only after claiming a task."""
from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import time

from .telemetry import emit_timing

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import httpx


def __getattr__(name):
    # Preserve the original imports used by recovery probes without making
    # the idle service load the graph implementation or its dependencies.
    if name in {"Agent", "AgentState", "RemoteSaver"}:
        from . import agent_graph
        return getattr(agent_graph,name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class IPCError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class IPC:
    def __init__(self, socket_path=None, transport=None):
        self.client = httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(uds=socket_path or os.getenv("PERSONAL_BOT_WORKER_SOCKET","/run/personal-bot/worker.sock")),base_url="http://api",timeout=600)

    async def call(self, path, body):
        response = await self.client.post("/worker/"+path,json=body)
        if response.status_code >= 400:
            payload = response.json().get("error",{})
            raise IPCError(payload.get("code","ipc_error"),payload.get("message","执行器请求失败"))
        return response.json()


class TaskIPC:
    """Measure the first completed tool without retaining task inputs globally."""
    def __init__(self, ipc: IPC, task_id: str, started: float):
        self.ipc, self.task_id, self.started = ipc, task_id, started
        self.first_tool_completed = False

    async def call(self, path, body):
        result = await self.ipc.call(path, body)
        if path == "tool" and not result.get("halted") and not self.first_tool_completed:
            self.first_tool_completed = True
            emit_timing("first_tool_completed", time.perf_counter()-self.started, task_id=self.task_id)
        return result


async def run_claimed(ipc: IPC, task: dict) -> None:
    """Own task-specific graph, result and heartbeat references until done."""
    started = time.perf_counter()
    from .agent_graph import Agent

    agent = Agent(TaskIPC(ipc,task["id"],started),task)
    emit_timing("task_initialized", time.perf_counter()-started, task_id=task["id"])
    running = asyncio.create_task(agent.run())

    async def heartbeat():
        while not running.done():
            await asyncio.sleep(10)
            try:
                await ipc.call("heartbeat",agent.envelope())
            except (httpx.HTTPError,IPCError):
                running.cancel()
                return

    pulse = asyncio.create_task(heartbeat())
    try:
        result = await running
        if not result.get("halted"):
            await ipc.call("finish",{**agent.envelope(),"status":"succeeded","result":result.get("result","")})
    except IPCError as exc:
        if exc.code not in ("stale_attempt","revision_conflict","runtime_draining"):
            await ipc.call("finish",{**agent.envelope(),"status":"failed","result":str(exc)})
    except asyncio.CancelledError:
        pass
    except Exception:
        # No private prompt, model state or traceback in shared logs.
        with contextlib.suppress(httpx.HTTPError,IPCError):
            await ipc.call("finish",{**agent.envelope(),"status":"failed","result":"执行器遇到内部错误，请查看诊断状态"})
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse


async def worker_main():
    runtime_id = os.environ["PERSONAL_BOT_RUNTIME_ID"]
    ipc = IPC()
    try:
        while True:
            try:
                claimed = await ipc.call("claim",{"runtime_id":runtime_id})
            except (httpx.HTTPError,IPCError):
                await asyncio.sleep(2)
                continue
            task = claimed.get("task")
            del claimed
            if task is None:
                await asyncio.sleep(1)
                continue
            try:
                await run_claimed(ipc,task)
            finally:
                del task
                # Graph nodes contain bound methods and form cycles. Collect
                # only at a task boundary. First let the loop retire callbacks
                # for the awaited execution/heartbeat Tasks and their results.
                await asyncio.sleep(0)
                gc.collect()
    finally:
        await ipc.client.aclose()


if __name__ == "__main__":
    asyncio.run(worker_main())
