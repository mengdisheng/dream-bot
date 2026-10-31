import sqlite3

import httpx
import pytest

from conftest import assertion
from personal_bot.api import create_app
from personal_bot.errors import BotError
from personal_bot.service import page_cursor
from personal_bot.store import dumps


STAMP = "2026-10-31T10:00:00Z"


def seed_tasks(store, count, *, content_size=0):
    with store.tx() as db:
        db.executemany(
            "INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?)",
            [(f"task-{i:04}",f"任务 {i}","需求"*content_size,"succeeded",STAMP,STAMP,"结果"*content_size) for i in range(count)],
        )


def test_summary_is_bounded_and_never_reads_task_bodies(service):
    seed_tasks(service.s,500,content_size=2000)
    with service.s.tx() as db:
        db.execute("UPDATE tasks SET wait_reason=?",("等待"*1000,))
        db.execute("INSERT INTO model_calls(id,task_id,connection_id,connection_revision,status,usage,started_at) VALUES(?,?,?,?,?,?,?)",
                   ("model","task-0499","test",1,"unknown","null",STAMP))
        db.execute("INSERT INTO tool_calls(id,task_id,attempt_id,task_revision,name,arguments,binding_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("tool","task-0499","attempt",1,"read_url","{}","binding","unknown",STAMP))
    original_activity = service.s.meta("last_activity")
    statements = []
    def authorizer(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "tasks" and column in {"prompt","result","summary"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    service.s.db.set_authorizer(authorizer)
    service.s.db.set_trace_callback(statements.append)
    try:
        first = service.task_summaries()
    finally:
        service.s.db.set_authorizer(None)
        service.s.db.set_trace_callback(None)
    assert len(statements)==1
    assert len(first["items"])==50 and first["has_more"] is True
    assert first["items"][0]["id"]=="task-0499"
    assert first["items"][0]["token_usage_estimated"] is True
    assert first["items"][0]["unresolved_effect_count"]==1
    assert len(dumps(first).encode())<=64*1024
    assert all(not {"prompt","result","summary"}.intersection(item) for item in first["items"])
    with service.s.tx() as db:
        db.execute("UPDATE tasks SET prompt=?,result=?",("大"*20000,"正文"*20000))
    assert service.task_summaries()==first
    assert service.s.meta("last_activity")==original_activity


def test_task_keyset_with_tied_timestamps_and_concurrent_newer_task(service):
    seed_tasks(service.s,117)
    first=service.task_summaries(limit=17)
    pages=[first]
    with service.s.tx() as db:
        db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   ("new-task","新增","正文","succeeded","2026-10-31T11:00:00Z",STAMP))
    while pages[-1]["has_more"]:
        pages.append(service.task_summaries(limit=17,cursor=pages[-1]["next_cursor"]))
    ids=[item["id"] for page in pages for item in page["items"]]
    assert ids==[f"task-{i:04}" for i in reversed(range(117))]
    assert pages[-1]["next_cursor"] is None
    plan=service.s.all("EXPLAIN QUERY PLAN SELECT id FROM tasks WHERE (created_at,id)<(?,?) ORDER BY created_at DESC,id DESC LIMIT 51",(STAMP,"task-0050"))
    assert any("tasks_created_id" in row["detail"] for row in plan)


def test_message_pages_are_stable_scoped_and_passive(service):
    seed_tasks(service.s,2)
    with service.s.tx() as db:
        db.executemany("INSERT INTO messages VALUES(?,?,?,?,?)",
                       [(f"message-{i:04}","task-0000","assistant",str(i),STAMP) for i in range(123)])
    activity=service.s.meta("last_activity")
    page=service.message_page("task-0000")
    assert [m["content"] for m in page["items"]]==[str(i) for i in range(73,123)]
    chunks=[page["items"]]
    cursor=page["next_before"]
    with pytest.raises(BotError,match="分页游标"):
        service.message_page("task-0001",before=cursor)
    while page["has_more"]:
        page=service.message_page("task-0000",before=page["next_before"])
        chunks.insert(0,page["items"])
    assert [m["content"] for chunk in chunks for m in chunk]==[str(i) for i in range(123)]
    assert page["next_before"] is None
    assert service.s.meta("last_activity")==activity
    assert service.message_page("task-0001")=={"items":[],"has_more":False,"next_before":None}


@pytest.mark.parametrize("cursor",["", "***", "a"*1025, "e30", page_cursor("messages",STAMP,"id","task-0000"),
                                        page_cursor("tasks","not-a-date","id"),page_cursor("tasks","2026-10-31T10:00:00","id")])
def test_task_cursor_rejects_malformed_or_wrong_scope(service,cursor):
    with pytest.raises(BotError) as error:
        service.task_summaries(cursor=cursor)
    assert error.value.code=="invalid_cursor"


@pytest.mark.parametrize("limit",[0,-1,101,True,1.5])
def test_page_limits_are_bounded(service,limit):
    with pytest.raises(BotError) as error:
        service.task_summaries(limit=limit)
    assert error.value.code=="invalid_limit"


@pytest.mark.asyncio
async def test_http_paging_preserves_legacy_and_handles_long_interleaved_history(configuration):
    config,private=configuration
    app=create_app(config)
    async with app.router.lifespan_context(app):
        seed_tasks(app.state.store,2)
        with app.state.store.tx() as db:
            for i in range(1005):
                app.state.store.event(db,"task-0000","model.text_delta",{"text":str(i)})
                app.state.store.event(db,"task-0001","task.progress",{"i":i})
            for identity in ("z-message","a-message"):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(identity,"task-0000","assistant",identity,STAMP))
        before_activity=app.state.store.meta("last_activity")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            async def get(path,query=None):
                return await client.get(path,params=query,headers={"X-Personal-Bot-Assertion":assertion(private,"GET",path),"X-Personal-Bot-Passive":"1"})
            legacy=await get("/api/tasks")
            assert legacy.status_code==200
            assert set(legacy.json())=={"items"}
            assert "prompt" in legacy.json()["items"][0]
            assert legacy.json()["items"]==app.state.service.list_tasks()
            summaries=await get("/api/tasks",{"view":"summary","limit":1})
            assert summaries.status_code==200 and summaries.json()["has_more"] is True
            assert "prompt" not in summaries.json()["items"][0]
            detail=await get("/api/tasks/task-0000")
            assert detail.status_code==200 and "prompt" in detail.json()
            messages=await get("/api/messages",{"task_id":"task-0000"})
            assert set(messages.json())=={"items"}
            assert [m["id"] for m in messages.json()["items"]]==["z-message","a-message"]
            paged=await get("/api/messages",{"task_id":"task-0000","view":"page","limit":1})
            assert paged.json()["items"][0]["id"]=="z-message"
            next_page=await get("/api/messages",{"task_id":"task-0000","view":"page","limit":1,"before":paged.json()["next_before"]})
            assert next_page.json()["items"][0]["id"]=="a-message"
            path="/api/tasks/task-0000/history"
            history=await get(path)
            assert len(history.json()["items"])==1000 and history.json()["has_more"] is True
            after=0
            events=[]
            while True:
                response=await get(path,{"after":after,"limit":200})
                assert response.status_code==200,response.text
                page=response.json()
                events.extend(page["items"])
                after=page["next_after"]
                if not page["has_more"]:break
            assert len(events)==1005
            assert [e["seq"] for e in events]==list(range(1,2010,2))
            assert len({e["seq"] for e in events})==1005
            empty=(await get(path,{"after":after})).json()
            assert empty=={"items":[],"next_after":after,"has_more":False}
            for query in ({"after":-1},{"limit":0},{"limit":1001}):
                assert (await get(path,query)).status_code==400
            assert (await get("/api/tasks",{"view":"unknown"})).status_code==400
            assert (await get("/api/tasks",{"view":"summary","cursor":"bad"})).status_code==400
            assert (await get("/api/messages",{"task_id":"task-0000","view":"page","before":summaries.json()["next_cursor"]})).status_code==400
            assert (await get("/api/messages",{"task_id":"missing","view":"page"})).status_code==404
        assert app.state.store.meta("last_activity")==before_activity
