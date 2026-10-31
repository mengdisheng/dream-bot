import asyncio
import dataclasses
import json
import sys
import types
from datetime import datetime,timedelta,timezone

import pytest

from personal_bot.errors import BotError
from personal_bot.service import Service
from personal_bot.service import next_schedule
from personal_bot.store import Store,now,uid
from personal_bot.tools import ToolRunner
from personal_bot.worker import Agent,IPC,IPCError
from personal_bot.api import create_app
from personal_bot.net import PublicHttp
import httpx
from test_api_graph import TestPeer


@pytest.mark.asyncio
async def test_one_way_drain_requires_all_outbox_acks_and_refuses_new_intake(service,monkeypatch):
    async def status():return {"mode":"idle"}
    async def quiesce():return {"closed":True}
    from personal_bot import browser
    monkeypatch.setattr(browser,"status",status)
    monkeypatch.setattr(browser,"quiesce",quiesce)
    with service.s.tx() as db:
        service.s.outbox(db,"notification",{"notification_id":"n1","task_id":"t1","category":"done"})
    async def no_ack():pass
    service.flush_outbox=no_ack
    proof=await service.quiesce({"drain_id":"drain-1","expected_intake_revision":1})
    assert not proof["safe_to_remove"] and proof["pending_outbox"]==1
    assert service.s.meta("intake")=="closed"
    with pytest.raises(BotError) as error:
        service.accept({"client_request_id":"late","mode":"new","text":"must not be accepted"})
    assert error.value.code=="runtime_draining"
    with service.s.tx() as db:
        db.execute("UPDATE outbox SET acked_at=?",(now(),))
    proof=await service.quiesce({"drain_id":"drain-1","expected_intake_revision":1})
    assert proof["safe_to_remove"] and proof["intake_revision"]==2
    with pytest.raises(BotError):
        await service.quiesce({"drain_id":"old-other-drain","expected_intake_revision":1})


@pytest.mark.asyncio
async def test_drain_gate_survives_same_runtime_restart_but_new_container_has_fresh_gate(configuration,monkeypatch):
    from personal_bot import browser
    async def status():return {"mode":"idle"}
    async def quiesce():return {"closed":True}
    monkeypatch.setattr(browser,"status",status)
    monkeypatch.setattr(browser,"quiesce",quiesce)
    config=configuration[0]
    first=Store(config)
    try:
        proof=await Service(first).quiesce({"drain_id":"generation-one","expected_intake_revision":1})
        assert proof["safe_to_remove"] and proof["intake_revision"]==2
    finally:
        first.close()
    restarted=Store(config)
    try:
        assert restarted.meta("intake")=="closed"
        assert restarted.meta("drain_id")=="generation-one"
        with pytest.raises(BotError) as error:
            Service(restarted).accept({"client_request_id":"late","mode":"new","text":"closed gate"})
        assert error.value.code=="runtime_draining"
        proof=await Service(restarted).quiesce({"drain_id":"generation-one","expected_intake_revision":1})
        assert proof["safe_to_remove"] and proof["intake_revision"]==2
    finally:
        restarted.close()
    recreated=Store(dataclasses.replace(config,runtime_id="runtime-next-container"))
    try:
        assert recreated.meta("intake")=="open" and recreated.meta("drain_id")==""
        assert recreated.meta("intake_revision")=="1"
        proof=await Service(recreated).quiesce({"drain_id":"generation-two","expected_intake_revision":1})
        assert proof["safe_to_remove"] and proof["drain_id"]=="generation-two"
    finally:
        recreated.close()


@pytest.mark.asyncio
async def test_already_accepted_work_wins_against_idle_drain(service,monkeypatch):
    async def status():return {"mode":"idle"}
    from personal_bot import browser
    monkeypatch.setattr(browser,"status",status)
    receipt=service.accept({"client_request_id":"first","text":"work","mode":"new"})
    result=await service.quiesce({"drain_id":"d","expected_intake_revision":1})
    assert result["safe_to_remove"] is False
    assert service.s.meta("intake")=="open"
    assert service.task(receipt["task_id"])["status"]=="queued"


def test_restore_marker_consumed_before_ready_and_revokes_old_work(service,configuration):
    receipt=service.accept({"client_request_id":"before-backup","text":"work","mode":"new"})
    task=service.claim("runtime-test")
    runner=ToolRunner(service)
    runner.prepare({"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test","revision":1,"model_call_id":"m","call_id":"c","name":"write_file","arguments":{"name":"a","content":"b"}})
    config=dataclasses.replace(configuration[0],deployment_epoch="restored-epoch",runtime_id="restored-runtime")
    marker=config.app_dir/".restore-required.json"
    marker.write_text(json.dumps({"deployment_epoch":"restored-epoch","maintenance_id":uid(),"backup_created_at":now()}))
    restored=Store(config)
    try:
        assert restored.reconciled
        assert not marker.exists()
        assert restored.task(receipt["task_id"])["status"]=="needs_review"
        assert restored.one("SELECT status FROM tool_calls")["status"]=="unknown"
        assert restored.one("SELECT COUNT(*) AS n FROM restore_audit")["n"]==1
        assert restored.one("SELECT COUNT(*) AS n FROM tasks WHERE status='queued'")["n"]==0
    finally:
        restored.close()


