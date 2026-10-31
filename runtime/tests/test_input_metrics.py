"""Input counts describe the existing payload; no supplier calls or trimming."""
import copy
import json

import httpx
import pytest

from personal_bot.models import Models, build_request, measure_input
from personal_bot.net import PublicHttp
from personal_bot.worker import TaskIPC


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_metrics_match_actual_utf8_payload_and_preserve_opaque_content(service, protocol):
    connection = service.connections.get(service.connections.bind()["id"], private=True)
    connection.update(protocol=protocol, provider="minimax", options={})
    opaque = {"type":"reasoning", "encrypted_content":"opaque供应商状态"}
    assistant = {"role":"assistant", "content":"", "reasoning_content":"完整推理",
                 "tool_calls":[{"id":"c1", "type":"function", "function":{"name":"echo", "arguments":"{}"}}]}
    messages = [{"role":"user", "content":"完整需求🌏"},
                {"role":"assistant", "content":"", "raw_output":[opaque] if protocol == "responses" else [assistant]},
                {"role":"tool", "tool_call_id":"c1", "content":"完整证据"}]
    tools = [{"type":"function", "function":{"name":"echo", "description":"中文工具", "parameters":{"type":"object"}}}]
    original = copy.deepcopy((messages, tools))
    metrics = measure_input(connection, messages, tools)
    captured = []

    def handle(request):
        captured.append(request)
        if protocol == "responses":
            event = {"type":"response.completed", "response":{"status":"completed", "output":[]}}
        else:
            event = {"choices":[{"delta":{"content":"OK"}, "finish_reason":"stop"}]}
        return httpx.Response(200, content="data: "+json.dumps(event)+"\n\ndata: [DONE]\n\n")

    models = Models(service.s, service.connections, PublicHttp(httpx.MockTransport(handle)))
    await models.generate(connection, messages, tools)
    body = json.loads(captured[0].content)
    assert metrics["request_json_utf8_bytes"] == len(captured[0].content)
    assert metrics["tool_schema_utf8_bytes"] > 0
    assert (messages, tools) == original
    assert "完整" not in json.dumps(metrics, ensure_ascii=False)
    if protocol == "responses":
        assert body["input"][1] == opaque
    else:
        assert body["messages"][2] == assistant
        assert body["stream_options"] == {"include_usage":True}
    assert build_request(connection, messages, tools, stream=True)[1] == body


async def test_input_counts_are_audited_once_per_attempt_not_per_cache_hit(service, monkeypatch):
    from personal_bot import models as module
    service.accept({"client_request_id":"metrics", "text":"私有需求", "mode":"new"})
    task = service.claim("runtime-test")
    sent = []

    def handle(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(503, json={"error":{"code":"server_is_overloaded"}}, headers={"retry-after":"0"})
        event = {"choices":[{"delta":{"content":"done"}, "finish_reason":"stop"}],
                 "usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}
        return httpx.Response(200, content="data: "+json.dumps(event)+"\n\ndata: [DONE]\n\n")

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    models = Models(service.s, service.connections, PublicHttp(httpx.MockTransport(handle)))
    args = (task["id"],task["attempt_id"],"runtime-test",task["revision"],"0",[{"role":"user","content":"私有需求"}],[])
    result = await models.task_call(*args)
    assert result["usage"]["total_tokens"] == 10
    assert await models.task_call(*args) == result
    rows = service.s.all("SELECT data FROM task_events WHERE task_id=? AND kind='model.input_measured' ORDER BY seq", (task["id"],))
    records = [json.loads(row["data"]) for row in rows]
    assert [record["attempt"] for record in records] == [0,1]
    assert len({record["model_call_id"] for record in records}) == 2
    assert len(sent) == 2
    assert all(record["request_json_utf8_bytes"] == len(request.content) for record, request in zip(records,sent))
    assert "私有需求" not in json.dumps(records,ensure_ascii=False)


async def test_first_tool_timing_ignores_halted_results_and_logs_no_body(capsys):
    class IPC:
        count = 0
        async def call(self,path,body):
            self.count += 1
            return {"halted":True} if self.count == 1 else {"result":{"private":"私有正文"}}

    import time
    ipc = TaskIPC(IPC(),"task-metrics",time.perf_counter())
    await ipc.call("tool",{"private":"私有正文"})
    assert capsys.readouterr().err == ""
    await ipc.call("tool",{"private":"私有正文"})
    await ipc.call("tool",{"private":"私有正文"})
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["stage"] == "first_tool_completed" and record["seconds"] >= 0
    assert record["task_id"] == "task-metrics"
    assert "私有正文" not in lines[0]


@pytest.mark.parametrize("error", [BrokenPipeError, ValueError])
def test_timing_sink_failure_cannot_fail_completed_work(monkeypatch, error):
    from personal_bot import telemetry
    class ClosedSink:
        def write(self, value):
            raise error("closed diagnostic stream")
        def flush(self):
            raise error("closed diagnostic stream")
    monkeypatch.setattr(telemetry.sys,"stderr",ClosedSink())
    telemetry.emit_timing("first_tool_completed",0.1,task_id="test")
