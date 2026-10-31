import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import assertion
from personal_bot.api import create_app
from personal_bot.errors import BotError
from personal_bot.store import dumps, now


def saved(service):
    return service.connections.get(service.s.meta("default_agent_connection"))


def add_connection(connections, **extra):
    return connections.save({"provider":"custom","billing_mode":"api","base_url":"https://model.example/v1",
                             "model_id":"mock","api_key":"test-delete-only",**extra})


def seed_task(store, connection, status="succeeded"):
    with store.tx() as db:
        db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,connection_id,connection_revision,model_id) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("history-task","history","retained evidence",status,now(),now(),connection["id"],connection["revision"],connection["model_id"]))
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",("history-message","history-task","assistant","retained response",now()))


def test_delete_clears_defaults_secrets_and_approval_but_preserves_history(service):
    connection=saved(service);identity=connection["id"];seed_task(service.s,connection)
    version=service.s.all("SELECT * FROM connection_versions WHERE connection_id=?",(identity,))
    with service.s.tx() as db:
        service.s.setmeta("default_chat_connection",identity,db)
        service.s.setmeta("overage_approval:"+identity,'{"revoked":true}',db)
        service.s.setmeta("overage_task:history-task",dumps({"connection_id":identity}),db)
        db.execute("INSERT INTO model_calls(id,task_id,connection_id,connection_revision,status,usage,started_at) VALUES(?,?,?,?,?,?,?)",
                   ("history-call","history-task",identity,connection["revision"],"unknown","null",now()))
        db.execute("INSERT INTO quota_groups VALUES(?,?,?,?,?)",("quota",connection["account_id"],"subscription","token","owner_configuration"))
        db.execute("INSERT INTO quota_bindings VALUES(?,?,?)",(identity,"quota",'["mock"]'))
        db.execute("INSERT INTO quota_windows VALUES(?,?,?,?,?,?,?)",("quota","monthly",None,None,"unknown",now(),0))
    result=service.connections.delete(identity,connection["revision"])
    assert result=={"deleted":True,"id":identity,"cleared_default_roles":["chat","agent"]}
    for table,column,value in (("model_connections","id",identity),("connection_details","connection_id",identity),
                               ("credentials","id",connection["credential_id"]),("model_profiles","id",connection["model_profile_id"]),
                               ("endpoints","id",connection["endpoint_id"]),("quota_bindings","connection_id",identity)):
        assert service.s.one(f"SELECT 1 FROM {table} WHERE {column}=?",(value,)) is None
    assert not service.s.meta("default_chat_connection") and not service.s.meta("default_agent_connection")
    assert not service.s.meta("overage_approval:"+identity) and not service.s.meta("overage_task:history-task")
    assert service.task("history-task")["prompt"]=="retained evidence"
    assert service.s.one("SELECT content FROM messages WHERE id='history-message'")["content"]=="retained response"
    assert service.s.one("SELECT status FROM model_calls WHERE id='history-call'")["status"]=="unknown"
    assert service.s.all("SELECT * FROM connection_versions WHERE connection_id=?",(identity,))==version
    assert "api_key" not in json.loads(version[0]["config"]) and "secret" not in json.loads(version[0]["config"])
    assert service.s.one("SELECT 1 FROM quota_groups WHERE id='quota'")
    assert service.s.one("SELECT 1 FROM quota_windows WHERE quota_group_id='quota'")
    with pytest.raises(BotError) as missing:service.connections.get(identity,private=True)
    assert missing.value.status==404


@pytest.mark.parametrize("status",["queued","running","paused","waiting_approval","waiting_model","needs_review"])
def test_unfinished_task_blocks_delete_without_mutating_connection(service,status):
    connection=saved(service);seed_task(service.s,connection,status)
    with pytest.raises(BotError) as blocked:service.connections.delete(connection["id"],connection["revision"])
    assert blocked.value.code=="connection_in_use" and blocked.value.status==409
    assert service.connections.get(connection["id"],private=True)["api_key"]=="test-key-only"
    assert service.s.meta("default_agent_connection")==connection["id"]