def test_shared_credentials_do_not_collapse_independent_quota_groups(service):
    original=service.connections.bind()
    updated=service.connections.save({"expected_revision":original["revision"],"quota_bindings":[{"quota_group_id":"general","unit":"credits","model_ids":["mock"]},{"quota_group_id":"hy","unit":"token","model_ids":["hy-model"]}]},original["id"])
    future=(datetime.now(timezone.utc)+timedelta(hours=5)).isoformat().replace("+00:00","Z")
    week=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat().replace("+00:00","Z")
    with service.s.tx() as db:
        for name,reset in (("five_hour",future),("week",week)):
            db.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)",("general",name,0,reset,"official_response",now(),1))
        db.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)",("hy","month",0,None,"owner_reported",now(),0))
    current=service.connections.get(original["id"])
    blocked=service.connections.quota_status(current)
    assert blocked["blocked"] and blocked["resume_at"]==week
    assert len(blocked["windows"])==2


def test_dst_nonexistent_time_is_skipped_and_repeated_time_uses_first_fold():
    before_gap=datetime(2026,3,8,5,0,tzinfo=timezone.utc)
    assert next_schedule("30 2 * * *","America/New_York",before_gap)=="2026-03-09T06:30:00Z"
    before_fold=datetime(2026,11,1,4,0,tzinfo=timezone.utc)
    assert next_schedule("30 1 * * *","America/New_York",before_fold)=="2026-11-01T05:30:00Z"
    after_first=datetime(2026,11,1,6,0,tzinfo=timezone.utc)
    assert next_schedule("30 1 * * *","America/New_York",after_first)=="2026-11-02T06:30:00Z"


@pytest.mark.asyncio
async def test_langgraph_resumes_approved_tool_without_repeating_model_call(configuration,monkeypatch):
    app=create_app(configuration[0])
    async with app.router.lifespan_context(app):
        s=app.state.store
        conn=app.state.service.connections.save({"provider":"custom","model_id":"mock","protocol":"chat_completions","base_url":"https://model.example/v1","api_key":"isolated-only"})
        with s.tx() as db:
            db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(json.dumps({"text":"passed","tools":"passed"}),conn["id"]))
            s.setmeta("default_agent_connection",conn["id"],db)
        model_calls=[]
        def handle(request):
            body=json.loads(request.content);model_calls.append(body)
            final=any(m.get("role")=="tool" for m in body["messages"])
            delta={"role":"assistant","content":"completed"} if final else {"role":"assistant","tool_calls":[{"index":0,"id":"click1","type":"function","function":{"name":"browser","arguments":json.dumps({"action":{"type":"click","ref":"ref1","observation_id":"obs1","browser_epoch":1},"site":"https://example.org","account":"owner"})}}]}
            return httpx.Response(200,content="data: "+json.dumps({"choices":[{"index":0,"delta":delta,"finish_reason":"stop" if final else "tool_calls"}]})+"\n\ndata: [DONE]\n\n")
        app.state.models.http=PublicHttp(httpx.MockTransport(handle))
        executions=[]
        async def execute(*args):executions.append(args);return {"receipt":"clicked-once"}
        app.state.tools.execute=execute
        receipt=app.state.service.accept({"client_request_id":"approval-task","mode":"new","text":"click after approval"})
        ipc=IPC(transport=httpx.ASGITransport(app=TestPeer(app)))
        try:
            first=(await ipc.call("claim",{"runtime_id":"runtime-test"}))["task"]
            with pytest.raises(IPCError) as error:
                await Agent(ipc,first).run()
            assert error.value.code=="stale_attempt"
            assert len(model_calls)==1 and executions==[]
            approval=s.one("SELECT * FROM approvals WHERE task_id=?",(receipt["task_id"],))
            app.state.service.decision(approval["id"],{"command_id":"approve","expected_revision":1,"decision":"approve"})
            second=(await ipc.call("claim",{"runtime_id":"runtime-test"}))["task"]
            result=await Agent(ipc,second).run()
            assert result["result"]=="completed"
            assert len(model_calls)==2 and len(executions)==1
            assert s.one("SELECT status FROM approvals")["status"]=="consumed"
        finally:
            await ipc.client.aclose()
