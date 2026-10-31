import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from conftest import assertion
from personal_bot.api import create_app
from personal_bot.errors import BotError
from personal_bot.models import Models
from personal_bot.net import PublicHttp
from personal_bot.store import dumps
from personal_bot.tools import ToolRunner


def plan(service, *, state="enabled", kind="purchased_credits"):
    rights = {scope:{"status":"documented_supported","source_url":"https://vendor.example/plan",
                     "quote":"Interactive tools are supported."}
              for scope in ("chat","agent","backend","schedule","background")}
    connection = service.connections.save({"provider":"minimax","billing_mode":"m_plan",
        "model_id":"test-plan-model","api_key":"test-only-not-a-real-key",
        "provider_overage_state":state,"provider_overage_kind":kind,"entitlements":rights})
    with service.s.tx() as db:
        db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",
                   (dumps({"text":"passed","tools":"passed"}),connection["id"]))
        service.s.setmeta("default_chat_connection",connection["id"],db)
        service.s.setmeta("default_agent_connection",connection["id"],db)
    return service.connections.get(connection["id"])


def approval_body(connection, **extra):
    return {"expected_revision":connection["revision"],
            "expected_secret_revision":connection["secret_revision"],"kind":"purchased_credits",
            "acknowledge_purchased_credits":True,"expires_in_seconds":3600,**extra}


def approve(service, connection, **extra):
    return service.connections.approve_overage(connection["id"],approval_body(connection,**extra))


def accept(service, identity="first", **extra):
    return service.accept({"client_request_id":identity,"mode":"new","text":"Test only",**extra})


@pytest.mark.parametrize("state,kind",[("unknown","purchased_credits"),("enabled","postpaid"),
                                         ("enabled","unknown"),("confirmed_disabled","purchased_credits")])
def test_unknown_or_other_billing_rules_cannot_be_granted(service,state,kind):
    connection=plan(service,state=state,kind=kind)
    with pytest.raises(BotError) as error:
        approve(service,connection)
    assert error.value.code=="overage_ineligible"
    assert service.connections.get(connection["id"])["provider_overage_state"]==state
    assert service.connections.get(connection["id"])["provider_overage_kind"]==kind


@pytest.mark.parametrize("extra",[{"acknowledge_purchased_credits":False},{"expires_in_seconds":3601},
    {"expires_in_seconds":True},{"max_tasks":4},{"max_tools":9},{"max_tokens":100001},
    {"max_active_seconds":301},{"expected_revision":999},{"expected_secret_revision":999}])
def test_grant_requires_explicit_acknowledgement_current_versions_and_bounded_limits(service,extra):
    connection=plan(service)
    with pytest.raises(BotError):
        approve(service,connection,**extra)
    assert service.connections.get(connection["id"])["overage_approval"]["active"] is False


def test_atomic_three_task_admission_clamps_budgets_and_keeps_billing_truth(service):
    connection=plan(service)
    with pytest.raises(BotError) as denied:
        accept(service)
    assert denied.value.code=="overage_unconfirmed"
    granted=approve(service,connection)
    assert granted["provider_overage_state"]=="enabled"
    assert granted["provider_overage_kind"]=="purchased_credits"
    assert granted["overage_approval"]["can_create_task"]
    def submit(index):
        try:
            return accept(service,"concurrent-"+str(index))
        except BotError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes=list(executor.map(submit,range(6)))
    assert len([item for item in outcomes if isinstance(item,dict)])==3
    assert outcomes.count("overage_task_limit")==3
    assert len(service.s.all("SELECT id FROM tasks"))==3
    for task in service.s.all("SELECT * FROM tasks"):
        assert (task["max_tools"],task["max_tokens"],task["max_active_seconds"])==(8,100000,300)
        service.connections.authorize_task_call(connection,task)
    status=service.connections.get(connection["id"])["overage_approval"]
    assert status["active"] is True and status["can_create_task"] is False
    assert status["remaining_tasks"]==0 and status["tasks_used"]==3
    assert "task_ids" not in status and "policy" not in status