def test_dispatched_request_blocks_delete_even_after_task_cancelled(service):
    connection=saved(service);seed_task(service.s,connection,"cancelled")
    with service.s.tx() as db:
        db.execute("INSERT INTO model_calls(id,task_id,connection_id,connection_revision,status,started_at) VALUES(?,?,?,?,?,?)",
                   ("active-call","history-task",connection["id"],connection["revision"],"dispatched",now()))
    with pytest.raises(BotError) as blocked:service.connections.delete(connection["id"],connection["revision"])
    assert blocked.value.code=="connection_in_use"


def test_shared_credential_and_other_default_remain_usable_until_last_reference(service):
    connection=saved(service)
    other=add_connection(service.connections,credential_id=connection["credential_id"],account_id=connection["account_id"],
                         billing_entry_id=connection["billing_entry_id"],quota_bindings=[])
    with service.s.tx() as db:service.s.setmeta("default_chat_connection",other["id"],db)
    result=service.connections.delete(connection["id"],connection["revision"])
    assert result["cleared_default_roles"]==["agent"]
    assert service.s.meta("default_chat_connection")==other["id"]
    assert service.connections.get(other["id"],private=True)["api_key"]=="test-delete-only"
    assert service.s.one("SELECT 1 FROM endpoints WHERE id=?",(other["endpoint_id"],))
    assert service.s.one("SELECT 1 FROM billing_entries WHERE id=?",(connection["billing_entry_id"],))
    service.connections.delete(other["id"],other["revision"])
    assert service.s.one("SELECT 1 FROM credentials WHERE id=?",(connection["credential_id"],)) is None


def test_shared_profile_and_endpoint_are_not_removed(service):
    connection=saved(service);other=add_connection(service.connections)
    with service.s.tx() as db:
        db.execute("UPDATE connection_details SET credential_id=?,endpoint_id=?,model_profile_id=? WHERE connection_id=?",
                   (connection["credential_id"],connection["endpoint_id"],connection["model_profile_id"],other["id"]))
    service.connections.delete(connection["id"],connection["revision"])
    assert service.s.one("SELECT 1 FROM model_profiles WHERE id=?",(connection["model_profile_id"],))
    assert service.s.one("SELECT 1 FROM endpoints WHERE id=?",(connection["endpoint_id"],))
    assert service.connections.get(other["id"],private=True)["api_key"]=="test-key-only"


def test_delete_rechecks_revision_and_honors_maintenance(service):
    connection=saved(service)
    updated=service.connections.save({"expected_revision":connection["revision"],"display_name":"newer"},connection["id"])
    with pytest.raises(BotError) as stale:service.connections.delete(connection["id"],connection["revision"])
    assert stale.value.code=="revision_conflict"
    with service.s.tx() as db:service.s.setmeta("maintenance","active",db)
    with pytest.raises(BotError) as maintenance:service.connections.delete(updated["id"],updated["revision"])
    assert maintenance.value.code=="maintenance"
    assert service.connections.get(updated["id"])["display_name"]=="newer"


def test_delete_rolls_back_all_cleanup_on_database_failure(service):
    connection=saved(service)
    with service.s.tx() as db:
        db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON model_connections BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):service.connections.delete(connection["id"],connection["revision"])
    assert service.s.meta("default_agent_connection")==connection["id"]
    assert service.connections.get(connection["id"],private=True)["api_key"]=="test-key-only"


