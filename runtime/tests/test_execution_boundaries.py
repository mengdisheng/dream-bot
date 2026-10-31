import asyncio
import io
import json
import time

import httpx
import pytest
from starlette.datastructures import UploadFile

from personal_bot.errors import BotError
from personal_bot.models import Models
from personal_bot.net import PublicHttp
from personal_bot.resources import Resources
from personal_bot.store import dumps
from personal_bot.tools import TOOLS, ToolRunner
from test_overage_approval import accept, approve, plan


class ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.waiting=asyncio.Event()

    async def acquire(self):
        self.waiting.set()
        return await super().acquire()


def response():
    return httpx.Response(200,content='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],"usage":{"total_tokens":10}}\n\ndata: [DONE]\n\n')


def body(task, name="memory_search", arguments=None):
    return {"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test",
            "revision":task["revision"],"model_call_id":"real-model-call","call_id":"tool-one",
            "name":name,"arguments":arguments or {"query":"test"}}


async def call(models,task):
    return await models.task_call(task["id"],task["attempt_id"],"runtime-test",task["revision"],"0",
                                  [{"role":"user","content":"small test"}],[])


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["revoke","expire","model","cancel"])
async def test_authorization_changed_while_waiting_for_quota_lock_sends_zero_requests(service,change):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    calls=[]
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(lambda request:(calls.append(request),response())[1])))
    lock=ObservedLock()
    await lock.acquire();lock.waiting.clear()
    models.group_locks["connection:"+connection["id"]]=lock
    running=asyncio.create_task(call(models,task))
    await asyncio.wait_for(lock.waiting.wait(),1)
    if change=="revoke":
        service.connections.revoke_overage(connection["id"])
    elif change=="expire":
        with service.s.tx() as db:
            key=service.connections.approval_key(connection["id"])
            value=json.loads(service.s.meta(key,db=db));value["expires_unix"]=time.time()-1
            service.s.setmeta(key,dumps(value),db)
    elif change=="model":
        with service.s.tx() as db:
            db.execute("UPDATE model_connections SET model_id='changed' WHERE id=?",(connection["id"],))
    else:
        service.command(task["id"],{"command_id":"cancel-queued-model","expected_revision":task["revision"],"action":"cancel"})
    lock.release()
    with pytest.raises(BotError):
        await running
    assert calls==[]
    receipt=service.s.one("SELECT status,usage FROM model_calls")
    assert receipt["status"]=="not_sent" and json.loads(receipt["usage"])["total_tokens"]==0
    assert service.task(task["id"])["token_count"]==0


@pytest.mark.asyncio
async def test_revocation_during_dns_is_checked_before_http_send(service,monkeypatch):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    stages=[]
    async def resolve(url):
        await asyncio.sleep(0)
        stages.append("dns")
        service.connections.revoke_overage(connection["id"])
        return "https://93.184.216.34/v1/chat/completions",{"Host":"example.org"},{"sni_hostname":"example.org"}
    async def send(client,request,**kwargs):
        stages.append("http-send")
        return response()
    monkeypatch.setattr("personal_bot.net.safe_url",resolve)
    monkeypatch.setattr(httpx.AsyncClient,"send",send)
    models=Models(service.s,service.connections,PublicHttp())
    with pytest.raises(BotError) as denied:
        await call(models,task)
    assert denied.value.code=="overage_unconfirmed"
    assert stages==["dns"]
    assert service.s.one("SELECT status FROM model_calls")["status"]=="not_sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting",["quota-lock","dns","network"])
