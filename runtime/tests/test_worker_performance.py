"""Worker startup and post-task ownership checks; no provider requests."""
import asyncio
import gc
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import weakref

import pytest

from personal_bot import worker


def test_idle_worker_does_not_import_langgraph():
    script = textwrap.dedent("""
        import asyncio
        import os
        import sys
        from personal_bot import worker

        class IdleReached(BaseException):
            pass

        class Client:
            async def aclose(self):
                self.closed = True

        class IPC:
            def __init__(self):
                self.client = Client()
                self.calls = 0

            async def call(self, path, body):
                assert path == 'claim'
                self.calls += 1
                if self.calls == 2:
                    raise IdleReached
                return {'task': None}

        class AsyncioProxy:
            def __getattr__(self, name):
                return getattr(asyncio, name)

            async def sleep(self, delay):
                assert delay == 1
                await asyncio.sleep(0)

        ipc = IPC()
        worker.IPC = lambda: ipc
        worker.asyncio = AsyncioProxy()
        os.environ['PERSONAL_BOT_RUNTIME_ID'] = 'idle-test'
        try:
            asyncio.run(worker.worker_main())
        except IdleReached:
            pass
        else:
            raise AssertionError('worker did not poll while idle')
        assert ipc.client.closed
        assert 'personal_bot.agent_graph' not in sys.modules
        assert not any(name == 'langgraph' or name.startswith('langgraph.')
                       for name in sys.modules)
        assert os.environ['LANGSMITH_TRACING'] == 'false'
        assert os.environ['LANGCHAIN_TRACING_V2'] == 'false'
    """)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable,"-c",script],env=environment,
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr


def test_original_worker_graph_imports_remain_available():
    from personal_bot.agent_graph import Agent, AgentState, RemoteSaver
    from personal_bot.worker import Agent as OriginalAgent
    from personal_bot.worker import AgentState as OriginalState
    from personal_bot.worker import RemoteSaver as OriginalSaver

    assert (OriginalAgent,OriginalState,OriginalSaver) == (Agent,AgentState,RemoteSaver)
    with pytest.raises(AttributeError):
        getattr(worker,"missing_worker_symbol")


@pytest.mark.parametrize("outcome",[
    "succeeded", "halted", "ipc_error", "internal_error", "heartbeat_cancel",
])
async def test_completed_graph_and_large_state_are_released_before_idle(monkeypatch,outcome):
    from personal_bot import agent_graph

    references = []
    finishes = []
    heartbeats = []
    tool_calls = []
    tasks_to_run = 2

    class TrackedDict(dict):
        pass

    class IdleReached(BaseException):
        pass

    class ObservedAgent(agent_graph.Agent):
        def __init__(self, ipc, task):
            super().__init__(ipc,task)
            references.extend((weakref.ref(self),weakref.ref(self.graph)))

        async def run(self):
            state = TrackedDict(await super().run())
            # A completed asyncio.Task also owns its returned value. This
            # allocation catches retention even if Agent itself is released.
            state["large_test_state"] = bytearray(2*1024*1024)
            references.append(weakref.ref(state))
            return state

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    class FakeIPC:
        def __init__(self):
            self.client = Client()
            self.claimed = 0

        async def call(self, path, body):
            if path == "claim":
                # Check before creating another task, while the worker would
                # otherwise keep its last task's whole transcript resident.
                alive = [(index,type(reference()).__name__,
                          [type(owner).__name__ for owner in gc.get_referrers(reference())])
                         for index,reference in enumerate(references)
                         if reference() is not None]
                assert not alive, alive
                if self.claimed == tasks_to_run:
                    raise IdleReached
                self.claimed += 1
                task = TrackedDict(
                    id=f"task-{self.claimed}",attempt_id="attempt",runtime_id="runtime-test",
                    revision=1,context_generation=1,files=[],completed_tools=[],
                    messages=[{"role":"user","content":"x"*(512*1024)}],
                )
                claim = TrackedDict(task=task)
                references.extend((weakref.ref(task),weakref.ref(claim)))
                return claim
            if path == "checkpoints/get":
                return {"checkpoint":None}
            if path in {"checkpoints/put","checkpoints/writes"}:
                return {}
            if path == "model":
                if outcome == "heartbeat_cancel":
                    await asyncio.Event().wait()
                if outcome == "ipc_error":
                    raise worker.IPCError("synthetic_failure","synthetic failure")
                if outcome == "internal_error":
                    raise ValueError("private synthetic prompt must not escape")
                if outcome == "halted":
                    return {"halted":True}
                if body["step"] == "0":
                    call = {"id":"search", "name":"memory_search", "arguments":{"query":"fixture"}}
                    return {"text":"", "raw_output":{"role":"assistant","tool_calls":[call]},
                            "tool_calls":[call], "model_call_id":"model-step"}
                return {"text":"done","raw_output":{"role":"assistant","content":"done"},
                        "tool_calls":[],"model_call_id":"model-step"}
            if path == "tool":
                tool_calls.append((body["task_id"],body["name"]))
                return {"result":{"items":[]}}
            if path == "heartbeat":
                heartbeats.append(body["task_id"])
                raise worker.IPCError("stale_attempt","expired attempt")
            if path == "finish":
                # Retain only the small commit, not the graph return value.
                finishes.append(dict(body))
                return {}
            raise AssertionError(path)

    class AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio,name)

        async def sleep(self, delay):
            if delay == 0:
                await asyncio.sleep(0)
                return
            assert delay == 10
            if outcome == "heartbeat_cancel":
                await asyncio.sleep(0)
            else:
                await asyncio.Event().wait()

    ipc = FakeIPC()

    def reject_thread_offload(*args,**kwargs):
        raise AssertionError("async graph routing must not copy task context into a thread")

    monkeypatch.setattr(asyncio.get_running_loop(),"run_in_executor",reject_thread_offload)
    monkeypatch.setattr(agent_graph,"Agent",ObservedAgent)
    monkeypatch.setattr(worker,"IPC",lambda: ipc)
    monkeypatch.setattr(worker,"asyncio",AsyncioProxy())
    monkeypatch.setenv("PERSONAL_BOT_RUNTIME_ID","runtime-test")
    with pytest.raises(IdleReached):
        await worker.worker_main()
    assert ipc.client.closed
    assert ipc.claimed == tasks_to_run
    assert all(reference() is None for reference in references)
    if outcome == "succeeded":
        assert [(item["status"],item["result"]) for item in finishes] == [("succeeded","done")]*tasks_to_run
        assert len(tool_calls) == tasks_to_run
    elif outcome in {"ipc_error","internal_error"}:
        assert [item["status"] for item in finishes] == ["failed"]*tasks_to_run
        assert all("private synthetic prompt" not in item["result"] for item in finishes)
    else:
        assert not finishes
    if outcome == "heartbeat_cancel":
        assert len(heartbeats) == tasks_to_run