def test_edit_waiting_behind_delete_cannot_recreate_removed_connection(service,monkeypatch):
    import personal_bot.connections as module
    connection=saved(service);entered=threading.Event();resume=threading.Event()
    original=module.normalize_base
    def delayed_normalize(*args):
        result=original(*args);entered.set()
        assert resume.wait(5)
        return result
    monkeypatch.setattr(module,"normalize_base",delayed_normalize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(service.connections.save,{"expected_revision":connection["revision"],"display_name":"stale"},connection["id"])
        try:
            assert entered.wait(5)
            service.connections.delete(connection["id"],connection["revision"])
        finally:resume.set()
        with pytest.raises(BotError) as stale:future.result(timeout=5)
    assert stale.value.status==404
    assert service.s.one("SELECT 1 FROM model_connections WHERE id=?",(connection["id"],)) is None


def test_edit_waiting_behind_newer_save_cannot_overwrite_new_revision(service,monkeypatch):
    import personal_bot.connections as module
    connection=saved(service);entered=threading.Event();resume=threading.Event();owner_thread=threading.get_ident()
    original=module.normalize_base
    def delayed_normalize(*args):
        result=original(*args)
        if threading.get_ident()!=owner_thread:
            entered.set();assert resume.wait(5)
        return result
    monkeypatch.setattr(module,"normalize_base",delayed_normalize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(service.connections.save,{"expected_revision":connection["revision"],"display_name":"stale"},connection["id"])
        try:
            assert entered.wait(5)
            service.connections.save({"expected_revision":connection["revision"],"display_name":"newer"},connection["id"])
        finally:resume.set()
        with pytest.raises(BotError) as stale:future.result(timeout=5)
    assert stale.value.code=="revision_conflict"
    assert service.connections.get(connection["id"])["display_name"]=="newer"


@pytest.mark.asyncio
async def test_owner_delete_api_requires_authority_and_revision(configuration):
    config,private=configuration;app=create_app(config)
    async with app.router.lifespan_context(app):
        connection=add_connection(app.state.service.connections);path="/api/model-connections/"+connection["id"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            for headers in ({},{"X-Personal-Bot-Assertion":assertion(private,"DELETE",path,role="control")},
                            {"X-Personal-Bot-Assertion":assertion(private,"PATCH",path)}):
                denied=await client.delete(path+"?expected_revision=1",headers=headers)
                assert denied.status_code==401
            headers={"X-Personal-Bot-Assertion":assertion(private,"DELETE",path)}
            assert (await client.delete(path,headers=headers)).status_code==422
            assert (await client.delete(path+"?expected_revision=invalid",headers=headers)).status_code==422
            stale=await client.delete(path+"?expected_revision=2",headers=headers)
            assert stale.status_code==409 and stale.json()["error"]["code"]=="revision_conflict"
            deleted=await client.delete(path+"?expected_revision=1",headers=headers)
            assert deleted.status_code==200 and deleted.json()=={"deleted":True,"id":connection["id"],"cleared_default_roles":[]}
            assert (await client.delete(path+"?expected_revision=1",headers=headers)).status_code==404
            listed=await client.get("/api/model-connections",headers={"X-Personal-Bot-Assertion":assertion(private,"GET","/api/model-connections")})
            assert listed.json()=={"items":[]}


@pytest.mark.asyncio
async def test_real_validation_lease_prevents_delete_until_validation_finishes(configuration,monkeypatch):
    config,private=configuration;app=create_app(config);entered=asyncio.Event();resume=asyncio.Event()
    async def validate(identity):
        entered.set();await resume.wait()
        return {"checks":{},"errors":[],"validated_at":now()}
    async with app.router.lifespan_context(app):
        connection=add_connection(app.state.service.connections);path="/api/model-connections/"+connection["id"]
        monkeypatch.setattr(app.state.models,"validate",validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://api") as client:
            validating=asyncio.create_task(client.post(path+"/validate",headers={"X-Personal-Bot-Assertion":assertion(private,"POST",path+"/validate")}))
            try:
                await asyncio.wait_for(entered.wait(),5)
                response=await client.delete(path+"?expected_revision=1",headers={"X-Personal-Bot-Assertion":assertion(private,"DELETE",path)})
                assert response.status_code==409 and response.json()["error"]["code"]=="connection_in_use"
                assert app.state.service.connections.get(connection["id"])["has_api_key"] is True
            finally:resume.set()
            assert (await validating).status_code==200
            response=await client.delete(path+"?expected_revision=1",headers={"X-Personal-Bot-Assertion":assertion(private,"DELETE",path)})
            assert response.status_code==200