async def test_remaining_activity_timeout_includes_entire_generation_wait(service,monkeypatch,waiting):
    connection=plan(service)
    approve(service,connection,max_active_seconds=1)
    accept(service)
    task=service.claim("runtime-test")
    calls=[]
    cancelled=asyncio.Event()
    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    if waiting=="dns":
        async def resolve(url):
            await blocked()
        monkeypatch.setattr("personal_bot.net.safe_url",resolve)
        http=PublicHttp()
    elif waiting=="network":
        async def handler(request):
            calls.append(request)
            await blocked()
        http=PublicHttp(httpx.MockTransport(handler))
    else:
        http=PublicHttp(httpx.MockTransport(lambda request:(calls.append(request),response())[1]))
    models=Models(service.s,service.connections,http)
    if waiting=="quota-lock":
        lock=asyncio.Lock()
        await lock.acquire()
        models.group_locks["connection:"+connection["id"]]=lock
    started=time.monotonic()
    with pytest.raises(BotError) as denied:
        await asyncio.wait_for(call(models,task),3)
    elapsed=time.monotonic()-started
    assert denied.value.code in ("overage_budget_exhausted","task_budget_exhausted")
    assert .8 <= elapsed < 2.5
    assert len(calls)==(1 if waiting=="network" else 0)
    assert 1 <= service.task(task["id"])["active_seconds"] < 2.5
    if waiting!="quota-lock":
        assert cancelled.is_set()


@pytest.mark.asyncio
async def test_first_request_plus_retry_backoff_cannot_spend_a_second_activity_budget(service):
    connection=plan(service)
    approve(service,connection,max_active_seconds=1)
    accept(service)
    task=service.claim("runtime-test")
    calls=[]
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.05)
        return httpx.Response(429,json={"error":{"code":"rate_limit_error"}})
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handler)))
    started=time.monotonic()
    with pytest.raises(BotError) as denied:
        await asyncio.wait_for(call(models,task),3)
    assert denied.value.code=="overage_budget_exhausted"
    assert len(calls)==1
    assert .8 <= time.monotonic()-started < 2.5
    assert 1 <= service.task(task["id"])["active_seconds"] < 2.5
    assert len(service.s.all("SELECT id FROM model_calls"))==1


@pytest.mark.asyncio
@pytest.mark.parametrize("counter,limit",[("token_count",100000),("active_seconds",300)])
@pytest.mark.parametrize("after_prepare",[False,True])
async def test_tool_dispatch_rechecks_cumulative_budget_after_model_or_prepare(service,monkeypatch,counter,limit,after_prepare):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    runner=ToolRunner(service)
    executions=[]
    async def execute(*args):
        executions.append(args)
        return {"items":[]}
    monkeypatch.setattr(runner,"execute",execute)
    def exhaust():
        with service.s.tx() as db:
            db.execute("UPDATE tasks SET "+counter+"=? WHERE id=?",(limit,task["id"]))
    if after_prepare:
        prepare=runner.prepare
        def exhaust_after_prepare(request):
            prepared=prepare(request)
            exhaust()
            return prepared
        monkeypatch.setattr(runner,"prepare",exhaust_after_prepare)
    else:
        exhaust()
    result=await runner.invoke(body(task))
    assert result["halted"] is True and executions==[]
    assert service.task(task["id"])["status"]=="paused"
    assert service.task(task["id"])["tool_count"]==0
    assert not service.s.all("SELECT id FROM tool_calls WHERE status='dispatched'")


@pytest.mark.asyncio
async def test_completed_tool_cache_does_not_reexecute_after_budget_and_grant_expire(service):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    runner=ToolRunner(service)
    first=await runner.invoke(body(task))
    with service.s.tx() as db:
        db.execute("UPDATE tasks SET token_count=max_tokens,active_seconds=max_active_seconds WHERE id=?",(task["id"],))
    service.connections.revoke_overage(connection["id"])
    cached=await runner.invoke(body(task))
    assert cached["cached"] is True and cached["result"]==first["result"]
    assert service.task(task["id"])["tool_count"]==1


