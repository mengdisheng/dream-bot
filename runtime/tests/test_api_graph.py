import json
import os
import sys
import asyncio

import httpx
import pytest

from personal_bot.api import create_app
from personal_bot.auth import PeerCredentialProtocol
from personal_bot.net import PublicHttp
from personal_bot.store import dumps
from personal_bot.worker import Agent, IPC, IPCError
from conftest import assertion


class TestPeer:
    """Test harness only; production receives this from SO_PEERCRED protocol."""
    __test__ = False
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        scope.setdefault("extensions",{})["personal_bot.peercred"]=(1234,31002,31000)
        await self.app(scope,receive,send)


def mock_response(body,tools_called=False):
    if body.get("stream"):
        message = {"role":"assistant","content":"done"}
        finish = "stop"
        if not tools_called:
            message = {"role":"assistant","content":"","tool_calls":[{"index":0,"id":"write1","type":"function","function":{"name":"write_file","arguments":json.dumps({"name":"report.md","content":"verified artifact","media_type":"text/markdown"})}}]}
            finish = "tool_calls"
        return httpx.Response(200,content="data: "+json.dumps({"choices":[{"index":0,"delta":message,"finish_reason":finish}],"usage":{"total_tokens":20}})+"\n\ndata: [DONE]\n\n")
    return httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}]})


@pytest.mark.asyncio
async def test_real_langgraph_worker_remote_checkpoint_and_artifact(configuration,tmp_path,monkeypatch):
    config,private = configuration
    monkeypatch.setenv("PERSONAL_BOT_OUTPUT_ROOT",str(tmp_path/"outputs"))
    app=create_app(config)
    async with app.router.lifespan_context(app):
        conn=app.state.service.connections.save({"provider":"custom","protocol":"chat_completions","model_id":"mock","base_url":"https://model.example/v1","api_key":"isolated-test"})
        with app.state.store.tx() as db:
            db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(dumps({"text":"passed","tools":"passed"}),conn["id"]))
            app.state.store.setmeta("default_agent_connection",conn["id"],db)
        calls=[]
        def handle(request):
            body=json.loads(request.content);calls.append(body)
            return mock_response(body,any(m.get("role")=="tool" for m in body["messages"]))
        app.state.models.http=PublicHttp(httpx.MockTransport(handle))
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url="http://api") as client:
            request={"client_request_id":"api-e2e","text":"make report","mode":"new"}
            headers={"X-Personal-Bot-Assertion":assertion(private,"POST","/api/requests")}
            response=await client.post("/api/requests",json=request,headers=headers)
            assert response.status_code==200,response.text
            receipt=response.json()
            # A claimed process identity header must not grant Worker access.
            denied=await client.post("/worker/claim",json={"runtime_id":config.runtime_id},headers={"X-Worker-Uid":"31002"})
            assert denied.status_code==403
        ipc=IPC(transport=httpx.ASGITransport(app=TestPeer(app)))
        try:
            task=(await ipc.call("claim",{"runtime_id":config.runtime_id}))["task"]
            result=await Agent(ipc,task).run()
            assert result["result"]=="done"
            assert len(calls)==2
            await ipc.call("finish",{**Agent(ipc,task).envelope(),"status":"succeeded","result":result["result"]})
            assert app.state.service.task(receipt["task_id"])["status"]=="succeeded"
            assert app.state.store.one("SELECT COUNT(*) AS n FROM checkpoints")["n"]>=4
            assert app.state.store.one("SELECT COUNT(*) AS n FROM checkpoint_writes")["n"]>0
            artifact=app.state.store.one("SELECT * FROM files")
            assert artifact["name"]=="report.md"
            from pathlib import Path
            assert Path(artifact["path"]).read_text()=="verified artifact"
        finally:
            await ipc.client.aclose()


@pytest.mark.asyncio
async def test_signed_assertion_cannot_cross_path_role_or_epoch(configuration):
    config,private=configuration
    app=create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            for header in (assertion(private,"GET","/api/tasks"),assertion(private,"GET","/internal/activity",role="owner"),assertion(private,"GET","/internal/activity",role="control",deployment_epoch="old"),assertion(private,"GET","/internal/activity",role="control",runtime_id="old-runtime")):
                result=await client.get("/internal/activity",headers={"X-Personal-Bot-Assertion":header})
                assert result.status_code==401
            result=await client.get("/api/tasks",headers={"X-Personal-Bot-Assertion":assertion(private,"GET","/api/tasks")})
            assert result.status_code==200


@pytest.mark.asyncio
async def test_responses_full_tool_roundtrip_and_opaque_state(configuration,tmp_path,monkeypatch):
    monkeypatch.setenv("PERSONAL_BOT_OUTPUT_ROOT",str(tmp_path/"outputs"))
    app=create_app(configuration[0])
    async with app.router.lifespan_context(app):
        conn=app.state.service.connections.save({"provider":"custom","model_id":"responses-mock","protocol":"responses","base_url":"https://model.example/v1","api_key":"isolated-test"})
        with app.state.store.tx() as db:
            db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(dumps({"text":"passed","tools":"passed"}),conn["id"]))
            app.state.store.setmeta("default_agent_connection",conn["id"],db)
        requests=[]
        opaque={"type":"reasoning","id":"reasoning1","encrypted_content":"opaque-kept-locally"}
        def handle(request):
            body=json.loads(request.content);requests.append(body)
            if any(item.get("type")=="function_call_output" for item in body["input"]):
                assert opaque in body["input"]
                assert body["instructions"] and body["store"] is False
                output=[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Responses done"}]}]
            else:
                output=[opaque,{"type":"function_call","id":"fc1","call_id":"call1","name":"write_file","arguments":json.dumps({"name":"responses.txt","content":"artifact"})}]
            result={"type":"response.completed","response":{"status":"completed","output":output,"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}
            return httpx.Response(200,content="data: "+json.dumps(result)+"\n\n")
        app.state.models.http=PublicHttp(httpx.MockTransport(handle))
        app.state.service.accept({"client_request_id":"responses-task","text":"make artifact","mode":"new"})
        ipc=IPC(transport=httpx.ASGITransport(app=TestPeer(app)))
        try:
            task=(await ipc.call("claim",{"runtime_id":"runtime-test"}))["task"]
            result=await Agent(ipc,task).run()
            assert result["result"]=="Responses done" and len(requests)==2
            assert app.state.store.one("SELECT name FROM files")["name"]=="responses.txt"
            assert app.state.service.task(task["id"])["token_count"]==30
        finally:
            await ipc.client.aclose()


@pytest.mark.skipif(sys.platform!="linux",reason="Real Unix SO_PEERCRED requires Linux; Windows ASGI checks are not process-identity evidence")
@pytest.mark.asyncio
async def test_linux_actual_unix_socket_credentials(configuration,tmp_path):
    import dataclasses
    import uvicorn
    config=dataclasses.replace(configuration[0],worker_uid=os.getuid())
    app=create_app(config)
    socket_path=str(tmp_path/"actual-peer.sock")
    async with app.router.lifespan_context(app):
        server=uvicorn.Server(uvicorn.Config(app,uds=socket_path,http=PeerCredentialProtocol,lifespan="off",log_level="error"))
        process=asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:break
                await asyncio.sleep(.02)
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path),base_url="http://api") as client:
                response=await client.post("/worker/claim",json={"runtime_id":config.runtime_id})
                assert response.status_code==200,response.text
        finally:
            server.should_exit=True
            await process