def test_idempotent_request_does_not_consume_another_slot_and_failed_transaction_rolls_back(service):
    connection=plan(service)
    approve(service,connection,max_tasks=2,max_tools=3,max_tokens=1234,max_active_seconds=25)
    first=accept(service)
    assert accept(service)==first
    with pytest.raises(BotError):
        accept(service,"missing-file",file_ids=["missing"])
    status=service.connections.get(connection["id"])["overage_approval"]
    assert status["tasks_used"]==1 and status["remaining_tasks"]==1
    task=service.task(first["task_id"])
    assert (task["max_tools"],task["max_tokens"],task["max_active_seconds"])==(3,1234,25)
    assert service.connections.cap_task_budgets(task,{"max_tools":99,"max_tokens":999999,"max_active_seconds":999})=={
        "max_tools":3,"max_tokens":1234,"max_active_seconds":25}


@pytest.mark.parametrize("invalidation",["expired","revoked","secret","model","rights","policy","reissued","epoch"])
def test_live_revocation_and_config_changes_block_previously_registered_task(service,invalidation):
    connection=plan(service)
    approve(service,connection)
    receipt=accept(service)
    task=service.task(receipt["task_id"])
    if invalidation=="revoked":
        service.connections.revoke_overage(connection["id"])
    elif invalidation=="reissued":
        approve(service,connection)
    elif invalidation=="expired":
        with service.s.tx() as db:
            key=service.connections.approval_key(connection["id"])
            value=json.loads(service.s.meta(key,db=db));value["expires_unix"]=time.time()-1
            service.s.setmeta(key,dumps(value),db)
    elif invalidation=="secret":
        with service.s.tx() as db:
            db.execute("UPDATE credentials SET secret_revision=secret_revision+1 WHERE id=?",(connection["credential_id"],))
    elif invalidation=="model":
        with service.s.tx() as db:
            db.execute("UPDATE model_connections SET model_id='changed' WHERE id=?",(connection["id"],))
    elif invalidation=="rights":
        with service.s.tx() as db:
            db.execute("UPDATE billing_entries SET entitlements=? WHERE id=?",(dumps({}),connection["billing_entry_id"]))
    elif invalidation=="epoch":
        service.s.config=replace(service.s.config,deployment_epoch="new-epoch-after-restore")
    else:
        with service.s.tx() as db:
            db.execute("UPDATE billing_entries SET overage_state='unknown' WHERE id=?",(connection["billing_entry_id"],))
    with pytest.raises(BotError):
        service.connections.authorize_task_call(connection,task)


def test_historical_disabled_snapshot_cannot_bypass_new_live_policy(service):
    old=plan(service,state="confirmed_disabled",kind="none")
    service.connections.allowed(old,"agent")
    with service.s.tx() as db:
        db.execute("UPDATE billing_entries SET overage_state='enabled',overage_kind='purchased_credits' WHERE id=?",
                   (old["billing_entry_id"],))
    with pytest.raises(BotError) as denied:
        service.connections.allowed(old,"agent")
    assert denied.value.code=="overage_unconfirmed"


def test_grant_never_authorizes_scheduled_tasks_or_unregistered_existing_tasks(service):
    connection=plan(service)
    approve(service,connection)
    with pytest.raises(BotError) as denied:
        with service.s.tx() as db:
            service.new_task(db,"scheduled",schedule="schedule-id")
    assert denied.value.code=="overage_background_forbidden"
    assert service.connections.get(connection["id"])["overage_approval"]["tasks_used"]==0
    ordinary=service.s.all("SELECT * FROM tasks")
    assert ordinary==[]
    with pytest.raises(BotError) as denied:
        service.connections.authorize_task_call(connection,{"id":"old-task","schedule_id":None,"execution_mode":"agent"})
    assert denied.value.code=="overage_task_not_approved"


@pytest.mark.parametrize("counter,value",[("token_count",100000),("active_seconds",300)])
def test_original_approval_caps_survive_task_budget_increases(service,counter,value):
    connection=plan(service)
    approve(service,connection)
    task=service.task(accept(service)["task_id"])
    task.update(max_tools=1000,max_tokens=10000000,max_active_seconds=86400)
    task[counter]=value
    with pytest.raises(BotError) as denied:
        service.connections.authorize_task_call(connection,task)
    assert denied.value.code=="overage_budget_exhausted"