def chat_connection(service,protocol="chat_completions"):
    connection=service.connections.save({"provider":"custom","billing_mode":"api","base_url":"https://model.example/v1",
        "protocol":protocol,"model_id":"chat-only","api_key":"test-only","entitlements":{
            "agent":{"status":"documented_restricted"},"backend":{"status":"not_applicable"},"chat":{"status":"not_applicable"}}})
    with service.s.tx() as db:
        db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(dumps({"text":"passed","tools":"not_tested"}),connection["id"]))
        service.s.setmeta("default_chat_connection",connection["id"],db)
    return service.connections.get(connection["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol",["chat_completions","responses"])
async def test_chat_mode_rejects_unsolicited_streamed_tool_calls_and_cannot_enable_tools(service,protocol):
    chat_connection(service,protocol)
    accept(service,execution_mode="chat")
    task=service.claim("runtime-test")
    requests=[]
    def malicious(request):
        requests.append(json.loads(request.content))
        arguments=dumps({"name":"unwanted.txt","content":"must not execute"})
        if protocol=="responses":
            event={"type":"response.completed","response":{"status":"completed","output":[{
                "type":"function_call","id":"fc","call_id":"evil","name":"write_file","arguments":arguments}],"usage":{"total_tokens":10}}}
        else:
            event={"choices":[{"delta":{"tool_calls":[{"index":0,"id":"evil","type":"function","function":{
                "name":"write_file","arguments":arguments}}]},"finish_reason":"tool_calls"}],"usage":{"total_tokens":10}}
        return httpx.Response(200,content="data: "+dumps(event)+"\n\ndata: [DONE]\n\n")
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(malicious)))
    with pytest.raises(BotError) as denied:
        await models.task_call(task["id"],task["attempt_id"],"runtime-test",task["revision"],"0",
                               [{"role":"user","content":"Reply only"}],TOOLS)
    assert denied.value.code=="chat_tool_calls_forbidden"
    assert len(requests)==1 and "tools" not in requests[0]
    assert service.s.all("SELECT id FROM tool_calls")==[]
    assert service.s.all("SELECT task_id FROM model_steps")==[]
    runner=ToolRunner(service)
    actual_model_call=service.s.one("SELECT id FROM model_calls")["id"]
    with pytest.raises(BotError) as denied:
        await runner.invoke({**body(task,"write_file",{"name":"unwanted.txt","content":"must not execute"}),
                             "model_call_id":actual_model_call,"call_id":"evil"})
    assert denied.value.code=="chat_tool_forbidden"
    assert service.s.all("SELECT id FROM tool_calls")==[]
    assert service.s.all("SELECT id FROM files")==[]


@pytest.mark.asyncio
async def test_chat_allows_exact_attachment_preprocessing_but_denies_model_read_file(service,monkeypatch):
    chat_connection(service)
    file=await Resources(service).upload(UploadFile(filename="test.csv",file=io.BytesIO(b"item,qty\nalpha,2\n")))
    accept(service,execution_mode="chat",file_ids=[file["id"]])
    task=service.claim("runtime-test")
    runner=ToolRunner(service)
    parses=[]
    async def parse(**kwargs):
        parses.append(kwargs)
        return {"status":"succeeded","text":"item,qty\nalpha,2\n","documents":[]}
    monkeypatch.setattr("personal_bot.sandbox.execute",parse)
    arguments={"file_id":file["id"],"limit":20000}
    with pytest.raises(BotError) as denied:
        await runner.invoke({**body(task,"read_file",arguments),"call_id":file["id"]})
    assert denied.value.code=="chat_tool_forbidden" and parses==[]
    result=await runner.invoke({**body(task,"read_file",arguments),"call_id":file["id"],
                                "model_call_id":"attachment-preprocess:"+str(task["context_generation"])})
    assert result["status"]=="succeeded" and "alpha,2" in result["result"]["text"]
    assert len(parses)==1 and parses[0]["action"]=="parse"
    assert parses[0]["input_files"][0]["id"]==file["id"]
    assert service.task(task["id"])["tool_count"]==1
