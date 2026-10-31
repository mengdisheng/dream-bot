import asyncio
import json

import pytest

from personal_bot.errors import BotError
from personal_bot.resources import Resources
from personal_bot.store import Store
from personal_bot.tools import ToolRunner


def queued(service,text="research"):
    receipt = service.accept({"client_request_id":"request-1","mode":"new","text":text})
    task = service.claim("runtime-test")
    return receipt,task


def tool_body(task,name="write_file",arguments=None):
    return {"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test","revision":task["revision"],"model_call_id":"model-call-1","call_id":"tool-1","name":name,"arguments":arguments or {"name":"r.txt","content":"result"}}


def test_receipt_is_durable_idempotent_and_content_bound(service):
    body = {"client_request_id":"same","mode":"new","text":"hello"}
    first = service.accept(body)
    assert service.accept(body) == first
    assert len(service.s.all("SELECT * FROM tasks")) == 1
    with pytest.raises(BotError,match="同一请求"):
        service.accept({**body,"text":"different"})


def test_cancel_commits_before_dispatch_blocks_action(service):
    receipt,task = queued(service)
    runner = ToolRunner(service)
    runner.prepare(tool_body(task))
    service.command(task["id"],{"command_id":"cancel","expected_revision":1,"action":"cancel"})
    with pytest.raises(BotError) as error:
        runner.prepare(tool_body(task))
    assert error.value.code == "stale_attempt"
    assert service.s.one("SELECT status FROM tool_calls")["status"] == "prepared"


@pytest.mark.asyncio
async def test_cancel_after_dispatch_records_late_evidence_without_resurrecting(service):
    receipt,task = queued(service)
    runner = ToolRunner(service)
    dispatched,finish = asyncio.Event(),asyncio.Event()
    async def execute(*args):
        dispatched.set()
        await finish.wait()
        return {"receipt":"external-123"}
    runner.execute = execute
    call = asyncio.create_task(runner.invoke(tool_body(task)))
    await dispatched.wait()
    service.command(task["id"],{"command_id":"cancel","expected_revision":1,"action":"cancel"})
    finish.set()
    result = await call
    assert result["status"] == "succeeded"
    assert service.task(task["id"])["status"] == "cancelled"
    assert service.list_tools(task["id"])[0]["result"]["receipt"] == "external-123"


def test_supplement_invalidates_pending_approval(service):
    _,task = queued(service)
    runner = ToolRunner(service)
    result = runner.prepare(tool_body(task,"browser",{"action":{"type":"click","selector":"button"},"site":"https://example.org","account":"owner"}))
    assert result["status"] == "waiting_approval"
    service.accept({"client_request_id":"supplement","mode":"supplement","task_id":task["id"],"text":"change recipient"})
    approval = service.s.one("SELECT * FROM approvals")
    assert approval["status"] == "invalidated"
    with pytest.raises(BotError):
        service.decision(approval["id"],{"command_id":"approve-old","expected_revision":1,"decision":"approve"})


def test_restart_marks_dispatched_unknown_and_task_needs_review(service,configuration):
    _,task = queued(service)
    runner = ToolRunner(service)
    prepared = runner.prepare(tool_body(task))
    with service.s.tx() as db:
        db.execute("UPDATE tool_calls SET status='dispatched' WHERE id=?",(prepared["id"],))
    recovered = Store(configuration[0])
    try:
        assert recovered.task(task["id"])["status"] == "needs_review"
        assert recovered.one("SELECT status FROM tool_calls")["status"] == "unknown"
    finally:
        recovered.close()


def test_unknown_effect_blocks_continue_even_after_cancel(service):
    _,task = queued(service)
    runner = ToolRunner(service)
    prepared = runner.prepare(tool_body(task))
    with service.s.tx() as db:
        db.execute("UPDATE tool_calls SET status='unknown' WHERE id=?",(prepared["id"],))
        db.execute("UPDATE tasks SET status='needs_review' WHERE id=?",(task["id"],))
    with pytest.raises(BotError) as error:
        service.command(task["id"],{"command_id":"continue","expected_revision":1,"action":"continue"})
    assert error.value.code == "reconciliation_required"


def test_memory_suppression_is_per_item_not_entire_source(service):
    resources = Resources(service)
    first = resources.memory({"content":"likes tea","source":"message-1","item_key":"drink"})
    resources.delete_memory(first["id"])
    assert resources.memory({"content":"likes tea","source":"message-1","item_key":"drink"},candidate=True) == {"suppressed":True}
    other = resources.memory({"content":"lives in Shanghai","source":"message-1","item_key":"city"},candidate=True)
    assert other["status"] == "candidate"
    assert resources.search_memories("tea") == []


def test_model_config_preserves_secret_on_omitted_patch_and_restricts_plans(service):
    connection = service.connections.bind()
    edited = service.connections.save({"expected_revision":connection["revision"],"display_name":"renamed"},connection["id"])
    assert edited["has_api_key"]
    assert service.connections.get(connection["id"],private=True)["api_key"] == "test-key-only"
    assert edited["capabilities"]["tools"] == "not_tested"
    restricted = service.connections.save({"provider":"zhipu","billing_mode":"coding_plan","protocol":"chat_completions","model_id":"glm","api_key":"test-only"})
    assert restricted["entitlements"]["backend"]["status"] == "documented_restricted"
    with pytest.raises(BotError) as error:
        service.connections.allowed(restricted,"agent")
    assert error.value.code == "entitlement_unconfirmed"
