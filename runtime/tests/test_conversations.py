import concurrent.futures
import io
import json
import sqlite3

import httpx
import pytest
from starlette.datastructures import UploadFile

from conftest import assertion
from personal_bot.api import create_app
from personal_bot.errors import BotError
from personal_bot.resources import Resources
from personal_bot.service import Service, page_cursor
from personal_bot.store import Store, dumps


STAMP = "2026-10-31T10:00:00Z"


def accept(service, identity, text="hello", **extra):
    return service.accept({"client_request_id":identity,"mode":"new","text":text,**extra})


def complete(service, task_id, result="answer"):
    task = service.claim("runtime-test")
    assert task["id"] == task_id
    service.finish({"task_id":task_id,"attempt_id":task["attempt_id"],"runtime_id":"runtime-test",
                    "status":"succeeded","result":result})
    return task


def test_legacy_migration_preserves_individual_tasks_and_is_idempotent(configuration):
    config,_ = configuration
    store = Store(config)
    with store.tx() as db:
        for identity,parent in (("old-a",None),("old-b","old-a")):
            db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,parent_task_id,result) VALUES(?,?,?,?,?,?,?,?)",
                       (identity,identity,"original prompt","succeeded",STAMP,STAMP,parent,"original result"))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",("message-"+identity,identity,"assistant","original reply",STAMP))
        db.execute("DROP INDEX tasks_conversation_created_id")
        db.execute("ALTER TABLE tasks DROP COLUMN conversation_id")
        db.execute("DROP TABLE conversations")
    key = (config.app_dir/"master.key").read_bytes()
    store.close()
    for _ in range(2):
        migrated = Store(config)
        try:
            service = Service(migrated)
            assert [(row["id"],row["conversation_id"]) for row in migrated.all("SELECT id,conversation_id FROM tasks ORDER BY id")] == [("old-a","old-a"),("old-b","old-b")]
            assert len(service.conversations()["items"]) == 2
            assert [m["content"] for m in service.conversation_messages("old-a")["items"]] == ["original reply"]
            assert migrated.task("old-b")["parent_task_id"] == "old-a"
            assert migrated.task("old-b")["prompt"] == "original prompt"
            assert migrated.task("old-b")["result"] == "original result"
            assert (config.app_dir/"master.key").read_bytes() == key
        finally:
            migrated.close()


@pytest.mark.asyncio
async def test_followup_keeps_conversation_history_files_and_readonly_evidence(service):
    resources = Resources(service)
    file = await resources.upload(UploadFile(file=io.BytesIO(b"explicitly attached"),filename="notes.txt"))
    first = accept(service,"first","remember this",file_ids=[file["id"]])
    first_task = complete(service,first["task_id"],"remembered answer")
    with service.s.tx() as db:
        db.execute("INSERT INTO tool_calls(id,task_id,attempt_id,task_revision,name,arguments,binding_hash,status,result,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("old-tool",first["task_id"],first_task["attempt_id"],1,"read_url","{}","binding","succeeded",dumps({"text":"old page evidence"}),STAMP))
        db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)",("old-approval",first["task_id"],1,"old-tool","binding","{}","approved",STAMP,STAMP))
    other = accept(service,"other","other conversation")
    complete(service,other["task_id"],"unrelated answer")
    second = accept(service,"second","what did I say?",conversation_id=first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]
    assert second["task_id"] != first["task_id"]
    assert service.task(second["task_id"])["parent_task_id"] == first["task_id"]
    claimed = service.claim("runtime-test")
    assert [m["content"] for m in claimed["messages"]] == ["remember this","remembered answer","what did I say?"]
    assert [f["id"] for f in claimed["files"]] == [file["id"]]
    assert claimed["prior_turn_tools"][0]["task_id"] == first["task_id"]
    assert json.loads(claimed["prior_turn_tools"][0]["result"])["text"] == "old page evidence"
    assert claimed["completed_tools"] == []
    assert service.s.one("SELECT 1 FROM approvals WHERE task_id=?",(second["task_id"],)) is None
    assert service.conversation(first["conversation_id"])["latest_task_id"] == second["task_id"]
    assert len(service.conversation(first["conversation_id"])["tasks"]) == 2
    assert len(service.conversation_messages(first["conversation_id"])["items"]) == 3


@pytest.mark.asyncio
async def test_followup_does_not_regrant_deleted_or_unrelated_files(service):
    resources = Resources(service)
    removed = await resources.upload(UploadFile(file=io.BytesIO(b"removed"),filename="removed.txt"))
    unrelated = await resources.upload(UploadFile(file=io.BytesIO(b"unrelated"),filename="unrelated.txt"))
    first = accept(service,"a",file_ids=[removed["id"]])
    complete(service,first["task_id"])
    resources.delete_file(removed["id"])
    resources.restore_file(removed["id"])
    other = accept(service,"b",file_ids=[unrelated["id"]])
    complete(service,other["task_id"])
    second = accept(service,"c",conversation_id=first["conversation_id"])
    assert service.claim("runtime-test")["files"] == []
    assert service.s.all("SELECT * FROM task_file_grants WHERE task_id=?",(second["task_id"],)) == []