@pytest.mark.asyncio
async def test_eighth_tool_allows_final_model_answer_but_ninth_dispatch_is_blocked(service):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    runner=ToolRunner(service)
    envelope={"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test",
              "revision":task["revision"],"name":"memory_search","arguments":{"query":"test"}}
    for index in range(8):
        result=await runner.invoke({**envelope,"model_call_id":"model-"+str(index),"call_id":"call-"+str(index)})
        assert result["status"]=="succeeded"
    assert service.task(task["id"])["tool_count"]==8
    def handle(request):
        return httpx.Response(200,content='data: {"choices":[{"delta":{"content":"Final verified answer"},"finish_reason":"stop"}],"usage":{"total_tokens":10}}\n\ndata: [DONE]\n\n')
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    result=await models.task_call(task["id"],task["attempt_id"],"runtime-test",task["revision"],"final",
                                  [{"role":"user","content":"Summarize the completed tools"}],[])
    assert result["text"]=="Final verified answer"
    denied=runner.prepare({**envelope,"model_call_id":"ninth","call_id":"ninth"})
    assert denied["halted"] is True
    assert len(service.s.all("SELECT id FROM tool_calls"))==8


def test_continue_reuses_slot_rerun_consumes_slot_and_cannot_switch_billing(service):
    connection=plan(service)
    approve(service,connection,max_tasks=2)
    task=service.task(accept(service)["task_id"])
    task=service.command(task["id"],{"command_id":"pause","expected_revision":task["revision"],"action":"pause"})
    task=service.command(task["id"],{"command_id":"continue","expected_revision":task["revision"],"action":"continue"})
    assert service.connections.get(connection["id"])["overage_approval"]["tasks_used"]==1
    ordinary=service.connections.save({"provider":"custom","base_url":"https://other.example/v1",
                                       "model_id":"another-model","api_key":"test-only"})
    with service.s.tx() as db:
        db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",
                   (dumps({"text":"passed","tools":"passed"}),ordinary["id"]))
        service.s.setmeta("default_agent_connection",ordinary["id"],db)
    with pytest.raises(BotError) as denied:
        service.command(task["id"],{"command_id":"switch","expected_revision":task["revision"],
                                    "action":"continue","connection_id":ordinary["id"]})
    assert denied.value.code=="overage_task_not_approved"
    rerun=service.command(task["id"],{"command_id":"rerun","expected_revision":task["revision"],"action":"rerun"})
    assert rerun["connection_id"]==connection["id"]
    assert service.connections.get(connection["id"])["overage_approval"]["tasks_used"]==2
    with pytest.raises(BotError) as denied:
        service.command(task["id"],{"command_id":"rerun-again","expected_revision":task["revision"],"action":"rerun"})
    assert denied.value.code=="overage_task_limit"


@pytest.mark.asyncio
async def test_revocation_during_retry_prevents_second_provider_request(service,monkeypatch):
    connection=plan(service)
    approve(service,connection)
    accept(service)
    task=service.claim("runtime-test")
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(429,json={"error":{"code":"rate_limit_error"}})
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    async def revoke_instead_of_sleep(seconds):
        service.connections.revoke_overage(connection["id"])
    monkeypatch.setattr(asyncio,"sleep",revoke_instead_of_sleep)
    with pytest.raises(BotError) as denied:
        await models.task_call(task["id"],task["attempt_id"],"runtime-test",task["revision"],"0",
                               [{"role":"user","content":"small test"}],[])
    assert denied.value.code=="overage_unconfirmed" and len(calls)==1
    assert len(service.s.all("SELECT id FROM model_calls"))==1


@pytest.mark.asyncio
async def test_approval_endpoints_require_owner_and_return_redacted_status(configuration):
    config,private=configuration
    app=create_app(config)
    async with app.router.lifespan_context(app):
        connection=plan(app.state.service)
        path="/api/model-connections/"+connection["id"]+"/overage-approval"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            for method in ("POST","DELETE"):
                unauthenticated=await client.request(method,path,json=approval_body(connection))
                assert unauthenticated.status_code==401
                control=await client.request(method,path,json=approval_body(connection),headers={
                    "X-Personal-Bot-Assertion":assertion(private,method,path,role="control")})
                assert control.status_code==401
            granted=await client.post(path,json=approval_body(connection),headers={
                "X-Personal-Bot-Assertion":assertion(private,"POST",path)})
            assert granted.status_code==200
            payload=granted.json()
            assert payload["overage_approval"]["active"] is True
            assert "api_key" not in payload and "secret" not in payload
            assert "test-only-not-a-real-key" not in granted.text
            revoked=await client.delete(path,headers={"X-Personal-Bot-Assertion":assertion(private,"DELETE",path)})
            assert revoked.status_code==200
            assert revoked.json()["overage_approval"]["reason"]=="revoked"