def test_busy_conversation_rejects_new_execution_but_accepts_supplement(service):
    first = accept(service,"first")
    with pytest.raises(BotError) as error:
        accept(service,"new-too-soon",conversation_id=first["conversation_id"])
    assert error.value.code == "conversation_busy"
    second = service.accept({"client_request_id":"supplement","mode":"supplement","text":"new detail",
                             "conversation_id":first["conversation_id"],"task_id":first["task_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["task_revision"] == 2
    other = accept(service,"independent")
    with pytest.raises(BotError) as error:
        service.accept({"client_request_id":"wrong","mode":"supplement","text":"wrong context",
                        "task_id":first["task_id"],"conversation_id":other["conversation_id"]})
    assert error.value.code == "conversation_mismatch"


def test_concurrent_followups_create_one_execution_and_receipt_is_idempotent(service):
    first = accept(service,"first")
    complete(service,first["task_id"])
    def send(identity):
        try:
            return accept(service,identity,conversation_id=first["conversation_id"])
        except BotError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send,["next-a","next-b"]))
    accepted = [item for item in results if isinstance(item,dict)]
    assert len(accepted) == 1
    assert results.count("conversation_busy") == 1
    receipt = accepted[0]
    assert accept(service,receipt["request_id"],conversation_id=first["conversation_id"]) == receipt
    with pytest.raises(BotError) as error:
        accept(service,receipt["request_id"],conversation_id="a-different-conversation")
    assert error.value.code == "idempotency_conflict"


def test_rerun_stays_in_conversation_and_unknown_effect_blocks_followup(service):
    first = accept(service,"first")
    complete(service,first["task_id"])
    rerun = service.command(first["task_id"],{"command_id":"rerun","expected_revision":1,"action":"rerun"})
    assert rerun["conversation_id"] == first["conversation_id"]
    service.command(rerun["id"],{"command_id":"cancel","expected_revision":1,"action":"cancel"})
    with service.s.tx() as db:
        db.execute("INSERT INTO tool_calls(id,task_id,attempt_id,task_revision,name,arguments,binding_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("unknown",rerun["id"],"attempt",1,"browser","{}","binding","unknown",STAMP))
    with pytest.raises(BotError) as error:
        accept(service,"unsafe-followup",conversation_id=first["conversation_id"])
    assert error.value.code == "reconciliation_required"


def test_conversation_and_message_pages_are_bounded_scoped_and_passive(service):
    with service.s.tx() as db:
        for i in range(123):
            identity = f"conversation-{i:04}"
            db.execute("INSERT INTO conversations VALUES(?,?,?,?)",(identity,"标题"*100,STAMP,STAMP))
            db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,result,conversation_id) VALUES(?,?,?,?,?,?,?,?)",
                       (f"task-{i:04}","title","body"*5000,"succeeded",STAMP,STAMP,"result"*5000,identity))
        for i in range(123):
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(f"m-{i:04}","task-0000","assistant",str(i),STAMP))
    activity = service.s.meta("last_activity")
    def authorizer(action,table,column,database,source):
        if action == sqlite3.SQLITE_READ and table == "tasks" and column in {"prompt","result","summary"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    service.s.db.set_authorizer(authorizer)
    try:
        page = service.conversations(limit=50)
        assert len(page["items"]) == 50 and len(dumps(page).encode()) < 64*1024
        assert page["items"][0]["id"] == "conversation-0122"
        pages = [page]
        while pages[-1]["has_more"]:
            pages.append(service.conversations(cursor=pages[-1]["next_cursor"]))
        assert [c["id"] for p in pages for c in p["items"]] == [f"conversation-{i:04}" for i in reversed(range(123))]
        assert service.conversation("conversation-0000")["tasks"][0]["id"] == "task-0000"
    finally:
        service.s.db.set_authorizer(None)
    page = service.conversation_messages("conversation-0000")
    assert [m["content"] for m in page["items"]] == [str(i) for i in range(73,123)]
    with pytest.raises(BotError):
        service.conversation_messages("conversation-0001",before=page["next_before"])
    with pytest.raises(BotError):
        service.conversation("conversation-0000",before=page["next_before"])
    chunks = [page["items"]]
    while page["has_more"]:
        page = service.conversation_messages("conversation-0000",before=page["next_before"])
        chunks.insert(0,page["items"])
    assert [m["content"] for chunk in chunks for m in chunk] == [str(i) for i in range(123)]
    assert service.s.meta("last_activity") == activity


def test_conversation_task_pages_keep_all_executions(service):
    with service.s.tx() as db:
        db.execute("INSERT INTO conversations VALUES(?,?,?,?)",("conv","title",STAMP,STAMP))
        for i in range(105):
            db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,conversation_id) VALUES(?,?,?,?,?,?,?)",
                       (f"t-{i:04}","title","prompt","succeeded",STAMP,STAMP,"conv"))
    page = service.conversation("conv",limit=30)
    assert page["latest_task_id"] == "t-0104"
    chunks = [page["tasks"]]
    while page["has_more"]:
        page = service.conversation("conv",limit=30,before=page["next_before"])
        chunks.insert(0,page["tasks"])
    assert [t["id"] for chunk in chunks for t in chunk] == [f"t-{i:04}" for i in range(105)]
    for cursor in ("bad",page_cursor("conversations",STAMP,"conv")):
        with pytest.raises(BotError):
            service.conversation("conv",before=cursor)


@pytest.mark.asyncio
async def test_http_conversation_reads_and_durable_receipts(configuration):
    config,private = configuration
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        connection = service.connections.save({"provider":"custom","billing_mode":"api","base_url":"https://model.example/v1","protocol":"chat_completions","model_id":"mock","api_key":"test-key-only"})
        with service.s.tx() as db:
            db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(dumps({k:"passed" for k in ("auth","text","stream","tools")}),connection["id"]))
            service.s.setmeta("default_agent_connection",connection["id"],db)
        first = accept(service,"http")
        activity = service.s.meta("last_activity")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            async def get(path,params=None):
                return await client.get(path,params=params,headers={"X-Personal-Bot-Assertion":assertion(private,"GET",path),"X-Personal-Bot-Passive":"1"})
            assert (await get("/api/requests/http")).json() == first
            listing = await get("/api/conversations")
            assert listing.status_code == 200
            assert listing.json()["items"][0]["latest_task_id"] == first["task_id"]
            path = "/api/conversations/"+first["conversation_id"]
            detail = await get(path)
            assert detail.status_code == 200 and detail.json()["tasks"][0]["conversation_id"] == first["conversation_id"]
            assert (await get(path+"/messages")).json()["items"][0]["content"] == "hello"
            assert (await get("/api/conversations/missing")).status_code == 404
            assert (await get("/api/conversations",{"limit":0})).status_code == 400
            assert (await get(path+"/messages",{"before":"bad"})).status_code == 400
        assert service.s.meta("last_activity") == activity


@pytest.mark.asyncio
async def test_real_worker_followup_sees_history_and_preserves_per_task_checkpoints(configuration):
    from personal_bot.net import PublicHttp
    from personal_bot.worker import Agent, IPC
    from test_api_graph import TestPeer, mock_response

    config,_ = configuration
    app = create_app(config)
    async with app.router.lifespan_context(app):
        service = app.state.service
        connection = service.connections.save({"provider":"custom","billing_mode":"api","base_url":"https://model.example/v1","protocol":"chat_completions","model_id":"mock","api_key":"test-key-only"})
        with service.s.tx() as db:
            db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",(dumps({k:"passed" for k in ("auth","text","stream","tools")}),connection["id"]))
            service.s.setmeta("default_agent_connection",connection["id"],db)
        calls = []
        def model(request):
            calls.append(json.loads(request.content))
            return mock_response(calls[-1],tools_called=True)
        app.state.models.http = PublicHttp(httpx.MockTransport(model))
        ipc = IPC(transport=httpx.ASGITransport(app=TestPeer(app)))
        try:
            first = accept(service,"first","remember blue")
            task = (await ipc.call("claim",{"runtime_id":config.runtime_id}))["task"]
            agent = Agent(ipc,task)
            result = await agent.run()
            await ipc.call("finish",{**agent.envelope(),"status":"succeeded","result":result["result"]})
            old_checkpoints = service.s.all("SELECT checkpoint_id,checkpoint FROM checkpoints WHERE task_id=? ORDER BY checkpoint_id",(first["task_id"],))
            assert old_checkpoints
            second = accept(service,"second","what color?",conversation_id=first["conversation_id"])
            task = (await ipc.call("claim",{"runtime_id":config.runtime_id}))["task"]
            agent = Agent(ipc,task)
            result = await agent.run()
            assert len(calls) == 2
            assert [m["content"] for m in calls[-1]["messages"] if m["role"] != "system"] == ["remember blue","done","what color?"]
            assert service.s.all("SELECT checkpoint_id,checkpoint FROM checkpoints WHERE task_id=? ORDER BY checkpoint_id",(first["task_id"],)) == old_checkpoints
            assert service.s.one("SELECT COUNT(*) AS n FROM checkpoints WHERE task_id=?",(second["task_id"],))["n"] > 0
            # A graph that completed before final API commit keeps its native
            # checkpoint state and must not send a third provider request.
            recovered = await Agent(ipc,task).run()
            assert recovered["result"] == result["result"]
            assert len(calls) == 2
        finally:
            await ipc.client.aclose()
